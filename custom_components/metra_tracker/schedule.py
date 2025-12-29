"""Static GTFS schedule helpers for Metra Tracker.

Provides: async_get_next_scheduled_trips(...)
- Ensures trips stop at BOTH start and end stop_ids
- Ensures end stop_sequence > start stop_sequence (filters reverse direction / wrong ordering)
- Looks ahead across days (default 7) until it finds N (default 3) trips
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

import aiohttp

from .utils import async_get_schedule_zip_path, _LOGGER, _read_gtfs_table_sync


def _t2sec(t: str) -> int:
    """Convert GTFS HH:MM:SS to seconds. Supports HH >= 24."""
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def _date_int(dt: datetime) -> int:
    return int(dt.strftime("%Y%m%d"))


@dataclass
class _StaticIndex:
    zip_path: Path
    mtime: float

    # route_short_name -> route_id
    routes_short_to_id: dict[str, str]

    # trip_id -> trip meta
    trip_route: dict[str, str]
    trip_service: dict[str, str]
    trip_headsign: dict[str, str]
    trip_direction: dict[str, str]

    # route_id -> list of trip_ids (pre-filter)
    route_to_trips: dict[str, list[str]]

    # calendar rows and calendar_dates rows
    calendar: list[dict[str, str]]
    calendar_dates: list[dict[str, str]]

    # stop_id -> { trip_id -> stop_info }
    # stop_info = {seq:int, arrival_time:str, departure_time:str}
    stop_times_by_stop: dict[str, dict[str, dict[str, Any]]]


_STATIC: _StaticIndex | None = None


def _strip_row(row: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in row.items():
        kk = (k or "").strip()
        vv = (
            (v or "").strip()
            if isinstance(v, str)
            else ("" if v is None else str(v).strip())
        )
        out[kk] = vv
    return out


def _load_index(zip_path: Path) -> _StaticIndex:
    # NOTE: uses the sync helper from utils.py; called via executor from async function.
    routes = [_strip_row(r) for r in _read_gtfs_table_sync(zip_path, "routes.txt")]
    trips = [_strip_row(r) for r in _read_gtfs_table_sync(zip_path, "trips.txt")]
    calendar = [_strip_row(r) for r in _read_gtfs_table_sync(zip_path, "calendar.txt")]
    calendar_dates = [
        _strip_row(r) for r in _read_gtfs_table_sync(zip_path, "calendar_dates.txt")
    ]
    stop_times = [
        _strip_row(r) for r in _read_gtfs_table_sync(zip_path, "stop_times.txt")
    ]

    routes_short_to_id: dict[str, str] = {}
    for r in routes:
        rsn = r.get("route_short_name", "")
        rid = r.get("route_id", "")
        if rsn and rid:
            routes_short_to_id[rsn] = rid

    trip_route: dict[str, str] = {}
    trip_service: dict[str, str] = {}
    trip_headsign: dict[str, str] = {}
    trip_direction: dict[str, str] = {}
    route_to_trips: dict[str, list[str]] = {}

    for t in trips:
        tid = t.get("trip_id", "").strip()
        rid = t.get("route_id", "").strip()
        sid = t.get("service_id", "").strip()
        if not tid:
            continue
        trip_route[tid] = rid
        trip_service[tid] = sid
        trip_headsign[tid] = t.get("trip_headsign", "").strip()
        trip_direction[tid] = t.get("direction_id", "").strip()
        if rid:
            route_to_trips.setdefault(rid, []).append(tid)

    # Build stop_times index by stop_id (fast join later)
    stop_times_by_stop: dict[str, dict[str, dict[str, Any]]] = {}
    for st in stop_times:
        stop_id = st.get("stop_id", "").strip()
        trip_id = st.get("trip_id", "").strip()
        if not stop_id or not trip_id:
            continue
        try:
            seq = int(st.get("stop_sequence", "0") or "0")
        except Exception:
            seq = 0

        stop_times_by_stop.setdefault(stop_id, {})[trip_id] = {
            "seq": seq,
            "arrival_time": (st.get("arrival_time") or "").strip(),
            "departure_time": (st.get("departure_time") or "").strip(),
        }

    return _StaticIndex(
        zip_path=zip_path,
        mtime=zip_path.stat().st_mtime,
        routes_short_to_id=routes_short_to_id,
        trip_route=trip_route,
        trip_service=trip_service,
        trip_headsign=trip_headsign,
        trip_direction=trip_direction,
        route_to_trips=route_to_trips,
        calendar=calendar,
        calendar_dates=calendar_dates,
        stop_times_by_stop=stop_times_by_stop,
    )


def _active_service_ids_for_date(static: _StaticIndex, dt_: datetime) -> set[str]:
    day_name = dt_.strftime("%A").lower()
    yyyymmdd = _date_int(dt_)

    active: set[str] = set()
    for row in static.calendar:
        try:
            if row.get(day_name) != "1":
                continue
            if int(row["start_date"]) <= yyyymmdd <= int(row["end_date"]):
                sid = row.get("service_id", "").strip()
                if sid:
                    active.add(sid)
        except Exception:
            continue

    # apply exceptions from calendar_dates
    for ex in static.calendar_dates:
        try:
            if int(ex.get("date", "0")) != yyyymmdd:
                continue
            sid = ex.get("service_id", "").strip()
            ex_type = int(ex.get("exception_type", "0"))
            if not sid:
                continue
            if ex_type == 1:
                active.add(sid)
            elif ex_type == 2:
                active.discard(sid)
        except Exception:
            continue

    return active


async def async_get_static_index(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
) -> _StaticIndex:
    """Load and cache the parsed schedule.zip."""
    global _STATIC  # noqa: PLW0603

    zip_path = await async_get_schedule_zip_path(hass, session)
    mtime = zip_path.stat().st_mtime

    if _STATIC and _STATIC.zip_path == zip_path and _STATIC.mtime == mtime:
        return _STATIC

    _LOGGER.debug("Loading Metra static schedule index from %s", zip_path)
    _STATIC = await hass.async_add_executor_job(_load_index, zip_path)
    return _STATIC


async def async_get_next_scheduled_trips(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    *,
    route_short_or_id: str,
    start_stop_id: str,
    end_stop_id: str,
    now_local: datetime,
    limit: int = 3,
    lookahead_days: int = 7,
) -> list[dict[str, Any]]:
    """Return the next N scheduled trips that stop at start & end (and end is after start)."""
    static = await async_get_static_index(hass, session)

    route_key = (route_short_or_id or "").strip()
    start_stop = (start_stop_id or "").strip()
    end_stop = (end_stop_id or "").strip()

    # resolve route_id if route_short_name was provided
    route_id = route_key
    if route_key in static.routes_short_to_id:
        route_id = static.routes_short_to_id[route_key]

    route_trip_ids = static.route_to_trips.get(route_id, [])

    start_map = static.stop_times_by_stop.get(start_stop, {})
    end_map = static.stop_times_by_stop.get(end_stop, {})

    if not route_trip_ids or not start_map or not end_map:
        return []

    base_day = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    out: list[dict[str, Any]] = []

    for day_offset in range(lookahead_days):
        service_day = base_day + timedelta(days=day_offset)
        active_services = _active_service_ids_for_date(static, service_day)
        if not active_services:
            continue

        for trip_id in route_trip_ids:
            service_id = static.trip_service.get(trip_id, "")
            if service_id not in active_services:
                continue

            o = start_map.get(trip_id)
            d = end_map.get(trip_id)
            if not o or not d:
                continue

            if int(d["seq"]) <= int(o["seq"]):
                continue

            dep_time = (o.get("departure_time") or o.get("arrival_time") or "").strip()
            arr_time = (d.get("arrival_time") or d.get("departure_time") or "").strip()
            if not dep_time:
                continue

            dep_dt = service_day + timedelta(seconds=_t2sec(dep_time))
            if dep_dt < now_local:
                continue

            arr_dt = (
                service_day + timedelta(seconds=_t2sec(arr_time)) if arr_time else None
            )

            out.append(
                {
                    "trip_id": trip_id,
                    "route_id": route_id,
                    "route_key": route_key,
                    "service_id": service_id,
                    "start_stop_id": start_stop,
                    "end_stop_id": end_stop,
                    "scheduled_departure_dt": dep_dt,
                    "scheduled_arrival_dt": arr_dt,
                    "scheduled_departure_time": dep_time,
                    "scheduled_arrival_time": arr_time,
                    "trip_headsign": static.trip_headsign.get(trip_id, ""),
                    "direction_id": static.trip_direction.get(trip_id, ""),
                }
            )

        out.sort(key=lambda x: x["scheduled_departure_dt"])
        if len(out) >= limit:
            return out[:limit]

    out.sort(key=lambda x: x["scheduled_departure_dt"])
    return out[:limit]
