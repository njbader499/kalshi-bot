"""Parse a Kalshi daily-temperature market into an exact integer outcome set.

The parser accepts ONLY the rules template quoted verbatim in DECISIONS.md (pulled from the
API 2026-11-04). Anything else (a new source, a different station, decimal strikes,
unexpected wording) raises RulesError, and the market is not priced. A silent template
change on Kalshi's side must never be mis-parsed.

Owner-approved semantics (2026-11-04):
  * The settlement value is an integer °F.
  * "greater than X"  -> X+1 or above      (outcomes [X+1, +inf))
  * "less than X"     -> X-1 or below      (outcomes (-inf, X-1])
  * "between A-B"     -> A..B inclusive    (outcomes [A, B])
  These are cross-checked against floor_strike / cap_strike and yes_sub_title.
  * The day is the NWS climate day in local standard time: [00:00 LST, 24:00 LST).
  * DST guard: dates on/after 2027-03-14 are refused until the owner rechecks the day-window
    rule (config weather.dst_rule_rechecked). A reminder is logged from 2027-02-28 on.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .stations import SERIES_STATION, STATIONS, Station

DST_RECHECK_DATE = date(2027, 3, 14)  # US DST resumes; recheck the day-window rule before
DST_REMINDER_FROM = date(2027, 2, 28)

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1
)}  # fmt: skip

_HEAD = (
    r"^If the (?P<kind>maximum|minimum) temperature recorded at (?P<place>[^()]+) "
    r"\((?P<code>CLI[A-Z]{3})\) for (?P<mon>[A-Z][a-z]{2}) (?P<day>\d{1,2}), (?P<year>\d{4}), is "
)
_TAIL = r"° fahrenheit according to The Weather Company, then the market resolves to Yes\.$"
RE_GREATER = re.compile(_HEAD + r"greater than (?P<x>-?\d+)" + _TAIL)
RE_LESS = re.compile(_HEAD + r"less than (?P<x>-?\d+)" + _TAIL)
RE_BETWEEN = re.compile(_HEAD + r"between (?P<a>-?\d+)-(?P<b>-?\d+)" + _TAIL)
RE_SUB = re.compile(r"^(?P<a>-?\d+)° (?:or above|or below|to (?P<b>-?\d+)°)$")


class RulesError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WeatherContract:
    ticker: str
    event_ticker: str
    series: str
    station: Station
    kind: str  # "max" | "min"
    day: date  # climate day (LST)
    strike_type: str  # greater | less | between
    lo: int | None  # inclusive lower bound of YES outcomes (None = -inf)
    hi: int | None  # inclusive upper bound of YES outcomes (None = +inf)

    def yes(self, value: int) -> bool:
        return (self.lo is None or value >= self.lo) and (self.hi is None or value <= self.hi)

    @property
    def label(self) -> str:
        if self.lo is None:
            return f"<={self.hi}"
        if self.hi is None:
            return f">={self.lo}"
        return f"{self.lo}-{self.hi}"


def _int_strike(v: Any, name: str) -> int:
    if v is None:
        raise RulesError(f"{name} missing")
    if isinstance(v, bool) or not isinstance(v, int | float | str):
        raise RulesError(f"{name} has unexpected type {type(v).__name__}")
    s = str(v)
    if not re.fullmatch(r"-?\d+(\.0+)?", s):
        raise RulesError(f"{name}={v!r} is not a whole number")
    return int(s.split(".")[0])


def parse_contract(market: Mapping[str, Any]) -> WeatherContract:
    ticker = str(market.get("ticker") or "")
    series = ticker.split("-", 1)[0]
    rules = str(market.get("rules_primary") or "")
    st = market.get("strike_type")
    rx = {"greater": RE_GREATER, "less": RE_LESS, "between": RE_BETWEEN}.get(str(st))
    if rx is None:
        raise RulesError(f"{ticker}: unsupported strike_type {st!r}")
    m = rx.match(rules)
    if m is None:
        raise RulesError(f"{ticker}: rules_primary does not match the verified template")
    code = m["code"]
    station = STATIONS.get(code)
    if station is None:
        raise RulesError(f"{ticker}: unknown station {code}")
    expected = SERIES_STATION.get(series)
    if expected is not None and expected != code:
        raise RulesError(f"{ticker}: series {series} now names {code}, expected {expected}")
    if m["mon"] not in _MONTHS:
        raise RulesError(f"{ticker}: bad month {m['mon']}")
    day = date(int(m["year"]), _MONTHS[m["mon"]], int(m["day"]))
    kind = "max" if m["kind"] == "maximum" else "min"

    lo: int | None
    hi: int | None
    if st == "greater":
        x = int(m["x"])
        if _int_strike(market.get("floor_strike"), "floor_strike") != x:
            raise RulesError(f"{ticker}: floor_strike disagrees with rules text")
        lo, hi = x + 1, None
    elif st == "less":
        x = int(m["x"])
        if _int_strike(market.get("cap_strike"), "cap_strike") != x:
            raise RulesError(f"{ticker}: cap_strike disagrees with rules text")
        lo, hi = None, x - 1
    else:
        a, b = int(m["a"]), int(m["b"])
        if a > b:
            raise RulesError(f"{ticker}: inverted range {a}-{b}")
        if (_int_strike(market.get("floor_strike"), "floor_strike"),
                _int_strike(market.get("cap_strike"), "cap_strike")) != (a, b):  # fmt: skip
            raise RulesError(f"{ticker}: floor/cap strikes disagree with rules text")
        lo, hi = a, b

    sub = str(market.get("yes_sub_title") or "")
    sm = RE_SUB.match(sub)
    if sm is None:
        raise RulesError(f"{ticker}: unexpected yes_sub_title {sub!r}")
    want = {
        "greater": f"{lo}° or above",
        "less": f"{hi}° or below",
        "between": f"{lo}° to {hi}°",
    }[str(st)]
    if sub != want:
        raise RulesError(f"{ticker}: yes_sub_title {sub!r} != implied {want!r}")

    return WeatherContract(
        ticker=ticker,
        event_ticker=str(market.get("event_ticker") or ""),
        series=series,
        station=station,
        kind=kind,
        day=day,
        strike_type=str(st),
        lo=lo,
        hi=hi,
    )


def climate_day_window(day: date, station: Station) -> tuple[datetime, datetime]:
    """[start, end) in UTC of the NWS climate day: midnight to midnight LST, fixed offset."""
    tz = timezone(timedelta(hours=station.lst_offset_hours))
    start = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(UTC)
    return start, start + timedelta(days=1)


def is_dst_day(day: date, station: Station) -> bool:
    noon = datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo(station.tz))
    return bool(noon.dst())


def dst_guard(day: date, station: Station, rechecked: bool) -> None:
    """Refuse DST-season climate days until the owner has rechecked the day-window rule."""
    if not rechecked and (day >= DST_RECHECK_DATE or is_dst_day(day, station)):
        raise RulesError(
            f"{day} is a DST date: recheck the climate-day rule (owner reminder due before "
            f"{DST_RECHECK_DATE}) and set weather.dst_rule_rechecked: true"
        )


def dst_reminder(today: date) -> str | None:
    if DST_REMINDER_FROM <= today < DST_RECHECK_DATE:
        return (
            f"REMINDER: US DST starts {DST_RECHECK_DATE}. Re-verify the weather climate-day "
            "rule (NWS LST day vs Kalshi close_time) before then; see DECISIONS.md."
        )
    return None
