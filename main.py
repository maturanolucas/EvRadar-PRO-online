#!/usr/bin/env python3
"""
EvRadar PRO — Versão Notebook/Nuvem (Telegram + API-Football, Render-friendly, odds reais + banca virtual básica)

- Funciona local (notebook) e em nuvem (Render Web Service free).
- Usa polling do Telegram (sem webhook).
- Abre um servidor HTTP mínimo só para o Render enxergar uma porta aberta.
- Autoscan sem spam: só manda mensagem automática SE houver alerta.
- Calcula EV usando, quando possível, a ODD REAL AO VIVO da casa (API-Football /odds/live),
  comparando com a odd justa do modelo.
- Inclui banca virtual opcional com stake sugerida e comando /entrei para registrar entradas.

Requisitos:
    pip install python-telegram-bot==21.6 httpx python-dotenv
"""

import asyncio
import logging
import os
import signal
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    # Em produção (Render) não é obrigatório ter python-dotenv
    pass

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# =========================
#  Logging
# =========================

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("EvRadarPRO")


# =========================
#  Config / Settings
# =========================

DEFAULT_LEAGUES = [
    39,   # Premier League
    140,  # La Liga
    135,  # Serie A
    78,   # Bundesliga
    61,   # Ligue 1
    71,   # Serie B Itália / ligas fortes
    72,
    13,   # Brasileirão A
    14,   # Brasileirão B
    2, 3, 4, 5,  # UCL, UEL etc (ajustado conforme API)
    180,
    203,
    62,
    88,
    89,
    94,
    128,
    136,
    141,
    144,
    79,
    253,
]


@dataclass
class Settings:
    api_key: str
    bot_token: str
    chat_id_default: Optional[int]

    target_odd: float
    ev_min_pct: float
    min_odd: float
    max_odd: float
    window_start: int
    window_end: int
    autostart: bool
    check_interval: int

    league_ids: List[int]
    bookmaker_name: str
    bookmaker_url: str
    use_live_odds: bool
    bookmaker_id: Optional[int]

    bank_virtual_enabled: bool
    bankroll_initial: float
    kelly_fraction: float
    stake_cap_pct: float
    bank_leverage: float
    context_boost_max_pp: float

    def describe(self) -> str:
        linhas: List[str] = []
        linhas.append("⚙️ Configuração atual do EvRadar PRO (Notebook/Nuvem):")
        linhas.append("")
        linhas.append("• Janela: {}–{}'".format(self.window_start, self.window_end))
        linhas.append(
            "• Odds alvo (fallback): {:.2f} (min: {:.2f} | max: {:.2f})".format(
                self.target_odd, self.min_odd, self.max_odd
            )
        )
        linhas.append("• EV mínimo p/ alerta: {:.2f}%".format(self.ev_min_pct))

        if self.use_live_odds:
            if self.bookmaker_id is not None:
                linhas.append(
                    "• Odds em uso: AO VIVO via API-Football (bookmaker_id={})".format(
                        self.bookmaker_id
                    )
                )
            else:
                linhas.append(
                    "• Odds em uso: AO VIVO via API-Football (primeiro bookmaker disponível)"
                )
        else:
            linhas.append(
                "• Odds em uso: fixas pela odd alvo {:.2f}".format(self.target_odd)
            )

        linhas.append(
            "• Competições: {} ids configurados (foco em ligas/copas relevantes)".format(
                len(self.league_ids)
            )
        )
        if self.autostart:
            linhas.append("• Autoscan: ON a cada {}s".format(self.check_interval))
        else:
            linhas.append("• Autoscan: OFF (use /scan manual)")

        if self.bank_virtual_enabled:
            linhas.append(
                "• Banca virtual: ON (R${:.2f}, Kelly x{:.2f}, cap {:.1f}%, alavancagem x{:.2f})".format(
                    self.bankroll_initial,
                    self.kelly_fraction,
                    self.stake_cap_pct,
                    self.bank_leverage,
                )
            )
        else:
            linhas.append("• Banca virtual: OFF")

        linhas.append(
            "• Casa referência: {} ({})".format(
                self.bookmaker_name, self.bookmaker_url
            )
        )
        return "\n".join(linhas)


@dataclass
class Candidate:
    fixture_id: int
    league_name: str
    home_team: str
    away_team: str
    minute: int
    goals_home: int
    goals_away: int
    prob_goal: float
    fair_odd: float
    used_odd: float
    odd_source: str
    ev_pct: float
    tier: str
    pressure_desc: str
    context_desc: str


@dataclass
class VirtualBet:
    fixture_id: int
    desc: str
    stake_pct: float
    stake_value: float
    odd: float


@dataclass
class BotState:
    autoscan_task: Optional[asyncio.Task] = None
    last_scan_summary: str = "Nenhuma varredura ainda."
    last_candidates: Dict[int, Candidate] = field(default_factory=dict)
    bank_virtual_balance: float = 0.0
    bank_virtual_enabled: bool = False
    virtual_bets: List[VirtualBet] = field(default_factory=list)
    chat_id_bound: Optional[int] = None
    last_live_odds: Dict[int, float] = field(default_factory=dict)


STATE = BotState()


def load_settings() -> Settings:
    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not api_key or not bot_token:
        logger.error("API_FOOTBALL_KEY e TELEGRAM_BOT_TOKEN são obrigatórios.")
        raise SystemExit(1)

    chat_raw = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    chat_id_default: Optional[int] = None
    if chat_raw:
        try:
            chat_id_default = int(chat_raw)
        except Exception:
            logger.warning("TELEGRAM_CHAT_ID inválido: %r", chat_raw)

    def _float_env(key: str, default: float) -> float:
        raw = os.getenv(key, "").strip()
        if not raw:
            return default
        try:
            return float(raw.replace(",", "."))
        except Exception:
            logger.warning("Valor inválido para %s=%r, usando default %.2f", key, raw, default)
            return default

    def _int_env(key: str, default: int) -> int:
        raw = os.getenv(key, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except Exception:
            logger.warning("Valor inválido para %s=%r, usando default %d", key, raw, default)
            return default

    target_odd = _float_env("TARGET_ODD", 1.70)
    ev_min_pct = _float_env("EV_MIN_PCT", 3.0)
    min_odd = _float_env("MIN_ODD", 1.47)
    max_odd = _float_env("MAX_ODD", 3.50)
    window_start = _int_env("WINDOW_START", 47)
    window_end = _int_env("WINDOW_END", 82)

    autostart_flag = os.getenv("AUTOSTART", "0").strip()
    autostart = autostart_flag == "1"
    check_interval = _int_env("CHECK_INTERVAL", 60)

    leagues_raw = os.getenv("LEAGUE_IDS", "").strip()
    if leagues_raw:
        league_ids: List[int] = []
        for part in leagues_raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                league_ids.append(int(part))
            except Exception:
                continue
        if not league_ids:
            league_ids = DEFAULT_LEAGUES
    else:
        league_ids = DEFAULT_LEAGUES

    bookmaker_name = os.getenv("BOOKMAKER_NAME", "Superbet").strip() or "Superbet"
    bookmaker_url = os.getenv("BOOKMAKER_URL", "https://www.superbet.com/").strip() or "https://www.superbet.com/"

    use_live_odds_flag = os.getenv("USE_API_FOOTBALL_ODDS", "1").strip()
    use_live_odds = use_live_odds_flag == "1"
    bookmaker_id_raw = os.getenv("BOOKMAKER_ID", "").strip()
    if bookmaker_id_raw:
        try:
            bookmaker_id: Optional[int] = int(bookmaker_id_raw)
        except Exception:
            logger.warning("BOOKMAKER_ID inválido: %r (ignorando)", bookmaker_id_raw)
            bookmaker_id = None
    else:
        bookmaker_id = None

    # Banca virtual
    bank_enabled_flag = os.getenv("BANK_VIRTUAL_ENABLED", "0").strip()
    bank_virtual_enabled = bank_enabled_flag == "1"
    bankroll_initial = _float_env("BANKROLL_INITIAL", 5000.0)
    kelly_fraction = _float_env("KELLY_FRACTION", 0.5)
    stake_cap_pct = _float_env("STAKE_CAP_PCT", 3.0)
    bank_leverage = _float_env("BANK_LEVERAGE", 1.0)
    context_boost_max_pp = _float_env("CONTEXT_BOOST_MAX_PP", 3.0)

    settings = Settings(
        api_key=api_key,
        bot_token=bot_token,
        chat_id_default=chat_id_default,
        target_odd=target_odd,
        ev_min_pct=ev_min_pct,
        min_odd=min_odd,
        max_odd=max_odd,
        window_start=window_start,
        window_end=window_end,
        autostart=autostart,
        check_interval=check_interval,
        league_ids=league_ids,
        bookmaker_name=bookmaker_name,
        bookmaker_url=bookmaker_url,
        use_live_odds=use_live_odds,
        bookmaker_id=bookmaker_id,
        bank_virtual_enabled=bank_virtual_enabled,
        bankroll_initial=bankroll_initial,
        kelly_fraction=kelly_fraction,
        stake_cap_pct=stake_cap_pct,
        bank_leverage=bank_leverage,
        context_boost_max_pp=context_boost_max_pp,
    )

    STATE.bank_virtual_enabled = bank_virtual_enabled
    STATE.bank_virtual_balance = bankroll_initial

    return settings


# =========================
#  API-Football Client
# =========================

API_BASE = "https://v3.football.api-sports.io"
OVER_UNDER_BET_ID = 36  # mercado Over/Under para odds/live (API-Football)


class ApiFootballClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=API_BASE,
                headers={"x-apisports-key": self.api_key},
                timeout=10.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch_live_fixtures(self) -> List[Dict[str, Any]]:
        client = await self._get_client()
        resp = await client.get("/fixtures", params={"live": "all"})
        if resp.status_code != 200:
            logger.error("HTTP %s em fixtures/live: %s", resp.status_code, resp.text[:300])
            return []
        data = resp.json()
        return data.get("response", []) or []

    async def fetch_statistics(self, fixture_id: int) -> Dict[str, Dict[str, Any]]:
        client = await self._get_client()
        resp = await client.get("/fixtures/statistics", params={"fixture": fixture_id})
        if resp.status_code != 200:
            logger.error("HTTP %s em fixtures/statistics(%s): %s", resp.status_code, fixture_id, resp.text[:300])
            return {}

        data = resp.json()
        stats = data.get("response", []) or []
        out: Dict[str, Dict[str, Any]] = {}
        for item in stats:
            team = item.get("team") or {}
            side_name = (team.get("name") or "").lower()
            side_key = "home" if "home" in side_name else "away"
            vals = item.get("statistics") or []
            stats_map: Dict[str, Any] = {}
            for s in vals:
                stat_name = str(s.get("type") or "").lower()
                val = s.get("value")
                stats_map[stat_name] = val
            out[side_key] = stats_map
        return out

    async def fetch_live_over_under_odd(
        self,
        fixture_id: int,
        total_goals: int,
        bookmaker_id: Optional[int],
    ) -> Optional[float]:
        client = await self._get_client()
        params: Dict[str, Any] = {
            "fixture": fixture_id,
            "bet": OVER_UNDER_BET_ID,
        }
        try:
            resp = await client.get("/odds/live", params=params)
        except Exception as exc:
            logger.error("Erro em odds/live para fixture %s: %s", fixture_id, exc)
            return None

        if resp.status_code != 200:
            logger.error(
                "HTTP %s em odds/live(%s): %s",
                resp.status_code,
                fixture_id,
                resp.text[:300],
            )
            return None

        data = resp.json()
        items = data.get("response", []) or []
        if not items:
            return None

        item = items[0]
        bookmakers = item.get("bookmakers") or []
        chosen_book: Optional[Dict[str, Any]] = None

        if bookmaker_id is not None:
            for bk in bookmakers:
                if bk.get("id") == bookmaker_id:
                    chosen_book = bk
                    break

        if chosen_book is None and bookmakers:
            chosen_book = bookmakers[0]

        if chosen_book is None:
            return None

        bets = chosen_book.get("bets") or []
        bet_obj: Optional[Dict[str, Any]] = None
        for b in bets:
            if b.get("id") == OVER_UNDER_BET_ID:
                bet_obj = b
                break
            name = str(b.get("name") or "").lower()
            if "over" in name and "under" in name:
                bet_obj = b
                break

        if bet_obj is None:
            return None

        desired_line = float(total_goals) + 0.5
        desired_str = "{:.1f}".format(desired_line)

        values = bet_obj.get("values") or []
        for v in values:
            label = str(v.get("value") or "")
            label_lower = label.lower()
            if "over" in label_lower and desired_str in label_lower:
                odd_raw = v.get("odd")
                if odd_raw is None:
                    continue
                try:
                    odd_val = float(str(odd_raw).replace(",", "."))
                    return odd_val
                except Exception:
                    continue

        return None


# =========================
#  Modelo / Heurísticas
# =========================

def _get_int_stat(stats: Dict[str, Any], key: str) -> int:
    val = stats.get(key)
    if val is None:
        return 0
    try:
        return int(str(val).replace("%", "").strip())
    except Exception:
        return 0


def compute_pressure_and_prob(
    minute: int,
    goals_home: int,
    goals_away: int,
    stats_home: Dict[str, Any],
    stats_away: Dict[str, Any],
    context_boost_max_pp: float,
    league_name: str,
) -> Tuple[float, str, float, str]:
    """
    Retorna:
      prob_goal (0-1),
      pressure_desc,
      context_boost_pp,
      context_desc
    """
    # Stats básicas
    sh_home = _get_int_stat(stats_home, "shots on goal")
    sh_away = _get_int_stat(stats_away, "shots on goal")
    st_home = _get_int_stat(stats_home, "shots on target")
    st_away = _get_int_stat(stats_away, "shots on target")
    da_home = _get_int_stat(stats_home, "dangerous attacks")
    da_away = _get_int_stat(stats_away, "dangerous attacks")
    poss_home = _get_int_stat(stats_home, "ball possession")
    poss_away = _get_int_stat(stats_away, "ball possession")

    total_shots = sh_home + sh_away + st_home + st_away
    total_da = da_home + da_away

    pressure_score = 0.0
    pressure_bits: List[str] = []

    if total_shots >= 8:
        pressure_score += 0.15
        pressure_bits.append("muitos chutes")
    elif total_shots >= 5:
        pressure_score += 0.08
        pressure_bits.append("bom volume de chutes")

    if st_home + st_away >= 5:
        pressure_score += 0.15
        pressure_bits.append("muitos chutes no alvo")
    elif st_home + st_away >= 3:
        pressure_score += 0.08
        pressure_bits.append("chutes no alvo razoáveis")

    if total_da >= 60:
        pressure_score += 0.10
        pressure_bits.append("muitos ataques perigosos")
    elif total_da >= 40:
        pressure_score += 0.06
        pressure_bits.append("ataques perigosos ok")

    # Ritmo por minuto (quanto mais tarde, menos tempo pra sair o gol)
    base_p = 0.60  # prob base bruta de 1 gol a partir de ~50'
    if minute >= 75:
        base_p -= 0.08
    elif minute >= 70:
        base_p -= 0.04
    elif minute <= 55:
        base_p += 0.03

    # Ajuste por placar (0x0, 1x0, etc.)
    total_goals = goals_home + goals_away
    if total_goals == 0:
        base_p += 0.04  # tendência pra abrir o placar
    elif total_goals == 1:
        base_p += 0.02

    # Contexto tático simples: desequilíbrio de posse
    poss_diff = abs(poss_home - poss_away)
    if poss_diff >= 20:
        pressure_score += 0.05
        pressure_bits.append("posse bem desequilibrada")

    # probability raw
    prob_goal = max(0.40, min(0.85, base_p + pressure_score))

    # Context boost por importância da competição (simples, baseado no nome)
    league_lower = league_name.lower()
    context_boost_pp = 0.0
    context_bits: List[str] = []
    if "champions" in league_lower or "libertadores" in league_lower:
        context_boost_pp += context_boost_max_pp
        context_bits.append("jogo grande (Champions/Liberta)")
    elif any(k in league_lower for k in ["serie a", "premier", "la liga", "bundesliga", "ligue 1"]):
        context_boost_pp += min(context_boost_max_pp, 2.0)
        context_bits.append("liga principal")
    elif any(k in league_lower for k in ["brasileirao", "brasileirão", "superliga", "primeira", "liga profissional"]):
        context_boost_pp += min(context_boost_max_pp, 2.0)
        context_bits.append("liga nacional forte")

    # Converte boost em probabilidade (pontos percentuais)
    prob_goal += context_boost_pp / 100.0
    prob_goal = max(0.40, min(0.90, prob_goal))

    pressure_desc = ", ".join(pressure_bits) if pressure_bits else "ritmo moderado"
    context_desc = ", ".join(context_bits) if context_bits else "contexto neutro"

    return prob_goal, pressure_desc, context_boost_pp, context_desc


def classify_tier(ev_pct: float) -> str:
    if ev_pct >= 7.0:
        return "A"
    elif ev_pct >= 4.0:
        return "B"
    else:
        return "C"


def compute_stake_pct(
    settings: Settings,
    prob_goal: float,
    used_odd: float,
) -> float:
    # Kelly fracionado simples
    if not settings.bank_virtual_enabled:
        return 0.0
    b = used_odd - 1.0
    p = prob_goal
    q = 1.0 - p
    edge = (b * p - q) / b if b > 0 else 0.0
    if edge <= 0:
        return 0.0
    stake_kelly = edge
    stake_frac = stake_kelly * settings.kelly_fraction
    # cap
    stake_frac = min(stake_frac, settings.stake_cap_pct / 100.0)
    # nunca negativo
    return max(0.0, stake_frac)


# =========================
#  Scanner de jogos
# =========================

async def find_candidates(settings: Settings, api: ApiFootballClient) -> List[Candidate]:
    fixtures = await api.fetch_live_fixtures()
    candidates: List[Candidate] = []

    for fx in fixtures:
        fixture = fx.get("fixture") or {}
        league = fx.get("league") or {}
        teams = fx.get("teams") or {}
        goals = fx.get("goals") or {}

        league_id = league.get("id")
        if league_id not in settings.league_ids:
            continue

        # status / minuto
        status = fixture.get("status") or {}
        elapsed = status.get("elapsed")
        if elapsed is None:
            continue
        try:
            minute = int(elapsed)
        except Exception:
            continue

        if minute < settings.window_start or minute > settings.window_end:
            continue

        fixture_id = fixture.get("id")
        if fixture_id is None:
            continue
        try:
            fixture_id_int = int(fixture_id)
        except Exception:
            continue

        home = teams.get("home") or {}
        away = teams.get("away") or {}
        home_name = home.get("name") or "Casa"
        away_name = away.get("name") or "Fora"
        league_name = league.get("name") or "Liga"

        goals_home = goals.get("home") or 0
        goals_away = goals.get("away") or 0

        stats = await api.fetch_statistics(fixture_id_int)
        stats_home = stats.get("home", {})
        stats_away = stats.get("away", {})

        prob_goal, pressure_desc, context_boost_pp, context_desc = compute_pressure_and_prob(
            minute,
            goals_home,
            goals_away,
            stats_home,
            stats_away,
            settings.context_boost_max_pp,
            league_name,
        )

        fair_odd = 1.0 / prob_goal if prob_goal > 0 else settings.target_odd

        total_goals = goals_home + goals_away
        used_odd = settings.target_odd
        odd_source = "alvo"

        if settings.use_live_odds:
            live_odd = await api.fetch_live_over_under_odd(
                fixture_id_int,
                total_goals,
                settings.bookmaker_id,
            )
            if live_odd is not None:
                used_odd = live_odd
                odd_source = "live"
                STATE.last_live_odds[fixture_id_int] = live_odd
            else:
                # se não veio odd nova (ex: mercado suspenso), tenta usar a última odd viva conhecida
                if fixture_id_int in STATE.last_live_odds:
                    used_odd = STATE.last_live_odds[fixture_id_int]
                    odd_source = "live-cache"

        if used_odd < settings.min_odd or used_odd > settings.max_odd:
            continue

        ev = prob_goal * used_odd - 1.0
        ev_pct = ev * 100.0

        if ev_pct < settings.ev_min_pct:
            continue

        tier = classify_tier(ev_pct)

        cand = Candidate(
            fixture_id=fixture_id_int,
            league_name=league_name,
            home_team=home_name,
            away_team=away_name,
            minute=minute,
            goals_home=goals_home,
            goals_away=goals_away,
            prob_goal=prob_goal,
            fair_odd=fair_odd,
            used_odd=used_odd,
            odd_source=odd_source,
            ev_pct=ev_pct,
            tier=tier,
            pressure_desc=pressure_desc,
            context_desc=context_desc,
        )
        candidates.append(cand)

    candidates.sort(key=lambda c: c.ev_pct, reverse=True)
    return candidates


def format_candidate_message(c: Candidate, settings: Settings) -> str:
    placar = "{}–{}".format(c.goals_home, c.goals_away)
    prob_str = "{:.1f}%".format(c.prob_goal * 100.0)
    ev_str = "{:.2f}%".format(c.ev_pct)
    fair_odd_str = "{:.2f}".format(c.fair_odd)
    used_odd_str = "{:.2f}".format(c.used_odd)
    linha = "Over (soma + 0,5) @ {}".format(used_odd_str)

    if c.tier == "A":
        tier_title = "Tier A — Sinal forte"
    elif c.tier == "B":
        tier_title = "Tier B — Sinal interessante"
    else:
        tier_title = "Tier C — Sinal mais leve"

    ev_label = "EV+ ✅" if c.ev_pct >= 0 else "EV- ❌"

    if c.odd_source == "live":
        odd_info = "odd real da casa"
    elif c.odd_source == "live-cache":
        odd_info = "última odd real conhecida"
    else:
        odd_info = "odd alvo (fallback)"

    # stake sugerida
    stake_pct = compute_stake_pct(settings, c.prob_goal, c.used_odd)
    stake_line = ""
    if settings.bank_virtual_enabled and stake_pct > 0 and STATE.bank_virtual_balance > 0:
        stake_value = STATE.bank_virtual_balance * stake_pct * settings.bank_leverage
        stake_line = "📌 Stake sugerida (banca virtual): {:.2f}% ≈ R${:.2f}".format(
            stake_pct * 100.0, stake_value
        )
    else:
        stake_line = "📌 Stake sugerida: ajuste conforme sua gestão."

    linhas: List[str] = []
    linhas.append("🔔 {} ({})".format(tier_title, c.league_name))
    linhas.append("")
    linhas.append("🏟️ {} vs {}".format(c.home_team, c.away_team))
    linhas.append("⏱️ {}' | 🔢 Placar {}".format(c.minute, placar))
    linhas.append("⚙️ Linha: {} [{}]".format(linha, odd_info))
    linhas.append("📊 Probabilidade: {} | Odd justa: {}".format(prob_str, fair_odd_str))
    linhas.append("💰 EV: {} → {}".format(ev_str, ev_label))
    linhas.append("")
    linhas.append("🧩 Contexto: {} | Pressão: {}".format(c.context_desc, c.pressure_desc))
    linhas.append(stake_line)
    linhas.append("")
    linhas.append(
        "Se entrar, responda com: /entrei {} <odd_que_você_pegar>\n"
        "Exemplo: /entrei {} {:.2f}".format(c.fixture_id, c.fixture_id, c.used_odd)
    )
    return "\n".join(linhas)


# =========================
#  Telegram Handlers
# =========================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]  # type: ignore[index]
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id:
        STATE.chat_id_bound = chat_id
    text = "👋 EvRadar PRO online (Notebook/Nuvem).\n\n" + settings.describe()
    await update.effective_message.reply_text(text)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = "📈 Status do EvRadar PRO (Notebook/Nuvem)\n\n" + STATE.last_scan_summary
    await update.effective_message.reply_text(msg)


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]  # type: ignore[index]
    linhas: List[str] = []
    linhas.append("🐞 Debug EvRadar PRO")
    linhas.append("")
    linhas.append("API base: {}".format(API_BASE))
    linhas.append("Ligas ativas ({}): {}".format(len(settings.league_ids), settings.league_ids))
    linhas.append(
        "Autoscan: {} (CHECK_INTERVAL={}s)".format(
            "ON" if settings.autostart else "OFF", settings.check_interval
        )
    )
    if settings.use_live_odds:
        if settings.bookmaker_id is not None:
            linhas.append("Odds: AO VIVO via /odds/live (bookmaker_id={})".format(settings.bookmaker_id))
        else:
            linhas.append("Odds: AO VIVO via /odds/live (primeiro bookmaker)")
    else:
        linhas.append("Odds: fixas pela odd alvo {:.2f}".format(settings.target_odd))
    if STATE.chat_id_bound:
        linhas.append("chat_id_bound atual: {}".format(STATE.chat_id_bound))
    if settings.chat_id_default:
        linhas.append("TELEGRAM_CHAT_ID default: {}".format(settings.chat_id_default))
    linhas.append(
        "Banca virtual: {} | saldo R${:.2f}".format(
            "ON" if settings.bank_virtual_enabled else "OFF",
            STATE.bank_virtual_balance,
        )
    )
    await update.effective_message.reply_text("\n".join(linhas))


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]  # type: ignore[index]
    linhas = [
        "🔗 Links úteis",
        "",
        "Casa referência: {} ({})".format(settings.bookmaker_name, settings.bookmaker_url),
        "",
        "API-Football: https://www.api-football.com/",
    ]
    await update.effective_message.reply_text("\n".join(linhas))


async def do_scan_once(app: Application, origin: str) -> None:
    settings: Settings = app.bot_data["settings"]  # type: ignore[index]
    api: ApiFootballClient = app.bot_data["api_client"]  # type: ignore[index]

    candidates = await find_candidates(settings, api)
    STATE.last_candidates = {c.fixture_id: c for c in candidates}

    if not candidates:
        summary = "[EvRadar PRO] Scan concluído (origem={}). Nenhum sinal na janela.".format(origin)
        STATE.last_scan_summary = summary
        # Só mandar mensagem em autoscan se houver sinal, pra evitar spam
        if origin == "manual":
            chat_id = STATE.chat_id_bound or settings.chat_id_default
            if chat_id:
                await app.bot.send_message(chat_id=chat_id, text=summary)
        return

    summary_lines: List[str] = []
    summary_lines.append(
        "[EvRadar PRO] Scan concluído (origem={}). Eventos com valor: {}".format(
            origin, len(candidates)
        )
    )
    for c in candidates[:5]:
        summary_lines.append(
            "- {} vs {} ({}', EV {:.2f}%, odd {:.2f})".format(
                c.home_team, c.away_team, c.minute, c.ev_pct, c.used_odd
            )
        )
    STATE.last_scan_summary = "\n".join(summary_lines)

    chat_id = STATE.chat_id_bound or settings.chat_id_default
    if not chat_id:
        return

    # Manda mensagem resumo + detalhes de cada candidato
    await app.bot.send_message(chat_id=chat_id, text=STATE.last_scan_summary)
    for c in candidates:
        msg = format_candidate_message(c, settings)
        await app.bot.send_message(chat_id=chat_id, text=msg)


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]  # type: ignore[index]
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id:
        STATE.chat_id_bound = chat_id
    await update.effective_message.reply_text(
        "🔍 Iniciando varredura manual de jogos ao vivo (EvRadar PRO)..."
    )
    await do_scan_once(context.application, origin="manual")
    # No final, o resumo já foi atualizado e (se houver sinais) enviado.


async def cmd_entrei(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /entrei <fixture_id> <odd>
    Exemplo: /entrei 1451092 1.85
    Registra entrada na banca virtual.
    """
    settings: Settings = context.application.bot_data["settings"]  # type: ignore[index]
    if not settings.bank_virtual_enabled:
        await update.effective_message.reply_text(
            "A banca virtual está desligada. Ative com BANK_VIRTUAL_ENABLED=1 nas variáveis de ambiente."
        )
        return

    args = context.args
    if len(args) < 2:
        await update.effective_message.reply_text(
            "Uso: /entrei <fixture_id> <odd>\nExemplo: /entrei 1451092 1.85"
        )
        return

    try:
        fixture_id = int(args[0])
        odd = float(args[1].replace(",", "."))
    except Exception:
        await update.effective_message.reply_text(
            "Não entendi os argumentos. Exemplo correto: /entrei 1451092 1.85"
        )
        return

    cand = STATE.last_candidates.get(fixture_id)
    if not cand:
        await update.effective_message.reply_text(
            "Não achei esse jogo no último scan. Tente usar /scan antes ou confira o ID."
        )
        return

    stake_pct = compute_stake_pct(settings, cand.prob_goal, odd)
    if stake_pct <= 0 or STATE.bank_virtual_balance <= 0:
        await update.effective_message.reply_text(
            "Esse jogo não gera stake positiva na banca virtual (edge ≤ 0). Entrada não registrada."
        )
        return

    stake_value = STATE.bank_virtual_balance * stake_pct * settings.bank_leverage
    vb = VirtualBet(
        fixture_id=fixture_id,
        desc="{} vs {} ({}')".format(cand.home_team, cand.away_team, cand.minute),
        stake_pct=stake_pct * 100.0,
        stake_value=stake_value,
        odd=odd,
    )
    STATE.virtual_bets.append(vb)

    await update.effective_message.reply_text(
        "Entrada registrada na banca virtual:\n"
        "🏟️ {desc}\n"
        "⚙️ Over (soma+0,5) @ {odd:.2f}\n"
        "📌 Stake: {pct:.2f}% ≈ R${val:.2f}\n\n"
        "Obs: por enquanto o resultado (green/red) ainda é manual, mas já guardamos stake e odd.\n"
        "Depois podemos evoluir para liquidar automaticamente pelo placar final.".format(
            desc=vb.desc,
            odd=vb.odd,
            pct=vb.stake_pct,
            val=vb.stake_value,
        )
    )


# =========================
#  Autoscan + HTTP dummy
# =========================

async def autoscan_loop(app: Application) -> None:
    settings: Settings = app.bot_data["settings"]  # type: ignore[index]
    logger.info("Autoscan iniciado (intervalo=%ss)", settings.check_interval)
    while True:
        try:
            await do_scan_once(app, origin="auto")
        except Exception as exc:
            logger.error("Erro no autoscan: %s", exc)
        await asyncio.sleep(settings.check_interval)


async def handle_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        data = await reader.read(1024)
        _ = data
        resp = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Content-Length: 16\r\n"
            "\r\n"
            "EvRadar online\n"
        )
        writer.write(resp.encode("utf-8"))
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def start_dummy_http_server() -> None:
    port_raw = os.getenv("PORT", "10000")
    try:
        port = int(port_raw)
    except Exception:
        port = 10000
    server = await asyncio.start_server(handle_http, host="0.0.0.0", port=port)
    addrs = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    logger.info("Servidor HTTP dummy ouvindo em %s", addrs)
    async with server:
        await server.serve_forever()


async def on_startup(app: Application) -> None:
    settings: Settings = app.bot_data["settings"]  # type: ignore[index]
    if settings.autostart and STATE.autoscan_task is None:
        STATE.autoscan_task = asyncio.create_task(autoscan_loop(app))
    # servidor HTTP dummy para o Render enxergar porta
    asyncio.create_task(start_dummy_http_server())


async def on_shutdown(app: Application) -> None:
    logger.info("Encerrando EvRadar PRO...")
    if STATE.autoscan_task is not None:
        STATE.autoscan_task.cancel()
        try:
            await STATE.autoscan_task
        except asyncio.CancelledError:
            pass
        STATE.autoscan_task = None
    api: ApiFootballClient = app.bot_data.get("api_client")  # type: ignore[assignment]
    if api:
        await api.close()


# =========================
#  main()
# =========================

def main() -> None:
    settings = load_settings()

    app = (
        Application.builder()
        .token(settings.bot_token)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    api_client = ApiFootballClient(settings.api_key)

    app.bot_data["settings"] = settings
    app.bot_data["api_client"] = api_client

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("links", cmd_links))
    app.add_handler(CommandHandler("entrei", cmd_entrei))

    # Em ambientes como Render, é importante não ter dois processos usando o mesmo bot.
    # Certifique-se de NÃO rodar o mesmo token em outro lugar ao mesmo tempo.
    logger.info("Iniciando bot do EvRadar PRO (Notebook/Nuvem)...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
