import aiohttp
import logging
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.config_entries import ConfigEntry


_LOGGER = logging.getLogger(__name__)

API_TIMEOUT = 10


class MetraAPI:

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self._username = entry.data[CONF_USERNAME]
        self._password = entry.data[CONF_PASSWORD]
        self._tz = get_time_zone("America/Chicago")
        self._base_url = "https://api.metra.com/..."  # replace with actual endpoint
