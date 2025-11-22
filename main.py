import os
import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional, Dict, Any, Tuple

import httpx
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

# =========================
# CONFIG E ENV
# =========================

@dataclass
class Config:
    api_football_key: str
    telegram_bot_token: str
    telegram_chat_id: Optional[int]
    window_start: int
    window_end: int
    min_odd: float
    max_odd: float
    ev_min: float
    cooldown_minutes: int
    check_interval: int
    autostart: bool
    target_odd: float
    use_api_football_odds: bool
    bookmaker_id: Optional[int]
    allowed_league_ids: List[int]
    bookmaker_name: str
    bookmaker_url: str
    sim_bankroll: float


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    try:
        return float(str(val).replace(",", "."))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    val = val.strip().lower()
    if val in ("1", "true", "t", "yes", "y", "on", "sim"):
        return True
    if val in ("0", "false", "f", "no", "n", "off", "nao", "não"):
        return False
    return default


def _env_int_list(name: str) -> List[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    result: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            continue
    return result


def load_config() -> Config:
    api_football_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id_env = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    telegram_chat_id = int(telegram_chat_id_env) if telegram_chat_id_env else None

    window_start = _env_int("WINDOW_START", 47)
    window_end = _env_int("WINDOW_END", 75)
    min_odd = _env_float("MIN_ODD", 1.47)
    max_odd = _env_float("MAX_ODD", 2.30)
    ev_min = _env_float("EV_MIN", 1.60)  # %
    cooldown_minutes = _env_int("COOLDOWN_MINUTES", 6)
    check_interval = _env_int("CHECK_INTERVAL", 1500)
    autostart = _env_bool("AUTOSTART", False)
    target_odd = _env_float("TARGET_ODD", 1.70)
    use_api_football_odds = _env_bool("USE_API_FOOTBALL_ODDS", False)
    bookmaker_id_env = os.getenv("BOOKMAKER_ID", "").strip()
    bookmaker_id = int(bookmaker_id_env) if bookmaker_id_env else None
    allowed_league_ids = _env_int_list("ALLOWED_LEAGUE_IDS")
    bookmaker_name = os.getenv("BOOKMAKER", "Superbet").strip() or "Superbet"
    bookmaker_url = os.getenv("BOOKMAKER_URL", "https://www.superbet.com").strip() or "https://www.superbet.com"
    sim_bankroll = _env_float("SIM_BANKROLL", 5000.0)

    return Config(
        api_football_key=api_football_key,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        window_start=window_start,
        window_end=window_end,
        min_odd=min_odd,
        max_odd=max_odd,
        ev_min=ev_min,
        cooldown_minutes=cooldown_minutes,
        check_interval=check_interval,
        autostart=autostart,
        target_odd=target_odd,
        use_api_football_odds=use_api_football_odds,
        bookmaker_id=bookmaker_id,
        allowed_league_ids=allowed_league_ids,
        bookmaker_name=bookmaker_name,
        bookmaker_url=bookmaker_url,
        sim_bankroll=sim_bankroll,
    )


# Países que queremos por padrão (nacionais grandes + World/Europe)
ALLOWED_COUNTRIES_BASE = {
    "Brazil",
    "England",
    "Spain",
    "Italy",
    "Germany",
    "France",
    "Portugal",
    "Netherlands",
    "Argentina",
    "World",   # Champions, Libertadores, etc.
    "Europe",
}

# Palavras para bloquear base / feminino / reservas / ligas fracas
BLOCKED_LEAGUE_KEYWORDS = [
    "u19",
    "u20",
    "u21",
    "u23",
    "primavera",
    "women",
    " w ",
    "femin",
    "reserves",
    "reserve",
    "oberliga",
    "tweede divisie",
    "liga classic",
    "promotion",
    "3. liga",
]


# =========================
# MODELOS
# =========================

@dataclass
class MatchStats:
    fixture_id: int
    league_id: int
    league_name: str
    league_country: str
    home_team: str
    away_team: str
    minute: int
    goals_home: int
    goals_away: int


@dataclass
class CandidateSignal:
    match: MatchStats
    prob_goal: float
    fair_odd: float
    ev: float
    used_odd: float
    tier: str
    stake_pct: float


@dataclass
class SimulatedBet:
    message_id: int
    match: MatchStats
    signal: CandidateSignal
    stake_value: float
    status: str  # pending/confirmed/skipped


# =========================
# MODELO EV / PROB
# =========================

class EvRadarModel:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def estimate_probability(self, m: MatchStats) -> float:
        # modelo simples: “sino” na janela + boost por gols
        total_goals = m.goals_home + m.goals_away

        minute = m.minute
        if minute < self.cfg.window_start:
            minute = self.cfg.window_start
        if minute > self.cfg.window_end:
            minute = self.cfg.window_end

        if self.cfg.window_end == self.cfg.window_start:
            t = 0.5
        else:
            t = (minute - self.cfg.window_start) / float(self.cfg.window_end - self.cfg.window_start)

        base_prob = 0.45 + 0.2 * (1.0 - (2.0 * (t - 0.5)) ** 2)
        goals_boost = 0.03 * total_goals

        prob = base_prob + goals_boost

        if prob < 0.40:
            prob = 0.40
        if prob > 0.78:
            prob = 0.78

        return prob

    def pick_tier_and_stake(self, ev: float, used_odd: float) -> Tuple[str, float]:
        tier = "C"
        stake_pct = 1.0

        if ev >= 0.07:
            tier = "A"
            stake_pct = 3.0
        elif ev >= 0.05:
            tier = "A"
            stake_pct = 2.5
        elif ev >= 0.03:
            tier = "B"
            stake_pct = 2.0
        elif ev >= 0.015:
            tier = "C"
            stake_pct = 1.5
        else:
            tier = "C"
            stake_pct = 1.0

        # throttle por odd
        if used_odd > 2.6:
            stake_pct *= 0.7
        elif used_odd > 1.8:
            stake_pct *= 0.9

        if stake_pct > 3.0:
            stake_pct = 3.0

        return tier, stake_pct

    def evaluate_match(self, m: MatchStats, current_odd: Optional[float]) -> Optional[CandidateSignal]:
        cfg = self.cfg
        used_odd = current_odd if current_odd is not None else cfg.target_odd

        if used_odd < cfg.min_odd or used_odd > cfg.max_odd:
            return None

        prob = self.estimate_probability(m)
        fair_odd = 1.0 / prob if prob > 0 else 99.0
        ev = prob * used_odd - 1.0

        if ev < cfg.ev_min / 100.0:
            return None

        tier, stake_pct = self.pick_tier_and_stake(ev, used_odd)

        return CandidateSignal(
            match=m,
            prob_goal=prob,
            fair_odd=fair_odd,
            ev=ev,
            used_odd=used_odd,
            tier=tier,
            stake_pct=stake_pct,
        )


# =========================
# API-FOOTBALL
# =========================

async def fetch_live_fixtures(cfg: Config, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    if not cfg.api_football_key:
        logging.warning("API_FOOTBALL_KEY não configurada.")
        return []

    headers = {"x-apisports-key": cfg.api_football_key}
    url = "https://v3.football.api-sports.io/fixtures?live=all"

    try:
        resp = await client.get(url, headers=headers, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", [])
    except Exception as exc:
        logging.error("Erro ao buscar fixtures ao vivo: %s", exc)
        return []


def parse_match(entry: Dict[str, Any]) -> Optional[MatchStats]:
    fixture = entry.get("fixture") or {}
    league = entry.get("league") or {}
    teams = entry.get("teams") or {}
    goals = entry.get("goals") or {}
    status = fixture.get("status") or {}

    minute = status.get("elapsed")
    if minute is None:
        return None

    try:
        minute_int = int(minute)
    except Exception:
        return None

    home_goals = goals.get("home") or 0
    away_goals = goals.get("away") or 0

    home_team = (teams.get("home") or {}).get("name") or "Home"
    away_team = (teams.get("away") or {}).get("name") or "Away"

    league_id = league.get("id") or 0
    league_name = league.get("name") or "League"
    league_country = league.get("country") or ""

    return MatchStats(
        fixture_id=int(fixture.get("id") or 0),
        league_id=int(league_id),
        league_name=str(league_name),
        league_country=str(league_country),
        home_team=str(home_team),
        away_team=str(away_team),
        minute=minute_int,
        goals_home=int(home_goals),
        goals_away=int(away_goals),
    )


def league_is_allowed(cfg: Config, m: MatchStats) -> bool:
    # Se tiver ALLOWED_LEAGUE_IDS configurado, respeita só eles
    if cfg.allowed_league_ids:
        return m.league_id in cfg.allowed_league_ids

    # Caso contrário, filtro por país e keywords
    if m.league_country not in ALLOWED_COUNTRIES_BASE:
        return False

    lname = m.league_name.lower()
    for kw in BLOCKED_LEAGUE_KEYWORDS:
        if kw in lname:
            return False

    return True


def minute_in_window(cfg: Config, minute: int) -> bool:
    return cfg.window_start <= minute <= cfg.window_end


# =========================
# BANCA FICTÍCIA
# =========================

def get_bot_state(app: Application) -> Dict[str, Any]:
    return app.bot_data


# =========================
# FORMATAÇÃO DAS MENSAGENS
# =========================

def format_signal_message(cfg: Config, sig: CandidateSignal) -> str:
    m = sig.match
    total_goals = m.goals_home + m.goals_away
    line_goals = total_goals + 0.5

    prob_pct = sig.prob_goal * 100.0
    ev_pct = sig.ev * 100.0
    stake_value = cfg.sim_bankroll * (sig.stake_pct / 100.0)

    header = (
        "🔔 Tier "
        + sig.tier
        + " — Sinal EvRadar PRO\n\n"
        + "🏟️ "
        + m.home_team
        + " vs "
        + m.away_team
        + " — "
        + m.league_name
        + "\n"
        + "⏱️ "
        + str(m.minute)
        + "' | 🔢 "
        + str(m.goals_home)
        + "–"
        + str(m.goals_away)
        + "\n"
        + "⚙️ Linha: Over "
        + f"{line_goals:.1f}"
        + " (soma + 0,5) @ "
        + f"{sig.used_odd:.2f}"
        + "\n\n"
    )

    body = (
        "📊 Probabilidade & valor:\n"
        + "- P_final (gol a mais): "
        + f"{prob_pct:.1f}%\n"
        + "- Odd justa (modelo): "
        + f"{sig.fair_odd:.2f}\n"
        + "- EV: "
        + f"{ev_pct:.2f}% → "
        + ("EV+" if sig.ev >= 0 else "EV-")
        + "\n\n"
    )

    stake_line = "💰 Stake sugerida: " + f"{sig.stake_pct:.1f}% da banca"
    if stake_value > 0:
        stake_line += " (~R$" + f"{stake_value:,.2f}" + ")"
    stake_line += "\n\n"

    interpretation = (
        "🧩 Interpretação:\n"
        "ritmo e contexto indicam boa chance de 1 gol a mais dentro da janela.\n\n"
    )

    link_line = (
        "🔗 <a href=\""
        + cfg.bookmaker_url
        + "\">Abrir evento ("
        + cfg.bookmaker_name
        + ")</a>"
    )

    return header + body + stake_line + interpretation + link_line


# =========================
# HANDLERS TELEGRAM
# =========================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    lines = [
        "👋 EvRadar PRO online.",
        "",
        "Janela padrão: " + str(cfg.window_start) + "–" + str(cfg.window_end) + "ʼ",
        "Odd ref (TARGET_ODD): " + f"{cfg.target_odd:.2f}",
        "EV mínimo: " + f"{cfg.ev_min:.2f}%",
        "Cooldown por jogo: " + str(cfg.cooldown_minutes) + " min",
        "",
        "Comandos:",
        "  /scan   → rodar varredura agora",
        "  /status → ver último resumo",
        "  /debug  → info técnica",
        "  /links  → links úteis / bookmaker",
        "  /id     → mostrar seu chat_id",
    ]
    if update.message:
        await update.message.reply_text("\n".join(lines))


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    text = (
        "🔗 Links úteis:\n"
        "- Bookmaker padrão: "
        + cfg.bookmaker_name
        + " ("
        + cfg.bookmaker_url
        + ")\n"
        "- API-FOOTBALL: https://www.api-football.com\n"
    )
    if update.message:
        await update.message.reply_text(text)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if update.message:
        await update.message.reply_text("📌 Seu chat_id: " + str(chat_id))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    state = get_bot_state(app)
    last_summary = state.get("last_summary")
    sim_bankroll = state.get("sim_bankroll")
    cfg: Config = state["cfg"]

    lines: List[str] = []
    if last_summary:
        lines.append(last_summary)
    else:
        lines.append("ℹ️ Ainda não houve nenhuma varredura registrada.")

    if sim_bankroll is None:
        sim_bankroll = cfg.sim_bankroll

    lines.append("")
    lines.append("💼 Banca fictícia de referência: R$" + f"{sim_bankroll:,.2f}")

    if update.message:
        await update.message.reply_text("\n".join(lines))


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    text = (
        "🛠 Debug EvRadar PRO\n\n"
        "Janela: " + str(cfg.window_start) + "–" + str(cfg.window_end) + "ʼ\n"
        "Odds aceitas: " + f"{cfg.min_odd:.2f}" + "–" + f"{cfg.max_odd:.2f}" + "\n"
        "EV mínimo: " + f"{cfg.ev_min:.2f}%\n"
        "Cooldown: " + str(cfg.cooldown_minutes) + " min\n"
        "CHECK_INTERVAL: " + str(cfg.check_interval) + " s\n"
        "AUTOSTART: " + str(cfg.autostart) + "\n"
        "TARGET_ODD: " + f"{cfg.target_odd:.2f}\n"
        "USE_API_FOOTBALL_ODDS: " + str(cfg.use_api_football_odds) + "\n"
        "BOOKMAKER_ID: " + str(cfg.bookmaker_id) + "\n"
        "ALLOWED_LEAGUE_IDS: " + str(cfg.allowed_league_ids) + "\n"
        "Bookmaker: " + cfg.bookmaker_name + " (" + cfg.bookmaker_url + ")\n"
        "SIM_BANKROLL: " + f"{cfg.sim_bankroll:,.2f}\n"
    )
    if update.message:
        await update.message.reply_text(text)


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    cfg: Config = app.bot_data["cfg"]
    model = EvRadarModel(cfg)

    chat_id = update.effective_chat.id if update.effective_chat else cfg.telegram_chat_id
    if not chat_id:
        if update.message:
            await update.message.reply_text("❌ TELEGRAM_CHAT_ID não configurado.")
        return

    if update.message:
        await update.message.reply_text("🔍 Iniciando varredura manual de jogos ao vivo...")

    async with httpx.AsyncClient() as client:
        fixtures = await fetch_live_fixtures(cfg, client)

    signals: List[CandidateSignal] = []

    for entry in fixtures:
        m = parse_match(entry)
        if not m:
            continue
        if not minute_in_window(cfg, m.minute):
            continue
        if not league_is_allowed(cfg, m):
            continue

        sig = model.evaluate_match(m, current_odd=None)
        if sig:
            signals.append(sig)

    state = get_bot_state(app)
    if "bets" not in state:
        state["bets"] = {}

    sent = 0
    for sig in signals:
        text = format_signal_message(cfg, sig)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Confirmar (" + f"{sig.stake_pct:.1f}" + "% da banca)",
                        callback_data="CONFIRM",
                    ),
                    InlineKeyboardButton("❌ Pular", callback_data="SKIP"),
                ]
            ]
        )

        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )

        stake_value = cfg.sim_bankroll * (sig.stake_pct / 100.0)
        bet = SimulatedBet(
            message_id=msg.message_id,
            match=sig.match,
            signal=sig,
            stake_value=stake_value,
            status="pending",
        )
        state["bets"][msg.message_id] = bet
        sent += 1

    summary = (
        "[EvRadar PRO] Scan concluído (origem=manual). "
        "Eventos ao vivo: "
        + str(len(fixtures))
        + " | Jogos analisados na janela: "
        + str(len(signals))
        + " | Alertas enviados: "
        + str(sent)
        + "."
    )
    state["last_summary"] = summary

    await context.bot.send_message(chat_id=chat_id, text=summary)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()

    data = query.data or ""
    msg = query.message
    if not msg:
        return

    app = context.application
    state = get_bot_state(app)
    bets: Dict[int, SimulatedBet] = state.get("bets", {})

    bet = bets.get(msg.message_id)
    if not bet:
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if data == "CONFIRM":
        bet.status = "confirmed"
        new_text = msg.text + "\n\n✅ Entrada confirmada na banca fictícia."
    elif data == "SKIP":
        bet.status = "skipped"
        new_text = msg.text + "\n\n⏭ Entrada marcada como pulada."
    else:
        return

    await query.edit_message_text(
        text=new_text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# =========================
# AUTOSCAN LOOP
# =========================

async def autoscan_loop(app: Application) -> None:
    cfg: Config = app.bot_data["cfg"]
    model = EvRadarModel(cfg)
    chat_id = cfg.telegram_chat_id

    if not chat_id:
        logging.warning("AUTOSTART=1 mas TELEGRAM_CHAT_ID não está configurado.")
        return

    async with httpx.AsyncClient() as client:
        while True:
            try:
                fixtures = await fetch_live_fixtures(cfg, client)
                signals: List[CandidateSignal] = []

                for entry in fixtures:
                    m = parse_match(entry)
                    if not m:
                        continue
                    if not minute_in_window(cfg, m.minute):
                        continue
                    if not league_is_allowed(cfg, m):
                        continue

                    sig = model.evaluate_match(m, current_odd=None)
                    if sig:
                        signals.append(sig)

                state = get_bot_state(app)
                if "bets" not in state:
                    state["bets"] = {}

                sent = 0
                for sig in signals:
                    text = format_signal_message(cfg, sig)
                    keyboard = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "✅ Confirmar (" + f"{sig.stake_pct:.1f}" + "% da banca)",
                                    callback_data="CONFIRM",
                                ),
                                InlineKeyboardButton("❌ Pular", callback_data="SKIP"),
                            ]
                        ]
                    )

                    msg = await app.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                        reply_markup=keyboard,
                    )

                    stake_value = cfg.sim_bankroll * (sig.stake_pct / 100.0)
                    bet = SimulatedBet(
                        message_id=msg.message_id,
                        match=sig.match,
                        signal=sig,
                        stake_value=stake_value,
                        status="pending",
                    )
                    state["bets"][msg.message_id] = bet
                    sent += 1

                summary = (
                    "[EvRadar PRO] Scan concluído (origem=autoscan). "
                    "Eventos ao vivo: "
                    + str(len(fixtures))
                    + " | Jogos analisados na janela: "
                    + str(len(signals))
                    + " | Alertas enviados: "
                    + str(sent)
                    + "."
                )
                state["last_summary"] = summary

                await app.bot.send_message(chat_id=chat_id, text=summary)

            except Exception as exc:
                logging.error("Erro no autoscan_loop: %s", exc)

            await asyncio.sleep(cfg.check_interval)


# =========================
# POST_INIT (WEBHOOK + AUTOSTART)
# =========================

async def post_init(application: Application) -> None:
    cfg: Config = application.bot_data["cfg"]

    # garante que está em long polling
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        logging.exception("Erro ao remover webhook.")

    if cfg.telegram_chat_id:
        try:
            await application.bot.send_message(
                chat_id=cfg.telegram_chat_id,
                text="✅ EvRadar PRO conectado. Use /start para ver as configs e /scan para varrer os jogos.",
            )
        except Exception:
            logging.exception("Erro ao enviar mensagem inicial.")

    if cfg.autostart:
        logging.info("AUTOSTART=1 → iniciando autoscan em background.")
        asyncio.create_task(autoscan_loop(application))
    else:
        logging.info("AUTOSTART=0 → varredura apenas via /scan.")


# =========================
# MAIN
# =========================

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    cfg = load_config()

    logging.info(
        "Config carregada:\n"
        "Config EvRadar PRO:\n"
        "- Janela: %d–%dʼ\n"
        "- Odds aceitas: %.2f–%.2f\n"
        "- EV mínimo: %.2f%%\n"
        "- Cooldown por jogo: %d min\n"
        "- Intervalo autoscan: %d s\n"
        "- AUTOSTART: %s\n"
        "- TARGET_ODD (fallback): %.2f\n"
        "- USE_API_FOOTBALL_ODDS: %s\n"
        "- BOOKMAKER_ID: %s\n"
        "- ALLOWED_LEAGUE_IDS: %s\n"
        "- Bookmaker: %s (%s)",
        cfg.window_start,
        cfg.window_end,
        cfg.min_odd,
        cfg.max_odd,
        cfg.ev_min,
        cfg.cooldown_minutes,
        cfg.check_interval,
        cfg.autostart,
        cfg.target_odd,
        cfg.use_api_football_odds,
        str(cfg.bookmaker_id),
        cfg.allowed_league_ids,
        cfg.bookmaker_name,
        cfg.bookmaker_url,
    )

    if not cfg.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN não configurado.")

    application = (
        Application.builder()
        .token(cfg.telegram_bot_token)
        .post_init(post_init)
        .build()
    )

    application.bot_data["cfg"] = cfg
    application.bot_data["last_summary"] = None
    application.bot_data["bets"] = {}
    application.bot_data["sim_bankroll"] = cfg.sim_bankroll

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("debug", cmd_debug))
    application.add_handler(CommandHandler("links", cmd_links))
    application.add_handler(CommandHandler("id", cmd_id))
    application.add_handler(CallbackQueryHandler(on_button))

    logging.info("Iniciando bot do EvRadar PRO...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
