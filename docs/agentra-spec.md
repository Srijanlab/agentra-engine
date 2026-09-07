# `.agentra/` — the spec standard

What lives in `.agentra/`, who owns each file, which agent reads it, and when it
changes. The goal: a durable, structured **product spec** — not a regenerated
scratchpad — that costs zero tokens to consult when nothing has changed.

## Two levels

### Coordination repo (`Srijanlab/agentra`) — product level

```
.agentra/
  product.md          identity, objective, non-goals            owner: human
  architecture.md     the system map: the three services, the loop→engine RPC
                      boundary (_ENGINE_PROXIED ↔ _REGISTRY_METHODS/_MEMORY_METHODS),
                      data/credential ownership, deploy topology, and one
                      reference row per code repo →
                      "<repo>/.agentra/architecture.md"                owner: human
  decisions/NNNN-*.md append-only ADRs (3-service split, DynamoDB, Socket Mode)
  memory/             UNCHANGED — features/, documentation.md, testing-notes.md
  released.json       UNCHANGED
```

The backlog is GitHub Issues + the Project board — not files here. **No agent
code writes `product.md` / `architecture.md` / `decisions/`.** They are seeded by
a human (or, in future, during client onboarding — see below).

### Each code repo (`agentra-engine` / `-ui` / `-loop`) — repo level

```
.agentra/
  architecture.md   fixed template ~200 lines:
                    ## Purpose / Stack / Module map / Invariants / Conventions / Gotchas
  design.md         design decisions actually visible in the code
  testing.md        ## Local test recipe (stable)  +  ## Last run (sha/status/when/summary)
  state.json        { indexed_sha, spec_synced_sha, last_local_test }   (generated)
```

Every non-JSON file starts with a provenance header:

```
<!-- owner: agent:codebase | agent:testing | human | generated -->
<!-- source-sha: <commit the content was built from> -->
```

A legacy single-repo app (including `agentra-loop`'s own self-management) keeps
the code templates at `.agentra/architecture.md` etc. plus `.agentra/product.md`;
no separate system-map file.

## Who reads / writes what

| Consumer | product.md | coord architecture.md | decisions/ | code architecture.md | code design.md | code testing.md | state.json |
|---|---|---|---|---|---|---|---|
| Codebase Agent | – | – | – | writes | writes | seeds recipe | writes `indexed_sha` |
| Requirements Agent | – | – | – | reads | reads | – | – |
| Architecture-Review Agent | – | reads | reads | reads | reads | – | – |
| Implementation Agent | – | – | – | reads | reads | – | – |
| Testing Agent (local) | – | – | – | reads | – | writes `## Last run` | writes `last_local_test` |
| Discovery Agent | reads | – | reads | reads | – | – | – |
| Brain preseed / SHA gate | – | – | – | reads | reads | – | reads/writes |
| Dashboard `/apps/{name}` | – | – | – | – | – | – | (deferred) |
| Humans | authoritative | authoritative | authoritative | review | review | review | ignore |

The string every agent receives as `codebase_summary` is the code repo's
`architecture.md` body + a short `design.md` tail (`codebase._joined`).

## Freshness model

`codebase.sync_spec(repo, mem)`, called from the brain preseed at cycle start,
per code repo:

- `git HEAD == state.json.indexed_sha` → serve the committed spec, **cost 0, no LLM**.
- No `state.json` / explicit `understand_codebase` call → **`mode="full"`** scan.
- `HEAD != indexed_sha` → **`mode="delta"`**: the agent gets the current
  header-stripped `architecture.md` + the `git diff indexed_sha..HEAD` and edits
  only the sections the diff touches, copying the rest verbatim.

So a steady-state cycle (nothing shipped) pays nothing. A repo's spec only moves
right after a real ship to it.

## When files get committed (git)

| What | By | When → branch |
|---|---|---|
| `architecture.md` / `design.md` / `state.json.indexed_sha` | `deployment.persist_repo_specs` (eager) | right after the preseed sync loop → the repo's pre-prod branch, **before** `implement_feature` forks a feature branch |
| `testing.md ## Last run` / `state.json.last_local_test` | `persist_repo_specs` (cycle end) | → the repo's pre-prod branch |
| coordination `memory/*` | `persist_audit_trail` (cycle end) | → the coordination repo's pre-prod branch |

`persist_repo_specs` copies the spec files aside, discards the working-tree copy,
switches to the pre-prod branch, restores, and commits **there** — it never
commits on the branch the cycle is currently sitting on, so `.agentra/` spec
changes never appear in a feature PR.

## codegraph is not part of this

`agents/codegraph.py` is a runtime navigation / impact tool that
`implementation.py` and `architecture_review.py` query during a task and refresh
after implementation. It is **not** a spec input and is **not** persisted into
`.agentra/`. Its `graphify-out/` stays ephemeral per checkout, gitignored.

## Future work (not built)

- **Onboarding**: when a client onboards an app, agentra creates the coordination
  repo, seeds `product.md` from the stated objective, and runs
  `sync_spec(mode="full")` per code repo to generate the initial specs.
- **PR-proposal flow** for the human-owned coordination `architecture.md` — an
  agent that notices the system map is stale opens a PR rather than editing it.
- Re-surfacing `last_local_test` on the dashboard (the engine has no code-repo
  checkout; would read `.agentra/state.json` via the GitHub contents API or a
  loop→engine push).

See also: `SPLIT.md` (the three-service split).
