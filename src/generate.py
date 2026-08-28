"""Builds `data/dashboard.html` from `data/strava.db`. Everything below is
organised as one `compute_*` function per dashboard panel (weekly volume,
HR zones, race readiness, and so on) — `build_payload()` near the bottom
calls all of them and assembles the result into the dict handed to the
Jinja2 template. Read-only: this file never writes back to the database."""

import json
import math
import sqlite3
from bisect import bisect_right
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

import requests
from jinja2 import Environment, FileSystemLoader

import config

RUNNING_TYPES = {"Run", "TrailRun", "VirtualRun"}
RUNNING_TYPES_SQL = "(" + ",".join(f"'{t}'" for t in sorted(RUNNING_TYPES)) + ")"


def get_db_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    return date.fromisoformat(s[:10])


# ── Freshness & HR coverage ─────────────────────────────────────────────


def compute_freshness(conn) -> dict:
    row = conn.execute("SELECT MAX(synced_at) AS m FROM activities").fetchone()
    last_synced = row["m"]
    status = "red"
    if last_synced:
        synced_dt = datetime.fromisoformat(last_synced)
        if synced_dt.tzinfo is None:
            synced_dt = synced_dt.replace(tzinfo=UTC)
        age_hours = (datetime.now(UTC) - synced_dt).total_seconds() / 3600
        status = "green" if age_hours <= 25 else ("amber" if age_hours <= 72 else "red")
    return {"last_synced_at": last_synced, "status": status}


def compute_hr_coverage(conn) -> float | None:
    row = conn.execute(
        "SELECT COUNT(*) AS total, SUM(has_heartrate) AS with_hr FROM activities"
    ).fetchone()
    if not row["total"]:
        return None
    return round(100.0 * (row["with_hr"] or 0) / row["total"], 1)


# ── Zone lookup (date-matched) ──────────────────────────────────────────


def load_zone_snapshots(conn):
    rows = conn.execute("""
        SELECT snapshotted_at, hr_z1_min, hr_z1_max, hr_z2_min, hr_z2_max,
               hr_z3_min, hr_z3_max, hr_z4_min, hr_z4_max, hr_z5_min, hr_z5_max
        FROM athlete_zones ORDER BY snapshotted_at
    """).fetchall()
    dates = [parse_date(r["snapshotted_at"]) for r in rows]
    return dates, rows


def zone_for_date(d: date, snap_dates: list, snap_rows: list):
    if not snap_dates:
        return None
    i = bisect_right(snap_dates, d) - 1
    i = max(i, 0)  # fall back to earliest snapshot for activities predating it
    return snap_rows[i]


def classify_hr_zone(hr: float, zrow) -> int:
    for z in (1, 2, 3, 4, 5):
        zmax = zrow[f"hr_z{z}_max"]
        if zmax is not None and hr <= zmax:
            return z
    return 5


# ── Weekly target lookup (interpolated between waypoints) ───────────────


def target_for_week(wk: date):
    # Exact match to a waypoint takes precedence — otherwise a wk that lands
    # exactly on a boundary shared by two segments picks up whichever segment
    # the loop below hits first, even when that segment's OTHER endpoint is
    # None (e.g. wk == 2026-08-25 sits on both the None->40 and 40->80
    # segments; without this it silently returns None instead of 40).
    targets = config.WEEKLY_KM_TARGETS
    for d, k in targets:
        if d == wk:
            return k
    for i in range(len(targets) - 1):
        d0, k0 = targets[i]
        d1, k1 = targets[i + 1]
        if d0 < wk < d1:
            if k0 is None or k1 is None:
                return None
            frac = (wk - d0).days / (d1 - d0).days
            return round(k0 + (k1 - k0) * frac, 1)
    return None


# ── Daily load (shared by ACR and the CTL/ATL/TSB load trend) ───────────


def compute_daily_effort(conn) -> dict[date, float]:
    """Date -> summed relative_effort across ALL activities (matches the ACR calc,
    which treats any logged effort — including weight training — as load, not just
    running). Shared by compute_weekly()'s ACR and compute_load_trend()."""
    rows = conn.execute("""
        SELECT start_local, relative_effort FROM activities WHERE start_local IS NOT NULL
    """).fetchall()
    daily_effort = defaultdict(float)
    for r in rows:
        daily_effort[parse_date(r["start_local"])] += r["relative_effort"] or 0
    return daily_effort


def compute_ctl_atl_series(daily_effort: dict, start: date, end: date) -> tuple[dict, dict]:
    """Exponentially-weighted CTL ('fitness') and ATL ('fatigue') from daily_effort,
    seeded at 0 on `start` — a fresh-start convention every such series needs, and one
    that conveniently sidesteps decaying stale load across the 2019-2023 data gap.
    Missing days are explicit zero load (not skipped), or the decay math is wrong."""
    ctl_alpha = 1 - math.exp(-1 / config.CTL_TIME_CONSTANT_DAYS)
    atl_alpha = 1 - math.exp(-1 / config.ATL_TIME_CONSTANT_DAYS)
    ctl_by_date, atl_by_date = {}, {}
    ctl = atl = 0.0
    d = start
    while d <= end:
        load = daily_effort.get(d, 0)
        ctl += (load - ctl) * ctl_alpha
        atl += (load - atl) * atl_alpha
        ctl_by_date[d] = ctl
        atl_by_date[d] = atl
        d += timedelta(days=1)
    return ctl_by_date, atl_by_date


def compute_race_readiness(ctl_by_date: dict, atl_by_date: dict, today: date) -> dict | None:
    """CTL/ATL/TSB today vs. at the start of each past race — answers "am I as
    fit as I was heading into my last few races", a comparison nothing else on
    the dashboard makes even though both halves (fitness trend, race history)
    already exist as separate panels. Only includes races on/after
    config.LOAD_TREND_START, the same cutoff the load model uses everywhere else."""
    ctl_today, atl_today = ctl_by_date.get(today), atl_by_date.get(today)
    if ctl_today is None:
        return None
    past_races = []
    for r in config.RACE_HISTORY:
        d = r["date"]
        ctl, atl = ctl_by_date.get(d), atl_by_date.get(d)
        if ctl is None:
            continue
        past_races.append(
            {
                "race": r["race"],
                "date": d.isoformat(),
                "ctl": round(ctl, 1),
                "atl": round(atl, 1),
                "tsb": round(ctl - atl, 1),
            }
        )
    past_races.sort(key=lambda r: r["date"], reverse=True)
    return {
        "today": {
            "ctl": round(ctl_today, 1),
            "atl": round(atl_today, 1),
            "tsb": round(ctl_today - atl_today, 1),
        },
        "past_races": past_races,
    }


def compute_ramp_check(weekly: list[dict], today: date) -> dict | None:
    """How steep a ramp reaching the next peak-mileage target actually requires,
    stated as a compound weekly growth rate from the trailing 4-week average.
    Deliberately NOT compared against a fixed "safe" percentage — the flat 10%
    week-on-week rule was removed 2026-08-27 for firing on every big (normal,
    for this athlete) training swing. This just states the honest math, which
    speaks for itself, rather than implying feasibility via a target-curve line
    on a chart that's never checked against anything.
    "Peak" = the highest WEEKLY_KM_TARGETS value between today and whichever
    race (Kosci or Delirious West) comes next; ties keep the earliest date."""
    full_weeks = [w for w in weekly if not w["partial"]]
    last4 = full_weeks[-4:]
    if len(last4) < 4:
        return None
    avg_km = sum(w["km"] for w in last4) / len(last4)

    next_race = min(
        (d for d in (config.KOSCI_DATE, config.DELIRIOUS_WEST_DATE) if d >= today), default=None
    )
    if next_race is None:
        return None
    upcoming = [
        (d, km) for d, km in config.WEEKLY_KM_TARGETS if today <= d <= next_race and km is not None
    ]
    if not upcoming or avg_km <= 0:
        return None
    peak_date, peak_km = max(upcoming, key=lambda t: t[1])
    weeks_remaining = (peak_date - today).days / 7
    if weeks_remaining <= 0:
        return None
    required_growth = (peak_km / avg_km) ** (1 / weeks_remaining) - 1
    return {
        "current_avg_km": round(avg_km, 1),
        "peak_km": peak_km,
        "peak_date": peak_date.isoformat(),
        "weeks_remaining": round(weeks_remaining, 1),
        "required_weekly_growth_pct": round(required_growth * 100, 1),
    }


# ── Weekly aggregates ────────────────────────────────────────────────────


def compute_weekly(conn, daily_effort: dict, ctl_by_date: dict, atl_by_date: dict) -> list[dict]:
    activities = conn.execute("""
        SELECT id, sport_type, workout_type, start_local, distance, moving_time,
               elapsed_time, elevation_gain, relative_effort, avg_cadence,
               avg_heartrate, has_heartrate, manual
        FROM activities
        WHERE start_local IS NOT NULL
        ORDER BY start_local
    """).fetchall()
    if not activities:
        return []

    snap_dates, snap_rows = load_zone_snapshots(conn)

    weeks = defaultdict(
        lambda: {
            "km": 0.0,
            "hours": 0.0,
            "elevation_m": 0.0,
            "run_days": set(),
            "weight_sessions": 0,
            "cadence_vals": [],
            "efficiency_vals": [],
            "effort": 0.0,
        }
    )
    run_dates_sorted = set()

    for a in activities:
        d = parse_date(a["start_local"])
        wk = week_start(d)
        bucket = weeks[wk]
        is_running = a["sport_type"] in RUNNING_TYPES

        if is_running:
            bucket["km"] += (a["distance"] or 0) / 1000
            bucket["hours"] += (a["moving_time"] or 0) / 3600
            bucket["elevation_m"] += a["elevation_gain"] or 0
            bucket["run_days"].add(d)
            run_dates_sorted.add(d)
            if a["avg_cadence"]:
                bucket["cadence_vals"].append(a["avg_cadence"])
        elif a["sport_type"] == "WeightTraining":
            bucket["weight_sessions"] += 1

    # Second pass for aerobic efficiency (needs avg_speed; re-query with it included)
    eff_rows = conn.execute(f"""
        SELECT start_local, avg_heartrate, avg_speed, workout_type, distance, has_heartrate
        FROM activities
        WHERE sport_type IN {RUNNING_TYPES_SQL} AND has_heartrate = 1
              AND avg_heartrate IS NOT NULL AND avg_speed IS NOT NULL
    """).fetchall()
    for r in eff_rows:
        d = parse_date(r["start_local"])
        dist_km = (r["distance"] or 0) / 1000
        if r["workout_type"] == 1 or dist_km < config.EASY_RUN_MIN_DISTANCE_KM:
            continue
        zrow = zone_for_date(d, snap_dates, snap_rows)
        if not zrow or zrow["hr_z2_max"] is None or r["avg_heartrate"] > zrow["hr_z2_max"]:
            continue
        wk = week_start(d)
        weeks[wk]["efficiency_vals"].append(r["avg_speed"] / r["avg_heartrate"])

    # Back-to-back day counting (consecutive running days, two sessions same day = one run day)
    sorted_run_days = sorted(run_dates_sorted)
    b2b_by_week = defaultdict(int)
    for i in range(1, len(sorted_run_days)):
        if (sorted_run_days[i] - sorted_run_days[i - 1]).days == 1:
            b2b_by_week[week_start(sorted_run_days[i])] += 1

    def acr_status(acr_val):
        if acr_val is None:
            return None
        if acr_val > config.UNSAFE_LOAD_RATIO:
            return "critical"
        if acr_val < config.ACR_SAFE_LOW or acr_val > config.ACR_SAFE_HIGH:
            return "warning"
        return "safe"

    result = []
    all_weeks = sorted(weeks.keys())
    today = date.today()
    current_wk = week_start(today)
    prev_wk, prev_elevation, prev_b2b = None, None, None
    for wk in all_weeks:
        b = weeks[wk]
        sunday = wk + timedelta(days=6)
        # For the in-progress week the rolling windows end today — ending on the
        # week's future Sunday would count not-yet-run days as zero effort and
        # deflate acute load (a mid-week ACR would always read as detraining).
        ref_day = min(sunday, today)
        acute_sum = sum(
            daily_effort.get(ref_day - timedelta(days=i), 0) for i in range(config.ACUTE_LOAD_DAYS)
        )
        chronic_sum = sum(
            daily_effort.get(ref_day - timedelta(days=i), 0)
            for i in range(config.CHRONIC_LOAD_DAYS)
        )
        acr = (
            round(
                (acute_sum / config.ACUTE_LOAD_DAYS) / (chronic_sum / config.CHRONIC_LOAD_DAYS), 2
            )
            if chronic_sum
            else None
        )
        ctl_today, atl_today = ctl_by_date.get(ref_day), atl_by_date.get(ref_day)
        ctl_week_ago = ctl_by_date.get(ref_day - timedelta(days=7))

        km = round(b["km"], 1)
        elevation_m = round(b["elevation_m"])
        b2b_pairs = b2b_by_week.get(wk, 0)
        partial = wk == current_wk
        # Spike flags only compare to the immediately preceding calendar week —
        # a gap (a week with zero activities, absent from `weeks` entirely)
        # means there's no real "week-on-week" to compare against. The partial
        # in-progress week never gets flagged: its totals aren't final.
        # (Week-on-week MILEAGE spikes were flagged here too until 2026-08-27 —
        # removed: a single-week % jump doesn't fit a high-mileage, by-feel
        # ultrarunner whose week-to-week swings routinely exceed any fixed
        # threshold, so the flag fired near-constantly and stopped being a
        # useful signal. Elevation and back-to-back flags stay — they're
        # narrower, achilles/back-specific signals, not a blanket volume rule.)
        is_consecutive = not partial and prev_wk is not None and (wk - prev_wk).days == 7
        elevation_spike = bool(
            is_consecutive
            and prev_elevation
            and (elevation_m - prev_elevation) / prev_elevation > config.ELEVATION_SPIKE_PCT
        )
        # An absolute back-to-back count doesn't work for an athlete who runs
        # near-daily by default — most weeks would trip it. The actual injury
        # signal is a sudden JUMP in consecutive-day running versus his own
        # recent pattern, not sustained high frequency the body has adapted to.
        b2b_spike = bool(
            is_consecutive and (b2b_pairs - (prev_b2b or 0)) >= config.B2B_SPIKE_INCREASE
        )

        result.append(
            {
                "week_start": wk.isoformat(),
                "km": km,
                "hours": round(b["hours"], 2),
                "elevation_m": elevation_m,
                "run_days": len(b["run_days"]),
                "weight_sessions": b["weight_sessions"],
                "back_to_back_pairs": b2b_pairs,
                "acr": acr,
                "acr_status": acr_status(acr),
                "ctl": round(ctl_today, 1) if ctl_today is not None else None,
                "atl": round(atl_today, 1) if atl_today is not None else None,
                "tsb": round(ctl_today - atl_today, 1)
                if ctl_today is not None and atl_today is not None
                else None,
                "ctl_ramp_7d": (
                    round(ctl_today - ctl_week_ago, 1)
                    if ctl_today is not None and ctl_week_ago is not None
                    else None
                ),
                "avg_cadence": round(sum(b["cadence_vals"]) / len(b["cadence_vals"]), 1)
                if b["cadence_vals"]
                else None,
                "aerobic_efficiency": (
                    round(sum(b["efficiency_vals"]) / len(b["efficiency_vals"]), 4)
                    if b["efficiency_vals"]
                    else None
                ),
                "target_km": target_for_week(wk),
                "partial": partial,
                "elevation_spike": elevation_spike,
                "b2b_spike": b2b_spike,
            }
        )
        prev_wk, prev_elevation, prev_b2b = wk, elevation_m, b2b_pairs
    return result


# ── HR zone distribution (weekly % time in Z1–Z5) ───────────────────────


def compute_hr_zone_distribution(conn) -> list[dict]:
    snap_dates, snap_rows = load_zone_snapshots(conn)
    if not snap_dates:
        return []
    rows = conn.execute(f"""
        SELECT a.start_local, s.heart_rate
        FROM streams s
        JOIN activities a ON a.id = s.activity_id
        WHERE s.moving = 1 AND s.heart_rate IS NOT NULL
              AND a.sport_type IN {RUNNING_TYPES_SQL} AND a.has_heartrate = 1
    """).fetchall()
    weekly_zone_seconds = defaultdict(lambda: [0, 0, 0, 0, 0])
    for r in rows:
        d = parse_date(r["start_local"])
        zrow = zone_for_date(d, snap_dates, snap_rows)
        if not zrow:
            continue
        zone = classify_hr_zone(r["heart_rate"], zrow)
        weekly_zone_seconds[week_start(d)][zone - 1] += 1

    # Raw per-zone seconds (not percentages) so the template can re-bucket
    # weeks into months/quarters on long ranges and still compute honest
    # percentages — pre-computed weekly %s can't be averaged correctly.
    result = []
    for wk in sorted(weekly_zone_seconds):
        secs = weekly_zone_seconds[wk]
        result.append(
            {
                "week_start": wk.isoformat(),
                "zone_seconds": secs,
            }
        )
    return result


# ── Monthly aggregates (night runs, group/solo, temperature) ───────────


def compute_monthly(conn) -> list[dict]:
    rows = conn.execute(f"""
        SELECT start_local, athlete_count, average_temp, trainer, sport_type
        FROM activities
        WHERE sport_type IN {RUNNING_TYPES_SQL} AND start_local IS NOT NULL
    """).fetchall()
    months = defaultdict(lambda: {"night": 0, "group": 0, "solo": 0, "temps": []})
    for r in rows:
        dt = datetime.fromisoformat(r["start_local"])
        month_key = f"{dt.year:04d}-{dt.month:02d}"
        b = months[month_key]
        if dt.hour < config.NIGHT_RUN_MORNING_CUTOFF or dt.hour >= config.NIGHT_RUN_EVENING_CUTOFF:
            b["night"] += 1
        if (r["athlete_count"] or 1) > 1:
            b["group"] += 1
        else:
            b["solo"] += 1
        if r["average_temp"] is not None and not r["trainer"]:
            b["temps"].append(r["average_temp"])

    result = []
    for m in sorted(months):
        b = months[m]
        result.append(
            {
                "month": m,
                "night_run_count": b["night"],
                "group_count": b["group"],
                "solo_count": b["solo"],
                "avg_temp": round(sum(b["temps"]) / len(b["temps"]), 1) if b["temps"] else None,
            }
        )
    return result


# ── Long run pacing (per-km splits) ─────────────────────────────────────


def compute_long_run_splits(conn, limit: int = 8) -> list[dict]:
    long_runs = conn.execute(
        f"""
        SELECT id, name, start_local FROM activities
        WHERE sport_type IN {RUNNING_TYPES_SQL} AND distance >= ?
        ORDER BY start_local DESC LIMIT ?
    """,
        (config.LONG_RUN_MIN_DISTANCE_KM * 1000, limit),
    ).fetchall()

    result = []
    for run in long_runs:
        splits = conn.execute(
            """
            SELECT split, distance, elapsed_time, avg_speed FROM splits
            WHERE activity_id = ? ORDER BY split
        """,
            (run["id"],),
        ).fetchall()
        if not splits:
            continue
        result.append(
            {
                "activity_id": run["id"],
                "name": run["name"],
                "date": (run["start_local"] or "")[:10],
                "splits": [
                    {
                        "km": s["split"],
                        "pace_sec_per_km": round(1000 / s["avg_speed"]) if s["avg_speed"] else None,
                    }
                    for s in splits
                ],
            }
        )
    return result


# ── Shoe mileage ─────────────────────────────────────────────────────────


def compute_shoes(conn) -> tuple[list[dict], dict]:
    shoes = conn.execute("""
        SELECT id, name, brand, model_name, retired, primary_gear, total_distance
        FROM gear WHERE gear_type = 'Shoe' ORDER BY total_distance DESC
    """).fetchall()
    shoe_list = []
    sessions = {}
    for s in shoes:
        shoe_list.append(
            {
                "id": s["id"],
                "name": s["name"],
                "brand": s["brand"],
                "model_name": s["model_name"],
                "retired": bool(s["retired"]),
                "primary": bool(s["primary_gear"]),
                "total_km": round((s["total_distance"] or 0) / 1000, 1),
                "threshold_km": config.SHOE_WEAR_THRESHOLD_KM,
            }
        )
        acts = conn.execute(
            """
            SELECT name, start_local, distance FROM activities
            WHERE gear_id = ? ORDER BY start_local DESC LIMIT 50
        """,
            (s["id"],),
        ).fetchall()
        sessions[s["id"]] = [
            {
                "name": a["name"],
                "date": (a["start_local"] or "")[:10],
                "distance_km": round((a["distance"] or 0) / 1000, 1),
            }
            for a in acts
        ]
    return shoe_list, sessions


# ── Best efforts (all 9 distances, each shown as all-time / 6wk / 6mo) ──
# Strava computes these natively up to 50K (its own casing, "50k") — anything
# longer isn't in the best_efforts table at all, so 80K/100K/100 Miles are
# derived via a sliding-window scan (see _fastest_window_for_activity).
NATIVE_BEST_EFFORT_NAMES = ("5K", "10K", "15K", "Half-Marathon", "Marathon", "50k")
ULTRA_DISTANCES_M = {
    "80K": 80_000,
    "100K": 100_000,
    "100 Miles": 160_934,
}
BEST_EFFORT_WINDOW_DAYS = {
    "all_time": None,
    "6w": 42,
    "6mo": 182,
}


def _fastest_window_for_activity(conn, activity_id: str, target_m: float) -> int | None:
    """Sliding-window scan over a candidate activity's cumulative distance
    stream for the fastest time covering >= target_m — the same method Strava
    uses internally for its own best efforts. More accurate than just taking a
    long run's total elapsed time, which would overstate the target distance's
    time for any run that continued past it (e.g. a 60K run's FULL time is not
    its 50K split time)."""
    rows = conn.execute(
        """
        SELECT time_offset, distance FROM streams
        WHERE activity_id = ? AND distance IS NOT NULL
        ORDER BY time_offset
    """,
        (activity_id,),
    ).fetchall()
    n = len(rows)
    if n < 2:
        return None
    times = [r["time_offset"] for r in rows]
    dists = [r["distance"] for r in rows]
    if dists[-1] - dists[0] < target_m:
        return None
    left = 0
    best = None
    for right in range(n):
        while left < right and dists[right] - dists[left + 1] >= target_m:
            left += 1
        if dists[right] - dists[left] >= target_m:
            duration = times[right] - times[left]
            if best is None or duration < best:
                best = duration
    return best


def compute_all_best_efforts(conn) -> dict:
    today = date.today()
    result = {}

    for name in NATIVE_BEST_EFFORT_NAMES:
        result[name] = {}
        for window_key, days in BEST_EFFORT_WINDOW_DAYS.items():
            if days is None:
                row = conn.execute(
                    """
                    SELECT elapsed_time, start_local FROM best_efforts
                    WHERE name = ? ORDER BY elapsed_time ASC LIMIT 1
                """,
                    (name,),
                ).fetchone()
            else:
                start = (today - timedelta(days=days)).isoformat()
                row = conn.execute(
                    """
                    SELECT elapsed_time, start_local FROM best_efforts
                    WHERE name = ? AND start_local >= ? ORDER BY elapsed_time ASC LIMIT 1
                """,
                    (name, start),
                ).fetchone()
            result[name][window_key] = (
                {"elapsed_time": row["elapsed_time"], "date": row["start_local"][:10]}
                if row
                else None
            )

    for name, target_m in ULTRA_DISTANCES_M.items():
        result[name] = {}
        for window_key, days in BEST_EFFORT_WINDOW_DAYS.items():
            query = f"""
                SELECT id, name, start_local FROM activities
                WHERE sport_type IN {RUNNING_TYPES_SQL} AND distance >= ?
            """
            params = [target_m]
            if days is not None:
                query += " AND start_local >= ?"
                params.append((today - timedelta(days=days)).isoformat())
            candidates = conn.execute(query, params).fetchall()
            best_duration, best_activity = None, None
            for c in candidates:
                duration = _fastest_window_for_activity(conn, c["id"], target_m)
                if duration is not None and (best_duration is None or duration < best_duration):
                    best_duration, best_activity = duration, c
            result[name][window_key] = (
                {
                    "elapsed_time": best_duration,
                    "date": (best_activity["start_local"] or "")[:10],
                    "name": best_activity["name"],
                }
                if best_activity
                else None
            )
    return result


# ── Longest run to date ──────────────────────────────────────────────────


def compute_longest_run(conn) -> dict | None:
    row = conn.execute(f"""
        SELECT name, start_local, distance, moving_time, elapsed_time
        FROM activities
        WHERE sport_type IN {RUNNING_TYPES_SQL}
        ORDER BY distance DESC LIMIT 1
    """).fetchone()
    if not row:
        return None
    return {
        "name": row["name"],
        "date": (row["start_local"] or "")[:10],
        "distance_km": round((row["distance"] or 0) / 1000, 1),
        "moving_time": row["moving_time"],
        "elapsed_time": row["elapsed_time"],
    }


# ── Training plan horizon (Training Plan tab) ────────────────────────────


def compute_plan_weeks(weekly: list[dict]) -> list[dict]:
    """One row per calendar week across the full plan horizon (first target
    waypoint through Delirious West race week), pairing the interpolated
    weekly target with actual km where the week has been run. Unlike
    `weekly`, this includes future weeks and zero-activity past weeks —
    the Training Plan tab needs the whole curve, not just weeks with data."""
    actual_by_week = {w["week_start"]: w for w in weekly}
    today_wk = week_start(date.today())
    kosci_wk = week_start(config.KOSCI_DATE)
    dw_wk = week_start(config.DELIRIOUS_WEST_DATE)
    wk = week_start(config.WEEKLY_KM_TARGETS[0][0])
    rows = []
    while wk <= dw_wk:
        a = actual_by_week.get(wk.isoformat())
        if a:
            actual = a["km"]
        else:
            actual = 0.0 if wk <= today_wk else None  # past week, nothing recorded
        rows.append(
            {
                "week_start": wk.isoformat(),
                "target_km": target_for_week(wk),
                "actual_km": actual,
                "is_current": wk == today_wk,
                "race": "kosci" if wk == kosci_wk else ("delirious" if wk == dw_wk else None),
            }
        )
        wk += timedelta(days=7)
    return rows


# ── Location frequency heatmap (city -> suburb) ──────────────────────────


def _cluster_locations(rows, threshold_deg: float = 1.0) -> tuple[list[dict], list[int]]:
    """Greedy clustering: a point joins the nearest existing cluster centroid
    within threshold_deg, else starts a new one. ~1.0 degree (~100km at these
    latitudes) comfortably separates distinct travel regions (e.g. Sydney vs
    a Portugal trip) while keeping one city's day-to-day GPS spread together.
    Strava's location_city is deprecated/always-NULL in this API version, so
    clustering GPS points ourselves is the only way to group by "place"."""
    clusters: list[dict] = []
    assignments: list[int] = []
    for r in rows:
        lat, lng = r["start_lat"], r["start_lng"]
        best_idx, best_dist = None, None
        for i, c in enumerate(clusters):
            clat, clng = c["sum_lat"] / c["n"], c["sum_lng"] / c["n"]
            d = ((lat - clat) ** 2 + (lng - clng) ** 2) ** 0.5
            if d <= threshold_deg and (best_dist is None or d < best_dist):
                best_idx, best_dist = i, d
        if best_idx is None:
            clusters.append({"sum_lat": lat, "sum_lng": lng, "n": 1})
            assignments.append(len(clusters) - 1)
        else:
            clusters[best_idx]["sum_lat"] += lat
            clusters[best_idx]["sum_lng"] += lng
            clusters[best_idx]["n"] += 1
            assignments.append(best_idx)
    return clusters, assignments


def _reverse_geocode_components(lat: float, lng: float, zoom: int = 10) -> dict | None:
    """Free, keyless reverse geocoding via OpenStreetMap's Nominatim. Only
    called once per cluster, never per-activity. zoom=10 resolves to city
    level; zoom=14 adds suburb-level fields. Returns None on request failure;
    on success, individual fields may still be missing (e.g. a city with no
    state, or a rural point with no suburb)."""
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={
                "lat": lat,
                "lon": lng,
                "format": "json",
                "zoom": zoom,
                "accept-language": "en",
            },
            headers={"User-Agent": "ultrarunning-personal-dashboard/1.0"},
            timeout=10,
        )
        resp.raise_for_status()
        addr = resp.json().get("address", {})
        return {
            "city": addr.get("city")
            or addr.get("town")
            or addr.get("village")
            or addr.get("municipality")
            or addr.get("county"),
            "state": addr.get("state"),
            "country": addr.get("country"),
            "suburb": addr.get("suburb")
            or addr.get("neighbourhood")
            or addr.get("quarter")
            or addr.get("city_district")
            or addr.get("village")
            or addr.get("town"),
        }
    except requests.RequestException:
        return None


def compute_location_heatmap(conn) -> list[dict]:
    """Run counts per city with a per-suburb breakdown, from the columns
    populated by backfill_locations.py. Activities with no resolved suburb
    land in an 'Other' bucket within their city. Replaces the retired
    tile-map view — pure counts, no geometry, no baked imagery."""
    rows = conn.execute(f"""
        SELECT location_country AS country, location_city AS city,
               COALESCE(location_suburb, 'Other') AS suburb, COUNT(*) AS n
        FROM activities
        WHERE sport_type IN {RUNNING_TYPES_SQL} AND start_lat IS NOT NULL
        GROUP BY 1, 2, 3
    """).fetchall()
    cities: dict = {}
    for r in rows:
        city_label = r["city"] or "(unknown)"
        key = (r["country"], city_label)
        c = cities.setdefault(
            key, {"city": city_label, "country": r["country"] or "", "count": 0, "suburbs": []}
        )
        c["count"] += r["n"]
        c["suburbs"].append({"name": r["suburb"], "count": r["n"]})
    out = sorted(cities.values(), key=lambda c: -c["count"])
    for c in out:
        c["suburbs"].sort(key=lambda s: -s["count"])
    return out


# ── Week drill-down sessions ─────────────────────────────────────────────


def compute_week_sessions(conn) -> dict:
    rows = conn.execute("""
        SELECT name, sport_type, start_local, distance, moving_time, relative_effort
        FROM activities WHERE start_local IS NOT NULL ORDER BY start_local
    """).fetchall()
    by_week = defaultdict(list)
    for r in rows:
        d = parse_date(r["start_local"])
        wk = week_start(d).isoformat()
        by_week[wk].append(
            {
                "name": r["name"],
                "sport_type": r["sport_type"],
                "date": d.isoformat(),
                "distance_km": round((r["distance"] or 0) / 1000, 1),
                "moving_min": round((r["moving_time"] or 0) / 60),
                "effort": r["relative_effort"],
            }
        )
    return dict(by_week)


def compute_yearly(weekly: list[dict]) -> list[dict]:
    """Derived from the already-computed weekly rows rather than re-querying —
    a calendar-year rollup of total km, total elevation, and average weekly
    consistency (run_days / 7), for the year-over-year view."""
    years = defaultdict(lambda: {"km": 0.0, "elevation_m": 0.0, "consistency_vals": []})
    for w in weekly:
        y = years[w["week_start"][:4]]
        y["km"] += w["km"]
        y["elevation_m"] += w["elevation_m"]
        y["consistency_vals"].append(w["run_days"] / 7)
    result = []
    for year in sorted(years):
        y = years[year]
        result.append(
            {
                "year": year,
                "km": round(y["km"], 1),
                "elevation_m": round(y["elevation_m"]),
                "avg_consistency_pct": (
                    round(100 * sum(y["consistency_vals"]) / len(y["consistency_vals"]), 1)
                    if y["consistency_vals"]
                    else None
                ),
            }
        )
    return result


def compute_training_phases() -> list[dict]:
    """Serializes config.TRAINING_PHASES, adding a target_km_range computed by scanning
    config.WEEKLY_KM_TARGETS for waypoints that fall inside each phase's date range —
    keeps the range honest against the target curve instead of a second hardcoded number
    that could drift from it."""
    out = []
    for p in config.TRAINING_PHASES:
        end = p["end"]
        targets_in_phase = [
            km
            for d, km in config.WEEKLY_KM_TARGETS
            if km is not None and p["start"] <= d and (end is None or d < end)
        ]
        if targets_in_phase:
            lo, hi = min(targets_in_phase), max(targets_in_phase)
            target_km_range = f"{lo} km/wk" if lo == hi else f"{lo}–{hi} km/wk"
        else:
            target_km_range = None
        out.append(
            {
                "start": p["start"].isoformat(),
                "end": end.isoformat() if end else None,
                "name": p["name"],
                "objective": p["objective"],
                "exit_criteria": p["exit_criteria"],
                "target_km_range": target_km_range,
            }
        )
    return out


def build_payload(conn) -> dict:
    shoe_list, shoe_sessions = compute_shoes(conn)
    today = date.today()
    daily_effort = compute_daily_effort(conn)
    ctl_by_date, atl_by_date = compute_ctl_atl_series(daily_effort, config.LOAD_TREND_START, today)
    weekly = compute_weekly(conn, daily_effort, ctl_by_date, atl_by_date)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "config": {
            "kosci_date": config.KOSCI_DATE.isoformat(),
            "delirious_date": config.DELIRIOUS_WEST_DATE.isoformat(),
            "europe_start": config.EUROPE_START.isoformat(),
            "europe_end": config.EUROPE_END.isoformat(),
            "sobriety_start": config.SOBRIETY_START.isoformat(),
            "unsafe_load_ratio": config.UNSAFE_LOAD_RATIO,
            "acr_safe_low": config.ACR_SAFE_LOW,
            "acr_safe_high": config.ACR_SAFE_HIGH,
            "shoe_threshold_km": config.SHOE_WEAR_THRESHOLD_KM,
            "elevation_spike_pct": config.ELEVATION_SPIKE_PCT,
            "b2b_spike_increase": config.B2B_SPIKE_INCREASE,
            "weekly_km_targets": [[d.isoformat(), km] for d, km in config.WEEKLY_KM_TARGETS],
            "training_phases": compute_training_phases(),
            "delirious_west_distance_km": config.DELIRIOUS_WEST_DISTANCE_KM,
        },
        "injuries": [{**inj, "date": inj["date"].isoformat()} for inj in config.INJURIES],
        "race_history": [{**r, "date": r["date"].isoformat()} for r in config.RACE_HISTORY],
        "race_readiness": compute_race_readiness(ctl_by_date, atl_by_date, today),
        "ramp_check": compute_ramp_check(weekly, today),
        "freshness": compute_freshness(conn),
        "hr_coverage_pct": compute_hr_coverage(conn),
        "weekly": weekly,
        "hr_zones": compute_hr_zone_distribution(conn),
        "monthly": compute_monthly(conn),
        "long_run_splits": compute_long_run_splits(conn),
        "shoes": shoe_list,
        "shoe_sessions": shoe_sessions,
        "best_efforts": compute_all_best_efforts(conn),
        "longest_run": compute_longest_run(conn),
        "location_heatmap": compute_location_heatmap(conn),
        "plan_weeks": compute_plan_weeks(weekly),
        "week_sessions": compute_week_sessions(conn),
        "yearly": compute_yearly(weekly),
    }


def main():
    conn = get_db_conn()
    payload = build_payload(conn)
    conn.close()

    env = Environment(loader=FileSystemLoader(str(config.TEMPLATES_DIR)))
    template = env.get_template("dashboard.html")
    html = template.render(data_json=json.dumps(payload, default=str))

    config.DASHBOARD_PATH.write_text(html)
    print(f"Dashboard generated ✓ ({config.DASHBOARD_PATH})")


if __name__ == "__main__":
    main()
