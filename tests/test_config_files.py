#!/usr/bin/env python3
#
# tests/test_config_files.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import builtins

import pytest

from app.utils.configparser import import_config


def test_later_config_file_overrides_earlier_one(tmp_path):
    first = tmp_path / "a.ini"
    second = tmp_path / "b.ini"
    first.write_text("[s]\nkey = one\nkeep = yes\n", encoding="utf-8")
    second.write_text("[s]\nkey = two\n", encoding="utf-8")

    config = import_config([str(first), str(second)])

    assert config["s"]["key"] == "two"
    assert config["s"]["keep"] == "yes"


def test_file_that_cannot_be_opened_after_the_checks_is_an_error(tmp_path, monkeypatch, capsys):
    path = tmp_path / "vanished.ini"
    path.write_text("[s]\nkey = one\n", encoding="utf-8")
    real_open = builtins.open

    def failing_open(file, *args, **kwargs):
        if str(file) == str(path):
            raise PermissionError("denied after the check")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", failing_open)

    with pytest.raises(SystemExit) as error:
        import_config([str(path)])

    assert error.value.code == 78
    assert "denied after the check" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("content", "expected"),
    [(b"[s]\nkey = \xff\xfe\n", "codec"), (b"no section header\n", "no section header")],
)
def test_invalid_encoding_and_invalid_ini_are_errors(tmp_path, capsys, content, expected):
    path = tmp_path / "broken.ini"
    path.write_bytes(content)

    with pytest.raises(SystemExit) as error:
        import_config([str(path)])

    assert error.value.code == 78
    assert expected in capsys.readouterr().err
