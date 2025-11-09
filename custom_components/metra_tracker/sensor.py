"""Sensor platform for Metra train arrivals (Public GTFS API)."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any
from google.transit import gtfs_realtime_pb2
import io
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
    CONF_API_TOKEN,
    CONF_LINE,
    METRA_LINES,
    DEFAULT_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=DEFAULT_SCAN_INTERVAL)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Metra Tracker sensors from a config entry."""
    coordinator = MetraArrivalsCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await coordinator.async_config_entry_first_refresh()

    async_add_entities(
        [
            MetraTrainSensor(coordinator, entry, 1),
            MetraTrainSensor(coordinator, entry, 2),
            MetraTrainSensor(coordinator, entry, 3),
        ]
    )


class MetraArrivalsCoordinator(DataUpdateCoordinator):
    """Class to manage fetching and parsing Metra GTFS trip updates."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self._api_token = entry.data[CONF_API_TOKEN]
        self._line_id = entry.data[CONF_LINE]
        self._start_station = entry.data["start_station"]
        self._end_station = entry.data["end_station"]
        self._start_station_name = entry.data["start_station_name"]
        self._end_station_name = entry.data["end_station_name"]
        self._tz = get_time_zone("America/Chicago")

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch and process train data from Metra GTFS public feed."""
        try:
            session = async_get_clientsession(self.hass)
            url = f"https://gtfspublic.metrarr.com/gtfs/public/tripupdates?api_token={self._api_token}"

            async with session.get(url, timeout=15) as response:
                if response.status != 200:
                    _LOGGER.error("API request failed: %s", response.status)
                    return {"error": f"HTTP {response.status}"}

                # Parse protobuf (binary)
                raw_bytes = await response.read()
                feed = gtfs_realtime_pb2.FeedMessage()
                feed.ParseFromString(raw_bytes)
                _LOGGER.debug("Decoded %d GTFS entities", len(feed.entity))

                trains = []
                current_time = now(self._tz)

                for entity in feed.entity:
                    if not entity.HasField("trip_update"):
                        continue

                    trip_update = entity.trip_update
                    trip = trip_update.trip
                    if trip.route_id != self._line_id:
                        continue
                    if trip.route_id == self._line_id:
                        stop_ids = [stu.stop_id for stu in trip_update.stop_time_update]
                        _LOGGER.debug("Route %s has stops: %s", trip.route_id, stop_ids)
                    start_time = None
                    end_time = None

                    for stu in trip_update.stop_time_update:
                        stop_id = stu.stop_id
                        if stu.arrival.time == 0:
                            continue

                        arrival_time = datetime.fromtimestamp(
                            stu.arrival.time, tz=self._tz
                        )
                        # Logs to make sure the start and end stop are found for the given line
                        if stop_id in (self._start_station, self._end_station):
                            _LOGGER.debug(
                                "Matched stop %s for route %s (feed stop_id=%s)",
                                stop_id,
                                self._line_id,
                                stop_id,
                            )

                        if stop_id == self._start_station:
                            start_time = arrival_time
                        elif stop_id == self._end_station:
                            end_time = arrival_time

                    if start_time and end_time:
                        time_diff = (start_time - current_time).total_seconds()
                        trains.append(
                            {
                                "start_time": start_time.strftime("%H:%M"),
                                "end_time": end_time.strftime("%H:%M"),
                                "start_full": start_time.isoformat(),
                                "end_full": end_time.isoformat(),
                                "trip_id": trip.trip_id or "unknown",
                                "time_diff": time_diff,
                                "date": start_time.date(),
                            }
                        )

                trains.sort(key=lambda x: x["time_diff"])
                upcoming_trains = [t for t in trains if t["time_diff"] > -300]

                if not upcoming_trains and trains:
                    upcoming_trains = [trains[0]]

                return {
                    "trains": upcoming_trains[:3],
                    "count": len(upcoming_trains),
                    "last_update": current_time.isoformat(),
                    "line_name": METRA_LINES.get(self._line_id, self._line_id),
                    "start_station_name": self._start_station_name,
                    "end_station_name": self._end_station_name,
                }

        except Exception as ex:
            _LOGGER.exception("Error fetching or decoding GTFS data: %s", ex)
            return {"error": str(ex)}


class MetraTrainSensor(SensorEntity):
    """Representation of an individual upcoming Metra train."""

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
        self._entry = entry
        self._attr_unique_id = f"metra_{entry.entry_id}_train_{train_number}"

    @property
    def name(self) -> str:
        """Return the sensor’s display name."""
        line_name = self._coordinator.data.get("line_name", "Metra")
        return f"{line_name} Train {self._train_number}"

    @property
    def state(self) -> str:
        """Return the main state string."""
        if not self.available:
            return "Unavailable"

        trains = self._coordinator.data.get("trains", [])
        if len(trains) >= self._train_number:
            train = trains[self._train_number - 1]
            date_str = (
                " (Tomorrow)" if train.get("date") != datetime.now().date() else ""
            )
            return f"{train['start_time']} → {train['end_time']}{date_str}"
        return "No trains scheduled within 60 min"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return detailed attributes."""
        if not self.available:
            return {}

        base_attrs = {
            "last_update": self._coordinator.data.get("last_update"),
            "train_number": self._train_number,
        }

        trains = self._coordinator.data.get("trains", [])
        if len(trains) >= self._train_number:
            train = trains[self._train_number - 1]
            base_attrs.update(
                {
                    "departure_time": train["start_time"],
                    "arrival_time": train["end_time"],
                    "departure_station": self._coordinator.data["start_station_name"],
                    "arrival_station": self._coordinator.data["end_station_name"],
                    "departure_full": train["start_full"],
                    "arrival_full": train["end_full"],
                    "trip_id": train["trip_id"],
                }
            )

        return base_attrs

    @property
    def available(self) -> bool:
        """Return whether data is valid."""
        return (
            self._coordinator.last_update_success
            and "error" not in self._coordinator.data
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def should_poll(self) -> bool:
        """Return False (we use the coordinator)."""
        return False
