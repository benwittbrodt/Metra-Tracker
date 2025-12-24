"""Config flow for Metra Tracker integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector
from homeassistant.util.yaml import load_yaml

from .const import (
    DOMAIN,
    CONF_API_TOKEN,
    CONF_LINE,
    METRA_LINES,
    METRA_STOPS_BY_LINE,
)

_LOGGER = logging.getLogger(__name__)

# Key in secrets.yaml / secrets.yml
SECRETS_TOKEN_KEY = "metra_tracker_api_token"


async def _async_load_secrets(hass: HomeAssistant) -> dict[str, Any]:
    """Load secrets.yaml or secrets.yml from the HA config directory."""
    for filename in ("secrets.yaml", "secrets.yml"):
        path = hass.config.path(filename)
        try:
            data = await hass.async_add_executor_job(load_yaml, path)
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            continue
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed reading %s: %s", path, err)
            continue
    return {}


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
            # For protobuf, just assume success on HTTP 200
            return True

    except Exception as ex:
        _LOGGER.error("Error validating token: %s", ex)
        return False


class MetraArrivalsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Metra Tracker."""

    VERSION = 2
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self) -> None:
        self.api_token: str | None = None
        self.selected_line_id: str | None = None
        self._secret_token: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: Get API token (or use secrets.yaml if present)."""
        errors: dict[str, str] = {}

        # Load secrets once per flow
        if self._secret_token is None:
            secrets = await _async_load_secrets(self.hass)
            token = secrets.get(SECRETS_TOKEN_KEY)
            self._secret_token = str(token).strip() if token else None

        # try to validate and skip this step entirely.
        if user_input is None and self._secret_token:
            _LOGGER.debug(
                "Metra Tracker: Found token in secrets (%s).", SECRETS_TOKEN_KEY
            )
            if await validate_token(self._secret_token, self.hass):
                self.api_token = self._secret_token
                return await self.async_step_line_select()
            # If secret exists but is invalid, fall through and show the form with an error
            errors["base"] = "invalid_token"

        if user_input is not None:
            entered = (user_input.get(CONF_API_TOKEN) or "").strip()
            self.api_token = entered or self._secret_token

            if not self.api_token:
                errors[CONF_API_TOKEN] = "missing_token"
            elif await validate_token(self.api_token, self.hass):
                return await self.async_step_line_select()
            else:
                errors["base"] = "invalid_token"

        token_selector = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )

        schema = vol.Schema(
            {
                (
                    vol.Optional(CONF_API_TOKEN)
                    if self._secret_token
                    else vol.Required(CONF_API_TOKEN)
                ): token_selector
            }
        )
        # return the details but on success it will just move to selecting the line
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "secrets_key": SECRETS_TOKEN_KEY,
                "secrets_file": "secrets.yaml",
            },
        )

    async def async_step_line_select(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: Select train line."""
        errors: dict[str, str] = {}

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
        errors: dict[str, str] = {}

        line_stops = METRA_STOPS_BY_LINE.get(self.selected_line_id, {})
        if not line_stops:
            return self.async_abort(reason="no_stops_found_for_line")

        stop_names = sorted(line_stops.values())
        name_to_id = {v: k for k, v in line_stops.items()}

        if user_input is not None:
            start_id = name_to_id[user_input["start_station"]]
            end_id = name_to_id[user_input["end_station"]]

            # Prevent duplicate entries of the same route, but allow same token across many entries
            unique_id = f"{self.selected_line_id}:{start_id}:{end_id}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=f"{METRA_LINES[self.selected_line_id]} Arrivals",
                data={
                    CONF_API_TOKEN: self.api_token,
                    CONF_LINE: self.selected_line_id,
                    "start_station": start_id,
                    "end_station": end_id,
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
