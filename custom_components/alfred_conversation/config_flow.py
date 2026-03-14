"""Config flow for ALFRED Conversation."""
from __future__ import annotations

from typing import Any

import openai
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY, CONF_LLM_HASS_API
from homeassistant.helpers.httpx_client import get_async_client

from .const import (
    CONF_BASE_URL,
    CONF_CHAT_MODEL,
    CONF_MAX_TOKENS,
    CONF_PROMPT,
    CONF_TEMPERATURE,
    CONF_TOP_P,
    DEFAULT_BASE_URL,
    DEFAULT_CHAT_MODEL,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    DEFAULT_TOP_P,
    DOMAIN,
)


class AlfredConversationConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ALFRED Conversation."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            base_url = user_input.get(CONF_BASE_URL, DEFAULT_BASE_URL)
            try:
                client = openai.AsyncOpenAI(
                    api_key="not-needed",
                    base_url=base_url,
                    http_client=get_async_client(self.hass),
                )
                await self.hass.async_add_executor_job(
                    client.with_options(timeout=10.0).models.list
                )
            except openai.OpenAIError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title="ALFRED",
                    data={
                        CONF_BASE_URL: base_url,
                        CONF_API_KEY: "not-needed",
                    },
                    options={
                        CONF_CHAT_MODEL: user_input.get(
                            CONF_CHAT_MODEL, DEFAULT_CHAT_MODEL
                        ),
                        CONF_PROMPT: user_input.get(CONF_PROMPT, ""),
                        CONF_LLM_HASS_API: "assist",
                        CONF_MAX_TOKENS: DEFAULT_MAX_TOKENS,
                        CONF_TEMPERATURE: DEFAULT_TEMPERATURE,
                        CONF_TOP_P: DEFAULT_TOP_P,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BASE_URL, default=DEFAULT_BASE_URL): str,
                    vol.Optional(CONF_CHAT_MODEL, default=DEFAULT_CHAT_MODEL): str,
                }
            ),
            errors=errors,
        )
