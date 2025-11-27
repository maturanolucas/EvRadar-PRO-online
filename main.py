#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EvRadar PRO - Telegram + Cérebro v0.2-lite
------------------------------------------
Base: EvRadar PRO - Base Telegram v0.1 (casca estável)
Esta versão adiciona um "cérebro" simplificado que:
- Consulta jogos ao vivo na API-FOOTBALL
- Aplica filtros de liga e janela de tempo
- Calcula um score de pressão/chances
- Estima uma probabilidade de 1 gol a mais
- Aproxima uma odd atual e calcula EV
- Dispara alertas no Telegram quando EV >= EV_MIN_PCT

IMPORTANTE:
- Ainda não é o modelo completo "parrudo" v0.2 (news, contexto avançado etc.),
  mas já é um cérebro real, com dados ao vivo.
- Usa apenas uma aproximação de odd (não integra Superbet ainda).
"""

import asyncio
import logging
import os
from typing import Optional, List, Dict, Any

import math
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------------------------------------------------------------------------
# Configuração básica via variáveis de ambiente
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

BOOKMAKER_NAME: str = _get_env_str("BOOKMAKER_NAME", "Superbet")
BOOKMAKER_URL: str = _get_env_str("BOOKMAKER_URL", "https://www.superbet.com/")

API_FOOTBALL_KEY: str = _get_env_str("API_FOOTBALL_KEY")
API_FOOTBALL_BASE_URL: str = _get_env_str(
    "API_FOOTBALL_BASE_URL",
    "https://v3.football.api-sports.io",
)

LEAGUE_IDS_RAW: str = _get_env_str("LEAGUE_IDS")
USE_API_FOOTBALL_ODDS: int = _get_env_int("USE_API_FOOTBALL_ODDS", 0)


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


LEAGUE_IDS: List[int] = _parse_league_ids(LEAGUE_IDS_RAW)

# ---------------------------------------------------------------------------
# Estado simples em memória
# ---------------------------------------------------------------------------

last_status_text: str = "Ainda não foi rodada nenhuma varredura."
last_scan_origin: str = "-"
last_scan_alerts: int = 0
last_scan_live_events: int = 0
last_scan_window_matches: int = 0


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
    """Busca jogos ao vivo na API-FOOTBALL."""
    if not API_FOOTBALL_KEY:
        logging.warning("API_FOOTBALL_KEY não definido; não há como buscar jogos ao vivo.")
        return []

    headers = {
        "x-apisports-key": API_FOOTBALL_KEY,
    }
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

            league_id = int(league.get("id"))
            if LEAGUE_IDS and league_id not in LEAGUE_IDS:
                continue

            status = fixture.get("status") or {}
            short = (status.get("short") or "").upper()
            elapsed = status.get("elapsed") or 0
            if elapsed is None:
                elapsed = 0

            # Apenas jogos no intervalo de tempo configurado
            if elapsed < WINDOW_START or elapsed > WINDOW_END:
                continue

            # Status ativos de jogo (1º ou 2º tempo)
            if short not in ("1H", "2H"):
                continue

            home_team = (teams.get("home") or {}).get("name") or "Home"
            away_team = (teams.get("away") or {}).get("name") or "Away"

            home_goals = goals.get("home")
            away_goals = goals.get("away")
            if home_goals is None:
                home_goals = 0
            if away_goals is None:
                away_goals = 0

            fixtures.append(
                {
                    "fixture_id": int(fixture.get("id")),
                    "league_id": league_id,
                    "league_name": league.get("name") or "",
                    "minute": int(elapsed),
                    "status_short": short,
                    "home_team": home_team,
                    "away_team": away_team,
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
    headers = {
        "x-apisports-key": API_FOOTBALL_KEY,
    }
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

    # A API geralmente retorna 2 entradas: home e away
    home = response[0]
    away = response[1]

    home_stats = home.get("statistics") or []
    away_stats = away.get("statistics") or []

    # Extração de algumas métricas básicas
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


def _estimate_prob_and_odd(
    minute: int,
    stats: Dict[str, Any],
    home_goals: int,
    away_goals: int,
) -> Dict[str, float]:
    """
    Estima probabilidade de +1 gol e uma odd "aproximada" com base em:
    - tempo de jogo
    - volume ofensivo (chutes, no alvo, ataques perigosos)
    - leve ajuste pelo placar.

    NÃO é modelo calibrado oficial, é uma v0.2-lite pra colocar o cérebro em campo.
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

    # Score de pressão (0–10) aproximado
    pressure_score = 0.0

    # Volume de chutes
    if total_shots >= 20:
        pressure_score += 3.0
    elif total_shots >= 14:
        pressure_score += 2.0
    elif total_shots >= 8:
        pressure_score += 1.0

    # Chutes no alvo
    if total_on >= 8:
        pressure_score += 3.0
    elif total_on >= 5:
        pressure_score += 2.0
    elif total_on >= 3:
        pressure_score += 1.0

    # Ataques perigosos
    if total_dang >= 50:
        pressure_score += 3.0
    elif total_dang >= 30:
        pressure_score += 2.0
    elif total_dang >= 18:
        pressure_score += 1.0

    # Pequeno ajuste por gols já marcados (jogo mais aberto)
    if total_goals >= 3:
        pressure_score += 1.0
    elif total_goals == 2:
        pressure_score += 0.5

    # Clipa score
    if pressure_score < 0.0:
        pressure_score = 0.0
    if pressure_score > 10.0:
        pressure_score = 10.0

    # Converte score em um "impulso" de probabilidade (0.35–0.80)
    # Base do 2º tempo: prob ~ 0.35
    base_prob = 0.35

    # Aumenta com pressão
    base_prob += (pressure_score / 10.0) * 0.35  # até +0.35

    # Ajuste por tempo restante (mais cedo no 2º tempo => mais tempo pra sair gol)
    # minuto ~ 47–75: mais cedo => mais prob.
    if minute <= 55:
        base_prob += 0.05
    elif minute <= 65:
        base_prob += 0.03
    elif minute <= 75:
        base_prob += 0.00
    else:
        base_prob -= 0.02

    # Clipa probabilidade em [0.20, 0.90]
    p_final = max(0.20, min(0.90, base_prob))

    # Odd justa = 1 / p
    odd_fair = 1.0 / p_final

    # Aproximação de odd atual:
    # - Começa por algo próximo da odd justa
    # - Joga dentro da janela [MIN_ODD, MAX_ODD]
    odd_current = odd_fair * 1.03  # leve margem da casa
    if odd_current < MIN_ODD:
        odd_current = MIN_ODD
    if odd_current > MAX_ODD:
        odd_current = MAX_ODD

    # EV em %
    ev = p_final * odd_current - 1.0
    ev_pct = ev * 100.0

    return {
        "p_final": p_final,
        "odd_fair": odd_fair,
        "odd_current": odd_current,
        "ev_pct": ev_pct,
        "pressure_score": pressure_score,
    }


def _format_alert_text(
    fixture: Dict[str, Any],
    metrics: Dict[str, float],
) -> str:
    """Formata texto do alerta no layout EvRadar."""
    jogo = "{home} vs {away} — {league}".format(
        home=fixture["home_team"],
        away=fixture["away_team"],
        league=fixture["league_name"],
    )
    minuto = fixture["minute"]
    placar = "{hg}–{ag}".format(hg=fixture["home_goals"], ag=fixture["away_goals"])
    total_goals = fixture["home_goals"] + fixture["away_goals"]
    linha = "Over (soma + 0,5)"  # padrão do projeto

    p_final = metrics["p_final"] * 100.0
    odd_fair = metrics["odd_fair"]
    odd_current = metrics["odd_current"]
    ev_pct = metrics["ev_pct"]
    pressure_score = metrics["pressure_score"]

    interpretacao_parts = []

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

    if ev_pct >= EV_MIN_PCT + 2.0:
        ev_flag = "EV+ forte"
    elif ev_pct >= EV_MIN_PCT:
        ev_flag = "EV+"
    else:
        ev_flag = "EV borderline"

    interpretacao_parts.append(ev_flag)

    interpretacao = " / ".join(interpretacao_parts)

    lines = [
        "🏟️ {jogo}".format(jogo=jogo),
        "⏱️ {minuto}' | 🔢 {placar}".format(minuto=minuto, placar=placar),
        "⚙️ Linha: {linha} @ {odd:.2f}".format(linha=linha, odd=odd_current),
        "📊 Probabilidade: {p:.1f}% | Odd justa: {odd_j:.2f}".format(
            p=p_final,
            odd_j=odd_fair,
        ),
        "💰 EV: {ev:.2f}%".format(ev=ev_pct),
        "",
        "🧩 Interpretação:",
        interpretacao,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Função principal de scan (CÉREBRO)
# ---------------------------------------------------------------------------

async def run_scan_cycle(origin: str, application: Application) -> List[str]:
    """
    Executa UM ciclo de varredura:
    - Busca jogos ao vivo na API-FOOTBALL
    - Aplica filtros de liga e janela
    - Calcula métrica de pressão/probabilidade/EV
    - Retorna lista de textos de alerta prontos para enviar no Telegram
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

        last_scan_live_events = len(fixtures)  # aqui já são apenas os da janela/ligas
        last_scan_window_matches = len(fixtures)

        alerts: List[str] = []

        for fx in fixtures:
            try:
                stats = await _fetch_statistics_for_fixture(client, fx["fixture_id"])
                if not stats:
                    continue

                metrics = _estimate_prob_and_odd(
                    minute=fx["minute"],
                    stats=stats,
                    home_goals=fx["home_goals"],
                    away_goals=fx["away_goals"],
                )

                if metrics["ev_pct"] < EV_MIN_PCT:
                    continue

                alert_text = _format_alert_text(fx, metrics)
                alerts.append(alert_text)
            except Exception:
                logging.exception("Erro ao processar fixture_id=%s", fx.get("fixture_id"))
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
    """Comando /start: mensagem de boas-vindas e resumo de config."""
    autoscan_status = "ativado" if AUTOSTART else "desativado"

    lines = [
        "👋 EvRadar PRO online (cérebro v0.2-lite + Telegram).",
        "",
        "Janela padrão: {ws}–{we}ʼ".format(ws=WINDOW_START, we=WINDOW_END),
        "EV mínimo: {ev:.2f}%".format(ev=EV_MIN_PCT),
        "Faixa de odds (aprox.): {mn:.2f}–{mx:.2f}".format(mn=MIN_ODD, mx=MAX_ODD),
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
    """Comando /scan: roda um ciclo de varredura manual com o cérebro v0.2-lite."""
    await update.message.reply_text(
        "🔍 Iniciando varredura manual de jogos ao vivo (cérebro v0.2-lite)..."
    )

    alerts = await run_scan_cycle(origin="manual", application=context.application)

    if not alerts:
        await update.message.reply_text(
            last_status_text
        )
        return

    for text in alerts:
        await update.message.reply_text(text)

    await update.message.reply_text(last_status_text)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /status: mostra o último resumo de varredura."""
    await update.message.reply_text(last_status_text)


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /debug: mostra informações técnicas básicas."""
    token_set = bool(TELEGRAM_BOT_TOKEN)
    chat_set = TELEGRAM_CHAT_ID is not None
    api_set = bool(API_FOOTBALL_KEY)

    lines = [
        "🛠 Debug EvRadar PRO (cérebro v0.2-lite)",
        "",
        "TELEGRAM_BOT_TOKEN definido: {v}".format(v="sim" if token_set else "não"),
        "TELEGRAM_CHAT_ID: {cid}".format(cid=TELEGRAM_CHAT_ID if chat_set else "não definido"),
        "AUTOSTART: {a}".format(a=AUTOSTART),
        "CHECK_INTERVAL: {sec}s".format(sec=CHECK_INTERVAL),
        "Janela: {ws}–{we}ʼ".format(ws=WINDOW_START, we=WINDOW_END),
        "EV_MIN_PCT: {ev:.2f}%".format(ev=EV_MIN_PCT),
        "Faixa de odds aprox.: {mn:.2f}–{mx:.2f}".format(mn=MIN_ODD, mx=MAX_ODD),
        "",
        "API_FOOTBALL_KEY definido: {v}".format(v="sim" if api_set else "não"),
        "LEAGUE_IDS: {ids}".format(ids=",".join(str(x) for x in LEAGUE_IDS) if LEAGUE_IDS else "não definido"),
        "",
        "Último scan:",
        "  origem: {origin}".format(origin=last_scan_origin),
        "  eventos janela/ligas: {live}".format(live=last_scan_window_matches),
        "  alertas: {alerts}".format(alerts=last_scan_alerts),
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /links: exibe link da casa / recursos úteis."""
    lines = [
        "🔗 Links úteis",
        "",
        "Casa principal: {name}".format(name=BOOKMAKER_NAME),
        "Site: {url}".format(url=BOOKMAKER_URL),
    ]
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# post_init e função principal
# ---------------------------------------------------------------------------

async def post_init(application: Application) -> None:
    """Executado automaticamente por run_polling após initialize()."""
    logging.info("Application started (post_init executado).")

    if AUTOSTART:
        # Inicia o loop de autoscan sem bloquear o polling.
        application.create_task(autoscan_loop(application), name="autoscan_loop")


def main() -> None:
    """Ponto de entrada do bot EvRadar PRO (cérebro v0.2-lite + Telegram)."""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN não definido. Configure a variável de ambiente antes de rodar."
        )

    # Configuração de logging
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )

    logging.info("Iniciando bot do EvRadar PRO (cérebro v0.2-lite + Telegram)...")

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Handlers de comando
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("debug", cmd_debug))
    application.add_handler(CommandHandler("links", cmd_links))

    # Inicia polling (loop principal)
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        stop_signals=None,  # deixa o host (Railway/Replit) matar o processo
    )


if __name__ == "__main__":
    main()
