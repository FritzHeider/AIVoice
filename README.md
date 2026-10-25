# VoiceAgents — Ralph Orchestrator Build Package

This folder is a complete, ready-to-run **Ralph orchestrator** project that builds **VoiceAgents**: an enterprise-grade,
developer-first voice AI platform functionally equivalent to Bland AI, built on **Telnyx** (telephony, numbers,
recording, AMD, SMS, LLM inference, embeddings) and **ElevenLabs** (streaming TTS, streaming STT, voice library, voice
cloning).

It contains no application code yet. It contains everything an autonomous coding agent needs to build the whole platform
phase by phase, verify its own work, and stop only when every acceptance criterion passes:

| Path | What it is |
|---|---|
| `MASTER_PROMPT.md` | The universal instructions every Ralph iteration receives (mission, fixed stack, rules, hat workflow, standards) |
| `ralph.yml` | Ralph configuration: Planner → Builder → Reviewer hats, backpressure gates, guardrails, memories, limits |
| `prompts/phase-00.md … phase-20.md` | 21 phase prompts. Each is appended to the master prompt to form `PROMPT.md` for that phase |
| `plan/phase-00.md … phase-20.md` | 21 phase checklists (~200 tasks) with spec references, dependencies, deliverables, and acceptance criteria. Agents tick these |
| `specs/00 … 31` | 32 normative specifications covering every subsystem — the contract the code must satisfy |
| `AGENTS.md` | Operational notes the agents keep current (commands, ports, gotchas) |
| `scripts/ralph/run_phase.sh` | Builds `PROMPT.md` for one phase and runs Ralph until that phase's checklist is complete |
| `scripts/ralph/run_all.sh` | Runs phases in order, retrying a phase up to 3 times, stopping on failure |
| `scripts/ralph/status.sh` | Shows per-phase progress |

## Platform at a glance

- **Bland-compatible REST API** (`/v1/calls`, `/v1/pathway`, `/v2/batches`, `/v1/inbound`, `/v1/tools`, `/v1/knowledge`,
  `/v1/voices`, `/v1/sms`, `/v1/widget`, memory, personas, agents, citations, evals, webhooks, billing).
- **Real-time pipeline:** Telnyx media WebSocket (PCMU 8 kHz) ⇄ ElevenLabs Scribe v2 Realtime ⇄ Conversational Pathways
  engine on Telnyx Inference (`zai-org/GLM-5.3-Flash`) ⇄ ElevenLabs Flash v2.5 TTS (`ulaw_8000`, alignment) — μ-law end to
  end with no transcoding, Silero VAD, adaptive endpointing, interruptibility levels 0–4, alignment-accurate barge-in.
  Target ≤ 800 ms voice-to-voice p50.
- **Conversational Pathways:** every Bland node type, global nodes, loop conditions, variable extraction, fine-tuning
  examples, versions, staging/production, test chat, unit tests, simulations, React Flow editor.
- **Omnichannel:** SMS/MMS on Telnyx with 10DLC, web widget with chat and browser voice, shared memory across channels.
- **Enterprise:** orgs/sub-accounts, RBAC, SSO, MFA, audit logs, guard rails (TCPA), DNC, redaction, HIPAA mode,
  citations, evals, A/B experiments, metered billing with Stripe, observability, Helm + Terraform.
- **Developer experience:** Python and TypeScript SDKs, `va` CLI, MCP server, docs site with `llms.txt`.

## Prerequisites

| Tool | Version | Install |
|---|---|---|
| Ralph orchestrator | latest | `npm install -g @ralph-orchestrator/ralph-cli` (or `cargo install ralph-cli`) |
| Claude Code CLI (Ralph backend) | latest | `npm install -g @anthropic-ai/claude-code`, then sign in |
| Docker Desktop / OrbStack | Docker 27, Compose v2 | — |
| Python + uv | 3.12, uv ≥ 0.5 | `brew install uv` |
| Node.js + pnpm | 22 LTS, pnpm 9 | `brew install node@22 pnpm` |
| Deno | 2.x | `brew install deno` |
| git | any recent | — |

Vendor accounts are **not** required to build: the agents build and test everything against in-repo simulators of
Telnyx, ElevenLabs, Telnyx Inference, and Stripe. Real accounts are only needed for live validation (below).

## Running the build

```bash
cd /Users/drop/AIVOICE
scripts/ralph/run_phase.sh 00      # bootstrap the monorepo, tooling, gates
scripts/ralph/status.sh            # check progress
scripts/ralph/run_all.sh 01 20     # continue through the remaining phases
```

`run_phase.sh` initialises git on first run, writes `PROMPT.md` (= `MASTER_PROMPT.md` + the phase prompt), runs
`ralph run -c ralph.yml`, logs to `logs/`, and fails if the phase checklist still has unticked tasks. Re-running a phase
resumes from the first unticked task. Each phase is sized to finish within one Ralph run (`max_iterations: 150`,
12 h runtime cap); if a run stops early, run the same phase again.

### How each iteration works

1. **📋 Planner** picks the next unticked, unblocked task in `plan/phase-NN.md`, reads the referenced spec sections and
   code, writes a concrete plan to the scratchpad, and emits `plan.ready`.
2. **🔨 Builder** implements exactly that task with tests, runs the gates (`make fmt lint typecheck placeholders test`,
   plus `test-ts`/`e2e` when relevant), and emits `build.done` with real evidence, or `build.blocked` with a written
   blocker.
3. **🔍 Reviewer** re-runs the gates, reviews the diff line by line against the specs, rejects with numbered findings or
   approves, ticks the task, commits, and emits `review.approved`.
4. When every task is ticked and the phase exit gate passes, the Planner prints `LOOP_COMPLETE`.

## Phase map

| Phase | Title | Build status |
|---|---|---|
| 00 | Repository bootstrap, tooling, quality gates | pending |
| 01 | Data layer: models, migrations, RLS, repositories, ClickHouse | pending |
| 02 | Authentication, tenancy, RBAC, API foundations | pending |
| 03 | Vendor clients and simulators (Telnyx, ElevenLabs, Telnyx Inference, Stripe) + event publisher | pending |
| 04 | Real-time media pipeline core | pending |
| 05 | Telephony edge, call lifecycle, task-mode calling end to end | pending |
| 06 | Event bus, webhooks, live streams, analytics sink | pending |
| 07 | Conversational Pathways engine, API, versions, testing | pending |
| 08 | Tools, dynamic data, custom code sandbox, knowledge bases | pending |
| 09 | Voices, cloning, standalone TTS | pending |
| 10 | Phone numbers, inbound config, pools, BYO Telnyx | pending |
| 11 | Batches, scheduling, retries, DNC, limits | pending |
| 12 | Post-call analysis, citations, evals, guard rails, compliance, warm transfer | pending |
| 13 | Memory, personas, agents, experiments | pending |
| 14 | SMS, omnichannel, 10DLC, web widget | pending |
| 15 | Billing, metering, Stripe, invoices, usage and analytics APIs | pending |
| 16 | Dashboard I (operations) | pending |
| 17 | Dashboard II (pathway editor and builders) | pending |
| 18 | OpenAPI, SDKs, CLI, MCP, docs | pending |
| 19 | Observability and security hardening | pending |
| 20 | Infrastructure, load test, live validation, runbooks, final acceptance | pending |

The Planner updates the "Build status" column when a phase completes.

## Live validation with real Telnyx and ElevenLabs

After phase 05 you can place real calls; phase 20 formalises it.

1. **Telnyx:** create an API key (Mission Control → API Keys), copy the Ed25519 public key (Keys & Credentials →
   Public Key), buy one number, and enable Inference. Put `TELNYX_API_KEY`, `TELNYX_PUBLIC_KEY`,
   `TELNYX_API_BASE=https://api.telnyx.com/v2`, `LLM_DEFAULT_BASE_URL=https://api.telnyx.com/v2/ai/openai`,
   `LLM_DEFAULT_API_KEY=<same Telnyx key>` in `.env`.
2. **ElevenLabs:** create an API key with TTS, STT, and Voices permissions; set `ELEVENLABS_API_KEY`,
   `ELEVENLABS_API_BASE=https://api.elevenlabs.io`, `ELEVENLABS_WS_BASE=wss://api.elevenlabs.io`.
3. **Public endpoints:** Telnyx must reach your webhook and media servers. Run two tunnels (for example
   `cloudflared tunnel --url http://localhost:8100` and `--url http://localhost:8200`) and set `EDGE_PUBLIC_HOST` and
   `MEDIA_PUBLIC_HOST` to their hostnames.
4. Run `uv run python scripts/telnyx_bootstrap.py` to create the Call Control Application, outbound voice profile, and
   messaging profile; paste the printed ids into `.env`; assign your number to the application.
5. `make up PROFILE=apps && make seed`, then place a call:

```bash
curl -X POST http://localhost:8000/v1/calls \
  -H "Authorization: $VA_API_KEY" -H "Content-Type: application/json" \
  -d '{"phone_number":"+14155550123","task":"You are a friendly receptionist confirming tomorrow'\''s appointment at 3pm.","first_sentence":"Hi, this is Ava from Bright Dental.","voice":"Karen","record":true}'
```

6. `LIVE_TESTS=1 uv run pytest tests_live -m live` runs the live smoke suite.

## Rough operating cost at list prices (hosted vendors)

Telnyx voice + media streaming ≈ $0.011/min, ElevenLabs Flash TTS ≈ $0.02/min of agent speech, Scribe realtime STT
≈ $0.005/min, Telnyx Inference ≈ $0.003–0.01/min, recording/compute ≈ $0.004/min — about **$0.04–0.05 per connected
minute** before platform overhead, versus Bland's $0.11–0.14 list. Exact COGS constants live in
`specs/24-billing-and-limits.md`.

## Editing the plan

- Specs are the source of truth. Change a spec, then add or adjust tasks in the affected `plan/phase-NN.md`.
- Keep task IDs stable. New tasks inserted by agents use suffixes (`P07-T04a`) or `P<NN>-FIX-<n>`.
- `scripts/check_spec_refs.py` (built in phase 00) verifies every spec reference in plans and prompts resolves.
