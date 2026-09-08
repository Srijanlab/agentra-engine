"""server/routes/chat.py — text-to-speech + agent chat history.

Chat *turns* run Claude with a repo checkout, which the engine (Vercel, cloud
mode) doesn't have -- so `POST .../chat` and `/chat/stream` are held (503) until
they move to the loop. History reads and /tts stay here.
"""

from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from agentra import chat_store, registry

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

_CHAT_HELD = "agent chat is temporarily disabled -- it is moving to agentra-loop (where the repo checkout is)."


class TtsPayload(BaseModel):
    text: str
    agent_id: str = "custom"


class ChatPayload(BaseModel):
    message: str


@router.post("/tts")
async def text_to_speech(payload: TtsPayload) -> Response:
    try:
        from google.cloud import texttospeech
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=f"text-to-speech unavailable: {exc}") from exc

    voice_name = AGENT_VOICES.get(payload.agent_id, AGENT_VOICES["custom"])
    try:
        client = texttospeech.TextToSpeechClient()
        response = client.synthesize_speech(
            input=texttospeech.SynthesisInput(text=payload.text),
            voice=texttospeech.VoiceSelectionParams(language_code="en-US", name=voice_name),
            audio_config=texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3),
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"text-to-speech unavailable: {exc}") from exc
    return Response(content=response.audio_content, media_type="audio/mpeg")


@router.get("/apps/{app_name}/agents/{agent_id}/chat")
async def get_agent_chat(app_name: str, agent_id: str) -> dict:
    if app_name not in registry.list_apps():
        raise HTTPException(status_code=404, detail=f"app {app_name!r} not registered")
    return {"messages": chat_store.get_agent_chat_messages(app_name, agent_id)}


@router.post("/apps/{app_name}/agents/{agent_id}/chat")
async def post_agent_chat(app_name: str, agent_id: str, payload: ChatPayload) -> dict:
    raise HTTPException(status_code=503, detail=_CHAT_HELD)


@router.post("/apps/{app_name}/agents/{agent_id}/chat/stream")
async def post_agent_chat_stream(app_name: str, agent_id: str, payload: ChatPayload) -> dict:
    raise HTTPException(status_code=503, detail=_CHAT_HELD)
