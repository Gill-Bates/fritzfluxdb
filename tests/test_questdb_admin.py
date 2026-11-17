#!/usr/bin/env python3
#
# tests/test_questdb_admin.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import asyncio
import configparser
import os
import signal
from types import SimpleNamespace

import httpx
import pytest

from app.influxdb.config import InfluxDBConfig
from app.utils.questdb_admin import (
    AdminError,
    QuestDBAdmin,
    _parser,
    _run,
    main_async,
)

pytestmark = pytest.mark.usefixtures("no_database_env")


@pytest.fixture(autouse=True)
def daemon(tmp_path, monkeypatch):
    # The real PID file may name a running daemon; no --apply test may pause, stop or flag it.
    state = SimpleNamespace(pid_file=tmp_path / "daemon.pid", stop_flag=tmp_path / "daemon.stop", signals=[])
    monkeypatch.setattr("app.utils.questdb_admin._DAEMON_PID_FILE", state.pid_file)
    monkeypatch.setattr("app.utils.questdb_admin._DAEMON_STOP_FLAG", state.stop_flag)
    monkeypatch.setattr("app.utils.questdb_admin.os.killpg", lambda pid, sig: state.signals.append(sig))
    return state


class FakeAdmin:
    def __init__(self, *, boxes=None, views=None, partitions=None, state=False, fail_on=None):
        self.boxes = boxes or ["fritzbox_AA1_metrics"]
        self.views = views or {"fritzbox_AA1_metrics": [("fritzbox_AA1_metrics_rollup_1m_g00_v1", "valid", 4, 4)]}
        self.partition_rows = partitions or [("DAY", "2026-01-01", 10), ("DAY", "2026-01-02", 20)]
        self.state = state
        self.fail_on = fail_on
        self.queries = []
        self.confirmed_ids = {}
        self.ids_now = {}

    async def current_ids(self, names):
        return {name: self.ids_now.get(name) for name in names}

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
        if self.fail_on and self.fail_on in sql:
            raise AdminError("QuestDB rejected SQL: boom")
        if "FROM tables()" in sql:
            return [["_fritzfluxdb_downsampling"]] if self.state else []
        return []


def invoke(admin, *args):
    asyncio.run(_run(_parser().parse_args(args), admin))


def test_questdb_admin_uses_password_for_basic_auth(monkeypatch):
    parser = configparser.ConfigParser()
    parser["influxdb"] = {
        "version": "questdb",
        "hostname": "localhost",
        "username": "admin",
        "password": "secret",
    }
    config = InfluxDBConfig(parser)
    headers = []
    real_client = httpx.AsyncClient

    def client(**kwargs):
        return real_client(
            transport=httpx.MockTransport(
                lambda request: headers.append(request.headers["Authorization"])
                or httpx.Response(200)
            ),
            **kwargs,
        )

    monkeypatch.setattr("app.utils.questdb_admin.httpx.AsyncClient", client)

    async def request():
        async with QuestDBAdmin(config) as admin:
            await admin.client.get("/")

    asyncio.run(request())

    assert headers == ["Basic YWRtaW46c2VjcmV0"]


def test_cli_without_arguments_prints_help_and_exits_successfully(capsys):
    assert asyncio.run(main_async([])) == 0

    output = capsys.readouterr().out
    assert "usage:" in output
    assert "doctor" in output
    assert "drop-all" in output
    assert "Commands are positional words" in output
    assert "fritzflux-cli drop-box fritzbox_AA1234567890_metrics --apply" in output


@pytest.mark.parametrize("arguments", [["--doctor"], ["--doctor", "list"]])
def test_cli_explains_that_commands_do_not_take_option_prefix(arguments, capsys):
    with pytest.raises(SystemExit) as error:
        asyncio.run(main_async(arguments))

    message = capsys.readouterr().err
    assert error.value.code == 2
    assert "is a command, not an option" in message
    assert "use 'fritzflux-cli doctor' without the '--' prefix" in message


def inventory_admin(tables, state_rows, views=()):
    """Admin with tables() rows [name, matView, partitionBy, id] and state rows [measurement, table_id]."""
    admin = object.__new__(QuestDBAdmin)

    async def fake_query(sql):
        if "matView, partitionBy, id FROM tables()" in sql:
            return tables
        if "materialized_views()" in sql:
            return list(views)
        if "LATEST ON timestamp PARTITION BY measurement" in sql:
            if state_rows is None:
                raise AdminError("QuestDB rejected SQL: Invalid column: table_id")
            return state_rows
        if "table_columns('fritzbox_archive_metrics')" in sql:
            return [["timestamp"], ["customer"], ["amount"]]
        if "table_columns" in sql:
            return [["timestamp"], ["box"], ["message"]]
        return []

    admin.query = fake_query
    return admin


STATE = ["_fritzfluxdb_downsampling", False, "DAY", 1]


def test_inventory_excludes_materialized_views_from_box_tables():
    admin = inventory_admin(
        [
            ["fritzbox_AA1_metrics", False, "DAY", 10],
            ["fritzbox_AA1_metrics_rollup_1m_g00_v1", True, "DAY", 11],
            ["unrelated", False, "DAY", 12],
            ["fritzbox_other_view", False, "NONE", 13],
            STATE,
        ],
        [["fritzbox_AA1_metrics", 10]],
    )
    boxes, _ = asyncio.run(admin.inventory())
    assert boxes == ["fritzbox_AA1_metrics"]


def test_inventory_ignores_foreign_table_that_only_shares_the_name_prefix():
    admin = inventory_admin(
        [["fritzbox_AA1_metrics", False, "DAY", 10], ["fritzbox_archive_metrics", False, "DAY", 11], STATE],
        [["fritzbox_AA1_metrics", 10], ["fritzbox_archive_metrics", 11]],
    )
    boxes, _ = asyncio.run(admin.inventory())
    assert boxes == ["fritzbox_AA1_metrics"]


def test_inventory_requires_an_application_written_record_not_only_matching_columns():
    tables = [["fritzbox_AA1_metrics", False, "DAY", 10], ["fritzbox_foreign_metrics", False, "DAY", 11]]
    boxes, _ = asyncio.run(inventory_admin([*tables, STATE], [["fritzbox_AA1_metrics", 10]]).inventory())
    assert boxes == ["fritzbox_AA1_metrics"]
    boxes, _ = asyncio.run(inventory_admin(tables, []).inventory())
    assert boxes == []


def test_inventory_groups_metrics_events_and_rollups_per_box_without_mixing_boxes():
    boxes_rows = [
        ["fritzbox_AA1_metrics", False, "DAY", 10],
        ["fritzbox_AA1_events", False, "DAY", 11],
        ["fritzbox_AA1_B_metrics", False, "DAY", 12],
        ["fritzbox_AA1_B_events", False, "DAY", 13],
        ["fritzbox_AA1_metrics_rollup_1m_g00_v1", True, "DAY", 14],
        ["fritzbox_AA1_B_metrics_rollup_1m_g00_v1", True, "DAY", 15],
        STATE,
    ]
    views = [
        ["fritzbox_AA1_metrics_rollup_1m_g00_v1", "fritzbox_AA1_metrics", "valid", 1, 1],
        ["fritzbox_AA1_B_metrics_rollup_1m_g00_v1", "fritzbox_AA1_B_metrics", "valid", 2, 2],
    ]
    # Even a status row for an events table must not make it a managed box table.
    state = [["fritzbox_AA1_metrics", 10], ["fritzbox_AA1_B_metrics", 12], ["fritzbox_AA1_events", 11]]
    boxes, by_base = asyncio.run(inventory_admin(boxes_rows, state, views).inventory())
    assert boxes == ["fritzbox_AA1_B_metrics", "fritzbox_AA1_metrics"]
    assert [v[0] for v in by_base["fritzbox_AA1_metrics"]] == ["fritzbox_AA1_metrics_rollup_1m_g00_v1"]
    assert [v[0] for v in by_base["fritzbox_AA1_B_metrics"]] == ["fritzbox_AA1_B_metrics_rollup_1m_g00_v1"]


def test_box_table_pattern_accepts_only_metrics_tables():
    from app.utils import questdb_admin
    assert questdb_admin._BOX_TABLE.fullmatch("fritzbox_50E63634FEA8_metrics")
    assert questdb_admin._BOX_TABLE.fullmatch("fritzbox_metrics")
    for name in (
        "fritzbox_50E63634FEA8", "fritzbox_50E63634FEA8_events", "fritzbox_50E63634FEA8_metrics_events",
        "fritzbox_50E63634FEA8_metrics_rollup_1m_g00_v1", "other_50E63634FEA8_metrics",
    ):
        assert not questdb_admin._BOX_TABLE.fullmatch(name), name


def test_require_box_rejects_an_events_table_name():
    admin = inventory_admin([["fritzbox_AA1_events", False, "DAY", 11], STATE], [["fritzbox_AA1_events", 11]])
    with pytest.raises(AdminError, match="box table"):
        asyncio.run(admin.require_box("fritzbox_AA1_events"))


def test_inventory_rejects_a_foreign_table_recreated_under_a_managed_name():
    # The status row survives DROP TABLE; the re-created table has a new QuestDB id.
    admin = inventory_admin([["fritzbox_AA1_metrics", False, "DAY", 99], STATE], [["fritzbox_AA1_metrics", 10]])
    boxes, _ = asyncio.run(admin.inventory())
    assert boxes == []


def test_inventory_ignores_status_rows_without_table_id():
    tables = [["fritzbox_AA1_metrics", False, "DAY", 10], STATE]
    boxes, _ = asyncio.run(inventory_admin(tables, [["fritzbox_AA1_metrics", None]]).inventory())
    assert boxes == []


def test_inventory_reports_an_unreadable_status_table():
    tables = [["fritzbox_AA1_metrics", False, "DAY", 10], STATE]
    with pytest.raises(AdminError, match="table_id"):
        asyncio.run(inventory_admin(tables, None).inventory())


@pytest.mark.parametrize("dataset", ["rows", {"a": 1}, [1, 2], [["ok"], "bad"]])
def test_query_rejects_an_invalid_dataset(dataset):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"dataset": dataset}

    class Client:
        async def get(self, *_, **__):
            return Response()

    admin = object.__new__(QuestDBAdmin)
    admin.client = Client()
    with pytest.raises(AdminError, match="invalid dataset"):
        asyncio.run(admin.query("SELECT 1;"))


def test_inventory_rejects_rows_with_unexpected_columns():
    admin = object.__new__(QuestDBAdmin)

    async def fake_query(sql):
        return [["fritzbox_AA1_metrics"]]

    admin.query = fake_query
    with pytest.raises(AdminError, match="unexpected columns"):
        asyncio.run(admin.inventory())


def test_drop_box_is_preview_by_default_and_records_disabled_state_on_apply(capsys):
    admin = FakeAdmin(state=True)
    invoke(admin, "drop-box", "fritzbox_AA1_metrics")
    assert admin.queries == []
    assert "Preview only" in capsys.readouterr().out

    invoke(admin, "drop-box", "fritzbox_AA1_metrics", "--apply")
    assert "FROM tables()" in admin.queries[0]
    assert admin.queries[1] == 'DROP MATERIALIZED VIEW "fritzbox_AA1_metrics_rollup_1m_g00_v1";'
    assert admin.queries[2] == 'DROP TABLE "fritzbox_AA1_metrics";'
    assert "'fritzbox_AA1_metrics', 'disabled'" in admin.queries[-1]


def test_drop_box_reports_the_partial_state_when_the_second_view_drop_fails():
    views = {"fritzbox_AA1_metrics": [
        ("fritzbox_AA1_metrics_rollup_1m_g00_v1", "valid", 4, 4),
        ("fritzbox_AA1_metrics_rollup_1h_g00_v1", "valid", 4, 4),
    ]}
    admin = FakeAdmin(views=views, fail_on='"fritzbox_AA1_metrics_rollup_1h_g00_v1"')
    with pytest.raises(AdminError) as error:
        invoke(admin, "drop-box", "fritzbox_AA1_metrics", "--apply")

    message = str(error.value)
    assert "Completed before the failure: DROP MATERIALIZED VIEW fritzbox_AA1_metrics_rollup_1m_g00_v1" in message
    assert "Not executed: DROP MATERIALIZED VIEW fritzbox_AA1_metrics_rollup_1h_g00_v1; DROP TABLE fritzbox_AA1_metrics" in message
    assert "not atomic" in message
    assert not any(query.startswith("DROP TABLE") for query in admin.queries)


def test_drop_box_reports_a_failed_state_insert_after_the_table_is_gone():
    admin = FakeAdmin(state=True, fail_on="INSERT INTO")
    with pytest.raises(AdminError) as error:
        invoke(admin, "drop-box", "fritzbox_AA1_metrics", "--apply")

    message = str(error.value)
    assert "DROP TABLE fritzbox_AA1_metrics" in message.split("Not executed")[0]
    assert "Not executed: record disabled downsampling state for fritzbox_AA1_metrics" in message


def test_drop_partitions_reports_dropped_and_remaining_partitions_on_failure():
    admin = FakeAdmin(
        partitions=[("DAY", "2026-01-01", 1), ("DAY", "2026-01-02", 2), ("DAY", "2026-01-03", 3)],
        fail_on="'2026-01-02'",
    )
    with pytest.raises(AdminError) as error:
        invoke(admin, "drop-partitions", "fritzbox_AA1_metrics", "--from", "2026-01-01", "--to", "2026-01-03", "--apply")

    message = str(error.value)
    assert "Completed before the failure: DROP PARTITION 2026-01-01; REFRESH fritzbox_AA1_metrics_rollup_1m_g00_v1" in message
    assert "Not executed: DROP PARTITION 2026-01-02" in message


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
    invoke(FakeAdmin(views={"fritzbox_AA1_metrics": []}), "list")

    lines = capsys.readouterr().out.splitlines()
    assert lines[-1].startswith("fritzbox_AA1_metrics")
    assert lines[-1].count("—") == 3


def test_doctor_keeps_connection_status_above_formatted_inventory(capsys):
    invoke(FakeAdmin(), "doctor")

    output = capsys.readouterr().out
    assert "QuestDB connection" in output
    assert "HTTP SQL read-only" in output
    assert "--apply required" in output
    assert "BOX TABLE" in output


def test_drop_box_rejects_unknown_dependency_before_mutation():
    admin = FakeAdmin(views={"fritzbox_AA1_metrics": [("foreign_view", "valid", 1, 1)]})
    with pytest.raises(AdminError, match="Unknown dependent"):
        invoke(admin, "drop-box", "fritzbox_AA1_metrics", "--apply")
    assert admin.queries == []


def test_drop_partitions_only_selects_whole_days_and_refreshes_rollup(capsys):
    admin = FakeAdmin()
    invoke(admin, "drop-partitions", "fritzbox_AA1_metrics", "--from", "2026-01-01", "--to", "2026-01-02")
    assert admin.queries == []
    assert "10 rows" in capsys.readouterr().out

    invoke(admin, "drop-partitions", "fritzbox_AA1_metrics", "--from", "2026-01-01", "--to", "2026-01-02", "--apply")
    assert admin.queries == [
        'ALTER TABLE "fritzbox_AA1_metrics" DROP PARTITION LIST \'2026-01-01\';',
        'REFRESH MATERIALIZED VIEW "fritzbox_AA1_metrics_rollup_1m_g00_v1" FULL;',
    ]


def test_drop_partitions_rejects_newest_and_non_day_partitions():
    admin = FakeAdmin()
    with pytest.raises(AdminError, match="newest"):
        invoke(admin, "drop-partitions", "fritzbox_AA1_metrics", "--from", "2026-01-02", "--to", "2026-01-03", "--apply")
    assert admin.queries == []

    admin.partition_rows = [("MONTH", "2026-01", 10)]
    with pytest.raises(AdminError, match="DAY"):
        invoke(admin, "drop-partitions", "fritzbox_AA1_metrics", "--from", "2026-01-01", "--to", "2026-02-01", "--apply")


def test_manual_range_refresh_targets_discovered_view():
    admin = FakeAdmin()
    invoke(admin, "refresh", "fritzbox_AA1_metrics", "--from", "2026-01-01", "--to", "2026-01-03", "--apply")
    assert admin.queries == [
        (
            'REFRESH MATERIALIZED VIEW "fritzbox_AA1_metrics_rollup_1m_g00_v1" '
            "RANGE FROM '2026-01-01T00:00:00Z' TO '2026-01-03T00:00:00Z';"
        )
    ]
    with pytest.raises(AdminError, match="No matching"):
        invoke(admin, "refresh", "fritzbox_AA1_metrics", "--full", "--view", "other_view", "--apply")


def test_drop_all_only_drops_discovered_project_tables_and_state():
    admin = FakeAdmin(state=True)
    invoke(admin, "drop-all", "--apply")
    assert admin.queries == [
        "SELECT table_name FROM tables() WHERE table_name = '_fritzfluxdb_downsampling';",
        'DROP MATERIALIZED VIEW "fritzbox_AA1_metrics_rollup_1m_g00_v1";',
        'DROP TABLE "fritzbox_AA1_metrics";',
        'DROP TABLE "_fritzfluxdb_downsampling";',
    ]


class DroppedTableAdmin(FakeAdmin):
    """Status row of an interrupted drop-box; the metrics table is gone unless ``table_exists``."""

    def __init__(self, *, latest_enabled=True, table_exists=False):
        super().__init__()
        self.boxes = []
        self.latest_enabled = latest_enabled
        self.table_exists = table_exists

    async def query(self, sql):
        self.queries.append(sql)
        if "table_name = 'fritzbox_AA1_metrics'" in sql:
            return [["fritzbox_AA1_metrics"]] if self.table_exists else []
        if "table_name = '_fritzfluxdb_downsampling'" in sql:
            return [["_fritzfluxdb_downsampling"]]
        if "LATEST ON" in sql:
            return [[self.latest_enabled]]
        return []


def test_drop_box_can_record_the_state_after_the_table_is_already_gone(capsys):
    admin = DroppedTableAdmin()
    invoke(admin, "drop-box", "fritzbox_AA1_metrics")
    assert not any(query.startswith("INSERT INTO") for query in admin.queries)
    assert "Preview only" in capsys.readouterr().out

    invoke(admin, "drop-box", "fritzbox_AA1_metrics", "--apply")
    assert "'fritzbox_AA1_metrics', 'disabled'" in admin.queries[-1]
    assert not any(query.startswith("DROP") for query in admin.queries)


@pytest.mark.parametrize("options", [{"latest_enabled": False}, {"table_exists": True}])
def test_drop_box_state_repair_is_limited_to_an_active_state_of_a_missing_table(options):
    admin = DroppedTableAdmin(**options)
    with pytest.raises(AdminError, match="not a managed table"):
        invoke(admin, "drop-box", "fritzbox_AA1_metrics", "--apply")
    assert not any(query.startswith("INSERT INTO") for query in admin.queries)


def test_every_rollup_view_name_matches_the_cli_naming_rules():
    from app.influxdb.config import QUESTDB_DOWNSAMPLING_PROFILES
    from app.influxdb.handler import questdb_rollup_groups, questdb_rollup_view_name
    from app.utils import questdb_admin

    for profile in QUESTDB_DOWNSAMPLING_PROFILES.values():
        for group in questdb_rollup_groups():
            view = questdb_rollup_view_name("fritzbox_AA1_metrics", profile, group)
            assert questdb_admin._ROLLUP_VIEW.fullmatch(view)
            assert view.startswith("fritzbox_AA1_metrics_rollup_")
        # A view without a group is not a managed object.
        assert not questdb_admin._ROLLUP_VIEW.fullmatch(f"fritzbox_AA1_metrics_rollup_{profile.rollup_interval}_v1")


def test_drop_box_aborts_when_a_table_was_recreated_after_the_preview():
    admin = FakeAdmin()
    admin.confirmed_ids = {"fritzbox_AA1_metrics": 10, "fritzbox_AA1_metrics_rollup_1m_g00_v1": 11}
    admin.ids_now = {"fritzbox_AA1_metrics": 99, "fritzbox_AA1_metrics_rollup_1m_g00_v1": 11}
    with pytest.raises(AdminError, match="changed after the preview"):
        invoke(admin, "drop-box", "fritzbox_AA1_metrics", "--apply")
    assert not any(sql.startswith("DROP") for sql in admin.queries)


def test_drop_box_proceeds_when_ids_are_unchanged_after_the_preview():
    admin = FakeAdmin()
    admin.confirmed_ids = admin.ids_now = {"fritzbox_AA1_metrics": 10, "fritzbox_AA1_metrics_rollup_1m_g00_v1": 11}
    invoke(admin, "drop-box", "fritzbox_AA1_metrics", "--apply")
    assert any(sql.startswith("DROP TABLE") for sql in admin.queries)


def test_inventory_reads_the_state_table_without_altering_it():
    queries = []
    admin = inventory_admin([["fritzbox_AA1_metrics", False, "DAY", 10], STATE], [["fritzbox_AA1_metrics", 10]])
    inner = admin.query
    async def recording(sql):
        queries.append(sql)
        return await inner(sql)
    admin.query = recording
    boxes, _ = asyncio.run(admin.inventory())
    assert boxes == ["fritzbox_AA1_metrics"]
    assert not any(sql.startswith("ALTER TABLE") for sql in queries)
    assert admin.confirmed_ids["fritzbox_AA1_metrics"] == 10


@pytest.mark.parametrize("command", [("drop-all",), ("drop-box", "fritzbox_AA1_metrics")])
def test_destructive_drops_pause_then_stop_the_daemon(daemon, command):
    daemon.pid_file.write_text(str(os.getppid()))
    admin = FakeAdmin(state=True)

    invoke(admin, *command, "--apply")

    assert daemon.signals == [signal.SIGSTOP, signal.SIGCONT, signal.SIGTERM]
    assert daemon.stop_flag.exists()
    assert any(sql.startswith("DROP TABLE") for sql in admin.queries)


def test_daemon_is_resumed_when_the_drop_fails(daemon):
    daemon.pid_file.write_text(str(os.getppid()))
    admin = FakeAdmin(state=True, fail_on="DROP")

    with pytest.raises(AdminError):
        invoke(admin, "drop-all", "--apply")

    assert daemon.signals == [signal.SIGSTOP, signal.SIGCONT]
    assert not daemon.stop_flag.exists()


def test_drop_all_ignores_a_pid_file_that_names_the_cli_itself(daemon):
    daemon.pid_file.write_text(str(os.getpid()))
    admin = FakeAdmin(state=True)

    invoke(admin, "drop-all", "--apply")

    assert any(sql.startswith("DROP TABLE") for sql in admin.queries)
    assert daemon.signals == []
    assert not daemon.stop_flag.exists()


def _query_through(handler):
    """Run one admin query over a mock transport and close the client on every path."""
    async def run():
        admin = object.__new__(QuestDBAdmin)
        async with httpx.AsyncClient(base_url="http://db", transport=httpx.MockTransport(handler)) as client:
            admin.client = client
            await admin.query("SELECT 1;")

    asyncio.run(run())


@pytest.mark.parametrize(("status", "config_error"), [(401, True), (403, True), (500, False), (404, False)])
def test_query_classifies_rejected_credentials_as_a_configuration_error(status, config_error):
    from app.utils.questdb_admin import AdminConfigError

    with pytest.raises(AdminError) as error:
        _query_through(lambda request: httpx.Response(status))
    assert isinstance(error.value, AdminConfigError) is config_error


def test_unreachable_database_is_a_plain_failure_not_a_configuration_error():
    from app.utils.questdb_admin import AdminConfigError

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(AdminError) as error:
        _query_through(refuse)
    assert not isinstance(error.value, AdminConfigError)


@pytest.mark.parametrize(("status", "code"), [(401, 78), (403, 78), (503, 1)])
def test_cli_exit_code_follows_the_contract(monkeypatch, status, code, capsys):
    monkeypatch.setenv("DB_TYPE", "questdb")
    monkeypatch.setenv("QUESTDB_HOSTNAME", "db")
    real_client = httpx.AsyncClient

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(lambda request: httpx.Response(status)), **kwargs)

    monkeypatch.setattr("app.utils.questdb_admin.httpx.AsyncClient", client)
    assert asyncio.run(main_async(["doctor"])) == code
    capsys.readouterr()


def test_cli_partial_failure_keeps_the_configuration_error_class():
    from app.utils.questdb_admin import AdminConfigError, _partial_failure

    assert isinstance(_partial_failure(AdminConfigError("denied"), ["a"], ["b"]), AdminConfigError)


def test_doctor_rejects_a_questdb_older_than_version_10_with_exit_78(monkeypatch, capsys):
    from app.utils.questdb_admin import AdminConfigError

    class OldAdmin(FakeAdmin):
        async def query(self, sql):
            if "build()" in sql:
                return [["Build Information: QuestDB 9.2.1, JDK 21"]]
            return await super().query(sql)

    with pytest.raises(AdminConfigError, match="version 10 or newer"):
        invoke(OldAdmin(), "doctor")
    capsys.readouterr()


@pytest.mark.parametrize("build", ["Build Information: QuestDB 10.0.1, JDK 25", "something else"])
def test_doctor_accepts_questdb_10_and_an_unreadable_build_string(build, capsys):
    class NewAdmin(FakeAdmin):
        async def query(self, sql):
            if "build()" in sql:
                return [[build]]
            return await super().query(sql)

    invoke(NewAdmin(), "doctor")

    output = capsys.readouterr().out
    assert f"OK ({build})" in output
    assert "BOX TABLE" in output
