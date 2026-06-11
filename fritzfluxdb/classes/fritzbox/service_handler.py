#!/usr/bin/env python3
#
# fritzfluxdb/classes/fritzbox/service_handler.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import re
import time
from typing import Any
from datetime import datetime, UTC
from collections import deque

from fritzfluxdb.log import get_logger
from fritzfluxdb.classes.common import FritzMeasurement

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

    available = True
    name = None
    value_instances = None
    interval = 10.0
    last_query = None

    def __init__(self, service_data: dict[str, Any] | None = None):

        if not isinstance(service_data, dict):
            raise TypeError(f"{self.__class__.__name__} service data must be a dict")

        self.available = True
        self.last_query = None
        self._last_query_monotonic: float | None = None
        self.name = service_data.get("name")
        self.description = service_data.get("description")
        
        params = service_data.get("params") or {}
        if not isinstance(params, dict):
            raise TypeError(
                f"{self.__class__.__name__} service '{self.name}' params must be a dict, got {type(params)!r}"
            )
        self.params = params
        
        self.value_instances = {}
        
        interval = service_data.get("interval", self.interval)
        if not isinstance(interval, (int, float)) or interval <= 0:
            raise ValueError(
                f"{self.__class__.__name__} service '{self.name}' has invalid interval: {interval!r}"
            )
        self.interval = float(interval)

        if not isinstance(self.name, str) or not self.name:
            raise ValueError(f"{self.__class__.__name__} instance has no name")

        self.add_value_instances(service_data.get("value_instances", dict()))

    def add_value_instances(self, data: dict[str, Any] | None = None) -> None:

        if data is None:
            log.error(f"Missing value instances data for {self.__class__.name} '{self.name}'")
            return

        if not isinstance(data, dict):
            log.error("Data for value_instances must be a dict")
            return

        self.value_instances = data

    def set_last_query_now(self) -> None:
        """
            needs to be called after every successful service query
        """
        self.last_query = datetime.now(UTC)
        self._last_query_monotonic = time.monotonic()

    def should_be_requested(self) -> bool:
        """
        determines if conditions are fulfilled to request this service again
        """

        if self.available is False:
            return False

        if (
            self._last_query_monotonic is not None
            and time.monotonic() - self._last_query_monotonic < self.interval
        ):
            return False

        return True


class FritzBoxTR069Service(FritzBoxService):
    """
    a single TR069 service
    """

    actions = None

    def __init__(self, service_data: dict[str, Any] | None = None):

        super().__init__(service_data)

        self.actions = list()
        self.link_type = service_data.get("link_type")  # defines for which link type this service is valid for

        actions = service_data.get("actions", [])
        if not isinstance(actions, list):
            raise TypeError(
                f"FritzBoxTR069Service '{self.name}' actions must be a list"
            )

        for action in actions:
            self.add_action(action)

    def add_action(self, action: str | dict[str, Any] | None = None) -> None:

        if action is None:
            log.error(f"Missing action for FritzBoxTR069Service '{self.name}'")
            return

        action_instance = FritzBoxAction(action)

        if action_instance.name is None:
            log.error(f"Failed to add action to FritzBoxTR069Service '{self.name}': {action}")
        else:
            self.actions.append(action_instance)


class FritzBoxLuaURLPath:
    data = "/data.lua"
    homeautomation = "/webservices/homeautoswitch.lua"
    foncalls_list = "/fon_num/foncalls_list.lua"


class FritzBoxLuaService(FritzBoxService):
    """
    a single Lua service
    """

    os_min_versions = None
    os_max_versions = None
    url_path = None
    default_method = "GET"
    default_url_path = FritzBoxLuaURLPath.data
    link_type = None
    max_tracked_measurements = 10_000

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

        # used for services parsing log entries
        self.track_measurements = bool(service_data.get("track", False))
        self.tracked_measurements: set[tuple[object, ...]] = set()
        self.tracked_measurement_order: deque[tuple[object, ...]] = deque()

    def _measurement_tracking_key(self, measurement: FritzMeasurement) -> tuple[object, ...]:
        return (
            measurement.name,
            measurement.value,
            measurement.timestamp,
            tuple(sorted((measurement.additional_tags or {}).items())),
        )

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

    def skip_tracked_measurement(self, measurement: FritzMeasurement) -> bool:
        """
        check if measurement has already been generated. This is helpful reading logs and only add logs
        which have not been seen before
        """
        if not self.track_measurements:
            return False

        return self._measurement_tracking_key(measurement) in self.tracked_measurements

    def add_tracked_measurement(self, measurement: FritzMeasurement) -> None:
        """
        adds a measurement to the tracking list
        """
        if not self.track_measurements:
            return

        measurement_key = self._measurement_tracking_key(measurement)
        if measurement_key in self.tracked_measurements:
            return

        self.tracked_measurements.add(measurement_key)
        self.tracked_measurement_order.append(measurement_key)

        while len(self.tracked_measurement_order) > self.max_tracked_measurements:
            old_key = self.tracked_measurement_order.popleft()
            self.tracked_measurements.discard(old_key)

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
            # tolerate lab/build suffixes like '7.62-123456' by extracting
            # the leading numeric components only
            parts = re.findall(r"\d+", str(v))
            if not parts:
                return None
            return tuple(int(x) for x in parts[:3])

        current = versiontuple(current_os_version)
        minimum = versiontuple(self.os_min_versions)

        if current is None or minimum is None:
            log.warning(
                "Unable to compare FritzOS version for service '%s': current=%r min=%r",
                self.name, current_os_version, self.os_min_versions,
            )
            return False

        maximum = versiontuple(self.os_max_versions)
        if maximum is None:
            return current >= minimum

        return minimum <= current <= maximum
