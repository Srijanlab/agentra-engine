# ADLC Multi-Agent Orchestration — agentra Mapping & Plan

This maps the ADLC (Agent Development Life Cycle) orchestration model to
agentra's actual implementation, file by file, then lays out an incremental
plan to close the real gaps. It supersedes vision.md §4-5's architecture
diagram where the two disagree — vision.md describes the originally
envisioned fixed pipeline (orchestrator.py's `run_cycle()`); this document
describes and extends the dynamic orchestrator that actually runs today
(`agents/brain/`, the default path).

Read this alongside vision.md (product intent, agent responsibilities) and
CONTAINER.md (sandbox boundary). This doc is about orchestration mechanics.

## 1. Core finding: agentra already IS an ADLC orchestrator

The ADLC model's central claim — "the orchestrator always owns the workflow;
agents perform specialized work and return results" — is not a redesign for
agentra, it's a description of what `agents/brain/__init__.py` already does.
`OrchestratorSession` + `run_autonomous_cycle()` is the durable workflow
orchestrator; the nine `@tool`-decorated functions in `agents/brain/tools.py`
are specialized work performed by delegate agents (`agents/codebase.py`,
`agents/discovery.py`, `agents/implementation.py`, etc.) and returned as
results, never as handoffs. No agent in this codebase takes independent
control of the session — that pattern is already avoided.

The gaps are real but narrower than "adopt this architecture": mostly they're
about making implicit structure explicit (capability registry, policy layer)
and closing specific holes (no Design Agent, no multi-tenant identity model,
no mid-cycle human-in-the-loop pause/resume).

## 2. Concept-by-concept mapping

| ADLC concept | agentra today | File |
|---|---|---|
| Orchestrator owns the workflow | `OrchestratorSession` + `run_autonomous_cycle()` | `agents/brain/__init__.py` |
| Agents as tools (not handoffs) | Nine `@tool` functions, each delegating to one specialized agent module and returning its result | `agents/brain/tools.py` |
| Dynamic capability selection | The orchestrator LLM picks which of the nine tools to call next each turn, guided by prompt heuristics, not a hardcoded sequence | `agents/brain/prompts.py` (`SYSTEM_PROMPT`) |
| Fixed-pipeline fallback (rare) | `run_cycle()` — deterministic, always-same-order path, not the default | `orchestrator.py` |
| Agent/capability registry | `AGENT_METADATA` — skills + tool grants per agent, currently UI-only (feeds the dashboard's Agent card), not read by the planner | `agents/catalog.py` |
| Authority boundaries per agent | Explicit CAN/CANNOT already documented and enforced two ways: `agents/catalog.py` tool grants (static) + `agents/safety.py` PreToolUse regex hook (runtime, e.g. no prod deploy, no `.env` edits, no `DROP TABLE`) | `agents/catalog.py`, `agents/safety.py` |
| Policy engine (deterministic gates) | Ad hoc but real: `deploy_pre_prod` tool refuses unless `session.tests_passed`; production release is a separate human-triggered endpoint, never reachable from the autonomous cycle | `agents/brain/tools.py:360-397`, `server/routes/triggers.py:252-271` |
| Deterministic human intervention (approval gate) | `POST /apps/{app_name}/promote` — a human explicitly calls this from the dashboard; nothing in `run_autonomous_cycle` can reach it | `server/routes/triggers.py:252-271` |
| Agent-required intervention (`HUMAN_INPUT_REQUIRED`) | `needs_human` / `blocking_agentra` GitHub issue labels. A failure that `cannot_be_fixed_by_agentra()` gets filed with both; `blocking_bugs()` is checked before every cycle starts and hard-stops if any are open | `memory/issues.py:74-78, 154-164`, `memory/core.py` |
| Human decisions as first-class, auditable events | GitHub Issue comments/labels ARE the event log — every `record_*` call posts a comment or label change with full context (run id, diagnosis, resolution). `registry/runs.py` + `registry/inbox.py` add a parallel structured event trail for run status | `memory/issues.py`, `registry/runs.py` |
| Execution vs. context separation | Partial and inconsistent: `run_id`/`run_key` is a real execution id (`registry/runs.py`), but there's no persistent `context_id` above it — each `run_autonomous_cycle()` call is a fresh `OrchestratorSession`, and cross-run continuity is reconstructed ad hoc from GitHub Issue state (`resume_branch_for`, `resume_run_id_for`, `get_spec`) rather than a first-class context object |  `memory/issues.py:208-238` |
| Provider session vs. ADLC-level context, kept separate | Already done, but only for the dashboard chat feature, not the orchestrator: `chat_store.py` stores Claude's own session id (`agent_sessions/{agent_id}.json`) separately from agentra's own chat history record | `chat_store.py:9-15` |
| Multi-tenant identity hierarchy | Not present. `registry` is single-tenant: apps are a flat `{app_name: {repo_path, repo_url, branch}}` map, no `tenant_id`/`app_id` layering | `registry/core.py:58-77` |
| Web app as primary human control plane | Yes — the dashboard (`agentra/web/`) is already the source of truth via `/apps`, `/runs`, `/agents/metadata`, chat endpoints | `server/routes/*.py` |
| Slack as a secondary channel | Not present at all today | — |
| Durable pause/resume across a human wait | Not present for the autonomous cycle. `run_autonomous_cycle()` is one live `query()` call end to end; if a human gate were needed mid-cycle, the process has nothing to persist and resume from — it can only stop before the call (`blocking_bugs()` check) or gate a *separate* call (`/promote`) | `agents/brain/__init__.py:144-235` |

## 3. What NOT to change

The ADLC brief's "agents as tools, not handoffs" recommendation is already
agentra's design — don't introduce a handoff/delegation mechanism where an
agent takes the wheel. Similarly:

- Keep GitHub Issues as the durable backlog/event store. It already gives
  you auditable human decisions (comments), state (labels), and survives
  process restarts for free — don't duplicate this into a new event-store
  service.
- Keep the LLM-as-planner pattern inside the orchestrator's system prompt
  for step-to-step sequencing. A separate rule-based "Planner" component
  would fight the thing that already works (`SYSTEM_PROMPT`'s "use judgment,
  not a rigid script").
- Keep the two-layer safety model (`agents/catalog.py` static grants +
  `agents/safety.py` runtime hook) — it's already the Policy Engine the ADLC
  brief describes, just not named that.

## 4. Gaps worth closing, in priority order

### 4.1 Capability registry the planner actually reads (Medium priority)

`AGENT_METADATA` in `agents/catalog.py` already has the right shape (skills,
tool grants) but is dashboard-only — the orchestrator's tool list in
`agents/brain/tools.py` is hardcoded and duplicates this information instead
of deriving from it. Add a `capability` field per entry (e.g. `"coding"`,
`"testing"`, `"deployment"`) and have `_tools_for()` build its tool list from
the registry instead of a literal `tools = [...]` array. This doesn't change
behavior — it makes "what capabilities exist" a single source of truth
instead of two.

### 4.2 A Design/Architecture-impact Agent (Medium priority)

vision.md §5.5 specified a Planning/Architect Agent (frontend/backend/
database/api change breakdown, risk level) that was never built —
`agents/requirements.py` covers spec + acceptance criteria but not
architectural blast radius. For agentra's actual failure mode (an
Implementation Agent making a wide-reaching change without flagging it),
this is the concrete version of the ADLC brief's "not every task needs a
Design Agent" — add it as a tenth tool, called conditionally: the
orchestrator's system prompt should call it only when `implement_feature`'s
brief look like it touches more than one layer (schema change, new API
surface, cross-cutting refactor), not on every feature.

**Decided:** conditional, not mandatory — matches agentra's existing
"judgment, not a rigid script" philosophy (same pattern as
`discover_opportunities` being last-resort rather than always-called).

### 4.3 Authority-boundary-driven `HUMAN_INPUT_REQUIRED` (Low priority, high leverage)

Today `needs_human`/`blocking_agentra` are set from one place
(`record_failure`'s `cannot_be_fixed_by_agentra()` text classifier). The ADLC
brief's authority-boundary idea is stronger: give `implementation.py` (and a
future design agent) a structured way to say "this requires a decision
outside my authority" *before* attempting the change, not just after failing.
Concretely: let `implementation.run()` return
`{"status": "HUMAN_INPUT_REQUIRED", "reason": ..., "question": ..., "options": [...]}`
in its `json_data`, and have `implement_feature` in `agents/brain/tools.py`
recognize that shape and route it through `record_known_bug(needs_human=True)`
directly, instead of only reaching that path via a caught failure.

**Decided:** land the shape on all three tools that can plausibly hit an
authority boundary in the same pass — `implement_feature` (schema/API/
security decisions), `deploy_pre_prod` (e.g. an environment/infra choice
outside the deploy agent's remit), and `discover_opportunities` (e.g. a
genuinely ambiguous strategic direction, not just "no opportunities found").
Each tool's underlying `run()` gets the same `json_data` shape; each
`agents/brain/tools.py` wrapper recognizes it and routes through
`record_known_bug(needs_human=True)` the same way `implement_feature` does
today for caught failures.

### 4.4 Context object above `run_id` (Low priority — do only if multi-run continuity becomes a real pain point)

Right now cross-run continuity is reconstructed per-field from GitHub Issues
(`resume_branch_for`, `get_spec`, etc.) rather than through one `context_id`.
This works because GitHub Issues already is the durable store — introducing
a formal `Context` object would mostly be renaming what already exists
unless a concrete need shows up (e.g. supporting a second LLM provider
per-agent, which is the actual reason the ADLC brief wants this separation).
Don't build this speculatively.

### 4.5 Multi-tenant identity model (Defer — no current multi-tenant requirement)

`registry`'s flat `app_name` keying would need a `tenant_id → app_id` layer
to match ADLC §16. There's no multi-tenant product requirement driving this
in agentra today (single operator, own fleet of apps) — defer until a real
tenant-isolation need exists rather than pre-building the hierarchy.

### 4.6 Slack channel (Defer — web dashboard is already primary and sufficient)

No current gap: the dashboard already serves as the ADLC brief's "primary
UX." Add Slack only when there's an actual notification/interaction need it
would serve, per the brief's own guidance that Slack must stay secondary to
the database as source of truth.

## 5. Plan

Ordered for independent, separately-landable, separately-tested changes —
each step should ship and pass `pytest tests/ -q` on its own before the next
starts.

1. **Add `capability` to `AGENT_METADATA`** (`agents/catalog.py`) — additive
   field, no behavior change, unblocks step 2.
2. **Derive `_tools_for()`'s tool list from the registry** instead of the
   literal array at `agents/brain/tools.py:490-500` — mechanical refactor,
   covered by existing brain/tool tests.
3. **Structured `HUMAN_INPUT_REQUIRED`** (§4.3) — add the
   `{"status": "HUMAN_INPUT_REQUIRED", ...}` shape to `implementation.run()`,
   `deployment.deploy_pre_prod()`, and `discovery.run()`; wire each of
   `implement_feature`/`deploy_pre_prod`/`discover_opportunities` in
   `agents/brain/tools.py` to recognize it and route through
   `record_known_bug(needs_human=True)`. Land `implement_feature` first (it's
   the clearest case and gives the other two a template), then the other two
   as follow-on commits — still one pytest-green landing per tool, not one
   giant commit.
4. **Design/Architecture-impact Agent** (§4.2) — new `agents/design.py`
   module following the existing agent-module shape (see
   `agents/requirements.py` for the smallest existing example), one new
   `@tool` in `agents/brain/tools.py`, catalog entry, and system-prompt
   guidance so the orchestrator calls it conditionally (brief spans more than
   one layer: schema change, new API surface, cross-cutting refactor) rather
   than on every `implement_feature` call.
5. Revisit §4.4/§4.5/§4.6 only when a concrete driving need appears —
   re-open this doc rather than building ahead of it.
