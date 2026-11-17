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
MAX_DEVICE_IDS = 20
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

    day: date | None = None
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
        return self.chips.get(rfid_code) or StatsWindow(day=self.day)

    def device(self, device_id: str) -> StatsWindow:
        """Return a device's stats, all zero for a device without rows."""
        return self.devices.get(device_id) or StatsWindow(day=self.day)


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


def _add(totals: dict[str, int], more: dict[str, int]) -> dict[str, int]:
    return {key: totals.get(key, 0) + more.get(key, 0) for key in totals | more}


def _add_histograms(totals: list[int], more: list[int]) -> list[int]:
    return [a + b for a, b in zip(totals, more, strict=True)]


def _chips(rows: list[dict]) -> Iterable[tuple[str, dict]]:
    """Yield (chip, row) for every identified chip's row."""
    for row in rows:
        rfid_code = row.get("rfidCode")
        if rfid_code and rfid_code != UNIDENTIFIED_RFID_CODE:
            yield rfid_code, row


async def fetch_activity_stats(
    send_message: Callable[[str, dict], Awaitable[Any]],
    devices: Iterable[tuple[str, tzinfo | None]],
    now: datetime | None = None,
) -> ActivityStats:
    """
    Fetch today's and the last seven days' stats for chips and devices.

    ``devices`` are (device id, device time zone) pairs. Chip stats are summed
    over every device; a chip's "today" is the first device's local day.
    """
    devices = list(devices)
    if not devices:
        return ActivityStats()

    async def request(
        event: str,
        key: str,
        device_ids: list[str],
        first: date,
        last: date,
        **extra: str,
    ) -> list[dict]:
        criteria = {
            "deviceIds": device_ids,
            "from": first.isoformat(),
            "to": last.isoformat(),
        }
        return _rows(await send_message(event, criteria | extra), key)

    today = local_today(devices[0][1], now)
    week_start = today - timedelta(days=STATS_DAYS - 1)
    stats = ActivityStats(day=today)
    all_ids = [device_id for device_id, _ in devices]
    # The gateway takes at most MAX_DEVICE_IDS devices per request.
    for start in range(0, len(all_ids), MAX_DEVICE_IDS):
        ids = all_ids[start : start + MAX_DEVICE_IDS]
        week = await request(
            "getActivityStats", "cats", ids, week_start, today, groupBy="cat"
        )
        for rfid_code, row in _chips(week):
            chip = stats.chips.setdefault(rfid_code, StatsWindow(day=today))
            chip.last_7_days = _add(chip.last_7_days, _sum_rows([row], ACTIVITY_COUNTS))
            chip.hour_histogram_7d = _add_histograms(
                chip.hour_histogram_7d, _sum_histograms([row])
            )
        for rfid_code, row in _chips(
            await request("getActivityStats", "cats", ids, today, today, groupBy="cat")
        ):
            chip = stats.chips.setdefault(rfid_code, StatsWindow(day=today))
            chip.today = _add(chip.today, _sum_rows([row], ACTIVITY_COUNTS))

    for device_id, time_zone in devices:
        device_today = local_today(time_zone, now)
        device_start = device_today - timedelta(days=STATS_DAYS - 1)
        ids = [device_id]
        days = await request(
            "getActivityStats", "days", ids, device_start, device_today, groupBy="day"
        )
        totals = await request(
            "getDeviceActivityStats", "days", ids, device_start, device_today
        )

        def on_today(rows: list[dict], day: date = device_today) -> list[dict]:
            return [row for row in rows if row.get("dayLocal") == day.isoformat()]

        # A device's prey attempts are event grain, not a sum over its chips.
        device_counts = (*DEVICE_COUNTS, "preyAttempts")
        stats.devices[device_id] = StatsWindow(
            day=device_today,
            today=_sum_rows(on_today(days), ACTIVITY_COUNTS)
            | _sum_rows(on_today(totals), device_counts),
            last_7_days=_sum_rows(days, ACTIVITY_COUNTS)
            | _sum_rows(totals, device_counts),
            hour_histogram_7d=_sum_histograms(days),
        )
    return stats
