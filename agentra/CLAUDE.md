## agentra-engine

This repo is the API + state authority. It runs **no** autonomous cycle,
promotion, or prod-debug pass -- a trigger endpoint records a run and calls
`registry.enqueue_job(...)`; agentra-loop claims the job and executes it. Do not
add cycle / agent-pipeline code here; it belongs in agentra-loop.

The engine runs **no Claude** at all -- no `claude-agent-sdk` dependency.
`agents/` here holds only `catalog.py` (static agent metadata for `/agents/metadata`
and the a2a cards). Chat and standup *generation* are held (503) until they move
to the loop; Slack runs entirely on the loop via Socket Mode.
