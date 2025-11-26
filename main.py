import os
import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, Any, Optional, List

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover
    load_dotenv = None

# -----------------------------------------------------
# Logging
# -----------------------------------------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("EvRadarPRO")

# -----------------------------------------------------
# Helpers para ENV
# -----------------------------------------------------
def env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    val = val.strip().lower()
    return val in {"1", "true", "t", "yes", "y", "on"}

def env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if not val:
        return default
    try:
        return float(val.replace(",", "."))
    except ValueError:
        logger.warning("Valor inválido para %s=%r, usando default=%s", name, val, default)
        return default

def env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        logger.warning("Valor inválido para %s=%r, usando default=%s", name, val, default)
        return default

# -----------------------------------------------------
# Carrega .env local (se existir)
# -----------------------------------------------------
if load_dotenv is not None:
    load_dotenv()

# -----------------------------------------------------
# Config via ENV
# -----------------------------------------------------
API_BASE = "https://v3.football.api-sports.io"
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()

AUTOSTART = env_bool("AUTOSTART", False)
CHECK_INTERVAL = env_int("CHECK_INTERVAL", 300)

WINDOW_START = env_int("WINDOW_START", 47)
WINDOW_END = env_int("WINDOW_END", 75)

MIN_ODD = env_float("MIN_ODD", 1.47)
MAX_ODD = env_float("MAX_ODD", 2.30)
TARGET_ODD = env_float("TARGET_ODD", 1.70)

EV_MIN_PCT = env_float("EV_MIN_PCT", 1.60)

BOOKMAKER_ID = env_int("BOOKMAKER_ID", 34)
BOOKMAKER_NAME = os.getenv("BOOKMAKER_NAME", "Superbet")
BOOKMAKER_URL = os.getenv("BOOKMAKER_URL", "https://www.superbet.com/")

USE_API_FOOTBALL_ODDS = env_bool("USE_API_FOOTBALL_ODDS", True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = env_int("TELEGRAM_CHAT_ID", 0)

# Banca virtual
USE_VIRTUAL_BANK = env_bool("USE_VIRTUAL_BANK", False)
BANKROLL_INITIAL = env_float("BANKROLL_INITIAL", 5000.0)
MAX_STAKE_PCT = 3.0  # hard cap de stake em %

# "Noticiário" / contexto global – bias simples
NEWS_BOOST_DEFAULT = env_float("NEWS_BOOST_DEFAULT", 0.0)  # em pontos percentuais

# Ligas
_raw_leagues = os.getenv("LEAGUE_IDS", "")
LEAGUE_IDS: List[int] = []
for part in _raw_leagues.split(","):
    part = part.strip()
    if not part:
        continue
    try:
        LEAGUE_IDS.append(int(part))
    except ValueError:
        logger.warning("ID de liga inválido em LEAGUE_IDS: %r", part)

if not LEAGUE_IDS:
    logger.warning("Nenhuma liga configurada em LEAGUE_IDS – o radar pode não encontrar jogos.")

# -----------------------------------------------------
# Estado global simples (apenas em memória)
# -----------------------------------------------------
@dataclass
class VirtualBankState:
    enabled: bool = USE_VIRTUAL_BANK
    initial_balance: float = BANKROLL_INITIAL
    balance: float = BANKROLL_INITIAL

@dataclass
class SignalInfo:
    fixture_id: int
    home: str
    away: str
    minute: int
    goals_home: int
    goals_away: int
    odd_used: float
    ev_pct: float
    prob_pct: float
    tier_label: str
    tier_name: str

VIRTUAL_BANK = VirtualBankState()

chat_id_bound: Optional[int] = TELEGRAM_CHAT_ID
LAST_SCAN_SUMMARY: str = "Nenhum scan executado ainda."
LAST_SCAN_ORIGIN: str = "N/A"

# odds por jogo
LAST_ODD_BY_FIXTURE: Dict[int, float] = {}
# sinais abertos (para mapear callback de /entrei)
OPEN_SIGNALS: Dict[int, SignalInfo] = {}
# aguardando odd digitada pelo usuário -> chat_id -> fixture_id
PENDING_ODD_INPUT: Dict[int, int] = {}
# histórico básico de entradas registradas
ENTRIES: List[Dict[str, Any]] = []

# client HTTP será criado no post_init
HTTP_CLIENT: Optional[httpx.AsyncClient] = None

# -----------------------------------------------------
# Probabilidade / EV / Stake
# -----------------------------------------------------
def estimate_goal_probability(minute: int, goals_sum: int) -> float:
    """
    Estimativa simples de probabilidade de 1 gol a mais até o fim,
    usando só tempo de jogo + placar + um pequeno bias de "contexto/notícias".
    """
    # base por tempo (janela 47–75+)
    if minute < 55:
        base = 0.65
    elif minute < 60:
        base = 0.62
    elif minute < 65:
        base = 0.60
    elif minute < 70:
        base = 0.58
    elif minute < 75:
        base = 0.55
    elif minute < 80:
        base = 0.52
    else:
        base = 0.48

    # ajuste por número de gols
    if goals_sum == 0:
        base -= 0.05
    elif goals_sum == 1:
        base += 0.02
    elif goals_sum == 2:
        base += 0.01
    else:
        base -= 0.02

    # "noticiário" global (em pontos percentuais)
    base += NEWS_BOOST_DEFAULT / 100.0

    p = max(0.01, min(0.99, base))
    return p

def compute_ev_pct(prob: float, odd: float) -> float:
    return (prob * odd - 1.0) * 100.0

def classify_tier(ev_pct: float) -> (str, str):
    if ev_pct >= 7.0:
        return "Tier A", "Sinal forte"
    elif ev_pct >= 5.0:
        return "Tier B", "Sinal bom"
    elif ev_pct >= 3.0:
        return "Tier C", "Sinal ok"
    elif ev_pct >= 1.5:
        return "Tier D", "Sinal marginal"
    else:
        return "Tier X", "Sinal fraco"

def suggest_stake_pct(ev_pct: float, odd: float) -> float:
    # tier pela EV
    if ev_pct >= 7.0:
        base = 3.0
    elif ev_pct >= 5.0:
        base = 2.5
    elif ev_pct >= 3.0:
        base = 2.0
    elif ev_pct >= 1.5:
        base = 1.25
    else:
        base = 0.75

    # throttle por odd
    if odd <= 1.80:
        mult_odds = 1.0
    elif odd <= 2.60:
        mult_odds = 0.9
    else:
        mult_odds = 0.7

    stake = base * mult_odds
    if stake > MAX_STAKE_PCT:
        stake = MAX_STAKE_PCT
    return max(0.25, stake)

# -----------------------------------------------------
# API-Football helpers
# -----------------------------------------------------
def get_headers() -> Dict[str, str]:
    return {
        "x-apisports-key": API_FOOTBALL_KEY,
    }

async def fetch_live_fixtures() -> List[Dict[str, Any]]:
    if not API_FOOTBALL_KEY:
        logger.warning("API_FOOTBALL_KEY não configurada.")
        return []
    if HTTP_CLIENT is None:
        raise RuntimeError("HTTP_CLIENT não inicializado ainda.")
    params = {"live": "all"}
    try:
        resp = await HTTP_CLIENT.get(f"{API_BASE}/fixtures", params=params, headers=get_headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", []) or []
    except Exception as exc:
        logger.exception("Erro ao buscar fixtures ao vivo: %s", exc)
        return []

def extract_over_under_odd(data: Dict[str, Any], target_line: float) -> Optional[float]:
    """
    Procura mercado de Over/Under gols com linha exata = target_line (ex.: 2.5)
    e retorna a odd do Over.
    """
    response = data.get("response") or []
    for item in response:
        for bookmaker in item.get("bookmakers", []):
            try:
                bid = int(bookmaker.get("id"))
            except Exception:
                bid = None
            if bid is not None and BOOKMAKER_ID and bid != BOOKMAKER_ID:
                continue
            for bet in bookmaker.get("bets", []):
                name = str(bet.get("name", "")).lower()
                if "over" not in name and "under" not in name:
                    continue
                for val in bet.get("values", []):
                    value_str = str(val.get("value", ""))
                    parts = value_str.split()
                    if len(parts) < 2:
                        continue
                    label = parts[0].lower()
                    try:
                        line = float(parts[1])
                    except Exception:
                        continue
                    if label.startswith("over") and abs(line - target_line) < 1e-6:
                        try:
                            return float(str(val.get("odd")))
                        except Exception:
                            continue
    return None

async def fetch_live_over_odd(fixture_id: int, goals_home: int, goals_away: int) -> Optional[float]:
    if not USE_API_FOOTBALL_ODDS:
        return None
    if not API_FOOTBALL_KEY:
        return None
    if HTTP_CLIENT is None:
        raise RuntimeError("HTTP_CLIENT não inicializado ainda.")
    params = {"fixture": fixture_id, "bookmaker": BOOKMAKER_ID}
    try:
        resp = await HTTP_CLIENT.get(f"{API_BASE}/odds/live", params=params, headers=get_headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Erro ao buscar odds ao vivo para fixture=%s: %s", fixture_id, exc)
        return None

    target_line = goals_home + goals_away + 0.5
    odd = extract_over_under_odd(data, target_line)
    if odd is None:
        logger.info("Não encontrei linha Over %.1f para fixture=%s, usando fallback", target_line, fixture_id)
    return odd

# -----------------------------------------------------
# Scan de jogos & geração de sinais
# -----------------------------------------------------
def bind_chat_id(chat_id: int) -> None:
    global chat_id_bound
    if chat_id_bound != chat_id:
        logger.info("Atualizando chat_id_bound: %s -> %s", chat_id_bound, chat_id)
        chat_id_bound = chat_id

def get_bound_chat_id() -> int:
    if chat_id_bound:
        return chat_id_bound
    return TELEGRAM_CHAT_ID

async def run_scan(application: Application, origin: str) -> None:
    global LAST_SCAN_SUMMARY, LAST_SCAN_ORIGIN, OPEN_SIGNALS

    fixtures = await fetch_live_fixtures()
    total_live = len(fixtures)
    candidates: List[SignalInfo] = []

    for item in fixtures:
        league = item.get("league") or {}
        league_id = league.get("id")
        if LEAGUE_IDS and league_id not in LEAGUE_IDS:
            continue

        fixture = item.get("fixture") or {}
        status = fixture.get("status") or {}
        elapsed = status.get("elapsed")
        if elapsed is None:
            continue
        try:
            minute = int(elapsed)
        except Exception:
            continue

        status_short = str(status.get("short", "")).upper()
        if status_short not in {"1H", "2H"}:
            continue

        if minute < WINDOW_START or minute > WINDOW_END:
            continue

        teams = item.get("teams") or {}
        home_team = (teams.get("home") or {}).get("name", "Home")
        away_team = (teams.get("away") or {}).get("name", "Away")

        goals = item.get("goals") or {}
        goals_home = goals.get("home") or 0
        goals_away = goals.get("away") or 0
        goals_sum = (goals_home or 0) + (goals_away or 0)

        fixture_id = fixture.get("id")
        if fixture_id is None:
            continue
        try:
            fixture_id_int = int(fixture_id)
        except Exception:
            continue

        # pegar odd ao vivo, ou última conhecida, ou fallback
        odd_source = "fallback"
        odd = await fetch_live_over_odd(fixture_id_int, goals_home, goals_away)
        if odd is not None:
            odd_source = "ao vivo"
            LAST_ODD_BY_FIXTURE[fixture_id_int] = odd
        else:
            if fixture_id_int in LAST_ODD_BY_FIXTURE:
                odd = LAST_ODD_BY_FIXTURE[fixture_id_int]
                odd_source = "última"
            else:
                odd = TARGET_ODD
                odd_source = "fallback"

        if odd < MIN_ODD or odd > MAX_ODD:
            continue

        prob = estimate_goal_probability(minute, goals_sum)
        ev_pct = compute_ev_pct(prob, odd)
        if ev_pct < EV_MIN_PCT:
            continue

        tier_label, tier_name = classify_tier(ev_pct)

        sig = SignalInfo(
            fixture_id=fixture_id_int,
            home=home_team,
            away=away_team,
            minute=minute,
            goals_home=goals_home or 0,
            goals_away=goals_away or 0,
            odd_used=odd,
            ev_pct=ev_pct,
            prob_pct=prob * 100.0,
            tier_label=tier_label,
            tier_name=tier_name,
        )
        candidates.append(sig)

        await send_signal_message(application, sig, odd_source)

    OPEN_SIGNALS = {s.fixture_id: s for s in candidates}

    chat_id = get_bound_chat_id()
    if chat_id:
        if candidates:
            resumo = (
                f"[EvRadar PRO] Scan concluído (origem={origin}). "
                f"Eventos ao vivo: {total_live} | Jogos analisados na janela: {len(candidates)} | "
                f"Alertas enviados: {len(candidates)}"
            )
        else:
            resumo = (
                f"[EvRadar PRO] Scan concluído (origem={origin}). Nenhum sinal na janela."
            )
        LAST_SCAN_SUMMARY = resumo
        LAST_SCAN_ORIGIN = origin
        try:
            await application.bot.send_message(chat_id=chat_id, text=resumo)
        except Exception as exc:
            logger.warning("Falha ao enviar resumo do scan: %s", exc)
    else:
        LAST_SCAN_SUMMARY = "Scan executado sem chat vinculado."
        LAST_SCAN_ORIGIN = origin

async def send_signal_message(application: Application, sig: SignalInfo, odd_source: str) -> None:
    chat_id = get_bound_chat_id()
    if not chat_id:
        return

    linha_total = sig.goals_home + sig.goals_away + 0.5
    prob_str = f"{sig.prob_pct:.1f}%"
    odd_fair = sig.odd_used / (sig.prob_pct / 100.0) if sig.prob_pct > 0 else 0.0
    ev_str = f"{sig.ev_pct:+.2f}%"

    if odd_source == "ao vivo":
        odd_src_label = "AO VIVO"
    elif odd_source == "última":
        odd_src_label = "última odd conhecida"
    else:
        odd_src_label = "fallback (TARGET_ODD)"

    if VIRTUAL_BANK.enabled:
        stake_pct = suggest_stake_pct(sig.ev_pct, sig.odd_used)
        stake_value = VIRTUAL_BANK.balance * stake_pct / 100.0
        bank_lines = [
            f"- Saldo simulado: R${VIRTUAL_BANK.balance:,.2f}",
            f"- Stake sugerida: {stake_pct:.2f}% da banca (≈ R${stake_value:,.2f})",
        ]
    else:
        bank_lines = [
            "- Banca virtual OFF (usando apenas EV para decisão)."
        ]

    lines: List[str] = []
    lines.append(f"🔔 {sig.tier_label} — {sig.tier_name} (EV {ev_str})")
    lines.append("")
    lines.append(f"🏟️ {sig.home} vs {sig.away}")
    lines.append(f"⏱️ {sig.minute}' | 🔢 {sig.goals_home}–{sig.goals_away}")
    lines.append(f"⚙️ Linha: Over (soma + 0,5) @ {sig.odd_used:.2f} [{odd_src_label}] (linha total: {linha_total:.1f})")
    lines.append("")
    lines.append("📊 Probabilidade & valor:")
    lines.append(f"- P_final (gol a mais): {prob_str}")
    lines.append(f"- Odd justa (modelo): {odd_fair:.2f}")
    lines.append(f"- EV na odd atual: {ev_str}")
    lines.append("")
    lines.append("💰 Gestão (banca virtual):")
    lines.extend(bank_lines)
    lines.append("")
    lines.append("🧩 Interpretação:")
    lines.append(
        "Jogo dentro da janela com probabilidade interessante de 1 gol a mais "
        "e odd acima do justo pelo modelo. Ajuste o risco conforme seu perfil."
    )
    lines.append("")
    lines.append(
        "Se entrar, toca em ✅ ENTREI abaixo para o radar registrar e sugerir stake pela banca virtual."
    )

    keyboard = [
        [
            InlineKeyboardButton("✅ Entrei", callback_data=f"enter:{sig.fixture_id}"),
            InlineKeyboardButton("❌ Pulei", callback_data=f"skip:{sig.fixture_id}"),
        ],
        [
            InlineKeyboardButton("🌐 Abrir mercado (Superbet)", url=BOOKMAKER_URL),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = "\n".join(lines)
    try:
        await application.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
        )
    except Exception as exc:
        logger.warning("Falha ao enviar sinal: %s", exc)

# -----------------------------------------------------
# Autoscan & HTTP dummy
# -----------------------------------------------------
async def autoscan_loop(application: Application) -> None:
    if not AUTOSTART:
        logger.info("AUTOSTART=0 – autoscan desabilitado (use /scan manual).")
        return
    logger.info("Autoscan iniciado (intervalo=%ss)", CHECK_INTERVAL)
    while True:
        try:
            await run_scan(application, origin="auto")
        except Exception as exc:
            logger.exception("Erro no autoscan: %s", exc)
        await asyncio.sleep(CHECK_INTERVAL)

async def start_dummy_http_server() -> None:
    """
    Servidor HTTP simples só para manter a porta 8080 aberta (health-check).
    """
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(1024)
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                "Connection: close\r\n"
                "\r\n"
                "EvRadar PRO online"
            )
            writer.write(response.encode("utf-8"))
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handle, host="0.0.0.0", port=8080)
    addr = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    logger.info("Servidor HTTP dummy ouvindo em %s", addr)
    async with server:
        await server.serve_forever()

# -----------------------------------------------------
# Comandos
# -----------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        bind_chat_id(update.effective_chat.id)

    autoscan_label = "ON a cada " + str(CHECK_INTERVAL) + "s" if AUTOSTART else "OFF (manual apenas)"
    odds_label = "AO VIVO via API-Football (bookmaker_id={})".format(BOOKMAKER_ID) if USE_API_FOOTBALL_ODDS else "Fallback TARGET_ODD"

    banca_label = "ON" if VIRTUAL_BANK.enabled else "OFF"

    lines = [
        "👋 EvRadar PRO online (Notebook/Nuvem).",
        "",
        "⚙️ Configuração atual do EvRadar PRO (Notebook/Nuvem):",
        "",
        f"• Janela: {WINDOW_START}–{WINDOW_END}'",
        f"• Odds alvo (fallback): {TARGET_ODD:.2f} (min: {MIN_ODD:.2f} | max: {MAX_ODD:.2f})",
        f"• EV mínimo p/ alerta: {EV_MIN_PCT:.2f}%",
        f"• Odds em uso: {odds_label}",
        f"• Competições: {len(LEAGUE_IDS)} ids configurados (foco em ligas/copas relevantes)",
        f"• Autoscan: {autoscan_label}",
        f"• Banca virtual: {banca_label}",
        f"• Casa referência: {BOOKMAKER_NAME} ({BOOKMAKER_URL})",
    ]
    await update.message.reply_text("\n".join(lines))

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        bind_chat_id(update.effective_chat.id)
    await update.message.reply_text("🔍 Iniciando varredura manual de jogos ao vivo (EvRadar PRO)...")
    await run_scan(context.application, origin="manual")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    banca_label = "ON" if VIRTUAL_BANK.enabled else "OFF"
    lines = [
        "📈 Status do EvRadar PRO (Notebook/Nuvem)",
        "",
        LAST_SCAN_SUMMARY,
        "",
        f"Banca virtual: {banca_label} | saldo R${VIRTUAL_BANK.balance:,.2f}",
    ]
    await update.message.reply_text("\n".join(lines))

async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    autoscan_label = "ON (CHECK_INTERVAL={}s)".format(CHECK_INTERVAL) if AUTOSTART else "OFF"
    odds_label = "AO VIVO via /odds/live (bookmaker_id={})".format(BOOKMAKER_ID) if USE_API_FOOTBALL_ODDS else "Fallback TARGET_ODD"
    banca_label = "ON" if VIRTUAL_BANK.enabled else "OFF"
    bound = chat_id_bound or TELEGRAM_CHAT_ID
    lines = [
        "🐞 Debug EvRadar PRO",
        "",
        f"API base: {API_BASE}",
        f"Ligas ativas ({len(LEAGUE_IDS)}): {LEAGUE_IDS}",
        f"Autoscan: {autoscan_label}",
        f"Odds: {odds_label}",
        f"chat_id_bound atual: {bound}",
        f"TELEGRAM_CHAT_ID default: {TELEGRAM_CHAT_ID}",
        f"Banca virtual: {banca_label} | saldo R${VIRTUAL_BANK.balance:,.2f}",
    ]
    await update.message.reply_text("\n".join(lines))

async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [
        "🔗 Links úteis:",
        f"- Casa referência: {BOOKMAKER_NAME} → {BOOKMAKER_URL}",
        "",
        "Comandos:",
        "/start  → ver configuração atual",
        "/scan   → rodar varredura agora",
        "/status → ver resumo da última varredura",
        "/debug  → info técnica",
        "/links  → esta mensagem",
    ]
    await update.message.reply_text("\n".join(lines))

# -----------------------------------------------------
# Callbacks (inline buttons) & captura de odd
# -----------------------------------------------------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    chat = query.message.chat if query.message else None
    chat_id = chat.id if chat else None
    if data.startswith("enter:"):
        parts = data.split(":", 1)
        try:
            fixture_id = int(parts[1])
        except Exception:
            return
        if chat_id is not None:
            PENDING_ODD_INPUT[chat_id] = fixture_id
        sig = OPEN_SIGNALS.get(fixture_id)
        title = f"{sig.home} vs {sig.away}" if sig else f"fixture {fixture_id}"
        text_lines = [
            "✅ Beleza, marquei que você ENTROU nesse jogo:",
            f"- {title}",
            "",
            "Agora manda a ODD exata que você pegou nesse mercado (ex.: 1.78).",
            "É só mandar o número aqui no chat, sem mais nada.",
        ]
        await query.message.reply_text("\n".join(text_lines))
    elif data.startswith("skip:"):
        parts = data.split(":", 1)
        try:
            fixture_id = int(parts[1])
        except Exception:
            fixture_id = None
        title = ""
        if fixture_id is not None and fixture_id in OPEN_SIGNALS:
            sig = OPEN_SIGNALS[fixture_id]
            title = f"{sig.home} vs {sig.away}"
        msg = "🧊 Blz, marcado como PULEI."
        if title:
            msg += " " + title
        await query.message.reply_text(msg)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    if chat_id not in PENDING_ODD_INPUT:
        # texto solto – ignoramos pra não spammar
        return

    # estamos esperando uma odd
    fixture_id = PENDING_ODD_INPUT.pop(chat_id)
    text_norm = text.replace(",", ".")
    try:
        odd_entered = float(text_norm)
    except ValueError:
        await update.message.reply_text(
            "Não entendi essa odd. Manda só o número, por exemplo: 1.78"
        )
        # recoloca na fila
        PENDING_ODD_INPUT[chat_id] = fixture_id
        return

    sig = OPEN_SIGNALS.get(fixture_id)
    if not sig:
        await update.message.reply_text(
            f"Registrei que você entrou a {odd_entered:.2f}, "
            "mas não encontrei esse jogo na memória de sinais (pode ter sido de um scan anterior)."
        )
        return

    ev_pct = sig.ev_pct
    stake_info_line = "Banca virtual está OFF, então não calculei stake."
    stake_pct = 0.0
    stake_value = 0.0
    if VIRTUAL_BANK.enabled:
        stake_pct = suggest_stake_pct(ev_pct, odd_entered)
        stake_value = VIRTUAL_BANK.balance * stake_pct / 100.0
        stake_info_line = (
            f"Stake sugerida (banca virtual): {stake_pct:.2f}% "
            f"(≈ R${stake_value:,.2f}) sobre R${VIRTUAL_BANK.balance:,.2f}."
        )

    entry = {
        "fixture_id": fixture_id,
        "home": sig.home,
        "away": sig.away,
        "minute": sig.minute,
        "goals_home": sig.goals_home,
        "goals_away": sig.goals_away,
        "odd_entered": odd_entered,
        "ev_pct": ev_pct,
        "stake_pct": stake_pct,
        "stake_value": stake_value,
    }
    ENTRIES.append(entry)

    lines = [
        "📝 Entrada registrada no radar:",
        f"- Jogo: {sig.home} vs {sig.away}",
        f"- Momento do alerta: {sig.minute}' | Placar {sig.goals_home}–{sig.goals_away}",
        f"- Odd que você pegou: {odd_entered:.2f}",
        f"- EV do modelo no alerta: {ev_pct:+.2f}%",
        "",
        stake_info_line,
        "",
        "Por enquanto a banca virtual é só simulada (não atualiza automática com green/red).",
        "Depois a gente adiciona /green e /red pra ir batendo com o resultado real.",
    ]
    await update.message.reply_text("\n".join(lines))

# -----------------------------------------------------
# Ciclo de vida (post_init / post_shutdown)
# -----------------------------------------------------
async def on_post_init(application: Application) -> None:
    global HTTP_CLIENT
    HTTP_CLIENT = httpx.AsyncClient()
    application.bot_data["autoscan_task"] = asyncio.create_task(autoscan_loop(application))
    application.bot_data["dummy_http_task"] = asyncio.create_task(start_dummy_http_server())
    logger.info("Application started (post_init executado).")

async def on_post_shutdown(application: Application) -> None:
    global HTTP_CLIENT
    logger.info("Encerrando EvRadar PRO...")
    task1 = application.bot_data.get("autoscan_task")
    task2 = application.bot_data.get("dummy_http_task")
    for t in (task1, task2):
        if isinstance(t, asyncio.Task):
            t.cancel()
    if HTTP_CLIENT is not None:
        try:
            await HTTP_CLIENT.aclose()
        except Exception:
            pass
        HTTP_CLIENT = None

# -----------------------------------------------------
# main()
# -----------------------------------------------------
def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN não configurado.")
    app_builder = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(on_post_init)
        .post_shutdown(on_post_shutdown)
    )
    application = app_builder.build()

    # handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("debug", cmd_debug))
    application.add_handler(CommandHandler("links", cmd_links))
    application.add_handler(CallbackQueryHandler(on_button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("Iniciando bot do EvRadar PRO (Notebook/Nuvem)...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
    )

if __name__ == "__main__":
    main()
