## agentra-engine

This repo is the API + state authority. It runs **no** autonomous cycle,
promotion, or prod-debug pass -- a trigger endpoint records a run and calls
`registry.enqueue_job(...)`; agentra-loop claims the job and executes it. Do not
add cycle / agent-pipeline code here; it belongs in agentra-loop.

`agents/` here holds only what the dashboard's chat / standup / Slack assistant
still call (`base.py`, `catalog.py`, `safety.py`, `slack_assistant.py`).
