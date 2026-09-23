"""server/routes/app_payloads.py — request bodies for the app registration and config endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class RepoEntryPayload(BaseModel):
    name: str
    repo_url: str
    branch: str = "main"
    role: str = "code"
    deploy_strategy: str | None = None


class RegisterAppPayload(BaseModel):
    name: str
    repo_url: str | None = None
    branch: str = "main"
    objective: str | None = None
    vercel: bool | None = None
    firebase: bool | None = None
    ci_cd_on_push: bool | None = None
    pre_prod_branch: str | None = None
    prod_branch: str | None = None
    schedule_hours: float | None = None
    schedule_continuous: bool | None = None
    alarm_enabled: bool | None = None
    slack_channel_id: str | None = None
    repos: list[RepoEntryPayload] | None = None


class UpdateAppPayload(BaseModel):
    objective: str | None = None
    vercel: bool | None = None
    firebase: bool | None = None
    ci_cd_on_push: bool | None = None
    pre_prod_branch: str | None = None
    prod_branch: str | None = None
    schedule_hours: float | None = None
    schedule_continuous: bool | None = None
    alarm_enabled: bool | None = None
    slack_channel_id: str | None = None


class BacklogRequestPayload(BaseModel):
    type: str = "feature_request"
    title: str | None = None
    description: str
    severity: str | None = None
