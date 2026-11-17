#!/usr/bin/env python3
#
# tests/test_wan_e2e.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""WAN watcher end to end: native TR-064 client -> WanWatcher -> real WanInterruptionTracker."""

import threading
import time
from datetime import timedelta

import pytest

from app.fritzbox.tr064.exceptions import TR064HttpUnauthorized
from app.fritzbox.wan_watch import WanWatcher
from tests.wanbox import ORIGIN, WanBox


class Clock:
    """Wall and monotonic clock that follow the box's scripted time ``t``."""

    def __init__(self, box: WanBox) -> None:
        self.box = box

    def now(self):
        return ORIGIN + timedelta(seconds=self.box.t)

    def monotonic(self) -> float:
        return 1000.0 + self.box.t


def _watcher(session_ref: dict, link_type: str = "DSL") -> WanWatcher:
    return WanWatcher(
        lambda: session_ref["session"],
        lambda: link_type,
        lambda exc: isinstance(exc, TR064HttpUnauthorized),
        log_once=lambda _key, _message: None,
    )


def _poll(watcher: WanWatcher, box: WanBox, clock: Clock, seconds: int):
    box.set(t=box.t + seconds)
    return watcher.poll_once(clock.now, clock.monotonic)


def test_interruption_is_detected_and_closed_through_the_real_client():
    box = WanBox()
    clock = Clock(box)
    session = {"session": box.client()}
    watcher = _watcher(session)
    events = []
    for _ in range(3):
        events += _poll(watcher, box, clock, 5)
    assert events == []

    box.set(status="Disconnected", sync=1)
    events += _poll(watcher, box, clock, 5)
    box.set(status="Connected", link_start=box.t, error="ERROR_NONE")
    events += _poll(watcher, box, clock, 5)
    events += _poll(watcher, box, clock, 5)

    kinds = [event.kind for event in events]
    assert kinds[0] == "opened" and kinds[-1] == "closed", kinds
    assert box.count("GetInfo") > 0
    assert box.count("X_AVM-DE_GetDSLDiagnoseInfo") > 0
    session["session"].close()


def test_all_calls_failing_raises_unavailable_and_recovers():
    from app.fritzbox.wan_watch import WanWatchUnavailable

    box = WanBox()
    clock = Clock(box)
    session = {"session": box.client()}
    watcher = _watcher(session)
    _poll(watcher, box, clock, 5)
    box.set(fail=True)
    with pytest.raises(WanWatchUnavailable):
        _poll(watcher, box, clock, 5)
    box.set(fail=False)
    assert _poll(watcher, box, clock, 5) == []
    session["session"].close()


def test_transient_time_error_does_not_pin_the_lifetime_fallback():
    box = WanBox()
    clock = Clock(box)
    session = {"session": box.client()}
    watcher = _watcher(session)
    # A failed optional lookup retries after its short cooldown, before the regular 900 s refresh.
    real_hook_state = {"fail_time": True}

    def hook(request):
        if b"time1" in request.url.raw_path.lower():
            if real_hook_state["fail_time"]:
                real_hook_state["fail_time"] = False
                import httpx

                raise httpx.ConnectError("simulated", request=request)

    box.fake.hook = hook
    _poll(watcher, box, clock, 5)
    assert watcher._window is not None
    assert watcher._window.kind == "lifetime"
    _poll(watcher, box, clock, 5)
    assert watcher._window.kind == "lifetime"
    _poll(watcher, box, clock, 55)
    assert watcher._window.kind == "hour_of_day"
    session["session"].close()


def test_closed_session_still_published_reports_unavailability():
    from app.fritzbox.wan_watch import WanWatchUnavailable

    box = WanBox()
    clock = Clock(box)
    session = {"session": box.client()}
    watcher = _watcher(session)
    _poll(watcher, box, clock, 5)
    session["session"].close()
    with pytest.raises(WanWatchUnavailable, match="published TR-064 session is closed"):
        _poll(watcher, box, clock, 5)
    assert watcher._tracker.needs_attention


def test_session_swap_under_parallel_workers_ends_cleanly():
    box = WanBox()
    clock = Clock(box)
    old = box.client(generation=1)
    new = box.client(generation=2)
    session = {"session": old}
    watcher = _watcher(session)
    _poll(watcher, box, clock, 5)

    errors: list[BaseException] = []

    def swap():
        session["session"] = new
        old.close()
        old.close()  # closing twice must be harmless

    def worker():
        try:
            for _ in range(5):
                watcher.poll_once(clock.now, clock.monotonic)
        except BaseException as exc:  # noqa: BLE001 - the test reports any escape
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    swap()
    started = time.monotonic()
    for t in threads:
        t.join(timeout=5)
    assert not any(t.is_alive() for t in threads)
    assert time.monotonic() - started < 5
    assert not errors or all(type(e).__name__ == "WanWatchUnavailable" for e in errors)
    # After the swap the watcher answers through the new client.
    before = len(box.calls)
    _poll(watcher, box, clock, 5)
    assert len(box.calls) > before
    new.close()


def test_late_success_of_a_replaced_session_never_reaches_the_tracker():
    box = WanBox()
    clock = Clock(box)
    old = box.client(generation=1)
    new = box.client(generation=2)
    session = {"session": old}
    watcher = _watcher(session)
    _poll(watcher, box, clock, 5)
    seen = []
    real_observe = watcher._tracker.observe
    watcher._tracker.observe = lambda sample, window: (seen.append(sample), real_observe(sample, window))[1]
    swapped = []

    def swap(request):
        if request.method == "POST" and not swapped:
            swapped.append(True)
            session["session"] = new

    box.fake.hook = swap
    assert _poll(watcher, box, clock, 5) == []
    assert seen == []
    box.fake.hook = None
    session["session"] = new
    _poll(watcher, box, clock, 5)
    assert len(seen) == 1
    old.close()
    new.close()
