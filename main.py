import os
import asyncio
import logging
import math
from typing import Any, Dict, List, Optional

import httpx
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, Application

# ============================================================
# Config & env
# ============================================================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
if not API_FOOTBALL_KEY:
    logging.error("API_FOOTBALL_KEY não configurada. Defina no ambiente.")
    # não damos exit aqui para permitir rodar sem API em ambiente de teste

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not TELEGRAM_BOT_TOKEN:
    try:
        TELEGRAM_BOT_TOKEN = input("Digite seu TELEGRAM_BOT_TOKEN (do BotFather) e pressione ENTER: ").strip()
    except EOFError:
        TELEGRAM_BOT_TOKEN = ""

TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BANKROLL_INITIAL = float(os.getenv("BANKROLL_INITIAL", "5000"))

WINDOW_START = int(os.getenv("WINDOW_START", "47"))
WINDOW_END = int(os.getenv("WINDOW_END", "75"))

MIN_ODD = float(os.getenv("MIN_ODD", "1.47"))
MAX_ODD = float(os.getenv("MAX_ODD", "2.30"))
EV_MIN_PCT = float(os.getenv("EV_MIN_PCT", "1.60"))  # em % (1.60 = 1,6%)

AUTOSTART = os.getenv("AUTOSTART", "0") == "1"
CHECK_INTERVAL_MS = int(os.getenv("CHECK_INTERVAL", "1500"))
CHECK_INTERVAL_SEC = max(5, CHECK_INTERVAL_MS / 1000)

USE_API_FOOTBALL_ODDS = os.getenv("USE_API_FOOTBALL_ODDS", "0") == "1"
BOOKMAKER_ID = os.getenv("BOOKMAKER_ID", "").strip() or None
BOOKMAKER_NAME = os.getenv("BOOKMAKER_NAME", "Superbet").strip()
BOOKMAKER_URL = os.getenv("BOOKMAKER_URL", "https://www.superbet.com/").strip()

LEAGUE_IDS_ENV = os.getenv("LEAGUE_IDS", "").strip()


def parse_league_ids(env_value: str) -> set[int]:
    ids: set[int] = set()
    if not env_value:
        return ids
    for part in env_value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logging.warning(f"[LEAGUE_IDS] Ignorando valor inválido: {part!r}")
    return ids


ALLOWED_LEAGUE_IDS = parse_league_ids(LEAGUE_IDS_ENV)
logging.info(
    "[LEAGUE_IDS] Permitidos: %s",
    sorted(ALLOWED_LEAGUE_IDS) if ALLOWED_LEAGUE_IDS else "TODOS (sem filtro)",
)

BLOCKED_KEYWORDS = [
    "u19",
    "u20",
    "u21",
    "u17",
    "juniores",
    "youth",
    "reserves",
    "reserve",
    " b ",
    " b-",
    " b)",
    " ii ",
    " iii ",
    "women",
    "feminine",
    "femenina",
    "feminino",
]

# Estado global simples para /status
LAST_STATUS_TEXT: str = "Nenhum scan executado ainda."
LAST_DEBUG_TEXT: str = "Sem debug ainda."


# ============================================================
# Núcleo do EvRadar PRO
# ============================================================


def is_fixture_allowed(fx: Dict[str, Any]) -> bool:
    league = fx.get("league") or {}
    league_id = league.get("id")
    league_name = (league.get("name") or "").lower()

    # 1) whitelist de LEAGUE_IDS
    if ALLOWED_LEAGUE_IDS:
        try:
            if int(league_id) not in ALLOWED_LEAGUE_IDS:
                return False
        except (TypeError, ValueError):
            return False

    # 2) proteção extra contra ligas bizarras
    for kw in BLOCKED_KEYWORDS:
        if kw in league_name:
            return False

    return True


def estimate_goal_prob(minute: int, goals_total: int) -> float:
    """
    Heurística simples para probabilidade de sair +1 gol até o fim.
    p = 1 - exp(-lambda), onde lambda depende do tempo restante e dos gols.
    """
    if minute is None:
        return 0.0

    minute = max(0, min(90, minute))
    time_left = max(0, 90 - minute)

    # base: quanto menos tempo, menor lambda
    base_lambda = 0.03 * time_left
    # jogo mais aberto → aumenta lambda
    base_lambda += 0.28 * goals_total

    base_lambda = max(0.05, min(base_lambda, 4.0))
    p = 1.0 - math.exp(-base_lambda)
    p = max(0.05, min(0.99, p))
    return p


def calcular_ev(prob_final: float, odd: float) -> float:
    """Retorna EV bruto (ex: 0.12 = 12%)."""
    return prob_final * odd - 1.0


def sugerir_stake_pct(ev_pct: float) -> float:
    """Tiering de stake aproximado em % da banca."""
    if ev_pct >= 7.0:
        return 3.0
    if ev_pct >= 5.0:
        return 2.5
    if ev_pct >= 1.5:
        return 1.5
    return 1.0


def montar_comentario_curto(minute: int, goals_home: int, goals_away: int) -> str:
    total_gols = goals_home + goals_away
    if minute < 55 and total_gols == 0:
        return "jogo começa a ganhar ritmo agora, ainda com tempo confortável para sair 1 gol."
    if total_gols == 0:
        return "pressão e necessidade de resultado aumentam, tendência de espaço para pelo menos 1 gol."
    if total_gols == 1:
        return "partida mais aberta após o gol, cenário bom para mais uma chegada perigosa virar gol."
    if total_gols >= 3:
        return "jogo totalmente aberto, defesas expostas e ritmo alto favorecendo mais um gol."
    return "ritmo e contexto indicam boa chance de 1 gol a mais dentro da janela."


async def fetch_live_fixtures() -> List[Dict[str, Any]]:
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    url = "https://v3.football.api-sports.io/fixtures"
    params = {"live": "all"}

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logging.exception("Erro ao buscar fixtures ao vivo: %s", e)
            return []

    raw_fixtures = data.get("response", []) or []
    fixtures: List[Dict[str, Any]] = []
    for fx in raw_fixtures:
        try:
            if not is_fixture_allowed(fx):
                continue
        except Exception as e:
            logging.exception("Erro em is_fixture_allowed: %s", e)
            continue
        fixtures.append(fx)

    logging.info(
        "[API-FOOTBALL] Fixtures ao vivo: %s | Após filtro de ligas: %s",
        len(raw_fixtures),
        len(fixtures),
    )
    return fixtures


async def fetch_fixture_odd(fixture_id: int, line: float) -> Optional[float]:
    """
    Busca a odd de Over (linha) na casa BOOKMAKER_ID via API-Football.
    Retorna float ou None se não encontrar.
    """
    if not USE_API_FOOTBALL_ODDS or not BOOKMAKER_ID:
        return None

    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    url = "https://v3.football.api-sports.io/odds"

    params = {
        "fixture": fixture_id,
        "bookmaker": BOOKMAKER_ID,
        "bet": 5,  # Over/Under FT
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logging.exception("Erro ao buscar odds do fixture %s: %s", fixture_id, e)
            return None

    alvo = f"over {line:.1f}".lower()
    for resp in data.get("response", []) or []:
        for book in resp.get("bookmakers", []) or []:
            for bet in book.get("bets", []) or []:
                bet_id = bet.get("id")
                if str(bet_id) != "5":
                    continue
                for val in bet.get("values", []) or []:
                    label = (val.get("value") or "").lower()
                    odd_str = val.get("odd")
                    if not odd_str:
                        continue
                    if alvo in label:
                        try:
                            return float(odd_str)
                        except ValueError:
                            continue
    return None


async def gerar_candidatos() -> List[Dict[str, Any]]:
    fixtures = await fetch_live_fixtures()
    candidatos: List[Dict[str, Any]] = []

    for fx in fixtures:
        try:
            fixture_info = fx.get("fixture") or {}
            teams = fx.get("teams") or {}
            goals = fx.get("goals") or {}
            league = fx.get("league") or {}

            minute = (fixture_info.get("status") or {}).get("elapsed")
            if minute is None:
                continue

            if minute < WINDOW_START or minute > WINDOW_END:
                continue

            home_goals = goals.get("home") or 0
            away_goals = goals.get("away") or 0
            total_goals = (home_goals or 0) + (away_goals or 0)

            # SUM_PLUS_HALF: linha = placar total + 0,5
            line_total = float(total_goals) + 0.5

            # Probabilidade de 1 gol a mais (modelo simplificado)
            p_final = estimate_goal_prob(minute, total_goals)
            odd_justa = 1.0 / max(p_final, 1e-6)

            # Odd real de mercado
            odd_mercado: Optional[float] = None
            if USE_API_FOOTBALL_ODDS:
                odd_mercado = await fetch_fixture_odd(fixture_info.get("id"), line_total)
                if odd_mercado is None:
                    continue
            else:
                odd_mercado = 1.70  # fallback de referência

            if odd_mercado < MIN_ODD or odd_mercado > MAX_ODD:
                continue

            ev_raw = calcular_ev(p_final, odd_mercado)
            ev_pct = ev_raw * 100.0

            if ev_pct < EV_MIN_PCT:
                continue

            stake_pct = sugerir_stake_pct(ev_pct)
            stake_reais = BANKROLL_INITIAL * (stake_pct / 100.0)

            home_name = (teams.get("home") or {}).get("name") or "Time da casa"
            away_name = (teams.get("away") or {}).get("name") or "Time visitante"
            league_name = league.get("name") or "Liga"

            comentario = montar_comentario_curto(minute, home_goals or 0, away_goals or 0)

            tier = "Tier A — Sinal EvRadar PRO"

            candidatos.append(
                {
                    "fixture_id": fixture_info.get("id"),
                    "home_team": home_name,
                    "away_team": away_name,
                    "league_name": league_name,
                    "minute": minute,
                    "home_goals": home_goals or 0,
                    "away_goals": away_goals or 0,
                    "total_goals": total_goals,
                    "line_total": line_total,
                    "p_final": p_final,
                    "odd_justa": odd_justa,
                    "odd_mercado": odd_mercado,
                    "ev_pct": ev_pct,
                    "stake_pct": stake_pct,
                    "stake_reais": stake_reais,
                    "comentario": comentario,
                    "tier": tier,
                }
            )
        except Exception as e:
            logging.exception("Erro ao gerar candidato: %s", e)
            continue

    candidatos.sort(key=lambda c: c["ev_pct"], reverse=True)
    return candidatos


def formatar_alerta(cand: Dict[str, Any]) -> str:
    linha_msg = (
        "⚙️ Linha: Over "
        + f"{cand['line_total']:.1f}"
        + " (soma + 0,5) @ "
        + f"{cand['odd_mercado']:.2f}"
    )

    prob_linha = "- P_final (gol a mais): " + f"{cand['p_final']*100:.1f}%"
    odd_justa_linha = "- Odd justa (modelo): " + f"{cand['odd_justa']:.2f}"
    ev_linha = "- EV: " + f"{cand['ev_pct']:.2f}% → " + ("EV+" if cand["ev_pct"] >= 0 else "EV-")

    stake_linha = (
        "💰 Stake sugerida: "
        + f"{cand['stake_pct']:.1f}%"
        + " da banca (~R$"
        + f"{cand['stake_reais']:.2f}"
        + ")"
    )

    header = "🔔 " + cand["tier"]

    linhas = [
        header,
        "",
        "🏟️ "
        + cand["home_team"]
        + " vs "
        + cand["away_team"]
        + " — "
        + cand["league_name"],
        f"⏱️ {cand['minute']}' | 🔢 {cand['home_goals']}–{cand['away_goals']}",
        linha_msg,
        "",
        "📊 Probabilidade & valor:",
        prob_linha,
        odd_justa_linha,
        ev_linha,
        "",
        stake_linha,
        "",
        "🧩 Interpretação:",
        cand["comentario"],
        "",
        "🔗 Abrir evento ("
        + BOOKMAKER_NAME
        + ") ("
        + BOOKMAKER_URL
        + ")",
    ]
    return "\n".join(linhas)


async def executar_scan(origin: str = "manual") -> Dict[str, Any]:
    global LAST_STATUS_TEXT, LAST_DEBUG_TEXT

    logging.info("[SCAN] Iniciando varredura (%s)...", origin)
    candidatos = await gerar_candidatos()

    resumo = (
        "[EvRadar PRO] Scan concluído (origem="
        + origin
        + "). Candidatos EV+: "
        + str(len(candidatos))
        + "."
    )
    LAST_STATUS_TEXT = resumo
    LAST_DEBUG_TEXT = resumo + " Detalhes internos não logados aqui."

    return {
        "candidatos": candidatos,
        "resumo": resumo,
    }


# ============================================================
# Telegram bot
# ============================================================


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    texto = (
        "👋 EvRadar PRO online.\n\n"
        "Janela padrão: "
        + f"{WINDOW_START}–{WINDOW_END}ʼ\n"
        + "Odds alvo (dinâmicas, via mercado): "
        + f"{MIN_ODD:.2f}–{MAX_ODD:.2f}\n"
        + "EV mínimo: "
        + f"{EV_MIN_PCT:.2f}%\n"
        + "Cooldown/global: baseado no intervalo de varredura.\n\n"
        "Comandos:\n"
        "  /scan   → rodar varredura agora\n"
        "  /status → ver último resumo\n"
        "  /debug  → info básica\n"
        "  /links  → links úteis / bookmaker\n"
    )
    await update.message.reply_text(texto)


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if TELEGRAM_CHAT_ID == "" else int(TELEGRAM_CHAT_ID)
    resultado = await executar_scan(origin="manual")
    candidatos = resultado["candidatos"]
    resumo = resultado["resumo"]

    if not candidatos:
        await context.bot.send_message(chat_id=chat_id, text=resumo)
        return

    for cand in candidatos:
        msg = formatar_alerta(cand)
        await context.bot.send_message(chat_id=chat_id, text=msg)

    await context.bot.send_message(chat_id=chat_id, text=resumo)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(LAST_STATUS_TEXT)


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    texto = (
        "🔧 Debug rápido EvRadar PRO\n\n"
        "WINDOW: "
        + f"{WINDOW_START}–{WINDOW_END}ʼ\n"
        + "ODDS: "
        + f"{MIN_ODD:.2f}–{MAX_ODD:.2f}\n"
        + "EV_MIN_PCT: "
        + f"{EV_MIN_PCT:.2f}%\n"
        + "USE_API_FOOTBALL_ODDS: "
        + str(int(USE_API_FOOTBALL_ODDS))
        + "\n"
        + "BOOKMAKER_ID: "
        + str(BOOKMAKER_ID)
        + "\n"
        + "LEAGUE_IDS: "
        + (LEAGUE_IDS_ENV or "N/A")
        + "\n\n"
        + LAST_DEBUG_TEXT
    )
    await update.message.reply_text(texto)


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    texto = (
        "🔗 Links úteis\n\n"
        "Casa principal: "
        + BOOKMAKER_NAME
        + " → "
        + BOOKMAKER_URL
        + "\n"
        "API-Football: https://www.api-football.com/\n"
    )
    await update.message.reply_text(texto)


async def auto_scan_loop(app: Application) -> None:
    if not AUTOSTART:
        logging.info("[AUTO-SCAN] AUTOSTART=0, loop automático desativado.")
        return

    chat_id_env = TELEGRAM_CHAT_ID.strip()
    if not chat_id_env:
        logging.warning(
            "[AUTO-SCAN] TELEGRAM_CHAT_ID não configurado. Auto-scan não enviará mensagens."
        )

    logging.info(
        "[AUTO-SCAN] Loop automático iniciado. Intervalo: %ss.",
        CHECK_INTERVAL_SEC,
    )

    while True:
        try:
            resultado = await executar_scan(origin="auto")
            candidatos = resultado["candidatos"]
            resumo = resultado["resumo"]

            if chat_id_env:
                chat_id = int(chat_id_env)
                for cand in candidatos:
                    msg = formatar_alerta(cand)
                    await app.bot.send_message(chat_id=chat_id, text=msg)
                await app.bot.send_message(chat_id=chat_id, text=resumo)
        except Exception as e:
            logging.exception("[AUTO-SCAN] Erro no loop automático: %s", e)

        await asyncio.sleep(CHECK_INTERVAL_SEC)


async def post_init(application: Application) -> None:
    if AUTOSTART:
        application.create_task(auto_scan_loop(application))


async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN não definido. Encerrando.")
        return

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("debug", cmd_debug))
    application.add_handler(CommandHandler("links", cmd_links))

    logging.info("Iniciando bot do EvRadar PRO...")
    await application.run_polling(close_loop=False)


if __name__ == "__main__":
    asyncio.run(main())
