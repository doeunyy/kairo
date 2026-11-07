# a3-router (spike)

Task-aware router between Hermes v0.22.7 (ChatGPT OAuth, `openai-codex` provider) and
local Gemma 4 on Ollama. The rule table and the held policy are in the top-level README.

## How Hermes reaches it

Hermes documents `HERMES_CODEX_BASE_URL` for sending the `openai-codex` provider through a
proxy (`website/docs/reference/environment-variables.md`). It then POSTs Responses API
requests (`store: false`, SSE stream) to `<base>/responses` with its own OAuth bearer token.

- **Cloud**: the router forwards the request unchanged to
  `https://chatgpt.com/backend-api/codex/responses`. It restores the `ChatGPT-Account-ID`
  and residency headers from the forwarded JWT, plus Hermes' harness identity, because
  Hermes only adds those for the official URL (`agent/codex_headers.py`). Our code never
  holds or refreshes the ChatGPT credential itself.
- **Local**: the router rewrites `model` to the Gemma tag, drops Codex-only fields
  (`include`, `prompt_cache_key`, encrypted `reasoning`/`compaction` items), sets
  `reasoning.effort: none`, and POSTs to Ollama `/v1/responses`, through
  `A3_OLLAMA_PROXY=socks5h://127.0.0.1:1055` in the cloud container.

Behind a proxy, Hermes sizes context from the Codex table (272K for most models). The
router therefore sends anything estimated above `A3_LOCAL_MAX_TOKENS` (40K) to the cloud.

- **Chat-only local turns**: a short untagged turn that names none of Kairo's data and has no
  tool history (`needs_tools: false`) goes to Gemma without `tools`, `tool_choice` and
  `parallel_tool_calls`, with a one-line note in `instructions`. With the full catalog (~10K
  tokens) Gemma returned empty replies; without it, it answers plain questions in about a second.
- **Failures**: a private local call that fails is held, never retried in the cloud. A
  non-private local call that fails is retried once in the cloud (`fallback_from: local`).

## Run

```bash
uv sync --python 3.14
uv run pytest -q
A3_LOCAL_MODEL=gemma4:e4b uv run uvicorn a3_router.app:app --host 127.0.0.1 --port 8787
HERMES_CODEX_BASE_URL=http://127.0.0.1:8787 hermes
```

Simulate a cloud rate limit (reversible): `curl -X POST 127.0.0.1:8787/router/ratelimit/on`
(and `/off`), or create/delete `state/flags/simulate_cloud_429`.

Routing decisions: `state/logs/routing/<UTC date>.jsonl`, mirrored to stdout.

## Live spike results (2026-11-04, laptop, Hermes v0.21.5 at the time)

Setup: Hermes v0.21.5 (`v2026.9.24`), `HERMES_HOME=~/.a3-kairo`, ChatGPT Edu login
(`plan_type: education`) imported from a dedicated `CODEX_HOME=~/.a3-codex`, model
`gpt-5.6-sol`; Ollama 0.40.14, `gemma4:e4b`, served context 65,536 (100% GPU);
`hermes chat -Q -t terminal --oneshot`.

| Test | Route / reason | Result |
|---|---|---|
| JD comparison + `terminal` tool call | cloud / `reasoning_high` | ok, tool called, 3.4 s per call |
| Mail classification + `terminal` tool call | local / `privacy_high` | ok, Gemma called the tool through Ollama `/v1/responses`, 2.1 s warm |
| Same reasoning prompt with `/router/ratelimit/on` | local / `rate_limit_simulated` | ok; `off` restores cloud routing |
| Salary note with Ollama unreachable | hold / `privacy_high_local_unavailable` | Hermes shows the hold message; no cloud call |

Findings:

- Ollama's `/v1/responses` accepts Hermes' tool schemas, so no Chat Completions
  translation layer is needed.
- Hermes records every session as `gpt-5.6-sol` even when Gemma answered. The router log
  is the source of truth for which model handled a turn.
- Hermes also sends small auxiliary calls (session titles, about 400 tokens). They carry
  the same user text and are routed and logged like the main turn.
- With only the `terminal` toolset the prompt estimate is about 5.8K tokens. Measure again
  with Kairo's full toolset.
- Gemma followed the tool step but dropped the requested first line. Note this in the
  performance comparison.

## Still unverified

1. The SOCKS5 path from the Cloud Run container.
2. Cold-start latency of a full-toolset local turn.
