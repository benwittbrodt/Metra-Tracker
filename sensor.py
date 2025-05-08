"""Sensor platform for Metra train arrivals."""

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
from homeassistant.util.dt import get_time_zone, now

from .const import (
    DOMAIN,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_LINE,
    CONF_END_STATION,
    CONF_START_STATION,
    DEFAULT_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=DEFAULT_SCAN_INTERVAL)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    coordinator = MetraArrivalsCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await coordinator.async_config_entry_first_refresh()

    # Create three sensors (one for each of the next three trains)
    async_add_entities(
        [
            MetraTrainSensor(coordinator, entry, 1),
            MetraTrainSensor(coordinator, entry, 2),
            MetraTrainSensor(coordinator, entry, 3),
        ]
    )


class MetraArrivalsCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Metra data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=30),  # Explicit 30 seconds
        )
        self._username = entry.data[CONF_USERNAME]
        self._password = entry.data[CONF_PASSWORD]
        self._line_id = entry.data[CONF_LINE]  # Line ID from config
        self._start_station = entry.data[CONF_START_STATION]  # Start station
        self._end_station = entry.data[CONF_END_STATION]  # End station
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
                current_time = now(self._tz)

                for item in data:
                    try:
                        trip = item.get("trip_update", {})
                        trip_info = trip.get("trip", {})

                        # Only process selected line
                        if trip_info.get("route_id") != self._line_id:
                            continue

                        start_time = None
                        end_time = None

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

                                if stop_id == self._start_station:
                                    start_time = local_time
                                elif stop_id == self._end_station:
                                    end_time = local_time

                            except Exception as e:
                                _LOGGER.debug("Error processing stop time: %s", e)
                                continue

                        # Only add trains with both times
                        if start_time and end_time:
                            # Calculate time difference from now
                            time_diff = (start_time - current_time).total_seconds()

                            trains.append(
                                {
                                    "start_time": start_time.strftime("%H:%M"),
                                    "end_time": end_time.strftime("%H:%M"),
                                    "start_full": start_time.isoformat(),
                                    "end_full": end_time.isoformat(),
                                    "trip_id": trip_info.get("trip_id", "unknown"),
                                    "time_diff": time_diff,
                                    "date": start_time.date(),
                                }
                            )

                    except Exception as ex:
                        _LOGGER.warning("Error processing trip: %s", ex, exc_info=True)
                        continue

                # Sort by time difference (closest upcoming train first)
                trains.sort(key=lambda x: x["time_diff"])

                # Filter out trains that have already departed (more than 5 minutes ago)
                upcoming_trains = [t for t in trains if t["time_diff"] > -300]

                # If no upcoming trains, show the next one (even if tomorrow)
                if not upcoming_trains and trains:
                    upcoming_trains = [trains[0]]

                _LOGGER.debug(
                    "Processed %d trains on line %s (%d upcoming)",
                    len(trains),
                    self._line_id,
                    len(upcoming_trains),
                )

                return {
                    "trains": upcoming_trains[:3],  # Only keep next 3 trains
                    "count": len(upcoming_trains),
                    "last_update": current_time.isoformat(),
                }

        except Exception as ex:
            _LOGGER.error("Error fetching data: %s", ex, exc_info=True)
            return {"error": str(ex)}


class MetraTrainSensor(SensorEntity):
    """Representation of a Metra Train arrival sensor."""

    _attr_icon = "mdi:train"
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MetraArrivalsCoordinator,
        entry: ConfigEntry,
        train_number: int,
    ) -> None:
        """Initialize the sensor."""
        self._coordinator = coordinator
        self._train_number = train_number
        self._line_id = entry.data[CONF_LINE]
        self._start_station = entry.data[CONF_START_STATION]
        self._end_station = entry.data[CONF_END_STATION]

        self._attr_name = f"Metra {self._line_id} Train {train_number}"
        self._attr_unique_id = (
            f"metra_{self._line_id}_train_{train_number}_{entry.entry_id}"
        )

    @property
    def state(self) -> str:
        """Return the state of the sensor."""
        if not self.available:
            return "Unavailable"

        trains = self._coordinator.data.get("trains", [])
        if len(trains) >= self._train_number:
            train = trains[self._train_number - 1]
            date_str = ""
            if train.get("date") != datetime.now().date():
                date_str = " (Tomorrow)"
            return f"{train['start_time']} → {train['end_time']}{date_str}"
        return "No data"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        if not self.available:
            return {}

        trains = self._coordinator.data.get("trains", [])
        if len(trains) >= self._train_number:
            train = trains[self._train_number - 1]
            return {
                "start_time": train["start_time"],
                "end_time": train["end_time"],
                "start_full": train["start_full"],
                "end_full": train["end_full"],
                "trip_id": train["trip_id"],
                "train_number": self._train_number,
                "last_update": self._coordinator.data.get("last_update"),
            }
        return {}

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return (
            self._coordinator.last_update_success
            and "error" not in self._coordinator.data
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def should_poll(self) -> bool:
        """Return False as updates are handled by coordinator."""
        return False
