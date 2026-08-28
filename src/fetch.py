"""Incremental sync — pulls anything new since the last run: recent
activities, gear, athlete profile, and HR/pace zones, then backfills any
missing weather-estimated temperature. This is what a `strava-sync`-style
alias calls; run it before regenerating the dashboard to pick up new
activities without re-pulling your whole history (that's `backfill.py`'s
job, once)."""

import sys
from datetime import UTC, datetime

import requests

import config
import utils
import weather
from backfill import API_BASE, get_db_conn, process_activity, to_unix


def fetch_new_activities(headers, after_ts):
    """Strava always returns activities newest-first; paginate with `page` (not
    `before`) since `after` already bounds the older edge of the window."""
    all_activities = []
    page = 1
    while True:
        params = {"per_page": 200, "page": page}
        if after_ts is not None:
            params["after"] = after_ts
        resp = utils.strava_request(
            "GET", f"{API_BASE}/athlete/activities", headers=headers, params=params
        )
        batch = resp.json()
        if not batch:
            break
        all_activities.extend(batch)
        if len(batch) < 200:
            break
        page += 1
    return all_activities


def _sync_gear_item(conn, headers, summary_item: dict, gear_type: str) -> None:
    gear_id = summary_item.get("id")
    if not gear_id:
        return
    detail = utils.strava_request("GET", f"{API_BASE}/gear/{gear_id}", headers=headers).json()
    conn.execute(
        """
        INSERT OR REPLACE INTO gear (
            id, gear_type, name, description, brand, model_name, retired, primary_gear, total_distance, synced_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            str(gear_id),
            gear_type,
            detail.get("name"),
            detail.get("description"),
            detail.get("brand_name"),
            detail.get("model_name"),
            int(bool(detail.get("retired"))),
            int(bool(summary_item.get("primary"))),
            detail.get("distance"),
            datetime.now(UTC).isoformat(),
        ),
    )


def _sync_athlete_profile(conn, athlete: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO athlete_profile (
            id, first_name, last_name, weight, measurement_preference, gender,
            athlete_type, city, state, country, date_preference, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            athlete.get("id"),
            athlete.get("firstname"),
            athlete.get("lastname"),
            athlete.get("weight"),
            athlete.get("measurement_preference"),
            athlete.get("sex"),
            athlete.get("athlete_type"),
            athlete.get("city"),
            athlete.get("state"),
            athlete.get("country"),
            athlete.get("date_preference"),
            datetime.now(UTC).isoformat(),
        ),
    )


def sync_gear_and_profile(conn, headers) -> None:
    """A single GET /athlete call returns the profile plus summary shoes/bikes
    (id, name, distance only) — full gear detail (brand, model, retired) needs
    one GET /gear/{id} per item, since Strava has no bulk gear-detail endpoint."""
    athlete = utils.strava_request("GET", f"{API_BASE}/athlete", headers=headers).json()
    _sync_athlete_profile(conn, athlete)
    for item in athlete.get("shoes") or []:
        _sync_gear_item(conn, headers, item, "Shoe")
    for item in athlete.get("bikes") or []:
        _sync_gear_item(conn, headers, item, "Bike")


def _needs_zone_snapshot(conn) -> bool:
    row = conn.execute("SELECT MAX(snapshotted_at) FROM athlete_zones").fetchone()
    if row is None or row[0] is None:
        return True
    last = datetime.fromisoformat(row[0])
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (datetime.now(UTC) - last).days > config.ZONE_SNAPSHOT_INTERVAL_DAYS


def sync_zones(conn, headers) -> None:
    """Strava's public API only exposes HR (and power) zones via /athlete/zones —
    running pace zones are a web-app-only feature with no API equivalent, so
    run_z*_min/max are always stored as NULL. No dashboard metric currently
    depends on them (aerobic efficiency uses hr_z2_max only)."""
    if not _needs_zone_snapshot(conn):
        return
    data = utils.strava_request("GET", f"{API_BASE}/athlete/zones", headers=headers).json()
    hr = data.get("heart_rate") or {}
    hr_zones = hr.get("zones") or []
    hr_zone_source = "custom" if hr.get("custom_zones") else "default"

    def z(i, key):
        return hr_zones[i][key] if i < len(hr_zones) else None

    conn.execute(
        """
        INSERT INTO athlete_zones (
            snapshotted_at,
            hr_z1_min, hr_z1_max, hr_z2_min, hr_z2_max, hr_z3_min, hr_z3_max,
            hr_z4_min, hr_z4_max, hr_z5_min, hr_z5_max,
            run_z1_min, run_z1_max, run_z2_min, run_z2_max, run_z3_min, run_z3_max,
            run_z4_min, run_z4_max, run_z5_min, run_z5_max,
            hr_zone_source, run_zone_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            datetime.now(UTC).isoformat(),
            z(0, "min"),
            z(0, "max"),
            z(1, "min"),
            z(1, "max"),
            z(2, "min"),
            z(2, "max"),
            z(3, "min"),
            z(3, "max"),
            z(4, "min"),
            z(4, "max"),
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            hr_zone_source,
            None,
        ),
    )


def main():
    config.ensure_strava_credentials()
    conn = get_db_conn()
    count = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    if count == 0:
        print("DB is empty — run strava-backfill first")
        conn.close()
        return

    try:
        access_token = config.get_access_token()
    except requests.HTTPError:
        # get_access_token() only ever talks to Strava's OAuth token endpoint,
        # so any error there is an auth/credential problem by definition — no
        # need to discriminate status codes (a bad client_id returns 400, not
        # 401, which a narrower check used to miss entirely).
        utils.handle_auth_failure("sync")

    headers = {"Authorization": f"Bearer {access_token}"}
    new_activities = []
    try:
        max_utc = conn.execute("SELECT MAX(start_date_utc) FROM activities").fetchone()[0]
        after_ts = to_unix(max_utc) if max_utc else None

        new_activities = fetch_new_activities(headers, after_ts)
        for i, summary in enumerate(new_activities, 1):
            process_activity(conn, headers, summary["id"], i, len(new_activities))
        conn.commit()

        sync_gear_and_profile(conn, headers)
        sync_zones(conn, headers)
        conn.commit()
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            conn.close()
            utils.handle_auth_failure("sync")
        conn.close()
        utils.log_and_notify_failure(config.SYNC_LOG, "sync")
        sys.exit(1)
    except Exception:
        conn.close()
        utils.log_and_notify_failure(config.SYNC_LOG, "sync")
        sys.exit(1)

    try:
        filled = weather.enrich_missing_temperatures(conn)
        if filled:
            print(f"Weather enrichment: filled temperature for {filled} activities.")
    except Exception as e:
        print(f"Weather enrichment skipped due to error: {e}")

    msg = f"Sync complete. {len(new_activities)} new activities added."
    print(msg)
    with open(config.SYNC_LOG, "a") as f:
        f.write(f"{datetime.now().isoformat()} — {msg}\n")
    conn.close()


if __name__ == "__main__":
    main()
