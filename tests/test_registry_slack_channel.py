"""registry.set_slack_channel/get_slack_channel: per-app Slack channel override stored directly
on the app's registry entry (GitHub issue #69's "more requirement" -- per-app channel routing
for notify_shipped/notify_human_input_required)."""

from pathlib import Path

from agentra import registry


def _isolate_registry(tmp_path: Path, monkeypatch):
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "_db", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "APPS_PATH", home / "apps.json")
    monkeypatch.setattr(registry, "INBOX_ROOT", home / "inbox")


def test_get_slack_channel_is_none_for_a_freshly_registered_app(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    registry.register_app("myapp", str(tmp_path / "myapp"), repo_url="https://github.com/acme/myapp.git", branch="main")

    assert registry.get_slack_channel("myapp") is None


def test_set_slack_channel_then_get_slack_channel_round_trips(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    registry.register_app("myapp", str(tmp_path / "myapp"), repo_url="https://github.com/acme/myapp.git", branch="main")

    registry.set_slack_channel("myapp", "C0123456789")

    assert registry.get_slack_channel("myapp") == "C0123456789"


def test_set_slack_channel_does_not_clobber_other_registry_fields(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    registry.register_app("myapp", str(tmp_path / "myapp"), repo_url="https://github.com/acme/myapp.git", branch="main")

    registry.set_slack_channel("myapp", "C0123456789")

    entry = registry.list_apps()["myapp"]
    assert entry["repo_url"] == "https://github.com/acme/myapp.git"
    assert entry["branch"] == "main"


def test_get_slack_channel_is_none_for_an_unregistered_app(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)

    assert registry.get_slack_channel("nonexistent") is None
