import aiohttp
import logging
from datetime import datetime, timedelta
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.config_entries import ConfigEntry
from homeassistant.util.dt import get_time_zone
from .const import DOMAIN, CONF_USERNAME, CONF_PASSWORD, DEFAULT_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=DEFAULT_SCAN_INTERVAL)

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
        self._tz = get_time_zone(hass.config.time_zone)
        self._base_url = "https://gtfsapi.metrarail.com/gtfs/"
        self._session = async_get_clientsession(hass)
        self._core_urls = {
            "alerts": self._base_url + "alerts",
            "tripUpdates": self._base_url + "tripUpdates",
            "positions": self._base_url + "positions",
        }
        self._schedule_urls = {
            "stops": self._base_url + "schedule/stops",
            "stop_times": self._base_url + "schedule/stop_times",
            "trips": self._base_url + "schedule/trips",
            "shapes": self._base_url + "schedule/shapes",
            "routes": self._base_url + "schedule/routes",
            "calendar": self._base_url + "schedule/calendar",
            "calendar_dates": self._base_url + "schedule/calendar_dates",
            "agency": self._base_url + "schedule/agency",
            "stop_times_per_trip": self._base_url + "schedule/stop_times/<TRIP_ID>",
        }
