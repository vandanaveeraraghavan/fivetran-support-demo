#!/usr/bin/env python3
"""
FivetranKnowledge MCP Re-authentication Script

Run this when the refresh token expires (~30 days) to get new credentials.
It opens the OAuth consent page in your browser and captures the callback.

Usage:
    python3 support_backend/reauth.py
"""

import base64
import hashlib
import http.server
import json
import pathlib
import secrets
import ssl
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

# Use certifi for SSL if available
try:
    import certifi as _certifi
    _SSL_CTX = ssl.create_default_context(cafile=_certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

TOKEN_FILE = pathlib.Path.home() / ".claude" / "fivetranknowledge-token.json"
TOKEN_ENDPOINT = "https://api.triage.cx/oauth-server/token"
REGISTER_ENDPOINT = "https://api.triage.cx/oauth-server/register"
AUTHORIZE_ENDPOINT = "https://api.triage.cx/oauth-server/authorize"
CALLBACK_PORT = 7654
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"


def _register_client() -> tuple[str, str]:
    """Dynamically register a new OAuth client and return (client_id, client_secret)."""
    data = json.dumps({
        "client_name": "fivetran-support-backend",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": "openid profile email offline_access",
        "token_endpoint_auth_method": "client_secret_post",
    }).encode()
    req = urllib.request.Request(
        REGISTER_ENDPOINT,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
        reg = json.loads(resp.read())
    print(f"Registered client: {reg['client_id']}")
    return reg["client_id"], reg["client_secret"]


def _wait_for_callback() -> str | None:
    """Start a one-shot HTTP server on CALLBACK_PORT and return the auth code."""
    code_holder: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Auth complete! You can close this tab.</h1>")
            if "code" in params:
                code_holder.append(params["code"][0])

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("localhost", CALLBACK_PORT), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout=120)
    return code_holder[0] if code_holder else None


def _exchange_code(code: str, code_verifier: str, client_id: str, client_secret: str) -> dict:
    data = urllib.parse.urlencode({
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  REDIRECT_URI,
        "client_id":     client_id,
        "client_secret": client_secret,
        "code_verifier": code_verifier,
    }).encode()
    req = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
        return json.loads(resp.read())


def _exp(token: str) -> int:
    payload = token.split(".")[1]
    payload += "==" * (4 - len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))["exp"]


def main():
    print("FivetranKnowledge MCP Re-authentication")
    print("=" * 45)

    client_id, client_secret = _register_client()

    # PKCE
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = secrets.token_urlsafe(16)

    auth_url = AUTHORIZE_ENDPOINT + "?" + urllib.parse.urlencode({
        "response_type":         "code",
        "client_id":             client_id,
        "redirect_uri":          REDIRECT_URI,
        "scope":                 "openid profile email offline_access",
        "state":                 state,
        "code_challenge":        code_challenge,
        "code_challenge_method": "S256",
    })

    print(f"\nOpening browser for consent...\n{auth_url}\n")
    webbrowser.open(auth_url)
    print("Waiting for callback (up to 120s)...")

    code = _wait_for_callback()
    if not code:
        print("ERROR: No callback received. Did you click Allow Access?")
        return 1

    print(f"Got code: {code[:20]}...")
    token_resp = _exchange_code(code, code_verifier, client_id, client_secret)

    if "access_token" not in token_resp:
        print(f"ERROR: Token exchange failed: {token_resp}")
        return 1

    creds = {
        "client_id":          client_id,
        "client_secret":      client_secret,
        "access_token":       token_resp["access_token"],
        "refresh_token":      token_resp.get("refresh_token", ""),
        "access_token_exp":   _exp(token_resp["access_token"]),
        "refresh_token_exp":  _exp(token_resp["refresh_token"]) if "refresh_token" in token_resp else 0,
    }

    TOKEN_FILE.write_text(json.dumps(creds, indent=2))
    print(f"\nCredentials saved to {TOKEN_FILE}")
    print(f"Access token expires:  {time.ctime(creds['access_token_exp'])}")
    print(f"Refresh token expires: {time.ctime(creds['refresh_token_exp'])}")
    print("\nRe-authentication complete!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
