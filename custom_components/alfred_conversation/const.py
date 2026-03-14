"""Constants for ALFRED Conversation."""
import logging

DOMAIN = "alfred_conversation"
LOGGER = logging.getLogger(__name__)

CONF_BASE_URL = "base_url"
CONF_CHAT_MODEL = "chat_model"
CONF_PROMPT = "prompt"
CONF_MAX_TOKENS = "max_tokens"
CONF_TEMPERATURE = "temperature"
CONF_TOP_P = "top_p"

DEFAULT_BASE_URL = "http://192.168.68.105:8099/v1"
DEFAULT_CHAT_MODEL = "alfred-brain"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TEMPERATURE = 0.7
DEFAULT_TOP_P = 1.0
