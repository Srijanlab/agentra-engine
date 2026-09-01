"""A2A protocol foundation: model round-trips, card generation, and discovery endpoints."""

import json

from fastapi.testclient import TestClient

from agentra import server
from agentra.a2a import (
    AgentCard,
    Artifact,
    Message,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)
from agentra.a2a import cards


SPECIALIZED = [
    "codebase",
    "discovery",
    "architecture_review",
    "human_answer_judge",
    "implementation",
    "testing",
    "deployment",
    "feedback",
    "prod_debug",
]


# -- models -----------------------------------------------------------------


def _roundtrip(model):
    dumped = model.model_dump_json(by_alias=True)
    restored = type(model).model_validate_json(dumped)
    assert restored.model_dump(by_alias=True) == model.model_dump(by_alias=True)
    return json.loads(dumped)


def test_message_roundtrip_uses_camelcase():
    msg = Message(role="user", parts=[TextPart(text="hello")])
    body = _roundtrip(msg)
    assert body["kind"] == "message"
    assert body["parts"][0] == {"kind": "text", "text": "hello", "metadata": None}
    assert "messageId" in body


def test_task_roundtrip_with_state_enum():
    task = Task(
        id="t1",
        context_id="c1",
        status=TaskStatus(state=TaskState.input_required),
        artifacts=[Artifact(name="out", parts=[TextPart(text="done")])],
    )
    body = _roundtrip(task)
    assert body["status"]["state"] == "input-required"
    assert body["contextId"] == "c1"
    assert body["artifacts"][0]["artifactId"]


def test_agent_card_roundtrip():
    card = cards.get_agent_card("implementation")
    body = _roundtrip(card)
    assert body["name"] == "implementation"
    assert body["capabilities"] == {
        "streaming": False,
        "pushNotifications": False,
        "stateTransitionHistory": False,
    }
    assert AgentCard.model_validate(body)


# -- card generation ------------------------------------------------------------


def test_build_agent_cards_covers_the_nine_specialized_agents():
    generated = cards.build_agent_cards()
    assert [c.name for c in generated] == SPECIALIZED
    assert "orchestrator" not in {c.name for c in generated}
    assert "custom" not in {c.name for c in generated}
    for card in generated:
        assert card.description
        assert card.skills
        for skill in card.skills:
            assert skill.id and skill.name and skill.description
        assert card.default_input_modes and all("text" in m for m in card.default_input_modes)
        assert card.default_output_modes and all("text" in m for m in card.default_output_modes)


def test_get_agent_card_unknown_returns_none():
    assert cards.get_agent_card("nope") is None


def test_service_card_enumerates_all_agents():
    svc = cards.build_service_card()
    skill_ids = {s.id for s in svc.skills}
    assert set(SPECIALIZED).issubset(skill_ids)


# -- endpoints ----------------------------------------------------------------


def _client():
    return TestClient(server.app)


def test_list_endpoint_returns_nine_cards():
    resp = _client().get("/a2a/agents")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"
    data = resp.json()
    assert isinstance(data, list) and len(data) == 9
    assert {c["name"] for c in data} == set(SPECIALIZED)
    for card in data:
        assert card["name"] and card["description"]
        assert isinstance(card["capabilities"], dict)
        assert card["skills"]
        assert isinstance(card["defaultInputModes"], list)
        assert isinstance(card["defaultOutputModes"], list)


def test_single_agent_endpoint():
    resp = _client().get("/a2a/agents/implementation")
    assert resp.status_code == 200
    assert resp.json()["name"] == "implementation"


def test_every_specialized_agent_has_an_endpoint():
    client = _client()
    for agent_id in SPECIALIZED:
        assert client.get(f"/a2a/agents/{agent_id}").status_code == 200


def test_unknown_agent_returns_404():
    assert _client().get("/a2a/agents/not-a-real-agent").status_code == 404


def test_well_known_service_card():
    resp = _client().get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"
    body = resp.json()
    assert body["name"] and body["description"]
    assert isinstance(body["capabilities"], dict)
    assert body["skills"]
    assert set(SPECIALIZED).issubset({s["id"] for s in body["skills"]})


def test_new_endpoints_need_no_auth():
    client = _client()
    for path in ("/a2a/agents", "/a2a/agents/testing", "/.well-known/agent-card.json"):
        assert client.get(path).status_code == 200


def test_regression_agents_metadata_still_lists_nine():
    body = _client().get("/agents/metadata").json()
    for agent_id in SPECIALIZED:
        assert agent_id in body["agents"]


def test_regression_health_ok():
    body = _client().get("/health").json()
    assert body["status"] == "ok"
