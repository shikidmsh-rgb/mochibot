"""One-time Oura OAuth2 authorization script.

Run this locally to authorize your Oura Ring and get the tokens needed
for MochiBot's Oura integration.

Setup:
  1. Go to https://cloud.ouraring.com/v2/docs and create an application
     - Set redirect URI to: http://localhost:8080/oura/callback
  2. Copy your Client ID and Client Secret
  3. Run: python oura_auth.py
  4. Follow the prompts — browser will open for Oura login
  5. Tokens will be printed and optionally written to .env

Note: Oura does NOT support Personal Access Tokens.
      OAuth2 is the only authentication method.
"""

import http.server
import json
import os
import sys
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

REDIRECT_URI = "http://localhost:8080/oura/callback"
AUTH_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"
SCOPES = "personal daily sleep heartrate workout session"


class OuraCallbackHandler(http.server.BaseHTTPRequestHandler):
    """Handle the OAuth callback from Oura."""

    auth_code = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/oura/callback":
            params = urllib.parse.parse_qs(parsed.query)
            if "code" in params:
                OuraCallbackHandler.auth_code = params["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h1>&#10004; Oura authorized!</h1>"
                    b"<p>You can close this tab and go back to terminal.</p></body></html>"
                )
            elif "error" in params:
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                error = params.get("error", ["unknown"])[0]
                self.wfile.write(f"<html><body><h1>Error: {error}</h1></body></html>".encode())
            else:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass


def exchange_code_for_token(code: str, client_id: str, client_secret: str) -> dict:
    """Exchange authorization code for access + refresh tokens."""
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()

    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def update_env_file(env_path: Path, updates: dict[str, str]):
    """Update or append key=value pairs in a .env file."""
    if env_path.exists():
        lines = env_path.read_text().splitlines(keepends=True)
    else:
        lines = []

    found = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip().lstrip("# ")
        if key in updates:
            lines[i] = f"{key}={updates[key]}\n"
            found.add(key)

    for key, val in updates.items():
        if key not in found:
            lines.append(f"{key}={val}\n")

    env_path.write_text("".join(lines))


def main():
    print("=" * 60)
    print("🍡 MochiBot × Oura Ring — OAuth Authorization")
    print("=" * 60)
    print()
    print("Before starting, you need an Oura developer app:")
    print("  1. Go to https://cloud.ouraring.com/v2/docs")
    print("  2. Create an application")
    print(f"  3. Set redirect URI to: {REDIRECT_URI}")
    print("  4. Copy your Client ID and Client Secret")
    print()

    # Check if already in .env
    env_path = Path(__file__).resolve().parent / ".env"
    client_id = os.getenv("OURA_CLIENT_ID", "")
    client_secret = os.getenv("OURA_CLIENT_SECRET", "")

    if client_id and client_secret:
        print(f"Found existing credentials in environment:")
        print(f"  Client ID: {client_id[:12]}...")
        use_existing = input("Use these? [Y/n] ").strip().lower()
        if use_existing in ("", "y", "yes"):
            pass
        else:
            client_id = ""
            client_secret = ""

    if not client_id:
        client_id = input("Oura Client ID: ").strip()
    if not client_secret:
        client_secret = input("Oura Client Secret: ").strip()

    if not client_id or not client_secret:
        print("❌ Client ID and Client Secret are required.")
        sys.exit(1)

    # Build authorization URL
    auth_params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
    })
    full_auth_url = f"{AUTH_URL}?{auth_params}"

    print()
    print("Opening browser for Oura authorization...")
    print(f"If browser doesn't open, go to:\n{full_auth_url}")
    print()

    webbrowser.open(full_auth_url)

    # Start local server to receive callback
    print("Waiting for OAuth callback on http://localhost:8080 ...")
    server = http.server.HTTPServer(("localhost", 8080), OuraCallbackHandler)

    while OuraCallbackHandler.auth_code is None:
        server.handle_request()

    code = OuraCallbackHandler.auth_code
    print(f"\n✅ Got auth code: {code[:10]}...")

    # Exchange for tokens
    print("Exchanging code for tokens...")
    token_data = exchange_code_for_token(code, client_id, client_secret)

    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    expires_in = token_data.get("expires_in", 0)

    print()
    print("=" * 60)
    print("✅ Authorization successful!")
    print("=" * 60)
    print()
    print(f"Access Token:  {access_token[:20]}...{access_token[-10:]}")
    print(f"Refresh Token: {refresh_token[:20]}...{refresh_token[-10:]}")
    print(f"Expires in:    {expires_in} seconds ({expires_in // 3600} hours)")
    print()

    # Offer to write to .env
    print(f"Write tokens to {env_path}?")
    write = input("[Y/n] ").strip().lower()

    if write in ("", "y", "yes"):
        update_env_file(env_path, {
            "OURA_CLIENT_ID": client_id,
            "OURA_CLIENT_SECRET": client_secret,
            "OURA_REFRESH_TOKEN": refresh_token,
            "OURA_ACCESS_TOKEN": access_token,
            "OURA_TOKEN_EXPIRES_AT": str(int(
                __import__("time").time() + expires_in - 60
            )),
        })
        print(f"✅ Written to {env_path}")
    else:
        print()
        print("Add these to your .env manually:")
        print("-" * 60)
        print(f"OURA_CLIENT_ID={client_id}")
        print(f"OURA_CLIENT_SECRET={client_secret}")
        print(f"OURA_REFRESH_TOKEN={refresh_token}")
        print("-" * 60)

    print()
    print("🍡 Done! Restart MochiBot and Oura data will flow in.")


if __name__ == "__main__":
    main()
