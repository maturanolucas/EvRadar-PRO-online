#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EvRadar PRO - Base Telegram v0.1
--------------------------------
Este arquivo é um "main.py" baseado apenas em:
- Bot Telegram estável (python-telegram-bot v21+)
- Comandos: /start, /scan, /status, /debug, /links
- Loop de autoscan opcional (apenas loga; não envia sinais reais)
NÃO contém ainda o cérebro estatístico v0.2 (modelo parrudo).
Use este arquivo como âncora de TELEGRAM funcionando.
"""

import asyncio
import logging
import os
from typing import Optional

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------------------------------------------------------------------------
# Configuração básica via variáveis de ambiente
# ---------------------------------------------------------------------------

def _get_env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        try:
            return int(float(raw.replace(",", ".")))
        except ValueError:
            return default


def _get_env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return default


TELEGRAM_BOT_TOKEN: str = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID: Optional[int] = None
_chat_raw = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
if _chat_raw:
    try:
        TELEGRAM_CHAT_ID = int(_chat_raw)
    except ValueError:
        TELEGRAM_CHAT_ID = None

AUTOSTART: int = _get_env_int("AUTOSTART", 0)
CHECK_INTERVAL: int = _get_env_int("CHECK_INTERVAL", 60)

WINDOW_START: int = _get_env_int("WINDOW_START", 47)
WINDOW_END: int = _get_env_int("WINDOW_END", 75)

EV_MIN_PCT: float = _get_env_float("EV_MIN_PCT", 4.0)
MIN_ODD: float = _get_env_float("MIN_ODD", 1.47)
MAX_ODD: float = _get_env_float("MAX_ODD", 3.50)

BOOKMAKER_NAME: str = (os.getenv("BOOKMAKER_NAME") or "Superbet").strip()
BOOKMAKER_URL: str = (os.getenv("BOOKMAKER_URL") or "https://www.superbet.com/").strip()

# ---------------------------------------------------------------------------
# Estado simples em memória
# ---------------------------------------------------------------------------

last_status_text: str = "Ainda não foi rodada nenhuma varredura."
last_scan_origin: str = "-"
last_scan_alerts: int = 0
last_scan_live_events: int = 0
last_scan_window_matches: int = 0


# ---------------------------------------------------------------------------
# Funções de scan (PLACEHOLDER - sem modelo parrudo ainda)
# ---------------------------------------------------------------------------

async def run_scan_cycle(origin: str, application: Application) -> None:
    """Executa UM ciclo de varredura (placeholder sem integração real)."""
    global last_status_text, last_scan_origin, last_scan_alerts
    global last_scan_live_events, last_scan_window_matches

    # Aqui depois vamos plugar o cérebro v0.2 (API, estatística, etc.).
    # Por enquanto, é apenas um mock que retorna 0 jogos / 0 alertas.
    last_scan_origin = origin
    last_scan_live_events = 0
    last_scan_window_matches = 0
    last_scan_alerts = 0

    # Mensagem de resumo padrão
    last_status_text = (
        "[EvRadar PRO] Scan concluído (origem={origin}). "
        "Eventos ao vivo: {live} | Jogos analisados na janela: {wnd} | Alertas enviados: {alerts}"
    ).format(
        origin=origin,
        live=last_scan_live_events,
        wnd=last_scan_window_matches,
        alerts=last_scan_alerts,
    )

    logging.info(last_status_text)


async def autoscan_loop(application: Application) -> None:
    """Loop de autoscan em background (usa create_task; não bloqueia polling)."""
    logging.info("Autoscan iniciado (intervalo=%ss)", CHECK_INTERVAL)
    while True:
        try:
            await run_scan_cycle(origin="auto", application=application)
        except Exception:
            logging.exception("Erro no autoscan")
        await asyncio.sleep(CHECK_INTERVAL)


# ---------------------------------------------------------------------------
# Handlers de comando
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /start: mensagem de boas-vindas e resumo de config."""
    autoscan_status = "ativado" if AUTOSTART else "desativado"

    lines = [
        "👋 EvRadar PRO online (modo base Telegram).",
        "",
        "Janela padrão: {ws}–{we}ʼ".format(ws=WINDOW_START, we=WINDOW_END),
        "EV mínimo (placebo): {ev:.2f}%".format(ev=EV_MIN_PCT),
        "Faixa de odds (placebo): {mn:.2f}–{mx:.2f}".format(mn=MIN_ODD, mx=MAX_ODD),
        "Autoscan: {auto} (intervalo {sec}s)".format(auto=autoscan_status, sec=CHECK_INTERVAL),
        "",
        "Comandos:",
        "  /scan   → rodar varredura de teste agora",
        "  /status → ver último resumo",
        "  /debug  → info técnica",
        "  /links  → links úteis / bookmaker",
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /scan: roda um ciclo de varredura manual (placeholder)."""
    await update.message.reply_text(
        "🔍 Iniciando varredura manual de jogos ao vivo (modo base / placeholder)..."
    )

    await run_scan_cycle(origin="manual", application=context.application)

    await update.message.reply_text(last_status_text)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /status: mostra o último resumo de varredura."""
    await update.message.reply_text(last_status_text)


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /debug: mostra informações técnicas básicas."""
    token_set = bool(TELEGRAM_BOT_TOKEN)
    chat_set = TELEGRAM_CHAT_ID is not None

    lines = [
        "🛠 Debug EvRadar PRO (modo base)",
        "",
        "TELEGRAM_BOT_TOKEN definido: {v}".format(v="sim" if token_set else "não"),
        "TELEGRAM_CHAT_ID: {cid}".format(cid=TELEGRAM_CHAT_ID if chat_set else "não definido"),
        "AUTOSTART: {a}".format(a=AUTOSTART),
        "CHECK_INTERVAL: {sec}s".format(sec=CHECK_INTERVAL),
        "Janela: {ws}–{we}ʼ".format(ws=WINDOW_START, we=WINDOW_END),
        "EV_MIN_PCT (placebo): {ev:.2f}%".format(ev=EV_MIN_PCT),
        "Faixa de odds (placebo): {mn:.2f}–{mx:.2f}".format(mn=MIN_ODD, mx=MAX_ODD),
        "",
        "Último scan:",
        "  origem: {origin}".format(origin=last_scan_origin),
        "  eventos ao vivo: {live}".format(live=last_scan_live_events),
        "  jogos na janela: {wnd}".format(wnd=last_scan_window_matches),
        "  alertas: {alerts}".format(alerts=last_scan_alerts),
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /links: exibe link da casa / recursos úteis."""
    lines = [
        "🔗 Links úteis",
        "",
        "Casa principal: {name}".format(name=BOOKMAKER_NAME),
        "Site: {url}".format(url=BOOKMAKER_URL),
    ]
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# post_init e função principal
# ---------------------------------------------------------------------------

async def post_init(application: Application) -> None:
    """Executado automaticamente por run_polling após initialize()."""
    logging.info("Application started (post_init executado).")

    if AUTOSTART:
        # Inicia o loop de autoscan sem bloquear o polling.
        application.create_task(autoscan_loop(application), name="autoscan_loop")


def main() -> None:
    """Ponto de entrada do bot EvRadar PRO (modo base Telegram)."""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN não definido. Configure a variável de ambiente antes de rodar."
        )

    # Configuração de logging
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )

    logging.info("Iniciando bot do EvRadar PRO (modo base Telegram)...")

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Handlers de comando
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("debug", cmd_debug))
    application.add_handler(CommandHandler("links", cmd_links))

    # Inicia polling (loop principal)
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        stop_signals=None,  # deixa o host (Railway/Replit) matar o processo
    )


if __name__ == "__main__":
    main()
