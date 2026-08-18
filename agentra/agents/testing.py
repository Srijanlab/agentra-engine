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
serving traffic. It also captures a full-page screenshot of {preview_url}
deterministically (agents/screenshot.py, not left to the LLM to script
itself) to .agentra/test_artifacts/{run_id}/screenshot.png, for a human
reviewing the run to see the actual live page rather than only reading the
agent's prose report of it.

run_pre_prod is deliberately black-box: it's given the feature's spec
(agents/requirements.py's acceptance_criteria, not a codebase summary) and
no Read/Glob/Grep on the repo at all -- only Bash, for curl-ing the live URL
or running an already-configured e2e suite against it. Used to also receive
codebase_summary and full filesystem access, which defeated the point: an
agent that can just read the source to "verify" a feature is no longer
independently checking the deployed artifact actually behaves correctly,
it's re-reading code that might look right and still not work once
deployed -- exactly the class of bug this pass exists to catch.
"""

import datetime as dt
import json
import logging
from pathlib import Path

from agentra.agents.base import AgentResult, run_agent
from agentra.memory import Memory

logger = logging.getLogger(__name__)

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

Separately from the pass/fail verdict above: if you notice anything else that \
looks broken while you're in there -- unrelated to the specific feature you \
were sent to verify (e.g. a console error on an unrelated page, a broken \
favicon, a stale dependency warning) -- do not fold it into failed_tests or \
just mention it in notes where nothing reads it back. Record it as its own \
entry in incidental_findings below so it can be filed and picked up later, \
even when the overall run is otherwise a clean "pass".

End your response with a fenced ```json block shaped like:
{
  "status": "pass" | "fail",
  "failed_tests": ["..."],
  "lint_status": "pass" | "fail" | "not_configured",
  "typecheck_status": "pass" | "fail" | "not_configured",
  "incidental_findings": [
    {"diagnosis": "...", "severity": "low" | "medium" | "high", "proposed_fix": "..."}
  ],
  "notes": "..."
}
"""

PRE_PROD_SYSTEM_PROMPT = """You are the Testing Agent in an autonomous \
product engineering system, running in PRE-PROD mode. The code already \
passed local testing and has just been deployed to a live pre-prod URL: \
{preview_url}

You are deliberately black-box here: you have no access to the source code \
(no Read/Glob/Grep -- only Bash, for curl-ing the live URL or running an \
already-configured e2e suite against it), and you're given the feature's \
spec/acceptance criteria instead of a codebase summary. Verify strictly \
against what the app actually does from the outside, the way a real user or \
API caller would -- never assume something works because it would make \
sense given the spec; check it.

Your job here is different from local mode — do not re-run the unit test \
suite, that already happened. Focus on what can only be verified once the \
app is actually deployed and serving traffic:

1. Check basic reachability: does {preview_url} actually respond, and with \
   a healthy status code (not 500s, not a crash page)?
2. Check each acceptance criterion below against the live URL directly -- \
   curl the relevant endpoint/route, or drive it through the UI. Do not \
   mark a criterion verified because the deploy succeeded or because it \
   sounds plausible; only because you actually exercised it and saw the \
   result.
2a. Record one entry per criterion in test_cases below -- the reachability \
   check from (1) as its own entry plus one entry for each acceptance \
   criterion you were given, never a single collapsed verdict covering \
   several criteria. Each entry needs the exact criterion text, a "pass" \
   or "fail" result, and a short evidence note describing what you \
   actually did and saw (e.g. "curled /api/x, got 200 with expected body" \
   or "clicked the button, no visible change -- fail"), not a restatement \
   of the criterion. A human reviewing this run before promoting to prod \
   reads this list directly, so it must reflect exactly what you checked.
3. If the project has e2e/browser tests configured (e.g. Playwright) that \
   support pointing at an external base URL, run them against {preview_url} \
   instead of a local dev server. Do not set up new e2e infrastructure \
   yourself if none exists.
4. You cannot modify source files (no Read/Write access) — you are \
   verifying a deployed artifact, not fixing it. Report problems; you \
   couldn't patch them even if you wanted to.

A deploy that returns HTTP 200 on the homepage but whose actual feature is \
broken is a failure you must catch — "the server started" is not the bar.

Separately from the pass/fail verdict above: if you notice anything else that \
looks broken on the live site while you're in there -- unrelated to the \
specific feature you were sent to verify (e.g. a console error on an \
unrelated page, a broken favicon, a stale dependency warning) -- do not fold \
it into notes where nothing reads it back. Record it as its own entry in \
incidental_findings below so it can be filed and picked up later, even when \
the overall run is otherwise a clean "pass".

End your response with a fenced ```json block shaped like:
{{
  "status": "pass" | "fail",
  "reachable": true | false,
  "feature_verified": true | false,
  "test_cases": [
    {{"criterion": "...", "result": "pass" | "fail", "evidence": "..."}}
  ],
  "incidental_findings": [
    {{"diagnosis": "...", "severity": "low" | "medium" | "high", "proposed_fix": "..."}}
  ],
  "notes": "..."
}}
"""


async def run_local(
    repo: Path, codebase_summary: str, mem: Memory | None = None, session_id: str | None = None
) -> AgentResult:
    prompt = f"""Codebase summary:
{codebase_summary}

Run the full local test/QA pass now, following your system prompt."""
    result = await run_agent(
        prompt=prompt,
        system_prompt=LOCAL_SYSTEM_PROMPT,
        cwd=repo,
        allowed_tools=["Read", "Bash", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        max_turns=30,
        agent_label="Testing Agent",
        resume=session_id,
    )
    if mem is not None and result.ok and result.json_data:
        # architecture/testing-notes.md: a live, agent-maintained snapshot
        # of what test infra actually exists right now -- overwritten
        # fresh each run, same freshness semantics as codebase.md, not an
        # accumulating log.
        data = result.json_data
        lines = [
            f"Lint: {data.get('lint_status', 'unknown')}",
            f"Typecheck: {data.get('typecheck_status', 'unknown')}",
        ]
        if data.get("notes"):
            lines.append(f"Notes: {data['notes']}")
        mem.write("architecture", "testing-notes", "\n".join(lines))
    return result


def screenshot_path(repo: Path, run_id: str) -> Path:
    return repo / ".agentra" / "test_artifacts" / run_id / "screenshot.png"


def report_path(repo: Path, run_id: str) -> Path:
    """The structured (JSON) test-report artifact for a run's live pre-prod
    verification -- same test_artifacts/{run_id}/ directory as
    screenshot_path and the human-readable report.md _write_report already
    produces below, so a human reviewing a run's promotion has the
    itemized per-criterion breakdown, the prose summary, and the
    screenshot all in one place. Same durability tier as both: VM-local
    only (test_artifacts/ is gitignored), lost on the next redeploy --
    server.py's /runs/{run_key}/test-report route 404s the same way the
    screenshot route does when this file never got written (e.g. this run
    never ran live pre-prod verification at all)."""
    return repo / ".agentra" / "test_artifacts" / run_id / "report.json"


def _persist_structured_report(repo: Path, run_id: str, data: dict, *, screenshot_captured: bool) -> None:
    """Persists run_pre_prod's structured JSON verdict (status, per-criterion
    test_cases, incidental findings, notes) for server.py's
    /runs/{run_key}/test-report endpoint -- the dashboard's Review Promotion
    panel reads this, distinct from _write_report's prose report.md below.
    Written whenever the agent returned any parsed JSON at all, even a
    failed run -- a human reviewing a failed pre-prod verification still
    wants the itemized breakdown of what failed, not just a bare "fail"."""
    path = report_path(repo, run_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "status": data.get("status"),
            "reachable": data.get("reachable"),
            "feature_verified": data.get("feature_verified"),
            "test_cases": data.get("test_cases") or [],
            "incidental_findings": data.get("incidental_findings") or [],
            "notes": data.get("notes"),
            "screenshot_captured": screenshot_captured,
        }, indent=2))
    except OSError:
        logger.warning("run_pre_prod: failed to write structured test report to %s", path, exc_info=True)


def _write_report(repo: Path, run_id: str, spec: str, preview_url: str, result: AgentResult) -> None:
    """Durable test report packet for a human to review, synthesizing the
    verification verdict rather than just dumping raw JSON -- same directory
    as the screenshot captured above. Every pre-prod verification gets one,
    not just agentra-fixing-agentra's self-hosted path (EnvironmentConfig.
    self_hosted_vm) -- that path just happens to have no public preview URL
    to otherwise point a reviewer at, but the artifact is generically useful.
    Best-effort: a failure to write this must never fail the verification
    call itself."""
    data = result.json_data or {}
    lines = [
        f"# Pre-prod verification report — run {run_id}",
        "",
        f"Preview URL: {preview_url}",
        f"Verified: {dt.datetime.now(dt.timezone.utc).isoformat()}",
        "",
        "## Spec / acceptance criteria",
        spec,
        "",
        "## Verdict",
        f"- status: {data.get('status', 'unknown')}",
        f"- reachable: {data.get('reachable', 'unknown')}",
        f"- feature_verified: {data.get('feature_verified', 'unknown')}",
        f"- notes: {data.get('notes', '')}",
    ]
    findings = data.get("incidental_findings") or []
    if findings:
        lines.append("")
        lines.append("## Incidental findings")
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            lines.append(
                f"- [{finding.get('severity', 'unknown')}] {finding.get('diagnosis', '')} "
                f"(proposed fix: {finding.get('proposed_fix', '')})"
            )
    screenshot_file = screenshot_path(repo, run_id)
    report_path = screenshot_file.parent / "report.md"
    if screenshot_file.exists():
        lines.append("")
        lines.append(f"## Screenshot\n\n![screenshot]({screenshot_file.name})")
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(lines))
    except OSError:
        logger.warning("run_pre_prod: failed to write test report packet to %s", report_path, exc_info=True)


async def run_pre_prod(
    repo: Path, spec: str, preview_url: str, run_id: str, session_id: str | None = None
) -> AgentResult:
    """`spec` is the feature's requirements/acceptance criteria (agents/
    requirements.py, formatted by the caller -- see brain.py's
    verify_pre_prod), NOT a codebase summary -- this pass is deliberately
    black-box, see this module's own docstring for why."""
    from agentra.agents import screenshot

    ok, detail = await screenshot.capture(preview_url, screenshot_path(repo, run_id))
    screenshot_note = (
        "A full-page screenshot of the live URL was already captured for you and is attached "
        "to this run's report -- no need to take your own."
        if ok
        else f"Screenshot capture failed ({detail}) -- note this in your report, but don't let it block the rest of your verification."
    )

    prompt = f"""{spec}

The feature was just deployed to: {preview_url}

{screenshot_note}

Independently verify the live deployment now, following your system prompt."""
    system_prompt = PRE_PROD_SYSTEM_PROMPT.format(preview_url=preview_url)
    result = await run_agent(
        prompt=prompt,
        system_prompt=system_prompt,
        cwd=repo,
        allowed_tools=["Bash"],
        permission_mode="bypassPermissions",
        max_turns=20,
        agent_label="Testing Agent",
        resume=session_id,
    )
    _write_report(repo, run_id, spec, preview_url, result)
    if result.json_data:
        _persist_structured_report(repo, run_id, result.json_data, screenshot_captured=ok)
    return result
