# Metra Tracker

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A Home Assistant integration to display **live Metra train arrival times** using the official Metra GTFS Realtime feed.

---

## Setup

1. **Get an API token**  
   Apply for access via the Metra Developer Portal:  
    [https://metra.com/developers](https://metra.com/developers)  
   Approval typically takes one business day.

   Once approved, you’ll receive a text file containing your unique `api_token`. It will be everything after the pipe "|"

2. **Install via HACS (Custom Repository)**  
   Add your integration as a custom repository under HACS → Integrations → “+ Explore & Download Repositories”.

3. **Configuration** via the UI
   - Go to **Settings → Devices & Services → + Add Integration → Metra Tracker**  
   - Enter your Metra API key, select a line, and choose your start and end stations (they are listed alphabetically).  

---

## Lines

Metra Tracker supports **all lines in the Metra network**, using live GTFS feeds.  
Each line corresponds to its standard Metra route code (e.g. `UP-N`, `BNSF`, `RI`, etc.).

---

## Start and End Stops

When adding an integration instance:
- Select a **Line** (e.g. UP-W)
- Then choose your **Start** (departure) and **End** (arrival) stations  
  The lists are shown alphabetically

---

## Sensors

Each configured Metra Tracker instance creates **three sensors**, representing the next three arriving trains at your chosen departure station.

| Sensor  | Description                   |
| ------- | ----------------------------- |
| Train 1 | The **next** train to arrive  |
| Train 2 | The **second** upcoming train |
| Train 3 | The **third** upcoming train  |

As trains depart, sensors shift automatically (e.g. Train 2 → Train 1).

**Note** - the Metra API doesn't like to show trains unless they are within 1 hour of arrival. 

---

### Sensor State Format

Each sensor’s **state** shows departure → arrival times:

```
22:30 → 22:49
```

If arrival time is unavailable yet, only the departure time will be shown:
```
Departs 22:30
```

---

### Sensor Attributes

| Attribute           | Type        | Description                           |
| ------------------- | ----------- | ------------------------------------- |
| `last_update`       | DateTime    | Last successful data fetch            |
| `train_number`      | Integer     | Sensor sequence (1–3)                 |
| `departure_time`    | Time        | Departure from start station          |
| `arrival_time`      | Time / None | Arrival at end station (if known)     |
| `departure_station` | Text        | Full name of departure station        |
| `arrival_station`   | Text        | Full name of arrival station          |
| `departure_full`    | DateTime    | Full ISO departure timestamp          |
| `arrival_full`      | DateTime    | Full ISO arrival timestamp            |
| `trip_id`           | Text        | Unique trip identifier from GTFS feed |

---

## Notes

- Data is sourced from Metra’s **official GTFS Realtime** and **Static GTFS** feeds:
  - Live trip updates: `https://gtfspublic.metrarr.com/gtfs/public/tripupdates?api_token=...`
  - Static stops and routes: `https://gtfspublic.metrarr.com/gtfs/public/metra_gtfs.zip`
- Updates every **30 seconds** by default. This is an attribute of the Metra API so the integration also updates every 30 seconds.
- Handles partial trips gracefully (shows departure even if arrival not yet in feed).
- Works with all active Metra lines and stops as of latest GTFS data.
