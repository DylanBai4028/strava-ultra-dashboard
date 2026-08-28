import sqlite3
import sys
from datetime import UTC, datetime

import requests

import config
import utils

API_BASE = "https://www.strava.com/api/v3"


def get_db_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def load_checkpoint():
    if config.CHECKPOINT.exists():
        return int(config.CHECKPOINT.read_text().strip())
    return None


def save_checkpoint(ts: int) -> None:
    config.CHECKPOINT.write_text(str(ts))


def clear_checkpoint() -> None:
    if config.CHECKPOINT.exists():
        config.CHECKPOINT.unlink()


def to_unix(iso_str: str) -> int:
    return int(datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp())


def fetch_activity_list(headers, before):
    params = {"per_page": 200}
    if before is not None:
        params["before"] = before
    resp = utils.strava_request(
        "GET", f"{API_BASE}/athlete/activities", headers=headers, params=params
    )
    return resp.json()


def fetch_activity_detail(headers, activity_id):
    resp = utils.strava_request("GET", f"{API_BASE}/activities/{activity_id}", headers=headers)
    return resp.json()


def fetch_streams(headers, activity_id):
    keys = (
        "time,distance,altitude,velocity_smooth,heartrate,cadence,grade_smooth,moving,temp,latlng"
    )
    try:
        resp = utils.strava_request(
            "GET",
            f"{API_BASE}/activities/{activity_id}/streams",
            headers=headers,
            params={"keys": keys, "key_by_type": "true", "series_type": "time"},
        )
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return {}
        raise
    return resp.json()


def upsert_activity(conn, d: dict) -> str:
    map_obj = d.get("map") or {}
    start_latlng = d.get("start_latlng") or [None, None]
    end_latlng = d.get("end_latlng") or [None, None]
    row = {
        "id": str(d["id"]),
        "name": d.get("name"),
        "description": d.get("description"),
        "sport_type": d.get("sport_type") or d.get("type"),
        "workout_type": d.get("workout_type"),
        "start_local": d.get("start_date_local"),
        "start_date_utc": d.get("start_date"),
        "timezone": d.get("timezone"),
        "utc_offset": d.get("utc_offset"),
        "gear_id": str(d["gear_id"]) if d.get("gear_id") else None,
        "external_id": d.get("external_id"),
        "device_name": d.get("device_name"),
        "distance": d.get("distance"),
        "moving_time": d.get("moving_time"),
        "elapsed_time": d.get("elapsed_time"),
        "elevation_gain": d.get("total_elevation_gain"),
        "elev_high": d.get("elev_high"),
        "elev_low": d.get("elev_low"),
        "avg_speed": d.get("average_speed"),
        "max_speed": d.get("max_speed"),
        "avg_cadence": d.get("average_cadence"),
        "avg_heartrate": d.get("average_heartrate"),
        "max_heartrate": d.get("max_heartrate"),
        "average_temp": d.get("average_temp"),
        "average_watts": d.get("average_watts"),
        "weighted_average_watts": d.get("weighted_average_watts"),
        "kilojoules": d.get("kilojoules"),
        "relative_effort": d.get("suffer_score"),
        "total_calories": d.get("calories"),
        "kudos_count": d.get("kudos_count"),
        "achievement_count": d.get("achievement_count"),
        "pr_count": d.get("pr_count"),
        "photo_count": d.get("photo_count"),
        "athlete_count": d.get("athlete_count"),
        "has_heartrate": int(bool(d.get("has_heartrate"))),
        "perceived_exertion": d.get("perceived_exertion"),
        "trainer": int(bool(d.get("trainer"))),
        "commute": int(bool(d.get("commute"))),
        "manual": int(bool(d.get("manual"))),
        "private": int(bool(d.get("private"))),
        "flagged": int(bool(d.get("flagged"))),
        "upload_id": str(d["upload_id"]) if d.get("upload_id") else None,
        "start_lat": start_latlng[0],
        "start_lng": start_latlng[1],
        "end_lat": end_latlng[0],
        "end_lng": end_latlng[1],
        "location_city": d.get("location_city"),
        "location_state": d.get("location_state"),
        "location_country": d.get("location_country"),
        "summary_polyline": map_obj.get("summary_polyline"),
        "synced_at": datetime.now(UTC).isoformat(),
    }
    conn.execute(
        """
        INSERT OR REPLACE INTO activities (
            id, name, description, sport_type, workout_type, start_local, start_date_utc,
            timezone, utc_offset, gear_id, external_id, device_name, distance, moving_time,
            elapsed_time, elevation_gain, elev_high, elev_low, avg_speed, max_speed, avg_cadence,
            avg_heartrate, max_heartrate, average_temp, average_watts, weighted_average_watts,
            kilojoules, relative_effort, total_calories, kudos_count, achievement_count, pr_count,
            photo_count, athlete_count, has_heartrate, perceived_exertion, trainer, commute,
            manual, private, flagged, upload_id, start_lat, start_lng, end_lat, end_lng,
            location_city, location_state, location_country, summary_polyline, synced_at
        ) VALUES (
            :id, :name, :description, :sport_type, :workout_type, :start_local, :start_date_utc,
            :timezone, :utc_offset, :gear_id, :external_id, :device_name, :distance, :moving_time,
            :elapsed_time, :elevation_gain, :elev_high, :elev_low, :avg_speed, :max_speed, :avg_cadence,
            :avg_heartrate, :max_heartrate, :average_temp, :average_watts, :weighted_average_watts,
            :kilojoules, :relative_effort, :total_calories, :kudos_count, :achievement_count, :pr_count,
            :photo_count, :athlete_count, :has_heartrate, :perceived_exertion, :trainer, :commute,
            :manual, :private, :flagged, :upload_id, :start_lat, :start_lng, :end_lat, :end_lng,
            :location_city, :location_state, :location_country, :summary_polyline, :synced_at
        )
    """,
        row,
    )
    return row["id"]


def upsert_splits(conn, activity_id: str, d: dict) -> None:
    for s in d.get("splits_metric") or []:
        conn.execute(
            """
            INSERT OR REPLACE INTO splits (
                activity_id, split, distance, elapsed_time, moving_time,
                elevation_difference, avg_speed, avg_heartrate, avg_cadence, pace_zone
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                activity_id,
                s.get("split"),
                s.get("distance"),
                s.get("elapsed_time"),
                s.get("moving_time"),
                s.get("elevation_difference"),
                s.get("average_speed"),
                s.get("average_heartrate"),
                s.get("average_cadence"),
                s.get("pace_zone"),
            ),
        )


def upsert_laps(conn, activity_id: str, d: dict) -> None:
    for lap in d.get("laps") or []:
        conn.execute(
            """
            INSERT OR REPLACE INTO laps (
                id, activity_id, lap_index, start_local, elapsed_time, moving_time,
                distance, elevation_gain, avg_speed, avg_heartrate, avg_cadence, split
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                str(lap["id"]),
                activity_id,
                lap.get("lap_index"),
                lap.get("start_date_local"),
                lap.get("elapsed_time"),
                lap.get("moving_time"),
                lap.get("distance"),
                lap.get("total_elevation_gain"),
                lap.get("average_speed"),
                lap.get("average_heartrate"),
                lap.get("average_cadence"),
                lap.get("split"),
            ),
        )


def upsert_best_efforts(conn, activity_id: str, d: dict) -> None:
    for be in d.get("best_efforts") or []:
        conn.execute(
            """
            INSERT OR REPLACE INTO best_efforts (
                id, activity_id, name, start_local, elapsed_time, moving_time, distance, pr_rank
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                str(be["id"]),
                activity_id,
                be.get("name"),
                be.get("start_date_local"),
                be.get("elapsed_time"),
                be.get("moving_time"),
                be.get("distance"),
                be.get("pr_rank"),
            ),
        )


def upsert_segment_efforts(conn, activity_id: str, d: dict) -> None:
    for se in d.get("segment_efforts") or []:
        seg = se.get("segment") or {}
        seg_id = str(seg["id"]) if seg.get("id") is not None else None
        seg_start = seg.get("start_latlng") or [None, None]
        seg_end = seg.get("end_latlng") or [None, None]
        if seg_id is not None:
            # Insert segment (parent) before the segment_effort that references it
            conn.execute(
                """
                INSERT OR REPLACE INTO segments (
                    id, name, activity_type, distance, avg_grade, max_grade, elev_high, elev_low,
                    start_lat, start_lng, end_lat, end_lng, city, state, country, climb_category, starred
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    seg_id,
                    seg.get("name"),
                    seg.get("activity_type"),
                    seg.get("distance"),
                    seg.get("average_grade"),
                    seg.get("maximum_grade"),
                    seg.get("elevation_high"),
                    seg.get("elevation_low"),
                    seg_start[0],
                    seg_start[1],
                    seg_end[0],
                    seg_end[1],
                    seg.get("city"),
                    seg.get("state"),
                    seg.get("country"),
                    seg.get("climb_category"),
                    int(bool(seg.get("starred"))),
                ),
            )
        conn.execute(
            """
            INSERT OR REPLACE INTO segment_efforts (
                id, activity_id, segment_id, segment_name, start_local, elapsed_time, moving_time,
                distance, avg_cadence, avg_heartrate, max_heartrate, avg_watts, device_watts,
                kom_rank, pr_rank, hidden
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                str(se["id"]),
                activity_id,
                seg_id,
                se.get("name"),
                se.get("start_date_local"),
                se.get("elapsed_time"),
                se.get("moving_time"),
                se.get("distance"),
                se.get("average_cadence"),
                se.get("average_heartrate"),
                se.get("max_heartrate"),
                se.get("average_watts"),
                int(bool(se.get("device_watts"))),
                se.get("kom_rank"),
                se.get("pr_rank"),
                int(bool(se.get("hidden"))),
            ),
        )


def upsert_streams(conn, activity_id: str, streams: dict) -> None:
    if not streams:
        return
    time_arr = (streams.get("time") or {}).get("data") or []
    n = len(time_arr)
    if n == 0:
        return

    def series(key):
        data = (streams.get(key) or {}).get("data") or []
        return data + [None] * (n - len(data))

    distance = series("distance")
    altitude = series("altitude")
    velocity = series("velocity_smooth")
    heartrate = series("heartrate")
    cadence = series("cadence")
    grade = series("grade_smooth")
    moving = series("moving")
    temp = series("temp")
    latlng = series("latlng")

    rows = []
    for i in range(n):
        ll = latlng[i] if latlng[i] else [None, None]
        rows.append(
            (
                activity_id,
                time_arr[i],
                distance[i],
                altitude[i],
                velocity[i],
                heartrate[i],
                cadence[i],
                grade[i],
                (int(bool(moving[i])) if moving[i] is not None else None),
                temp[i],
                ll[0],
                ll[1],
            )
        )
    conn.executemany(
        """
        INSERT OR IGNORE INTO streams (
            activity_id, time_offset, distance, altitude, velocity_smooth,
            heart_rate, cadence, grade_smooth, moving, temp, lat, lng
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        rows,
    )


def process_activity(conn, headers, activity_id, index: int, total: int) -> dict:
    detail = fetch_activity_detail(headers, activity_id)
    aid = upsert_activity(conn, detail)
    upsert_splits(conn, aid, detail)
    upsert_laps(conn, aid, detail)
    upsert_best_efforts(conn, aid, detail)
    upsert_segment_efforts(conn, aid, detail)

    sport_type = detail.get("sport_type") or detail.get("type")
    is_manual = bool(detail.get("manual"))
    streams_tag = ""
    if sport_type in config.GPS_SPORT_TYPES and not is_manual:
        streams = fetch_streams(headers, activity_id)
        upsert_streams(conn, aid, streams)
    else:
        streams_tag = " [no streams]"

    dist_km = (detail.get("distance") or 0) / 1000
    date_str = (detail.get("start_date_local") or "")[:10]
    print(
        f"  [{index}/{total}] {detail.get('name', '(unnamed)')} — {date_str} ({dist_km:.1f} km){streams_tag} ✓"
    )
    return detail


def main():
    try:
        access_token = config.get_access_token()
    except requests.HTTPError:
        # get_access_token() only ever talks to Strava's OAuth token endpoint,
        # so any error there is an auth/credential problem by definition — no
        # need to discriminate status codes (a bad client_id returns 400, not
        # 401, which a narrower check used to miss entirely).
        utils.handle_auth_failure("backfill")

    headers = {"Authorization": f"Bearer {access_token}"}
    conn = get_db_conn()

    before = load_checkpoint()
    start_label = (
        datetime.fromtimestamp(before, tz=UTC).date() if before else "from beginning"
    )
    print(f"Starting backfill from checkpoint: {start_label}")

    total_processed = 0
    page_num = 1
    try:
        while True:
            activities = fetch_activity_list(headers, before)
            if not activities:
                break
            print(f"Page {page_num}: fetching up to {len(activities)} activities...")
            oldest_ts = None
            for i, summary in enumerate(activities, 1):
                detail = process_activity(conn, headers, summary["id"], i, len(activities))
                ts = to_unix(detail["start_date"])
                if oldest_ts is None or ts < oldest_ts:
                    oldest_ts = ts
                total_processed += 1
            conn.commit()
            save_checkpoint(oldest_ts - 1)
            print(
                f"Page {page_num} complete. {len(activities)} activities processed. Checkpoint saved."
            )
            if len(activities) < 200:
                break
            before = oldest_ts - 1
            page_num += 1
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            conn.close()
            utils.handle_auth_failure("backfill")
        conn.close()
        utils.log_and_notify_failure(config.BACKFILL_LOG, "backfill")
        sys.exit(1)
    except Exception:
        conn.close()
        utils.log_and_notify_failure(config.BACKFILL_LOG, "backfill")
        sys.exit(1)

    clear_checkpoint()
    conn.close()
    print(f"Backfill complete. {total_processed} activities processed. All data stored.")


if __name__ == "__main__":
    main()
