"""One-time OAuth setup for the Strava pipeline. Run once to authorise API
access and write credentials to .env. Re-run only if the refresh token is
lost or scopes need to change."""

import http.server
import re
import socket
import urllib.parse
import webbrowser
from getpass import getpass
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent.parent
ENV_PATH = BASE_DIR / ".env"
REDIRECT_PORT = 8080
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}"
SCOPES = "read,activity:read_all,profile:read_all"

auth_code = None


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        auth_code = params.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        if auth_code:
            self.wfile.write(
                b"<html><body><h2>Authorised. You can close this tab and "
                b"return to the terminal.</h2></body></html>"
            )
        else:
            self.wfile.write(
                b"<html><body><h2>Authorisation failed or was denied. "
                b"Check the terminal.</h2></body></html>"
            )

    def log_message(self, format, *args):
        pass


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) != 0


def main():
    if not _port_available(REDIRECT_PORT):
        print(f"Port {REDIRECT_PORT} is in use — close whatever is using it and try again")
        raise SystemExit(1)

    client_id = input("Strava Client ID: ").strip()
    client_secret = getpass("Strava Client Secret: ").strip()

    auth_url = "https://www.strava.com/oauth/authorize?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "approval_prompt": "force",
            "scope": SCOPES,
        }
    )
    print(f"Opening browser for authorisation...\nIf it doesn't open, visit:\n{auth_url}")
    webbrowser.open(auth_url)

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), _RedirectHandler)
    server.timeout = 180
    server.handle_request()
    server.server_close()

    if auth_code is None:
        print("No authorisation code received (timed out or denied). Aborting.")
        raise SystemExit(1)

    resp = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": auth_code,
            "grant_type": "authorization_code",
        },
    )
    resp.raise_for_status()
    token_data = resp.json()

    # Google displays app passwords as "xxxx xxxx xxxx xxxx" for readability;
    # copying from the account page often carries those as non-breaking
    # spaces (U+00A0), not regular ones. .strip() only trims the ends, so it
    # misses the ones *between* groups — strip all whitespace anywhere, since
    # the real credential is a plain 16-character string with none at all.
    gmail_app_password = re.sub(
        r"\s+",
        "",
        getpass("Gmail App Password (for failure notifications, optional — press Enter to skip): "),
    )

    ENV_PATH.write_text(
        f"STRAVA_CLIENT_ID={client_id}\n"
        f"STRAVA_CLIENT_SECRET={client_secret}\n"
        f"STRAVA_REFRESH_TOKEN={token_data['refresh_token']}\n"
        f"GMAIL_APP_PASSWORD={gmail_app_password}\n"
    )
    ENV_PATH.chmod(0o600)

    print("Setup complete. Refresh token rotated ✓")


if __name__ == "__main__":
    main()
