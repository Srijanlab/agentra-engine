"""Agent chat history, standup reports, and the live standup channel -- server-side (VM-local disk under AGENTRA_HOME), never the target repo's own .agentra/."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from agentra import registry


def _app_root(app_name: str) -> Path:
    return registry.AGENTRA_HOME / "chat_store" / app_name


def _read_json_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


# ── Agent chat ──────────────────────────────────────────────────────────

def _agent_chat_path(app_name: str, agent_id: str) -> Path:
    return _app_root(app_name) / "agent_chats" / f"{agent_id}.json"


def get_agent_chat_messages(app_name: str, agent_id: str) -> list[dict]:
    return _read_json_list(_agent_chat_path(app_name, agent_id))


def record_agent_chat_message(app_name: str, agent_id: str, sender: str, text: str) -> dict:
    path = _agent_chat_path(app_name, agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    messages = get_agent_chat_messages(app_name, agent_id)
    message = {"sender": sender, "text": text, "ts": dt.datetime.now(dt.timezone.utc).isoformat()}
    messages.append(message)
    path.write_text(json.dumps(messages, indent=2))
    return message


# ── Agent session continuity: lets server.py resume the exact prior

def _agent_session_path(app_name: str, agent_id: str) -> Path:
    return _app_root(app_name) / "agent_sessions" / f"{agent_id}.json"


def get_agent_session_id(app_name: str, agent_id: str) -> str | None:
    path = _agent_session_path(app_name, agent_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text()).get("session_id")
    except Exception:
        return None


def set_agent_session_id(app_name: str, agent_id: str, session_id: str) -> None:
    path = _agent_session_path(app_name, agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"session_id": session_id}))


def _standup_agent_session_path(app_name: str, date_str: str, agent_id: str) -> Path:
    return _app_root(app_name) / "agent_sessions" / "standup" / date_str / f"{agent_id}.json"


def get_standup_agent_session_id(app_name: str, date_str: str, agent_id: str) -> str | None:
    path = _standup_agent_session_path(app_name, date_str, agent_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text()).get("session_id")
    except Exception:
        return None


def set_standup_agent_session_id(app_name: str, date_str: str, agent_id: str, session_id: str) -> None:
    path = _standup_agent_session_path(app_name, date_str, agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"session_id": session_id}))


# ── Standup report ──────────────────────────────────────────────────────

def _standups_root(app_name: str) -> Path:
    return _app_root(app_name) / "standups"


def record_standup(app_name: str, date_str: str, content: str) -> Path:
    root = _standups_root(app_name)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{date_str}.md"
    path.write_text(content)
    return path


def record_standup_updates_json(app_name: str, date_str: str, updates: dict) -> Path:
    root = _standups_root(app_name)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{date_str}.json"
    path.write_text(json.dumps(updates, indent=2))
    return path


def get_standup(app_name: str, date_str: str) -> str | None:
    path = _standups_root(app_name) / f"{date_str}.md"
    return path.read_text() if path.exists() else None


def get_standup_updates_json(app_name: str, date_str: str) -> dict | None:
    """The structured per-agent updates for one specific UTC calendar day (unlike latest_standup, which always reports the most recent date on disk)."""
    path = _standups_root(app_name) / f"{date_str}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def latest_standup(app_name: str) -> dict | None:
    root = _standups_root(app_name)
    if not root.is_dir():
        return None
    dated = sorted(root.glob("*.md"), key=lambda p: p.stem)
    if not dated:
        return None
    latest = dated[-1]
    date_str = latest.stem
    json_path = root / f"{date_str}.json"
    updates = None
    if json_path.exists():
        try:
            updates = json.loads(json_path.read_text())
        except Exception:
            pass
    return {"date": date_str, "content": latest.read_text(), "updates": updates}


# ── Live standup channel: one shared transcript per day (not per-agent,

def _standup_channel_path(app_name: str, date_str: str) -> Path:
    return _standups_root(app_name) / f"{date_str}-channel.json"


def get_standup_channel_messages(app_name: str, date_str: str) -> list[dict]:
    return _read_json_list(_standup_channel_path(app_name, date_str))


def record_standup_channel_message(app_name: str, date_str: str, sender: str, text: str) -> dict:
    path = _standup_channel_path(app_name, date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    messages = get_standup_channel_messages(app_name, date_str)
    message = {"sender": sender, "text": text, "ts": dt.datetime.now(dt.timezone.utc).isoformat()}
    messages.append(message)
    path.write_text(json.dumps(messages, indent=2))
    return message
