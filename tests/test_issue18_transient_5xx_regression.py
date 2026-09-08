"""GitHub issue #18 regression lock-in: an autonomous cycle hard-failed when
Claude Code returned "API Error: 500 Internal Server Error" -- a transient,
server-side error. The failure taxonomy must classify 5xx / gateway errors as
transient/retryable so they are not filed as a noisy medium-severity bug on
every transient blip.
"""

from agentra.memory import cannot_be_fixed_by_agentra, is_login_required_failure, is_transient_failure

_ISSUE_18_TEXT = (
    "autonomous cycle raised: Claude Code returned an error result: "
    "API Error: 500 Internal Server Error. This is a server-side issue, "
    "usually temporary (exit code: 1)"
)


def test_issue_18_exact_text_is_transient():
    assert is_transient_failure(_ISSUE_18_TEXT) is True


def test_five_hundred_and_other_5xx_gateway_errors_are_transient():
    assert is_transient_failure("API Error: 500 Internal Server Error")
    assert is_transient_failure("API Error: 502 Bad Gateway")
    assert is_transient_failure("API Error: 503 Service Unavailable")
    assert is_transient_failure("API Error: 529 overloaded")
    assert is_transient_failure("500 Internal Server Error")
    assert is_transient_failure("Received 502 Bad Gateway from localhost:8079")
    assert is_transient_failure("upstream returned 503 Service Unavailable")
    assert is_transient_failure("504 Gateway Timeout")
    assert is_transient_failure("This is a server-side issue, usually temporary")


def test_a_5xx_is_not_treated_as_unfixable_or_login_required():
    assert not cannot_be_fixed_by_agentra(_ISSUE_18_TEXT)
    assert not is_login_required_failure(_ISSUE_18_TEXT)


def test_a_real_4xx_client_defect_is_still_not_transient():
    # A genuine application bug (4xx from the app under test) must still be
    # filed, not swallowed as a retryable blip.
    assert not is_transient_failure("app returned 404 Not Found for GET /api/widgets")
    assert not is_transient_failure("400 Bad Request: missing required field 'email'")
    assert not is_transient_failure("TypeError: cannot read property 'x' of undefined at line 42")
