#!/usr/bin/env python3
#
# fritzfluxdb/classes/fritzbox/service_definitions/logs.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

from datetime import datetime

from fritzfluxdb.classes.fritzbox.service_definitions import lua_services


def prepare_json_response_data(response):
    """
    handler to prepare returned json data for parsing
    """

    return response.json()


lua_services.append(
    {
        "name": "System logs",
        "os_min_versions": "7.29",
        "os_max_versions": "7.38",
        "method": "POST",
        "params": {
            "filter": 1,
            "page": "log",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "track": True,
        "interval": 60,
        "value_instances": {
            "log_entry": {
                "data_path": "data.log",
                "type": list,
                "next": {
                    # data struct type: list
                    "type": str,
                    "tags": {
                        "log_type": "System"
                    },
                    "timestamp_function": lambda data:
                        datetime.strptime(f'{data[0]} {data[1]}', '%d.%m.%y %H:%M:%S'),
                    "value_function": lambda data: data[2],
                    "tags_function": None
                }
            }
        }
    })

lua_services.append(
    {
        "name": "System logs",
        "os_min_versions": "7.39",
        "method": "POST",
        "params": {
            "filter": "sys",
            "page": "log",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "track": True,
        "interval": 60,
        "value_instances": {
            "log_entry": {
                "data_path": "data.log",
                "type": list,
                "next": {
                    # data struct type: list
                    "type": str,
                    "tags": {
                        "log_type": "System"
                    },
                    "timestamp_function": lambda data:
                        datetime.strptime(f'{data.get("date")} {data.get("time")}', '%d.%m.%y %H:%M:%S'),
                    "value_function": lambda data: data.get("msg"),
                    "tags_function": None
                }
            }
        }
    })

lua_services.append(
    {
        "name": "Internet connection logs",
        "os_min_versions": "7.29",
        "os_max_versions": "7.38",
        "method": "POST",
        "params": {
            "filter": 2,
            "page": "log",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "track": True,
        "interval": 61,
        "value_instances": {
            "log_entry": {
                "data_path": "data.log",
                "type": list,
                "next": {
                    # data struct type: list
                    "type": str,
                    "tags": {
                        "log_type": "Internet connection"
                    },
                    "timestamp_function": lambda data:
                        datetime.strptime(f'{data[0]} {data[1]}', '%d.%m.%y %H:%M:%S'),
                    "value_function": lambda data: data[2],
                    "tags_function": None
                }
            }
        }
    })

lua_services.append(
    {
        "name": "Internet connection logs",
        "os_min_versions": "7.39",
        "method": "POST",
        "params": {
            "filter": "net",
            "page": "log",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "track": True,
        "interval": 61,
        "value_instances": {
            "log_entry": {
                "data_path": "data.log",
                "type": list,
                "next": {
                    # data struct type: list
                    "type": str,
                    "tags": {
                        "log_type": "Internet connection"
                    },
                    "timestamp_function": lambda data:
                        datetime.strptime(f'{data.get("date")} {data.get("time")}', '%d.%m.%y %H:%M:%S'),
                    "value_function": lambda data: data.get("msg"),
                    "tags_function": None
                }
            }
        }
    })

lua_services.append(
    {
        "name": "Telephony logs",
        "os_min_versions": "7.29",
        "os_max_versions": "7.38",
        "method": "POST",
        "params": {
            "filter": 3,
            "page": "log",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "track": True,
        "interval": 62,
        "value_instances": {
            "log_entry": {
                "data_path": "data.log",
                "type": list,
                "next": {
                    # data struct type: list
                    "type": str,
                    "tags": {
                        "log_type": "Telephony"
                    },
                    "timestamp_function": lambda data:
                        datetime.strptime(f'{data[0]} {data[1]}', '%d.%m.%y %H:%M:%S'),
                    "value_function": lambda data: data[2],
                    "tags_function": None
                }
            }
        }
    })

lua_services.append(
    {
        "name": "Telephony logs",
        "os_min_versions": "7.39",
        "method": "POST",
        "params": {
            "filter": "fon",
            "page": "log",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "track": True,
        "interval": 62,
        "value_instances": {
            "log_entry": {
                "data_path": "data.log",
                "type": list,
                "next": {
                    # data struct type: list
                    "type": str,
                    "tags": {
                        "log_type": "Telephony"
                    },
                    "timestamp_function": lambda data:
                        datetime.strptime(f'{data.get("date")} {data.get("time")}', '%d.%m.%y %H:%M:%S'),
                    "value_function": lambda data: data.get("msg"),
                    "tags_function": None
                }
            }
        }
    })

lua_services.append(
    {
        "name": "WLAN logs",
        "os_min_versions": "7.29",
        "os_max_versions": "7.38",
        "method": "POST",
        "params": {
            "filter": 4,
            "page": "log",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "track": True,
        "interval": 63,
        "value_instances": {
            "log_entry": {
                "data_path": "data.log",
                "type": list,
                "next": {
                    # data struct type: list
                    "type": str,
                    "tags": {
                        "log_type": "WLAN"
                    },
                    "timestamp_function": lambda data:
                        datetime.strptime(f'{data[0]} {data[1]}', '%d.%m.%y %H:%M:%S'),
                    "value_function": lambda data: data[2],
                    "tags_function": None
                }
            }
        }
    })

lua_services.append(
    {
        "name": "WLAN logs",
        "os_min_versions": "7.39",
        "method": "POST",
        "params": {
            "filter": "wlan",
            "page": "log",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "track": True,
        "interval": 63,
        "value_instances": {
            "log_entry": {
                "data_path": "data.log",
                "type": list,
                "next": {
                    # data struct type: list
                    "type": str,
                    "tags": {
                        "log_type": "WLAN"
                    },
                    "timestamp_function": lambda data:
                        datetime.strptime(f'{data.get("date")} {data.get("time")}', '%d.%m.%y %H:%M:%S'),
                    "value_function": lambda data: data.get("msg"),
                    "tags_function": None
                }
            }
        }
    })

lua_services.append(
    {
        "name": "USB Devices logs",
        "os_min_versions": "7.29",
        "os_max_versions": "7.38",
        "method": "POST",
        "params": {
            "filter": 5,
            "page": "log",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "track": True,
        "interval": 64,
        "value_instances": {
            "log_entry": {
                "data_path": "data.log",
                "type": list,
                "next": {
                    # data struct type: list
                    "type": str,
                    "tags": {
                        "log_type": "USB Devices"
                    },
                    "timestamp_function": lambda data:
                        datetime.strptime(f'{data[0]} {data[1]}', '%d.%m.%y %H:%M:%S'),
                    "value_function": lambda data: data[2],
                    "tags_function": None
                }
            }
        }
    })

lua_services.append(
    {
        "name": "USB Devices logs",
        "os_min_versions": "7.39",
        "method": "POST",
        "params": {
            "filter": "usb",
            "page": "log",
            "lang": "de"
        },
        "response_parser": prepare_json_response_data,
        "track": True,
        "interval": 64,
        "value_instances": {
            "log_entry": {
                "data_path": "data.log",
                "type": list,
                "next": {
                    # data struct type: list
                    "type": str,
                    "tags": {
                        "log_type": "USB Devices"
                    },
                    "timestamp_function": lambda data:
                        datetime.strptime(f'{data.get("date")} {data.get("time")}', '%d.%m.%y %H:%M:%S'),
                    "value_function": lambda data: data.get("msg"),
                    "tags_function": None
                }
            }
        }
    })
