"""standup.py's updates must be self-identifying (agent name + app name) and,
for the no-LLM idle fallback, deterministic. The LLM-generated path's actual
wording quality (natural language, no raw code identifiers) is governed by
STANDUP_SYSTEM_PROMPT and isn't unit-testable without a real/mocked model
call, but the fallback path (_idle_updates, hit when a project has nothing
at all to report) is plain Python and must carry the same "<Agent Name>,
<app name>. ..." shape the real updates use -- a message read on its own
(e.g. in the live standup channel) needs to be self-identifying either way.
"""

import asyncio

from agentra import standup
from agentra.memory import Memory


def test_idle_updates_are_self_identifying_per_agent_and_app():
    updates = standup._idle_updates("my-app")

    assert set(updates) == set(standup.AGENT_LABELS)
    for agent_id, text in updates.items():
        label = standup.AGENT_LABELS[agent_id]
        assert text == f"{label}, my-app. Yesterday: No activity. Today: Idle."


def test_idle_updates_differ_by_app_name():
    a = standup._idle_updates("app-a")
    b = standup._idle_updates("app-b")

    assert a["orchestrator"] != b["orchestrator"]
    assert "app-a" in a["orchestrator"]
    assert "app-b" in b["orchestrator"]


def test_generate_standup_updates_uses_idle_fallback_for_a_genuinely_empty_project(tmp_path):
    """No logged activity, no shipped features, no backlog, no objective --
    the empty-context short-circuit (no LLM call) must still produce
    self-identifying text for this specific app name."""
    repo = tmp_path / "repo"
    repo.mkdir()
    mem = Memory(repo)

    updates = asyncio.run(standup.generate_standup_updates(repo, "empty-app", mem))

    assert updates == standup._idle_updates("empty-app")
    assert all("empty-app" in text for text in updates.values())
