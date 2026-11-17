"""Tests for the OnlyCat daily activity stats, against faked gateway replies."""
# ruff: noqa: PLR2004 (expected counts read best as the figures themselves)

from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.onlycat.coordinator import OnlyCatStatsCoordinator
from custom_components.onlycat.data.activity_stats import (
    ActivityStats,
    ActivityStatsError,
    fetch_activity_stats,
)
from custom_components.onlycat.data.device import Device
from custom_components.onlycat.data.pet import Pet
from custom_components.onlycat.sensor_activity import (
    CHIP_SENSORS,
    DEVICE_SENSORS,
    OnlyCatChipActivitySensor,
    OnlyCatDeviceActivitySensor,
    activity_sensors,
)

LONDON = zoneinfo.ZoneInfo("Europe/London")
DEVICE_ID = "OC-00000000001"
LUNA = "981000000000001"
# 23:30 UTC on 17 Nov is still 17 Nov in London; 00:30 UTC on 1 Jul is 01:30 BST.
NOW = datetime(2026, 11, 17, 23, 30, tzinfo=UTC)


def histogram(**hours: int) -> list[int]:
    """Return a 24-hour histogram with the given hours (h0..h23) set."""
    values = [0] * 24
    for hour, count in hours.items():
        values[int(hour[1:])] = count
    return values


def cat(rfid: str, **counts: int) -> dict:
    """Return a groupBy=cat row as the gateway sends it."""
    row = {
        "rfidCode": rfid,
        "transitsIn": 0,
        "transitsOut": 0,
        "peeks": 0,
        "denies": 0,
        "breaches": 0,
        "preyAttempts": 0,
        "hourHistogram": [0] * 24,
        "activeDays": 1,
    }
    row.update(counts)
    return row


def day(day_local: str, **counts: int) -> dict:
    """Return a groupBy=day row as the gateway sends it."""
    row = {
        "dayLocal": day_local,
        "transitsIn": 0,
        "transitsOut": 0,
        "peeks": 0,
        "denies": 0,
        "breaches": 0,
        "preyAttempts": 0,
        "hourHistogram": [0] * 24,
    }
    row.update(counts)
    return row


def device_day(day_local: str, **counts: int) -> dict:
    """Return a getDeviceActivityStats row as the gateway sends it."""
    row = {
        "dayLocal": day_local,
        "deviceId": DEVICE_ID,
        "totalEvents": 0,
        "preyAttempts": 0,
        "humanActivityEvents": 0,
        "remoteUnlocks": 0,
        "manualUnlocks": 0,
        "unidentifiedTransits": 0,
    }
    row.update(counts)
    return row


class FakeGateway:
    """Answers the stats requests like gateway.onlycat.com, recording them."""

    def __init__(
        self,
        week_cats: list[dict],
        today_cats: list[dict],
        days: list[dict],
        device_days: list[dict],
    ) -> None:
        """Hold the rows each request returns."""
        self.week_cats = week_cats
        self.today_cats = today_cats
        self.days = days
        self.device_days = device_days
        self.calls: list[tuple[str, dict]] = []

    async def send_message(self, event: str, data: dict) -> dict:
        """Reply to one request."""
        self.calls.append((event, data))
        envelope = {
            "from": data["from"],
            "to": data["to"],
            "timeZones": {DEVICE_ID: "Europe/London"},
        }
        if event == "getDeviceActivityStats":
            return envelope | {"days": self.device_days}
        if event != "getActivityStats":
            return None
        if data.get("groupBy") == "day":
            return envelope | {"days": self.days}
        if data["from"] == data["to"]:
            return envelope | {"cats": self.today_cats}
        return envelope | {"cats": self.week_cats}


def luna_gateway() -> FakeGateway:
    """Luna's week: rows on some days only, and an unidentified bucket."""
    return FakeGateway(
        week_cats=[
            cat(
                LUNA,
                transitsIn=46,
                transitsOut=47,
                peeks=5,
                denies=1,
                preyAttempts=1,
                hourHistogram=histogram(h8=14, h21=13),
            ),
            cat("none", denies=5, peeks=3),
        ],
        today_cats=[cat(LUNA, transitsIn=3, transitsOut=3, peeks=2)],
        days=[
            day("2026-11-12", transitsIn=11, transitsOut=11, denies=5, preyAttempts=1),
            day(
                "2026-11-17",
                transitsIn=3,
                transitsOut=3,
                peeks=4,
                hourHistogram=histogram(h20=2, h22=2),
            ),
        ],
        device_days=[
            device_day("2026-11-12", totalEvents=29, preyAttempts=1),
            device_day("2026-11-17", totalEvents=8, humanActivityEvents=1),
        ],
    )


@pytest.mark.asyncio
async def test_requests_cover_the_device_local_week() -> None:
    """The requests ask for the last seven device-local days, today included."""
    gateway = luna_gateway()
    await fetch_activity_stats(gateway.send_message, [(DEVICE_ID, LONDON)], now=NOW)
    assert gateway.calls == [
        (
            "getActivityStats",
            {
                "deviceIds": [DEVICE_ID],
                "from": "2026-11-11",
                "to": "2026-11-17",
                "groupBy": "cat",
            },
        ),
        (
            "getActivityStats",
            {
                "deviceIds": [DEVICE_ID],
                "from": "2026-11-17",
                "to": "2026-11-17",
                "groupBy": "cat",
            },
        ),
        (
            "getActivityStats",
            {
                "deviceIds": [DEVICE_ID],
                "from": "2026-11-11",
                "to": "2026-11-17",
                "groupBy": "day",
            },
        ),
        (
            "getDeviceActivityStats",
            {"deviceIds": [DEVICE_ID], "from": "2026-11-11", "to": "2026-11-17"},
        ),
    ]


@pytest.mark.asyncio
async def test_today_is_the_device_local_day() -> None:
    """Just after midnight BST it is already the next day on the flap."""
    gateway = luna_gateway()
    stats = await fetch_activity_stats(
        gateway.send_message,
        [(DEVICE_ID, LONDON)],
        now=datetime(2026, 6, 30, 23, 30, tzinfo=UTC),
    )
    assert stats.day.isoformat() == "2026-07-01"
    assert gateway.calls[1][1]["from"] == "2026-07-01"


@pytest.mark.asyncio
async def test_chip_stats() -> None:
    """A chip gets today's counts, its week's counts and its week's histogram."""
    stats = await fetch_activity_stats(
        luna_gateway().send_message, [(DEVICE_ID, LONDON)], now=NOW
    )
    luna = stats.chip(LUNA)
    assert luna.today == {
        "transitsIn": 3,
        "transitsOut": 3,
        "peeks": 2,
        "denies": 0,
        "preyAttempts": 0,
    }
    assert luna.get_last_7_days("transitsIn") == 46
    assert luna.get_last_7_days("preyAttempts") == 1
    assert luna.hour_histogram_7d == histogram(h8=14, h21=13)
    # The unidentified bucket is not a chip.
    assert "none" not in stats.chips


@pytest.mark.asyncio
async def test_chip_without_rows_counts_zero() -> None:
    """A chip with nothing today, or nothing all week, reads zero."""
    gateway = luna_gateway()
    gateway.today_cats = []
    stats = await fetch_activity_stats(
        gateway.send_message, [(DEVICE_ID, LONDON)], now=NOW
    )
    assert stats.chip(LUNA).get_today("transitsIn") == 0
    assert stats.chip(LUNA).get_last_7_days("transitsIn") == 46
    unknown = stats.chip("900000000000009")
    assert unknown.get_today("peeks") == 0
    assert unknown.get_last_7_days("peeks") == 0
    assert unknown.hour_histogram_7d == [0] * 24


@pytest.mark.asyncio
async def test_device_stats() -> None:
    """A device gets every cat's counts together, and its event-grain totals."""
    stats = await fetch_activity_stats(
        luna_gateway().send_message, [(DEVICE_ID, LONDON)], now=NOW
    )
    flap = stats.device(DEVICE_ID)
    assert flap.get_today("transitsIn") == 3
    assert flap.get_today("peeks") == 4
    assert flap.get_today("denies") == 0
    assert flap.get_today("totalEvents") == 8
    assert flap.get_today("humanActivityEvents") == 1
    assert flap.get_last_7_days("transitsIn") == 14
    assert flap.get_last_7_days("denies") == 5
    assert flap.get_last_7_days("totalEvents") == 37
    # Device prey attempts come from the event-grain totals, not the chip sum.
    assert flap.get_last_7_days("preyAttempts") == 1
    assert flap.hour_histogram_7d == histogram(h20=2, h22=2)


@pytest.mark.asyncio
async def test_device_with_no_rows_today_counts_zero() -> None:
    """No rollup row today means zero, not unknown."""
    gateway = luna_gateway()
    gateway.days = [day("2026-11-12", transitsIn=11)]
    gateway.device_days = []
    stats = await fetch_activity_stats(
        gateway.send_message, [(DEVICE_ID, LONDON)], now=NOW
    )
    flap = stats.device(DEVICE_ID)
    assert flap.get_today("transitsIn") == 0
    assert flap.get_today("totalEvents") == 0
    assert flap.get_last_7_days("transitsIn") == 11


@pytest.mark.asyncio
async def test_error_reply_raises() -> None:
    """A {code, message} reply is an error, not an empty day."""

    async def refuse(_event: str, _data: dict) -> dict:
        return {"code": 400, "message": "deviceIds must be a non-empty array."}

    with pytest.raises(ActivityStatsError, match="deviceIds"):
        await fetch_activity_stats(refuse, [(DEVICE_ID, LONDON)], now=NOW)


@pytest.mark.asyncio
async def test_no_devices_makes_no_requests() -> None:
    """An account without flaps asks for nothing."""
    gateway = luna_gateway()
    stats = await fetch_activity_stats(gateway.send_message, [], now=NOW)
    assert gateway.calls == []
    assert stats.chips == {}


@pytest.mark.asyncio
async def test_coordinator_turns_errors_into_update_failed() -> None:
    """A refused request marks the stats unavailable instead of crashing."""

    async def refuse(_event: str, _data: dict) -> dict:
        return {"code": 500, "message": "boom"}

    coordinator = OnlyCatStatsCoordinator.__new__(OnlyCatStatsCoordinator)
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.runtime_data.client.send_message = refuse
    coordinator.config_entry.runtime_data.devices = [
        Device(device_id=DEVICE_ID, time_zone=LONDON)
    ]
    with pytest.raises(UpdateFailed, match="boom"):
        await coordinator._async_update_data()  # noqa: SLF001


async def sensors_over(stats: ActivityStats | None) -> list:
    """Build the sensors for one flap, Luna and an unnamed chip."""
    coordinator = MagicMock()
    coordinator.data = stats
    coordinator.last_update_success = stats is not None
    device = Device(device_id=DEVICE_ID, description="Back Door", time_zone=LONDON)
    pets = [
        Pet(rfid_code=LUNA, location="home", last_seen=None, label="Luna"),
        Pet(rfid_code="900000000000009", location="home", last_seen=None),
    ]
    return activity_sensors(coordinator, [device], pets)


@pytest.mark.asyncio
async def test_sensor_entities_and_ids() -> None:
    """Every flap gets its totals and every chip its five counts."""
    stats = await fetch_activity_stats(
        luna_gateway().send_message, [(DEVICE_ID, LONDON)], now=NOW
    )
    sensors = await sensors_over(stats)
    device_sensors = [s for s in sensors if isinstance(s, OnlyCatDeviceActivitySensor)]
    chip_sensors = [s for s in sensors if isinstance(s, OnlyCatChipActivitySensor)]
    assert len(device_sensors) == len(DEVICE_SENSORS)
    assert len(chip_sensors) == 2 * len(CHIP_SENSORS)
    ids = {s.entity_id for s in sensors}
    assert f"sensor.{LUNA}_transits_in_today" in ids
    assert f"sensor.{LUNA}_prey_attempts_today" in ids
    assert "sensor.oc_00000000001_transits_in_today" in ids
    assert "sensor.oc_00000000001_events_today" in ids
    assert len({s.unique_id for s in sensors}) == len(sensors)
    # A chip nobody named is a visitor: its sensors start disabled.
    by_id = {s.entity_id: s for s in sensors}
    assert by_id[f"sensor.{LUNA}_peeks_today"].entity_registry_enabled_default
    assert not by_id[
        "sensor.900000000000009_peeks_today"
    ].entity_registry_enabled_default
    assert by_id[f"sensor.{LUNA}_peeks_today"].translation_placeholders == {
        "pet_name": "Luna"
    }


@pytest.mark.asyncio
async def test_sensor_values_and_attributes() -> None:
    """A sensor's state is today's count; its week rides along as attributes."""
    stats = await fetch_activity_stats(
        luna_gateway().send_message, [(DEVICE_ID, LONDON)], now=NOW
    )
    by_id = {s.entity_id: s for s in await sensors_over(stats)}
    luna_in = by_id[f"sensor.{LUNA}_transits_in_today"]
    assert luna_in.native_value == 3
    assert luna_in.extra_state_attributes == {
        "last_7_days": 46,
        "hour_histogram_7d": histogram(h8=14, h21=13),
        "day": "2026-11-17",
    }
    assert (
        by_id[f"sensor.{LUNA}_prey_attempts_today"].extra_state_attributes[
            "last_7_days"
        ]
        == 1
    )
    assert by_id["sensor.900000000000009_denies_today"].native_value == 0
    assert by_id["sensor.oc_00000000001_peeks_today"].native_value == 4
    assert by_id["sensor.oc_00000000001_events_today"].native_value == 8
    # The histogram stays out of the recorder.
    assert "hour_histogram_7d" in luna_in._unrecorded_attributes  # noqa: SLF001


@pytest.mark.asyncio
async def test_sensors_without_data_are_unavailable() -> None:
    """Before the first successful fetch the sensors have no value."""
    by_id = {s.entity_id: s for s in await sensors_over(None)}
    luna_in = by_id[f"sensor.{LUNA}_transits_in_today"]
    assert luna_in.native_value is None
    assert luna_in.extra_state_attributes is None
    assert not luna_in.available


@pytest.mark.asyncio
async def test_chip_requests_batch_twenty_devices() -> None:
    """The gateway takes 20 devices a request; a chip sums over the batches."""
    gateway = luna_gateway()
    devices = [(f"OC-{n:011d}", LONDON) for n in range(21)]
    stats = await fetch_activity_stats(gateway.send_message, devices, now=NOW)
    cat_calls = [data for _, data in gateway.calls if data.get("groupBy") == "cat"]
    assert [len(data["deviceIds"]) for data in cat_calls] == [20, 20, 1, 1]
    assert stats.chip(LUNA).get_last_7_days("transitsIn") == 2 * 46
    assert stats.chip(LUNA).get_today("transitsIn") == 2 * 3


@pytest.mark.asyncio
async def test_each_device_counts_its_own_day() -> None:
    """A flap in another zone reports on its own local day."""
    gateway = luna_gateway()
    tokyo = zoneinfo.ZoneInfo("Asia/Tokyo")
    stats = await fetch_activity_stats(
        gateway.send_message, [(DEVICE_ID, LONDON), ("OC-TOKYO", tokyo)], now=NOW
    )
    assert stats.device(DEVICE_ID).day.isoformat() == "2026-11-17"
    assert stats.device("OC-TOKYO").day.isoformat() == "2026-11-18"
    assert stats.chip(LUNA).day.isoformat() == "2026-11-17"
