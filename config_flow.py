"""Config flow for Metra Tracker integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN,
    DEFAULT_NAME,
    CONF_LINE,
    CONF_USERNAME,
    CONF_PASSWORD,
    METRA_LINES,
    METRA_STOPS_BY_LINE,
)

_LOGGER = logging.getLogger(__name__)


async def validate_auth(username: str, password: str, hass: HomeAssistant) -> bool:
    """Validate credentials with Metra API."""
    session = async_get_clientsession(hass)

    try:
        async with session.get(
            "https://gtfsapi.metrarail.com/gtfs/tripUpdates",
            auth=aiohttp.BasicAuth(username, password),
            timeout=10,
            ssl=False,
        ) as response:
            if response.status == 401:
                return False
            data = await response.json()
            return isinstance(data, list)
    except Exception as ex:
        _LOGGER.error("Validation error: %s", ex)
        return False


class MetraArrivalsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Metra Tracker."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self):
        self.username = None
        self.password = None
        self.selected_line_id = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: Get credentials."""
        errors = {}

        if user_input is not None:
            self.username = user_input[CONF_USERNAME]
            self.password = user_input[CONF_PASSWORD]
            await self.async_set_unique_id(self.username)
            self._abort_if_unique_id_configured()

            if await validate_auth(self.username, self.password, self.hass):
                return await self.async_step_line_select()
            errors["base"] = "invalid_auth"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_line_select(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: Select train line."""
        errors = {}

        line_name_map = {v: k for k, v in METRA_LINES.items()}  # name -> id

        if user_input is not None:
            selected_name = user_input["line"]
            self.selected_line_id = line_name_map[selected_name]
            return await self.async_step_stop_select()

        return self.async_show_form(
            step_id="line_select",
            data_schema=vol.Schema(
                {vol.Required("line"): vol.In(list(line_name_map.keys()))}
            ),
            errors=errors,
        )

    async def async_step_stop_select(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3: Select start and end stops for selected line."""
        errors = {}
        stops = METRA_STOPS_BY_LINE.get(self.selected_line_id, [])

        if not stops:
            errors["base"] = "no_stops"
            return self.async_abort(reason="no_stops_found_for_line")

        if user_input is not None:
            return self.async_create_entry(
                title=f"Metra: {METRA_LINES[self.selected_line_id]}",
                data={
                    CONF_USERNAME: self.username,
                    CONF_PASSWORD: self.password,
                    CONF_LINE: self.selected_line_id,
                    "line_id": self.selected_line_id,
                    "start_station": user_input["start_station"],
                    "end_station": user_input["end_station"],
                },
            )

        return self.async_show_form(
            step_id="stop_select",
            data_schema=vol.Schema(
                {
                    vol.Required("start_station"): vol.In(stops),
                    vol.Required("end_station"): vol.In(stops),
                }
            ),
            errors=errors,
        )
