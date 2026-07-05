"""Deployment Agent (vision.md 5.8), split across the environment pipeline.

deploy_beta() is what every regular cycle calls: push to the beta branch,
deploy the isolated Firebase beta project + a Vercel preview, smoke test.
It never touches production.

promote_prod() is the one place in the whole system that's allowed to touch
production, and only when called with allow_prod=True by a caller that has
already checked EnvironmentConfig.auto_remediate_prod (see
agents/prod_debug.py) or by a human running `agentos promote`.
"""

from pathlib import Path

from agentos.agents.base import AgentResult, run_agent
from agentos.environments import EnvironmentConfig

BETA_SYSTEM_PROMPT = """You are the Deployment Agent, deploying to the BETA \
environment only. You must never touch production.

Environment for this deploy:
- dev branch: {dev_branch}
- beta branch: {beta_branch}
- Vercel configured: {vercel}
- Firebase configured: {firebase} (beta alias: {firebase_beta_alias})

Steps:
1. Merge or fast-forward the beta branch to the current dev branch tip \
   (git checkout {beta_branch}, merge {dev_branch}, push).
2. If Vercel is configured, run a preview deploy (`vercel deploy`, never \
   `--prod`) and capture the resulting preview URL.
3. If Firebase is configured, switch to the beta alias \
   (`firebase use {firebase_beta_alias}`) and deploy functions \
   (`firebase deploy --only functions`). Never switch to any other alias.
4. Run any available smoke test against the preview URL.
5. If neither Vercel nor Firebase is configured, report status "skipped" \
   and do nothing else.

End your response with a fenced ```json block shaped like:
{{
  "status": "deployed" | "skipped" | "failed",
  "preview_url": "...",
  "firebase_beta_deployed": true | false,
  "notes": "..."
}}
"""

PROD_SYSTEM_PROMPT = """You are the Deployment Agent, promoting a change to \
PRODUCTION. This call has been explicitly authorized — either by a human \
running `agentos promote`, or by the Production Debugging Agent's opted-in \
auto-remediate path after the fix passed beta testing. Do exactly this and \
nothing more:

Environment for this promotion:
- beta branch: {beta_branch}
- prod branch: {prod_branch}
- Vercel configured: {vercel}
- Firebase configured: {firebase} (prod alias: {firebase_prod_alias})

Steps:
1. Merge or fast-forward the prod branch to the current beta branch tip \
   (git checkout {prod_branch}, merge {beta_branch}, push).
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


async def deploy_beta(repo: Path, env: EnvironmentConfig) -> AgentResult:
    prompt = "Deploy the current dev branch to beta, following your system prompt."
    system_prompt = BETA_SYSTEM_PROMPT.format(
        dev_branch=env.dev_branch,
        beta_branch=env.beta_branch,
        vercel=env.vercel,
        firebase=env.firebase,
        firebase_beta_alias=env.firebase_beta_alias,
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
    prompt = "Promote the current beta branch to production, following your system prompt."
    system_prompt = PROD_SYSTEM_PROMPT.format(
        beta_branch=env.beta_branch,
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
