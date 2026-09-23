from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, PreCheckoutQueryHandler, filters)
import asyncio
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
import sqlite3
from pathlib import Path


# Seguridad: el token NUNCA debe vivir dentro del archivo.
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

# Configuración del envío automático diario.
AUTO_CHAT_ID = os.environ.get("AUTO_CHAT_ID", "").strip()
AUTO_HOUR = int(os.environ.get("AUTO_HOUR", "8"))
AUTO_MINUTE = int(os.environ.get("AUTO_MINUTE", "0"))
AUTO_TZ = os.environ.get("AUTO_TZ", "America/Puerto_Rico").strip()


def resolve_timezone(name):
    """Resolve an IANA timezone, with a safe Puerto Rico fallback for Windows."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # Windows Python installations may not ship the IANA tz database.
        # Puerto Rico is UTC-4 year-round and does not observe DST.
        if name in {"America/Puerto_Rico", "US/Puerto_Rico"}:
            print(
                "⚠️ tzdata no disponible; usando UTC-04:00 fijo para Puerto Rico. "
                "Para soporte completo de zonas horarias instala: pip install tzdata"
            )
            return timezone(timedelta(hours=-4), name="America/Puerto_Rico")
        raise RuntimeError(
            f"No se encontró la zona horaria {name!r}. "
            "Instala tzdata con: pip install tzdata"
        )


LOCAL_TZ = resolve_timezone(AUTO_TZ)

MLB_API = "https://statsapi.mlb.com/api/v1"
HTTP_CACHE_TTL = int(os.environ.get("MLB_CACHE_TTL", "300"))
_HTTP_CACHE = {}
_HTTP = requests.Session()

# Triple Pick v2.8 Market Engine.
# The Odds API is optional at startup: without a key the bot keeps v2.7.1
# model behavior and clearly reports MARKET OFF instead of fabricating prices.
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
ODDS_API_KEY = (
    os.environ.get("ODDS_API_KEY", "").strip()
    or os.environ.get("THE_ODDS_API_KEY", "").strip()
)
ODDS_PRIMARY_BOOKMAKER = os.environ.get(
    "ODDS_PRIMARY_BOOKMAKER", "hardrockbet_fl"
).strip()
ODDS_BOOKMAKERS = os.environ.get(
    "ODDS_BOOKMAKERS", "hardrockbet_fl,fanduel,draftkings"
).strip()
ODDS_CACHE_TTL = int(os.environ.get("ODDS_CACHE_TTL", "120"))
_ODDS_CACHE = {}
_ODDS_LAST_META = {"remaining": None, "used": None, "last": None, "error": None}


# Triple Pick v2.9.7 — Telegram + optional Twilio SMS pregame alerts.
BOT_VERSION = "3.1.0"
MODEL_VERSION = "MLB_MODEL_2.7.1_PROXY"
RAILWAY_VOLUME_MOUNT_PATH = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
TRACK_DB_PATH = os.environ.get("TRACK_DB_PATH", "").strip()
if not TRACK_DB_PATH:
    TRACK_DB_PATH = (
        str(Path(RAILWAY_VOLUME_MOUNT_PATH) / "triple_pick_tracking.sqlite3")
        if RAILWAY_VOLUME_MOUNT_PATH
        else "triple_pick_tracking.sqlite3"
    )
TRACK_AUTO_SETTLE = os.environ.get("TRACK_AUTO_SETTLE", "1").strip().lower() not in {
    "0", "false", "no", "off"
}
TRACK_SETTLE_HOUR = int(os.environ.get("TRACK_SETTLE_HOUR", "4"))
TRACK_SETTLE_MINUTE = int(os.environ.get("TRACK_SETTLE_MINUTE", "30"))

# Triple Pick Membership / Telegram Stars
# Subscription prices are configured in Stars, not USD.
SUBSCRIPTION_PERIOD = 30 * 24 * 60 * 60  # Telegram currently requires exactly 30 days.
FREE_TRIAL_PERIOD = 30 * 24 * 60 * 60  # One-time 30-day trial per Telegram user ID.
PREMIUM_TARGET_USD = 10
PRO_TARGET_USD = 20
PREMIUM_STARS = int(os.environ.get("PREMIUM_STARS", "300"))
PRO_STARS = int(os.environ.get("PRO_STARS", "500"))
SUBSCRIPTION_ENFORCE = os.environ.get("SUBSCRIPTION_ENFORCE", "0").strip().lower() in {
    "1", "true", "yes", "on"
}
SUBSCRIPTION_ADMIN_IDS = {
    int(x.strip())
    for x in os.environ.get("SUBSCRIPTION_ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}
PLAN_RANK = {"FREE": 0, "PREMIUM": 1, "PRO": 2}


# Triple Pick v2.9.7 — Telegram pregame alerts + optional Twilio SMS channel.
ALERT_LEAD_MINUTES = int(os.environ.get("ALERT_LEAD_MINUTES", "45"))
ALERTS_DEFAULT_ENABLED = os.environ.get("ALERTS_DEFAULT_ENABLED", "0").strip().lower() in {
    "1", "true", "yes", "on"
}

# Optional Twilio SMS channel. Secrets stay in Railway environment variables.
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "").strip()
TWILIO_MESSAGING_SERVICE_SID = os.environ.get("TWILIO_MESSAGING_SERVICE_SID", "").strip()
SMS_DEFAULT_TO_NUMBER = os.environ.get("SMS_TO_NUMBER", "").strip()
SMS_DEFAULT_ENABLED = os.environ.get("SMS_DEFAULT_ENABLED", "0").strip().lower() in {
    "1", "true", "yes", "on"
}


# MLB Triple Pick v2.9.5 - v2.8 Market Engine + primary-feed degradation notice + odds diagnostics + persistent tracking/calibration layer
#
# v2.7.1 preserves the v2.7 starter sample/recency engine and the 45/25/15/10/5
# matchup structure. It makes 100/100 Data Reliability unavailable until both
# lineups are published, calibrates Confidence 2.1 more conservatively, and adds
# /pool to audit every game, gate, and rejection reason in the slate.


async def mlb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fecha = local_now().strftime("%Y-%m-%d")

    datos = await asyncio.to_thread(
        safe_get_json,
        f"{MLB_API}/schedule",
        {
            "sportId": 1,
            "date": fecha,
            "hydrate": "probablePitcher",
        },
        60,
    )

    if not datos:
        await update.message.reply_text("❌ No pude conectar con la API de MLB.")
        return

    juegos = datos.get("dates", [])
    if not juegos:
        await update.message.reply_text(
            "⚾ No hay juegos de MLB programados para hoy."
        )
        return

    mensaje = f"⚾ MLB — JUEGOS DE HOY\n📅 {fecha}\n\n"

    for fecha_juegos in juegos:
        for juego in fecha_juegos.get("games", []):
            try:
                visitante = juego["teams"]["away"]["team"]["name"]
                local = juego["teams"]["home"]["team"]["name"]
            except (KeyError, TypeError):
                continue

            hora = format_game_time_local(juego.get("gameDate"))
            mensaje += f"🕐 {hora} — {visitante} vs {local}\n"

    await update.message.reply_text(mensaje)



def local_now():
    """Return timezone-aware current datetime in the configured local timezone."""
    return datetime.now(LOCAL_TZ)


def format_game_time_local(game_date):
    """Convert MLB UTC gameDate to the configured local timezone."""
    if not game_date:
        return "N/D"
    try:
        dt_utc = datetime.fromisoformat(game_date.replace("Z", "+00:00"))
        return dt_utc.astimezone(LOCAL_TZ).strftime("%I:%M %p").lstrip("0")
    except (TypeError, ValueError):
        return "N/D"


def safe_get_json(url, params=None, cache_ttl=HTTP_CACHE_TTL):
    """GET JSON from MLB Stats API with a small in-memory TTL cache."""
    params = params or {}
    key = (url, tuple(sorted((str(k), str(v)) for k, v in params.items())))
    now = time.monotonic()

    cached = _HTTP_CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1]

    try:
        response = _HTTP.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if cache_ttl and cache_ttl > 0:
            _HTTP_CACHE[key] = (now + cache_ttl, data)
        return data
    except requests.RequestException as exc:
        print(f"MLB API error: {exc}")
        return None


def innings_to_outs(value):
    """Convert MLB innings notation (e.g. 5.2 = 5 innings + 2 outs) to outs."""
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    try:
        if "." not in text:
            return max(0, int(text) * 3)
        whole_text, frac_text = text.split(".", 1)
        whole = int(whole_text or 0)
        frac = int((frac_text or "0")[0])
        # MLB legal fractional innings are .0, .1 and .2. Be conservative otherwise.
        if frac not in (0, 1, 2):
            frac = 0
        return max(0, whole * 3 + frac)
    except (TypeError, ValueError):
        return 0


def outs_to_innings(outs):
    """Convert outs to true decimal innings for rate calculations."""
    return max(0, int(outs)) / 3.0


def format_baseball_innings(innings):
    """Render true decimal innings back to baseball notation."""
    outs = int(round(max(0.0, float(innings or 0.0)) * 3))
    return f"{outs // 3}.{outs % 3}"


def get_current_pitcher_stats(pitcher_id, season):
    """Return current-season pitching stats for a pitcher."""
    if not pitcher_id:
        return None

    data = safe_get_json(
        f"{MLB_API}/people/{pitcher_id}/stats",
        params={
            "stats": "season",
            "group": "pitching",
            "season": season,
        },
    )

    stats = data.get("stats", []) if data else []
    if not stats:
        return None

    splits = stats[0].get("splits", [])
    if not splits:
        return None

    stat = splits[0].get("stat", {})
    try:
        return {
            "era": float(stat.get("era", 99.0)),
            "whip": float(stat.get("whip", 99.0)),
            "strikeouts": int(stat.get("strikeOuts", 0)),
            "walks": int(stat.get("baseOnBalls", 0)),
            "innings": outs_to_innings(innings_to_outs(stat.get("inningsPitched", "0"))),
            "wins": int(stat.get("wins", 0)),
            "losses": int(stat.get("losses", 0)),
        }
    except (TypeError, ValueError):
        return None


def get_recent_pitcher_form(pitcher_id, season, starts=5):
    """
    Aggregate the pitcher's most recent MLB starts from the season game log.

    This is deliberately optional. If MLB does not expose a usable game log for
    the pitcher, v2.7 falls back to season stats instead of inventing recency.
    """
    if not pitcher_id:
        return None

    data = safe_get_json(
        f"{MLB_API}/people/{pitcher_id}/stats",
        params={
            "stats": "gameLog",
            "group": "pitching",
            "season": season,
        },
    )

    blocks = data.get("stats", []) if data else []
    rows = []
    for block in blocks:
        for split in block.get("splits", []):
            stat = split.get("stat", {})
            try:
                games_started = int(stat.get("gamesStarted", 0) or 0)
            except (TypeError, ValueError):
                games_started = 0
            if games_started < 1:
                continue

            outs = innings_to_outs(stat.get("inningsPitched", "0"))
            if outs <= 0:
                continue

            try:
                rows.append({
                    "date": split.get("date", ""),
                    "outs": outs,
                    "earned_runs": int(stat.get("earnedRuns", 0) or 0),
                    "hits": int(stat.get("hits", 0) or 0),
                    "walks": int(stat.get("baseOnBalls", 0) or 0),
                    "strikeouts": int(stat.get("strikeOuts", 0) or 0),
                })
            except (TypeError, ValueError):
                continue

    if not rows:
        return None

    rows.sort(key=lambda row: row["date"])
    rows = rows[-max(1, int(starts)):]

    total_outs = sum(row["outs"] for row in rows)
    innings = outs_to_innings(total_outs)
    if innings <= 0:
        return None

    earned_runs = sum(row["earned_runs"] for row in rows)
    hits = sum(row["hits"] for row in rows)
    walks = sum(row["walks"] for row in rows)
    strikeouts = sum(row["strikeouts"] for row in rows)

    return {
        "starts": len(rows),
        "innings": innings,
        "era": earned_runs * 9.0 / innings,
        "whip": (hits + walks) / innings,
        "strikeouts": strikeouts,
        "walks": walks,
        "hits": hits,
        "earned_runs": earned_runs,
    }


def get_team_hitting_stats(team_id, season):
    """Return current-season team hitting summary."""
    if not team_id:
        return None

    data = safe_get_json(
        f"{MLB_API}/teams/{team_id}/stats",
        params={
            "stats": "season",
            "group": "hitting",
            "season": season,
        },
    )

    stats = data.get("stats", []) if data else []
    if not stats:
        return None

    splits = stats[0].get("splits", [])
    if not splits:
        return None

    stat = splits[0].get("stat", {})
    return {
        "avg": float(stat.get("avg", 0.0)),
        "obp": float(stat.get("obp", 0.0)),
        "slg": float(stat.get("slg", 0.0)),
        "ops": float(stat.get("ops", 0.0)),
        "runs": int(stat.get("runs", 0)),
        "home_runs": int(stat.get("homeRuns", 0)),
        "strikeouts": int(stat.get("strikeOuts", 0)),
        "walks": int(stat.get("baseOnBalls", 0)),
    }


def clamp(value, low=0.0, high=100.0):
    return max(low, min(high, value))


def pitcher_score(stats):
    """
    0-35 points.
    Lower ERA/WHIP is better; strikeout-to-walk ratio adds a small bonus.
    Missing stats return a neutral score so we do not fabricate an advantage.
    """
    if not stats:
        return 17.5

    era_component = clamp(35 - (stats["era"] * 7), 0, 35)
    whip_component = clamp(15 - (stats["whip"] * 10), 0, 15)

    bb = stats["walks"]
    k = stats["strikeouts"]
    kbb = k / bb if bb else (k if k else 0)
    kbb_component = clamp(kbb * 1.5, 0, 15)

    raw = (era_component * 0.55) + (whip_component * 0.30) + (kbb_component * 0.15)
    return clamp(raw)


def offense_score(stats):
    """
    0-30 points.
    Uses OPS as the primary team-offense signal, with OBP/SLG as supporting data.
    """
    if not stats:
        return 15.0

    ops = stats["ops"]
    obp = stats["obp"]
    slg = stats["slg"]

    ops_component = clamp((ops - 0.550) / 0.450 * 100)
    obp_component = clamp((obp - 0.250) / 0.150 * 100)
    slg_component = clamp((slg - 0.300) / 0.400 * 100)

    normalized = (
        ops_component * 0.60
        + obp_component * 0.20
        + slg_component * 0.20
    )
    return clamp(normalized * 0.30)


def get_recent_team_form(team_id, days=14):
    """
    Recent form from MLB schedule results.
    Returns win rate and average run differential over completed games.
    """
    if not team_id:
        return None

    data = safe_get_json(
        f"{MLB_API}/schedule",
        params={
            "sportId": 1,
            "teamId": team_id,
            "startDate": (
                local_now().date()
                - timedelta(days=days)
            ).strftime("%Y-%m-%d"),
            "endDate": local_now().strftime("%Y-%m-%d"),
            "hydrate": "linescore",
        },
    )

    if not data:
        return None

    results = []
    for date_block in data.get("dates", []):
        for game in date_block.get("games", []):
            status = game.get("status", {}).get("abstractGameState")
            if status != "Final":
                continue

            home = game.get("teams", {}).get("home", {})
            away = game.get("teams", {}).get("away", {})

            home_id = home.get("team", {}).get("id")
            away_id = away.get("team", {}).get("id")
            home_runs = home.get("score")
            away_runs = away.get("score")

            if home_runs is None or away_runs is None:
                continue

            if team_id == home_id:
                runs_for, runs_against = home_runs, away_runs
            elif team_id == away_id:
                runs_for, runs_against = away_runs, home_runs
            else:
                continue

            results.append({
                "win": runs_for > runs_against,
                "run_diff": runs_for - runs_against,
            })

    if not results:
        return None

    return {
        "games": len(results),
        "win_rate": sum(r["win"] for r in results) / len(results),
        "run_diff": sum(r["run_diff"] for r in results) / len(results),
    }


def get_team_bullpen_proxy(team_id, season):
    """
    Bullpen proxy using team pitching totals.

    MLB Stats API does not give a clean bullpen-only season split through
    the simple team endpoint used here. Therefore this is deliberately
    labeled a PROXY rather than pretending it is a true bullpen metric.
    """
    if not team_id:
        return None

    data = safe_get_json(
        f"{MLB_API}/teams/{team_id}/stats",
        params={
            "stats": "season",
            "group": "pitching",
            "season": season,
        },
    )

    stats = data.get("stats", []) if data else []
    if not stats or not stats[0].get("splits"):
        return None

    stat = stats[0]["splits"][0].get("stat", {})

    try:
        era = float(stat.get("era", 99.0))
        whip = float(stat.get("whip", 99.0))
        walks = int(stat.get("baseOnBalls", 0))
        strikeouts = int(stat.get("strikeOuts", 0))
        innings = outs_to_innings(innings_to_outs(stat.get("inningsPitched", "0")))
    except (TypeError, ValueError):
        return None

    return {
        "era": era,
        "whip": whip,
        "walks": walks,
        "strikeouts": strikeouts,
        "innings": innings,
    }


def bullpen_proxy_score(stats):
    """0-15 points. Lower ERA/WHIP and better K/BB are rewarded."""
    if not stats:
        return 7.5

    era_score = clamp(15 - stats["era"] * 3.0, 0, 15)
    whip_score = clamp(10 - stats["whip"] * 7.0, 0, 10)

    kbb = (
        stats["strikeouts"] / stats["walks"]
        if stats["walks"] > 0
        else 0
    )
    kbb_score = clamp(kbb * 2.0, 0, 10)

    return clamp(
        era_score * 0.50
        + whip_score * 0.30
        + kbb_score * 0.20
    )


def recent_form_score(form):
    """0-15 points based on recent win rate and run differential."""
    if not form:
        return 7.5

    win_component = clamp(form["win_rate"] * 100)
    run_component = clamp(50 + form["run_diff"] * 8, 0, 100)

    return clamp(
        win_component * 0.65 * 0.15
        + run_component * 0.35 * 0.15
    )


def _pairwise_advantage(own, opp, scale, max_score=45.0):
    """Convert a positive own-vs-opponent margin into a 50-centered score."""
    if own is None or opp is None:
        return 50.0
    return clamp(50.0 + ((own - opp) / scale) * max_score, 0, 100)


def _pitcher_quality(stats):
    """0-100 pitcher quality; lower ERA/WHIP and stronger K/BB are better."""
    if not stats:
        return None
    era = clamp(100.0 - stats["era"] * 5.0, 0, 100)
    whip = clamp(100.0 - stats["whip"] * 55.0, 0, 100)
    kbb = stats["strikeouts"] / stats["walks"] if stats["walks"] else 0.0
    kbb_score = clamp(kbb * 20.0, 0, 100)
    return era * 0.55 + whip * 0.30 + kbb_score * 0.15


def starter_reliability(stats):
    """
    Reliability of a starter's season sample, 0-100.

    Thresholds intentionally match v2.7 policy:
      >=80 IP: 100
      50-79.2: 90
      30-49.2: 80
      15-29.2: 65
      >0-14.2: 50
    """
    if not stats:
        return 0.0
    ip = float(stats.get("innings", 0.0) or 0.0)
    if ip >= 80:
        return 100.0
    if ip >= 50:
        return 90.0
    if ip >= 30:
        return 80.0
    if ip >= 15:
        return 65.0
    if ip > 0:
        return 50.0
    return 0.0


def starter_sample_label(stats):
    reliability = starter_reliability(stats)
    if reliability >= 100:
        return "ALTA"
    if reliability >= 80:
        return "BUENA"
    if reliability >= 65:
        return "MEDIA"
    if reliability >= 50:
        return "SMALL SAMPLE"
    return "SIN MUESTRA"


def _pitcher_quality_v27(season_stats, recent_stats):
    """
    Starter quality with recency + sample-size shrinkage.

    Base mix is up to 65% season / 35% recent starts. Recent weight is reduced
    when fewer than ~25 recent innings are available. The final quality is then
    shrunk toward neutral (50) according to season sample reliability.
    """
    season_quality = _pitcher_quality(season_stats)
    if season_quality is None:
        return None

    blended = season_quality
    recent_quality = _pitcher_quality(recent_stats)
    if recent_quality is not None:
        recent_ip = float(recent_stats.get("innings", 0.0) or 0.0)
        recent_weight = 0.35 * min(1.0, recent_ip / 25.0)
        blended = season_quality * (1.0 - recent_weight) + recent_quality * recent_weight

    reliability = starter_reliability(season_stats) / 100.0
    return clamp(50.0 + (blended - 50.0) * reliability, 0, 100)


def _lineup_coverage_state(lineup_status):
    """Return lineup coverage as (state, cap) without inferring injuries."""
    if not lineup_status:
        return "PENDIENTES", 94.0

    away_count = int(lineup_status.get("away_count", 0) or 0)
    home_count = int(lineup_status.get("home_count", 0) or 0)
    if away_count > 0 and home_count > 0:
        return "AMBAS PUBLICADAS", 100.0
    if away_count > 0 or home_count > 0:
        return "PARCIAL", 97.0
    return "PENDIENTES", 94.0


def calculate_data_reliability(
    data_count,
    away_starter_reliability,
    home_starter_reliability,
    away_recent_pitcher,
    home_recent_pitcher,
    lineup_status,
):
    """
    0-100 quality/completeness score, separate from matchup strength.

    v2.7.1 rule: 100/100 is reserved for a slate item with both lineups
    published. With lineups pending, otherwise-complete data is capped at 94.
    """
    completeness = clamp((data_count / 8.0) * 100.0)
    starter_avg = (
        float(away_starter_reliability) + float(home_starter_reliability)
    ) / 2.0
    if away_recent_pitcher and home_recent_pitcher:
        recent_coverage = 100.0
    elif away_recent_pitcher or home_recent_pitcher:
        recent_coverage = 75.0
    else:
        recent_coverage = 50.0

    raw = clamp(
        completeness * 0.50
        + starter_avg * 0.40
        + recent_coverage * 0.10
    )
    _state, lineup_cap = _lineup_coverage_state(lineup_status)
    return min(raw, lineup_cap)


def calculate_confidence_v271(
    difference,
    data_reliability,
    min_starter_reliability,
    lineup_status,
):
    """
    Heuristic Confidence 2.1: deliberately more conservative than v2.7.

    It is a ranking signal, NOT a calibrated probability. The score separates
    matchup strength, data quality and starter sample reliability. A 90+ score
    is intentionally rare.
    """
    # Difference still drives the signal, but no longer maps almost one-for-one
    # into the high 80s/90s. Around a 20-point matchup gap is a strong, not
    # near-certain, model signal.
    matchup_component = clamp(55.0 + difference * 0.90, 55.0, 86.0)
    confidence = (
        matchup_component * 0.65
        + data_reliability * 0.20
        + min_starter_reliability * 0.15
    )

    lineup_state, _cap = _lineup_coverage_state(lineup_status)
    if lineup_state == "AMBAS PUBLICADAS":
        confidence += 1.0
    elif lineup_state == "PARCIAL":
        confidence += 0.3

    # Tiny samples are allowed to remain candidates, but cannot create fake certainty.
    if min_starter_reliability < 60:
        confidence = min(confidence, 77.0)
    elif min_starter_reliability < 70:
        confidence = min(confidence, 80.0)
    elif min_starter_reliability < 80:
        confidence = min(confidence, 83.0)

    return min(clamp(confidence), 92.0)


def _offense_quality(stats):
    """0-100 offensive quality centered on realistic MLB ranges."""
    if not stats:
        return None
    ops = clamp((stats["ops"] - 0.550) / 0.300 * 100.0, 0, 100)
    obp = clamp((stats["obp"] - 0.250) / 0.120 * 100.0, 0, 100)
    slg = clamp((stats["slg"] - 0.300) / 0.350 * 100.0, 0, 100)
    return ops * 0.60 + obp * 0.20 + slg * 0.20


def _form_quality(form):
    if not form:
        return None
    win = clamp(form["win_rate"] * 100.0)
    rd = clamp(50.0 + form["run_diff"] * 8.0, 0, 100)
    return win * 0.70 + rd * 0.30


def _bullpen_quality(stats):
    if not stats:
        return None
    era = clamp(100.0 - stats["era"] * 10.0, 0, 100)
    whip = clamp(100.0 - stats["whip"] * 55.0, 0, 100)
    kbb = stats["strikeouts"] / stats["walks"] if stats["walks"] else 0.0
    kbb_score = clamp(kbb * 20.0, 0, 100)
    return era * 0.50 + whip * 0.30 + kbb_score * 0.20


def build_team_score_v27(
    team_hitting,
    opponent_hitting,
    own_pitching,
    opponent_pitching,
    own_recent_pitcher,
    opponent_recent_pitcher,
    bullpen,
    opponent_bullpen,
    recent_form,
    opponent_form,
    home,
):
    """
    v2.7 matchup score. Structure remains 45/25/15/10/5, but the 45% starter
    block now uses season + recent-start form + sample-size shrinkage.
    """
    parts = []

    own_p = _pitcher_quality_v27(own_pitching, own_recent_pitcher)
    opp_p = _pitcher_quality_v27(opponent_pitching, opponent_recent_pitcher)
    if own_p is not None and opp_p is not None:
        parts.append((45, _pairwise_advantage(own_p, opp_p, 20.0, 50.0)))

    own_o = _offense_quality(team_hitting)
    opp_o = _offense_quality(opponent_hitting)
    if own_o is not None and opp_o is not None:
        parts.append((25, _pairwise_advantage(own_o, opp_o, 20.0, 40.0)))

    own_f = _form_quality(recent_form)
    opp_f = _form_quality(opponent_form)
    if own_f is not None and opp_f is not None:
        parts.append((15, _pairwise_advantage(own_f, opp_f, 20.0, 30.0)))

    own_b = _bullpen_quality(bullpen)
    opp_b = _bullpen_quality(opponent_bullpen)
    if own_b is not None and opp_b is not None:
        parts.append((10, _pairwise_advantage(own_b, opp_b, 20.0, 25.0)))

    parts.append((5, 60.0 if home else 50.0))

    total_weight = sum(w for w, _ in parts)
    return clamp(sum(w * value for w, value in parts) / total_weight, 0, 100)


def get_game_lineup_status(game_pk):
    """
    Reads the scheduled lineup hydration when MLB has published it.
    Returns counts only; it does not assume that an unpublished lineup means
    a player is injured.
    """
    if not game_pk:
        return None

    data = safe_get_json(
        f"{MLB_API}/schedule",
        params={
            "sportId": 1,
            "gamePk": game_pk,
            "hydrate": "lineup,players",
        },
    )

    try:
        game = data["dates"][0]["games"][0]
        lineups = game.get("lineups", {})

        away_players = lineups.get("awayPlayers", [])
        home_players = lineups.get("homePlayers", [])

        return {
            "away_count": len(away_players),
            "home_count": len(home_players),
            "published": bool(away_players or home_players),
        }
    except (KeyError, IndexError, TypeError):
        return None


def classify_pick_risk(
    confidence,
    difference,
    data_count,
    data_reliability,
    min_starter_reliability,
):
    """Human-readable v2.7 tier; it does not change the underlying prediction."""
    if data_count < 6 or data_reliability < 70:
        return "🔴 DATOS INCOMPLETOS"

    if (
        confidence >= 84
        and difference >= 10
        and data_reliability >= 85
        and min_starter_reliability >= 80
    ):
        return "🟢 PICK FUERTE"

    if (
        confidence >= 76
        and difference >= 5
        and data_reliability >= 75
        and min_starter_reliability >= 50
    ):
        return "🟡 PICK MODERADO"

    if confidence >= 68 and difference >= 2:
        return "🟠 PICK DE RIESGO"

    return "🔴 NO BET"


def lineup_adjustment(lineup_status):
    """
    Small reliability adjustment only when both lineups are published.
    We deliberately do not guess individual injuries.
    """
    if not lineup_status:
        return 0.0

    if (
        lineup_status["published"]
        and lineup_status["away_count"] > 0
        and lineup_status["home_count"] > 0
    ):
        return 2.0

    return 0.0


def build_daily_matchups(fecha, season):
    """Collect and score the entire slate synchronously; caller may offload to a thread."""
    datos = safe_get_json(
        f"{MLB_API}/schedule",
        params={
            "sportId": 1,
            "date": fecha,
            "hydrate": "probablePitcher,team",
        },
        cache_ttl=60,
    )

    if datos is None:
        return "api_error", []

    juegos = datos.get("dates", [])
    if not juegos:
        return "no_games", []

    partidos = []

    for fecha_juegos in juegos:
        for juego in fecha_juegos.get("games", []):
            away = juego.get("teams", {}).get("away", {})
            home = juego.get("teams", {}).get("home", {})

            away_team = away.get("team", {})
            home_team = home.get("team", {})

            away_id = away_team.get("id")
            home_id = home_team.get("id")
            game_pk = juego.get("gamePk")

            away_name = away_team.get("name", "Visitante")
            home_name = home_team.get("name", "Local")

            away_pitcher = away.get("probablePitcher", {})
            home_pitcher = home.get("probablePitcher", {})

            away_pitcher_id = away_pitcher.get("id")
            home_pitcher_id = home_pitcher.get("id")

            away_pitcher_name = away_pitcher.get("fullName", "Por confirmar")
            home_pitcher_name = home_pitcher.get("fullName", "Por confirmar")

            away_pitching = get_current_pitcher_stats(away_pitcher_id, season)
            home_pitching = get_current_pitcher_stats(home_pitcher_id, season)
            away_recent_pitcher = get_recent_pitcher_form(away_pitcher_id, season, starts=5)
            home_recent_pitcher = get_recent_pitcher_form(home_pitcher_id, season, starts=5)

            away_hitting = get_team_hitting_stats(away_id, season)
            home_hitting = get_team_hitting_stats(home_id, season)
            away_form = get_recent_team_form(away_id, days=14)
            home_form = get_recent_team_form(home_id, days=14)
            away_bullpen = get_team_bullpen_proxy(away_id, season)
            home_bullpen = get_team_bullpen_proxy(home_id, season)

            away_score = build_team_score_v27(
                away_hitting, home_hitting,
                away_pitching, home_pitching,
                away_recent_pitcher, home_recent_pitcher,
                away_bullpen, home_bullpen,
                away_form, home_form,
                home=False,
            )
            home_score = build_team_score_v27(
                home_hitting, away_hitting,
                home_pitching, away_pitching,
                home_recent_pitcher, away_recent_pitcher,
                home_bullpen, away_bullpen,
                home_form, away_form,
                home=True,
            )

            favorite = home_name if home_score >= away_score else away_name
            favorite_score = max(home_score, away_score)
            difference = abs(home_score - away_score)

            blocks = (
                away_pitching,
                home_pitching,
                away_hitting,
                home_hitting,
                away_form,
                home_form,
                away_bullpen,
                home_bullpen,
            )
            data_count = sum(x is not None for x in blocks)

            away_starter_reliability = starter_reliability(away_pitching)
            home_starter_reliability = starter_reliability(home_pitching)
            min_starter_reliability = min(
                away_starter_reliability,
                home_starter_reliability,
            )

            lineup_status = get_game_lineup_status(game_pk)
            data_reliability = calculate_data_reliability(
                data_count,
                away_starter_reliability,
                home_starter_reliability,
                away_recent_pitcher,
                home_recent_pitcher,
                lineup_status,
            )

            confidence = calculate_confidence_v271(
                difference,
                data_reliability,
                min_starter_reliability,
                lineup_status,
            )

            risk = classify_pick_risk(
                confidence,
                difference,
                data_count,
                data_reliability,
                min_starter_reliability,
            )

            starters_ready = (
                away_pitching is not None
                and home_pitching is not None
                and away_pitching.get("innings", 0) > 0
                and home_pitching.get("innings", 0) > 0
            )
            eligible = (
                starters_ready
                and data_count >= 6
                and data_reliability >= 75
                and confidence >= 76
                and difference >= 5
                and risk in {"🟢 PICK FUERTE", "🟡 PICK MODERADO"}
            )

            if not starters_ready:
                gate_reason = "abridor(es) sin muestra utilizable/por confirmar"
            elif data_count < 6:
                gate_reason = "datos insuficientes"
            elif data_reliability < 75:
                gate_reason = "calidad de datos insuficiente"
            elif confidence < 76 or difference < 5:
                gate_reason = "ventaja/confianza insuficiente"
            elif risk not in {"🟢 PICK FUERTE", "🟡 PICK MODERADO"}:
                gate_reason = "perfil de riesgo no aprobado"
            else:
                gate_reason = "APROBADO"

            partidos.append({
                "away": away_name,
                "home": home_name,
                "game_pk": game_pk,
                "game_date": juego.get("gameDate"),
                "away_pitcher": away_pitcher_name,
                "home_pitcher": home_pitcher_name,
                "favorite": favorite,
                "home_score": home_score,
                "away_score": away_score,
                "score": favorite_score,
                "difference": difference,
                "confidence": confidence,
                "risk": risk,
                "eligible": eligible,
                "gate_reason": gate_reason,
                "data_count": data_count,
                "data_reliability": data_reliability,
                "away_starter_reliability": away_starter_reliability,
                "home_starter_reliability": home_starter_reliability,
                "min_starter_reliability": min_starter_reliability,
                "small_sample": min_starter_reliability < 60,
                "away_pitching": away_pitching,
                "home_pitching": home_pitching,
                "away_recent_pitcher": away_recent_pitcher,
                "home_recent_pitcher": home_recent_pitcher,
                "away_hitting": away_hitting,
                "home_hitting": home_hitting,
                "away_form": away_form,
                "home_form": home_form,
                "away_bullpen": away_bullpen,
                "home_bullpen": home_bullpen,
                "lineup_status": lineup_status,
            })

    partidos.sort(
        key=lambda x: (
            x["eligible"],
            x["confidence"],
            x["data_reliability"],
            x["difference"],
            x["score"],
        ),
        reverse=True,
    )
    return "ok", partidos


# ---------------------------------------------------------------------------
# v2.8 MARKET ENGINE
# ---------------------------------------------------------------------------

_MLB_TEAM_ALIASES = {
    "athletics": "athletics",
    "oakland athletics": "athletics",
    "sacramento athletics": "athletics",
    "as": "athletics",
}


def _normalize_team_name(name):
    """Normalize team names across MLB Stats API and odds feeds."""
    value = (name or "").lower().replace("&", "and")
    value = re.sub(r"[^a-z0-9 ]+", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return _MLB_TEAM_ALIASES.get(value, value)


def american_to_implied_probability(price):
    """Convert American odds to raw implied probability, including vig."""
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if price == 0:
        return None
    if price < 0:
        return (-price) / ((-price) + 100.0)
    return 100.0 / (price + 100.0)


def probability_to_american(probability):
    """Fair American price for a probability proxy; display only."""
    try:
        p = float(probability)
    except (TypeError, ValueError):
        return None
    p = max(0.001, min(0.999, p))
    if p >= 0.5:
        return -100.0 * p / (1.0 - p)
    return 100.0 * (1.0 - p) / p


def remove_two_way_vig(price_a, price_b):
    """Return normalized no-vig probabilities for a two-way moneyline."""
    pa = american_to_implied_probability(price_a)
    pb = american_to_implied_probability(price_b)
    if pa is None or pb is None or (pa + pb) <= 0:
        return None, None
    total = pa + pb
    return pa / total, pb / total


def calculate_model_probability_proxy(partido):
    """
    Convert v2.7.1 matchup separation into a conservative probability proxy.

    IMPORTANT: this is not historically calibrated win probability. It exists so
    v2.8 can compare model direction/strength with a no-vig market baseline while
    tracking data is accumulated for future calibration.
    """
    difference = max(0.0, float(partido.get("difference", 0.0) or 0.0))
    # Logistic scale chosen so a ~20-25 matchup gap is a meaningful but not
    # near-certain edge. Extreme outputs are intentionally capped.
    raw = 1.0 / (1.0 + math.exp(-(difference / 30.0)))

    dr = clamp(float(partido.get("data_reliability", 0.0) or 0.0), 0, 100) / 100.0
    sr = clamp(float(partido.get("min_starter_reliability", 0.0) or 0.0), 0, 100) / 100.0
    reliability = 0.65 + 0.20 * dr + 0.15 * sr
    proxy = 0.5 + (raw - 0.5) * reliability

    if partido.get("small_sample"):
        proxy = min(proxy, 0.62)
    return clamp(proxy, 0.50, 0.80)


def _odds_cache_get(key):
    cached = _ODDS_CACHE.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    return None


def get_mlb_moneyline_odds(fecha):
    """
    Fetch MLB h2h/moneyline odds once per cache window.

    Primary target is Hard Rock Bet Florida. FanDuel/DraftKings are requested by
    default only to create a consensus market reference and fallback audit layer.
    """
    if not ODDS_API_KEY:
        _ODDS_LAST_META["error"] = "ODDS_API_KEY no configurada"
        return "not_configured", []

    bookmakers = ",".join(
        b.strip() for b in ODDS_BOOKMAKERS.split(",") if b.strip()
    )
    key = (fecha, bookmakers, "h2h")
    cached = _odds_cache_get(key)
    if cached is not None:
        return "ok", cached

    params = {
        "apiKey": ODDS_API_KEY,
        "bookmakers": bookmakers,
        "markets": "h2h",
        "oddsFormat": "american",
        "dateFormat": "iso",
    }

    try:
        response = _HTTP.get(
            f"{ODDS_API_BASE}/sports/baseball_mlb/odds",
            params=params,
            timeout=12,
        )
        _ODDS_LAST_META["remaining"] = response.headers.get("x-requests-remaining")
        _ODDS_LAST_META["used"] = response.headers.get("x-requests-used")
        _ODDS_LAST_META["last"] = response.headers.get("x-requests-last")
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise ValueError("respuesta de odds no es una lista")
        _ODDS_LAST_META["error"] = None
        _ODDS_CACHE[key] = (time.monotonic() + ODDS_CACHE_TTL, data)
        return "ok", data
    except (requests.RequestException, ValueError) as exc:
        _ODDS_LAST_META["error"] = str(exc)
        print(f"Odds API error: {exc}")
        return "api_error", []


def _parse_iso_utc(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _event_is_local_date(event, fecha):
    dt = _parse_iso_utc(event.get("commence_time"))
    if dt is None:
        return True
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d") == fecha


def _event_is_pregame(event):
    """Only allow pregame market snapshots; never mix live odds with pregame model output."""
    dt = _parse_iso_utc(event.get("commence_time"))
    if dt is None:
        return True
    return dt > datetime.now(timezone.utc)


def _find_odds_event(partido, events, fecha):
    away_key = _normalize_team_name(partido.get("away"))
    home_key = _normalize_team_name(partido.get("home"))
    candidates = []
    for event in events:
        if not _event_is_local_date(event, fecha):
            continue
        if not _event_is_pregame(event):
            continue
        if (
            _normalize_team_name(event.get("away_team")) == away_key
            and _normalize_team_name(event.get("home_team")) == home_key
        ):
            candidates.append(event)

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # Doubleheader-safe selection: use closest scheduled start if available.
    mlb_dt = _parse_iso_utc(partido.get("game_date"))
    if mlb_dt is None:
        return candidates[0]

    def distance(event):
        event_dt = _parse_iso_utc(event.get("commence_time"))
        if event_dt is None:
            return float("inf")
        return abs((event_dt - mlb_dt).total_seconds())

    return min(candidates, key=distance)


def _book_moneyline_quote(book, home_name, away_name):
    for market in book.get("markets", []):
        if market.get("key") != "h2h":
            continue
        prices = {}
        for outcome in market.get("outcomes", []):
            prices[_normalize_team_name(outcome.get("name"))] = outcome.get("price")

        home_price = prices.get(_normalize_team_name(home_name))
        away_price = prices.get(_normalize_team_name(away_name))
        if home_price is None or away_price is None:
            continue

        home_nv, away_nv = remove_two_way_vig(home_price, away_price)
        if home_nv is None:
            continue
        return {
            "bookmaker_key": book.get("key"),
            "bookmaker_title": book.get("title", book.get("key", "Book")),
            "home_price": float(home_price),
            "away_price": float(away_price),
            "home_no_vig": home_nv,
            "away_no_vig": away_nv,
            "last_update": book.get("last_update") or market.get("last_update"),
        }
    return None


def _market_snapshot(partido, event):
    if not event:
        return None

    quotes = []
    primary = None
    for book in event.get("bookmakers", []):
        quote = _book_moneyline_quote(book, partido["home"], partido["away"])
        if not quote:
            continue
        quotes.append(quote)
        if quote["bookmaker_key"] == ODDS_PRIMARY_BOOKMAKER:
            primary = quote

    if not quotes:
        return None

    favorite_is_home = partido["favorite"] == partido["home"]
    consensus_values = [
        q["home_no_vig"] if favorite_is_home else q["away_no_vig"]
        for q in quotes
    ]
    consensus_no_vig = sum(consensus_values) / len(consensus_values)

    if primary:
        selected_price = primary["home_price"] if favorite_is_home else primary["away_price"]
        selected_no_vig = primary["home_no_vig"] if favorite_is_home else primary["away_no_vig"]
        opponent_price = primary["away_price"] if favorite_is_home else primary["home_price"]
    else:
        selected_price = None
        selected_no_vig = None
        opponent_price = None

    return {
        "event_id": event.get("id"),
        "commence_time": event.get("commence_time"),
        "primary_available": primary is not None,
        "primary_title": primary["bookmaker_title"] if primary else None,
        "selected_price": selected_price,
        "opponent_price": opponent_price,
        "primary_no_vig": selected_no_vig,
        "consensus_no_vig": consensus_no_vig,
        "book_count": len(quotes),
        "quotes": quotes,
    }


def _market_agreement(model_prob, market_prob):
    if market_prob is None:
        return "SIN MERCADO"
    gap = model_prob - market_prob
    if market_prob < 0.50:
        return "DESACUERDO DIRECCIONAL"
    if abs(gap) <= 0.04:
        return "ACUERDO FUERTE"
    if abs(gap) <= 0.08:
        return "ACUERDO MODERADO"
    return "DIVERGENCIA ALTA"


def _classify_market_pick(partido):
    """Separate survival and value objectives; never force a market approval."""
    if not partido.get("eligible"):
        return "🔴 NO BET", False, False
    if not partido.get("market_available"):
        return "⚪ MODEL ONLY", False, False

    model_prob = partido["model_probability"]
    market_prob = partido["market_probability"]
    gap = partido["value_gap"]
    agreement = partido["market_agreement"]

    if agreement == "DESACUERDO DIRECCIONAL":
        return "🔴 MARKET REJECT", False, False
    if agreement == "DIVERGENCIA ALTA" and abs(gap) > 0.10:
        return "🟠 REVIEW — DIVERGENCIA", False, False

    survival = (
        market_prob >= 0.55
        and gap >= -0.03
        and abs(gap) <= 0.08
        and partido.get("confidence", 0) >= 76
    )

    # VALUE requires an actionable Hard Rock price, not just consensus odds.
    value = (
        partido.get("hardrock_available")
        and model_prob >= 0.56
        and gap >= 0.035
        and gap <= 0.10
    )

    if survival and value:
        return "🟢 HYBRID", True, True
    if survival:
        return "🔵 SURVIVAL", True, False
    if value:
        return "🟣 VALUE", False, True
    return "🟡 MARKET WATCH", False, False


def build_daily_matchups_v28(fecha, season):
    """v2.7.1 model slate enriched by one cached v2.8 market request."""
    status, partidos = build_daily_matchups(fecha, season)
    if status != "ok":
        return status, partidos, "unavailable"

    odds_status, events = get_mlb_moneyline_odds(fecha)

    for partido in partidos:
        model_prob = calculate_model_probability_proxy(partido)
        partido["model_probability"] = model_prob
        partido["model_fair_odds"] = probability_to_american(model_prob)
        partido["market_available"] = False
        partido["hardrock_available"] = False
        partido["market_snapshot"] = None
        partido["market_probability"] = None
        partido["hardrock_no_vig"] = None
        partido["hardrock_price"] = None
        partido["consensus_no_vig"] = None
        partido["value_gap"] = None
        partido["market_agreement"] = "SIN MERCADO"
        partido["market_grade"] = "⚪ MARKET OFF" if odds_status == "not_configured" else "⚪ SIN MERCADO"
        partido["survival_approved"] = False
        partido["value_approved"] = False

        if odds_status != "ok":
            continue

        event = _find_odds_event(partido, events, fecha)
        snapshot = _market_snapshot(partido, event)
        if not snapshot:
            continue

        partido["market_available"] = True
        partido["market_snapshot"] = snapshot
        partido["hardrock_available"] = snapshot["primary_available"]
        partido["hardrock_no_vig"] = snapshot["primary_no_vig"]
        partido["hardrock_price"] = snapshot["selected_price"]
        partido["consensus_no_vig"] = snapshot["consensus_no_vig"]

        # Agreement uses consensus when available; the value gap uses Hard Rock
        # no-vig whenever Hard Rock is available, otherwise consensus for audit only.
        market_prob = snapshot["consensus_no_vig"]
        partido["market_probability"] = market_prob
        actionable_market_prob = (
            snapshot["primary_no_vig"]
            if snapshot["primary_no_vig"] is not None
            else market_prob
        )
        partido["value_gap"] = model_prob - actionable_market_prob
        partido["market_agreement"] = _market_agreement(model_prob, market_prob)
        grade, survival, value = _classify_market_pick(partido)
        partido["market_grade"] = grade
        partido["survival_approved"] = survival
        partido["value_approved"] = value

    grade_rank = {
        "🟢 HYBRID": 5,
        "🔵 SURVIVAL": 4,
        "🟣 VALUE": 3,
        "🟡 MARKET WATCH": 2,
        "🟠 REVIEW — DIVERGENCIA": 1,
        "⚪ MODEL ONLY": 0,
        "⚪ SIN MERCADO": 0,
        "⚪ MARKET OFF": 0,
        "🔴 MARKET REJECT": -1,
        "🔴 NO BET": -2,
    }
    partidos.sort(
        key=lambda p: (
            grade_rank.get(p.get("market_grade"), -3),
            bool(p.get("eligible")),
            p.get("market_probability") or 0.0,
            p.get("model_probability") or 0.0,
            p.get("confidence") or 0.0,
        ),
        reverse=True,
    )
    return "ok", partidos, odds_status


def _primary_feed_notice(partidos, odds_status):
    """Transparent degradation notice when the primary book is absent from an otherwise healthy feed."""
    if odds_status != "ok":
        return ""
    market_rows = [p for p in partidos if p.get("market_available")]
    if not market_rows:
        return ""
    if any(p.get("hardrock_available") for p in market_rows):
        return ""
    return (
        "⚠️ HARD ROCK TEMPORALMENTE NO DISPONIBLE EN EL FEED\n"
        "🛡️ SURVIVAL: evaluado con el consenso de mercado disponible.\n"
        "🟣 VALUE: suspendido hasta recuperar precio Hard Rock.\n\n"
    )


def _format_american(price):
    if price is None:
        return "N/D"
    value = int(round(float(price)))
    return f"+{value}" if value > 0 else str(value)


def _pct(value):
    return "N/D" if value is None else f"{value * 100:.1f}%"


def _market_source_label(partido):
    snap = partido.get("market_snapshot") or {}
    if partido.get("hardrock_available"):
        return snap.get("primary_title") or "Hard Rock Bet"
    if snap.get("book_count"):
        return f"Consensus {snap['book_count']} books"
    return "N/D"


def _select_triple_pick_v28(partidos, odds_status):
    """Market-aware survival output; falls back only when the market engine is off."""
    if odds_status == "ok":
        approved = [
            p for p in partidos
            if p.get("market_grade") in {"🟢 HYBRID", "🔵 SURVIVAL"}
        ]
        return approved[:3], "market"

    # API key missing/error should not break the stable model path.
    model = [p for p in partidos if p.get("eligible")]
    model.sort(
        key=lambda p: (
            p.get("confidence", 0),
            p.get("data_reliability", 0),
            p.get("difference", 0),
        ),
        reverse=True,
    )
    return model[:3], "model_fallback"


async def _reply_long(message_obj, text, limit=3900):
    """Send long Telegram output safely without breaking paragraphs mid-block."""
    if len(text) <= limit:
        await message_obj.reply_text(text)
        return

    paragraphs = text.split("\n\n")
    chunk = ""
    for paragraph in paragraphs:
        candidate = paragraph if not chunk else chunk + "\n\n" + paragraph
        if len(candidate) <= limit:
            chunk = candidate
            continue
        if chunk:
            await message_obj.reply_text(chunk)
        # Defensive fallback for a single oversized paragraph.
        while len(paragraph) > limit:
            await message_obj.reply_text(paragraph[:limit])
            paragraph = paragraph[limit:]
        chunk = paragraph
    if chunk:
        await message_obj.reply_text(chunk)



# ---------------------------------------------------------------------------
# v2.9.1 TRACKING & CALIBRATION ENGINE
# ---------------------------------------------------------------------------


def _tracking_connection():
    """Open a short-lived SQLite connection; safe for asyncio.to_thread usage."""
    db_path = Path(TRACK_DB_PATH).expanduser()
    if db_path.parent and str(db_path.parent) not in {"", "."}:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_tracking_db():
    """Create the v2.9.1 tracking schema without modifying any existing records."""
    with _tracking_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tracked_picks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                pick_date TEXT NOT NULL,
                game_pk INTEGER NOT NULL,
                game_date TEXT,
                away TEXT NOT NULL,
                home TEXT NOT NULL,
                selection TEXT NOT NULL,
                product TEXT NOT NULL,
                source TEXT NOT NULL,
                official INTEGER NOT NULL DEFAULT 1,
                market_family TEXT NOT NULL DEFAULT 'FULL_GAME_ML',
                line REAL,
                odds REAL,
                book TEXT,
                model_version TEXT NOT NULL,
                bot_version TEXT NOT NULL,
                matchup_score REAL,
                difference REAL,
                model_probability REAL,
                confidence REAL,
                data_reliability REAL,
                starter_reliability REAL,
                lineup_published INTEGER,
                market_probability REAL,
                hardrock_no_vig REAL,
                value_gap REAL,
                market_grade TEXT,
                result TEXT NOT NULL DEFAULT 'PENDING',
                outcome INTEGER,
                units_risked REAL NOT NULL DEFAULT 1.0,
                units_won_lost REAL,
                home_score INTEGER,
                away_score INTEGER,
                settled_at TEXT,
                brier REAL,
                notes TEXT,
                UNIQUE (
                    pick_date, game_pk, selection, product, source, model_version
                )
            );

            CREATE INDEX IF NOT EXISTS idx_tracked_picks_date
            ON tracked_picks(pick_date);

            CREATE INDEX IF NOT EXISTS idx_tracked_picks_result
            ON tracked_picks(result);

            CREATE INDEX IF NOT EXISTS idx_tracked_picks_official
            ON tracked_picks(official, product, result);

            CREATE TABLE IF NOT EXISTS official_daily_picks (
                pick_date TEXT NOT NULL,
                slot INTEGER NOT NULL,
                game_pk INTEGER NOT NULL,
                game_date TEXT,
                away TEXT NOT NULL,
                home TEXT NOT NULL,
                selection TEXT NOT NULL,
                pitcher TEXT,
                pick_text TEXT,
                market_family TEXT NOT NULL DEFAULT 'FULL_GAME_ML',
                line REAL,
                created_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (pick_date, slot)
            );

            CREATE INDEX IF NOT EXISTS idx_official_daily_picks_date
            ON official_daily_picks(pick_date, slot);

            -- Triple Pick v3.1 Multi-Sport: soccer is intentionally isolated from MLB tables.
            CREATE TABLE IF NOT EXISTS soccer_picks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pick_date TEXT NOT NULL,
                slot INTEGER NOT NULL,
                league TEXT NOT NULL,
                away TEXT NOT NULL,
                home TEXT NOT NULL,
                selection TEXT NOT NULL,
                product TEXT NOT NULL DEFAULT 'TOP PICK',
                plan_required TEXT NOT NULL DEFAULT 'PREMIUM',
                kickoff_utc TEXT,
                player TEXT,
                line REAL,
                odds REAL,
                book TEXT,
                source TEXT NOT NULL DEFAULT 'SOCCER_GLOBAL_OPTIMIZER_2.0',
                result TEXT NOT NULL DEFAULT 'PENDING',
                created_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(pick_date, slot)
            );

            CREATE INDEX IF NOT EXISTS idx_soccer_picks_date
            ON soccer_picks(pick_date, slot);

            CREATE INDEX IF NOT EXISTS idx_soccer_picks_product
            ON soccer_picks(product, result);

            CREATE TABLE IF NOT EXISTS soccer_alert_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                soccer_pick_id INTEGER NOT NULL,
                scheduled_for TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                UNIQUE(chat_id, soccer_pick_id),
                FOREIGN KEY(soccer_pick_id) REFERENCES soccer_picks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS alert_subscriptions (
                chat_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                lead_minutes INTEGER NOT NULL DEFAULT 45,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS alert_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                pick_date TEXT NOT NULL,
                game_pk INTEGER NOT NULL,
                selection TEXT NOT NULL,
                scheduled_for TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                UNIQUE(chat_id, pick_date, game_pk, selection)
            );

            CREATE INDEX IF NOT EXISTS idx_alert_deliveries_date
            ON alert_deliveries(pick_date, chat_id);

            CREATE TABLE IF NOT EXISTS alert_game_settings (
                chat_id INTEGER NOT NULL,
                pick_date TEXT NOT NULL,
                game_pk INTEGER NOT NULL,
                selection TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(chat_id, pick_date, game_pk, selection)
            );

            CREATE INDEX IF NOT EXISTS idx_alert_game_settings_chat_date
            ON alert_game_settings(chat_id, pick_date);


            CREATE TABLE IF NOT EXISTS sms_subscriptions (
                chat_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                phone_number TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sms_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                pick_date TEXT NOT NULL,
                game_pk INTEGER NOT NULL,
                selection TEXT NOT NULL,
                phone_number TEXT,
                provider_sid TEXT,
                status TEXT NOT NULL,
                error TEXT,
                sent_at TEXT NOT NULL,
                UNIQUE(chat_id, pick_date, game_pk, selection)
            );

            CREATE INDEX IF NOT EXISTS idx_sms_deliveries_date
            ON sms_deliveries(pick_date, chat_id);
            """
        )

        # Schema migration for v3.0 official market-aware picks.
        # Existing Railway SQLite volumes are upgraded in place without deleting data.
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(official_daily_picks)").fetchall()}
        if "pick_text" not in cols:
            conn.execute("ALTER TABLE official_daily_picks ADD COLUMN pick_text TEXT")
        if "market_family" not in cols:
            conn.execute("ALTER TABLE official_daily_picks ADD COLUMN market_family TEXT NOT NULL DEFAULT 'FULL_GAME_ML'")
        if "line" not in cols:
            conn.execute("ALTER TABLE official_daily_picks ADD COLUMN line REAL")


def _product_from_market_grade(grade):
    if grade == "🟢 HYBRID":
        return "HYBRID"
    if grade == "🔵 SURVIVAL":
        return "SURVIVAL"
    if grade == "🟣 VALUE":
        return "VALUE"
    return "UNCLASSIFIED"


def _lineup_is_published(partido):
    lineup = partido.get("lineup_status") or {}
    return int(bool(lineup.get("published")))


def _insert_tracked_pick(conn, partido, pick_date, product, source, official):
    """Insert the first recommendation snapshot only; repeated /picks never rewrites history."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO tracked_picks (
            created_at, pick_date, game_pk, game_date, away, home, selection,
            product, source, official, market_family, line, odds, book,
            model_version, bot_version, matchup_score, difference,
            model_probability, confidence, data_reliability, starter_reliability,
            lineup_published, market_probability, hardrock_no_vig, value_gap,
            market_grade, result, units_risked
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'FULL_GAME_ML', NULL, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 1.0
        )
        """,
        (
            local_now().isoformat(),
            pick_date,
            int(partido.get("game_pk") or 0),
            partido.get("game_date"),
            partido.get("away") or "N/D",
            partido.get("home") or "N/D",
            partido.get("favorite") or "N/D",
            product,
            source,
            int(bool(official)),
            partido.get("hardrock_price"),
            ODDS_PRIMARY_BOOKMAKER if partido.get("hardrock_available") else None,
            MODEL_VERSION,
            BOT_VERSION,
            partido.get("score"),
            partido.get("difference"),
            partido.get("model_probability"),
            partido.get("confidence"),
            partido.get("data_reliability"),
            partido.get("min_starter_reliability"),
            _lineup_is_published(partido),
            partido.get("market_probability"),
            partido.get("hardrock_no_vig"),
            partido.get("value_gap"),
            partido.get("market_grade"),
        ),
    )
    return cur.rowcount > 0


def track_final_picks(partidos, pick_date, mode):
    """
    Persist the final Triple Pick slate.

    Market-approved SURVIVAL/HYBRID selections are official. Model fallback
    selections are retained for audit but excluded from official performance.
    """
    if not partidos:
        return {"inserted": 0, "existing": 0, "official": 0, "fallback": 0}

    init_tracking_db()
    inserted = existing = official_count = fallback_count = 0
    with _tracking_connection() as conn:
        for partido in partidos:
            if mode == "market":
                product = _product_from_market_grade(partido.get("market_grade"))
                official = product in {"SURVIVAL", "HYBRID"}
                source = "TRIPLE_PICK"
            else:
                product = "MODEL_FALLBACK"
                official = False
                source = "TRIPLE_PICK_FALLBACK"

            if official:
                official_count += 1
            else:
                fallback_count += 1

            if _insert_tracked_pick(
                conn, partido, pick_date, product, source, official
            ):
                inserted += 1
            else:
                existing += 1

    return {
        "inserted": inserted,
        "existing": existing,
        "official": official_count,
        "fallback": fallback_count,
    }


def _american_profit_per_unit(price):
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    if p == 0:
        return None
    if p > 0:
        return p / 100.0
    return 100.0 / abs(p)


def _settle_one_row(conn, row, game):
    status = (game.get("status") or {})
    abstract = status.get("abstractGameState")
    detailed = str(status.get("detailedState") or "").lower()

    if "cancel" in detailed:
        conn.execute(
            """
            UPDATE tracked_picks
            SET result='VOID', settled_at=?, notes=COALESCE(notes, '') || ?
            WHERE id=?
            """,
            (local_now().isoformat(), " | MLB game cancelled", row["id"]),
        )
        return "VOID"

    if abstract != "Final":
        return "PENDING"

    home = (game.get("teams") or {}).get("home") or {}
    away = (game.get("teams") or {}).get("away") or {}
    home_score = home.get("score")
    away_score = away.get("score")
    if home_score is None or away_score is None:
        return "PENDING"

    home_score = int(home_score)
    away_score = int(away_score)
    home_name = ((home.get("team") or {}).get("name") or row["home"])
    away_name = ((away.get("team") or {}).get("name") or row["away"])
    family = (row["market_family"] or "FULL_GAME_ML").upper()
    line = row["line"]
    result = None
    outcome = None

    if family == "FULL_GAME_ML":
        if home_score == away_score:
            return "PENDING"
        winner = home_name if home_score > away_score else away_name
        won = _normalize_team_name(row["selection"]) == _normalize_team_name(winner)
        result = "WIN" if won else "LOSS"
        outcome = 1 if won else 0
    elif family == "RUN_LINE" and line is not None:
        selected_home = _normalize_team_name(row["selection"]) == _normalize_team_name(home_name)
        selected_score = home_score if selected_home else away_score
        opponent_score = away_score if selected_home else home_score
        adjusted = selected_score + float(line)
        if adjusted == opponent_score:
            result = "PUSH"
        else:
            won = adjusted > opponent_score
            result = "WIN" if won else "LOSS"
            outcome = 1 if won else 0
    elif family in {"TEAM_TOTAL_OVER", "TEAM_TOTAL_UNDER"} and line is not None:
        selected_home = _normalize_team_name(row["selection"]) == _normalize_team_name(home_name)
        team_score = home_score if selected_home else away_score
        target = float(line)
        if team_score == target:
            result = "PUSH"
        else:
            won = team_score > target if family.endswith("OVER") else team_score < target
            result = "WIN" if won else "LOSS"
            outcome = 1 if won else 0
    elif family in {"FULL_GAME_TOTAL_OVER", "FULL_GAME_TOTAL_UNDER"} and line is not None:
        total = home_score + away_score
        target = float(line)
        if total == target:
            result = "PUSH"
        else:
            won = total > target if family.endswith("OVER") else total < target
            result = "WIN" if won else "LOSS"
            outcome = 1 if won else 0
    else:
        # Unknown market: keep pending instead of settling incorrectly.
        return "PENDING"

    profit = _american_profit_per_unit(row["odds"])
    units = None
    if result == "PUSH":
        units = 0.0
    elif profit is not None:
        units = profit if result == "WIN" else -float(row["units_risked"] or 1.0)

    model_p = row["model_probability"]
    brier = None
    if model_p is not None and outcome is not None:
        try:
            brier = (float(model_p) - float(outcome)) ** 2
        except (TypeError, ValueError):
            brier = None

    conn.execute(
        """
        UPDATE tracked_picks
        SET result=?, outcome=?, units_won_lost=?, home_score=?, away_score=?,
            settled_at=?, brier=?
        WHERE id=?
        """,
        (
            result, outcome, units, home_score, away_score,
            local_now().isoformat(), brier, row["id"],
        ),
    )
    return result


def settle_pending_picks():
    """Settle every pending recommendation whose MLB game is final."""
    init_tracking_db()
    with _tracking_connection() as conn:
        pending = conn.execute(
            """
            SELECT * FROM tracked_picks
            WHERE result='PENDING'
            ORDER BY pick_date, id
            """
        ).fetchall()

        if not pending:
            return {"pending_before": 0, "wins": 0, "losses": 0, "pushes": 0, "void": 0, "still_pending": 0}

        by_date = {}
        for row in pending:
            by_date.setdefault(row["pick_date"], []).append(row)

        wins = losses = pushes = void = still_pending = 0
        for pick_date, rows in by_date.items():
            data = safe_get_json(
                f"{MLB_API}/schedule",
                params={
                    "sportId": 1,
                    "date": pick_date,
                    "hydrate": "team",
                },
                cache_ttl=60,
            )
            if not data:
                still_pending += len(rows)
                continue

            games = {}
            for date_block in data.get("dates", []):
                for game in date_block.get("games", []):
                    games[int(game.get("gamePk") or 0)] = game

            for row in rows:
                game = games.get(int(row["game_pk"] or 0))
                if not game:
                    still_pending += 1
                    continue
                settled = _settle_one_row(conn, row, game)
                if settled == "WIN":
                    wins += 1
                elif settled == "LOSS":
                    losses += 1
                elif settled == "PUSH":
                    pushes += 1
                elif settled == "VOID":
                    void += 1
                else:
                    still_pending += 1

        return {
            "pending_before": len(pending),
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "void": void,
            "still_pending": still_pending,
        }


def _sample_label(n):
    if n < 20:
        return "Exploratory"
    if n < 50:
        return "Early Signal"
    if n < 100:
        return "Developing"
    if n < 250:
        return "Stable"
    return "Strong Evidence"


def tracking_status_snapshot():
    init_tracking_db()
    with _tracking_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM tracked_picks").fetchone()[0]
        official = conn.execute(
            "SELECT COUNT(*) FROM tracked_picks WHERE official=1"
        ).fetchone()[0]
        fallback = conn.execute(
            "SELECT COUNT(*) FROM tracked_picks WHERE official=0"
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM tracked_picks WHERE result='PENDING'"
        ).fetchone()[0]
        settled = conn.execute(
            "SELECT COUNT(*) FROM tracked_picks WHERE result IN ('WIN','LOSS')"
        ).fetchone()[0]
    return {
        "total": total,
        "official": official,
        "fallback": fallback,
        "pending": pending,
        "settled": settled,
        "path": str(Path(TRACK_DB_PATH).expanduser()),
        "railway_volume": RAILWAY_VOLUME_MOUNT_PATH or None,
    }


def performance_snapshot():
    init_tracking_db()
    with _tracking_connection() as conn:
        overall = conn.execute(
            """
            SELECT
                COUNT(*) AS n,
                SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) AS losses,
                AVG(brier) AS brier,
                SUM(CASE WHEN units_won_lost IS NOT NULL THEN units_won_lost ELSE 0 END) AS units,
                SUM(CASE WHEN units_won_lost IS NOT NULL THEN units_risked ELSE 0 END) AS risked,
                SUM(CASE WHEN units_won_lost IS NOT NULL THEN 1 ELSE 0 END) AS priced_n
            FROM tracked_picks
            WHERE official=1 AND result IN ('WIN','LOSS')
            """
        ).fetchone()

        by_product = conn.execute(
            """
            SELECT
                product,
                COUNT(*) AS n,
                SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) AS losses,
                AVG(brier) AS brier,
                SUM(CASE WHEN units_won_lost IS NOT NULL THEN units_won_lost ELSE 0 END) AS units,
                SUM(CASE WHEN units_won_lost IS NOT NULL THEN units_risked ELSE 0 END) AS risked,
                SUM(CASE WHEN units_won_lost IS NOT NULL THEN 1 ELSE 0 END) AS priced_n
            FROM tracked_picks
            WHERE official=1 AND result IN ('WIN','LOSS')
            GROUP BY product
            ORDER BY n DESC, product
            """
        ).fetchall()
    return overall, by_product


def calibration_snapshot():
    """Return predicted-vs-actual bins for official resolved recommendations."""
    init_tracking_db()
    bins = [
        (0.00, 0.55, "<55%"),
        (0.55, 0.60, "55–60%"),
        (0.60, 0.65, "60–65%"),
        (0.65, 0.70, "65–70%"),
        (0.70, 0.75, "70–75%"),
        (0.75, 0.80, "75–80%"),
        (0.80, 0.85, "80–85%"),
        (0.85, 0.90, "85–90%"),
        (0.90, 1.01, "90%+"),
    ]
    with _tracking_connection() as conn:
        rows = conn.execute(
            """
            SELECT model_probability, outcome, brier
            FROM tracked_picks
            WHERE official=1
              AND result IN ('WIN','LOSS')
              AND model_probability IS NOT NULL
              AND outcome IS NOT NULL
            """
        ).fetchall()

    out = []
    for low, high, label in bins:
        bucket = [r for r in rows if low <= float(r["model_probability"]) < high]
        if not bucket:
            continue
        n = len(bucket)
        pred = sum(float(r["model_probability"]) for r in bucket) / n
        actual = sum(int(r["outcome"]) for r in bucket) / n
        briers = [float(r["brier"]) for r in bucket if r["brier"] is not None]
        brier = sum(briers) / len(briers) if briers else None
        out.append(
            {
                "label": label,
                "n": n,
                "predicted": pred,
                "actual": actual,
                "error": actual - pred,
                "brier": brier,
            }
        )
    return out


def recent_history_snapshot(limit=12):
    init_tracking_db()
    with _tracking_connection() as conn:
        return conn.execute(
            """
            SELECT pick_date, away, home, selection, product, official,
                   odds, result, model_probability, market_probability,
                   units_won_lost
            FROM tracked_picks
            ORDER BY pick_date DESC, id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()


def _format_units(value):
    if value is None:
        return "N/D"
    return f"{float(value):+.2f}u"



def _twilio_configured():
    sender_ok = bool(TWILIO_FROM_NUMBER or TWILIO_MESSAGING_SERVICE_SID)
    return bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and sender_ok)


def _normalize_e164(value):
    """Return a conservative E.164 phone number or None."""
    text = (value or "").strip()
    text = re.sub(r"[\s().-]+", "", text)
    if not re.fullmatch(r"\+[1-9]\d{7,14}", text):
        return None
    return text


def _mask_phone(value):
    phone = _normalize_e164(value)
    if not phone:
        return "NO CONFIGURADO"
    if len(phone) <= 6:
        return phone
    return phone[:3] + "••••" + phone[-4:]


def _get_sms_subscription(chat_id):
    init_tracking_db()
    with _tracking_connection() as conn:
        row = conn.execute(
            "SELECT enabled, phone_number, updated_at FROM sms_subscriptions WHERE chat_id=?",
            (int(chat_id),),
        ).fetchone()
    if row is None:
        return {
            "enabled": SMS_DEFAULT_ENABLED,
            "phone_number": _normalize_e164(SMS_DEFAULT_TO_NUMBER),
            "updated_at": None,
        }
    return {
        "enabled": bool(row["enabled"]),
        "phone_number": _normalize_e164(row["phone_number"]),
        "updated_at": row["updated_at"],
    }


def _set_sms_subscription(chat_id, enabled=None, phone_number=None):
    init_tracking_db()
    current = _get_sms_subscription(chat_id)
    final_enabled = current["enabled"] if enabled is None else bool(enabled)
    final_phone = current["phone_number"]
    if phone_number is not None:
        final_phone = _normalize_e164(phone_number)
        if not final_phone:
            raise ValueError("Número inválido. Usa formato E.164, por ejemplo +17865551234")
    with _tracking_connection() as conn:
        conn.execute(
            """
            INSERT INTO sms_subscriptions(chat_id, enabled, phone_number, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                enabled=excluded.enabled,
                phone_number=excluded.phone_number,
                updated_at=excluded.updated_at
            """,
            (int(chat_id), int(final_enabled), final_phone, local_now().isoformat()),
        )
    return {"enabled": final_enabled, "phone_number": final_phone}


def _sms_was_sent(chat_id, pick_date, game_pk, selection):
    init_tracking_db()
    with _tracking_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM sms_deliveries
            WHERE chat_id=? AND pick_date=? AND game_pk=? AND selection=? AND status='SENT'
            """,
            (int(chat_id), pick_date, int(game_pk), selection),
        ).fetchone()
    return row is not None


def _record_sms_delivery(chat_id, row, phone_number, status, provider_sid=None, error=None):
    init_tracking_db()
    with _tracking_connection() as conn:
        conn.execute(
            """
            INSERT INTO sms_deliveries(
                chat_id, pick_date, game_pk, selection, phone_number,
                provider_sid, status, error, sent_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, pick_date, game_pk, selection) DO UPDATE SET
                phone_number=excluded.phone_number,
                provider_sid=excluded.provider_sid,
                status=excluded.status,
                error=excluded.error,
                sent_at=excluded.sent_at
            """,
            (
                int(chat_id), row.get("pick_date"), int(row.get("game_pk")), row.get("selection"),
                phone_number, provider_sid, status, error, local_now().isoformat(),
            ),
        )


def _send_twilio_sms(to_number, body):
    """Send one SMS through Twilio REST API. Returns (ok, sid, error)."""
    to_number = _normalize_e164(to_number)
    if not to_number:
        return False, None, "Número SMS inválido"
    if not _twilio_configured():
        return False, None, "Twilio no configurado"

    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/"
        f"{TWILIO_ACCOUNT_SID}/Messages.json"
    )
    data = {"To": to_number, "Body": body}
    if TWILIO_MESSAGING_SERVICE_SID:
        data["MessagingServiceSid"] = TWILIO_MESSAGING_SERVICE_SID
    else:
        data["From"] = TWILIO_FROM_NUMBER

    try:
        response = _HTTP.post(
            url,
            data=data,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            timeout=12,
        )
        payload = response.json() if response.content else {}
        if response.ok:
            return True, payload.get("sid"), None
        message = payload.get("message") or f"HTTP {response.status_code}"
        return False, payload.get("sid"), str(message)[:300]
    except (requests.RequestException, ValueError) as exc:
        return False, None, str(exc)[:300]


def _set_alert_subscription(chat_id, enabled, lead_minutes=ALERT_LEAD_MINUTES):
    init_tracking_db()
    with _tracking_connection() as conn:
        conn.execute(
            """
            INSERT INTO alert_subscriptions(chat_id, enabled, lead_minutes, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                enabled=excluded.enabled,
                lead_minutes=excluded.lead_minutes,
                updated_at=excluded.updated_at
            """,
            (int(chat_id), int(bool(enabled)), int(lead_minutes), local_now().isoformat()),
        )


def _get_alert_subscription(chat_id):
    init_tracking_db()
    with _tracking_connection() as conn:
        row = conn.execute(
            "SELECT enabled, lead_minutes, updated_at FROM alert_subscriptions WHERE chat_id=?",
            (int(chat_id),),
        ).fetchone()
    if row is None:
        return {
            "enabled": ALERTS_DEFAULT_ENABLED,
            "lead_minutes": ALERT_LEAD_MINUTES,
            "updated_at": None,
        }
    return {
        "enabled": bool(row["enabled"]),
        "lead_minutes": int(row["lead_minutes"] or ALERT_LEAD_MINUTES),
        "updated_at": row["updated_at"],
    }


def _enabled_alert_chats():
    init_tracking_db()
    with _tracking_connection() as conn:
        return conn.execute(
            "SELECT chat_id, lead_minutes FROM alert_subscriptions WHERE enabled=1"
        ).fetchall()


def _set_game_alert_enabled(chat_id, pick_date, game_pk, selection, enabled):
    init_tracking_db()
    with _tracking_connection() as conn:
        conn.execute(
            """
            INSERT INTO alert_game_settings(
                chat_id, pick_date, game_pk, selection, enabled, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, pick_date, game_pk, selection) DO UPDATE SET
                enabled=excluded.enabled,
                updated_at=excluded.updated_at
            """,
            (
                int(chat_id), pick_date, int(game_pk), selection,
                int(bool(enabled)), local_now().isoformat(),
            ),
        )


def _game_alert_enabled(chat_id, pick_date, game_pk, selection):
    """Per-game alerts default ON unless explicitly disabled."""
    init_tracking_db()
    with _tracking_connection() as conn:
        row = conn.execute(
            """
            SELECT enabled FROM alert_game_settings
            WHERE chat_id=? AND pick_date=? AND game_pk=? AND selection=?
            """,
            (int(chat_id), pick_date, int(game_pk), selection),
        ).fetchone()
    return True if row is None else bool(row["enabled"])


def _alert_status(chat_id, row, global_enabled=True):
    if _alert_was_sent(chat_id, row["pick_date"], row["game_pk"], row["selection"]):
        return "ENVIADA"
    if not global_enabled or not _game_alert_enabled(
        chat_id, row["pick_date"], row["game_pk"], row["selection"]
    ):
        return "DESACTIVADA"
    return "PENDIENTE"


def _pending_alert_picks(pick_date=None):
    """Return only admin-confirmed channel picks for alerts."""
    init_tracking_db()
    params = []
    where = "WHERE tp.result='PENDING' AND tp.game_date IS NOT NULL AND tp.source='CHANNEL_OFFICIAL'"
    if pick_date:
        where += " AND tp.pick_date=?"
        params.append(pick_date)
    with _tracking_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT MAX(tp.id) AS id, tp.pick_date, tp.game_pk, tp.game_date, tp.away, tp.home,
                   tp.selection, tp.product, tp.odds, tp.book, tp.market_family, tp.line
            FROM tracked_picks tp
            {where}
            GROUP BY tp.pick_date, tp.game_pk, tp.selection, tp.market_family, tp.line
            ORDER BY tp.game_date ASC
            """,
            params,
        ).fetchall()
    return rows

def _alert_was_sent(chat_id, pick_date, game_pk, selection):
    init_tracking_db()
    with _tracking_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM alert_deliveries
            WHERE chat_id=? AND pick_date=? AND game_pk=? AND selection=?
            """,
            (int(chat_id), pick_date, int(game_pk), selection),
        ).fetchone()
    return row is not None


def _record_alert_delivery(chat_id, row, scheduled_for):
    init_tracking_db()
    with _tracking_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO alert_deliveries(
                chat_id, pick_date, game_pk, selection, scheduled_for, sent_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                int(chat_id), row["pick_date"], int(row["game_pk"]), row["selection"],
                scheduled_for.isoformat(), local_now().isoformat(),
            ),
        )


def _alert_job_name(chat_id, row):
    safe_selection = re.sub(r"[^a-zA-Z0-9]+", "_", str(row["selection"]))[:40]
    return f"tp_alert_{chat_id}_{row['pick_date']}_{row['game_pk']}_{safe_selection}"


def _tracked_pick_display(row):
    """Render a tracked pick without assuming every market is moneyline."""
    family = (row["market_family"] if hasattr(row, "keys") and "market_family" in row.keys() else row.get("market_family")) or "FULL_GAME_ML"
    selection = row["selection"] if hasattr(row, "keys") and "selection" in row.keys() else row.get("selection")
    line = row["line"] if hasattr(row, "keys") and "line" in row.keys() else row.get("line")
    if family == "FULL_GAME_ML":
        return f"{selection} ML"
    if family == "RUN_LINE":
        sign = "+" if line is not None and float(line) > 0 else ""
        return f"{selection} {sign}{_fmt_line(line)}" if line is not None else str(selection)
    if family == "TEAM_TOTAL_OVER":
        return f"{selection} Team Total Over {_fmt_line(line)}"
    if family == "TEAM_TOTAL_UNDER":
        return f"{selection} Team Total Under {_fmt_line(line)}"
    if family == "FULL_GAME_TOTAL_OVER":
        return f"{selection} Over {_fmt_line(line)}"
    if family == "FULL_GAME_TOTAL_UNDER":
        return f"{selection} Under {_fmt_line(line)}"
    return str(selection)


async def _send_pick_alert_job(context: ContextTypes.DEFAULT_TYPE):
    payload = context.job.data or {}
    row = payload.get("pick") or {}
    chat_id = int(payload.get("chat_id"))
    scheduled_for_iso = payload.get("scheduled_for")
    try:
        scheduled_for = datetime.fromisoformat(scheduled_for_iso)
    except Exception:
        scheduled_for = local_now()

    sub = await asyncio.to_thread(_get_alert_subscription, chat_id)
    if not sub.get("enabled"):
        return
    game_enabled = await asyncio.to_thread(
        _game_alert_enabled, chat_id, row.get("pick_date"), row.get("game_pk"), row.get("selection")
    )
    if not game_enabled:
        return
    if await asyncio.to_thread(
        _alert_was_sent, chat_id, row.get("pick_date"), row.get("game_pk"), row.get("selection")
    ):
        return

    game_dt = _parse_iso_utc(row.get("game_date"))
    local_game = game_dt.astimezone(LOCAL_TZ) if game_dt else None
    game_time = local_game.strftime("%I:%M %p").lstrip("0") if local_game else "N/D"
    odds_text = _format_american(row.get("odds")) if row.get("odds") is not None else "N/D"
    product = row.get("product") or "TRIPLE PICK"
    text = (
        f"🔔 TRIPLE PICK — FALTAN {sub['lead_minutes']} MIN\n\n"
        f"🏟️ {row.get('away')} vs {row.get('home')}\n"
        f"🎯 {_tracked_pick_display(row)}\n"
        f"🕐 Inicio: {game_time} ({AUTO_TZ})\n"
        f"🛡️ Producto: {product}\n"
        f"💵 Cuota registrada: {odds_text}\n\n"
        "Revisa alineaciones y cualquier cambio de última hora antes del inicio."
    )
    await context.bot.send_message(chat_id=chat_id, text=text)
    await asyncio.to_thread(_record_alert_delivery, chat_id, row, scheduled_for)

    # SMS is a supplemental channel. Telegram remains the primary delivery.
    sms_sub = await asyncio.to_thread(_get_sms_subscription, chat_id)
    if sms_sub.get("enabled") and sms_sub.get("phone_number"):
        already_sms = await asyncio.to_thread(
            _sms_was_sent, chat_id, row.get("pick_date"), row.get("game_pk"), row.get("selection")
        )
        if not already_sms:
            sms_body = (
                f"TRIPLE PICK: faltan {sub['lead_minutes']} min. "
                f"{_tracked_pick_display(row)} | {row.get('away')} vs {row.get('home')} | "
                f"Inicio {game_time}. Revisa cambios de ultima hora. Juega responsablemente."
            )
            ok, sid, error = await asyncio.to_thread(
                _send_twilio_sms, sms_sub["phone_number"], sms_body
            )
            await asyncio.to_thread(
                _record_sms_delivery, chat_id, row, sms_sub["phone_number"],
                "SENT" if ok else "FAILED", sid, error
            )
            if not ok:
                print(f"⚠️ SMS no enviado para chat {chat_id}: {error}")


def _row_to_alert_dict(row):
    return {k: row[k] for k in row.keys()}


async def schedule_alert_jobs(application, pick_date=None):
    """Schedule unsent Telegram alerts for every enabled chat and tracked Triple Pick."""
    if application.job_queue is None:
        return {"scheduled": 0, "skipped": 0, "reason": "job_queue_unavailable"}

    rows = await asyncio.to_thread(_pending_alert_picks, pick_date)
    chats = await asyncio.to_thread(_enabled_alert_chats)
    now_utc = datetime.now(timezone.utc)
    scheduled = skipped = 0

    for chat in chats:
        chat_id = int(chat["chat_id"])
        lead = int(chat["lead_minutes"] or ALERT_LEAD_MINUTES)
        for row in rows:
            if not await asyncio.to_thread(
                _game_alert_enabled, chat_id, row["pick_date"], row["game_pk"], row["selection"]
            ):
                skipped += 1
                continue
            if await asyncio.to_thread(
                _alert_was_sent, chat_id, row["pick_date"], row["game_pk"], row["selection"]
            ):
                skipped += 1
                continue
            game_dt = _parse_iso_utc(row["game_date"])
            if game_dt is None:
                skipped += 1
                continue
            alert_dt = game_dt - timedelta(minutes=lead)
            if alert_dt <= now_utc:
                skipped += 1
                continue
            name = _alert_job_name(chat_id, row)
            for existing in application.job_queue.get_jobs_by_name(name):
                existing.schedule_removal()
            application.job_queue.run_once(
                _send_pick_alert_job,
                when=alert_dt,
                data={
                    "chat_id": chat_id,
                    "pick": _row_to_alert_dict(row),
                    "scheduled_for": alert_dt.astimezone(LOCAL_TZ).isoformat(),
                },
                name=name,
                chat_id=chat_id,
            )
            scheduled += 1
    return {"scheduled": scheduled, "skipped": skipped, "reason": None}


ALERT_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["✅ Activar alertas", "⛔ Desactivar alertas"],
        ["📋 Próximas alertas", "📱 SMS"],
        ["⬅️ Menú principal"],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Alertas Triple Pick",
)

SMS_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["✅ Activar SMS", "⛔ Desactivar SMS"],
        ["📱 Estado SMS", "⬅️ Alertas"],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="SMS Triple Pick",
)


async def alerts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sub = await asyncio.to_thread(_get_alert_subscription, chat_id)
    state = "ACTIVAS" if sub["enabled"] else "DESACTIVADAS"
    sms_sub = await asyncio.to_thread(_get_sms_subscription, chat_id)
    sms_state = "ACTIVO" if sms_sub.get("enabled") else "DESACTIVADO"
    await update.message.reply_text(
        "🔔 ALERTAS TRIPLE PICK\n\n"
        f"Telegram: {state}\n"
        f"SMS: {sms_state}\n"
        f"Aviso: {sub['lead_minutes']} minutos antes de cada juego del Triple Pick.\n\n"
        "Usa los botones para administrar Telegram, SMS o revisar las próximas alertas.",
        reply_markup=ALERT_MENU_KEYBOARD,
    )


async def alerts_enable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await asyncio.to_thread(_set_alert_subscription, chat_id, True, ALERT_LEAD_MINUTES)
    info = await schedule_alert_jobs(context.application)
    await update.message.reply_text(
        "✅ Alertas activadas.\n"
        f"Recibirás un aviso {ALERT_LEAD_MINUTES} minutos antes de cada juego registrado en Triple Pick.\n"
        f"Alertas programadas ahora: {info['scheduled']}.",
        reply_markup=ALERT_MENU_KEYBOARD,
    )


async def alerts_disable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await asyncio.to_thread(_set_alert_subscription, chat_id, False, ALERT_LEAD_MINUTES)
    if context.application.job_queue is not None:
        prefix = f"tp_alert_{chat_id}_"
        for job in context.application.job_queue.jobs():
            if job.name and job.name.startswith(prefix):
                job.schedule_removal()
    await update.message.reply_text(
        "⛔ Alertas desactivadas para este chat.",
        reply_markup=ALERT_MENU_KEYBOARD,
    )


async def sms_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sub = await asyncio.to_thread(_get_sms_subscription, chat_id)
    twilio_state = "CONFIGURADO" if _twilio_configured() else "NO CONFIGURADO"
    state = "ACTIVO" if sub.get("enabled") else "DESACTIVADO"
    await update.message.reply_text(
        "📱 SMS TRIPLE PICK\n\n"
        f"Estado SMS: {state}\n"
        f"Número: {_mask_phone(sub.get('phone_number'))}\n"
        f"Twilio: {twilio_state}\n"
        f"Aviso: {ALERT_LEAD_MINUTES} minutos antes, además de Telegram.\n\n"
        "Para registrar/cambiar el número usa:\n"
        "/smsset +1XXXXXXXXXX\n\n"
        "El número debe estar en formato internacional E.164.",
        reply_markup=SMS_MENU_KEYBOARD,
    )


async def sms_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text(
            "Uso: /smsset +1XXXXXXXXXX\nEjemplo de formato: +17865551234",
            reply_markup=SMS_MENU_KEYBOARD,
        )
        return
    phone = _normalize_e164(context.args[0])
    if not phone:
        await update.message.reply_text(
            "❌ Número inválido. Usa formato E.164, comenzando con + y código de país.",
            reply_markup=SMS_MENU_KEYBOARD,
        )
        return
    await asyncio.to_thread(_set_sms_subscription, chat_id, None, phone)
    await update.message.reply_text(
        f"✅ Número SMS guardado: {_mask_phone(phone)}\n"
        "Ahora puedes pulsar ✅ Activar SMS.",
        reply_markup=SMS_MENU_KEYBOARD,
    )


async def sms_enable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sub = await asyncio.to_thread(_get_sms_subscription, chat_id)
    if not sub.get("phone_number"):
        await update.message.reply_text(
            "⚠️ Primero registra el número con /smsset +1XXXXXXXXXX.",
            reply_markup=SMS_MENU_KEYBOARD,
        )
        return
    if not _twilio_configured():
        await update.message.reply_text(
            "⚠️ Twilio todavía no está configurado en Railway.\n"
            "Necesitas TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN y TWILIO_FROM_NUMBER "
            "o TWILIO_MESSAGING_SERVICE_SID.",
            reply_markup=SMS_MENU_KEYBOARD,
        )
        return
    await asyncio.to_thread(_set_sms_subscription, chat_id, True, None)
    await update.message.reply_text(
        f"✅ SMS activado para {_mask_phone(sub.get('phone_number'))}.\n"
        "Se enviará junto con la alerta de Telegram.",
        reply_markup=SMS_MENU_KEYBOARD,
    )


async def sms_disable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await asyncio.to_thread(_set_sms_subscription, chat_id, False, None)
    await update.message.reply_text(
        "⛔ SMS desactivado. Las alertas de Telegram no cambian.",
        reply_markup=SMS_MENU_KEYBOARD,
    )


async def sms_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await sms_menu(update, context)


async def _build_alerts_panel(chat_id):
    sub = await asyncio.to_thread(_get_alert_subscription, chat_id)
    rows = await asyncio.to_thread(_pending_alert_picks, None)
    now_utc = datetime.now(timezone.utc)
    items = []
    counts = {"PENDIENTE": 0, "ENVIADA": 0, "DESACTIVADA": 0}

    for row in rows:
        game_dt = _parse_iso_utc(row["game_date"])
        if not game_dt:
            continue
        status = await asyncio.to_thread(_alert_status, chat_id, row, sub["enabled"])
        if status in counts:
            counts[status] += 1
        alert_dt = game_dt - timedelta(minutes=sub["lead_minutes"])
        if game_dt > now_utc or status == "ENVIADA":
            items.append((row, game_dt, alert_dt, status))

    lines = [
        "📋 ALERTAS TRIPLE PICK",
        f"Estado global: {'ACTIVAS' if sub['enabled'] else 'DESACTIVADAS'}",
        f"Pendientes: {counts['PENDIENTE']} | Enviadas: {counts['ENVIADA']} | Desactivadas: {counts['DESACTIVADA']}",
        "",
    ]
    keyboard = []
    for idx, (row, game_dt, alert_dt, status) in enumerate(items[:12], 1):
        status_icon = {"PENDIENTE": "🟡", "ENVIADA": "✅", "DESACTIVADA": "⛔"}.get(status, "•")
        lines.append(
            f"{idx}. {status_icon} {_tracked_pick_display(row)} — {row['away']} vs {row['home']}\n"
            f"   ⚾ Juego: {game_dt.astimezone(LOCAL_TZ).strftime('%m/%d %I:%M %p').lstrip('0')}\n"
            f"   🔔 Aviso: {alert_dt.astimezone(LOCAL_TZ).strftime('%m/%d %I:%M %p').lstrip('0')}\n"
            f"   Estado: {status}"
        )
        if status != "ENVIADA":
            action = "off" if status == "PENDIENTE" else "on"
            label = "⛔ Desactivar" if action == "off" else "✅ Activar"
            callback = f"alertgame|{action}|{row['pick_date']}|{row['game_pk']}|{idx}"
            keyboard.append([InlineKeyboardButton(f"{label} · {row['selection']}", callback_data=callback)])

    if not items:
        lines.append("No hay alertas registradas para mostrar en este momento.")
    return "\n".join(lines), InlineKeyboardMarkup(keyboard) if keyboard else None, items


async def alerts_upcoming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text, inline_markup, _items = await _build_alerts_panel(chat_id)
    await update.message.reply_text(
        text,
        reply_markup=inline_markup if inline_markup is not None else ALERT_MENU_KEYBOARD,
    )


async def alert_game_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    parts = (query.data or "").split("|")
    if len(parts) != 5 or parts[0] != "alertgame":
        return
    _prefix, action, pick_date, game_pk_text, idx_text = parts
    try:
        game_pk = int(game_pk_text)
        idx = int(idx_text) - 1
    except ValueError:
        return

    rows = await asyncio.to_thread(_pending_alert_picks, None)
    candidates = [r for r in rows if r["pick_date"] == pick_date and int(r["game_pk"]) == game_pk]
    if not candidates:
        await query.edit_message_text("⚠️ Esa selección ya no está disponible en el tracking.")
        return
    row = candidates[0]
    enabled = action == "on"
    await asyncio.to_thread(
        _set_game_alert_enabled, chat_id, row["pick_date"], row["game_pk"], row["selection"], enabled
    )

    if context.application.job_queue is not None:
        name = _alert_job_name(chat_id, row)
        for job in context.application.job_queue.get_jobs_by_name(name):
            job.schedule_removal()
        if enabled:
            await schedule_alert_jobs(context.application, row["pick_date"])

    text, inline_markup, _items = await _build_alerts_panel(chat_id)
    await query.edit_message_text(text, reply_markup=inline_markup)


async def trackstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        info = await asyncio.to_thread(tracking_status_snapshot)
    except Exception as exc:
        await update.message.reply_text(f"❌ Tracking DB error: {exc}")
        return
    await update.message.reply_text(
        "🧾 TRACKING ENGINE — v2.9.1\n"
        f"DB: {info['path']}\n"
        f"Railway Volume: {info['railway_volume'] or 'NO DETECTADO'}\n"
        f"Total snapshots: {info['total']}\n"
        f"Official: {info['official']} | Fallback audit: {info['fallback']}\n"
        f"Settled W/L: {info['settled']} | Pending: {info['pending']}\n"
        f"Auto-settle: {'ON' if TRACK_AUTO_SETTLE else 'OFF'} — "
        f"{TRACK_SETTLE_HOUR:02d}:{TRACK_SETTLE_MINUTE:02d} {AUTO_TZ}"
    )


async def settle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        info = await asyncio.to_thread(settle_pending_picks)
    except Exception as exc:
        await update.message.reply_text(f"❌ No pude liquidar el tracking: {exc}")
        return
    await update.message.reply_text(
        "✅ TRACK SETTLEMENT — v2.9.1\n"
        f"Pending al iniciar: {info['pending_before']}\n"
        f"WIN: {info['wins']} | LOSS: {info['losses']} | VOID: {info['void']}\n"
        f"Aún pendientes: {info['still_pending']}"
    )


async def performance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        overall, by_product = await asyncio.to_thread(performance_snapshot)
    except Exception as exc:
        await update.message.reply_text(f"❌ Tracking DB error: {exc}")
        return

    n = int(overall["n"] or 0)
    if n == 0:
        await update.message.reply_text(
            "📊 PERFORMANCE — v2.9.1\nAún no hay picks oficiales liquidados."
        )
        return

    wins = int(overall["wins"] or 0)
    hit = wins / n if n else 0.0
    risked = float(overall["risked"] or 0.0)
    units = float(overall["units"] or 0.0)
    roi = units / risked if risked > 0 else None
    brier = overall["brier"]

    brier_text = "N/D" if brier is None else f"{float(brier):.4f}"
    msg = (
        "📊 PERFORMANCE — v2.9.1\n"
        f"Official settled: {n} | Sample: {_sample_label(n)}\n"
        f"W-L: {wins}-{int(overall['losses'] or 0)} | Hit Rate: {hit*100:.1f}%\n"
        f"Brier: {brier_text}\n"
    )
    if roi is not None:
        msg += (
            f"Priced picks: {int(overall['priced_n'] or 0)} | "
            f"Units: {units:+.2f}u | ROI: {roi*100:+.1f}%\n"
        )
    else:
        msg += "ROI: N/D — faltan precios accionables en los picks liquidados.\n"

    if by_product:
        msg += "\nPOR PRODUCTO\n"
        for row in by_product:
            pn = int(row["n"] or 0)
            pw = int(row["wins"] or 0)
            phr = pw / pn if pn else 0.0
            prisked = float(row["risked"] or 0.0)
            punits = float(row["units"] or 0.0)
            proi = punits / prisked if prisked > 0 else None
            roi_text = "N/D" if proi is None else f"{proi*100:+.1f}%"
            msg += (
                f"• {row['product']}: {pw}-{int(row['losses'] or 0)} | "
                f"HR {phr*100:.1f}% | ROI {roi_text} | n={pn}\n"
            )

    msg += "\n⚠️ No ajustar el modelo por muestras pequeñas; tracking busca calibración, no solo hit rate."
    await _reply_long(update.message, msg)


async def calibration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rows = await asyncio.to_thread(calibration_snapshot)
    except Exception as exc:
        await update.message.reply_text(f"❌ Tracking DB error: {exc}")
        return

    if not rows:
        await update.message.reply_text(
            "🧪 CALIBRATION — v2.9.1\nAún no hay muestra oficial suficiente para formar bins."
        )
        return

    msg = (
        "🧪 CALIBRATION — v2.9.1\n"
        "Pred = probabilidad proxy promedio | Actual = hit rate observado.\n\n"
    )
    for row in rows:
        brier_text = "N/D" if row["brier"] is None else f"{row['brier']:.4f}"
        msg += (
            f"{row['label']} | n={row['n']}\n"
            f"Pred {row['predicted']*100:.1f}% | Actual {row['actual']*100:.1f}% | "
            f"Error {row['error']*100:+.1f} pp | Brier {brier_text}\n\n"
        )
    msg += "⚠️ Esto evalúa el proxy actual; no lo convierte automáticamente en probabilidad calibrada."
    await _reply_long(update.message, msg)


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rows = await asyncio.to_thread(recent_history_snapshot, 12)
    except Exception as exc:
        await update.message.reply_text(f"❌ Tracking DB error: {exc}")
        return

    if not rows:
        await update.message.reply_text("📚 HISTORY — v2.9.1\nAún no hay picks registrados.")
        return

    msg = "📚 HISTORY — v2.9.1\nÚltimos registros:\n\n"
    for row in rows:
        official = "OFF" if row["official"] else "AUDIT"
        odds = _format_american(row["odds"])
        msg += (
            f"{row['pick_date']} | {row['result']} | {official}\n"
            f"{row['selection']} ML ({odds}) — {row['product']}\n"
            f"{row['away']} vs {row['home']} | MP {_pct(row['model_probability'])} | "
            f"MKT {_pct(row['market_probability'])} | {_format_units(row['units_won_lost'])}\n\n"
        )
    await _reply_long(update.message, msg)


async def auto_settle_tracking(context: ContextTypes.DEFAULT_TYPE):
    if not TRACK_AUTO_SETTLE:
        return
    try:
        info = await asyncio.to_thread(settle_pending_picks)
        if info["wins"] or info["losses"] or info["void"]:
            print(
                "✅ Tracking auto-settle: "
                f"W {info['wins']} | L {info['losses']} | V {info['void']} | "
                f"pending {info['still_pending']}"
            )
    except Exception as exc:
        print(f"❌ Tracking auto-settle error: {exc}")


# ---------------------------------------------------------------------------
# v2.9.8 MEMBERSHIP / TELEGRAM STARS
# ---------------------------------------------------------------------------

def init_membership_db():
    """Create subscription and one-time FREE trial tables without touching tracking/alert data."""
    with _tracking_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER NOT NULL,
                plan TEXT NOT NULL,
                username TEXT,
                first_name TEXT,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                started_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                charge_id TEXT NOT NULL,
                is_recurring INTEGER NOT NULL DEFAULT 1,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, plan)
            );

            CREATE INDEX IF NOT EXISTS idx_subscriptions_active
            ON subscriptions(user_id, status, expires_at);

            CREATE TABLE IF NOT EXISTS subscription_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan TEXT NOT NULL,
                amount_stars INTEGER NOT NULL,
                charge_id TEXT NOT NULL UNIQUE,
                invoice_payload TEXT NOT NULL,
                paid_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                is_recurring INTEGER NOT NULL DEFAULT 0,
                is_first_recurring INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS membership_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                trial_started_at INTEGER NOT NULL,
                trial_expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_membership_trial_expiry
            ON membership_users(trial_expires_at);
            """
        )


def migrate_legacy_free_trials_to_30_days():
    """Upgrade legacy 7-day FREE trials to 30 days from their original start.

    Idempotent: only rows whose stored trial duration is approximately 7 days
    are changed, so current 30-day trials are never extended again.
    """
    legacy_max_seconds = 8 * 24 * 60 * 60
    new_period_seconds = FREE_TRIAL_PERIOD
    now_ts = _subscription_now_ts()
    with _tracking_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE membership_users
            SET trial_expires_at = trial_started_at + ?,
                updated_at = ?
            WHERE (trial_expires_at - trial_started_at) > 0
              AND (trial_expires_at - trial_started_at) <= ?
            """,
            (new_period_seconds, now_ts, legacy_max_seconds),
        )
        migrated = int(cursor.rowcount or 0)
    if migrated:
        print(f"✅ Migración FREE: {migrated} prueba(s) antigua(s) ampliada(s) a 30 días.")
    return migrated


def _subscription_now_ts():
    return int(datetime.now(timezone.utc).timestamp())


def _subscription_plan_from_payload(payload):
    if not payload or not payload.startswith("TRIPLE_PICK_SUB:"):
        return None
    plan = payload.split(":", 1)[1].strip().upper()
    return plan if plan in {"PREMIUM", "PRO"} else None


def _ensure_trial_user(user):
    """Create the one-time 30-day FREE trial on first use; never reset it."""
    now_ts = _subscription_now_ts()
    trial_expires = now_ts + FREE_TRIAL_PERIOD
    with _tracking_connection() as conn:
        conn.execute(
            """
            INSERT INTO membership_users (
                user_id, username, first_name, trial_started_at,
                trial_expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                updated_at = excluded.updated_at
            """,
            (
                int(user.id), user.username, user.first_name, now_ts,
                trial_expires, now_ts, now_ts,
            ),
        )
        return conn.execute(
            """
            SELECT user_id, trial_started_at, trial_expires_at
            FROM membership_users
            WHERE user_id = ?
            """,
            (int(user.id),),
        ).fetchone()


def _trial_row(user_id):
    with _tracking_connection() as conn:
        return conn.execute(
            """
            SELECT user_id, trial_started_at, trial_expires_at
            FROM membership_users
            WHERE user_id = ?
            """,
            (int(user_id),),
        ).fetchone()


def _trial_status(user_id):
    row = _trial_row(user_id)
    if row is None:
        return False, None
    return int(row["trial_expires_at"]) > _subscription_now_ts(), row


def _remaining_trial_text(expires_at):
    remaining = max(0, int(expires_at) - _subscription_now_ts())
    days, rem = divmod(remaining, 86400)
    hours = rem // 3600
    if days > 0:
        return f"{days} día(s) y {hours} h"
    if hours > 0:
        return f"{hours} h"
    minutes = max(1, remaining // 60)
    return f"{minutes} min"


def _active_subscription_rows(user_id):
    now_ts = _subscription_now_ts()
    with _tracking_connection() as conn:
        return conn.execute(
            """
            SELECT user_id, plan, status, expires_at, charge_id, is_recurring
            FROM subscriptions
            WHERE user_id = ? AND status = 'ACTIVE' AND expires_at > ?
            ORDER BY expires_at DESC
            """,
            (int(user_id), now_ts),
        ).fetchall()


def _best_active_plan(user_id):
    if int(user_id) in SUBSCRIPTION_ADMIN_IDS:
        return "PRO", None
    rows = _active_subscription_rows(user_id)
    if rows:
        best = max(rows, key=lambda r: PLAN_RANK.get(r["plan"], 0))
        return best["plan"], best
    trial_active, trial = _trial_status(user_id)
    if trial_active:
        return "FREE", trial
    if trial is None:
        return "NONE", None
    return "EXPIRED", trial


def _has_plan(user_id, required_plan="PREMIUM"):
    plan, _row = _best_active_plan(user_id)
    if plan == "FREE":
        # During the 30-day trial, FREE users can test PREMIUM-level content.
        return required_plan == "PREMIUM"
    return PLAN_RANK.get(plan, -1) >= PLAN_RANK.get(required_plan, 1)


def _save_successful_subscription(user, payment):
    plan = _subscription_plan_from_payload(payment.invoice_payload)
    if plan is None or payment.currency != "XTR":
        return None

    now_ts = _subscription_now_ts()
    expires_at = int(payment.subscription_expiration_date or (now_ts + SUBSCRIPTION_PERIOD))
    charge_id = payment.telegram_payment_charge_id
    recurring = bool(payment.is_recurring)
    first_recurring = bool(payment.is_first_recurring)

    with _tracking_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO subscription_payments (
                user_id, plan, amount_stars, charge_id, invoice_payload,
                paid_at, expires_at, is_recurring, is_first_recurring
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(user.id), plan, int(payment.total_amount), charge_id,
                payment.invoice_payload, now_ts, expires_at,
                int(recurring), int(first_recurring),
            ),
        )
        conn.execute(
            """
            INSERT INTO subscriptions (
                user_id, plan, username, first_name, status, started_at,
                expires_at, charge_id, is_recurring, updated_at
            ) VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, plan) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                status = 'ACTIVE',
                expires_at = MAX(subscriptions.expires_at, excluded.expires_at),
                charge_id = excluded.charge_id,
                is_recurring = excluded.is_recurring,
                updated_at = excluded.updated_at
            """,
            (
                int(user.id), plan, user.username, user.first_name,
                now_ts, expires_at, charge_id, int(recurring), now_ts,
            ),
        )
    return plan, expires_at


def _format_expiry(expires_at):
    if not expires_at:
        return "N/D"
    return datetime.fromtimestamp(int(expires_at), tz=LOCAL_TZ).strftime("%d %b %Y, %I:%M %p")



def _membership_admin_stats():
    """Return membership counts for the private admin dashboard."""
    now_ts = _subscription_now_ts()
    day_ago = now_ts - 24 * 60 * 60
    week_ago = now_ts - 7 * 24 * 60 * 60

    with _tracking_connection() as conn:
        total_users = conn.execute(
            "SELECT COUNT(*) FROM membership_users"
        ).fetchone()[0]

        free_active = conn.execute(
            """
            SELECT COUNT(*)
            FROM membership_users m
            WHERE m.trial_expires_at > ?
              AND NOT EXISTS (
                  SELECT 1 FROM subscriptions s
                  WHERE s.user_id = m.user_id
                    AND s.status = 'ACTIVE'
                    AND s.expires_at > ?
                    AND s.plan IN ('PREMIUM', 'PRO')
              )
            """,
            (now_ts, now_ts),
        ).fetchone()[0]

        free_expired = conn.execute(
            """
            SELECT COUNT(*)
            FROM membership_users m
            WHERE m.trial_expires_at <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM subscriptions s
                  WHERE s.user_id = m.user_id
                    AND s.status = 'ACTIVE'
                    AND s.expires_at > ?
                    AND s.plan IN ('PREMIUM', 'PRO')
              )
            """,
            (now_ts, now_ts),
        ).fetchone()[0]

        premium_active = conn.execute(
            """
            SELECT COUNT(DISTINCT user_id)
            FROM subscriptions
            WHERE plan = 'PREMIUM' AND status = 'ACTIVE' AND expires_at > ?
            """,
            (now_ts,),
        ).fetchone()[0]

        pro_active = conn.execute(
            """
            SELECT COUNT(DISTINCT user_id)
            FROM subscriptions
            WHERE plan = 'PRO' AND status = 'ACTIVE' AND expires_at > ?
            """,
            (now_ts,),
        ).fetchone()[0]

        paid_active = conn.execute(
            """
            SELECT COUNT(DISTINCT user_id)
            FROM subscriptions
            WHERE status = 'ACTIVE' AND expires_at > ?
              AND plan IN ('PREMIUM', 'PRO')
            """,
            (now_ts,),
        ).fetchone()[0]

        new_today = conn.execute(
            "SELECT COUNT(*) FROM membership_users WHERE created_at >= ?",
            (day_ago,),
        ).fetchone()[0]

        new_week = conn.execute(
            "SELECT COUNT(*) FROM membership_users WHERE created_at >= ?",
            (week_ago,),
        ).fetchone()[0]

    return {
        "total_users": int(total_users),
        "free_active": int(free_active),
        "free_expired": int(free_expired),
        "premium_active": int(premium_active),
        "pro_active": int(pro_active),
        "paid_active": int(paid_active),
        "new_today": int(new_today),
        "new_week": int(new_week),
    }



def _membership_admin_users(page=1, page_size=15):
    """Return one page of membership users with their current plan/status."""
    now_ts = _subscription_now_ts()
    page = max(1, int(page or 1))
    page_size = max(1, min(25, int(page_size or 15)))
    offset = (page - 1) * page_size

    with _tracking_connection() as conn:
        total = int(conn.execute(
            "SELECT COUNT(*) FROM membership_users"
        ).fetchone()[0])

        rows = conn.execute(
            """
            SELECT
                m.user_id, m.username, m.first_name,
                m.trial_started_at, m.trial_expires_at, m.created_at,
                (
                    SELECT s.plan
                    FROM subscriptions s
                    WHERE s.user_id = m.user_id
                      AND s.status = 'ACTIVE'
                      AND s.expires_at > ?
                      AND s.plan IN ('PREMIUM', 'PRO')
                    ORDER BY CASE s.plan WHEN 'PRO' THEN 2 WHEN 'PREMIUM' THEN 1 ELSE 0 END DESC,
                             s.expires_at DESC
                    LIMIT 1
                ) AS paid_plan,
                (
                    SELECT s.expires_at
                    FROM subscriptions s
                    WHERE s.user_id = m.user_id
                      AND s.status = 'ACTIVE'
                      AND s.expires_at > ?
                      AND s.plan IN ('PREMIUM', 'PRO')
                    ORDER BY CASE s.plan WHEN 'PRO' THEN 2 WHEN 'PREMIUM' THEN 1 ELSE 0 END DESC,
                             s.expires_at DESC
                    LIMIT 1
                ) AS paid_expires_at
            FROM membership_users m
            ORDER BY m.created_at DESC, m.user_id DESC
            LIMIT ? OFFSET ?
            """,
            (now_ts, now_ts, page_size, offset),
        ).fetchall()

    total_pages = max(1, (total + page_size - 1) // page_size)
    return total, total_pages, rows


def _format_admin_user_line(row, index):
    username = (row["username"] or "").strip()
    first_name = (row["first_name"] or "").strip()
    identity = f"@{username}" if username else (first_name or f"ID {row['user_id']}")
    joined = datetime.fromtimestamp(
        int(row["created_at"]), tz=LOCAL_TZ
    ).strftime("%d/%m/%Y")

    if row["paid_plan"]:
        plan = row["paid_plan"]
        expires_at = int(row["paid_expires_at"] or 0)
        remaining = _remaining_trial_text(expires_at) if expires_at else "N/D"
        status = f"{plan} · {remaining}"
        expiry = _format_expiry(expires_at)
    else:
        trial_expires = int(row["trial_expires_at"] or 0)
        if trial_expires > _subscription_now_ts():
            status = f"FREE · {_remaining_trial_text(trial_expires)}"
        else:
            status = "FREE VENCIDO"
        expiry = _format_expiry(trial_expires)

    return (
        f"{index}. {identity}\n"
        f"   ID: {row['user_id']} | Alta: {joined}\n"
        f"   {status} | Vence: {expiry}"
    )


async def adminusers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Private paginated membership list for configured administrators."""
    user = update.effective_user
    if user is None or int(user.id) not in SUBSCRIPTION_ADMIN_IDS:
        await update.effective_message.reply_text("⛔ Comando exclusivo para administradores.")
        return

    page = 1
    if context.args:
        try:
            page = max(1, int(context.args[0]))
        except (TypeError, ValueError):
            await update.effective_message.reply_text("Uso: /adminusers o /adminusers 2")
            return

    total, total_pages, rows = await asyncio.to_thread(_membership_admin_users, page, 15)
    if page > total_pages:
        await update.effective_message.reply_text(
            f"No existe la página {page}. Total de páginas: {total_pages}."
        )
        return

    header = (
        "👥 TRIPLE PICK — USUARIOS\n\n"
        f"Total registrados: {total}\n"
        f"Página {page}/{total_pages}\n\n"
    )
    if not rows:
        body = "No hay usuarios registrados."
    else:
        start = (page - 1) * 15 + 1
        body = "\n\n".join(
            _format_admin_user_line(row, start + i)
            for i, row in enumerate(rows)
        )

    footer = ""
    if total_pages > 1:
        footer = "\n\nNavegación: /adminusers 1, /adminusers 2, etc."

    await update.effective_message.reply_text(header + body + footer)

async def adminstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Private membership counter for configured Triple Pick administrators."""
    user = update.effective_user
    if user is None or int(user.id) not in SUBSCRIPTION_ADMIN_IDS:
        await update.effective_message.reply_text("⛔ Comando exclusivo para administradores.")
        return

    stats = await asyncio.to_thread(_membership_admin_stats)
    text = (
        "📊 TRIPLE PICK — SUSCRIPCIONES\n\n"
        f"👥 Usuarios registrados: {stats['total_users']}\n\n"
        f"🆓 FREE activos: {stats['free_active']}\n"
        f"⏳ FREE vencidos: {stats['free_expired']}\n\n"
        f"⭐ PREMIUM activos: {stats['premium_active']}\n"
        f"🚀 PRO activos: {stats['pro_active']}\n\n"
        f"💳 Suscripciones pagadas activas: {stats['paid_active']}\n"
        f"📅 Nuevos FREE últimas 24 h: {stats['new_today']}\n"
        f"📅 Nuevos FREE últimos 7 días: {stats['new_week']}"
    )
    await update.effective_message.reply_text(text)


def _admin_expiring_rows(days=7, limit=25):
    """Return memberships expiring within the next N days."""
    now_ts = _subscription_now_ts()
    cutoff = now_ts + int(days) * 86400
    with _tracking_connection() as conn:
        rows = conn.execute(
            """
            SELECT m.user_id, m.username, m.first_name, m.trial_expires_at,
                   s.plan AS paid_plan, s.expires_at AS paid_expires_at
            FROM membership_users m
            LEFT JOIN subscriptions s
              ON s.user_id = m.user_id
             AND s.status = 'ACTIVE'
             AND s.expires_at > ?
             AND s.plan IN ('PREMIUM','PRO')
            WHERE (s.expires_at BETWEEN ? AND ?)
               OR (s.user_id IS NULL AND m.trial_expires_at BETWEEN ? AND ?)
            ORDER BY COALESCE(s.expires_at, m.trial_expires_at) ASC
            LIMIT ?
            """,
            (now_ts, now_ts, cutoff, now_ts, cutoff, int(limit)),
        ).fetchall()
    return rows


def _admin_paid_rows(limit=25):
    now_ts = _subscription_now_ts()
    with _tracking_connection() as conn:
        return conn.execute(
            """
            SELECT user_id, plan, username, first_name, expires_at
            FROM subscriptions
            WHERE status = 'ACTIVE' AND expires_at > ?
              AND plan IN ('PREMIUM','PRO')
            ORDER BY expires_at ASC
            LIMIT ?
            """,
            (now_ts, int(limit)),
        ).fetchall()


def _official_pick_rows(pick_date):
    init_tracking_db()
    with _tracking_connection() as conn:
        return conn.execute(
            """
            SELECT pick_date, slot, game_pk, game_date, away, home, selection, pitcher,
                   pick_text, market_family, line, created_by, created_at, updated_at
            FROM official_daily_picks
            WHERE pick_date=?
            ORDER BY slot ASC
            """,
            (pick_date,),
        ).fetchall()


def _clear_official_picks(pick_date):
    init_tracking_db()
    with _tracking_connection() as conn:
        conn.execute("DELETE FROM official_daily_picks WHERE pick_date=?", (pick_date,))
        conn.execute(
            "DELETE FROM tracked_picks WHERE pick_date=? AND source='CHANNEL_OFFICIAL' AND result='PENDING'",
            (pick_date,),
        )


def _today_schedule_games(pick_date):
    data = safe_get_json(
        f"{MLB_API}/schedule",
        params={"sportId": 1, "date": pick_date, "hydrate": "probablePitcher,team"},
        cache_ttl=60,
    )
    games = []
    for block in (data or {}).get("dates", []):
        for game in block.get("games", []):
            away = game.get("teams", {}).get("away", {})
            home = game.get("teams", {}).get("home", {})
            games.append({
                "game_pk": int(game.get("gamePk") or 0),
                "game_date": game.get("gameDate"),
                "away": away.get("team", {}).get("name") or "Visitante",
                "home": home.get("team", {}).get("name") or "Local",
                "away_pitcher": away.get("probablePitcher", {}).get("fullName") or "Por confirmar",
                "home_pitcher": home.get("probablePitcher", {}).get("fullName") or "Por confirmar",
            })
    return games


def _resolve_official_team(team_text, games):
    """Resolve one team name/nickname against the day's MLB schedule."""
    key = _normalize_team_name(team_text)
    exact = []
    partial = []
    for game in games:
        for side in ("away", "home"):
            name = game[side]
            nkey = _normalize_team_name(name)
            if key == nkey:
                exact.append((game, side))
            elif key and (key in nkey or nkey in key):
                partial.append((game, side))
    matches = exact or partial
    if len(matches) != 1:
        return None
    game, side = matches[0]
    return {
        "game_pk": game["game_pk"],
        "game_date": game["game_date"],
        "away": game["away"],
        "home": game["home"],
        "selection": game[side],
        "pitcher": game[f"{side}_pitcher"],
        "side": side,
    }


def _resolve_official_matchup(left_text, right_text, games):
    """Resolve a written A vs B matchup to one scheduled MLB game."""
    left = _resolve_official_team(left_text, games)
    right = _resolve_official_team(right_text, games)
    if not left or not right or left["game_pk"] != right["game_pk"]:
        return None
    game = next((g for g in games if int(g["game_pk"]) == int(left["game_pk"])), None)
    if not game:
        return None
    return {
        "game_pk": game["game_pk"],
        "game_date": game["game_date"],
        "away": game["away"],
        "home": game["home"],
        "selection": f"{game['away']} vs {game['home']}",
        "pitcher": f"{game['away_pitcher']} / {game['home_pitcher']}",
    }


def _fmt_line(value):
    value = float(value)
    if value.is_integer():
        return str(int(value))
    return (f"{value:.2f}").rstrip("0").rstrip(".")


def _parse_official_pick(raw_text, games):
    """Parse an admin-entered channel pick and bind it to today's MLB game.

    Supported examples:
      Baltimore Orioles ML
      Orioles Over 3.5                    -> team total over
      Orioles Team Total Over 3.5
      Orioles +1.5                        -> run line
      Orioles vs Yankees Over 8.5         -> full-game total
    A bare team name remains backward-compatible and means ML.
    """
    raw = re.sub(r"\s+", " ", str(raw_text or "").strip())
    if not raw:
        return None, "Pick vacío."

    # Full-game total must be checked before team-total syntax.
    m = re.match(r"^(.+?)\s+vs\.?\s+(.+?)\s+(over|under)\s+([0-9]+(?:\.[0-9]+)?)$", raw, re.I)
    if m:
        matchup = _resolve_official_matchup(m.group(1), m.group(2), games)
        if not matchup:
            return None, f"No pude identificar el partido de forma única: {raw}"
        direction = m.group(3).upper()
        line = float(m.group(4))
        family = f"FULL_GAME_TOTAL_{direction}"
        matchup["market_family"] = family
        matchup["line"] = line
        matchup["pick_text"] = f"{matchup['away']} vs {matchup['home']} {direction.title()} {_fmt_line(line)}"
        return matchup, None

    # Team total. The words "Team Total" are optional, so "Orioles Over 3.5" works.
    m = re.match(r"^(.+?)\s+(?:team\s+total\s+)?(over|under)\s+([0-9]+(?:\.[0-9]+)?)$", raw, re.I)
    if m:
        team = _resolve_official_team(m.group(1), games)
        if not team:
            return None, f"No pude identificar de forma única el equipo: {m.group(1)}"
        direction = m.group(2).upper()
        line = float(m.group(3))
        team["market_family"] = f"TEAM_TOTAL_{direction}"
        team["line"] = line
        team["pick_text"] = f"{team['selection']} Team Total {direction.title()} {_fmt_line(line)}"
        return team, None

    # Run line / spread.
    m = re.match(r"^(.+?)\s+([+-][0-9]+(?:\.[0-9]+)?)$", raw, re.I)
    if m:
        team = _resolve_official_team(m.group(1), games)
        if not team:
            return None, f"No pude identificar de forma única el equipo: {m.group(1)}"
        line = float(m.group(2))
        team["market_family"] = "RUN_LINE"
        team["line"] = line
        sign = "+" if line > 0 else ""
        team["pick_text"] = f"{team['selection']} {sign}{_fmt_line(line)}"
        return team, None

    # Explicit ML or Moneyline.
    m = re.match(r"^(.+?)\s+(?:ml|moneyline)$", raw, re.I)
    if m:
        team = _resolve_official_team(m.group(1), games)
        if not team:
            return None, f"No pude identificar de forma única el equipo: {m.group(1)}"
        team["market_family"] = "FULL_GAME_ML"
        team["line"] = None
        team["pick_text"] = f"{team['selection']} ML"
        return team, None

    # Helpful validation for an incomplete over/under such as "Orioles Over".
    if re.search(r"\b(over|under)\s*$", raw, re.I):
        return None, f"Falta la línea del mercado en: {raw}. Ejemplo: Orioles Over 3.5"

    # Backward compatibility: a bare team is still interpreted as ML.
    team = _resolve_official_team(raw, games)
    if not team:
        return None, f"No pude identificar de forma única el pick: {raw}"
    team["market_family"] = "FULL_GAME_ML"
    team["line"] = None
    team["pick_text"] = f"{team['selection']} ML"
    return team, None


def _save_official_market_picks(pick_date, pick_lines, admin_user_id):
    """Parse three complete channel picks and persist one canonical bot/channel slate."""
    clean = [str(x).strip() for x in pick_lines if str(x).strip()]
    if len(clean) != 3:
        return False, "Debes enviar exactamente 3 picks.", []

    games = _today_schedule_games(pick_date)
    if not games:
        return False, "No pude obtener la cartelera MLB para esa fecha.", []

    resolved = []
    used_games = set()
    for pick_text in clean:
        item, error = _parse_official_pick(pick_text, games)
        if item is None:
            return False, error or f"No pude interpretar: {pick_text}", []
        if item["game_pk"] in used_games:
            return False, f"Hay dos selecciones del mismo partido: {item['away']} vs {item['home']}", []
        used_games.add(item["game_pk"])
        resolved.append(item)

    now_iso = local_now().isoformat()
    with _tracking_connection() as conn:
        conn.execute(
            "UPDATE tracked_picks SET official=0 WHERE pick_date=? AND source IN ('TRIPLE_PICK','TRIPLE_PICK_FALLBACK')",
            (pick_date,),
        )
        conn.execute("DELETE FROM official_daily_picks WHERE pick_date=?", (pick_date,))
        conn.execute(
            "DELETE FROM tracked_picks WHERE pick_date=? AND source='CHANNEL_OFFICIAL' AND result='PENDING'",
            (pick_date,),
        )

        for slot, item in enumerate(resolved, 1):
            conn.execute(
                """
                INSERT INTO official_daily_picks (
                    pick_date, slot, game_pk, game_date, away, home, selection, pitcher,
                    pick_text, market_family, line, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pick_date, slot, item["game_pk"], item["game_date"], item["away"], item["home"],
                    item["selection"], item["pitcher"], item["pick_text"], item["market_family"],
                    item["line"], int(admin_user_id), now_iso, now_iso,
                ),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO tracked_picks (
                    created_at, pick_date, game_pk, game_date, away, home, selection,
                    product, source, official, market_family, line, odds, book,
                    model_version, bot_version, result, units_risked, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'OFFICIAL', 'CHANNEL_OFFICIAL', 1,
                          ?, ?, NULL, NULL, 'CHANNEL_OFFICIAL', ?,
                          'PENDING', 1.0, 'Confirmed by admin for channel/bot sync')
                """,
                (
                    now_iso, pick_date, item["game_pk"], item["game_date"], item["away"], item["home"],
                    item["selection"], item["market_family"], item["line"], BOT_VERSION,
                ),
            )
    return True, "OK", resolved


def _save_official_team_picks(pick_date, team_names, admin_user_id):
    """Backward-compatible wrapper: bare team names are treated as moneylines."""
    return _save_official_market_picks(pick_date, team_names, admin_user_id)


def _format_official_picks_text(rows, title="🎯 PICKS OFICIALES TRIPLE PICK"):
    if not rows:
        return title + "\n\nTodavía no hay picks oficiales cargados para hoy."
    medals = ["🥇", "🥈", "🥉"]
    lines = [title, ""]
    for i, row in enumerate(rows):
        time_text = format_game_time_local(row["game_date"]) if row["game_date"] else "N/D"
        pick_text = row["pick_text"] if "pick_text" in row.keys() and row["pick_text"] else None
        if not pick_text:
            pick_text = _tracked_pick_display(row)
        lines.extend([
            f"{medals[i] if i < 3 else '•'} PICK #{row['slot']}",
            f"🏟️ {row['away']} vs {row['home']}",
            f"🎯 {pick_text}",
            f"⚾ Pitcher: {row['pitcher'] or 'Por confirmar'}",
            f"🕐 {time_text}",
            "",
        ])
    lines.append("✅ Esta es la lista maestra usada por el bot, las alertas y el canal.")
    return "\n".join(lines)


OFFICIAL_PICKS_ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📋 Ver picks oficiales", "📝 Cargar 3 picks"],
        ["🤖 Usar picks del modelo", "🗑️ Borrar picks oficiales"],
        ["⬅️ Panel Admin"],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Picks oficiales Triple Pick",
)


async def admin_official_picks_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or int(user.id) not in SUBSCRIPTION_ADMIN_IDS:
        await update.effective_message.reply_text("⛔ Acceso exclusivo para administradores.")
        return
    fecha = local_now().strftime("%Y-%m-%d")
    rows = await asyncio.to_thread(_official_pick_rows, fecha)
    await update.effective_message.reply_text(
        _format_official_picks_text(rows) +
        "\n\n📝 Cargar 3 picks: pega los tres picks completos, uno por línea.\n"
        "Formatos: Orioles ML · Orioles Over 3.5 · Orioles +1.5 · Orioles vs Yankees Over 8.5.\n"
        "🤖 Usar picks del modelo: toma el Triple Pick calculado por el bot y lo fija como oficial.",
        reply_markup=OFFICIAL_PICKS_ADMIN_KEYBOARD,
    )


async def admin_official_start_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or int(user.id) not in SUBSCRIPTION_ADMIN_IDS:
        await update.effective_message.reply_text("⛔ Acceso exclusivo para administradores.")
        return
    context.user_data["awaiting_official_picks"] = True
    await update.effective_message.reply_text(
        "📝 CARGAR PICKS OFICIALES\n\n"
        "Envía exactamente 3 picks MLB completos, uno por línea, tal como quieres que aparezcan en el canal.\n\n"
        "Ejemplos válidos:\n"
        "Baltimore Orioles ML\n"
        "Orioles Over 3.5\n"
        "Dodgers vs Padres Over 8.5\n\n"
        "También acepta Team Total Over/Under y run line (+1.5/-1.5).\n"
        "El bot identificará automáticamente rival, pitcher, horario y partido.\n"
        "Pulsa ❌ Cancelar carga para salir.",
        reply_markup=ReplyKeyboardMarkup([["❌ Cancelar carga"]], resize_keyboard=True, is_persistent=True),
    )


async def _handle_official_picks_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_official_picks"):
        return False
    user = update.effective_user
    if user is None or int(user.id) not in SUBSCRIPTION_ADMIN_IDS:
        context.user_data.pop("awaiting_official_picks", None)
        return False
    text = (update.message.text or "").strip()
    if text == "❌ Cancelar carga":
        context.user_data.pop("awaiting_official_picks", None)
        await update.effective_message.reply_text("Carga cancelada.", reply_markup=OFFICIAL_PICKS_ADMIN_KEYBOARD)
        return True
    picks_input = [line.strip().lstrip("123.-) ") for line in text.splitlines() if line.strip()]
    if len(picks_input) != 3:
        await update.effective_message.reply_text(
            "⚠️ Necesito exactamente 3 líneas, una por pick. Inténtalo nuevamente o pulsa ❌ Cancelar carga."
        )
        return True
    fecha = local_now().strftime("%Y-%m-%d")
    ok, msg, _resolved = await asyncio.to_thread(_save_official_market_picks, fecha, picks_input, user.id)
    if not ok:
        await update.effective_message.reply_text(f"❌ {msg}\n\nCorrige el pick y vuelve a enviar las 3 líneas.")
        return True
    context.user_data.pop("awaiting_official_picks", None)
    rows = await asyncio.to_thread(_official_pick_rows, fecha)
    try:
        await schedule_alert_jobs(context.application, fecha)
    except Exception as exc:
        print(f"Official picks alert scheduling error: {exc}")
    await update.effective_message.reply_text(
        "✅ PICKS OFICIALES GUARDADOS\n\n" + _format_official_picks_text(rows),
        reply_markup=OFFICIAL_PICKS_ADMIN_KEYBOARD,
    )
    return True


async def admin_official_use_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or int(user.id) not in SUBSCRIPTION_ADMIN_IDS:
        await update.effective_message.reply_text("⛔ Acceso exclusivo para administradores.")
        return
    now = local_now()
    fecha = now.strftime("%Y-%m-%d")
    status, partidos, odds_status = await asyncio.to_thread(build_daily_matchups_v28, fecha, now.year)
    if status != "ok":
        await update.effective_message.reply_text("❌ No pude obtener el slate actual de MLB.")
        return
    triple_pick, _mode = _select_triple_pick_v28(partidos, odds_status)
    if len(triple_pick) < 3:
        await update.effective_message.reply_text(
            f"⚠️ El modelo solo produjo {len(triple_pick)} pick(s). No se fijó ningún slate oficial."
        )
        return
    teams = [p["favorite"] for p in triple_pick[:3]]
    ok, msg, _ = await asyncio.to_thread(_save_official_team_picks, fecha, teams, user.id)
    if not ok:
        await update.effective_message.reply_text(f"❌ {msg}")
        return
    rows = await asyncio.to_thread(_official_pick_rows, fecha)
    try:
        await schedule_alert_jobs(context.application, fecha)
    except Exception as exc:
        print(f"Official picks alert scheduling error: {exc}")
    await update.effective_message.reply_text(
        "✅ EL TRIPLE PICK DEL MODELO QUEDÓ FIJADO COMO OFICIAL\n\n" + _format_official_picks_text(rows),
        reply_markup=OFFICIAL_PICKS_ADMIN_KEYBOARD,
    )


async def admin_official_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or int(user.id) not in SUBSCRIPTION_ADMIN_IDS:
        await update.effective_message.reply_text("⛔ Acceso exclusivo para administradores.")
        return
    fecha = local_now().strftime("%Y-%m-%d")
    await asyncio.to_thread(_clear_official_picks, fecha)
    await update.effective_message.reply_text(
        "🗑️ Picks oficiales de hoy eliminados.\n\nHasta que cargues otros, ⚾ Picks de hoy mostrará que están pendientes de publicación.",
        reply_markup=OFFICIAL_PICKS_ADMIN_KEYBOARD,
    )


def _admin_identity(row):
    username = (row['username'] or '').strip() if 'username' in row.keys() else ''
    first_name = (row['first_name'] or '').strip() if 'first_name' in row.keys() else ''
    return f"@{username}" if username else (first_name or f"ID {row['user_id']}")


ADMIN_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📊 Estadísticas", "👥 Usuarios"],
        ["🎯 Picks oficiales", "⚽ Admin Fútbol"],
        ["⏳ Vencen pronto", "💳 Suscripciones"],
        ["⬅️ Menú principal"],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Panel administrativo Triple Pick",
)


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or int(user.id) not in SUBSCRIPTION_ADMIN_IDS:
        await update.effective_message.reply_text("⛔ Acceso exclusivo para administradores.")
        return
    await update.effective_message.reply_text(
        "🛡️ PANEL ADMIN — TRIPLE PICK\n\n"
        "📊 Estadísticas — conteo general\n"
        "👥 Usuarios — listado de miembros\n"
        "🎯 Picks oficiales — lista maestra del canal y el bot\n"
        "⏳ Vencen pronto — próximos 7 días\n"
        "💳 Suscripciones — PREMIUM/PRO activas",
        reply_markup=ADMIN_MENU_KEYBOARD,
    )


async def admin_expiring_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or int(user.id) not in SUBSCRIPTION_ADMIN_IDS:
        await update.effective_message.reply_text("⛔ Comando exclusivo para administradores.")
        return
    rows = await asyncio.to_thread(_admin_expiring_rows, 7, 25)
    if not rows:
        text = "⏳ VENCEN PRONTO\n\nNo hay membresías que venzan en los próximos 7 días."
    else:
        lines = []
        for i, row in enumerate(rows, 1):
            paid = row['paid_plan']
            exp = int(row['paid_expires_at'] or row['trial_expires_at'])
            plan = paid or 'FREE'
            lines.append(
                f"{i}. {_admin_identity(row)} — {plan}\n"
                f"   {_remaining_trial_text(exp)} | {_format_expiry(exp)}"
            )
        text = "⏳ VENCEN PRONTO — 7 DÍAS\n\n" + "\n\n".join(lines)
    await update.effective_message.reply_text(text, reply_markup=ADMIN_MENU_KEYBOARD)


async def admin_subscriptions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or int(user.id) not in SUBSCRIPTION_ADMIN_IDS:
        await update.effective_message.reply_text("⛔ Comando exclusivo para administradores.")
        return
    rows = await asyncio.to_thread(_admin_paid_rows, 25)
    if not rows:
        text = "💳 SUSCRIPCIONES ACTIVAS\n\nNo hay suscripciones PREMIUM/PRO activas."
    else:
        lines = []
        for i, row in enumerate(rows, 1):
            exp = int(row['expires_at'])
            lines.append(
                f"{i}. {_admin_identity(row)} — {row['plan']}\n"
                f"   {_remaining_trial_text(exp)} | {_format_expiry(exp)}"
            )
        text = "💳 SUSCRIPCIONES ACTIVAS\n\n" + "\n\n".join(lines)
    await update.effective_message.reply_text(text, reply_markup=ADMIN_MENU_KEYBOARD)

def _membership_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ Ver planes", callback_data="tp_plans")],
        [InlineKeyboardButton("👤 Mi cuenta", callback_data="tp_account")],
    ])


def _plans_keyboard():
    # Modo de prueba: por ahora solo se ofrece la suscripción FREE de 30 días.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆓 SUSCRIBIRME FREE — 30 DÍAS", callback_data="tp_join_free")],
        [InlineKeyboardButton("👤 Mi cuenta", callback_data="tp_account")],
    ])


async def _send_plans(chat_id, context):
    text = (
        "💎 TRIPLE PICK — PRUEBA DE SUSCRIPCIÓN\n\n"
        "🆓 FREE — 30 DÍAS\n"
        "• Activación manual mediante el botón de suscripción\n"
        "• Una sola prueba por usuario de Telegram\n"
        "• Acceso a Triple Pick durante los 30 días\n\n"
        "Pulsa el botón para activar tu prueba FREE."
    )
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=_plans_keyboard())


async def subscription_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_plans(update.effective_chat.id, context)


async def account_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    plan, row = await asyncio.to_thread(_best_active_plan, user_id)
    if plan == "NONE":
        text = (
            "👤 MI CUENTA\n\n"
            "Plan: SIN SUSCRIPCIÓN\n"
            "Estado: ⚪ NO ACTIVADO\n\n"
            "Pulsa ⭐ Suscripción y después 🆓 SUSCRIBIRME FREE — 30 DÍAS."
        )
    elif plan == "FREE":
        expiry = _format_expiry(row["trial_expires_at"])
        remaining = _remaining_trial_text(row["trial_expires_at"])
        text = (
            "👤 MI CUENTA\n\n"
            "Plan: FREE — PRUEBA 30 DÍAS\n"
            "Estado: 🟢 ACTIVO\n"
            f"Tiempo restante: {remaining}\n"
            f"Finaliza: {expiry}\n\n"
            "Después puedes continuar con PREMIUM ($10/mes) o PRO ($20/mes)."
        )
    elif plan == "EXPIRED":
        text = (
            "👤 MI CUENTA\n\n"
            "Plan: FREE\n"
            "Estado: 🔴 PRUEBA FINALIZADA\n\n"
            "Elige PREMIUM ($10/mes) o PRO ($20/mes) para continuar."
        )
    else:
        expiry = _format_expiry(row["expires_at"]) if row is not None else "ADMIN"
        text = (
            "👤 MI CUENTA\n\n"
            f"Plan: {plan}\n"
            "Estado: 🟢 ACTIVO\n"
            f"Válido hasta: {expiry}\n"
            "Renovación: automática mientras la suscripción siga activa en Telegram."
        )
    await update.effective_message.reply_text(text, reply_markup=_membership_keyboard())


async def membership_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data
    if action == "tp_plans":
        await _send_plans(query.message.chat_id, context)
        return
    if action == "tp_join_free":
        current_plan, current_row = await asyncio.to_thread(_best_active_plan, query.from_user.id)
        if current_plan == "FREE":
            text = (
                "✅ YA ESTÁS SUSCRITO\n\n"
                "Plan: FREE — PRUEBA 30 DÍAS\n"
                f"Tiempo restante: {_remaining_trial_text(current_row['trial_expires_at'])}\n"
                f"Finaliza: {_format_expiry(current_row['trial_expires_at'])}"
            )
        elif current_plan == "EXPIRED":
            text = (
                "⚠️ PRUEBA FREE YA UTILIZADA\n\n"
                "La prueba gratuita de 30 días solo puede activarse una vez por usuario."
            )
        elif current_plan in {"PREMIUM", "PRO"}:
            text = f"✅ Ya tienes una membresía activa: {current_plan}."
        else:
            row = await asyncio.to_thread(_ensure_trial_user, query.from_user)
            text = (
                "✅ SUSCRIPCIÓN FREE ACTIVADA\n\n"
                "Plan: FREE — PRUEBA 30 DÍAS\n"
                "Estado: 🟢 ACTIVO\n"
                f"Finaliza: {_format_expiry(row['trial_expires_at'])}\n\n"
                "Ahora puedes entrar a 👤 Mi cuenta para comprobar tu suscripción."
            )
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=text,
            reply_markup=_membership_keyboard(),
        )
        return

    if action == "tp_account":
        user_id = query.from_user.id
        plan, row = await asyncio.to_thread(_best_active_plan, user_id)
        if plan == "NONE":
            text = (
                "👤 MI CUENTA\n\n"
                "Plan: SIN SUSCRIPCIÓN\n"
                "Estado: ⚪ NO ACTIVADO\n\n"
                "Pulsa ⭐ Ver planes para activar FREE por 30 días."
            )
        elif plan == "FREE":
            text = (
                "👤 MI CUENTA\n\n"
                "Plan: FREE — PRUEBA 30 DÍAS\n"
                "Estado: 🟢 ACTIVO\n"
                f"Tiempo restante: {_remaining_trial_text(row['trial_expires_at'])}\n"
                f"Finaliza: {_format_expiry(row['trial_expires_at'])}"
            )
        elif plan == "EXPIRED":
            text = (
                "👤 MI CUENTA\n\n"
                "Plan: FREE\n"
                "Estado: 🔴 PRUEBA FINALIZADA\n\n"
                "PREMIUM: $10/mes | PRO: $20/mes"
            )
        else:
            expiry = _format_expiry(row["expires_at"]) if row is not None else "ADMIN"
            text = f"👤 MI CUENTA\n\nPlan: {plan}\nEstado: 🟢 ACTIVO\nVálido hasta: {expiry}"
        await context.bot.send_message(chat_id=query.message.chat_id, text=text, reply_markup=_membership_keyboard())
        return

    plan = None
    stars = None
    if action == "tp_buy_premium":
        plan, stars = "PREMIUM", PREMIUM_STARS
    elif action == "tp_buy_pro":
        plan, stars = "PRO", PRO_STARS
    if plan is None:
        return

    if stars < 1 or stars > 10000:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="❌ Precio de suscripción inválido. Debe estar entre 1 y 10000 Stars.",
        )
        return

    payload = f"TRIPLE_PICK_SUB:{plan}"
    try:
        link = await context.bot.create_invoice_link(
            title=f"Triple Pick {plan}",
            description=f"Membresía {plan} de Triple Pick por 30 días con renovación automática.",
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=f"Triple Pick {plan}", amount=int(stars))],
            subscription_period=SUBSCRIPTION_PERIOD,
        )
    except TypeError:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                "❌ Esta versión de python-telegram-bot no soporta subscription_period. "
                "Actualiza python-telegram-bot antes de activar cobros."
            ),
        )
        return
    except Exception as exc:
        print(f"Subscription invoice error: {exc}")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="❌ No pude crear el enlace de pago en este momento.",
        )
        return

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=(
            f"{plan} — {stars} Stars cada 30 días\n\n"
            "Telegram mostrará los términos de la suscripción antes de confirmar el pago."
        ),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⭐ PAGAR CON STARS", url=link)]]),
    )


async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    plan = _subscription_plan_from_payload(query.invoice_payload)
    expected = PREMIUM_STARS if plan == "PREMIUM" else PRO_STARS if plan == "PRO" else None
    ok = (
        plan is not None
        and query.currency == "XTR"
        and expected is not None
        and int(query.total_amount) == int(expected)
    )
    if ok:
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="La orden no coincide con un plan válido de Triple Pick.")


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.effective_message.successful_payment
    if payment is None:
        return
    saved = await asyncio.to_thread(_save_successful_subscription, update.effective_user, payment)
    if not saved:
        await update.effective_message.reply_text("⚠️ Pago recibido, pero no pude asociarlo a un plan válido.")
        return
    plan, expires_at = saved
    await update.effective_message.reply_text(
        "✅ SUSCRIPCIÓN ACTIVADA\n\n"
        f"Plan: {plan}\n"
        f"Válido hasta: {_format_expiry(expires_at)}\n\n"
        "Tu acceso premium ya está habilitado.",
        reply_markup=_membership_keyboard(),
    )


async def _premium_gate(update, required="PREMIUM"):
    """Return True when access is allowed. Enforcement is opt-in for safe rollout."""
    if not SUBSCRIPTION_ENFORCE:
        return True
    user = update.effective_user
    if user and await asyncio.to_thread(_has_plan, user.id, required):
        return True
    await update.effective_message.reply_text(
        "🔒 Tu prueba FREE de 30 días finalizó o esta función requiere un nivel superior de Triple Pick.",
        reply_markup=_plans_keyboard(),
    )
    return False


# ---------------------------------------------------------------------------
# v3.1 MULTI-SPORT FOUNDATION — SOCCER
# ---------------------------------------------------------------------------

SOCCER_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🔥 Picks Fútbol", "🛡️ Survival Fútbol"],
        ["⭐ Top Picks Fútbol", "🎯 Player Props Fútbol"],
        ["🏆 Ligas Fútbol", "📊 Resultados Fútbol"],
        ["⬅️ Menú principal"],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Triple Pick Fútbol",
)

MLB_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["⚾ Picks de hoy", "⚾ Juegos MLB"],
        ["📊 Estado", "📈 Rendimiento"],
        ["🧮 Mercado", "📋 Historial"],
        ["🧪 Más opciones", "⬅️ Menú principal"],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Triple Pick MLB",
)

SOCCER_ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📋 Ver picks fútbol", "📝 Cargar picks fútbol"],
        ["🗑️ Borrar picks fútbol", "📊 Resultados Fútbol"],
        ["⬅️ Panel Admin"],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Admin Fútbol Triple Pick",
)


def _soccer_plan_rank(plan):
    return PLAN_RANK.get((plan or "FREE").upper(), 0)


def _soccer_pick_rows(pick_date=None, product=None):
    pick_date = pick_date or local_now().strftime("%Y-%m-%d")
    sql = "SELECT * FROM soccer_picks WHERE pick_date=?"
    params = [pick_date]
    if product:
        sql += " AND UPPER(product)=?"
        params.append(product.upper())
    sql += " ORDER BY slot, id"
    with _tracking_connection() as conn:
        return conn.execute(sql, params).fetchall()


def _soccer_visible_rows(user_id, pick_date=None, product=None):
    rows = _soccer_pick_rows(pick_date, product)
    if not SUBSCRIPTION_ENFORCE:
        return rows
    plan, _row = _best_active_plan(user_id)
    if plan in {"NONE", "EXPIRED"}:
        return []
    rank = _soccer_plan_rank(plan)
    return [r for r in rows if _soccer_plan_rank(r["plan_required"]) <= rank]


def _format_soccer_rows(rows, title="⚽ TRIPLE PICK — FÚTBOL"):
    if not rows:
        return title + "\n\nNo hay picks publicados para esta sección."
    lines = [title, ""]
    for row in rows:
        kickoff = "N/D"
        dt = _parse_iso_utc(row["kickoff_utc"])
        if dt:
            kickoff = dt.astimezone(LOCAL_TZ).strftime("%I:%M %p").lstrip("0")
        lines.extend([
            f"{row['slot']}️⃣ {row['away']} vs {row['home']}",
            f"🏆 {row['league']}",
            f"🎯 {row['selection']}",
            f"🛡️ {row['product']} | 🔐 {row['plan_required']}",
            f"🕐 {kickoff} ({AUTO_TZ})",
            "",
        ])
    return "\n".join(lines).rstrip()


async def mlb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "⚾ TRIPLE PICK — MLB\n\nSelecciona una opción.",
        reply_markup=MLB_MENU_KEYBOARD,
    )


async def soccer_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "⚽ TRIPLE PICK — FÚTBOL\n\n"
        "Los picks publicados aquí provienen de la selección final aprobada para fútbol.",
        reply_markup=SOCCER_MENU_KEYBOARD,
    )


async def soccer_picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _premium_gate(update, "FREE"):
        return
    user_id = update.effective_user.id if update.effective_user else 0
    rows = await asyncio.to_thread(_soccer_visible_rows, user_id)
    await update.effective_message.reply_text(
        _format_soccer_rows(rows, "🔥 PICKS DE HOY — FÚTBOL"),
        reply_markup=SOCCER_MENU_KEYBOARD,
    )


async def soccer_survival(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _premium_gate(update, "PREMIUM"):
        return
    user_id = update.effective_user.id if update.effective_user else 0
    rows = await asyncio.to_thread(_soccer_visible_rows, user_id, None, "SURVIVAL")
    await update.effective_message.reply_text(
        _format_soccer_rows(rows, "🛡️ SURVIVAL — FÚTBOL"),
        reply_markup=SOCCER_MENU_KEYBOARD,
    )


async def soccer_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _premium_gate(update, "PREMIUM"):
        return
    user_id = update.effective_user.id if update.effective_user else 0
    rows = await asyncio.to_thread(_soccer_visible_rows, user_id)
    rows = [r for r in rows if r["product"].upper() in {"TOP PICK", "CORE", "VALUE", "HYBRID"}]
    await update.effective_message.reply_text(
        _format_soccer_rows(rows, "⭐ TOP PICKS — FÚTBOL"),
        reply_markup=SOCCER_MENU_KEYBOARD,
    )


async def soccer_player_props(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _premium_gate(update, "PRO"):
        return
    user_id = update.effective_user.id if update.effective_user else 0
    rows = await asyncio.to_thread(_soccer_visible_rows, user_id, None, "PLAYER")
    await update.effective_message.reply_text(
        _format_soccer_rows(rows, "🎯 PLAYER PROPS — FÚTBOL"),
        reply_markup=SOCCER_MENU_KEYBOARD,
    )


async def soccer_leagues(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(_soccer_pick_rows)
    leagues = sorted({r["league"] for r in rows})
    text = "🏆 LIGAS — FÚTBOL\n\n" + ("\n".join(f"• {x}" for x in leagues) if leagues else "No hay ligas con picks publicados hoy.")
    await update.effective_message.reply_text(text, reply_markup=SOCCER_MENU_KEYBOARD)


async def soccer_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with _tracking_connection() as conn:
        rows = conn.execute(
            "SELECT pick_date, away, home, selection, product, result FROM soccer_picks "
            "WHERE result <> 'PENDING' ORDER BY pick_date DESC, slot LIMIT 30"
        ).fetchall()
    if not rows:
        text = "📊 RESULTADOS — FÚTBOL\n\nAún no hay resultados liquidados."
    else:
        lines = ["📊 RESULTADOS — FÚTBOL", ""]
        for r in rows:
            icon = "✅" if r["result"] == "WIN" else ("❌" if r["result"] == "LOSS" else "➖")
            lines.append(f"{icon} {r['pick_date']} | {r['selection']} | {r['away']} vs {r['home']}")
        text = "\n".join(lines)
    await update.effective_message.reply_text(text, reply_markup=SOCCER_MENU_KEYBOARD)


def _parse_soccer_kickoff(value):
    value = (value or "").strip()
    if not value or value.upper() in {"N/D", "ND", "TBD"}:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p"):
        try:
            local_dt = datetime.strptime(value, fmt).replace(tzinfo=LOCAL_TZ)
            return local_dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        raise ValueError("hora inválida")


def _save_soccer_picks(rows, created_by):
    now = local_now().isoformat()
    today = local_now().strftime("%Y-%m-%d")
    with _tracking_connection() as conn:
        conn.execute("DELETE FROM soccer_picks WHERE pick_date=?", (today,))
        for slot, item in enumerate(rows, start=1):
            conn.execute(
                """INSERT INTO soccer_picks
                (pick_date, slot, league, away, home, selection, product, plan_required,
                 kickoff_utc, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (today, slot, item["league"], item["away"], item["home"], item["selection"],
                 item["product"], item["plan_required"], item["kickoff_utc"], created_by, now, now),
            )


async def admin_soccer_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or int(user.id) not in SUBSCRIPTION_ADMIN_IDS:
        await update.effective_message.reply_text("⛔ Acceso exclusivo para administradores.")
        return
    rows = await asyncio.to_thread(_soccer_pick_rows)
    await update.effective_message.reply_text(
        _format_soccer_rows(rows, "⚽ ADMIN — PICKS FÚTBOL"),
        reply_markup=SOCCER_ADMIN_KEYBOARD,
    )


async def admin_soccer_start_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or int(user.id) not in SUBSCRIPTION_ADMIN_IDS:
        await update.effective_message.reply_text("⛔ Acceso exclusivo para administradores.")
        return
    context.user_data["awaiting_soccer_picks"] = True
    await update.effective_message.reply_text(
        "📝 CARGAR PICKS DE FÚTBOL\n\n"
        "Pega de 1 a 20 picks, uno por línea, usando:\n\n"
        "LIGA | VISITANTE vs LOCAL | SELECCIÓN | PRODUCTO | PLAN | HORA\n\n"
        "Ejemplo:\n"
        "LaLiga | Villarreal vs Real Madrid | Under 4.5 Goals | SURVIVAL | PREMIUM | 2026-09-24 15:00\n"
        "Premier League | Arsenal vs Everton | Over 7.5 Corners | CORE | PREMIUM | 2026-09-24 14:45\n"
        "LaLiga | Barcelona vs Sevilla | Lamine Yamal Over 1.5 SOT | PLAYER | PRO | 2026-09-24 16:00\n\n"
        "Productos: SURVIVAL, TOP PICK, CORE, VALUE, HYBRID, PLAYER.\n"
        "Planes: FREE, PREMIUM, PRO. Hora local del bot. Usa N/D si no está confirmada.",
        reply_markup=ReplyKeyboardMarkup([["❌ Cancelar carga fútbol"]], resize_keyboard=True, is_persistent=True),
    )


async def _handle_soccer_picks_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_soccer_picks"):
        return False
    user = update.effective_user
    if user is None or int(user.id) not in SUBSCRIPTION_ADMIN_IDS:
        context.user_data.pop("awaiting_soccer_picks", None)
        return False
    text = (update.message.text or "").strip()
    if text == "❌ Cancelar carga fútbol":
        context.user_data.pop("awaiting_soccer_picks", None)
        await update.effective_message.reply_text("Carga cancelada.", reply_markup=SOCCER_ADMIN_KEYBOARD)
        return True
    raw_lines = [x.strip() for x in text.splitlines() if x.strip()]
    if not (1 <= len(raw_lines) <= 20):
        await update.effective_message.reply_text("⚠️ Pega entre 1 y 20 picks, uno por línea.")
        return True
    parsed = []
    products = {"SURVIVAL", "TOP PICK", "CORE", "VALUE", "HYBRID", "PLAYER"}
    plans = {"FREE", "PREMIUM", "PRO"}
    try:
        for raw in raw_lines:
            parts = [x.strip() for x in raw.split("|")]
            if len(parts) != 6:
                raise ValueError("cada línea necesita 6 campos separados por |")
            league, matchup, selection, product, plan, kickoff = parts
            if " vs " not in matchup.lower():
                raise ValueError("el partido debe usar VISITANTE vs LOCAL")
            m = re.split(r"\s+vs\s+", matchup, maxsplit=1, flags=re.I)
            if len(m) != 2 or not m[0].strip() or not m[1].strip():
                raise ValueError("partido inválido")
            product = product.upper()
            plan = plan.upper()
            if product not in products:
                raise ValueError(f"producto no válido: {product}")
            if plan not in plans:
                raise ValueError(f"plan no válido: {plan}")
            parsed.append({
                "league": league,
                "away": m[0].strip(),
                "home": m[1].strip(),
                "selection": selection,
                "product": product,
                "plan_required": plan,
                "kickoff_utc": _parse_soccer_kickoff(kickoff),
            })
    except ValueError as exc:
        await update.effective_message.reply_text(f"⚠️ No pude cargar los picks: {exc}")
        return True
    await asyncio.to_thread(_save_soccer_picks, parsed, int(user.id))
    context.user_data.pop("awaiting_soccer_picks", None)
    await schedule_soccer_alert_jobs(context.application)
    rows = await asyncio.to_thread(_soccer_pick_rows)
    await update.effective_message.reply_text(
        "✅ Picks de fútbol publicados.\n\n" + _format_soccer_rows(rows, "⚽ PICKS ACTIVOS"),
        reply_markup=SOCCER_ADMIN_KEYBOARD,
    )
    return True


async def admin_soccer_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or int(user.id) not in SUBSCRIPTION_ADMIN_IDS:
        await update.effective_message.reply_text("⛔ Acceso exclusivo para administradores.")
        return
    today = local_now().strftime("%Y-%m-%d")
    with _tracking_connection() as conn:
        conn.execute("DELETE FROM soccer_picks WHERE pick_date=?", (today,))
    await update.effective_message.reply_text("🗑️ Picks de fútbol de hoy eliminados.", reply_markup=SOCCER_ADMIN_KEYBOARD)


def _soccer_alert_was_sent(chat_id, soccer_pick_id):
    with _tracking_connection() as conn:
        return conn.execute(
            "SELECT 1 FROM soccer_alert_deliveries WHERE chat_id=? AND soccer_pick_id=?",
            (chat_id, soccer_pick_id),
        ).fetchone() is not None


def _record_soccer_alert_delivery(chat_id, soccer_pick_id, scheduled_for):
    with _tracking_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO soccer_alert_deliveries(chat_id, soccer_pick_id, scheduled_for, sent_at) VALUES (?, ?, ?, ?)",
            (chat_id, soccer_pick_id, scheduled_for, local_now().isoformat()),
        )


async def _send_soccer_alert_job(context: ContextTypes.DEFAULT_TYPE):
    payload = context.job.data or {}
    row = payload.get("pick") or {}
    chat_id = int(payload.get("chat_id"))
    sub = await asyncio.to_thread(_get_alert_subscription, chat_id)
    if not sub.get("enabled"):
        return
    user_plan, _ = await asyncio.to_thread(_best_active_plan, chat_id)
    if SUBSCRIPTION_ENFORCE and _soccer_plan_rank(user_plan) < _soccer_plan_rank(row.get("plan_required")):
        return
    if await asyncio.to_thread(_soccer_alert_was_sent, chat_id, row.get("id")):
        return
    dt = _parse_iso_utc(row.get("kickoff_utc"))
    game_time = dt.astimezone(LOCAL_TZ).strftime("%I:%M %p").lstrip("0") if dt else "N/D"
    text = (
        f"⚽ TRIPLE PICK — FALTAN {sub['lead_minutes']} MIN\n\n"
        f"🏆 {row.get('league')}\n"
        f"🏟️ {row.get('away')} vs {row.get('home')}\n"
        f"🎯 {row.get('selection')}\n"
        f"🛡️ {row.get('product')} | 🔐 {row.get('plan_required')}\n"
        f"🕐 Inicio: {game_time} ({AUTO_TZ})\n\n"
        "Revisa alineaciones y cualquier cambio de última hora antes del inicio."
    )
    await context.bot.send_message(chat_id=chat_id, text=text)
    await asyncio.to_thread(_record_soccer_alert_delivery, chat_id, row.get("id"), payload.get("scheduled_for") or local_now().isoformat())


async def schedule_soccer_alert_jobs(application, pick_date=None):
    if application.job_queue is None:
        return {"scheduled": 0, "skipped": 0}
    rows = await asyncio.to_thread(_soccer_pick_rows, pick_date)
    chats = await asyncio.to_thread(_enabled_alert_chats)
    now_utc = datetime.now(timezone.utc)
    scheduled = skipped = 0
    for chat in chats:
        chat_id = int(chat["chat_id"])
        lead = int(chat["lead_minutes"] or ALERT_LEAD_MINUTES)
        user_plan, _ = await asyncio.to_thread(_best_active_plan, chat_id)
        for dbrow in rows:
            row = {k: dbrow[k] for k in dbrow.keys()}
            if SUBSCRIPTION_ENFORCE and _soccer_plan_rank(user_plan) < _soccer_plan_rank(row.get("plan_required")):
                skipped += 1
                continue
            dt = _parse_iso_utc(row.get("kickoff_utc"))
            if dt is None:
                skipped += 1
                continue
            alert_dt = dt - timedelta(minutes=lead)
            if alert_dt <= now_utc or await asyncio.to_thread(_soccer_alert_was_sent, chat_id, row["id"]):
                skipped += 1
                continue
            name = f"soccer_alert:{chat_id}:{row['id']}"
            for existing in application.job_queue.get_jobs_by_name(name):
                existing.schedule_removal()
            application.job_queue.run_once(
                _send_soccer_alert_job,
                when=alert_dt,
                data={"chat_id": chat_id, "pick": row, "scheduled_for": alert_dt.astimezone(LOCAL_TZ).isoformat()},
                name=name,
                chat_id=chat_id,
            )
            scheduled += 1
    return {"scheduled": scheduled, "skipped": skipped}


# ---------------------------------------------------------------------------
# v2.9.4 VISUAL MENU
# ---------------------------------------------------------------------------

MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["⚾ MLB", "⚽ Fútbol"],
        ["🔔 Alertas", "👤 Mi cuenta"],
        ["⭐ Suscripción"],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Selecciona un deporte o una opción",
)


def _main_menu_keyboard_for(user_id=None):
    rows = [
        ["⚾ MLB", "⚽ Fútbol"],
        ["🔔 Alertas", "👤 Mi cuenta"],
        ["⭐ Suscripción"],
    ]
    if user_id is not None and int(user_id) in SUBSCRIPTION_ADMIN_IDS:
        rows.append(["🛡️ Panel Admin"])
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Selecciona un deporte o una opción",
    )

MORE_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🔎 Candidate Pool", "🟣 Value Board"],
        ["💵 Estado mercado", "🧪 Diagnóstico odds"],
        ["🧾 Liquidar picks", "🎯 Calibración"],
        ["🆔 Mi ID", "⬅️ Menú principal"],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Herramientas avanzadas Triple Pick",
)


def _menu_text():
    return (
        f"🏆 TRIPLE PICK v{BOT_VERSION} — MULTI-SPORT\n\n"
        "Selecciona tu deporte. No necesitas escribir comandos.\n\n"
        "⚾ MLB — motor, mercado, tracking y picks MLB\n"
        "⚽ Fútbol — picks aprobados, Survival, Top Picks y Player Props\n"
        "🔔 Alertas — avisos pregame\n"
        "👤 Mi cuenta — estado de membresía\n"
        "⭐ Suscripción — FREE, PREMIUM y PRO"
    )

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_menu_text(), reply_markup=_main_menu_keyboard_for(update.effective_user.id if update.effective_user else None))


async def menu_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🧪 HERRAMIENTAS AVANZADAS\n\n"
        "Selecciona una herramienta. No necesitas escribir comandos.\n\n"
        "🔎 Candidate Pool — auditoría completa del slate\n"
        "🟣 Value Board — candidatos de valor\n"
        "💵 Estado mercado — estado de cuotas/API\n"
        "🧪 Diagnóstico odds — revisión de bookmakers\n"
        "🧾 Liquidar picks — actualizar resultados\n"
        "🎯 Calibración — predicted vs actual\n"
        "🆔 Mi ID — identificador del chat",
        reply_markup=MORE_MENU_KEYBOARD,
    )


async def visual_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route visual keyboard labels to the existing command functions."""
    if await _handle_official_picks_input(update, context):
        return
    if await _handle_soccer_picks_input(update, context):
        return
    text = (update.message.text or "").strip()
    routes = {
        "⚾ MLB": mlb_menu,
        "⚽ Fútbol": soccer_menu,
        "🔥 Picks Fútbol": soccer_picks,
        "🛡️ Survival Fútbol": soccer_survival,
        "⭐ Top Picks Fútbol": soccer_top,
        "🎯 Player Props Fútbol": soccer_player_props,
        "🏆 Ligas Fútbol": soccer_leagues,
        "📊 Resultados Fútbol": soccer_results,
        "⚾ Picks de hoy": picks,
        "📊 Estado": trackstatus,
        "📈 Rendimiento": performance,
        "🧮 Mercado": market,
        "📋 Historial": history,
        "🔔 Alertas": alerts_menu,
        "👤 Mi cuenta": account_command,
        "⭐ Suscripción": subscription_command,
        "✅ Activar alertas": alerts_enable,
        "⛔ Desactivar alertas": alerts_disable,
        "📋 Próximas alertas": alerts_upcoming,
        "📱 SMS": sms_menu,
        "✅ Activar SMS": sms_enable,
        "⛔ Desactivar SMS": sms_disable,
        "📱 Estado SMS": sms_status,
        "⬅️ Alertas": alerts_menu,
        "⬅️ Menú principal": menu,
        "⚾ Juegos MLB": mlb,
        "🧪 Más opciones": menu_more,
        "🔎 Candidate Pool": pool,
        "🟣 Value Board": value,
        "💵 Estado mercado": oddsstatus,
        "🧪 Diagnóstico odds": oddsdebug,
        "🧾 Liquidar picks": settle,
        "🎯 Calibración": calibration,
        "🆔 Mi ID": myid,
        "🛡️ Panel Admin": admin_panel,
        "🎯 Picks oficiales": admin_official_picks_panel,
        "⚽ Admin Fútbol": admin_soccer_panel,
        "📋 Ver picks fútbol": admin_soccer_panel,
        "📝 Cargar picks fútbol": admin_soccer_start_input,
        "🗑️ Borrar picks fútbol": admin_soccer_clear,
        "📋 Ver picks oficiales": admin_official_picks_panel,
        "📝 Cargar 3 picks": admin_official_start_input,
        "🤖 Usar picks del modelo": admin_official_use_model,
        "🗑️ Borrar picks oficiales": admin_official_clear,
        "⬅️ Panel Admin": admin_panel,
        "📊 Estadísticas": adminstats_command,
        "👥 Usuarios": adminusers_command,
        "⏳ Vencen pronto": admin_expiring_command,
        "💳 Suscripciones": admin_subscriptions_command,
    }
    handler = routes.get(text)
    if handler is not None:
        await handler(update, context)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    plan, row = await asyncio.to_thread(_best_active_plan, update.effective_user.id)
    if plan == "NONE":
        membership = "⚪ Aún no tienes suscripción. Pulsa ⭐ Suscripción para activar FREE por 30 días."
    elif plan == "FREE":
        membership = (
            f"🆓 FREE activo — {_remaining_trial_text(row['trial_expires_at'])} restantes.\n"
            "Después: ⭐ PREMIUM $10/mes | 🔥 PRO $20/mes"
        )
    elif plan == "EXPIRED":
        membership = "🔴 Tu prueba FREE finalizó. Usa ⭐ Suscripción para continuar."
    else:
        membership = f"💎 Membresía activa: {plan}"

    await update.message.reply_text(
        _menu_text() + "\n\n" + membership,
        reply_markup=_main_menu_keyboard_for(update.effective_user.id if update.effective_user else None),
    )


async def picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triple Pick v2.9.1: market-aware selection plus immutable recommendation tracking."""
    if not await _premium_gate(update, "PREMIUM"):
        return
    now = local_now()
    fecha = now.strftime("%Y-%m-%d")
    season = now.year

    official_rows = await asyncio.to_thread(_official_pick_rows, fecha)
    if official_rows:
        mensaje = _format_official_picks_text(
            official_rows,
            title=f"🔥 TRIPLE PICK OFICIAL — {fecha}"
        )
        mensaje += "\n\n📺 Estos picks son exactamente los confirmados para el canal Triple Pick."
        try:
            alert_info = await schedule_alert_jobs(context.application, fecha)
            if alert_info.get("scheduled"):
                mensaje += f"\n🔔 Alertas 45 min: {alert_info['scheduled']} programada(s)."
        except Exception as exc:
            mensaje += f"\n⚠️ No pude programar alertas: {exc}"
        await _reply_long(update.message, mensaje)
        return

    await update.message.reply_text(
        "🔥 TRIPLE PICK OFICIAL — HOY\n\n"
        "⏳ Los picks oficiales todavía no han sido publicados.\n\n"
        "Vuelve a consultar más tarde.",
        reply_markup=_main_menu_keyboard_for(update.effective_user.id if update.effective_user else None),
    )
    return

    status, partidos, odds_status = await asyncio.to_thread(
        build_daily_matchups_v28, fecha, season
    )
    if status == "api_error":
        await update.message.reply_text("❌ No pude obtener los datos actuales de MLB.")
        return
    if status == "no_games":
        await update.message.reply_text("⚾ No hay juegos de MLB para hoy.")
        return

    triple_pick, mode = _select_triple_pick_v28(partidos, odds_status)
    base_eligible = sum(bool(p.get("eligible")) for p in partidos)
    survival_count = sum(bool(p.get("survival_approved")) for p in partidos)
    value_count = sum(bool(p.get("value_approved")) for p in partidos)

    mensaje = (
        "🔥 MLB TRIPLE PICK v2.9.5 — MARKET + TRACKING\n"
        f"📅 {fecha} — {AUTO_TZ}\n\n"
        "🧠 MODEL: v2.7.1 starter sample + recency + offense + form + bullpen proxy.\n"
        "💵 MARKET: Moneyline → implied probability → no-vig → model/market agreement.\n"
        "⚠️ Model Probability = PROXY no calibrado; se usa para ranking/comparación, no como certeza real.\n\n"
        f"📋 Juegos: {len(partidos)} | ✅ Model eligible: {base_eligible} | "
        f"🔵 Survival: {survival_count} | 🟣 Value: {value_count}\n"
    )

    if mode == "model_fallback":
        reason = (
            "ODDS_API_KEY no configurada"
            if odds_status == "not_configured"
            else "odds temporalmente no disponibles"
        )
        mensaje += (
            f"⚠️ MARKET ENGINE EN FALLBACK: {reason}.\n"
            "Los picks siguientes son del modelo v2.7.1 y NO han pasado Market Gate.\n\n"
        )
    else:
        mensaje += (
            f"🏦 Primary book: {ODDS_PRIMARY_BOOKMAKER}\n"
            "🏆 Triple Pick final usa solo HYBRID/SURVIVAL; VALUE puro vive en /value.\n\n"
        )
        mensaje += _primary_feed_notice(partidos, odds_status)

    if not triple_pick:
        mensaje += (
            "🚫 HOY NO HAY TRIPLE PICK APROBADO\n"
            "El sistema no completará tres selecciones por obligación.\n\n"
        )
        for p in partidos[:3]:
            mensaje += (
                f"• {p['favorite']} — {p.get('market_grade', 'N/D')} | "
                f"Model {_pct(p.get('model_probability'))} | "
                f"Market {_pct(p.get('market_probability'))}\n"
            )
    else:
        medallas = ["🥇", "🥈", "🥉"]
        for i, p in enumerate(triple_pick):
            mensaje += (
                f"{medallas[i]} PICK #{i + 1}\n"
                f"🏟️ {p['away']} vs {p['home']}\n"
                f"🎯 {p['favorite']} ML\n"
                f"📈 Matchup {p['score']:.1f}/100 | C {p['confidence']:.0f}/100 | "
                f"DR {p['data_reliability']:.0f}/100\n"
                f"🧮 Model Probability Proxy: {_pct(p.get('model_probability'))}\n"
            )
            if mode == "market":
                mensaje += (
                    f"💵 Hard Rock: {_format_american(p.get('hardrock_price'))} | "
                    f"HR no-vig {_pct(p.get('hardrock_no_vig'))}\n"
                    f"🌐 Consensus no-vig: {_pct(p.get('consensus_no_vig'))} | "
                    f"Gap {p.get('value_gap', 0) * 100:+.1f} pp\n"
                    f"🤝 {p.get('market_agreement')} | {p.get('market_grade')}\n"
                )
            else:
                mensaje += f"🛡️ {p['risk']} | Gate modelo: {p['gate_reason']}\n"
            mensaje += "\n"

    if triple_pick:
        try:
            track_info = await asyncio.to_thread(
                track_final_picks, triple_pick, fecha, mode
            )
            if mode == "market":
                mensaje += (
                    f"🧾 Tracking v2.9.5: {track_info['inserted']} nuevo(s), "
                    f"{track_info['existing']} ya registrado(s). "
                    "SURVIVAL/HYBRID = muestra oficial.\n"
                )
            else:
                mensaje += (
                    f"🧾 Tracking v2.9.5: {track_info['inserted']} fallback nuevo(s). "
                    "Se guardan para auditoría, fuera de métricas oficiales.\n"
                )
        except Exception as exc:
            mensaje += f"⚠️ Tracking no pudo guardar este snapshot: {exc}\n"

        try:
            alert_info = await schedule_alert_jobs(context.application, fecha)
            if alert_info.get("scheduled"):
                mensaje += f"🔔 Alertas 45 min: {alert_info['scheduled']} programada(s).\n"
        except Exception as exc:
            mensaje += f"⚠️ No pude programar alertas: {exc}\n"

    mensaje += (
        "ℹ️ SURVIVAL no exige +EV estricto; VALUE sí exige un gap positivo mínimo y "
        "precio Hard Rock disponible. Divergencias grandes se envían a REVIEW."
    )
    await _reply_long(update.message, mensaje)


async def pool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """v2.8 Candidate Pool with model and market audit fields."""
    now = local_now()
    fecha = now.strftime("%Y-%m-%d")
    season = now.year
    status, partidos, odds_status = await asyncio.to_thread(
        build_daily_matchups_v28, fecha, season
    )
    if status == "api_error":
        await update.message.reply_text("❌ No pude obtener los datos actuales de MLB.")
        return
    if status == "no_games":
        await update.message.reply_text("⚾ No hay juegos de MLB para hoy.")
        return

    mensaje = (
        "🔎 MLB CANDIDATE POOL — v2.9.5\n"
        f"📅 {fecha}\n"
        f"Market: {'ON' if odds_status == 'ok' else 'OFF/FALLBACK'} | "
        f"Primary: {ODDS_PRIMARY_BOOKMAKER}\n\n"
        "C=Confidence | DR=Data Reliability | MP=Model Probability Proxy | "
        "MKT=consensus no-vig | VG=value gap.\n\n"
    )
    mensaje += _primary_feed_notice(partidos, odds_status)
    for i, p in enumerate(partidos, 1):
        icon = "✅" if p.get("eligible") else "❌"
        vg = p.get("value_gap")
        vg_text = "N/D" if vg is None else f"{vg * 100:+.1f}pp"
        mensaje += (
            f"{i}. {icon} {p['away']} vs {p['home']}\n"
            f"🎯 {p['favorite']} | {p['risk']} | {p.get('market_grade')}\n"
            f"📈 C {p['confidence']:.0f} | DR {p['data_reliability']:.0f} | "
            f"Δ {p['difference']:.1f} | SR {p['min_starter_reliability']:.0f}\n"
            f"🧮 MP {_pct(p.get('model_probability'))} | MKT {_pct(p.get('market_probability'))} | VG {vg_text}\n"
            f"🛡️ Model Gate: {p['gate_reason']}\n\n"
        )
    await _reply_long(update.message, mensaje)


async def market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Audit Hard Rock/consensus moneyline against the model for every game."""
    now = local_now()
    fecha = now.strftime("%Y-%m-%d")
    season = now.year
    status, partidos, odds_status = await asyncio.to_thread(
        build_daily_matchups_v28, fecha, season
    )
    if status != "ok":
        await update.message.reply_text("❌ No pude construir el slate de mercado de hoy.")
        return
    if odds_status == "not_configured":
        await update.message.reply_text(
            "⚪ MARKET ENGINE OFF\n"
            "Falta ODDS_API_KEY / THE_ODDS_API_KEY. El modelo MLB sigue funcionando, "
            "pero no puedo calcular precio, no-vig, agreement ni value sin una fuente de odds."
        )
        return
    if odds_status != "ok":
        await update.message.reply_text(
            "⚠️ Market Engine temporalmente no disponible.\n"
            f"Detalle: {_ODDS_LAST_META.get('error') or 'sin detalle'}"
        )
        return

    mensaje = (
        "💵 MLB MARKET AUDIT — v2.9.5\n"
        f"📅 {fecha}\n"
        f"Primary: {ODDS_PRIMARY_BOOKMAKER} | Books: {ODDS_BOOKMAKERS}\n""⏱️ PREMATCH ONLY: juegos iniciados/live se excluyen del Market Engine.\n\n"
    )
    mensaje += _primary_feed_notice(partidos, odds_status)
    for i, p in enumerate(partidos, 1):
        vg = p.get("value_gap")
        vg_text = "N/D" if vg is None else f"{vg * 100:+.1f} pp"
        mensaje += (
            f"{i}. {p['away']} vs {p['home']}\n"
            f"🎯 Model: {p['favorite']} | MP {_pct(p.get('model_probability'))}\n"
            f"💵 Hard Rock {_format_american(p.get('hardrock_price'))} | "
            f"HR no-vig {_pct(p.get('hardrock_no_vig'))}\n"
            f"🌐 Consensus {_pct(p.get('consensus_no_vig'))} | Gap {vg_text}\n"
            f"🤝 {p.get('market_agreement')} | {p.get('market_grade')}\n\n"
        )
    await _reply_long(update.message, mensaje)


async def value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show only Hard Rock value/hybrid candidates that pass the model gate."""
    if not await _premium_gate(update, "PRO"):
        return
    now = local_now()
    fecha = now.strftime("%Y-%m-%d")
    season = now.year
    status, partidos, odds_status = await asyncio.to_thread(
        build_daily_matchups_v28, fecha, season
    )
    if status != "ok":
        await update.message.reply_text("❌ No pude construir el Candidate Pool de hoy.")
        return
    if odds_status != "ok":
        await update.message.reply_text(
            "⚪ /value necesita el Market Engine activo. Configura ODDS_API_KEY primero."
        )
        return

    primary_notice = _primary_feed_notice(partidos, odds_status)
    if primary_notice:
        await update.message.reply_text(
            "🟣 MLB VALUE BOARD — v2.9.5\n"
            f"📅 {fecha}\n\n"
            + primary_notice
            + "VALUE requiere un precio accionable de Hard Rock; no se sustituye por consenso."
        )
        return

    candidatos = [p for p in partidos if p.get("value_approved")]
    candidatos.sort(
        key=lambda p: (
            p.get("value_gap") or -1,
            p.get("model_probability") or 0,
            p.get("confidence") or 0,
        ),
        reverse=True,
    )

    mensaje = (
        "🟣 MLB VALUE BOARD — v2.9.5\n"
        f"📅 {fecha}\n"
        "Regla: Model Gate aprobado + precio Hard Rock + gap ≥3.5 pp; "
        "divergencias >10 pp se mandan a REVIEW, no a VALUE automático.\n\n"
    )
    if not candidatos:
        mensaje += "🚫 No hay VALUE/HYBRID aprobado en este momento.\n"
    else:
        for i, p in enumerate(candidatos, 1):
            mensaje += (
                f"{i}. {p.get('market_grade')} — {p['favorite']} ML\n"
                f"🏟️ {p['away']} vs {p['home']}\n"
                f"💵 Hard Rock {_format_american(p.get('hardrock_price'))}\n"
                f"🧮 Model {_pct(p.get('model_probability'))} | "
                f"HR no-vig {_pct(p.get('hardrock_no_vig'))} | "
                f"Gap {p['value_gap'] * 100:+.1f} pp\n"
                f"🧪 C {p['confidence']:.0f} | DR {p['data_reliability']:.0f}\n\n"
            )
    mensaje += "⚠️ Model Probability sigue siendo proxy no calibrado hasta completar tracking/backtest."
    await _reply_long(update.message, mensaje)


async def oddsstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    configured = bool(ODDS_API_KEY)
    await update.message.reply_text(
        "💵 MARKET ENGINE STATUS\n"
        f"API key: {'CONFIGURADA' if configured else 'NO CONFIGURADA'}\n"
        f"Primary bookmaker: {ODDS_PRIMARY_BOOKMAKER}\n"
        f"Bookmakers: {ODDS_BOOKMAKERS}\n"
        f"Cache: {ODDS_CACHE_TTL}s\n"
        f"Quota remaining: {_ODDS_LAST_META.get('remaining') or 'N/D'}\n"
        f"Last request cost: {_ODDS_LAST_META.get('last') or 'N/D'}\n"
        f"Last error: {_ODDS_LAST_META.get('error') or 'ninguno'}"
    )


async def oddsdebug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Diagnóstico seguro de bookmakers recibidos desde The Odds API.

    No muestra la API key ni modifica gates/scoring. Sirve para verificar si
    hardrockbet_fl está realmente presente en la respuesta cruda por evento.
    """
    fecha = local_now().strftime("%Y-%m-%d")
    odds_status, events = await asyncio.to_thread(get_mlb_moneyline_odds, fecha)

    if odds_status == "not_configured":
        await update.message.reply_text(
            "🧪 ODDS DEBUG — v2.9.5\n"
            "❌ ODDS_API_KEY no configurada."
        )
        return
    if odds_status != "ok":
        await update.message.reply_text(
            "🧪 ODDS DEBUG — v2.9.5\n"
            f"❌ Odds API status: {odds_status}\n"
            f"Last error: {_ODDS_LAST_META.get('error') or 'N/D'}"
        )
        return

    requested = [b.strip() for b in ODDS_BOOKMAKERS.split(",") if b.strip()]
    all_keys = sorted({
        str(book.get("key"))
        for event in events
        for book in event.get("bookmakers", [])
        if book.get("key")
    })
    primary_anywhere = ODDS_PRIMARY_BOOKMAKER in all_keys

    msg = (
        "🧪 ODDS DEBUG — v2.9.5\n"
        f"📅 {fecha} — {AUTO_TZ}\n"
        f"🎯 Primary esperado: {ODDS_PRIMARY_BOOKMAKER}\n"
        f"📨 Solicitados: {', '.join(requested) or 'N/D'}\n"
        f"📥 Recibidos globalmente: {', '.join(all_keys) or 'NINGUNO'}\n"
        f"🏦 Primary presente en algún evento: {'SÍ' if primary_anywhere else 'NO'}\n"
        f"📦 Eventos API: {len(events)} | Quota restante: {_ODDS_LAST_META.get('remaining') or 'N/D'}\n\n"
    )

    shown = 0
    for event in events:
        if not _event_is_local_date(event, fecha):
            continue
        # Se muestra también si ya inició para diagnosticar la respuesta cruda,
        # pero se etiqueta LIVE/STARTED; /market sigue siendo PREMATCH ONLY.
        pregame = _event_is_pregame(event)
        keys = [
            str(book.get("key"))
            for book in event.get("bookmakers", [])
            if book.get("key")
        ]
        titles = [
            str(book.get("title") or book.get("key"))
            for book in event.get("bookmakers", [])
            if book.get("key")
        ]
        has_primary = ODDS_PRIMARY_BOOKMAKER in keys
        away = event.get("away_team") or "Away"
        home = event.get("home_team") or "Home"
        state = "PREMATCH" if pregame else "STARTED/LIVE"
        msg += (
            f"{shown + 1}. {away} vs {home}\n"
            f"   ⏱️ {state} | Primary: {'✅' if has_primary else '❌'}\n"
            f"   🔑 Keys: {', '.join(keys) or 'NINGUNA'}\n"
            f"   🏷️ Books: {', '.join(titles) or 'NINGUNO'}\n\n"
        )
        shown += 1
        if shown >= 20:
            break

    if shown == 0:
        msg += "No se encontraron eventos de la fecha local en la respuesta.\n"

    msg += (
        "ℹ️ Este comando es diagnóstico solamente: no altera picks, gates, "
        "tracking ni calibración."
    )
    await _reply_long(update.message, msg)


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el chat ID para configurarlo en Railway."""
    await update.message.reply_text(
        f"🆔 Chat ID: {update.effective_chat.id}"
    )


async def autostatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    estado = "ACTIVO" if AUTO_CHAT_ID else "PENDIENTE DE CONFIGURACIÓN"
    await update.message.reply_text(
        "🤖 AUTO-PICKS\n"
        f"Estado: {estado}\n"
        f"Hora: {AUTO_HOUR:02d}:{AUTO_MINUTE:02d}\n"
        f"Zona: {AUTO_TZ}\n"
        f"Chat destino: {AUTO_CHAT_ID or 'no configurado'}"
    )


async def enviar_picks_automaticos(context: ContextTypes.DEFAULT_TYPE):
    """Envía al chat configurado el mismo análisis que /picks."""
    if not AUTO_CHAT_ID:
        print("⚠️ AUTO_CHAT_ID no configurado; se omite el envío automático.")
        return

    class _Message:
        async def reply_text(self, message):
            await context.bot.send_message(chat_id=int(AUTO_CHAT_ID), text=message)

    class _Update:
        message = _Message()

    try:
        await picks(_Update(), context)
        print("✅ Auto-picks enviados correctamente.")
    except Exception as exc:
        print(f"❌ Error en auto-picks: {exc}")


async def post_init_schedule_alerts(application):
    """Rebuild future alert jobs after a Railway restart/deploy."""
    try:
        info = await schedule_alert_jobs(application)
        soccer_info = await schedule_soccer_alert_jobs(application)
        print(f"🔔 Alertas MLB restauradas al iniciar: {info['scheduled']} programada(s).")
        print(f"⚽ Alertas fútbol restauradas al iniciar: {soccer_info['scheduled']} programada(s).")
    except Exception as exc:
        print(f"⚠️ No pude restaurar alertas al iniciar: {exc}")


def main():
    if not TOKEN:
        raise RuntimeError(
            "Falta TELEGRAM_BOT_TOKEN. Configura el nuevo token de BotFather "
            "como variable de entorno antes de iniciar el bot."
        )

    init_tracking_db()
    init_membership_db()
    migrate_legacy_free_trials_to_30_days()
    app = ApplicationBuilder().token(TOKEN).post_init(post_init_schedule_alerts).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("subscribe", subscription_command))
    app.add_handler(CommandHandler("account", account_command))
    app.add_handler(CommandHandler("adminstats", adminstats_command))
    app.add_handler(CommandHandler("adminusers", adminusers_command))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("adminexpiring", admin_expiring_command))
    app.add_handler(CommandHandler("adminsubs", admin_subscriptions_command))
    app.add_handler(CommandHandler("official", admin_official_picks_panel))
    app.add_handler(CallbackQueryHandler(membership_callback, pattern=r"^tp_"))
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    app.add_handler(CommandHandler("mlb", mlb))
    app.add_handler(CommandHandler("soccer", soccer_menu))
    app.add_handler(CommandHandler("soccerpicks", soccer_picks))
    app.add_handler(CommandHandler("socceradmin", admin_soccer_panel))
    app.add_handler(CommandHandler("picks", picks))
    app.add_handler(CommandHandler("pool", pool))
    app.add_handler(CommandHandler("market", market))
    app.add_handler(CommandHandler("value", value))
    app.add_handler(CommandHandler("oddsstatus", oddsstatus))
    app.add_handler(CommandHandler("oddsdebug", oddsdebug))
    app.add_handler(CommandHandler("trackstatus", trackstatus))
    app.add_handler(CommandHandler("settle", settle))
    app.add_handler(CommandHandler("performance", performance))
    app.add_handler(CommandHandler("calibration", calibration))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("autostatus", autostatus))
    app.add_handler(CommandHandler("alerts", alerts_menu))
    app.add_handler(CommandHandler("alertson", alerts_enable))
    app.add_handler(CommandHandler("alertsoff", alerts_disable))
    app.add_handler(CommandHandler("sms", sms_menu))
    app.add_handler(CommandHandler("smsset", sms_set))
    app.add_handler(CommandHandler("smson", sms_enable))
    app.add_handler(CommandHandler("smsoff", sms_disable))
    app.add_handler(CallbackQueryHandler(alert_game_callback, pattern=r"^alertgame\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, visual_menu_router))

    if app.job_queue is not None:
        tz = LOCAL_TZ
        daily_time = __import__("datetime").time(
            hour=AUTO_HOUR,
            minute=AUTO_MINUTE,
            tzinfo=tz
        )
        app.job_queue.run_daily(
            enviar_picks_automaticos,
            time=daily_time,
            name="triple_pick_daily"
        )
        print(
            f"⏰ Auto-picks programado diariamente a "
            f"{AUTO_HOUR:02d}:{AUTO_MINUTE:02d} ({AUTO_TZ})"
        )

        if TRACK_AUTO_SETTLE:
            settle_time = __import__("datetime").time(
                hour=TRACK_SETTLE_HOUR,
                minute=TRACK_SETTLE_MINUTE,
                tzinfo=tz,
            )
            app.job_queue.run_daily(
                auto_settle_tracking,
                time=settle_time,
                name="triple_pick_tracking_settle",
            )
            print(
                f"🧾 Auto-settle programado diariamente a "
                f"{TRACK_SETTLE_HOUR:02d}:{TRACK_SETTLE_MINUTE:02d} ({AUTO_TZ})"
            )
    else:
        print(
            "⚠️ JobQueue no disponible. Usa "
            "python-telegram-bot[job-queue] en requirements.txt."
        )

    print(
        "📱 Twilio SMS: " + ("CONFIGURADO" if _twilio_configured() else "NO CONFIGURADO")
    )
    print(f"🤖 Triple Pick Multi-Sport v{BOT_VERSION} iniciado...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
