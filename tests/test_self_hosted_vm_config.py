"""environments.load_self_hosted_vm_config -- reads <repo>/.agentra/memory/
architecture/deployment.md's fenced YAML config block. Every identifier
deploy_pre_prod_self_hosted/promote_prod_self_hosted use (network names,
anchor container, image repo, data mount path) comes from here, not from any
hardcoded agentra-specific constant -- see agents/deployment.py.
"""

from pathlib import Path

from agentra import environments

_VALID_BLOCK = """# Self-hosted VM deployment config

```yaml
vm_name: widget-vm
vm_zone: us-east1-b
gcp_project: widget-prod
image_repo: registry.example.com/acme/widget-app
anchor_container: widget-proxy
app_network: widget-app-net
preprod_network: widget-preprod-net
data_mount: /mnt/disks/widget-data
firestore_project: widget-prod
```
"""


def _write_config(repo: Path, text: str) -> None:
    path = repo / ".agentra" / "memory" / "architecture" / "deployment.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_load_self_hosted_vm_config_parses_a_valid_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo, _VALID_BLOCK)

    config = environments.load_self_hosted_vm_config(repo)

    assert config is not None
    assert config.vm_name == "widget-vm"
    assert config.image_repo == "registry.example.com/acme/widget-app"
    assert config.anchor_container == "widget-proxy"
    assert config.app_network == "widget-app-net"
    assert config.preprod_network == "widget-preprod-net"
    assert config.data_mount == "/mnt/disks/widget-data"
    assert config.firestore_project == "widget-prod"


def test_load_self_hosted_vm_config_firestore_project_is_optional(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    text = _VALID_BLOCK.replace("firestore_project: widget-prod\n", "")
    _write_config(repo, text)

    config = environments.load_self_hosted_vm_config(repo)

    assert config is not None
    assert config.firestore_project is None


def test_load_self_hosted_vm_config_returns_none_when_file_is_missing(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    assert environments.load_self_hosted_vm_config(repo) is None


def test_load_self_hosted_vm_config_returns_none_when_no_fenced_block(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo, "# Self-hosted VM deployment config\n\nNo config block here.\n")

    assert environments.load_self_hosted_vm_config(repo) is None


def test_load_self_hosted_vm_config_returns_none_on_missing_required_field(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    text = _VALID_BLOCK.replace("anchor_container: widget-proxy\n", "")
    _write_config(repo, text)

    assert environments.load_self_hosted_vm_config(repo) is None


def test_load_self_hosted_vm_config_returns_none_on_malformed_yaml(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo, "```yaml\nvm_name: [unclosed\n```\n")

    assert environments.load_self_hosted_vm_config(repo) is None
