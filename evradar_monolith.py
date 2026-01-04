#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EvRadar PRO - Telegram + Cérebro v0.4-SENSIBLE
-----------------------------------------------------
VERSÃO SENSÍVEL: Ajustada para gerar mais alertas mantendo qualidade.
Principais mudanças:
1. Pressão mínima reduzida
2. Filtro de empate relaxado
3. Janela estendida
4. Bloqueios reduzidos
5. Odds máxima aumentada
6. Exceções simplificadas
"""

import asyncio
import contextlib
import signal
import logging
import os
import json
import tempfile
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timedelta, timezone

# Tratamento para zoneinfo (compatibilidade com Python 3.8+)
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

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
    except (TypeError, ValueError):
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
# Variáveis de ambiente com valores sensíveis
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN: str = _get_env_str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: Optional[int] = None
_chat_raw = _get_env_str("TELEGRAM_CHAT_ID")
if _chat_raw:
    try:
        TELEGRAM_CHAT_ID = int(_chat_raw)
    except ValueError:
        TELEGRAM_CHAT_ID = None

AUTOSTART: int = _get_env_int("AUTOSTART", 1)
CHECK_INTERVAL: int = _get_env_int("CHECK_INTERVAL", 60)
HTTP_TIMEOUT: float = _get_env_float("HTTP_TIMEOUT", 10.0)

# JANELA ESTENDIDA para pegar finais de jogo
WINDOW_START: int = _get_env_int("WINDOW_START", 55)
WINDOW_END: int = _get_env_int("WINDOW_END", 85)  # Aumentado de 74 para 85

# EV mínimo ZERO para não bloquear por EV
EV_MIN_PCT: float = _get_env_float("EV_MIN_PCT", 0.0)

# ODDS MÁXIMA AUMENTADA para Over
MIN_ODD: float = _get_env_float("MIN_ODD", 1.47)
MAX_ODD: float = _get_env_float("MAX_ODD", 3.00)  # Aumentado de 2.30 para 3.00

# Watch/observação: AGORA ENVIA sinais
ALLOW_WATCH_ALERTS: int = _get_env_int("ALLOW_WATCH_ALERTS", 1)

# BLOQUEIOS REDUZIDOS
BLOCK_FAVORITE_LEADING: int = _get_env_int("BLOCK_FAVORITE_LEADING", 0)  # Desativado
BLOCK_LEAD_BY_2: int = _get_env_int("BLOCK_LEAD_BY_2", 0)  # Desativado
LEAD_BY_2_MINUTE: int = _get_env_int("LEAD_BY_2_MINUTE", 55)
BLOCK_SUPER_UNDER_LEADING: int = _get_env_int("BLOCK_SUPER_UNDER_LEADING", 0)  # Desativado

# PRESSÃO MÍNIMA REDUZIDA
MIN_PRESSURE_SCORE: float = _get_env_float("MIN_PRESSURE_SCORE", 1.5)  # Reduzido de 2.5

# Cooldown reduzido
COOLDOWN_MINUTES: int = _get_env_int("COOLDOWN_MINUTES", 5)  # Reduzido de 9

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

USE_API_FOOTBALL_ODDS: int = _get_env_int("USE_API_FOOTBALL_ODDS", 1)
BOOKMAKER_ID: int = _get_env_int("BOOKMAKER_ID", 34)
BOOKMAKER_FALLBACK_IDS_RAW: str = _get_env_str("BOOKMAKER_FALLBACK_IDS", "")
BOOKMAKER_FALLBACK_IDS: List[int] = _parse_league_ids(BOOKMAKER_FALLBACK_IDS_RAW)
ODDS_BET_ID: int = _get_env_int("ODDS_BET_ID", 0)

# NewsAPI
NEWS_API_KEY: str = _get_env_str("NEWS_API_KEY")
USE_NEWS_API: int = _get_env_int("USE_NEWS_API", 1)
NEWS_TIME_WINDOW_HOURS: int = _get_env_int("NEWS_TIME_WINDOW_HOURS", 30)

# Pré-jogo auto
USE_API_PREGAME: int = _get_env_int("USE_API_PREGAME", 0)
PREGAME_CACHE_HOURS: int = _get_env_int("PREGAME_CACHE_HOURS", 12)

# Impacto de jogadores
USE_PLAYER_IMPACT: int = _get_env_int("USE_PLAYER_IMPACT", 1)
PLAYER_STATS_CACHE_HOURS: int = _get_env_int("PLAYER_STATS_CACHE_HOURS", 24)
PLAYER_EVENTS_CACHE_MINUTES: int = _get_env_int("PLAYER_EVENTS_CACHE_MINUTES", 4)
PLAYER_MAX_BOOST_PCT: float = _get_env_float("PLAYER_MAX_BOOST_PCT", 6.0)
PLAYER_SUB_TRIGGER_WINDOW: int = _get_env_int("PLAYER_SUB_TRIGGER_WINDOW", 22)

# The Odds API
ODDS_API_KEY: str = _get_env_str("ODDS_API_KEY")
ODDS_API_USE: int = _get_env_int("ODDS_API_USE", 0)
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

ODDS_API_DAILY_LIMIT: int = _get_env_int("ODDS_API_DAILY_LIMIT", 15)
ALLOW_ALERTS_WITHOUT_ODDS: int = _get_env_int("ALLOW_ALERTS_WITHOUT_ODDS", 1)
MANUAL_MIN_ODD_HINT: float = _get_env_float("MANUAL_MIN_ODD_HINT", 1.47)

# Favorito pré-live
USE_PRELIVE_FAVORITE: int = _get_env_int("USE_PRELIVE_FAVORITE", 1)
PRELIVE_CACHE_HOURS: int = _get_env_int("PRELIVE_CACHE_HOURS", 24)
PRELIVE_ODDS_BOOKMAKER_ID: int = _get_env_int("PRELIVE_ODDS_BOOKMAKER_ID", 6)

# Thresholds do favorito
PRELIVE_ELITE_MAX_ODD: float = _get_env_float("PRELIVE_ELITE_MAX_ODD", 1.25)
PRELIVE_SUPER_MAX_ODD: float = _get_env_float("PRELIVE_SUPER_MAX_ODD", 1.45)
PRELIVE_STRONG_MAX_ODD: float = _get_env_float("PRELIVE_STRONG_MAX_ODD", 1.65)
PRELIVE_FAVORITE_MAX_ODD: float = _get_env_float("PRELIVE_FAVORITE_MAX_ODD", 1.85)
PRELIVE_LIGHT_MAX_ODD: float = _get_env_float("PRELIVE_LIGHT_MAX_ODD", 2.15)

FAVORITE_BLOCK_MIN_STRENGTH: int = _get_env_int("FAVORITE_BLOCK_MIN_STRENGTH", 2)
FAVORITE_LEAD_BLOCK_GOALS: int = _get_env_int("FAVORITE_LEAD_BLOCK_GOALS", 1)
FAVORITE_LEAD_EXCEPTION_ENABLE: int = _get_env_int("FAVORITE_LEAD_EXCEPTION_ENABLE", 1)
FAVORITE_LEAD_EXC_MIN_PRESSURE_DELTA: float = _get_env_float("FAVORITE_LEAD_EXC_MIN_PRESSURE_DELTA", 1.0)  # Reduzido
FAVORITE_LEAD_EXC_OPP_ATTACK_MIN: float = _get_env_float("FAVORITE_LEAD_EXC_OPP_ATTACK_MIN", 1.80)
FAVORITE_LEAD_EXC_FAV_DEF_MIN: float = _get_env_float("FAVORITE_LEAD_EXC_FAV_DEF_MIN", 1.50)
FAVORITE_LEAD_EXC_ALLOW_ONLY_LEAD1: int = _get_env_int("FAVORITE_LEAD_EXC_ALLOW_ONLY_LEAD1", 1)

# Warmup pré-live
PRELIVE_CACHE_FILE: str = _get_env_str("PRELIVE_CACHE_FILE", "prelive_cache.json")
PRELIVE_WARMUP_ENABLE: int = _get_env_int("PRELIVE_WARMUP_ENABLE", 1)
PRELIVE_WARMUP_INTERVAL_MIN: int = _get_env_int("PRELIVE_WARMUP_INTERVAL_MIN", 30)
API_FOOTBALL_TIMEZONE: str = _get_env_str("API_FOOTBALL_TIMEZONE", "America/Sao_Paulo")
HTTPX_TIMEOUT: float = _get_env_float("HTTPX_TIMEOUT", 20.0)
HTTPX_RETRY: int = _get_env_int("HTTPX_RETRY", 1)
PRELIVE_LOOKAHEAD_HOURS: int = _get_env_int("PRELIVE_LOOKAHEAD_HOURS", 72)
PRELIVE_WARMUP_MAX_FIXTURES: int = _get_env_int("PRELIVE_WARMUP_MAX_FIXTURES", 80)
PRELIVE_NEGATIVE_TTL_MIN: int = _get_env_int("PRELIVE_NEGATIVE_TTL_MIN", 20)
PRELIVE_FORCE_REFRESH_HOURS: int = _get_env_int("PRELIVE_FORCE_REFRESH_HOURS", 8)
PRELIVE_MATCH_WINNER_BET_ID: int = _get_env_int("PRELIVE_MATCH_WINNER_BET_ID", 1)

# Heurísticas relaxadas
FAVORITE_RATING_THRESH: float = _get_env_float("FAVORITE_RATING_THRESH", 0.10)  # Reduzido
FAVORITE_POWER_THRESH: float = _get_env_float("FAVORITE_POWER_THRESH", 0.15)  # Reduzido
BLOCK_UNDER_TRAILER_VS_SOLID_DEF: int = _get_env_int("BLOCK_UNDER_TRAILER_VS_SOLID_DEF", 0)  # Desativado
UNDER_ATTACK_MAX: float = _get_env_float("UNDER_ATTACK_MAX", 1.80)
SOLID_DEFENSE_MAX: float = _get_env_float("SOLID_DEFENSE_MAX", 1.80)

# Desconfiança em linhas altas REDUZIDA
HIGH_LINE_START: float = _get_env_float("HIGH_LINE_START", 4.5)  # Aumentado de 3.5
HIGH_LINE_STEP_MALUS_PROB: float = _get_env_float("HIGH_LINE_STEP_MALUS_PROB", 0.003)  # Reduzido
HIGH_LINE_PRESSURE_STEP: float = _get_env_float("HIGH_LINE_PRESSURE_STEP", 0.5)  # Reduzido

# ---------------------------------------------------------------------------
# NOVA FUNÇÃO: Filtro de empate relaxado
# ---------------------------------------------------------------------------

def _should_allow_draw(
    fixture: Dict[str, Any],
    metrics: Dict[str, float],
    attack_home_gpm: float,
    defense_home_gpm: float,
    attack_away_gpm: float,
    defense_away_gpm: float,
    rating_home: float,
    rating_away: float,
    context_boost_prob: float,
    minute_int: int,
) -> bool:
    """
    Filtro de empate RELAXADO - permite mais cenários
    """
    score_diff = (fixture.get("home_goals") or 0) - (fixture.get("away_goals") or 0)
    if score_diff != 0:
        return True  # Não é empate
    
    # 1. Pressão muito alta (>7.0) sempre permite
    if metrics["pressure_score"] >= 7.0:
        return True
    
    # 2. Jogo com times over (ataque forte)
    home_over = attack_home_gpm >= 1.8 or defense_home_gpm >= 1.6
    away_over = attack_away_gpm >= 1.8 or defense_away_gpm >= 1.6
    if home_over and away_over:
        return True
    
    # 3. Contexto positivo (favorito precisa de gol)
    if context_boost_prob >= 0.02:
        return True
    
    # 4. Minuto avançado (>60) com pressão moderada
    if minute_int >= 60 and metrics["pressure_score"] >= 4.0:
        return True
    
    # 5. Rating dos times indica jogo aberto
    if rating_home >= 0.5 or rating_away >= 0.5:
        return True
    
    # 6. Chance muito alta de gol (probabilidade > 45%)
    if metrics["p_final"] >= 0.45:
        return True
    
    # Por padrão, bloqueia apenas jogos muito travados
    return False

# ---------------------------------------------------------------------------
# FUNÇÃO MODIFICADA: run_scan_cycle com filtros relaxados
# ---------------------------------------------------------------------------

async def run_scan_cycle_relaxed(origin: str, application: Application) -> List[str]:
    """
    Versão relaxada da função principal de scan.
    """
    global last_status_text, last_scan_origin, last_scan_alerts
    global last_scan_live_events, last_scan_window_matches

    last_scan_origin = origin
    last_scan_live_events = 0
    last_scan_window_matches = 0
    last_scan_alerts = 0

    block_counters = {
        "favorite_leading": 0,
        "super_under_draw": 0,
        "no_live_data": 0,
        "under_team_no_munition": 0,
        "goalfest": 0,
        "draw_filter": 0,
        "pressure_threshold": 0,
        "odd_threshold": 0,
        "cooldown": 0,
        "ev_threshold": 0,
        "goleada": 0,
        "context_negative": 0,
        "mandante_under_vencendo": 0,
        "linha_alta_malus": 0,
    }

    if not API_FOOTBALL_KEY:
        last_status_text = "[EvRadar PRO] API_FOOTBALL_KEY não definido."
        return []

    async with httpx.AsyncClient() as client:
        fixtures = await _fetch_live_fixtures(client)
        last_scan_live_events = len(fixtures)
        alerts: List[str] = []

        for fx in fixtures:
            try:
                # Verificação básica de janela
                minute_int = fx.get("minute") or 0
                if minute_int < WINDOW_START or minute_int > WINDOW_END:
                    continue

                stats = await _fetch_statistics_for_fixture(client, fx["fixture_id"])
                if not stats:
                    block_counters["no_live_data"] += 1
                    continue

                # Boosts
                news_boost_prob = 0.0
                try:
                    news_boost_prob = await _fetch_news_boost_for_fixture(client, fx)
                except Exception:
                    pass

                pregame_boost_prob, context_boost_prob, rating_home, rating_away = 0.0, 0.0, 0.0, 0.0
                try:
                    (
                        pregame_boost_prob,
                        context_boost_prob,
                        rating_home,
                        rating_away,
                    ) = await _get_pregame_boost_for_fixture(client, fx)
                except Exception:
                    pass

                # Perfis de ataque/defesa
                attack_home_gpm = _to_float(fx.get("attack_home_gpm", 0.0), 0.0)
                defense_home_gpm = _to_float(fx.get("defense_home_gpm", 0.0), 0.0)
                attack_away_gpm = _to_float(fx.get("attack_away_gpm", 0.0), 0.0)
                defense_away_gpm = _to_float(fx.get("defense_away_gpm", 0.0), 0.0)

                # Cálculo simplificado de probabilidade
                total_goals = fx["home_goals"] + fx["away_goals"]
                home_goals = fx["home_goals"]
                away_goals = fx["away_goals"]
                
                # Pressão ajustada
                pressure_score = _calculate_pressure_score_simple(stats)
                
                # Base probability mais alta
                base_prob = 0.40  # Aumentado de 0.38
                base_prob += (pressure_score / 10.0) * 0.40  # Aumentado de 0.37
                
                # Boost por tempo
                if minute_int <= 55:
                    base_prob += 0.06
                elif minute_int <= 65:
                    base_prob += 0.04
                elif minute_int <= 75:
                    base_prob += 0.01
                
                # Boosts
                base_prob += news_boost_prob
                base_prob += pregame_boost_prob
                base_prob += context_boost_prob
                
                # Boost Lucas (relaxado)
                lucas_boost = _compute_lucas_pattern_boost_relaxed(
                    minute_int, home_goals, away_goals, pressure_score, context_boost_prob
                )
                base_prob += lucas_boost
                
                # Malus linha alta (reduzido)
                linha_num = total_goals + 0.5
                if linha_num >= HIGH_LINE_START:
                    steps_high = int((linha_num - 2.5) // 1.0)
                    base_prob -= steps_high * HIGH_LINE_STEP_MALUS_PROB
                
                p_final = max(0.25, min(0.85, base_prob))
                
                # Odd e EV
                odd_fair = 1.0 / p_final
                odd_current = odd_fair  # Fallback
                
                # Tenta buscar odd real
                api_odd = None
                try:
                    api_odd = await _fetch_live_odds_for_fixture(
                        client, fx["fixture_id"], total_goals
                    )
                except Exception:
                    pass
                
                if api_odd is not None and api_odd > 1.0:
                    odd_current = api_odd
                
                ev_pct = p_final * odd_current - 1.0
                
                metrics = {
                    "p_final": p_final,
                    "odd_fair": odd_fair,
                    "odd_current": odd_current,
                    "ev_pct": ev_pct,
                    "pressure_score": pressure_score,
                    "context_boost_prob": context_boost_prob,
                    "lucas_boost_prob": lucas_boost,
                }
                
                # FILTROS RELAXADOS
                score_diff = home_goals - away_goals
                
                # 1. Filtro de pressão (RELAXADO)
                if pressure_score < MIN_PRESSURE_SCORE:
                    block_counters["pressure_threshold"] += 1
                    continue
                
                # 2. Filtro de empate (RELAXADO)
                if score_diff == 0:
                    if not _should_allow_draw(
                        fx, metrics, attack_home_gpm, defense_home_gpm,
                        attack_away_gpm, defense_away_gpm, rating_home,
                        rating_away, context_boost_prob, minute_int
                    ):
                        block_counters["draw_filter"] += 1
                        continue
                
                # 3. Filtro de goleada (APENAS diferença >= 4)
                if abs(score_diff) >= 4 and minute_int >= 60:
                    block_counters["goleada"] += 1
                    continue
                
                # 4. Filtro de odd (RELAXADO)
                if api_odd is not None:
                    if api_odd < MIN_ODD:
                        if ALLOW_WATCH_ALERTS:
                            alert_text = _format_watch_text(fx, metrics)
                            alerts.append(alert_text)
                        continue
                    elif api_odd > MAX_ODD:
                        block_counters["odd_threshold"] += 1
                        continue
                
                # 5. Filtro de EV (ZERO - desativado)
                if ev_pct < EV_MIN_PCT and api_odd is not None:
                    block_counters["ev_threshold"] += 1
                    continue
                
                # 6. Cooldown
                now = _now_utc()
                cd_key = _cooldown_key(fx["fixture_id"], home_goals, away_goals)
                last_ts = fixture_last_alert_at.get(cd_key)
                if last_ts is not None and (now - last_ts) < timedelta(minutes=COOLDOWN_MINUTES):
                    block_counters["cooldown"] += 1
                    continue
                
                # GERAR ALERTA
                if api_odd is None and ALLOW_ALERTS_WITHOUT_ODDS:
                    alert_text = _format_manual_no_odds_text(fx, metrics)
                elif api_odd is not None:
                    alert_text = _format_alert_text(fx, metrics)
                else:
                    continue
                
                alerts.append(alert_text)
                fixture_last_alert_at[cd_key] = now
                last_scan_alerts += 1
                
            except Exception as e:
                logging.error(f"Erro ao processar fixture {fx.get('fixture_id')}: {e}")
                continue

    # Status
    block_summary = "; ".join([f"{k}:{v}" for k, v in block_counters.items() if v > 0])
    last_status_text = (
        f"[EvRadar PRO-SENSIBLE] Scan {origin}. "
        f"Jogos: {last_scan_live_events} | Alertas: {last_scan_alerts} | Bloqueios: {block_summary}"
    )
    
    return alerts

# ---------------------------------------------------------------------------
# Funções auxiliares relaxadas
# ---------------------------------------------------------------------------

def _calculate_pressure_score_simple(stats: Dict[str, Any]) -> float:
    """Cálculo de pressão SIMPLIFICADO e mais sensível."""
    home_shots = stats.get("home_shots_total", 0)
    away_shots = stats.get("away_shots_total", 0)
    home_on = stats.get("home_shots_on", 0)
    away_on = stats.get("away_shots_on", 0)
    home_dang = stats.get("home_dangerous", 0)
    away_dang = stats.get("away_dangerous", 0)

    total_shots = home_shots + away_shots
    total_on = home_on + away_on
    total_dang = home_dang + away_dang

    pressure = 0.0
    
    # Chutes totais (limites reduzidos)
    if total_shots >= 8:
        pressure += 3.0
    elif total_shots >= 5:
        pressure += 2.0
    elif total_shots >= 3:
        pressure += 1.0
    
    # Chutes no alvo (limites reduzidos)
    if total_on >= 3:
        pressure += 3.0
    elif total_on >= 2:
        pressure += 2.0
    elif total_on >= 1:
        pressure += 1.0
    
    # Ataques perigosos (limites reduzidos)
    if total_dang >= 20:
        pressure += 3.0
    elif total_dang >= 12:
        pressure += 2.0
    elif total_dang >= 6:
        pressure += 1.0
    
    return min(pressure, 10.0)

def _compute_lucas_pattern_boost_relaxed(
    minute: int,
    home_goals: int,
    away_goals: int,
    pressure_score: float,
    context_boost_prob: float,
) -> float:
    """Boost Lucas RELAXADO."""
    total_goals = home_goals + away_goals
    score_diff = home_goals - away_goals
    
    boost = 0.0
    
    # Pressão
    if pressure_score >= 6.0:
        boost += 0.04
    elif pressure_score >= 4.0:
        boost += 0.02
    
    # Placar
    if abs(score_diff) <= 1:
        boost += 0.02
    if total_goals == 0 and pressure_score >= 4.0:
        boost += 0.03
    
    # Janela
    if 55 <= minute <= 75:
        boost += 0.02
    
    # Contexto
    if context_boost_prob > 0.0:
        boost += min(context_boost_prob * 0.4, 0.02)
    
    return min(boost, 0.08)

# ---------------------------------------------------------------------------
# Comando /scan atualizado
# ---------------------------------------------------------------------------

async def cmd_scan_relaxed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando scan com versão relaxada."""
    try:
        if update.effective_chat:
            await update.effective_chat.send_message(
                "🔍 Varredura RELAXADA iniciada (filtros sensíveis)..."
            )
    except Exception:
        pass

    alerts: List[str] = []
    try:
        alerts = await run_scan_cycle_relaxed("manual_relaxed", context.application)
    except Exception as e:
        logging.error(f"Erro no scan relaxado: {e}")

    if update.effective_chat and alerts:
        for text in alerts:
            try:
                await update.effective_chat.send_message(text)
            except Exception:
                pass

    try:
        resumo = last_status_text
        if update.effective_chat:
            await update.effective_chat.send_message(resumo)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Main atualizado
# ---------------------------------------------------------------------------

async def autoscan_loop_relaxed(application: Application) -> None:
    """Loop de autoscan com versão relaxada."""
    logging.info("Autoscan RELAXADO iniciado (intervalo=%ss)", CHECK_INTERVAL)
    while True:
        try:
            alerts = await run_scan_cycle_relaxed("auto_relaxed", application)
            if TELEGRAM_CHAT_ID and alerts:
                for msg in alerts:
                    try:
                        await application.bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=msg,
                        )
                    except Exception:
                        pass
        except Exception as e:
            logging.error(f"Erro no autoscan relaxado: {e}")
        await asyncio.sleep(CHECK_INTERVAL)

async def post_init_relaxed(application: Application) -> None:
    """Inicialização relaxada."""
    _load_prelive_cache_from_file()
    
    if AUTOSTART:
        asyncio.create_task(autoscan_loop_relaxed(application))
        logging.info("Autoscan relaxado iniciado.")
    
    if PRELIVE_WARMUP_ENABLE and USE_PRELIVE_FAVORITE:
        asyncio.create_task(prelive_warmup_loop(application))
        logging.info("Prelive warmup iniciado.")

def main_relaxed() -> None:
    """Main relaxado."""
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    
    if not TELEGRAM_BOT_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN não definido.")
        return
    
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init_relaxed)
        .build()
    )
    
    # Comandos
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("scan", cmd_scan_relaxed))  # Usa versão relaxada
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("debug", cmd_debug))
    application.add_handler(CommandHandler("links", cmd_links))
    application.add_handler(CommandHandler("prelive", cmd_prelive))
    application.add_handler(CommandHandler("prelive_next", cmd_prelive_next))
    application.add_handler(CommandHandler("prelive_show", cmd_prelive_show))
    application.add_handler(CommandHandler("prelive_status", cmd_prelive_status))
    
    logging.info("EvRadar PRO v0.4-SENSIBLE iniciando...")
    
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except KeyboardInterrupt:
        logging.info("Bot interrompido.")
    finally:
        logging.info("EvRadar PRO encerrado.")

if __name__ == "__main__":
    main_relaxed()
