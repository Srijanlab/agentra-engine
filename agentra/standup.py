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

from agentra import chat_store
from agentra.agents.base import run_agent
from agentra.memory import Memory

STANDUP_SYSTEM_PROMPT = """You are writing a daily standup update for agentra, an autonomous engineering system.
You are given, verbatim, everything this project's own record-keeping has about the last 24 hours of activity, its recent work updates from agents, and its current backlog.

Write a daily standup update structured by agent. Respond with a JSON block containing the standup updates for each agent. The JSON block must look exactly like this:
```json
{
  "updates": {
    "orchestrator": "Yesterday: [brief summary of orchestrator activity]. Today: [brief plan].",
    "codebase": "Yesterday: [brief summary]. Today: [brief plan].",
    "discovery": "Yesterday: [brief summary]. Today: [brief plan].",
    "implementation": "Yesterday: [brief summary]. Today: [brief plan].",
    "testing": "Yesterday: [brief summary]. Today: [brief plan].",
    "deployment": "Yesterday: [brief summary]. Today: [brief plan].",
    "feedback": "Yesterday: [brief summary]. Today: [brief plan].",
    "prod_debug": "Yesterday: [brief summary]. Today: [brief plan]."
  }
}
```

Rules:
- For each agent, summarize their actual logged activity or work updates in the last 24 hours for "Yesterday", and their role-specific next step or backlog item for "Today".
- Only mention things that appear in the data you were given. Never invent activity, features, or plans that aren't present.
- If an agent had no logged activity or work updates, its update should simply say: "Yesterday: No activity. Today: Idle." or similar, do not pad it out.
- Ensure the response contains the fenced JSON block and nothing else.
"""


def _format_input(
    app_name: str,
    yesterday_lines: list[str],
    work_updates: list[dict],
    known_bugs: list[dict],
    feature_queue: list[dict],
    objective: str | None,
) -> str:
    lines = [f"Project: {app_name}", "", "=== Yesterday (last 24h of logged activity) ==="]
    lines.extend(yesterday_lines or ["(none)"])
    lines += ["", "=== Yesterday (last 24h of agent work updates) ==="]
    for wu in work_updates:
        lines.append(f"[{wu.get('agent_id')}] {wu.get('description')}")
    if not work_updates:
        lines.append("(none)")
    lines += ["", "=== Today's backlog ===", f"Objective: {objective or '(none set)'}"]
    lines.append(f"Open known bugs ({len(known_bugs)}):")
    lines.extend([f"- [{b.get('severity')}] {b.get('diagnosis')}" for b in known_bugs] or ["(none)"])
    lines.append(f"Open feature requests ({len(feature_queue)}):")
    lines.extend([f"- {f.get('description')}" for f in feature_queue] or ["(none)"])
    return "\n".join(lines)


AGENT_LABELS = {
    "orchestrator": "Orchestrator",
    "codebase": "Codebase Agent",
    "discovery": "Discovery Agent",
    "implementation": "Implementation Agent",
    "testing": "Testing Agent",
    "deployment": "Deployment Agent",
    "feedback": "Analytics Feedback Agent",
    "prod_debug": "Production Debugging Agent",
}


async def generate_standup_updates(
    repo: Path, app_name: str, mem: Memory, window_hours: int = 24
) -> dict[str, str]:
    """The one LLM call this whole module makes: turns real logged
    activity/backlog into a {agent_id: "Yesterday: ... Today: ..."} dict,
    one entry per agent. Split out from run_standup (which turns this into
    a persisted markdown report) so server.py's live standup channel can
    post each agent's line as its own message without a second LLM call or
    a copy of this prompt-building logic."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=window_hours)
    yesterday_lines = mem.recent_log_lines(since)

    all_work_updates = mem.get_work_updates()
    work_updates = []
    for wu in all_work_updates:
        if "ts" in wu:
            try:
                ts = dt.datetime.fromisoformat(wu["ts"])
                if ts >= since:
                    work_updates.append(wu)
            except Exception:
                pass

    known_bugs = mem.known_bugs()
    feature_queue = mem.feature_queue()
    objective = mem.get_objective()

    from agentra.agents.base import extract_json_block

    if not yesterday_lines and not work_updates and not known_bugs and not feature_queue and not objective:
        # Nothing at all to report -- don't spend an LLM call manufacturing
        # prose around an empty project. Same rule the model itself is
        # given below: no activity/backlog means say so plainly.
        return {agent_id: "Yesterday: No activity. Today: Idle." for agent_id in AGENT_LABELS}

    prompt = _format_input(app_name, yesterday_lines, work_updates, known_bugs, feature_queue, objective)
    result = await run_agent(
        prompt=prompt,
        system_prompt=STANDUP_SYSTEM_PROMPT,
        cwd=repo,
        allowed_tools=[],
        max_turns=1,
    )

    json_data = extract_json_block(result.text) if result.ok and result.text else None
    if json_data and "updates" in json_data:
        return json_data["updates"]

    # Model didn't return the expected shape -- surface whatever it said
    # under the Orchestrator rather than silently dropping it.
    return {"orchestrator": result.text if result.ok and result.text else f"(standup generation failed: {result.text or 'no output'})"}


async def run_standup(repo: Path, app_name: str, mem: Memory | None = None, window_hours: int = 24) -> str:
    """Generate today's standup for one app and persist it server-side
    (chat_store.py, AGENTRA_HOME -- not the target repo's own .agentra/,
    which is git-committed to that repo's history and not where a chat
    transcript-adjacent artifact belongs). Returns the generated report
    text."""
    if mem is None:
        mem = Memory(repo)

    updates_json = await generate_standup_updates(repo, app_name, mem, window_hours)

    lines = [f"# Daily Standup - {app_name}", ""]
    for agent_id, text in updates_json.items():
        lines.append(f"## {AGENT_LABELS.get(agent_id, agent_id.capitalize())}")
        lines.append(text)
        lines.append("")
    report = "\n".join(lines).strip()

    date_str = dt.datetime.now(dt.timezone.utc).date().isoformat()
    chat_store.record_standup(app_name, date_str, report)
    chat_store.record_standup_updates_json(app_name, date_str, updates_json)

    return report


async def run_daily_standup(apps: dict[str, dict]) -> dict[str, str]:
    """Run run_standup for every registered app. `apps` is
    registry.list_apps()'s shape ({name: {"repo_path": ...}}). Returns
    {app_name: report}, skipping (and noting, not raising on) any app
    whose repo_path no longer exists rather than aborting the whole batch
    over one stale registration."""
    from agentra import registry

    reports: dict[str, str] = {}
    for name, info in apps.items():
        repo = Path(info["repo_path"])
        if not repo.is_dir():
            reports[name] = f"(skipped: repo path {repo} does not exist)"
            continue
        reports[name] = await run_standup(repo, name)
        # record_standup() (inside run_standup) only writes to this
        # instance's local checkout -- not durable on its own, same as
        # every other .agentra/ write path. See persist_agentra_dir's
        # docstring for the cross-instance-loss story this prevents.
        registry.persist_agentra_dir(repo, info.get("branch") or "main", f"agentra: daily standup for {name!r}")
    return reports
