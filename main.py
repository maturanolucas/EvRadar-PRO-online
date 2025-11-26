import os
import asyncio
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional, Set, Tuple

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ============================================================
#  LOG & ESTADO GLOBAL
# ============================================================

logger = logging.getLogger("EvRadarPRO")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

API_FOOTBALL_BASE = "https://v3.football.api-sports.io"

LAST_SCAN_SUMMARY: str = "Nenhum scan executado ainda."
LAST_ERROR: Optional[str] = None


# ============================================================
#  HELPERS DE AMBIENTE
# ============================================================

def env_str(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Variável de ambiente obrigatória não definida: {name}")
    return value


def env_int(name: str, default: Optional[int] = None) -> int:
    val = os.getenv(name)
    if val is None or val == "":
        if default is None:
            raise RuntimeError(f"Variável de ambiente obrigatória não definida: {name}")
        return default
    try:
        return int(val)
    except ValueError:
        raise RuntimeError(f"Variável {name} precisa ser int, valor atual: {val!r}")


def env_float(name: str, default: Optional[float] = None) -> float:
    val = os.getenv(name)
    if val is None or val == "":
        if default is None:
            raise RuntimeError(f"Variável de ambiente obrigatória não definida: {name}")
        return default
    try:
        return float(val.replace(",", "."))
    except ValueError:
        raise RuntimeError(f"Variável {name} precisa ser float, valor atual: {val!r}")


def parse_league_ids(raw: str) -> Set[int]:
    ids: Set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logger.warning("LEAGUE_IDS: valor ignorado %r (não é int)", part)
    return ids


# ============================================================
#  CONFIG (FIXO + ENV)
# ============================================================

TELEGRAM_BOT_TOKEN = env_str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env_str("TELEGRAM_CHAT_ID")

API_FOOTBALL_KEY = env_str("API_FOOTBALL_KEY")

AUTOSTART = env_int("AUTOSTART", 1)
CHECK_INTERVAL = env_int("CHECK_INTERVAL", 60)  # segundos

# Janela padrão 2º tempo
WINDOW_START = env_int("WINDOW_START", 47)
WINDOW_END = env_int("WINDOW_END", 85)

# Odds alvo (odd real da casa)
MIN_ODD = env_float("MIN_ODD", 1.47)
MAX_ODD = env_float("MAX_ODD", 3.50)

# EV mínimo em %
EV_MIN_PCT = env_float("EV_MIN_PCT", 4.0)

# Bookmaker (para odds da API-Football)
BOOKMAKER_ID = env_int("BOOKMAKER_ID", 34)
BOOKMAKER_NAME = os.getenv("BOOKMAKER_NAME", "Superbet")
BOOKMAKER_URL = os.getenv("BOOKMAKER_URL", "https://www.superbet.com/")

# Ligas permitidas (só grandes ligas / copas)
LEAGUE_IDS_RAW = os.getenv("LEAGUE_IDS", "")
ALLOWED_LEAGUES: Set[int] = parse_league_ids(LEAGUE_IDS_RAW) if LEAGUE_IDS_RAW else set()

# Liga estatística & odds
USE_API_FOOTBALL_ODDS = env_int("USE_API_FOOTBALL_ODDS", 1)
USE_STATS = env_int("USE_STATS", 1)  # 1 = usa /fixtures/statistics; 0 = ignora

# Banca e news/contexto
BANKROLL = env_float("BANKROLL_INITIAL", 5000.0)
NEWS_BOOST_DEFAULT = env_float("NEWS_BOOST_DEFAULT", 0.0)  # -2 a +2 idealmente

# Kelly & caps
KELLY_FRACTION = env_float("KELLY_FRACTION", 0.5)
STAKE_CAP_PCT = env_float("STAKE_CAP_PCT", 3.0)  # máximo % da banca por entrada


# ============================================================
#  API-FOOTBALL HELPERS
# ============================================================

async def fetch_api_football(
    client: httpx.AsyncClient,
    endpoint: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    headers = {
        "x-apisports-key": API_FOOTBALL_KEY,
    }
    url = f"{API_FOOTBALL_BASE}/{endpoint.lstrip('/')}"
    resp = await client.get(url, headers=headers, params=params, timeout=15.0)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        logger.warning("API-Football retornou errors: %s", data["errors"])
    return data


async def fetch_live_fixtures(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    data = await fetch_api_football(client, "fixtures", params={"live": "all"})
    return data.get("response", [])


async def fetch_odds_for_fixture(
    client: httpx.AsyncClient,
    fixture_id: int,
) -> Optional[Dict[str, Any]]:
    if not USE_API_FOOTBALL_ODDS:
        return None
    params = {
        "fixture": fixture_id,
        "bookmaker": BOOKMAKER_ID,
    }
    data = await fetch_api_football(client, "odds", params=params)
    arr = data.get("response", [])
    if not arr:
        return None
    return arr[0]


async def fetch_stats_for_fixture(
    client: httpx.AsyncClient,
    fixture_id: int,
) -> Optional[List[Dict[str, Any]]]:
    """
    Busca estatísticas detalhadas do jogo.
    Endpoint: /fixtures/statistics?fixture=ID
    Retorno: lista [ { team: {...}, statistics: [ {type, value}, ...] }, ... ]
    """
    if not USE_STATS:
        return None
    params = {"fixture": fixture_id}
    data = await fetch_api_football(client, "fixtures/statistics", params=params)
    return data.get("response", [])


# ============================================================
#  ANÁLISE ESTATÍSTICA (v0.2)
# ============================================================

def get_stat_value(stats: List[Dict[str, Any]], key: str) -> Optional[float]:
    """
    stats: lista de {type: 'Total Shots', value: '10'}
    Retorna float ou None.
    """
    for item in stats:
        t = (item.get("type") or "").strip().lower()
        if t == key.lower():
            value = item.get("value")
            if value is None:
                return None
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                value = value.replace("%", "").strip()
                if value == "":
                    return None
                try:
                    return float(value)
                except ValueError:
                    return None
    return None


def aggregate_stats(
    stats_payload: Optional[List[Dict[str, Any]]]
) -> Dict[str, float]:
    """
    Junta estatísticas dos dois times em um pacote único:
    total_shots, shots_on_target, dangerous_attacks, possession_avg.
    Se não tiver stats, devolve tudo zero.
    """
    result = {
        "total_shots": 0.0,
        "shots_on_target": 0.0,
        "dangerous_attacks": 0.0,
        "possession_avg": 50.0,
    }

    if not stats_payload:
        return result

    total_pos = 0.0
    pos_count = 0

    for team_stats in stats_payload:
        stats_list = team_stats.get("statistics") or []
        ts = get_stat_value(stats_list, "Total Shots")
        sog = get_stat_value(stats_list, "Shots on Goal")
        da = get_stat_value(stats_list, "Dangerous Attacks")
        pos = get_stat_value(stats_list, "Ball Possession")

        if ts is not None:
            result["total_shots"] += ts
        if sog is not None:
            result["shots_on_target"] += sog
        if da is not None:
            result["dangerous_attacks"] += da
        if pos is not None:
            total_pos += pos
            pos_count += 1

    if pos_count > 0:
        result["possession_avg"] = total_pos / pos_count

    return result


def compute_pressure_score(stats_agg: Dict[str, float], minute: int) -> float:
    """
    Score 0–10 de pressão / ritmo:
    combinação de chutes, chutes no alvo, ataques perigosos e posse média, ajustado pelo tempo.
    """
    total_shots = stats_agg["total_shots"]
    shots_on_target = stats_agg["shots_on_target"]
    dangerous_attacks = stats_agg["dangerous_attacks"]
    possession_avg = stats_agg["possession_avg"]

    # Normalizações (limiares típicos)
    shots_norm = min(total_shots / 20.0, 1.5)        # até ~20 chutes
    sog_norm = min(shots_on_target / 8.0, 1.5)       # até ~8 chutes no alvo
    da_norm = min(dangerous_attacks / 60.0, 1.5)     # até ~60 ataques perigosos
    pos_norm = min(possession_avg / 50.0, 1.5)       # ~50% como base

    # Peso maior pra chutes no alvo e ataques perigosos
    base = (
        0.25 * shots_norm +
        0.35 * sog_norm +
        0.30 * da_norm +
        0.10 * pos_norm
    )  # ~0–1.5

    # Ajuste pelo tempo: pressão tardia vale mais
    # No fim do jogo, a mesma pressão sobe o score
    if minute <= 45:
        time_factor = 0.8
    elif minute <= 60:
        time_factor = 1.0
    elif minute <= 75:
        time_factor = 1.1
    else:
        time_factor = 1.2

    score = base * 10.0 * time_factor
    if score < 0.0:
        score = 0.0
    if score > 10.0:
        score = 10.0
    return score


def estimate_goal_probability(
    pressure_score: float,
    minute: int,
    total_goals: int,
    news_boost: float,
) -> float:
    """
    Estima probabilidade de sair +1 gol no resto do jogo (Over SUM_PLUS_HALF)
    com base na pressão (0–10), minuto, placar e news/contexto.
    """
    # Base: pressão -> probabilidade bruta 0.25–0.85
    base = 0.25 + 0.06 * pressure_score  # pressão 0 → 25%, 10 → 85%

    # Fator tempo: menos tempo -> menor probabilidade efetiva
    if minute <= 45:
        time_mult = 1.0
    else:
        # de 45 a 90, cai de 1.0 para ~0.4
        dec = (minute - 45) * 0.014
        time_mult = 1.0 - dec
        if time_mult < 0.4:
            time_mult = 0.4

    # Placar: jogos mais abertos (mais gols) tendem a aceitar mais um gol
    score_mult = 1.0 + 0.08 * total_goals
    if score_mult > 1.4:
        score_mult = 1.4

    # News/contexto: -2 a +2 → 0.85 a 1.15 aprox.
    context_mult = 1.0 + 0.07 * news_boost
    if context_mult < 0.85:
        context_mult = 0.85
    if context_mult > 1.15:
        context_mult = 1.15

    p = base * time_mult * score_mult * context_mult

    if p < 0.01:
        p = 0.01
    if p > 0.95:
        p = 0.95

    return p


def compute_ev(p: float, odd: float) -> Tuple[float, float, float]:
    """
    Retorna (ev, ev_pct, fair_odd)
    ev = p * odd - 1
    """
    if odd <= 1.0:
        return -1.0, -100.0, 999.0
    fair_odd = 1.0 / p
    ev = p * odd - 1.0
    ev_pct = ev * 100.0
    return ev, ev_pct, fair_odd


def compute_kelly_stake_pct(p: float, odd: float, ev_pct: float) -> float:
    """
    Kelly fracionado 0.5 com cap 3%, mais:
    - throttle por odd
    - tier por EV
    Retorna % da banca sugerida.
    """
    if odd <= 1.0:
        return 0.0

    b = odd - 1.0
    q = 1.0 - p
    f_star = (b * p - q) / b  # Kelly pleno

    if f_star <= 0.0:
        return 0.0

    # Fractional kelly
    f_star *= KELLY_FRACTION

    # Cap global
    max_cap = STAKE_CAP_PCT / 100.0
    if f_star > max_cap:
        f_star = max_cap

    # Throttle por odds
    if odd <= 1.80:
        odds_mult = 1.0
    elif odd <= 2.60:
        odds_mult = 0.9
    else:
        odds_mult = 0.7

    # Tier por EV
    if ev_pct < 1.5:
        ev_mult = 0.0  # nem deveria passar pelo filtro
    elif ev_pct < 3.0:
        ev_mult = 0.5
    elif ev_pct < 5.0:
        ev_mult = 0.75
    elif ev_pct < 7.0:
        ev_mult = 1.0
    else:
        ev_mult = 1.25

    stake_pct = f_star * odds_mult * ev_mult

    if stake_pct < 0.0:
        stake_pct = 0.0
    if stake_pct > max_cap:
        stake_pct = max_cap

    return stake_pct


# ============================================================
#  FORMATAÇÃO DE ALERTA
# ============================================================

def format_signal_message(ev: Dict[str, Any]) -> str:
    league = ev.get("league_name", "?")
    home = ev.get("home", "?")
    away = ev.get("away", "?")
    minute = ev.get("minute", 0)
    gh = ev.get("goals_home", 0)
    ga = ev.get("goals_away", 0)
    total_goals = gh + ga
    odd = ev.get("odd")
    pressure = ev.get("pressure_score", 0.0)
    p_goal = ev.get("p_goal", 0.0)
    ev_pct = ev.get("ev_pct", 0.0)
    fair_odd = ev.get("fair_odd", 0.0)
    stake_pct = ev.get("stake_pct", 0.0)
    stats_agg = ev.get("stats_agg", {})

    line = f"Over (soma + 0,5) ⇒ Over {total_goals + 0.5}"

    header = f"🏟️ {home} vs {away} — {league}"
    l1 = f"⏱️ {minute}' | 🔢 {gh}–{ga}"
    if odd is not None:
        l2 = f"⚙️ Linha: {line} @ {odd:.2f}"
    else:
        l2 = f"⚙️ Linha: {line} (odd indisponível)"

    l3 = (
        "📊 Modelo:\n"
        f"- Pressão: {pressure:.1f} / 10\n"
        f"- Prob. gol (modelo): {p_goal*100:.1f}%\n"
        f"- Odd justa (modelo): {fair_odd:.2f}"
    )

    l4 = (
        "💰 Valor:\n"
        f"- EV: {ev_pct:+.2f}%\n"
        f"- Stake sugerida: {stake_pct*100:.2f}% da banca"
    )
    if BANKROLL > 0 and stake_pct > 0:
        valor = BANKROLL * stake_pct
        l4 = l4 + f" (~R${valor:,.2f})".replace(",", "X").replace(".", ",").replace("X", ".")

    l5 = (
        "📈 Estatísticas (soma dos dois times):\n"
        f"- Total de chutes: {stats_agg.get('total_shots', 0):.0f}\n"
        f"- Chutes no alvo: {stats_agg.get('shots_on_target', 0):.0f}\n"
        f"- Ataques perigosos: {stats_agg.get('dangerous_attacks', 0):.0f}\n"
        f"- Posse média: {stats_agg.get('possession_avg', 50):.1f}%"
    )

    l6 = f"🔗 Abrir mercado ({BOOKMAKER_NAME}): {BOOKMAKER_URL}"

    return "\n".join([header, l1, l2, "", l3, "", l4, "", l5, "", l6])


# ============================================================
#  SCAN PRINCIPAL (v0.2)
# ============================================================

async def scan_once(application: Optional[Application] = None) -> str:
    global LAST_SCAN_SUMMARY, LAST_ERROR

    async with httpx.AsyncClient() as client:
        try:
            fixtures = await fetch_live_fixtures(client)
        except Exception as e:
            msg = f"Erro ao buscar fixtures ao vivo: {e}"
            logger.exception(msg)
            LAST_ERROR = msg
            LAST_SCAN_SUMMARY = msg
            if application is not None:
                try:
                    await application.bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=f"[EvRadar PRO] {msg}",
                        disable_web_page_preview=True,
                    )
                except Exception as e2:
                    logger.warning("Falha ao enviar erro no Telegram: %s", e2)
            return msg

        total_live = len(fixtures)
        candidates: List[Dict[str, Any]] = []

        for fx in fixtures:
            try:
                league = fx.get("league", {}) or {}
                league_id = int(league.get("id"))
                league_name = league.get("name", "?")

                # Filtro de ligas
                if ALLOWED_LEAGUES and league_id not in ALLOWED_LEAGUES:
                    continue

                fixture = fx.get("fixture", {}) or {}
                status = fixture.get("status", {}) or {}
                minute = status.get("elapsed")
                if minute is None:
                    continue

                # Janela 2º tempo
                if minute < WINDOW_START or minute > WINDOW_END:
                    continue

                goals = fx.get("goals", {}) or {}
                gh = goals.get("home") or 0
                ga = goals.get("away") or 0
                total_goals = gh + ga

                teams = fx.get("teams", {}) or {}
                home = teams.get("home", {}).get("name", "?")
                away = teams.get("away", {}).get("name", "?")

                fixture_id = int(fixture.get("id"))

                # 1º: Odds (pra não gastar stats à toa)
                odd_val: Optional[float] = None
                odds_payload: Optional[Dict[str, Any]] = None

                if USE_API_FOOTBALL_ODDS:
                    try:
                        odds_payload = await fetch_odds_for_fixture(client, fixture_id)
                    except Exception as e:
                        logger.warning("Falha ao buscar odds para fixture %s: %s", fixture_id, e)

                if odds_payload:
                    odd_val = extract_over_line_odd(odds_payload, total_goals)
                else:
                    odd_val = None

                # Sem odd → não dá pra calcular EV
                if odd_val is None:
                    continue

                if odd_val < MIN_ODD or odd_val > MAX_ODD:
                    continue

                # 2º: Stats (só agora)
                stats_payload = None
                stats_agg = {
                    "total_shots": 0.0,
                    "shots_on_target": 0.0,
                    "dangerous_attacks": 0.0,
                    "possession_avg": 50.0,
                }

                if USE_STATS:
                    try:
                        stats_payload = await fetch_stats_for_fixture(client, fixture_id)
                        stats_agg = aggregate_stats(stats_payload)
                    except Exception as e:
                        logger.warning("Falha ao buscar stats para fixture %s: %s", fixture_id, e)

                # 3º: Pressão v0.2
                pressure_score = compute_pressure_score(stats_agg, minute)

                # 4º: News/contexto — por enquanto default global (camada existe, mas neutra)
                news_boost = NEWS_BOOST_DEFAULT

                # 5º: Probabilidade de gol e EV
                p_goal = estimate_goal_probability(
                    pressure_score=pressure_score,
                    minute=minute,
                    total_goals=total_goals,
                    news_boost=news_boost,
                )
                ev, ev_pct, fair_odd = compute_ev(p_goal, odd_val)

                if ev_pct < EV_MIN_PCT:
                    continue

                # 6º: Stake por Kelly fracionado + tiers
                stake_pct = compute_kelly_stake_pct(p_goal, odd_val, ev_pct)

                candidates.append(
                    {
                        "league_id": league_id,
                        "league_name": league_name,
                        "home": home,
                        "away": away,
                        "minute": minute,
                        "goals_home": gh,
                        "goals_away": ga,
                        "fixture_id": fixture_id,
                        "odd": odd_val,
                        "pressure_score": pressure_score,
                        "p_goal": p_goal,
                        "ev": ev,
                        "ev_pct": ev_pct,
                        "fair_odd": fair_odd,
                        "stake_pct": stake_pct,
                        "stats_agg": stats_agg,
                    }
                )
            except Exception as inner:
                logger.warning("Erro ao processar fixture: %s", inner)

        # Ordena por EV desc, depois minuto desc
        candidates.sort(key=lambda x: (x["ev_pct"], x["minute"]), reverse=True)

        # === RESUMO ===
        summary_lines: List[str] = []
        summary_lines.append(
            f"[EvRadar PRO] Scan concluído. Eventos ao vivo: {total_live} | Candidatos (EV ≥ {EV_MIN_PCT:.2f}%): {len(candidates)}."
        )
        if ALLOWED_LEAGUES:
            summary_lines.append(f"Ligas filtradas: {len(ALLOWED_LEAGUES)} (LEAGUE_IDS)")
        summary_lines.append(
            f"Janela: {WINDOW_START}–{WINDOW_END}ʼ | Odds alvo: {MIN_ODD:.2f}–{MAX_ODD:.2f}"
        )
        summary_lines.append(
            f"Config stake: Kelly {KELLY_FRACTION:.2f}x, cap {STAKE_CAP_PCT:.1f}% da banca (R${BANKROLL:,.2f})"
            .replace(",", "X").replace(".", ",").replace("X", ".")
        )

        if candidates:
            summary_lines.append("")
            summary_lines.append("Top candidatos:")
            for ev in candidates[:5]:
                summary_lines.append(
                    f"- {ev['league_name']}: {ev['home']} x {ev['away']} | "
                    f"{ev['minute']}' | {ev['goals_home']}–{ev['goals_away']} | "
                    f"odd={ev['odd']:.2f} | EV={ev['ev_pct']:+.2f}% | "
                    f"stake≈{ev['stake_pct']*100:.2f}%"
                )
        else:
            summary_lines.append("")
            summary_lines.append("Nenhum jogo encaixou nos filtros neste scan.")

        LAST_SCAN_SUMMARY = "\n".join(summary_lines)
        LAST_ERROR = None
        logger.info(LAST_SCAN_SUMMARY)

        # Envia resumo + alertas
        if application is not None:
            try:
                await application.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=LAST_SCAN_SUMMARY,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.warning("Falha ao enviar resumo no Telegram: %s", e)

            for ev in candidates:
                text = format_signal_message(ev)
                try:
                    await application.bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=text,
                        disable_web_page_preview=True,
                    )
                except Exception as e:
                    logger.warning("Falha ao enviar alerta no Telegram: %s", e)

        return LAST_SCAN_SUMMARY


# ============================================================
#  HANDLERS TELEGRAM
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid = update.effective_chat.id
    lines = [
        "👋 EvRadar PRO v0.2 (parrudo) online.",
        "",
        f"Seu chat_id: {cid}",
        "",
        f"Janela padrão: {WINDOW_START}–{WINDOW_END}ʼ (2º tempo)",
        f"Odds alvo: {MIN_ODD:.2f}–{MAX_ODD:.2f}",
        f"EV mínimo: {EV_MIN_PCT:.2f}%",
        f"Banca (config): R${BANKROLL:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
        "",
        "Comandos:",
        "  /scan   → rodar varredura agora",
        "  /status → ver último resumo",
        "  /debug  → info técnica",
        "  /links  → links úteis / bookmaker",
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔍 Iniciando varredura manual de jogos ao vivo (EvRadar v0.2)...")
    application = context.application
    summary = await scan_once(application)
    await update.message.reply_text(summary)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(LAST_SCAN_SUMMARY)


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "🛠 Debug EvRadar PRO v0.2",
        f"UTC agora: {now}",
        f"AUTOSTART: {AUTOSTART}",
        f"CHECK_INTERVAL: {CHECK_INTERVAL}s",
        f"WINDOW_START/END: {WINDOW_START}–{WINDOW_END}",
        f"MIN_ODD/MAX_ODD: {MIN_ODD:.2f}/{MAX_ODD:.2f}",
        f"EV_MIN_PCT: {EV_MIN_PCT:.2f}%",
        f"BANKROLL: R${BANKROLL:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
        f"BOOKMAKER_ID: {BOOKMAKER_ID} ({BOOKMAKER_NAME})",
        f"LEAGUE_IDS: {LEAGUE_IDS_RAW or '(não definido, todas as ligas)'}",
        f"USE_API_FOOTBALL_ODDS: {USE_API_FOOTBALL_ODDS}",
        f"USE_STATS: {USE_STATS}",
    ]
    if LAST_ERROR:
        lines.append("")
        lines.append(f"Último erro: {LAST_ERROR}")
    await update.message.reply_text("\n".join(lines))


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [
        "🔗 Links úteis",
        f"- Bookmaker: {BOOKMAKER_NAME} → {BOOKMAKER_URL}",
        "- API-Football Dashboard: https://dashboard.api-football.com/",
    ]
    await update.message.reply_text("\n".join(lines))


# ============================================================
#  LOOP AUTOSCAN & HTTP DUMMY
# ============================================================

async def autoscan_loop(application: Application) -> None:
    logger.info("Autoscan iniciado (intervalo=%ss)", CHECK_INTERVAL)
    while True:
        try:
            await scan_once(application)
        except Exception as e:
            logger.exception("Erro no autoscan: %s", e)
        await asyncio.sleep(CHECK_INTERVAL)


async def dummy_http_server() -> None:
    import socket

    host = "0.0.0.0"
    port = int(os.getenv("PORT", "8080"))
    logger.info("Servidor HTTP dummy ouvindo em (%s, %s)", host, port)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, port))
        s.listen(5)
        while True:
            conn, _ = s.accept()
            with conn:
                try:
                    _ = conn.recv(1024)
                    response = (
                        "HTTP/1.1 200 OK\r\n"
                        "Content-Type: text/plain\r\n"
                        "Content-Length: 2\r\n"
                        "\r\nOK"
                    )
                    conn.sendall(response.encode("utf-8"))
                except Exception:
                    pass


async def post_init(application: Application) -> None:
    if AUTOSTART:
        application.create_task(autoscan_loop(application))
    if os.getenv("PORT"):
        application.create_task(dummy_http_server())
    logger.info("post_init executado. Autoscan=%s", bool(AUTOSTART))


# ============================================================
#  BUILD & MAIN
# ============================================================

def build_application() -> Application:
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

    return application


def main() -> None:
    logger.info("Iniciando bot do EvRadar PRO v0.2 (Local/Railway)...")
    application = build_application()
    application.run_polling()


def extract_over_line_odd(
    odds_payload: Dict[str, Any],
    total_goals: int,
) -> Optional[float]:
    """
    Procura no payload de odds a linha Over (soma + 0,5) do placar atual.
    Ex.: placar 1–0 -> linha Over 1.5.
    """
    target_line = f"Over {total_goals + 0.5}"
    for bookmaker in odds_payload.get("bookmakers", []):
        for bet in bookmaker.get("bets", []):
            name = bet.get("name", "") or ""
            if "Goals" in name or "Over/Under" in name:
                for v in bet.get("values", []):
                    val = v.get("value", "")
                    if val == target_line:
                        odd_str = v.get("odd")
                        if not odd_str:
                            continue
                        try:
                            return float(odd_str.replace(",", "."))
                        except ValueError:
                            continue
    return None


if __name__ == "__main__":
    main()
