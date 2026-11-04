"""Settlement stations for the weather series the bot trades.

Every field here was read from an authoritative API on 2026-11-04, not from memory:
  * `cli_code` comes from Kalshi's rules_primary, e.g. "New York City (CLINYC)".
  * The CLI product for that code (api.weather.gov/products/types/CLI/locations/{loc}) is the
    NWS Daily Climate Report: CLINYC "CENTRAL PARK NY" (office OKX), CLIMIA "MIAMI" (MFL),
    CLIMDW "CHICAGO-MIDWAY" (LOT). Max/min are integer °F, times in LST.
  * Observation stations from api.weather.gov/stations/{id}: KNYC "New York City, Central
    Park", KMIA "Miami, Miami International Airport", KMDW "Chicago, Chicago Midway
    Airport". The coordinates and time zones below are from those responses.

LST = local standard time. NWS climate days run midnight to midnight LST all year (during
DST that is 1:00 AM to 12:59 AM local daylight time), so the UTC offset used for the day
window is the fixed standard offset, never the current clock offset.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Station:
    cli_code: str  # as written in Kalshi rules, e.g. "CLINYC"
    cli_location: str  # NWS CLI product location id, e.g. "NYC"
    wfo: str  # issuing NWS office
    name: str
    obs_station: str  # NWS observation station id
    lat: float
    lon: float
    tz: str  # IANA zone of the station
    lst_offset_hours: int  # standard-time UTC offset (never DST)


STATIONS: dict[str, Station] = {
    "CLINYC": Station(
        "CLINYC", "NYC", "OKX", "Central Park NY", "KNYC",
        40.78333, -73.96667, "America/New_York", -5,
    ),
    "CLIMIA": Station(
        "CLIMIA", "MIA", "MFL", "Miami", "KMIA",
        25.79056, -80.31639, "America/New_York", -5,
    ),
    "CLIMDW": Station(
        "CLIMDW", "MDW", "LOT", "Chicago-Midway", "KMDW",
        41.78417, -87.75528, "America/Chicago", -6,
    ),
    # Added 2026-11-04 (owner asked for up to 5 more liquid cities). Each verified the same
    # way: rules template + TWC source + one station code on every open prod market,
    # rules_secondary identical to NYC's, NWS CLI product header/name, and NWS station
    # metadata (coordinates, time zone) from api.weather.gov/stations/{id}.
    "CLILAX": Station(
        "CLILAX", "LAX", "LOX", "Los Angeles Intl Airport", "KLAX",
        33.93806, -118.38889, "America/Los_Angeles", -8,
    ),
    "CLIATL": Station(
        "CLIATL", "ATL", "FFC", "Atlanta", "KATL",
        33.64028, -84.42694, "America/New_York", -5,
    ),
    "CLIBOS": Station(
        "CLIBOS", "BOS", "BOX", "Boston MA", "KBOS",
        42.36056, -71.01056, "America/New_York", -5,
    ),
    "CLIDFW": Station(
        "CLIDFW", "DFW", "FWD", "Dallas/Fort Worth", "KDFW",
        32.89743, -97.02196, "America/Chicago", -6,
    ),
    "CLIDEN": Station(
        "CLIDEN", "DEN", "BOU", "Denver CO", "KDEN",
        39.84658, -104.65622, "America/Denver", -7,
    ),
}  # fmt: skip

# Which station each series is expected to name (cross-checked against the rules text at
# parse time; a series that suddenly names a different station is refused).
SERIES_STATION = {
    "KXHIGHNY": "CLINYC",
    "KXHIGHMIA": "CLIMIA",
    "KXHIGHCHI": "CLIMDW",
    "KXHIGHLAX": "CLILAX",
    "KXHIGHTATL": "CLIATL",
    "KXHIGHTBOS": "CLIBOS",
    "KXHIGHTDAL": "CLIDFW",
    "KXHIGHDEN": "CLIDEN",
}
