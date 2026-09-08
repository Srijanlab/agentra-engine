"""Test isolation -- no test may ever reach production state.

On the loop, registry.* / Memory.* become agentra-engine RPC proxies at import
time when AGENTRA_ENGINE_URL is set; on the engine, the registry talks to
DynamoDB when AGENTRA_DYNAMODB_TABLE_PREFIX is set. Either, present in a test
environment (e.g. `pytest` run inside the deployed container), means a test
writes fixtures straight into prod -- which has happened. Strip them before
anything imports agentra. Tests that exercise the proxy / DynamoDB paths set
their own env via monkeypatch and mock the transport.
"""

import os

for _var in (
    "AGENTRA_ENGINE_URL",
    "AGENTRA_DYNAMODB_TABLE_PREFIX",
    "AGENTRA_AWS_ACCESS_KEY_ID",
    "AGENTRA_AWS_SECRET_ACCESS_KEY",
    "AGENTRA_AWS_REGION",
    "AGENTRA_FIRESTORE_PROJECT",
    "GCP_WORKLOAD_IDENTITY_CONFIG",
):
    os.environ.pop(_var, None)
