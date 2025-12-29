"""Config flow for Metra Tracker integration."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.yaml import load_yaml

from .const import (
    DOMAIN,
    CONF_API_TOKEN,
    CONF_LINE,
    CONF_ORIGIN_STATION,
    CONF_DEST_STATION,
)

from .utils import async_get_schedule_zip_path, _read_gtfs_table_sync

_LOGGER = logging.getLogger(__name__)

SECRETS_TOKEN_KEY = "metra_tracker_api_token"


# ----------------------------
# Helpers: secrets + token
# ----------------------------
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

            # If JSON, sanity check it parses; if protobuf, HTTP 200 is enough.
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                data = await response.json()
                return isinstance(data, (dict, list))
            return True

    except Exception as ex:  # noqa: BLE001
        _LOGGER.error("Error validating token: %s", ex)
        return False


# ----------------------------
# Helpers: GTFS cleaning
# ----------------------------
def _clean_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Strip whitespace from keys + values. Prevents GTFS header/key mismatch."""
    out: list[dict[str, str]] = []
    for r in rows:
        cleaned: dict[str, str] = {}
        for k, v in (r or {}).items():
            kk = (k or "").strip()
            if isinstance(v, str):
                vv = v.strip()
            elif v is None:
                vv = ""
            else:
                vv = str(v).strip()
            cleaned[kk] = vv
        out.append(cleaned)
    return out


def _dest_label(stop_id: str, stop_name: str) -> str:
    """Pretty labels for titles."""
    sid = (stop_id or "").strip().upper()
    name = (stop_name or "").strip()

    if sid == "OTC" or name == "Chicago OTC":
        return "OTC"
    if sid == "CUS" or name == "Chicago Union Station":
        return "Union Station"
    return name or sid


# ----------------------------
# Helpers: schedule.zip -> lines
# ----------------------------
def _lines_from_schedule_sync(zip_path: Path) -> list[dict[str, str]]:
    """Return list of routes with id/short/long names from schedule.zip."""
    routes = _clean_rows(_read_gtfs_table_sync(zip_path, "routes.txt"))

    # Minimal filtering: must have route_id and route_short_name
    out: list[dict[str, str]] = []
    for r in routes:
        rid = r.get("route_id", "").strip()
        short = r.get("route_short_name", "").strip()
        long = r.get("route_long_name", "").strip()
        if not rid or not short:
            continue
        out.append(
            {
                "route_id": rid,
                "route_short_name": short,
                "route_long_name": long,
            }
        )

    # Stable ordering: by short name
    out.sort(key=lambda x: x["route_short_name"])
    return out


async def _async_lines_from_schedule(hass: HomeAssistant) -> list[dict[str, str]]:
    session = async_get_clientsession(hass)
    zip_path = await async_get_schedule_zip_path(hass, session)
    return await hass.async_add_executor_job(_lines_from_schedule_sync, zip_path)


# ----------------------------
# Helpers: schedule.zip -> ordered stops for route
# ----------------------------
def _ordered_stops_for_line_sync(
    zip_path: Path, line_key: str
) -> list[tuple[str, str]]:
    """Return [(stop_id, stop_name), ...] ordered from Chicago outward for a line."""
    line_key = (line_key or "").strip()

    stops_rows = _clean_rows(_read_gtfs_table_sync(zip_path, "stops.txt"))
    stop_id_to_name: dict[str, str] = {}
    for r in stops_rows:
        sid = r.get("stop_id", "").strip()
        sname = r.get("stop_name", "").strip()
        if sid and sname:
            stop_id_to_name[sid] = sname

    # NOTE: You said Metra route_id == route_short_name. We still keep a tiny fallback.
    routes_rows = _clean_rows(_read_gtfs_table_sync(zip_path, "routes.txt"))
    route_ids = {
        r.get("route_id", "").strip() for r in routes_rows if r.get("route_id")
    }
    short_to_id = {
        r.get("route_short_name", "").strip(): r.get("route_id", "").strip()
        for r in routes_rows
        if r.get("route_short_name") and r.get("route_id")
    }

    route_id = line_key
    if route_id not in route_ids and route_id in short_to_id:
        route_id = short_to_id[route_id]

    trips_rows = _clean_rows(_read_gtfs_table_sync(zip_path, "trips.txt"))
    trip_ids: set[str] = set()
    for t in trips_rows:
        if t.get("route_id", "").strip() != route_id:
            continue
        tid = t.get("trip_id", "").strip()
        if tid:
            trip_ids.add(tid)

    if not trip_ids:
        _LOGGER.debug(
            "No trips found for route_id=%s (line_key=%s)", route_id, line_key
        )
        return []

    stop_times_rows = _clean_rows(_read_gtfs_table_sync(zip_path, "stop_times.txt"))
    trip_stops: dict[str, list[tuple[int, str]]] = {}
    for st in stop_times_rows:
        tid = st.get("trip_id", "").strip()
        if tid not in trip_ids:
            continue
        sid = st.get("stop_id", "").strip()
        if not sid:
            continue
        try:
            seq = int((st.get("stop_sequence", "0") or "0").strip())
        except Exception:
            seq = 0
        trip_stops.setdefault(tid, []).append((seq, sid))

    if not trip_stops:
        return []

    def looks_like_chicago(stop_id: str) -> bool:
        return (stop_id or "").strip().upper() in {"OTC", "CUS"}

    # Pick a representative trip: prefer Chicago-first, then most stops
    best_tid = None
    best_score = (-1, -1)  # (chicago_first, stop_count)
    for tid, items in trip_stops.items():
        if not items:
            continue
        items_sorted = sorted(items, key=lambda x: x[0])
        first_sid = items_sorted[0][1]
        chicago_first = 1 if looks_like_chicago(first_sid) else 0
        stop_count = len({sid for _, sid in items_sorted})
        score = (chicago_first, stop_count)
        if score > best_score:
            best_score = score
            best_tid = tid

    if not best_tid:
        return []

    ordered = sorted(trip_stops[best_tid], key=lambda x: x[0])

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


# ----------------------------
# Config Flow
# ----------------------------
class MetraArrivalsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Metra Tracker."""

    VERSION = 2
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self) -> None:
        self.api_token: str | None = None
        self.selected_line_id: str | None = None
        self.selected_line_short: str | None = None
        self.selected_line_long: str | None = None
        self._secret_token: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: Get API token (or use secrets.yaml if present)."""
        errors: dict[str, str] = {}

        if self._secret_token is None:
            secrets = await _async_load_secrets(self.hass)
            token = secrets.get(SECRETS_TOKEN_KEY)
            self._secret_token = str(token).strip() if token else None

        if user_input is None and self._secret_token:
            _LOGGER.debug(
                "Metra Tracker: Found token in secrets (%s).", SECRETS_TOKEN_KEY
            )
            if await validate_token(self._secret_token, self.hass):
                self.api_token = self._secret_token
                return await self.async_step_line_select()
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
        """Step 2: Select train line (from schedule.zip routes.txt)."""
        errors: dict[str, str] = {}

        routes = await _async_lines_from_schedule(self.hass)
        if not routes:
            return self.async_abort(reason="no_lines_found")

        # Build labels (e.g. "UP-W — Union Pacific West")
        label_to_route: dict[str, dict[str, str]] = {}
        labels: list[str] = []
        for r in routes:
            short = r["route_short_name"]
            long = r.get("route_long_name") or ""
            label = f"{short} — {long}" if long else short
            labels.append(label)
            label_to_route[label] = r

        if user_input is not None:
            chosen = user_input["line"]
            r = label_to_route[chosen]
            self.selected_line_id = r["route_id"]
            self.selected_line_short = r["route_short_name"]
            self.selected_line_long = r.get("route_long_name") or ""
            return await self.async_step_stop_select()

        return self.async_show_form(
            step_id="line_select",
            data_schema=vol.Schema({vol.Required("line"): vol.In(labels)}),
            errors=errors,
        )

    async def async_step_stop_select(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3: Select origin and destination stops (ordered from Chicago outward)."""
        errors: dict[str, str] = {}

        if not self.selected_line_id:
            return self.async_abort(reason="no_line_selected")

        ordered_stops = await _async_ordered_stops_for_line(
            self.hass, self.selected_line_id
        )
        if not ordered_stops:
            return self.async_abort(reason="no_stops_found_for_line")

        options = [
            selector.SelectOptionDict(value=stop_id, label=stop_name)
            for stop_id, stop_name in ordered_stops
        ]

        origin_selector = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=options, mode=selector.SelectSelectorMode.DROPDOWN
            )
        )
        dest_selector = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=options, mode=selector.SelectSelectorMode.DROPDOWN
            )
        )

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

                origin_name = stop_name_by_id.get(origin_id, origin_id)
                dest_name = stop_name_by_id.get(dest_id, dest_id)

                # Title like "UP-W to OTC"
                line_label = self.selected_line_short or (
                    self.selected_line_id or "Metra"
                )
                title = f"{line_label} to {_dest_label(dest_id, dest_name)}"

                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_API_TOKEN: self.api_token,
                        CONF_LINE: self.selected_line_id,  # keep storing route_id
                        CONF_ORIGIN_STATION: origin_id,
                        CONF_DEST_STATION: dest_id,
                        "origin_station_name": origin_name,
                        "destination_station_name": dest_name,
                        # optional backwards-compat keys (sensor can read either)
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
