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

# ============================================================
#  EvRadar PRO — Bot Telegram + API-FOOTBALL (versão compacta)
#  Tudo em um arquivo só (main.py), focado em:
#   - Varredura manual (/scan) e automática (AUTOSTART)
#   - Filtro de ligas por LEAGUE_IDS (feito APENAS no Python)
#   - Janela de minutos, faixa de odds, EV mínimo simples
#  IMPORTANTE: o modelo de probabilidade é simples (tempo + gols).
# ============================================================

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
    bookmaker_name: str
    bookmaker_url: str
    use_api_football_odds: bool


# Estado global simples (para evitar banco por enquanto)
CONFIG: Optional[Config] = None
HTTP_CLIENT: Optional[httpx.AsyncClient] = None
LAST_SUMMARY: str = "Ainda não houve nenhum scan."
LAST_DEBUG_INFO: str = ""
LAST_ALERT_TS_BY_FIXTURE: Dict[int, float] = {}
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
    max_odd = _parse_float(_get_env("MAX_ODD", "2.30"), 2.30)
    target_odd = _parse_float(_get_env("TARGET_ODD", "1.70"), 1.70)

    ev_min_pct = _parse_float(_get_env("EV_MIN_PCT", "1.60"), 1.60)
    cooldown_minutes = _parse_float(_get_env("COOLDOWN_MINUTES", "6"), 6.0)

    league_ids = _parse_league_ids(_get_env("LEAGUE_IDS", ""))

    bookmaker_name = _get_env("BOOKMAKER_NAME", "Superbet")
    bookmaker_url = _get_env("BOOKMAKER_URL", "https://www.superbet.com/")
    use_odds = _parse_bool(_get_env("USE_API_FOOTBALL_ODDS", "0"), default=False)

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
        bookmaker_name=bookmaker_name,
        bookmaker_url=bookmaker_url,
        use_api_football_odds=use_odds,
    )

    logging.info("Config carregada: %s", cfg)
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
    """
    Busca TODOS os jogos ao vivo e o filtro de ligas é feito
    SOMENTE aqui no Python usando CONFIG.league_ids.
    Isso evita quebrar a API com parâmetro league inválido
    e evita radar mudo por query errada.
    """
    data = await api_get("/fixtures", params={"live": "all"})
    fixtures = data.get("response", []) or []
    if not isinstance(fixtures, list):
        return []
    return fixtures


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


def _model_probability(minute: int, total_goals: int) -> float:
    """
    Modelo simples de probabilidade de sair +1 gol até os 90'.
    Usa apenas tempo e total de gols. Não é o modelo parrudo,
    mas já cria uma noção de valor.
    """
    minutes_left = max(0, 90 - minute)
    base_lambda = 0.035

    if total_goals == 0:
        base_lambda *= 0.9
    elif total_goals == 1:
        base_lambda *= 1.05
    elif total_goals == 2:
        base_lambda *= 1.15
    else:
        base_lambda *= 1.25

    if minutes_left <= 0:
        return 0.0

    p = 1.0 - math.exp(-base_lambda * minutes_left)
    p = max(0.01, min(0.99, p))
    return p


def _classify_tier(ev_pct: float) -> str:
    if ev_pct >= 7.0:
        return "Tier A — Sinal muito forte"
    if ev_pct >= 4.0:
        return "Tier B — Sinal forte"
    if ev_pct >= 2.0:
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

    # Filtro de ligas (feito apenas aqui, NÃO na URL)
    if CONFIG.league_ids and league_id not in CONFIG.league_ids:
        return None

    # Janela de minutos (2º tempo padrão 47–75)
    if minute < CONFIG.window_start or minute > CONFIG.window_end:
        return None

    # Cooldown por jogo (evita spam)
    now_ts = time.time()
    last_ts = LAST_ALERT_TS_BY_FIXTURE.get(fixture_id, 0)
    if now_ts - last_ts < CONFIG.cooldown_minutes * 60.0:
        logging.debug(
            "Ignorando fixture %s por cooldown (%.1fs restante)",
            fixture_id,
            CONFIG.cooldown_minutes * 60.0 - (now_ts - last_ts),
        )
        return None

    # Modelo simples de probabilidade
    p_goal = _model_probability(minute, total_goals)

    # Odd usada para o cálculo de EV:
    # - se no futuro integrar odds reais da API_FOOTBALL, encaixa aqui
    odd = CONFIG.target_odd
    if odd <= 1.0:
        return None

    ev = p_goal * odd - 1.0
    ev_pct = ev * 100.0

    if ev_pct < CONFIG.ev_min_pct:
        return None

    # Respeita faixa de odds "esperada"
    if odd < CONFIG.min_odd or odd > CONFIG.max_odd:
        return None

    LAST_ALERT_TS_BY_FIXTURE[fixture_id] = now_ts

    return {
        "fixture_id": fixture_id,
        "league_name": league_name,
        "home_team": home_team,
        "away_team": away_team,
        "minute": minute,
        "total_goals": total_goals,
        "p_goal": p_goal,
        "odd": odd,
        "ev_pct": ev_pct,
    }


def format_alert(event: Dict[str, Any]) -> str:
    global CONFIG
    if CONFIG is None:
        CONFIG = load_config()

    line_text = f"Over (soma + 0,5) @ {event['odd']:.2f}"
    prob_pct = event["p_goal"] * 100.0
    fair_odd = 1.0 / max(1e-6, event["p_goal"])

    tier_label = _classify_tier(event["ev_pct"])

    header = tier_label

    linhas = [
        f"🔔 {header}",
        "",
        f"🏟️ {event['home_team']} vs {event['away_team']} — {event['league_name']}",
        f"⏱️ {event['minute']}' | 🔢 {event['total_goals']} gols (soma)",
        f"⚙️ Linha: {line_text}",
        "",
        "📊 Probabilidade & valor:",
        f"- P_final (gol a mais): {prob_pct:.1f}%",
        f"- Odd justa (modelo simples): {fair_odd:.2f}",
        f"- Odd usada no cálculo: {event['odd']:.2f}",
        f"- EV estimado: {event['ev_pct']:.2f}%",
        "",
        "💰 Interpretação:",
        "Jogo dentro da janela, com probabilidade interessante de sair mais 1 gol.",
        "Avalie contexto, pressão em campo e news antes de clicar.",
        "",
        f"🔗 Abrir mercado ({CONFIG.bookmaker_name}): {CONFIG.bookmaker_url}",
    ]
    return "\n".join(linhas)


async def run_scan(origin: str, bot) -> str:
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
            f"ligas_filtradas={sorted(CONFIG.league_ids) if CONFIG.league_ids else 'todas'}"
        )

        await bot.send_message(chat_id=CONFIG.telegram_chat_id, text=summary)

        logging.info(summary)
        logging.info("Debug scan: %s", LAST_DEBUG_INFO)

        return summary


# ===================== Comandos Telegram =====================


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global CONFIG
    if CONFIG is None:
        CONFIG = load_config()

    linhas = [
        "👋 EvRadar PRO online.",
        "",
        f"Janela padrão: {CONFIG.window_start}–{CONFIG.window_end}ʼ",
        f"Odd ref (TARGET_ODD): {CONFIG.target_odd:.2f}",
        f"EV mínimo: {CONFIG.ev_min_pct:.2f}%",
        f"Cooldown por jogo: {CONFIG.cooldown_minutes:.1f} min",
        f"AUTOSTART: {'ligado' if CONFIG.autostart else 'desligado'}",
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

    await update.message.reply_text("🔍 Iniciando varredura manual de jogos ao vivo...")
    await run_scan("manual", context.bot)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global LAST_SUMMARY
    await update.message.reply_text(LAST_SUMMARY)


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global CONFIG, LAST_DEBUG_INFO
    if CONFIG is None:
        CONFIG = load_config()

    linhas = [
        "🛠 Debug EvRadar PRO",
        "",
        LAST_DEBUG_INFO or "Ainda não houve nenhum scan.",
        "",
        f"LEAGUE_IDS carregadas: {sorted(CONFIG.league_ids) if CONFIG.league_ids else 'todas'}",
        f"CHECK_INTERVAL: {CONFIG.check_interval_ms} ms",
        f"USE_API_FOOTBALL_ODDS (placeholder): {CONFIG.use_api_football_odds}",
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


# ===================== Loop automático =====================


async def auto_scan_loop(app) -> None:
    global CONFIG
    if CONFIG is None:
        CONFIG = load_config()

    if not CONFIG.autostart:
        logging.info("AUTOSTART=0 → loop automático desativado.")
        return

    logging.info(
        "Iniciando loop automático: intervalo=%d ms (%.1fs)",
        CONFIG.check_interval_ms,
        CONFIG.check_interval_ms / 1000.0,
    )

    # Evita estourar o limite da API: comece com intervalos maiores.
    while True:
        try:
            await run_scan("auto", app.bot)
        except Exception as exc:
            logging.exception("Erro no loop automático de scan: %s", exc)
        await asyncio.sleep(CONFIG.check_interval_ms / 1000.0)


async def post_init(app) -> None:
    """
    Executado automaticamente pelo python-telegram-bot dentro do mesmo
    event loop do run_polling. É aqui que inicializamos o HTTP_CLIENT
    e disparamos o loop automático se AUTOSTART=1.
    """
    global CONFIG, HTTP_CLIENT
    if CONFIG is None:
        CONFIG = load_config()

    HTTP_CLIENT = await build_http_client(CONFIG)
    logging.info("HTTP_CLIENT inicializado.")

    # Loop automático em background (sem JobQueue)
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

    logging.info("Iniciando EvRadar PRO (run_polling)...")
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
