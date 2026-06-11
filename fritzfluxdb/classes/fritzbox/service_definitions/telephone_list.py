#!/usr/bin/env python3
#
# fritzfluxdb/classes/fritzbox/service_definitions/telephone_list.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import csv
import hashlib
from datetime import datetime
from io import StringIO

from fritzfluxdb.classes.fritzbox.service_handler import FritzBoxLuaURLPath
from fritzfluxdb.classes.fritzbox.service_definitions import lua_services


read_interval = 60


class CallLogConfig:

    def __init__(self, sep, header):
        self.sep = sep.strip() if isinstance(sep, str) and sep.strip() else ";"
        self.header_list = []

        if isinstance(header, str) and header:
            self.header_list = next(csv.reader(StringIO(header), delimiter=self.sep), [])


class CallLogEntry:
    """
    parse a single call log entry
    maps columns to be backwards compatible
    """

    call_types = {
        "1": "incoming",
        "2": "unanswered",
        "3": "blocked",
        "4": "outgoing"
    }

    def __init__(self, entry: str, config: CallLogConfig):

        # compute a MD5 hash and use as ID to track and group log data by uid tag
        self._hash = hashlib.md5(entry.encode("UTF-8"), usedforsecurity=False).hexdigest()

        entry_dict = dict(zip(
            config.header_list,
            next(csv.reader(StringIO(entry), delimiter=config.sep), []),
            strict=False,
        ))

        self._call_type = self.call_types.get(entry_dict.get("Typ"), "undefined")
        date_value = entry_dict.get("Datum")
        if not date_value:
            raise ValueError(f"missing call date in entry: {entry!r}")
        self._date_time = datetime.strptime(date_value, "%d.%m.%y %H:%M")
        self._caller_name = entry_dict.get("Name", "")
        self._caller_number = entry_dict.get("Rufnummer", "")
        self._caller_location = entry_dict.get("Landes-/Ortsnetzbereich", "")
        self._extension = entry_dict.get("Nebenstelle", "")
        self._number_called = entry_dict.get("Eigene Rufnummer", "")
        self._duration = self.get_call_duration(entry_dict.get("Dauer"))

    @staticmethod
    def get_call_duration(field) -> int:
        """
        returns call duration in minutes
        """

        if not field:
            return 0

        try:
            hours, minutes = field.split(":", maxsplit=1)
            return int(hours) * 60 + int(minutes)
        except (AttributeError, ValueError):
            return 0

    @property
    def hash(self) -> str:
        return self._hash

    @property
    def type(self) -> str:
        return self._call_type

    @property
    def date_time(self) -> datetime:
        return self._date_time

    @property
    def caller_name(self) -> str:
        return self._caller_name.strip('"')

    @property
    def caller_number(self) -> str:
        return self._caller_number.strip('"')

    @property
    def caller_location(self) -> str:
        return self._caller_location.strip('"')

    @property
    def extension(self) -> str:
        return self._extension.strip('"')

    @property
    def number_called(self) -> str:
        return self._number_called.strip('"')

    @property
    def duration(self) -> int:
        return self._duration


class CallLog:
    """
    Parse FritzBox call log entries csv list
    extracts separator and header, parses each line with given seperator
    """

    new_line_char = "\n"

    def __init__(self, data):

        self.entries = list()
        if not isinstance(data, str):
            return

        lines = data.splitlines()

        if len(lines) == 0:
            return

        sep = ""
        header = ""

        # extract separator
        if lines[0].startswith("sep="):
            sep = lines[0].removeprefix("sep=").strip()
            lines = lines[1:]

        if not lines:
            return

        # extract header
        if lines[0].strip('"').startswith("Typ"):
            header = lines[0]
            lines = lines[1:]

        config = CallLogConfig(sep, header)

        if len(config.header_list) == 0:
            return

        for line in lines:
            if len(line) == 0:
                continue

            try:
                self.entries.append(CallLogEntry(line, config))
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError(f"invalid call log line: {line!r}") from exc


def _call_entry_metric(attr: str, data_type: type = str) -> dict:
    return {
        "type": list,
        "value_function": lambda data: data,
        "next": {
            "type": data_type,
            "tags_function": lambda entry: {"uid": entry.hash},
            "value_function": lambda entry, _a=attr: getattr(entry, _a),
            "timestamp_function": lambda entry: entry.date_time,
        },
    }


# Tracking prevents the same Fritz!Box call-list row from being emitted repeatedly
# across polling intervals.
lua_services.append(
    {
        "name": "Phone call list",
        "os_min_versions": "7.29",
        "url_path": FritzBoxLuaURLPath.foncalls_list,
        "method": "GET",
        "params": {
            "switchcmd": "getdevicelistinfos",
            "csv": "",
        },
        "response_parser": lambda response: CallLog(response.text).entries,
        "interval": read_interval,
        "track": True,
        "value_instances": {
            "call_list_type":            _call_entry_metric("type"),
            "call_list_caller_name":     _call_entry_metric("caller_name"),
            "call_list_caller_number":   _call_entry_metric("caller_number"),
            "call_list_caller_location": _call_entry_metric("caller_location"),
            "call_list_extension":       _call_entry_metric("extension"),
            "call_list_number_called":   _call_entry_metric("number_called"),
            "call_list_duration":        _call_entry_metric("duration", int),
        }
    }
)
