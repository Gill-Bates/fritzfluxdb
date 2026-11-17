#!/usr/bin/env python3
#
# app/fritzbox/service_handler.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import logging
import math
import re
import time
from typing import Any

from app.utils.log import get_logger

log = get_logger()


class FritzBoxAction:
    """
        defines a single FritzBox query action
    """

    def __init__(self, action: str | dict[str, Any] | None = None) -> None:
        self.available = True
        self.params = {}

        if action is None:
            raise ValueError("Missing action for FritzBoxAction")

        if isinstance(action, str):
            self.name = action
        elif isinstance(action, dict):
            self.name = action.get("name")
            params = action.get("params", {})
            if not isinstance(params, dict):
                raise TypeError(
                    f"FritzBoxAction '{self.name}' params must be a dict, got {type(params)!r}"
                )
            self.params = params
        else:
            raise TypeError(f"A FritzBoxAction param action must be a string or dict, got '{type(action)}'")

        if not isinstance(self.name, str) or not self.name:
            raise ValueError("FritzBoxAction name was not defined in action parameter")


class FritzBoxService:
    """
        base class to provide a FritzBox service. It is used to manage a single service request interval,
        keeping track of last query and if this service was enabled/disabled during discovery.
    """

    # default request interval, overridable per service definition
    interval = 10.0

    def __init__(self, service_data: dict[str, Any] | None = None):

        if not isinstance(service_data, dict):
            raise TypeError(f"{self.__class__.__name__} service data must be a dict")

        self.available = True
        self._next_request_monotonic: float | None = None
        self.failure_count = 0
        # Set by the handler while a query fails transiently; outage_reported marks an escalation.
        self.transient_failure: str | None = None
        self.outage_reported = False
        self.name = service_data.get("name")
        self.description = service_data.get("description")

        params = service_data.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise TypeError(
                f"{self.__class__.__name__} service '{self.name}' params must be a dict, got {type(params)!r}"
            )
        self.params = params

        self.value_instances = {}

        interval = service_data.get("interval", self.interval)
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval <= 0:
            raise ValueError(
                f"{self.__class__.__name__} service '{self.name}' has invalid interval: {interval!r}"
            )
        self.interval = float(interval)

        if not isinstance(self.name, str) or not self.name:
            raise ValueError(f"{self.__class__.__name__} instance has no name")

        self.add_value_instances(service_data.get("value_instances", {}))

    def add_value_instances(self, data: dict[str, Any] | None = None) -> None:

        if data is None:
            log.error(f"Missing value instances data for {self.__class__.__name__} '{self.name}'")
            return

        if not isinstance(data, dict):
            log.error("Data for value_instances must be a dict")
            return

        self.value_instances = data

    def set_last_query_now(self) -> None:
        """
            needs to be called after every successful service query
        """
        self._next_request_monotonic = time.monotonic() + self.interval
        self.failure_count = 0

    def set_failed_query_now(self, initial_backoff: float = 10.0, max_backoff: float = 300.0) -> float:
        """
            needs to be called after transient service query failures
        """
        now = time.monotonic()
        self.failure_count += 1
        delay = float(initial_backoff)
        for _ in range(min(self.failure_count - 1, 1024)):
            if delay >= max_backoff / 2:
                delay = float(max_backoff)
                break
            delay *= 2
        delay = max(
            self.interval,
            min(float(max_backoff), delay),
        )
        self._next_request_monotonic = now + delay
        return delay

    def should_be_requested(self) -> bool:
        """
        determines if conditions are fulfilled to request this service again
        """

        if self.available is False:
            return False

        return not (self._next_request_monotonic is not None and time.monotonic() < self._next_request_monotonic)


class FritzBoxTR064Service(FritzBoxService):
    """
    a single TR064 service
    """

    def __init__(self, service_data: dict[str, Any] | None = None):

        super().__init__(service_data)

        self.actions = []
        self.link_type = service_data.get("link_type")  # defines for which link type this service is valid for

        actions = service_data.get("actions", [])
        if not isinstance(actions, list):
            raise TypeError(
                f"FritzBoxTR064Service '{self.name}' actions must be a list"
            )

        for action in actions:
            self.add_action(action)

    def add_action(self, action: str | dict[str, Any] | None = None) -> None:

        if action is None:
            log.error(f"Missing action for FritzBoxTR064Service '{self.name}'")
            return

        self.actions.append(FritzBoxAction(action))


class FritzBoxLuaURLPath:
    data = "/data.lua"
    homeautomation = "/webservices/homeautoswitch.lua"
    foncalls_list = "/fon_num/foncalls_list.lua"


class FritzBoxLuaService(FritzBoxService):
    """
    a single Lua service
    """

    default_method = "GET"
    default_url_path = FritzBoxLuaURLPath.data
    _version_warned = False

    def __init__(self, service_data: dict[str, Any] | None = None):

        super().__init__(service_data)

        url_path = service_data.get("url_path", self.default_url_path)
        if (
            not isinstance(url_path, str)
            or not url_path
            or not url_path.startswith("/")
            or "://" in url_path
            or any(char in url_path for char in "\r\n")
        ):
            raise ValueError(
                f"FritzBoxLuaService '{self.name}' instance has invalid url_path: {url_path!r}"
            )
        self.url_path = url_path

        self.os_min_versions = service_data.get("os_min_versions")
        self.os_max_versions = service_data.get("os_max_versions")

        method = service_data.get("method", self.default_method)
        if not isinstance(method, str):
            raise TypeError(
                f"FritzBoxLuaService '{self.name}' instance 'method' must be a string"
            )
        method = method.upper()
        if method not in {"GET", "POST"}:
            raise ValueError(
                f"FritzBoxLuaService '{self.name}' instance 'method' invalid: {method}"
            )
        self.method = method

        self.response_parser = service_data.get("response_parser", self.response_parser)
        self.link_type = service_data.get("link_type")  # defines for which link type this service is valid for

        if self.os_min_versions is None:
            raise ValueError(f"FritzBoxLuaService '{self.name}' instance has no supported 'os_min_versions' defined")

        if not callable(self.response_parser):
            raise TypeError(f"FritzBoxLuaService '{self.name}' instance 'response_parser' is not a callable function")

        self.validate_value_instances()

        # Event services (log entries, call list) are stored as deduplicated events:
        # QuestDB routes them to a separate table, the databases overwrite repeated points.
        self.is_event = bool(service_data.get("event", False))

    def _validate_metric_definition(self, metric_name: str, metric_params: dict[str, Any]) -> None:
        if not isinstance(metric_params, dict):
            raise TypeError(
                f"FritzBoxLuaService '{self.name}' metric '{metric_name}' "
                f"must be a dict, got {type(metric_params)!r}"
            )

        has_source = (
            metric_params.get("data_path") is not None
            or metric_params.get("value_function") is not None
        )
        if not has_source:
            raise ValueError(
                f"FritzBoxLuaService '{self.name}' metric '{metric_name}' "
                "has no 'data_path' and no 'value_function' defined"
            )

        if metric_params.get("type") is None:
            raise ValueError(
                f"FritzBoxLuaService '{self.name}' metric '{metric_name}' has no 'type' defined"
            )

        for callable_key in ("value_function", "tags_function", "timestamp_function", "exclude_filter_function"):
            value = metric_params.get(callable_key)
            if value is not None and not callable(value):
                raise TypeError(
                    f"FritzBoxLuaService '{self.name}' metric '{metric_name}' "
                    f"has non-callable '{callable_key}'"
                )

        nested = metric_params.get("next")
        if nested is not None:
            if not isinstance(nested, dict):
                raise TypeError(
                    f"FritzBoxLuaService '{self.name}' metric '{metric_name}' "
                    "'next' must be a dict"
                )
            self._validate_metric_definition(metric_name, nested)

    def validate_value_instances(self) -> None:
        """
        validates the schema of each provided value instance mapping
        """
        for metric_name, metric_params in self.value_instances.items():
            self._validate_metric_definition(metric_name, metric_params)

    @staticmethod
    def response_parser(response):
        """
        handler to prepare returned data for parsing
        """

        return response.text

    def os_version_match(self, current_os_version) -> bool:

        def versiontuple(v):
            if not v:
                return None
            # tolerate lab/build suffixes like '7.62-123456': only the first dotted
            # numeric group counts, so a build number never sorts above the maximum
            match = re.search(r"\d+(?:\.\d+)*", str(v))
            if match is None:
                return None
            return tuple(int(x) for x in match.group().split(".")[:3])

        current = versiontuple(current_os_version)
        minimum = versiontuple(self.os_min_versions)

        if current is None or minimum is None:
            # warn once per service; the check runs on every poll
            level = logging.DEBUG if self._version_warned else logging.WARNING
            self._version_warned = True
            log.log(
                level,
                "Unable to compare FritzOS version for service '%s': current=%r min=%r",
                self.name, current_os_version, self.os_min_versions,
            )
            return False

        maximum = versiontuple(self.os_max_versions)
        if maximum is None:
            return current >= minimum

        return minimum <= current <= maximum
