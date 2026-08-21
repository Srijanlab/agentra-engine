"""TASK-019: daily standup, per registered project."""

import datetime as dt
from pathlib import Path

from agentra import chat_store
from agentra.agents.base import run_agent
from agentra.memory import Memory

STANDUP_SYSTEM_PROMPT = """You are writing a daily standup update for agentra, an autonomous engineering system.
You are given, verbatim, everything this project's own record-keeping has about the last 24 hours of activity, its recently shipped features, and its current backlog.

The input starts with a "Project: <app name>" line -- read the app name from there.

Write a daily standup update structured by agent. Respond with a JSON block containing the standup updates for each agent. The JSON block must look exactly like this (using the real agent names and the real app name from the input in place of the bracketed placeholders):
```json
{
  "updates": {
    "orchestrator": "Orchestrator, [app name]. Yesterday: [one plain-English sentence]. Today: [one plain-English sentence].",
    "codebase": "Codebase Agent, [app name]. Yesterday: [...]. Today: [...].",
    "discovery": "Discovery Agent, [app name]. Yesterday: [...]. Today: [...].",
    "implementation": "Implementation Agent, [app name]. Yesterday: [...]. Today: [...].",
    "testing": "Testing Agent, [app name]. Yesterday: [...]. Today: [...].",
    "deployment": "Deployment Agent, [app name]. Yesterday: [...]. Today: [...].",
    "feedback": "Analytics Feedback Agent, [app name]. Yesterday: [...]. Today: [...].",
    "prod_debug": "Production Debugging Agent, [app name]. Yesterday: [...]. Today: [...]."
  }
}
```

Rules:
- Open every update with "<Agent Name>, <app name>." exactly as shown above, so each update is self-identifying even read on its own, out of context.
- Write the way a person would actually talk in a standup meeting, not a commit message or a code diff. Say what changed and why it matters in plain English -- never paste raw function/variable names, snake_case identifiers, file paths, or other code syntax verbatim. "Fixed a bug where pre-prod couldn't reach the deployed app over the network" is right; "added _own_container_id/_own_container_name helpers and joined preprod_network" is not -- paraphrase it.
- One short, concrete sentence each for "Yesterday" and "Today" -- this is a standup, not a changelog. If there's a lot going on, pick the single most important thing, don't list everything.
- For each agent, base "Yesterday" on their actual logged activity or shipped features in the last 24 hours, and "Today" on their role-specific next step or backlog item.
- Only mention things that appear in the data you were given. Never invent activity, features, or plans that aren't present.
- If an agent had no logged activity or shipped features, its update should simply say: "<Agent Name>, <app name>. Yesterday: No activity. Today: Idle." -- do not pad it out.
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

def _idle_updates(app_name: str) -> dict[str, str]:
    """The no-LLM-call fallback for a genuinely empty project (see _is_empty_context) -- kept in the same "<Agent Name>, <app name>."""
    return {
        agent_id: f"{label}, {app_name}. Yesterday: No activity. Today: Idle."
        for agent_id, label in AGENT_LABELS.items()
    }


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


def _parse_updates_from_result(result) -> dict[str, str]:
    from agentra.agents.base import extract_json_block

    json_data = extract_json_block(result.text) if result.ok and result.text else None
    if json_data and "updates" in json_data:
        return json_data["updates"]

    # Model didn't return the expected shape -- surface whatever it said
    return {
        "orchestrator": result.text
        if result.ok and result.text
        else f"(standup generation failed: {result.text or 'no output'})"
    }


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


async def generate_standup_updates(
    repo: Path, app_name: str, mem: Memory, window_hours: int = 24
) -> dict[str, str]:
    """The one LLM call this whole module makes: turns real logged activity/backlog into a {agent_id: "Yesterday: ..."""
    yesterday_lines, shipped_recently, known_bugs, feature_queue, objective = (
        _extract_memory_context(mem, window_hours)
    )

    if _is_empty_context(yesterday_lines, shipped_recently, known_bugs, feature_queue, objective):
        # Nothing at all to report -- don't spend an LLM call manufacturing
        return _idle_updates(app_name)

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
    """The single 'does a standup for this app already exist today (UTC calendar date)' check shared by all three entry points -- the live WS channel, POST /apps/{app}/standup, and POST /standup/daily -- so they treat it as one question instead of each deciding independently."""
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
    """Generate (or, if one already exists for today, reuse) today's standup for one app and persist it server-side (chat_store.py, AGENTRA_HOME -- not the target repo's own .agentra/, which is git-committed to that repo's history and not where a chat transcript-adjacent artifact belongs)."""
    if mem is None:
        mem = Memory(repo)

    updates_json, _ = await get_or_generate_standup_updates(repo, app_name, mem, window_hours=window_hours)
    return _format_report(app_name, updates_json)


async def run_daily_standup(apps: dict[str, dict]) -> dict[str, str]:
    """Run run_standup for every registered app."""
    from agentra import registry

    reports: dict[str, str] = {}
    for name, info in apps.items():
        repo = Path(info["repo_path"])
        if not repo.is_dir():
            reports[name] = f"(skipped: repo path {repo} does not exist)"
            continue
        reports[name] = await run_standup(repo, name)
        # record_standup() (inside run_standup) only writes to this
        registry.persist_agentra_dir(repo, info.get("branch") or "main", f"agentra: daily standup for {name!r}")
    return reports
