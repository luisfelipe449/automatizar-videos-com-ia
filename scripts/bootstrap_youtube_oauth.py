"""One-shot helper to obtain a YouTube Data API v3 refresh token.

Usage:
    # Option A — use a downloaded client_secret.json (recommended):
    python scripts/bootstrap_youtube_oauth.py \\
        --client-secrets path/to/client_secret.json

    # Option B — read YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET from .env:
    python scripts/bootstrap_youtube_oauth.py

What it does:
    1. Starts a local OAuth2 consent flow (opens your browser).
    2. Asks for the `youtube.upload` scope only.
    3. Prints the resulting refresh_token to stdout so you can paste it
       into .env as YOUTUBE_REFRESH_TOKEN=<value>.

Notes:
    - This script is intentionally runnable as a plain script (no package import).
    - `access_type='offline'` + `prompt='consent'` are required so Google
      actually issues a refresh_token instead of just an access_token.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Load .env from repo root so Option B works without extra setup.
try:
    from dotenv import load_dotenv

    _REPO_ROOT = Path(__file__).resolve().parent.parent
    load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    # dotenv is a runtime dep; if missing the script still works via CLI flags.
    pass

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError as exc:  # pragma: no cover - import-time guard
    print(
        "ERROR: google-auth-oauthlib is not installed. "
        "Run: pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _build_client_config_from_env() -> dict:
    """Construct an InstalledAppFlow client config dict from env vars."""
    client_id = os.getenv("YOUTUBE_CLIENT_ID", "").strip()
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise SystemExit(
            "ERROR: YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET must be set in .env, "
            "or pass --client-secrets path/to/client_secret.json."
        )
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bootstrap_youtube_oauth",
        description=(
            "One-shot OAuth2 flow to obtain a YouTube refresh_token. "
            "Opens a browser, requests the youtube.upload scope, and prints "
            "the refresh_token for you to paste into .env."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/bootstrap_youtube_oauth.py --client-secrets client_secret.json\n"
            "  python scripts/bootstrap_youtube_oauth.py  # reads client id/secret from .env\n"
        ),
    )
    parser.add_argument(
        "--client-secrets",
        metavar="PATH",
        default=None,
        help=(
            "Path to the OAuth client_secret.json downloaded from Google Cloud Console. "
            "If omitted, YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET are read from .env."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Local server port for the OAuth redirect (0 = auto-pick). Default: 0.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    if args.client_secrets:
        secrets_path = Path(args.client_secrets).expanduser().resolve()
        if not secrets_path.is_file():
            print(f"ERROR: client secrets file not found: {secrets_path}", file=sys.stderr)
            return 1
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
    else:
        client_config = _build_client_config_from_env()
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)

    # access_type='offline' + prompt='consent' are what actually force Google
    # to hand back a refresh_token (otherwise you may get only an access_token).
    creds = flow.run_local_server(
        port=args.port,
        access_type="offline",
        prompt="consent",
        open_browser=True,
    )

    refresh_token = getattr(creds, "refresh_token", None)
    if not refresh_token:
        print(
            "ERROR: no refresh_token returned. This usually means the account has "
            "already granted consent — revoke it at https://myaccount.google.com/permissions "
            "and re-run this script.",
            file=sys.stderr,
        )
        return 2

    print()
    print("=" * 72)
    print("SUCCESS — copy the line below into your .env file:")
    print("=" * 72)
    print(f"YOUTUBE_REFRESH_TOKEN={refresh_token}")
    print("=" * 72)
    print()
    print("Scopes granted:", json.dumps(list(creds.scopes or SCOPES)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
