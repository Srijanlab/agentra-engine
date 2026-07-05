"""Deployment Agent (vision.md 5.8), split across the environment pipeline.

deploy_pre_prod() is what every regular cycle calls: push to the pre-prod
branch, deploy the isolated Firebase pre-prod project + a Vercel preview,
smoke test. It never touches production.

promote_prod() is the one place in the whole system that's allowed to touch
production, and only when called with allow_prod=True by a caller that has
already checked EnvironmentConfig.auto_remediate_prod (see
agents/prod_debug.py) or by a human running `agentos promote`.
"""

from pathlib import Path

from agentos.agents.base import AgentResult, run_agent
from agentos.environments import EnvironmentConfig

PRE_PROD_SYSTEM_PROMPT = """You are the Deployment Agent, deploying to the \
PRE-PROD environment only. You must never touch production.

Environment for this deploy:
- local branch: {local_branch}
- pre-prod branch: {pre_prod_branch}
- Vercel configured: {vercel}
- Firebase configured: {firebase} (pre-prod alias: {firebase_pre_prod_alias})

Steps:
1. Merge or fast-forward the pre-prod branch to the current local branch tip \
   (git checkout {pre_prod_branch}, merge {local_branch}, push).
2. If Vercel is configured, run a preview deploy (`vercel deploy`, never \
   `--prod`) and capture the resulting preview URL.
3. If Firebase is configured, switch to the pre-prod alias \
   (`firebase use {firebase_pre_prod_alias}`) and deploy functions \
   (`firebase deploy --only functions`). Never switch to any other alias.
4. Run any available smoke test against the preview URL.
5. If neither Vercel nor Firebase is configured, report status "skipped" \
   and do nothing else.

End your response with a fenced ```json block shaped like:
{{
  "status": "deployed" | "skipped" | "failed",
  "preview_url": "...",
  "firebase_pre_prod_deployed": true | false,
  "notes": "..."
}}
"""

PROD_SYSTEM_PROMPT = """You are the Deployment Agent, promoting a change to \
PRODUCTION. This call has been explicitly authorized — either by a human \
running `agentos promote`, or by the Production Debugging Agent's opted-in \
auto-remediate path after the fix passed pre-prod testing. Do exactly this \
and nothing more:

Environment for this promotion:
- pre-prod branch: {pre_prod_branch}
- prod branch: {prod_branch}
- Vercel configured: {vercel}
- Firebase configured: {firebase} (prod alias: {firebase_prod_alias})

Steps:
1. Merge or fast-forward the prod branch to the current pre-prod branch tip \
   (git checkout {prod_branch}, merge {pre_prod_branch}, push).
2. If Vercel is configured, run `vercel deploy --prod`.
3. If Firebase is configured, switch to the prod alias \
   (`firebase use {firebase_prod_alias}`) and deploy functions \
   (`firebase deploy --only functions`).
4. Run any available smoke test against the production URL.
5. Report clearly if anything failed — do not retry destructive steps.

End your response with a fenced ```json block shaped like:
{{
  "status": "deployed" | "failed",
  "prod_url": "...",
  "notes": "..."
}}
"""


async def deploy_pre_prod(repo: Path, env: EnvironmentConfig) -> AgentResult:
    prompt = "Deploy the current local branch to pre-prod, following your system prompt."
    system_prompt = PRE_PROD_SYSTEM_PROMPT.format(
        local_branch=env.local_branch,
        pre_prod_branch=env.pre_prod_branch,
        vercel=env.vercel,
        firebase=env.firebase,
        firebase_pre_prod_alias=env.firebase_pre_prod_alias,
    )
    return await run_agent(
        prompt=prompt,
        system_prompt=system_prompt,
        cwd=repo,
        allowed_tools=["Read", "Bash", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        max_turns=20,
        allow_prod=False,
    )


async def promote_prod(repo: Path, env: EnvironmentConfig) -> AgentResult:
    prompt = "Promote the current pre-prod branch to production, following your system prompt."
    system_prompt = PROD_SYSTEM_PROMPT.format(
        pre_prod_branch=env.pre_prod_branch,
        prod_branch=env.prod_branch,
        vercel=env.vercel,
        firebase=env.firebase,
        firebase_prod_alias=env.firebase_prod_alias,
    )
    return await run_agent(
        prompt=prompt,
        system_prompt=system_prompt,
        cwd=repo,
        allowed_tools=["Read", "Bash", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        max_turns=20,
        allow_prod=True,
    )
