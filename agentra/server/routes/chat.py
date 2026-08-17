"""server/routes/chat.py — text-to-speech synthesis and agent chat sessions."""

from __future__ import annotations

import asyncio
import json
import logging
from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agentra import chat_store, registry
from agentra.server.utils import AGENT_CHAT_SYSTEM_PROMPTS, _chat_agent_label

logger = logging.getLogger(__name__)

router = APIRouter()

AGENT_VOICES = {
    "orchestrator": "en-US-Neural2-D",
    "codebase": "en-US-Neural2-A",
    "discovery": "en-US-Neural2-C",
    "implementation": "en-US-Neural2-I",
    "testing": "en-US-Neural2-E",
    "deployment": "en-US-Neural2-J",
    "feedback": "en-US-Neural2-F",
    "prod_debug": "en-US-Neural2-G",
    "custom": "en-US-Neural2-H",
}


class TtsPayload(BaseModel):
    text: str
    agent_id: str = "custom"


class ChatPayload(BaseModel):
    message: str


def _finalize_chat_turn(app_name: str, agent_id: str, raw_text: str, session_id: str | None) -> str:
    response_text = raw_text or "(no response)"
    if session_id:
        chat_store.set_agent_session_id(app_name, agent_id, session_id)
    chat_store.record_agent_chat_message(app_name, agent_id, "agent", response_text)
    return response_text


@router.post("/tts")
async def text_to_speech(payload: TtsPayload) -> Response:
    from google.cloud import texttospeech

    voice_name = AGENT_VOICES.get(payload.agent_id, AGENT_VOICES["custom"])

    def _synthesize() -> bytes:
        client = texttospeech.TextToSpeechClient()
        response = client.synthesize_speech(
            input=texttospeech.SynthesisInput(text=payload.text),
            voice=texttospeech.VoiceSelectionParams(language_code="en-US", name=voice_name),
            audio_config=texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3),
        )
        return response.audio_content

    try:
        audio = await asyncio.get_running_loop().run_in_executor(None, _synthesize)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"text-to-speech unavailable: {exc}") from exc

    return Response(content=audio, media_type="audio/mpeg")


@router.get("/apps/{app_name}/agents/{agent_id}/chat")
async def get_agent_chat(app_name: str, agent_id: str) -> dict:
    if app_name not in registry.list_apps():
        raise HTTPException(status_code=404, detail=f"app {app_name!r} not registered")
    return {"messages": chat_store.get_agent_chat_messages(app_name, agent_id)}


@router.post("/apps/{app_name}/agents/{agent_id}/chat")
async def post_agent_chat(app_name: str, agent_id: str, payload: ChatPayload) -> dict:
    repo = registry.get_app_repo(app_name)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"app {app_name!r} not registered")

    chat_store.record_agent_chat_message(app_name, agent_id, "human", payload.message)

    from agentra.agents.base import run_agent
    system_prompt = AGENT_CHAT_SYSTEM_PROMPTS.get(agent_id, AGENT_CHAT_SYSTEM_PROMPTS["custom"])
    resume_session_id = chat_store.get_agent_session_id(app_name, agent_id)

    result = await run_agent(
        prompt=payload.message,
        system_prompt=system_prompt,
        cwd=repo,
        allowed_tools=["Read", "Grep", "Glob"],
        max_turns=10,
        agent_label=_chat_agent_label(agent_id),
        resume=resume_session_id,
    )

    response_text = _finalize_chat_turn(app_name, agent_id, result.text, result.session_id)
    return {"response": response_text}


@router.post("/apps/{app_name}/agents/{agent_id}/chat/stream")
async def post_agent_chat_stream(app_name: str, agent_id: str, payload: ChatPayload) -> StreamingResponse:
    repo = registry.get_app_repo(app_name)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"app {app_name!r} not registered")

    chat_store.record_agent_chat_message(app_name, agent_id, "human", payload.message)

    from agentra.agents.base import stream_chat_turn
    system_prompt = AGENT_CHAT_SYSTEM_PROMPTS.get(agent_id, AGENT_CHAT_SYSTEM_PROMPTS["custom"])
    resume_session_id = chat_store.get_agent_session_id(app_name, agent_id)

    async def event_stream():
        async for event in stream_chat_turn(
            prompt=payload.message,
            system_prompt=system_prompt,
            cwd=repo,
            allowed_tools=["Read", "Grep", "Glob"],
            max_turns=10,
            agent_label=_chat_agent_label(agent_id),
            resume=resume_session_id,
        ):
            if event["type"] == "delta":
                yield f"data: {json.dumps({'delta': event['text']})}\n\n"
            else:
                response_text = _finalize_chat_turn(app_name, agent_id, event["text"], event["session_id"])
                yield f"data: {json.dumps({'done': True, 'response': response_text})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
