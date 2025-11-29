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
- News boost (NewsAPI, opcional)
- Pré-jogo boost:
    - Manual (PREMATCH_TEAM_RATINGS)
    - Automático (API-FOOTBALL /teams/statistics, com cache diário)
- Impacto de jogadores em campo:
    - Stats por time/temporada (API-FOOTBALL /players)
    - Lineups + substituições (fixtures/lineups + fixtures/events)
    - Boost de probabilidade conforme “peso ofensivo” do XI atual
- Cálculo de EV e alertas Telegram quando EV >= EV_MIN_PCT

Baseado na tua versão estável anterior (v0.2-lite + odds reais + news + pré-jogo manual).
"""

import asyncio
import logging
import os
from typing import Optional, List, Dict, Any
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
    # Exemplo: ajuste conforme teu faro
    # "Santos": 1.5,
    # "Palmeiras": 1.8,
    # "Flamengo": 1.7,
    # "Atlético Mineiro": 1.4,
    # "Cuiabá": -0.4,
}


# ---------------------------------------------------------------------------
# Estado em memória
# ---------------------------------------------------------------------------

last_status_text: str = "Ainda não foi rodada nenhuma varredura."
last_scan_origin: str = "-"
last_scan_alerts: int = 0
last_scan_live_events: int = 0
last_scan_window_matches: int = 0

# Cache de última odd real por jogo (fixture_id -> odd da linha correta)
last_odd_cache: Dict[int, float] = {}

# Cache simples de último "news boost" por fixture (fixture_id -> boost)
last_news_boost_cache: Dict[int, float] = {}

# Cache de pré-jogo auto por time (chave: "league:season:team_id")
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


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


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

            fixtures.append(
                {
                    "fixture_id": int(fixture.get("id")),
                    "league_id": league_id,
                    "league_name": league.get("name") or "",
                    "season": season,
                    "minute": int(elapsed),
                    "status_short": short,
                    "home_team": home_team,
                    "away_team": away_team,
                    "home_team_id": home_team_id,
                    "away_team_id": away_team_id,
                    "home_goals": int(home_goals),
                    "away_goals": int(away_goals),
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
    - Tenta achar a linha Over (soma do placar + 0,5) via 'handicap' ou número
      dentro de 'value' (ex.: "Over 2.5").
    - Se não encontrar exatamente essa linha, usa o PRIMEIRO mercado Over
      elegível encontrado (primeira linha de gols em aberto), que na prática
      tende a ser a linha soma+0,5, porque as casas removem mercados já liquidados.
    """
    if not API_FOOTBALL_KEY or not USE_API_FOOTBALL_ODDS:
        return None

    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params: Dict[str, Any] = {
        "fixture": fixture_id,
        "bookmaker": BOOKMAKER_ID,
    }
    # Se ODDS_BET_ID > 0, filtra por bet específico (ex.: Over/Under)
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
            "Fixture %s: resposta de odds LIVE vazia (sem mercados).",
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

    # Escolhe o bookmaker alvo (BOOKMAKER_ID) ou, se não achar, o primeiro.
    selected_bm: Optional[Dict[str, Any]] = None
    for b in bookmakers:
        b_id_raw = b.get("id")
        try:
            if b_id_raw is not None and int(b_id_raw) == BOOKMAKER_ID:
                selected_bm = b
                break
        except Exception:
            continue

    if selected_bm is None:
        selected_bm = bookmakers[0]

    bets = selected_bm.get("bets") or []
    if not bets:
        logging.info(
            "Fixture %s: bookmaker %s sem bets disponíveis em odds LIVE.",
            fixture_id,
            BOOKMAKER_ID,
        )
        return None

    target_line = float(total_goals) + 0.5
    target_line_str = "{:.1f}".format(target_line)

    exact_odd: Optional[float] = None
    first_over_odd: Optional[float] = None
    first_over_line: Optional[float] = None

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
    )

    for bet in bets:
        name = (bet.get("name") or "").lower()

        # Ignora mercados que claramente não são de gols "full time"
        if any(tok in name for tok in negative_tokens):
            continue

        # Queremos apenas mercados de gols / total goals / over under
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

            # Tenta extrair a linha (handicap) como número
            line_num: Optional[float] = None

            handicap_raw = val.get("handicap")
            if handicap_raw is not None:
                try:
                    line_num = float(str(handicap_raw).replace(",", "."))
                except Exception:
                    line_num = None

            if line_num is None:
                # Fallback: procurar número dentro de "Over 2.5"
                for token in side_raw.replace(",", ".").split():
                    try:
                        line_num = float(token)
                        break
                    except Exception:
                        continue

            # Guarda o PRIMEIRO Over elegível como fallback
            if first_over_odd is None:
                first_over_odd = odd_val
                first_over_line = line_num

            # Se conseguir linha e bater com soma+0.5, usamos essa e encerramos
            if line_num is not None:
                line_str = "{:.1f}".format(line_num)
                if line_str == target_line_str:
                    exact_odd = odd_val
                    logging.info(
                        "Odd LIVE encontrada para fixture %s (linha Over %s = soma+0.5): %.3f",
                        fixture_id,
                        line_str,
                        odd_val,
                    )
                    return exact_odd

    if exact_odd is not None:
        return exact_odd

    if first_over_odd is not None:
        if first_over_line is not None:
            logging.info(
                "Odd LIVE fallback (primeiro Over) para fixture %s (linha aprox %.1f): %.3f",
                fixture_id,
                first_over_line,
                first_over_odd,
            )
        else:
            logging.info(
                "Odd LIVE fallback (primeiro Over) para fixture %s (linha sem handicap explícito): %.3f",
                fixture_id,
                first_over_odd,
            )
        return first_over_odd

    logging.info(
        "Fixture %s: odds LIVE presentes, mas nenhuma seleção Over elegível encontrada.",
        fixture_id,
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
# Pré-jogo boost (manual + automático)
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
    """
    if not API_FOOTBALL_KEY or not USE_API_PREGAME:
        return 0.0

    if team_id is None or league_id is None or season is None:
        return 0.0

    cache_key = "{lg}:{ss}:{tm}".format(lg=league_id, ss=season, tm=team_id)
    now = _now_utc()

    cached = pregame_auto_cache.get(cache_key)
    if cached:
        ts: datetime = cached.get("ts")  # type: ignore
        rating_cached = float(cached.get("rating", 0.0))
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
        pregame_auto_cache[cache_key] = {"rating": 0.0, "ts": now}
        return 0.0

    stats = data.get("response") or {}
    if not stats:
        pregame_auto_cache[cache_key] = {"rating": 0.0, "ts": now}
        return 0.0

    rating = 0.0

    fixtures_info = stats.get("fixtures") or {}
    played_total = ((fixtures_info.get("played") or {}).get("total")) or 0
    wins_total = ((fixtures_info.get("wins") or {}).get("total")) or 0
    draws_total = ((fixtures_info.get("draws") or {}).get("total")) or 0
    loses_total = ((fixtures_info.get("loses") or {}).get("total")) or 0

    goals_info = stats.get("goals") or {}
    gf_total = (
        ((goals_info.get("for") or {}).get("total") or {}).get("total", 0) or 0
    )
    ga_total = (
        ((goals_info.get("against") or {}).get("total") or {}).get("total", 0) or 0
    )

    gpm = 0.0
    if played_total > 0:
        gpm = (gf_total + ga_total) / float(played_total)

    if gpm >= 3.2:
        rating += 1.2
    elif gpm >= 2.8:
        rating += 0.9
    elif gpm >= 2.4:
        rating += 0.6
    elif gpm >= 2.1:
        rating += 0.3
    elif gpm <= 1.6:
        rating -= 0.4
    elif gpm <= 1.3:
        rating -= 0.7

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

    if played_total > 0:
        gf_per = gf_total / float(played_total)
        ga_per = ga_total / float(played_total)
        if gf_per >= 1.8 and ga_per >= 1.0:
            rating += 0.3
        elif gf_per >= 1.8 and ga_per < 0.8:
            rating += 0.15

    if rating > 2.0:
        rating = 2.0
    if rating < -2.0:
        rating = -2.0

    pregame_auto_cache[cache_key] = {"rating": rating, "ts": now}
    return rating


async def _get_pregame_boost_auto(
    client: httpx.AsyncClient,
    fixture: Dict[str, Any],
) -> float:
    if not USE_API_PREGAME:
        return 0.0

    league_id = fixture.get("league_id")
    season = fixture.get("season")
    home_team_id = fixture.get("home_team_id")
    away_team_id = fixture.get("away_team_id")

    if league_id is None or season is None:
        return 0.0

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

    return boost


async def _get_pregame_boost_for_fixture(
    client: httpx.AsyncClient,
    fixture: Dict[str, Any],
) -> float:
    manual_boost = _get_pregame_boost_manual(fixture)

    if not USE_API_PREGAME:
        return manual_boost

    auto_boost = 0.0
    try:
        auto_boost = await _get_pregame_boost_auto(client, fixture)
    except Exception:
        logging.exception(
            "Erro inesperado ao calcular pré-jogo automático para fixture=%s",
            fixture.get("fixture_id"),
        )
        auto_boost = 0.0

    total = manual_boost + auto_boost

    if total > 0.03:
        total = 0.03
    if total < -0.03:
        total = -0.03
    return total


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
) -> Dict[str, float]:
    """
    Estima probabilidade de +1 gol e uma odd "aproximada".

    IMPORTANTE:
    - Quando forced_odd_current vem da API (odd real da casa),
      não fazemos clamp em [MIN_ODD, MAX_ODD] aqui.
    - O filtro por faixa de odds é feito no scan.
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

    base_prob = 0.35
    base_prob += (pressure_score / 10.0) * 0.35

    if minute <= 55:
        base_prob += 0.05
    elif minute <= 65:
        base_prob += 0.03
    elif minute <= 75:
        base_prob += 0.00
    else:
        base_prob -= 0.02

    base_prob += news_boost_prob
    base_prob += pregame_boost_prob
    base_prob += player_boost_prob

    p_final = max(0.20, min(0.90, base_prob))

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
    """Formata texto do alerta no layout EvRadar (agora com stake % e R$)."""
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
    news_boost_prob = metrics.get("news_boost_prob", 0.0) * 100.0
    pregame_boost_prob = metrics.get("pregame_boost_prob", 0.0) * 100.0
    player_boost_prob = metrics.get("player_boost_prob", 0.0) * 100.0

    stake_pct = _suggest_stake_pct(ev_pct, odd_current)
    stake_brl = BANKROLL_INITIAL * (stake_pct / 100.0)

    interpretacao_parts: List[str] = []

    if pressure_score >= 7.5:
        interpretacao_parts.append("pressão ofensiva alta")
    elif pressure_score >= 5.0:
        interpretacao_parts.append("jogo com pressão moderada para cima")
    else:
        interpretacao_parts.append("pressão apenas ok (cuidado)")

    if total_goals >= 3:
        interpretacao_parts.append("jogo aberto em gols")
    elif total_goals == 0:
        interpretacao_parts.append("placar magro, mas estatísticas sugerem risco/valor")

    if news_boost_prob > 0.0:
        if news_boost_prob >= 2.0:
            interpretacao_parts.append("noticiário reforça tendência de gol")
        else:
            interpretacao_parts.append("noticiário levemente favorável a gol")
    elif news_boost_prob < 0.0:
        interpretacao_parts.append("noticiário pesa um pouco contra (cautela)")

    if pregame_boost_prob > 0.0:
        if pregame_boost_prob >= 2.0:
            interpretacao_parts.append("força pré-jogo favorece gols")
        else:
            interpretacao_parts.append("leve viés pré-jogo pró-gol")
    elif pregame_boost_prob < 0.0:
        interpretacao_parts.append("pré-jogo sugeria menos gols (cautela)")

    if player_boost_prob > 0.5:
        interpretacao_parts.append("elenco em campo puxa pró-gol (impacto jogadores)")
    elif player_boost_prob < -0.5:
        interpretacao_parts.append("elenco em campo tira um pouco da força ofensiva")

    if ev_pct >= EV_MIN_PCT + 2.0:
        ev_flag = "EV+ forte"
    elif ev_pct >= EV_MIN_PCT:
        ev_flag = "EV+"
    else:
        ev_flag = "EV borderline"

    interpretacao_parts.append(ev_flag)
    interpretacao = " / ".join(interpretacao_parts)

    adjust_parts: List[str] = []
    if news_boost_prob != 0.0:
        adjust_parts.append("news {nb:+.1f} pp".format(nb=news_boost_prob))
    if pregame_boost_prob != 0.0:
        adjust_parts.append("pré {pg:+.1f} pp".format(pg=pregame_boost_prob))
    if player_boost_prob != 0.0:
        adjust_parts.append("jogadores {pl:+.1f} pp".format(pl=player_boost_prob))

    adjust_str = ""
    if adjust_parts:
        adjust_str = " (ajustes: {txt})".format(txt=", ".join(adjust_parts))

    lines = [
        "🏟️ {jogo}".format(jogo=jogo),
        "⏱️ {minuto}' | 🔢 {placar}".format(minuto=minuto, placar=placar),
        "⚙️ Linha: {linha} @ {odd:.2f}".format(linha=linha_str, odd=odd_current),
        "📊 Probabilidade: {p:.1f}% | Odd justa: {odd_j:.2f}{adj}".format(
            p=p_final,
            odd_j=odd_fair,
            adj=adjust_str,
        ),
        "💰 EV: {ev:.2f}%".format(ev=ev_pct),
        "💵 Stake sugerida (banca virtual): {spct:.2f}% ≈ R$ {sbrl:.2f}".format(
            spct=stake_pct,
            sbrl=stake_brl,
        ),
        "",
        "✅ Para registrar na banca virtual (manual):",
        "   {spct:.2f}% (~R$ {sbrl:.2f}) em {linha} @ {odd:.2f}".format(
            spct=stake_pct,
            sbrl=stake_brl,
            linha=linha_str,
            odd=odd_current,
        ),
        "",
        "🧩 Interpretação:",
        interpretacao,
        "",
        "🔗 Mercado: {book} → {url}".format(
            book=BOOKMAKER_NAME,
            url=BOOKMAKER_URL,
        ),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Função principal de scan (CÉREBRO)
# ---------------------------------------------------------------------------

async def run_scan_cycle(origin: str, application: Application) -> List[str]:
    """
    Executa UM ciclo de varredura.
    Usa odd ao vivo ou, em caso de mercado suspenso, a última odd em cache
    da linha correta. Se não houver nem live nem cache, ignora o jogo.
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

                try:
                    api_odd = await _fetch_live_odds_for_fixture(
                        client=client,
                        fixture_id=fx["fixture_id"],
                        total_goals=total_goals,
                    )
                    if api_odd is not None:
                        last_odd_cache[fx["fixture_id"]] = api_odd
                        got_live_odd = True
                    else:
                        api_odd = last_odd_cache.get(fx["fixture_id"])
                except Exception:
                    logging.exception(
                        "Erro inesperado ao buscar odds ao vivo para fixture=%s",
                        fx["fixture_id"],
                    )
                    api_odd = last_odd_cache.get(fx["fixture_id"])

                if api_odd is None:
                    logging.info(
                        "Fixture %s sem odd ao vivo nem cache (possível mercado suspenso). Ignorando jogo.",
                        fx["fixture_id"],
                    )
                    continue

                if not got_live_odd:
                    logging.info(
                        "Usando odd em cache para fixture %s (mercado possivelmente suspenso).",
                        fx["fixture_id"],
                    )

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
                try:
                    pregame_boost_prob = await _get_pregame_boost_for_fixture(
                        client=client,
                        fixture=fx,
                    )
                except Exception:
                    logging.exception(
                        "Erro inesperado ao calcular pré-jogo para fixture=%s",
                        fx["fixture_id"],
                    )
                    pregame_boost_prob = 0.0

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

                metrics = _estimate_prob_and_odd(
                    minute=fx["minute"],
                    stats=stats,
                    home_goals=fx["home_goals"],
                    away_goals=fx["away_goals"],
                    forced_odd_current=api_odd,
                    news_boost_prob=news_boost_prob,
                    pregame_boost_prob=pregame_boost_prob,
                    player_boost_prob=player_boost_prob,
                )

                odd_cur = metrics["odd_current"]
                if odd_cur < MIN_ODD or odd_cur > MAX_ODD:
                    continue

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

                fixture_last_alert_at[fixture_id] = now

                alert_text = _format_alert_text(fx, metrics)
                alerts.append(alert_text)
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

    lines = [
        "👋 EvRadar PRO online (cérebro v0.3-lite: odds reais + news + pré-jogo auto + camada de jogadores).",
        "",
        "Janela padrão: {ws}–{we}ʼ".format(ws=WINDOW_START, we=WINDOW_END),
        "EV mínimo: {ev:.2f}%".format(ev=EV_MIN_PCT),
        "Faixa de odds: {mn:.2f}–{mx:.2f}".format(mn=MIN_ODD, mx=MAX_ODD),
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
    await update.message.reply_text("\n".join(lines))


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🔍 Iniciando varredura manual de jogos ao vivo (cérebro v0.3-lite, odds reais + news + pré-jogo auto + jogadores)..."
    )

    alerts = await run_scan_cycle(origin="manual", application=context.application)

    if not alerts:
        await update.message.reply_text(last_status_text)
        return

    for text in alerts:
        await update.message.reply_text(text)

    await update.message.reply_text(last_status_text)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(last_status_text)


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    token_set = bool(TELEGRAM_BOT_TOKEN)
    chat_set = TELEGRAM_CHAT_ID is not None
    api_set = bool(API_FOOTBALL_KEY)
    news_set = bool(NEWS_API_KEY)

    lines = [
        "🛠 Debug EvRadar PRO (cérebro v0.3-lite, odds reais + news + pré-jogo auto + jogadores)",
        "",
        "TELEGRAM_BOT_TOKEN definido: {v}".format(v="sim" if token_set else "não"),
        "TELEGRAM_CHAT_ID: {cid}".format(
            cid=TELEGRAM_CHAT_ID if chat_set else "não definido"
        ),
        "AUTOSTART: {a}".format(a=AUTOSTART),
        "CHECK_INTERVAL: {sec}s".format(sec=CHECK_INTERVAL),
        "Janela: {ws}–{we}ʼ".format(ws=WINDOW_START, we=WINDOW_END),
        "EV_MIN_PCT: {ev:.2f}%".format(ev=EV_MIN_PCT),
        "Faixa de odds: {mn:.2f}–{mx:.2f}".format(mn=MIN_ODD, mx=MAX_ODD),
        "Pressão mínima (score): {ps:.1f}".format(ps=MIN_PRESSURE_SCORE),
        "COOLDOWN_MINUTES: {cd} min".format(cd=COOLDOWN_MINUTES),
        "BANKROLL_INITIAL (banca virtual): R$ {bk:.2f}".format(bk=BANKROLL_INITIAL),
        "",
        "API_FOOTBALL_KEY definido: {v}".format(v="sim" if api_set else "não"),
        "USE_API_FOOTBALL_ODDS: {v}".format(v=USE_API_FOOTBALL_ODDS),
        "BOOKMAKER_ID: {bid}".format(bid=BOOKMAKER_ID),
        "ODDS_BET_ID: {obid}".format(obid=ODDS_BET_ID),
        "LEAGUE_IDS: {ids}".format(
            ids=",".join(str(x) for x in LEAGUE_IDS) if LEAGUE_IDS else "não definido"
        ),
        "",
        "NEWS_API_KEY definido: {v}".format(v="sim" if news_set else "não"),
        "USE_NEWS_API: {v}".format(v=USE_NEWS_API),
        "NEWS_TIME_WINDOW_HOURS: {h}".format(h=NEWS_TIME_WINDOW_HOURS),
        "",
        "USE_API_PREGAME: {v}".format(v=USE_API_PREGAME),
        "PREGAME_CACHE_HOURS: {h}".format(h=PREGAME_CACHE_HOURS),
        "Ratings pré-jogo manuais: {n} times".format(
            n=len(PREMATCH_TEAM_RATINGS)
        ),
        "",
        "USE_PLAYER_IMPACT: {v}".format(v=USE_PLAYER_IMPACT),
        "PLAYER_STATS_CACHE_HOURS: {h}".format(h=PLAYER_STATS_CACHE_HOURS),
        "PLAYER_EVENTS_CACHE_MINUTES: {m}".format(m=PLAYER_EVENTS_CACHE_MINUTES),
        "PLAYER_MAX_BOOST_PCT: {p:.1f}%".format(p=PLAYER_MAX_BOOST_PCT),
        "PLAYER_SUB_TRIGGER_WINDOW: {w} min".format(w=PLAYER_SUB_TRIGGER_WINDOW),
        "",
        "Último scan:",
        "  origem: {origin}".format(origin=last_scan_origin),
        "  eventos janela/ligas: {live}".format(live=last_scan_window_matches),
        "  alertas: {alerts}".format(alerts=last_scan_alerts),
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [
        "🔗 Links úteis",
        "",
        "Casa principal: {name}".format(name=BOOKMAKER_NAME),
        "Site: {url}".format(url=BOOKMAKER_URL),
    ]
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# post_init e main
# ---------------------------------------------------------------------------

async def post_init(application: Application) -> None:
    logging.info("Application started (post_init executado).")

    if AUTOSTART:
        application.create_task(autoscan_loop(application), name="autoscan_loop")


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN não definido. Configure a variável de ambiente antes de rodar."
        )

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )

    logging.info(
        "Iniciando bot do EvRadar PRO (cérebro v0.3-lite: odds reais + news + pré-jogo auto + jogadores)..."
    )

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("debug", cmd_debug))
    application.add_handler(CommandHandler("links", cmd_links))

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        stop_signals=None,
    )


if __name__ == "__main__":
    main()
