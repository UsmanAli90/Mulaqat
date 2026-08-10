"""Wall-clock times as minutes from midnight.

Availability is stored as INTEGER minutes since midnight, host-local, rather
than as `TIME`. The reason is arithmetic: Phase 3 does constant maths on these
values — add the slot granularity, add and subtract service buffers, subtract
the minimum notice, compare boundaries — and `datetime.time` supports none of
it. Every one of those operations would become combine-to-datetime, add
timedelta, extract time again. As integers, slot generation is
`range(start, end, granularity)` and a boundary check is a plain `<=`.

It also sidesteps a trap: Postgres accepts `'24:00:00'::time` but Python's
`datetime.time` cannot represent it, so such a row is unreadable through
asyncpg. Minute 1440 is just an integer.

    Conversion reference
    ────────────────────
        0  →  00:00   (midnight, start of day)
      540  →  09:00
      720  →  12:00
     1200  →  20:00
     1320  →  22:00
     1439  →  23:59
     1440  →  24:00   (midnight, END of day — only valid as an end bound)

Minute 1440 is the exclusive end of the day. It is a legitimate `end_minute`
and never a legitimate `start_minute`; the database CHECK constraints allow
0..1440 on both, with `end > start` making 1440-as-start impossible anyway.
"""

import re

MINUTES_PER_HOUR = 60
MINUTES_PER_DAY = 1440

_HHMM = re.compile(r"^(\d{1,2}):([0-5]\d)$")


def hhmm_to_minutes(value: str) -> int:
    """Parse "22:00" into 1320.

    Accepts "24:00" (= 1440) as the end-of-day bound. Rejects anything else
    out of range, so a typo like "25:00" fails at the API boundary rather than
    becoming a nonsensical row.
    """
    match = _HHMM.match(value.strip())
    if match is None:
        raise ValueError(f"expected a wall-clock time like '22:00', got {value!r}")

    hours, minutes = int(match.group(1)), int(match.group(2))
    total = hours * MINUTES_PER_HOUR + minutes

    if total > MINUTES_PER_DAY:
        raise ValueError(f"{value!r} is past the end of the day (maximum is '24:00')")
    if hours == 24 and minutes != 0:
        raise ValueError(f"{value!r} is past the end of the day (maximum is '24:00')")
    return total


def minutes_to_hhmm(minutes: int) -> str:
    """Render 1320 as "22:00", and 1440 as "24:00"."""
    if not 0 <= minutes <= MINUTES_PER_DAY:
        raise ValueError(f"minutes must be between 0 and {MINUTES_PER_DAY}, got {minutes}")
    hours, remainder = divmod(minutes, MINUTES_PER_HOUR)
    return f"{hours:02d}:{remainder:02d}"
