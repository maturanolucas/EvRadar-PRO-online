import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# =============================
# Configurações globais
# =============================

def getenv_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def getenv_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
API_FOOTBALL_HOST = os.getenv("API_FOOTBALL_HOST", "v3.football.api-sports.io").strip()

WINDOW_START = getenv_int("WINDOW_START", 47)
WINDOW_END = getenv_int("WINDOW_END", 85)

EV_MIN_PERCENT = getenv_float("EV_MIN", 4.0)
TARGET_ODD = getenv_float("TARGET_ODD", 1.70)
MIN_ODD = getenv_float("MIN_ODD", 1.47)
MAX_ODD = getenv_float("MAX_ODD", 3.50)

KELLY_FRACTION = getenv_float("KELLY_FRACTION", 0.5)
MAX_STAKE_PCT = getenv_float("MAX_STAKE_PCT", 3.0)

CHECK_INTERVAL = getenv_int("CHECK_INTERVAL", 90)  # segundos entre varreduras automáticas
AUTOSTART = getenv_int("AUTOSTART", 1)

# Ligações principais (padrão: top 5 + Eredivisie, permite override por env)
DEFAULT_LEAGUE_IDS = [39, 140, 135, 78, 61, 88]  # EPL, La Liga, Serie A, Bundesliga, Ligue 1, Eredivisie
_env_leagues = os.getenv("LEAGUE_IDS", "").strip()
if _env_leagues:
    ACCEPTED_LEAGUE_IDS = {
        int(x) for x in _env_leagues.split(",") if x.strip().isdigit()
    }
else:
    ACCEPTED_LEAGUE_IDS = set(DEFAULT_LEAGUE_IDS)

COOLDOWN_MINUTES = getenv_int("COOLDOWN_MINUTES", 6)

BANKROLL = getenv_float("BANKROLL", 0.0)  # para stake em R$ na DM (opcional)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)

logger = logging.getLogger("EvRadarPRO")

# =============================
# Estruturas de dados
# =============================

@dataclass
class MatchCandidate:
    fixture_id: int
    league_id: int
    league_name: str
    league_country: str
    home: str
    away: str
    minute: int
    goals_home: int
    goals_away: int
    pressure_index: float
    prob_goal: float
    fair_odd: float
    market_odd: float
    ev_percent: float
    tier: str


cooldowns: Dict[int, float] = {}
last_scan_summary: str = "Ainda não foi feita nenhuma varredura."
last_scan_time: Optional[float] = None


# =============================
# Funções de modelo / valor
# =============================

def compute_ev(prob: float, odd: float) -> float:
    """EV simples: p * odd - 1 (retorna em %)"""
    if odd <= 1.0:
        return -100.0
    return (prob * odd - 1.0) * 100.0


def kelly_stake_pct(prob: float, odd: float, kelly_fraction: float, max_pct: float) -> float:
    """
    Kelly fracionado:
    f* = (p*(b+1)-1)/b, onde b = odd-1
    """
    b = odd - 1.0
    if b <= 0.0:
        return 0.0
    edge = prob * (b + 1.0) - 1.0
    f_star = edge / b
    if f_star <= 0.0:
        return 0.0
    stake_pct = f_star * kelly_fraction * 100.0
    if stake_pct > max_pct:
        stake_pct = max_pct
    return max(stake_pct, 0.0)


def minute_base_prob(minute: int) -> float:
    """
    Probabilidade base (sem stats) de sair +1 gol até o fim, bem aproximada.
    Usamos degraus simples só para ordem de grandeza.
    """
    if minute < 50:
        return 0.60
    if minute < 60:
        return 0.55
    if minute < 70:
        return 0.48
    if minute < 80:
        return 0.40
    if minute < 88:
        return 0.32
    return 0.22


def estimate_goal_probability(
    minute: int,
    goals_home: int,
    goals_away: int,
    stats: Optional[Dict[str, Dict[str, float]]] = None,
) -> float:
    """
    Estima P(+1 gol) até o fim usando minuto, placar e um índice de pressão simplificado.
    stats: {"home": {...}, "away": {...}}
    """
    base = minute_base_prob(minute)
    total_goals = goals_home + goals_away

    # Ajuste por placar
    if total_goals == 0:
        base += 0.04
    elif total_goals == 1:
        base += 0.02
    elif total_goals == 2:
        base -= 0.01
    elif total_goals == 3:
        base -= 0.04
    else:
        base -= 0.06

    pressure_index = 1.0
    if stats:
        def side_pressure(side: str) -> float:
            s = stats.get(side, {})
            shots_on = s.get("shots_on_goal", 0.0)
            shots_total = s.get("total_shots", 0.0)
            dang_att = s.get("dangerous_attacks", 0.0)
            attacks = s.get("attacks", 0.0)
            return shots_on * 2.0 + shots_total * 0.5 + dang_att * 0.1 + attacks * 0.02

        press_home = side_pressure("home")
        press_away = side_pressure("away")
        total_press = press_home + press_away

        # Normaliza para algo tipo 0.7–1.5
        if total_press <= 5:
            pressure_index = 0.7
        elif total_press <= 15:
            pressure_index = 0.9
        elif total_press <= 30:
            pressure_index = 1.05
        elif total_press <= 50:
            pressure_index = 1.20
        else:
            pressure_index = 1.35

    prob = base * pressure_index
    if prob < 0.05:
        prob = 0.05
    if prob > 0.90:
        prob = 0.90
    return prob


def classify_tier(ev_percent: float, prob: float) -> str:
    """
    Classifica sinal em Tier A/B/C para texto.
    """
    if ev_percent >= EV_MIN_PERCENT + 3.0 and prob >= 0.60:
        return "Tier A"
    if ev_percent >= EV_MIN_PERCENT and prob >= 0.52:
        return "Tier B"
    if ev_percent >= EV_MIN_PERCENT * 0.7 and prob >= 0.48:
        return "Tier C"
    return "Descartar"


# =============================
# Integração API-FOOTBALL
# =============================

HEADERS_DIRECT = {
    "x-apisports-key": API_FOOTBALL_KEY,
}

HEADERS_RAPID = {
    "x-rapidapi-key": API_FOOTBALL_KEY,
    "x-rapidapi-host": API_FOOTBALL_HOST,
}

def get_api_headers() -> Dict[str, str]:
    """
    Decide se vamos usar cabeçalho direto (x-apisports-key) ou RapidAPI.
    Se o host for v3.football.api-sports.io usamos direto.
    """
    if not API_FOOTBALL_KEY:
        return {}
    if API_FOOTBALL_HOST == "v3.football.api-sports.io":
        return HEADERS_DIRECT
    return HEADERS_RAPID


async def fetch_live_fixtures(client: httpx.AsyncClient) -> List[Dict]:
    if not API_FOOTBALL_KEY:
        logger.warning("API_FOOTBALL_KEY não definido. Nenhum jogo será analisado.")
        return []
    url = f"https://{API_FOOTBALL_HOST}/fixtures"
    params = {"live": "all"}
    try:
        r = await client.get(url, headers=get_api_headers(), params=params, timeout=10.0)
        r.raise_for_status()
        data = r.json()
        return data.get("response", [])
    except Exception as exc:
        logger.error("Erro ao buscar fixtures ao vivo: %s", exc)
        return []


async def fetch_fixture_statistics(
    client: httpx.AsyncClient,
    fixture_id: int,
    home_id: int,
    away_id: int,
) -> Optional[Dict[str, Dict[str, float]]]:
    url = f"https://{API_FOOTBALL_HOST}/fixtures/statistics"
    params = {"fixture": fixture_id}
    try:
        r = await client.get(url, headers=get_api_headers(), params=params, timeout=10.0)
        r.raise_for_status()
        data = r.json()
        response = data.get("response", [])
    except Exception as exc:
        logger.error("Erro ao buscar statistics para fixture %s: %s", fixture_id, exc)
        return None

    stats: Dict[str, Dict[str, float]] = {"home": {}, "away": {}}

    type_map = {
        "Shots on Goal": "shots_on_goal",
        "Shots off Goal": "shots_off_goal",
        "Total Shots": "total_shots",
        "Attacks": "attacks",
        "Dangerous Attacks": "dangerous_attacks",
        "Ball Possession": "possession",
    }

    for block in response:
        team = block.get("team", {})
        team_id = team.get("id")
        if team_id == home_id:
            side = "home"
        elif team_id == away_id:
            side = "away"
        else:
            continue

        for st in block.get("statistics", []):
            st_type = st.get("type")
            raw_val = st.get("value")
            key = type_map.get(st_type)
            if not key:
                continue
            if raw_val is None:
                val = 0.0
            elif isinstance(raw_val, (int, float)):
                val = float(raw_val)
            elif isinstance(raw_val, str) and raw_val.endswith("%"):
                try:
                    val = float(raw_val.strip("%"))
                except ValueError:
                    val = 0.0
            else:
                try:
                    val = float(raw_val)
                except Exception:
                    val = 0.0
            stats[side][key] = val

    return stats


async def fetch_live_odds_for_fixture(
    client: httpx.AsyncClient,
    fixture_id: int,
) -> Optional[float]:
    """
    Busca odds in-play para o fixture. Tentamos pegar mercado Over/Under genérico (bet=36).
    Se não encontrar, devolve TARGET_ODD.
    """
    url = f"https://{API_FOOTBALL_HOST}/odds/live"
    params = {"fixture": fixture_id, "bet": 36}
    try:
        r = await client.get(url, headers=get_api_headers(), params=params, timeout=10.0)
        r.raise_for_status()
        data = r.json()
        resp = data.get("response", [])
        if not resp:
            return TARGET_ODD
        # Estrutura típica: response[0]["bookmakers"][...]["bets"][0]["values"][...]
        first = resp[0]
        bookmakers = first.get("bookmakers", [])
        for bm in bookmakers:
            bets = bm.get("bets", [])
            for bet in bets:
                values = bet.get("values", [])
                for val in values:
                    odd_val = val.get("odd")
                    if odd_val is None:
                        continue
                    try:
                        odd_f = float(odd_val)
                    except ValueError:
                        continue
                    if MIN_ODD <= odd_f <= MAX_ODD:
                        return odd_f
        return TARGET_ODD
    except Exception as exc:
        logger.error("Erro ao buscar odds in-play para fixture %s: %s", fixture_id, exc)
        return TARGET_ODD


# =============================
# Construção dos candidatos
# =============================

def fixture_in_window(fixture: Dict) -> bool:
    fixture_info = fixture.get("fixture", {})
    status = fixture_info.get("status", {})
    elapsed = status.get("elapsed")
    if elapsed is None:
        return False
    if elapsed < WINDOW_START or elapsed > WINDOW_END:
        return False
    short = status.get("short")
    if short not in ("2H", "ET"):
        # obrigamos pelo menos 2º tempo
        if elapsed < 45:
            return False
    return True


def fixture_in_leagues(fixture: Dict) -> bool:
    league = fixture.get("league", {})
    league_id = league.get("id")
    if ACCEPTED_LEAGUE_IDS and league_id not in ACCEPTED_LEAGUE_IDS:
        return False
    return True


def build_candidate_from_fixture(
    fixture: Dict,
    stats: Optional[Dict[str, Dict[str, float]]],
    market_odd: float,
) -> Optional[MatchCandidate]:
    league = fixture.get("league", {})
    fixture_info = fixture.get("fixture", {})
    teams = fixture.get("teams", {})
    goals = fixture.get("goals", {})

    fixture_id = fixture_info.get("id")
    league_id = league.get("id") or 0
    league_name = league.get("name") or "Liga desconhecida"
    league_country = league.get("country") or "N/A"

    home_team = teams.get("home", {}).get("name") or "Casa"
    away_team = teams.get("away", {}).get("name") or "Fora"

    minute = fixture_info.get("status", {}).get("elapsed") or 0
    goals_home = goals.get("home") or 0
    goals_away = goals.get("away") or 0

    prob = estimate_goal_probability(minute, goals_home, goals_away, stats)
    fair_odd = 1.0 / prob if prob > 0 else 99.9
    ev_percent = compute_ev(prob, market_odd)

    # pressão agregada apenas para exibição
    pressure_index = 1.0
    if stats:
        def total_side_pressure(side: str) -> float:
            s = stats.get(side, {})
            return (
                s.get("shots_on_goal", 0.0) * 2.0
                + s.get("total_shots", 0.0) * 0.5
                + s.get("dangerous_attacks", 0.0) * 0.1
            )
        pressure_index = total_side_pressure("home") + total_side_pressure("away")

    tier = classify_tier(ev_percent, prob)
    if tier == "Descartar":
        return None

    return MatchCandidate(
        fixture_id=int(fixture_id),
        league_id=int(league_id),
        league_name=league_name,
        league_country=league_country,
        home=home_team,
        away=away_team,
        minute=int(minute),
        goals_home=int(goals_home),
        goals_away=int(goals_away),
        pressure_index=pressure_index,
        prob_goal=prob,
        fair_odd=fair_odd,
        market_odd=market_odd,
        ev_percent=ev_percent,
        tier=tier,
    )


def format_candidate_message(c: MatchCandidate, stake_pct: float) -> str:
    """
    Monta texto em HTML para o alerta no Telegram.
    """
    linha = f"Over (soma + 0,5) @ {c.market_odd:.2f}"
    p_str = f"{c.prob_goal * 100.0:.1f}%"
    fair_str = f"{c.fair_odd:.2f}"
    ev_str = f"{c.ev_percent:+.2f}%"
    stake_pct_str = f"{stake_pct:.2f}%"

    stake_reais = ""
    if BANKROLL > 0 and stake_pct > 0:
        valor = BANKROLL * stake_pct / 100.0
        stake_reais = f" (~R$ {valor:.2f} em banca de R$ {BANKROLL:.2f})"

    header_tier = "🔔 Sinal forte"
    if c.tier == "Tier A":
        header_tier = "🔔 Tier A — Sinal muito forte"
    elif c.tier == "Tier B":
        header_tier = "🔔 Tier B — Sinal forte"
    elif c.tier == "Tier C":
        header_tier = "🔔 Tier C — Sinal moderado"

    interpret_lines: List[str] = []
    interpret_lines.append("🧩 Interpretação rápida:")
    interpret_lines.append(
        "- Jogo com pressão interessante e janela boa para mais 1 gol."
    )
    if c.minute >= 80:
        interpret_lines.append("- Fase final: atenção ao relógio e ao cash out.")
    if c.goals_home + c.goals_away == 0:
        interpret_lines.append("- 0–0 com pressão: padrão clássico de late goal.")
    elif c.goals_home + c.goals_away >= 3:
        interpret_lines.append("- Jogo já movimentado, cuidado para não pagar caro demais.")

    lines = []
    lines.append(header_tier)
    lines.append("")
    lines.append(
        f"🏟️ <b>{c.home} vs {c.away}</b> — {c.league_name} ({c.league_country})"
    )
    lines.append(
        f"⏱️ {c.minute}' | 🔢 {c.goals_home}–{c.goals_away}"
    )
    lines.append(f"⚙️ Linha: {linha}")
    lines.append("")
    lines.append("📊 Probabilidade & valor:")
    lines.append(f"- P_final (gol a mais): {p_str}")
    lines.append(f"- Odd justa (modelo): {fair_str}")
    lines.append(f"- Edge (EV): {ev_str}")
    lines.append("")
    lines.append("💰 Stake sugerida:")
    if stake_pct > 0:
        lines.append(f"- {stake_pct_str} da banca{stake_reais}")
    else:
        lines.append("- 0% (sem valor claro, apenas monitorar)")
    lines.append("")
    lines.extend(interpret_lines)
    lines.append("")
    lines.append("🔗 Abra o mercado na sua casa de aposta favorita.")

    return "\n".join(lines)


# =============================
# Loop de varredura
# =============================

async def run_scan(app: Application, origin: str) -> None:
    global last_scan_summary, last_scan_time, cooldowns

    now_ts = time.time()
    started_at = time.strftime("%H:%M:%S", time.localtime(now_ts))
    logger.info("Iniciando varredura (%s) às %s", origin, started_at)

    async with httpx.AsyncClient(timeout=10.0) as client:
        fixtures = await fetch_live_fixtures(client)

        total_live = len(fixtures)
        in_window = 0
        candidates: List[MatchCandidate] = []

        for fx in fixtures:
            if not fixture_in_leagues(fx):
                continue
            if not fixture_in_window(fx):
                continue

            in_window += 1

            fixture_info = fx.get("fixture", {})
            teams = fx.get("teams", {})
            goals = fx.get("goals", {})

            fixture_id = fixture_info.get("id")
            home_id = teams.get("home", {}).get("id")
            away_id = teams.get("away", {}).get("id")

            if not fixture_id or not home_id or not away_id:
                continue

            stats = await fetch_fixture_statistics(client, fixture_id, home_id, away_id)
            market_odd = await fetch_live_odds_for_fixture(client, fixture_id)

            candidate = build_candidate_from_fixture(fx, stats, market_odd)
            if not candidate:
                continue

            # cooldown por fixture
            last_alert_ts = cooldowns.get(candidate.fixture_id)
            if last_alert_ts is not None:
                elapsed = (now_ts - last_alert_ts) / 60.0
                if elapsed < COOLDOWN_MINUTES:
                    continue

            # stake por Kelly
            stake_pct = kelly_stake_pct(
                candidate.prob_goal,
                candidate.market_odd,
                KELLY_FRACTION,
                MAX_STAKE_PCT,
            )

            text = format_candidate_message(candidate, stake_pct)
            try:
                await app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                logger.info(
                    "Alerta enviado para fixture %s (%s vs %s, EV=%.2f%%)",
                    candidate.fixture_id,
                    candidate.home,
                    candidate.away,
                    candidate.ev_percent,
                )
                cooldowns[candidate.fixture_id] = now_ts
                candidates.append(candidate)
            except Exception as exc:
                logger.error("Erro ao enviar alerta Telegram: %s", exc)

    alerts_sent = len(candidates)
    last_scan_time = now_ts
    last_scan_summary = (
        f"[EvRadar PRO] Scan concluído (origem={origin}). "
        f"Eventos ao vivo: {total_live} | "
        f"Jogos analisados na janela: {in_window} | "
        f"Alertas enviados: {alerts_sent}."
    )
    logger.info(last_scan_summary)


async def scan_loop(app: Application) -> None:
    if not AUTOSTART:
        logger.info("AUTOSTART=0, loop automático desativado.")
        return
    logger.info(
        "Loop automático iniciado. Intervalo: %ss | Janela: %d–%d'",
        CHECK_INTERVAL,
        WINDOW_START,
        WINDOW_END,
    )
    while True:
        try:
            await run_scan(app, origin="auto")
        except Exception as exc:
            logger.error("Erro inesperado no loop de varredura: %s", exc)
        await asyncio.sleep(CHECK_INTERVAL)


# =============================
# Handlers Telegram
# =============================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uptime_sec = 0.0
    if last_scan_time:
        uptime_sec = time.time() - last_scan_time

    lines = []
    lines.append("👋 EvRadar PRO online.")
    lines.append("")
    lines.append(f"Janela padrão: {WINDOW_START}–{WINDOW_END}ʼ")
    lines.append(f"Odd ref (TARGET_ODD): {TARGET_ODD:.2f}")
    lines.append(f"EV mínimo: {EV_MIN_PERCENT:.2f}%")
    lines.append(f"Cooldown por jogo: {COOLDOWN_MINUTES} min")
    lines.append("")
    lines.append("Comandos:")
    lines.append("  /scan   → rodar varredura agora")
    lines.append("  /status → ver último resumo")
    lines.append("  /debug  → info técnica")
    lines.append("  /links  → links úteis / bookmaker")

    await update.message.reply_text("\n".join(lines))


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔍 Iniciando varredura manual de jogos ao vivo...")
    app = context.application
    await run_scan(app, origin="manual")
    await update.message.reply_text(last_scan_summary)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if last_scan_time:
        last_time_str = time.strftime(
            "%d/%m %H:%M:%S", time.localtime(last_scan_time)
        )
    else:
        last_time_str = "nunca"

    lines = []
    lines.append("📊 Status do EvRadar PRO:")
    lines.append(f"- Último scan: {last_time_str}")
    lines.append(f"- Janela atual: {WINDOW_START}–{WINDOW_END}ʼ")
    lines.append(f"- EV mínimo: {EV_MIN_PERCENT:.2f}%")
    lines.append(f"- Intervalo auto: {CHECK_INTERVAL}s (AUTOSTART={AUTOSTART})")
    lines.append("")
    lines.append(last_scan_summary)

    await update.message.reply_text("\n".join(lines))


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    leagues_str = ", ".join(str(x) for x in sorted(ACCEPTED_LEAGUE_IDS)) or "todas"
    lines = []
    lines.append("🛠 Debug EvRadar PRO")
    lines.append(f"- WINDOW_START: {WINDOW_START}")
    lines.append(f"- WINDOW_END: {WINDOW_END}")
    lines.append(f"- EV_MIN: {EV_MIN_PERCENT:.2f}%")
    lines.append(f"- TARGET_ODD: {TARGET_ODD:.2f}")
    lines.append(f"- MIN_ODD: {MIN_ODD:.2f}")
    lines.append(f"- MAX_ODD: {MAX_ODD:.2f}")
    lines.append(f"- CHECK_INTERVAL: {CHECK_INTERVAL}s")
    lines.append(f"- COOLDOWN_MINUTES: {COOLDOWN_MINUTES}")
    lines.append(f"- AUTOSTART: {AUTOSTART}")
    lines.append(f"- LEAGUE_IDS: {leagues_str}")
    lines.append(f"- API_FOOTBALL_HOST: {API_FOOTBALL_HOST}")
    lines.append(f"- BANKROLL: R$ {BANKROLL:.2f}")
    await update.message.reply_text("\n".join(lines))


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = []
    lines.append("🔗 Links úteis:")
    lines.append("- Superbet: abra o app/site e procure pelo jogo sinalizado.")
    lines.append("- API-FOOTBALL: dashboard para monitorar consumo de requisições.")
    await update.message.reply_text("\n".join(lines))


# =============================
# Bootstrap da aplicação
# =============================

async def on_startup(app: Application) -> None:
    logger.info("EvRadar PRO iniciado. Preparando loop automático...")
    if AUTOSTART:
        asyncio.create_task(scan_loop(app))


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN não definido no ambiente.")
    if not TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_CHAT_ID não definido. Nenhuma mensagem será enviada.")

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("debug", cmd_debug))
    application.add_handler(CommandHandler("links", cmd_links))

    logger.info("Bot Telegram iniciando polling...")
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
