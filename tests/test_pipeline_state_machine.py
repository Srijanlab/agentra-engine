"""The loop<->engine RPC boundary: methods a cycle needs must cross it."""


def test_new_registry_and_memory_methods_cross_the_loop_engine_boundary():
    names = {
        "set_loop_pipeline", "get_loop_pipeline", "set_loop_human_input",
        "enqueue_job", "claim_next_job", "report_job", "list_jobs",
    }
    from agentra.server.routes.internal import _MEMORY_METHODS, _REGISTRY_METHODS
    assert names <= _REGISTRY_METHODS
    assert "issue_status" in _MEMORY_METHODS
