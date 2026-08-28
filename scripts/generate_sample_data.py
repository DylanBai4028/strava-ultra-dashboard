"""Generate a small, fully synthetic training history so the dashboard can be
built and explored with zero real Strava data.

No real activity data is used here — this script fabricates ~15 months of
plausible running + weight training sessions from scratch, using a fixed
random seed so the output is reproducible. It writes directly to
`data/strava.db` using the exact schema `schema.py` creates, so run
`schema.py` first.

The training *narrative* here (build → peak → step-back, repeating) is a
generic periodization pattern — it has no connection to the real race
calendar and personal data in `config.py`. That's expected: `config.py`
ships as a worked example of the author's own training (see the README's
"What's in src/config.py" section), while this script exists purely to
make every dashboard tab render with believable-looking numbers for
anyone trying the project out.

Usage:
    uv run python scripts/generate_sample_data.py
"""

import math
import random
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config  # the sys.path insert above must run first

SEED = 42
WEEKS_OF_HISTORY = (
    64  # ~15 months — enough for CTL(42d)/ATL(7d) and a yearly rollup to mean something
)

# A handful of fake-but-plausible training locations so the location
# heatmap has more than one dot on it. Not tied to any real GPS track.
LOCATIONS = [
    {
        "city": "Sydney",
        "state": "NSW",
        "country": "Australia",
        "suburb": "Centennial Park",
        "lat": -33.8987,
        "lng": 151.2270,
    },
    {
        "city": "Sydney",
        "state": "NSW",
        "country": "Australia",
        "suburb": "Manly",
        "lat": -33.7969,
        "lng": 151.2857,
    },
    {
        "city": "Sydney",
        "state": "NSW",
        "country": "Australia",
        "suburb": "Bondi",
        "lat": -33.8908,
        "lng": 151.2743,
    },
    {
        "city": "Katoomba",
        "state": "NSW",
        "country": "Australia",
        "suburb": "Blue Mountains",
        "lat": -33.7128,
        "lng": 150.3119,
    },
]

SHOES = [
    {
        "id": "shoe_1",
        "name": "Trail Shoe A",
        "brand": "DemoBrand",
        "model_name": "Trailrunner 3",
        "retired": 0,
        "primary_gear": 1,
    },
    {
        "id": "shoe_2",
        "name": "Road Shoe B",
        "brand": "DemoBrand",
        "model_name": "Roadster 7",
        "retired": 1,
        "primary_gear": 0,
    },
]


def weekly_target_km(week_index: int) -> float:
    """A generic 4-week build/step-back cycle with a slow overall ramp —
    not derived from any real training plan, just enough structure that the
    load charts (CTL/ATL/TSB, ramp-rate check) have something realistic to
    show instead of pure noise."""
    cycle_position = week_index % 4  # weeks 0,1,2 build; week 3 steps back
    base = 35 + week_index * 0.6  # slow overall ramp across the window
    if cycle_position == 3:
        base *= 0.65  # recovery week
    else:
        base *= 1.0 + cycle_position * 0.12
    return max(15.0, base)


def make_activity(
    rng,
    activity_id,
    sport_type,
    start_dt,
    distance_km,
    elevation_m,
    avg_hr,
    has_hr,
    cadence,
    location,
    gear_id=None,
    workout_type=0,
):
    moving_time = int(
        distance_km * 1000 / (2.6 + rng.uniform(-0.3, 0.3))
    )  # ~2.3-2.9 m/s pace, trail-ish
    elapsed_time = moving_time + rng.randint(0, 300)
    relative_effort = round(moving_time / 60 * (avg_hr / 140 if has_hr else 0.85))
    return {
        "id": activity_id,
        "name": f"{'Long run' if distance_km > 25 else ('Easy run' if sport_type != 'WeightTraining' else 'Strength session')}",
        "description": None,
        "sport_type": sport_type,
        "workout_type": workout_type,
        "start_local": start_dt.isoformat(),
        "start_date_utc": start_dt.isoformat(),
        "timezone": "(GMT+10:00) Australia/Sydney",
        "utc_offset": 36000.0,
        "gear_id": gear_id,
        "external_id": None,
        "device_name": "Demo Watch",
        "distance": distance_km * 1000,
        "moving_time": moving_time,
        "elapsed_time": elapsed_time,
        "elevation_gain": elevation_m,
        "elev_high": 50 + elevation_m,
        "elev_low": 50,
        "avg_speed": distance_km * 1000 / moving_time if moving_time else None,
        "max_speed": None,
        "avg_cadence": cadence,
        "avg_heartrate": avg_hr if has_hr else None,
        "max_heartrate": (avg_hr + rng.randint(8, 20)) if has_hr else None,
        "average_temp": round(rng.uniform(12, 26), 1),
        "average_watts": None,
        "weighted_average_watts": None,
        "kilojoules": None,
        "relative_effort": relative_effort,
        "total_calories": round(distance_km * 65)
        if sport_type != "WeightTraining"
        else rng.randint(150, 300),
        "kudos_count": rng.randint(0, 25),
        "achievement_count": rng.randint(0, 3),
        "pr_count": 0,
        "photo_count": 0,
        "athlete_count": 1,
        "has_heartrate": int(has_hr),
        "perceived_exertion": None,
        "trainer": 0,
        "commute": 0,
        "manual": 0,
        "private": 0,
        "flagged": 0,
        "upload_id": None,
        "start_lat": location["lat"] + rng.uniform(-0.01, 0.01),
        "start_lng": location["lng"] + rng.uniform(-0.01, 0.01),
        "end_lat": location["lat"] + rng.uniform(-0.01, 0.01),
        "end_lng": location["lng"] + rng.uniform(-0.01, 0.01),
        "location_city": location["city"],
        "location_state": location["state"],
        "location_country": location["country"],
        "location_suburb": location["suburb"],
        "summary_polyline": None,
        "synced_at": datetime.now(UTC).isoformat(),
        "temp_estimated": 0,
    }


def make_splits(rng, activity_id, distance_km, moving_time, avg_hr):
    """Per-km splits for the pacing chart — only long runs get these, matching
    what compute_long_run_splits() in generate.py looks for.

    Columns match the `splits` table: activity_id, split, distance,
    elapsed_time, moving_time, elevation_difference, avg_speed, avg_heartrate,
    avg_cadence, pace_zone.
    """
    n_km = int(distance_km)
    base_pace = moving_time / distance_km  # seconds/km, average
    rows = []
    for km in range(1, n_km + 1):
        pace = base_pace * rng.uniform(0.92, 1.1)  # natural per-km variation
        rows.append(
            (
                activity_id,
                km,
                1000.0,
                round(pace),
                round(pace * 0.9),
                round(rng.uniform(-5, 15)),
                1000 / pace,
                (avg_hr + rng.randint(-5, 5)) if avg_hr else None,
                None,
                None,
            )
        )
    return rows


def build_dataset(seed: int = SEED):
    rng = random.Random(seed)
    activities = []
    streams = []
    splits = []
    end = datetime.now(UTC).date()
    start = end - timedelta(weeks=WEEKS_OF_HISTORY)

    activity_counter = 0
    for week_index in range(WEEKS_OF_HISTORY):
        week_monday = start + timedelta(weeks=week_index)
        target_km = weekly_target_km(week_index)

        # 3-5 runs a week: one long run (~35% of weekly volume), the rest easy/moderate
        n_runs = rng.choice([3, 4, 4, 5])
        long_run_km = round(target_km * 0.35, 1)
        remaining_km = target_km - long_run_km
        other_runs_km = (
            [
                round(remaining_km / (n_runs - 1) * rng.uniform(0.7, 1.3), 1)
                for _ in range(n_runs - 1)
            ]
            if n_runs > 1
            else []
        )

        run_days = sorted(rng.sample(range(7), n_runs))
        long_run_day = run_days[-1]  # put the long run on the last selected day (weekend-ish)

        for i, day_offset in enumerate(run_days):
            distance_km = (
                long_run_km
                if day_offset == long_run_day
                else other_runs_km[min(i, len(other_runs_km) - 1)]
            )
            distance_km = max(3.0, distance_km)
            elevation_m = round(distance_km * rng.uniform(8, 22))
            has_hr = rng.random() < 0.92
            avg_hr = rng.randint(148, 165) if distance_km > 25 else rng.randint(132, 152)
            cadence = rng.randint(158, 172)
            location = rng.choice(LOCATIONS)
            hour = rng.choice([5, 6, 6, 17, 18])
            start_dt = datetime.combine(
                week_monday + timedelta(days=day_offset), datetime.min.time()
            ) + timedelta(hours=hour, minutes=rng.randint(0, 59))

            activity_counter += 1
            activity_id = f"demo_run_{activity_counter}"
            gear = (
                SHOES[0]["id"]
                if distance_km <= 20
                else rng.choice([SHOES[0]["id"], SHOES[1]["id"]])
            )
            act = make_activity(
                rng,
                activity_id,
                rng.choice(["Run", "TrailRun"]),
                start_dt,
                distance_km,
                elevation_m,
                avg_hr,
                has_hr,
                cadence,
                location,
                gear_id=gear,
            )
            activities.append(act)

            if has_hr:
                # Minute-resolution HR trace (not per-second — real Strava data is
                # far higher resolution; this keeps the demo dataset small).
                n_points = max(1, act["moving_time"] // 60)
                for t in range(n_points):
                    hr = avg_hr + round(
                        10 * math.sin(t / max(1, n_points) * math.pi) + rng.uniform(-6, 6)
                    )
                    streams.append(
                        (
                            activity_id,
                            t * 60,
                            None,
                            None,
                            None,
                            hr,
                            cadence,
                            None,
                            1,
                            None,
                            None,
                            None,
                        )
                    )

            if distance_km >= config.LONG_RUN_MIN_DISTANCE_KM:
                splits.extend(
                    make_splits(
                        rng,
                        activity_id,
                        distance_km,
                        act["moving_time"],
                        avg_hr if has_hr else None,
                    )
                )

        # ~1 weight-training session a week
        if rng.random() < 0.8:
            activity_counter += 1
            activity_id = f"demo_weights_{activity_counter}"
            day_offset = rng.choice([d for d in range(7) if d not in run_days] or [2])
            start_dt = datetime.combine(
                week_monday + timedelta(days=day_offset), datetime.min.time()
            ) + timedelta(hours=rng.choice([7, 18]))
            act = make_activity(
                rng,
                activity_id,
                "WeightTraining",
                start_dt,
                0,
                0,
                rng.randint(105, 125),
                rng.random() < 0.6,
                None,
                rng.choice(LOCATIONS),
            )
            activities.append(act)

    return activities, streams, splits


def write_dataset(activities, streams, splits):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")

    cols = list(activities[0].keys())
    placeholders = ",".join("?" * len(cols))
    conn.executemany(
        f"INSERT OR REPLACE INTO activities ({','.join(cols)}) VALUES ({placeholders})",
        [tuple(a[c] for c in cols) for a in activities],
    )

    conn.executemany(
        """INSERT INTO streams (activity_id, time_offset, distance, altitude,
           velocity_smooth, heart_rate, cadence, grade_smooth, moving, temp, lat, lng)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        streams,
    )

    conn.executemany(
        """INSERT INTO splits (activity_id, split, distance, elapsed_time, moving_time,
           elevation_difference, avg_speed, avg_heartrate, avg_cadence, pace_zone)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        splits,
    )

    for shoe in SHOES:
        total_m = sum(a["distance"] for a in activities if a.get("gear_id") == shoe["id"])
        conn.execute(
            """INSERT OR REPLACE INTO gear (id, gear_type, name, brand, model_name, retired,
               primary_gear, total_distance, synced_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                shoe["id"],
                "Shoe",
                shoe["name"],
                shoe["brand"],
                shoe["model_name"],
                shoe["retired"],
                shoe["primary_gear"],
                total_m,
                datetime.now(UTC).isoformat(),
            ),
        )

    conn.execute(
        """INSERT OR REPLACE INTO athlete_zones (snapshotted_at,
           hr_z1_min, hr_z1_max, hr_z2_min, hr_z2_max, hr_z3_min, hr_z3_max,
           hr_z4_min, hr_z4_max, hr_z5_min, hr_z5_max, hr_zone_source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            (datetime.now(UTC).date() - timedelta(weeks=WEEKS_OF_HISTORY)).isoformat(),
            0,
            125,
            126,
            143,
            144,
            161,
            162,
            179,
            180,
            220,
            "demo",
        ),
    )

    conn.execute(
        """INSERT OR REPLACE INTO athlete_profile (id, first_name, last_name,
           measurement_preference, updated_at) VALUES (1, ?, ?, ?, ?)""",
        ("Demo", "Athlete", "metric", datetime.now(UTC).isoformat()),
    )

    conn.commit()
    conn.close()


def main():
    activities, streams, splits = build_dataset()
    write_dataset(activities, streams, splits)
    n_runs = sum(1 for a in activities if a["sport_type"] != "WeightTraining")
    n_weights = len(activities) - n_runs
    n_long_runs = len({s[0] for s in splits})
    print(
        f"Synthetic dataset written to {config.DB_PATH}\n"
        f"  {n_runs} runs, {n_weights} weight-training sessions, "
        f"{n_long_runs} with per-km splits, across {WEEKS_OF_HISTORY} weeks (seed={SEED})"
    )


if __name__ == "__main__":
    main()
