"""Sensor platform for Metra train arrivals (Public GTFS API)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from google.transit import gtfs_realtime_pb2

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.dt import get_time_zone, now as ha_now
from homeassistant.helpers.device_registry import async_get

from .const import (
    DOMAIN,
    CONF_API_TOKEN,
    CONF_LINE,
    METRA_LINES,
    DEFAULT_SCAN_INTERVAL,
    CONF_ORIGIN_STATION,
    CONF_DEST_STATION,
)
from .utils import build_entity_name, async_ensure_schedule_zip, _LOGGER
from .schedule import async_get_next_scheduled_trips


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Metra Tracker sensors from a config entry."""
    coordinator = MetraArrivalsCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await coordinator.async_config_entry_first_refresh()

    device_registry = async_get(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        configuration_url="https://metra.com/metra-gtfs-api",
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="Metra",
        name=entry.title,
    )

    trainsensors = [
        MetraTrainSensor(coordinator, entry, 1, device),
        MetraTrainSensor(coordinator, entry, 2, device),
        MetraTrainSensor(coordinator, entry, 3, device),
    ]
    async_add_entities(trainsensors, update_before_add=True)

    # store by entry_id (not entry object)
    hass.data[DOMAIN][entry.entry_id] = {
        "device": device,
        "sensors": trainsensors,
    }


class MetraArrivalsCoordinator(DataUpdateCoordinator):
    """Fetch realtime updates and merge with static schedule to return next 3 trains."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self._api_token = (entry.data[CONF_API_TOKEN] or "").strip()
        self._line_id = (entry.data[CONF_LINE] or "").strip()

        # These are stop_ids (e.g., OTC / OAKPARK)
        self._start_station = (entry.data["start_station"] or "").strip()
        self._end_station = (entry.data["end_station"] or "").strip()

        # These are display names
        self._start_station_name = entry.data["start_station_name"]
        self._end_station_name = entry.data["end_station_name"]

        self._tz = get_time_zone("America/Chicago")
        self._schedule_status = None  # set by async_ensure_schedule_zip()

    async def _fetch_realtime_by_trip(self, session) -> dict[str, dict[str, Any]]:
        """Return realtime predictions indexed by trip_id for our route."""
        url = f"https://gtfspublic.metrarr.com/gtfs/public/tripupdates?api_token={self._api_token}"
        realtime: dict[str, dict[str, Any]] = {}

        async with session.get(url, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError(f"Realtime request failed HTTP {response.status}")

            raw_bytes = await response.read()
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(raw_bytes)

        for entity in feed.entity:
            if not entity.HasField("trip_update"):
                continue

            tu = entity.trip_update
            trip = tu.trip

            if (trip.route_id or "").strip() != self._line_id:
                continue

            trip_id = (trip.trip_id or "").strip()
            if not trip_id:
                continue

            live_start: datetime | None = None
            live_end: datetime | None = None
            live_start_delay_min: int | None = None

            for stu in tu.stop_time_update:
                stop_id = (stu.stop_id or "").strip()

                # Origin: prefer departure.time, fallback arrival.time
                if stop_id == self._start_station:
                    ts = 0
                    if stu.HasField("departure") and getattr(stu.departure, "time", 0):
                        ts = int(stu.departure.time)
                        if getattr(stu.departure, "delay", None) is not None:
                            live_start_delay_min = int(stu.departure.delay) // 60
                    elif stu.HasField("arrival") and getattr(stu.arrival, "time", 0):
                        ts = int(stu.arrival.time)
                        if getattr(stu.arrival, "delay", None) is not None:
                            live_start_delay_min = int(stu.arrival.delay) // 60
                    if ts:
                        live_start = datetime.fromtimestamp(ts, tz=self._tz)

                # Destination: prefer arrival.time, fallback departure.time
                if stop_id == self._end_station:
                    ts = 0
                    if stu.HasField("arrival") and getattr(stu.arrival, "time", 0):
                        ts = int(stu.arrival.time)
                    elif stu.HasField("departure") and getattr(
                        stu.departure, "time", 0
                    ):
                        ts = int(stu.departure.time)
                    if ts:
                        live_end = datetime.fromtimestamp(ts, tz=self._tz)

            if live_start or live_end:
                realtime[trip_id] = {
                    "live_start_dt": live_start,
                    "live_end_dt": live_end,
                    "delay_min": live_start_delay_min,
                }

        return realtime

    async def _async_update_data(self) -> dict[str, Any]:
        current_time = ha_now(self._tz)

        try:
            session = async_get_clientsession(self.hass)

            # Ensure schedule.zip exists (best effort); schedule.py uses utils to read it.
            try:
                self._schedule_status = await async_ensure_schedule_zip(
                    self.hass, session
                )
            except Exception:  # noqa: BLE001
                self._schedule_status = None
                _LOGGER.debug("Could not ensure schedule.zip cache.", exc_info=True)

            # 1) Scheduled next 3 trips (filters express + includes tomorrow)
            scheduled = await async_get_next_scheduled_trips(
                self.hass,
                session,
                route_short_or_id=self._line_id,
                start_stop_id=self._start_station,
                end_stop_id=self._end_station,
                now_local=current_time,
                limit=3,
                lookahead_days=7,
            )

            # 2) Realtime overlay
            realtime_by_trip = await self._fetch_realtime_by_trip(session)

            trains: list[dict[str, Any]] = []
            for s in scheduled:
                trip_id = s["trip_id"]

                sched_dep_dt: datetime = s["scheduled_departure_dt"]
                sched_arr_dt: datetime | None = s["scheduled_arrival_dt"]

                live = realtime_by_trip.get(trip_id, {})
                live_dep_dt: datetime | None = live.get("live_start_dt")
                live_arr_dt: datetime | None = live.get("live_end_dt")

                dep_dt = live_dep_dt or sched_dep_dt
                arr_dt = live_arr_dt or sched_arr_dt

                is_live = bool(live_dep_dt or live_arr_dt)
                delay_min = live.get("delay_min")
                if delay_min is None and live_dep_dt:
                    delay_min = int((live_dep_dt - sched_dep_dt).total_seconds() // 60)

                trains.append(
                    {
                        # Primary times (used by sensor)
                        "start_time": dep_dt.strftime("%H:%M"),
                        "end_time": arr_dt.strftime("%H:%M") if arr_dt else None,
                        "start_full": dep_dt.isoformat(),
                        "end_full": arr_dt.isoformat() if arr_dt else None,
                        "date": dep_dt.date(),
                        "trip_id": trip_id,
                        # Requested indicator
                        "is_live": is_live,
                        # Keep all relevant extras
                        "scheduled_start_time": sched_dep_dt.strftime("%H:%M"),
                        "scheduled_end_time": (
                            sched_arr_dt.strftime("%H:%M") if sched_arr_dt else None
                        ),
                        "scheduled_start_full": sched_dep_dt.isoformat(),
                        "scheduled_end_full": (
                            sched_arr_dt.isoformat() if sched_arr_dt else None
                        ),
                        "delay_min": delay_min,
                        "trip_headsign": s.get("trip_headsign"),
                        "direction_id": s.get("direction_id"),
                        "service_id": s.get("service_id"),
                        "route_id": s.get("route_id"),
                    }
                )

            # If we somehow got nothing scheduled, fall back to realtime-only (best effort)
            if not trains:
                realtime_trains: list[dict[str, Any]] = []
                for trip_id, live in realtime_by_trip.items():
                    live_start = live.get("live_start_dt")
                    live_end = live.get("live_end_dt")
                    if not (live_start and live_end):
                        continue
                    # keep upcoming-ish
                    if (live_start - current_time).total_seconds() < -300:
                        continue
                    realtime_trains.append(
                        {
                            "start_time": live_start.strftime("%H:%M"),
                            "end_time": live_end.strftime("%H:%M"),
                            "start_full": live_start.isoformat(),
                            "end_full": live_end.isoformat(),
                            "date": live_start.date(),
                            "trip_id": trip_id,
                            "is_live": True,
                            "scheduled_start_time": None,
                            "scheduled_end_time": None,
                            "scheduled_start_full": None,
                            "scheduled_end_full": None,
                            "delay_min": live.get("delay_min"),
                        }
                    )

                realtime_trains.sort(key=lambda x: x["start_full"])
                trains = realtime_trains[:3]

            return {
                "trains": trains,
                "count": len(trains),
                "last_update": current_time.isoformat(),
                "line_name": METRA_LINES.get(self._line_id, self._line_id),
                "start_station_name": self._start_station_name,
                "end_station_name": self._end_station_name,
            }

        except Exception as ex:  # noqa: BLE001
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
        device,
    ) -> None:
        self._coordinator = coordinator
        self._train_number = train_number
        self._entry = entry
        self._attr_unique_id = f"metra_{entry.entry_id}_train_{train_number}"
        self._device = device

        line = entry.data.get(CONF_LINE)
        start_code = entry.data.get(CONF_ORIGIN_STATION)
        end_code = entry.data.get(CONF_DEST_STATION)
        self._attr_name = build_entity_name(line, start_code, end_code, train_number)

    @property
    def device_info(self):
        return {
            "identifiers": self._device.identifiers,
            "name": self._device.name,
            "manufacturer": self._device.manufacturer,
        }

    @property
    def name(self):
        return self._attr_name

    @property
    def state(self) -> str:
        if not self.available:
            return "Unavailable"

        trains = (self._coordinator.data or {}).get("trains", [])
        if len(trains) >= self._train_number:
            train = trains[self._train_number - 1]
            today = ha_now(self._coordinator._tz).date()
            date_str = (
                " (Tomorrow)" if train.get("date") and train["date"] != today else ""
            )
            end_time = train.get("end_time")
            if end_time:
                return f"{train['start_time']} → {end_time}{date_str}"
            return f"{train['start_time']}{date_str}"

        return "No upcoming trains"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.available:
            return {}

        base_attrs: dict[str, Any] = {
            "last_update": (self._coordinator.data or {}).get("last_update"),
            "train_number": self._train_number,
            "departure_station": (self._coordinator.data or {}).get(
                "start_station_name"
            ),
            "arrival_station": (self._coordinator.data or {}).get("end_station_name"),
        }

        trains = (self._coordinator.data or {}).get("trains", [])
        if len(trains) >= self._train_number:
            train = trains[self._train_number - 1]
            base_attrs.update(
                {
                    "departure_time": train.get("start_time"),
                    "arrival_time": train.get("end_time"),
                    "departure_full": train.get("start_full"),
                    "arrival_full": train.get("end_full"),
                    "trip_id": train.get("trip_id"),
                    # requested
                    "is_live": train.get("is_live", False),
                    # schedule fallback info + delay
                    "scheduled_departure_time": train.get("scheduled_start_time"),
                    "scheduled_arrival_time": train.get("scheduled_end_time"),
                    "scheduled_departure_full": train.get("scheduled_start_full"),
                    "scheduled_arrival_full": train.get("scheduled_end_full"),
                    "delay_min": train.get("delay_min"),
                    # extra relevant info
                    "trip_headsign": train.get("trip_headsign"),
                    "direction_id": train.get("direction_id"),
                    "service_id": train.get("service_id"),
                    "route_id": train.get("route_id"),
                }
            )

        return base_attrs

    @property
    def available(self) -> bool:
        return self._coordinator.last_update_success and "error" not in (
            self._coordinator.data or {}
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def should_poll(self) -> bool:
        return False
