#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EvRadar PRO - Telegram + Cérebro v0.3-lite
------------------------------------------
Features:
- Telegram estável (python-telegram-bot v21)
- Consulta jogos ao vivo na API-FOOTBALL
- Filtro por ligas e janela de tempo
- Score de pressão / chances ao vivo
- Probabilidade de +1 gol no 2º tempo
- Odds em tempo real (API-FOOTBALL) com backup em cache
- Integração opcional com The Odds API para odds ao vivo
- News boost (NewsAPI, opcional)
- Pré-jogo boost:
    - Manual (PREMATCH_TEAM_RATINGS)
    - Automático (API-FOOTBALL /teams/statistics, com cache diário)
- Impacto de jogadores em campo:
    - Stats por time/temporada (API-FOOTBALL /players)
    - Lineups + substituições (fixtures/lineups + fixtures/events)
    - Boost de probabilidade conforme “peso ofensivo” do XI atual
- Filtros de contexto alinhados ao teu faro:
    - Times under vs over (ataque/defesa por gols/jogo)
    - Flag match_super_under (duas equipes bem under)
    - Muito mais cautela em EMPATES (só libera favorito forte + pressão alta + defesa frágil)
    - Mandante claramente under vencendo → torneira quase sempre fechada
    - Linhas altas em jogos super under podem ser bloqueadas
    - Malus específico para mata-mata ida/volta (1º jogo tende a ser mais travado)
- Cálculo de EV e alertas Telegram quando EV >= EV_MIN_PCT

Baseado na tua versão estável anterior (v0.2-lite + odds reais + news + pré-jogo manual).
"""

import asyncio
import logging
import os

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "12"))

from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timedelta, timezone

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------------------------------------------------------------------------
# Helpers de env
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


def _get_env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _parse_league_ids(raw: str) -> List[int]:
    if not raw:
        return []
    parts = raw.replace(" ", "").split(",")
    ids: List[int] = []
    for p in parts:
        if not p:
            continue
        try:
            ids.append(int(p))
        except ValueError:
            continue
    return ids


def _parse_odds_api_league_map(raw: str) -> Dict[int, str]:
    """
    Converte string "39:soccer_epl;140:soccer_spain_la_liga" em
    {39: "soccer_epl", 140: "soccer_spain_la_liga"}.
    """
    mapping: Dict[int, str] = {}
    if not raw:
        return mapping
    parts = raw.split(";")
    for part in parts:
        part = part.strip()
        if not part or ":" not in part:
            continue
        lid_str, sport_key = part.split(":", 1)
        try:
            lid = int(lid_str.strip())
        except ValueError:
            continue
        mapping[lid] = sport_key.strip()
    return mapping


# ---------------------------------------------------------------------------
# Variáveis de ambiente
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN: str = _get_env_str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: Optional[int] = None
_chat_raw = _get_env_str("TELEGRAM_CHAT_ID")
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

# Cooldown e pressão mínima
COOLDOWN_MINUTES: int = _get_env_int("COOLDOWN_MINUTES", 6)
MIN_PRESSURE_SCORE: float = _get_env_float("MIN_PRESSURE_SCORE", 5.0)

# Banca virtual para sugestão de stake
BANKROLL_INITIAL: float = _get_env_float("BANKROLL_INITIAL", 5000.0)

BOOKMAKER_NAME: str = _get_env_str("BOOKMAKER_NAME", "Superbet")
BOOKMAKER_URL: str = _get_env_str("BOOKMAKER_URL", "https://www.superbet.com/")

API_FOOTBALL_KEY: str = _get_env_str("API_FOOTBALL_KEY")
API_FOOTBALL_BASE_URL: str = _get_env_str(
    "API_FOOTBALL_BASE_URL",
    "https://v3.football.api-sports.io",
)

LEAGUE_IDS_RAW: str = _get_env_str("LEAGUE_IDS")
LEAGUE_IDS: List[int] = _parse_league_ids(LEAGUE_IDS_RAW)

USE_API_FOOTBALL_ODDS: int = _get_env_int("USE_API_FOOTBALL_ODDS", 0)
BOOKMAKER_ID: int = _get_env_int("BOOKMAKER_ID", 34)  # 34 = Superbet
# Lista de bookmakers fallback (por ex.: 6,8,9)
BOOKMAKER_FALLBACK_IDS_RAW: str = _get_env_str("BOOKMAKER_FALLBACK_IDS", "")
BOOKMAKER_FALLBACK_IDS: List[int] = _parse_league_ids(BOOKMAKER_FALLBACK_IDS_RAW)
# Bet de Over/Under na API-FOOTBALL. Se 0, não filtra por bet.
ODDS_BET_ID: int = _get_env_int("ODDS_BET_ID", 0)

# NewsAPI (opcional)
NEWS_API_KEY: str = _get_env_str("NEWS_API_KEY")
USE_NEWS_API: int = _get_env_int("USE_NEWS_API", 0)
NEWS_TIME_WINDOW_HOURS: int = _get_env_int("NEWS_TIME_WINDOW_HOURS", 24)

# Pré-jogo auto (API-FOOTBALL /teams/statistics)
USE_API_PREGAME: int = _get_env_int("USE_API_PREGAME", 0)
PREGAME_CACHE_HOURS: int = _get_env_int("PREGAME_CACHE_HOURS", 12)

# Impacto de jogadores (camada nova)
USE_PLAYER_IMPACT: int = _get_env_int("USE_PLAYER_IMPACT", 0)
PLAYER_STATS_CACHE_HOURS: int = _get_env_int("PLAYER_STATS_CACHE_HOURS", 24)
PLAYER_EVENTS_CACHE_MINUTES: int = _get_env_int("PLAYER_EVENTS_CACHE_MINUTES", 4)
PLAYER_MAX_BOOST_PCT: float = _get_env_float("PLAYER_MAX_BOOST_PCT", 6.0)  # pp máx
PLAYER_SUB_TRIGGER_WINDOW: int = _get_env_int("PLAYER_SUB_TRIGGER_WINDOW", 15)

# The Odds API (integração de odds ao vivo)
ODDS_API_KEY: str = _get_env_str("ODDS_API_KEY")
ODDS_API_USE: int = _get_env_int("ODDS_API_USE", 1)
ODDS_API_BASE_URL: str = _get_env_str(
    "ODDS_API_BASE_URL",
    "https://api.the-odds-api.com/v4",
)
ODDS_API_REGIONS: str = _get_env_str("ODDS_API_REGIONS", "eu")
ODDS_API_MARKETS: str = _get_env_str("ODDS_API_MARKETS", "totals")
ODDS_API_DEFAULT_SPORT_KEY: str = _get_env_str("ODDS_API_DEFAULT_SPORT_KEY")
ODDS_API_LEAGUE_MAP_RAW: str = _get_env_str("ODDS_API_LEAGUE_MAP", "")
ODDS_API_LEAGUE_MAP: Dict[int, str] = _parse_odds_api_league_map(
    ODDS_API_LEAGUE_MAP_RAW
)
ODDS_API_BOOKMAKERS_RAW: str = _get_env_str("ODDS_API_BOOKMAKERS", "")
ODDS_API_BOOKMAKERS: List[str] = [
    s.strip()
    for s in ODDS_API_BOOKMAKERS_RAW.split(",")
    if s.strip()
]

# Limite diário de chamadas à The Odds API (para proteger o plano grátis)
ODDS_API_DAILY_LIMIT: int = _get_env_int("ODDS_API_DAILY_LIMIT", 15)

# NOVO: modo de alerta manual quando não houver odd nas APIs
ALLOW_ALERTS_WITHOUT_ODDS: int = _get_env_int("ALLOW_ALERTS_WITHOUT_ODDS", 1)
MANUAL_MIN_ODD_HINT: float = _get_env_float("MANUAL_MIN_ODD_HINT", 1.47)

# NOVO: detecção de favorito via odds pré-live (API-FOOTBALL /odds)
USE_PRELIVE_FAVORITE: int = _get_env_int("USE_PRELIVE_FAVORITE", 1)
PRELIVE_CACHE_HOURS: int = _get_env_int("PRELIVE_CACHE_HOURS", 24)
PRELIVE_ODDS_BOOKMAKER_ID: int = _get_env_int("PRELIVE_ODDS_BOOKMAKER_ID", 0)
PRELIVE_STRONG_MAX_ODD: float = _get_env_float("PRELIVE_STRONG_MAX_ODD", 1.60)
PRELIVE_SUPER_MAX_ODD: float = _get_env_float("PRELIVE_SUPER_MAX_ODD", 1.35)

# NOVO: desconfiança progressiva em linhas altas (3.5, 4.5, 5.5...)
HIGH_LINE_START: float = _get_env_float("HIGH_LINE_START", 3.5)
HIGH_LINE_STEP_MALUS_PROB: float = _get_env_float("HIGH_LINE_STEP_MALUS_PROB", 0.012)
HIGH_LINE_PRESSURE_STEP: float = _get_env_float("HIGH_LINE_PRESSURE_STEP", 1.0)

# ---------------------------------------------------------------------------
# Ratings pré-jogo (manual por enquanto)
# ---------------------------------------------------------------------------
"""
PREMATCH_TEAM_RATINGS:
- Escala sugerida: de -2.0 a +2.0
- Foca no "quão propenso a jogo de gol" é o time / confronto.
  Ex.: ataque forte, estilo ofensivo, bola parada forte, elenco que acelera
       ⇒ nota positiva.
  Ex.: time que trava, retranca, jogo pesado, muito under
       ⇒ nota negativa.
- Se um time não estiver aqui, assume 0.0 (neutro).
"""
PREMATCH_TEAM_RATINGS: Dict[str, float] = {
    # Ajuste conforme teu faro, ex.:
    # "Nice": -0.8,
    # "Famalicão": -1.0,
}

# ---------------------------------------------------------------------------
# Estado em memória
# ---------------------------------------------------------------------------

last_status_text: str = "Ainda não foi rodada nenhuma varredura."
last_scan_origin: str = "-"
last_scan_alerts: int = 0
last_scan_live_events: int = 0
last_scan_window_matches: int = 0

# Cache de última odd real por jogo/linha (fixture_id -> (total_goals, odd))
last_odd_cache: Dict[int, Tuple[int, float]] = {}

# Cache de favorito pré-live (fixture_id -> dict)
prelive_favorite_cache: Dict[int, Dict[str, Any]] = {}

# Cache simples de último "news boost" por fixture (fixture_id -> boost)
last_news_boost_cache: Dict[int, float] = {}

# Cache de pré-jogo auto por time (chave: "league:season:team_id")
# Agora também guarda attack_gpm / defense_gpm (gols feitos/sofridos por jogo).
pregame_auto_cache: Dict[str, Dict[str, Any]] = {}

# Cooldown por jogo (fixture_id -> datetime do último alerta)
fixture_last_alert_at: Dict[int, datetime] = {}

# Caches da camada de jogadores
# fixture_id -> lista de lineups (API /fixtures/lineups)
fixture_lineups_cache: Dict[int, List[Dict[str, Any]]] = {}
# fixture_id -> {"ts": datetime, "events": [...]}
fixture_events_cache: Dict[int, Dict[str, Any]] = {}
# chave "team_id:season" -> {player_id -> rating_ofensivo}
team_player_ratings_cache: Dict[str, Dict[int, float]] = {}
team_player_ratings_ts: Dict[str, datetime] = {}

# Controle simples de consumo diário da The Odds API (aproximado, só em memória)
oddsapi_calls_today: int = 0
oddsapi_calls_date_key: str = ""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# aliases para normalizar nome de time entre APIs
TEAM_NAME_ALIASES: Dict[str, str] = {
    "wolverhampton wanderers": "wolverhampton",
    "wolverhampton": "wolverhampton",
    "wolves": "wolverhampton",
    "tottenham hotspur": "tottenham",
    "tottenham": "tottenham",
    "spurs": "tottenham",
    "paris saint germain": "paris saint germain",
    "psg": "paris saint germain",
    "paris sg": "paris saint germain",
    "manchester united": "manchester united",
    "man utd": "manchester united",
    "manchester utd": "manchester united",
    "manchester city": "manchester city",
    "man city": "manchester city",
}


def _normalize_team_name(name: str) -> str:
    """
    Normaliza nome de time para comparação entre APIs (remove "FC", acentos simples, etc.).
    Aplica também aliases para casar exemplos como "Wolverhampton" x "Wolves", "Spurs" x "Tottenham".
    """
    s = (name or "").lower()
    # tira sufixos comuns
    for token in [
        " fc",
        " cf",
        " sc",
        " afc",
        " c.f.",
        " s.c.",
        " f.c.",
        " de",
        " ac",
        " bc",
        " u19",
        " u21",
    ]:
        s = s.replace(token, " ")
    # mantém apenas letras/números/espaço
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace())
    # normaliza espaços
    s = " ".join(s.split())
    alias = TEAM_NAME_ALIASES.get(s)
    if alias:
        s = alias
    return s


def _update_last_odd_cache(fixture_id: int, total_goals: int, odd_val: float) -> None:
    """
    Atualiza cache de odd: guarda por fixture + total de gols (linha SUM_PLUS_HALF).
    Assim não reutilizamos odd de linha antiga depois que sai gol.
    """
    last_odd_cache[fixture_id] = (total_goals, odd_val)


def _get_cached_odd_for_line(fixture_id: int, total_goals: int) -> Optional[float]:
    """
    Retorna odd em cache apenas se for da MESMA linha (mesma soma de gols).
    Evita reaproveitar odd do Over 3.5 quando o jogo já virou 4x1 (linha correta 5.5).
    """
    cached = last_odd_cache.get(fixture_id)
    if not cached:
        return None
    cached_goals, cached_odd = cached
    if cached_goals == total_goals:
        return cached_odd
    return None


# ---------------------------------------------------------------------------
# Funções auxiliares do cérebro
# ---------------------------------------------------------------------------

def _safe_get_stat(stats_list: List[Dict[str, Any]], stat_type: str) -> int:
    """Extrai um valor inteiro da lista de estatísticas da API-FOOTBALL."""
    for item in stats_list:
        if item.get("type") == stat_type:
            val = item.get("value")
            if val is None:
                return 0
            try:
                return int(val)
            except (TypeError, ValueError):
                try:
                    return int(float(str(val).replace(",", ".")))
                except (TypeError, ValueError):
                    return 0
    return 0


async def _fetch_live_fixtures(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    """Busca jogos ao vivo na API-FOOTBALL, já filtrando por liga e janela."""
    if not API_FOOTBALL_KEY:
        logging.warning("API_FOOTBALL_KEY não definido; não há como buscar jogos ao vivo.")
        return []

    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params = {"live": "all"}

    try:
        resp = await client.get(
            API_FOOTBALL_BASE_URL.rstrip("/") + "/fixtures",
            headers=headers,
            params=params,
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logging.exception("Erro ao buscar fixtures na API-FOOTBALL")
        return []

    response = data.get("response") or []
    fixtures: List[Dict[str, Any]] = []

    for item in response:
        try:
            fixture = item.get("fixture") or {}
            league = item.get("league") or {}
            teams = item.get("teams") or {}
            goals = item.get("goals") or {}

            league_id_raw = league.get("id")
            if league_id_raw is None:
                continue
            league_id = int(league_id_raw)

            if LEAGUE_IDS and league_id not in LEAGUE_IDS:
                continue

            status = fixture.get("status") or {}
            short = (status.get("short") or "").upper()
            elapsed = status.get("elapsed") or 0
            if elapsed is None:
                elapsed = 0

            if elapsed < WINDOW_START or elapsed > WINDOW_END:
                continue

            if short not in ("1H", "2H"):
                continue

            league_type = league.get("type") or ""
            league_round = league.get("round") or ""

            home_team_obj = (teams.get("home") or {})
            away_team_obj = (teams.get("away") or {})

            home_team = home_team_obj.get("name") or "Home"
            away_team = away_team_obj.get("name") or "Away"
            home_team_id = home_team_obj.get("id")
            away_team_id = away_team_obj.get("id")

            home_goals = goals.get("home")
            away_goals = goals.get("away")
            if home_goals is None:
                home_goals = 0
            if away_goals is None:
                away_goals = 0

            season_raw = league.get("season")
            try:
                season = int(season_raw) if season_raw is not None else None
            except (TypeError, ValueError):
                season = None

            fixture_ts_raw = fixture.get("timestamp")
            kickoff_ts: Optional[int] = None
            try:
                if fixture_ts_raw is not None:
                    kickoff_ts = int(fixture_ts_raw)
            except (TypeError, ValueError):
                kickoff_ts = None

            fixtures.append(
                {
                    "fixture_id": int(fixture.get("id")),
                    "league_id": league_id,
                    "league_name": league.get("name") or "",
                    "league_type": league_type,
                    "league_round": league_round,
                    "season": season,
                    "minute": int(elapsed),
                    "status_short": short,
                    "home_team": home_team,
                    "away_team": away_team,
                    "home_team_id": home_team_id,
                    "away_team_id": away_team_id,
                    "home_goals": int(home_goals),
                    "away_goals": int(away_goals),
                    "kickoff_ts": kickoff_ts,
                }
            )
        except Exception:
            logging.exception("Erro ao processar item de fixture")
            continue

    return fixtures


async def _fetch_statistics_for_fixture(
    client: httpx.AsyncClient,
    fixture_id: int,
) -> Dict[str, Any]:
    """Busca estatísticas do jogo (shots, ataques, posse, etc.)."""
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params = {"fixture": fixture_id}

    try:
        resp = await client.get(
            API_FOOTBALL_BASE_URL.rstrip("/") + "/fixtures/statistics",
            headers=headers,
            params=params,
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logging.exception("Erro ao buscar estatísticas para fixture=%s", fixture_id)
        return {}

    response = data.get("response") or []
    if not response or len(response) < 2:
        return {}

    home = response[0]
    away = response[1]

    home_stats = home.get("statistics") or []
    away_stats = away.get("statistics") or []

    home_shots_total = _safe_get_stat(home_stats, "Total Shots")
    away_shots_total = _safe_get_stat(away_stats, "Total Shots")
    home_shots_on = _safe_get_stat(home_stats, "Shots on Goal")
    away_shots_on = _safe_get_stat(away_stats, "Shots on Goal")
    home_attacks = _safe_get_stat(home_stats, "Attacks")
    away_attacks = _safe_get_stat(away_stats, "Attacks")
    home_dangerous = _safe_get_stat(home_stats, "Dangerous Attacks")
    away_dangerous = _safe_get_stat(away_stats, "Dangerous Attacks")
    home_possession = _safe_get_stat(home_stats, "Ball Possession")
    away_possession = _safe_get_stat(away_stats, "Ball Possession")

    return {
        "home_shots_total": home_shots_total,
        "away_shots_total": away_shots_total,
        "home_shots_on": home_shots_on,
        "away_shots_on": away_shots_on,
        "home_attacks": home_attacks,
        "away_attacks": away_attacks,
        "home_dangerous": home_dangerous,
        "away_dangerous": away_dangerous,
        "home_possession": home_possession,
        "away_possession": away_possession,
    }


async def _fetch_live_odds_for_fixture(
    client: httpx.AsyncClient,
    fixture_id: int,
    total_goals: int,
) -> Optional[float]:
    """
    Busca odd em tempo real na API-FOOTBALL para a linha de gols do jogo.

    Lógica:
    - Usa /odds/live com filtro por fixture (odds ao vivo).
    - Procura EXCLUSIVAMENTE a linha Over (soma do placar + 0,5).
    - Se não encontrar essa linha exata em nenhum bookmaker:
        → retorna None (melhor radar calado do que EV com linha errada).
    """
    if not API_FOOTBALL_KEY or not USE_API_FOOTBALL_ODDS:
        return None

    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params: Dict[str, Any] = {
        "fixture": fixture_id,
    }
    if ODDS_BET_ID > 0:
        params["bet"] = ODDS_BET_ID

    try:
        resp = await client.get(
            API_FOOTBALL_BASE_URL.rstrip("/") + "/odds/live",
            headers=headers,
            params=params,
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logging.exception("Erro ao buscar odds LIVE para fixture=%s", fixture_id)
        return None

    response = data.get("response") or []
    if not response:
        logging.info(
            "Fixture %s: resposta de odds LIVE vazia (sem mercados) para qualquer bookmaker.",
            fixture_id,
        )
        return None

    odds_item = response[0]
    bookmakers = odds_item.get("bookmakers") or []
    if not bookmakers:
        logging.info(
            "Fixture %s: odds LIVE sem lista de bookmakers.",
            fixture_id,
        )
        return None

    candidate_bookmaker_ids: List[int] = []
    if BOOKMAKER_ID > 0:
        candidate_bookmaker_ids.append(BOOKMAKER_ID)
    for fb_id in BOOKMAKER_FALLBACK_IDS:
        if fb_id > 0 and fb_id not in candidate_bookmaker_ids:
            candidate_bookmaker_ids.append(fb_id)

    target_line = float(total_goals) + 0.5
    target_line_str = "{:.1f}".format(target_line)

    negative_tokens = (
        "corner",
        "corners",
        "card",
        "cards",
        "booking",
        "yellow",
        "red",
        "handicap",
        "asian",
        "1st half",
        "first half",
        "2nd half",
        "second half",
        "1st period",
        "second period",
        "2nd period",
        "team",
        "both teams",
        "btts",
    )

    def _extract_from_bookmaker(bm: Dict[str, Any], bm_id_label: Optional[int]) -> Optional[float]:
        """
        Tenta encontrar APENAS a odd da linha Over (total_goals + 0,5) neste bookmaker.
        Se não achar, retorna None (sem fallback de Over aleatório).

        IMPORTANTE: agora a linha é lida primeiro do texto ("Over 1.5", "Over 2.5"),
        e só depois cai para o campo handicap. Isso reduz o risco de pegar a odd
        de mercados de time (team totals, BTTS etc.).
        """
        bets = bm.get("bets") or []
        if not bets:
            return None

        available_lines: List[str] = []

        for bet in bets:
            name = (bet.get("name") or "").lower()

            # Ignora mercados que não são de gols totais do jogo
            if any(tok in name for tok in negative_tokens):
                continue
            if (
                "goal" not in name
                and "goals" not in name
                and "over/under" not in name
                and "total" not in name
            ):
                continue

            values = bet.get("values") or []
            for val in values:
                side_raw = str(val.get("value") or "").strip()
                side = side_raw.lower()
                if "over" not in side:
                    continue

                odd_raw = val.get("odd")
                if odd_raw is None:
                    continue
                try:
                    odd_val = float(str(odd_raw).replace(",", "."))
                except (TypeError, ValueError):
                    continue

                if odd_val <= 1.0:
                    continue

                # --- tenta pegar o número primeiro do texto "Over X.Y" ---
                line_num: Optional[float] = None

                for token in side_raw.replace(",", ".").split():
                    try:
                        line_num = float(token)
                        break
                    except Exception:
                        continue

                # fallback: usar handicap se ainda não achou nada
                if line_num is None:
                    handicap_raw = val.get("handicap")
                    if handicap_raw is not None:
                        try:
                            line_num = float(str(handicap_raw).replace(",", "."))
                        except Exception:
                            line_num = None

                if line_num is None:
                    continue

                line_str = "{:.1f}".format(line_num)
                if line_str not in available_lines:
                    available_lines.append(line_str)

                # Só aceitamos se for EXATAMENTE a linha alvo (soma do placar + 0,5)
                if line_str == target_line_str:
                    logging.info(
                        "Fixture %s: bookmaker %s → Over %s (target %s) @ %.3f (API-FOOTBALL).",
                        fixture_id,
                        bm_id_label,
                        line_str,
                        target_line_str,
                        odd_val,
                    )
                    return odd_val

        if available_lines:
            logging.info(
                "Fixture %s: bookmaker %s sem Over %s. Linhas Over encontradas (API-FOOTBALL): %s",
                fixture_id,
                bm_id_label,
                target_line_str,
                ", ".join(sorted(available_lines)),
            )

        return None

    # 1) Tenta BOOKMAKER_ID e fallbacks na ordem
    for bm_id in candidate_bookmaker_ids:
        bm = None
        for b in bookmakers:
            b_id_raw = b.get("id")
            try:
                if b_id_raw is not None and int(b_id_raw) == bm_id:
                    bm = b
                    break
            except Exception:
                continue

        if bm is None:
            continue

        odd_val = _extract_from_bookmaker(bm, bm_id)
        if odd_val is not None:
            return odd_val

    # 2) Fallback: tenta qualquer bookmaker que tenha exatamente a linha alvo
    for b in bookmakers:
        b_id_raw = b.get("id")
        try:
            b_id = int(b_id_raw) if b_id_raw is not None else None
        except Exception:
            b_id = None

        odd_val = _extract_from_bookmaker(b, b_id)
        if odd_val is not None:
            return odd_val

    logging.info(
        "Fixture %s: odds LIVE presentes (API-FOOTBALL), mas nenhuma seleção Over %s encontrada em nenhum bookmaker.",
        fixture_id,
        target_line_str,
    )
    return None


# ---------------------------------------------------------------------------
# Helper para The Odds API: linha SUM_PLUS_HALF
# ---------------------------------------------------------------------------

def _pick_totals_over_sum_plus_half_from_the_odds_api(
    events: List[Dict[str, Any]],
    fixture_id: int,
    home_goals: int,
    away_goals: int,
) -> Optional[Tuple[float, float, str]]:
    """
    Procura na resposta da The Odds API a linha de 'totals' correspondente a
    SUM_PLUS_HALF (gols atuais + 0,5) e retorna (linha, odd, bookmaker_name).

    NÃO filtra por MIN_ODD/MAX_ODD aqui, pra permitir alerta de observação
    quando a odd estiver abaixo da mínima (watch).
    """
    target_line = float(home_goals + away_goals) + 0.5
    target_line = float(f"{target_line:.1f}")

    best_price: float = 0.0
    best_book: str = ""
    best_line: float = target_line

    for ev in events:
        bookmakers = ev.get("bookmakers") or []
        for bk in bookmakers:
            book_name = bk.get("title") or bk.get("key") or "desconhecido"
            markets = bk.get("markets") or []
            for market in markets:
                if (market.get("key") or "").lower() != "totals":
                    continue

                outcomes = market.get("outcomes") or []
                available_points = set()

                # coleta todas as linhas numéricas deste bookmaker (pra log)
                for oc in outcomes:
                    pt_raw = oc.get("point")
                    if pt_raw is None:
                        continue
                    try:
                        pt_val = float(str(pt_raw).replace(",", "."))
                        available_points.add(pt_val)
                    except (TypeError, ValueError):
                        continue

                # agora tenta achar exatamente Over target_line
                for oc in outcomes:
                    name = (oc.get("name") or "").lower()
                    if "over" not in name:
                        continue

                    price_raw = oc.get("price")
                    if price_raw is None:
                        continue
                    try:
                        price_val = float(str(price_raw).replace(",", "."))
                    except (TypeError, ValueError):
                        continue
                    if price_val <= 1.0:
                        continue

                    point_raw = oc.get("point")
                    if point_raw is None:
                        continue
                    try:
                        point_val = float(str(point_raw).replace(",", "."))
                    except (TypeError, ValueError):
                        continue

                    if abs(point_val - target_line) > 1e-6:
                        continue

                    # pega sempre a MAIOR odd dessa linha entre os bookmakers
                    if price_val > best_price:
                        best_price = price_val
                        best_book = book_name
                        best_line = point_val

                if available_points:
                    points_str = ", ".join(
                        f"{p:.1f}" for p in sorted(available_points)
                    )
                    logging.info(
                        "The Odds API: fixture %s, bk=%s, linhas totals disponíveis: %s (buscando Over %.1f)",
                        fixture_id,
                        book_name,
                        points_str,
                        target_line,
                    )

    if best_price > 0.0:
        logging.info(
            "The Odds API: fixture %s, selecionado Over %.1f @ %.2f (%s)",
            fixture_id,
            best_line,
            best_price,
            best_book,
        )
        return best_line, best_price, best_book

    logging.info(
        "The Odds API: nenhuma seleção Over %.1f encontrada para fixture=%s em nenhum bookmaker.",
        target_line,
        fixture_id,
    )
    return None

# ---------------------------------------------------------------------------
# Favorito pré-live via odds (API-FOOTBALL /odds)
# ---------------------------------------------------------------------------

def _favorite_strength_from_odd(odd: Optional[float]) -> int:
    """Retorna 0/1/2/3 (nenhum / leve / forte / super) a partir da odd pré-live."""
    if odd is None:
        return 0
    try:
        o = float(odd)
    except (TypeError, ValueError):
        return 0
    if o <= PRELIVE_SUPER_MAX_ODD:
        return 3
    if o <= PRELIVE_STRONG_MAX_ODD:
        return 2
    # acima disso, pode ser favorito leve (ou jogo equilibrado)
    return 1


async def _fetch_prelive_match_winner_odds_api_football(
    client: httpx.AsyncClient,
    fixture_id: int,
    home_team: str,
    away_team: str,
) -> Optional[Dict[str, Optional[float]]]:
    """Busca odds pré-jogo 1x2 (casa/empate/fora) na API-FOOTBALL (/odds).

    Observação:
    - Nem sempre o bookmaker/mercado vem completo; quando não achar 1x2, retorna None.
    - Mantém robusto (melhor sem favorito do que favorito errado).
    """
    if not API_FOOTBALL_KEY:
        return None

    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params: Dict[str, Any] = {"fixture": int(fixture_id)}

    # se definido, tenta forçar bookmaker (nem sempre disponível para todos)
    if PRELIVE_ODDS_BOOKMAKER_ID and PRELIVE_ODDS_BOOKMAKER_ID > 0:
        params["bookmaker"] = int(PRELIVE_ODDS_BOOKMAKER_ID)

    try:
        resp = await client.get(
            API_FOOTBALL_BASE_URL.rstrip("/") + "/odds",
            headers=headers,
            params=params,
            timeout=HTTP_TIMEOUT,
        )
        data = resp.json()
    except Exception:
        logging.exception("Erro ao buscar odds pré-live (API-FOOTBALL) fixture=%s", fixture_id)
        return None

    try:
        items = data.get("response") or []
        if not items:
            return None

        # alguns retornos vêm em lista; escolhe o primeiro item
        item0 = items[0] if isinstance(items, list) else items
        bookmakers = item0.get("bookmakers") or []
        if not bookmakers:
            return None

        # helper normalizado para comparar valores
        hn = _normalize_team_name(home_team)
        an = _normalize_team_name(away_team)

        home_odd: Optional[float] = None
        draw_odd: Optional[float] = None
        away_odd: Optional[float] = None

        for bm in bookmakers:
            bets = bm.get("bets") or []
            for bet in bets:
                bet_name = str(bet.get("name") or "").lower()
                # tenta achar mercado de vencedor (1x2)
                if not any(k in bet_name for k in ["match winner", "winner", "1x2", "fulltime result", "result"]):
                    continue

                values = bet.get("values") or []
                for v in values:
                    label = str(v.get("value") or "").strip()
                    label_l = label.lower()
                    try:
                        odd = float(v.get("odd"))
                    except (TypeError, ValueError):
                        continue

                    # mapeia entradas comuns
                    if label_l in ("home", "1"):
                        home_odd = odd
                    elif label_l in ("away", "2"):
                        away_odd = odd
                    elif label_l in ("draw", "x"):
                        draw_odd = odd
                    else:
                        # compara por nome do time
                        ln = _normalize_team_name(label)
                        if ln and ln == hn:
                            home_odd = odd
                        elif ln and ln == an:
                            away_odd = odd

                # se já temos casa e fora, já dá para detectar favorito
                if home_odd is not None and away_odd is not None:
                    return {"home": home_odd, "draw": draw_odd, "away": away_odd}

        return None
    except Exception:
        logging.exception("Erro ao parsear odds pré-live fixture=%s", fixture_id)
        return None


async def _ensure_prelive_favorite(
    client: httpx.AsyncClient,
    fixture: Dict[str, Any],
) -> None:
    """Preenche fixture com favorito pré-live, com cache."""
    try:
        fixture_id = int(fixture.get("fixture_id") or 0)
    except (TypeError, ValueError):
        return
    if fixture_id <= 0:
        return
    if not USE_PRELIVE_FAVORITE:
        return

    now = datetime.utcnow()
    cached = prelive_favorite_cache.get(fixture_id)
    if cached:
        ts = cached.get("ts")
        if isinstance(ts, datetime) and (now - ts) <= timedelta(hours=PRELIVE_CACHE_HOURS):
            # reaplica no fixture
            for k, v in cached.items():
                if k != "ts":
                    fixture[k] = v
            return

    home_team = str(fixture.get("home_team") or "")
    away_team = str(fixture.get("away_team") or "")

    odds = await _fetch_prelive_match_winner_odds_api_football(client, fixture_id, home_team, away_team)
    if not odds:
        # não achou; deixa sem favorito (fallback por rating será usado)
        prelive_favorite_cache[fixture_id] = {"ts": now, "favorite_side": None, "favorite_odd": None, "favorite_strength": 0}
        return

    home_odd = odds.get("home")
    draw_odd = odds.get("draw")
    away_odd = odds.get("away")

    fav_side: Optional[str] = None
    fav_odd: Optional[float] = None
    try:
        if home_odd is not None and away_odd is not None:
            if float(home_odd) < float(away_odd):
                fav_side = "home"
                fav_odd = float(home_odd)
            elif float(away_odd) < float(home_odd):
                fav_side = "away"
                fav_odd = float(away_odd)
            else:
                fav_side = None
                fav_odd = None
    except Exception:
        fav_side, fav_odd = None, None

    fav_strength = _favorite_strength_from_odd(fav_odd)

    cache_payload: Dict[str, Any] = {
        "ts": now,
        "prelive_home_odd": home_odd,
        "prelive_draw_odd": draw_odd,
        "prelive_away_odd": away_odd,
        "favorite_side": fav_side,
        "favorite_odd": fav_odd,
        "favorite_strength": fav_strength,
    }
    prelive_favorite_cache[fixture_id] = cache_payload

    for k, v in cache_payload.items():
        if k != "ts":
            fixture[k] = v


async def _fetch_live_odds_for_fixture_odds_api(
    client: httpx.AsyncClient,
    fixture: Dict[str, Any],
    total_goals: int,
) -> Optional[float]:
    """
    Busca odd em tempo real via The Odds API para a linha Over (soma do placar + 0,5).
    Usa:
      GET /v4/sports/{sport_key}/odds?regions=...&markets=totals&oddsFormat=decimal
    e casa o evento pelo par (home_team, away_team) + horário (kickoff).
    """
    if not ODDS_API_KEY or not ODDS_API_USE:
        return None

    # Controle de limite diário (aproximado, só em memória)
    global oddsapi_calls_today, oddsapi_calls_date_key

    today_key = _now_utc().strftime("%Y-%m-%d")
    if oddsapi_calls_date_key != today_key:
        # Mudou o dia (UTC) → reseta contador
        oddsapi_calls_date_key = today_key
        oddsapi_calls_today = 0

    if ODDS_API_DAILY_LIMIT > 0 and oddsapi_calls_today >= ODDS_API_DAILY_LIMIT:
        logging.info(
            "The Odds API: limite diário de chamadas atingido (%s); pulando fixture=%s",
            ODDS_API_DAILY_LIMIT,
            fixture.get("fixture_id"),
        )
        return None

    league_id = fixture.get("league_id")
    sport_key = None

    if league_id is not None:
        sport_key = ODDS_API_LEAGUE_MAP.get(int(league_id))

    if not sport_key:
        sport_key = ODDS_API_DEFAULT_SPORT_KEY

    if not sport_key:
        # sem sport_key, não dá para chamar The Odds API
        return None

    target_line = float(total_goals) + 0.5
    target_line_str = "{:.1f}".format(target_line)

    params = {
        "apiKey": ODDS_API_KEY,
        "regions": ODDS_API_REGIONS,
        "markets": ODDS_API_MARKETS or "totals",
        "oddsFormat": "decimal",
    }

    url = "{base}/sports/{sport_key}/odds".format(
        base=ODDS_API_BASE_URL.rstrip("/"),
        sport_key=sport_key,
    )

    try:
        resp = await client.get(
            url,
            params=params,
            timeout=10.0,
        )

        # contamos essa chamada no limite diário
        oddsapi_calls_today += 1

        # (opcional) loga o header de créditos restantes se a API informar
        remaining = (
            resp.headers.get("x-requests-remaining")
            or resp.headers.get("X-Requests-Remaining")
        )
        if remaining is not None:
            logging.info(
                "The Odds API: chamadas restantes reportadas pelo provedor: %s",
                remaining,
            )

        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logging.exception(
            "Erro ao buscar odds na The Odds API para sport_key=%s (fixture=%s)",
            sport_key,
            fixture.get("fixture_id"),
        )
        return None

    events = data or []
    if not isinstance(events, list) or not events:
        logging.info(
            "The Odds API retornou lista vazia de eventos para sport_key=%s",
            sport_key,
        )
        return None

    home_name_norm = _normalize_team_name(fixture.get("home_team") or "")
    away_name_norm = _normalize_team_name(fixture.get("away_team") or "")

    fixture_ts = fixture.get("kickoff_ts")
    fixture_dt: Optional[datetime] = None
    if fixture_ts is not None:
        try:
            fixture_dt = datetime.fromtimestamp(int(fixture_ts), tz=timezone.utc)
        except Exception:
            fixture_dt = None

    def _parse_commence_time(ev: Dict[str, Any]) -> Optional[datetime]:
        ct_raw = ev.get("commence_time")
        if not ct_raw:
            return None
        try:
            ct_str = str(ct_raw)
            if ct_str.endswith("Z"):
                ct_str = ct_str.replace("Z", "+00:00")
            return datetime.fromisoformat(ct_str)
        except Exception:
            return None

    def _names_match(a: str, b: str) -> bool:
        """
        Casa nomes tolerando variações tipo:
        - 'Twente' vs 'FC Twente Enschede'
        - 'AZ' vs 'AZ Alkmaar'
        - 'Wolves' vs 'Wolverhampton Wanderers'
        """
        a = a.strip()
        b = b.strip()
        if not a or not b:
            return False

        if a == b:
            return True

        # prefixo/sufixo simples
        if a in b or b in a:
            return True

        a_tokens_all = [t for t in a.split() if t]
        b_tokens_all = [t for t in b.split() if t]

        a_tokens = set(t for t in a_tokens_all if len(t) >= 3)
        b_tokens = set(t for t in b_tokens_all if len(t) >= 3)
        if len(a_tokens & b_tokens) >= 1:
            return True

        # novo: checa prefixo de 3–4 letras em tokens
        for ta in a_tokens:
            for tb in b_tokens:
                n = min(len(ta), len(tb), 4)
                if n >= 3 and ta[:n] == tb[:n]:
                    return True

        return False

    direct_candidates: List[Tuple[Dict[str, Any], float]] = []
    swap_candidates: List[Tuple[Dict[str, Any], float]] = []

    for ev in events:
        ev_home = _normalize_team_name(str(ev.get("home_team") or ""))
        ev_away = _normalize_team_name(str(ev.get("away_team") or ""))

        ev_dt = _parse_commence_time(ev)
        if fixture_dt is not None and ev_dt is not None:
            diff_min = abs((ev_dt - fixture_dt).total_seconds()) / 60.0
        else:
            diff_min = 999999.0

        # home/away normal
        if _names_match(ev_home, home_name_norm) and _names_match(ev_away, away_name_norm):
            direct_candidates.append((ev, diff_min))
            continue

        # fallback invertido (por segurança)
        if _names_match(ev_home, away_name_norm) and _names_match(ev_away, home_name_norm):
            swap_candidates.append((ev, diff_min))
            continue

    matched_event: Optional[Dict[str, Any]] = None

    chosen_diff = None
    if direct_candidates:
        ev_best, diff_best = min(direct_candidates, key=lambda t: t[1])
        # se tiver horário confiável, rejeita se for muito distante (>4h)
        if diff_best <= 240.0 or diff_best == 999999.0:
            matched_event = ev_best
            chosen_diff = diff_best
    elif swap_candidates:
        ev_best, diff_best = min(swap_candidates, key=lambda t: t[1])
        if diff_best <= 240.0 or diff_best == 999999.0:
            matched_event = ev_best
            chosen_diff = diff_best

    if matched_event is None:
        logging.info(
            "The Odds API: nenhum evento casou com %s vs %s em sport_key=%s",
            fixture.get("home_team"),
            fixture.get("away_team"),
            sport_key,
        )
        return None

    if chosen_diff is not None and chosen_diff != 999999.0:
        logging.info(
            "The Odds API: evento casado para fixture=%s (diferença de horário ~%.1f min, sport_key=%s)",
            fixture.get("fixture_id"),
            chosen_diff,
            sport_key,
        )
    else:
        logging.info(
            "The Odds API: evento casado para fixture=%s (sem comparação confiável de horário, sport_key=%s)",
            fixture.get("fixture_id"),
            sport_key,
        )

    bookmakers = matched_event.get("bookmakers") or []
    if not bookmakers:
        logging.info(
            "The Odds API: evento casado mas sem bookmakers para fixture=%s",
            fixture.get("fixture_id"),
        )
        return None

    # Ordena bookmakers pela preferência configurada, se houver
    ordered_bookmakers: List[Dict[str, Any]] = []
    used_indices = set()

    if ODDS_API_BOOKMAKERS:
        for pref_key in ODDS_API_BOOKMAKERS:
            for idx, bk in enumerate(bookmakers):
                if idx in used_indices:
                    continue
                if (bk.get("key") or "").lower() == pref_key.lower():
                    ordered_bookmakers.append(bk)
                    used_indices.add(idx)
                    break

    # adiciona o restante que não entrou na preferência
    for idx, bk in enumerate(bookmakers):
        if idx not in used_indices:
            ordered_bookmakers.append(bk)

    matched_event["bookmakers"] = ordered_bookmakers

    # Usa helper centralizado para pegar a linha SUM_PLUS_HALF
    home_goals = fixture.get("home_goals") or 0
    away_goals = fixture.get("away_goals") or 0

    picked = _pick_totals_over_sum_plus_half_from_the_odds_api(
        events=[matched_event],
        fixture_id=fixture.get("fixture_id") or 0,
        home_goals=home_goals,
        away_goals=away_goals,
    )

    if picked is not None:
        total_line, price_val, book_name = picked
        # só por garantia, confere se é mesmo a linha alvo esperada
        line_str = "{:.1f}".format(total_line)
        if line_str != target_line_str:
            logging.info(
                "The Odds API: linha retornada (%.1f) difere da SUM_PLUS_HALF esperada (%.1f) para fixture=%s; descartando.",
                total_line,
                target_line,
                fixture.get("fixture_id"),
            )
            return None
        return price_val

    # Se helper não achou nada, já logou; só reforça
    logging.info(
        "The Odds API: nenhuma seleção Over %s encontrada para fixture=%s em nenhum bookmaker.",
        target_line_str,
        fixture.get("fixture_id"),
    )
    return None


# ---------------------------------------------------------------------------
# Camada de jogadores: lineups, eventos e ratings
# ---------------------------------------------------------------------------

async def _fetch_lineups_for_fixture(
    client: httpx.AsyncClient,
    fixture_id: int,
) -> List[Dict[str, Any]]:
    """Busca lineups do jogo (XI inicial + banco). Usa cache por fixture."""
    if not API_FOOTBALL_KEY or not USE_PLAYER_IMPACT:
        return []

    cached = fixture_lineups_cache.get(fixture_id)
    if cached is not None:
        return cached

    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params = {"fixture": fixture_id}

    try:
        resp = await client.get(
            API_FOOTBALL_BASE_URL.rstrip("/") + "/fixtures/lineups",
            headers=headers,
            params=params,
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logging.exception("Erro ao buscar lineups para fixture=%s", fixture_id)
        fixture_lineups_cache[fixture_id] = []
        return []

    response = data.get("response") or []
    fixture_lineups_cache[fixture_id] = response
    return response


async def _fetch_events_for_fixture(
    client: httpx.AsyncClient,
    fixture_id: int,
) -> List[Dict[str, Any]]:
    """
    Busca eventos do jogo (incluindo substituições), com cache de poucos minutos.
    """
    if not API_FOOTBALL_KEY or not USE_PLAYER_IMPACT:
        return []

    now = _now_utc()
    cached = fixture_events_cache.get(fixture_id)
    if cached is not None:
        ts = cached.get("ts")
        if isinstance(ts, datetime):
            if (now - ts) <= timedelta(minutes=PLAYER_EVENTS_CACHE_MINUTES):
                events_cached = cached.get("events") or []
                return events_cached

    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params = {"fixture": fixture_id}

    try:
        resp = await client.get(
            API_FOOTBALL_BASE_URL.rstrip("/") + "/fixtures/events",
            headers=headers,
            params=params,
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logging.exception("Erro ao buscar eventos para fixture=%s", fixture_id)
        fixture_events_cache[fixture_id] = {"ts": now, "events": []}
        return []

    response = data.get("response") or []
    fixture_events_cache[fixture_id] = {"ts": now, "events": response}
    return response


async def _ensure_team_player_ratings(
    client: httpx.AsyncClient,
    team_id: Optional[int],
    season: Optional[int],
) -> Dict[int, float]:
    """
    Garante um mapa {player_id -> rating_ofensivo} para (time, temporada)
    usando /players da API-FOOTBALL, com cache em memória.

    Rating aproximado:
    - goals_per90 (peso maior)
    - shots_on_target_per90
    - total_shots_per90
    """
    if not API_FOOTBALL_KEY or not USE_PLAYER_IMPACT:
        return {}

    if team_id is None or season is None:
        return {}

    key = "{tm}:{ss}".format(tm=team_id, ss=season)
    now = _now_utc()

    cached = team_player_ratings_cache.get(key)
    ts = team_player_ratings_ts.get(key)
    if cached is not None and ts is not None:
        if (now - ts) <= timedelta(hours=PLAYER_STATS_CACHE_HOURS):
            return cached

    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    ratings: Dict[int, float] = {}

    page = 1
    while True:
        params = {
            "team": team_id,
            "season": season,
            "page": page,
        }
        try:
            resp = await client.get(
                API_FOOTBALL_BASE_URL.rstrip("/") + "/players",
                headers=headers,
                params=params,
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logging.exception(
                "Erro ao buscar stats de jogadores para team=%s season=%s (page=%s)",
                team_id,
                season,
                page,
            )
            break

        response = data.get("response") or []
        if not response:
            break

        for item in response:
            try:
                player_info = item.get("player") or {}
                player_id = player_info.get("id")
                if player_id is None:
                    continue
                try:
                    pid_int = int(player_id)
                except Exception:
                    continue

                stats_list = item.get("statistics") or []
                if not stats_list:
                    continue
                st = stats_list[0] or {}

                games = st.get("games") or {}
                minutes = games.get("minutes") or 0

                goals_info = st.get("goals") or {}
                goals_total = goals_info.get("total") or 0

                shots_info = st.get("shots") or {}
                shots_total = shots_info.get("total") or 0
                shots_on = shots_info.get("on") or 0

                try:
                    minutes = int(minutes or 0)
                except (TypeError, ValueError):
                    minutes = 0

                try:
                    goals_total = int(goals_total or 0)
                except (TypeError, ValueError):
                    goals_total = 0

                try:
                    shots_total = int(shots_total or 0)
                except (TypeError, ValueError):
                    shots_total = 0

                try:
                    shots_on = int(shots_on or 0)
                except (TypeError, ValueError):
                    shots_on = 0

                if minutes <= 0:
                    rating = 0.0
                else:
                    m = float(minutes)
                    goals_per90 = (goals_total * 90.0) / m
                    shots_total_per90 = (shots_total * 90.0) / m
                    shots_on_per90 = (shots_on * 90.0) / m

                    rating = (
                        goals_per90 * 1.8
                        + shots_on_per90 * 0.9
                        + shots_total_per90 * 0.3
                    )

                    if rating < 0.0:
                        rating = 0.0
                    if rating > 4.0:
                        rating = 4.0

                ratings[pid_int] = rating
            except Exception:
                logging.exception("Erro ao processar stats de jogador (team=%s)", team_id)
                continue

        paging = data.get("paging") or {}
        total_pages = paging.get("total") or 1
        try:
            total_pages = int(total_pages or 1)
        except Exception:
            total_pages = 1

        if page >= total_pages or page >= 2:
            break
        page += 1

    team_player_ratings_cache[key] = ratings
    team_player_ratings_ts[key] = now

    return ratings


async def _compute_player_boost_for_fixture(
    client: httpx.AsyncClient,
    fixture: Dict[str, Any],
) -> float:
    """
    Calcula um boost de probabilidade baseado:
    - na "força ofensiva" do XI em campo vs XI inicial
    - na troca de jogadores recentes (substituições nos últimos N minutos)

    Saída em delta de probabilidade (ex.: +0.03 = +3pp), clampado por
    PLAYER_MAX_BOOST_PCT.
    """
    if not USE_PLAYER_IMPACT:
        return 0.0

    fixture_id = fixture.get("fixture_id")
    if fixture_id is None:
        return 0.0

    minute = fixture.get("minute") or 0
    try:
        minute_int = int(minute)
    except (TypeError, ValueError):
        minute_int = 0

    league_id = fixture.get("league_id")
    season = fixture.get("season")
    home_team_id = fixture.get("home_team_id")
    away_team_id = fixture.get("away_team_id")

    if home_team_id is None or away_team_id is None or season is None:
        return 0.0

    lineups = await _fetch_lineups_for_fixture(client, fixture_id)
    if not lineups:
        return 0.0

    start_on_field: Dict[int, set] = {}
    on_field: Dict[int, set] = {}

    for lu in lineups:
        team_info = lu.get("team") or {}
        t_id = team_info.get("id")
        if t_id is None:
            continue
        try:
            t_id_int = int(t_id)
        except Exception:
            continue

        start_set = set()
        start_list = lu.get("startXI") or []
        for p in start_list:
            pinfo = p.get("player") or {}
            pid = pinfo.get("id")
            if pid is None:
                continue
            try:
                pid_int = int(pid)
            except Exception:
                continue
            start_set.add(pid_int)

        if start_set:
            start_on_field[t_id_int] = set(start_set)
            on_field[t_id_int] = set(start_set)

    if home_team_id not in on_field or away_team_id not in on_field:
        return 0.0

    events = await _fetch_events_for_fixture(client, fixture_id)
    recent_subs: List[tuple] = []

    for ev in events:
        ev_type = (ev.get("type") or "").lower()
        if "subst" not in ev_type:
            continue

        team_info = ev.get("team") or {}
        t_id = team_info.get("id")
        if t_id is None:
            continue
        try:
            t_id_int = int(t_id)
        except Exception:
            continue

        if t_id_int not in on_field:
            continue

        time_info = ev.get("time") or {}
        ev_min_raw = time_info.get("elapsed")
        try:
            ev_min = int(ev_min_raw) if ev_min_raw is not None else None
        except (TypeError, ValueError):
            ev_min = None

        player_out_obj = ev.get("player") or {}
        player_in_obj = ev.get("assist") or {}

        pid_out_raw = player_out_obj.get("id")
        pid_in_raw = player_in_obj.get("id")

        pid_out_int: Optional[int] = None
        pid_in_int: Optional[int] = None

        if pid_out_raw is not None:
            try:
                pid_out_int = int(pid_out_raw)
            except Exception:
                pid_out_int = None

        if pid_in_raw is not None:
            try:
                pid_in_int = int(pid_in_raw)
            except Exception:
                pid_in_int = None

        if pid_out_int is not None and pid_out_int in on_field[t_id_int]:
            on_field[t_id_int].discard(pid_out_int)
        if pid_in_int is not None:
            on_field[t_id_int].add(pid_in_int)

        if ev_min is not None and minute_int:
            diff = minute_int - ev_min
            if diff >= 0 and diff <= PLAYER_SUB_TRIGGER_WINDOW:
                if pid_out_int is not None and pid_in_int is not None:
                    recent_subs.append((t_id_int, pid_out_int, pid_in_int))

    home_ratings = await _ensure_team_player_ratings(client, home_team_id, season)
    away_ratings = await _ensure_team_player_ratings(client, away_team_id, season)

    def _sum_ratings(ids_set: set, ratings_map: Dict[int, float]) -> float:
        total = 0.0
        for pid in ids_set:
            r = ratings_map.get(pid)
            if r is not None:
                total += float(r)
        return total

    home_start_ids = start_on_field.get(home_team_id, set())
    away_start_ids = start_on_field.get(away_team_id, set())
    home_current_ids = on_field.get(home_team_id, set())
    away_current_ids = on_field.get(away_team_id, set())

    if not home_ratings and not away_ratings:
        return 0.0

    home_start_attack = _sum_ratings(home_start_ids, home_ratings)
    away_start_attack = _sum_ratings(away_start_ids, away_ratings)
    home_current_attack = _sum_ratings(home_current_ids, home_ratings)
    away_current_attack = _sum_ratings(away_current_ids, away_ratings)

    attack_start_total = home_start_attack + away_start_attack
    attack_current_total = home_current_attack + away_current_attack

    if attack_start_total <= 0.0:
        attack_start_total = attack_current_total if attack_current_total > 0 else 1.0

    ratio = attack_current_total / attack_start_total
    main_boost = (ratio - 1.0) * 0.04

    if main_boost > 0.04:
        main_boost = 0.04
    if main_boost < -0.03:
        main_boost = -0.03

    delta_recent_total = 0.0
    for t_id_int, pid_out_int, pid_in_int in recent_subs:
        ratings_map = home_ratings if t_id_int == home_team_id else away_ratings
        r_out = ratings_map.get(pid_out_int, 0.0)
        r_in = ratings_map.get(pid_in_int, 0.0)
        delta_recent_total += (r_in - r_out)

    sub_boost = 0.0
    if delta_recent_total != 0.0 and attack_start_total > 0:
        sub_boost = (delta_recent_total / attack_start_total) * 0.05
        if sub_boost > 0.04:
            sub_boost = 0.04
        if sub_boost < -0.03:
            sub_boost = -0.03

    boost = main_boost + sub_boost
    max_abs = PLAYER_MAX_BOOST_PCT / 100.0
    if boost > max_abs:
        boost = max_abs
    if boost < -max_abs:
        boost = -max_abs

    return boost


# ---------------------------------------------------------------------------
# News boost (heurística simples usando NewsAPI)
# ---------------------------------------------------------------------------

_POSITIVE_KEYWORDS = [
    "back from injury",
    "returns",
    "returning",
    "fit to play",
    "star striker",
    "must win",
    "decisive match",
    "title race",
    "relegation battle",
    "home crowd",
    "sold out",
    "full stadium",
    "coach praises attack",
    "goal spree",
]

_NEGATIVE_KEYWORDS = [
    "injured",
    "out for season",
    "suspension",
    "suspended",
    "defensive approach",
    "park the bus",
    "missing key players",
    "fatigue",
    "tired legs",
    "rotation",
    "resting starters",
    "heavy pitch",
    "bad weather",
]


async def _fetch_news_boost_for_fixture(
    client: httpx.AsyncClient,
    fixture: Dict[str, Any],
) -> float:
    """
    Busca notícias recentes sobre os times e retorna um "boost" de probabilidade:
    - Resultado em delta de probabilidade (ex.: +0.02 = +2pp)
    - Intervalo típico: ~[-0.02, +0.03]
    """
    if not NEWS_API_KEY or not USE_NEWS_API:
        return 0.0

    fixture_id = fixture.get("fixture_id")
    if fixture_id in last_news_boost_cache:
        return last_news_boost_cache[fixture_id]

    home = fixture.get("home_team") or ""
    away = fixture.get("away_team") or ""

    query = '"{home}" OR "{away}"'.format(home=home, away=away)

    now = _now_utc()
    from_dt = now - timedelta(hours=NEWS_TIME_WINDOW_HOURS)
    from_param = from_dt.isoformat(timespec="seconds").replace("+00:00", "Z")

    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": 20,
        "from": from_param,
        "apiKey": NEWS_API_KEY,
    }

    try:
        resp = await client.get(
            "https://newsapi.org/v2/everything",
            params=params,
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logging.exception("Erro ao buscar notícias para fixture=%s", fixture_id)
        last_news_boost_cache[fixture_id] = 0.0
        return 0.0

    articles = data.get("articles") or []
    if not articles:
        last_news_boost_cache[fixture_id] = 0.0
        return 0.0

    score = 0

    for art in articles:
        text_parts = [
            art.get("title") or "",
            art.get("description") or "",
            art.get("content") or "",
        ]
        text = " ".join(text_parts).lower()

        for kw in _POSITIVE_KEYWORDS:
            if kw.lower() in text:
                score += 1

        for kw in _NEGATIVE_KEYWORDS:
            if kw.lower() in text:
                score -= 1

    boost = 0.0
    if score >= 3:
        boost = 0.03
    elif score == 2:
        boost = 0.02
    elif score == 1:
        boost = 0.01
    elif score == 0:
        boost = 0.0
    elif score == -1:
        boost = -0.01
    elif score <= -2:
        boost = -0.02

    last_news_boost_cache[fixture_id] = boost
    return boost


# ---------------------------------------------------------------------------
# Pré-jogo boost (manual + automático + contexto de placar/favorito)
# ---------------------------------------------------------------------------

def _get_pregame_boost_manual(fixture: Dict[str, Any]) -> float:
    """
    Converte ratings pré-jogo manuais dos times em ajuste de probabilidade.
    """
    home = fixture.get("home_team") or ""
    away = fixture.get("away_team") or ""

    rh = PREMATCH_TEAM_RATINGS.get(home, 0.0)
    ra = PREMATCH_TEAM_RATINGS.get(away, 0.0)

    avg = (rh + ra) / 2.0
    boost = avg * 0.01
    if boost > 0.02:
        boost = 0.02
    if boost < -0.02:
        boost = -0.02
    return boost


async def _get_team_auto_rating(
    client: httpx.AsyncClient,
    team_id: Optional[int],
    league_id: Optional[int],
    season: Optional[int],
) -> float:
    """
    Rating em [-2, +2] indicando quão "golento" o time costuma ser.
    Também preenche no cache:
        attack_gpm  = gols marcados por jogo
        defense_gpm = gols sofridos por jogo
    """
    if not API_FOOTBALL_KEY or not USE_API_PREGAME:
        return 0.0

    if team_id is None or league_id is None or season is None:
        return 0.0

    cache_key = "{lg}:{ss}:{tm}".format(lg=league_id, ss=season, tm=team_id)
    now = _now_utc()

    cached = pregame_auto_cache.get(cache_key)
    if cached:
        ts = cached.get("ts")
        rating_cached = float(cached.get("rating", 0.0))
        if isinstance(ts, datetime):
            if (now - ts) <= timedelta(hours=PREGAME_CACHE_HOURS):
                return rating_cached

    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params = {
        "team": team_id,
        "league": league_id,
        "season": season,
    }

    try:
        resp = await client.get(
            API_FOOTBALL_BASE_URL.rstrip("/") + "/teams/statistics",
            headers=headers,
            params=params,
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logging.exception(
            "Erro ao buscar estatísticas de time (pré-jogo) team=%s league=%s season=%s",
            team_id,
            league_id,
            season,
        )
        pregame_auto_cache[cache_key] = {
            "rating": 0.0,
            "ts": now,
            "attack_gpm": 0.0,
            "defense_gpm": 0.0,
        }
        return 0.0

    stats = data.get("response") or {}
    if not stats:
        pregame_auto_cache[cache_key] = {
            "rating": 0.0,
            "ts": now,
            "attack_gpm": 0.0,
            "defense_gpm": 0.0,
        }
        return 0.0

    rating = 0.0

    fixtures_info = stats.get("fixtures") or {}
    played_total = ((fixtures_info.get("played") or {}).get("total")) or 0

    goals_info = stats.get("goals") or {}
    gf_total = (
        ((goals_info.get("for") or {}).get("total") or {}).get("total", 0) or 0
    )
    ga_total = (
        ((goals_info.get("against") or {}).get("total") or {}).get("total", 0) or 0
    )

    try:
        played_total_int = int(played_total or 0)
    except Exception:
        played_total_int = 0

    gf_per = 0.0
    ga_per = 0.0
    gpm = 0.0
    if played_total_int > 0:
        gf_per = gf_total / float(played_total_int)
        ga_per = ga_total / float(played_total_int)
        gpm = (gf_total + ga_total) / float(played_total_int)

    if gpm >= 3.2:
        rating += 1.2
    elif gpm >= 2.8:
        rating += 0.9
    elif gpm >= 2.4:
        rating += 0.6
    elif gpm >= 2.1:
        rating += 0.3
    elif gpm <= 1.3:
        rating -= 0.7
    elif gpm <= 1.6:
        rating -= 0.4

    form_str = (stats.get("form") or "").upper()
    if form_str:
        form_score = 0.0
        count_chars = 0
        for ch in form_str:
            if ch not in ("W", "D", "L"):
                continue
            count_chars += 1
            if ch == "W":
                form_score += 0.15
            elif ch == "D":
                form_score += 0.05
            elif ch == "L":
                form_score -= 0.15

        if count_chars > 0:
            rating += form_score

    if played_total_int > 0:
        if gf_per >= 1.8 and ga_per >= 1.0:
            rating += 0.3
        elif gf_per >= 1.8 and ga_per < 0.8:
            rating += 0.15

    if rating > 2.0:
        rating = 2.0
    if rating < -2.0:
        rating = -2.0

    pregame_auto_cache[cache_key] = {
        "rating": rating,
        "ts": now,
        "attack_gpm": gf_per,
        "defense_gpm": ga_per,
    }
    return rating


def _get_team_attack_defense_from_cache(
    team_id: Optional[int],
    league_id: Optional[int],
    season: Optional[int],
) -> Tuple[float, float]:
    """
    Lê do cache o perfil de ataque/defesa (gols marcados/sofridos por jogo)
    calculado em _get_team_auto_rating.
    """
    if team_id is None or league_id is None or season is None:
        return 0.0, 0.0
    cache_key = "{lg}:{ss}:{tm}".format(lg=league_id, ss=season, tm=team_id)
    cached = pregame_auto_cache.get(cache_key) or {}
    try:
        atk = float(cached.get("attack_gpm", 0.0))
    except (TypeError, ValueError):
        atk = 0.0
    try:
        dfn = float(cached.get("defense_gpm", 0.0))
    except (TypeError, ValueError):
        dfn = 0.0
    return atk, dfn


def _is_team_under_profile(attack_gpm: float, defense_gpm: float) -> bool:
    """
    Time claramente under:
    - Ataque fraco (< 1.3 gol/jogo)
    - Defesa sólida (< 1.3 gol sofrido/jogo)
    """
    if attack_gpm <= 0.0 or defense_gpm < 0.0:
        return False
    return attack_gpm < 1.3 and defense_gpm < 1.3


def _is_match_super_under(
    home_attack_gpm: float,
    home_defense_gpm: float,
    away_attack_gpm: float,
    away_defense_gpm: float,
) -> bool:
    """
    Flag de jogo super under: dois times under de forma clara.
    """
    return _is_team_under_profile(home_attack_gpm, home_defense_gpm) and _is_team_under_profile(
        away_attack_gpm, away_defense_gpm
    )




def _goal_tier_gpm(gpm: Optional[float]) -> Optional[int]:
    """Bucketiza gols/jogo na escala do Lucas.
    0(<1.0), 1([1.0,1.3)), 2([1.3,1.5)), 3([1.5,1.8)), 4(>=1.8)
    """
    if gpm is None:
        return None
    try:
        v = float(gpm)
    except (TypeError, ValueError):
        return None
    if v < 1.0:
        return 0
    if v < 1.3:
        return 1
    if v < 1.5:
        return 2
    if v < 1.8:
        return 3
    return 4


def _has_goal_ammo(attack_gpm: Optional[float], defense_gpm: Optional[float]) -> bool:
    """Munição p/ gol: ou faz >=1.5/jogo ou toma >=1.5/jogo."""
    if attack_gpm is None or defense_gpm is None:
        return True
    return (attack_gpm >= 1.5) or (defense_gpm >= 1.5)


def _is_team_no_ammo(attack_gpm: Optional[float], defense_gpm: Optional[float]) -> bool:
    """Sem munição: faz <1.5 E toma <1.5 (tende a ser cenário ruim p/ teus overs)."""
    if attack_gpm is None or defense_gpm is None:
        return False
    return (attack_gpm < 1.5) and (defense_gpm < 1.5)


def _is_super_over_team(attack_gpm: Optional[float], defense_gpm: Optional[float]) -> bool:
    """Super over: faz muito ou toma muito (>=1.8)."""
    if attack_gpm is None or defense_gpm is None:
        return False
    return (attack_gpm >= 1.8) or (defense_gpm >= 1.8)

def _compute_score_context_boost(
    fixture: Dict[str, Any],
    rating_home: float,
    rating_away: float,
) -> float:
    """
    Ajuste de probabilidade baseado em CONTEXTO de placar + favorito.

    Regras alinhadas ao padrão:
    - Favorito perdendo → necessidade alta de gol (+boost).
    - Favorito empatando → boost leve (principalmente se mandante e pressionando).
    - Favorito ganhando, principalmente em casa e por 2+ gols → penalização forte (torneira fecha).
    - Perfil under real (gols feitos/sofridos baixos) reduz muito a necessidade, principalmente se já está na frente.
    """
    try:
        minute_int = int(fixture.get("minute") or 0)
    except (TypeError, ValueError):
        minute_int = 0

    try:
        home_goals = int(fixture.get("home_goals") or 0)
        away_goals = int(fixture.get("away_goals") or 0)
    except (TypeError, ValueError):
        home_goals, away_goals = 0, 0

    score_diff = home_goals - away_goals  # >0 home vence

    # 1) Favorito (prioridade: odds pré-live; fallback: rating)
    fav_side = fixture.get("favorite_side")  # "home" | "away" | None
    try:
        fav_strength = int(fixture.get("favorite_strength") or 0)  # 0..3
    except (TypeError, ValueError):
        fav_strength = 0

    if fav_side not in ("home", "away"):
        diff_rating = float(rating_home or 0.0) - float(rating_away or 0.0)
        if diff_rating >= 0.25:
            fav_side = "home"
        elif diff_rating <= -0.25:
            fav_side = "away"
        else:
            fav_side = None
        # força aproximada por rating
        if abs(diff_rating) >= 0.70:
            fav_strength = max(fav_strength, 2)
        elif abs(diff_rating) >= 0.40:
            fav_strength = max(fav_strength, 1)

    boost = 0.0

    # 2) Necessidade pelo placar vs favorito
    if fav_side == "home":
        if score_diff < 0:  # favorito perdendo em casa
            boost += 0.035 + 0.010 * min(2, abs(score_diff))
            if fav_strength >= 2:
                boost += 0.010
        elif score_diff == 0:  # empate com favorito em casa
            boost += 0.018
            if fav_strength >= 2:
                boost += 0.008
            if minute_int >= 55:
                boost += 0.004
        else:  # favorito ganhando em casa
            if score_diff >= 2:
                boost -= 0.050
            else:  # 1 gol
                boost -= 0.032
    elif fav_side == "away":
        if score_diff > 0:  # favorito perdendo fora
            boost += 0.030 + 0.010 * min(2, abs(score_diff))
            if fav_strength >= 2:
                boost += 0.008
        elif score_diff == 0:  # empate com favorito fora
            boost += 0.012
            if fav_strength >= 2:
                boost += 0.006
            if minute_int >= 55:
                boost += 0.003
        else:  # favorito ganhando fora
            if abs(score_diff) >= 2:
                boost -= 0.040
            else:
                boost -= 0.025
    else:
        # sem favorito claro → nada agressivo
        boost += 0.0

    # 3) Perfil under/over real via gols por jogo (munição)
    attack_home_gpm = fixture.get("attack_home_gpm")
    defense_home_gpm = fixture.get("defense_home_gpm")
    attack_away_gpm = fixture.get("attack_away_gpm")
    defense_away_gpm = fixture.get("defense_away_gpm")

    home_under = _is_team_under_profile(float(fixture.get("attack_home_gpm", 0.0)), float(fx.get("defense_home_gpm", 0.0)))
    away_under = _is_team_under_profile(attack_away_gpm, defense_away_gpm)

    # se o time "under" está na frente, penaliza mais
    if home_under and score_diff > 0:
        boost -= 0.020
        if fav_side == "home":
            boost -= 0.010
    if away_under and score_diff < 0:
        boost -= 0.020
        if fav_side == "away":
            boost -= 0.010

    # se o favorito under está empatando/vencendo, reduz muito a chance de buscar mais
    if fav_side == "home" and home_under and score_diff >= 0:
        boost -= 0.012
    if fav_side == "away" and away_under and score_diff <= 0:
        boost -= 0.010

    # 4) Escala por minuto (tua janela)
    if minute_int < 50:
        scale = 0.6
    elif minute_int < 60:
        scale = 0.85
    elif minute_int < 72:
        scale = 1.0
    elif minute_int < 80:
        scale = 0.9
    else:
        scale = 0.7

    boost *= scale

    # Clamp final (±6 pp é MUITO)
    if boost > 0.06:
        boost = 0.06
    if boost < -0.06:
        boost = -0.06

    return boost



def _compute_knockout_malus(
    fixture: Dict[str, Any],
    context_boost_prob: float,
) -> float:
    """
    Malus extra para jogos de mata-mata ida/volta (1ª partida tende a ser mais fechada).

    Heurística:
    - Só aplica em competições tipo "Cup" ou com nome típico de copa/torneio continental.
    - Janela ~45–80'.
    - Placar apertado (0x0, 1x0, 1x1, 2x1) e poucos gols.
    - Reduz um pouco o contexto positivo (favorito atrás/empatando).
    """
    league_type = (fixture.get("league_type") or "").lower()
    league_name = (fixture.get("league_name") or "").lower()
    league_round = (fixture.get("league_round") or "").lower()

    # Detecta "clima de mata-mata"
    is_cup = league_type == "cup" or any(
        kw in league_name
        for kw in (
            "cup",
            "copa",
            "taça",
            "champions",
            "europa league",
            "conference league",
        )
    )
    if not is_cup:
        return 0.0

    minute = fixture.get("minute") or 0
    try:
        minute_int = int(minute)
    except (TypeError, ValueError):
        minute_int = 0

    if minute_int < 45 or minute_int > 80:
        return 0.0

    home_goals = fixture.get("home_goals") or 0
    away_goals = fixture.get("away_goals") or 0
    total_goals = home_goals + away_goals
    score_diff = home_goals - away_goals

    # Jogo equilibrado / placar curto → tendência a "não se abrir" tanto
    if total_goals > 3 or abs(score_diff) > 1:
        return 0.0

    malus = 0.0

    # 0x0 em copa é bem travado na ida
    if total_goals == 0:
        malus -= 0.03
    else:
        malus -= 0.02

    # Se o contexto já está puxando muito pra cima (favorito atrás/empatando),
    # corta um pedaço desse boost.
    if context_boost_prob > 0.0:
        malus -= min(context_boost_prob * 0.5, 0.03)

    # Em rodadas com cara de "ida" (round genérico, sem final/semifinal único),
    # mantém esse malus; em finais únicas (round contém "final"), não pesa tanto.
    if "final" in league_round:
        malus *= 0.5

    return malus


async def _get_pregame_boost_auto(
    client: httpx.AsyncClient,
    fixture: Dict[str, Any],
) -> Dict[str, float]:
    """
    Calcula boost pré-jogo automático e devolve também ratings de casa/fora.

    Retorna:
        {
            "rating_home": float,
            "rating_away": float,
            "boost": float,
        }
    """
    if not USE_API_PREGAME:
        return {"rating_home": 0.0, "rating_away": 0.0, "boost": 0.0}

    league_id = fixture.get("league_id")
    season = fixture.get("season")
    home_team_id = fixture.get("home_team_id")
    away_team_id = fixture.get("away_team_id")

    if league_id is None or season is None:
        return {"rating_home": 0.0, "rating_away": 0.0, "boost": 0.0}

    rating_home = await _get_team_auto_rating(
        client=client,
        team_id=home_team_id,
        league_id=league_id,
        season=season,
    )
    rating_away = await _get_team_auto_rating(
        client=client,
        team_id=away_team_id,
        league_id=league_id,
        season=season,
    )

    avg_rating = (rating_home + rating_away) / 2.0

    boost = avg_rating * 0.008
    if boost > 0.02:
        boost = 0.02
    if boost < -0.02:
        boost = -0.02

    return {
        "rating_home": rating_home,
        "rating_away": rating_away,
        "boost": boost,
    }


async def _get_pregame_boost_for_fixture(
    client: httpx.AsyncClient,
    fixture: Dict[str, Any],
) -> tuple[float, float, float, float]:
    """
    Retorna:
        (pregame_boost_prob, context_boost_prob, rating_home, rating_away)

    pregame_boost_prob → pré-jogo manual + automático
    context_boost_prob → ajuste de necessidade de gol (favorito x placar x casa/fora)
    rating_home / rating_away → ratings usados para detectar cenário under, etc.
    Além disso, preenche no fixture:
        - attack_home_gpm / defense_home_gpm
        - attack_away_gpm / defense_away_gpm
        - match_super_under (bool)
    """
    manual_boost = _get_pregame_boost_manual(fixture)

    home_name = fixture.get("home_team") or ""
    away_name = fixture.get("away_team") or ""

    # Favorito pré-live (odds) — se não tiver, seguimos com fallback de rating
    try:
        await _ensure_prelive_favorite(client, fixture)
    except Exception:
        logging.exception("Erro ao garantir favorito pré-live para fixture=%s", fixture.get("fixture_id"))

    # ratings manuais como fallback
    rating_home = PREMATCH_TEAM_RATINGS.get(home_name, 0.0)
    rating_away = PREMATCH_TEAM_RATINGS.get(away_name, 0.0)

    # Se não conseguimos odds pré-live, define um favorito por rating (fallback leve)
    if fixture.get("favorite_side") is None:
        diff_rating = float(rating_home or 0.0) - float(rating_away or 0.0)
        if diff_rating >= 0.25:
            fixture["favorite_side"] = "home"
        elif diff_rating <= -0.25:
            fixture["favorite_side"] = "away"
        else:
            fixture["favorite_side"] = None

        # força aproximada (2=forte, 1=leve)
        if abs(diff_rating) >= 0.70:
            fixture["favorite_strength"] = 2
        elif abs(diff_rating) >= 0.40:
            fixture["favorite_strength"] = 1
        else:
            fixture["favorite_strength"] = 0

    league_id = fixture.get("league_id")
    season = fixture.get("season")
    home_team_id = fixture.get("home_team_id")
    away_team_id = fixture.get("away_team_id")

    auto_boost = 0.0

    if USE_API_PREGAME:
        try:
            auto_data = await _get_pregame_boost_auto(client, fixture)
            auto_boost = float(auto_data.get("boost", 0.0))
            rating_home = float(auto_data.get("rating_home", rating_home))
            rating_away = float(auto_data.get("rating_away", rating_away))
        except Exception:
            logging.exception(
                "Erro inesperado ao calcular pré-jogo automático para fixture=%s",
                fixture.get("fixture_id"),
            )
            auto_boost = 0.0

    # Pega perfis de ataque/defesa (gols por jogo) do cache
    attack_home_gpm, defense_home_gpm = _get_team_attack_defense_from_cache(
        home_team_id, league_id, season
    )
    attack_away_gpm, defense_away_gpm = _get_team_attack_defense_from_cache(
        away_team_id, league_id, season
    )

    fixture["attack_home_gpm"] = attack_home_gpm
    fixture["defense_home_gpm"] = defense_home_gpm
    fixture["attack_away_gpm"] = attack_away_gpm
    fixture["defense_away_gpm"] = defense_away_gpm

    match_super_under = _is_match_super_under(
        attack_home_gpm,
        defense_home_gpm,
        attack_away_gpm,
        defense_away_gpm,
    )
    fixture["match_super_under"] = match_super_under

    pregame_total = manual_boost + auto_boost
    if pregame_total > 0.03:
        pregame_total = 0.03
    if pregame_total < -0.03:
        pregame_total = -0.03

    context_boost = 0.0
    try:
        context_boost = _compute_score_context_boost(
            fixture=fixture,
            rating_home=rating_home,
            rating_away=rating_away,
        )
    except Exception:
        logging.exception(
            "Erro inesperado ao calcular contexto de placar para fixture=%s",
            fixture.get("fixture_id"),
        )
        context_boost = 0.0

    # Ajuste do contexto pelo tipo de time (over x under)
    try:
        # times claramente over (ataque forte ou defesa vazada)
        home_overish = attack_home_gpm >= 1.8 or defense_away_gpm >= 1.6
        away_overish = attack_away_gpm >= 1.8 or defense_home_gpm >= 1.6
        any_overish = home_overish or away_overish
        both_underish = _is_team_under_profile(
            attack_home_gpm, defense_home_gpm
        ) and _is_team_under_profile(
            attack_away_gpm, defense_away_gpm
        )

        if both_underish:
            # jogo muito under → necessidade de gol não pode inflar tanto
            context_boost *= 0.4
        elif any_overish:
            # jogo com característica over → peso levemente maior
            context_boost *= 1.1
        else:
            # meio termo → leve redução
            context_boost *= 0.9
    except Exception:
        pass

    # Ajuste extra para mata-mata ida/volta (1º jogo tende a ser mais travado)
    try:
        ko_malus = _compute_knockout_malus(fixture, context_boost)
        context_boost += ko_malus
    except Exception:
        logging.exception(
            "Erro ao aplicar malus de mata-mata para fixture=%s",
            fixture.get("fixture_id"),
        )

    # Clamp de segurança para o contexto (±5 pp já é um empurrão forte)
    if context_boost > 0.05:
        context_boost = 0.05
    if context_boost < -0.05:
        context_boost = -0.05

    return pregame_total, context_boost, rating_home, rating_away


# ---------------------------------------------------------------------------
# Boost extra "padrão Lucas" (faro de gol)
# ---------------------------------------------------------------------------

def _compute_lucas_pattern_boost(
    minute: int,
    home_goals: int,
    away_goals: int,
    pressure_score: float,
    context_boost_prob: float,
) -> float:
    """
    Boost adicional de probabilidade (0–10 pp) alinhado ao teu padrão:

    - Pressão alta (muitos chutes / perigo).
    - Placar tenso (diferença ≤ 1, nada de goleada confortável).
    - Janela de minuto que você mais entra (55–70).
    - Necessidade de gol via contexto (favorito atrás/empatando).
    """
    try:
        minute_int = int(minute)
    except (TypeError, ValueError):
        minute_int = 0

    total_goals = (home_goals or 0) + (away_goals or 0)
    score_diff = (home_goals or 0) - (away_goals or 0)

    boost = 0.0

    # Pressão ao vivo
    if pressure_score >= 8.5:
        boost += 0.06
    elif pressure_score >= 7.0:
        boost += 0.04
    elif pressure_score >= 5.5:
        boost += 0.025

    # Placar tenso / jogo vivo
    if abs(score_diff) <= 1 and total_goals <= 4:
        boost += 0.02
    elif total_goals == 0 and pressure_score >= 5.5:
        boost += 0.03

    # Janela de tempo preferida (onde você costuma pegar 1.47–1.70)
    if 55 <= minute_int <= 70:
        boost += 0.02
    elif 47 <= minute_int < 55 or 70 < minute_int <= 80:
        boost += 0.01

    # Necessidade de gol vinda do contexto (favorito atrás/empatando)
    if context_boost_prob > 0.0:
        # context_boost_prob está em probabilidade (0–1) → converte para peso
        boost += min(context_boost_prob * 0.5, 0.03)

    # Em goleadas o próprio contexto já derruba bastante, então não forçamos boost
    if abs(score_diff) >= 3 and minute_int >= 55:
        boost = 0.0

    # Penalização para contextos claramente negativos (favorito confortável no placar)
    if context_boost_prob < 0.0:
        context_pp = context_boost_prob * 100.0
        # contexto bem negativo (ex.: favorito forte ganhando em casa)
        if context_pp <= -2.0 and minute_int >= 55:
            boost *= 0.2
        elif context_pp < 0.0 and minute_int >= 50:
            boost *= 0.5

    # Clamp final 0–10 pp
    if boost < 0.0:
        boost = 0.0
    if boost > 0.10:
        boost = 0.10

    return boost


# ---------------------------------------------------------------------------
# Estimador de probabilidade / odd / EV + sugestão de stake
# ---------------------------------------------------------------------------

def _estimate_prob_and_odd(
    minute: int,
    stats: Dict[str, Any],
    home_goals: int,
    away_goals: int,
    forced_odd_current: Optional[float] = None,
    news_boost_prob: float = 0.0,
    pregame_boost_prob: float = 0.0,
    player_boost_prob: float = 0.0,
    context_boost_prob: float = 0.0,
) -> Dict[str, float]:
    """
    Estima probabilidade de +1 gol e uma odd "aproximada".

    IMPORTANTE:
    - Quando forced_odd_current vem da API (odd real da casa),
      não fazemos clamp em [MIN_ODD, MAX_ODD] aqui.
    - O filtro por faixa de odds é feito no scan.
    - Inclui boost extra "padrão Lucas" para cenários que batem com teu faro.
    """

    total_goals = home_goals + away_goals

    home_shots = stats.get("home_shots_total", 0)
    away_shots = stats.get("away_shots_total", 0)
    home_on = stats.get("home_shots_on", 0)
    away_on = stats.get("away_shots_on", 0)
    home_dang = stats.get("home_dangerous", 0)
    away_dang = stats.get("away_dangerous", 0)

    total_shots = home_shots + away_shots
    total_on = home_on + away_on
    total_dang = home_dang + away_dang

    pressure_score = 0.0

    # CHUTES TOTAIS
    if total_shots >= 15:
        pressure_score += 3.0
    elif total_shots >= 10:
        pressure_score += 2.0
    elif total_shots >= 6:
        pressure_score += 1.0

    # CHUTES NO ALVO
    if total_on >= 5:
        pressure_score += 3.0
    elif total_on >= 3:
        pressure_score += 2.0
    elif total_on >= 1:
        pressure_score += 1.0

    # ATAQUES PERIGOSOS
    if total_dang >= 40:
        pressure_score += 3.0
    elif total_dang >= 25:
        pressure_score += 2.0
    elif total_dang >= 15:
        pressure_score += 1.0

    # GOLS NO JOGO
    if total_goals >= 3:
        pressure_score += 1.0
    elif total_goals == 2:
        pressure_score += 0.5

    if pressure_score < 0.0:
        pressure_score = 0.0
    if pressure_score > 10.0:
        pressure_score = 10.0

    # Base levemente mais agressiva que a versão anterior
    base_prob = 0.38
    base_prob += (pressure_score / 10.0) * 0.37

    # Tempo de jogo
    if minute <= 55:
        base_prob += 0.05
    elif minute <= 65:
        base_prob += 0.03
    elif minute <= 75:
        base_prob += 0.00
    else:
        base_prob -= 0.02

    # Boosts individuais
    base_prob += news_boost_prob
    base_prob += pregame_boost_prob
    base_prob += player_boost_prob
    base_prob += context_boost_prob

    # Boost extra "padrão Lucas"
    lucas_boost_prob = _compute_lucas_pattern_boost(
        minute=minute,
        home_goals=home_goals,
        away_goals=away_goals,
        pressure_score=pressure_score,
        context_boost_prob=context_boost_prob,
    )
    base_prob += lucas_boost_prob

    # Clamp final
    # Malus por linhas altas (3.5, 4.5, 5.5...) — por padrão desconfiamos, a menos que o resto compense
    try:
        linha_gols = (home_goals + away_goals) + 0.5
    except Exception:
        linha_gols = 0.5
    if linha_gols >= HIGH_LINE_START:
        steps_high = int((linha_gols - 2.5) // 1.0)  # 3.5->1, 4.5->2, ...
        if steps_high > 0:
            base_prob -= steps_high * float(HIGH_LINE_STEP_MALUS_PROB)

    p_final = max(0.20, min(0.93, base_prob))

    odd_fair = 1.0 / p_final

    if forced_odd_current is not None and forced_odd_current > 1.0:
        odd_current = forced_odd_current
    else:
        odd_current = odd_fair * 1.03

    ev = p_final * odd_current - 1.0
    ev_pct = ev * 100.0

    return {
        "p_final": p_final,
        "odd_fair": odd_fair,
        "odd_current": odd_current,
        "ev_pct": ev_pct,
        "pressure_score": pressure_score,
        "news_boost_prob": news_boost_prob,
        "pregame_boost_prob": pregame_boost_prob,
        "player_boost_prob": player_boost_prob,
        "context_boost_prob": context_boost_prob,
        "lucas_boost_prob": lucas_boost_prob,
    }


def _suggest_stake_pct(ev_pct: float, odd_current: float) -> float:
    """
    Sugestão de stake em % da banca, aproximando tua lógica de tiers:

    - EV >= 7%  → ~3.0% da banca
    - 5%–7%     → ~2.5%
    - 3%–5%     → ~2.0%
    - 1.5%–3%   → 1.2%
    - abaixo disso: 0.8% simbólico
    """
    if ev_pct >= 7.0:
        return 3.0
    if ev_pct >= 5.0:
        return 2.5
    if ev_pct >= 3.0:
        return 2.0
    if ev_pct >= 1.5:
        return 1.2
    return 0.8


def _format_alert_text(
    fixture: Dict[str, Any],
    metrics: Dict[str, float],
) -> str:
    """
    Layout enxuto, padrão único pra você só bater o olho e decidir:

    🏟️ Jogo
    ⏱️ minuto | 🔢 placar
    ⚙️ Linha: Over x,5 @ odd
    📊 Probabilidade | Odd justa
    💰 EV: x% → EV+ / EV-
    🎯 Stake sugerida: x% da banca
    🧩 Nota: frase curta (pressão / necessidade de gol)
    """
    jogo = "{home} vs {away} — {league}".format(
        home=fixture["home_team"],
        away=fixture["away_team"],
        league=fixture["league_name"],
    )
    minuto = fixture["minute"]
    placar = "{hg}–{ag}".format(hg=fixture["home_goals"], ag=fixture["away_goals"])

    total_goals = fixture["home_goals"] + fixture["away_goals"]
    linha_gols = total_goals + 0.5
    linha_str = "Over {v:.1f}".format(v=linha_gols)

    p_final = metrics["p_final"] * 100.0
    odd_fair = metrics["odd_fair"]
    odd_current = metrics["odd_current"]
    ev_pct = metrics["ev_pct"]
    pressure_score = metrics["pressure_score"]
    context_boost_prob = metrics.get("context_boost_prob", 0.0) * 100.0
    lucas_boost_prob = metrics.get("lucas_boost_prob", 0.0) * 100.0

    stake_pct = _suggest_stake_pct(ev_pct, odd_current)

    # EV+ / EV-
    ev_label = "EV+" if ev_pct >= 0.0 else "EV-"

    # Nota rápida, 1 linha
    nota_parts: List[str] = []

    if pressure_score >= 7.5:
        nota_parts.append("pressão forte")
    elif pressure_score >= 5.0:
        nota_parts.append("pressão boa")
    else:
        nota_parts.append("pressão no limite")

    if context_boost_prob > 0.5:
        nota_parts.append("favorito ainda precisa do gol")
    elif context_boost_prob < -0.5:
        nota_parts.append("favorito confortável")

    if lucas_boost_prob > 0.0:
        nota_parts.append("padrão bem alinhado ao teu faro")

    nota = " / ".join(nota_parts)

    lines = [
        "🏟️ {jogo}".format(jogo=jogo),
        "⏱️ {minuto}' | 🔢 {placar}".format(minuto=minuto, placar=placar),
        "⚙️ Linha: {linha} @ {odd:.2f}".format(linha=linha_str, odd=odd_current),
        "📊 Probabilidade: {p:.1f}% | Odd justa: {odd_j:.2f}".format(
            p=p_final,
            odd_j=odd_fair,
        ),
        "💰 EV: {ev:.2f}% → {lbl}".format(ev=ev_pct, lbl=ev_label),
        "🎯 Stake sugerida: {spct:.2f}% da banca".format(spct=stake_pct),
        "🧩 Nota: {nota}".format(nota=nota),
    ]
    return "\n".join(lines)


def _format_watch_text(
    fixture: Dict[str, Any],
    metrics: Dict[str, float],
) -> str:
    """
    Alerta de OBSERVAÇÃO:
    - cenário de gol está bom,
    - mas a odd ainda está abaixo da mínima configurada.
    Layout enxuto.
    """
    jogo = "{home} vs {away} — {league}".format(
        home=fixture["home_team"],
        away=fixture["away_team"],
        league=fixture["league_name"],
    )
    minuto = fixture["minute"]
    placar = "{hg}–{ag}".format(hg=fixture["home_goals"], ag=fixture["away_goals"])

    total_goals = fixture["home_goals"] + fixture["away_goals"]
    linha_gols = total_goals + 0.5
    linha_str = "Over {v:.1f}".format(v=linha_gols)

    p_final = metrics["p_final"] * 100.0
    odd_fair = metrics["odd_fair"]
    odd_current = metrics["odd_current"]
    ev_pct = metrics["ev_pct"]
    pressure_score = metrics["pressure_score"]

    # Nota curta
    nota_parts: List[str] = []
    if pressure_score >= 7.5:
        nota_parts.append("pressão forte")
    elif pressure_score >= 5.0:
        nota_parts.append("pressão boa")
    else:
        nota_parts.append("pressão ok")

    nota_parts.append("esperar odd bater a mínima antes de entrar")
    nota = " / ".join(nota_parts)

    lines = [
        "👀 Observação de gol",
        "🏟️ {jogo}".format(jogo=jogo),
        "⏱️ {minuto}' | 🔢 {placar}".format(minuto=minuto, placar=placar),
        "⚙️ Linha: {linha}".format(linha=linha_str),
        "📊 Probabilidade: {p:.1f}% | Odd justa: {odd_j:.2f}".format(
            p=p_final,
            odd_j=odd_fair,
        ),
        "⚠️ Odd atual: {odd:.2f} (mínima configurada {mn:.2f})".format(
            odd=odd_current,
            mn=MIN_ODD,
        ),
        "💰 EV (na odd atual): {ev:.2f}%".format(ev=ev_pct),
        "🧩 Nota: {nota}".format(nota=nota),
    ]
    return "\n".join(lines)


def _format_manual_no_odds_text(
    fixture: Dict[str, Any],
    metrics: Dict[str, float],
) -> str:
    """
    Alerta MANUAL quando não há odd em nenhuma API, mas o jogo está no teu padrão.
    Layout enxuto, focado em probabilidade e plano de ação.
    """
    jogo = "{home} vs {away} — {league}".format(
        home=fixture["home_team"],
        away=fixture["away_team"],
        league=fixture["league_name"],
    )
    minuto = fixture["minute"]
    placar = "{hg}–{ag}".format(hg=fixture["home_goals"], ag=fixture["away_goals"])

    total_goals = fixture["home_goals"] + fixture["away_goals"]
    linha_gols = total_goals + 0.5
    linha_str = "Over {v:.1f}".format(v=linha_gols)

    p_final = metrics["p_final"] * 100.0
    odd_fair = metrics["odd_fair"]
    pressure_score = metrics["pressure_score"]
    context_boost_prob = metrics.get("context_boost_prob", 0.0) * 100.0
    lucas_boost_prob = metrics.get("lucas_boost_prob", 0.0) * 100.0

    # Nota curta
    nota_parts: List[str] = []

    if pressure_score >= 7.5:
        nota_parts.append("pressão forte dentro do teu padrão")
    elif pressure_score >= 5.0:
        nota_parts.append("pressão boa pra +1 gol")
    else:
        nota_parts.append("pressão mínima aceitável")

    if context_boost_prob > 0.0:
        nota_parts.append("placar/necessidade empurram pró gol")
    elif context_boost_prob < 0.0:
        nota_parts.append("contexto não força tanto")

    if lucas_boost_prob > 0.0:
        nota_parts.append("cenário encaixado no teu faro")

    nota_parts.append(
        "abrir mercado e só entrar se odd ≥ {mn:.2f}".format(mn=MANUAL_MIN_ODD_HINT)
    )
    nota = " / ".join(nota_parts)

    lines: List[str] = [
        "⚠️ Alerta manual (sem odd nas APIs)",
        "🏟️ {jogo}".format(jogo=jogo),
        "⏱️ {minuto}' | 🔢 {placar}".format(minuto=minuto, placar=placar),
        "⚙️ Linha sugerida: {linha}".format(linha=linha_str),
        "📊 Probabilidade do modelo: {p:.1f}% | Odd justa: {odd_j:.2f}".format(
            p=p_final,
            odd_j=odd_fair,
        ),
        "🧩 Nota: {nota}".format(nota=nota),
    ]
    return "\n".join(lines)


def _format_pattern_only_text(
    fixture: Dict[str, Any],
    metrics: Dict[str, float],
) -> str:
    """
    (Atualmente não usado diretamente; mantido como backup)
    Alerta de PADRÃO FORTE quando a API não trouxer odd nem cache.
    """
    jogo = "{home} vs {away} — {league}".format(
        home=fixture["home_team"],
        away=fixture["away_team"],
        league=fixture["league_name"],
    )
    minuto = fixture["minute"]
    placar = "{hg}–{ag}".format(hg=fixture["home_goals"], ag=fixture["away_goals"])

    total_goals = fixture["home_goals"] + fixture["away_goals"]
    linha_gols = total_goals + 0.5
    linha_str = "Over {v:.1f}".format(v=linha_gols)

    p_final = metrics["p_final"] * 100.0
    odd_fair = metrics["odd_fair"]
    odd_ref = metrics["odd_current"]
    ev_pct = metrics["ev_pct"]
    pressure_score = metrics["pressure_score"]

    lines: List[str] = [
        "👀 Padrão forte (sem odd na API)",
        "🏟️ {jogo}".format(jogo=jogo),
        "⏱️ {minuto}' | 🔢 {placar}".format(minuto=minuto, placar=placar),
        "⚙️ Linha alvo: {linha}".format(linha=linha_str),
        "📊 Probabilidade estimada: {p:.1f}% | Odd justa: {odd_j:.2f}".format(
            p=p_final,
            odd_j=odd_fair,
        ),
        "ℹ️ EV estimado usando odd de referência {od:.2f}: {ev:.2f}%".format(
            od=odd_ref,
            ev=ev_pct,
        ),
        "",
        "🧩 Interpretação:",
        "- Pressão {ps:.1f} indica cenário compatível com teu padrão de gol.".format(
            ps=pressure_score
        ),
        "- Nenhuma odd ao vivo disponível nas fontes (API-FOOTBALL/The Odds API).",
        "- Usa este alerta como radar de padrão; confere a odd real na casa antes de entrar.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Função principal de scan (CÉREBRO)
# ---------------------------------------------------------------------------

async def run_scan_cycle(origin: str, application: Application) -> List[str]:
    """
    Executa UM ciclo de varredura.
    Usa odd ao vivo ou, em caso de mercado suspenso, a última odd em cache
    da linha correta.

    Tipos de alerta:
    - ALERTA NORMAL: cenário bom + odd dentro da faixa [MIN_ODD, MAX_ODD]
    - ALERTA OBSERVAÇÃO: cenário bom + odd POSITIVA mas abaixo de MIN_ODD
    - ALERTA MANUAL: jogo muito no padrão, mas SEM odd nas APIs (você julga o preço)
    """
    global last_status_text, last_scan_origin, last_scan_alerts
    global last_scan_live_events, last_scan_window_matches

    last_scan_origin = origin
    last_scan_live_events = 0
    last_scan_window_matches = 0
    last_scan_alerts = 0

    if not API_FOOTBALL_KEY:
        last_status_text = (
            "[EvRadar PRO] Scan concluído (origem={origin}). "
            "API_FOOTBALL_KEY não definido; nenhum jogo analisado."
        ).format(origin=origin)
        logging.warning(last_status_text)
        return []

    async with httpx.AsyncClient() as client:
        fixtures = await _fetch_live_fixtures(client)

        last_scan_live_events = len(fixtures)
        last_scan_window_matches = len(fixtures)

        alerts: List[str] = []

        for fx in fixtures:
            try:
                stats = await _fetch_statistics_for_fixture(client, fx["fixture_id"])
                if not stats:
                    continue

                total_goals = fx["home_goals"] + fx["away_goals"]

                api_odd: Optional[float] = None
                got_live_odd = False
                used_cache_odd = False

                # 1) Tenta API-FOOTBALL
                try:
                    api_odd = await _fetch_live_odds_for_fixture(
                        client=client,
                        fixture_id=fx["fixture_id"],
                        total_goals=total_goals,
                    )
                    if api_odd is not None:
                        _update_last_odd_cache(fx["fixture_id"], total_goals, api_odd)
                        got_live_odd = True
                    else:
                        cached_odd = _get_cached_odd_for_line(fx["fixture_id"], total_goals)
                        if cached_odd is not None:
                            api_odd = cached_odd
                            used_cache_odd = True
                except Exception:
                    logging.exception(
                        "Erro inesperado ao buscar odds ao vivo (API-FOOTBALL) para fixture=%s",
                        fx["fixture_id"],
                    )
                    cached_odd = _get_cached_odd_for_line(fx["fixture_id"], total_goals)
                    if cached_odd is not None:
                        api_odd = cached_odd
                        used_cache_odd = True

                # 2) Se nada veio da API-FOOTBALL (nem cache), tenta The Odds API
                if api_odd is None and ODDS_API_KEY and ODDS_API_USE:
                    try:
                        oddsapi_odd = await _fetch_live_odds_for_fixture_odds_api(
                            client=client,
                            fixture=fx,
                            total_goals=total_goals,
                        )
                    except Exception:
                        logging.exception(
                            "Erro inesperado ao buscar odds via The Odds API para fixture=%s",
                            fx["fixture_id"],
                        )
                        oddsapi_odd = None

                    if oddsapi_odd is not None:
                        api_odd = oddsapi_odd
                        got_live_odd = True
                        _update_last_odd_cache(fx["fixture_id"], total_goals, api_odd)

                # 3) Boosts que não dependem de odd (sempre calculados)
                news_boost_prob = 0.0
                try:
                    news_boost_prob = await _fetch_news_boost_for_fixture(
                        client=client,
                        fixture=fx,
                    )
                except Exception:
                    logging.exception(
                        "Erro inesperado ao calcular news boost para fixture=%s",
                        fx["fixture_id"],
                    )
                    news_boost_prob = 0.0

                pregame_boost_prob = 0.0
                context_boost_prob = 0.0
                rating_home = 0.0
                rating_away = 0.0
                try:
                    (
                        pregame_boost_prob,
                        context_boost_prob,
                        rating_home,
                        rating_away,
                    ) = await _get_pregame_boost_for_fixture(
                        client=client,
                        fixture=fx,
                    )
                except Exception:
                    logging.exception(
                        "Erro inesperado ao calcular pré-jogo/contexto para fixture=%s",
                        fx["fixture_id"],
                    )
                    pregame_boost_prob = 0.0
                    context_boost_prob = 0.0
                    rating_home = 0.0
                    rating_away = 0.0

                player_boost_prob = 0.0
if USE_PLAYER_IMPACT:
    try:
        player_boost_prob = await _compute_player_boost_for_fixture(
            client=client,
            fixture=fx,
        )
    except Exception:
        logging.exception(
            "Erro inesperado ao calcular impacto de jogadores para fixture=%s",
            fx["fixture_id"],
        )
        player_boost_prob = 0.0

# Perfis de ataque/defesa e flag super under preenchidos no fixture
attack_home_gpm = float(fx.get("attack_home_gpm", 0.0))
defense_home_gpm = float(fx.get("defense_home_gpm", 0.0))
attack_away_gpm = float(fx.get("attack_away_gpm", 0.0))
defense_away_gpm = float(fx.get("defense_away_gpm", 0.0))
match_super_under = bool(fx.get("match_super_under", False))

# Flag de mandante claramente under (para filtros duros)
home_under = _is_team_under_profile(attack_home_gpm, defense_home_gpm)
                # Perfis de ataque/defesa e flag super under preenchidos no fixture
                attack_home_gpm = float(fx.get("attack_home_gpm", 0.0))
                defense_home_gpm = float(fx.get("defense_home_gpm", 0.0))
                attack_away_gpm = float(fx.get("attack_away_gpm", 0.0))
                defense_away_gpm = float(fx.get("defense_away_gpm", 0.0))
                match_super_under = bool(fx.get("match_super_under", False))

                # 4) Caso não haja odd em NENHUMA fonte → modo MANUAL (se permitido)
                if api_odd is None:
                    logging.info(
                        "Fixture %s sem odd ao vivo (API-FOOTBALL/The Odds API) e sem cache.",
                        fx["fixture_id"],
                    )

                    if not ALLOW_ALERTS_WITHOUT_ODDS:
                        logging.info(
                            "ALLOW_ALERTS_WITHOUT_ODDS=0; ignorando fixture %s.",
                            fx["fixture_id"],
                        )
                        continue

                    # Calcula probabilidade e pressão, sem EV baseado em odd real
                    metrics = _estimate_prob_and_odd(
                        minute=fx["minute"],
                        stats=stats,
                        home_goals=fx["home_goals"],
                        away_goals=fx["away_goals"],
                        forced_odd_current=None,
                        news_boost_prob=news_boost_prob,
                        pregame_boost_prob=pregame_boost_prob,
                        player_boost_prob=player_boost_prob,
                        context_boost_prob=context_boost_prob,
                    )

                    # Filtros de contexto / goleada / pressão semelhantes aos alertas normais
                    score_diff = (fx["home_goals"] or 0) - (fx["away_goals"] or 0)
                    minute_int = fx["minute"] or 0
                    try:
                        minute_int = int(minute_int)
                    except (TypeError, ValueError):
                        minute_int = 0

                    # Mandante under vencendo: cenário que você geralmente evita
                    if home_under and score_diff > 0 and minute_int >= 50:
                        continue

                    # Goleada a partir dos 55' → em geral você ignora
                    if abs(score_diff) >= 3 and minute_int >= 55:
                        continue

                    context_pp = metrics.get("context_boost_prob", 0.0) * 100.0

                    # Filtro forte para jogos com contexto negativo (favorito confortável etc.)
                    if context_pp <= -1.5 and score_diff != 0 and minute_int >= 60:
                        continue

                # NOVO: filtro de empate alinhado ao teu faro (favorito + munição under/over)
                is_draw = (score_diff == 0)
                if is_draw:
                    # Dados de munição (cache pré-jogo já anexado no fx)
                    home_attack_gpm = fx.get("home_attack_gpm")
                    home_defense_gpm = fx.get("home_defense_gpm")
                    away_attack_gpm = fx.get("away_attack_gpm")
                    away_defense_gpm = fx.get("away_defense_gpm")

                    # Bloqueio duro: empate em jogo “seco” (pouca munição)
                    if _is_match_super_under(home_attack_gpm, home_defense_gpm, away_attack_gpm, away_defense_gpm):
                        continue

                    # EXCEÇÃO A: amplo favorito pressionando (“amassando”) contra defesa frágil
                    fav_side = fx.get("favorite_side")
                    try:
                        fav_strength = int(fx.get("favorite_strength") or 0)
                    except (TypeError, ValueError):
                        fav_strength = 0

                    diff_rating = (rating_home or 0.0) - (rating_away or 0.0)
                    if fav_side not in ("home", "away"):
                        fav_side = "home" if diff_rating >= 0.25 else ("away" if diff_rating <= -0.25 else None)

                    big_fav = (fav_strength >= 2) or (abs(diff_rating) >= 0.65)

                    if fav_side == "home":
                        opp_def_weak = (away_defense_gpm is not None) and (away_defense_gpm >= 1.5)
                    elif fav_side == "away":
                        opp_def_weak = (home_defense_gpm is not None) and (home_defense_gpm >= 1.5)
                    else:
                        opp_def_weak = False

                    allow_big_fav_amass = (
                        big_fav
                        and opp_def_weak
                        and (pressure_score >= 7.0)
                        and (context_pp >= 1.3)
                    )

                    # EXCEÇÃO B: mesmo equilibrado, só libera se os dois forem “super over” e o jogo estiver MUITO aberto
                    home_super_over = _is_super_over_team(home_attack_gpm, home_defense_gpm)
                    away_super_over = _is_super_over_team(away_attack_gpm, away_defense_gpm)
                    allow_both_super_over = (
                        home_super_over and away_super_over
                        and (pressure_score >= 7.5)
                        and (context_pp >= 1.0)
                        and (minute_int >= 50)
                    )

                    if not (allow_big_fav_amass or allow_both_super_over):
                        continue


                    # Filtro específico: favorito forte vencendo em casa (ex.: Barcelona/Monaco)
                    fav_side2 = fx.get("favorite_side")
                    try:
                        fav_strength2 = int(fx.get("favorite_strength") or 0)
                    except (TypeError, ValueError):
                        fav_strength2 = 0
                    diff_rating = rating_home - rating_away
                    fav_home_clear = ((fav_side2 == "home") and (fav_strength2 >= 2)) or (diff_rating >= 0.7)
                    if fav_home_clear and score_diff > 0 and minute_int >= 55:
                        # se favorito está ganhando em casa e contexto não indica necessidade real,
                        # e/ou pressão não é absurda, ignora alerta manual
                        if (
                            context_pp <= 0.5
                            or metrics["pressure_score"] < (MIN_PRESSURE_SCORE + 2.0)
                        ):
                            continue

                    # Bloqueio extra: linhas altas em jogos super under
                    linha_num = (fx["home_goals"] + fx["away_goals"]) + 0.5
                    if match_super_under and linha_num >= 2.5:
                        continue

                    # Desconfiança em linhas altas (3.5+): exige pressão maior
                    if linha_num >= HIGH_LINE_START:
                        steps_high = int((linha_num - 2.5) // 1.0)
                        req_pressure = MIN_PRESSURE_SCORE + (HIGH_LINE_PRESSURE_STEP * steps_high)
                        if metrics["pressure_score"] < req_pressure:
                            continue

                    if metrics["pressure_score"] < MIN_PRESSURE_SCORE:
                        continue

                    # Limiar de probabilidade para valer um alerta manual:
                    # precisa ser maior que o break-even da odd mínima sugerida
                    if MANUAL_MIN_ODD_HINT > 1.0:
                        prob_min_for_manual = 1.0 / MANUAL_MIN_ODD_HINT
                    else:
                        prob_min_for_manual = 0.68  # fallback seguro

                    if metrics["p_final"] < prob_min_for_manual:
                        # prob baixa demais para o preço-alvo (ex.: < 1/1.47)
                        continue

                    # Cooldown por jogo
                    now = _now_utc()
                    fixture_id = fx["fixture_id"]
                    last_ts = fixture_last_alert_at.get(fixture_id)
                    if last_ts is not None:
                        if (now - last_ts) < timedelta(minutes=COOLDOWN_MINUTES):
                            continue

                    alert_text = _format_manual_no_odds_text(fx, metrics)
                    alerts.append(alert_text)
                    fixture_last_alert_at[fixture_id] = now
                    continue  # pula restante da lógica (sem odd real)

                # 5) Fluxo normal com odd real
                if used_cache_odd and not got_live_odd:
                    logging.info(
                        "Usando odd em cache para fixture %s (mercado possivelmente suspenso ou em delay, mesma linha SUM_PLUS_HALF).",
                        fx["fixture_id"],
                    )

                metrics = _estimate_prob_and_odd(
                    minute=fx["minute"],
                    stats=stats,
                    home_goals=fx["home_goals"],
                    away_goals=fx["away_goals"],
                    forced_odd_current=api_odd,
                    news_boost_prob=news_boost_prob,
                    pregame_boost_prob=pregame_boost_prob,
                    player_boost_prob=player_boost_prob,
                    context_boost_prob=context_boost_prob,
                )

                odd_cur = metrics["odd_current"]

                # CORTE POR GOLEADA / CONTEXTO / PERFIL UNDER/OVER
                score_diff = (fx["home_goals"] or 0) - (fx["away_goals"] or 0)
                minute_int = fx["minute"] or 0
                try:
                    minute_int = int(minute_int)
                except (TypeError, ValueError):
                    minute_int = 0

                # Mandante claramente under vencendo a partir dos 50'
                if home_under and score_diff > 0 and minute_int >= 50:
                    continue

                if abs(score_diff) >= 3 and minute_int >= 55:
                    # goleada a partir dos 55' → quase sempre torneira fechada pra você
                    continue

                context_pp = metrics.get("context_boost_prob", 0.0) * 100.0

                # Filtro forte para contexto muito negativo (favorito confortável) com tempo avançado
                if context_pp <= -1.5 and score_diff != 0 and minute_int >= 60:
                    continue

                # NOVO: filtro pesado para empates em jogos under/equilibrados
                is_draw = (score_diff == 0)
                if is_draw:
                    under_draw_block = False

                    # super under + tempo avançado
                    if match_super_under and minute_int >= 50:
                        under_draw_block = True

                    # dois times com rating ruim para gols, pressão fraca
                    if not under_draw_block:
                        if (
                            rating_home <= 0.0
                            and rating_away <= 0.0
                            and minute_int >= 50
                            and metrics["pressure_score"] < (MIN_PRESSURE_SCORE + 1.0)
                        ):
                            under_draw_block = True

                    # jogo empatado, sem grande favorito pressionando
                    if not under_draw_block:
                        if context_pp < 1.0 and metrics["pressure_score"] < 7.0 and minute_int >= 55:
                            under_draw_block = True

                    # exceção: favorito pressionando muito + defesa frágil
                    if under_draw_block:
                        if context_pp >= 2.0 and metrics["pressure_score"] >= 7.0:
                            under_draw_block = False

                    if under_draw_block:
                        continue

                # Filtro específico: favorito forte vencendo em casa (ex.: Barcelona/Monaco)
                diff_rating = rating_home - rating_away
                fav_home_clear = diff_rating >= 0.7
                if fav_home_clear and score_diff > 0 and minute_int >= 55:
                    # se favorito está ganhando em casa e contexto não indica necessidade real
                    # ou pressão não for bem acima do mínimo, ignora sinal
                    if (
                        context_pp <= 0.5
                        or metrics["pressure_score"] < (MIN_PRESSURE_SCORE + 2.0)
                    ):
                        continue

                # Bloqueio extra: linhas altas em jogos super under
                linha_num = (fx["home_goals"] + fx["away_goals"]) + 0.5
                if match_super_under and linha_num >= 2.5:
                    continue

                # Desconfiança em linhas altas (3.5+): exige pressão maior
                if linha_num >= HIGH_LINE_START:
                    steps_high = int((linha_num - 2.5) // 1.0)
                    req_pressure = MIN_PRESSURE_SCORE + (HIGH_LINE_PRESSURE_STEP * steps_high)
                    if metrics["pressure_score"] < req_pressure:
                        continue

                # Primeiro: filtros de pressão e EV
                if metrics["pressure_score"] < MIN_PRESSURE_SCORE:
                    continue

                if metrics["ev_pct"] < EV_MIN_PCT:
                    continue

                now = _now_utc()
                fixture_id = fx["fixture_id"]
                last_ts = fixture_last_alert_at.get(fixture_id)
                if last_ts is not None:
                    if (now - last_ts) < timedelta(minutes=COOLDOWN_MINUTES):
                        continue

                # Aqui odd vem das APIs (ou cache real) → aplica faixa de odds
                if odd_cur > MAX_ODD:
                    continue

                if odd_cur < MIN_ODD:
                    alert_text = _format_watch_text(fx, metrics)
                else:
                    alert_text = _format_alert_text(fx, metrics)

                alerts.append(alert_text)
                fixture_last_alert_at[fixture_id] = now

            except Exception:
                logging.exception(
                    "Erro ao processar fixture_id=%s",
                    fx.get("fixture_id"),
                )
                continue

    last_scan_alerts = len(alerts)

    last_status_text = (
        "[EvRadar PRO] Scan concluído (origem={origin}). "
        "Eventos ao vivo na janela/ligas: {live} | Alertas enviados: {alerts}"
    ).format(
        origin=origin,
        live=last_scan_window_matches,
        alerts=last_scan_alerts,
    )

    logging.info(last_status_text)
    return alerts


async def autoscan_loop(application: Application) -> None:
    """Loop de autoscan em background (usa create_task; não bloqueia polling)."""
    logging.info("Autoscan iniciado (intervalo=%ss)", CHECK_INTERVAL)
    while True:
        try:
            alerts = await run_scan_cycle(origin="auto", application=application)
            if TELEGRAM_CHAT_ID and alerts:
                for text in alerts:
                    try:
                        await application.bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=text,
                        )
                    except Exception:
                        logging.exception("Erro ao enviar alerta de autoscan")
        except Exception:
            logging.exception("Erro no autoscan")
        await asyncio.sleep(CHECK_INTERVAL)


# ---------------------------------------------------------------------------
# Handlers de comando
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    autoscan_status = "ativado" if AUTOSTART else "desativado"
    player_layer_status = "ligada" if USE_PLAYER_IMPACT else "desligada"
    manual_mode_status = "ligado" if ALLOW_ALERTS_WITHOUT_ODDS else "desligado"

    lines = [
        "👋 EvRadar PRO online (cérebro v0.3-lite: odds reais + news + pré-jogo auto + camada de jogadores).",
        "",
        "Janela padrão: {ws}–{we}ʼ".format(ws=WINDOW_START, we=WINDOW_END),
        "EV mínimo: {ev:.2f}%".format(ev=EV_MIN_PCT),
        "Faixa de odds: {mn:.2f}–{mx:.2f}".format(mn=MIN_ODD, mx=MAX_ODD),
        "Alertas sem odd na API (manual): {m} (odd mínima sugerida ≥ {od:.2f})".format(
            m=manual_mode_status,
            od=MANUAL_MIN_ODD_HINT,
        ),
        "Banca virtual para sugestão: R$ {bk:.2f}".format(bk=BANKROLL_INITIAL),
        "Pressão mínima (score): {ps:.1f}".format(ps=MIN_PRESSURE_SCORE),
        "Cooldown por jogo: {cd} min".format(cd=COOLDOWN_MINUTES),
        "Camada de jogadores (impacto): {pl}".format(pl=player_layer_status),
        "Autoscan: {auto} (intervalo {sec}s)".format(auto=autoscan_status, sec=CHECK_INTERVAL),
        "",
        "Comandos:",
        "  /scan   → rodar varredura agora",
        "  /status → ver último resumo",
        "  /debug  → info técnica",
        "  /links  → links úteis / bookmaker",
    ]
    text = "\n".join(lines)
    try:
        if update.effective_chat:
            await update.effective_chat.send_message(text)
        elif TELEGRAM_CHAT_ID:
            await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
    except Exception:
        logging.exception("Erro ao enviar resposta do /start")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [
        "📊 Último status do EvRadar PRO:",
        "",
        last_status_text,
        "",
        "Origem da última varredura: {o}".format(o=last_scan_origin),
        "Eventos analisados na janela/ligas: {live}".format(
            live=last_scan_window_matches
        ),
        "Alertas enviados na última varredura: {al}".format(
            al=last_scan_alerts
        ),
    ]
    text = "\n".join(lines)
    try:
        if update.effective_chat:
            await update.effective_chat.send_message(text)
        elif TELEGRAM_CHAT_ID:
            await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
    except Exception:
        logging.exception("Erro ao enviar resposta do /status")


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if update.effective_chat:
            await update.effective_chat.send_message(
                "🔍 Iniciando varredura manual de jogos ao vivo (cérebro v0.3-lite, odds reais + news + pré-jogo auto + jogadores)..."
            )
    except Exception:
        logging.exception("Erro ao enviar mensagem inicial do /scan")

    alerts: List[str] = []
    try:
        alerts = await run_scan_cycle(origin="manual", application=context.application)
    except Exception:
        logging.exception("Erro ao rodar run_scan_cycle(manual)")

    # Envia alertas (se houver)
    if update.effective_chat and alerts:
        for text in alerts:
            try:
                await update.effective_chat.send_message(text)
            except Exception:
                logging.exception("Erro ao enviar alerta de /scan")

    # Resumo final
    try:
        resumo = last_status_text
        if update.effective_chat:
            await update.effective_chat.send_message(resumo)
        elif TELEGRAM_CHAT_ID:
            await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=resumo)
    except Exception:
        logging.exception("Erro ao enviar resumo final do /scan")


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    def _mask(key: str) -> str:
        if not key:
            return "(vazio)"
        if len(key) <= 6:
            return key[0:2] + "..." + key[-2:]
        return key[0:4] + "..." + key[-4:]

    lines = [
        "🛠 Debug EvRadar PRO",
        "",
        "LEAGUE_IDS: {ids}".format(ids=",".join(str(x) for x in LEAGUE_IDS) or "(nenhuma)"),
        "WINDOW_START/END: {ws}/{we}".format(ws=WINDOW_START, we=WINDOW_END),
        "EV_MIN_PCT: {ev:.2f}%".format(ev=EV_MIN_PCT),
        "MIN_ODD/MAX_ODD: {mn:.2f}/{mx:.2f}".format(mn=MIN_ODD, mx=MAX_ODD),
        "MIN_PRESSURE_SCORE: {ps:.1f}".format(ps=MIN_PRESSURE_SCORE),
        "COOLDOWN_MINUTES: {cd}".format(cd=COOLDOWN_MINUTES),
        "",
        "USE_API_FOOTBALL_ODDS: {v}".format(v=USE_API_FOOTBALL_ODDS),
        "BOOKMAKER_ID: {v}".format(v=BOOKMAKER_ID),
        "BOOKMAKER_FALLBACK_IDS: {v}".format(
            v=",".join(str(x) for x in BOOKMAKER_FALLBACK_IDS) or "(nenhum)"
        ),
        "ODDS_BET_ID: {v}".format(v=ODDS_BET_ID),
        "",
        "USE_API_PREGAME: {v}".format(v=USE_API_PREGAME),
        "USE_PLAYER_IMPACT: {v}".format(v=USE_PLAYER_IMPACT),
        "USE_NEWS_API: {v}".format(v=USE_NEWS_API),
        "",
        "ODDS_API_USE: {v}".format(v=ODDS_API_USE),
        "ODDS_API_DAILY_LIMIT: {v}".format(v=ODDS_API_DAILY_LIMIT),
        "ODDS_API_LEAGUE_MAP: {v}".format(v=ODDS_API_LEAGUE_MAP or "{}"),
        "",
        "API_FOOTBALL_KEY: {v}".format(v=_mask(API_FOOTBALL_KEY)),
        "ODDS_API_KEY: {v}".format(v=_mask(ODDS_API_KEY)),
        "NEWS_API_KEY: {v}".format(v=_mask(NEWS_API_KEY)),
    ]
    text = "\n".join(lines)
    try:
        if update.effective_chat:
            await update.effective_chat.send_message(text)
        elif TELEGRAM_CHAT_ID:
            await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
    except Exception:
        logging.exception("Erro ao enviar resposta do /debug")


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [
        "🔗 Links úteis EvRadar PRO",
        "",
        "Casa/base para operar:",
        "- {book}: {url}".format(book=BOOKMAKER_NAME, url=BOOKMAKER_URL),
        "",
        "APIs utilizadas (requer chaves configuradas no Railway/.env):",
        "- API-FOOTBALL (fixtures, estatísticas, odds): https://www.api-football.com/",
        "- The Odds API (odds globais): https://the-odds-api.com/",
        "- NewsAPI (notícias): https://newsapi.org/",
        "",
        "Dica: mantém essas chaves em variáveis de ambiente e NUNCA commita pro GitHub.",
    ]
    text = "\n".join(lines)
    try:
        if update.effective_chat:
            await update.effective_chat.send_message(text)
        elif TELEGRAM_CHAT_ID:
            await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
    except Exception:
        logging.exception("Erro ao enviar resposta do /links")


# ---------------------------------------------------------------------------
# Função main / bootstrap do bot
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )
    logging.info("Iniciando bot do EvRadar PRO (cérebro v0.3-lite)...")

    if not TELEGRAM_BOT_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN não definido; encerrando.")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_handler(CommandHandler("debug", cmd_debug))
    application.add_handler(CommandHandler("links", cmd_links))

    # Autoscan (inicia APÓS o start do PTB, evita PTBUserWarning)
    if AUTOSTART:
        async def _post_start(app: Application) -> None:
            app.create_task(autoscan_loop(app), name="autoscan_loop")

        if hasattr(application, "post_start"):
            application.post_start = _post_start  # type: ignore[attr-defined]
        else:
            # Fallback (PTB antigo): pode gerar warning, mas mantém AUTOSTART funcionando
            application.post_init = _post_start  # type: ignore[assignment]
# Polling
    application.run_polling()


if __name__ == "__main__":
    main()
