"""registry/jobs.py against a real (moto) DynamoDB: the by-status GSI drives the
claim order and the CAS claim prevents two loops grabbing the same job."""

import boto3
import pytest
from moto import mock_aws

from agentra.registry import core, jobs


@pytest.fixture
def ddb(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AGENTRA_DYNAMODB_TABLE_PREFIX", "")
        monkeypatch.setenv("AGENTRA_AWS_REGION", "us-west-2")
        resource = boto3.resource("dynamodb", region_name="us-west-2")
        resource.create_table(
            TableName="jobs",
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "job_id", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "enqueued_at", "AttributeType": "N"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "by-status",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "enqueued_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(core, "_ddb", resource)
        yield resource


def test_enqueue_claim_report_roundtrip_on_dynamo(ddb):
    a = jobs.enqueue_job("cycle", {"app": "agentra"})
    b = jobs.enqueue_job("promote", {"app": "agentra"})

    first = jobs.claim_next_job()
    assert first["job_id"] == a and first["status"] == "claimed"
    second = jobs.claim_next_job()
    assert second["job_id"] == b
    assert jobs.claim_next_job() is None

    jobs.report_job(a, "done", {"ok": True})
    assert jobs.list_jobs(status="done")[0]["result"] == {"ok": True}
    assert {j["job_id"] for j in jobs.list_jobs(status="claimed")} == {b}


def test_cas_claim_blocks_a_double_grab(ddb):
    jid = jobs.enqueue_job("cycle", {})
    assert jobs._try_claim(jid) is True
    assert jobs._try_claim(jid) is False  # already claimed -- CAS refuses


def test_stale_claimed_job_is_requeued_on_dynamo(ddb, monkeypatch):
    monkeypatch.setattr(core, "STALE_PROCESSING_SECONDS", 0)
    jid = jobs.enqueue_job("cycle", {})
    jobs.claim_next_job()
    again = jobs.claim_next_job()
    assert again["job_id"] == jid and again["attempts"] == 2
