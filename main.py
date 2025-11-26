import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any, Dict, List, Tuple, Optional

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# Logging
# ============================================================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("EvRadarPRO")

# ============================================================
# Env & Config
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0") or "0")

API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
API_FOOTBALL_BASE = "https://v3.football.api-sports.io"

AUTOSTART = int(os.getenv("AUTOSTART", "1"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))  # segundos

WINDOW_START = int(os.getenv("WINDOW_START", "47"))
WINDOW_END = int(os.getenv("WINDOW_END", "85"))

MIN_ODD = float(os.getenv("MIN_ODD", "1.47"))
MAX_ODD = float(os.getenv("MAX_ODD", "3.50"))
EV_MIN_PCT = float(os.getenv("EV_MIN_PCT", "4.0"))

TARGET_ODD = float(os.getenv("TARGET_ODD", "1.70"))

USE_API_FOOTBALL_ODDS = int(os.getenv("USE_API_FOOTBALL_ODDS", "0"))
BOOKMAKER_ID = int(os.getenv("BOOKMAKER_ID", "34") or "34")
BOOKMAKER_NAME = os.getenv("BOOKMAKER_NAME", "Superbet")
BOOKMAKER_URL = os.getenv("BOOKMAKER_URL", "https://www.superbet.com/")

LEAGUE_IDS_RAW = os.getenv("LEAGUE_IDS", "")
LEAGUE_IDS: List[int] = []
for part in LEAGUE_IDS_RAW.split(","):
    part = part.strip()
    if not part:
        continue
    try:
        LEAGUE_IDS.append(int(part))
    except ValueError:
        logger.warning("Valor inválido em LEAGUE_IDS: %r", part)
logger.info("[LEAGUE_IDS] Permitidos: %s", LEAGUE_IDS)

# === Camada de notícias (NewsAPI) ===
NEWS_ENABLED = int(os.getenv("NEWS_ENABLED", "1"))
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "").strip()
NEWS_LOOKBACK_HOURS = int(os.getenv("NEWS_LOOKBACK_HOURS", "30"))

# ============================================================
# HTTP dummy server para health check no Railway
# ============================================================


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"EvRadar PRO online")

    def log_message(self, fmt: str, *args: Any) -> None:
        # suprime log padrão do http.server
        logger.debug("HTTPServer: " + fmt, *args)


def start_health_server(host: str = "0.0.0.0", port: int = 8080) -> None:
    def _run() -> None:
        httpd = HTTPServer((host, port), HealthHandler)
        logger.info("Servidor HTTP dummy ouvindo em %s:%d", host, port)
        httpd.serve_forever()

    thread = Thread(target=_run, daemon=True)
    thread.start()


# ============================================================
# Utils
# ============================================================

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ============================================================
# Camada de notícias (NewsAPI)
# ============================================================

async def fetch_raw_news(query: str) -> List[Dict[str, Any]]:
    """
    Busca notícias recentes usando NewsAPI (se NEWS_API_KEY estiver setado).
    Retorna lista de artigos (title, description, content).
    Se não tiver API, retorna lista vazia.
    """
    if not NEWS_ENABLED or not NEWS_API_KEY:
        return []

    url = "https://newsapi.org/v2/everything"

    now = datetime.now(timezone.utc)
    from_param = (now - timedelta(hours=NEWS_LOOKBACK_HOURS)).isoformat()

    params = {
        "q": query,
        "from": from_param,
        "sortBy": "publishedAt",
        "language": "pt",
        "apiKey": NEWS_API_KEY,
        "pageSize": 20,
    }

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get("articles", [])
        except Exception as e:
            logger.warning("[news] Erro ao buscar notícias para query=%r: %s", query, e)
            return []


def score_news_articles(
    articles: List[Dict[str, Any]],
    home: str,
    away: str,
) -> Tuple[float, str]:
    """
    Lê títulos/descrições e tenta entender se o contexto favorece ou atrapalha o gol.
    Retorna (boost_em_pontos_percentuais, motivo_resumido).
    Range alvo: -2.0 a +2.0 pontos percentuais.
    """
    if not articles:
        return 0.0, "Sem notícias relevantes recentes"

    text_blob_parts: List[str] = []
    for a in articles:
        title = a.get("title") or ""
        desc = a.get("description") or ""
        text_blob_parts.append(str(title))
        text_blob_parts.append(str(desc))
    text_blob = " ".join(text_blob_parts).lower()

    negativos = [
        "crise",
        "pressão",
        "protesto",
        "boatos de demissão",
        "problema financeiro",
        "atraso de salário",
        "torcida contra",
        "lesão",
        "contundido",
        "fora da partida",
        "suspenso",
        "suspensão",
        "desfalque",
        "poupado",
        "time reserva",
    ]
    positivos = [
        "invicto",
        "sequência de vitórias",
        "sequência positiva",
        "liderança",
        "decisão",
        "mata-mata",
        "clássico",
        "rivalidade",
        "casa cheia",
        "lotado",
        "estádio cheio",
        "apoio da torcida",
        "reforço",
        "estreia",
        "ataque forte",
        "melhor ataque",
    ]

    score = 0.0

    for w in positivos:
        if w in text_blob:
            score += 1.0
    for w in negativos:
        if w in text_blob:
            score -= 1.0

    derby_terms = ["derbi", "derby", "clássico", "rivalidade", "superclássico"]
    for t in derby_terms:
        if t in text_blob:
            score += 1.0
            break

    if "final" in text_blob or "semifinal" in text_blob:
        score += 0.5

    if score > 4.0:
        score = 4.0
    if score < -4.0:
        score = -4.0

    boost = (score / 4.0) * 2.0

    if boost > 0.5:
        reason = "Noticiário quente (+%.1f pp): clima favorecendo jogo aberto" % boost
    elif boost < -0.5:
        reason = "Noticiário pesado (%.1f pp): contexto mais travado/instável" % boost
    else:
        reason = "Noticiário neutro (%.1f pp)" % boost

    return boost, reason


async def get_news_boost(home: str, away: str) -> Tuple[float, str]:
    """
    Camada de notícias do EvRadar PRO.
    Retorna (boost_em_pontos_percentuais, motivo_resumido).
    """
    if not NEWS_ENABLED or not NEWS_API_KEY:
        return 0.0, "Camada de notícias desativada"

    query = '"%s" OR "%s" futebol OR soccer' % (home, away)

    articles = await fetch_raw_news(query)
    boost, reason = score_news_articles(articles, home, away)

    logger.info(
        "[news] %s x %s → boost=%.2f pp | %s | artigos=%d",
        home,
        away,
        boost,
        reason,
        len(articles),
    )
    return boost, reason


# ============================================================
# Camada de dados ao vivo (API-Football)
# ============================================================

async def fetch_live_fixtures() -> List[Dict[str, Any]]:
    """
    Busca todos os jogos ao vivo no API-Football e filtra por ligas permitidas.
    """
    if not API_FOOTBALL_KEY:
        logger.error("API_FOOTBALL_KEY não configurada.")
        return []

    headers = {
        "x-apisports-key": API_FOOTBALL_KEY,
    }
    url = f"{API_FOOTBALL_BASE}/fixtures"
    params = {"live": "all"}

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("Erro ao buscar fixtures ao vivo: %s", e)
            return []

    response = data.get("response") or []
    fixtures: List[Dict[str, Any]] = []

    for item in response:
        try:
            league_id = int(item.get("league", {}).get("id") or 0)
            if LEAGUE_IDS and league_id not in LEAGUE_IDS:
                continue

            fixture = item.get("fixture", {})
            status = fixture.get("status", {}) or {}
            minute = int(status.get("elapsed") or 0)

            if minute < WINDOW_START or minute > WINDOW_END:
                continue

            teams = item.get("teams", {})
            goals = item.get("goals", {})

            home_name = (teams.get("home", {}) or {}).get("name") or "Home"
            away_name = (teams.get("away", {}) or {}).get("name") or "Away"

            goals_home = int(goals.get("home") if goals.get("home") is not None else 0)
            goals_away = int(goals.get("away") if goals.get("away") is not None else 0)

            fixtures.append(
                {
                    "fixture_id": int(fixture.get("id") or 0),
                    "league_id": league_id,
                    "minute": minute,
                    "home": home_name,
                    "away": away_name,
                    "goals_home": goals_home,
                    "goals_away": goals_away,
                }
            )
        except Exception as e:
            logger.warning("Erro ao processar fixture ao vivo: %s", e)

    logger.info("Fixtures ao vivo filtrados na janela: %d", len(fixtures))
    return fixtures


async def fetch_fixture_odds(fixture_id: int) -> Optional[float]:
    """
    Tenta buscar a odd atual do mercado Over (soma + 0,5) para o fixture.
    Implementação simplificada: se falhar ou não encontrar, retorna None.
    """
    if not USE_API_FOOTBALL_ODDS or not API_FOOTBALL_KEY:
        return None

    headers = {
        "x-apisports-key": API_FOOTBALL_KEY,
    }
    url = f"{API_FOOTBALL_BASE}/odds"
    params = {
        "fixture": fixture_id,
        "bookmaker": BOOKMAKER_ID,
    }

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("Erro ao buscar odds para fixture %s: %s", fixture_id, e)
            return None

    response = data.get("response") or []
    for item in response:
        bookmakers = item.get("bookmakers") or []
        for bm in bookmakers:
            try:
                bm_id = int(bm.get("id") or 0)
            except Exception:
                bm_id = 0
            if bm_id != BOOKMAKER_ID:
                continue
            bets = bm.get("bets") or []
            for bet in bets:
                name = (bet.get("name") or "").lower()
                if "over" not in name and "goals" not in name:
                    continue
                values = bet.get("values") or []
                for v in values:
                    val_name = (v.get("value") or "").lower()
                    if (
                        "0.5" in val_name
                        or "1.5" in val_name
                        or "2.5" in val_name
                        or "3.5" in val_name
                    ):
                        odd_str = v.get("odd")
                        try:
                            odd = float(odd_str)
                            return odd
                        except Exception:
                            continue
    return None


# ============================================================
# Modelo simplificado de probabilidade (v0.1 online)
# ============================================================

def estimate_goal_prob(minute: int, total_goals: int) -> float:
    """
    Estima a probabilidade de sair pelo menos 1 gol a mais
    até o fim do jogo com base no tempo e número de gols.
    Modelo simplificado, calibrado de forma heurística.
    """
    remaining = clamp(90 - minute, 0, 90)

    base = 0.25 + (remaining / 90.0) * 0.25  # 0.25–0.5 dependendo do tempo
    goals_factor = 0.05 * total_goals

    p = base + goals_factor
    return clamp(p, 0.10, 0.80)


# ============================================================
# Núcleo de varredura e geração de alertas
# ============================================================

async def analyze_fixture(
    app: Application,
    fix: Dict[str, Any],
) -> Optional[str]:
    """
    Analisa um fixture e, se houver valor, envia alerta.
    Retorna texto enviado ou None.
    """
    fixture_id = fix["fixture_id"]
    minute = fix["minute"]
    home = fix["home"]
    away = fix["away"]
    goals_home = fix["goals_home"]
    goals_away = fix["goals_away"]

    total_goals = goals_home + goals_away

    # Probabilidade base pelo modelo simplificado
    base_prob = estimate_goal_prob(minute, total_goals)

    # Camada de notícias
    news_boost_pp, news_reason = await get_news_boost(home, away)
    p_final = clamp(base_prob + news_boost_pp / 100.0, 0.01, 0.99)

    fair_odd = 1.0 / p_final

    # Odd atual: tenta buscar na API, senão cai no TARGET_ODD
    current_odd: Optional[float] = None
    if USE_API_FOOTBALL_ODDS:
        current_odd = await fetch_fixture_odds(fixture_id)

    if current_odd is None:
        current_odd = TARGET_ODD

    # filtros de faixa de odd
    if current_odd < MIN_ODD or current_odd > MAX_ODD:
        logger.debug(
            "Fixture %s filtrado por odd (%.2f fora de [%.2f, %.2f])",
            fixture_id,
            current_odd,
            MIN_ODD,
            MAX_ODD,
        )
        return None

    ev = p_final * current_odd - 1.0
    ev_pct = ev * 100.0

    if ev_pct < EV_MIN_PCT:
        logger.debug(
            "Fixture %s filtrado por EV (%.2f%% < %.2f%%)",
            fixture_id,
            ev_pct,
            EV_MIN_PCT,
        )
        return None

    # Montagem da mensagem para Telegram (layout EvRadar)
    header = "🏟️ %s vs %s" % (home, away)
    line1 = "⏱️ %d' | 🔢 %d–%d" % (minute, goals_home, goals_away)
    line2 = "⚙️ Linha: Over (soma + 0,5) @ %.2f" % current_odd

    prob_lines = [
        "📊 Probabilidade & valor:",
        "- P_final (gol a mais): %.1f%%" % (p_final * 100.0),
        "- Odd justa (modelo): %.2f" % fair_odd,
        "- EV: %.2f%%" % ev_pct,
    ]

    news_line = "🧩 News: %+0.1f pp — %s" % (news_boost_pp, news_reason)

    extra_lines = [
        "💰 Banca simulada: stake em %% da banca (Kelly fracionado depois)",
        "🔗 Abrir mercado (%s)" % BOOKMAKER_NAME,
        BOOKMAKER_URL,
    ]

    msg_lines: List[str] = []
    msg_lines.append(header)
    msg_lines.append(line1)
    msg_lines.append(line2)
    msg_lines.append("")
    msg_lines.extend(prob_lines)
    msg_lines.append("")
    msg_lines.append(news_line)
    msg_lines.append("")
    msg_lines.extend(extra_lines)

    text = "\n".join(msg_lines)

    try:
        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
        logger.info(
            "Alerta enviado: %s x %s (%d') odd=%.2f EV=%.2f%%",
            home,
            away,
            minute,
            current_odd,
            ev_pct,
        )
        return text
    except Exception as e:
        logger.error("Erro ao enviar mensagem Telegram: %s", e)
        return None


async def run_full_scan(app: Application, origin: str) -> None:
    """
    Varredura completa de jogos ao vivo com base no API-Football.
    origin = 'manual' ou 'auto' (usado só para log/mensagem).
    """
    logger.info("Iniciando varredura (%s)...", origin)

    fixtures = await fetch_live_fixtures()
    total_live = len(fixtures)
    window_count = total_live  # já filtrados pela janela
    alerts_sent = 0

    for fix in fixtures:
        try:
            sent = await analyze_fixture(app, fix)
            if sent:
                alerts_sent += 1
        except Exception as e:
            logger.exception(
                "Erro ao analisar fixture %s: %s",
                fix.get("fixture_id"),
                e,
            )

    summary = (
        "[EvRadar PRO] Scan concluído (origem=%s). "
        "Eventos ao vivo: %d | Jogos analisados na janela: %d | Alertas enviados: %d."
        % (origin, total_live, window_count, alerts_sent)
    )

    if origin == "manual":
        try:
            await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=summary)
        except Exception as e:
            logger.error("Erro ao enviar resumo manual: %s", e)
    else:
        # autoscan: sem spam; só loga e, opcional, avisa se teve alerta
        if alerts_sent > 0:
            mini = "[EvRadar PRO] Autoscan: %d alerta(s) enviado(s) nesse ciclo." % alerts_sent
            try:
                await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=mini)
            except Exception as e:
                logger.error("Erro ao enviar mini-resumo autoscan: %s", e)
        logger.info(summary)


# ============================================================
# Loop de autoscan (sem JobQueue)
# ============================================================

async def autoscan_loop(app: Application) -> None:
    logger.info("Autoscan iniciado (intervalo=%ds)", CHECK_INTERVAL)
    while True:
        try:
            await run_full_scan(app, origin="auto")
        except Exception as e:
            logger.exception("Erro no autoscan: %s", e)
        await asyncio.sleep(CHECK_INTERVAL)


# ============================================================
# Handlers de comandos Telegram
# ============================================================

LAST_STATUS: Dict[str, Any] = {
    "last_run": None,
    "last_origin": None,
    "last_alerts": 0,
}


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    logger.info("/start chamado por %s", user.id if user else "desconhecido")

    lines = [
        "👋 EvRadar PRO online.",
        "",
        "Janela padrão: %d–%dʼ" % (WINDOW_START, WINDOW_END),
        "Odd ref (TARGET_ODD): %.2f" % TARGET_ODD,
        "EV mínimo: %.2f%%" % EV_MIN_PCT,
        "Cooldown por jogo: (a definir)",
        "",
        "Comandos:",
        "  /scan   → rodar varredura agora",
        "  /status → ver último resumo",
        "  /debug  → info técnica",
        "  /links  → links úteis / bookmaker",
    ]
    text = "\n".join(lines)

    await update.message.reply_text(text)

    # inicia autoscan se habilitado e ainda não iniciado
    app = context.application
    if AUTOSTART and not app.bot_data.get("autoscan_started"):
        app.bot_data["autoscan_started"] = True
        asyncio.create_task(autoscan_loop(app))
        logger.info("Autoscan agendado após /start (intervalo=%ds)", CHECK_INTERVAL)


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔍 Iniciando varredura manual de jogos ao vivo (WR+)...")
    app = context.application
    await run_full_scan(app, origin="manual")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [
        "📈 Status básico (v0.1 online):",
        "- Janela: %d–%dʼ" % (WINDOW_START, WINDOW_END),
        "- EV mínimo: %.2f%%" % EV_MIN_PCT,
        "- Odds aceitas: %.2f–%.2f" % (MIN_ODD, MAX_ODD),
        "- Ligas filtradas: %s" % (", ".join(str(x) for x in LEAGUE_IDS) or "todas"),
        "- NewsAPI: %s" % ("ON" if NEWS_ENABLED and NEWS_API_KEY else "OFF"),
    ]
    text = "\n".join(lines)
    await update.message.reply_text(text)


async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [
        "🛠 Debug EvRadar PRO (online):",
        "Python: %s" % sys.version.split()[0],
        "AUTOSTART: %d" % AUTOSTART,
        "CHECK_INTERVAL: %ds" % CHECK_INTERVAL,
        "API_FOOTBALL_KEY configurada: %s" % ("SIM" if bool(API_FOOTBALL_KEY) else "NÃO"),
        "LEAGUE_IDS: %s" % (", ".join(str(x) for x in LEAGUE_IDS) or "N/D"),
        "USE_API_FOOTBALL_ODDS: %d" % USE_API_FOOTBALL_ODDS,
        "BOOKMAKER: %s (id=%d)" % (BOOKMAKER_NAME, BOOKMAKER_ID),
        "NEWS_ENABLED: %d" % NEWS_ENABLED,
        "NEWS_API_KEY configurada: %s" % ("SIM" if bool(NEWS_API_KEY) else "NÃO"),
    ]
    text = "\n".join(lines)
    await update.message.reply_text(text)


async def links_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [
        "🔗 Links úteis:",
        "- Bookmaker: %s" % BOOKMAKER_NAME,
        BOOKMAKER_URL,
        "",
        "Lembrete: jogo responsável. Isso aqui é radar de valor, não garantia de lucro.",
    ]
    text = "\n".join(lines)
    await update.message.reply_text(text)


# ============================================================
# Main
# ============================================================

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN não configurado.")
        sys.exit(1)

    start_health_server()

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("debug", debug_command))
    app.add_handler(CommandHandler("links", links_command))

    logger.info("Iniciando bot do EvRadar PRO (online)...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
