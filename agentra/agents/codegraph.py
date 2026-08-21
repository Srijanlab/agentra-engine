"""Local code-graph step (graphify), run against whatever app repo this cycle is pointed at -- not agentra's own source."""

import subprocess
from pathlib import Path

_EXCLUDE_LINE = "graphify-out/"
_GRAPHIFY_TIMEOUT = 600

# The subset of graphify-mcp's tools that are pure local reads against the
MCP_SERVER_NAME = "graphify"
READ_ONLY_MCP_TOOLS = [
    f"mcp__{MCP_SERVER_NAME}__query_graph",
    f"mcp__{MCP_SERVER_NAME}__get_node",
    f"mcp__{MCP_SERVER_NAME}__get_neighbors",
    f"mcp__{MCP_SERVER_NAME}__get_community",
    f"mcp__{MCP_SERVER_NAME}__god_nodes",
    f"mcp__{MCP_SERVER_NAME}__graph_stats",
    f"mcp__{MCP_SERVER_NAME}__shortest_path",
]

_SUMMARY_PREAMBLE = (
    "\n\n--- graphify code graph ---\n"
    "A local, queryable knowledge graph of this repo is available at graphify-out/graph.json. "
    'Query it directly for anything more specific than this excerpt: `graphify query "<question>"`, '
    '`graphify path "<A>" "<B>"` for how two things connect, `graphify explain "<name>"` for a focused '
    "look at one node and its neighbors.\n\n"
)


def _ensure_local_exclude(repo: Path) -> None:
    exclude_path = repo / ".git" / "info" / "exclude"
    if not exclude_path.parent.is_dir():
        return  # not a git checkout (unexpected here, but this step must never fail the cycle)
    existing = exclude_path.read_text() if exclude_path.exists() else ""
    if _EXCLUDE_LINE in existing.splitlines():
        return
    with exclude_path.open("a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(_EXCLUDE_LINE + "\n")


def _summarize(graph_json: Path, report_path: Path) -> str:
    god_nodes = subprocess.run(
        ["graphify", "god-nodes", "--graph", str(graph_json), "--top", "10"],
        capture_output=True, text=True, timeout=60,
    )
    report_excerpt = report_path.read_text()[:4000] if report_path.exists() else ""

    parts = []
    if god_nodes.returncode == 0 and god_nodes.stdout.strip():
        parts.append("Code graph -- most connected nodes (architectural hubs):\n" + god_nodes.stdout.strip())
    if report_excerpt:
        parts.append("Code graph report excerpt (community structure, cross-file relationships):\n" + report_excerpt)
    if not parts:
        return ""
    return _SUMMARY_PREAMBLE + "\n\n".join(parts)


def _cluster_and_summarize(repo: Path, graph_json: Path) -> str:
    try:
        subprocess.run(
            ["graphify", "cluster-only", str(repo), "--no-label", "--no-viz"],
            check=True, capture_output=True, text=True, timeout=_GRAPHIFY_TIMEOUT,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return _summarize(graph_json, repo / "graphify-out" / "GRAPH_REPORT.md")


def load_or_build(repo: Path) -> str:
    """Reuse the existing graph if one is already present -- just read and summarize it, no rebuild, no re-cluster."""
    graph_json = repo / "graphify-out" / "graph.json"
    if graph_json.exists():
        return _summarize(graph_json, repo / "graphify-out" / "GRAPH_REPORT.md")

    _ensure_local_exclude(repo)
    try:
        subprocess.run(
            ["graphify", "extract", str(repo), "--code-only"],
            check=True, capture_output=True, text=True, timeout=_GRAPHIFY_TIMEOUT,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return _cluster_and_summarize(repo, graph_json)


def mcp_config(repo: Path) -> dict[str, dict]:
    """The mcp_servers={} entry that gives a caller live, scoped graph queries (see READ_ONLY_MCP_TOOLS) without opening full Bash -- for agents like Architecture Review that are deliberately kept read-only."""
    graph_json = repo / "graphify-out" / "graph.json"
    if not graph_json.exists():
        return {}
    return {MCP_SERVER_NAME: {"command": "graphify-mcp", "args": [str(graph_json)]}}


def refresh(repo: Path) -> None:
    """Incrementally update the graph (AST re-extraction only, no LLM, no network) after a run that may have changed code."""
    graph_json = repo / "graphify-out" / "graph.json"
    if not graph_json.exists():
        return
    try:
        subprocess.run(
            ["graphify", "update", str(repo)],
            check=True, capture_output=True, text=True, timeout=_GRAPHIFY_TIMEOUT,
        )
        subprocess.run(
            ["graphify", "cluster-only", str(repo), "--no-label", "--no-viz"],
            check=True, capture_output=True, text=True, timeout=_GRAPHIFY_TIMEOUT,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return
