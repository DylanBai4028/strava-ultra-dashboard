import os
import sys
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

# Paths — derived from this file's location; correct regardless of where scripts are invoked from
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "strava.db"
DASHBOARD_PATH = DATA_DIR / "dashboard.html"
SYNC_LOG = DATA_DIR / "sync.log"
BACKFILL_LOG = DATA_DIR / "backfill.log"
CHECKPOINT = DATA_DIR / "backfill_checkpoint.txt"
TEMPLATES_DIR = Path(__file__).parent / "templates"
ENV_PATH = BASE_DIR / ".env"

# Strava credentials are optional at import time — schema.py, generate.py, and
# the synthetic-data script never touch the Strava API, so they shouldn't
# require .env to exist. Only fetch.py/backfill.py actually call Strava, so
# they call ensure_strava_credentials() themselves before doing so.
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
STRAVA_CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET")
STRAVA_REFRESH_TOKEN = os.environ.get("STRAVA_REFRESH_TOKEN")
GMAIL_APP_PASSWORD = os.environ.get(
    "GMAIL_APP_PASSWORD", ""
)  # optional; validated only in notify_failure()
NOTIFICATION_EMAIL = "dulabai4028@gmail.com"


def ensure_strava_credentials():
    """Call at the start of any script that actually hits the Strava API.
    Scripts that only read the local database (generate.py, schema.py, the
    synthetic-data generator) don't need this."""
    if not (STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET and STRAVA_REFRESH_TOKEN):
        sys.exit("Credentials not found. Run auth.py first to set up Strava access.")

# Race dates
KOSCI_DATE = date(2026, 11, 27)
DELIRIOUS_WEST_DATE = date(2027, 3, 31)
DELIRIOUS_WEST_DISTANCE_KM = 322  # ~200 mi — course distance, not GPS-measured

# Training calendar
EUROPE_START = date(2026, 7, 16)
EUROPE_END = date(2026, 8, 25)
SOBRIETY_START = date(2027, 1, 1)
KOSCI_RACE_WEEK_START = date(2026, 11, 23)
KOSCI_RACE_WEEK_END = date(2026, 11, 29)
TAPER_START = date(2027, 3, 15)

# Training phases — the six blocks between now and Delirious West, formalized here so
# both the Summary phase strip and the Training Plan phases table read from one source
# instead of duplicating dates as template JS constants. Boundaries match the phase
# logic that already existed in the dashboard template (currentPhase()/renderPhaseStrip())
# before this was pulled into config — not new judgment calls, just named.
TRAINING_PHASES = [
    {
        "start": EUROPE_START,
        "end": EUROPE_END,
        "name": "Europe deload",
        "objective": "planned break — low volume, leisure running only expected",
        "exit_criteria": "Return to Sydney, resume structured running",
    },
    {
        "start": EUROPE_END,
        "end": KOSCI_RACE_WEEK_START,
        "name": "Post-Europe rebuild",
        "objective": "rebuild fitness from a detrained base toward the Kosci Miler",
        "exit_criteria": "Reach peak week before Kosci Miler race week",
    },
    {
        "start": KOSCI_RACE_WEEK_START,
        "end": KOSCI_RACE_WEEK_END + timedelta(days=1),
        "name": "Kosci Miler race week",
        "objective": "taper, travel, and execute the Kosci Miler (27 Nov 2026)",
        "exit_criteria": "Finish Kosci Miler",
    },
    {
        "start": KOSCI_RACE_WEEK_END + timedelta(days=1),
        "end": SOBRIETY_START,
        "name": "Recover & rebuild",
        "objective": "post-Kosci recovery, then rebuild into the Delirious West build",
        "exit_criteria": "Begin sobriety block (1 Jan 2027)",
    },
    {
        "start": SOBRIETY_START,
        "end": TAPER_START,
        "name": "Peak block (sober)",
        "objective": "final build block — sobriety active for recovery and adaptation",
        "exit_criteria": "Reach peak week before taper",
    },
    {
        "start": TAPER_START,
        "end": DELIRIOUS_WEST_DATE,
        "name": "Taper",
        "objective": "shed fatigue, keep sharpness ahead of race day",
        "exit_criteria": "Delirious West 200 start line, healthy and rested",
    },
    {
        "start": DELIRIOUS_WEST_DATE,
        "end": None,
        "name": "Race window",
        "objective": "Northcliffe → Albany, sub-70h goal (104h cutoff)",
        "exit_criteria": "Finish",
    },
]

# Race history — manually maintained (official results aren't in Strava; distances/
# elevations here are the race's own figures, not GPS-measured). Dates cross-checked
# against the matching Strava activity. Solo unofficial efforts (block runs) are
# deliberately excluded — this is race results only, not training efforts; the 48h
# block run already has its own "longest run" card on the dashboard.
RACE_HISTORY = [
    {
        "date": date(2025, 11, 28),
        "race": "Ultra-Trail Kosciuszko (UTK)",
        "distance_km": 161,
        "elevation_m": 4710,
        "time": "31:34:43",
        "placing": "117th overall",
        "notes": "Alpine — Kosciuszko National Park",
    },
    {
        "date": date(2025, 5, 16),
        "race": "Ultra-Trail Australia (UTA) Miler",
        "distance_km": 161.4,
        "elevation_m": 6510,
        "time": "35:15:42",
        "placing": "173rd overall",
        "notes": "Blue Mountains",
    },
    {
        "date": date(2024, 8, 31),
        "race": "Southern Sydney 24 Hour Ultra",
        "distance_km": 161.6,
        "elevation_m": None,
        "time": "24:00:00",
        "placing": "11th overall / 8th M",
        "notes": "Fixed-time event — distance covered in 24h is the result",
    },
    {
        "date": date(2024, 10, 26),
        "race": "Bondi to Manly Ultra",
        "distance_km": 78.4,
        "elevation_m": 1604,
        "time": "09:55:41",
        "placing": "245th overall",
        "notes": "",
    },
]

# Injuries — manually maintained (not derivable from Strava). Each entry:
#   date: when it happened
#   title: short label shown on the dashboard
#   detail: one-line context for the banner
#   status: "recovering" | "resolved" — only non-resolved entries render a banner
#   affects: subset of {"weight_training", "fall_risk"} — used to annotate
#            specific dashboard panels (e.g. weight training hint)
# Update status to "resolved" once cleared (by physio or self-assessment) rather
# than deleting the entry — keeps it in the brief's injury history.
INJURIES = [
    {
        "date": date(2026, 8, 20),
        "title": "Elbow dislocation (Milos, Greece)",
        "detail": "Reduced on-site, X-ray clear (no fracture). Recovering conservatively — "
        "rebuilding range of motion before reloading. Upper-body lifting paused; "
        "running tolerated if pain-free, but fall-risk terrain (technical trail) "
        "is being approached cautiously until ligament/soft-tissue healing is further along.",
        "status": "recovering",
        "affects": ["weight_training", "fall_risk"],
    },
]

# Weekly mileage target waypoints — (date, target_km); None = no target (period shown without target line)
WEEKLY_KM_TARGETS = [
    (date(2026, 7, 16), None),
    (date(2026, 8, 25), 40),
    (date(2026, 9, 8), 80),
    (date(2026, 10, 1), 100),
    (date(2026, 10, 19), 161),
    (date(2026, 10, 26), 161),
    (date(2026, 11, 20), 80),
    (date(2026, 11, 27), None),
    (date(2026, 12, 7), 50),
    (date(2026, 12, 21), 100),
    (date(2027, 1, 12), 161),
    (date(2027, 3, 16), 161),
    (date(2027, 3, 17), 80),
    (date(2027, 3, 24), 40),
]

# Load monitoring
SHOE_WEAR_THRESHOLD_KM = 800
ZONE_SNAPSHOT_INTERVAL_DAYS = 30
ACUTE_LOAD_DAYS = 7
CHRONIC_LOAD_DAYS = 28
UNSAFE_LOAD_RATIO = 1.5
ACR_SAFE_LOW = 0.8
ACR_SAFE_HIGH = 1.3
# A flat week-on-week mileage % threshold (previously 10%) was removed
# 2026-08-27 — it fired near-constantly for a high-mileage, by-feel
# ultrarunner whose week-to-week swings routinely blow past any fixed
# percentage, so it had stopped being a useful signal. ACR (above) already
# covers sustained load risk properly; elevation/back-to-back below are
# narrower, achilles/back-specific signals, not a blanket volume rule.
# Elevation and back-to-back thresholds are a judgment call (not sourced from the
# brief like ACR was) — flagging for review. Elevation spikes are noisier
# week-to-week than mileage, so the bar is higher; back-to-back is an absolute
# count since a handful of consecutive-day weeks is already a real
# achilles/back load signal regardless of trend.
ELEVATION_SPIKE_PCT = 0.40
B2B_SPIKE_INCREASE = 3

# Fitness/Fatigue/Form (CTL/ATL/TSB) — standard Coggan exponentially-weighted load
# model. Distinct from the ACR's simple rolling-average method above (different
# methodology, conventional time constants, not tuned to this athlete).
CTL_TIME_CONSTANT_DAYS = 42
ATL_TIME_CONSTANT_DAYS = 7
# Skips the sparse April–July 2019 activity stub before the multi-year gap to 2023 —
# an EWMA seeded there would just decay to ~0 across the gap anyway, but starting
# clean avoids computing years of meaningless daily values. Matches where the
# monthly-granularity views already effectively begin.
LOAD_TREND_START = date(2023, 1, 1)

# Run definitions
EASY_RUN_MIN_DISTANCE_KM = (
    5  # avg_heartrate <= Z2 max AND workout_type != 1 AND has_heartrate = TRUE
)
LONG_RUN_MIN_DISTANCE_KM = 21.1  # half marathon and above

# Night run cutoffs (hour of day, 24h clock)
NIGHT_RUN_MORNING_CUTOFF = 6
NIGHT_RUN_EVENING_CUTOFF = 20

# Sport types that have GPS — streams are fetched for these only
GPS_SPORT_TYPES = {
    "Run",
    "TrailRun",
    "Walk",
    "Hike",
    "Ride",
    "MountainBikeRide",
    "GravelRide",
    "EBikeRide",
    "VirtualRide",
    "VirtualRun",
    "Swim",
    "Kayaking",
    "Rowing",
    "Canoe",
    "StandUpPaddling",
    "Surfing",
    "NordicSki",
    "AlpineSki",
    "BackcountrySki",
    "Snowshoe",
}


def get_access_token() -> str:
    """Exchange refresh token for new access + refresh tokens. Writes new token to .env."""
    resp = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "refresh_token": os.environ["STRAVA_REFRESH_TOKEN"],  # always read current value
            "grant_type": "refresh_token",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    new_token = data["refresh_token"]
    _update_env("STRAVA_REFRESH_TOKEN", new_token)
    os.environ["STRAVA_REFRESH_TOKEN"] = new_token
    print("Refresh token rotated ✓")
    return data["access_token"]


def _update_env(key: str, value: str) -> None:
    lines = ENV_PATH.read_text().splitlines()
    updated = [f"{key}={value}" if l.startswith(f"{key}=") else l for l in lines]
    ENV_PATH.write_text("\n".join(updated))
