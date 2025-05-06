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

from .const import DOMAIN, DEFAULT_NAME, CONF_USERNAME, CONF_PASSWORD

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def validate_auth(username: str, password: str, hass: HomeAssistant) -> bool:
    """Validate credentials with Metra API."""
    session = async_get_clientsession(hass)

    try:
        async with session.get(
            "https://gtfsapi.metrarail.com/gtfs/tripUpdates",
            auth=aiohttp.BasicAuth(username, password),
            timeout=10,
            ssl=False,  # Try with False if having SSL issues
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

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_USERNAME])
            self._abort_if_unique_id_configured()

            if await validate_auth(
                user_input[CONF_USERNAME], user_input[CONF_PASSWORD], self.hass
            ):
                return self.async_create_entry(title=DEFAULT_NAME, data=user_input)
            errors["base"] = "invalid_auth"

        return self.async_show_form(
            step_id="user",
            data_schema=DATA_SCHEMA,
            errors=errors,
        )
