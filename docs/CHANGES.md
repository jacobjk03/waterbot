# WaterBot — Session Changes & Fix Summary

## Overview

This document covers all changes made during this work session: migrating RAG from Railway
PostgreSQL to AWS Bedrock Knowledge Base, fixing the broken RAG pipeline (double LLM invocation),
setting up a production CI/CD pipeline for `S-Carradini/waterbot`, and resolving 6 confirmed bugs
plus removing a significant amount of dead code.

---

## Why RAG Was Not Working

### Root Cause: Double LLM Invocation (`application/adapters/bedrock_kb.py`)

`ann_search()` was calling the `RetrieveAndGenerate` API — a **complete, self-contained RAG
pipeline** that retrieves knowledge base chunks *and* generates a finished answer. That
fully-generated answer was then returned to `main.py`, which treated it as raw knowledge base
context and fed it into the LLM again alongside the conversation history.

**Result:** The model was paraphrasing a paraphrase. The second LLM call had no access to the
original source chunks — just a polished summary — so it could not properly ground its answer or
cite sources. Response quality degraded silently and there was no error.

**Fix:** Switched `ann_search` to the `Retrieve` API, which returns raw text chunks only.
`main.py` continues to handle generation via the OpenAI adapter with full conversation history,
exactly as designed.

### Secondary: Sources Silently Dropped (`application/managers/memory_manager.py`)

`format_sources_as_html()` contained `if url:` as a gate before rendering any source entry.
Bedrock KB sources whose filename is not mapped in `knowledge_sources.py` receive `url=""` and
were silently skipped. Users saw "I did not use any specific sources" even when the knowledge base
returned relevant chunks.

**Fix:** Deduplication key changed to `url or human_readable`. Sources now always render their
human-readable label; a URL is appended only when present.

---

## Infrastructure Changes

**File:** `iac/cdk/stacks/app_stack.py` — CDK deployed to both dev (us-west-2) and prod (us-east-1)

| Change | Detail |
|--------|--------|
| Removed Railway PostgreSQL | Deleted `DATABASE_URL` Secrets Manager secret and container injection. Bedrock KB handles RAG; RDS (DB_HOST/DB_USER/DB_PASSWORD/DB_NAME) handles message storage only. |
| Added Bedrock KB IAM policy | `bedrock:Retrieve` + `bedrock:RetrieveAndGenerate` scoped to `arn:aws:bedrock:us-west-2:590183827936:knowledge-base/Z2NHZ8JMMQ` |
| Added container environment vars | `AWS_KB_ID=Z2NHZ8JMMQ`, `AWS_REGION=us-west-2` |
| Startup guard | `_ensure_rag_chunks_table()` is skipped on startup when `AWS_KB_ID` is set (pgvector table not needed with Bedrock KB) |

### Important: CDK Is the Source of Truth for Container Environment Variables

CDK fully controls the ECS task definition on every deploy. Any environment variable set manually
in the AWS console or directly on the ECS service will be **overwritten the next time CDK deploys**.

This means:
- `CLAUDE_API_KEY`, `BASIC_AUTH_SECRET`, and `DB_PASSWORD` must be in **Secrets Manager** (injected
  via `secrets=` in CDK) — not set manually on the service.
- `AWS_KB_ID`, `AWS_REGION`, and other non-secret config must be in the `environment=` dict in
  `app_stack.py`.
- Never set environment variables directly in the ECS console expecting them to persist — they will
  be wiped on the next CDK or CI/CD deploy.

---

## Production CI/CD Pipeline

**New file:** `.github/workflows/deploy-waterbot-prod.yaml`

Triggers on push to `S-Carradini/waterbot` main (guarded by `if: github.repository == 'S-Carradini/waterbot'`).

**Pipeline steps:** OIDC auth → ECR login → Docker build (no-cache) → tag → push → force ECS
deploy → wait for service stability → verify all running tasks are on the new image digest →
invalidate CloudFront cache → write job summary.

### OIDC Trust Policy — Two Rounds of Fixes on `GitHubActionsECSRole`

**Round 1:** The role's trust policy only allowed the fork (`shankerram3/waterbot-test`). Added
`repo:S-Carradini/waterbot:ref:refs/heads/main`.

**Round 2 (the real fix):** When a GitHub Actions job specifies `environment:`, the OIDC token's
`sub` claim changes format:

```
# Without environment:
repo:S-Carradini/waterbot:ref:refs/heads/main

# With environment: aws-prod-deploy
repo:S-Carradini/waterbot:environment:aws-prod-deploy   ← this is what actually appears
```

The trust policy was matching the wrong format. Updated to:
`repo:S-Carradini/waterbot:environment:aws-prod-deploy`

**Round 3:** `GitHubActionsECSRole` was also missing `cloudfront:CreateInvalidation` and
`cloudfront:GetInvalidation` — the pipeline was deploying successfully to ECS but failing at the
CloudFront invalidation step, leaving the old cached site live. Added as an inline policy.

### `aws-prod-deploy` Environment (in `S-Carradini/waterbot`)

| Key | Value |
|-----|-------|
| `AWS_ROLE_ARN` (secret) | `arn:aws:iam::590183827936:role/GitHubActionsECSRole` |
| `AWS_REGION` | `us-east-1` |
| `ECR_ACCOUNT_ID` | `590183827936` |
| `ECR_REPO` | `cdk-ecr-stack-dev-waterbot7e9d62bf-rfwka9reh2q0` |
| `ECS_CLUSTER` | `cdk-app-stack-dev-WaterbotFargateCluster8D18135A-qC2WwpVI2CWn` |
| `ECS_SERVICE` | `cdk-app-stack-dev-FargateServiceECC8084D-rS8F16asVy4Q` |
| `CLOUDFRONT_DISTRIBUTION_ID` | `ETLDXNKKXIRJE` |

---

## Bug Fixes

| # | File | Bug | Fix |
|---|------|-----|-----|
| 1 | `main.py` | `increment_message_count` called twice in action-items endpoints — once standalone, once in the `return` statement. `msgID` in every response was stale (one behind). | Removed the standalone call; changed `return` to use `get_message_count`. |
| 2 | `main.py` | `data` variable used on the safety-check error path but only assigned inside a `try:` block. If `intent_result` was not valid JSON, `data` was undefined → `NameError` crash. | Pre-initialized `data = {}` before each `try:` block (in both `chat_api` and `riverbot_chat_api`). |
| 3 | `memory_manager.py` | Sources silently dropped when `url=""` (see RAG section above). | Dedup on `url or human_readable`; render source label even without a URL. |
| 4 | `openai.py` | `self.client.chat.completions.create()` is a synchronous blocking call inside an `async` function. Under concurrent requests, it would stall the entire uvicorn event loop until the LLM responded. | Wrapped in `asyncio.to_thread()`. |
| 5 | `InputWrapper.jsx` | Two `return () => {...}` cleanup blocks in the same `useEffect`. The first was unreachable dead code — JavaScript only executes the first `return`. | Removed the first return block. |
| 6 | `main.py` | `_ensure_rag_chunks_table()` referenced `RAG_POSTGRES_ENABLED` and `_rag_pg_connect()` which had been renamed to `POSTGRES_ENABLED` and `_pg_connect()`. Would have crashed at startup when no `AWS_KB_ID` was set. | Replaced with correct names; added `AWS_KB_ID` guard to skip pgvector table creation when Bedrock KB is active. |

---

## Dead Code Removed

| What | Why |
|------|-----|
| `application/adapters/claude.py` (file deleted) | `BedrockClaudeAdapter` was never used — the active adapter is `ClaudeAdapter(...)`. |
| Claude import + `ADAPTERS` registration in `main.py` | Same as above. |
| Amazon Transcribe imports in `main.py` | The frontend uses the browser's native `SpeechRecognition` / `webkitSpeechRecognition` API for voice input. AWS Transcribe was never called from the frontend. |
| `MyEventHandler` class in `main.py` | Only used by the `/transcribe` endpoint. |
| `/transcribe` WebSocket endpoint in `main.py` | Dead — the frontend never connected to it. |
| `/transcribe` proxy in `vite.config.js` | Dead proxy entry for the removed endpoint. |
| `httpx`, `WebSocket`, `WebSocketState` imports in `main.py` | All three became unused after the endpoint removal. |

---

## Environments

| | Dev | Prod |
|-|-----|------|
| Region | us-west-2 | us-east-1 |
| ECS Cluster (short) | `...Rh49QXMfHkSP` | `...qC2WwpVI2CWn` |
| URL | — | https://azwaterbot.org |
| Deploys from | `shankerram3/waterbot-test` | `S-Carradini/waterbot` |
| GitHub Environment | `aws-test-deploy` | `aws-prod-deploy` |

---

## Files Changed

| File | Change |
|------|--------|
| `application/adapters/bedrock_kb.py` | Switch `ann_search` from `RetrieveAndGenerate` to `Retrieve` API |
| `application/adapters/openai.py` | Wrap blocking LLM call in `asyncio.to_thread()` |
| `application/adapters/claude.py` | **Deleted** |
| `application/main.py` | Bug fixes 1, 2, 6; remove Claude + Transcribe dead code |
| `application/managers/memory_manager.py` | Fix sources silently dropped (Bug 3) |
| `application/sample.env` | Remove `DATABASE_URL`; document Bedrock KB vars |
| `frontend/src/components/InputWrapper.jsx` | Remove unreachable cleanup block (Bug 5) |
| `frontend/vite.config.js` | Remove `/transcribe` proxy |
| `iac/cdk/stacks/app_stack.py` | Remove Railway DB; add Bedrock KB IAM + env vars |
| `.github/workflows/deploy-waterbot-prod.yaml` | **New** — production CI/CD pipeline |
