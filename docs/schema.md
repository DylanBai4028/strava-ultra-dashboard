# Data model

The pipeline stores everything in a single SQLite file (`data/strava.db`).
This is the shape of it — see `src/schema.py` for the exact `CREATE TABLE`
statements (it's the source of truth; this doc is the readable version).

## Tables and how they relate

```
activities  ──┬──< streams          (per-second/per-minute time series: HR, pace, altitude, GPS)
              ├──< splits           (per-km summary rows — pace, HR, elevation per kilometre)
              ├──< laps             (device-recorded laps, when available)
              ├──< segment_efforts  ──> segments   (Strava segment leaderboard efforts)
              └──< best_efforts     (Strava's own "fastest time over distance X" records)

gear            (shoes/bikes — activities.gear_id references gear.id, loosely)
athlete_zones   (HR + pace zone boundaries, snapshotted over time — your zones change as fitness does)
athlete_profile (single-row: name, measurement preference, etc.)
schema_version  (internal migration tracking — not training data)
```

`──<` reads as "one activity has many of these." Nothing is a hard foreign
key at the SQLite level except `streams`/`splits`/`laps`/`segment_efforts`/
`best_efforts` → `activities` — the rest are just matched by ID at query
time.

## `activities` — the core table

One row per Strava activity (run, weight-training session, etc). The
columns fall into a few groups:

- **Identity & timing** — `id`, `name`, `sport_type`, `workout_type`,
  `start_local`, `start_date_utc`, `timezone`
- **Distance & effort** — `distance`, `moving_time`, `elapsed_time`,
  `elevation_gain`, `relative_effort` (Strava's own training-load score —
  used throughout the dashboard as a TSS proxy), `avg_heartrate`,
  `avg_cadence`, `avg_speed`
- **Location** — `start_lat`/`start_lng`, `location_city`/`location_state`/
  `location_country`/`location_suburb`. Strava's API never fills the
  `location_*` fields, so this pipeline reverse-geocodes GPS coordinates
  itself (`backfill_locations.py`) and caches the result here — that's
  why they're separate from `start_lat`/`start_lng`.
- **Gear** — `gear_id`, matched against `gear.id`
- **Everything else** — kudos/achievement counts, power data (rarely
  populated for trail running), device metadata

## `streams` — the detailed trace

Time-series data for one activity: heart rate, pace, altitude, GPS, grade,
one row per time offset. This is what the HR zone distribution and
aerobic-efficiency charts are computed from. Real Strava data is
per-second resolution; the synthetic generator (`scripts/generate_sample_data.py`)
uses per-minute points instead, to keep the demo dataset small.

## `splits` — per-kilometre summaries

One row per kilometre of a run (`split` = km number), used for the
per-km pacing chart on long runs. Strava computes these server-side.

## `gear`, `athlete_zones`, `athlete_profile`

Small reference tables, refreshed periodically by `fetch.py`:
- `gear` tracks shoe/bike mileage (the dashboard's shoe-wear warning reads
  `total_distance` against `config.SHOE_WEAR_THRESHOLD_KM`)
- `athlete_zones` is a snapshot each time your zones change, so historical
  HR-zone classification uses the zones that were active *at the time*,
  not your current ones
- `athlete_profile` is a single row of Strava profile metadata

## What's genuinely personal vs. generic

The database schema and pipeline code are entirely generic — they'd work
for anyone's Strava account. What's *not* generic is `src/config.py`: race
dates, training phases, injury history, and weekly mileage targets are
all hardcoded there as a worked example of the author's own training plan.
See the main [README](../README.md) for what that means if you're adapting
this for yourself.
