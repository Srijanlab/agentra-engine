"""Resolves this process's own Docker container identity and manages joining
it to a Docker network -- split out of deployment.py (SRP: that module owns
the deploy pipeline itself, this one owns "which container am I, and is it on
the network it needs to be on").

Exists because of a real production bug: deploy_pre_prod_self_hosted used to
trust a single hardcoded source (the HOSTNAME env var) for its own container
identity, and fire-and-forget a `docker network connect` without ever
verifying it actually took effect -- so a stale HOSTNAME or a silently-failed
connect left the orchestrator's own container never actually joined to
agentra-preprod-net, and the only symptom was an opaque `curl` timeout much
later against an address that was never reachable to begin with.
"""

import json
import os
import re
import subprocess
from pathlib import Path

_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}")


def _own_container_id_from_cgroup() -> str | None:
    """Reads this process's own container ID out of the kernel's own record of
    its cgroup membership (/proc/self/cgroup) -- works on cgroup v1 hosts,
    where the cgroup path itself embeds the full 64-hex container ID. Not
    reliable on cgroup v2 (see _own_container_id_from_mountinfo for that
    case), so this is one candidate among several, never trusted alone."""
    try:
        text = Path("/proc/self/cgroup").read_text()
    except OSError:
        return None
    matches = _CONTAINER_ID_RE.findall(text)
    return matches[-1] if matches else None


def _own_container_id_from_mountinfo() -> str | None:
    """Reads this process's own container ID out of /proc/self/mountinfo --
    the kernel's own view of this process's mount namespace. Works
    regardless of cgroup driver/version (unlike _own_container_id_from_cgroup):
    Docker always bind-mounts a per-container resolv.conf/hostname file from
    /var/lib/docker/containers/<64-hex-id>/... into every container it
    starts, and that source path is recorded verbatim here."""
    try:
        text = Path("/proc/self/mountinfo").read_text()
    except OSError:
        return None
    match = re.search(r"/containers/([0-9a-f]{64})/", text)
    return match.group(1) if match else None


def _own_container_id_candidates() -> list[str]:
    """Every plausible ID for this process's own Docker container, in order
    of trust, deduplicated. HOSTNAME first -- Docker sets a container's
    hostname to its own short ID by default, and nothing in compute.tf's
    `docker run` for agentra-blue/agentra-green passes --hostname to
    override that, so it's usually right and cheapest to check -- but it is
    no longer treated as the single hardcoded source of truth the way it
    used to be: it can go stale (overridden by a flag, unset outside a real
    container) without any signal that it did, which is exactly how this
    process previously ended up never actually joined to
    agentra-preprod-net. The /proc-derived candidates fall back to the
    kernel's own bookkeeping instead, which can't be overridden by an env
    var."""
    candidates: list[str] = []
    for candidate in (
        os.environ.get("HOSTNAME", "").strip(),
        _own_container_id_from_mountinfo(),
        _own_container_id_from_cgroup(),
    ):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _own_container_name() -> str | None:
    """Resolves this process's own container's Docker --name (e.g.
    'agentra-blue') by asking the daemon (over the bind-mounted docker.sock)
    to inspect each ID candidate from _own_container_id_candidates in turn,
    returning the first one that actually resolves to a real container --
    rather than trusting a single source blindly. None only if every
    candidate is exhausted without a match (e.g. running entirely outside
    Docker, as under plain local pytest)."""
    for container_id in _own_container_id_candidates():
        inspect = subprocess.run(
            ["docker", "inspect", container_id, "--format", "{{.Name}}"],
            capture_output=True, text=True,
        )
        if inspect.returncode == 0 and inspect.stdout.strip():
            return inspect.stdout.strip().lstrip("/")
    return None


def _container_networks(container_name: str) -> set[str] | None:
    """The Docker networks container_name is currently attached to, or None
    if that can't be determined (e.g. the container doesn't exist)."""
    inspect = subprocess.run(
        ["docker", "inspect", container_name, "--format", "{{json .NetworkSettings.Networks}}"],
        capture_output=True, text=True,
    )
    if inspect.returncode != 0 or not inspect.stdout.strip():
        return None
    try:
        networks = json.loads(inspect.stdout)
    except json.JSONDecodeError:
        return None
    return set(networks) if isinstance(networks, dict) else None


def _ensure_network_joined(network: str, container_name: str) -> bool:
    """Connects container_name to network if it isn't already a member, then
    verifies membership by re-inspecting -- never trusts `docker network
    connect`'s exit code alone. A silently-swallowed connect failure
    (permissions, a stale/misidentified container reference, the network
    not existing) used to surface only much later as an opaque `curl`
    timeout against an address that was never actually reachable, instead
    of being caught here where the real cause is knowable. Returns whether
    membership was actually confirmed, not just whether the connect
    command exited 0."""
    networks = _container_networks(container_name)
    if networks is not None and network in networks:
        return True
    subprocess.run(["docker", "network", "connect", network, container_name], capture_output=True, text=True)
    networks = _container_networks(container_name)
    return bool(networks and network in networks)
