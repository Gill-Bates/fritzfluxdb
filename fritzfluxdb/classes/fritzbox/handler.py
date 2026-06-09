#!/usr/bin/env python3
#
# fritzfluxdb/classes/fritzbox/handler.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio

import requests
import xml.etree.ElementTree as ET
import hashlib

# import 3rd party modules
from fritzconnection import FritzConnection
from fritzconnection.core.exceptions import FritzConnectionException, FritzServiceError, FritzActionError

from fritzfluxdb.classes.fritzbox.config import FritzBoxConfig
from fritzfluxdb.log import get_logger
from fritzfluxdb.classes.fritzbox.service_handler import FritzBoxTR069Service, FritzBoxLuaService
import fritzfluxdb.classes.fritzbox.service_definitions as service_definitions
from fritzfluxdb.classes.common import FritzMeasurement
from fritzfluxdb.common import grab
from fritzfluxdb.classes.fritzbox.model import FritzBoxModel

log = get_logger()


class FritzBoxHandlerBase:
    """
        base class to provide common methods to both FritzBox handler classes
    """

    def __init__(self, config):
        if isinstance(config, FritzBoxConfig):
            self.config = config
        else:
            self.config = FritzBoxConfig(config)

        self.init_successful = False
        self.services = list()
        self.current_result_list = list()
        self.discovery_done = False

        self.version = None

    def add_services(self, class_name, service_definition):
        """
        Adds services from config to handler

        Parameters
        ----------
        class_name: FritzBoxTR069Service, FritzBoxLuaService
            the fritzbox service class
        service_definition: list
            list of service definitions
        """

        for fritzbox_service in service_definition:
            new_service = class_name(fritzbox_service)

            # adjust request interval if necessary
            if self.config.request_interval > new_service.interval:
                new_service.interval = self.config.request_interval

            self.services.append(new_service)

    def query_service_data(self, _):
        # stub for the default function
        pass

    async def task_loop(self, queue):
        while True:
            self.current_result_list = list()
            for service in self.services:
                await asyncio.to_thread(self.query_service_data, service)

            for result in self.current_result_list:
                log.debug(result)
                await queue.put(result)

            self.discovery_done = True
            await asyncio.sleep(1)


class FritzBoxHandler(FritzBoxHandlerBase):

    name = "FritzBox TR-069"

    def __init__(self, config):

        super().__init__(config)

        self.session = None
        self.add_services(FritzBoxTR069Service, service_definitions.tr069_services)

    def connect(self):

        if self.init_successful is True:
            return

        log.debug(f"Initiating new {self.name} session")

        auto_detect = (self.config.tls_enabled is None)
        use_tls = True if auto_detect else bool(self.config.tls_enabled)

        # For auto-detect, probe the TLS port (default 49000 + 443 = 49443)
        default_port = FritzBoxConfig.port["default"]
        port = self.config.port
        if auto_detect and port == default_port:
            port = default_port + 443

        def _create_session(use_tls_flag, port_num):
            return FritzConnection(
                address=self.config.hostname,
                port=port_num,
                user=self.config.username,
                password=self.config.password,
                timeout=(self.config.connect_timeout, self.config.connect_timeout * 4),
                use_tls=use_tls_flag
            )

        try:
            self.session = _create_session(use_tls, port)
            self.version = self.session.system_version
            if auto_detect:
                self.config.tls_enabled = use_tls
        except FritzConnectionException as exc:
            if auto_detect and use_tls:
                log.warning(
                    "FritzBox '%s' TR-069 HTTPS unavailable (%s); falling back to plain HTTP",
                    self.config.hostname, exc,
                )
                self.config.tls_enabled = False
                try:
                    self.session = _create_session(False, self.config.port)
                    self.version = self.session.system_version
                except FritzConnectionException as exc2:
                    log.error(f"Failed to connect to FritzBox via TR-069 '{exc2}'")
                    return
                except Exception:
                    log.exception("Unexpected error while creating FritzBox TR-069 session")
                    return
            else:
                log.error(f"Failed to connect to FritzBox via TR-069 '{exc}'")
                return
        except Exception:
            log.exception("Unexpected error while creating FritzBox TR-069 session")
            return

        # test connection
        try:
            device_info = self.session.call_action("DeviceInfo", "GetInfo")
        except FritzConnectionException as e:
            if "401" in str(e):
                log.error(f"Failed to connect to {self.name} '{self.config.hostname}' using credentials. "
                          "Check username and password!")
            else:
                log.error(f"Failed to connect to {self.name} '{self.config.hostname}': {e}")

            return
        except Exception as e:
            log.error(f"Failed to connect to {self.name} '{self.config.hostname}': {e}")
            return

        if isinstance(device_info, dict):
            self.config.model = device_info.get("NewModelName")
            self.config.fw_version = device_info.get("NewSoftwareVersion")
            self.config.serial_number = device_info.get("NewSerialNumber") or None

        # get link type
        try:
            link_info = self.session.call_action("WANCommonIFC", "GetCommonLinkProperties")
            link_type = FritzBoxModel.get_link_type(self.config.model, link_info.get("NewWANAccessType"))
            self.config.link_type = link_type
        except FritzConnectionException as exc:
            log.warning(f"Unable to determine FritzBox link type: {exc}")
        except Exception:
            pass

        proto = "HTTPS" if self.config.tls_enabled else "HTTP"
        log.info(f"Successfully established {self.name} session ({proto})")

        self.init_successful = True

    def close(self):
        if self.session is None:
            return
            
        raw_session = getattr(self.session, "session", None)
        if raw_session is not None:
            raw_session.close()

        if self.init_successful is True:
            log.info(f"Closed {self.name} connection")

    def query_service_data(self, service):

        def service_invalid_log(log_message):
            if service.link_type is not None and service.link_type != self.config.link_type:
                log_handler = log.debug
                log_message += f" (only available on {service.link_type} connections)"
            else:
                log_handler = log.warning

            log_handler(log_message)

        if not isinstance(service, FritzBoxTR069Service):
            log.error("Query service must be of type 'FritzBoxTR069Service'")
            return

        if self.discovery_done is True and service.should_be_requested() is False:
            return

        # Request every action
        for action in service.actions:

            if service.available is False:
                break

            if self.discovery_done is True and action.available is False:
                log.debug(f"Skipping disabled service action: {service.name} - {action.name}")
                continue

            # add parameters
            try:
                call_result = self.session.call_action(service.name, action.name, **action.params)
            except FritzServiceError:
                service_invalid_log(f"Requested invalid service: {service.name}")
                if self.discovery_done is False:
                    service_invalid_log(f"Querying service '{service.name}' will be disabled")
                    service.available = False
                continue
            except FritzActionError:
                service_invalid_log(f"Requested invalid action '{action.name}' for service: {service.name}")
                if self.discovery_done is False:
                    service_invalid_log(f"Querying action '{action.name}' will be disabled")
                    action.available = False
                continue
            except FritzConnectionException as e:
                if "401" in str(e):
                    log.error(f"Failed to connect to {self.name} '{self.config.hostname}' using credentials. "
                              "Check username and password!")
                elif "820" in str(e) and self.discovery_done is False:
                    service_invalid_log(f"Querying action '{action.name}' will be disabled")
                    action.available = False
                else:
                    log.error(f"Failed to connect to {self.name} '{self.config.hostname}': {e}")
                continue
            except Exception as e:
                log.error(f"Unable to request {self.name} data: {e}")
                continue

            if call_result is None:
                continue

            debug_msg = f"Request {self.name} service '{service.name}' returned successfully: {action.name}"
            if len(action.params) > 0:
                debug_msg += f" ({action.params})"
            log.debug(debug_msg)

            # set time stamp of this query
            service.set_last_query_now()

            for key, value in call_result.items():
                log.debug(f"{self.name} result: {key} = {value}")
                metric_name = service.value_instances.get(key)

                if metric_name is not None:

                    data_type = None

                    # support setting a data type by appending it to the metric name by a double colon
                    if ":" in metric_name:
                        metric_data_type = metric_name.split(":")[1]
                        metric_name = metric_name.split(":")[0]

                        data_type = {
                            "str": str,
                            "int": int,
                            "float": float,
                            "bool": bool
                        }.get(metric_data_type)

                        if data_type is None:
                            log.warning(f"Unknown data type '{metric_data_type}' for metric '{key}' "
                                        f"in service '{service.name}'")

                    self.current_result_list.append(
                        FritzMeasurement(metric_name, value, box_tag=self.config.box_tag, data_type=data_type)
                    )

            # special case: update firmware version when requested
            if service.name == "DeviceInfo" and action.name == "GetInfo":
                self.config.fw_version = call_result.get("NewSoftwareVersion", "154.7.50")

        if self.discovery_done is False:
            if True not in [x.available for x in service.actions]:
                service_invalid_log(f"All actions for service '{service.name}' are unavailable. Disabling service.")
                service.available = False

        return


class FritzBoxLuaHandler(FritzBoxHandlerBase):

    name = "FritzBox Lua"

    def __init__(self, config):
        super().__init__(config)

        self.url = None  # built lazily in connect() after TR-069 auto-detect resolves tls_enabled
        self.sid = None

        self.session = requests.Session()
        self.session.verify = self.config.verify_tls

        self.add_services(FritzBoxLuaService, service_definitions.lua_services)

    def connect(self):

        if self.sid is not None:
            return

        # Build URL now — TR-069 connect() may have resolved tls_enabled from None to True/False
        use_tls = bool(self.config.tls_enabled) if self.config.tls_enabled is not None else True

        # In auto-detect mode use HTTPS without cert verification: FritzBox always has a
        # self-signed certificate, so SSLError would be a false negative. Only a
        # ConnectionError (port unreachable) is a real signal to fall back to HTTP.
        if self.config.tls_auto and use_tls:
            self.session.verify = False
        elif self.config.tls_enabled is True and not self.config.verify_tls:
            self.session.verify = False
            log.warning(f"TLS certificate verification is disabled for FritzBox '{self.config.hostname}'")

        self.url = f"{'https' if use_tls else 'http'}://{self.config.hostname}"

        login_url = f"{self.url}/login_sid.lua"

        log.debug(f"Initiating new {self.name} session")

        # perform login
        try:
            response = self.session.get(login_url, timeout=(self.config.connect_timeout, self.config.connect_timeout*4))
            dom = ET.fromstring(response.content)
            sid = dom.findtext('./SID')
            challenge = dom.findtext('./Challenge')
        except requests.exceptions.ConnectionError as exc:
            if self.config.tls_auto and use_tls:
                # HTTPS port unreachable — fall back to HTTP
                log.warning(
                    "FritzBox '%s' Lua HTTPS unreachable; falling back to plain HTTP.",
                    self.config.hostname,
                )
                self.config.tls_enabled = False
                self.session.verify = True
                self.url = f"http://{self.config.hostname}"
                login_url = f"{self.url}/login_sid.lua"
                try:
                    response = self.session.get(login_url, timeout=(self.config.connect_timeout, self.config.connect_timeout*4))
                    dom = ET.fromstring(response.content)
                    sid = dom.findtext('./SID')
                    challenge = dom.findtext('./Challenge')
                except (requests.RequestException, ET.ParseError) as exc2:
                    log.error(f"Unable to parse {self.name} login response after HTTP fallback: {exc2}")
                    return
            else:
                log.error(f"Unable to parse {self.name} login response: {exc}")
                return
        except (requests.RequestException, ET.ParseError) as exc:
            log.error(f"Unable to parse {self.name} login response: {exc}")
            return

        if sid != "0" * 16:
            log.error(f"Unexpected {self.name} session id: {sid}")
            return

        if not challenge:
            log.error(f"Missing {self.name} login challenge")
            return

        md5 = hashlib.md5()
        md5.update(challenge.encode('utf-16le'))
        md5.update('-'.encode('utf-16le'))
        md5.update(self.config.password.encode('utf-16le'))

        login_params = {
            "username": self.config.username,
            "response": challenge + '-' + md5.hexdigest()
        }

        try:
            response = self.session.get(login_url, timeout=self.config.connect_timeout, params=login_params)
            dom = ET.fromstring(response.content)
            sid = dom.findtext('./SID')
            block_time = dom.findtext('./BlockTime')
        except (requests.RequestException, ET.ParseError) as exc:
            log.error(f"Unable to parse {self.name} login response: {exc}")
            return

        if block_time != "0":
            log.error(f"Failed to connect to {self.name} '{self.config.hostname}'. "
                      f"Logins blocked for '{block_time}' seconds!")
            return

        if sid == "0" * 16:
            log.error(f"Failed to connect to {self.name} '{self.config.hostname}' using credentials. "
                      "Check username and password!")
            return

        proto = "HTTPS" if self.config.tls_enabled else "HTTP"
        log.info(f"Successfully established {self.name} session ({proto})")

        self.sid = sid
        self.init_successful = True

    def request(self, service_to_request, additional_params, *, retry_on_auth: bool = True):

        if self.sid is None:
            self.connect()
            if self.sid is None:
                return None

        params = {
            "sid": self.sid
        }

        # appending additional params
        if isinstance(additional_params, dict):
            params = {**params, **additional_params}

        # basic function call attributes
        call_attributes = {
            "timeout": self.config.connect_timeout
        }

        if service_to_request.method == "POST":
            call_attributes["data"] = params
        else:
            call_attributes["params"] = params

        data_url = f"{self.url}{service_to_request.url_path}"

        # perform request
        try:
            response = self.session.request(service_to_request.method, data_url, **call_attributes)
        except requests.RequestException as exc:
            log.error(f"Unable to perform request to '{data_url}': {exc}")
            return None

        # invalidate session on auth-related status codes before parsing
        if response.status_code in [303, 401, 403]:
            self.sid = None
            if retry_on_auth:
                return self.request(service_to_request, additional_params, retry_on_auth=False)
            return None

        # check for HTML response indicating an expired/invalid session
        if response.content[:100].lstrip().lower().startswith(b"<html"):
            self.sid = None
            if retry_on_auth:
                return self.request(service_to_request, additional_params, retry_on_auth=False)
            return None

        try:
            result = service_to_request.response_parser(response)
        except Exception as e:
            log.error(f"{self.name} request parsing for '{service_to_request.name}' failed: {e}")
            return None

        if response.status_code == 200:
            log.debug(f"{self.name} request successful")
        else:
            log.error(f"{self.name} request '{service_to_request.name}' returned {response.status_code}: {response.reason}")

        return result

    def close(self):
        self.session.close()
        if self.init_successful is True:
            log.info(f"Closed {self.name} connection")

    @staticmethod
    def _parse_bool(value) -> bool:
        if isinstance(value, bool):
            return value

        if isinstance(value, int):
            return value != 0

        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "t", "1", "yes", "on"}:
                return True
            if normalized in {"false", "f", "0", "no", "off"}:
                return False

        raise ValueError(f"invalid boolean value: {value!r}")


    def extract_value(self, service, data, metric_name, metric_params):

        # read config
        data_path = metric_params.get("data_path")
        data_type = metric_params.get("type")
        data_next = metric_params.get("next")
        data_tags = metric_params.get("tags")
        value_function = metric_params.get("value_function")
        tags_function = metric_params.get("tags_function")                      # needs to return a dict
        timestamp_function = metric_params.get("timestamp_function")            # needs to return a datetime
        exclude_filter_function = metric_params.get("exclude_filter_function")  # needs to return a bool

        # define defaults
        metric_value = None
        timestamp = None
        metric_tags = dict()

        if callable(exclude_filter_function):
            try:
                if exclude_filter_function(data) is True:
                    return
            except Exception as exc:
                log.error(f"Exclude filter for metric '{metric_name}' failed: {exc}")
                return

        if data_path is not None and value_function is not None:
            log.error("Attributes 'data_path' and 'value_function' cant be defined for the same entry"
                      f"at the same time: {metric_params}")
            return

        # first we try to use the value_function
        if value_function is not None:
            try:
                metric_value = value_function(data)
            except Exception as exc:
                log.error(f"Value function for metric '{metric_name}' failed: {exc}")
                return

        elif data_path is not None:
            metric_value = grab(data, data_path, fallback="" if data_type is str else None)

        # always apply tags_function when present, regardless of static tags
        metric_tags = dict(data_tags or {})
        if callable(tags_function):
            try:
                generated_tags = tags_function(data)
            except ValueError as exc:
                log.error(f"Invalid tags for metric '{metric_name}': {exc}")
                return
            if not isinstance(generated_tags, dict):
                log.error(f"tags_function for metric '{metric_name}' did not return a dict")
                return
            metric_tags.update(generated_tags)

        if callable(timestamp_function):
            try:
                timestamp = timestamp_function(data)

                # make timestamp time zone aware if time zone is missing
                if timestamp.tzinfo is None or timestamp.tzinfo.utcoffset(timestamp) is None:
                    timestamp = timestamp.replace(tzinfo=self.config.timezone)

            except Exception as exc:
                log.error(f"Timestamp function for metric '{metric_name}' failed: {exc}")
                return

        if metric_value is None:
            log.error(f"Unable to extract '{metric_name}' form '{data}', got '{type(metric_value)}'")
            return

        if data_type in [int, float, bool, str]:
            try:
                if data_type is bool:
                    metric_value = self._parse_bool(metric_value)
                else:
                    metric_value = data_type(metric_value)
            except Exception as e:
                log.error(f"Unable to convert {self.name} value '{metric_value}' "
                          f"for '{metric_name}' to '{data_type}': {e}")
                return

            metric = FritzMeasurement(metric_name, metric_value, data_type=data_type, box_tag=self.config.box_tag,
                                      additional_tags=metric_tags, timestamp=timestamp)

            # check if measurement is tracked and already reported
            if service.skip_tracked_measurement(metric) is True:
                return

            # track measurement (if configured)
            service.add_tracked_measurement(metric)

            self.current_result_list.append(metric)
            return

        if type(metric_value) is not data_type:
            log.error(f"FritzBox metric type '{data_type}' for '{metric_name}' "
                      f"does not match '{type(metric_value)}' data: {metric_value}")
            return

        if data_type is list and data_next is not None:
            for next_metric_value in metric_value:
                self.extract_value(service, next_metric_value, metric_name, data_next)

            return

        if data_type is dict and data_next is not None:
            for next_metric_value in metric_value.values():
                self.extract_value(service, next_metric_value, metric_name, data_next)

            return

        log.error(f"Unknown metric '{data_path}' form '{data}', with type '{type(metric_value)}' "
                  f"and defined type '{data_type}'")

    def query_service_data(self, service):

        if not isinstance(service, FritzBoxLuaService):
            log.error("Query service must be of type 'FritzBoxLuaService'")
            return

        if self.discovery_done is True and service.should_be_requested() is False:
            return

        service_and_version_name = f"{service.name} " \
                                   f"(Fritz!OS {service.os_min_versions} - {service.os_max_versions or 'latest'})"
        if self.discovery_done is False:
            if service.os_version_match(self.config.fw_version) is False:
                log.debug(f"FritzOS version {self.config.fw_version} not compatible with "
                          f"supported versions for '{service.name}': "
                          f"{service.os_min_versions} - {service.os_max_versions or 'latest'}")
                service.available = False
                return

            if service.link_type is not None and self.config.link_type != service.link_type:
                log.info(f"Service '{service_and_version_name}' not applicable for this "
                         f"FritzBox Model Link type '{self.config.link_type}'")
                service.available = False
                return

        # request data
        result = self.request(service, additional_params=service.params)

        if result is None:
            message_handler = log.info
            message_text = f"Unable to request {self.name} service '{service.name}'"
            if result is None:
                message_handler = log.error
                message_text += ", no data returned"
            elif self.discovery_done is True:
                message_handler = log.error

            message_handler(message_text)

            if self.discovery_done is False:
                log.info(f"{self.name} service '{service_and_version_name}' will be disabled.")
                service.available = False
            return

        log.debug(f"Request {self.name} service '{service_and_version_name}' returned successfully")

        # set time stamp of this query
        service.set_last_query_now()

        # Request every param
        for metric_name, metric_params in service.value_instances.items():
            self.extract_value(service, result, metric_name, metric_params)

        return
