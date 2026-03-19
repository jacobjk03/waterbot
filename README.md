# WaterBot — RAG-Powered Chatbot for Arizona Water Information

WaterBot is a Retrieval-Augmented Generation (RAG) chatbot that provides information about water in Arizona. It features a React/Vite frontend and a FastAPI backend. RAG is powered by **AWS Bedrock Knowledge Bases**; LLM responses are generated via **OpenAI GPT-4.1**. A secondary "RiverBot" persona is also supported via separate routes and templates.

**Production URL:** https://azwaterbot.org

---

## Features

- **RAG via AWS Bedrock Knowledge Base** — semantic search over Arizona water documents, no self-hosted vector DB required
- **Multi-language** — English and Spanish, auto-detected via `langdetect` with user override
- **Voice transcription** — real-time speech-to-text via browser Web Speech API / WebSocket
- **Session memory** — per-user UUID cookie keeps conversation context
- **Source citations** — every answer links back to source documents
- **RiverBot persona** — separate chat UI and system prompt at `/riverbot`
- **React frontend** — responsive, mobile-friendly UI with typewriter animation
- **Legacy Jinja2 templates** — `index.html`, `riverbot.html`, `spanish.html`, `splashScreen.html` served directly by FastAPI for non-React paths

---

## Architecture

### Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | React + Vite, Framer Motion, Tailwind CSS |
| Backend | FastAPI (Python 3.11) |
| RAG | AWS Bedrock Knowledge Base (Retrieve API) |
| LLM | OpenAI GPT-4.1 |
| Session identity | UUID cookie (`USER_SESSION`) set by `SetCookieMiddleware` |
| Message logging | PostgreSQL (`messages` table) — optional, done in background |
| Ratings | AWS DynamoDB — optional |
| Transcripts | AWS S3 — optional |
| Container | Docker (multi-stage build) |
| Hosting | AWS ECS Fargate + ALB + CloudFront |

### Request Flow

```
Browser → UUID Cookie → FastAPI
  → Safety checks (OpenAI moderation + intent detection)
  → Language detection (langdetect)
  → Bedrock KB Retrieve API (top-k chunks)
  → Build prompt (system + KB context + chat history)
  → OpenAI GPT-4.1
  → Response + sources → Browser
  → [Background] Log to PostgreSQL messages table
```

### System Architecture

```mermaid
graph TB
    subgraph "Client"
        Browser
    end

    subgraph "AWS CloudFront + ALB"
        CF[CloudFront\nazwaterbot.org]
        ALB[Application Load Balancer]
    end

    subgraph "ECS Fargate"
        App[FastAPI App\nmain.py]
    end

    subgraph "AWS Services"
        KB[Bedrock Knowledge Base\nZ2NHZ8JMMQ us-west-2]
        RDS[RDS PostgreSQL\nmessages table]
        DDB[DynamoDB\nratings]
        S3[S3\ntranscripts]
    end

    subgraph "External"
        OAI[OpenAI GPT-4.1\nLLM + embeddings + moderation]
    end

    Browser --> CF --> ALB --> App
    App --> KB
    App --> RDS
    App --> DDB
    App --> S3
    App --> OAI
```

### RAG Pipeline

```mermaid
flowchart TD
    Query([User Query]) --> Safety[Safety Checks\nmoderation + intent]
    Safety -->|fail| Reject[Return safe fallback]
    Safety -->|pass| Lang[Detect Language\nen / es]
    Lang --> KB[Bedrock KB Retrieve\nAWS_KB_ID + locale filter]
    KB --> Chunks[Top-K document chunks\nwith source metadata]
    Chunks --> Prompt[Build Prompt\nsystem + KB + chat history]
    Prompt --> LLM[OpenAI GPT-4.1]
    LLM --> Response[Response + sources]
    Response --> Memory[Update MemoryManager]
    Memory --> BG[Background: log to PostgreSQL]
    Response --> Browser([Return to Browser])
```

---

## Local Development

### Prerequisites

- Python 3.11+
- Node.js 18+
- AWS credentials with `bedrock:Retrieve` + `bedrock:RetrieveAndGenerate` on KB `Z2NHZ8JMMQ` in `us-west-2`
- Claude API key

### Step 1 — Create `application/.env`

```bash
cp application/sample.env application/.env
```

Then fill in:

```env
# ── Required ────────────────────────────────────────────
# LLM (used for chat + source verification)
CLAUDE_API_KEY=sk-...

# RAG via AWS Bedrock Knowledge Base
AWS_KB_ID=Z2NHZ8JMMQ
AWS_REGION=us-west-2

# AWS credentials — choose one method:
# Option A: explicit keys
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...

# Option B: named profile (boto3 picks it up automatically; omit the key vars above)
# AWS_PROFILE=your-profile

# ── Optional — message persistence ──────────────────────
# If omitted, chat still works but messages are not logged to a DB.
# Use either DATABASE_URL or the DB_* individual vars.
# DATABASE_URL=postgresql://user:pass@localhost:5432/waterbot
# DB_HOST=localhost
# DB_USER=waterbot
# DB_PASSWORD=secret
# DB_NAME=waterbot
# DB_PORT=5432

# ── Optional — ratings + transcripts ────────────────────
# MESSAGES_TABLE=your-dynamodb-table-name
# TRANSCRIPT_BUCKET_NAME=your-s3-bucket-name

# ── Optional — CORS / cookie tuning ─────────────────────
# ALLOWED_ORIGINS=https://your-domain.com
# COOKIE_DOMAIN=.your-domain.com
```

### Step 2 — Start the backend

```bash
cd application
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
fastapi dev main.py          # auto-reload at http://localhost:8000
```

### Step 3 — Start the frontend (separate terminal)

```bash
cd frontend
npm install
npm run dev                  # http://localhost:5173
```

Vite proxies all `/chat_api`, `/chat_sources_api`, `/submit_rating_api`, `/session-transcript`, `/translate`, and `/static` requests to `localhost:8000` automatically — no manual CORS configuration needed in dev.

### What works without optional vars

| Feature | Required vars |
|---------|--------------|
| RAG + chat | `CLAUDE_API_KEY`, `AWS_KB_ID`, `AWS_REGION`, AWS creds |
| Message logging | `DATABASE_URL` or `DB_*` |
| Ratings | `MESSAGES_TABLE` |
| Transcript download | `TRANSCRIPT_BUCKET_NAME` |

When optional vars are absent the app starts cleanly — those features are silently skipped.

### Docker (full stack)

```bash
docker-compose up           # builds image, runs on http://localhost:8000
# or individually:
./docker_build.sh
./docker_run.sh
```

The `docker-compose.yml` reads `CLAUDE_API_KEY`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `TRANSCRIPT_BUCKET_NAME`, and `MESSAGES_TABLE` from your shell environment.

---

## Project Structure

```
waterbot/
├── application/                  # FastAPI backend
│   ├── main.py                   # App entry point: all 13 endpoints, middleware, startup
│   ├── adapters/
│   │   ├── base.py               # ModelAdapter abstract base class
│   │   ├── claude.py            # ClaudeAdapter (Claude chat + source verification)
│   │   └── bedrock_kb.py         # BedrockKnowledgeBase (Retrieve + RetrieveAndGenerate)
│   ├── managers/
│   │   ├── memory_manager.py     # In-memory session state (chat history, message counts)
│   │   ├── rag_manager.py        # RAG orchestration (query → KB → prompt → LLM)
│   │   ├── pgvector_store.py     # pgvector store (legacy / local dev fallback)
│   │   ├── dynamodb_manager.py   # Ratings persistence
│   │   └── s3_manager.py         # Transcript storage + presigned URLs
│   ├── templates/                # Jinja2 HTML templates
│   │   ├── index.html            # WaterBot legacy UI
│   │   ├── riverbot.html         # RiverBot persona UI
│   │   ├── spanish.html          # Spanish UI
│   │   ├── splashScreen.html     # Splash / landing page
│   │   └── aboutWaterbot.html    # About page
│   ├── static/                   # CSS, JS, images for Jinja templates
│   ├── mappings/                 # knowledge_sources.py, custom_tags.py
│   ├── scripts/                  # Data ingestion (Add_files_to_db*.py)
│   └── sample.env                # Environment variable template
├── frontend/                     # React + Vite frontend
│   ├── src/
│   │   ├── App.jsx               # Root component, global state
│   │   ├── components/           # ChatBubble, InputWrapper, Header, MobileChatbot, RecordingModal
│   │   └── services/api.js       # All fetch calls to backend
│   ├── public/                   # Static assets (images, icons)
│   └── vite.config.js            # Dev proxy config
├── iac/
│   └── cdk/                      # AWS CDK stacks (Python)
│       ├── app.py                # CDK app entry
│       ├── cdk_app_stack.py      # ECS Fargate, ALB, CloudFront, RDS, DynamoDB, S3
│       └── lambda/               # db_init, dynamo_export, postgres_backup Lambdas
├── .github/workflows/
│   ├── deploy-waterbot-dev.yaml  # Dev CI/CD (shankerram3/waterbot-test → us-west-2)
│   └── deploy-waterbot-prod.yaml # Prod CI/CD (S-Carradini/waterbot → us-east-1)
├── docker-compose.yml
├── Dockerfile
└── README.md
```

---

## API Endpoints

### Chat

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/chat_api` | POST | WaterBot main chat (English) |
| `/riverbot_chat_api` | POST | RiverBot persona chat |
| `/chat_sources_api` | POST | Retrieve sources for a prior response |
| `/chat_actionItems_api` | POST | Get action items from a prior response |
| `/chat_detailed_api` | POST | Get a more detailed response for a prior query |

### Pages (Jinja2)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Splash screen |
| `/waterbot` | GET | WaterBot Jinja2 chat UI |
| `/riverbot` | GET | RiverBot Jinja2 chat UI |
| `/spanish` | GET | Spanish Jinja2 chat UI |

### Utilities

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/transcribe` | WebSocket | Real-time voice transcription |
| `/session-transcript` | POST | Download session transcript (S3 presigned URL) |
| `/submit_rating_api` | POST | Submit thumbs up/down rating |
| `/translate` | POST | Translate text between en/es |

---

## CI/CD Pipeline

Both workflows are manually triggered (`workflow_dispatch`) and use **GitHub OIDC** (no long-lived AWS keys stored in GitHub secrets).

### Dev — `shankerram3/waterbot-test` → us-west-2

1. Authenticates to AWS via OIDC role `GitHubActionsECSRole`
2. Builds Docker image (no cache)
3. Pushes to ECR (`us-west-2`)
4. Force-deploys ECS Fargate service
5. Waits for service stability and verifies image digest
6. Invalidates CloudFront cache

### Prod — `S-Carradini/waterbot` → us-east-1

Same steps, targeting the production ECS cluster and CloudFront distribution (`azwaterbot.org`).

### Required GitHub Environment Secrets/Vars

| Name | Type | Value |
|------|------|-------|
| `AWS_ROLE_ARN` | Secret | `arn:aws:iam::590183827936:role/GitHubActionsECSRole` |
| `AWS_REGION` | Var | `us-west-2` (dev) / `us-east-1` (prod) |
| `ECR_ACCOUNT_ID` | Var | `590183827936` |
| `ECR_REPO` | Var | ECR repository name |
| `ECS_CLUSTER` | Var | ECS cluster name |
| `ECS_SERVICE` | Var | ECS service name |
| `CLOUDFRONT_DISTRIBUTION_ID` | Var | CloudFront distribution ID |

---

## AWS Infrastructure

### Environments

| | Dev | Prod |
|-|-----|------|
| Region | us-west-2 | us-east-1 |
| GitHub repo | `shankerram3/waterbot-test` | `S-Carradini/waterbot` |
| GitHub environment | `aws-test-deploy` | `aws-prod-deploy` |
| URL | (ECS task public IP) | https://azwaterbot.org |

### Bedrock Knowledge Base

- **KB ID**: `Z2NHZ8JMMQ`
- **Region**: `us-west-2` (used by both dev and prod — the KB is not replicated)
- Set `AWS_KB_ID=Z2NHZ8JMMQ` and `AWS_REGION=us-west-2` in the ECS task definition or `.env`

### CDK Stacks

Infrastructure is managed with AWS CDK (Python) in `iac/cdk/`.

```bash
cd iac/cdk
source .venv-cdk/bin/activate
pip install -r requirements.txt

# Deploy dev stack (us-west-2)
AWS_DEFAULT_REGION=us-west-2 cdk deploy --context env=dev

# Deploy prod stack (us-east-1)
AWS_DEFAULT_REGION=us-east-1 cdk deploy --context env=dev
```

> Use `AWS_DEFAULT_REGION` env var to target the correct region — the `--region` CLI flag does not work reliably with CDK.

---

## Configuration Reference

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `CLAUDE_API_KEY` | Yes | Claude (Anthropic) API key (chat + source verification) |
| `CLAUDE_MODEL` | No | Main chat model (defaults to `claude-sonnet-4-6`) |
| `SOURCES_VERIFIER_MODEL` | No | Sources YES/NO classifier model (defaults to `claude-sonnet-4-6`) |
| `AWS_KB_ID` | Yes | Bedrock Knowledge Base ID (`Z2NHZ8JMMQ`) |
| `AWS_REGION` | Yes | Bedrock KB region (`us-west-2`) |
| `AWS_ACCESS_KEY_ID` | Yes* | AWS credential (*or use `AWS_PROFILE` / instance role) |
| `AWS_SECRET_ACCESS_KEY` | Yes* | AWS credential |
| `DATABASE_URL` | No | PostgreSQL URL for message logging |
| `DB_HOST/USER/PASSWORD/NAME/PORT` | No | Alternative to `DATABASE_URL` |
| `MESSAGES_TABLE` | No | DynamoDB table name for ratings |
| `TRANSCRIPT_BUCKET_NAME` | No | S3 bucket for chat transcripts |
| `COOKIE_DOMAIN` | No | Cookie domain (e.g. `.azwaterbot.org`) — only set when domain matches request host |
| `ALLOWED_ORIGINS` | No | Comma-separated CORS origins |
| `AWS_KB_MODEL_ARN` | No | Override Bedrock model ARN for RAG (defaults to Claude 3 Sonnet) |

### LLM Adapter

The current default is `ClaudeAdapter(...)` set in `main.py`. To swap adapters, modify:

```python
# application/main.py
ADAPTERS: dict[str, object] = {
    "claude-default": ClaudeAdapter(CLAUDE_MODEL),
    # "bedrock-kb": BedrockKnowledgeBase(kb_id=AWS_KB_ID),
}
llm_adapter = ADAPTERS["claude-default"]
```

---

## Troubleshooting

### RAG not returning results

- Verify `AWS_KB_ID=Z2NHZ8JMMQ` and `AWS_REGION=us-west-2` are set
- Confirm your AWS identity has `bedrock:Retrieve` and `bedrock:RetrieveAndGenerate` on the KB
- Check CloudWatch logs for `bedrock-agent-runtime` errors

### Messages not persisting after restart

`MemoryManager` is in-memory — this is expected. Set `DATABASE_URL` or `DB_*` vars to enable PostgreSQL message logging. The `messages` table is auto-created on startup if it doesn't exist.

### `relation "messages" does not exist`

The app creates the `messages` table automatically at startup (`startup_ensure_db`). If you see this error, check that your DB user has `CREATE TABLE` privileges.

### Cookie/session not sticking

- On HTTP (local dev), the cookie uses `SameSite=Lax` — works fine
- On HTTPS (prod), it uses `SameSite=None; Secure` — requires HTTPS
- `COOKIE_DOMAIN` is only applied when the request host matches — don't set it for local dev

### CSS not updating in dev

```bash
rm -rf frontend/node_modules/.vite
npm run dev
```

### Voice transcription not working

Check browser permissions for microphone access. Transcription uses the browser Web Speech API (no server-side processing for basic transcription).

### GitHub Actions deploy fails with "AWS_ROLE_ARN secret is missing"

1. Go to your repo → Settings → Environments → `aws-test-deploy` (dev) or `aws-prod-deploy` (prod)
2. Add secret `AWS_ROLE_ARN = arn:aws:iam::590183827936:role/GitHubActionsECSRole`
3. Re-run the workflow — existing runs do not pick up newly added secrets

---

## License

See LICENSE file for details.
