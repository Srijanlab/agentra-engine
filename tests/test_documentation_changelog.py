"""Tests for Memory.append_documentation() -- the running changelog
section of architecture/documentation.md that a shipped feature gets
appended to (agents/brain.py's implement_feature tool, orchestrator.py's
run_cycle), distinct from record_shipped()'s structured record (a closed
GitHub 'enhancement' issue) that shipped_features() reads back.
"""

from pathlib import Path

from agentra.memory import Memory


def test_append_documentation_creates_changelog_section_when_file_is_empty(tmp_path):
    mem = Memory(tmp_path)

    mem.append_documentation("Shipped **dark mode**: adds a theme toggle.")

    content = mem.read("architecture", "documentation")
    assert "## Changelog" in content
    assert "Shipped **dark mode**: adds a theme toggle." in content


def test_append_documentation_preserves_existing_static_description(tmp_path):
    mem = Memory(tmp_path)
    mem.write("architecture", "documentation", "This is a static architecture overview, written by a human.")

    mem.append_documentation("Shipped **dark mode**.")

    content = mem.read("architecture", "documentation")
    assert "This is a static architecture overview, written by a human." in content
    assert "## Changelog" in content
    assert "Shipped **dark mode**." in content
    # The static description must come before the changelog section, not
    # get overwritten by it.
    assert content.index("static architecture overview") < content.index("## Changelog")


def test_append_documentation_accumulates_multiple_entries(tmp_path):
    mem = Memory(tmp_path)

    mem.append_documentation("Shipped feature A.")
    mem.append_documentation("Shipped feature B.")

    content = mem.read("architecture", "documentation")
    assert content.count("## Changelog") == 1
    assert "Shipped feature A." in content
    assert "Shipped feature B." in content
    assert content.index("Shipped feature A.") < content.index("Shipped feature B.")
