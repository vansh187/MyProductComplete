"""
One-time Google Drive login for the market data recorder.

Run on a machine with a browser:
    pip install google-auth-oauthlib
    python scripts/recorder_drive_login.py --client-secret path/to/client_secret.json \
        --token ~/.primepip/drive_token.json

Then copy the token file to the VM, to the path in RECORDER_DRIVE_TOKEN_FILE
(default ~/.primepip/drive_token.json), and chmod 600 it. Never commit it.
The OAuth app must be "In production" (not "Testing"), or the token stops
working after 7 days.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from service.marketRecorder.driveClient import SCOPES  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client-secret", required=True, help="OAuth client JSON (Desktop app) from Google Cloud Console")
    parser.add_argument("--token", default=str(Path.home() / ".primepip" / "drive_token.json"), help="where to write the token")
    parser.add_argument("--port", type=int, default=0,
                        help="local port for Google's redirect: 0 (any) for a Desktop app client; for a Web "
                             "application client use the port of its registered redirect URI, e.g. 8080 for "
                             "http://localhost:8080/")
    args = parser.parse_args()

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(args.client_secret, SCOPES)
    credentials = flow.run_local_server(port=args.port, access_type="offline", prompt="consent")
    token_path = Path(args.token).expanduser()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    try:
        token_path.chmod(0o600)
    except OSError:
        pass
    from googleapiclient.discovery import build

    account = build("drive", "v3", credentials=credentials, cache_discovery=False).about().get(
        fields="user(emailAddress)").execute().get("user", {}).get("emailAddress")
    print(f"Token written to {token_path} for Google account {account}")
    print(f"Set RECORDER_DRIVE_ACCOUNT={account} in the VM's .env so uploads only ever go to this account.")
    if not credentials.refresh_token:
        print("WARNING: no refresh token returned - revoke the app's access in your Google account and run again.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Login failed: {exc!r}")
        sys.exit(1)
