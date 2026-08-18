"""Per-app local/pre-prod/prod environment configuration.

Every app the system operates on gets one of these, stored at
<repo>/.agentra/environments.yaml. It's the thing that turns "deploy" from a
single generic step into a real pipeline: local (agentra's own branch, fast
iteration, nothing deployed) -> pre-prod (isolated Firebase project + Vercel
preview, a real deployed environment for integration testing) -> prod
(gated, or auto-remediated only when the app has explicitly opted in).
"""

import json
import logging
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


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
    # Which deployment mechanism agents/deployment.py's PRE_PROD_STRATEGIES/
    # PROD_STRATEGIES registries dispatch to. "vercel_firebase" (default)
    # drives the Vercel/Firebase CLIs via an LLM agent turn. "self_hosted_vm"
    # is deterministic Docker-CLI plumbing against a VM the app already runs
    # on -- requires that repo's own .agentra/memory/architecture/
    # deployment.md (see environments.load_self_hosted_vm_config); a repo
    # that selects this strategy without that file gets a clear error, not a
    # guess at some other app's VM details.
    deploy_strategy: str = "vercel_firebase"
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
        return "GitHub Actions repo variables (AGENTRA_*)"


# GitHub Actions repository Variables are the ONLY store for this config --
# no local file, no fallback. One variable per field so each is
# individually browsable/editable in GitHub's own UI (Settings -> Secrets
# and variables -> Actions -> Variables), not one opaque blob. A repo with
# no github.com remote, or an unreachable/unconfigured GitHub App, simply
# cannot have its environment configured at all -- a deliberate
# availability tradeoff (see memory.py's module docstring for the same
# choice applied to known_bugs/feature_queue/objective).
_GITHUB_VARIABLE_NAMES = {
    "local_branch": "AGENTRA_LOCAL_BRANCH",
    "pre_prod_branch": "AGENTRA_PRE_PROD_BRANCH",
    "prod_branch": "AGENTRA_PROD_BRANCH",
    "vercel": "AGENTRA_VERCEL",
    "firebase": "AGENTRA_FIREBASE",
    "firebase_pre_prod_alias": "AGENTRA_FIREBASE_PRE_PROD_ALIAS",
    "firebase_prod_alias": "AGENTRA_FIREBASE_PROD_ALIAS",
    "ci_cd_on_push": "AGENTRA_CI_CD_ON_PUSH",
    "auto_remediate_prod": "AGENTRA_AUTO_REMEDIATE_PROD",
    "schedule_hours": "AGENTRA_SCHEDULE_HOURS",
    "alarm_enabled": "AGENTRA_ALARM_ENABLED",
    "deploy_strategy": "AGENTRA_DEPLOY_STRATEGY",
}
_BOOL_FIELDS = {"vercel", "firebase", "ci_cd_on_push", "auto_remediate_prod", "alarm_enabled"}
_FLOAT_FIELDS = {"schedule_hours"}


def _repo_url(repo: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"], capture_output=True, text=True, timeout=10
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _coerce(field: str, raw: str):
    if field in _BOOL_FIELDS:
        return raw.strip().lower() == "true"
    if field in _FLOAT_FIELDS:
        try:
            return float(raw)
        except ValueError:
            return None
    return raw


def load(repo: Path) -> EnvironmentConfig | None:
    repo_url = _repo_url(repo)
    if not repo_url:
        logger.error("environments.load: %s has no github.com remote -- no environment config is visible at all", repo)
        return None
    data: dict = {}
    found_anything = False
    try:
        from agentra.connectors import github_variables

        remote_vars = github_variables.list_variables(repo_url)
        for field, var_name in _GITHUB_VARIABLE_NAMES.items():
            if var_name not in remote_vars:
                continue
            coerced = _coerce(field, remote_vars[var_name])
            if coerced is not None:
                data[field] = coerced
                found_anything = True
    except Exception:
        logger.error("environments.load: GitHub Variables unavailable for %s -- environment config is unreadable until it recovers", repo_url, exc_info=True)
        return None

    if not found_anything:
        return None
    return EnvironmentConfig(**data)


def save(repo: Path, config: EnvironmentConfig) -> None:
    repo_url = _repo_url(repo)
    if not repo_url:
        logger.error("environments.save: %s has no github.com remote -- environment config was NOT saved anywhere", repo)
        return
    try:
        from agentra.connectors import github_variables

        data = asdict(config)
        for field, var_name in _GITHUB_VARIABLE_NAMES.items():
            value = data[field]
            str_value = ("true" if value else "false") if field in _BOOL_FIELDS else str(value)
            github_variables.set_variable(repo_url, var_name, str_value)
    except Exception:
        logger.error("environments.save: failed to save to GitHub Variables for %s -- environment config was NOT saved anywhere", repo_url, exc_info=True)


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


@dataclass
class SelfHostedVMConfig:
    """The 'self_hosted_vm' deploy_strategy's own connection/topology details
    for THIS repo's VM -- deliberately not GitHub Variables like
    EnvironmentConfig's fields: these are infrastructure identifiers tied to
    a specific, already-provisioned VM, versioned with the code that assumes
    them, not admin-togglable runtime config. See load_self_hosted_vm_config."""

    vm_name: str
    vm_zone: str
    gcp_project: str
    image_repo: str
    anchor_container: str
    app_network: str
    preprod_network: str
    # Host-side (VM) path to the persistent data disk -- e.g.
    # "/mnt/disks/agentra-data", holding claude/, agentra-home/, repos/
    # subdirectories. Docker commands issued from inside a container run
    # against the HOST's own daemon (via the bind-mounted docker.sock), so
    # -v source paths must be real HOST paths, never a path resolved from
    # inside the calling container's own filesystem -- see
    # deploy_pre_prod_self_hosted/promote_prod_self_hosted in
    # agents/deployment.py for where this actually gets used.
    data_mount: str
    firestore_project: str | None = None


_SELF_HOSTED_VM_CONFIG_PATH = Path(".agentra") / "memory" / "architecture" / "deployment.md"
_SELF_HOSTED_VM_CONFIG_BLOCK_RE = re.compile(r"```ya?ml\s*\n(.*?)```", re.DOTALL)

# self_hosted_vm is only ever safe for a repo that's actually co-located with
# the orchestrator process on the same machine: /var/run/docker.sock grants
# access to THIS host's Docker daemon only, never a different app's VM. If
# some other app's repo ever ended up with a deployment.md (a copy-paste
# mistake, a misconfigured autonomous cycle), running its container/network
# names through THIS host's docker.sock wouldn't fail loudly -- it would
# silently operate on agentra's own VM's resources under that other app's
# name, since that's the only daemon reachable at all. An allowlist, not
# just "does this file exist", is the actual safety boundary here -- until a
# real remote-Docker-context/SSH execution path exists for other apps.
_SELF_HOSTED_VM_ALLOWED_REPO_URLS = {
    "https://github.com/Srijanlab/srijanlab-agentra.git",
}


def load_self_hosted_vm_config(repo: Path) -> SelfHostedVMConfig | None:
    """Reads <repo>/.agentra/memory/architecture/deployment.md's fenced YAML
    config block -- same fenced-block-in-a-committed-file convention as
    github_issue_lifecycle.py's record_spec/get_spec (there, GitHub-comment
    JSON; here, a git-committed YAML block, since these values belong to the
    repo's own history, not a mutable issue thread).

    None if the file doesn't exist, its config block is missing/malformed, or
    repo isn't on _SELF_HOSTED_VM_ALLOWED_REPO_URLS -- callers
    (deploy_pre_prod_self_hosted, promote_prod_self_hosted) must treat that as
    'self_hosted_vm strategy selected but not usable for this repo' and
    report a clear error, never fall back to guessing another app's VM
    details."""
    if _repo_url(repo) not in _SELF_HOSTED_VM_ALLOWED_REPO_URLS:
        logger.warning(
            "load_self_hosted_vm_config: %s is not on the self_hosted_vm allowlist -- "
            "this strategy only works for a repo co-located with the orchestrator's own "
            "Docker daemon, see _SELF_HOSTED_VM_ALLOWED_REPO_URLS's comment", repo,
        )
        return None
    path = repo / _SELF_HOSTED_VM_CONFIG_PATH
    if not path.exists():
        return None
    try:
        text = path.read_text()
    except OSError:
        logger.warning("load_self_hosted_vm_config: could not read %s", path, exc_info=True)
        return None
    match = _SELF_HOSTED_VM_CONFIG_BLOCK_RE.search(text)
    if not match:
        logger.warning("load_self_hosted_vm_config: %s has no fenced yaml config block", path)
        return None
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        logger.warning("load_self_hosted_vm_config: %s's config block is not valid YAML", path, exc_info=True)
        return None
    required = {
        "vm_name", "vm_zone", "gcp_project", "image_repo",
        "anchor_container", "app_network", "preprod_network", "data_mount",
    }
    missing = required - data.keys()
    if missing:
        logger.warning("load_self_hosted_vm_config: %s is missing required field(s): %s", path, sorted(missing))
        return None
    try:
        return SelfHostedVMConfig(**{k: v for k, v in data.items() if k in required | {"firestore_project"}})
    except TypeError:
        logger.warning("load_self_hosted_vm_config: %s's config block has an unexpected shape", path, exc_info=True)
        return None
