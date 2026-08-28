"""One-time/idempotent backfill of location_city/state/country/suburb on `activities`.

Strava's API never sends these fields (always NULL) — see the note on
generate._cluster_locations. This reuses that same GPS-clustering approach
(group activities into clusters, reverse-geocode each cluster's centroid once)
at two granularities:

- City pass: coarse clusters (~100 km), zoom=10 -> location_city/state/country
- Suburb pass: fine clusters (~1.5 km), zoom=14 -> location_suburb

Safe to re-run: each pass only touches rows where its column IS NULL, so a
synced activity from an already-seen region just reuses that region's existing
label via its nearest cluster centroid.

Run manually: .venv/bin/python backfill_locations.py
"""

import time

from generate import _cluster_locations, _reverse_geocode_components, get_db_conn

SUBURB_CLUSTER_DEG = 0.015  # ~1.5 km — separates suburbs without splitting one park into many


def _geocode_clusters(clusters, zoom):
    """One Nominatim call per cluster centroid, 1 req/sec per usage policy."""
    results = []
    for i, c in enumerate(clusters):
        if i > 0:
            time.sleep(1.0)
        results.append(
            _reverse_geocode_components(c["sum_lat"] / c["n"], c["sum_lng"] / c["n"], zoom=zoom)
        )
    return results


def backfill_cities(conn):
    rows = conn.execute("""
        SELECT id, start_lat, start_lng FROM activities
        WHERE start_lat IS NOT NULL AND start_lng IS NOT NULL AND location_city IS NULL
    """).fetchall()
    if not rows:
        print("City pass: nothing to backfill.")
        return

    print(f"City pass: clustering {len(rows)} GPS-tagged activities...")
    clusters, assignments = _cluster_locations(rows)
    print(f"  {len(clusters)} cluster(s). Reverse-geocoding centroids...")
    components_by_cluster = _geocode_clusters(clusters, zoom=10)
    for i, (c, comp) in enumerate(zip(clusters, components_by_cluster)):
        label = f"{comp['city']}, {comp['country']}" if comp and comp["city"] else "(unresolved)"
        print(f"  Cluster {i}: {c['n']} activities -> {label}")

    updated = 0
    for row, cluster_idx in zip(rows, assignments):
        comp = components_by_cluster[cluster_idx]
        if comp is None:
            continue
        conn.execute(
            "UPDATE activities SET location_city = ?, location_state = ?, location_country = ? WHERE id = ?",
            (comp["city"], comp["state"], comp["country"], row["id"]),
        )
        updated += 1
    conn.commit()
    print(f"City pass complete. {updated}/{len(rows)} activities updated.")


def backfill_suburbs(conn):
    rows = conn.execute("""
        SELECT id, start_lat, start_lng FROM activities
        WHERE start_lat IS NOT NULL AND start_lng IS NOT NULL AND location_suburb IS NULL
    """).fetchall()
    if not rows:
        print("Suburb pass: nothing to backfill.")
        return

    print(f"Suburb pass: fine-clustering {len(rows)} GPS-tagged activities...")
    clusters, assignments = _cluster_locations(rows, threshold_deg=SUBURB_CLUSTER_DEG)
    print(f"  {len(clusters)} cluster(s). Reverse-geocoding centroids (~{len(clusters)}s)...")
    components_by_cluster = _geocode_clusters(clusters, zoom=14)

    updated = 0
    unresolved = 0
    for row, cluster_idx in zip(rows, assignments):
        comp = components_by_cluster[cluster_idx]
        if comp is None or not comp["suburb"]:
            unresolved += 1
            continue
        conn.execute(
            "UPDATE activities SET location_suburb = ? WHERE id = ?", (comp["suburb"], row["id"])
        )
        updated += 1
    conn.commit()
    print(
        f"Suburb pass complete. {updated}/{len(rows)} activities updated ({unresolved} unresolved)."
    )


def main():
    conn = get_db_conn()
    backfill_cities(conn)
    backfill_suburbs(conn)
    conn.close()


if __name__ == "__main__":
    main()
