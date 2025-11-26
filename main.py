#!/usr/bin/env python3
"""
EvRadar PRO — Versão Notebook/Nuvem (Telegram + API-Football, Render-friendly, odds reais)

- Funciona local (notebook) e em nuvem (Render Web Service free).
- Usa polling do Telegram (sem webhook).
- Abre um servidor HTTP mínimo só para o Render enxergar uma porta aberta.
- Autoscan sem spam: só manda mensagem automática SE houver alerta.
- Calcula EV usando, quando possível, a ODD REAL AO VIVO da casa (API-Football /odds/live),
  comparando com a odd justa do modelo.

Requisitos (instalar uma vez no seu Python local):
    pip install python-telegram-bot==21.6 httpx python-dotenv

Principais variáveis de ambiente:
    API_FOOTBALL_KEY      -> sua chave da API-Football (obrigatória)
    TELEGRAM_BOT_TOKEN    -> token do BotFather (obrigatório)
    TELEGRAM_CHAT_ID      -> (opcional) chat padrão para alertas

    TARGET_ODD            -> odd de referência p/ EV quando não houver odd da casa (padrão: 1.70)
    EV_MIN_PCT            -> EV mínimo em % para mandar alerta              (padrão: 3.0)
    MIN_ODD               -> odd mínima aceitável (apenas display)          (padrão: 1.47)
    MAX_ODD               -> odd máxima aceitável (apenas display)          (padrão: 3.50)
    WINDOW_START          -> início da janela em minutos                    (padrão: 47)
    WINDOW_END            -> fim da janela em minutos                       (padrão: 82)
    AUTOSTART             -> "1" para varredura automática                  (padrão: 0 - OFF)
    CHECK_INTERVAL        -> intervalo autoscan em segundos                 (padrão: 60)

    LEAGUE_IDS            -> ids separados por vírgula; se vazio,
                             usa pacote default de ligas/copas relevantes.

    USE_API_FOOTBALL_ODDS -> "1" para usar odds ao vivo via /odds/live      (padrão: 1)
    BOOKMAKER_ID          -> id numérico do bookmaker na API-Football       (opcional, ex: 34 Superbet)
    BOOKMAKER_NAME        -> nome da casa (padrão: Superbet)
    BOOKMAKER_URL         -> URL da casa (padrão: https://www.superbet.com/)
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# =========================
#  Logging
# =========================

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# =========================
#  Config / Settings
# =========================


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
        linhas.append(
            "• Casa referência: {} ({})".format(
                self.bookmaker_name, self.bookmaker_url
            )
        )
        return "\n".join(linhas)


def parse_int_env(name: str, default: int) -> int:
    val = os.getenv(name)
    if not val:
        return default
    try:
        return int(val.strip())
    except Exception:
        logger.warning("Variável %s inválida: %r — usando default %s", name, val, default)
        return default


def parse_float_env(name: str, default: float) -> float:
    val = os.getenv(name)
    if not val:
        return default
    try:
        return float(val.replace(",", ".").strip())
    except Exception:
        logger.warning("Variável %s inválida: %r — usando default %s", name, val, default)
        return default


def parse_league_ids_env(name: str, default_ids: List[int]) -> List[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default_ids
    out: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except Exception:
            logger.warning("LEAGUE_IDS: valor inválido %r — ignorando", part)
    return out or default_ids


def load_settings() -> Settings:
    load_dotenv()

    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not api_key:
        raise RuntimeError("API_FOOTBALL_KEY não configurado")
    if not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN não configurado")

    chat_id_default_raw = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    chat_id_default: Optional[int]
    if chat_id_default_raw:
        try:
            chat_id_default = int(chat_id_default_raw)
        except Exception:
            logger.warning("TELEGRAM_CHAT_ID inválido: %r", chat_id_default_raw)
            chat_id_default = None
    else:
        chat_id_default = None

    target_odd = parse_float_env("TARGET_ODD", 1.70)
    ev_min_pct = parse_float_env("EV_MIN_PCT", 3.0)
    min_odd = parse_float_env("MIN_ODD", 1.47)
    max_odd = parse_float_env("MAX_ODD", 3.50)
    window_start = parse_int_env("WINDOW_START", 47)
    window_end = parse_int_env("WINDOW_END", 82)
    autostart_flag = os.getenv("AUTOSTART", "0").strip()
    autostart = autostart_flag == "1"
    check_interval = parse_int_env("CHECK_INTERVAL", 60)

    default_leagues = [
        39,   # Premier League
        140,  # La Liga
        135,  # Serie A
        78,   # Bundesliga
        61,   # Ligue 1
        71,   # Série A Brasil
        72,   # Série B Brasil
        13,   # Champions League
        14,   # Europa League
        2, 3, 4, 5,        # top divisões extras
        180, 203,          # Libertadores / Sul-Americana (exemplo)
        62, 88, 89, 94,    # França 2, etc
        128, 136, 141, 144, 79, 253,
    ]
    league_ids = parse_league_ids_env("LEAGUE_IDS", default_leagues)

    bookmaker_name = os.getenv("BOOKMAKER_NAME", "Superbet").strip() or "Superbet"
    bookmaker_url = (
        os.getenv("BOOKMAKER_URL", "https://www.superbet.com/").strip()
        or "https://www.superbet.com/"
    )

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

    return Settings(
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
    )


# =========================
#  API-Football client
# =========================

API_BASE = "https://v3.football.api-sports.io"
OVER_UNDER_BET_ID = 36  # mercado Over/Under para odds/live (API-Football)


class ApiFootballClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"x-apisports-key": self.api_key}
            self._client = httpx.AsyncClient(
                base_url=API_BASE,
                headers=headers,
                timeout=httpx.Timeout(10.0, read=10.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch_live_fixtures(self) -> List[Dict[str, Any]]:
        client = await self._get_client()
        try:
            resp = await client.get("/fixtures", params={"live": "all"})
        except Exception as exc:
            logger.error("Erro em fixtures live: %s", exc)
            return []
        if resp.status_code != 200:
            logger.error(
                "HTTP %s em fixtures live: %s",
                resp.status_code,
                resp.text[:300],
            )
            return []
        data = resp.json()
        return data.get("response", []) or []

    async def fetch_statistics(self, fixture_id: int) -> Dict[str, Dict[str, Any]]:
        client = await self._get_client()
        try:
            resp = await client.get("/fixtures/statistics", params={"fixture": fixture_id})
        except Exception as exc:
            logger.error("Erro em statistics(%s): %s", fixture_id, exc)
            return {}
        if resp.status_code != 200:
            logger.error(
                "HTTP %s em statistics(%s): %s",
                resp.status_code,
                fixture_id,
                resp.text[:300],
            )
            return {}
        data = resp.json()
        items = data.get("response", []) or []
        out: Dict[str, Dict[str, Any]] = {}
        for item in items:
            team = item.get("team", {})
            team_id = team.get("id")
            stats_list = item.get("statistics", []) or []
            stats_map: Dict[str, Any] = {}
            for entry in stats_list:
                type_name = entry.get("type")
                val = entry.get("value")
                stats_map[type_name] = val
            if team_id is not None:
                out["home" if len(out) == 0 else "away"] = stats_map
        return out

    async def fetch_live_over_under_odd(
        self,
        fixture_id: int,
        total_goals: int,
        bookmaker_id: Optional[int],
    ) -> Optional[float]:
        """
        Busca a ODD ao vivo para o mercado Over (soma + 0,5) via /odds/live.

        Estratégia:
        - Chama /odds/live?fixture={id}&bet=36
        - Se bookmaker_id for informado, tenta usar esse bookmaker.
        - Caso contrário, usa o primeiro bookmaker da resposta.
        - Procura pelo valor "Over X.5" onde X = total_goals.
        """
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
#  Modelo de probabilidade
# =========================


@dataclass
class MatchStats:
    minute: int
    goals_home: int
    goals_away: int
    pressure_home: float
    pressure_away: float
    context_score: float


def extract_int(stat_map: Dict[str, Any], key: str) -> int:
    val = stat_map.get(key)
    if val is None:
        return 0
    if isinstance(val, int):
        return val
    try:
        return int(str(val))
    except Exception:
        return 0


def compute_pressure_for_team(stats: Dict[str, Any]) -> float:
    """
    Calcula um score de pressão 0–10 combinando finalizações, no alvo e ataques perigosos.
    """
    shots = extract_int(stats, "Total Shots")
    on_target = extract_int(stats, "Shots on Goal")
    attacks = extract_int(stats, "Attacks")
    dang_attacks = extract_int(stats, "Dangerous Attacks")

    base = shots * 0.6 + on_target * 1.2 + dang_attacks * 0.35 + attacks * 0.05
    score = base ** 0.5 if base > 0 else 0.0
    if score > 10.0:
        score = 10.0
    return score


def build_match_stats(
    minute: int,
    goals_home: int,
    goals_away: int,
    stats_home: Dict[str, Any],
    stats_away: Dict[str, Any],
) -> MatchStats:
    ph = compute_pressure_for_team(stats_home)
    pa = compute_pressure_for_team(stats_away)
    goal_diff = abs(goals_home - goals_away)
    if goal_diff == 0:
        context = 0.5
    elif goal_diff == 1:
        context = 0.3
    else:
        context = 0.0
    return MatchStats(
        minute=minute,
        goals_home=goals_home,
        goals_away=goals_away,
        pressure_home=ph,
        pressure_away=pa,
        context_score=context,
    )


def estimate_goal_probability(ms: MatchStats) -> Tuple[float, str]:
    """
    Estima probabilidade de 1 gol a mais até o fim (bem simplificado).
    Retorna (prob, descrição_pressão).
    """
    minute = ms.minute
    base_time_left = max(0, 95 - minute)
    time_factor = base_time_left / 48.0  # 47–95 ~ 48 min
    if time_factor > 1.0:
        time_factor = 1.0

    pressure_avg = (ms.pressure_home + ms.pressure_away) / 2.0
    pressure_norm = pressure_avg / 10.0

    if pressure_avg >= 7.5:
        pressure_desc = "Pressão insana, jogo elétrico."
    elif pressure_avg >= 5.0:
        pressure_desc = "Boa pressão ofensiva, jogo vivo."
    elif pressure_avg >= 3.0:
        pressure_desc = "Ritmo ok, mas sem tanta pressão."
    else:
        pressure_desc = "Jogo morno / travado."

    p = 0.25 + 0.45 * pressure_norm + 0.25 * time_factor + 0.05 * ms.context_score
    if p < 0.05:
        p = 0.05
    if p > 0.95:
        p = 0.95

    return p, pressure_desc


def tier_by_ev_and_pressure(
    prob_goal: float,
    ev_pct: float,
    minute: int,
    pressure_desc: str,
) -> str:
    if ev_pct >= 7.0 and prob_goal >= 0.60:
        return "A"
    if ev_pct >= 4.0 and prob_goal >= 0.52:
        return "B"
    return "C"


# =========================
#  Candidatos / formatação
# =========================


@dataclass
class Candidate:
    fixture_id: int
    league_name: str
    home: str
    away: str
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


def format_candidate_message(c: Candidate, settings: Settings) -> str:
    jogo = "{} vs {}".format(c.home, c.away)
    placar = "{}–{}".format(c.goals_home, c.goals_away)
    prob_pct = c.prob_goal * 100.0
    prob_str = "{:.1f}%".format(prob_pct)
    ev_str = "{:.2f}%".format(c.ev_pct)
    fair_odd_str = "{:.2f}".format(c.fair_odd)
    used_odd_str = "{:.2f}".format(c.used_odd)
    linha = "Over (soma + 0,5) @ {}".format(used_odd_str)

    if c.tier == "A":
        tier_title = "Tier A — Sinal forte"
        recomendacao = "Jogo muito vivo, vale considerar entrada com stake padrão."
    elif c.tier == "B":
        tier_title = "Tier B — Bom cenário"
        recomendacao = "Cenário interessante, mas dá pra ajustar stake com cautela."
    else:
        tier_title = "Tier C — Sinal mais leve"
        recomendacao = "Pode ser oportunidade, mas bem situacional. Ajuste stake pra baixo."

    ev_label = "EV+ ✅" if c.ev_pct >= 0 else "EV- ❌"

    if c.odd_source == "live":
        odd_info = "odd real da casa"
    elif c.odd_source == "cache":
        odd_info = "última odd conhecida (mercado pode ter suspenso)"
    else:
        odd_info = "odd alvo (fallback)"

    linhas: List[str] = []
    linhas.append("🔔 {} ({})".format(tier_title, c.league_name))
    linhas.append("")
    linhas.append("🏟️ {}".format(jogo))
    linhas.append("⏱️ {}' | 🔢 Placar {}".format(c.minute, placar))
    linhas.append("⚙️ Linha: {} [{}]".format(linha, odd_info))
    linhas.append("📊 Probabilidade: {} | Odd justa: {}".format(prob_str, fair_odd_str))
    linhas.append("💰 EV: {} → {}".format(ev_str, ev_label))
    linhas.append("")
    linhas.append("🧩 Interpretação:")
    linhas.append("{} {}".format(c.pressure_desc, recomendacao))
    linhas.append("")
    linhas.append("👉 Abrir mercado: {}".format(settings.bookmaker_url))
    return "\n".join(linhas)


# =========================
#  Estado global do bot
# =========================


@dataclass
class BotState:
    autoscan_task: Optional[asyncio.Task]
    last_scan_summary: str
    chat_id_bound: Optional[int]
    last_live_odds: Dict[int, float]


STATE = BotState(
    autoscan_task=None,
    last_scan_summary="(ainda não foi rodada nenhuma varredura)",
    chat_id_bound=None,
    last_live_odds={},
)


# =========================
#  Lógica principal do radar
# =========================


async def find_candidates(
    settings: Settings,
    api: ApiFootballClient,
) -> Tuple[List[Candidate], str]:
    fixtures = await api.fetch_live_fixtures()
    total_events = len(fixtures)
    candidates: List[Candidate] = []

    for fx in fixtures:
        league = fx.get("league", {}) or {}
        league_id = league.get("id")
        if league_id not in settings.league_ids:
            continue

        fixture = fx.get("fixture", {}) or {}
        fixture_id = fixture.get("id")
        if fixture_id is None:
            continue
        try:
            fixture_id_int = int(fixture_id)
        except Exception:
            continue

        status = fixture.get("status", {}) or {}
        minute = status.get("elapsed") or 0
        if not isinstance(minute, int):
            try:
                minute = int(minute)
            except Exception:
                minute = 0

        if minute < settings.window_start or minute > settings.window_end:
            continue

        goals = fx.get("goals", {}) or {}
        goals_home = goals.get("home") or 0
        goals_away = goals.get("away") or 0
        try:
            goals_home = int(goals_home)
            goals_away = int(goals_away)
        except Exception:
            goals_home = int(goals_home or 0)
            goals_away = int(goals_away or 0)

        try:
            stats = await api.fetch_statistics(fixture_id_int)
        except Exception as exc:
            logger.error("Erro buscando stats do fixture %s: %s", fixture_id_int, exc)
            continue

        stats_home = stats.get("home", {})
        stats_away = stats.get("away", {})
        ms = build_match_stats(
            minute=minute,
            goals_home=goals_home,
            goals_away=goals_away,
            stats_home=stats_home,
            stats_away=stats_away,
        )
        prob_goal, pressure_desc = estimate_goal_probability(ms)
        fair_odd = 1.0 / prob_goal if prob_goal > 0 else 99.99

        total_goals = goals_home + goals_away
        used_odd: Optional[float] = None
        odd_source = ""

        # ===== Odds com cache inteligente =====
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
                cached = STATE.last_live_odds.get(fixture_id_int)
                if cached is not None:
                    used_odd = cached
                    odd_source = "cache"
                else:
                    # Sem odd real (nem cache) -> não dá pra confiar no EV
                    continue
        else:
            used_odd = settings.target_odd
            odd_source = "alvo"

        if used_odd is None:
            continue

        if used_odd < settings.min_odd or used_odd > settings.max_odd:
            continue

        ev = prob_goal * used_odd - 1.0
        ev_pct = ev * 100.0

        if ev_pct < settings.ev_min_pct:
            continue

        tier = tier_by_ev_and_pressure(prob_goal, ev_pct, minute, pressure_desc)

        teams = fx.get("teams", {}) or {}
        home_team = teams.get("home", {}) or {}
        away_team = teams.get("away", {}) or {}
        home_name = home_team.get("name") or "Casa"
        away_name = away_team.get("name") or "Fora"
        league_name = league.get("name") or "Liga"

        c = Candidate(
            fixture_id=fixture_id_int,
            league_name=str(league_name),
            home=str(home_name),
            away=str(away_name),
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
        )
        candidates.append(c)

    candidates.sort(key=lambda c: c.ev_pct, reverse=True)

    resumo = "[EvRadar PRO] Scan concluído.\nEventos ao vivo: {} | Jogos analisados na janela: {} | Alertas enviados: {}".format(
        total_events, len(candidates), len(candidates)
    )
    return candidates, resumo


# =========================
#  Telegram handlers
# =========================


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]  # type: ignore[index]
    chat_id = update.effective_chat.id
    STATE.chat_id_bound = chat_id
    texto = ["👋 EvRadar PRO online (Notebook/Nuvem).", ""]
    texto.append(settings.describe())
    msg = "\n".join(texto)
    await update.message.reply_text(msg)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]  # type: ignore[index]
    linhas: List[str] = []
    linhas.append("📈 Status do EvRadar PRO (Notebook/Nuvem)")
    linhas.append("")
    linhas.append(STATE.last_scan_summary)
    linhas.append("")
    linhas.append(settings.describe())
    await update.message.reply_text("\n".join(linhas))


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]  # type: ignore[index]
    linhas: List[str] = []
    linhas.append("🐞 Debug EvRadar PRO")
    linhas.append("")
    linhas.append("API base: {}".format(API_BASE))
    linhas.append(
        "Ligas ativas ({}): {}".format(
            len(settings.league_ids),
            sorted(settings.league_ids),
        )
    )
    linhas.append(
        "Autoscan: {} (CHECK_INTERVAL={}s)".format(
            "ON" if settings.autostart else "OFF",
            settings.check_interval,
        )
    )
    if settings.use_live_odds:
        if settings.bookmaker_id is not None:
            linhas.append(
                "Odds: AO VIVO via /odds/live (bookmaker_id={})".format(
                    settings.bookmaker_id
                )
            )
        else:
            linhas.append("Odds: AO VIVO via /odds/live (primeiro bookmaker)")
    else:
        linhas.append(
            "Odds: fixas pela odd alvo {:.2f}".format(settings.target_odd)
        )

    if STATE.chat_id_bound:
        linhas.append("chat_id_bound atual: {}".format(STATE.chat_id_bound))
    if settings.chat_id_default:
        linhas.append("TELEGRAM_CHAT_ID default: {}".format(settings.chat_id_default))
    await update.message.reply_text("\n".join(linhas))


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]  # type: ignore[index]
    linhas = [
        "🔗 Links úteis",
        "",
        "• Casa referência: {} ({})".format(
            settings.bookmaker_name, settings.bookmaker_url
        ),
        "• API-Football: https://www.api-football.com/",
    ]
    await update.message.reply_text("\n".join(linhas), disable_web_page_preview=True)


async def run_scan_and_notify(
    app: Application,
    origin: str,
    chat_id: Optional[int],
) -> None:
    settings: Settings = app.bot_data["settings"]  # type: ignore[index]
    api: ApiFootballClient = app.bot_data["api_client"]  # type: ignore[index]

    candidates, resumo = await find_candidates(settings, api)
    STATE.last_scan_summary = resumo

    if origin == "manual":
        # manual sempre responde com resumo, mesmo sem alerta
        if chat_id is not None:
            await app.bot.send_message(chat_id=chat_id, text=resumo)
    else:
        # autoscan só avisa se houver pelo menos 1 alerta
        if candidates and chat_id is not None:
            await app.bot.send_message(
                chat_id=chat_id,
                text=resumo + "\n\nEnviando {} alerta(s)...".format(len(candidates)),
            )

    if not candidates or chat_id is None:
        return

    for c in candidates:
        msg = format_candidate_message(c, settings)
        await app.bot.send_message(
            chat_id=chat_id,
            text=msg,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "🔍 Iniciando varredura manual de jogos ao vivo (EvRadar PRO)..."
    )
    await run_scan_and_notify(app, origin="manual", chat_id=chat_id)


# =========================
#  Autoscan + HTTP dummy
# =========================


async def autoscan_loop(app: Application) -> None:
    settings: Settings = app.bot_data["settings"]  # type: ignore[index]
    chat_id = STATE.chat_id_bound or settings.chat_id_default
    if not chat_id:
        logger.info("Autoscan ativo, mas sem chat_id configurado.")
    while True:
        try:
            if chat_id:
                await run_scan_and_notify(app, origin="auto", chat_id=chat_id)
        except Exception as exc:
            logger.error("Erro no autoscan: %s", exc)
        await asyncio.sleep(settings.check_interval)


async def handle_dummy_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        await reader.read(1024)
        body = b"OK\n"
        headers = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
            b"\r\n"
        )
        writer.write(headers + body)
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def start_dummy_http_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    server = await asyncio.start_server(handle_dummy_http, "0.0.0.0", port)
    addr = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    logger.info("Dummy HTTP server ouvindo em %s", addr)
    async with server:
        await server.serve_forever()


async def on_startup(app: Application) -> None:
    settings: Settings = app.bot_data["settings"]  # type: ignore[index]
    if settings.autostart and STATE.autoscan_task is None:
        logger.info("Autoscan iniciado (intervalo=%ss)", settings.check_interval)
        STATE.autoscan_task = asyncio.create_task(autoscan_loop(app))
    # servidor HTTP fake para Render/WebService
    asyncio.create_task(start_dummy_http_server())


async def on_shutdown(app: Application) -> None:
    api: ApiFootballClient = app.bot_data["api_client"]  # type: ignore[index]
    await api.close()
    if STATE.autoscan_task is not None:
        STATE.autoscan_task.cancel()
        try:
            await STATE.autoscan_task
        except asyncio.CancelledError:
            pass
        STATE.autoscan_task = None


# =========================
#  main()
# =========================


def main() -> None:
    settings = load_settings()
    api_client = ApiFootballClient(settings.api_key)

    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    application.bot_data["settings"] = settings
    application.bot_data["api_client"] = api_client

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_handler(CommandHandler("debug", cmd_debug))
    application.add_handler(CommandHandler("links", cmd_links))

    logger.info("Iniciando bot do EvRadar PRO (Notebook/Nuvem)...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
