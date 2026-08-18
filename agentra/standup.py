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
You are given, verbatim, everything this project's own record-keeping has about the last 24 hours of activity, its recently shipped features, and its current backlog.

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
- For each agent, summarize their actual logged activity or shipped features in the last 24 hours for "Yesterday", and their role-specific next step or backlog item for "Today".
- Only mention things that appear in the data you were given. Never invent activity, features, or plans that aren't present.
- If an agent had no logged activity or shipped features, its update should simply say: "Yesterday: No activity. Today: Idle." or similar, do not pad it out.
- Ensure the response contains the fenced JSON block and nothing else.
"""

AGENT_LABELS: dict[str, str] = {
    "orchestrator": "Orchestrator",
    "codebase": "Codebase Agent",
    "discovery": "Discovery Agent",
    "implementation": "Implementation Agent",
    "testing": "Testing Agent",
    "deployment": "Deployment Agent",
    "feedback": "Analytics Feedback Agent",
    "prod_debug": "Production Debugging Agent",
}

_IDLE_UPDATES: dict[str, str] = {
    agent_id: "Yesterday: No activity. Today: Idle." for agent_id in AGENT_LABELS
}


# ---------------------------------------------------------------------------
# Prompt assembly helpers
# ---------------------------------------------------------------------------


def _format_activity_section(yesterday_lines: list[str]) -> list[str]:
    lines = ["=== Yesterday (last 24h of logged activity) ==="]
    lines.extend(yesterday_lines or ["(none)"])
    return lines


def _format_shipped_section(shipped_recently: list[dict]) -> list[str]:
    lines = ["=== Yesterday (last 24h of shipped features) ==="]
    for s in shipped_recently:
        suffix = f" (commit {s['commit_sha']})" if s.get("commit_sha") else ""
        lines.append(f"- {s.get('feature')}{suffix}")
    if not shipped_recently:
        lines.append("(none)")
    return lines


def _format_backlog_section(
    objective: str | None,
    known_bugs: list[dict],
    feature_queue: list[dict],
) -> list[str]:
    lines = [
        "=== Today's backlog ===",
        f"Objective: {objective or '(none set)'}",
        f"Open known bugs ({len(known_bugs)}):",
    ]
    lines.extend([f"- [{b.get('severity')}] {b.get('diagnosis')}" for b in known_bugs] or ["(none)"])
    lines.append(f"Open feature requests ({len(feature_queue)}):")
    lines.extend([f"- {f.get('description')}" for f in feature_queue] or ["(none)"])
    return lines


def _format_input(
    app_name: str,
    yesterday_lines: list[str],
    shipped_recently: list[dict],
    known_bugs: list[dict],
    feature_queue: list[dict],
    objective: str | None,
) -> str:
    lines: list[str] = [f"Project: {app_name}", ""]
    lines += _format_activity_section(yesterday_lines)
    lines += [""]
    lines += _format_shipped_section(shipped_recently)
    lines += [""]
    lines += _format_backlog_section(objective, known_bugs, feature_queue)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Memory extraction helpers
# ---------------------------------------------------------------------------


def _collect_shipped_since(mem: Memory, since: dt.datetime) -> list[dict]:
    result: list[dict] = []
    for s in mem.shipped_features():
        ts = s.get("ts")
        if not ts:
            continue
        try:
            shipped_ts = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if shipped_ts >= since:
                result.append(s)
        except Exception:
            pass
    return result


def _extract_memory_context(
    mem: Memory, window_hours: int
) -> tuple[list[str], list[dict], list[dict], list[dict], str | None]:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=window_hours)
    yesterday_lines = mem.recent_log_lines(since)
    shipped_recently = _collect_shipped_since(mem, since)
    known_bugs = mem.known_bugs()
    feature_queue = mem.feature_queue()
    objective = mem.get_objective()
    return yesterday_lines, shipped_recently, known_bugs, feature_queue, objective


def _is_empty_context(
    yesterday_lines: list[str],
    shipped_recently: list[dict],
    known_bugs: list[dict],
    feature_queue: list[dict],
    objective: str | None,
) -> bool:
    return not any([yesterday_lines, shipped_recently, known_bugs, feature_queue, objective])


# ---------------------------------------------------------------------------
# LLM response parsing
# ---------------------------------------------------------------------------


def _parse_updates_from_result(result) -> dict[str, str]:
    from agentra.agents.base import extract_json_block

    json_data = extract_json_block(result.text) if result.ok and result.text else None
    if json_data and "updates" in json_data:
        return json_data["updates"]

    # Model didn't return the expected shape -- surface whatever it said
    # under the Orchestrator rather than silently dropping it.
    return {
        "orchestrator": result.text
        if result.ok and result.text
        else f"(standup generation failed: {result.text or 'no output'})"
    }


# ---------------------------------------------------------------------------
# Report formatting & persistence
# ---------------------------------------------------------------------------


def _format_report(app_name: str, updates_json: dict[str, str]) -> str:
    lines: list[str] = [f"# Daily Standup - {app_name}", ""]
    for agent_id, text in updates_json.items():
        lines.append(f"## {AGENT_LABELS.get(agent_id, agent_id.capitalize())}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).strip()


def _persist_standup(app_name: str, date_str: str, updates_json: dict[str, str], report: str) -> None:
    chat_store.record_standup(app_name, date_str, report)
    chat_store.record_standup_updates_json(app_name, date_str, updates_json)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def generate_standup_updates(
    repo: Path, app_name: str, mem: Memory, window_hours: int = 24
) -> dict[str, str]:
    """The one LLM call this whole module makes: turns real logged
    activity/backlog into a {agent_id: "Yesterday: ... Today: ..."} dict,
    one entry per agent. Split out from run_standup (which turns this into
    a persisted markdown report) so server.py's live standup channel can
    post each agent's line as its own message without a second LLM call or
    a copy of this prompt-building logic."""
    yesterday_lines, shipped_recently, known_bugs, feature_queue, objective = (
        _extract_memory_context(mem, window_hours)
    )

    if _is_empty_context(yesterday_lines, shipped_recently, known_bugs, feature_queue, objective):
        # Nothing at all to report -- don't spend an LLM call manufacturing
        # prose around an empty project. Same rule the model itself is
        # given below: no activity/backlog means say so plainly.
        return _IDLE_UPDATES

    prompt = _format_input(app_name, yesterday_lines, shipped_recently, known_bugs, feature_queue, objective)
    result = await run_agent(
        prompt=prompt,
        system_prompt=STANDUP_SYSTEM_PROMPT,
        cwd=repo,
        allowed_tools=[],
        max_turns=1,
    )

    return _parse_updates_from_result(result)


async def get_or_generate_standup_updates(
    repo: Path,
    app_name: str,
    mem: Memory,
    date_str: str | None = None,
    window_hours: int = 24,
) -> tuple[dict[str, str], bool]:
    """The single 'does a standup for this app already exist today (UTC
    calendar date)' check shared by all three entry points -- the live WS
    channel, POST /apps/{app}/standup, and POST /standup/daily -- so they
    treat it as one question instead of each deciding independently.

    Reuses the durable report-store entry for `date_str` verbatim (no LLM
    call) if one was already generated -- by any entry point -- earlier
    that day; otherwise generates one via generate_standup_updates (the
    one LLM call, unchanged) and persists it there so every other entry
    point sees it too. Returns (updates, was_freshly_generated) so callers
    (the WS route in particular) can tell a genuinely fresh generation
    apart from a reused one.
    """
    if date_str is None:
        date_str = dt.datetime.now(dt.timezone.utc).date().isoformat()

    existing = chat_store.get_standup_updates_json(app_name, date_str)
    if existing is not None:
        return existing, False

    updates_json = await generate_standup_updates(repo, app_name, mem, window_hours)
    report = _format_report(app_name, updates_json)
    _persist_standup(app_name, date_str, updates_json, report)
    return updates_json, True


async def run_standup(
    repo: Path, app_name: str, mem: Memory | None = None, window_hours: int = 24
) -> str:
    """Generate (or, if one already exists for today, reuse) today's
    standup for one app and persist it server-side (chat_store.py,
    AGENTRA_HOME -- not the target repo's own .agentra/, which is
    git-committed to that repo's history and not where a chat
    transcript-adjacent artifact belongs). Returns the report text --
    identical, byte-for-byte, to what any other entry point already
    generated for this app today, if applicable."""
    if mem is None:
        mem = Memory(repo)

    updates_json, _ = await get_or_generate_standup_updates(repo, app_name, mem, window_hours=window_hours)
    return _format_report(app_name, updates_json)


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
