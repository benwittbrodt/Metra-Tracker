"""Config flow for Metra Tracker integration."""

from __future__ import annotations

import logging
from typing import Any
import csv
import io
import zipfile
from pathlib import Path

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
    CONF_ORIGIN_STATION,
    CONF_DEST_STATION,
    METRA_LINES,
    METRA_STOPS_BY_LINE,
)

from .utils import async_get_schedule_zip_path, _read_gtfs_table_sync


_LOGGER = logging.getLogger(__name__)

# Key in secrets.yaml / secrets.yml
SECRETS_TOKEN_KEY = "metra_tracker_api_token"


def _stop_name(line: str, stop_code: str) -> str:
    """Human-readable stop name from the constants mapping."""
    return METRA_STOPS_BY_LINE.get(line, {}).get(stop_code, stop_code)


def _dest_label_for_title(line: str, stop_code: str) -> str:
    """
    Destination label used in the config entry title.
    - OTC should show as 'OTC'
    - CUS / Chicago Union Station should show as 'Union Station'
    - Otherwise, show the human-readable stop name
    """
    name = _stop_name(line, stop_code)

    if stop_code == "OTC" or name == "Chicago OTC":
        return "OTC"

    if stop_code == "CUS" or name == "Chicago Union Station":
        return "Union Station"

    return name


def _build_entry_title(line: str, end_code: str) -> str:
    """Config entry title shown on the Integrations page."""
    return f"{line} to {_dest_label_for_title(line, end_code)}"


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


def _strip(v: str | None) -> str:
    return (v or "").strip()


def _looks_like_chicago(name: str, stop_id: str) -> bool:
    n = name.lower()
    sid = stop_id.upper()
    return (
        "chicago" in n
        or "ogilvie" in n
        or "union station" in n
        or sid in {"OTC", "CUS"}
    )


def _ordered_stops_for_line_sync(
    zip_path: Path, line_key: str
) -> list[tuple[str, str]]:
    """Return [(stop_id, stop_name), ...] ordered from Chicago outward for a line."""
    line_key = (line_key or "").strip()

    # stops: stop_id -> stop_name
    stops_rows = _read_gtfs_table_sync(zip_path, "stops.txt")
    stop_id_to_name: dict[str, str] = {}
    for r in stops_rows:
        sid = (r.get("stop_id") or "").strip()
        sname = (r.get("stop_name") or "").strip()
        if sid and sname:
            stop_id_to_name[sid] = sname

    # routes: resolve route_id if LINE is route_short_name
    routes_rows = _read_gtfs_table_sync(zip_path, "routes.txt")
    route_ids = {
        (r.get("route_id") or "").strip() for r in routes_rows if r.get("route_id")
    }
    short_to_id = {
        (r.get("route_short_name") or "").strip(): (r.get("route_id") or "").strip()
        for r in routes_rows
        if r.get("route_short_name") and r.get("route_id")
    }

    route_id = line_key
    if route_id not in route_ids and route_id in short_to_id:
        route_id = short_to_id[route_id]

    # trips on this route
    trips_rows = _read_gtfs_table_sync(zip_path, "trips.txt")
    trip_ids: set[str] = set()
    for t in trips_rows:
        if (t.get("route_id") or "").strip() != route_id:
            continue
        tid = (t.get("trip_id") or "").strip()
        if tid:
            trip_ids.add(tid)

    if not trip_ids:
        return []

    # stop_times for those trips (we only need stop_sequence + stop_id)
    stop_times_rows = _read_gtfs_table_sync(zip_path, "stop_times.txt")
    trip_stops: dict[str, list[tuple[int, str]]] = {}
    for st in stop_times_rows:
        tid = (st.get("trip_id") or "").strip()
        if tid not in trip_ids:
            continue
        sid = (st.get("stop_id") or "").strip()
        if not sid:
            continue
        try:
            seq = int((st.get("stop_sequence") or "0").strip())
        except Exception:
            seq = 0
        trip_stops.setdefault(tid, []).append((seq, sid))

    if not trip_stops:
        return []

    def looks_like_chicago(name: str, stop_id: str) -> bool:
        sid = (stop_id or "").upper()
        return sid in {"OTC", "CUS"}

    # Pick a representative trip: prefer chicago-first, then most stops
    best_tid = None
    best_score = (-1, -1)  # (chicago_first, stop_count)
    for tid, items in trip_stops.items():
        if not items:
            continue
        items_sorted = sorted(items, key=lambda x: x[0])
        first_sid = items_sorted[0][1]
        first_name = stop_id_to_name.get(first_sid, first_sid)
        chicago_first = 1 if looks_like_chicago(first_name, first_sid) else 0
        stop_count = len({sid for _, sid in items_sorted})
        score = (chicago_first, stop_count)
        if score > best_score:
            best_score = score
            best_tid = tid

    if not best_tid:
        return []

    ordered = sorted(trip_stops[best_tid], key=lambda x: x[0])

    # Unique stop_ids in order
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for _, sid in ordered:
        if sid in seen:
            continue
        seen.add(sid)
        out.append((sid, stop_id_to_name.get(sid, sid)))

    return out


async def _async_ordered_stops_for_line(
    hass: HomeAssistant, line_key: str
) -> list[tuple[str, str]]:
    session = async_get_clientsession(hass)
    zip_path = await async_get_schedule_zip_path(hass, session)
    return await hass.async_add_executor_job(
        _ordered_stops_for_line_sync, zip_path, line_key
    )


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
        """Step 3: Select origin and destination stops for selected line (ordered from Chicago outward)."""
        errors: dict[str, str] = {}

        # Build ordered stop list from schedule.zip
        ordered_stops = await _async_ordered_stops_for_line(
            self.hass, self.selected_line_id
        )

        if not ordered_stops:
            return self.async_abort(reason="no_stops_found_for_line")

        # Build selector options in ORDER, but store VALUE as stop_id (prevents duplicate-name issues)
        options = [
            selector.SelectOptionDict(value=stop_id, label=stop_name)
            for stop_id, stop_name in ordered_stops
        ]

        origin_selector = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=options,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )

        dest_selector = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=options,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )

        # For display name lookups
        stop_name_by_id = {sid: sname for sid, sname in ordered_stops}

        if user_input is not None:
            origin_id = user_input["origin_station"]
            dest_id = user_input["destination_station"]

            if origin_id == dest_id:
                errors["base"] = "same_origin_destination"
            else:
                unique_id = f"{self.selected_line_id}:{origin_id}:{dest_id}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                title = _build_entry_title(self.selected_line_id, dest_id)

                origin_name = stop_name_by_id.get(origin_id, origin_id)
                dest_name = stop_name_by_id.get(dest_id, dest_id)

                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_API_TOKEN: self.api_token,
                        CONF_LINE: self.selected_line_id,
                        CONF_ORIGIN_STATION: origin_id,
                        CONF_DEST_STATION: dest_id,
                        # new name keys
                        "origin_station_name": origin_name,
                        "destination_station_name": dest_name,
                        # (optional) keep old keys for backwards compat in your sensor.py
                        "start_station_name": origin_name,
                        "end_station_name": dest_name,
                    },
                )

        return self.async_show_form(
            step_id="stop_select",
            data_schema=vol.Schema(
                {
                    vol.Required("origin_station"): origin_selector,
                    vol.Required("destination_station"): dest_selector,
                }
            ),
            errors=errors,
        )
