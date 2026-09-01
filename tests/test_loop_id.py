"""registry.loop_id_for_issue: a loop maps 1:1 to a tracked GitHub issue, so
every run working that issue (implement, resume-after-human, deploy) shares one
stable id derived from app + issue number.
"""

from agentra import registry


def test_loop_id_for_issue_is_stable_and_issue_scoped():
    a = registry.loop_id_for_issue("agentra", 42)
    assert a == registry.loop_id_for_issue("agentra", "42")  # int/str parity
    assert a == registry.loop_id_for_issue("agentra", 42)  # deterministic
    assert a != registry.loop_id_for_issue("agentra", 43)  # per-issue
    assert a != registry.loop_id_for_issue("otherapp", 42)  # per-app
    assert len(a) == 10


def test_loop_id_for_issue_differs_from_the_objective_hash():
    assert registry.loop_id_for_issue("agentra", 42) != registry.loop_id_for("some objective")
