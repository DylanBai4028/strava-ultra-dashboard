# Strava Ultra Dashboard

A local pipeline that turns your Strava training history into a self-hosted
training dashboard, built for ultramarathon training: load monitoring
(acute:chronic ratio, CTL/ATL/TSB), race-readiness tracking, HR zone
distribution, elevation and pacing trends, and an auto-generated "Coach's
Note" that reads those numbers back to you in plain language.

**[See it live →](https://claude.ai/code/artifact/890ffba1-1a26-458b-862a-697c44d66790)**
— a real, currently-updating example: my own training toward a 200-mile
race in March 2027.

![Dashboard — Summary tab](docs/images/summary.png)
![Dashboard — Load & Safety tab](docs/images/load-safety.png)

## Why this exists

Strava's own analytics are thin, and third-party training-load tools are
either built for cyclists (power-meter-first) or gate the interesting
metrics behind a subscription. This pulls your full activity history via
the Strava API into a local SQLite database, then generates a single
self-contained HTML dashboard from it — no server, no account, no
recurring cost, and your data never leaves your machine unless you choose
to publish the output somewhere.

Everything here — the local-first pipeline, the SQLite schema, the load
model (ACR, CTL/ATL/TSB), the race-readiness framing, the Coach's Note —
is built around ultramarathon training specifically, because that's my
sport and this is my own dashboard. But none of those *principles* are
running-specific: tracking training load, comparing current fitness
against past efforts, flagging when volume is ramping unsafely, and
surfacing trends instead of raw numbers are useful for any endurance
sport with enough history in Strava — cycling, triathlon, swimming,
rowing, whatever you log there. The pipeline and dashboard mechanics
(fetching, storing, rendering) would carry over largely unchanged; what's
specific to running lives in `src/config.py` (thresholds, zones, race
framing) and parts of `src/generate.py` (the metrics computed from
`sport_type`). Adapting it for another sport means rewriting those parts
around your own sport's numbers, not starting from scratch.

## Try it in 60 seconds (no Strava account needed)

The repo ships a synthetic data generator so you can see the whole thing
work before connecting anything real:

```bash
git clone https://github.com/DylanBai4028/strava-ultra-dashboard.git
cd strava-ultra-dashboard
uv sync
uv run python src/schema.py                    # creates the (empty) database
uv run python scripts/generate_sample_data.py   # fills it with ~15 months of fake training data
uv run python src/generate.py                   # builds data/dashboard.html
open data/dashboard.html
```

That's the whole pipeline. The synthetic data is fabricated — random but
plausible running + weight-training sessions with a realistic
build/step-back weekly pattern — generated fresh each time you run the
script, from a fixed random seed (so it's reproducible, not different
every run). See [docs/schema.md](docs/schema.md) for exactly what it
generates and why.

## Using it with your own Strava data

> **Requires a Strava subscription.** Strava requires a paid subscription
> to create an API application — free accounts can't generate the
> Client ID/Secret this pipeline needs. See
> [Strava's API getting-started guide](https://developers.strava.com/docs/getting-started/)
> for details. (The synthetic-data quickstart above doesn't need this —
> only connecting your own account does.)

1. **Register a Strava API app** — go to
   [strava.com/settings/api](https://www.strava.com/settings/api), create
   an app (any name/website works, "Authorization Callback Domain" should
   be `localhost`). Note the Client ID and Client Secret.
2. **Run the auth flow**:
   ```bash
   uv run python src/auth.py
   ```
   This walks you through Strava's OAuth consent screen in your browser,
   then writes `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, and
   `STRAVA_REFRESH_TOKEN` to a new `.env` file (`chmod 600`, kept out of
   git — see `.env.example` for the format). It'll also ask for an
   optional Gmail App Password, used only to email you if a sync fails.
3. **Initialise the database** (if you haven't already):
   ```bash
   uv run python src/schema.py
   ```
4. **Pull your full history** (one-time; can take a while on a large account):
   ```bash
   uv run python src/backfill.py
   ```
5. **Generate the dashboard**:
   ```bash
   uv run python src/generate.py
   open data/dashboard.html
   ```

For ongoing use, `src/fetch.py` pulls anything new since your last sync
(gear, profile, HR zones, recent activities) — run it before regenerating
the dashboard each time:

```bash
uv run python src/fetch.py
uv run python src/generate.py
```

`src/backfill_locations.py` is a separate, idempotent one-off — it fills
in `location_city`/`location_state`/`location_country`/`location_suburb`
by reverse-geocoding each activity's GPS coordinates in two passes (city,
then suburb), since Strava's API never returns these fields itself. Run
it manually after a sync brings in activities from a genuinely new
region; day-to-day syncing doesn't need it.

## What's in `src/config.py`

Race dates, training phases, past-race results, injury history, and
weekly mileage targets all live in `src/config.py` as plain Python
constants — and they're **mine**, not generic placeholders. I've left
them in as a fully worked example (they're what makes the Coach's Note
and race-readiness panels say anything specific at all), but if you're
running this against your own Strava data, the dashboard will still show
*my* race calendar and targets until you edit those constants yourself.
There's no separate "your data" vs "my config" split — it's one file,
meant to be forked and rewritten for your own race.

## Data model

See [docs/schema.md](docs/schema.md) for the full table-by-table
breakdown of `data/strava.db` — what each table holds and how they relate.

## Project layout

```
src/
  auth.py               one-time OAuth setup
  schema.py             creates/migrates the SQLite database
  backfill.py            one-time full-history pull
  backfill_locations.py  one-off reverse-geocoding pass (city/suburb)
  fetch.py               incremental sync — new activities, gear, zones
  weather.py              backfills temperature Strava didn't record
  generate.py             builds data/dashboard.html from the database
  config.py               race calendar, targets, thresholds (see above)
  utils.py                shared helpers, failure-notification email
  templates/dashboard.html  the Jinja2 dashboard template
scripts/
  generate_sample_data.py  synthetic data generator (see Quickstart)
docs/
  schema.md                data model reference
data/                       gitignored — your database and generated dashboard live here
```

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) for dependency management
- A **Strava account with an active subscription**, only if you want to use
  your own data — a paid plan is required to create the API app this
  pipeline authenticates with (see the callout above). Not needed for the
  synthetic-data quickstart.

## Built with Claude Code

I'll be upfront about this: the whole thing — the pipeline, the schema
design, the load-monitoring model, the dashboard itself, and the analysis
behind what to visualise and how — was built by vibe-coding with
[Claude Code](https://claude.com/claude-code). I described what I wanted
(a real training dashboard, not a toy) and iterated on it with Claude over
a series of sessions, reviewing and directing every change rather than
accepting things blind — but Claude wrote the large majority of the code
and did the data analysis behind most of the design decisions.
If you have suggestions for how to make it better — a chart that's
misleading, a metric that's missing, code that could be cleaner — I'd
genuinely like to hear it. Good or bad, feedback is welcome; open an issue
or a PR.

### How I run it day to day

None of this is required to use the project — `generate.py` produces a
complete, self-contained `dashboard.html` you can open directly or host
anywhere that serves static files. This is just how *I* run it, also via
Claude Code:

- **Refreshing:** two shell aliases, `strava-sync` (runs `fetch.py`) and
  `strava-dashboard` (runs `generate.py` and opens the result), chain the
  two steps I'd otherwise type by hand.
- **Publishing:** I keep a live copy of my own dashboard published as a
  [Claude Artifact](https://claude.ai/code/artifact/890ffba1-1a26-458b-862a-697c44d66790)
  so I can check it from my phone without a server. A third alias,
  `strava-publish`, runs the sync + regenerate steps and copies a
  ready-to-send prompt to my clipboard — I paste it into a Claude Code
  session and it republishes the Artifact at the same URL. The paste step
  can't be fully automated away: Claude Code's non-interactive mode
  (`claude -p`) can publish Artifacts, but caps them at 256KB, and my
  dashboard runs larger than that (mostly embedded fonts and data) — only
  an interactive session can publish something that size. So the
  automation gets everything ready in one command; only the last "hit
  enter" step is manual.

This is a personal convenience layer, not something the pipeline depends
on — if you're running this yourself, `generate.py` → `open
data/dashboard.html` is the whole loop.

## License

[MIT](LICENSE) — do whatever you'd like with this.
