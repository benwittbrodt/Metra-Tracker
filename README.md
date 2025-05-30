# Metra Tracker
[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
# Setup
Ensure that you have credentials for the METRA API which can be applied for here:  https://metra.com/developers and should only take a day or so to get.

You'll receive a text file with the username (u:) and passowrd (p:)

## Lines
This integration works throughout the METRA network so pick which line you are wanting to track 

## Start and End stops 
These lists are in alphabetical order 

# Sensors
The integration will create 3 sensors once configured. Each sensor is numbered related to the sequence of arrival into the chosen departure station, 1-3. 

Sensor 1 is always the next train to arrive. Once that trian departs the chosen departure station the arrival time from sensor 2 becomes that of sensor 1 and 3 to 2 etc.

The sensor state will be the time of arrival for the intitial (departure) station and the arrival time at the destination station. 
    
- Formatted as such for each view on a entity card: 22:30 → 22:49

## Sensor Naming 
"\<tracked line name> Train \<train index (1-3)>"

## Sensor Attributes
| Name              | Data Type                                 | Notes |
| ----------------- | ----------------------------------------- | ----- |
| Last update       | DateTime (May 9, 2025 at 9:47:47 PM)      |       |
| Train number      | Integer                                   |       |
| Departure time    | Time (22:30)                              |       |
| Arrival time      | Time (22:30)                              |       |
| Departure station | Text - Full name of station               |       |
| Arrival station   | Text - Full name of station               |       |
| Departure full    | DateTime (May 9, 2025 at 9:47:47 PM)      |       |
| Arrival full      | DateTime (May 9, 2025 at 9:47:47 PM)      |       |
| Trip ID           | Text - Unique trip ID for the given train |       |
