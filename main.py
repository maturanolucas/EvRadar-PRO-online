import os
import math
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ============================================================
#  Configuração básica de log
# ============================================================

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("EvRadarPRO")

# ============================================================
#  Config
# ============================================================

API_FOOTBALL_BASE = "https://v3.football.api-sports.io"


def _parse_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    return v in ("1", "true", "yes", "y", "sim")


def _parse_int_list(value: Optional[str]) -> List[int]:
    if not value:
        return []
    out: List[int] = []
    for part in value.split(","):
        p = part.strip()
        if not p:
            continue
        try:
            out.append(int(p))
        except ValueError:
            continue
    return out


@dataclass
class Config:
    telegram_token: str
    api_football_key: str

    window_start: int
    window_end: int

    min_odd: float
    max_odd: float
    ev_min: float  # em fração (ex.: 0.016 = 1.6%)

    cooldown_minutes: int
    check_interval: int
    autostart: bool

    target_odd: float
    use_api_football_odds: bool
    bookmaker_id: Optional[int]
    bookmaker_name: str
    allowed_league_ids: List[int]

    sim_bankroll_initial: float
    bookmaker_url: str

    @classmethod
    def from_env(cls) -> "Config":
        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        api_football_key = os.getenv("API_FOOTBALL_KEY", "").strip()

        if not telegram_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN não definido")
        if not api_football_key:
            raise RuntimeError("API_FOOTBALL_KEY não definido")

        window_start = int(os.getenv("WINDOW_START", "47"))
        window_end = int(os.getenv("WINDOW_END", "75"))

        min_odd = float(os.getenv("MIN_ODD", "1.47"))
        max_odd = float(os.getenv("MAX_ODD", "2.30"))

        # EV mínimo vem em porcentagem no .env (ex.: 1.60)
        ev_min_env = float(os.getenv("EV_MIN_PCT", "1.60"))
        ev_min = ev_min_env / 100.0

        cooldown_minutes = int(os.getenv("COOLDOWN_MINUTES", "6"))
        check_interval = int(os.getenv("CHECK_INTERVAL", "1500"))
        autostart = _parse_bool(os.getenv("AUTOSTART", "0"), default=False)

        target_odd = float(os.getenv("TARGET_ODD", "1.70"))
        use_api_football_odds = _parse_bool(
            os.getenv("USE_API_FOOTBALL_ODDS", "1"), default=True
        )

        bookmaker_id_env = os.getenv("BOOKMAKER_ID", "").strip()
        bookmaker_id: Optional[int]
        if bookmaker_id_env:
            try:
                bookmaker_id = int(bookmaker_id_env)
            except ValueError:
                bookmaker_id = None
        else:
            bookmaker_id = None

        bookmaker_name = os.getenv("BOOKMAKER_NAME", "Superbet").strip() or "Superbet"
        allowed_league_ids = _parse_int_list(os.getenv("ALLOWED_LEAGUE_IDS", ""))

        sim_bankroll_initial = float(os.getenv("BANKROLL_INITIAL", "5000"))
        bookmaker_url = os.getenv("BOOKMAKER_URL", "https://www.superbet.com/").strip()

        return cls(
            telegram_token=telegram_token,
            api_football_key=api_football_key,
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
            bookmaker_name=bookmaker_name,
            allowed_league_ids=allowed_league_ids,
            sim_bankroll_initial=sim_bankroll_initial,
            bookmaker_url=bookmaker_url,
        )


# ============================================================
#  Estado em memória
# ============================================================

def get_state(application: Application) -> Dict[str, Any]:
    if "state" not in application.bot_data:
        application.bot_data["state"] = {
            "users": {},
            "next_bet_id": 1,
        }
    return application.bot_data["state"]  # type: ignore[return-value]


def get_user_state(application: Application, chat_id: int, cfg: Config) -> Dict[str, Any]:
    state = get_state(application)
    users: Dict[int, Dict[str, Any]] = state["users"]  # type: ignore[assignment]
    if chat_id not in users:
        users[chat_id] = {
            "bankroll": cfg.sim_bankroll_initial,
            "bets": [],  # lista de dicts com as apostas
        }
    return users[chat_id]


def get_http_client(application: Application) -> httpx.AsyncClient:
    client = application.bot_data.get("http_client")
    if client is None:
        client = httpx.AsyncClient(timeout=10.0)
        application.bot_data["http_client"] = client
    return client  # type: ignore[return-value]


# ============================================================
#  Funções de API-FOOTBALL
# ============================================================

async def api_get(
    client: httpx.AsyncClient,
    cfg: Config,
    path: str,
    params: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    headers = {"x-apisports-key": cfg.api_football_key}
    url = f"{API_FOOTBALL_BASE}{path}"
    try:
        resp = await client.get(url, headers=headers, params=params or {}, timeout=10.0)
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("Erro ao chamar %s: %s", path, exc)
        return None

    try:
        data = resp.json()
    except Exception:
        logger.warning("Resposta JSON inválida em %s", path)
        return None

    return data


async def fetch_live_fixtures(
    client: httpx.AsyncClient,
    cfg: Config,
) -> List[Dict[str, Any]]:
    data = await api_get(client, cfg, "/fixtures", {"live": "all"})
    if not data:
        return []
    resp = data.get("response") or []
    return resp


async def fetch_fixture_stats(
    client: httpx.AsyncClient,
    cfg: Config,
    fixture_id: int,
) -> List[Dict[str, Any]]:
    data = await api_get(client, cfg, "/fixtures/statistics", {"fixture": fixture_id})
    if not data:
        return []
    resp = data.get("response") or []
    return resp


async def fetch_fixture_by_id(
    client: httpx.AsyncClient,
    cfg: Config,
    fixture_id: int,
) -> Optional[Dict[str, Any]]:
    data = await api_get(client, cfg, "/fixtures", {"id": fixture_id})
    if not data:
        return None
    resp = data.get("response") or []
    if not resp:
        return None
    return resp[0]


async def fetch_live_over_odd_for_sum(
    client: httpx.AsyncClient,
    cfg: Config,
    fixture_id: int,
    goals_sum: int,
) -> Optional[float]:
    """
    Usa odds/live da API-FOOTBALL para pegar a odd da linha Over (soma + 0,5)
    da casa configurada (BOOKMAKER_ID).
    """
    if not cfg.use_api_football_odds or not cfg.bookmaker_id:
        return None

    target_line = float(goals_sum) + 0.5

    params = {
        "fixture": fixture_id,
        "bookmaker": cfg.bookmaker_id,
        "bet": 36,  # Over/Under FT em muitos exemplos
    }
    data = await api_get(client, cfg, "/odds/live", params)
    if not data:
        return None

    items = data.get("response") or []
    if not items:
        logger.info("Sem odds/live para fixture %s (bookmaker=%s)", fixture_id, cfg.bookmaker_id)
        return None

    first = items[0]
    odds_blocks = first.get("odds") or first.get("Odds") or []
    if not odds_blocks:
        logger.info("Formato inesperado de odds/live para fixture %s: %s", fixture_id, first)
        return None

    best_over_odd: Optional[float] = None

    for bet in odds_blocks:
        values = bet.get("values") or bet.get("Values") or []
        for v in values:
            side_raw = v.get("value") or v.get("Value") or ""
            side = str(side_raw).lower()
            handicap_raw = v.get("handicap") or v.get("Handicap")

            line: Optional[float] = None

            if handicap_raw is not None:
                try:
                    line = float(str(handicap_raw).replace(",", "."))
                except ValueError:
                    line = None

            if line is None:
                tokens = str(side_raw).split()
                for t in tokens:
                    try:
                        line = float(t.replace(",", "."))
                        break
                    except ValueError:
                        continue

            if line is None:
                continue

            if "over" not in side:
                continue

            if not math.isclose(line, target_line, abs_tol=1e-6):
                continue

            odd_raw = v.get("odd") or v.get("Odd")
            try:
                price = float(str(odd_raw).replace(",", "."))
            except (TypeError, ValueError):
                continue

            if best_over_odd is None or price < best_over_odd:
                best_over_odd = price

    if best_over_odd is None:
        logger.info(
            "Não achei linha Over %.1f em odds/live para fixture %s",
            target_line,
            fixture_id,
        )
    else:
        logger.info(
            "Odd LIVE encontrada para fixture %s (Over %.1f) @ %.2f",
            fixture_id,
            target_line,
            best_over_odd,
        )

    return best_over_odd


# ============================================================
#  Modelo simples de probabilidade e stake
# ============================================================

def compute_pressure_score(stats_response: List[Dict[str, Any]]) -> float:
    """
    Lê as estatísticas da API-FOOTBALL e monta um índice de pressão 0–10
    baseado em chutes e ataques perigosos.
    """
    total_shots_on_goal = 0
    total_shots = 0
    total_dangerous = 0

    for item in stats_response:
        stats_list = item.get("statistics") or []
        for s in stats_list:
            s_type = s.get("type")
            val = s.get("value")
            try:
                v = int(val)
            except (TypeError, ValueError):
                continue

            if s_type == "Shots on Goal":
                total_shots_on_goal += v
            elif s_type == "Total Shots":
                total_shots += v
            elif s_type == "Dangerous Attacks":
                total_dangerous += v

    # índice bruto
    pressure_raw = total_shots_on_goal * 2.0 + total_shots * 0.5 + total_dangerous * 0.1
    # normalizar em 0–10
    pressure_score = pressure_raw / 3.0
    if pressure_score > 10.0:
        pressure_score = 10.0
    if pressure_score < 0.0:
        pressure_score = 0.0
    return pressure_score


def estimate_goal_probability(
    minute: int,
    goals_sum: int,
    pressure_score: float,
) -> float:
    """
    Estima probabilidade de sair pelo menos 1 gol a mais até o fim.
    Heurística simples, mas calibrada pra dar algo entre ~40–90%.
    """
    remaining = 90 - minute
    if remaining < 0:
        remaining = 0

    base = 0.02 * remaining
    if base > 0.85:
        base = 0.85
    if base < 0.15:
        base = 0.15

    scoreboard_adj = 0.03 * goals_sum
    if scoreboard_adj > 0.18:
        scoreboard_adj = 0.18

    pressure_adj = (pressure_score / 10.0) * 0.25

    p = base + scoreboard_adj + pressure_adj
    if p > 0.96:
        p = 0.96
    if p < 0.25:
        p = 0.25

    return p


def choose_tier_and_stake(
    ev: float,
    used_odd: float,
    bankroll: float,
) -> Optional[Tuple[str, float, float]]:
    """
    Define Tier (A/B/C) e % da banca, com throttle por odd.
    """
    if ev < 0.015:
        return None

    if ev >= 0.07:
        tier = "A"
        stake_pct = 0.030
    elif ev >= 0.05:
        tier = "A"
        stake_pct = 0.0275
    elif ev >= 0.03:
        tier = "B"
        stake_pct = 0.020
    else:
        tier = "C"
        stake_pct = 0.0125

    if used_odd <= 1.80:
        mult = 1.0
    elif used_odd <= 2.60:
        mult = 0.9
    else:
        mult = 0.7

    stake_pct = stake_pct * mult

    if stake_pct < 0.01:
        return None
    if stake_pct > 0.03:
        stake_pct = 0.03

    stake_reais = round(bankroll * stake_pct, 2)
    return tier, stake_pct, stake_reais


# ============================================================
#  Lógica principal de scan + avaliação
# ============================================================

async def perform_scan(
    context: ContextTypes.DEFAULT_TYPE,
    origin: str,
    chat_id: int,
) -> None:
    application = context.application
    cfg: Config = application.bot_data["config"]  # type: ignore[assignment]
    client = get_http_client(application)
    user_state = get_user_state(application, chat_id, cfg)

    await settle_bets_for_all_users(context)

    fixtures = await fetch_live_fixtures(client, cfg)
    total_events = len(fixtures)
    jogos_analizados = 0
    alertas_enviados = 0

    for f in fixtures:
        fixture_info = f.get("fixture") or {}
        league_info = f.get("league") or {}
        teams_info = f.get("teams") or {}
        goals_info = f.get("goals") or {}

        fixture_id = fixture_info.get("id")
        if fixture_id is None:
            continue

        status = fixture_info.get("status") or {}
        minute = status.get("elapsed")
        if minute is None:
            continue

        if minute < cfg.window_start or minute > cfg.window_end:
            continue

        league_id = league_info.get("id")
        if cfg.allowed_league_ids and league_id not in cfg.allowed_league_ids:
            continue

        league_name = str(league_info.get("name") or "").strip()
        round_name = str(league_info.get("round") or "").strip()
        if "friendly" in league_name.lower() or "friendly" in round_name.lower():
            continue

        home_team = (teams_info.get("home") or {}).get("name") or "Home"
        away_team = (teams_info.get("away") or {}).get("name") or "Away"

        goals_home = goals_info.get("home")
        goals_away = goals_info.get("away")
        if goals_home is None:
            goals_home = 0
        if goals_away is None:
            goals_away = 0
        goals_sum = goals_home + goals_away

        jogos_analizados += 1

        stats_response = await fetch_fixture_stats(client, cfg, fixture_id)
        pressure_score = compute_pressure_score(stats_response)

        used_odd = cfg.target_odd
        odd_source = "TARGET"

        if cfg.use_api_football_odds and cfg.bookmaker_id:
            live_odd = await fetch_live_over_odd_for_sum(client, cfg, fixture_id, goals_sum)
            if live_odd is not None:
                used_odd = live_odd
                odd_source = "LIVE"

        if used_odd < cfg.min_odd or used_odd > cfg.max_odd:
            continue

        p_final = estimate_goal_probability(minute, goals_sum, pressure_score)
        ev = p_final * used_odd - 1.0

        if ev < cfg.ev_min:
            continue

        tier_info = choose_tier_and_stake(ev, used_odd, user_state["bankroll"])
        if tier_info is None:
            continue

        tier, stake_pct, stake_reais = tier_info

        state = get_state(application)
        bet_id = state["next_bet_id"]
        state["next_bet_id"] = bet_id + 1

        bet = {
            "id": bet_id,
            "chat_id": chat_id,
            "fixture_id": fixture_id,
            "league": league_name,
            "home": home_team,
            "away": away_team,
            "minute_at_signal": minute,
            "goals_home_at_signal": goals_home,
            "goals_away_at_signal": goals_away,
            "goals_sum_at_signal": goals_sum,
            "line": float(goals_sum) + 0.5,
            "odd": used_odd,
            "odd_source": odd_source,
            "stake_pct": stake_pct,
            "stake_reais": stake_reais,
            "tier": tier,
            "status": "pending",
            "created_at": datetime.utcnow().isoformat(),
        }
        user_state["bets"].append(bet)

        linha_gols = float(goals_sum) + 0.5
        linha_str = f"{linha_gols:.1f}"

        header_line = f"🔔 Tier {tier} — Sinal EvRadar PRO"
        jogo_line = f"🏟️ {home_team} vs {away_team} — {league_name}"
        minuto_line = f"⏱️ {minute}' | 🔢 {goals_home}–{goals_away}"

        odd_str = f"{used_odd:.2f}"
        if odd_source != "LIVE":
            odd_str = odd_str + " (ref.)"

        linha_line = f"⚙️ Linha: Over {linha_str} (soma + 0,5) @ {odd_str}"

        p_pct = p_final * 100.0
        odd_justa = 1.0 / p_final
        ev_pct = ev * 100.0

        prob_line = "📊 Probabilidade & valor:"
        p_line = f"- P_final (gol a mais): {p_pct:.1f}%"
        oddj_line = f"- Odd justa (modelo): {odd_justa:.2f}"
        ev_line = f"- EV: {ev_pct:.2f}% → EV+"

        stake_pct_str = stake_pct * 100.0
        stake_line = (
            f"💰 Stake sugerida: {stake_pct_str:.1f}% da banca (~R${stake_reais:.2f})"
        )

        interp_lines = [
            "🧩 Interpretação:",
            "ritmo e contexto indicam boa chance de 1 gol a mais dentro da janela.",
        ]
        interp_block = "\n".join(interp_lines)

        link_line = f"🔗 Abrir evento ({cfg.bookmaker_name}) ({cfg.bookmaker_url})"

        text_parts = [
            header_line,
            "",
            jogo_line,
            minuto_line,
            linha_line,
            "",
            prob_line,
            p_line,
            oddj_line,
            ev_line,
            "",
            stake_line,
            "",
            interp_block,
            "",
            link_line,
        ]
        text = "\n".join(text_parts)

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Confirmei a entrada", callback_data=f"BET|{bet_id}|CONF"
                    ),
                    InlineKeyboardButton(
                        "🚫 Pulei essa", callback_data=f"BET|{bet_id}|SKIP"
                    ),
                ]
            ]
        )

        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard,
        )
        alertas_enviados += 1

    resumo = (
        f"[EvRadar PRO] Scan concluído (origem={origin}). "
        f"Eventos ao vivo: {total_events} | "
        f"Jogos analisados na janela: {jogos_analizados} | "
        f"Alertas enviados: {alertas_enviados}."
    )
    await context.bot.send_message(chat_id=chat_id, text=resumo)


# ============================================================
#  Liquidação automática das apostas confirmadas
# ============================================================

async def settle_bets_for_all_users(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    application = context.application
    cfg: Config = application.bot_data["config"]  # type: ignore[assignment]
    client = get_http_client(application)
    state = get_state(application)
    users: Dict[int, Dict[str, Any]] = state["users"]  # type: ignore[assignment]

    for chat_id, user_state in users.items():
        bets: List[Dict[str, Any]] = user_state.get("bets", [])
        bankroll = user_state.get("bankroll", cfg.sim_bankroll_initial)

        for bet in bets:
            if bet.get("status") != "confirmed":
                continue
            if bet.get("settled"):
                continue

            fixture_id = bet["fixture_id"]
            fixture_data = await fetch_fixture_by_id(client, cfg, fixture_id)
            if not fixture_data:
                continue

            fixture_info = fixture_data.get("fixture") or {}
            goals_info = fixture_data.get("goals") or {}

            status = fixture_info.get("status") or {}
            short_status = str(status.get("short") or "")

            if short_status not in ("FT", "AET", "PEN"):
                continue

            final_home = goals_info.get("home")
            final_away = goals_info.get("away")
            if final_home is None:
                final_home = 0
            if final_away is None:
                final_away = 0
            final_sum = final_home + final_away

            sum_at_signal = bet.get("goals_sum_at_signal", 0)
            stake_reais = bet.get("stake_reais", 0.0)
            used_odd = bet.get("odd", 1.0)

            if final_sum >= sum_at_signal + 1:
                profit = stake_reais * (used_odd - 1.0)
                outcome = "GREEN"
                emoji = "✅"
            else:
                profit = -stake_reais
                outcome = "RED"
                emoji = "❌"

            bankroll = bankroll + profit
            user_state["bankroll"] = bankroll

            bet["status"] = "won" if profit > 0 else "lost"
            bet["settled"] = True
            bet["settled_at"] = datetime.utcnow().isoformat()
            bet["profit"] = profit

            home_team = bet.get("home", "Home")
            away_team = bet.get("away", "Away")
            line = bet.get("line", 0.5)

            lines = []
            header_line = f"{emoji} {outcome} — {home_team} {final_home}–{final_away} {away_team}"
            lines.append(header_line)
            lines.append(
                f"⚙️ Linha: Over {line:.1f} (soma + 0,5) @ {used_odd:.2f}"
            )
            lines.append(
                f"💰 Resultado na banca fictícia: R${profit:.2f} (banca atual: R${bankroll:.2f})"
            )

            text = "\n".join(lines)
            await context.bot.send_message(chat_id=chat_id, text=text)


# ============================================================
#  Handlers do Telegram
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    application = context.application
    cfg: Config = application.bot_data["config"]  # type: ignore[assignment]
    chat_id = update.effective_chat.id
    user_state = get_user_state(application, chat_id, cfg)

    linhas = [
        "👋 EvRadar PRO online.",
        "",
        f"Janela padrão: {cfg.window_start}–{cfg.window_end}ʼ",
        f"Odd ref (TARGET_ODD): {cfg.target_odd:.2f}",
        f"Odds aceitas: {cfg.min_odd:.2f}–{cfg.max_odd:.2f}",
        f"EV mínimo: {cfg.ev_min * 100.0:.2f}%",
        f"Cooldown por jogo: {cfg.cooldown_minutes} min",
        f"Banca fictícia: R${user_state['bankroll']:.2f}",
        "",
        "Comandos:",
        "  /scan   → rodar varredura agora",
        "  /status → ver último resumo e banca",
        "  /debug  → info técnica",
        "  /links  → links úteis / bookmaker",
        "  /id     → mostrar seu chat_id",
    ]
    text = "\n".join(linhas)
    await update.message.reply_text(text)


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await update.message.reply_text("🔍 Iniciando varredura manual de jogos ao vivo...")
    await perform_scan(context, origin="manual", chat_id=chat_id)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    application = context.application
    cfg: Config = application.bot_data["config"]  # type: ignore[assignment]
    chat_id = update.effective_chat.id
    await settle_bets_for_all_users(context)
    user_state = get_user_state(application, chat_id, cfg)

    bets: List[Dict[str, Any]] = user_state.get("bets", [])
    pendentes = [b for b in bets if b.get("status") == "confirmed" and not b.get("settled")]
    texto = (
        f"📊 Status EvRadar PRO\n"
        f"- Banca fictícia: R${user_state['bankroll']:.2f}\n"
        f"- Apostas confirmadas em aberto: {len(pendentes)}"
    )
    await update.message.reply_text(texto)


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["config"]  # type: ignore[assignment]
    linhas = [
        "🛠 Debug EvRadar PRO",
        f"- Janela: {cfg.window_start}–{cfg.window_end}ʼ",
        f"- Odds aceitas: {cfg.min_odd:.2f}–{cfg.max_odd:.2f}",
        f"- EV mínimo: {cfg.ev_min * 100.0:.2f}%",
        f"- Cooldown: {cfg.cooldown_minutes} min",
        f"- Intervalo autoscan: {cfg.check_interval} s",
        f"- AUTOSTART: {cfg.autostart}",
        f"- USE_API_FOOTBALL_ODDS: {cfg.use_api_football_odds}",
        f"- BOOKMAKER_ID: {cfg.bookmaker_id}",
        f"- Bookmaker: {cfg.bookmaker_name} ({cfg.bookmaker_url})",
        f"- ALLOWED_LEAGUE_IDS: {cfg.allowed_league_ids}",
    ]
    text = "\n".join(linhas)
    await update.message.reply_text(text)


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["config"]  # type: ignore[assignment]
    linhas = [
        "🔗 Links úteis:",
        f"- Casa principal: {cfg.bookmaker_name} ({cfg.bookmaker_url})",
        "- API-FOOTBALL: https://rapidapi.com/api-sports/api/api-football",
    ]
    text = "\n".join(linhas)
    await update.message.reply_text(text)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"🆔 Seu chat_id: {chat_id}")


async def cb_bet_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    parts = data.split("|")
    if len(parts) != 3 or parts[0] != "BET":
        return

    try:
        bet_id = int(parts[1])
    except ValueError:
        await query.edit_message_reply_markup(reply_markup=None)
        return

    action = parts[2]

    application = context.application
    cfg: Config = application.bot_data["config"]  # type: ignore[assignment]
    chat_id = query.message.chat.id
    user_state = get_user_state(application, chat_id, cfg)
    bets: List[Dict[str, Any]] = user_state.get("bets", [])

    bet = None
    for b in bets:
        if b.get("id") == bet_id:
            bet = b
            break

    if bet is None:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("⚠ Aposta não encontrada (pode já ter sido tratada).")
        return

    if action == "CONF":
        if bet.get("status") == "pending":
            bet["status"] = "confirmed"
            msg = (
                f"✅ Entrada confirmada em {bet.get('home')} vs {bet.get('away')} "
                f"@ {bet.get('odd'):.2f} (stake ~R${bet.get('stake_reais'):.2f})."
            )
        else:
            msg = "Essa aposta já havia sido tratada."
    elif action == "SKIP":
        if bet.get("status") == "pending":
            bet["status"] = "skipped"
            msg = "🚫 Entrada marcada como 'pulei essa'."
        else:
            msg = "Essa aposta já havia sido tratada."
    else:
        msg = "Ação desconhecida."

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(msg)


# ============================================================
#  main()
# ============================================================

def main() -> None:
    cfg = Config.from_env()

    logger.info("Config EvRadar PRO:")
    logger.info("- Janela: %s–%sʼ", cfg.window_start, cfg.window_end)
    logger.info("- Odds aceitas: %.2f–%.2f", cfg.min_odd, cfg.max_odd)
    logger.info("- EV mínimo: %.2f%%", cfg.ev_min * 100.0)
    logger.info("- Cooldown por jogo: %s min", cfg.cooldown_minutes)
    logger.info("- Intervalo autoscan: %s s", cfg.check_interval)
    logger.info("- AUTOSTART: %s", cfg.autostart)
    logger.info("- TARGET_ODD (fallback): %.2f", cfg.target_odd)
    logger.info("- USE_API_FOOTBALL_ODDS: %s", cfg.use_api_football_odds)
    logger.info("- BOOKMAKER_ID: %s", cfg.bookmaker_id)
    logger.info("- Bookmaker: %s (%s)", cfg.bookmaker_name, cfg.bookmaker_url)
    logger.info("- ALLOWED_LEAGUE_IDS: %s", cfg.allowed_league_ids)

    application = Application.builder().token(cfg.telegram_token).build()
    application.bot_data["config"] = cfg
    application.bot_data["http_client"] = httpx.AsyncClient(timeout=10.0)

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("debug", cmd_debug))
    application.add_handler(CommandHandler("links", cmd_links))
    application.add_handler(CommandHandler("id", cmd_id))
    application.add_handler(CallbackQueryHandler(cb_bet_buttons))

    logger.info("Iniciando bot do EvRadar PRO...")
    logger.info("AUTOSTART=%s → varredura apenas via /scan por enquanto.", cfg.autostart)

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
