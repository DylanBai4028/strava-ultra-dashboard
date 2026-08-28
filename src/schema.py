import sqlite3

from config import DATA_DIR, DB_PATH

MIGRATIONS = {
    1: """
    CREATE TABLE IF NOT EXISTS activities (
        id                      TEXT PRIMARY KEY,
        name                    TEXT,
        description             TEXT,
        sport_type              TEXT,
        workout_type            INTEGER,
        start_local             TEXT,
        start_date_utc          TEXT,
        timezone                TEXT,
        utc_offset              REAL,
        gear_id                 TEXT,
        external_id             TEXT,
        device_name             TEXT,
        distance                REAL,
        moving_time             INTEGER,
        elapsed_time            INTEGER,
        elevation_gain          REAL,
        elev_high               REAL,
        elev_low                REAL,
        avg_speed               REAL,
        max_speed               REAL,
        avg_cadence             REAL,
        avg_heartrate           REAL,
        max_heartrate           REAL,
        average_temp            REAL,
        average_watts           REAL,
        weighted_average_watts  REAL,
        kilojoules              REAL,
        relative_effort         INTEGER,
        total_calories          INTEGER,
        kudos_count             INTEGER,
        achievement_count       INTEGER,
        pr_count                INTEGER,
        photo_count             INTEGER,
        athlete_count           INTEGER,
        has_heartrate           INTEGER,
        perceived_exertion      REAL,
        trainer                 INTEGER,
        commute                 INTEGER,
        manual                  INTEGER,
        private                 INTEGER,
        flagged                 INTEGER,
        upload_id               TEXT,
        start_lat               REAL,
        start_lng                REAL,
        end_lat                 REAL,
        end_lng                 REAL,
        location_city           TEXT,
        location_state          TEXT,
        location_country        TEXT,
        summary_polyline        TEXT,
        synced_at                TEXT
    );

    CREATE TABLE IF NOT EXISTS streams (
        activity_id      TEXT NOT NULL REFERENCES activities(id),
        time_offset      INTEGER NOT NULL,
        distance         REAL,
        altitude         REAL,
        velocity_smooth  REAL,
        heart_rate       INTEGER,
        cadence          INTEGER,
        grade_smooth     REAL,
        moving           INTEGER,
        temp             REAL,
        lat              REAL,
        lng              REAL,
        PRIMARY KEY (activity_id, time_offset)
    );

    CREATE TABLE IF NOT EXISTS splits (
        activity_id            TEXT NOT NULL REFERENCES activities(id),
        split                   INTEGER NOT NULL,
        distance                REAL,
        elapsed_time            INTEGER,
        moving_time             INTEGER,
        elevation_difference    REAL,
        avg_speed                REAL,
        avg_heartrate            REAL,
        avg_cadence              REAL,
        pace_zone                INTEGER,
        PRIMARY KEY (activity_id, split)
    );

    CREATE TABLE IF NOT EXISTS segments (
        id               TEXT PRIMARY KEY,
        name             TEXT,
        activity_type    TEXT,
        distance         REAL,
        avg_grade        REAL,
        max_grade        REAL,
        elev_high        REAL,
        elev_low         REAL,
        start_lat        REAL,
        start_lng        REAL,
        end_lat          REAL,
        end_lng          REAL,
        city             TEXT,
        state            TEXT,
        country          TEXT,
        climb_category   INTEGER,
        starred          INTEGER
    );

    CREATE TABLE IF NOT EXISTS segment_efforts (
        id             TEXT PRIMARY KEY,
        activity_id    TEXT NOT NULL REFERENCES activities(id),
        segment_id     TEXT REFERENCES segments(id),
        segment_name   TEXT,
        start_local    TEXT,
        elapsed_time   INTEGER,
        moving_time    INTEGER,
        distance       REAL,
        avg_cadence    REAL,
        avg_heartrate  REAL,
        max_heartrate  REAL,
        avg_watts      REAL,
        device_watts   INTEGER,
        kom_rank       INTEGER,
        pr_rank        INTEGER,
        hidden         INTEGER
    );

    CREATE TABLE IF NOT EXISTS best_efforts (
        id             TEXT PRIMARY KEY,
        activity_id    TEXT NOT NULL REFERENCES activities(id),
        name           TEXT,
        start_local    TEXT,
        elapsed_time   INTEGER,
        moving_time    INTEGER,
        distance       REAL,
        pr_rank        INTEGER
    );

    CREATE TABLE IF NOT EXISTS laps (
        id               TEXT PRIMARY KEY,
        activity_id      TEXT NOT NULL REFERENCES activities(id),
        lap_index        INTEGER,
        start_local      TEXT,
        elapsed_time     INTEGER,
        moving_time      INTEGER,
        distance         REAL,
        elevation_gain   REAL,
        avg_speed        REAL,
        avg_heartrate    REAL,
        avg_cadence      REAL,
        split            INTEGER
    );

    CREATE TABLE IF NOT EXISTS gear (
        id              TEXT PRIMARY KEY,
        gear_type       TEXT,
        name            TEXT,
        description     TEXT,
        brand           TEXT,
        model_name      TEXT,
        retired         INTEGER,
        primary_gear    INTEGER,
        total_distance  REAL,
        synced_at       TEXT
    );

    CREATE TABLE IF NOT EXISTS athlete_zones (
        snapshotted_at   TEXT PRIMARY KEY,
        hr_z1_min INTEGER, hr_z1_max INTEGER,
        hr_z2_min INTEGER, hr_z2_max INTEGER,
        hr_z3_min INTEGER, hr_z3_max INTEGER,
        hr_z4_min INTEGER, hr_z4_max INTEGER,
        hr_z5_min INTEGER, hr_z5_max INTEGER,
        run_z1_min REAL, run_z1_max REAL,
        run_z2_min REAL, run_z2_max REAL,
        run_z3_min REAL, run_z3_max REAL,
        run_z4_min REAL, run_z4_max REAL,
        run_z5_min REAL, run_z5_max REAL,
        hr_zone_source   TEXT,
        run_zone_source  TEXT
    );

    CREATE TABLE IF NOT EXISTS athlete_profile (
        id                       INTEGER PRIMARY KEY,
        first_name               TEXT,
        last_name                TEXT,
        weight                   REAL,
        measurement_preference   TEXT,
        gender                   TEXT,
        athlete_type             INTEGER,
        city                     TEXT,
        state                    TEXT,
        country                  TEXT,
        date_preference          TEXT,
        updated_at               TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_activities_start_local  ON activities(start_local);
    CREATE INDEX IF NOT EXISTS idx_activities_start_utc    ON activities(start_date_utc);
    CREATE INDEX IF NOT EXISTS idx_activities_sport_type   ON activities(sport_type);
    CREATE INDEX IF NOT EXISTS idx_activities_gear_id      ON activities(gear_id);
    CREATE INDEX IF NOT EXISTS idx_activities_workout_type ON activities(workout_type);
    CREATE INDEX IF NOT EXISTS idx_splits_activity_id      ON splits(activity_id);
    CREATE INDEX IF NOT EXISTS idx_segment_efforts_act_id  ON segment_efforts(activity_id);
    CREATE INDEX IF NOT EXISTS idx_best_efforts_act_id     ON best_efforts(activity_id);
    CREATE INDEX IF NOT EXISTS idx_laps_activity_id        ON laps(activity_id);
    """,
    2: """
    ALTER TABLE activities ADD COLUMN temp_estimated INTEGER;
    """,
    3: """
    ALTER TABLE activities ADD COLUMN location_suburb TEXT;
    """,
}


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)  # safe to re-run; creates data/ if absent

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")

    # Bootstrap schema_version before running any migrations
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)
    conn.commit()

    # Apply any unapplied migrations
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_version")}
    for version, sql in sorted(MIGRATIONS.items()):
        if version not in applied:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_version VALUES (?, datetime('now'))", (version,))
    conn.commit()
    conn.close()
    print("Schema initialised ✓")


if __name__ == "__main__":
    main()
