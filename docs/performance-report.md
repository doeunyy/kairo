# Performance comparison: cloud vs local

Kairo routes each model call either to the cloud model or to local Gemma 4 on the student's laptop.
This report measures both on the same tasks through the deployed agent, and checks that the router's
choices match what each model is good at.

Measured 2026-11-13 (18:46–18:54 UTC) against the live Cloud Run Instance (image `kairo:8571f7f`),
plus the 48-hour production run (2026-11-10/12). All data is synthetic.

## 1. Setup

| | Cloud | Local |
|---|---|---|
| Model | `gpt-5.6-sol` | `gemma4:e4b` |
| Access | ChatGPT Edu OAuth through Hermes' Codex provider; the router passes Hermes' own authenticated request through | Ollama 0.40.14 on the laptop, reached from Cloud Run over Tailscale (userspace SOCKS proxy) |
| Hardware | OpenAI | Apple M5 Pro, 48 GB unified memory, macOS 26.6; model fully on GPU (MLX runner, 13 GB loaded at 64K context) |
| Settings | Hermes defaults | Served context `OLLAMA_CONTEXT_LENGTH=65536` (Hermes needs ≥ 64K); **thinking off**: the router sends `reasoning: {effort: "none"}` on every local call (`A3_LOCAL_REASONING_EFFORT`), direct benchmark calls use `think: false`; `OLLAMA_KEEP_ALIVE=5m` |

**Cloud Run sizing (1 vCPU, 2 GiB).** Soak 2 ran on 1 GiB. Container memory stayed at 710–854 MB
(69–83%): it grew about 11 MB an hour, plus steps of about 110 MB. A full day between restarts would
have come close to the 1 GiB limit, and an instance over its limit is killed. At 2 GiB the 48-hour
run peaked at 804 MB (39%). 1 vCPU was enough: the instance only orchestrates, and both models run
elsewhere.

## 2. Method

`deploy/bench.py` sends prompts to the deployed agent through its public web-chat API, the same
path a grader uses. Each one is a full Hermes agent turn, so it includes the system prompt and the
~10K-token tool catalog. The client measures **end-to-end time** (request to reply, including Cloud
Run ↔ laptop over Tailscale). Afterwards the router's Cloud Logging lines are joined to each turn,
to record which model actually answered, why, and how many model calls the turn made.

Every task is run in up to three modes:

- **auto**: no tag. The router decides; this is the production behavior.
- **local**: `<!--route privacy=high-->` forces Gemma, with the full tool catalog.
- **cloud**: `<!--route reasoning=high-->` forces the cloud model.

Each turn was repeated twice (T1–T4), alternating modes. Quality is scored by code, not by eye:

- **T1:** field-by-field match against the known answer.
- **T2:** regex on the expected answer.
- **T3/T4:** required facts present and no invented companies, checked against the Notion test data.
- **T5:** exact match on the hidden code.

Raw data is in `docs/evidence/bench/`: `runs.jsonl`, `router_lines.jsonl`, `turns.jsonl`,
`scored.jsonl`, `prod_extract.jsonl`, and `summary.md`.

## 3. Results

| Task | Mode | Answered by (router log) | Route / reason | Turns | Quality | Median s | p90 s | Model calls / turn | Est. prompt tokens |
|---|---|---|---|---|---|---|---|---|---|
| **T1 Mail extraction** (4 emails → JSON) | local | gemma4:e4b | local / privacy_high | 8 | 81% | 0.9 | 2.2 | 1.0 | 10,183 |
| | cloud | gpt-5.6-sol | cloud / reasoning_high | 8 | 100% | 3.8 | 4.7 | 1.0 | 10,184 |
| | Gemma, production prompt (see 3.1) | gemma4:e4b | (extractor path) | 30 | **100%** | **0.3** | 0.45 | 1 | ~540 |
| **T2 Simple chat** (translate, arithmetic, polite rewrite) | auto | gemma4:e4b | local / simple_task (no tools) | 6 | 100% | **0.5** | 0.7 | 1.0 | 9,998 |
| | cloud | gpt-5.6-sol | cloud / reasoning_high | 6 | 100% | 2.5 | 2.9 | 1.0 | 10,007 |
| **T3 Data question with tools** (which applications are in Interview; when is the assessment due) | auto | gpt-5.6-sol | cloud / agentic_prompt | 4 | **100%** | 13.7 | 21.4 | 3.5 | 15,001 |
| | local | gemma4:e4b | local / privacy_high | 4 | **0%** | 2.1 | 7.7 | 2.2 | 10,183 |
| **T4 Multi-step plan** ("plan my next three days step by step") | auto | gpt-5.6-sol | cloud / reasoning_high | 2 | **100%** | 34.3 | 35.9 | 4.0 | 20,691 |
| | local | gemma4:e4b | local / privacy_high | 2 | **0%** | 22.6 | 23.5 | 4.5 | 10,441 |
| **T5 Long context** (find one fact in ~150K characters) | auto | gpt-5.6-sol | cloud / long_context | 2 | 100% | 4.9 | 6.7 | 1.0 | 60,571 |
| | Gemma direct (Ollama) | gemma4:e4b | — | 2 | 100% | 10.1 uncached | — | — | 36,545 (measured) |

**Cold start.** With Gemma unloaded first, a one-word chat took **17.0 s** end to end: model load
plus prefill of the ~10K-token agent prompt. Warm, the same kind of turn took 0.5 s (six turns,
0.48–0.67 s). In production the model is often cold. Ollama unloads it after 5 idle minutes and the
supervisor probes it only about every 30, which is why the 61-token heartbeat probe has a median of
3.8 s (§5).

### 3.1 What the numbers show

**T1 Mail extraction → local is right.** Mail is private, so the router never sends it to the
cloud (rule 1, `privacy_high`); the question is whether Gemma is good enough.

- With a bare prompt, Gemma got the three ordinary emails right but **followed the injected
  instruction in the newsletter every time** (4/4 runs: "offer for NW-305 due Nov 15"). The cloud
  model labeled it a newsletter.
- Production does not use a bare prompt. `core/kairo_core/extractor.py` tells the model the email
  is untrusted data, wraps it in `<email>`, and neutralizes route tags. That exact request, sent
  to Gemma the way the router sends it and checked by the production parser, was **30/30 correct**
  (four emails, the E2 hidden-instruction reminder from the security audit, and the soak newsletter;
  five runs each).
- Median 0.3 s against 3.8 s for the cloud. So for this task local is as accurate as the cloud, about
  ten times faster, and keeps the mail on the laptop.

**T2 Simple chat → local is right.** Both models scored 100%. The router sends short plain chat to
Gemma **without** the tool catalog (`chat_only`), and it answered in 0.5 s against 2.5 s in the
cloud.

**T3 and T4 tool use and planning → cloud is right.** With the ~10K-token tool catalog Gemma did
not complete a single task. Its replies:

- "I do not have the specific tool to filter applications by status" (the tool exists).
- "[Tool execution required to proceed]".
- Asking whether "Cardinal" meant a project.
- An invalid tool call (`mcp__kairo__recent_mail`).
- An empty reply that Hermes reported as "ChatGPT or Codex Subscription sent back an empty or broken
  reply". Hermes believes every turn is `gpt-5.6-sol`; the router log is the truth.

The cloud model answered all of them correctly from live Notion data. It noted the Cardinal
assessment was due Wed Nov 11 at 11:59 PM PT, and it put the two overdue items first in the plan.
It is slower (13.7 s and 34.3 s) because it makes 3–4 tool round trips, but local's speed does not
matter at 0% quality. This is why rule 6 (`agentic_prompt`) sends even short Kairo-data questions
to the cloud.

**T5 Long context → cloud, for capacity and headroom.**

- **One message cannot be long.** Hermes' API server caps each message at 65,536 characters
  (`MAX_NORMALIZED_TEXT_LENGTH`), so a single long chat message is cut. The first T5 run sent the
  text as one message; the cloud saw only 261 of 600 postings and said so. That run is kept in
  `t5_single_message_truncated.jsonl`.
- **Long contexts come from history.** In practice they build up from conversation history and
  tool results. Re-run as three turns of one conversation, the prompt reached ~60K estimated
  tokens, the router chose `cloud / long_context`, and the answer was right in 4.9 s.
- **Gemma can technically do it.** Given the same text directly (36.5K real tokens, inside its 64K
  window), Gemma also found it, in 10.1 s uncached. The second run took 0.2 s only because Ollama
  reused its prompt cache.
- **Why route it to the cloud anyway.** With an agent session the prompt is the 10K-token tool
  catalog plus history. Past the router's 40K-token estimate there is little room left in a 64K
  window, and Gemma's tool use is already unreliable at 10K. So the rule keeps long agent prompts
  on the cloud model.

## 4. Routing decisions vs. results

| Task type | Router decision (auto) | Correct? | Evidence |
|---|---|---|---|
| Private mail extraction | local (`privacy_high`), never cloud; held if the laptop is offline | Yes | Production prompt: 30/30 correct at 0.3 s; privacy rule keeps mail off the cloud (security audit W4) |
| Short plain chat | local, no tools (`simple_task`) | Yes | 100% both ways; local 5× faster |
| Questions about Kairo data | cloud (`agentic_prompt`) | Yes | Cloud 100%, local 0% with tools |
| Reasoning / planning | cloud (`reasoning_high`) | Yes | Cloud 100%, local 0% |
| Long sessions (>40K est. tokens) | cloud (`long_context`) | Yes | Cloud 100% at 60K est. tokens; local window too small for agent prompt + history |
| Cloud rate-limited (429 or simulated) | local (`rate_limit` / `rate_limit_simulated`) | Yes, as degraded service | Router tests and the laptop spike (`router/README.md`); quality then follows the local rows above |
| Hermes session titles | local (`auxiliary_task`) | Yes | ~0.3K tokens, 0.2–0.7 s |
| One-line summary of every email (added 2026-11-16) | local (`privacy_high`, task `mail_summary`), never cloud; subject only if the laptop is offline | Yes | Live 2026-11-16 (E2–E4, E7 and three real emails): 7/7 ok, median 0.45 s (max 0.67 s) at ~350 est. tokens. Mail can be personal, so it stays local; a one-sentence summary is within Gemma's ability (the urgent lab-form email was summarized correctly and alerted) |

## 5. Production (48-hour run)

Every model call in the 48-hour run, from `docs/evidence/run48h/logs/routing.jsonl`:

| Route | Reason | Task | Model | Calls | OK | Median ms | p90 ms | Median est. tokens |
|---|---|---|---|---|---|---|---|---|
| local | simple_task | heartbeat probe | gemma4:e4b | 88 | 88 | 3,776 | 4,930 | 61 |
| cloud | reasoning_high | follow-ups (12:00) | gpt-5.6-sol | 10 | 10 | 7,088 | 12,554 | 16,038 |
| cloud | reasoning_high | memory consolidation (03:00) | gpt-5.6-sol | 8 | 8 | 4,502 | 10,344 | 12,214 |
| cloud | reasoning_high | morning briefing (08:00) | gpt-5.6-sol | 7 | 7 | 6,025 | 7,161 | 15,953 |
| cloud | reasoning_high | end-of-day recap (21:00) | gpt-5.6-sol | 7 | 7 | 5,092 | 6,649 | 13,925 |
| cloud | long_context | memory consolidation | gpt-5.6-sol | 2 | 2 | 3,577 | 3,734 | 41,804 |

- 122/122 calls succeeded. The scheduled agent jobs are planning tasks and go to the cloud.
- Only one email reached the model during the window (dropped by the sender allowlist), so there
  were no production extraction calls. After the trust-tier change (2026-11-16), live mail made 4
  `mail_extract` calls (median 3.4 s, max 6.6 s, ~550 est. tokens) and 7 `mail_summary` calls
  (median 0.45 s), all local and all ok (Cloud Logging, 05:50–06:34Z).
- The probe's 3.8 s median at 61 tokens is mostly model load: Ollama had unloaded Gemma after 5 idle
  minutes. Raising `OLLAMA_KEEP_ALIVE` (for example to 1 h) would make a first local reply after a
  quiet period about 15 s faster (§3, cold start). The cost is keeping 13 GB of the laptop's memory
  reserved (`ollama ps`). It was left at the default so the laptop keeps that memory free.

## 6. Cost

| Item | Marginal cost | Notes |
|---|---|---|
| Cloud model (`gpt-5.6-sol`) | **$0 per call** | Covered by the student's ChatGPT Edu plan through OAuth; no API key or per-token billing. The real limit is the plan's usage limits (the router falls back to local on 429). A typical agent turn sends 10–21K tokens (estimated) and makes 1–4 calls, so cloud use grows with tool round trips. |
| Local model (`gemma4:e4b`) | **$0 per call** | Runs on the student's laptop; the cost is electricity and keeping the laptop on, plugged in and awake. |
| Google Cloud infrastructure (Billing report, project `kairo-dk2026`, November to 2026-11-13) | **$13.26 usage, $0.00 billed** | Cloud Run $12.88 (the always-on instance, deployed since 2026-11-06, 1 GiB through soak 2 and 2 GiB afterwards; includes the soaks, the 48-hour run and every redeploy), Cloud Storage $0.20 (state bucket, snapshots), Artifact Registry $0.18 (images), Cloud Logging $0.00. The whole amount was offset by the course's education credit. About $1.70 a day of instance time so far, roughly $50 for 30 days if it kept running; Google's quote of about $5.70 for 30 days is for 1 vCPU / 1 GiB. |

**Where local saves.**

- **Plan quota:** routine traffic (titles, short chat, heartbeat probes, every mail extraction)
  never touches the cloud quota. In the 48 hours that was 88 of 122 calls.
- **Privacy:** mail content never leaves the laptop.
- **Latency:** on short tasks local is 5–10× faster when warm.

**Where it does not.** Agentic and planning work fails locally (0%), so sending it local would save
nothing.

## 7. Limitations

- **Small samples:** 2 repetitions per task and mode (T1 production prompt: 5). The differences
  that matter (0% vs 100%, 0.5 s vs 2.5 s) are large compared to run-to-run variation, but the p90
  values are rough.
- **Laptop state:** local latency depends on whether Gemma is loaded and what else the laptop is
  doing. The benchmark ran with the laptop on battery power.
- **Estimated tokens:** prompt sizes are the router's estimate (characters / 3). Real token counts
  are only available for direct Ollama calls (T5: 36,545 real vs ~50K estimated).
- **Rate-limit fallback** was not re-run on the deployed instance during this benchmark, to avoid
  changing its state; it is covered by router tests and the laptop spike.
