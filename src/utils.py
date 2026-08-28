"""Shared helpers used across the pipeline: `strava_request()` wraps every
Strava API call with rate-limit awareness and retry logic (429/5xx/network
errors), and `notify_failure()`/`log_and_notify_failure()`/
`handle_auth_failure()` are what `fetch.py`/`backfill.py` call on error to
log the failure and optionally email `NOTIFICATION_EMAIL`."""

import smtplib
import sys
import time
import traceback
from datetime import date, datetime
from email.mime.text import MIMEText

import requests

from config import GMAIL_APP_PASSWORD, NOTIFICATION_EMAIL


def strava_request(method: str, url: str, headers: dict, **kwargs) -> requests.Response:
    """Make a Strava API request. Handles 429 throttling, 5xx and network retries.
    Reads both the overall and read-specific rate-limit headers and sleeps before
    either 15-min cap. 429s retry with their own budget (up to ~20min of waiting)
    separate from the tighter 3-retry budget for 5xx/network errors, since a 429
    is expected and recoverable (the 15-min window resetting), not a fault."""
    backoff = [5, 15, 45]
    fault_attempt = 0
    rate_limit_attempt = 0
    while True:
        try:
            resp = requests.request(method, url, headers=headers, **kwargs)
        except requests.ConnectionError:
            if fault_attempt < 3:
                time.sleep(backoff[fault_attempt])
                fault_attempt += 1
                continue
            raise

        _check_rate_limit(resp)

        if resp.status_code == 429:
            if rate_limit_attempt >= 20:
                raise RuntimeError(
                    f"Still rate limited after {rate_limit_attempt} retries: {method} {url}"
                )
            wait = int(resp.headers.get("Retry-After", 60))
            print(f"Rate limited. Sleeping {wait}s...")
            time.sleep(wait)
            rate_limit_attempt += 1
            continue

        if resp.status_code in (500, 502, 503, 504) and fault_attempt < 3:
            time.sleep(backoff[fault_attempt])
            fault_attempt += 1
            continue

        resp.raise_for_status()
        return resp


def _check_rate_limit(resp: requests.Response) -> None:
    """Print rate limit status; sleep if approaching either 15-min cap.
    Strava enforces overall (X-RateLimit-*) and read-specific (X-ReadRateLimit-*)
    limits separately — almost all our calls are reads, so the read cap binds first."""
    for usage_key, limit_key, label in (
        ("X-RateLimit-Usage", "X-RateLimit-Limit", "overall"),
        ("X-ReadRateLimit-Usage", "X-ReadRateLimit-Limit", "read"),
    ):
        usage_header = resp.headers.get(usage_key, "")
        limit_header = resp.headers.get(limit_key, "")
        if not usage_header or not limit_header:
            continue
        used_15, used_day = (int(x) for x in usage_header.split(","))
        cap_15, cap_day = (int(x) for x in limit_header.split(","))
        print(f"Rate limit ({label}): {used_15}/{cap_15} (15min) · {used_day}/{cap_day} (day)")
        if used_15 >= cap_15 - 5:
            print(f"Approaching {label} 15-min rate limit. Sleeping 60s...")
            time.sleep(60)


def notify_failure(subject: str, body: str) -> None:
    """Send one failure notification email. Skips silently if no password configured.
    Logs and continues if sending fails — never suppresses the original error."""
    if not GMAIL_APP_PASSWORD:
        print("Email notification skipped — GMAIL_APP_PASSWORD not configured.")
        return
    try:
        msg = MIMEText(body, _charset="utf-8")
        msg["Subject"] = subject
        msg["From"] = NOTIFICATION_EMAIL
        msg["To"] = NOTIFICATION_EMAIL
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.starttls()
            smtp.login(NOTIFICATION_EMAIL, GMAIL_APP_PASSWORD)
            smtp.send_message(msg)
        print("Failure notification sent.")
    except Exception as e:
        print(f"Failed to send notification email: {e}")


def log_and_notify_failure(log_path, context: str) -> None:
    """Log the currently-handled exception to log_path and send a failure
    notification email. Must be called from inside an `except` block — reads
    the ambient exception via traceback.format_exc() rather than taking one
    as an argument, since the caller's exception object carries no extra
    information format_exc() doesn't already have."""
    tb = traceback.format_exc()
    with open(log_path, "a") as f:
        f.write(f"\n--- {datetime.now().isoformat()} ---\n{tb}\n")
    notify_failure(
        f"Strava {context} FAILED — {date.today().isoformat()}",
        f"{context.capitalize()} failed with error:\n\n{tb}",
    )


def handle_auth_failure(context: str) -> None:
    print("AUTH FAILED — re-run auth.py")
    notify_failure(
        f"Strava {context} FAILED — {date.today().isoformat()}",
        "Authentication failed. Re-run auth.py to re-authorise.",
    )
    sys.exit(1)
