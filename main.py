import os
import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta, timezone

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# =========================
# CONFIGURAÇÃO (ENV VARS)
# =========================

@dataclass
class Config:
    telegram_token: str
    telegram_chat_id: int
    api_football_key: str

    window_start: int = 47
    window_end: int = 85

    min_odd: float = 1.47
    max_odd: float = 3.50
    ev_min: float = 4.0  # em %

    cooldown_minutes: int = 6
    check_interval: int = 30  # segundos entre scans automáticos
    autostart: bool = True

    target_odd: float = 1.70  # fallback se não achar odd ao vivo
    use_api_football_odds: bool = False
    bookmaker_id: Optional[int] = None

    allowed_league_ids: List[int] = field(default_factory=list)

    bookmaker_name: str = "Superbet"
    bookmaker_url: str = "https://www.superbet.com"

    @staticmethod
    def _env_bool(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in ("1", "true", "yes", "y", "sim")

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            return float(raw.replace(",", "."))
        except Exception:
            return default

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except Exception:
            return default

    @classmethod
    def from_env(cls) -> "Config":
        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN") or ""
        if not telegram_token:
            raise RuntimeError("Defina TELEGRAM_BOT_TOKEN no ambiente.")

        chat_raw = os.getenv("TELEGRAM_CHAT_ID") or ""
        if not chat_raw:
            raise RuntimeError("Defina TELEGRAM_CHAT_ID no ambiente.")
        try:
            telegram_chat_id = int(chat_raw)
        except Exception as exc:
            raise RuntimeError(f"TELEGRAM_CHAT_ID inválido: {chat_raw}") from exc

        api_key = os.getenv("API_FOOTBALL_KEY") or ""
        if not api_key:
            raise RuntimeError("Defina API_FOOTBALL_KEY no ambiente.")

        window_start = cls._env_int("WINDOW_START", 47)
        window_end = cls._env_int("WINDOW_END", 75)  # tua config atual
        min_odd = cls._env_float("MIN_ODD", 1.47)
        max_odd = cls._env_float("MAX_ODD", 2.30)  # tua config atual
        ev_min = cls._env_float("EV_MIN", 1.60)     # tua config atual
        cooldown_minutes = cls._env_int("COOLDOWN_MINUTES", 6)
        check_interval = cls._env_int("CHECK_INTERVAL", 1500)
        autostart = cls._env_bool("AUTOSTART", False)

        target_odd = cls._env_float("TARGET_ODD", 1.70)
        use_api_football_odds = cls._env_bool("USE_API_FOOTBALL_ODDS", False)

        bookmaker_id_env = os.getenv("BOOKMAKER_ID")
        bookmaker_id = None
        if bookmaker_id_env:
            try:
                bookmaker_id = int(bookmaker_id_env)
            except Exception:
                bookmaker_id = None

        league_list_env = os.getenv("ALLOWED_LEAGUE_IDS", "").strip()
        allowed_league_ids: List[int] = []
        if league_list_env:
            for part in league_list_env.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    allowed_league_ids.append(int(part))
                except Exception:
                    continue

        bookmaker_name = os.getenv("BOOKMAKER_NAME", "Superbet")
        bookmaker_url = os.getenv("BOOKMAKER_URL", "https://www.superbet.com")

        return cls(
            telegram_token=telegram_token,
            telegram_chat_id=telegram_chat_id,
            api_football_key=api_key,
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
        )

    def pretty(self) -> str:
        lines = [
            "Config EvRadar PRO:",
            f"- Janela: {self.window_start}–{self.window_end}ʼ",
            f"- Odds aceitas: {self.min_odd:.2f}–{self.max_odd:.2f}",
            f"- EV mínimo: {self.ev_min:.2f}%",
            f"- Cooldown por jogo: {self.cooldown_minutes} min",
            f"- Intervalo autoscan: {self.check_interval} s",
            f"- AUTOSTART: {self.autostart}",
            f"- TARGET_ODD (fallback): {self.target_odd:.2f}",
            f"- USE_API_FOOTBALL_ODDS: {self.use_api_football_odds}",
            f"- BOOKMAKER_ID: {self.bookmaker_id}",
            f"- ALLOWED_LEAGUE_IDS: {self.allowed_league_ids}",
            f"- Bookmaker: {self.bookmaker_name} ({self.bookmaker_url})",
        ]
        return "\n".join(lines)


# =========================
# MODELOS DE DADOS
# =========================

@dataclass
class MatchStats:
    fixture_id: int
    league_id: Optional[int]
    league_name: str
    league_country: str

    home_team: str
    away_team: str

    minute: int
    goals_home: int
    goals_away: int

    shots_on_goal_home: int
    shots_on_goal_away: int
    shots_total_home: int
    shots_total_away: int
    dangerous_attacks_home: int
    dangerous_attacks_away: int
    possession_home: float
    possession_away: float


@dataclass
class OddsInfo:
    market_name: str
    line: float
    over_price: float
    under_price: Optional[float] = None


@dataclass
class ScanSummary:
    timestamp: datetime
    total_live_events: int
    analyzed_in_window: int
    signals_sent: int


# =========================
# CLIENTE API-FOOTBALL
# =========================

class APIFootballClient:
    BASE_URL = "https://v3.football.api-sports.io"

    def __init__(self, api_key: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "x-apisports-key": api_key,
                "Accept": "application/json",
            },
            timeout=10.0,
        )

    async def get_live_fixtures(self) -> List[dict]:
        resp = await self._client.get("/fixtures", params={"live": "all"})
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", []) or []

    async def get_fixture_statistics(self, fixture_id: int) -> List[dict]:
        resp = await self._client.get("/fixtures/statistics", params={"fixture": fixture_id})
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", []) or []

    async def get_fixture_odds(
        self,
        fixture_id: int,
        bookmaker_id: Optional[int] = None,
    ) -> Optional[OddsInfo]:
        params: Dict[str, object] = {"fixture": fixture_id}
        if bookmaker_id is not None:
            params["bookmaker"] = bookmaker_id

        resp = await self._client.get("/odds", params=params)
        resp.raise_for_status()
        data = resp.json()
        entries = data.get("response", []) or []
        if not entries:
            return None

        entry = entries[0]
        bookmakers = entry.get("bookmakers", []) or []
        if not bookmakers:
            return None

        chosen = None
        if bookmaker_id is not None:
            for bk in bookmakers:
                try:
                    if int(bk.get("id")) == bookmaker_id:
                        chosen = bk
                        break
                except Exception:
                    continue

        if chosen is None:
            chosen = bookmakers[0]

        bets = chosen.get("bets", []) or []
        for bet in bets:
            name = str(bet.get("name") or "")
            if "Over/Under" in name or "Goals Over/Under" in name or "Total Goals" in name:
                values = bet.get("values", []) or []
                for val in values:
                    val_name = str(val.get("value") or "")
                    odd_str = val.get("odd")
                    if odd_str is None:
                        continue
                    if "Over 0.5" in val_name or val_name.strip() == "Over 0.5":
                        try:
                            price = float(str(odd_str).replace(",", "."))
                        except Exception:
                            continue
                        return OddsInfo(market_name=name, line=0.5, over_price=price)

        return None


# =========================
# MODELO DO EVRADAR
# =========================

class EvRadarModel:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    @staticmethod
    def _clamp(value: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, value))

    def estimate_goal_probability(self, stats: MatchStats) -> float:
        minute = stats.minute
        total_goals = stats.goals_home + stats.goals_away

        base_logit = 1.3 - 0.055 * (minute - 60) + 0.3 * total_goals

        total_shots = stats.shots_total_home + stats.shots_total_away
        total_sog = stats.shots_on_goal_home + stats.shots_on_goal_away
        total_dang = stats.dangerous_attacks_home + stats.dangerous_attacks_away

        shot_pressure = (total_shots / 10.0) * 0.4
        sog_pressure = (total_sog / 4.0) * 0.5
        danger_pressure = (total_dang / 30.0) * 0.4

        poss_home = stats.possession_home
        poss_away = stats.possession_away
        poss_diff = abs(poss_home - poss_away)
        dom_boost = (poss_diff / 100.0) * 0.8

        logit = base_logit + shot_pressure + sog_pressure + danger_pressure + dom_boost

        if total_sog <= 1 and total_dang < 20 and minute > 60:
            logit -= 0.8

        if total_goals >= 3 and (total_sog >= 8 or total_dang >= 50):
            logit += 0.7

        prob = 1.0 / (1.0 + math.exp(-logit))
        prob = self._clamp(prob, 0.05, 0.95)
        return prob

    @staticmethod
    def compute_ev(prob: float, odd: float) -> float:
        return prob * odd - 1.0

    def suggest_stake(self, ev: float, odd: float) -> Tuple[float, str]:
        ev_pct = ev * 100.0

        if ev_pct >= 7.0:
            base_pct = 3.0
            tier = "Tier A"
        elif 5.0 <= ev_pct < 7.0:
            base_pct = 2.5
            tier = "Tier A"
        elif 3.0 <= ev_pct < 5.0:
            base_pct = 2.0
            tier = "Tier B"
        elif 1.5 <= ev_pct < 3.0:
            base_pct = 1.5
            tier = "Tier C"
        else:
            return 0.0, "Ignore"

        if odd > 2.60:
            base_pct *= 0.7
        elif odd > 1.80:
            base_pct *= 0.9

        stake_pct = min(base_pct, 3.0)
        return stake_pct, tier


# =========================
# HELPERS DE STATS
# =========================

def _as_int(value: object, default: int = 0) -> int:
    if value is None:
        return default
    try:
        if isinstance(value, str) and value.endswith("%"):
            value = value.replace("%", "")
        return int(float(str(value).replace(",", ".")))
    except Exception:
        return default


def _as_float(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        if isinstance(value, str) and value.endswith("%"):
            value = value.replace("%", "")
        return float(str(value).replace(",", "."))
    except Exception:
        return default


def build_match_stats(entry: dict, stats_raw: List[dict]) -> MatchStats:
    fixture = entry.get("fixture", {}) or {}
    league = entry.get("league", {}) or {}
    teams = entry.get("teams", {}) or {}
    goals = entry.get("goals", {}) or {}
    status = fixture.get("status", {}) or {}

    league_id = league.get("id")
    league_name = str(league.get("name") or "Liga")
    league_country = str(league.get("country") or "")

    home_team_info = teams.get("home", {}) or {}
    away_team_info = teams.get("away", {}) or {}
    home_name = str(home_team_info.get("name") or "Home")
    away_name = str(away_team_info.get("name") or "Away")

    fixture_id = int(fixture.get("id") or 0)
    minute = int(status.get("elapsed") or 0)

    goals_home = _as_int(goals.get("home"), 0)
    goals_away = _as_int(goals.get("away"), 0)

    home_stats_map: Dict[str, object] = {}
    away_stats_map: Dict[str, object] = {}

    if stats_raw:
        for item in stats_raw:
            team_info = item.get("team", {}) or {}
            tname = str(team_info.get("name") or "")
            statistics = item.get("statistics", []) or []
            smap = {str(s.get("type") or ""): s.get("value") for s in statistics}

            if tname == home_name and not home_stats_map:
                home_stats_map = smap
            elif tname == away_name and not away_stats_map:
                away_stats_map = smap

        if not home_stats_map and len(stats_raw) >= 1:
            statistics = stats_raw[0].get("statistics", []) or []
            home_stats_map = {str(s.get("type") or ""): s.get("value") for s in statistics}
        if not away_stats_map and len(stats_raw) >= 2:
            statistics = stats_raw[1].get("statistics", []) or []
            away_stats_map = {str(s.get("type") or ""): s.get("value") for s in statistics}

    def get_stat(smap: Dict[str, object], key: str, default: int = 0) -> int:
        return _as_int(smap.get(key), default)

    def get_possession(smap: Dict[str, object]) -> float:
        raw = smap.get("Ball Possession")
        return _as_float(raw, 0.0)

    shots_on_goal_home = get_stat(home_stats_map, "Shots on Goal", 0)
    shots_on_goal_away = get_stat(away_stats_map, "Shots on Goal", 0)
    shots_total_home = get_stat(home_stats_map, "Total Shots", 0)
    shots_total_away = get_stat(away_stats_map, "Total Shots", 0)
    dangerous_attacks_home = get_stat(home_stats_map, "Dangerous Attacks", 0)
    dangerous_attacks_away = get_stat(away_stats_map, "Dangerous Attacks", 0)
    possession_home = get_possession(home_stats_map)
    possession_away = get_possession(away_stats_map)

    return MatchStats(
        fixture_id=fixture_id,
        league_id=league_id,
        league_name=league_name,
        league_country=league_country,
        home_team=home_name,
        away_team=away_name,
        minute=minute,
        goals_home=goals_home,
        goals_away=goals_away,
        shots_on_goal_home=shots_on_goal_home,
        shots_on_goal_away=shots_on_goal_away,
        shots_total_home=shots_total_home,
        shots_total_away=shots_total_away,
        dangerous_attacks_home=dangerous_attacks_home,
        dangerous_attacks_away=dangerous_attacks_away,
        possession_home=possession_home,
        possession_away=possession_away,
    )


async def resolve_current_odd(
    cfg: Config,
    client: APIFootballClient,
    fixture_id: int,
) -> Tuple[float, str]:
    current_odd: Optional[float] = None
    source = "TARGET_ODD"

    if cfg.use_api_football_odds:
        try:
            odds_info = await client.get_fixture_odds(fixture_id, cfg.bookmaker_id)
            if odds_info and odds_info.over_price:
                current_odd = float(odds_info.over_price)
                source = "API_FOOTBALL"
        except Exception:
            logging.exception("Erro ao buscar odds no API-Football para fixture %s", fixture_id)

    if current_odd is None:
        current_odd = cfg.target_odd
        source = "TARGET_ODD"

    return current_odd, source


def format_signal_message(
    stats: MatchStats,
    prob: float,
    ev: float,
    odd: float,
    stake_pct: float,
    tier: str,
    cfg: Config,
    odd_source: str,
) -> str:
    prob_pct = prob * 100.0
    ev_pct = ev * 100.0
    fair_odd = 1.0 / prob if prob > 0 else 0.0
    total_sog = stats.shots_on_goal_home + stats.shots_on_goal_away
    total_dang = stats.dangerous_attacks_home + stats.dangerous_attacks_away
    total_shots = stats.shots_total_home + stats.shots_total_away

    contexto_parts: List[str] = []
    if total_sog >= 6:
        contexto_parts.append("muitos chutes no alvo")
    elif total_sog >= 3:
        contexto_parts.append("volume razoável de finalizações no alvo")

    if total_dang >= 50:
        contexto_parts.append("pressão constante e ataques perigosos em alta")
    elif total_dang >= 30:
        contexto_parts.append("bom volume de ataques perigosos")

    if total_shots >= 15 and not contexto_parts:
        contexto_parts.append("muitos chutes, mesmo com poucos no alvo")

    if not contexto_parts:
        contexto_parts.append("ritmo moderado, mas modelo vê espaço para um gol a mais")

    if ev_pct >= 7.0:
        label_valor = "valor alto"
    elif ev_pct >= 4.0:
        label_valor = "bom valor"
    else:
        label_valor = "valor leve"

    interpretacao = (
        f"{', '.join(contexto_parts)}. Modelo aponta {prob_pct:.1f}% "
        f"de chance de 1 gol a mais e {label_valor} na odd atual."
    )

    odd_source_str = "odd ref" if odd_source == "TARGET_ODD" else "odd ao vivo (API-Football)"
    ev_flag = "EV+" if ev >= 0 else "EV-"

    lines = [
        f"🔔 <b>{tier} — Sinal EvRadar PRO</b>",
        "",
        f"🏟️ {stats.home_team} vs {stats.away_team} — {stats.league_name}",
        f"⏱️ {stats.minute}' | 🔢 {stats.goals_home}–{stats.goals_away}",
        f"⚙️ Linha: Over (soma + 0,5) @ {odd:.2f} <i>({odd_source_str})</i>",
        "",
        "📊 Probabilidade & valor:",
        f"- P_final (gol a mais): {prob_pct:.1f}%",
        f"- Odd justa (modelo): {fair_odd:.2f}",
        f"- EV: {ev_pct:.2f}% → <b>{ev_flag}</b>",
        "",
        f"💰 Stake sugerida: <b>{stake_pct:.1f}%</b> da banca",
        "",
        "🧩 Interpretação:",
        interpretacao,
        "",
        f"🔗 <a href=\"{cfg.bookmaker_url}\">Abrir evento ({cfg.bookmaker_name})</a>",
    ]
    return "\n".join(lines)


def format_summary_message(summary: ScanSummary, origin: str) -> str:
    time_str = summary.timestamp.astimezone(timezone(timedelta(hours=-3))).strftime("%H:%M:%S")
    line = (
        f"[EvRadar PRO] Scan concluído (origem={origin}, {time_str}). "
        f"Eventos ao vivo: {summary.total_live_events} | "
        f"Jogos analisados na janela: {summary.analyzed_in_window} | "
        f"Alertas enviados: {summary.signals_sent}."
    )
    return line


# =========================
# LÓGICA DE SCAN
# =========================

async def perform_scan(
    cfg: Config,
    client: APIFootballClient,
    model: EvRadarModel,
    cooldowns: Dict[int, datetime],
) -> Tuple[ScanSummary, List[str]]:
    now = datetime.now(timezone.utc)
    try:
        live_fixtures = await client.get_live_fixtures()
    except Exception as exc:
        logging.exception("Erro ao buscar fixtures ao vivo: %s", exc)
        live_fixtures = []

    total_live = len(live_fixtures)
    analyzed_in_window = 0
    signals: List[str] = []

    for entry in live_fixtures:
        try:
            fixture = entry.get("fixture", {}) or {}
            league = entry.get("league", {}) or {}
            status = fixture.get("status", {}) or {}

            elapsed = status.get("elapsed")
            if elapsed is None:
                continue
            minute = int(elapsed)

            if minute < cfg.window_start or minute > cfg.window_end:
                continue

            league_type = str(league.get("type") or "")
            if "Friendly" in league_type:
                continue

            league_id = league.get("id")
            if cfg.allowed_league_ids and league_id not in cfg.allowed_league_ids:
                continue

            fixture_id = int(fixture.get("id") or 0)
            analyzed_in_window += 1

            last_alert = cooldowns.get(fixture_id)
            if last_alert is not None:
                diff = (now - last_alert).total_seconds() / 60.0
                if diff < cfg.cooldown_minutes:
                    continue

            stats_raw = await client.get_fixture_statistics(fixture_id)
            stats_obj = build_match_stats(entry, stats_raw)

            current_odd, odd_source = await resolve_current_odd(cfg, client, fixture_id)
            if current_odd < cfg.min_odd or current_odd > cfg.max_odd:
                continue

            prob = model.estimate_goal_probability(stats_obj)
            ev = model.compute_ev(prob, current_odd)

            if ev * 100.0 < cfg.ev_min:
                continue

            stake_pct, tier = model.suggest_stake(ev, current_odd)
            if stake_pct <= 0.0:
                continue

            msg = format_signal_message(
                stats=stats_obj,
                prob=prob,
                ev=ev,
                odd=current_odd,
                stake_pct=stake_pct,
                tier=tier,
                cfg=cfg,
                odd_source=odd_source,
            )
            signals.append(msg)
            cooldowns[fixture_id] = now

        except Exception:
            logging.exception("Erro ao processar fixture (radar continua nos demais).")
            continue

    summary = ScanSummary(
        timestamp=datetime.now(timezone.utc),
        total_live_events=total_live,
        analyzed_in_window=analyzed_in_window,
        signals_sent=len(signals),
    )
    return summary, signals


# =========================
# HANDLERS TELEGRAM
# =========================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    lines = [
        "👋 EvRadar PRO online.",
        "",
        f"Janela padrão: {cfg.window_start}–{cfg.window_end}ʼ",
        f"Odd ref (TARGET_ODD): {cfg.target_odd:.2f}",
        f"EV mínimo: {cfg.ev_min:.2f}%",
        f"Cooldown por jogo: {cfg.cooldown_minutes} min",
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


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    client: APIFootballClient = context.application.bot_data["client"]
    model: EvRadarModel = context.application.bot_data["model"]
    cooldowns: Dict[int, datetime] = context.application.bot_data["cooldowns"]

    if update.message:
        await update.message.reply_text("🔍 Iniciando varredura manual de jogos ao vivo...")

    summary, signals = await perform_scan(cfg, client, model, cooldowns)
    bot = context.application.bot
    chat_id = cfg.telegram_chat_id

    for msg in signals:
        await bot.send_message(
            chat_id=chat_id,
            text=msg,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    summary_msg = format_summary_message(summary, origin="manual")
    await bot.send_message(chat_id=chat_id, text=summary_msg)

    context.application.bot_data["last_summary"] = summary


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    summary: Optional[ScanSummary] = context.application.bot_data.get("last_summary")
    if summary is None:
        if update.message:
            await update.message.reply_text(
                "Ainda não fiz nenhuma varredura nesta sessão. Use /scan para rodar uma agora."
            )
        return

    msg = format_summary_message(summary, origin="último")
    if update.message:
        await update.message.reply_text(msg)


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    cooldowns: Dict[int, datetime] = context.application.bot_data["cooldowns"]
    last_summary: Optional[ScanSummary] = context.application.bot_data.get("last_summary")

    lines = [
        "🔧 <b>Debug EvRadar PRO</b>",
        "",
        "<b>Config:</b>",
        cfg.pretty(),
        "",
        f"<b>Jogos em cooldown:</b> {len(cooldowns)}",
    ]
    if last_summary:
        lines.append("")
        lines.append("<b>Último resumo:</b>")
        lines.append(format_summary_message(last_summary, origin="último"))

    if update.message:
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    lines = [
        "🔗 Links úteis:",
        f"- <a href=\"{cfg.bookmaker_url}\">Abrir bookmaker ({cfg.bookmaker_name})</a>",
    ]
    if update.message:
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if update.message and chat:
        await update.message.reply_text(f"Seu chat_id é: {chat.id}")


async def debug_update_logger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    user_id = update.effective_user.id if update.effective_user else None
    text = update.message.text if update.message else None
    logging.info("Recebi update: chat_id=%s user_id=%s text=%r", chat_id, user_id, text)


# =========================
# LOOP AUTO-SCAN
# =========================

async def auto_scan_loop(application: Application) -> None:
    cfg: Config = application.bot_data["cfg"]
    client: APIFootballClient = application.bot_data["client"]
    model: EvRadarModel = application.bot_data["model"]
    cooldowns: Dict[int, datetime] = application.bot_data["cooldowns"]
    chat_id = cfg.telegram_chat_id

    await asyncio.sleep(3)

    while True:
        try:
            summary, signals = await perform_scan(cfg, client, model, cooldowns)
            bot = application.bot

            for msg in signals:
                await bot.send_message(
                    chat_id=chat_id,
                    text=msg,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )

            summary_msg = format_summary_message(summary, origin="auto")
            await bot.send_message(chat_id=chat_id, text=summary_msg)

            application.bot_data["last_summary"] = summary

        except Exception:
            logging.exception("Erro na varredura automática (loop continua).")

        await asyncio.sleep(cfg.check_interval)


async def post_init(application: Application) -> None:
    cfg: Config = application.bot_data["cfg"]
    application.bot_data.setdefault("cooldowns", {})

    # Mensagem de boas-vindas no chat configurado
    try:
        await application.bot.send_message(
            chat_id=cfg.telegram_chat_id,
            text="✅ EvRadar PRO conectado. Use /start para ver as configs e /scan para varrer os jogos.",
        )
        logging.info("Mensagem de boas-vindas enviada para chat_id=%s", cfg.telegram_chat_id)
    except Exception:
        logging.exception("Erro ao enviar mensagem de boas-vindas.")

    if cfg.autostart:
        logging.info("AUTOSTART=1 → iniciando loop de varredura automática.")
        application.bot_data["autoscan_task"] = asyncio.create_task(auto_scan_loop(application))
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

    cfg = Config.from_env()
    logging.info("Config carregada:\n%s", cfg.pretty())

    api_client = APIFootballClient(cfg.api_football_key)
    model = EvRadarModel(cfg)

    application = (
        Application.builder()
        .token(cfg.telegram_token)
        .post_init(post_init)
        .build()
    )

    application.bot_data["cfg"] = cfg
    application.bot_data["client"] = api_client
    application.bot_data["model"] = model
    application.bot_data["cooldowns"] = {}

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("debug", cmd_debug))
    application.add_handler(CommandHandler("links", cmd_links))
    application.add_handler(CommandHandler("id", cmd_id))

    # logger de tudo (por último, pra não atropelar commands)
    application.add_handler(MessageHandler(filters.ALL, debug_update_logger))

    logging.info("Iniciando bot do EvRadar PRO...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
