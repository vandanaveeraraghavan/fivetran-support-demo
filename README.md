# Fivetran AI Support Portal

An AI-powered support assistant for Fivetran that deflects L1 support tickets by answering connector, sync, and pipeline questions before they reach a human agent.

**Live demo:** https://vandanaveeraraghavan.github.io/fivetran-support-demo/

---

## Architecture

```
Browser (portal/)
    │  HTTP POST /ask  (SSE stream)
    ▼
Backend API (backend/server.py)   ← FastAPI + uvicorn, port 3001
    │  subprocess
    ▼
claude -p  (Claude CLI, non-interactive)
    │  MCP tools
    ├── FivetranKnowledge MCP  (api.triage.cx)
    │     ├── fivetran_public_docs  – public connector/destination docs
    │     └── zendesk_new          – internal support ticket history
    ├── WebFetch   – live fivetran.com/docs, status pages
    └── WebSearch  – Stack Overflow, GitHub issues
```

The backend spawns `claude -p` (Claude CLI) for every message, streaming the response back to the browser via Server-Sent Events. The Claude CLI runs the `test-support` skill (skill/SKILL.md) which guides the model to act as a Fivetran CSE focused on ticket deflection.

---

## Repository Layout

```
fivetran-support-demo/
├── README.md
├── portal/
│   └── index.html          # Static frontend (also served via GitHub Pages)
├── backend/
│   ├── server.py           # FastAPI backend — the production entry point
│   ├── reauth.py           # One-time OAuth re-authentication helper
│   ├── requirements.txt    # Python dependencies
│   └── .env.example        # Environment variable reference
└── skill/
    └── SKILL.md            # Claude skill definition (system prompt + tool guidance)
```

---

## Running Locally

### Prerequisites

- Python 3.11+
- [Claude CLI](https://claude.ai/code) installed and authenticated (`claude --version`)
- `pip install -r backend/requirements.txt`

### 1. Start the backend

```bash
cd backend
uvicorn server:app --port 3001 --reload
```

Verify it's healthy:

```bash
curl http://localhost:3001/health
# {"status":"ok","claude_bin":"/path/to/claude","skill_loaded":true,"active_sessions":0}
```

### 2. Open the portal

Open `portal/index.html` in your browser, or serve it:

```bash
cd portal && python3 -m http.server 8080
# Then visit http://localhost:8080
```

The portal connects to `http://localhost:3001` by default.

---

## FivetranKnowledge MCP Authentication

The backend optionally uses the internal FivetranKnowledge MCP server for richer answers (internal docs + Zendesk ticket history). Without it, the agent falls back to WebFetch and WebSearch, which still works well for public connector/destination questions.

### MCP server details

| Field | Value |
|-------|-------|
| URL | `https://api.triage.cx/oauth-server/mcp?kb_name=FivetranKnowledge` |
| Auth | OAuth 2.0 Authorization Code + PKCE |
| Token TTL | 24 hours (access), 30 days (refresh) |
| Token storage | `~/.claude/fivetranknowledge-token.json` |

### First-time authentication

```bash
cd backend
python3 reauth.py
```

This opens a browser window for OAuth consent and writes credentials to `~/.claude/fivetranknowledge-token.json`. The backend auto-refreshes the access token on expiry. Re-run `reauth.py` only when the 30-day refresh token expires.

---

## Deploying to Production

### Backend

The backend is a standard FastAPI app. Recommended deployment options:

- **Cloud Run / Fly.io / Railway** — containerise with the Dockerfile below
- **Internal Kubernetes** — standard Python service
- **AWS Lambda / GCP Cloud Functions** — needs adaptation (streaming SSE may require response buffering)

Sample `Dockerfile`:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Install claude CLI (requires Anthropic account)
RUN npm install -g @anthropic-ai/claude-code
COPY . .
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "3001"]
```

Key production considerations:

1. **Claude CLI auth** — the container needs a valid `~/.claude` directory with a logged-in session, or pass `ANTHROPIC_API_KEY` and use the `--api-key` flag in `server.py`.
2. **FivetranKnowledge token** — mount `fivetranknowledge-token.json` as a secret; the backend reads and refreshes it automatically.
3. **CORS** — update the `allow_origins` list in `server.py` to your production portal domain.
4. **Session persistence** — currently in-memory; replace `sessions` dict with Redis for multi-instance deployments.

### Frontend

The `portal/index.html` is a single self-contained file. Options:

- **GitHub Pages** — already set up at the repo root (`index.html`); update `API_BASE` in the JS to point at your deployed backend URL.
- **S3 + CloudFront / GCS + CDN** — copy the file, set the right `API_BASE`.
- **Serve from the backend** — add a `StaticFiles` mount in `server.py`.

---

## Skill Customisation

The Claude skill lives in `skill/SKILL.md`. Edit it to:

- Adjust the agent's persona or escalation policy
- Add connector-specific troubleshooting trees
- Change which tools the agent is allowed to use
- Tune the Zendesk ticket creation criteria

After editing, restart the backend — the skill is loaded fresh for each `claude -p` invocation.

---

## API Reference

### `POST /ask`

Streams a support response for a user message.

**Request body:**
```json
{
  "message": "My BigQuery connector is stuck on schema inspection",
  "session_id": "optional-uuid-to-continue-a-conversation"
}
```

**Response:** `text/event-stream`, each event is a JSON object:
```
data: {"type":"text","text":"Let me look into that..."}
data: {"type":"text","text":" Here are the common causes..."}
data: {"type":"done","session_id":"abc123"}
```

### `GET /health`

Returns backend status.

```json
{"status":"ok","claude_bin":"/path/to/claude","skill_loaded":true,"active_sessions":2}
```

---

## Productionisation Checklist

- [ ] Replace in-memory session store with Redis
- [ ] Add authentication to the `/ask` endpoint (internal SSO)
- [ ] Set `allow_origins` to production domain in CORS config
- [ ] Rotate FivetranKnowledge OAuth client to a service account
- [ ] Add request logging / tracing (OpenTelemetry)
- [ ] Set up alerting on `skill_loaded: false` in the health check
- [ ] Load-test with realistic concurrent session counts
- [ ] Evaluate moving from `claude -p` subprocess to the Anthropic SDK for lower latency
