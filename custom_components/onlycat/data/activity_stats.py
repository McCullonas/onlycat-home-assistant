"""
Daily activity stats from the OnlyCat gateway.

The gateway keeps daily rollups per chip and per device, on device-local
calendar days (see ActivityStats.ts in OnlyCatAI/onlycat-shared-models).
Two requests return them:

- ``getActivityStats`` with ``deviceIds``, ``from`` and ``to`` ('YYYY-MM-DD',
  inclusive) and ``groupBy`` ('day', 'cat' or 'month'): transits, peeks,
  denies, breaches, prey attempts and a 24-entry hour histogram of transits.
- ``getDeviceActivityStats`` with ``deviceIds``, ``from`` and ``to``: event-grain
  device totals per day.

Days with no activity have no row, so a missing day or chip counts as zero.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable
    from datetime import tzinfo

_LOGGER = logging.getLogger(__name__)

STATS_DAYS = 7
HOURS = 24
UNIDENTIFIED_RFID_CODE = "none"

ACTIVITY_COUNTS = ("transitsIn", "transitsOut", "peeks", "denies", "preyAttempts")
DEVICE_COUNTS = (
    "totalEvents",
    "humanActivityEvents",
    "remoteUnlocks",
    "manualUnlocks",
    "unidentifiedTransits",
)


class ActivityStatsError(Exception):
    """The gateway refused or failed an activity stats request."""


@dataclass
class StatsWindow:
    """Counts for today and summed over the last seven days, today included."""

    today: dict[str, int] = field(default_factory=dict)
    last_7_days: dict[str, int] = field(default_factory=dict)
    hour_histogram_7d: list[int] = field(default_factory=lambda: [0] * HOURS)

    def get_today(self, key: str) -> int:
        """Return today's count, zero when there was no rollup row."""
        return self.today.get(key, 0)

    def get_last_7_days(self, key: str) -> int:
        """Return the seven-day count, zero when there was no rollup row."""
        return self.last_7_days.get(key, 0)


@dataclass
class ActivityStats:
    """Activity stats for every chip and every device of one account."""

    day: date | None = None
    chips: dict[str, StatsWindow] = field(default_factory=dict)
    devices: dict[str, StatsWindow] = field(default_factory=dict)

    def chip(self, rfid_code: str) -> StatsWindow:
        """Return a chip's stats, all zero for a chip without rows."""
        return self.chips.get(rfid_code) or StatsWindow()

    def device(self, device_id: str) -> StatsWindow:
        """Return a device's stats, all zero for a device without rows."""
        return self.devices.get(device_id) or StatsWindow()


def _count(row: dict, key: str) -> int:
    value = row.get(key)
    return value if isinstance(value, int) else 0


def _sum_rows(rows: Iterable[dict], keys: Iterable[str]) -> dict[str, int]:
    keys = tuple(keys)
    totals = dict.fromkeys(keys, 0)
    for row in rows:
        for key in keys:
            totals[key] += _count(row, key)
    return totals


def _sum_histograms(rows: Iterable[dict]) -> list[int]:
    totals = [0] * HOURS
    for row in rows:
        histogram = row.get("hourHistogram") or []
        for hour, value in enumerate(histogram[:HOURS]):
            if isinstance(value, int):
                totals[hour] += value
    return totals


def _rows(reply: Any, key: str) -> list[dict]:
    """Return the rows of a gateway reply, raising on an error reply."""
    if not isinstance(reply, dict):
        msg = f"Unexpected activity stats reply: {reply!r}"
        raise ActivityStatsError(msg)
    if key not in reply:
        msg = f"Activity stats request failed: {reply.get('message', reply)!r}"
        raise ActivityStatsError(msg)
    return [row for row in reply[key] or [] if isinstance(row, dict)]


def local_today(time_zone: tzinfo | None, now: datetime | None = None) -> date:
    """Return today's date on the device's own clock."""
    now = now or datetime.now(time_zone)
    return now.astimezone(time_zone).date() if time_zone else now.date()


async def fetch_activity_stats(
    send_message: Callable[[str, dict], Awaitable[Any]],
    devices: Iterable[tuple[str, tzinfo | None]],
    now: datetime | None = None,
) -> ActivityStats:
    """
    Fetch today's and the last seven days' stats for chips and devices.

    ``devices`` are (device id, device time zone) pairs. Chip stats are summed
    over every device; "today" is the first device's local day.
    """
    devices = list(devices)
    if not devices:
        return ActivityStats()
    today = local_today(devices[0][1], now)
    first = today - timedelta(days=STATS_DAYS - 1)
    stats = ActivityStats(day=today)

    def window(from_day: date, to_day: date, device_ids: list[str]) -> dict:
        return {
            "deviceIds": device_ids,
            "from": from_day.isoformat(),
            "to": to_day.isoformat(),
        }

    all_ids = [device_id for device_id, _ in devices]
    week_cats = _rows(
        await send_message(
            "getActivityStats", window(first, today, all_ids) | {"groupBy": "cat"}
        ),
        "cats",
    )
    today_cats = _rows(
        await send_message(
            "getActivityStats", window(today, today, all_ids) | {"groupBy": "cat"}
        ),
        "cats",
    )
    for row in week_cats:
        rfid_code = row.get("rfidCode")
        if not rfid_code or rfid_code == UNIDENTIFIED_RFID_CODE:
            continue
        chip = stats.chips.setdefault(rfid_code, StatsWindow())
        chip.last_7_days = _sum_rows([row], ACTIVITY_COUNTS)
        chip.hour_histogram_7d = _sum_histograms([row])
    for row in today_cats:
        rfid_code = row.get("rfidCode")
        if not rfid_code or rfid_code == UNIDENTIFIED_RFID_CODE:
            continue
        stats.chips.setdefault(rfid_code, StatsWindow()).today = _sum_rows(
            [row], ACTIVITY_COUNTS
        )

    for device_id, time_zone in devices:
        device_today = local_today(time_zone, now)
        device_first = device_today - timedelta(days=STATS_DAYS - 1)
        days = _rows(
            await send_message(
                "getActivityStats",
                window(device_first, device_today, [device_id]) | {"groupBy": "day"},
            ),
            "days",
        )
        totals = _rows(
            await send_message(
                "getDeviceActivityStats",
                window(device_first, device_today, [device_id]),
            ),
            "days",
        )
        today_str = device_today.isoformat()
        device = StatsWindow(hour_histogram_7d=_sum_histograms(days))
        device.today = _sum_rows(
            (row for row in days if row.get("dayLocal") == today_str), ACTIVITY_COUNTS
        )
        device.last_7_days = _sum_rows(days, ACTIVITY_COUNTS)
        # Prey attempts are event grain for a device, not a sum over chips.
        today_totals = [row for row in totals if row.get("dayLocal") == today_str]
        device.today |= _sum_rows(today_totals, (*DEVICE_COUNTS, "preyAttempts"))
        device.last_7_days |= _sum_rows(totals, (*DEVICE_COUNTS, "preyAttempts"))
        stats.devices[device_id] = device
    return stats
