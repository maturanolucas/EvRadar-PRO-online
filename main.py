#!/usr/bin/env python3
"""
EvRadar PRO — Versão Notebook/Nuvem (Telegram + API-Football, Render-friendly, odds reais, contexto, banca virtual e auto-settle)

- Funciona local (notebook) e em nuvem (Render Web Service free).
- Usa polling do Telegram (sem webhook).
- Abre um servidor HTTP mínimo só para o Render enxergar uma porta aberta.
- Autoscan sem spam: só manda mensagem automática SE houver alerta.
- Calcula EV usando, quando possível, a ODD REAL AO VIVO da casa (API-Football /odds/live),
  comparando com a odd justa do modelo.
- Inclui camada de contexto/noticiário heurístico automático.
- Controla uma banca virtual com stake sugerida (Kelly fracionado + alavancagem).
- AUTO-SETTLE: resolve apostas registradas via /entrei, usando /fixtures?id=fixture_id:
    - Se sair +1 gol depois da entrada → green automático.
    - Se o jogo terminar sem +1 gol → red automático.

Requisitos (instalar uma vez no seu Python):
    pip install python-telegram-bot==21.6 httpx python-dotenv
"""

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv
from telegram import Update
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
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("evradar")

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

    # Banca virtual / Kelly
    virtual_bankroll_enabled: bool
    virtual_bankroll_initial: float
    kelly_fraction: float
    stake_cap_pct: float
    leverage: float

    # Boost de contexto/news
    context_base_boost_max: float  # em pontos percentuais (pp)
    context_league_boost: Dict[int, float]

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
        if self.virtual_bankroll_enabled:
            linhas.append(
                "• Banca virtual: ON (inicial ≈ R${:.2f}, alavancagem x{:.2f})".format(
                    self.virtual_bankroll_initial, self.leverage
                )
            )
        else:
            linhas.append("• Banca virtual: OFF")
        return "\n".join(linhas)


@dataclass
class Candidate:
    fixture_id: int
    league_id: int
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
    context_boost_pp: float
    context_desc: str
    stake_pct: float
    stake_amount: float


@dataclass
class Bet:
    bet_id: int
    fixture_id: int
    description: str
    odd: float
    stake_pct: float
    stake_amount: float
    total_goals_at_entry: int
    result: Optional[str] = None  # "green", "red" ou None


@dataclass
class State:
    last_scan_summary: str = ""
    last_scan_time: float = 0.0
    autoscan_task: Optional[asyncio.Task] = None
    chat_id_bound: Optional[int] = None
    last_candidates: List[Candidate] = field(default_factory=list)
    virtual_bankroll: float = 0.0
    virtual_bankroll_initial: float = 0.0
    bets: List[Bet] = field(default_factory=list)
    next_bet_id: int = 1


STATE = State()

# =========================
#  API-Football Client
# =========================

API_BASE = "https://v3.football.api-sports.io"
OVER_UNDER_BET_ID = 36  # mercado Over/Under para odds/live (API-Football)


class ApiFootballClient:
    def __init__(self, api_key: str, league_filter: List[int]) -> None:
        self.api_key = api_key
        self.league_filter = set(league_filter)
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {
                "x-apisports-key": self.api_key,
            }
            self._client = httpx.AsyncClient(
                base_url=API_BASE,
                headers=headers,
                timeout=10.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch_live_fixtures(self) -> List[Dict[str, Any]]:
        client = await self._get_client()
        params = {"live": "all"}
        resp = await client.get("/fixtures", params=params)
        if resp.status_code != 200:
            logger.error(
                "HTTP %s em fixtures(live=all): %s",
                resp.status_code,
                resp.text[:300],
            )
            return []
        data = resp.json()
        fixtures = data.get("response", []) or []
        out: List[Dict[str, Any]] = []
        for fx in fixtures:
            league = fx.get("league") or {}
            league_id = league.get("id")
            if league_id is None:
                continue
            try:
                league_id_int = int(league_id)
            except Exception:
                continue
            if self.league_filter and league_id_int not in self.league_filter:
                continue
            out.append(fx)
        return out

    async def fetch_statistics(self, fixture_id: int) -> Dict[str, Dict[str, Any]]:
        client = await self._get_client()
        params = {"fixture": fixture_id}
        resp = await client.get("/fixtures/statistics", params=params)
        if resp.status_code != 200:
            logger.error(
                "HTTP %s em fixtures/statistics(%s): %s",
                resp.status_code,
                fixture_id,
                resp.text[:300],
            )
            return {}
        data = resp.json()
        stats_list = data.get("response", []) or []
        out: Dict[str, Dict[str, Any]] = {}
        for item in stats_list:
            team = item.get("team") or {}
            stats = item.get("statistics") or []
            stats_map: Dict[str, Any] = {}
            for s in stats:
                t = s.get("type")
                v = s.get("value")
                if t:
                    stats_map[t] = v
            if not out.get("home"):
                out["home"] = stats_map
            else:
                out["away"] = stats_map
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

    async def fetch_fixture_basic(self, fixture_id: int) -> Optional[Dict[str, Any]]:
        """
        Busca status e gols do fixture via /fixtures?id=...
        Retorna dict com:
            - status_short
            - goals_home
            - goals_away
        """
        client = await self._get_client()
        params = {"id": fixture_id}
        try:
            resp = await client.get("/fixtures", params=params)
        except Exception as exc:
            logger.error("Erro em fixtures(id=%s): %s", fixture_id, exc)
            return None

        if resp.status_code != 200:
            logger.error(
                "HTTP %s em fixtures(id=%s): %s",
                resp.status_code,
                fixture_id,
                resp.text[:300],
            )
            return None

        data = resp.json()
        arr = data.get("response", []) or []
        if not arr:
            return None
        fx = arr[0]
        fixture = fx.get("fixture") or {}
        goals = fx.get("goals") or {}
        status_short = (fixture.get("status") or {}).get("short") or ""
        gh = goals.get("home") or 0
        ga = goals.get("away") or 0
        try:
            gh = int(gh)
        except Exception:
            gh = 0
        try:
            ga = int(ga)
        except Exception:
            ga = 0
        return {
            "status_short": status_short,
            "goals_home": gh,
            "goals_away": ga,
        }


# =========================
#  Modelo de probabilidade
# =========================


def _safe_int(x: Any) -> int:
    try:
        if x is None:
            return 0
        return int(x)
    except Exception:
        try:
            return int(float(x))
        except Exception:
            return 0


def extract_basic_stats(
    stats_home: Dict[str, Any],
    stats_away: Dict[str, Any],
) -> Tuple[int, int, int, int, int, int]:
    sh = _safe_int(stats_home.get("Shots on Goal"))
    sa = _safe_int(stats_away.get("Shots on Goal"))
    st_h = _safe_int(stats_home.get("Shots off Goal"))
    st_a = _safe_int(stats_away.get("Shots off Goal"))
    da_h = _safe_int(stats_home.get("Dangerous Attacks"))
    da_a = _safe_int(stats_away.get("Dangerous Attacks"))
    return sh, sa, st_h, st_a, da_h, da_a


def compute_pressure_index(
    minute: int,
    sh: int,
    sa: int,
    st_h: int,
    st_a: int,
    da_h: int,
    da_a: int,
) -> Tuple[float, str]:
    total_shots = sh + sa + st_h + st_a
    total_da = da_h + da_a

    shot_factor = min(total_shots / 12.0, 1.5)
    da_factor = min(total_da / 40.0, 1.5)
    pace = (shot_factor * 0.6 + da_factor * 0.4) * 10.0

    minute_factor = 1.0
    if minute >= 70:
        minute_factor = 1.15
    elif minute >= 60:
        minute_factor = 1.05
    elif minute <= 50:
        minute_factor = 0.9

    pressure = max(0.0, min(pace * minute_factor, 10.0))

    if pressure >= 8.0:
        desc = "pressão altíssima, jogo franco"
    elif pressure >= 6.0:
        desc = "jogo bem aberto, bom ritmo"
    elif pressure >= 4.0:
        desc = "ritmo ok, jogo morno mas chegando"
    else:
        desc = "ritmo baixo, poucas chegadas"

    return pressure, desc


def compute_base_goal_probability(
    minute: int,
    window_start: int,
    window_end: int,
    pressure_index: float,
    goals_home: int,
    goals_away: int,
) -> float:
    base = 0.35
    base += (pressure_index - 5.0) * 0.03
    if goals_home + goals_away == 0:
        base += 0.03
    elif goals_home + goals_away >= 3:
        base -= 0.03

    if minute < window_start:
        t = window_start
    elif minute > window_end:
        t = window_end
    else:
        t = minute

    remaining = max(window_end - t, 1)
    total_window = max(window_end - window_start, 1)
    time_factor = remaining / float(total_window)
    base *= (0.8 + 0.4 * time_factor)

    p = max(0.05, min(base, 0.90))
    return p


def compute_context_boost(
    league_id: int,
    league_name: str,
    minute: int,
    goals_home: int,
    goals_away: int,
    pressure_index: float,
    stats_home: Dict[str, Any],
    stats_away: Dict[str, Any],
    settings: Settings,
) -> Tuple[float, str]:
    boost = 0.0
    motivos: List[str] = []

    league_boost_map = settings.context_league_boost
    lb = league_boost_map.get(league_id, 0.0)
    if lb != 0.0:
        boost += lb
        if lb > 0:
            motivos.append("jogo de liga/copas importante")
        else:
            motivos.append("competição com menor peso")

    ln = (league_name or "").lower()
    if "champions" in ln:
        boost += 0.8
        motivos.append("Champions League, clima decisivo")
    elif "libertadores" in ln:
        boost += 0.8
        motivos.append("Libertadores, clima decisivo")
    elif "premier" in ln or "brasileirao" in ln or "serie a" in ln:
        boost += 0.4
        motivos.append("liga principal de alto nível")

    if minute >= 70 and goals_home == goals_away:
        if pressure_index >= 6.0:
            boost += 0.5
            motivos.append("fim de jogo empatado com boa pressão")
        elif pressure_index >= 4.5:
            boost += 0.3
            motivos.append("fim de jogo empatado com ritmo razoável")

    red_h = _safe_int(stats_home.get("Red Cards"))
    red_a = _safe_int(stats_away.get("Red Cards"))
    total_reds = red_h + red_a
    if total_reds >= 2:
        boost -= 0.4
        motivos.append("muitos cartões vermelhos, jogo travado")

    max_boost = settings.context_base_boost_max
    boost = max(-max_boost, min(boost, max_boost))

    if not motivos:
        motivos.append("sem grande destaque de contexto")

    desc = "; ".join(motivos)
    return boost, desc


def classify_tier(ev_pct: float) -> str:
    if ev_pct >= 7.0:
        return "A"
    if ev_pct >= 4.0:
        return "B"
    return "C"


def compute_kelly_stake_pct(
    prob_goal: float,
    used_odd: float,
    ev_pct: float,
    settings: Settings,
) -> float:
    b = used_odd - 1.0
    p = prob_goal
    q = 1.0 - p
    kelly_raw = (b * p - q) / b if b > 0 else 0.0
    if kelly_raw < 0:
        kelly_raw = 0.0
    stake = kelly_raw * settings.kelly_fraction * 100.0

    if used_odd <= 1.80:
        odd_factor = 1.0
    elif used_odd <= 2.60:
        odd_factor = 0.9
    else:
        odd_factor = 0.7
    stake *= odd_factor

    if ev_pct >= 7.0:
        tier_factor = 1.25
    elif ev_pct >= 5.0:
        tier_factor = 1.0
    elif ev_pct >= 3.0:
        tier_factor = 0.9
    else:
        tier_factor = 0.7
    stake *= tier_factor

    stake = max(0.0, min(stake, settings.stake_cap_pct))
    return stake


# =========================
#  Core de varredura
# =========================


async def find_candidates(
    api: ApiFootballClient,
    settings: Settings,
) -> List[Candidate]:
    fixtures = await api.fetch_live_fixtures()
    total_live = len(fixtures)
    logger.info("Live fixtures (filtradas por ligas): %s", total_live)

    candidates: List[Candidate] = []
    for fx in fixtures:
        fixture = fx.get("fixture") or {}
        league = fx.get("league") or {}
        teams = fx.get("teams") or {}
        goals = fx.get("goals") or {}

        fixture_id = fixture.get("id")
        if fixture_id is None:
            continue
        try:
            fixture_id_int = int(fixture_id)
        except Exception:
            continue

        status = (fixture.get("status") or {}).get("short") or ""
        if status not in {"2H", "ET"}:
            continue

        minute = fixture.get("status", {}).get("elapsed")
        if minute is None:
            continue
        try:
            minute_int = int(minute)
        except Exception:
            continue

        if minute_int < settings.window_start or minute_int > settings.window_end:
            continue

        home_team = (teams.get("home") or {}).get("name") or "Casa"
        away_team = (teams.get("away") or {}).get("name") or "Fora"
        goals_home = goals.get("home") or 0
        goals_away = goals.get("away") or 0
        try:
            goals_home = int(goals_home)
            goals_away = int(goals_away)
        except Exception:
            goals_home = 0
            goals_away = 0

        stats = await api.fetch_statistics(fixture_id_int)
        stats_home = stats.get("home") or {}
        stats_away = stats.get("away") or {}
        if not stats_home or not stats_away:
            continue

        sh, sa, st_h, st_a, da_h, da_a = extract_basic_stats(stats_home, stats_away)
        pressure_index, pressure_desc = compute_pressure_index(
            minute_int, sh, sa, st_h, st_a, da_h, da_a
        )
        prob_base = compute_base_goal_probability(
            minute_int,
            settings.window_start,
            settings.window_end,
            pressure_index,
            goals_home,
            goals_away,
        )

        league_id = league.get("id") or 0
        try:
            league_id_int = int(league_id)
        except Exception:
            league_id_int = 0
        league_name = league.get("name") or "Liga"

        context_boost_pp, context_desc = compute_context_boost(
            league_id_int,
            league_name,
            minute_int,
            goals_home,
            goals_away,
            pressure_index,
            stats_home,
            stats_away,
            settings,
        )

        prob_goal = prob_base + context_boost_pp / 100.0
        prob_goal = max(0.05, min(prob_goal, 0.95))

        fair_odd = 1.0 / prob_goal if prob_goal > 0 else 99.99

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

        if used_odd < settings.min_odd or used_odd > settings.max_odd:
            continue

        ev = prob_goal * used_odd - 1.0
        ev_pct = ev * 100.0
        if ev_pct < settings.ev_min_pct:
            continue

        tier = classify_tier(ev_pct)

        if settings.virtual_bankroll_enabled and STATE.virtual_bankroll > 0:
            stake_pct = compute_kelly_stake_pct(
                prob_goal,
                used_odd,
                ev_pct,
                settings,
            )
            stake_amount = (
                STATE.virtual_bankroll * (stake_pct / 100.0) * settings.leverage
            )
        else:
            stake_pct = 0.0
            stake_amount = 0.0

        cand = Candidate(
            fixture_id=fixture_id_int,
            league_id=league_id_int,
            league_name=league_name,
            home_team=home_team,
            away_team=away_team,
            minute=minute_int,
            goals_home=goals_home,
            goals_away=goals_away,
            prob_goal=prob_goal,
            fair_odd=fair_odd,
            used_odd=used_odd,
            odd_source=odd_source,
            ev_pct=ev_pct,
            tier=tier,
            pressure_desc=pressure_desc,
            context_boost_pp=context_boost_pp,
            context_desc=context_desc,
            stake_pct=stake_pct,
            stake_amount=stake_amount,
        )
        candidates.append(cand)

    candidates.sort(key=lambda c: c.ev_pct, reverse=True)
    STATE.last_candidates = candidates
    return candidates


# =========================
#  Formatação de mensagens
# =========================


def format_candidate_message(c: Candidate, settings: Settings) -> str:
    jogo = "{} vs {}".format(c.home_team, c.away_team)
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
        tier_title = "Tier C — Sinal marginal"

    ev_label = "EV+ ✅" if c.ev_pct >= 0 else "EV- ❌"

    if c.odd_source == "live":
        odd_info = "odd real da casa"
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
    if c.context_boost_pp != 0.0:
        sinais = "+" if c.context_boost_pp > 0 else ""
        linhas.append(
            "🧠 Contexto: {}{:.1f}pp → {}".format(
                sinais, c.context_boost_pp, c.context_desc
            )
        )
    else:
        linhas.append("🧠 Contexto: neutro → {}".format(c.context_desc))
    linhas.append("")
    linhas.append("🧩 Interpretação:")
    linhas.append("{}".format(c.pressure_desc))
    if settings.virtual_bankroll_enabled and c.stake_pct > 0.0:
        linhas.append("")
        linhas.append("🏦 Banca virtual & stake sugerida:")
        linhas.append(
            "- Stake: {:.2f}% da banca virtual (alavancagem x{:.2f})".format(
                c.stake_pct, settings.leverage
            )
        )
        linhas.append(
            "- Valor aproximado: R${:.2f} (banca atual ≈ R${:.2f})".format(
                c.stake_amount, STATE.virtual_bankroll
            )
        )
        linhas.append("")
        linhas.append(
            "👉 Se entrar, responda com: /entrei {:.2f}".format(c.used_odd)
        )
        linhas.append("   (use a odd REAL que você pegou, ex: /entrei 1.68)")
    return "\n".join(linhas)


def format_scan_summary(
    total_live: int,
    games_in_window: int,
    alerts: int,
) -> str:
    linhas: List[str] = []
    linhas.append("[EvRadar PRO] Scan concluído.")
    linhas.append(
        "Eventos ao vivo: {} | Jogos analisados na janela: {} | Alertas enviados: {}".format(
            total_live, games_in_window, alerts
        )
    )
    return "\n".join(linhas)


# =========================
#  Banca virtual: lógica & comandos
# =========================


def _settle_bet(bet: Bet, result: str) -> Optional[Bet]:
    if bet.result is not None:
        return bet
    if result not in {"green", "red"}:
        return None
    bet.result = result
    if result == "green":
        ganho = bet.stake_amount * (bet.odd - 1.0)
        STATE.virtual_bankroll += ganho
    else:
        STATE.virtual_bankroll -= bet.stake_amount
        if STATE.virtual_bankroll < 0:
            STATE.virtual_bankroll = 0.0
    return bet


def register_bet(odd: float, settings: Settings) -> Optional[Bet]:
    if not STATE.last_candidates:
        return None
    c = STATE.last_candidates[0]
    if not settings.virtual_bankroll_enabled or STATE.virtual_bankroll <= 0:
        return None
    stake_pct = c.stake_pct
    if stake_pct <= 0:
        return None
    stake_amount = STATE.virtual_bankroll * (stake_pct / 100.0) * settings.leverage
    total_goals = c.goals_home + c.goals_away
    bet = Bet(
        bet_id=STATE.next_bet_id,
        fixture_id=c.fixture_id,
        description="{} vs {} ({}')".format(
            c.home_team, c.away_team, c.minute
        ),
        odd=odd,
        stake_pct=stake_pct,
        stake_amount=stake_amount,
        total_goals_at_entry=total_goals,
        result=None,
    )
    STATE.bets.append(bet)
    STATE.next_bet_id += 1
    return bet


def settle_last_bet(result: str) -> Optional[Bet]:
    if not STATE.bets:
        return None
    bet = STATE.bets[-1]
    return _settle_bet(bet, result)


def format_bankroll_status() -> str:
    linhas: List[str] = []
    linhas.append("🏦 Banca virtual do EvRadar PRO")
    linhas.append(
        "Banca atual: R${:.2f} (inicial R${:.2f})".format(
            STATE.virtual_bankroll, STATE.virtual_bankroll_initial
        )
    )
    if not STATE.bets:
        linhas.append("Ainda não há apostas registradas via /entrei.")
        return "\n".join(linhas)
    linhas.append("")
    linhas.append("Últimas apostas registradas:")
    ultimas = STATE.bets[-5:]
    for bet in ultimas:
        status = bet.result or "em aberto"
        linhas.append(
            "#{} — {} | odd {:.2f} | stake {:.2f}% (R${:.2f}) → {}".format(
                bet.bet_id,
                bet.description,
                bet.odd,
                bet.stake_pct,
                bet.stake_amount,
                status,
            )
        )
    return "\n".join(linhas)


# =========================
#  Auto-settle das apostas
# =========================


async def auto_settle_open_bets(
    api: ApiFootballClient,
    settings: Settings,
    app: Application,
) -> None:
    if not settings.virtual_bankroll_enabled:
        return
    if not STATE.bets:
        return

    chat_id = STATE.chat_id_bound or settings.chat_id_default
    if chat_id is None:
        return

    for bet in STATE.bets:
        if bet.result is not None:
            continue
        fx = await api.fetch_fixture_basic(bet.fixture_id)
        if not fx:
            continue
        status = fx.get("status_short") or ""
        gh = fx.get("goals_home") or 0
        ga = fx.get("goals_away") or 0
        total_now = gh + ga
        total_entry = bet.total_goals_at_entry

        # GREEN: assim que sair +1 gol depois da entrada (mesmo com jogo rolando)
        if total_now > total_entry:
            before = STATE.virtual_bankroll
            settled = _settle_bet(bet, "green")
            if settled is None:
                continue
            ganho = STATE.virtual_bankroll - before
            linhas = []
            linhas.append("🟢 Green AUTO na banca virtual:")
            linhas.append(
                "#{} — {} | odd {:.2f}".format(
                    bet.bet_id, bet.description, bet.odd
                )
            )
            linhas.append(
                "Stake: {:.2f}% (R${:.2f})".format(
                    bet.stake_pct, bet.stake_amount
                )
            )
            linhas.append(
                "Lucro aproximado: R${:.2f}".format(ganho)
            )
            linhas.append(
                "Banca virtual atual: R${:.2f}".format(STATE.virtual_bankroll)
            )
            await app.bot.send_message(chat_id=chat_id, text="\n".join(linhas))
            continue

        # RED: só fecha como red se o jogo acabou sem +1 gol
        if status in {"FT", "AET", "PEN"} and total_now == total_entry:
            before = STATE.virtual_bankroll
            settled = _settle_bet(bet, "red")
            if settled is None:
                continue
            perda = before - STATE.virtual_bankroll
            linhas = []
            linhas.append("🔴 Red AUTO na banca virtual:")
            linhas.append(
                "#{} — {} | odd {:.2f}".format(
                    bet.bet_id, bet.description, bet.odd
                )
            )
            linhas.append(
                "Stake: {:.2f}% (R${:.2f})".format(
                    bet.stake_pct, bet.stake_amount
                )
            )
            linhas.append(
                "Perda aproximada: R${:.2f}".format(perda)
            )
            linhas.append(
                "Banca virtual atual: R${:.2f}".format(STATE.virtual_bankroll)
            )
            await app.bot.send_message(chat_id=chat_id, text="\n".join(linhas))


# =========================
#  Telegram handlers
# =========================


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id:
        STATE.chat_id_bound = chat_id
    settings: Settings = context.application.bot_data["settings"]  # type: ignore[index]
    msg = []
    msg.append("👋 EvRadar PRO online (Notebook/Nuvem).")
    msg.append("")
    msg.append(settings.describe())
    text = "\n".join(msg)
    await update.message.reply_text(text)  # type: ignore[union-attr]


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]  # type: ignore[index]
    api: ApiFootballClient = context.application.bot_data["api"]  # type: ignore[index]
    await update.message.reply_text(  # type: ignore[union-attr]
        "🔍 Iniciando varredura manual de jogos ao vivo (EvRadar PRO)..."
    )
    try:
        fixtures = await api.fetch_live_fixtures()
        total_live = len(fixtures)
        candidates = await find_candidates(api, settings)
        alerts = 0
        for c in candidates:
            text = format_candidate_message(c, settings)
            await update.message.reply_text(text)  # type: ignore[union-attr]
            alerts += 1
        games_in_window = len(candidates)
        summary = format_scan_summary(total_live, games_in_window, alerts)
        STATE.last_scan_summary = summary
        STATE.last_scan_time = time.time()
        await update.message.reply_text(summary)  # type: ignore[union-attr]

        # Após o scan manual, também tenta auto-settle das apostas
        await auto_settle_open_bets(api, settings, context.application)
    except Exception as exc:
        logger.exception("Erro no cmd_scan: %s", exc)
        await update.message.reply_text(  # type: ignore[union-attr]
            "⚠️ Erro ao rodar varredura manual: {}".format(exc)
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]  # type: ignore[index]
    if STATE.last_scan_summary:
        elapsed = time.time() - STATE.last_scan_time
        minutos = int(elapsed // 60)
        segundos = int(elapsed % 60)
        linhas = []
        linhas.append("📈 Status do EvRadar PRO (Notebook/Nuvem)")
        linhas.append("")
        linhas.append(STATE.last_scan_summary)
        linhas.append("")
        linhas.append("Última varredura: há {}m{}s".format(minutos, segundos))
        linhas.append("")
        linhas.append(settings.describe())
        await update.message.reply_text("\n".join(linhas))  # type: ignore[union-attr]
    else:
        await update.message.reply_text(  # type: ignore[union-attr]
            "Ainda não foi feita nenhuma varredura nesta sessão."
        )


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]  # type: ignore[index]
    linhas: List[str] = []
    linhas.append("🐞 Debug EvRadar PRO")
    linhas.append("")
    linhas.append("API base: {}".format(API_BASE))
    linhas.append(
        "Ligas ativas ({}): {}".format(
            len(settings.league_ids), sorted(settings.league_ids)
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
        linhas.append(
            "TELEGRAM_CHAT_ID default: {}".format(settings.chat_id_default)
        )
    if settings.virtual_bankroll_enabled:
        linhas.append(
            "Banca virtual: ON (atual R${:.2f})".format(STATE.virtual_bankroll)
        )
    else:
        linhas.append("Banca virtual: OFF")
    await update.message.reply_text("\n".join(linhas))  # type: ignore[union-attr]


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]  # type: ignore[index]
    linhas = []
    linhas.append("🔗 Links úteis")
    linhas.append("")
    linhas.append("Casa de referência: {} ({})".format(
        settings.bookmaker_name, settings.bookmaker_url
    ))
    linhas.append("")
    linhas.append("Abra o evento na casa, confira se a odd está próxima da indicada.")
    await update.message.reply_text("\n".join(linhas))  # type: ignore[union-attr]


async def cmd_entrei(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]  # type: ignore[index]
    if not settings.virtual_bankroll_enabled:
        await update.message.reply_text(  # type: ignore[union-attr]
            "Banca virtual está OFF. Ative via variáveis (BANK_VIRTUAL_ENABLED=1)."
        )
        return
    if STATE.virtual_bankroll <= 0:
        await update.message.reply_text(  # type: ignore[union-attr]
            "Banca virtual esgotada ou não inicializada."
        )
        return
    if not context.args:
        await update.message.reply_text(  # type: ignore[union-attr]
            "Use: /entrei <odd>. Exemplo: /entrei 1.68"
        )
        return
    try:
        odd = float(str(context.args[0]).replace(",", "."))
    except Exception:
        await update.message.reply_text(  # type: ignore[union-attr]
            "Odd inválida. Exemplo de uso: /entrei 1.68"
        )
        return
    bet = register_bet(odd, settings)
    if bet is None:
        await update.message.reply_text(  # type: ignore[union-attr]
            "Não há sinal recente elegível ou stake calculada para registrar aposta."
        )
        return
    linhas = []
    linhas.append("✅ Aposta registrada na banca virtual:")
    linhas.append(
        "#{} — {} | odd {:.2f}".format(
            bet.bet_id, bet.description, bet.odd
        )
    )
    linhas.append(
        "Stake: {:.2f}% da banca (R${:.2f})".format(
            bet.stake_pct, bet.stake_amount
        )
    )
    linhas.append(
        "Total de gols no momento da entrada: {}".format(
            bet.total_goals_at_entry
        )
    )
    linhas.append(
        "Banca virtual atual (antes do resultado): R${:.2f}".format(
            STATE.virtual_bankroll
        )
    )
    linhas.append("")
    linhas.append(
        "O radar vai tentar resolver automaticamente (green/red) assim que sair 1 gol a mais "
        "ou quando o jogo terminar. Você também pode usar /green ou /red manualmente."
    )
    await update.message.reply_text("\n".join(linhas))  # type: ignore[union-attr]


async def cmd_green(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bet = settle_last_bet("green")
    if bet is None:
        await update.message.reply_text(  # type: ignore[union-attr]
            "Nenhuma aposta recente para marcar como green."
        )
        return
    linhas = []
    linhas.append("🟢 Green registrado na banca virtual (manual):")
    linhas.append(
        "#{} — {} | odd {:.2f}".format(
            bet.bet_id, bet.description, bet.odd
        )
    )
    linhas.append(
        "Stake: {:.2f}% (R${:.2f})".format(
            bet.stake_pct, bet.stake_amount
        )
    )
    linhas.append(
        "Banca virtual atual: R${:.2f}".format(STATE.virtual_bankroll)
    )
    await update.message.reply_text("\n".join(linhas))  # type: ignore[union-attr]


async def cmd_red(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bet = settle_last_bet("red")
    if bet is None:
        await update.message.reply_text(  # type: ignore[union-attr]
            "Nenhuma aposta recente para marcar como red."
        )
        return
    linhas = []
    linhas.append("🔴 Red registrado na banca virtual (manual):")
    linhas.append(
        "#{} — {} | odd {:.2f}".format(
            bet.bet_id, bet.description, bet.odd
        )
    )
    linhas.append(
        "Stake: {:.2f}% (R${:.2f})".format(
            bet.stake_pct, bet.stake_amount
        )
    )
    linhas.append(
        "Banca virtual atual: R${:.2f}".format(STATE.virtual_bankroll)
    )
    await update.message.reply_text("\n".join(linhas))  # type: ignore[union-attr]


async def cmd_bank(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = format_bankroll_status()
    await update.message.reply_text(text)  # type: ignore[union-attr]


# =========================
#  Autoscan loop
# =========================


async def autoscan_loop(app: Application) -> None:
    settings: Settings = app.bot_data["settings"]  # type: ignore[index]
    api: ApiFootballClient = app.bot_data["api"]  # type: ignore[index]
    logger.info(
        "Autoscan iniciado (intervalo=%ss) - chat_id_default=%s",
        settings.check_interval,
        settings.chat_id_default,
    )
    while True:
        try:
            await asyncio.sleep(settings.check_interval)
            fixtures = await api.fetch_live_fixtures()
            total_live = len(fixtures)
            candidates = await find_candidates(api, settings)
            alerts = 0
            chat_id = STATE.chat_id_bound or settings.chat_id_default
            if chat_id is None:
                continue
            for c in candidates:
                text = format_candidate_message(c, settings)
                await app.bot.send_message(chat_id=chat_id, text=text)
                alerts += 1
            games_in_window = len(candidates)
            summary = format_scan_summary(total_live, games_in_window, alerts)
            STATE.last_scan_summary = summary
            STATE.last_scan_time = time.time()
            if alerts > 0:
                await app.bot.send_message(chat_id=chat_id, text=summary)

            # Após o scan automático, tenta auto-settle das apostas abertas
            await auto_settle_open_bets(api, settings, app)
        except asyncio.CancelledError:
            logger.info("Autoscan loop cancelado.")
            break
        except Exception as exc:
            logger.error("Erro no autoscan: %s", exc)


# =========================
#  HTTP server fake para Render
# =========================


async def handle_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        await reader.read(1024)
        response = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/plain\r\n"
            "Connection: close\r\n"
            "\r\n"
            "EvRadar PRO running.\r\n"
        )
        writer.write(response.encode("utf-8"))
        await writer.drain()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def start_dummy_http_server() -> None:
    port_str = os.getenv("PORT", "10000")
    try:
        port = int(port_str)
    except Exception:
        port = 10000
    server = await asyncio.start_server(handle_http, "0.0.0.0", port)
    addr = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    logger.info("Servidor HTTP fake ouvindo em %s", addr)
    async with server:
        await server.serve_forever()


# =========================
#  Carregar settings
# =========================


def parse_league_ids(raw: str) -> List[int]:
    items = [x.strip() for x in raw.split(",") if x.strip()]
    out: List[int] = []
    for it in items:
        try:
            out.append(int(it))
        except Exception:
            continue
    return out


def load_settings() -> Settings:
    load_dotenv()

    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id_default_raw = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    chat_id_default: Optional[int] = None
    if chat_id_default_raw:
        try:
            chat_id_default = int(chat_id_default_raw)
        except Exception:
            chat_id_default = None

    if not api_key:
        logger.error("API_FOOTBALL_KEY não configurada.")
        sys.exit(1)
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN não configurado.")
        sys.exit(1)

    def env_float(name: str, default: float) -> float:
        raw = os.getenv(name, "").strip()
        if not raw:
            return default
        try:
            return float(raw.replace(",", "."))
        except Exception:
            return default

    def env_int(name: str, default: int) -> int:
        raw = os.getenv(name, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except Exception:
            return default

    target_odd = env_float("TARGET_ODD", 1.70)
    ev_min_pct = env_float("EV_MIN_PCT", 3.0)
    min_odd = env_float("MIN_ODD", 1.47)
    max_odd = env_float("MAX_ODD", 3.50)
    window_start = env_int("WINDOW_START", 47)
    window_end = env_int("WINDOW_END", 82)
    autostart_flag = os.getenv("AUTOSTART", "1").strip()
    autostart = autostart_flag == "1"
    check_interval = env_int("CHECK_INTERVAL", 60)

    league_ids_raw = os.getenv("LEAGUE_IDS", "").strip()
    if league_ids_raw:
        league_ids = parse_league_ids(league_ids_raw)
    else:
        league_ids = [
            39,
            140,
            135,
            78,
            61,
            71,
            72,
            13,
            14,
            2,
            3,
            4,
            5,
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

    virtual_enabled_flag = os.getenv("BANK_VIRTUAL_ENABLED", "1").strip()
    virtual_enabled = virtual_enabled_flag == "1"
    virtual_initial = env_float("BANKROLL_INITIAL", 5000.0)
    kelly_fraction = env_float("KELLY_FRACTION", 0.5)
    stake_cap_pct = env_float("STAKE_CAP_PCT", 3.0)
    leverage = env_float("BANK_LEVERAGE", 1.0)

    context_base_boost_max = env_float("CONTEXT_BOOST_MAX_PP", 3.0)

    context_league_boost: Dict[int, float] = {}
    high_importance_leagues = [2, 3, 4, 5, 39, 135, 140, 78, 61, 71, 72, 180, 203]
    medium_importance_leagues = [13, 14, 62, 88, 89, 94]
    low_importance_leagues = [128, 136, 141, 144, 79, 253]

    for lid in high_importance_leagues:
        context_league_boost[lid] = 0.8
    for lid in medium_importance_leagues:
        if lid not in context_league_boost:
            context_league_boost[lid] = 0.4
    for lid in low_importance_leagues:
        if lid not in context_league_boost:
            context_league_boost[lid] = 0.0

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
        virtual_bankroll_enabled=virtual_enabled,
        virtual_bankroll_initial=virtual_initial,
        kelly_fraction=kelly_fraction,
        stake_cap_pct=stake_cap_pct,
        leverage=leverage,
        context_base_boost_max=context_base_boost_max,
        context_league_boost=context_league_boost,
    )
    return settings


# =========================
#  Startup / Shutdown hooks
# =========================


async def on_startup(app: Application) -> None:
    settings: Settings = app.bot_data["settings"]  # type: ignore[index]
    if settings.virtual_bankroll_initial > 0 and settings.virtual_bankroll_enabled:
        STATE.virtual_bankroll = settings.virtual_bankroll_initial
        STATE.virtual_bankroll_initial = settings.virtual_bankroll_initial
    if settings.autostart and STATE.autoscan_task is None:
        STATE.autoscan_task = asyncio.create_task(autoscan_loop(app))
    asyncio.create_task(start_dummy_http_server())


async def on_shutdown(app: Application) -> None:
    api: ApiFootballClient = app.bot_data["api"]  # type: ignore[index]
    try:
        if STATE.autoscan_task is not None:
            STATE.autoscan_task.cancel()
            try:
                await STATE.autoscan_task
            except Exception:
                pass
    finally:
        await api.close()


# =========================
#  Main
# =========================


def main() -> None:
    settings = load_settings()
    api = ApiFootballClient(settings.api_key, settings.league_ids)

    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .concurrent_updates(True)
        .build()
    )

    application.bot_data["settings"] = settings
    application.bot_data["api"] = api

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("debug", cmd_debug))
    application.add_handler(CommandHandler("links", cmd_links))
    application.add_handler(CommandHandler("entrei", cmd_entrei))
    application.add_handler(CommandHandler("green", cmd_green))
    application.add_handler(CommandHandler("red", cmd_red))
    application.add_handler(CommandHandler("bank", cmd_bank))

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        stop_signals=None,
        before_startup=on_startup,
        post_shutdown=on_shutdown,
    )


if __name__ == "__main__":
    main()
