"""Deterministic classification of how "heavy" a shipped change is --
used to decide whether it's worth the cost of a full pre-prod deploy +
live Testing Agent verification (real docker resources, a spun-up
sibling, an LLM agent turn actually clicking around) or whether a
passing local test suite is already sufficient proof on its own.
See agents/brain/tools.py's deploy_pre_prod tool, the actual gate.

Deliberately not an LLM judgment call: "is this a trivial rename/test-fix
or a real feature" is answerable directly from git's own diff stats --
files changed, lines changed, git's own rename detection -- without
spending a turn asking a model to guess. Same philosophy as
agents/deployment.py's own git plumbing: deterministic Python for
anything that shouldn't be left to an agent's improvisation.
"""

import re
import subprocess
from pathlib import Path

TRIVIAL = "trivial"
STANDARD = "standard"

# A change touching only these kinds of paths never ships new product
# behavior on its own -- fixing a flaky test, tightening a lint config,
# bumping a lockfile, editing docs. Any file NOT matching one of these is
# "real" code and pushes the whole change to STANDARD regardless of size.
_TRIVIAL_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec)(/|_|\.)|_test\.py$|\.test\.[jt]sx?$|\.spec\.[jt]sx?$|"
    r"\.(md|txt|rst)$|"
    r"(^|/)(docs?/|\.github/)|"
    r"(^|/)(package-lock\.json|poetry\.lock|uv\.lock|Pipfile\.lock|yarn\.lock)$|"
    r"\.(ya?ml|cfg|ini|toml)$"
)

# Even real code can be STANDARD-worthy of skipping if the diff is this
# small -- covers one-line/two-line bug fixes, a renamed identifier, a
# tweaked label/string. Both thresholds must hold; a 1-file 200-line diff
# is not trivial just because it's one file.
TRIVIAL_MAX_FILES = 3
TRIVIAL_MAX_LINES = 15


def classify_change(repo: Path, base_ref: str, feature_ref: str) -> str:
    """TRIVIAL or STANDARD, based on the diff of everything feature_ref has
    that base_ref doesn't (three-dot semantics: from their merge-base to
    feature_ref's tip, not base_ref's own independent history). Returns
    STANDARD -- the safe default -- if the diff can't be read at all."""
    diff = subprocess.run(
        ["git", "-C", str(repo), "diff", "--numstat", "-M", f"{base_ref}...{feature_ref}"],
        capture_output=True, text=True,
    )
    if diff.returncode != 0:
        return STANDARD
    lines = [line for line in diff.stdout.splitlines() if line.strip()]
    if not lines:
        # Nothing to diff (e.g. feature_ref == base_ref) -- nothing to ship
        # either, so there's no case where this matters in practice, but
        # STANDARD is still the conservative answer.
        return STANDARD

    total_changed = 0
    non_trivial_paths: list[str] = []
    for line in lines:
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        # git numstat marks a pure rename (-M) as "0\t0\told => new" -- no
        # content actually changed, so it never counts against either
        # threshold, whatever the file itself is.
        if added == "0" and deleted == "0":
            continue
        if _TRIVIAL_PATH_RE.search(path):
            # Uncapped -- a large test-only or docs-only change is still
            # trivial (a real rewrite of the test suite doesn't ship new
            # product behavior), only the real-code portion below counts
            # against the line threshold.
            continue
        non_trivial_paths.append(path)
        # "-" means binary; can't measure lines changed, so count it as a
        # real (non-zero) contribution without crashing on int().
        total_changed += (0 if added == "-" else int(added)) + (0 if deleted == "-" else int(deleted))

    if not non_trivial_paths:
        return TRIVIAL
    if len(non_trivial_paths) <= TRIVIAL_MAX_FILES and total_changed <= TRIVIAL_MAX_LINES:
        return TRIVIAL
    return STANDARD
