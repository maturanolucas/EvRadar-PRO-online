#!/usr/bin/env python3
"""
EvRadar PRO — Versão Notebook/Nuvem (Telegram + API-Football, torneira mais aberta)

Requisitos (instalar uma vez no seu Python):
    pip install python-telegram-bot==21.6 httpx python-dotenv

Principais variáveis de ambiente:
    API_FOOTBALL_KEY   -> sua chave da API-Football (obrigatória)
    TELEGRAM_BOT_TOKEN -> token do BotFather (obrigatório)
    TELEGRAM_CHAT_ID   -> (opcional) chat padrão para alertas

    TARGET_ODD         -> odd de referência p/ cálculo de EV  (padrão: 1.70)
    EV_MIN_PCT         -> EV mínimo em % para mandar alerta    (padrão: 3.0)
    MIN_ODD            -> odd mínima aceitável                 (apenas display)
    MAX_ODD            -> odd máxima aceitável                 (apenas display)
    WINDOW_START       -> início da janela em minutos          (padrão: 47)
    WINDOW_END         -> fim da janela em minutos             (padrão: 82)
    AUTOSTART          -> "1" para varredura automática        (padrão: 0 - OFF)
    CHECK_INTERVAL     -> intervalo autoscan em segundos       (padrão: 45)

    LEAGUE_IDS         -> ids separados por vírgula; se vazio,
                          uso um pacote de ligas/copas relevantes.
"""

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]


# =========================
#  Logging básico
# =========================

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("EvRadarNotebook")


# =========================
#  Config padrão
# =========================

LEAGUE_IDS_DEFAULT: List[int] = [
    # Big 5 Europa
    39,   # Premier League
    140,  # La Liga
    135,  # Serie A
    78,   # Bundesliga
    61,   # Ligue 1
    # Brasil / América do Sul
    71,   # Brasileirão Série A
    72,   # Brasileirão Série B
    13,   # Argentina Liga Profesional
    14,   # Argentina Primera Nacional / alternativas
    # Copas relevantes
    2,    # Champions League
    3,    # Europa League
    4,    # Europa Conference League
    5,    # Eurocopa
    180,  # Libertadores
    203,  # Sudamericana
    # Outras ligas boas
    62,   # Ligue 2
    88,   # Eredivisie
    89,   # Jupiler Pro League
    94,   # Primeira Liga (POR)
    128,  # Superliga (TUR)
    136,  # Super League (GRE)
    141,  # Süper Lig (TUR?) / ajuste
    144,  # Championship (ING)
    79,   # 2. Bundesliga
    253,  # MLS
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

    def describe(self) -> str:
        lines: List[str] = []
        lines.append("⚙️ Configuração atual do EvRadar PRO (Notebook):")
        lines.append("")
        linhas_janela = "• Janela: {}–{}'".format(self.window_start, self.window_end)
        lines.append(linhas_janela)
        linhas_odds = "• Odds alvo: {:.2f} (min: {:.2f} | max: {:.2f})".format(
            self.target_odd, self.min_odd, self.max_odd
        )
        lines.append(linhas_odds)
        linha_ev = "• EV mínimo p/ alerta: {:.2f}%".format(self.ev_min_pct)
        lines.append(linha_ev)
        linha_leagues = "• Competições: {} ids configurados (foco em ligas/copas relevantes)".format(
            len(self.league_ids)
        )
        lines.append(linha_leagues)
        if self.autostart:
            linha_auto = "• Autoscan: ON a cada {}s".format(self.check_interval)
        else:
            linha_auto = "• Autoscan: OFF (use /scan manual)"
        lines.append(linha_auto)
        linha_book = "• Casa referência: {} ({})".format(
            self.bookmaker_name, self.bookmaker_url
        )
        lines.append(linha_book)
        return "\n".join(lines)


@dataclass
class GlobalState:
    last_scan_summary: str = "Ainda não houve varredura."
    last_scan_time: Optional[float] = None
    last_alerts: int = 0
    autoscan_task: Optional[asyncio.Task] = None
    chat_id_bound: Optional[int] = None


STATE = GlobalState()


# =========================
#  Helpers de ambiente
# =========================

def _load_env() -> None:
    if load_dotenv is not None:
        try:
            load_dotenv()
        except Exception:
            logger.debug("Não foi possível carregar .env, seguindo com variáveis atuais.")


def _get_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if not val:
        return default
    try:
        return float(val.replace(",", "."))
    except Exception:
        logger.warning("Valor inválido para %s=%r, usando padrão %.2f", name, val, default)
        return default


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if not val:
        return default
    try:
        return int(val)
    except Exception:
        logger.warning("Valor inválido para %s=%r, usando padrão %d", name, val, default)
        return default


def _parse_league_ids(raw: Optional[str]) -> List[int]:
    if not raw:
        return LEAGUE_IDS_DEFAULT[:]
    result: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except Exception:
            logger.warning("Ignorando LEAGUE_ID inválido: %r", part)
    if not result:
        return LEAGUE_IDS_DEFAULT[:]
    return result


def load_settings() -> Settings:
    _load_env()

    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id_raw = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    chat_id_default: Optional[int]
    if chat_id_raw:
        try:
            chat_id_default = int(chat_id_raw)
        except Exception:
            logger.warning("TELEGRAM_CHAT_ID inválido: %r (ignorando)", chat_id_raw)
            chat_id_default = None
    else:
        chat_id_default = None

    if not api_key:
        logger.error("API_FOOTBALL_KEY não configurada. Defina a variável de ambiente.")
        print("ERRO: API_FOOTBALL_KEY não configurada. Configure no sistema ou .env.")
        sys.exit(1)
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN não configurado. Defina a variável de ambiente.")
        print("ERRO: TELEGRAM_BOT_TOKEN não configurado. Configure no sistema ou .env.")
        sys.exit(1)

    target_odd = _get_float("TARGET_ODD", 1.70)
    ev_min_pct = _get_float("EV_MIN_PCT", 3.0)
    min_odd = _get_float("MIN_ODD", 1.47)
    max_odd = _get_float("MAX_ODD", 3.50)
    window_start = _get_int("WINDOW_START", 47)
    window_end = _get_int("WINDOW_END", 82)
    autostart_flag = os.getenv("AUTOSTART", "0").strip()
    autostart = autostart_flag == "1"
    check_interval = _get_int("CHECK_INTERVAL", 45)
    leagues_raw = os.getenv("LEAGUE_IDS")
    league_ids = _parse_league_ids(leagues_raw)

    bookmaker_name = os.getenv("BOOKMAKER_NAME", "Superbet").strip() or "Superbet"
    bookmaker_url = os.getenv("BOOKMAKER_URL", "https://www.superbet.com/").strip() or "https://www.superbet.com/"

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
    )
    return settings


# =========================
#  Cliente API-Football
# =========================

API_BASE = "https://v3.football.api-sports.io"


class ApiFootballClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"x-apisports-key": self.api_key}
            self._client = httpx.AsyncClient(
                base_url=API_BASE,
                headers=headers,
                timeout=httpx.Timeout(10.0, connect=5.0),
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
            logger.error("Erro ao chamar fixtures live: %s", exc)
            return []
        if resp.status_code != 200:
            logger.error("HTTP %s em fixtures live: %s", resp.status_code, resp.text[:500])
            return []
        data = resp.json()
        return data.get("response", []) or []

    async def fetch_statistics(self, fixture_id: int) -> Dict[str, Dict[str, Any]]:
        client = await self._get_client()
        try:
            resp = await client.get("/fixtures/statistics", params={"fixture": fixture_id})
        except Exception as exc:
            logger.error("Erro em fixtures/statistics para %s: %s", fixture_id, exc)
            return {}
        if resp.status_code != 200:
            logger.error(
                "HTTP %s em fixtures/statistics(%s): %s",
                resp.status_code,
                fixture_id,
                resp.text[:300],
            )
            return {}
        data = resp.json()
        items = data.get("response", []) or []
        if not items:
            return {}
        out: Dict[str, Dict[str, Any]] = {"home": {}, "away": {}}
        for item in items:
            stats_list = item.get("statistics") or []
            stats_map: Dict[str, Any] = {}
            for s in stats_list:
                s_type = s.get("type") or ""
                s_val = s.get("value")
                stats_map[s_type] = s_val
            if not out["home"]:
                out["home"] = stats_map
            else:
                out["away"] = stats_map
        return out


# =========================
#  Modelo de probabilidade
# =========================

def _parse_percent(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.endswith("%"):
        text = text[:-1].strip()
    try:
        return float(text.replace(",", "."))
    except Exception:
        return 0.0


def _get_int_stat(stats: Dict[str, Any], key: str) -> int:
    val = stats.get(key)
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    try:
        text = str(val).strip()
        if text.endswith("%"):
            text = text[:-1]
        return int(float(text))
    except Exception:
        return 0


def compute_pressure_score(stats_home: Dict[str, Any], stats_away: Dict[str, Any]) -> Tuple[float, str]:
    shots_on_target = _get_int_stat(stats_home, "Shots on Target") + _get_int_stat(
        stats_away, "Shots on Target"
    )
    shots_total = _get_int_stat(stats_home, "Total Shots") + _get_int_stat(
        stats_away, "Total Shots"
    )
    attacks = _get_int_stat(stats_home, "Attacks") + _get_int_stat(
        stats_away, "Attacks"
    )
    dang_attacks = _get_int_stat(stats_home, "Dangerous Attacks") + _get_int_stat(
        stats_away, "Dangerous Attacks"
    )
    poss_home = _parse_percent(stats_home.get("Ball Possession"))
    poss_away = _parse_percent(stats_away.get("Ball Possession"))
    poss_diff = abs(poss_home - poss_away)

    volume = shots_total + shots_on_target * 1.5
    ataque_peso = attacks * 0.2 + dang_attacks * 0.6
    pressure_raw = volume * 0.7 + ataque_peso * 0.3
    pressure_raw += poss_diff * 0.3

    pressure = float(min(100.0, max(0.0, pressure_raw / 2.0)))

    if pressure < 25:
        desc = "Jogo morno, pouca pressão."
    elif pressure < 50:
        desc = "Pressão moderada, jogo ok."
    elif pressure < 75:
        desc = "Boa pressão, jogo quente."
    else:
        desc = "Pressão alta, clima de gol."

    return pressure, desc


def estimate_goal_probability(
    minute: int,
    goals_home: int,
    goals_away: int,
    stats_home: Dict[str, Any],
    stats_away: Dict[str, Any],
    window_start: int,
    window_end: int,
) -> Tuple[float, float, str]:
    total_goals = goals_home + goals_away

    minute_clamped = minute
    if minute_clamped < window_start:
        minute_clamped = window_start
    if minute_clamped > window_end:
        minute_clamped = window_end
    span = max(1, window_end - window_start)
    t_norm = (minute_clamped - window_start) / float(span)
    p_time = 0.65 - 0.40 * t_norm

    pressure, pressure_desc = compute_pressure_score(stats_home, stats_away)
    p_pressure = (pressure / 100.0) * 0.20
    p_goals = min(0.12, 0.04 * float(total_goals))

    p = p_time + p_pressure + p_goals
    if p < 0.05:
        p = 0.05
    if p > 0.90:
        p = 0.90

    fair_odd = 1.0 / p
    return p, fair_odd, pressure_desc


# =========================
#  Lógica de scan
# =========================

@dataclass
class Candidate:
    fixture_id: int
    league_name: str
    country: str
    home_team: str
    away_team: str
    minute: int
    goals_home: int
    goals_away: int
    prob_goal: float
    fair_odd: float
    ev_pct: float
    tier: str
    pressure_desc: str


async def find_candidates(
    api: ApiFootballClient,
    settings: Settings,
) -> Tuple[List[Candidate], int]:
    fixtures = await api.fetch_live_fixtures()
    total_live = len(fixtures)
    candidates: List[Candidate] = []

    for item in fixtures:
        fixture = item.get("fixture") or {}
        league = item.get("league") or {}
        teams = item.get("teams") or {}
        goals = item.get("goals") or {}

        league_id = league.get("id")
        if isinstance(league_id, str):
            try:
                league_id = int(league_id)
            except Exception:
                league_id = None

        if league_id not in settings.league_ids:
            continue

        league_name = str(league.get("name") or "")
        round_name = str(league.get("round") or "")
        lower_name = league_name.lower()
        lower_round = round_name.lower()
        if "friendly" in lower_name or "friendly" in lower_round:
            continue

        status = fixture.get("status") or {}
        status_short = str(status.get("short") or "")
        minute = status.get("elapsed")
        if minute is None:
            minute = 0
        try:
            minute_int = int(minute)
        except Exception:
            minute_int = 0

        if minute_int < settings.window_start or minute_int > settings.window_end:
            continue

        if status_short not in {"1H", "2H", "HT"}:
            continue

        goals_home = goals.get("home") or 0
        goals_away = goals.get("away") or 0
        try:
            goals_home = int(goals_home)
        except Exception:
            goals_home = 0
        try:
            goals_away = int(goals_away)
        except Exception:
            goals_away = 0

        home = teams.get("home") or {}
        away = teams.get("away") or {}
        home_name = str(home.get("name") or "?")
        away_name = str(away.get("name") or "?")

        fixture_id = fixture.get("id")
        if fixture_id is None:
            continue
        try:
            fixture_id_int = int(fixture_id)
        except Exception:
            continue

        stats = await api.fetch_statistics(fixture_id_int)
        stats_home = stats.get("home") or {}
        stats_away = stats.get("away") or {}

        prob_goal, fair_odd, pressure_desc = estimate_goal_probability(
            minute_int,
            goals_home,
            goals_away,
            stats_home,
            stats_away,
            settings.window_start,
            settings.window_end,
        )

        ev = prob_goal * settings.target_odd - 1.0
        ev_pct = ev * 100.0

        if ev_pct < settings.ev_min_pct:
            continue

        if ev_pct >= 7.0:
            tier = "A"
        elif ev_pct >= 4.0:
            tier = "B"
        else:
            tier = "C"

        candidate = Candidate(
            fixture_id=fixture_id_int,
            league_name=league_name,
            country=str(league.get("country") or ""),
            home_team=home_name,
            away_team=away_name,
            minute=minute_int,
            goals_home=goals_home,
            goals_away=goals_away,
            prob_goal=prob_goal,
            fair_odd=fair_odd,
            ev_pct=ev_pct,
            tier=tier,
            pressure_desc=pressure_desc,
        )
        candidates.append(candidate)

    candidates.sort(key=lambda c: c.ev_pct, reverse=True)
    return candidates, total_live


def format_candidate_message(c: Candidate, settings: Settings) -> str:
    jogo = "{} vs {}".format(c.home_team, c.away_team)
    placar = "{}–{}".format(c.goals_home, c.goals_away)
    prob_pct = c.prob_goal * 100.0
    ev_str = "{:.2f}%".format(c.ev_pct)
    prob_str = "{:.1f}%".format(prob_pct)
    fair_odd_str = "{:.2f}".format(c.fair_odd)
    linha = "Over (soma + 0,5) @ {:.2f}".format(settings.target_odd)

    if c.tier == "A":
        tier_title = "Tier A — Sinal forte"
    elif c.tier == "B":
        tier_title = "Tier B — Sinal interessante"
    else:
        tier_title = "Tier C — Sinal mais leve"

    ev_label = "EV+ ✅" if c.ev_pct >= 0 else "EV- ❌"

    linhas: List[str] = []
    linhas.append("🔔 {} ({})".format(tier_title, c.league_name))
    linhas.append("")
    linhas.append("🏟️ {}".format(jogo))
    linhas.append("⏱️ {}' | 🔢 Placar {}".format(c.minute, placar))
    linhas.append("⚙️ Linha: {}".format(linha))
    linhas.append("📊 Probabilidade: {} | Odd justa: {}".format(prob_str, fair_odd_str))
    linhas.append("💰 EV: {} → {}".format(ev_str, ev_label))
    linhas.append("")
    linhas.append("🧩 Interpretação:")
    linhas.append(c.pressure_desc)
    return "\n".join(linhas)


def format_scan_summary(
    origin: str,
    total_live: int,
    games_window: int,
    alerts_sent: int,
) -> str:
    linhas: List[str] = []
    header = "[EvRadar PRO] Scan concluído (origem={}).".format(origin)
    linhas.append(header)
    resumo = "Eventos ao vivo: {} | Jogos analisados na janela: {} | Alertas enviados: {}".format(
        total_live, games_window, alerts_sent
    )
    linhas.append(resumo)
    return "\n".join(linhas)


# =========================
#  Handlers do Telegram
# =========================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]  # type: ignore[index]
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is not None:
        STATE.chat_id_bound = chat_id
    text = settings.describe()
    await update.effective_message.reply_text(
        "\n".join(
            [
                "👋 EvRadar PRO online (Notebook/Nuvem).",
                "",
                text,
                "",
                "Comandos:",
                "  /scan   → varrer jogos ao vivo agora",
                "  /status → ver resumo da última varredura",
                "  /debug  → ver detalhes técnicos",
                "  /links  → links úteis / bookmaker",
            ]
        )
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]  # type: ignore[index]
    api: ApiFootballClient = context.application.bot_data["api"]  # type: ignore[index]
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is not None:
        STATE.chat_id_bound = chat_id

    await update.effective_message.reply_text(
        "🔍 Iniciando varredura manual de jogos ao vivo (EvRadar PRO)..."
    )

    candidates, total_live = await find_candidates(api, settings)
    games_window = len(candidates)
    alerts_sent = 0

    for cand in candidates:
        msg = format_candidate_message(cand, settings)
        await context.bot.send_message(
            chat_id=STATE.chat_id_bound or chat_id or settings.chat_id_default,
            text=msg,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        alerts_sent += 1
        await asyncio.sleep(0.8)

    summary = format_scan_summary("manual", total_live, games_window, alerts_sent)
    STATE.last_scan_summary = summary
    STATE.last_scan_time = asyncio.get_event_loop().time()
    STATE.last_alerts = alerts_sent

    await update.effective_message.reply_text(summary)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]  # type: ignore[index]
    if STATE.last_scan_time is None:
        ago_text = "nenhuma varredura ainda."
    else:
        delta = asyncio.get_event_loop().time() - STATE.last_scan_time
        mins = int(delta // 60)
        secs = int(delta % 60)
        ago_text = "há {}m{}s".format(mins, secs)
    linhas: List[str] = []
    linhas.append("📈 Status do EvRadar PRO (Notebook/Nuvem)")
    linhas.append("")
    linhas.append(STATE.last_scan_summary)
    linhas.append("")
    linhas.append("Última varredura: {}".format(ago_text))
    linhas.append("")
    linhas.append("Config atual:")
    linhas.append(settings.describe())
    await update.effective_message.reply_text("\n".join(linhas))


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]  # type: ignore[index]
    linhas: List[str] = []
    linhas.append("🐞 Debug EvRadar PRO")
    linhas.append("")
    linhas.append("API base: {}".format(API_BASE))
    linhas.append("Ligas ativas ({}): {}".format(len(settings.league_ids), settings.league_ids))
    linhas.append("Autoscan: {} (CHECK_INTERVAL={}s)".format(
        "ON" if settings.autostart else "OFF",
        settings.check_interval,
    ))
    if STATE.chat_id_bound:
        linhas.append("chat_id_bound atual: {}".format(STATE.chat_id_bound))
    if settings.chat_id_default:
        linhas.append("TELEGRAM_CHAT_ID default: {}".format(settings.chat_id_default))
    await update.effective_message.reply_text("\n".join(linhas))


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]  # type: ignore[index]
    linhas: List[str] = []
    linhas.append("🔗 Links úteis")
    linhas.append("")
    linhas.append("• Casa referência: {} ({})".format(settings.bookmaker_name, settings.bookmaker_url))
    linhas.append("• Abrir mercado (Superbet): {}".format(settings.bookmaker_url))
    await update.effective_message.reply_text("\n".join(linhas))


# =========================
#  Autoscan (sem spam)
# =========================

async def autoscan_loop(app: Application) -> None:
    settings: Settings = app.bot_data["settings"]  # type: ignore[index]
    api: ApiFootballClient = app.bot_data["api"]  # type: ignore[index]
    chat_id = settings.chat_id_default
    logger.info("Autoscan iniciado (intervalo=%ss) - chat_id_default=%s", settings.check_interval, chat_id)

    while True:
        try:
            candidates, total_live = await find_candidates(api, settings)
            games_window = len(candidates)
            alerts_sent = 0

            # só manda mensagem se houver candidatos
            if candidates and chat_id is not None:
                for cand in candidates:
                    msg = format_candidate_message(cand, settings)
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=msg,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                    alerts_sent += 1
                    await asyncio.sleep(0.8)

            # atualiza status interno sempre (para /status)
            summary = format_scan_summary("auto", total_live, games_window, alerts_sent)
            STATE.last_scan_summary = summary
            STATE.last_scan_time = asyncio.get_event_loop().time()
            STATE.last_alerts = alerts_sent

            # só manda o resumo se teve alerta (evita spam)
            if chat_id is not None and alerts_sent > 0:
                await app.bot.send_message(chat_id=chat_id, text=summary)

        except Exception as exc:
            logger.error("Erro no autoscan: %s", exc)

        await asyncio.sleep(settings.check_interval)


async def on_startup(app: Application) -> None:
    settings: Settings = app.bot_data["settings"]  # type: ignore[index]
    if settings.autostart and STATE.autoscan_task is None:
        STATE.autoscan_task = asyncio.create_task(autoscan_loop(app))


async def on_shutdown(app: Application) -> None:
    api: ApiFootballClient = app.bot_data.get("api")  # type: ignore[assignment]
    if isinstance(api, ApiFootballClient):
        await api.close()
    if STATE.autoscan_task is not None:
        STATE.autoscan_task.cancel()
        try:
            await STATE.autoscan_task
        except Exception:
            pass


# =========================
#  Main
# =========================

def main() -> None:
    settings = load_settings()
    api_client = ApiFootballClient(settings.api_key)

    application = ApplicationBuilder().token(settings.bot_token).build()
    application.bot_data["settings"] = settings
    application.bot_data["api"] = api_client

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("debug", cmd_debug))
    application.add_handler(CommandHandler("links", cmd_links))

    application.post_init = on_startup  # type: ignore[assignment]
    application.post_shutdown = on_shutdown  # type: ignore[assignment]

    logger.info("Iniciando bot do EvRadar PRO (Notebook/Nuvem)...")
    application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
