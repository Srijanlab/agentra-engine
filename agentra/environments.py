"""Per-app local/pre-prod/prod environment configuration.

Every app the system operates on gets one of these, stored at
<repo>/.agentra/environments.yaml. It's the thing that turns "deploy" from a
single generic step into a real pipeline: local (agentra's own branch, fast
iteration, nothing deployed) -> pre-prod (isolated Firebase project + Vercel
preview, a real deployed environment for integration testing) -> prod
(gated, or auto-remediated only when the app has explicitly opted in).
"""

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class EnvironmentConfig:
    # Prefix for each cycle's dedicated feature branch (see feature_branch_name
    # below), not a single shared branch -- every feature gets its own
    # "{local_branch}/{run_id}-{feature-slug}" branch, forked fresh from
    # pre-prod's tip, so concurrent/sequential features never pile onto or
    # conflict with each other.
    local_branch: str = "dev"
    pre_prod_branch: str = "beta"
    prod_branch: str = "main"
    vercel: bool = False
    firebase: bool = False
    firebase_pre_prod_alias: str = "beta"
    firebase_prod_alias: str = "default"
    # True when this app already has CI/CD that deploys on push to pre_prod_branch/
    # prod_branch (GitHub Actions, Vercel's git integration, etc.) -- Deployment
    # Agent then only merges + pushes (see agents/deployment.py) and verifies the
    # resulting CI run, rather than also invoking `vercel deploy`/`firebase deploy`
    # itself, which would be redundant with (and could race/conflict with) the
    # pipeline that's about to fire from the same push.
    ci_cd_on_push: bool = False
    # Opt-in only. When true, the Production Debugging Agent may deploy a
    # verified hotfix straight to prod once it passes pre-prod testing, with
    # no human approval step. False is the safe default for every app.
    auto_remediate_prod: bool = False
    # Per-app schedule: how often a scheduled cycle should actually run this
    # app, in hours. The dashboard's dashboard-configurable "one Cloud
    # Scheduler tick decides per-app whether it's due" design (rather than a
    # real Scheduler job per app) -- server.py's /trigger/scheduled checks
    # this against the app's last scheduled run before dispatching, so one
    # cron tick (e.g. hourly) can serve every app on its own cadence without
    # provisioning GCP infra per registration.
    schedule_hours: float = 24.0
    # Per-app opt-out of the alarm-triggered prod-debug path. The GCP
    # Monitoring policy and its webhook are global (one alerting policy,
    # not one per app -- see cloudrun.tf's comment on why /trigger/alarm
    # needs its own Basic Auth instead of per-app IAM), so this is agentra's
    # own routing decision once an incident names this app: True (default)
    # runs prod-debug as normal, False no-ops so an app can be temporarily
    # or permanently excluded without touching the GCP-side policy.
    alarm_enabled: bool = True

    @property
    def path_hint(self) -> str:
        return ".agentra/environments.yaml"


def config_path(repo: Path) -> Path:
    return repo / ".agentra" / "environments.yaml"


def load(repo: Path) -> EnvironmentConfig | None:
    path = config_path(repo)
    if not path.exists():
        return None
    try:
        import yaml

        data = yaml.safe_load(path.read_text()) or {}
    except ImportError:
        data = _naive_yaml_load(path.read_text())
    return EnvironmentConfig(**data)


def save(repo: Path, config: EnvironmentConfig) -> Path:
    path = config_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import yaml

        path.write_text(yaml.safe_dump(asdict(config), sort_keys=False))
    except ImportError:
        path.write_text(_naive_yaml_dump(asdict(config)))
    return path


def _naive_yaml_dump(data: dict) -> str:
    lines = []
    for key, value in data.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        lines.append(f"{key}: {value}")
    return "\n".join(lines) + "\n"


def _naive_yaml_load(text: str) -> dict:
    data: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if value in ("true", "false"):
            data[key.strip()] = value == "true"
        else:
            data[key.strip()] = value
    return data


def detect(repo: Path) -> EnvironmentConfig:
    """Best-effort auto-detection; caller fills gaps interactively."""
    config = EnvironmentConfig()

    vercel_project = repo / ".vercel" / "project.json"
    config.vercel = vercel_project.exists()

    firebaserc = repo / ".firebaserc"
    if firebaserc.exists():
        try:
            data = json.loads(firebaserc.read_text())
            aliases = data.get("projects", {})
            config.firebase = bool(aliases)
            for candidate in ("pre-prod", "pre_prod", "beta", "staging"):
                if candidate in aliases:
                    config.firebase_pre_prod_alias = candidate
                    break
            if "default" in aliases:
                config.firebase_prod_alias = "default"
        except (json.JSONDecodeError, OSError):
            config.firebase = False

    branches = _git_branches(repo)
    for candidate in ("main", "master"):
        if candidate in branches:
            config.prod_branch = candidate
            break
    for candidate in ("pre-prod", "pre_prod", "beta", "staging"):
        if candidate in branches:
            config.pre_prod_branch = candidate
            break
    if "dev" in branches:
        config.local_branch = "dev"
    elif "develop" in branches:
        config.local_branch = "develop"
    elif "local" in branches:
        config.local_branch = "local"

    config.ci_cd_on_push = _has_push_triggered_workflow(repo)

    return config


def _has_push_triggered_workflow(repo: Path) -> bool:
    """Best-effort: any GitHub Actions workflow that triggers on push. Good enough as
    a detected default -- the human confirms/overrides it in `agentra env init`."""
    workflows_dir = repo / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return False
    for path in workflows_dir.glob("*.y*ml"):
        try:
            text = path.read_text()
        except OSError:
            continue
        if "on:" in text and "push:" in text:
            return True
    return False


def slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")[:60]


def feature_branch_name(env: EnvironmentConfig, run_id: str, feature: str) -> str:
    """Dedicated branch for one cycle's feature -- shared by both orchestrator.py's
    fixed sequence and brain.py's dynamic tool loop. Never reuse a branch across
    different run_ids/features (see EnvironmentConfig.local_branch above for why)."""
    return f"{env.local_branch}/{run_id}-{slug(feature)}"


def _git_branches(repo: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "branch", "-a", "--format=%(refname:short)"],
            capture_output=True,
            text=True,
            check=True,
        )
        return [line.strip().removeprefix("origin/") for line in result.stdout.splitlines()]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
