"""find_unanswered_human_input_comment must never mistake the orchestrator's
own escalation/diagnosis comment for a human's answer to itself.

Real bug, confirmed live on agentra#38: escalate_existing_issue's diagnosis
comment ("Blocked, needs human input (run X): ...") didn't start with any of
_INTERNAL_COMMENT_PREFIXES, so once *any* Human-Input-Required marker had ever
appeared earlier in the thread, every later escalation's own diagnosis comment
was picked up as "the answer" -- dispatch_human_answer immediately resumed the
run with that diagnosis text as the resolution, which of course didn't fix
anything, so the resumed run hit the same wall and escalated again. Dozens of
self-answered escalate/resume bounces over many hours before this was caught
-- and the engine (this copy) is where _reconcile_human_input_for_app
actually runs, not the loop, so fixing only the loop's copy left it fully
live in production.
"""

from agentra.connectors import github_issues  # noqa: F401 -- import first, breaks the lifecycle<->issues circular import
from agentra.connectors import github_issue_lifecycle as lifecycle


def _comment(body: str) -> dict:
    return {"body": body}


def test_an_escalation_diagnosis_comment_is_never_read_as_the_answer_to_itself(monkeypatch):
    posted = []
    monkeypatch.setattr(lifecycle, "add_comment", lambda repo_url, n, body: posted.append(body))
    monkeypatch.setattr(lifecycle, "add_labels", lambda *a, **k: None)

    lifecycle.escalate_existing_issue("https://github.com/acme/app.git", 38, "run1", "diagnosis text", ["need_human"])
    [diagnosis_comment] = posted
    comments = [
        _comment("Human-Input-Required (agentra):\nApp: acme\nRun-ID: run1\nTracking-Issue: 38\nQuestion: what now?"),
        _comment(diagnosis_comment),  # the orchestrator's own escalation, posted after its own marker
    ]
    monkeypatch.setattr(lifecycle, "list_comments", lambda repo_url, n: comments)

    assert lifecycle.find_unanswered_human_input_comment("https://github.com/acme/app.git", 38) is None


def test_a_real_human_comment_after_the_marker_is_still_detected(monkeypatch):
    comments = [
        _comment("Human-Input-Required (agentra):\nApp: acme\nRun-ID: run1\nTracking-Issue: 38\nQuestion: what now?"),
        _comment("Go ahead and retry, the branch is fine now."),
    ]
    monkeypatch.setattr(lifecycle, "list_comments", lambda repo_url, n: comments)

    answer = lifecycle.find_unanswered_human_input_comment("https://github.com/acme/app.git", 38)

    assert answer == "Go ahead and retry, the branch is fine now."


def test_a_second_escalation_after_the_first_is_also_excluded(monkeypatch):
    """The exact live sequence: marker, this escalation's own diagnosis, a later
    escalation's diagnosis too -- none of them are ever mistaken for an answer."""
    posted = []
    monkeypatch.setattr(lifecycle, "add_comment", lambda repo_url, n, body: posted.append(body))
    monkeypatch.setattr(lifecycle, "add_labels", lambda *a, **k: None)

    lifecycle.escalate_existing_issue("https://github.com/acme/app.git", 38, "run1", "first diagnosis", ["need_human"])
    lifecycle.escalate_existing_issue("https://github.com/acme/app.git", 38, "run2", "second diagnosis", ["need_human"])
    comments = [
        _comment("Human-Input-Required (agentra):\nApp: acme\nRun-ID: run1\nTracking-Issue: 38\nQuestion: q1"),
        _comment(posted[0]),
        _comment(posted[1]),
    ]
    monkeypatch.setattr(lifecycle, "list_comments", lambda repo_url, n: comments)

    assert lifecycle.find_unanswered_human_input_comment("https://github.com/acme/app.git", 38) is None


def test_an_old_answer_to_a_past_escalation_does_not_satisfy_a_later_one(monkeypatch):
    """GitHub issue #54: a real answer that correctly resolved the FIRST escalation
    must not keep being "found" as the answer to a SECOND, separate escalation raised
    on the same issue later -- only a genuinely new comment after the latest marker
    counts."""
    comments = [
        _comment("Human-Input-Required (agentra):\nApp: acme\nRun-ID: run1\nTracking-Issue: 15\nQuestion: what now?"),
        _comment("Test the dashboard and report back."),  # answered the FIRST escalation
        _comment("Human-Input-Required (agentra):\nApp: acme\nRun-ID: run2\nTracking-Issue: 15\nQuestion: still stuck, what now?"),
        # no comment after this second marker yet -- nobody has answered it
    ]
    monkeypatch.setattr(lifecycle, "list_comments", lambda repo_url, n: comments)

    assert lifecycle.find_unanswered_human_input_comment("https://github.com/acme/app.git", 15) is None


def test_a_fresh_comment_after_a_later_escalation_is_still_detected(monkeypatch):
    """Same setup as above, but this time a genuinely new reply lands after the
    second marker -- that one must be found."""
    comments = [
        _comment("Human-Input-Required (agentra):\nApp: acme\nRun-ID: run1\nTracking-Issue: 15\nQuestion: what now?"),
        _comment("Test the dashboard and report back."),
        _comment("Human-Input-Required (agentra):\nApp: acme\nRun-ID: run2\nTracking-Issue: 15\nQuestion: still stuck, what now?"),
        _comment("Checked -- latency is fine now, go ahead and promote."),
    ]
    monkeypatch.setattr(lifecycle, "list_comments", lambda repo_url, n: comments)

    answer = lifecycle.find_unanswered_human_input_comment("https://github.com/acme/app.git", 15)

    assert answer == "Checked -- latency is fine now, go ahead and promote."
