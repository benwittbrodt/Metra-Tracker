# Metra Tracker

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A Home Assistant integration that shows the **next 3 Metra trains** for an *origin → destination* pair, using:

- **GTFS Realtime TripUpdates** (live predictions when available)
- **Static GTFS `schedule.zip`** (schedule fallback based on Metra's notes to assume the train is running on schedule when no live updates are available)

The sensors will display **departure → arrival** and expose an **`is_live`** attribute so you can tell whether the train time is coming from realtime or the schedule.

---

## Features

- **Next 3 trains** (looks ahead for the next 3 scheduled trains based off of your selected origin and destination stop)
- **Express-aware**: only returns trips that stop at **both** your origin and destination (and in the correct order)
- **Live overlay**: uses realtime timestamps when present; falls back to static schedule otherwise
- **Static schedule caching** in `/config/.metra_tracker/` and refreshes when Metra publishes a new schedule

---

## Installation (HACS)
0. **Get an API token** - Needed before you can use this integration

   Apply for access via the Metra Developer Portal:  
    [https://metra.com/developers](https://metra.com/developers)  
   Approval typically takes one business day.

   Once approved, you’ll receive a text file containing your unique `api_token`.

1. In Home Assistant, open **HACS → Integrations → ⋮ → Custom repositories**
2. Add this repository URL and select **Integration**
3. Install **Metra Tracker**
4. Restart Home Assistant

---

## Setup (UI)

1. Go to **Settings → Devices & Services → Add Integration → Metra Tracker**
2. Enter your Metra GTFS API token *(optional if you store it in `secrets.yaml`)*.
3. Pick your **Line**
4. Pick your **Origin** and **Destination** stops (ordered from Chicago outward when possible)

### Using `secrets.yaml` (optional)

Add this to your `secrets.yaml`:

```yaml
metra_tracker_api_token: "YOUR_TOKEN_HERE"
```

If present, the config flow will automatically use it and may skip the token step.

---

## Entities

This integration creates **three sensors** per configured route:

- `... (1)` = next train
- `... (2)` = second next train
- `... (3)` = third next train

### State

The sensor state is a friendly string:

- `HH:MM → HH:MM` (departure from origin → arrival at destination)
- Adds `(Tomorrow)` when the departure date is not today

### Attributes

Each sensor includes:

| Attribute                                             | Type           | Description                                              |
| ----------------------------------------------------- | -------------- | -------------------------------------------------------- |
| `is_live`                                             | bool           | `true` if realtime timestamps were used for this train   |
| `departure_time` / `arrival_time`                     | string         | Display times (`HH:MM`) actually used (live or schedule) |
| `departure_full` / `arrival_full`                     | string         | ISO timestamps actually used                             |
| `scheduled_departure_time` / `scheduled_arrival_time` | string \| null | Baseline scheduled times (`HH:MM`)                       |
| `scheduled_departure_full` / `scheduled_arrival_full` | string \| null | Baseline scheduled ISO timestamps                        |
| `delay_min`                                           | int \| null    | Delay minutes when derivable from realtime feed          |
| `trip_id`                                             | string         | GTFS trip identifier                                     |
| `trip_headsign`                                       | string \| null | Headsign from static schedule (when available)           |
| `direction_id`                                        | int \| null    | GTFS direction_id (when available)                       |
| `service_id`                                          | string \| null | GTFS service_id (when available)                         |
| `route_id`                                            | string         | GTFS route_id                                            |

---

## Data Sources

- **Realtime TripUpdates**  
  `https://gtfspublic.metrarr.com/gtfs/public/tripupdates`

- **Static GTFS schedule.zip** (cached locally)  
  `https://schedules.metrarail.com/gtfs/schedule.zip`  
  *(old URL fallback mirror: `https://gtfspublic.metrarr.com/gtfs/raw/schedule.zip`)*

The integration caches schedule files under:

- `/config/.metra_tracker/schedule.zip`
- `/config/.metra_tracker/schedule_meta.json`

It checks `published.txt` periodically to detect schedule updates.

---

## Notes / Troubleshooting

- If you see “no upcoming trains”, it usually means **there are no trips that stop at both selected stops** in the next lookahead window (default 7 days), or the direction/order is reversed.
- The realtime feed can omit intermediate stops. This is why `schedule.zip` is used to:
  - ensure the trip actually serves your destination
  - compute scheduled arrival times (when realtime doesn’t provide them)

---

## Development

This integration depends on:

- `gtfs-realtime-bindings` (protobuf GTFS Realtime parser)

If you run in a dev container, ensure the integration’s `manifest.json` includes the dependency in `requirements`.

