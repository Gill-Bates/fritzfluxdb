#!/usr/bin/env python3
#
# tests/wanbox.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""WAN scenario box for the watcher tests: a ``FakeFritzBox`` whose answers follow mutable state.

The watcher and the handler run against the real native client; only the transport
is simulated. Uptimes follow ``t`` so a scenario only sets start times.
"""

from datetime import UTC, datetime, timedelta

import httpx

from app.fritzbox.tr064.client import TR064Client
from tests.fakebox import FakeFritzBox, FakeService

TR64 = "tr64desc.xml"
IGD = "igddesc.xml"
DIAG = "X_AVM-DE_GetDSLDiagnoseInfo"
PREV = "X_AVM_DE_GetAutoDisconnectTimeSpan"
ORIGIN = datetime(2026, 11, 16, 2, 0, 0, tzinfo=UTC)

_STATE_FIELDS = {
    "t", "status", "link_start", "router_start", "sync", "error",
    "prevention_enabled", "prevention_hour", "local_time", "uptime",
}


def wan_services(*, dsl_diagnose: bool = True, prevention: bool = True) -> list[FakeService]:
    """The services the WAN watcher uses; the two optional AVM actions can be left out."""
    status = (("NewConnectionStatus", "out", "string"), ("NewUptime", "out", "ui4"), ("NewLastConnectionError", "out", "string"))
    ppp = {"GetInfo": status}
    if prevention:
        ppp[PREV] = (("NewX_AVM-DE_DisconnectPreventionEnable", "out", "boolean"), ("NewX_AVM-DE_DisconnectPreventionHour", "out", "ui1"))
    dsl = {DIAG: (("NewX_AVM-DE_DSLSync", "out", "boolean"),)} if dsl_diagnose else {}
    return [
        FakeService("WANPPPConnection1", "WANPPPConnection:1", TR64, depth=2, actions=ppp),
        FakeService("WANDSLInterfaceConfig1", "WANDSLInterfaceConfig:1", TR64, depth=2, actions=dsl),
        FakeService("WANIPConn1", "WANIPConnection:1", IGD, depth=2, actions={"GetStatusInfo": status}),
        FakeService("DeviceInfo1", "DeviceInfo:1", TR64, actions={"GetInfo": (("NewUpTime", "out", "ui4"),)}),
        FakeService("Time1", "Time:1", TR64, actions={"GetInfo": (("NewCurrentLocalTime", "out", "string"),)}),
    ]


class WanBox:
    """Scripted WAN state served by a ``FakeFritzBox``; ``set()`` changes it between polls."""

    def __init__(self, *, origin: datetime = ORIGIN, dsl_diagnose: bool = True, prevention: bool = True, **state) -> None:
        self.origin = origin
        self.fake = FakeFritzBox(wan_services(dsl_diagnose=dsl_diagnose, prevention=prevention))
        self.t = 0
        self.status = "Connected"
        self.link_start = -72000
        self.router_start = -500000
        self.sync = 1
        self.error = "ERROR_NONE"
        self.prevention_enabled = "1"
        self.prevention_hour = 4
        self.local_time: str | None = None  # None: origin plus t
        self.uptime: int | None = None  # None: t minus link_start while connected
        self.set(**state)

    @property
    def calls(self) -> list[tuple[str, str]]:
        return self.fake.calls

    def count(self, action: str) -> int:
        return sum(1 for _, name in self.fake.calls if name == action)

    def set(self, *, fail: bool | None = None, **state) -> None:
        """Change scripted state (see ``_STATE_FIELDS``) and re-publish the answers; ``fail`` refuses every connection."""
        unknown = set(state) - _STATE_FIELDS
        if unknown:
            raise AttributeError(f"unknown WAN state: {sorted(unknown)}")
        for key, value in state.items():
            setattr(self, key, value)
        if fail is not None:
            self.fake.hook = self._refuse if fail else None
        self._publish()

    @staticmethod
    def _refuse(request: httpx.Request) -> None:
        raise httpx.ConnectError("simulated unreachable box", request=request)

    def _publish(self) -> None:
        up = self.status == "Connected"
        uptime = self.uptime if self.uptime is not None else (self.t - self.link_start if up else 0)
        info = {"NewConnectionStatus": self.status, "NewUptime": str(uptime), "NewLastConnectionError": self.error}
        names = {s.name for s in self.fake.services}
        if "WANPPPConnection1" in names:
            self.fake.respond("WANPPPConnection1", "GetInfo", info)
            if PREV in next(s for s in self.fake.services if s.name == "WANPPPConnection1").actions:
                self.fake.respond("WANPPPConnection1", PREV, {
                    "NewX_AVM-DE_DisconnectPreventionEnable": str(self.prevention_enabled),
                    "NewX_AVM-DE_DisconnectPreventionHour": str(self.prevention_hour),
                })
        self.fake.respond("WANIPConn1", "GetStatusInfo", info)
        if DIAG in next(s for s in self.fake.services if s.name == "WANDSLInterfaceConfig1").actions:
            self.fake.respond("WANDSLInterfaceConfig1", DIAG, {"NewX_AVM-DE_DSLSync": str(self.sync)})
        self.fake.respond("DeviceInfo1", "GetInfo", {"NewUpTime": str(self.t - self.router_start)})
        local = self.local_time or (self.origin + timedelta(seconds=self.t)).strftime("%Y-%m-%dT%H:%M:%S")
        self.fake.respond("Time1", "GetInfo", {"NewCurrentLocalTime": local})

    def client(self, *, generation: int = 1) -> TR064Client:
        """A discovered native client wired to this box."""
        return self.fake.tr064_client(generation=generation)
