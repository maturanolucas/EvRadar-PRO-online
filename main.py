import asyncio
import logging
import math
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

API_FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"


@dataclass
class Config:
    api_key: str
    telegram_token: str
    telegram_chat_id: str
    autostart: bool
    check_interval_ms: int
    window_start: int
    window_end: int
    min_odd: float
    max_odd: float
    target_odd: float
    ev_min_pct: float
    cooldown_minutes: float
    league_ids: Set[int]
    bookmaker_id: int
    bookmaker_name: str
    bookmaker_url: str
    use_api_football_odds: bool
    bankroll_initial: float
    kelly_fraction: float
    stake_max_pct: float


CONFIG: Optional[Config] = None
HTTP_CLIENT: Optional[httpx.AsyncClient] = None
LAST_SUMMARY: str = "Ainda não houve nenhum scan."
LAST_DEBUG_INFO: str = ""
LAST_ALERT_TS_BY_FIXTURE: Dict[int, float] = {}
BANKROLL_STATE: Dict[str, float] = {"current": 0.0}
SCAN_LOCK = asyncio.Lock()


def _get_env(name: str, default: Optional[str] = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and (value is None or value == ""):
        raise RuntimeError(f"Variável de ambiente obrigatória ausente: {name}")
    return value or ""


def _parse_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    value = value.strip().lower()
    if value in {"1", "true", "t", "yes", "y", "sim"}:
        return True
    if value in {"0", "false", "f", "no", "n", "nao", "não"}:
        return False
    return default


def _parse_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_float(value: str, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_league_ids(raw: str) -> Set[int]:
    ids: Set[int] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logging.warning("LEAGUE_IDS contém valor não numérico: %s", part)
    return ids


def load_config() -> Config:
    api_key = _get_env("API_FOOTBALL_KEY", required=True)
    telegram_token = _get_env("TELEGRAM_BOT_TOKEN", required=True)
    telegram_chat_id = _get_env("TELEGRAM_CHAT_ID", required=True)

    autostart = _parse_bool(_get_env("AUTOSTART", "0"), default=False)
    check_interval_ms = _parse_int(_get_env("CHECK_INTERVAL", "20000"), 20000)

    window_start = _parse_int(_get_env("WINDOW_START", "47"), 47)
    window_end = _parse_int(_get_env("WINDOW_END", "75"), 75)

    min_odd = _parse_float(_get_env("MIN_ODD", "1.47"), 1.47)
    max_odd = _parse_float(_get_env("MAX_ODD", "3.50"), 3.50)
    target_odd = _parse_float(_get_env("TARGET_ODD", "1.70"), 1.70)

    ev_min_pct = _parse_float(_get_env("EV_MIN_PCT", "4.00"), 4.00)
    cooldown_minutes = _parse_float(_get_env("COOLDOWN_MINUTES", "6"), 6.0)

    league_ids = _parse_league_ids(_get_env("LEAGUE_IDS", ""))

    bookmaker_id = _parse_int(_get_env("BOOKMAKER_ID", "34"), 34)
    bookmaker_name = _get_env("BOOKMAKER_NAME", "Superbet")
    bookmaker_url = _get_env("BOOKMAKER_URL", "https://www.superbet.com/")
    use_odds = _parse_bool(_get_env("USE_API_FOOTBALL_ODDS", "1"), default=True)

    bankroll_initial = _parse_float(_get_env("BANKROLL_INITIAL", "5000"), 5000.0)
    kelly_fraction = _parse_float(_get_env("KELLY_FRACTION", "0.5"), 0.5)
    stake_max_pct = _parse_float(_get_env("STAKE_MAX_PCT", "3.0"), 3.0)

    cfg = Config(
        api_key=api_key,
        telegram_token=telegram_token,
        telegram_chat_id=telegram_chat_id,
        autostart=autostart,
        check_interval_ms=check_interval_ms,
        window_start=window_start,
        window_end=window_end,
        min_odd=min_odd,
        max_odd=max_odd,
        target_odd=target_odd,
        ev_min_pct=ev_min_pct,
        cooldown_minutes=cooldown_minutes,
        league_ids=league_ids,
        bookmaker_id=bookmaker_id,
        bookmaker_name=bookmaker_name,
        bookmaker_url=bookmaker_url,
        use_api_football_odds=use_odds,
        bankroll_initial=bankroll_initial,
        kelly_fraction=kelly_fraction,
        stake_max_pct=stake_max_pct,
    )

    BANKROLL_STATE["current"] = bankroll_initial

    logging.info("Config carregada: %s", cfg)
    logging.info("Bankroll inicial definido em: %.2f", bankroll_initial)
    return cfg


async def build_http_client(cfg: Config) -> httpx.AsyncClient:
    headers = {
        "x-apisports-key": cfg.api_key,
        "Accept": "application/json",
    }
    timeout = httpx.Timeout(10.0, connect=5.0)
    client = httpx.AsyncClient(
        base_url=API_FOOTBALL_BASE_URL,
        headers=headers,
        timeout=timeout,
    )
    return client


async def api_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if HTTP_CLIENT is None:
        raise RuntimeError("HTTP_CLIENT não inicializado")
    try:
        response = await HTTP_CLIENT.get(path, params=params or {})
        response.raise_for_status()
        data = response.json()
        return data
    except httpx.HTTPError as exc:
        logging.error("Erro ao chamar API-FOOTBALL em %s: %s", path, exc)
        return {"errors": str(exc), "response": []}


async def fetch_live_fixtures() -> List[Dict[str, Any]]:
    data = await api_get("/fixtures", params={"live": "all"})
    fixtures = data.get("response", []) or []
    if not isinstance(fixtures, list):
        return []
    return fixtures


async def fetch_fixture_stats(fixture_id: int) -> Dict[str, Any]:
    data = await api_get("/fixtures/statistics", params={"fixture": fixture_id})
    resp = data.get("response", []) or []
    stats_map: Dict[str, Dict[str, float]] = {}
    for team_block in resp:
        team = (team_block.get("team") or {}).get("name") or ""
        stats_list = team_block.get("statistics") or []
        stat_dict: Dict[str, float] = {}
        for s in stats_list:
            t = s.get("type")
            v = s.get("value")
            if t is None or v is None:
                continue
            try:
                if isinstance(v, (int, float)):
                    stat_dict[t] = float(v)
                elif isinstance(v, str) and v.endswith("%"):
                    num = float(v.strip().replace("%", ""))
                    stat_dict[t] = num
                else:
                    num = float(v)
                    stat_dict[t] = num
            except Exception:
                continue
        if team:
            stats_map[team] = stat_dict
    return stats_map


async def fetch_fixture_odds(fixture_id: int, bookmaker_id: int) -> Optional[float]:
    """
    Tenta pegar odd de Over 0.5 gols no mercado de Over/Under.
    Se não achar, retorna None e usamos TARGET_ODD.
    """
    data = await api_get("/odds", params={"fixture": fixture_id, "bookmaker": bookmaker_id})
    resp = data.get("response", []) or []
    best_over_05: Optional[float] = None
    for item in resp:
        bookies = item.get("bookmakers") or []
        for b in bookies:
            bets = b.get("bets") or []
            for bet in bets:
                name = (bet.get("name") or "").lower()
                if "over/under" not in name and "total goals" not in name:
                    continue
                values = bet.get("values") or []
                for v in values:
                    val_handicap = (v.get("value") or "").lower()
                    try:
                        if "0.5" not in val_handicap:
                            continue
                        odd_str = v.get("odd")
                        if odd_str is None:
                            continue
                        odd = float(odd_str)
                        if best_over_05 is None or odd > best_over_05:
                            best_over_05 = odd
                    except Exception:
                        continue
    return best_over_05


def _extract_fixture_core(fx: Dict[str, Any]) -> Tuple[int, str, str, str, int, int, int]:
    fixture = fx.get("fixture", {})
    teams = fx.get("teams", {})
    goals = fx.get("goals", {})
    league = fx.get("league", {})

    fixture_id = fixture.get("id") or 0
    league_id = league.get("id") or 0
    league_name = league.get("name") or "Liga desconhecida"

    home_team = (teams.get("home") or {}).get("name") or "Time da casa"
    away_team = (teams.get("away") or {}).get("name") or "Time visitante"

    minute = (fixture.get("status") or {}).get("elapsed") or 0
    home_goals = goals.get("home") or 0
    away_goals = goals.get("away") or 0

    return (
        fixture_id,
        league_name,
        home_team,
        away_team,
        league_id,
        minute,
        int(home_goals) + int(away_goals),
    )


def _safe_get_stat(stats_map: Dict[str, Dict[str, float]], team: str, key: str) -> float:
    team_stats = stats_map.get(team) or {}
    return float(team_stats.get(key) or 0.0)


def _compute_pressao_index(
    minute: int,
    home_team: str,
    away_team: str,
    total_goals: int,
    stats_map: Dict[str, Dict[str, float]],
) -> Tuple[float, str]:
    """
    Retorna (pressao_0_10, time_atacando)
    Usa chutes, chutes no alvo, ataques perigosos e posse.
    """
    home_shots = _safe_get_stat(stats_map, home_team, "Total Shots")
    away_shots = _safe_get_stat(stats_map, away_team, "Total Shots")

    home_shots_ot = _safe_get_stat(stats_map, home_team, "Shots on Goal")
    away_shots_ot = _safe_get_stat(stats_map, away_team, "Shots on Goal")

    home_danger = _safe_get_stat(stats_map, home_team, "Dangerous Attacks")
    away_danger = _safe_get_stat(stats_map, away_team, "Dangerous Attacks")

    home_poss = _safe_get_stat(stats_map, home_team, "Ball Possession")
    away_poss = _safe_get_stat(stats_map, away_team, "Ball Possession")

    home_score = (
        home_shots * 0.15
        + home_shots_ot * 0.4
        + home_danger * 0.08
        + home_poss * 0.03
    )
    away_score = (
        away_shots * 0.15
        + away_shots_ot * 0.4
        + away_danger * 0.08
        + away_poss * 0.03
    )

    diff = home_score - away_score
    if diff > 0:
        atk_team = home_team
    elif diff < 0:
        atk_team = away_team
    else:
        atk_team = home_team

    max_abs = max(abs(home_score), abs(away_score), 1.0)
    pressao_raw = abs(diff) / max_abs
    pressao_scaled = max(0.0, min(10.0, pressao_raw * 10.0))

    return pressao_scaled, atk_team


def _model_probability_advanced(
    minute: int,
    total_goals: int,
    pressao: float,
    news_boost: float,
) -> float:
    """
    Modelo WR+:
    - Base: tempo + total de gols
    - Multiplicadores: pressão (0–10) e news_boost (-2..+2)
    """
    minutes_left = max(0, 90 - minute)
    base_lambda = 0.035

    if total_goals == 0:
        base_lambda *= 0.9
    elif total_goals == 1:
        base_lambda *= 1.05
    elif total_goals == 2:
        base_lambda *= 1.20
    else:
        base_lambda *= 1.35

    pressao_factor = 1.0 + (pressao - 5.0) * 0.05
    pressao_factor = max(0.75, min(1.25, pressao_factor))
    base_lambda *= pressao_factor

    nb_factor = 1.0 + news_boost * 0.075
    nb_factor = max(0.7, min(1.3, nb_factor))
    base_lambda *= nb_factor

    if minutes_left <= 0:
        return 0.0

    p = 1.0 - math.exp(-base_lambda * minutes_left)
    p = max(0.01, min(0.99, p))
    return p


def _kelly_stake_pct(p: float, odd: float, frac: float, stake_max_pct: float) -> float:
    if odd <= 1.0:
        return 0.0
    q = 1.0 - p
    edge = p * (odd - 1.0) - q
    if edge <= 0:
        return 0.0
    f_star = edge / (odd - 1.0)
    stake_pct = max(0.0, f_star * frac * 100.0)
    if stake_pct > stake_max_pct:
        stake_pct = stake_max_pct
    return stake_pct


def _classify_tier(ev_pct: float) -> str:
    if ev_pct >= 7.0:
        return "Tier A — Sinal muito forte"
    if ev_pct >= 5.0:
        return "Tier B — Sinal forte"
    if ev_pct >= 3.0:
        return "Tier C — Sinal moderado"
    return "Sinal fraco"


async def evaluate_fixture(fx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    global CONFIG
    if CONFIG is None:
        return None

    (
        fixture_id,
        league_name,
        home_team,
        away_team,
        league_id,
        minute,
        total_goals,
    ) = _extract_fixture_core(fx)

    if CONFIG.league_ids and league_id not in CONFIG.league_ids:
        return None

    if minute < CONFIG.window_start or minute > CONFIG.window_end:
        return None

    now_ts = time.time()
    last_ts = LAST_ALERT_TS_BY_FIXTURE.get(fixture_id, 0)
    if now_ts - last_ts < CONFIG.cooldown_minutes * 60.0:
        return None

    stats_map = await fetch_fixture_stats(fixture_id)
    pressao, atk_team = _compute_pressao_index(
        minute=minute,
        home_team=home_team,
        away_team=away_team,
        total_goals=total_goals,
        stats_map=stats_map,
    )

    news_boost = 0.0  # placeholder pra camada de notícias no futuro

    p_goal = _model_probability_advanced(
        minute=minute,
        total_goals=total_goals,
        pressao=pressao,
        news_boost=news_boost,
    )

    odd = CONFIG.target_odd
    if CONFIG.use_api_football_odds:
        live_odd = await fetch_fixture_odds(fixture_id, CONFIG.bookmaker_id)
        if live_odd is not None:
            odd = live_odd

    if odd <= 1.01:
        return None

    if odd < CONFIG.min_odd or odd > CONFIG.max_odd:
        return None

    ev = p_goal * odd - 1.0
    ev_pct = ev * 100.0
    if ev_pct < CONFIG.ev_min_pct:
        return None

    LAST_ALERT_TS_BY_FIXTURE[fixture_id] = now_ts

    stake_pct = _kelly_stake_pct(
        p=p_goal,
        odd=odd,
        frac=CONFIG.kelly_fraction,
        stake_max_pct=CONFIG.stake_max_pct,
    )
    stake_brl = BANKROLL_STATE["current"] * (stake_pct / 100.0)

    event = {
        "fixture_id": fixture_id,
        "league_name": league_name,
        "home_team": home_team,
        "away_team": away_team,
        "minute": minute,
        "total_goals": total_goals,
        "pressao": pressao,
        "atk_team": atk_team,
        "p_goal": p_goal,
        "odd": odd,
        "ev_pct": ev_pct,
        "stake_pct": stake_pct,
        "stake_brl": stake_brl,
    }
    return event


def format_alert(event: Dict[str, Any]) -> str:
    global CONFIG
    if CONFIG is None:
        CONFIG = load_config()

    line_text = f"Over (soma + 0,5) @ {event['odd']:.2f}"
    prob_pct = event["p_goal"] * 100.0
    fair_odd = 1.0 / max(1e-6, event["p_goal"])

    tier_label = _classify_tier(event["ev_pct"])
    header = tier_label

    stake_pct = event["stake_pct"]
    stake_brl = event["stake_brl"]

    pressao_txt = f"{event['pressao']:.1f}/10"
    quem_aperta = event["atk_team"]

    linhas = [
        f"🔔 {header}",
        "",
        f"🏟️ {event['home_team']} vs {event['away_team']} — {event['league_name']}",
        f"⏱️ {event['minute']}' | 🔢 {event['total_goals']} gols (soma)",
        f"⚙️ Linha: {line_text}",
        "",
        "📊 Probabilidade & valor:",
        f"- P_final (gol a mais): {prob_pct:.1f}%",
        f"- Odd justa (modelo WR+): {fair_odd:.2f}",
        f"- Odd do momento (usada): {event['odd']:.2f}",
        f"- EV estimado: {event['ev_pct']:.2f}%",
        "",
        "🔥 Ritmo / pressão:",
        f"- Pressão: {pressao_txt} (time que mais aperta: {quem_aperta})",
        "",
        "💰 Stake sugerida (Kelly fracionado):",
        f"- ~{stake_pct:.2f}% da banca",
        f"- Aproximado: R${stake_brl:.2f}",
        "",
        "🧩 Interpretação:",
        "Jogo quente no 2º tempo, com padrão de pressão e estatística compatível com gol a mais.",
        "Use este sinal como guia, alinhando com seu faro e disciplina de valor.",
        "",
        f"🔗 Abrir mercado ({CONFIG.bookmaker_name}): {CONFIG.bookmaker_url}",
    ]

    return "\n".join(linhas)


async def run_scan(origin: str, bot) -> str:
    """
    Ajuste importante:
    - /scan (manual)        → SEMPRE manda resumo pro Telegram.
    - loop auto (origem=auto) → SÓ manda resumo se houve pelo menos 1 alerta.
    """
    global CONFIG, LAST_SUMMARY, LAST_DEBUG_INFO

    if CONFIG is None:
        CONFIG = load_config()

    async with SCAN_LOCK:
        fixtures = await fetch_live_fixtures()
        total_live = len(fixtures)

        analisados = 0
        eventos_aceitos: List[Dict[str, Any]] = []

        for fx in fixtures:
            result = await evaluate_fixture(fx)
            if result is None:
                continue
            analisados += 1
            eventos_aceitos.append(result)

        alertas = len(eventos_aceitos)

        for ev in eventos_aceitos:
            msg = format_alert(ev)
            await bot.send_message(chat_id=CONFIG.telegram_chat_id, text=msg)

        summary = (
            f"[EvRadar PRO] Scan concluído (origem={origin}). "
            f"Eventos ao vivo: {total_live} | "
            f"Jogos analisados na janela: {analisados} | "
            f"Alertas enviados: {alertas}."
        )

        LAST_SUMMARY = summary
        LAST_DEBUG_INFO = (
            f"live={total_live}, analisados={analisados}, alertas={alertas}, "
            f"janela={CONFIG.window_start}-{CONFIG.window_end}, "
            f"EV_MIN={CONFIG.ev_min_pct:.2f}%, "
            f"odd_ref={CONFIG.target_odd:.2f}, "
            f"ligas_filtradas={sorted(CONFIG.league_ids) if CONFIG.league_ids else 'todas'}, "
            f"STAKE_MAX={CONFIG.stake_max_pct:.2f}%"
        )

        # 🔇 Auto-loop silencioso se não teve alerta (evita spam)
        if origin == "auto" and alertas == 0:
            logging.info(summary)
            logging.info("Debug scan: %s", LAST_DEBUG_INFO)
            return summary

        # Manual (e auto com alerta) → manda resumo pro Telegram
        await bot.send_message(chat_id=CONFIG.telegram_chat_id, text=summary)
        logging.info(summary)
        logging.info("Debug scan: %s", LAST_DEBUG_INFO)

        return summary


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global CONFIG
    if CONFIG is None:
        CONFIG = load_config()

    linhas = [
        "👋 EvRadar PRO WR+ online.",
        "",
        f"Janela padrão: {CONFIG.window_start}–{CONFIG.window_end}ʼ",
        f"Odds alvo: {CONFIG.min_odd:.2f}–{CONFIG.max_odd:.2f}",
        f"Odd ref (TARGET_ODD): {CONFIG.target_odd:.2f}",
        f"EV mínimo: {CONFIG.ev_min_pct:.2f}%",
        f"Cooldown por jogo: {CONFIG.cooldown_minutes:.1f} min",
        f"AUTOSTART: {'ligado' if CONFIG.autostart else 'desligado'}",
        f"Bankroll inicial: R${CONFIG.bankroll_initial:.2f}",
        f"Kelly fracionado: {CONFIG.kelly_fraction:.2f}x (cap {CONFIG.stake_max_pct:.2f}% da banca)",
        "",
        "Comandos:",
        "  /scan   → rodar varredura agora",
        "  /status → ver último resumo",
        "  /debug  → info técnica",
        "  /links  → links úteis / bookmaker",
    ]

    await update.message.reply_text("\n".join(linhas))


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global CONFIG
    if CONFIG is None:
        CONFIG = load_config()

    await update.message.reply_text("🔍 Iniciando varredura manual de jogos ao vivo (WR+)...")
    await run_scan("manual", context.bot)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global LAST_SUMMARY, BANKROLL_STATE
    linhas = [
        LAST_SUMMARY,
        "",
        f"Bankroll atual (em memória): R${BANKROLL_STATE['current']:.2f}",
    ]
    await update.message.reply_text("\n".join(linhas))


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global CONFIG, LAST_DEBUG_INFO
    if CONFIG is None:
        CONFIG = load_config()

    linhas = [
        "🛠 Debug EvRadar PRO WR+",
        "",
        LAST_DEBUG_INFO or "Ainda não houve nenhum scan.",
        "",
        f"LEAGUE_IDS carregadas: {sorted(CONFIG.league_ids) if CONFIG.league_ids else 'todas'}",
        f"CHECK_INTERVAL: {CONFIG.check_interval_ms} ms",
        f"USE_API_FOOTBALL_ODDS: {CONFIG.use_api_football_odds}",
        f"BOOKMAKER_ID: {CONFIG.bookmaker_id} ({CONFIG.bookmaker_name})",
    ]
    await update.message.reply_text("\n".join(linhas))


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global CONFIG
    if CONFIG is None:
        CONFIG = load_config()

    linhas = [
        "🔗 Links úteis:",
        f"- Bookmaker principal ({CONFIG.bookmaker_name}): {CONFIG.bookmaker_url}",
        "",
        "Dica: mantenha o radar rodando e use este link para abrir o mercado rapidamente.",
    ]
    await update.message.reply_text("\n".join(linhas))


async def auto_scan_loop(app) -> None:
    global CONFIG
    if CONFIG is None:
        CONFIG = load_config()

    if not CONFIG.autostart:
        logging.info("AUTOSTART=0 → loop automático desativado.")
        return

    logging.info(
        "Iniciando loop automático WR+: intervalo=%d ms (%.1fs)",
        CONFIG.check_interval_ms,
        CONFIG.check_interval_ms / 1000.0,
    )

    while True:
        try:
            await run_scan("auto", app.bot)
        except Exception as exc:
            logging.exception("Erro no loop automático de scan: %s", exc)
        await asyncio.sleep(CONFIG.check_interval_ms / 1000.0)


async def post_init(app) -> None:
    global CONFIG, HTTP_CLIENT
    if CONFIG is None:
        CONFIG = load_config()

    HTTP_CLIENT = await build_http_client(CONFIG)
    logging.info("HTTP_CLIENT inicializado.")

    asyncio.create_task(auto_scan_loop(app))


def main() -> None:
    global CONFIG

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )

    CONFIG = load_config()

    application = (
        ApplicationBuilder()
        .token(CONFIG.telegram_token)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("debug", cmd_debug))
    application.add_handler(CommandHandler("links", cmd_links))

    logging.info("Iniciando EvRadar PRO WR+ (run_polling)...")
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
