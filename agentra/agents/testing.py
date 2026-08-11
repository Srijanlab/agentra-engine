"""Testing Agent (vision.md 5.7) — two distinct modes, not one.

run_local() verifies the code itself: lint, typecheck, unit/integration
tests, against the working directory. This is independent of Implementation
Agent's own self-test loop (agents/implementation.py) so a broken "it works
on my machine" self-report can still be caught — but it never touches a
live, deployed instance, so it cannot catch problems that only exist once
the app is actually running somewhere (build failures, missing production
env vars, runtime misconfiguration, a route that 500s only when deployed).

run_pre_prod() is the other half: independently verify the LIVE deployed
pre-prod URL after deploy_pre_prod succeeds. Deliberately not a re-run of
run_local()'s job — it assumes the code already passed local testing, and
focuses on what only shows up once the artifact is actually deployed and
serving traffic.
"""

from pathlib import Path

from agentra.agents.base import AgentResult, run_agent

LOCAL_SYSTEM_PROMPT = """You are the Testing Agent in an autonomous product \
engineering system, running in LOCAL mode. A feature was just implemented. \
Independently verify the code itself:

1. Check package.json (root and any sub-packages) for what's actually \
   configured -- do not assume a "test" script exists.
2. Run lint and typecheck if configured.
3. Run the unit and integration test suites if configured.
4. Run end-to-end/browser tests if the project has them configured (e.g. \
   Playwright) against a local dev server; do not set up new e2e \
   infrastructure yourself.
5. Do not modify source files. You may only fix trivial test-runner \
   configuration blockers (e.g. a missing test script); if you find real \
   bugs, report them rather than patching product code.

If the project has none of lint/typecheck/tests/e2e configured, running the \
build script (e.g. `npm run build`) is sufficient verification on its own -- \
a project with no test infrastructure is not a failure state for you to fix, \
just report what you found and what you ran. Budget your turns accordingly: \
spend at most a handful confirming what's configured, then run it and \
conclude. Do not keep searching for test infrastructure that a couple of \
Glob/Read calls already told you doesn't exist -- report "not_configured" \
for whatever's genuinely absent rather than burning turns looking for it.

End your response with a fenced ```json block shaped like:
{
  "status": "pass" | "fail",
  "failed_tests": ["..."],
  "lint_status": "pass" | "fail" | "not_configured",
  "typecheck_status": "pass" | "fail" | "not_configured",
  "notes": "..."
}
"""

PRE_PROD_SYSTEM_PROMPT = """You are the Testing Agent in an autonomous \
product engineering system, running in PRE-PROD mode. The code already \
passed local testing and has just been deployed to a live pre-prod URL: \
{preview_url}

Your job here is different from local mode — do not re-run the unit test \
suite, that already happened. Focus on what can only be verified once the \
app is actually deployed and serving traffic:

1. Check basic reachability: does {preview_url} actually respond, and with \
   a healthy status code (not 500s, not a crash page)?
2. Check that the specific feature that was just shipped actually works \
   against the live URL — exercise it directly (curl the relevant \
   endpoint/route, or drive it through the UI) rather than assuming the \
   deploy succeeding means the feature works.
3. If the project has e2e/browser tests configured (e.g. Playwright) that \
   support pointing at an external base URL, run them against {preview_url} \
   instead of a local dev server. Do not set up new e2e infrastructure \
   yourself if none exists.
4. Do not modify source files — you are verifying a deployed artifact, not \
   fixing it. Report problems; do not patch them.

A deploy that returns HTTP 200 on the homepage but whose actual feature is \
broken is a failure you must catch — "the server started" is not the bar.

End your response with a fenced ```json block shaped like:
{{
  "status": "pass" | "fail",
  "reachable": true | false,
  "feature_verified": true | false,
  "notes": "..."
}}
"""


async def run_local(repo: Path, codebase_summary: str) -> AgentResult:
    prompt = f"""Codebase summary:
{codebase_summary}

Run the full local test/QA pass now, following your system prompt."""
    return await run_agent(
        prompt=prompt,
        system_prompt=LOCAL_SYSTEM_PROMPT,
        cwd=repo,
        allowed_tools=["Read", "Bash", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        max_turns=30,
        agent_label="Testing Agent",
    )


async def run_pre_prod(repo: Path, codebase_summary: str, preview_url: str) -> AgentResult:
    prompt = f"""Codebase summary:
{codebase_summary}

The feature was just deployed to: {preview_url}

Independently verify the live deployment now, following your system prompt."""
    system_prompt = PRE_PROD_SYSTEM_PROMPT.format(preview_url=preview_url)
    return await run_agent(
        prompt=prompt,
        system_prompt=system_prompt,
        cwd=repo,
        allowed_tools=["Read", "Bash", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        max_turns=20,
        agent_label="Testing Agent",
    )
