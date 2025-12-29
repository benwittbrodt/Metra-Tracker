"""Utility helpers for Metra Tracker.

This module centralizes:
- stop ID <-> display name helpers (keep IDs for matching; names for UI)
- config entry/device/entity naming helpers
- GTFS static schedule download + cache helpers (schedule.zip)

The realtime TripUpdates feed often omits intermediate stops. The static schedule
is used as a fallback reference.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from datetime import datetime, timedelta, timezone
import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Iterable
import csv
import io
import zipfile
import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.util.dt import utcnow

_LOGGER = logging.getLogger(__name__)

# Public static GTFS schedule zip (Metra)
SCHEDULE_ZIP_URLS: tuple[str, ...] = (
    # Commonly accessible static schedule host
    "https://schedules.metrarail.com/gtfs/schedule.zip",
    # Alternative Metra API host path
    "https://gtfspublic.metrarr.com/gtfs/raw/schedule.zip",
)

# Lightweight marker that changes when schedule changes (Metra)
PUBLISHED_TXT_URLS: tuple[str, ...] = (
    "https://gtfspublic.metrarr.com/gtfs/raw/published.txt",
    "https://schedules.metrarail.com/gtfs/published.txt",
)

_CACHE_DIRNAME = ".metra_tracker"
_SCHEDULE_ZIP_NAME = "schedule.zip"
_META_NAME = "schedule_meta.json"

# How often we check published.txt when HA is running
_PUBLISHED_CHECK_INTERVAL = timedelta(hours=24)
# Hard refresh even if published.txt isn't reachable
_MAX_ZIP_AGE = timedelta(days=7)


_STOP_ID_TO_NAME: dict[str, str] = {}
_STOP_CACHE_LOCK = asyncio.Lock()
_STOP_CACHE_READY = False


async def async_init_stop_name_cache(
    hass: HomeAssistant, session: aiohttp.ClientSession
) -> None:
    """Load stops.txt into an in-memory stop_id -> stop_name cache (once)."""
    global _STOP_CACHE_READY, _STOP_ID_TO_NAME

    if _STOP_CACHE_READY:
        return

    async with _STOP_CACHE_LOCK:
        if _STOP_CACHE_READY:
            return

        zip_path = await async_get_schedule_zip_path(hass, session)
        rows = await hass.async_add_executor_job(
            _read_gtfs_table_sync, zip_path, "stops.txt"
        )

        mapping: dict[str, str] = {}
        for r in rows:
            sid = (r.get("stop_id") or "").strip()
            sname = (r.get("stop_name") or "").strip()
            if sid and sname:
                mapping[sid] = sname

        _STOP_ID_TO_NAME = mapping
        _STOP_CACHE_READY = True


def stop_name(line: str, stop_code: str) -> str:
    """Return human readable name for a stop_id (from schedule.zip cache)."""
    # line is kept for API compatibility; stops are not line-specific in GTFS
    code = (stop_code or "").strip()
    return _STOP_ID_TO_NAME.get(code, code)


def stop_display(line: str, stop_code: str) -> str:
    """Return a UI label for a stop while controlling for OTC and Union Station names."""
    name = stop_name(line, stop_code)

    if stop_code == "OTC" or name == "Chicago OTC":
        return "OTC"

    if stop_code == "CUS" or name == "Chicago Union Station":
        return "Union Station"

    return name


def build_entity_name(
    line: str, start_code: str, end_code: str, train_number: int
) -> str:
    """Entity (sensor) name shown in HA."""
    start = stop_display(line, start_code)
    end = stop_display(line, end_code)
    return f"{line} {start} → {end} ({train_number})"


def device_destination_label(line: str, stop_code: str) -> str:
    """Destination label used for device/config titles."""
    return stop_display(line, stop_code)


def build_device_name(line: str, end_code: str) -> str:
    """Device name (groups entities)."""
    return f"{line} to {device_destination_label(line, end_code)}"


def _cache_dir(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(_CACHE_DIRNAME))


def _zip_path(hass: HomeAssistant) -> Path:
    return _cache_dir(hass) / _SCHEDULE_ZIP_NAME


def _meta_path(hass: HomeAssistant) -> Path:
    return _cache_dir(hass) / _META_NAME


def _load_meta_sync(meta_path: Path) -> dict[str, Any]:
    try:
        if meta_path.exists():
            return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Failed reading schedule meta", exc_info=True)
    return {}


def _save_meta_sync(cache_dir: Path, meta_path: Path, meta: dict[str, Any]) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(
            json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Failed writing schedule meta", exc_info=True)


async def _async_load_meta(hass: HomeAssistant) -> dict[str, Any]:
    """Async wrapper to load meta without blocking event loop."""
    return await hass.async_add_executor_job(_load_meta_sync, _meta_path(hass))


async def _async_save_meta(hass: HomeAssistant, meta: dict[str, Any]) -> None:
    """Async wrapper to save meta without blocking event loop."""
    await hass.async_add_executor_job(
        _save_meta_sync,
        _cache_dir(hass),
        _meta_path(hass),
        meta,
    )


async def _fetch_text_first(
    session: aiohttp.ClientSession, urls: Iterable[str], timeout_s: int = 10
) -> str | None:
    for url in urls:
        try:
            async with session.get(url, timeout=timeout_s) as resp:
                if resp.status != 200:
                    continue
                return (await resp.text()).strip()
        except Exception:  # noqa: BLE001
            continue
    return None


async def _download_bytes_first(
    session: aiohttp.ClientSession, urls: Iterable[str], timeout_s: int = 30
) -> bytes | None:
    for url in urls:
        try:
            async with session.get(url, timeout=timeout_s) as resp:
                if resp.status != 200:
                    _LOGGER.debug(
                        "Schedule download failed %s: HTTP %s", url, resp.status
                    )
                    continue
                return await resp.read()
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("Schedule download failed %s: %s", url, e)
            continue
    return None


@dataclass(frozen=True)
class ScheduleCacheStatus:
    zip_path: Path
    published: str | None
    last_checked_utc: datetime | None
    last_downloaded_utc: datetime | None


async def async_ensure_schedule_zip(
    hass: HomeAssistant, session: aiohttp.ClientSession
) -> ScheduleCacheStatus:
    """Ensure schedule.zip exists locally and is reasonably fresh.

    This downloads the static schedule.zip and caches it on disk.
    It also checks published.txt (when reachable) to avoid unnecessary downloads.

    Returns status info useful for debugging.
    """
    meta = await _async_load_meta(hass)
    zp = _zip_path(hass)

    now_utc = utcnow()
    last_checked = _parse_dt(meta.get("last_checked_utc"))
    last_downloaded = _parse_dt(meta.get("last_downloaded_utc"))
    cached_published = meta.get("published")

    # If zip missing, we must download
    needs_download = not zp.exists()

    # If we haven't checked published.txt recently, check it
    published = cached_published
    if not needs_download and (
        last_checked is None or now_utc - last_checked > _PUBLISHED_CHECK_INTERVAL
    ):
        published = await _fetch_text_first(session, PUBLISHED_TXT_URLS)
        meta["last_checked_utc"] = now_utc.isoformat()
        if published:
            meta["published"] = published
        await _async_save_meta(hass, meta)

        # If published marker changed, re-download
        if published and cached_published and published != cached_published:
            _LOGGER.info(
                "Metra schedule published marker changed (%s -> %s); refreshing schedule.zip",
                cached_published,
                published,
            )
            needs_download = True

    # If published couldn't be fetched, do a max-age check
    if (
        not needs_download
        and last_downloaded is not None
        and now_utc - last_downloaded > _MAX_ZIP_AGE
    ):
        _LOGGER.info("Metra schedule.zip older than %s; refreshing", _MAX_ZIP_AGE)
        needs_download = True

    if needs_download:
        data = await _download_bytes_first(session, SCHEDULE_ZIP_URLS)
        if data is None:
            # Keep old cache if present
            if zp.exists():
                _LOGGER.warning(
                    "Could not refresh Metra schedule.zip; using existing cached file: %s",
                    zp,
                )
            else:
                raise RuntimeError(
                    "Unable to download Metra schedule.zip from known URLs"
                )

        else:
            d = _cache_dir(hass)
            d.mkdir(parents=True, exist_ok=True)
            await hass.async_add_executor_job(zp.write_bytes, data)

            # Update meta
            meta["last_downloaded_utc"] = now_utc.isoformat()
            meta["last_checked_utc"] = now_utc.isoformat()
            # Refresh published marker after download (best effort)
            new_published = await _fetch_text_first(session, PUBLISHED_TXT_URLS)
            if new_published:
                meta["published"] = new_published
                published = new_published
            await _async_save_meta(hass, meta)

            _LOGGER.info(
                "Downloaded Metra schedule.zip (%s bytes) to %s", len(data), zp
            )

    return ScheduleCacheStatus(
        zip_path=zp,
        published=published,
        last_checked_utc=_parse_dt(meta.get("last_checked_utc")),
        last_downloaded_utc=_parse_dt(meta.get("last_downloaded_utc")),
    )


def _parse_dt(v: Any) -> datetime | None:
    if not v or not isinstance(v, str):
        return None
    try:
        # HA uses ISO strings; treat as UTC
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return None


# -------------------------
# Static GTFS table helpers
# -------------------------

# These helpers are intentionally simple and meant for development / iteration.
# For production, you'll likely want indexes and more memory-efficient parsing.


def _read_gtfs_table_sync(zip_path: Path, table_name: str) -> list[dict[str, str]]:
    """Read a GTFS .txt table from schedule.zip into memory (sync).

    Normalizes column names + values by stripping whitespace (GTFS often has BOM/spacey headers).
    """
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(table_name) as raw:
            wrapper = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            reader = csv.DictReader(wrapper)

            rows: list[dict[str, str]] = []
            for r in reader:
                cleaned: dict[str, str] = {}
                for k, v in r.items():
                    kk = (k or "").strip()
                    if isinstance(v, str):
                        vv = v.strip()
                    elif v is None:
                        vv = ""
                    else:
                        vv = str(v).strip()
                    cleaned[kk] = vv
                rows.append(cleaned)

            return rows


async def async_get_schedule_zip_path(
    hass: HomeAssistant, session: aiohttp.ClientSession
) -> Path:
    """Return the on-disk path to the cached schedule.zip (ensuring it exists)."""
    status = await async_ensure_schedule_zip(hass, session)
    return status.zip_path


@dataclass(frozen=True)
class RouteContext:
    """All metadata needed for a configured route (entry)."""

    route_id: str
    route_short_name: str
    route_long_name: str
    origin_stop_id: str
    destination_stop_id: str
    stop_id_to_name: Mapping[str, str]

    @property
    def route_label(self) -> str:
        # Prefer short name (e.g., UP-W), else long, else route_id
        return self.route_short_name or self.route_long_name or self.route_id

    def stop_name(self, stop_id: str) -> str:
        sid = (stop_id or "").strip()
        return self.stop_id_to_name.get(sid, sid)

    def stop_display(self, stop_id: str) -> str:
        """UI-friendly stop label (your special cases)."""
        sid = (stop_id or "").strip().upper()
        name = self.stop_name(stop_id)

        if sid == "OTC" or name == "Chicago OTC":
            return "OTC"
        if sid == "CUS" or name == "Chicago Union Station":
            return "Union Station"
        return name

    def entity_name(self, train_number: int) -> str:
        """Entity name shown in HA."""
        start = self.stop_display(self.origin_stop_id)
        end = self.stop_display(self.destination_stop_id)
        return f"{self.route_label} {start} → {end} ({train_number})"

    def destination_label(self) -> str:
        return self.stop_display(self.destination_stop_id)


def _build_route_context_sync(
    zip_path: Path, route_id: str, origin_stop_id: str, destination_stop_id: str
) -> RouteContext:
    """Build RouteContext from schedule.zip (sync; run in executor)."""
    route_id = (route_id or "").strip()
    origin_stop_id = (origin_stop_id or "").strip()
    destination_stop_id = (destination_stop_id or "").strip()

    # stops
    stops_rows = _read_gtfs_table_sync(zip_path, "stops.txt")
    stop_id_to_name: dict[str, str] = {}
    for r in stops_rows:
        sid = (r.get("stop_id") or "").strip()
        sname = (r.get("stop_name") or "").strip()
        if sid and sname:
            stop_id_to_name[sid] = sname

    # routes
    routes_rows = _read_gtfs_table_sync(zip_path, "routes.txt")
    short = ""
    long = ""
    for r in routes_rows:
        rid = (r.get("route_id") or "").strip()
        if rid == route_id:
            short = (r.get("route_short_name") or "").strip()
            long = (r.get("route_long_name") or "").strip()
            break

    return RouteContext(
        route_id=route_id,
        route_short_name=short,
        route_long_name=long,
        origin_stop_id=origin_stop_id,
        destination_stop_id=destination_stop_id,
        stop_id_to_name=stop_id_to_name,
    )


async def async_build_route_context(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    route_id: str,
    origin_stop_id: str,
    destination_stop_id: str,
) -> RouteContext:
    """Ensure schedule.zip exists, then build a RouteContext."""
    zip_path = await async_get_schedule_zip_path(hass, session)
    return await hass.async_add_executor_job(
        _build_route_context_sync,
        zip_path,
        route_id,
        origin_stop_id,
        destination_stop_id,
    )
