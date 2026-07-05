"""Per-app local/pre-prod/prod environment configuration.

Every app the system operates on gets one of these, stored at
<repo>/.agentos/environments.yaml. It's the thing that turns "deploy" from a
single generic step into a real pipeline: local (agentos's own branch, fast
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
    local_branch: str = "dev"
    pre_prod_branch: str = "beta"
    prod_branch: str = "main"
    vercel: bool = False
    firebase: bool = False
    firebase_pre_prod_alias: str = "beta"
    firebase_prod_alias: str = "default"
    # Opt-in only. When true, the Production Debugging Agent may deploy a
    # verified hotfix straight to prod once it passes pre-prod testing, with
    # no human approval step. False is the safe default for every app.
    auto_remediate_prod: bool = False

    @property
    def path_hint(self) -> str:
        return ".agentos/environments.yaml"


def config_path(repo: Path) -> Path:
    return repo / ".agentos" / "environments.yaml"


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

    return config


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
