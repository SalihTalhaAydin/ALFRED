"""Proactive behaviour -- security alerts, morning briefings, door reminders.

Connects to HA via hass-client, subscribes to state_changed events, and
announces via TTS when something notable happens. This is the ONLY module
that talks to Home Assistant directly.
"""

import asyncio
import logging
import os
from datetime import datetime, time as dtime

import litellm
from hass_client import HomeAssistantClient
from hass_client.exceptions import AuthenticationFailed

log = logging.getLogger(__name__)

BRIEFING_PROMPT = """\
You are ALFRED, a composed British AI butler. Generate a short morning \
briefing (2-3 sentences, spoken aloud) covering:
- Weather: {weather}
- Day summary: It is {day}.
Keep it warm but concise. Start with "Good morning"."""


class Monitor:
    def __init__(self, config: dict):
        self._ws_url = config.get("ha_websocket_url", "")
        self._token = config.get("ha_token") or os.environ.get("SUPERVISOR_TOKEN")
        self._tts_entity = config.get("tts_entity", "tts.google_en_com")
        self._default_speaker = config.get("default_speaker", "media_player.living_room")
        self._fact_model = config.get(
            "fact_extraction_model", "anthropic/claude-haiku-4-5"
        )
        self._client: HomeAssistantClient | None = None
        self._briefed_today: str = ""  # date string of last briefing
        self._open_door_tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Connection with auto-reconnect
    # ------------------------------------------------------------------

    async def start(self):
        while True:
            try:
                self._client = HomeAssistantClient(self._ws_url, self._token)
                await self._client.connect()
                await self._client.subscribe_events(
                    self._on_state_changed, "state_changed"
                )
                log.info("Monitor connected to Home Assistant")
                await self._client.start_listening()
            except AuthenticationFailed:
                log.error("HA authentication failed -- check token. Not retrying.")
                return
            except Exception:
                log.warning(
                    "HA connection lost, reconnecting in 10s", exc_info=True
                )
            self._client = None
            await asyncio.sleep(10)

    # ------------------------------------------------------------------
    # Event handler
    # ------------------------------------------------------------------

    async def _on_state_changed(self, event: dict):
        data = event.get("data", {})
        new_state_obj = data.get("new_state")
        if new_state_obj is None:
            return

        entity_id: str = data.get("entity_id", "")
        new_state: str = new_state_obj.get("state", "")
        attrs: dict = new_state_obj.get("attributes", {})
        name: str = attrs.get("friendly_name", entity_id)
        device_class: str | None = attrs.get("device_class")

        old_state_obj = data.get("old_state")
        old_state = old_state_obj.get("state", "") if old_state_obj else ""

        # --- Safety sensors (always, any time) ---
        if device_class in ("smoke", "gas", "moisture") and new_state in (
            "on",
            "detected",
        ):
            await self._announce(
                f"Warning: {name} has been triggered!", all_speakers=True
            )
            return

        # --- Door / window opened at night ---
        if device_class == "door" and new_state == "on" and old_state != "on":
            if self._is_night():
                await self._announce(f"The {name} has been opened, sir.")
            # Start a reminder task regardless of time
            self._start_door_reminder(entity_id, name)

        # --- Lock unlocked at night ---
        if device_class == "lock" and new_state == "unlocked" and self._is_night():
            await self._announce(
                f"The {name} has been unlocked, sir.", urgent=True
            )

        # --- Door closed -> cancel reminder ---
        if device_class == "door" and new_state == "off" and entity_id in self._open_door_tasks:
            self._open_door_tasks.pop(entity_id).cancel()

        # --- Morning briefing (first motion of the day) ---
        if (
            device_class == "motion"
            and new_state == "on"
            and self._is_morning()
            and self._briefed_today != self._today()
        ):
            self._briefed_today = self._today()
            asyncio.create_task(self._morning_briefing())

    # ------------------------------------------------------------------
    # Proactive actions
    # ------------------------------------------------------------------

    def _start_door_reminder(self, entity_id: str, name: str):
        if entity_id in self._open_door_tasks:
            return

        async def _remind():
            await asyncio.sleep(1800)  # 30 minutes
            if self._client:
                try:
                    states = await self._client.get_states()
                    for s in states:
                        if s["entity_id"] == entity_id and s["state"] == "on":
                            await self._announce(
                                f"Sir, the {name} has been open for 30 minutes."
                            )
                            break
                except Exception:
                    log.warning("Door reminder check failed", exc_info=True)
            self._open_door_tasks.pop(entity_id, None)

        self._open_door_tasks[entity_id] = asyncio.create_task(_remind())

    async def _morning_briefing(self):
        try:
            weather = await self._get_weather()
            day = datetime.now().strftime("%A, %B %d")
            prompt = BRIEFING_PROMPT.format(weather=weather, day=day)
            response = await litellm.acompletion(
                model=self._fact_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=200,
            )
            briefing = response.choices[0].message.content.strip()
            await self._announce(briefing)
        except Exception:
            log.warning("Morning briefing failed", exc_info=True)

    async def _get_weather(self) -> str:
        if not self._client:
            return "unavailable"
        try:
            states = await self._client.get_states()
            for s in states:
                if s["entity_id"].startswith("weather."):
                    attrs = s.get("attributes", {})
                    temp = attrs.get("temperature", "?")
                    unit = attrs.get("temperature_unit", "")
                    condition = s.get("state", "unknown")
                    return f"{condition}, {temp}{unit}"
        except Exception:
            log.warning("Weather fetch failed", exc_info=True)
        return "unavailable"

    # ------------------------------------------------------------------
    # TTS announcement
    # ------------------------------------------------------------------

    async def _announce(
        self,
        message: str,
        speaker: str | None = None,
        all_speakers: bool = False,
        urgent: bool = False,
    ):
        if not self._client:
            log.warning("Cannot announce -- not connected to HA")
            return

        target_speaker = speaker or self._default_speaker
        try:
            await self._client.call_service(
                domain="tts",
                service="speak",
                target={"entity_id": self._tts_entity},
                service_data={
                    "media_player_entity_id": target_speaker,
                    "message": message,
                },
            )
            log.info("Announced: %s", message[:80])
        except Exception:
            log.warning("TTS announce failed", exc_info=True)

        if urgent:
            try:
                await self._client.call_service(
                    domain="persistent_notification",
                    service="create",
                    service_data={"message": message, "title": "ALFRED"},
                )
            except Exception:
                log.warning("Persistent notification failed", exc_info=True)

    # ------------------------------------------------------------------
    # Time helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_night() -> bool:
        hour = datetime.now().hour
        return hour >= 22 or hour < 6

    @staticmethod
    def _is_morning() -> bool:
        hour = datetime.now().hour
        return 6 <= hour < 10

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")
