"""OrchestratorSession's multi-repo additions (Phase 2): code_repos/active_repo/
cb_summaries, env_for's per-repo lazy-loaded+cached EnvironmentConfig, and
set_active_repo's env/cb_summary swap -- plus the app_name fix (a multi-repo
app's coordination repo isn't named after the app, so app_name can't just be
repo.name)."""

from pathlib import Path

from agentra.agents import brain
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory
from agentra.registry.core import RepoSpec


def _session(tmp_path: Path, **overrides) -> brain.OrchestratorSession:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    defaults = dict(
        repo=repo,
        objective="test objective",
        env=EnvironmentConfig(),
        mem=Memory(repo),
        run_id="testrun1",
    )
    defaults.update(overrides)
    return brain.OrchestratorSession(**defaults)


def test_app_name_falls_back_to_repo_name_when_unset(tmp_path):
    session = _session(tmp_path)
    assert session.app_name == "repo"


def test_app_name_uses_the_real_registered_name_when_set(tmp_path):
    # Regression: a multi-repo app's coordination RepoSpec is not named after
    # the app (e.g. agentra's coordination repo is named "backlog") -- app_name
    # must come from the registry, not repo.name, once it's threaded in.
    session = _session(tmp_path, _app_name="agentra")
    assert session.app_name == "agentra"


def test_env_for_loads_and_caches_per_repo(tmp_path, monkeypatch):
    engine_repo = tmp_path / "engine"
    ui_repo = tmp_path / "ui"
    engine_repo.mkdir()
    ui_repo.mkdir()
    calls = []

    def _fake_load(path):
        calls.append(path)
        return EnvironmentConfig(vercel=(path == engine_repo), firebase=(path == ui_repo))

    monkeypatch.setattr("agentra.environments.load", _fake_load)
    session = _session(
        tmp_path,
        code_repos={
            "engine": RepoSpec(name="engine", path=engine_repo, repo_url=None, branch="main", role="code"),
            "ui": RepoSpec(name="ui", path=ui_repo, repo_url=None, branch="main", role="code"),
        },
    )

    engine_env = session.env_for("engine")
    ui_env = session.env_for("ui")

    assert engine_env.vercel is True and engine_env.firebase is False
    assert ui_env.vercel is False and ui_env.firebase is True
    assert len(calls) == 2

    # Second call to the same repo hits the cache -- no second environments.load call.
    session.env_for("engine")
    assert len(calls) == 2


def test_env_for_defaults_when_load_returns_none(tmp_path, monkeypatch):
    repo = tmp_path / "no-remote"
    repo.mkdir()
    monkeypatch.setattr("agentra.environments.load", lambda path: None)
    session = _session(
        tmp_path,
        code_repos={"solo": RepoSpec(name="solo", path=repo, repo_url=None, branch="main", role="code")},
    )

    env = session.env_for("solo")

    assert env == EnvironmentConfig()


def test_set_active_repo_swaps_env_and_cb_summary(tmp_path, monkeypatch):
    engine_repo = tmp_path / "engine"
    engine_repo.mkdir()
    engine_env = EnvironmentConfig(vercel=True)
    monkeypatch.setattr("agentra.environments.load", lambda path: engine_env)
    session = _session(
        tmp_path,
        code_repos={"engine": RepoSpec(name="engine", path=engine_repo, repo_url=None, branch="main", role="code")},
        cb_summaries={"engine": "engine's codebase summary"},
    )

    session.set_active_repo("engine")

    assert session.active_repo == "engine"
    assert session.env is engine_env
    assert session.cb_summary == "engine's codebase summary"
