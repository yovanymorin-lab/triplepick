from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import asyncio
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests


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

# MLB Triple Pick v2.8 - Hard Rock market engine + no-vig + model/market gates
#
# v2.7.1 preserves the v2.7 starter sample/recency engine and the 45/25/15/10/5
# matchup structure. It makes 100/100 Data Reliability unavailable until both
# lineups are published, calibrates Confidence 2.1 more conservatively, and adds
# /pool to audit every game, gate, and rejection reason in the slate.


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "¡Hola! Soy tu bot MLB ⚾\n\n"
        "Comandos disponibles:\n"
        "/mlb - Ver los juegos de hoy\n"
        "/picks - Ver Triple Pick v2.7.1 de hoy\n"
        "/pool - Auditar el Candidate Pool completo\n"
        "/myid - Ver el ID de este chat\n"
        "/autostatus - Ver estado del envío automático\n\n"
        "⏰ Auto-picks se configura en Railway."
    )


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


def _find_odds_event(partido, events, fecha):
    away_key = _normalize_team_name(partido.get("away"))
    home_key = _normalize_team_name(partido.get("home"))
    candidates = []
    for event in events:
        if not _event_is_local_date(event, fecha):
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


async def picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Triple Pick v2.7.1.

    Key changes:
    - Starter season sample is explicitly scored for reliability.
    - Last 5 starts are blended into the starter block when MLB game logs exist.
    - Small samples are shrunk toward neutral and confidence is capped.
    - Data Reliability and Starter Reliability are separate from matchup strength.
    - Candidate Pool, eligible candidates and final Triple Pick are reported separately.
    - v2.7.1 caps Data Reliability until lineups are published and calibrates Confidence 2.1.
    """
    now = local_now()
    fecha = now.strftime("%Y-%m-%d")
    season = now.year

    status, partidos = await asyncio.to_thread(
        build_daily_matchups, fecha, season
    )

    if status == "api_error":
        await update.message.reply_text(
            "❌ No pude obtener los datos actuales de MLB."
        )
        return

    if status == "no_games":
        await update.message.reply_text("⚾ No hay juegos de MLB para hoy.")
        return

    elegibles = [p for p in partidos if p["eligible"]]
    triple_pick = elegibles[:3]

    fuertes = sum(p["risk"] == "🟢 PICK FUERTE" for p in partidos)
    moderados = sum(p["risk"] == "🟡 PICK MODERADO" for p in partidos)
    riesgos = sum(p["risk"] == "🟠 PICK DE RIESGO" for p in partidos)
    no_bet = len(partidos) - fuertes - moderados - riesgos

    mensaje = (
        "🔥 MLB TRIPLE PICK v2.7.1\n"
        f"📅 {fecha} — {AUTO_TZ}\n\n"
        "📊 Matchup: abridor 45% + ofensiva 25% + forma 15% + "
        "bullpen proxy 10% + localía 5%\n"
        "⚾ Abridor v2.7.1: temporada + últimas 5 aperturas (hasta 35%) + "
        "shrinkage por tamaño de muestra.\n"
        "🧪 Confidence Score 2.1 = señal heurística; NO es probabilidad calibrada.\n"
        "🛡️ Gate: abridores utilizables + ≥6/8 bloques + Data Reliability ≥75 + "
        "Confidence ≥76 + ventaja ≥5.\n"
        "📝 Reliability 100/100 requiere ambas alineaciones publicadas.\n\n"
        f"📋 Candidate Pool: {len(partidos)} juegos | "
        f"🟢 {fuertes} | 🟡 {moderados} | 🟠 {riesgos} | 🔴 {no_bet}\n"
        f"✅ Candidatos elegibles: {len(elegibles)}\n"
        f"🏆 TRIPLE PICK FINAL: {len(triple_pick)}\n\n"
    )

    if not triple_pick:
        mensaje += (
            "🚫 HOY NO HAY TRIPLE PICK APROBADO\n"
            "Ningún juego supera todos los gates de v2.7.1.\n\n"
            "🔎 Mejores señales observadas (NO son picks aprobados):\n"
        )
        for partido in partidos[:3]:
            mensaje += (
                f"• {partido['favorite']} — C {partido['confidence']:.0f}/100 | "
                f"DR {partido['data_reliability']:.0f}/100 | "
                f"Δ {partido['difference']:.1f} | {partido['risk']} | "
                f"Gate: {partido['gate_reason']}\n"
            )
    else:
        if len(triple_pick) < 3:
            mensaje += (
                f"⚠️ Solo {len(triple_pick)} pick(s) superan todos los filtros; "
                "el bot NO completará tres por obligación.\n\n"
            )

        medallas = ["🥇", "🥈", "🥉"]
        for i, partido in enumerate(triple_pick):
            mensaje += (
                f"{medallas[i]} PICK #{i + 1}\n"
                f"🏟️ {partido['away']} vs {partido['home']}\n"
                f"🎯 Selección: {partido['favorite']}\n"
                f"📈 Matchup Score: {partido['score']:.1f}/100\n"
                f"🧪 Confidence Score 2.1: {partido['confidence']:.0f}/100\n"
                f"📚 Data Reliability: {partido['data_reliability']:.0f}/100\n"
                f"📝 Lineup Reliability: {_lineup_coverage_state(partido['lineup_status'])[0]}\n"
                f"↔️ Ventaja relativa: {partido['difference']:.1f}\n"
                f"{partido['risk']}\n"
                f"📦 Bloques base: {partido['data_count']}/8\n"
                f"⚾ {partido['away']}: {partido['away_pitcher']} — "
                f"SR {partido['away_starter_reliability']:.0f}/100 "
                f"({starter_sample_label(partido['away_pitching'])})\n"
                f"⚾ {partido['home']}: {partido['home_pitcher']} — "
                f"SR {partido['home_starter_reliability']:.0f}/100 "
                f"({starter_sample_label(partido['home_pitching'])})\n"
            )

            if partido["small_sample"]:
                mensaje += (
                    "⚠️ STARTER SMALL SAMPLE: v2.7.1 limita la confianza y este juego "
                    "no puede subir a PICK FUERTE por muestra insuficiente.\n"
                )

            lineup = partido["lineup_status"]
            if lineup and lineup["published"]:
                mensaje += (
                    f"📝 Alineaciones publicadas — "
                    f"{partido['away']}: {lineup['away_count']} | "
                    f"{partido['home']}: {lineup['home_count']}\n"
                )
            else:
                mensaje += "📝 Alineaciones: todavía no publicadas/disponibles\n"

            if partido["away_recent_pitcher"] or partido["home_recent_pitcher"]:
                recent_parts = []
                if partido["away_recent_pitcher"]:
                    rp = partido["away_recent_pitcher"]
                    recent_parts.append(
                        f"{partido['away_pitcher']}: {rp['starts']} GS, "
                        f"ERA {rp['era']:.2f}, WHIP {rp['whip']:.2f}"
                    )
                if partido["home_recent_pitcher"]:
                    rp = partido["home_recent_pitcher"]
                    recent_parts.append(
                        f"{partido['home_pitcher']}: {rp['starts']} GS, "
                        f"ERA {rp['era']:.2f}, WHIP {rp['whip']:.2f}"
                    )
                mensaje += "🕔 Abridor reciente — " + " | ".join(recent_parts) + "\n"

            if partido["away_form"] and partido["home_form"]:
                mensaje += (
                    f"🔥 Forma 14 días — "
                    f"{partido['away']}: "
                    f"{partido['away_form']['win_rate'] * 100:.0f}% W, "
                    f"{partido['away_form']['run_diff']:+.1f} RD | "
                    f"{partido['home']}: "
                    f"{partido['home_form']['win_rate'] * 100:.0f}% W, "
                    f"{partido['home_form']['run_diff']:+.1f} RD\n"
                )

            if partido["away_bullpen"] and partido["home_bullpen"]:
                mensaje += (
                    f"🧱 Bullpen proxy — ERA "
                    f"{partido['away_bullpen']['era']:.2f} vs "
                    f"{partido['home_bullpen']['era']:.2f} | WHIP "
                    f"{partido['away_bullpen']['whip']:.2f} vs "
                    f"{partido['home_bullpen']['whip']:.2f}\n"
                )

            if partido["away_pitching"] and partido["home_pitching"]:
                mensaje += (
                    f"⚾ Abridores temporada — ERA "
                    f"{partido['away_pitching']['era']:.2f} vs "
                    f"{partido['home_pitching']['era']:.2f} | WHIP "
                    f"{partido['away_pitching']['whip']:.2f} vs "
                    f"{partido['home_pitching']['whip']:.2f} | IP "
                    f"{format_baseball_innings(partido['away_pitching']['innings'])} vs "
                    f"{format_baseball_innings(partido['home_pitching']['innings'])}\n"
                )

            if partido["away_hitting"] and partido["home_hitting"]:
                mensaje += (
                    f"🏏 OPS — "
                    f"{partido['away']}: {partido['away_hitting']['ops']:.3f} | "
                    f"{partido['home']}: {partido['home_hitting']['ops']:.3f}\n"
                )

            mensaje += "\n"

    mensaje += (
        "🧠 v2.7.1 separa fuerza del matchup, calidad de datos y confiabilidad del "
        "abridor. Un pitcher con muestra pequeña puede seguir siendo candidato, "
        "pero su señal se acerca a neutral y su Confidence queda limitada. "
        "El bullpen continúa siendo un PROXY de team pitching."
    )

    await _reply_long(update.message, mensaje)


async def pool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Audit the complete v2.7.1 Candidate Pool, including rejected games."""
    now = local_now()
    fecha = now.strftime("%Y-%m-%d")
    season = now.year

    status, partidos = await asyncio.to_thread(
        build_daily_matchups, fecha, season
    )

    if status == "api_error":
        await update.message.reply_text("❌ No pude obtener los datos actuales de MLB.")
        return
    if status == "no_games":
        await update.message.reply_text("⚾ No hay juegos de MLB para hoy.")
        return

    elegibles = sum(bool(p["eligible"]) for p in partidos)
    mensaje = (
        "🔎 MLB CANDIDATE POOL — v2.7.1\n"
        f"📅 {fecha} — {AUTO_TZ}\n"
        f"📋 Juegos: {len(partidos)} | ✅ Elegibles: {elegibles} | "
        f"❌ No elegibles: {len(partidos) - elegibles}\n\n"
        "Orden operativo: elegibles primero; luego Confidence. C = Confidence 2.1 | DR = Data Reliability "
        "| Δ = ventaja relativa | SR = menor Starter Reliability.\n\n"
    )

    for i, partido in enumerate(partidos, start=1):
        status_icon = "✅" if partido["eligible"] else "❌"
        lineup_state, _cap = _lineup_coverage_state(partido["lineup_status"])
        small = " | ⚠️ SMALL SAMPLE" if partido["small_sample"] else ""
        mensaje += (
            f"{i}. {status_icon} {partido['away']} vs {partido['home']}\n"
            f"🎯 {partido['favorite']} | {partido['risk']}\n"
            f"📈 Matchup {partido['score']:.1f} | C {partido['confidence']:.0f} | "
            f"DR {partido['data_reliability']:.0f} | Δ {partido['difference']:.1f} | "
            f"SR {partido['min_starter_reliability']:.0f}{small}\n"
            f"📝 Lineups: {lineup_state} | 📦 {partido['data_count']}/8 bloques\n"
            f"🛡️ Gate: {partido['gate_reason']}\n\n"
        )

    mensaje += (
        "ℹ️ /pool es una vista de auditoría. Solo /picks construye el Triple Pick final. "
        "Confidence sigue siendo una señal heurística, no una probabilidad calibrada."
    )
    await _reply_long(update.message, mensaje)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚾ MLB TRIPLE PICK v2.8\n\n"
        "Comandos disponibles:\n"
        "/mlb - Juegos de hoy\n"
        "/picks - Triple Pick final market-aware\n"
        "/pool - Candidate Pool completo\n"
        "/market - Auditoría MODEL vs MARKET\n"
        "/value - Picks con señal de valor en Hard Rock\n"
        "/oddsstatus - Estado del Market Engine\n"
        "/myid - ID de este chat\n"
        "/autostatus - Estado del envío automático\n\n"
        "Pipeline: MODEL → MARKET → NO-VIG → AGREEMENT → VALUE → FINAL PICK"
    )


async def picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triple Pick v2.8: market-aware survival selection with safe model fallback."""
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

    triple_pick, mode = _select_triple_pick_v28(partidos, odds_status)
    base_eligible = sum(bool(p.get("eligible")) for p in partidos)
    survival_count = sum(bool(p.get("survival_approved")) for p in partidos)
    value_count = sum(bool(p.get("value_approved")) for p in partidos)

    mensaje = (
        "🔥 MLB TRIPLE PICK v2.8 — MARKET ENGINE\n"
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
        "🔎 MLB CANDIDATE POOL — v2.8\n"
        f"📅 {fecha}\n"
        f"Market: {'ON' if odds_status == 'ok' else 'OFF/FALLBACK'} | "
        f"Primary: {ODDS_PRIMARY_BOOKMAKER}\n\n"
        "C=Confidence | DR=Data Reliability | MP=Model Probability Proxy | "
        "MKT=consensus no-vig | VG=value gap.\n\n"
    )
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
        "💵 MLB MARKET AUDIT — v2.8\n"
        f"📅 {fecha}\n"
        f"Primary: {ODDS_PRIMARY_BOOKMAKER} | Books: {ODDS_BOOKMAKERS}\n\n"
    )
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
        "🟣 MLB VALUE BOARD — v2.8\n"
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


def main():
    if not TOKEN:
        raise RuntimeError(
            "Falta TELEGRAM_BOT_TOKEN. Configura el nuevo token de BotFather "
            "como variable de entorno antes de iniciar el bot."
        )

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mlb", mlb))
    app.add_handler(CommandHandler("picks", picks))
    app.add_handler(CommandHandler("pool", pool))
    app.add_handler(CommandHandler("market", market))
    app.add_handler(CommandHandler("value", value))
    app.add_handler(CommandHandler("oddsstatus", oddsstatus))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("autostatus", autostatus))

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
    else:
        print(
            "⚠️ JobQueue no disponible. Usa "
            "python-telegram-bot[job-queue] en requirements.txt."
        )

    print("🤖 Bot MLB Triple Pick v2.8 iniciado...")
    app.run_polling()


if __name__ == "__main__":
    main()
