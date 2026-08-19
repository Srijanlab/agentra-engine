"""Local code-graph step (graphify), run against whatever app repo this
cycle is pointed at -- not agentra's own source. Deterministic Python, not
an LLM instruction (same reasoning as implementation.py's checkout/commit: a
mechanical step that must actually happen belongs in plain code, not prose
an agent might skip).

Two entry points, split so a read never pays for a rebuild:
- load_or_build(repo): reuse the existing graph as-is if one is already
  present; only builds fresh the first time, when there's nothing to reuse
  yet. Called wherever the codebase summary is assembled (codebase.py).
- refresh(repo): incrementally re-extracts and re-clusters after a run that
  may have changed code. Called once, at the end of Implementation Agent's
  run (agents/implementation.py) -- not on every read -- so the graph is
  current for the *next* cycle's reads without re-clustering on every single
  read in between.

Local only, by design: `graphify extract --code-only` parses with tree-sitter
alone (no docs/PDF/image LLM extraction, no API key, nothing leaves the
machine) and `cluster-only --no-label` skips the LLM community-naming pass
too -- the whole thing stays AST-only and fully offline, since it runs
unattended against arbitrary target repos inside a VM with no reason to
spend API budget or trust an outbound call.

graphify-out/ is excluded via .git/info/exclude (not the target repo's own
.gitignore) so it never shows up as a diff in the app being worked on and
never rides along in implementation.py's `git add -A` safety-net commit --
this is agentra's own working data about the repo, not something that
belongs in the target app's history.
"""

import subprocess
from pathlib import Path

_EXCLUDE_LINE = "graphify-out/"
_GRAPHIFY_TIMEOUT = 600

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
    """Reuse the existing graph if one is already present -- just read and
    summarize it, no rebuild, no re-cluster. Only builds fresh (extract +
    cluster) the first time, when graphify-out/graph.json doesn't exist yet.
    Best-effort: returns "" on any failure (missing `graphify` binary,
    timeout, malformed repo) rather than raising -- this is context
    enrichment, not something a cycle should abort over."""
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


def refresh(repo: Path) -> None:
    """Incrementally update the graph (AST re-extraction only, no LLM, no
    network) after a run that may have changed code. Best-effort and silent
    -- called deterministically at the end of implementation.py's run(), not
    something a cycle should ever fail over. No-ops if no graph has been
    built yet; the next load_or_build call builds one fresh instead."""
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
