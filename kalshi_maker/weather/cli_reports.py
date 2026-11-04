"""Parse NWS Daily Climate Reports (CLI products).

Format verified on live products 2026-11-04 (CLINYC/CLIMIA/CLIMDW):

    ...THE CENTRAL PARK NY CLIMATE SUMMARY FOR NOVEMBER 3 2026...
    VALID TODAY AS OF 0400 PM LOCAL TIME.          <- only on intraday (preliminary) issues
    ...
    TEMPERATURE (F)
     TODAY            (or YESTERDAY on the next-morning final issue)
      MAXIMUM         68    336 PM  79    2003  58     10       59
      MINIMUM         52    559 AM  28    1875  46      6       50

Values are integer °F. "MM" means missing. An issue with "VALID ... AS OF" is a partial-day
report; it gives a lower bound on the day's max, never the final value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
            "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER",
        ],
        1,
    )
}  # fmt: skip
RE_SUMMARY = re.compile(r"CLIMATE SUMMARY FOR ([A-Z]+) (\d{1,2}) (\d{4})")
RE_PARTIAL = re.compile(r"VALID (?:TODAY )?AS OF")
RE_ITEM = re.compile(r"^\s*(MAXIMUM|MINIMUM)\s+(-?\d+|MM)\b")
RE_CODE = re.compile(r"^(CLI[A-Z]{3})\s*$", re.M)


class CliParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CliReport:
    code: str  # e.g. CLINYC
    day: date
    max_f: int | None
    min_f: int | None
    final: bool  # False for intraday "VALID ... AS OF" issues
    issued: datetime | None = None
    product_id: str = ""


def parse_cli(text: str, issued: datetime | None = None, product_id: str = "") -> CliReport:
    cm = RE_CODE.search(text)
    if cm is None:
        raise CliParseError("no CLIxxx product code")
    sm = RE_SUMMARY.search(text)
    if sm is None or sm.group(1) not in _MONTHS:
        raise CliParseError("no 'CLIMATE SUMMARY FOR <date>' line")
    day = date(int(sm.group(3)), _MONTHS[sm.group(1)], int(sm.group(2)))
    final = RE_PARTIAL.search(text) is None
    # Only read MAXIMUM/MINIMUM from the TEMPERATURE block (the first occurrences).
    temp_at = text.find("TEMPERATURE (F)")
    if temp_at < 0:
        raise CliParseError("no TEMPERATURE (F) block")
    block = text[temp_at:].split("PRECIPITATION", 1)[0]
    vals: dict[str, int | None] = {}
    for line in block.splitlines():
        im = RE_ITEM.match(line)
        if im and im.group(1) not in vals:
            vals[im.group(1)] = None if im.group(2) == "MM" else int(im.group(2))
    if "MAXIMUM" not in vals:
        raise CliParseError("no MAXIMUM line")
    return CliReport(
        code=cm.group(1),
        day=day,
        max_f=vals.get("MAXIMUM"),
        min_f=vals.get("MINIMUM"),
        final=final,
        issued=issued,
        product_id=product_id,
    )
