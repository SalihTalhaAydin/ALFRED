"""ALFRED entry point -- wires server, memory, monitor, and home layout refresh."""

import asyncio
import json
import logging
import os
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

from .memory import Memory
from .monitor import Monitor
from .server import create_app

log = logging.getLogger(__name__)

# Mutable container so server.py can always read the latest layout
_home_layout: list[str] = [""]


def load_config() -> dict:
    """Load configuration from add-on options or .env for standalone dev."""
    options_path = Path("/data/options.json")
    if options_path.exists():
        with open(options_path) as f:
            opts = json.load(f)
        return {
            "anthropic_api_key": opts.get("anthropic_api_key", ""),
            "openai_api_key": opts.get("openai_api_key", ""),
            "tts_entity": opts.get("tts_entity", "tts.google_en_com"),
            "default_speaker": opts.get("default_speaker", "media_player.living_room"),
            "ha_websocket_url": "",
            "ha_token": None,
            "db_path": "/data/alfred.db",
            "litellm_model": os.environ.get(
                "LITELLM_MODEL", "anthropic/claude-sonnet-4-6"
            ),
            "embedding_model": "text-embedding-3-small",
            "fact_extraction_model": os.environ.get(
                "FACT_EXTRACTION_MODEL", "anthropic/claude-haiku-4-5"
            ),
        }

    # Standalone dev mode
    load_dotenv()
    return {
        "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "openai_api_key": os.environ.get("OPENAI_API_KEY", ""),
        "tts_entity": os.environ.get("TTS_ENTITY", "tts.google_en_com"),
        "default_speaker": os.environ.get(
            "DEFAULT_SPEAKER", "media_player.living_room"
        ),
        "ha_websocket_url": _build_ws_url(),
        "ha_token": os.environ.get("HA_TOKEN", ""),
        "db_path": os.environ.get("DB_PATH", "./alfred.db"),
        "litellm_model": os.environ.get(
            "LITELLM_MODEL", "anthropic/claude-sonnet-4-6"
        ),
        "embedding_model": os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
        "fact_extraction_model": os.environ.get(
            "FACT_EXTRACTION_MODEL", "anthropic/claude-haiku-4-5"
        ),
    }


def _build_ws_url() -> str:
    ha_url = os.environ.get("HA_URL", "http://homeassistant.local:8123")
    return ha_url.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"


async def fetch_home_layout(config: dict) -> str:
    """Query HA's REST template API for floor/room/entity mapping."""
    import aiohttp as _aiohttp

    template = (
        "{% for floor in floors() %}"
        "{{ floor_name(floor) }}\n"
        "{% for area in floor_areas(floor) %}"
        "  - {{ area_name(area) }}: {{ area_entities(area) | join(', ') }}\n"
        "{% endfor %}"
        "{% endfor %}"
    )

    ha_url = config.get("ha_websocket_url", "")
    token = config.get("ha_token") or os.environ.get("SUPERVISOR_TOKEN", "")

    # Build REST base URL from WebSocket URL or fall back
    if ha_url:
        base = ha_url.replace("wss://", "https://").replace("ws://", "http://")
        base = base.replace("/api/websocket", "")
    else:
        base = "http://supervisor/core"

    url = f"{base}/api/template"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        timeout = _aiohttp.ClientTimeout(total=10)
        async with _aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json={"template": template}, headers=headers) as resp:
                if resp.status == 200:
                    layout = (await resp.text()).strip()
                    if layout:
                        log.info("Home layout loaded (%d chars)", len(layout))
                    else:
                        log.info("No floor/area layout found in HA (empty result)")
                    return layout
                log.warning("Home layout fetch returned %d", resp.status)
                return ""
    except Exception:
        log.warning("Could not fetch home layout from HA", exc_info=True)
        return ""


async def layout_refresh_loop(config: dict, interval: int = 1800):
    """Refresh the home layout every `interval` seconds."""
    while True:
        await asyncio.sleep(interval)
        try:
            layout = await fetch_home_layout(config)
            _home_layout[0] = layout
        except Exception:
            log.warning("Layout refresh failed", exc_info=True)


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    config = load_config()

    # Set API keys as env vars for LiteLLM and OpenAI SDK
    if config["anthropic_api_key"]:
        os.environ.setdefault("ANTHROPIC_API_KEY", config["anthropic_api_key"])
    if config["openai_api_key"]:
        os.environ.setdefault("OPENAI_API_KEY", config["openai_api_key"])

    # Initialize memory
    memory = Memory(
        db_path=config["db_path"],
        embedding_model=config["embedding_model"],
        fact_model=config["fact_extraction_model"],
    )
    await memory.init()

    # Fetch initial home layout
    _home_layout[0] = await fetch_home_layout(config)

    # Start proactive monitor in background
    monitor = Monitor(config)
    asyncio.create_task(monitor.start())

    # Start layout refresh in background
    asyncio.create_task(layout_refresh_loop(config))

    # Start the streaming proxy server
    app = create_app(memory, _home_layout, config)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("ALFRED_PORT", "5000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("ALFRED listening on 0.0.0.0:%d", port)

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
