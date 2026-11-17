#!/usr/bin/env python3
#
# app/utils/settings.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared base for the pydantic configuration models.

Value sources stay in one loader (environment variable, then INI key, then the
field default) because pydantic-settings would need a custom INI source plus the
QUESTDB_* mapping on top, i.e. a second loader.
"""

import configparser
import os
from typing import Any, ClassVar

from pydantic import BaseModel, PrivateAttr, SecretStr, ValidationError, field_validator

from app.utils.common import do_error_exit, parse_bool
from app.utils.log import get_logger

log = get_logger()

SENSITIVE_KEYS = frozenset({"password", "token", "secret", "key"})


def reveal(value: Any) -> Any:
    """Return the plain value of a SecretStr, or the value itself."""
    return value.get_secret_value() if isinstance(value, SecretStr) else value


class ConfigModel(BaseModel):
    """Pydantic model that is built from a ConfigParser and the environment.

    Invalid values are logged as readable lines (no traceback) and set
    ``parser_error``; the object stays constructible so main() can exit with
    EX_CONFIG. Fields with ``exclude=True`` are runtime state, not options.
    """

    config_section_name: ClassVar[str]
    label: ClassVar[str]

    _parser_error: bool = PrivateAttr(default=False)

    def __init__(self, config_data: configparser.ConfigParser):
        if not isinstance(config_data, configparser.ConfigParser):
            do_error_exit("config data is not a config parser object")

        raw, pre_error = self.prepare(config_data)
        errors = self._validate_into_self(raw)
        self._parser_error = pre_error or errors
        self._parser_error = self._check_required() or self._parser_error
        self._log_effective()

    @property
    def parser_error(self) -> bool:
        return self._parser_error

    def mark_parser_error(self) -> None:
        # a private attribute, so this also works on a frozen model
        self._parser_error = True

    @classmethod
    def prepare(cls, config_data: configparser.ConfigParser) -> tuple[dict[str, Any], bool]:
        """Return the raw option values and whether preparing already failed."""
        return cls.collect_raw(config_data), False

    @classmethod
    def collect_raw(cls, config_data: configparser.ConfigParser) -> dict[str, str]:
        section = cls.config_section_name
        raw: dict[str, str] = {}
        for name, field in cls.model_fields.items():
            if field.exclude:
                continue
            value = config_data.get(section, name, fallback=None)
            env_value = os.environ.get(f"{section}_{name}".upper())
            # a blank variable counts as unset (Unraid sends "") and must not hide the INI value
            if env_value is not None and env_value.strip():
                value = env_value
            if value is not None and value.strip():
                raw[name] = value
        return raw

    def _validate_into_self(self, raw: dict[str, Any]) -> bool:
        validator = self.__pydantic_validator__
        try:
            validator.validate_python(raw, self_instance=self, context={"lenient": False})
            return False
        except ValidationError as exc:
            failed = {err["loc"][0] for err in exc.errors() if err["loc"]}
            for line in self._error_lines(exc):
                log.error(line)

        # still build the object, with defaults for the rejected options
        rest = {k: v for k, v in raw.items() if k not in failed}
        try:
            validator.validate_python(rest, self_instance=self, context={"lenient": True})
        except ValidationError:
            for slot in ("__dict__", "__pydantic_fields_set__", "__pydantic_extra__", "__pydantic_private__"):
                object.__setattr__(self, slot, getattr(self.model_construct(**self._safe_strings(rest)), slot))
        return True

    @classmethod
    def _safe_strings(cls, raw: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in raw.items() if cls.model_fields[k].annotation in (str, str | None)}

    @classmethod
    def _error_lines(cls, exc: ValidationError) -> list[str]:
        lines: list[str] = []
        for err in exc.errors(include_url=False):
            name = err["loc"][0] if err["loc"] else None
            kind = err["type"]
            if kind == "int_parsing":
                lines.append(f"Unable to parse '{err.get('input', '')}' for '{name}' as int")
            elif kind == "value_error":
                # validators raise complete messages, one line per problem
                lines.extend(err["msg"].removeprefix("Value error, ").splitlines())
            else:
                where = f"{cls.label} {name}" if name else cls.label
                lines.append(f"Invalid {where}: {err['msg']}")
        return lines

    @field_validator("*", mode="before")
    @classmethod
    def _coerce_bool(cls, value: Any, info) -> Any:
        if cls.model_fields[info.field_name].annotation is bool and isinstance(value, str):
            try:
                return parse_bool(value)
            except ValueError:
                raise ValueError(f"Unable to parse '{value}' for '{info.field_name}' as bool") from None
        return value

    def required_options(self) -> tuple[str, ...]:
        return ()

    def _check_required(self) -> bool:
        missing = False
        for name in self.required_options():
            if getattr(self, name) is None:
                log.error("%s %s not defined", self.label, name)
                missing = True
        return missing

    def _log_effective(self) -> None:
        for name, field in type(self).model_fields.items():
            if field.exclude:
                continue
            value = getattr(self, name)
            if any(key in name.lower() for key in SENSITIVE_KEYS) or isinstance(value, SecretStr):
                value = "***"
            log.debug("Config: %s.%s = %s", self.config_section_name, name, value)

