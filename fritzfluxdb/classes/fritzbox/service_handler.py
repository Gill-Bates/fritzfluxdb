#!/usr/bin/env python3
#
# fritzfluxdb/classes/fritzbox/service_handler.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from typing import Union, AnyStr, Dict
from datetime import datetime, UTC
from collections import deque

from fritzfluxdb.common import do_error_exit
from fritzfluxdb.log import get_logger
from fritzfluxdb.classes.common import FritzMeasurement

log = get_logger()


class FritzBoxAction:
    """
        defines a single FritzBox query action
    """

    available = True

    def __init__(self, action: Union[AnyStr, Dict] = None) -> None:

        if action is None:
            do_error_exit("Missing action for FritzBoxAction")

        self.params = dict()
        if isinstance(action, str):
            self.name = action
        elif isinstance(action, dict):
            self.name = action.get("name", None)
            self.params = action.get("params", dict())
        else:
            do_error_exit(f"A FritzBoxAction param action must be a string or dict, got '{type(action)}'")

        if self.name is None:
            do_error_exit("FritzBoxAction name was not defined in action parameter")


class FritzBoxService:
    """
        base class to provide a FritzBox service. It is used to manage a single service request interval,
        keeping track of last query and if this service was enabled/disabled during discovery.
    """

    available = True
    name = None
    value_instances = None
    interval = 10
    last_query = None

    def __init__(self, service_data: Dict = None):

        if not isinstance(service_data, dict):
            do_error_exit(f"{self.__class__.name} service data must be a dict")
            return

        self.name = service_data.get("name")
        self.params = service_data.get("params")
        self.value_instances = dict()
        self.interval = service_data.get("interval", self.interval)

        if self.name is None:
            do_error_exit(f"{self.__class__.name} instance has no name")
            return

        self.add_value_instances(service_data.get("value_instances", dict()))

    def add_value_instances(self, data: Dict = None) -> None:

        if data is None:
            log.error(f"Missing value instances data for {self.__class__.name} '{self.name}'")
            return

        if not isinstance(data, dict):
            log.error("Data for value_instances must be a dict")
            return

        self.value_instances = data

        return

    def set_last_query_now(self):
        """
            needs to be called after every successful service query
        """
        self.last_query = datetime.now(UTC)

    def should_be_requested(self):
        """
        determines if conditions are fulfilled to request this service again
        """

        if self.available is False:
            return False

        if self.last_query and (datetime.now(UTC)-self.last_query).total_seconds() < self.interval:
            return False

        return True


class FritzBoxTR069Service(FritzBoxService):
    """
    a single TR069 service
    """

    actions = None

    def __init__(self, service_data=None):

        super().__init__(service_data)

        self.actions = list()
        self.link_type = service_data.get("link_type")  # defines for which link type this service is valid for

        for action in service_data.get("actions", list()):
            self.add_action(action)

    def add_action(self, action: Union[AnyStr, Dict] = None) -> None:

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

    def __init__(self, service_data=None):

        super().__init__(service_data)

        self.url_path = service_data.get("url_path", self.default_url_path)
        self.os_min_versions = service_data.get("os_min_versions")
        self.os_max_versions = service_data.get("os_max_versions")
        self.method = service_data.get("method", self.default_method)
        self.response_parser = service_data.get("response_parser", self.response_parser)
        self.link_type = service_data.get("link_type")  # defines for which link type this service is valid for

        if len(self.url_path) == 0:
            do_error_exit(f"FritzBoxLuaService '{self.name}' instance has no url_path defined")

        if self.os_min_versions is None:
            do_error_exit(f"FritzBoxLuaService '{self.name}' instance has no supported 'os_min_versions' defined")

        if not callable(self.response_parser):
            do_error_exit(f"FritzBoxLuaService '{self.name}' instance 'response_parser' is not a callable function")

        if self.method not in ["GET", "POST", "HEAD"]:
            do_error_exit(f"FritzBoxLuaService '{self.name}' instance 'method' invalid: {self.method}")

        self.validate_value_instances()

        # used for services parsing log entries
        self.track_measurements = bool(service_data.get("track", False))
        self.tracked_measurements: set[int] = set()
        self.tracked_measurement_order: deque[int] = deque()

    def _validate_metric_definition(self, metric_name: str, metric_params: dict) -> None:
        has_source = (
            metric_params.get("data_path") is not None
            or metric_params.get("value_function") is not None
        )
        if not has_source:
            do_error_exit(
                f"FritzBoxLuaService '{self.name}' metric '{metric_name}' "
                "has no 'data_path' and no 'value_function' defined"
            )

        if metric_params.get("type") is None:
            do_error_exit(
                f"FritzBoxLuaService '{self.name}' metric '{metric_name}' has no 'type' defined"
            )

        for callable_key in ("value_function", "tags_function", "timestamp_function", "exclude_filter_function"):
            value = metric_params.get(callable_key)
            if value is not None and not callable(value):
                do_error_exit(
                    f"FritzBoxLuaService '{self.name}' metric '{metric_name}' "
                    f"has non-callable '{callable_key}'"
                )

        nested = metric_params.get("next")
        if nested is not None:
            self._validate_metric_definition(metric_name, nested)

    def validate_value_instances(self) -> None:
        """
        validates the schema of each provided value instance mapping
        """
        for metric_name, metric_params in self.value_instances.items():
            self._validate_metric_definition(metric_name, metric_params)

    def skip_tracked_measurement(self, measurement: FritzMeasurement):
        """
        check if measurement has already been generated. This is helpful reading logs and only add logs
        which have not been seen before
        """

        if self.track_measurements is True and hash(measurement) in self.tracked_measurements:
            return True

        return False

    def add_tracked_measurement(self, measurement: FritzMeasurement) -> None:
        """
        adds a measurement to the tracking list
        """

        if not self.track_measurements:
            return

        measurement_hash = hash(measurement)
        if measurement_hash in self.tracked_measurements:
            return

        self.tracked_measurements.add(measurement_hash)
        self.tracked_measurement_order.append(measurement_hash)

        while len(self.tracked_measurement_order) > self.max_tracked_measurements:
            old_hash = self.tracked_measurement_order.popleft()
            self.tracked_measurements.discard(old_hash)

    @staticmethod
    def response_parser(response):
        """
        handler to prepare returned data for parsing
        """

        return response.text

    def os_version_match(self, current_os_version):

        # very simple version comparison but should do with the current AVM version schema.
        def versiontuple(v):
            return tuple(map(int, (v.split("."))))

        if self.os_max_versions is None:
            return versiontuple(current_os_version) >= versiontuple(self.os_min_versions)

        return versiontuple(self.os_min_versions) <= versiontuple(current_os_version) <= versiontuple(self.os_max_versions)
