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
    CONF_API_TOKEN,
    CONF_LINE,
    METRA_LINES,
    METRA_STOPS_BY_LINE,
)

_LOGGER = logging.getLogger(__name__)


async def validate_token(api_token: str, hass: HomeAssistant) -> bool:
    """Validate API token with Metra public GTFS endpoint."""
    session = async_get_clientsession(hass)
    url = (
        f"https://gtfspublic.metrarr.com/gtfs/public/tripupdates?api_token={api_token}"
    )

    try:
        async with session.get(url, timeout=10) as response:
            if response.status != 200:
                _LOGGER.error("Invalid token or request failed: %s", response.status)
                return False

            # Basic sanity check: expecting GTFS Realtime feed (protobuf or JSON)
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                data = await response.json()
                return isinstance(data, (dict, list))
            else:
                # For protobuf, just assume success on HTTP 200
                return True
    except Exception as ex:
        _LOGGER.error("Error validating token: %s", ex)
        return False


class MetraArrivalsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Metra Tracker."""

    VERSION = 2
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self):
        self.api_token = None
        self.selected_line_id = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: Get API token."""
        errors = {}

        if user_input is not None:
            self.api_token = user_input[CONF_API_TOKEN]
            await self.async_set_unique_id(self.api_token)
            self._abort_if_unique_id_configured()

            if await validate_token(self.api_token, self.hass):
                return await self.async_step_line_select()
            errors["base"] = "invalid_token"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_API_TOKEN): str}),
            errors=errors,
        )

    async def async_step_line_select(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: Select train line."""
        errors = {}

        line_options = {
            friendly_name: line_id for line_id, friendly_name in METRA_LINES.items()
        }

        if user_input is not None:
            selected_name = user_input["line"]
            self.selected_line_id = line_options[selected_name]
            return await self.async_step_stop_select()

        return self.async_show_form(
            step_id="line_select",
            data_schema=vol.Schema(
                {vol.Required("line"): vol.In(sorted(line_options.keys()))}
            ),
            errors=errors,
        )

    async def async_step_stop_select(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3: Select start and end stops for selected line."""
        errors = {}

        line_stops = METRA_STOPS_BY_LINE.get(self.selected_line_id, {})

        if not line_stops:
            return self.async_abort(reason="no_stops_found_for_line")

        stop_names = sorted(line_stops.values())
        name_to_id = {v: k for k, v in line_stops.items()}

        if user_input is not None:
            return self.async_create_entry(
                title=f"{METRA_LINES[self.selected_line_id]} Arrivals",
                data={
                    CONF_API_TOKEN: self.api_token,
                    CONF_LINE: self.selected_line_id,
                    "start_station": name_to_id[user_input["start_station"]],
                    "end_station": name_to_id[user_input["end_station"]],
                    "start_station_name": user_input["start_station"],
                    "end_station_name": user_input["end_station"],
                },
            )

        return self.async_show_form(
            step_id="stop_select",
            data_schema=vol.Schema(
                {
                    vol.Required("start_station"): vol.In(stop_names),
                    vol.Required("end_station"): vol.In(stop_names),
                }
            ),
            errors=errors,
        )
