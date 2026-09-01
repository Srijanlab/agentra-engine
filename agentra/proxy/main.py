"""Translates Anthropic Messages API calls into NVIDIA NIM chat-completions calls."""

import json
import logging
import os
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger("agentra.proxy")
logging.basicConfig(level=logging.INFO)

app = FastAPI()

NIM_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NIM_API_KEY = os.getenv("NVIDIA_API_KEY", "")
NIM_MODEL = os.getenv("NIM_MODEL", "meta/llama-3.2-90b-vision-instruct")


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "model": NIM_MODEL}


def translate_anthropic_to_openai(anthropic_req: dict) -> dict:
    messages = []

    # Handle system prompt (can be string or list of blocks)
    if "system" in anthropic_req and anthropic_req["system"]:
        sys_content = anthropic_req["system"]
        if isinstance(sys_content, list):
            sys_text = "".join(
                b.get("text", "") for b in sys_content if isinstance(b, dict)
            )
        else:
            sys_text = str(sys_content)
        if sys_text:
            messages.append({"role": "system", "content": sys_text})

    # Handle normal messages
    for msg in anthropic_req.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = []
            for b in content:
                if isinstance(b, dict):
                    b_type = b.get("type")
                    if b_type == "text":
                        text_parts.append(b.get("text", ""))
                    elif b_type == "tool_result":
                        res_content = b.get("content", "")
                        if isinstance(res_content, list):
                            res_content = "\n".join(
                                x.get("text", "") for x in res_content if isinstance(x, dict)
                            )
                        text_parts.append(f"[Tool Result]: {res_content}")
                    elif b_type == "tool_use":
                        text_parts.append(
                            f"[Tool Call {b.get('name')}]: {json.dumps(b.get('input', {}))}"
                        )
                elif isinstance(b, str):
                    text_parts.append(b)
            content = "\n".join(text_parts)
        elif not isinstance(content, str):
            content = str(content)
        messages.append({"role": role, "content": content or " "})

    # Handle tools
    tools = []
    for tool in anthropic_req.get("tools", []):
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            }
        )

    openai_req = {
        "model": NIM_MODEL,
        "messages": messages,
        "stream": anthropic_req.get("stream", False),
    }

    if tools:
        openai_req["tools"] = tools

    if "max_tokens" in anthropic_req:
        openai_req["max_tokens"] = min(anthropic_req["max_tokens"], 4096)

    return openai_req


async def stream_generator(response):
    msg_start = {
        "type": "message_start",
        "message": {
            "id": "msg_nim",
            "type": "message",
            "role": "assistant",
            "model": "nim-model",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 1},
        },
    }
    yield f"event: message_start\ndata: {json.dumps(msg_start)}\n\n"

    current_index = 0
    text_block_open = False
    active_tools = {}
    stop_reason = "end_turn"
    total_tokens = 0

    async for line in response.aiter_lines():
        if not line.startswith("data: ") or line.strip() == "data: [DONE]":
            continue
        try:
            data = json.loads(line[6:])
            choices = data.get("choices", [])
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            finish = choices[0].get("finish_reason")
            if finish == "tool_calls":
                stop_reason = "tool_use"
            elif finish == "stop":
                stop_reason = "end_turn"

            content = delta.get("content")
            if content:
                if not text_block_open:
                    block_start = {
                        "type": "content_block_start",
                        "index": current_index,
                        "content_block": {"type": "text", "text": ""},
                    }
                    yield f"event: content_block_start\ndata: {json.dumps(block_start)}\n\n"
                    text_block_open = True

                total_tokens += 1
                chunk = {
                    "type": "content_block_delta",
                    "index": current_index,
                    "delta": {"type": "text_delta", "text": content},
                }
                yield f"event: content_block_delta\ndata: {json.dumps(chunk)}\n\n"

            tool_calls = delta.get("tool_calls", [])
            for tc in tool_calls:
                tc_idx = tc.get("index", 0)
                if tc_idx not in active_tools:
                    if text_block_open:
                        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': current_index})}\n\n"
                        current_index += 1
                        text_block_open = False

                    tc_id = tc.get("id") or f"toolu_{uuid.uuid4().hex[:8]}"
                    fn_name = tc.get("function", {}).get("name", "")
                    active_tools[tc_idx] = current_index
                    block_start = {
                        "type": "content_block_start",
                        "index": current_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tc_id,
                            "name": fn_name,
                            "input": {},
                        },
                    }
                    yield f"event: content_block_start\ndata: {json.dumps(block_start)}\n\n"
                    current_index += 1
                    stop_reason = "tool_use"

                args_delta = tc.get("function", {}).get("arguments", "")
                if args_delta:
                    total_tokens += 1
                    chunk = {
                        "type": "content_block_delta",
                        "index": active_tools[tc_idx],
                        "delta": {"type": "input_json_delta", "partial_json": args_delta},
                    }
                    yield f"event: content_block_delta\ndata: {json.dumps(chunk)}\n\n"

        except Exception as err:
            logger.warning("Stream parse error: %s", err)
            continue

    if text_block_open:
        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': current_index})}\n\n"
    elif not active_tools and not text_block_open:
        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
    elif active_tools:
        for blk_idx in active_tools.values():
            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': blk_idx})}\n\n"

    msg_delta = {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": total_tokens or 1},
    }
    yield f"event: message_delta\ndata: {json.dumps(msg_delta)}\n\n"

    msg_stop = {"type": "message_stop"}
    yield f"event: message_stop\ndata: {json.dumps(msg_stop)}\n\n"


@app.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()
    openai_req = translate_anthropic_to_openai(body)

    headers = {
        "Authorization": f"Bearer {NIM_API_KEY}",
        "Content-Type": "application/json",
    }

    client = httpx.AsyncClient(timeout=120.0)

    if openai_req.get("stream"):
        req = client.build_request("POST", NIM_API_URL, headers=headers, json=openai_req)
        response = await client.send(req, stream=True)
        return StreamingResponse(stream_generator(response), media_type="text/event-stream")
    else:
        response = await client.post(NIM_API_URL, headers=headers, json=openai_req)
        data = response.json()

        choices = data.get("choices", [])
        if not choices:
            logger.error("NIM API returned non-choice response: %s", data)
            return JSONResponse(status_code=502, content={"error": data})

        msg_content = choices[0].get("message", {}).get("content", "") or ""

        anthropic_resp = {
            "id": data.get("id", "msg_nim"),
            "type": "message",
            "role": "assistant",
            "model": "nim-model",
            "content": [{"type": "text", "text": msg_content}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
                "output_tokens": data.get("usage", {}).get("completion_tokens", 0),
            },
        }

        # Handle tool calls
        if choices[0].get("message", {}).get("tool_calls"):
            anthropic_resp["stop_reason"] = "tool_use"
            anthropic_resp["content"] = []
            for tc in choices[0]["message"]["tool_calls"]:
                anthropic_resp["content"].append(
                    {
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "input": json.loads(tc["function"]["arguments"]),
                    }
                )

        return anthropic_resp
