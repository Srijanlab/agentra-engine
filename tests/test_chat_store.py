"""Tests for chat_store.py -- agent chat history, standup reports, the
live standup channel, and session continuity, all server-side (VM-local
disk under AGENTRA_HOME), never the target repo's own git-committed
.agentra/. Namespaced by app_name specifically so two apps' chat/standup
data can never cross-contaminate (the same bug class github_fake.py's
namespacing fix addressed for the GitHub-only backlog).
"""

from agentra import chat_store, registry


def _isolate_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "AGENTRA_HOME", tmp_path / "agentra_home")


# ── Agent chat ──────────────────────────────────────────────────────────


def test_agent_chat_round_trips(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)

    assert chat_store.get_agent_chat_messages("app-a", "codebase") == []
    chat_store.record_agent_chat_message("app-a", "codebase", "human", "hello")
    chat_store.record_agent_chat_message("app-a", "codebase", "agent", "hi there")

    msgs = chat_store.get_agent_chat_messages("app-a", "codebase")
    assert [m["sender"] for m in msgs] == ["human", "agent"]
    assert [m["text"] for m in msgs] == ["hello", "hi there"]


def test_agent_chat_is_namespaced_by_app_and_agent(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)

    chat_store.record_agent_chat_message("app-a", "codebase", "human", "for app-a codebase")
    chat_store.record_agent_chat_message("app-b", "codebase", "human", "for app-b codebase")
    chat_store.record_agent_chat_message("app-a", "testing", "human", "for app-a testing")

    assert [m["text"] for m in chat_store.get_agent_chat_messages("app-a", "codebase")] == ["for app-a codebase"]
    assert [m["text"] for m in chat_store.get_agent_chat_messages("app-b", "codebase")] == ["for app-b codebase"]
    assert [m["text"] for m in chat_store.get_agent_chat_messages("app-a", "testing")] == ["for app-a testing"]


# ── Agent session continuity ───────────────────────────────────────────


def test_agent_session_id_round_trips(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)

    assert chat_store.get_agent_session_id("app-a", "codebase") is None
    chat_store.set_agent_session_id("app-a", "codebase", "session-123")
    assert chat_store.get_agent_session_id("app-a", "codebase") == "session-123"


def test_standup_agent_session_id_is_independent_of_regular_chat_session(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)

    chat_store.set_agent_session_id("app-a", "testing", "regular-chat-session")
    chat_store.set_standup_agent_session_id("app-a", "2026-08-12", "testing", "standup-session")

    assert chat_store.get_agent_session_id("app-a", "testing") == "regular-chat-session"
    assert chat_store.get_standup_agent_session_id("app-a", "2026-08-12", "testing") == "standup-session"


def test_standup_agent_session_id_is_namespaced_by_date(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)

    chat_store.set_standup_agent_session_id("app-a", "2026-08-11", "testing", "yesterday-session")
    chat_store.set_standup_agent_session_id("app-a", "2026-08-12", "testing", "today-session")

    assert chat_store.get_standup_agent_session_id("app-a", "2026-08-11", "testing") == "yesterday-session"
    assert chat_store.get_standup_agent_session_id("app-a", "2026-08-12", "testing") == "today-session"


# ── Standup report ──────────────────────────────────────────────────────


def test_standup_report_round_trips(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)

    assert chat_store.get_standup("app-a", "2026-08-12") is None
    chat_store.record_standup("app-a", "2026-08-12", "# Daily Standup\n\nAll good.")

    assert chat_store.get_standup("app-a", "2026-08-12") == "# Daily Standup\n\nAll good."


def test_latest_standup_returns_the_most_recent_date_with_structured_updates(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)

    chat_store.record_standup("app-a", "2026-08-11", "# Yesterday's report")
    chat_store.record_standup_updates_json("app-a", "2026-08-11", {"testing": "all good yesterday"})
    chat_store.record_standup("app-a", "2026-08-12", "# Today's report")
    chat_store.record_standup_updates_json("app-a", "2026-08-12", {"testing": "all good today"})

    latest = chat_store.latest_standup("app-a")

    assert latest["date"] == "2026-08-12"
    assert latest["content"] == "# Today's report"
    assert latest["updates"] == {"testing": "all good today"}


def test_latest_standup_returns_none_when_nothing_recorded(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)

    assert chat_store.latest_standup("app-a") is None


# ── Live standup channel ────────────────────────────────────────────────


def test_standup_channel_messages_round_trip_in_order(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)

    assert chat_store.get_standup_channel_messages("app-a", "2026-08-12") == []
    chat_store.record_standup_channel_message("app-a", "2026-08-12", "testing", "All green.")
    chat_store.record_standup_channel_message("app-a", "2026-08-12", "human", "Nice work.")

    messages = chat_store.get_standup_channel_messages("app-a", "2026-08-12")
    assert [m["sender"] for m in messages] == ["testing", "human"]


def test_standup_channel_is_namespaced_by_app_and_date(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)

    chat_store.record_standup_channel_message("app-a", "2026-08-12", "testing", "for app-a today")
    chat_store.record_standup_channel_message("app-b", "2026-08-12", "testing", "for app-b today")
    chat_store.record_standup_channel_message("app-a", "2026-08-11", "testing", "for app-a yesterday")

    assert [m["text"] for m in chat_store.get_standup_channel_messages("app-a", "2026-08-12")] == ["for app-a today"]
    assert [m["text"] for m in chat_store.get_standup_channel_messages("app-b", "2026-08-12")] == ["for app-b today"]
    assert [m["text"] for m in chat_store.get_standup_channel_messages("app-a", "2026-08-11")] == ["for app-a yesterday"]
