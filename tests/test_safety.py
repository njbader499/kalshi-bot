"""Laptop safety: key ACL policy, continuity detection, recovery gate, expiry refresh, power."""

from __future__ import annotations

import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

from kalshi_maker.continuity import (
    Cause,
    ContinuityMonitor,
    Discontinuity,
    GateState,
    RecoverySteps,
    SafetyGate,
    describe_clock_offset,
)
from kalshi_maker.expiry import ExpiryKeeper, LiveOrder
from kalshi_maker.keyperms import AceInfo, KeyPermissionError, evaluate_windows_acl
from kalshi_maker.power import keep_awake

ME = "S-1-5-21-1-2-3-1001"
P = Path("C:/k.pem")


# ---------- Windows ACL policy (pure, runs on every OS) ----------


def test_acl_owner_only_read_ok() -> None:
    evaluate_windows_acl([AceInfo(0, 0, 0x120089, ME)], ME, P, protected=True)


@pytest.mark.parametrize(
    "aces,protected,msg",
    [
        (None, True, "NULL DACL"),
        ([AceInfo(0, 0, 0x120089, ME)], False, "inherits"),
        ([AceInfo(0, 0x10, 0x120089, ME)], True, "inherited entry"),
        ([AceInfo(0, 0, 0x120089, ME), AceInfo(0, 0, 0x1F01FF, "S-1-5-18")], True, "another"),
        ([AceInfo(0, 0, 0x120089, "S-1-1-0")], True, "another"),  # Everyone
        ([AceInfo(5, 0, 0, None)], True, "unsupported"),
        ([], True, "does not grant"),
    ],
)
def test_acl_refusals(aces, protected, msg) -> None:
    with pytest.raises(KeyPermissionError, match=msg) as ei:
        evaluate_windows_acl(aces, ME, P, protected)
    assert "icacls" in str(ei.value)


def test_acl_deny_entries_are_fine() -> None:
    evaluate_windows_acl(
        [AceInfo(1, 0, 0x1F01FF, "S-1-1-0"), AceInfo(0, 0, 0x120089, ME)], ME, P, protected=True
    )


# ---------- continuity monitor ----------


class FakeClocks:
    def __init__(self) -> None:
        self.wall = 1_800_000_000.0
        self.mono = 1000.0
        self.unbiased = 500.0

    def advance(self, wall: float, mono: float, unbiased: float | None = None) -> None:
        self.wall += wall
        self.mono += mono
        self.unbiased += mono if unbiased is None else unbiased


def _mon(c: FakeClocks, probe: bool = True) -> ContinuityMonitor:
    return ContinuityMonitor(
        clock_jump_seconds=30,
        stall_seconds=15,
        wall=lambda: c.wall,
        mono=lambda: c.mono,
        unbiased=(lambda: c.unbiased) if probe else False,
    )


def test_normal_ticks_are_quiet() -> None:
    c = FakeClocks()
    m = _mon(c)
    for _ in range(100):
        c.advance(1.0, 1.0)
        assert m.tick() == []


def test_sleep_detected_as_suspend() -> None:
    c = FakeClocks()
    m = _mon(c)
    c.advance(3600, 3600, unbiased=1)  # laptop lid closed for an hour
    (d,) = m.tick()
    assert d.cause is Cause.SUSPEND


def test_stall_without_suspend() -> None:
    c = FakeClocks()
    m = _mon(c)
    c.advance(20, 20)  # process frozen, machine awake
    assert [d.cause for d in m.tick()] == [Cause.STALL]


def test_stall_without_probe_still_trips() -> None:
    c = FakeClocks()
    m = _mon(c, probe=False)
    c.advance(3600, 3600)
    assert [d.cause for d in m.tick()] == [Cause.STALL]


@pytest.mark.parametrize("jump", [31, -31, 3600, -7200])
def test_clock_jump(jump: float) -> None:
    c = FakeClocks()
    m = _mon(c)
    c.advance(1 + jump, 1)
    assert [d.cause for d in m.tick()] == [Cause.CLOCK_JUMP]


def test_small_clock_slew_ignored() -> None:
    c = FakeClocks()
    m = _mon(c)
    c.advance(1 + 29, 1)
    assert m.tick() == []


def test_real_monitor_constructs() -> None:
    m = ContinuityMonitor(30, 15)
    assert m.tick() == []


# ---------- safety gate ----------


def _gate(log: list[str], fail_first: set[str] | None = None) -> SafetyGate:
    fails = dict.fromkeys(fail_first or set(), 1)

    def step(name: str):
        async def run(*_a: object) -> None:
            if fails.get(name):
                fails[name] -= 1
                log.append(f"{name}!")
                raise ConnectionError(name)
            log.append(name)

        return run

    async def no_sleep(_: float) -> None:
        log.append("wait")

    return SafetyGate(
        RecoverySteps(step("reach"), step("cancel"), step("snap"), step("recon")), sleep=no_sleep
    )


async def test_gate_starts_closed_and_recovers_in_order() -> None:
    log: list[str] = []
    g = _gate(log)
    assert not g.can_quote
    assert await g.recover()
    assert g.can_quote
    assert log == ["reach", "cancel", "snap", "recon"]


async def test_gate_waits_for_network_before_cancelling() -> None:
    log: list[str] = []
    g = _gate(log, fail_first={"reach"})
    await g.recover()
    assert log == ["reach!", "wait", "reach", "cancel", "snap", "recon"]
    assert g.state is GateState.RUNNING


async def test_gate_trip_closes_quoting_immediately() -> None:
    log: list[str] = []
    g = _gate(log)
    await g.recover()
    g.trip(Discontinuity(Cause.NETWORK, "wifi"))
    assert not g.can_quote
    log.clear()
    await g.recover()
    assert log[:2] == ["reach", "cancel"]  # cancel-all happens before anything else


async def test_gate_trip_during_recovery_reruns() -> None:
    log: list[str] = []
    g = _gate(log)

    async def recon() -> None:
        log.append("recon")
        if log.count("recon") == 1:
            g.trip(Discontinuity(Cause.SUSPEND, "slept mid-recovery"))

    g.steps.reconcile = recon
    await g.recover()
    assert log.count("cancel") == 2 and g.can_quote


async def test_gate_gives_up_after_max_attempts() -> None:
    log: list[str] = []
    g = _gate(log, fail_first={"cancel"})
    assert not await g.recover(max_attempts=1)
    assert not g.can_quote


def test_clock_drift_warning() -> None:
    assert describe_clock_offset(-0.4, 0.6, 1.0) is None
    assert "BEHIND" in (describe_clock_offset(1.5, 2.5, 1.0) or "")
    assert "AHEAD" in (describe_clock_offset(-3.0, -2.0, 1.0) or "")


# ---------- expiry keeper ----------


async def test_expiry_refresh_only_due_orders_and_keeps_price_count() -> None:
    now = [1_800_000_000.0]
    calls: list[tuple] = []

    async def amend(order_id: str, **kw: object) -> dict:
        calls.append((order_id, kw))
        return {}

    k = ExpiryKeeper(amend, expiry_seconds=600, refresh_seconds=240, clock=lambda: now[0])
    fresh = LiveOrder("a", "T", "bid", D("0.05"), D(3), int(now[0]) + 590)
    stale = LiveOrder("b", "T", "ask", D("0.95"), D(2), int(now[0]) + 300)
    assert await k.refresh([fresh, stale], can_quote=lambda: True) == 1
    ((oid, kw),) = calls
    assert oid == "b"
    assert kw == {
        "ticker": "T", "side": "ask", "price": D("0.95"), "count": D(2),
        "expiration_time": int(now[0]) + 600,
    }  # fmt: skip
    assert stale.expiration_time == int(now[0]) + 600


async def test_expiry_refresh_stops_when_gate_closed() -> None:
    async def amend(*_a: object, **_k: object) -> dict:
        raise AssertionError("must not amend while gate is closed")

    k = ExpiryKeeper(amend, 600, 240, clock=lambda: 0.0)
    assert await k.refresh([LiveOrder("a", "T", "bid", D("0.1"), D(1), 10)], lambda: False) == 0


def test_expiry_keeper_rejects_unsafe_cadence() -> None:
    async def amend(*_a: object, **_k: object) -> dict:
        return {}

    with pytest.raises(ValueError):
        ExpiryKeeper(amend, 600, 400)


# ---------- power ----------


def test_keep_awake_enters_and_exits() -> None:
    with keep_awake(True) as active:
        assert active is (sys.platform == "win32")
    with keep_awake(False) as active:
        assert active is False
