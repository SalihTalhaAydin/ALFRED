"""The ALFRED Conversation integration."""
from __future__ import annotations

import openai

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.httpx_client import get_async_client

from .const import CONF_BASE_URL, DEFAULT_BASE_URL, DOMAIN, LOGGER

PLATFORMS = (Platform.CONVERSATION,)

type AlfredConfigEntry = ConfigEntry[openai.AsyncOpenAI]


async def async_setup_entry(hass: HomeAssistant, entry: AlfredConfigEntry) -> bool:
    """Set up ALFRED Conversation from a config entry."""
    base_url = entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL)
    api_key = entry.data.get(CONF_API_KEY, "not-needed")

    client = openai.AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        http_client=get_async_client(hass),
    )

    try:
        await hass.async_add_executor_job(client.with_options(timeout=10.0).models.list)
    except openai.OpenAIError as err:
        LOGGER.warning("Could not reach ALFRED at %s: %s", base_url, err)

    entry.runtime_data = client
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AlfredConfigEntry) -> bool:
    """Unload ALFRED Conversation."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
