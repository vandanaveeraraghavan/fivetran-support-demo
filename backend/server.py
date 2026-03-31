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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3001, log_level="info")
