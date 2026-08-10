"""TASK-019: daily standup, per registered project.

Not a chat transcript between fictional personas -- one LLM call per
project that turns that project's own real Memory data (yesterday's
timestamped log activity, today's actual open backlog) into a short
written report, explicitly instructed not to invent anything not present
in that data. No tools needed: the data is already extracted and handed
to the prompt directly, so there's nothing for the model to go read, which
also keeps this cheap and fast enough to run synchronously from an HTTP
handler (unlike a full autonomous cycle).
"""

import datetime as dt
from pathlib import Path

from agentra.agents.base import run_agent
from agentra.memory import Memory

STANDUP_SYSTEM_PROMPT = """You are writing a one-project daily standup update for agentra, an
autonomous engineering system. You are given, verbatim, everything this
project's own record-keeping has about the last 24 hours of activity and
its current backlog. Write a short "Yesterday" / "Today" standup update in
plain prose (a few sentences each, not a bullet dump of the raw data).

Rules:
- Only mention things that appear in the data you were given. Never invent
  activity, features, or plans that aren't present.
- If "Yesterday" has no logged activity, say so plainly (e.g. "No activity
  in the last 24 hours") rather than padding it out.
- If "Today" has no open bugs/features/objective, say so plainly.
- No preamble, no sign-off -- just the two sections."""


def _format_input(
    app_name: str,
    yesterday_lines: list[str],
    known_bugs: list[dict],
    feature_queue: list[dict],
    objective: str | None,
) -> str:
    lines = [f"Project: {app_name}", "", "=== Yesterday (last 24h of logged activity) ==="]
    lines.extend(yesterday_lines or ["(none)"])
    lines += ["", "=== Today's backlog ===", f"Objective: {objective or '(none set)'}"]
    lines.append(f"Open known bugs ({len(known_bugs)}):")
    lines.extend([f"- [{b.get('severity')}] {b.get('diagnosis')}" for b in known_bugs] or ["(none)"])
    lines.append(f"Open feature requests ({len(feature_queue)}):")
    lines.extend([f"- {f.get('description')}" for f in feature_queue] or ["(none)"])
    return "\n".join(lines)


async def run_standup(repo: Path, app_name: str, mem: Memory | None = None, window_hours: int = 24) -> str:
    """Generate and persist today's standup for one app. Returns the
    generated report text (also written to .agentra/standups/<date>.md)."""
    if mem is None:
        mem = Memory(repo)

    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=window_hours)
    yesterday_lines = mem.recent_log_lines(since)
    known_bugs = mem.known_bugs()
    feature_queue = mem.feature_queue()
    objective = mem.get_objective()

    if not yesterday_lines and not known_bugs and not feature_queue and not objective:
        # Nothing at all to report -- don't spend an LLM call manufacturing
        # prose around an empty project. Same rule the model itself is
        # given below: no activity/backlog means say so plainly.
        report = (
            "Yesterday: No activity in the last 24 hours.\n\n"
            "Today: No open bugs, feature requests, or objective set."
        )
    else:
        prompt = _format_input(app_name, yesterday_lines, known_bugs, feature_queue, objective)
        result = await run_agent(
            prompt=prompt,
            system_prompt=STANDUP_SYSTEM_PROMPT,
            cwd=repo,
            allowed_tools=[],
            max_turns=1,
        )
        report = result.text if result.ok and result.text else f"(standup generation failed: {result.text or 'no output'})"

    date_str = dt.datetime.now(dt.timezone.utc).date().isoformat()
    mem.record_standup(date_str, report)
    return report


async def run_daily_standup(apps: dict[str, dict]) -> dict[str, str]:
    """Run run_standup for every registered app. `apps` is
    registry.list_apps()'s shape ({name: {"repo_path": ...}}). Returns
    {app_name: report}, skipping (and noting, not raising on) any app
    whose repo_path no longer exists rather than aborting the whole batch
    over one stale registration."""
    reports: dict[str, str] = {}
    for name, info in apps.items():
        repo = Path(info["repo_path"])
        if not repo.is_dir():
            reports[name] = f"(skipped: repo path {repo} does not exist)"
            continue
        reports[name] = await run_standup(repo, name)
    return reports
