from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import requests
from datetime import datetime, timedelta
import os


# IMPORTANTE: coloca aquí el token nuevo de BotFather.
TOKEN = "8480223191:AAGNmOKRXjY8HR-D4KxnohFFm9rmcGE-88Q"

# MLB Triple Pick v2.5 - archivo limpio y único


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "¡Hola! Soy tu bot MLB ⚾\n\n"
        "Comandos disponibles:\n"
        "/mlb - Ver los juegos de hoy\n"
        "/picks - Ver el análisis preliminar de hoy"
    )


async def mlb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fecha = datetime.now().strftime("%Y-%m-%d")
    url = (
        f"https://statsapi.mlb.com/api/v1/schedule"
        f"?sportId=1&date={fecha}&hydrate=probablePitcher"
    )

    try:
        respuesta = requests.get(url, timeout=10)
        respuesta.raise_for_status()
        datos = respuesta.json()

        juegos = datos.get("dates", [])

        if not juegos:
            await update.message.reply_text(
                "⚾ No hay juegos de MLB programados para hoy."
            )
            return

        mensaje = "⚾ MLB — JUEGOS DE HOY\n\n"

        for fecha_juegos in juegos:
            for juego in fecha_juegos.get("games", []):
                visitante = juego["teams"]["away"]["team"]["name"]
                local = juego["teams"]["home"]["team"]["name"]

                # gameDate viene en UTC; por ahora conservamos la hora
                # que entrega MLB para evitar conversiones incorrectas.
                hora = juego.get("gameDate", "")[11:16] or "N/D"

                mensaje += (
                    f"🕐 {hora} — {visitante} vs {local}\n"
                )

        await update.message.reply_text(mensaje)

    except requests.RequestException as e:
        await update.message.reply_text(
            "❌ No pude conectar con la API de MLB."
        )
        print(f"Error API /mlb: {e}")
    except (KeyError, TypeError, ValueError) as e:
        await update.message.reply_text(
            "❌ Los datos recibidos de MLB no tienen el formato esperado."
        )
        print(f"Error datos /mlb: {e}")
    except Exception as e:
        await update.message.reply_text(
            "❌ Ocurrió un error obteniendo los juegos de MLB."
        )
        print(f"Error /mlb: {e}")


MLB_API = "https://statsapi.mlb.com/api/v1"


def safe_get_json(url, params=None):
    """GET JSON from MLB Stats API. Returns None when unavailable."""
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        print(f"MLB API error: {exc}")
        return None


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
    return {
        "era": float(stat.get("era", 99.0)),
        "whip": float(stat.get("whip", 99.0)),
        "strikeouts": int(stat.get("strikeOuts", 0)),
        "walks": int(stat.get("baseOnBalls", 0)),
        "innings": float(stat.get("inningsPitched", "0").replace(" ", "") or 0),
        "wins": int(stat.get("wins", 0)),
        "losses": int(stat.get("losses", 0)),
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
                datetime.now().date()
                - timedelta(days=days)
            ).strftime("%Y-%m-%d"),
            "endDate": datetime.now().strftime("%Y-%m-%d"),
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
        innings = float(stat.get("inningsPitched", "0").replace(" ", "") or 0)
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


def build_team_score_v25(
    team_hitting,
    opponent_hitting,
    own_pitching,
    opponent_pitching,
    bullpen,
    opponent_bullpen,
    recent_form,
    opponent_form,
    home,
):
    """
    v2.5 is a TRUE matchup score: 50 is neutral and the score rises only
    when this team has an advantage over its opponent.

    Weights:
      45% starting-pitcher matchup
      25% offense matchup
      15% recent-form matchup
      10% bullpen-proxy matchup
       5% home field
    """
    parts = []

    own_p = _pitcher_quality(own_pitching)
    opp_p = _pitcher_quality(opponent_pitching)
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


def classify_pick_risk(confidence, difference, data_count):
    """Human-readable tier; it does not change the underlying prediction."""
    if data_count < 6:
        return "🔴 DATOS INCOMPLETOS"

    if confidence >= 82 and difference >= 10:
        return "🟢 PICK FUERTE"

    if confidence >= 75 and difference >= 5:
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


async def picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Triple Pick v2.5:
    v2.5 + estado de alineaciones publicadas + clasificación de riesgo.

    Importante:
    - Una alineación no publicada NO se interpreta como lesión.
    - El bullpen sigue siendo un PROXY hasta disponer de un desglose fiable
      específico de relevistas.
    """

    fecha = datetime.now().strftime("%Y-%m-%d")
    season = datetime.now().year

    datos = safe_get_json(
        f"{MLB_API}/schedule",
        params={
            "sportId": 1,
            "date": fecha,
            "hydrate": "probablePitcher,team",
        },
    )

    if not datos:
        await update.message.reply_text(
            "❌ No pude obtener los datos actuales de MLB."
        )
        return

    juegos = datos.get("dates", [])
    if not juegos:
        await update.message.reply_text("⚾ No hay juegos de MLB para hoy.")
        return

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

            away_pitching = get_current_pitcher_stats(
                away_pitcher_id, season
            )
            home_pitching = get_current_pitcher_stats(
                home_pitcher_id, season
            )

            away_hitting = get_team_hitting_stats(away_id, season)
            home_hitting = get_team_hitting_stats(home_id, season)

            away_form = get_recent_team_form(away_id, days=14)
            home_form = get_recent_team_form(home_id, days=14)

            away_bullpen = get_team_bullpen_proxy(away_id, season)
            home_bullpen = get_team_bullpen_proxy(home_id, season)

            away_score = build_team_score_v25(
                away_hitting, home_hitting,
                away_pitching, home_pitching,
                away_bullpen, home_bullpen,
                away_form, home_form,
                home=False,
            )

            home_score = build_team_score_v25(
                home_hitting, away_hitting,
                home_pitching, away_pitching,
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

            lineup_status = get_game_lineup_status(game_pk)
            lineup_bonus = lineup_adjustment(lineup_status)

            # v2.5 confidence is tied to the actual score gap.
            # Complete data improves reliability, but never creates certainty.
            matchup_signal = clamp(difference * 2.2, 0, 30)
            data_reliability = (data_count / 8.0) * 10

            confidence = clamp(
                55
                + matchup_signal
                + data_reliability
                - 10
                + lineup_bonus
            )
            confidence = min(confidence, 93.0)

            risk = classify_pick_risk(
                confidence,
                difference,
                data_count,
            )

            partidos.append(
                {
                    "away": away_name,
                    "home": home_name,
                    "away_pitcher": away_pitcher_name,
                    "home_pitcher": home_pitcher_name,
                    "favorite": favorite,
                    "home_score": home_score,
                    "away_score": away_score,
                    "score": favorite_score,
                    "difference": difference,
                    "confidence": confidence,
                    "risk": risk,
                    "data_count": data_count,
                    "away_pitching": away_pitching,
                    "home_pitching": home_pitching,
                    "away_hitting": away_hitting,
                    "home_hitting": home_hitting,
                    "away_form": away_form,
                    "home_form": home_form,
                    "away_bullpen": away_bullpen,
                    "home_bullpen": home_bullpen,
                    "lineup_status": lineup_status,
                }
            )

    # Strongest matchup first; risk tier is displayed but does not artificially
    # inflate the underlying score.
    partidos.sort(
        key=lambda x: (
            x["confidence"],
            x["difference"],
            x["score"],
            x["data_count"],
        ),
        reverse=True,
    )

    triple_pick = partidos[:3]

    mensaje = (
        "🔥 MLB TRIPLE PICK v2.5\n"
        f"📅 {fecha}\n\n"
        "📊 Matchup: abridores 45% + ofensiva 25% + forma 15% + bullpen proxy 10% + localía 5%\n"
        "📝 Alineaciones: solo se usan cuando MLB las publica.\n"
        "⚠️ No se interpreta una alineación no publicada como lesión.\n\n"
    )

    medallas = ["🥇", "🥈", "🥉"]

    for i, partido in enumerate(triple_pick):
        mensaje += (
            f"{medallas[i]} PICK #{i + 1}\n"
            f"🏟️ {partido['away']} vs {partido['home']}\n"
            f"🎯 Selección: {partido['favorite'] if 'favorite' in partido else (partido['home'] if partido['score'] == partido['home_score'] else partido['away'])}\n"
            f"📈 Score: {partido['score']:.1f}/100\n"
            f"🎯 Confianza calibrada: {partido['confidence']:.0f}%\n"
            f"{partido['risk']}\n"
            f"📚 Datos estadísticos: {partido['data_count']}/8\n"
            f"⚾ {partido['away']}: {partido['away_pitcher']}\n"
            f"⚾ {partido['home']}: {partido['home_pitcher']}\n"
        )

        lineup = partido["lineup_status"]
        if lineup and lineup["published"]:
            mensaje += (
                f"📝 Alineaciones publicadas — "
                f"{partido['away']}: {lineup['away_count']} jugadores | "
                f"{partido['home']}: {lineup['home_count']} jugadores\n"
            )
        else:
            mensaje += "📝 Alineaciones: todavía no publicadas/disponibles\n"

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
                f"⚾ Abridores — ERA "
                f"{partido['away_pitching']['era']:.2f} vs "
                f"{partido['home_pitching']['era']:.2f} | WHIP "
                f"{partido['away_pitching']['whip']:.2f} vs "
                f"{partido['home_pitching']['whip']:.2f}\n"
            )

        if partido["away_hitting"] and partido["home_hitting"]:
            mensaje += (
                f"🏏 OPS — "
                f"{partido['away']}: {partido['away_hitting']['ops']:.3f} | "
                f"{partido['home']}: {partido['home_hitting']['ops']:.3f}\n"
            )

        mensaje += "\n"

    mensaje += (
        "🧠 v2.5 convierte el Score en una comparación directa entre ambos equipos: 50 = partido parejo; más alto = mayor ventaja relativa. Mantiene clasificación de riesgo y estado de alineaciones. "
        "El bullpen continúa marcado como PROXY; no se presenta como una "
        "estadística pura de relevistas."
    )

    await update.message.reply_text(mensaje)


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

    print("🤖 Bot MLB iniciado...")
    app.run_polling()


if __name__ == "__main__":
    main()
