import os
import asyncio
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional, Set

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ----------------- Config & Globals ----------------- #

logger = logging.getLogger("EvRadarPRO")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

API_FOOTBALL_BASE = "https://v3.football.api-sports.io"

LAST_SCAN_SUMMARY: str = "Nenhum scan executado ainda."
LAST_ERROR: Optional[str] = None


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


TELEGRAM_BOT_TOKEN = env_str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env_str("TELEGRAM_CHAT_ID")

API_FOOTBALL_KEY = env_str("API_FOOTBALL_KEY")

AUTOSTART = env_int("AUTOSTART", 1)
CHECK_INTERVAL = env_int("CHECK_INTERVAL", 60)

WINDOW_START = env_int("WINDOW_START", 47)
WINDOW_END = env_int("WINDOW_END", 75)

MIN_ODD = env_float("MIN_ODD", 1.47)
MAX_ODD = env_float("MAX_ODD", 2.30)

EV_MIN_PCT = env_float("EV_MIN_PCT", 0.0)

BOOKMAKER_ID = env_int("BOOKMAKER_ID", 34)
BOOKMAKER_NAME = os.getenv("BOOKMAKER_NAME", "Superbet")
BOOKMAKER_URL = os.getenv("BOOKMAKER_URL", "https://www.superbet.com/")

LEAGUE_IDS_RAW = os.getenv("LEAGUE_IDS", "")
ALLOWED_LEAGUES: Set[int] = parse_league_ids(LEAGUE_IDS_RAW) if LEAGUE_IDS_RAW else set()

USE_API_FOOTBALL_ODDS = env_int("USE_API_FOOTBALL_ODDS", 1)


# ----------------- API-Football helpers ----------------- #

async def fetch_api_football(
    client: httpx.AsyncClient, endpoint: str, params: Optional[Dict[str, Any]] = None
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
    client: httpx.AsyncClient, fixture_id: int
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


def extract_over_line_odd(
    odds_payload: Dict[str, Any], total_goals: int
) -> Optional[float]:
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


def format_signal_message(ev: Dict[str, Any]) -> str:
    league = ev.get("league_name", "?")
    home = ev.get("home", "?")
    away = ev.get("away", "?")
    minute = ev.get("minute", 0)
    gh = ev.get("goals_home", 0)
    ga = ev.get("goals_away", 0)
    total_goals = gh + ga
    odd = ev.get("odd")
    line = f"Over (soma + 0,5) ⇒ Over {total_goals + 0.5}"

    header = f"🏟️ {home} vs {away} — {league}"
    line1 = f"⏱️ {minute}' | 🔢 {gh}–{ga}"
    if odd is not None:
        line2 = f"⚙️ Linha: {line} @ {odd:.2f}"
    else:
        line2 = f"⚙️ Linha: {line} (sem odd disponível)"

    line3 = (
        f"📊 EV: manual (avaliar estatísticas ao vivo) | "
        f"Faixa de odds alvo: {MIN_ODD:.2f}–{MAX_ODD:.2f}"
    )
    line4 = "💰 Ação: revisar posse, chutes, pressão e contexto. Se estiver forte, considerar entrada."
    line5 = f"🔗 Abrir mercado ({BOOKMAKER_NAME}): {BOOKMAKER_URL}"

    return "\n".join([header, line1, line2, line3, line4, line5])


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
            # tenta avisar no Telegram também
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
                league = fx.get("league", {})
                league_id = int(league.get("id"))
                league_name = league.get("name", "?")

                if ALLOWED_LEAGUES and league_id not in ALLOWED_LEAGUES:
                    continue

                fixture = fx.get("fixture", {})
                status = fixture.get("status", {})
                minute = status.get("elapsed")
                if minute is None:
                    continue

                if minute < WINDOW_START or minute > WINDOW_END:
                    continue

                goals = fx.get("goals", {})
                gh = goals.get("home") or 0
                ga = goals.get("away") or 0
                total_goals = gh + ga

                teams = fx.get("teams", {})
                home = teams.get("home", {}).get("name", "?")
                away = teams.get("away", {}).get("name", "?")

                fixture_id = int(fixture.get("id"))

                odd_val: Optional[float] = None
                odds_payload: Optional[Dict[str, Any]] = None

                if USE_API_FOOTBALL_ODDS:
                    try:
                        odds_payload = await fetch_odds_for_fixture(client, fixture_id)
                    except Exception as e:
                        logger.warning("Falha ao buscar odds para fixture %s: %s", fixture_id, e)

                if odds_payload:
                    odd_val = extract_over_line_odd(odds_payload, total_goals)

                if odd_val is not None:
                    if odd_val < MIN_ODD or odd_val > MAX_ODD:
                        continue

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
                    }
                )
            except Exception as inner:
                logger.warning("Erro ao processar fixture: %s", inner)

        candidates.sort(key=lambda x: x["minute"], reverse=True)

        # Monta resumo SEMPRE
        summary_lines: List[str] = []
        summary_lines.append(
            f"[EvRadar PRO] Scan concluído. Eventos ao vivo: {total_live} | Candidatos: {len(candidates)}."
        )
        if ALLOWED_LEAGUES:
            summary_lines.append(f"Ligas filtradas: {len(ALLOWED_LEAGUES)} (LEAGUE_IDS)")
        summary_lines.append(
            f"Janela: {WINDOW_START}–{WINDOW_END}ʼ | Odds alvo: {MIN_ODD:.2f}–{MAX_ODD:.2f}"
        )

        if candidates:
            preview = []
            for ev in candidates[:5]:
                preview.append(
                    f"- {ev['league_name']}: {ev['home']} x {ev['away']} "
                    f"({ev['minute']}' | {ev['goals_home']}–{ev['goals_away']} | odd={ev['odd']})"
                )
            summary_lines.append("")
            summary_lines.append("Amostra de candidatos:")
            summary_lines.extend(preview)
        else:
            summary_lines.append("")
            summary_lines.append("Nenhum jogo encaixou nos filtros neste scan.")

        LAST_SCAN_SUMMARY = "\n".join(summary_lines)
        LAST_ERROR = None
        logger.info(LAST_SCAN_SUMMARY)

        # Sempre manda resumo pro chat_id configurado no autoscan/manual
        if application is not None:
            try:
                await application.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=LAST_SCAN_SUMMARY,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.warning("Falha ao enviar resumo no Telegram: %s", e)

            # Se tiver candidatos, manda alerta detalhado também
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


# ----------------- Telegram Handlers ----------------- #

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid = update.effective_chat.id
    text_lines = [
        "👋 EvRadar PRO online.",
        "",
        f"Seu chat_id: {cid}",
        "",
        f"Janela padrão: {WINDOW_START}–{WINDOW_END}ʼ",
        f"Odds alvo (Superbet): {MIN_ODD:.2f}–{MAX_ODD:.2f}",
        f"EV mínimo (info): {EV_MIN_PCT:.2f}%",
        "",
        "Comandos:",
        "  /scan   → rodar varredura agora",
        "  /status → ver último resumo",
        "  /debug  → info técnica",
        "  /links  → links úteis / bookmaker",
    ]
    await update.message.reply_text("\n".join(text_lines))


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔍 Iniciando varredura manual de jogos ao vivo (API-Football)...")
    application = context.application
    summary = await scan_once(application)
    await update.message.reply_text(summary)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(LAST_SCAN_SUMMARY)


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "🛠 Debug EvRadar PRO",
        f"UTC agora: {now}",
        f"AUTOSTART: {AUTOSTART}",
        f"CHECK_INTERVAL: {CHECK_INTERVAL}s",
        f"WINDOW_START/END: {WINDOW_START}–{WINDOW_END}",
        f"MIN_ODD/MAX_ODD: {MIN_ODD:.2f}/{MAX_ODD:.2f}",
        f"BOOKMAKER_ID: {BOOKMAKER_ID} ({BOOKMAKER_NAME})",
        f"LEAGUE_IDS: {LEAGUE_IDS_RAW or '(não definido, todas as ligas)'}",
    ]
    if LAST_ERROR:
        lines.append("")
        lines.append(f"Último erro: {LAST_ERROR}")
    await update.message.reply_text("\n".join(lines))


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [
        "🔗 Links úteis",
        f"- Bookmaker: {BOOKMAKER_NAME} → {BOOKMAKER_URL}",
        "- API-Football: https://dashboard.api-football.com/",
    ]
    await update.message.reply_text("\n".join(lines))


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
    logger.info("Iniciando bot do EvRadar PRO (Local/Railway)...")
    application = build_application()
    application.run_polling()


if __name__ == "__main__":
    main()
