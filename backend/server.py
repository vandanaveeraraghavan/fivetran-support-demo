"""
Fivetran Support Portal Backend
Bridges the browser portal to Claude CLI with the test-support skill.

Knowledge sources used:
  1. WebFetch  – fivetran.com/docs, third-party status pages, official API docs
  2. WebSearch – public web (Stack Overflow accepted answers, GitHub issues)
  3. FivetranKnowledge MCP (fivetran_public_docs, zendesk_new) – via api.triage.cx

FIVETRAN_MCP_URL:
  The FivetranKnowledge MCP server URL.  Defaults to the known production URL.
  Override by setting this env var if the URL ever changes.

  URL discovered from claude.ai /api/organizations/{org}/mcp/v2/bootstrap:
    https://api.triage.cx/oauth-server/mcp?kb_name=FivetranKnowledge

  Token credentials are stored in ~/.claude/fivetranknowledge-token.json:
    {client_id, client_secret, access_token, refresh_token, access_token_exp, refresh_token_exp}

  The access token is refreshed automatically when it expires (24h TTL).
  The refresh token is valid for 30 days.  To re-authenticate after refresh
  token expiry, run: python3 support_backend/reauth.py

  Start the server with:
    uvicorn server:app --port 3001
"""

import asyncio
import json
import os
import ssl
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# Load .env file if present (simple parser, no extra dependencies)
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# Load Zendesk API token from the local key file (never committed to the repo).
# File location: ~/Downloads/zd_sandbox_api_key  (plain text, one line — just the token)
_zd_key_file = Path.home() / "Downloads" / "zd_sandbox_api_key"
if _zd_key_file.exists():
    _zd_token = _zd_key_file.read_text().strip()
    if _zd_token:
        os.environ.setdefault("ZENDESK_API_TOKEN", _zd_token)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

# ── Config ────────────────────────────────────────────────────────────────────
CLAUDE_BIN   = str(Path.home() / ".local/bin/claude")
SKILL_MD     = Path.home() / ".claude/skills/test-support/SKILL.md"
WORK_DIR     = str(Path.home() / "Downloads")

# FivetranKnowledge MCP server URL (override via env var if it ever changes)
FIVETRAN_MCP_URL = os.environ.get(
    "FIVETRAN_MCP_URL",
    "https://api.triage.cx/oauth-server/mcp?kb_name=FivetranKnowledge",
)

# Strip YAML frontmatter from SKILL.md to use as system prompt
_raw = SKILL_MD.read_text()
if _raw.startswith("---"):
    _end = _raw.index("---", 3)
    SYSTEM_PROMPT = _raw[_end + 3:].strip()
else:
    SYSTEM_PROMPT = _raw

# In-memory session store: browser_session_id → claude_session_id
sessions: dict[str, str] = {}

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Fivetran Support Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── FivetranKnowledge MCP token management ────────────────────────────────────
TOKEN_FILE = Path.home() / ".claude" / "fivetranknowledge-token.json"
TOKEN_ENDPOINT = "https://api.triage.cx/oauth-server/token"
TOKEN_REFRESH_BUFFER = 300   # refresh if token expires within 5 minutes

# Use certifi CA bundle if available, otherwise fall back to system default
try:
    import certifi as _certifi
    _SSL_CTX = ssl.create_default_context(cafile=_certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()


def _load_token_file() -> dict:
    return json.loads(TOKEN_FILE.read_text())


def _save_token_file(creds: dict) -> None:
    TOKEN_FILE.write_text(json.dumps(creds, indent=2))


def _refresh_access_token(creds: dict) -> dict:
    """Exchange the refresh_token for a new access_token and persist it."""
    import base64

    data = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "refresh_token": creds["refresh_token"],
        "client_id":     creds["client_id"],
        "client_secret": creds["client_secret"],
    }).encode()

    req = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
        token_resp = json.loads(resp.read())

    def _exp(token: str) -> int:
        payload = token.split(".")[1]
        payload += "==" * (4 - len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))["exp"]

    creds = dict(creds)
    creds["access_token"] = token_resp["access_token"]
    creds["access_token_exp"] = _exp(token_resp["access_token"])
    if "refresh_token" in token_resp:
        creds["refresh_token"] = token_resp["refresh_token"]
        creds["refresh_token_exp"] = _exp(token_resp["refresh_token"])
    _save_token_file(creds)
    print("[mcp] Access token refreshed, new exp:", creds["access_token_exp"], flush=True)
    return creds


def _get_fivetran_mcp_token() -> str:
    """
    Return a valid FivetranKnowledge access token, refreshing if needed.

    Tokens are stored in ~/.claude/fivetranknowledge-token.json.
    Raises if the token file is missing or the refresh token has expired.
    """
    creds = _load_token_file()
    now = time.time()

    if creds["access_token_exp"] - now < TOKEN_REFRESH_BUFFER:
        if creds.get("refresh_token_exp", 0) < now:
            raise RuntimeError(
                "FivetranKnowledge refresh token expired. "
                "Run support_backend/reauth.py to re-authenticate."
            )
        print("[mcp] Access token near/past expiry — refreshing…", flush=True)
        creds = _refresh_access_token(creds)

    return creds["access_token"]


# ── Helpers ───────────────────────────────────────────────────────────────────
def build_cmd(message: str, claude_session: str | None) -> list[str]:
    """
    Build the claude -p command with FivetranKnowledge MCP enabled.

    Uses --mcp-config to inject the FivetranKnowledge HTTP MCP server so
    claude can call fivetran_public_docs and zendesk_new natively.
    Falls back to WebFetch/WebSearch if the token file is missing or expired.
    """
    try:
        mcp_token = _get_fivetran_mcp_token()
        mcp_config = json.dumps({
            "mcpServers": {
                "FivetranKnowledge": {
                    "type": "http",
                    "url": FIVETRAN_MCP_URL,
                    "headers": {"Authorization": f"Bearer {mcp_token}"},
                }
            }
        })
        tools = (
            "mcp__FivetranKnowledge__fivetran_public_docs,"
            "mcp__FivetranKnowledge__zendesk_new,"
            "WebFetch,WebSearch"
        )
        base = [
            CLAUDE_BIN, "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--mcp-config", mcp_config,
            "--allowedTools", tools,
            "--append-system-prompt", SYSTEM_PROMPT,
        ]
    except Exception as exc:
        print(f"[warn] MCP token unavailable ({exc}); falling back to WebFetch/WebSearch")
        base = [
            CLAUDE_BIN, "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--allowedTools", "WebFetch,WebSearch",
            "--append-system-prompt", SYSTEM_PROMPT,
        ]

    if claude_session:
        return base + ["--resume", claude_session, message]
    else:
        return base + [message]


async def run_stream(cmd: list[str]):
    """
    Async generator: yields raw stdout lines from the claude CLI subprocess.

    Also handles the control_request / control_response bidirectional protocol
    used when type:http MCP servers are configured.  When claude needs to call
    an MCP tool it emits a control_request JSON on stdout; the backend must
    write a control_response JSON to stdin.  For http-type servers this proxy
    is not needed (claude calls the URL directly), but the handler is here for
    completeness and future type:sdk support.
    """
    env = os.environ.copy()
    env["PATH"] = str(Path.home() / ".local/bin") + ":" + env.get("PATH", "")

    print(f"[cmd] {' '.join(cmd[:6])}", flush=True)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,   # needed for control_response
        cwd=WORK_DIR,
        env=env,
    )
    assert proc.stdout is not None
    assert proc.stdin  is not None

    async def read_stderr():
        assert proc.stderr is not None
        async for line in proc.stderr:
            print(f"[stderr] {line.decode('utf-8', errors='replace').strip()}", flush=True)

    asyncio.create_task(read_stderr())

    async for raw_line in proc.stdout:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue

        # Intercept control_request (only emitted when MCP servers are SDK-type)
        try:
            parsed = json.loads(line)
            if parsed.get("type") == "control_request":
                # Not needed for http-type MCP, but handle gracefully
                error_resp = json.dumps({
                    "type": "control_response",
                    "response": {
                        "subtype": "error",
                        "request_id": parsed.get("request_id", ""),
                        "error": "SDK-type MCP proxy not active; use http-type via FIVETRAN_MCP_URL",
                    },
                }) + "\n"
                proc.stdin.write(error_resp.encode())
                await proc.stdin.drain()
                continue   # don't yield control_request to caller
        except (json.JSONDecodeError, AttributeError):
            pass

        yield line

    await proc.wait()
    print(f"[proc] exit code {proc.returncode}", flush=True)


# ── SSE Stream endpoint ───────────────────────────────────────────────────────
@app.post("/chat/stream")
async def chat_stream(body: dict):
    """
    Streams the Claude response as Server-Sent Events.
    Event types emitted to browser:
      • { type: "tool_search", query: "...", source: "docs"|"zendesk" }
      • { type: "text",        text:  "..." }
      • { type: "done",        sessionId: "..", claudeSessionId: ".." }
      • { type: "error",       message: "..." }
    """
    browser_session = body.get("sessionId") or str(uuid.uuid4())
    message         = body.get("message", "").strip()
    if not message:
        async def empty():
            yield f"data: {json.dumps({'type':'error','message':'Empty message'})}\n\n"
        return StreamingResponse(empty(), media_type="text/event-stream")

    claude_session = sessions.get(browser_session)
    cmd = build_cmd(message, claude_session)

    async def event_generator():
        accumulated_text = []
        new_claude_session = claude_session

        try:
            async for line in run_stream(cmd):
                print(f"[stream] {line[:120]}", flush=True)
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue

                chunk_type = chunk.get("type")

                # ── Assistant message (text block or tool_use) ────────────
                if chunk_type == "assistant":
                    msg_content = chunk.get("message", {}).get("content", [])
                    for block in msg_content:
                        btype = block.get("type")

                        if btype == "text":
                            text = block.get("text", "")
                            if text:
                                accumulated_text.append(text)
                                yield f"data: {json.dumps({'type':'text','text':text})}\n\n"

                        elif btype == "tool_use":
                            tool_name = block.get("name", "")
                            tool_input = block.get("input", {})
                            query = (
                                tool_input.get("query")
                                or tool_input.get("url")
                                or tool_input.get("prompt", "")
                            )
                            # Map tool name → source label for UI pill
                            tl = tool_name.lower()
                            if "zendesk" in tl:
                                source = "zendesk"
                            elif "public_docs" in tl or "fivetran" in tl:
                                source = "docs"
                            elif tl in ("websearch", "webfetch"):
                                source = "docs"
                            else:
                                source = None
                            if source and query:
                                yield f"data: {json.dumps({'type':'tool_search','query':query,'source':source})}\n\n"

                # ── Final result ─────────────────────────────────────────
                elif chunk_type == "result":
                    new_claude_session = chunk.get("session_id") or claude_session
                    full_result = chunk.get("result", "")
                    if full_result and not accumulated_text:
                        yield f"data: {json.dumps({'type':'text','text':full_result})}\n\n"

                # ── System init ──────────────────────────────────────────
                elif chunk_type == "system":
                    sid = chunk.get("session_id")
                    if sid:
                        new_claude_session = sid

        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'type':'error','message':'Request timed out'})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type':'error','message':str(exc)})}\n\n"
        finally:
            if new_claude_session:
                sessions[browser_session] = new_claude_session
            yield f"data: {json.dumps({'type':'done','sessionId':browser_session,'claudeSessionId':new_claude_session})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    mcp_status = "unavailable"
    token_exp   = None
    refresh_exp = None
    try:
        creds = _load_token_file()
        now = time.time()
        token_exp   = int(creds.get("access_token_exp", 0))
        refresh_exp = int(creds.get("refresh_token_exp", 0))
        if token_exp > now + TOKEN_REFRESH_BUFFER:
            mcp_status = "ready"
        elif refresh_exp > now:
            mcp_status = "needs_refresh"
        else:
            mcp_status = "reauth_required"
    except Exception as exc:
        mcp_status = f"error: {exc}"

    return {
        "status":          "ok",
        "claude_bin":      CLAUDE_BIN,
        "skill_loaded":    SKILL_MD.exists(),
        "active_sessions": len(sessions),
        "mcp_url":         FIVETRAN_MCP_URL,
        "mcp_status":      mcp_status,
        "token_expires_at": token_exp,
        "refresh_expires_at": refresh_exp,
    }


# ── Reset session ─────────────────────────────────────────────────────────────
@app.post("/session/reset")
async def reset_session(body: dict):
    sid = body.get("sessionId")
    if sid and sid in sessions:
        del sessions[sid]
    return {"status": "reset"}


# ── Zendesk ticket creation (sandbox: fivetran18131705608885) ─────────────────
ZENDESK_SUBDOMAIN = os.environ.get("ZENDESK_SUBDOMAIN", "fivetran18131705608885")
ZENDESK_EMAIL     = os.environ.get("ZENDESK_EMAIL", "")
ZENDESK_API_TOKEN = os.environ.get("ZENDESK_API_TOKEN", "")

SEV_PRIORITY = {"P1": "urgent", "P2": "high", "P3": "normal", "P4": "low"}


@app.post("/create-zendesk-ticket")
async def create_zendesk_ticket(body: dict):
    """
    Create a Zendesk ticket in the Fivetran support sandbox.

    Env vars (optional — falls back to mock if absent):
      ZENDESK_EMAIL      – agent email  e.g. vandana@fivetran.com
      ZENDESK_API_TOKEN  – Zendesk API token (Admin → Apps & Integrations → API)
      ZENDESK_SUBDOMAIN  – defaults to fivetran18131705608885
    """
    subject     = body.get("subject", "Support Request")[:200]
    description = body.get("description", "")
    email        = body.get("email", "customer@example.com")
    severity     = body.get("severity", "P3")
    product_type = body.get("productType", "")   # Fivetran | HVR | HVA | Hybrid Deployment | Activations
    connector    = body.get("connector", "")
    destination  = body.get("destination", "")
    category     = body.get("category", "")
    tag          = body.get("tag", "ai_handoff")   # ai_resolved | ai_handoff | ai_bypassed
    transcript   = body.get("transcript", description)

    # Build full ticket description
    parts = [transcript or description]
    if product_type: parts.append(f"\nProduct Type: {product_type}")
    if connector:    parts.append(f"Connector: {connector}")
    if destination:  parts.append(f"Destination: {destination}")
    if category:     parts.append(f"Category: {category}")
    parts.append(f"Severity: {severity}")
    full_description = "\n".join(parts)

    # Return a realistic mock when credentials are not set
    if not ZENDESK_EMAIL or not ZENDESK_API_TOKEN:
        import random
        mock_id = random.randint(10000, 99999)
        print(f"[zendesk] No credentials — mock ticket #{mock_id}", flush=True)
        return {
            "ticket_id": mock_id,
            "url": f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/agent/tickets/{mock_id}",
            "tag": tag,
            "mock": True,
        }

    # Live Zendesk API call
    import base64
    api_url = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2/tickets.json"
    auth_b64 = base64.b64encode(f"{ZENDESK_EMAIL}/token:{ZENDESK_API_TOKEN}".encode()).decode()

    payload = json.dumps({
        "ticket": {
            "subject":   subject,
            "comment":   {"body": full_description},
            "requester": {"email": email, "name": email.split("@")[0]},
            "priority":  SEV_PRIORITY.get(severity, "normal"),
            "tags":      [tag, "ai_support_demo", f"sev_{severity.lower()}"] + (
                             [f"product_{product_type.lower().replace(' ', '_')}"] if product_type else []
                         ),
        }
    }).encode()

    req = urllib.request.Request(
        api_url,
        data=payload,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Basic {auth_b64}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            result = json.loads(resp.read())
        ticket_id = result["ticket"]["id"]
        print(f"[zendesk] Created ticket #{ticket_id} tag={tag}", flush=True)
        return {
            "ticket_id": ticket_id,
            "url": f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/agent/tickets/{ticket_id}",
            "tag": tag,
            "mock": False,
        }
    except Exception as exc:
        import random
        mock_id = random.randint(10000, 99999)
        print(f"[zendesk] API error ({exc}) — mock #{mock_id}", flush=True)
        return {
            "ticket_id": mock_id,
            "url": f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/agent/tickets/{mock_id}",
            "tag": tag, "mock": True, "error": str(exc),
        }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3001, log_level="info")
