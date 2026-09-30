#!/usr/bin/env python3
#
# tests/test_questdb_admin.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
from unittest.mock import patch

import pytest

from app.questdb_admin import (
    AdminError,
    QuestDBAdmin,
    _confirm_action,
    _parser,
    _run,
    main_async,
)


class FakeAdmin:
    def __init__(self, *, boxes=None, views=None, partitions=None, state=False):
        self.boxes = boxes or ["fritzbox_AA1"]
        self.views = views or {"fritzbox_AA1": [("fritzbox_AA1_rollup_1m_v1", "valid", 4, 4)]}
        self.partition_rows = partitions or [("DAY", "2026-01-01", 10), ("DAY", "2026-01-02", 20)]
        self.state = state
        self.queries = []

    async def inventory(self):
        return self.boxes, self.views

    async def require_box(self, table):
        if table not in self.boxes or not table.startswith("fritzbox"):
            raise AdminError("not a managed table")
        return self.views[table]

    async def partitions(self, table):
        await self.require_box(table)
        return self.partition_rows

    async def query(self, sql):
        self.queries.append(sql)
        if "FROM tables()" in sql:
            return [["_fritzfluxdb_downsampling"]] if self.state else []
        return []


def invoke(admin, *args, answer="yes"):
    with patch("builtins.input", return_value=answer):
        asyncio.run(_run(_parser().parse_args(args), admin))


def test_cli_without_arguments_prints_help_and_exits_successfully(capsys):
    assert asyncio.run(main_async([])) == 0

    output = capsys.readouterr().out
    assert "usage:" in output
    assert "doctor" in output
    assert "drop-all" in output
    assert "Commands are positional words" in output
    assert "fritzflux-cli drop-box fritzbox_AA1234567890 --apply" in output


@pytest.mark.parametrize("arguments", [["--doctor"], ["--doctor", "list"]])
def test_cli_explains_that_commands_do_not_take_option_prefix(arguments, capsys):
    with pytest.raises(SystemExit) as error:
        asyncio.run(main_async(arguments))

    message = capsys.readouterr().err
    assert error.value.code == 2
    assert "is a command, not an option" in message
    assert "use 'fritzflux-cli doctor' without the '--' prefix" in message


def test_inventory_excludes_materialized_views_from_box_tables():
    admin = object.__new__(QuestDBAdmin)

    async def fake_query(sql):
        if "matView, partitionBy FROM tables()" in sql:
            return [
                ["fritzbox_AA1", False, "DAY"],
                ["fritzbox_AA1_rollup_1m_v1", True, "DAY"],
                ["unrelated", False, "DAY"],
                ["fritzbox_other_view", False, "NONE"],
            ]
        return [["fritzbox_AA1_rollup_1m_v1", "fritzbox_AA1", "valid", 4, 4]]

    admin.query = fake_query
    boxes, views = asyncio.run(admin.inventory())
    assert boxes == ["fritzbox_AA1"]
    assert views["fritzbox_AA1"][0][0] == "fritzbox_AA1_rollup_1m_v1"


def test_drop_box_is_preview_by_default_and_records_disabled_state_on_apply(capsys):
    admin = FakeAdmin(state=True)
    invoke(admin, "drop-box", "fritzbox_AA1")
    assert admin.queries == []
    assert "Preview only" in capsys.readouterr().out

    invoke(admin, "drop-box", "fritzbox_AA1", "--apply")
    assert admin.queries[0] == 'DROP MATERIALIZED VIEW "fritzbox_AA1_rollup_1m_v1";'
    assert admin.queries[1] == 'DROP TABLE "fritzbox_AA1";'
    assert "'fritzbox_AA1', 'disabled'" in admin.queries[-1]


@pytest.mark.parametrize("answer", ["no", ""])
def test_drop_box_apply_can_be_cancelled_at_confirmation(capsys, answer):
    admin = FakeAdmin(state=True)
    invoke(admin, "drop-box", "fritzbox_AA1", "--apply", answer=answer)

    assert admin.queries == []
    assert "Cancelled." in capsys.readouterr().out


def test_confirmation_prompt_accepts_yes(capsys):
    with patch("builtins.input", return_value="y") as input_mock:
        assert _confirm_action()

    input_mock.assert_called_once_with("Are you sure? [y/N]: ")
    assert capsys.readouterr().out == ""


def test_list_formats_inventory_as_aligned_table(capsys):
    invoke(FakeAdmin(), "list")

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Managed QuestDB objects (1 box table)"
    assert "BOX TABLE" in lines[1]
    assert "ROLLUP VIEW" in lines[1]
    assert "REFRESH TXN" in lines[1]
    assert len({len(line) for line in lines[1:]}) == 1
    assert "4 / 4" in lines[-1]


def test_list_shows_box_without_rollup_views(capsys):
    invoke(FakeAdmin(views={"fritzbox_AA1": []}), "list")

    lines = capsys.readouterr().out.splitlines()
    assert lines[-1].startswith("fritzbox_AA1")
    assert lines[-1].count("—") == 3


def test_doctor_keeps_connection_status_above_formatted_inventory(capsys):
    invoke(FakeAdmin(), "doctor")

    output = capsys.readouterr().out
    assert "QuestDB connection" in output
    assert "HTTP SQL read-only" in output
    assert "--apply and confirmation required" in output
    assert "BOX TABLE" in output


def test_drop_box_rejects_unknown_dependency_before_mutation():
    admin = FakeAdmin(views={"fritzbox_AA1": [("foreign_view", "valid", 1, 1)]})
    with pytest.raises(AdminError, match="Unknown dependent"):
        invoke(admin, "drop-box", "fritzbox_AA1", "--apply")
    assert admin.queries == []


def test_drop_partitions_only_selects_whole_days_and_refreshes_rollup(capsys):
    admin = FakeAdmin()
    invoke(admin, "drop-partitions", "fritzbox_AA1", "--from", "2026-01-01", "--to", "2026-01-02")
    assert admin.queries == []
    assert "10 rows" in capsys.readouterr().out

    invoke(admin, "drop-partitions", "fritzbox_AA1", "--from", "2026-01-01", "--to", "2026-01-02", "--apply")
    assert admin.queries == [
        'ALTER TABLE "fritzbox_AA1" DROP PARTITION LIST \'2026-01-01\';',
        'REFRESH MATERIALIZED VIEW "fritzbox_AA1_rollup_1m_v1" FULL;',
    ]


def test_drop_partitions_rejects_newest_and_non_day_partitions():
    admin = FakeAdmin()
    with pytest.raises(AdminError, match="newest"):
        invoke(admin, "drop-partitions", "fritzbox_AA1", "--from", "2026-01-02", "--to", "2026-01-03", "--apply")
    assert admin.queries == []

    admin.partition_rows = [("MONTH", "2026-01", 10)]
    with pytest.raises(AdminError, match="DAY"):
        invoke(admin, "drop-partitions", "fritzbox_AA1", "--from", "2026-01-01", "--to", "2026-02-01", "--apply")


def test_manual_range_refresh_targets_discovered_view():
    admin = FakeAdmin()
    invoke(admin, "refresh", "fritzbox_AA1", "--from", "2026-01-01", "--to", "2026-01-03", "--apply")
    assert admin.queries == [
        (
            'REFRESH MATERIALIZED VIEW "fritzbox_AA1_rollup_1m_v1" '
            "RANGE FROM '2026-01-01T00:00:00Z' TO '2026-01-03T00:00:00Z';"
        )
    ]
    with pytest.raises(AdminError, match="No matching"):
        invoke(admin, "refresh", "fritzbox_AA1", "--full", "--view", "other_view", "--apply")


def test_drop_all_only_drops_discovered_project_tables_and_state():
    admin = FakeAdmin(state=True)
    invoke(admin, "drop-all", "--apply")
    assert admin.queries == [
        "SELECT table_name FROM tables() WHERE table_name = '_fritzfluxdb_downsampling';",
        'DROP MATERIALIZED VIEW "fritzbox_AA1_rollup_1m_v1";',
        'DROP TABLE "fritzbox_AA1";',
        'DROP TABLE "_fritzfluxdb_downsampling";',
    ]
