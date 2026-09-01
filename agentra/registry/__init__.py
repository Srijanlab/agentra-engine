"""agentra.registry — multi-app registry and durable inbox."""

from __future__ import annotations

import sys
import types
from typing import Any

from agentra.registry import core
from agentra.registry.core import (
    VALID_LLM_BACKENDS,
    firestore_client,
    get_app_repo,
    get_llm_backend,
    get_slack_channel,
    is_paused,
    list_apps,
    pause,
    persist_agentra_dir,
    record_slack_thread,
    register_app,
    remove_app,
    resolve_slack_thread,
    slack_thread_for,
    resume,
    set_llm_backend,
    set_slack_channel,
)
from agentra.registry.inbox import (
    DispatchSummary,
    dispatch_once,
    submit_request,
)
from agentra.registry.runs import (
    get_run,
    last_run_at,
    list_agent_steps,
    list_loops,
    list_runs,
    list_waiting_for_human,
    loop_id_for,
    loop_id_for_issue,
    record_agent_step,
    record_run,
    reconcile_stale_runs,
    reconcile_waiting_for_human,
)

_DELEGATED_NAMES = {
    "AGENTRA_HOME",
    "APPS_PATH",
    "INBOX_ROOT",
    "PAUSE_PATH",
    "_SLACK_THREADS_PATH",
    "_LLM_BACKEND_PATH",
    "REPOS_ROOT",
    "REQUEST_TYPES",
    "STALE_PROCESSING_SECONDS",
    "STALE_RUN_SECONDS",
    "HUMAN_INPUT_MAX_WAIT_SECONDS",
    "_db",
    "_RUNS_PATH",
    "_AGENT_STEPS_PATH",
}


class RegistryModule(types.ModuleType):
    def __getattr__(self, name: str) -> Any:
        if name in _DELEGATED_NAMES:
            return getattr(core, name)
        return super().__getattribute__(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _DELEGATED_NAMES:
            setattr(core, name, value)
        else:
            super().__setattr__(name, value)


# Replace this module with our custom proxy module in sys.modules
sys.modules[__name__].__class__ = RegistryModule

__all__ = [
    "DispatchSummary",
    "VALID_LLM_BACKENDS",
    "firestore_client",
    "get_app_repo",
    "get_llm_backend",
    "get_run",
    "get_slack_channel",
    "is_paused",
    "last_run_at",
    "list_agent_steps",
    "list_apps",
    "list_loops",
    "list_runs",
    "list_waiting_for_human",
    "loop_id_for",
    "loop_id_for_issue",
    "pause",
    "persist_agentra_dir",
    "record_slack_thread",
    "resolve_slack_thread",
    "slack_thread_for",
    "record_agent_step",
    "record_run",
    "reconcile_stale_runs",
    "reconcile_waiting_for_human",
    "register_app",
    "remove_app",
    "resume",
    "set_llm_backend",
    "set_slack_channel",
    "dispatch_once",
    "submit_request",
]
