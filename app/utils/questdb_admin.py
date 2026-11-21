#!/usr/bin/env python3
#
# app/utils/questdb_admin.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Administrative QuestDB commands for the FritzFluxDB container."""

import argparse
import asyncio
import configparser
import contextlib
import os
import re
import signal
import sys
from argparse import RawDescriptionHelpFormatter
from datetime import date
from pathlib import Path

import httpx
from gillbates_questdb_client import (
    AsyncQuestDBClient,
    QuestDBConfig,
    QuestDBConfigError,
    QuestDBError,
    QuestDBQueryError,
    format_exception,
    parse_version,
    quote_ident,
    quote_literal,
)

from app.influxdb.config import import (
    QUESTDB_DOWNSAMPLING_SCHEMA_VERSION,
    QUESTDB_DOWNSAMPLING_STATE_TABLE,
    InfluxDBConfig,
)
from app.influxdb.handler import (
    _QUESTDB_TAG_COLUMNS,
    QUESTDB_METRICS_TABLE_SUFFIX,
    QUESTDB_MINIMUM_MAJOR_VERSION,
    questdb_expected_columns,
)
from app.utils.settings import reveal

# Written by docker/entrypoint.sh while the daemon runs.
_DAEMON_PID_FILE = Path("/tmp/fritzfluxdb.pid")
_DAEMON_STOP_FLAG = Path("/tmp/fritzfluxdb.stop")
# Only the metrics table is a box table; `_events` tables and rollup views never match this.
_BOX_TABLE = re.compile(rf"fritzbox(?:_[A-Za-z0-9_]+)?{re.escape(QUESTDB_METRICS_TABLE_SUFFIX)}\Z")
_ROLLUP_VIEW = re.compile(r".+_rollup_[0-9]+[mhd]_g[0-9]{2}_v[0-9]+\Z")
_STATE_TABLE_SQL = (
    "SELECT table_name FROM tables() "
    f"WHERE table_name = {quote_literal(QUESTDB_DOWNSAMPLING_STATE_TABLE)};"
)


class AdminError(Exception):
    """An expected configuration, metadata, or QuestDB operation failure."""


class AdminConfigError(AdminError):
    """QuestDB rejects the credentials or is unsupported; repeating the command cannot help (exit 78)."""


class QuestDBAdmin:
    def __init__(self, config: InfluxDBConfig):
        self.confirmed_ids: dict[str, object] = {}
        self.questdb = AsyncQuestDBClient(
            QuestDBConfig(
                host=config.hostname,
                port=config.port,
                https=config.tls_enabled,
                username=config.username,
                password=reveal(config.password),
                verify_certificate=config.verify_tls,
                timeout=20,
            ),
            minimum_major=QUESTDB_MINIMUM_MAJOR_VERSION,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.questdb.aclose()

    async def query(self, sql: str) -> list[list]:
        """Run one statement; the single place where package errors become admin errors (78 vs 1)."""
        try:
            return await self.questdb.execute(sql)
        except QuestDBConfigError as exc:
            raise AdminConfigError(str(exc)) from exc
        except QuestDBQueryError as exc:
            raise AdminError(f"QuestDB rejected SQL: {exc}") from exc
        except (httpx.HTTPError, QuestDBError) as exc:
            raise AdminError(f"QuestDB request failed: {format_exception(exc)}") from exc

    async def current_ids(self, names: list[str]) -> dict[str, object]:
        """QuestDB ids of the named tables or views right now; None for a missing object."""
        try:
            return await self.questdb.table_ids(names)
        except QuestDBConfigError as exc:
            raise AdminConfigError(str(exc)) from exc
        except (httpx.HTTPError, QuestDBError) as exc:
            raise AdminError(f"QuestDB request failed: {format_exception(exc)}") from exc
    async def is_fritzfluxdb_table(self, table: str) -> bool:
        """Check the table schema as a supplement to the ownership record.

        A FritzFluxDB box table carries the `box` tag plus at least one metric column
        the application writes. This does not prove ownership on its own; inventory()
        additionally requires the status row the application writes for the table.
        """
        rows = await self.query(f"SELECT \"column\" FROM table_columns({quote_literal(table)});")
        columns = {str(row[0]) for row in rows if row}
        metric_columns = set(questdb_expected_columns()) - set(_QUESTDB_TAG_COLUMNS)
        return "box" in columns and bool(columns & metric_columns)

    async def inventory(self) -> tuple[list[str], dict[str, list[tuple[str, str, int | None, int | None]]]]:
        tables = await self.query("SELECT table_name, matView, partitionBy, id FROM tables();")
        views = await self.query(
            "SELECT view_name, base_table_name, view_status, refresh_base_table_txn, "
            "base_table_txn FROM materialized_views();"
        )
        if any(len(row) < 4 for row in tables) or any(len(row) < 5 for row in views):
            raise AdminError("QuestDB returned rows with unexpected columns")
        candidates = sorted(
            str(row[0]) for row in tables
            if not row[1] and row[2] == "DAY" and _BOX_TABLE.fullmatch(str(row[0]))
            and not _ROLLUP_VIEW.fullmatch(str(row[0]))
        )
        # Destructive commands act on this list: the latest status row must name this table
        # instance (QuestDB id), so a foreign table re-created under the name is not adopted.
        table_ids = {str(row[0]): row[3] for row in tables}
        self.confirmed_ids = table_ids
        recorded: dict[str, object] = {}
        if QUESTDB_DOWNSAMPLING_STATE_TABLE in table_ids:
            state_rows = await self.query(
                "SELECT measurement, table_id FROM "
                f"{quote_ident(QUESTDB_DOWNSAMPLING_STATE_TABLE)} "
                "LATEST ON timestamp PARTITION BY measurement;"
            )
            recorded = {str(row[0]): row[1] for row in state_rows if len(row) > 1}
        boxes = [
            table for table in candidates
            if recorded.get(table) is not None and recorded[table] == table_ids[table]
            and await self.is_fritzfluxdb_table(table)
        ]
        by_base: dict[str, list[tuple[str, str, int | None, int | None]]] = {box: [] for box in boxes}
        for name, base, status, refreshed, latest, *_ in views:
            if str(base) in by_base:
                by_base[str(base)].append((str(name), str(status), refreshed, latest))
        for entries in by_base.values():
            entries.sort()
        return boxes, by_base

    async def require_box(self, table: str):
        if not _BOX_TABLE.fullmatch(table):
            raise AdminError("Table must be a FritzFluxDB box table (fritzbox_<serial>_metrics)")
        boxes, views = await self.inventory()
        if table not in boxes:
            raise AdminError(f"Managed box table {table!r} does not exist")
        return views[table]

    async def partitions(self, table: str) -> list[tuple[str, str, int]]:
        await self.require_box(table)
        try:
            return await self.questdb.partitions(table)
        except QuestDBConfigError as exc:
            raise AdminConfigError(str(exc)) from exc
        except (httpx.HTTPError, QuestDBError) as exc:
            raise AdminError(f"QuestDB request failed: {format_exception(exc)}") from exc


def _partial_failure(exc: AdminError, done: list[str], remaining: list[str]) -> AdminError:
    """Describe the state a failed multi-statement operation left behind.

    QuestDB DDL statements are not atomic across a command, so the caller must see which
    steps already took effect and which did not.
    """
    lines = [str(exc)]
    if done:
        lines.append("Completed before the failure: " + "; ".join(done))
    if remaining:
        lines.append("Not executed: " + "; ".join(remaining))
    lines.append(
        "The operation is not atomic. Run `fritzflux-cli list` to inspect the current state "
        "and repeat the command for what is left."
    )
    # Keep the class so a rejected credential mid-operation still maps to exit 78.
    error = type(exc)("\n".join(lines))
    error.__cause__ = exc
    return error


def _date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD")
    return parsed


def _daemon_pid() -> int | None:
    """Return the running daemon's PID, ignoring this CLI when it is the container command."""
    try:
        pid = int(_DAEMON_PID_FILE.read_text().strip())
        if pid == os.getpid():
            return None
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    return pid


@contextlib.contextmanager
def _daemon_halted():
    """Freeze the daemon so it cannot re-create dropped tables; stop it afterwards, resume on error."""
    pid = _daemon_pid()
    if pid is None:
        yield
        return
    # The entrypoint starts the daemon with setsid, so its PID is also the process group.
    os.killpg(pid, signal.SIGSTOP)
    print("Daemon paused while dropping")
    try:
        yield
    except BaseException:
        os.killpg(pid, signal.SIGCONT)
        print("Daemon resumed")
        raise
    # The flag parks the watchdog, so the container stays up and nothing re-creates the tables.
    # A stopped process only handles SIGTERM after SIGCONT.
    _DAEMON_STOP_FLAG.touch()
    os.killpg(pid, signal.SIGCONT)
    os.killpg(pid, signal.SIGTERM)
    print("Daemon stopped; restart the container to start it again with a fresh schema")


async def _ensure_unchanged(admin: QuestDBAdmin, names: list[str]) -> None:
    """Abort if a previewed object was dropped or re-created since the inventory."""
    expected = {name: admin.confirmed_ids.get(name) for name in names}
    if await admin.current_ids(names) != expected:
        raise AdminError("A listed table or view changed after the preview; nothing was changed")


def _print_inventory(boxes: list[str], views: dict[str, list[tuple]]) -> None:
    """Print managed tables and rollup status in aligned columns."""
    if not boxes:
        print("No managed box tables found.")
        return

    rows = []
    for box in boxes:
        box_views = views.get(box, [])
        if not box_views:
            rows.append((box, "—", "—", "—"))
            continue
        for name, status, refreshed, latest in box_views:
            refresh_txn = f"{refreshed if refreshed is not None else '—'} / {latest if latest is not None else '—'}"
            rows.append((box, name, status, refresh_txn))

    headers = ("BOX TABLE", "ROLLUP VIEW", "STATUS", "REFRESH TXN")
    widths = [max(len(header), *(len(row[index]) for row in rows)) for index, header in enumerate(headers)]
    print(f"Managed QuestDB objects ({len(boxes)} box table{'s' if len(boxes) != 1 else ''})")
    print(" | ".join(header.ljust(width) for header, width in zip(headers, widths, strict=True)))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(" | ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "QuestDB Open Source administration for FritzFluxDB. Commands are positional words "
            "without a leading '--'; options such as --apply follow the command."
        ),
        epilog=(
            "Examples:\n"
            "  fritzflux-cli doctor\n"
            "  fritzflux-cli list\n"
            "  fritzflux-cli drop-box fritzbox_AA1234567890_metrics           # preview\n"
            "  fritzflux-cli drop-box fritzbox_AA1234567890_metrics --apply # execute\n"
            "  fritzflux-cli drop-partitions fritzbox_AA1234567890_metrics --from 2026-09-01 --to 2026-09-08\n"
            "  fritzflux-cli refresh fritzbox_AA1234567890_metrics --full\n\n"
            "Password changes, API tokens, and row-level device deletion are unsupported."
        ),
        formatter_class=RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check the SQL connection and show managed objects")
    commands.add_parser("list", help="List managed box tables and their materialized views")
    drop_all = commands.add_parser("drop-all", help="Preview or drop all FritzFluxDB tables and rollups")
    drop_all.add_argument("--apply", action="store_true", help="Execute the previewed changes")
    partitions = commands.add_parser("partitions", help="List partitions for a managed box table")
    partitions.add_argument("table")
    drop_box = commands.add_parser("drop-box", help="Preview or drop a box table and its rollup views")
    drop_box.add_argument("table")
    drop_box.add_argument("--apply", action="store_true", help="Execute the previewed changes")
    drop = commands.add_parser("drop-partitions", help="Preview or drop whole DAY partitions in a UTC date range")
    drop.add_argument("table")
    drop.add_argument("--from", dest="start", required=True, type=_date, help="First UTC day, inclusive")
    drop.add_argument("--to", dest="end", required=True, type=_date, help="Last UTC day, exclusive")
    drop.add_argument("--apply", action="store_true", help="Execute the previewed changes")
    refresh = commands.add_parser("refresh", help="Preview or trigger a materialized view refresh")
    refresh.add_argument("table", help="Base box table")
    mode = refresh.add_mutually_exclusive_group(required=True)
    mode.add_argument("--full", action="store_true", help="Rebuild the full view")
    mode.add_argument("--from", dest="start", type=_date, help="First UTC day, inclusive; requires --to")
    refresh.add_argument("--to", dest="end", type=_date, help="Last UTC day, exclusive")
    refresh.add_argument("--view", help="One discovered rollup view; defaults to all views for the table")
    refresh.add_argument("--apply", action="store_true", help="Queue the refresh")
    return parser


async def _run(args: argparse.Namespace, admin: QuestDBAdmin) -> None:
    if args.command in {"doctor", "list"}:
        if args.command == "doctor":
            build = await admin.query("SELECT build();")
            print(f"{'QuestDB connection':<23} OK ({build[0][0] if build and build[0] else 'version unknown'})")
            _require_supported_version(build)
            try:
                parameters = await admin.query(
                    "(SHOW PARAMETERS) WHERE property_path = 'http.security.readonly';"
                )
            except AdminError:
                parameters = []
            if parameters:
                read_only_mode = parameters[0][2]
            else:
                read_only_mode = "unknown"
            print(f"{'HTTP SQL read-only':<23} {read_only_mode}")
            print(f"{'Mutations':<23} --apply required")
        boxes, views = await admin.inventory()
        _print_inventory(boxes, views)
        return

    if args.command == "drop-all":
        boxes, all_views = await admin.inventory()
        for box in boxes:
            _require_managed_views(box, all_views[box])
        for box in boxes:
            for name, *_ in all_views[box]:
                print(f"DROP MATERIALIZED VIEW {name}")
            print(f"DROP TABLE {box}")
        state = await admin.query(_STATE_TABLE_SQL)
        if state:
            print(f"DROP TABLE {QUESTDB_DOWNSAMPLING_STATE_TABLE}")
        if not boxes and not state:
            print("No FritzFluxDB tables found")
            return
        if not args.apply:
            print("Preview only; pass --apply to execute")
            return
        with _daemon_halted():
            names = [QUESTDB_DOWNSAMPLING_STATE_TABLE] if state else []
            for box in boxes:
                names += [box, *(view[0] for view in all_views[box])]
            await _ensure_unchanged(admin, names)
            done: list[str] = []
            planned = [f"box table {box}" for box in boxes]
            if state:
                planned.append(f"state table {QUESTDB_DOWNSAMPLING_STATE_TABLE}")
            try:
                for box in boxes:
                    await _drop_box(admin, box, all_views[box], record_disabled=False)
                    done.append(f"box table {box}")
                if state:
                    await admin.query(f"DROP TABLE {quote_ident(QUESTDB_DOWNSAMPLING_STATE_TABLE)};")
            except AdminError as exc:
                raise _partial_failure(exc, done, [item for item in planned if item not in done]) from exc
            print("FritzFluxDB tables and rollups dropped")
        return

    if args.command == "partitions":
        for kind, name, count in await admin.partitions(args.table):
            print(f"{name}: {count} rows ({kind})")
        return

    try:
        views = await admin.require_box(args.table)
    except AdminError:
        if args.command != "drop-box" or not await _state_repair_pending(admin, args.table):
            raise
        print(f"Box table {args.table} is already dropped, but its downsampling state is still active")
        print(f"RECORD disabled downsampling state for {args.table}")
        if not args.apply:
            print("Preview only; pass --apply to execute")
            return
        await admin.query(_disabled_state_sql(args.table))
        print("Downsampling state disabled")
        return
    if args.command == "drop-box":
        _require_managed_views(args.table, views)
        for name, *_ in views:
            print(f"DROP MATERIALIZED VIEW {name}")
        print(f"DROP TABLE {args.table}")
        if not args.apply:
            print("Preview only; pass --apply to execute")
            return
        with _daemon_halted():
            await _ensure_unchanged(admin, [args.table, *(view[0] for view in views)])
            await _drop_box(admin, args.table, views, record_disabled=True)
            print("Box table and rollups dropped; downsampling state disabled if present")
        return

    if args.command == "drop-partitions":
        _require_managed_views(args.table, views)
        if args.start >= args.end:
            raise AdminError("--from must precede --to")
        partitions = await admin.partitions(args.table)
        if any(kind != "DAY" for kind, *_ in partitions):
            raise AdminError("Only DAY-partitioned box tables are supported")
        selected = [(name, count) for _, name, count in partitions if args.start.isoformat() <= name < args.end.isoformat()]
        if any(not re.fullmatch(r"\d{4}-\d{2}-\d{2}", name) for name, _ in selected):
            raise AdminError("Detached or unexpected partition names cannot be dropped")
        if not selected:
            print("No whole DAY partitions in the selected UTC range")
            return
        if max(name for name, _ in selected) == max(name for _, name, _ in partitions):
            raise AdminError("QuestDB cannot drop the newest partition; select an older range")
        for name, count in selected:
            print(f"DROP PARTITION {name}: {count} rows")
        print(f"Total: {sum(count for _, count in selected)} rows; dependent rollups: {len(views)}")
        if not args.apply:
            print("Preview only; pass --apply to execute")
            return
        await _ensure_unchanged(admin, [args.table, *(view[0] for view in views)])
        dropped: list[str] = []
        drop_error: AdminError | None = None
        try:
            for name, _ in selected:
                await admin.query(
                    f"ALTER TABLE {quote_ident(args.table)} "
                    f"DROP PARTITION LIST {quote_literal(name)};"
                )
                dropped.append(name)
        except AdminError as exc:
            drop_error = exc
        # Rollups must follow whatever was dropped, also after a partial failure.
        view_names = [name for name, *_ in views]
        refreshed: list[str] = []
        refresh_error: AdminError | None = None
        if dropped:
            try:
                for name in view_names:
                    await admin.query(f"REFRESH MATERIALIZED VIEW {quote_ident(name)} FULL;")
                    refreshed.append(name)
                if view_names:
                    print("Full refresh queued for dependent views; refresh runs asynchronously")
            except AdminError as exc:
                refresh_error = exc
        failure = drop_error or refresh_error
        if failure:
            done = [f"DROP PARTITION {name}" for name in dropped]
            done += [f"REFRESH {name}" for name in refreshed]
            remaining = [f"DROP PARTITION {name}" for name, _ in selected if name not in dropped]
            remaining += [f"REFRESH {name}" for name in view_names if name not in refreshed]
            raise _partial_failure(failure, done, remaining)
        print("Selected partitions dropped")
        return

    if args.command == "refresh":
        _require_managed_views(args.table, views)
        if args.full:
            if args.end is not None:
                raise AdminError("--to is only valid with --from")
            suffix = "FULL"
        else:
            if args.end is None or args.start >= args.end:
                raise AdminError("A valid --from and --to range is required")
            suffix = f"RANGE FROM '{args.start.isoformat()}T00:00:00Z' TO '{args.end.isoformat()}T00:00:00Z'"
        selected = [name for name, *_ in views if args.view is None or name == args.view]
        if not selected:
            raise AdminError("No matching materialized view exists for this box table")
        for name in selected:
            print(f"REFRESH MATERIALIZED VIEW {name} {suffix}")
        if not args.apply:
            print("Preview only; pass --apply to execute")
            return
        await _ensure_unchanged(admin, [args.table, *selected])
        for name in selected:
            await admin.query(f"REFRESH MATERIALIZED VIEW {quote_ident(name)} {suffix};")
        print("Refresh queued; check `list` for asynchronous completion")


def _require_supported_version(build: list[list]) -> None:
    """Raise AdminConfigError for a QuestDB older than the daemon supports; an unreadable build() is skipped."""
    if not build or not build[0]:
        return
    version = parse_version(build[0][0])
    if version is None:
        return
    major = version[0]
    if major < QUESTDB_MINIMUM_MAJOR_VERSION:
        raise AdminConfigError(
            f"QuestDB {major} is not supported; version {QUESTDB_MINIMUM_MAJOR_VERSION} or newer is required"
        )


def _require_managed_views(table: str, views: list[tuple]) -> None:
    if any(not _ROLLUP_VIEW.fullmatch(view[0]) or not view[0].startswith(table + "_rollup_") for view in views):
        raise AdminError(f"Unknown dependent materialized view on {table!r}; refusing the operation")


def _disabled_state_sql(table: str) -> str:
    return (
        f"INSERT INTO {quote_ident(QUESTDB_DOWNSAMPLING_STATE_TABLE)} "
        "(timestamp, measurement, profile, schema_version, enabled, raw_days, "
        "rollup_interval, rollup_view, raw_ttl_managed) VALUES ("
        f"now(), {quote_literal(table)}, 'disabled', "
        f"{QUESTDB_DOWNSAMPLING_SCHEMA_VERSION}, false, 0, '', '', false);"
    )


async def _state_repair_pending(admin: QuestDBAdmin, table: str) -> bool:
    """True if an interrupted drop-box left an active status row for a table that is gone.

    The status row is written by fritzfluxdb, so it is the ownership proof once the table
    no longer exists. A table that still exists is never treated as a repair case.
    """
    if not _BOX_TABLE.fullmatch(table):
        return False
    if await admin.query(f"SELECT table_name FROM tables() WHERE table_name = {quote_literal(table)};"):
        return False
    if not await admin.query(_STATE_TABLE_SQL):
        return False
    latest = await admin.query(
        f"SELECT enabled FROM {quote_ident(QUESTDB_DOWNSAMPLING_STATE_TABLE)} "
        f"WHERE measurement = {quote_literal(table)} LATEST ON timestamp PARTITION BY measurement;"
    )
    return bool(latest) and latest[0][0] is True


async def _drop_box(admin: QuestDBAdmin, table: str, views: list[tuple], *, record_disabled: bool) -> None:
    operations = [
        (f"DROP MATERIALIZED VIEW {name}", f"DROP MATERIALIZED VIEW {quote_ident(name)};")
        for name, *_ in views
    ]
    operations.append((f"DROP TABLE {table}", f"DROP TABLE {quote_ident(table)};"))
    if record_disabled:
        # Looked up before the first change, so every read happens while nothing is modified.
        state = await admin.query(_STATE_TABLE_SQL)
        if state:
            operations.append((f"record disabled downsampling state for {table}", _disabled_state_sql(table)))
    labels = [label for label, _ in operations]
    for index, (_, sql) in enumerate(operations):
        try:
            await admin.query(sql)
        except AdminError as exc:
            raise _partial_failure(exc, labels[:index], labels[index:]) from exc


async def main_async(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments:
        parser.print_help()
        return 0

    commands = {"doctor", "list", "drop-all", "partitions", "drop-box", "drop-partitions", "refresh"}
    prefixed_command = next((argument for argument in arguments if argument.startswith("--") and argument[2:] in commands), None)
    if prefixed_command:
        parser.error(
            f"{prefixed_command!r} is a command, not an option; use "
            f"'fritzflux-cli {prefixed_command[2:]}' without the '--' prefix"
        )

    args = parser.parse_args(arguments)
    config = InfluxDBConfig(configparser.ConfigParser())
    if config.parser_error or config.version != "questdb":
        print("QuestDB configuration is required (DB_TYPE=questdb and QUESTDB_HOSTNAME)", file=sys.stderr)
        return 78
    if bool(config.username) != bool(config.password):
        print("QUESTDB_USERNAME and QUESTDB_PASSWORD must be set together", file=sys.stderr)
        return 78
    try:
        async with QuestDBAdmin(config) as admin:
            await _run(args, admin)
    except AdminConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 78
    except AdminError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
