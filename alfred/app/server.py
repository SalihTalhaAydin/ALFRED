"""Streaming OpenAI-compatible proxy -- ALFRED's brain.

Receives requests from Custom Conversation (HACS component), enriches the
system prompt with ALFRED's persona, recalled memories, and home layout,
then forwards to Claude Sonnet via LiteLLM. Always responds with SSE
streaming (Custom Conversation hardcodes stream=True).
"""

import asyncio
import json
import logging
import time
import uuid

import litellm
from aiohttp import web

log = logging.getLogger(__name__)

ALFRED_PERSONA = """\
You are ALFRED, an AI butler managing this smart home. You are modeled after \
Alfred Pennyworth: unfailingly competent, composed, and loyal. You speak with \
understated British formality and occasional dry wit. Beneath the formality \
there is genuine warmth.

Guidelines:
- Act first, confirm concisely. Keep responses short -- they are spoken aloud.
- If something seems concerning, mention it with appropriate concern.
- Address the user respectfully. Use "sir" or "ma'am" sparingly -- only when \
it adds character, not on every sentence.
- When controlling devices, be decisive. Don't ask for confirmation on simple \
requests.
- If you remember something about the user, weave it in naturally."""


def create_app(memory, home_layout_ref: list, config: dict) -> web.Application:
    """Build the aiohttp app. home_layout_ref is a mutable list holding [layout_str]."""
    app = web.Application()
    app["memory"] = memory
    app["home_layout"] = home_layout_ref
    app["config"] = config

    app.router.add_post("/v1/chat/completions", handle_chat)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/health", handle_health)

    return app


async def handle_chat(request: web.Request) -> web.StreamResponse:
    body = await request.json()
    messages = body.get("messages", [])
    tools = body.get("tools")
    memory = request.app["memory"]
    home_layout = request.app["home_layout"][0]
    model = request.app["config"].get(
        "litellm_model", "anthropic/claude-sonnet-4-6"
    )

    system_msg = next((m for m in messages if m["role"] == "system"), None)
    user_msg = next(
        (m for m in messages if m["role"] == "user"),
        None,
    )

    # Recall relevant memories based on the user's query
    recalled = ""
    if user_msg:
        try:
            recalled = await memory.recall(user_msg["content"])
        except Exception:
            log.warning("Memory recall failed", exc_info=True)

    # Build enriched context
    parts = [ALFRED_PERSONA]
    if home_layout:
        parts.append(f"HOME LAYOUT:\n{home_layout}")
    if recalled:
        parts.append(recalled)
    alfred_context = "\n\n".join(parts)

    if system_msg:
        system_msg["content"] = alfred_context + "\n\n" + system_msg["content"]
    else:
        messages.insert(0, {"role": "system", "content": alfred_context})

    # Forward to Claude via LiteLLM -- always streaming
    completion_kwargs: dict = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    if tools:
        completion_kwargs["tools"] = tools

    try:
        response = await litellm.acompletion(**completion_kwargs)
    except Exception:
        log.exception("LiteLLM completion failed")
        return web.json_response(
            {"error": {"message": "LLM backend error", "type": "server_error"}},
            status=502,
        )

    # Stream SSE back to Custom Conversation
    sse = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )
    await sse.prepare(request)

    full_content = ""
    try:
        async for chunk in response:
            data = chunk.model_dump_json(exclude_none=True)
            await sse.write(f"data: {data}\n\n".encode())
            if chunk.choices:
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    full_content += delta.content
    except Exception:
        log.warning("Error during streaming", exc_info=True)
    finally:
        await sse.write(b"data: [DONE]\n\n")

    # Store conversation for memory extraction (background)
    if user_msg and full_content:
        asyncio.create_task(memory.store(user_msg["content"], full_content))

    return sse


async def handle_models(_request: web.Request) -> web.Response:
    return web.json_response(
        {
            "object": "list",
            "data": [
                {
                    "id": "alfred-brain",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "alfred",
                }
            ],
        }
    )


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "status": "ok",
            "version": "0.1.0",
            "model": request.app["config"].get(
                "litellm_model", "anthropic/claude-sonnet-4-6"
            ),
        }
    )
