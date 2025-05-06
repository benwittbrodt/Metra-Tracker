"""Sensor platform for Metra UP-W train arrivals."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

import aiohttp
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.dt import get_time_zone

from .const import DOMAIN, CONF_USERNAME, CONF_PASSWORD, DEFAULT_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=DEFAULT_SCAN_INTERVAL)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    coordinator = MetraArrivalsCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    async_add_entities([MetraArrivalsSensor(coordinator, entry)])


class MetraArrivalsCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Metra data."""

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

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch and process train data from Metra API."""
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(
                "https://gtfsapi.metrarail.com/gtfs/tripUpdates",
                auth=aiohttp.BasicAuth(self._username, self._password),
                timeout=10,
            ) as response:

                _LOGGER.debug("API response status: %s", response.status)

                if response.status != 200:
                    _LOGGER.error("API request failed with status %s", response.status)
                    return {"error": f"API returned {response.status}"}

                data = await response.json()
                _LOGGER.debug("Received %s trips from API", len(data))

                trains = []
                for item in data:
                    try:
                        trip = item.get("trip_update", {})
                        trip_info = trip.get("trip", {})

                        # Only process UP-W trains
                        if trip_info.get("route_id") != "UP-W":
                            continue

                        oakpark_time = None
                        otc_time = None

                        # Process all stops for this train
                        for stop in trip.get("stop_time_update", []):
                            stop_id = stop.get("stop_id")
                            arrival_raw = (
                                stop.get("arrival", {}).get("time", {}).get("low")
                            )

                            if not arrival_raw:
                                continue

                            try:
                                dt = datetime.fromisoformat(
                                    arrival_raw.replace("Z", "+00:00")
                                )
                                local_time = dt.astimezone(self._tz)

                                if stop_id == "OAKPARK":
                                    oakpark_time = local_time
                                elif stop_id == "OTC":
                                    otc_time = local_time

                            except Exception as e:
                                _LOGGER.debug("Error processing stop time: %s", e)
                                continue

                        # Only add trains with both times
                        if oakpark_time and otc_time:
                            trains.append(
                                {
                                    "oakpark": oakpark_time.strftime("%H:%M"),
                                    "otc": otc_time.strftime("%H:%M"),
                                    "oakpark_full": oakpark_time.isoformat(),
                                    "otc_full": otc_time.isoformat(),
                                    "trip_id": trip_info.get("trip_id", "unknown"),
                                }
                            )

                    except Exception as ex:
                        _LOGGER.warning("Error processing trip: %s", ex, exc_info=True)
                        continue

                # Sort by Oak Park departure time
                trains.sort(key=lambda x: x["oakpark_full"])
                _LOGGER.debug("Processed %d UP-W trains", len(trains))

                return {
                    "trains": trains[:3],  # Only keep next 3 trains
                    "count": len(trains),
                    "last_update": datetime.now().isoformat(),
                }

        except Exception as ex:
            _LOGGER.error("Error fetching data: %s", ex, exc_info=True)
            return {"error": str(ex)}


class MetraArrivalsSensor(SensorEntity):
    """Representation of a Metra Arrivals sensor."""

    _attr_icon = "mdi:train"
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: MetraArrivalsCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the sensor."""
        self._coordinator = coordinator
        self._attr_name = "Metra UP-W Arrivals"
        self._attr_unique_id = f"metra_upw_arrivals_{entry.entry_id}"

    @property
    def state(self) -> str:
        """Return the state of the sensor."""
        if not self.available:
            return "Unavailable"
        return f"{self._coordinator.data.get('count', 0)} upcoming trains"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        return self._coordinator.data or {}

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return (
            self._coordinator.last_update_success
            and isinstance(self._coordinator.data, dict)
            and "error" not in self._coordinator.data
        )

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )

    async def async_update(self) -> None:
        """Update the entity."""
        await self._coordinator.async_request_refresh()
