"""Backfills average_temp for activities whose device didn't report temperature
(e.g. the Forerunner 265 doesn't expose one via Strava's API) using Open-Meteo's
free historical weather API — keyed on each run's GPS start point and UTC time.
Estimated values are flagged via temp_estimated so they stay distinguishable
from real device readings."""

import time
from collections import defaultdict
from datetime import datetime, timedelta

import requests

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def _fetch_day_hourly_temps(lat: float, lng: float, date_str: str) -> dict:
    """One call returns all 24 hourly UTC temperatures for that date at that point."""
    resp = requests.get(
        ARCHIVE_URL,
        params={
            "latitude": lat,
            "longitude": lng,
            "start_date": date_str,
            "end_date": date_str,
            "hourly": "temperature_2m",
            "timezone": "UTC",
        },
        timeout=15,
    )
    resp.raise_for_status()
    hourly = resp.json().get("hourly", {})
    return dict(zip(hourly.get("time", []), hourly.get("temperature_2m", [])))


def enrich_missing_temperatures(conn) -> int:
    """Fills average_temp for outdoor activities missing it, at least 2 days old
    (Open-Meteo's archive has a short reanalysis delay). Returns rows filled."""
    rows = conn.execute("""
        SELECT id, start_lat, start_lng, start_date_utc, elapsed_time
        FROM activities
        WHERE average_temp IS NULL AND start_lat IS NOT NULL AND start_lng IS NOT NULL
              AND trainer = 0 AND manual = 0
              AND start_date_utc <= datetime('now', '-2 days')
    """).fetchall()
    if not rows:
        return 0

    by_date = defaultdict(list)
    for r in rows:
        by_date[r["start_date_utc"][:10]].append(r)

    filled = 0
    for day, day_rows in by_date.items():
        lat = sum(r["start_lat"] for r in day_rows) / len(day_rows)
        lng = sum(r["start_lng"] for r in day_rows) / len(day_rows)
        try:
            hourly_temps = _fetch_day_hourly_temps(lat, lng, day)
        except requests.RequestException as e:
            print(f"Weather lookup failed for {day}: {e}")
            continue
        if not hourly_temps:
            continue

        for r in day_rows:
            start_dt = datetime.fromisoformat(r["start_date_utc"].replace("Z", "+00:00"))
            end_dt = start_dt + timedelta(seconds=r["elapsed_time"] or 0)
            hour = start_dt.replace(minute=0, second=0, microsecond=0)
            temps = []
            while hour <= end_dt:
                v = hourly_temps.get(hour.strftime("%Y-%m-%dT%H:00"))
                if v is not None:
                    temps.append(v)
                hour += timedelta(hours=1)
            if not temps:
                continue
            conn.execute(
                "UPDATE activities SET average_temp = ?, temp_estimated = 1 WHERE id = ?",
                (round(sum(temps) / len(temps), 1), r["id"]),
            )
            filled += 1
        time.sleep(0.2)  # stay polite to the free API

    conn.commit()
    return filled
