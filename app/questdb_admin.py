#!/usr/bin/env python3
#
# app/questdb_admin.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Administrative QuestDB commands for the FritzFluxDB container."""

import argparse
import asyncio
import configparser
import re
import sys
from argparse import RawDescriptionHelpFormatter
from datetime import date

import httpx

from app.classes.influxdb.config import (
    QUESTDB_DOWNSAMPLING_SCHEMA_VERSION,
    QUESTDB_DOWNSAMPLING_STATE_TABLE,
    InfluxDBConfig,
)
from app.classes.influxdb.handler import (
    _format_url_host,
    _questdb_identifier,
    _questdb_literal,
)

_BOX_TABLE = re.compile(r"fritzbox(?:_[A-Za-z0-9_]+)?\Z")
_ROLLUP_VIEW = re.compile(r".+_rollup_[0-9]+[mhd]_v[0-9]+\Z")


class AdminError(Exception):
    """An expected configuration, metadata, or QuestDB operation failure."""


class QuestDBAdmin:
    def __init__(self, config: InfluxDBConfig):
        scheme = "https" if config.tls_enabled else "http"
        host = _format_url_host(config.hostname)
        auth = (config.username, config.password) if config.username else None
        self.client = httpx.AsyncClient(
            base_url=f"{scheme}://{host}:{config.port}",
            auth=auth,
            timeout=20,
            verify=config.verify_tls,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.client.aclose()

    async def query(self, sql: str) -> list[list]:
        try:
            response = await self.client.get("/exec", params={"query": sql})
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AdminError(f"QuestDB request failed: {type(exc).__name__}: {exc}") from exc
        if not isinstance(data, dict):
            raise AdminError("QuestDB returned an invalid SQL response")
        if data.get("error"):
            raise AdminError(f"QuestDB rejected SQL: {data['error']}")
        return data.get("dataset") or []

    async def inventory(self) -> tuple[list[str], dict[str, list[tuple[str, str, int | None, int | None]]]]:
        tables = await self.query("SELECT table_name, matView, partitionBy FROM tables();")
        views = await self.query(
            "SELECT view_name, base_table_name, view_status, refresh_base_table_txn, "
            "base_table_txn FROM materialized_views();"
        )
        boxes = sorted(
            str(row[0]) for row in tables
            if not row[1] and row[2] == "DAY" and _BOX_TABLE.fullmatch(str(row[0]))
            and not _ROLLUP_VIEW.fullmatch(str(row[0]))
        )
        by_base: dict[str, list[tuple[str, str, int | None, int | None]]] = {box: [] for box in boxes}
        for name, base, status, refreshed, latest, *_ in views:
            if str(base) in by_base:
                by_base[str(base)].append((str(name), str(status), refreshed, latest))
        for entries in by_base.values():
            entries.sort()
        return boxes, by_base

    async def require_box(self, table: str):
        if not _BOX_TABLE.fullmatch(table):
            raise AdminError("Table must be a FritzFluxDB box table (fritzbox or fritzbox_<serial>)")
        boxes, views = await self.inventory()
        if table not in boxes:
            raise AdminError(f"Managed box table {table!r} does not exist")
        return views[table]

    async def partitions(self, table: str) -> list[tuple[str, str, int]]:
        await self.require_box(table)
        rows = await self.query(
            "SELECT partitionBy, name, numRows FROM "
            f"table_partitions({_questdb_literal(table)});"
        )
        return [(str(kind), str(name), int(count)) for kind, name, count in rows]


def _date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD")
    return parsed


def _confirm_action() -> bool:
    """Ask for interactive confirmation and default to cancelling."""
    try:
        response = input("Are you sure? [y/N]: ")
    except EOFError:
        response = ""
    if response.strip().lower() not in {"y", "yes"}:
        print("Cancelled.")
        return False
    return True


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
            "  fritzflux-cli drop-box fritzbox_AA1234567890                 # preview\n"
            "  fritzflux-cli drop-box fritzbox_AA1234567890 --apply       # execute\n"
            "  fritzflux-cli drop-partitions fritzbox_AA1234567890 --from 2026-09-01 --to 2026-09-08\n"
            "  fritzflux-cli refresh fritzbox_AA1234567890 --full\n\n"
            "Password changes, API tokens, and row-level device deletion are unsupported."
        ),
        formatter_class=RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check the SQL connection and show managed objects")
    commands.add_parser("list", help="List managed box tables and their materialized views")
    drop_all = commands.add_parser("drop-all", help="Preview or drop all FritzFluxDB tables and rollups")
    drop_all.add_argument("--apply", action="store_true", help="Execute after an interactive confirmation")
    partitions = commands.add_parser("partitions", help="List partitions for a managed box table")
    partitions.add_argument("table")
    drop_box = commands.add_parser("drop-box", help="Preview or drop a box table and its rollup views")
    drop_box.add_argument("table")
    drop_box.add_argument("--apply", action="store_true", help="Execute after an interactive confirmation")
    drop = commands.add_parser("drop-partitions", help="Preview or drop whole DAY partitions in a UTC date range")
    drop.add_argument("table")
    drop.add_argument("--from", dest="start", required=True, type=_date, help="First UTC day, inclusive")
    drop.add_argument("--to", dest="end", required=True, type=_date, help="Last UTC day, exclusive")
    drop.add_argument("--apply", action="store_true", help="Execute after an interactive confirmation")
    refresh = commands.add_parser("refresh", help="Preview or trigger a materialized view refresh")
    refresh.add_argument("table", help="Base box table")
    mode = refresh.add_mutually_exclusive_group(required=True)
    mode.add_argument("--full", action="store_true", help="Rebuild the full view")
    mode.add_argument("--from", dest="start", type=_date, help="First UTC day, inclusive; requires --to")
    refresh.add_argument("--to", dest="end", type=_date, help="Last UTC day, exclusive")
    refresh.add_argument("--view", help="One discovered rollup view; defaults to all views for the table")
    refresh.add_argument("--apply", action="store_true", help="Queue the refresh after an interactive confirmation")
    return parser


async def _run(args: argparse.Namespace, admin: QuestDBAdmin) -> None:
    if args.command in {"doctor", "list"}:
        if args.command == "doctor":
            build = await admin.query("SELECT build();")
            print(f"{'QuestDB connection':<23} OK ({build[0][0] if build else 'version unknown'})")
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
            print(f"{'Mutations':<23} --apply and confirmation required")
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
        state = await admin.query(
            "SELECT table_name FROM tables() "
            f"WHERE table_name = {_questdb_literal(QUESTDB_DOWNSAMPLING_STATE_TABLE)};"
        )
        if state:
            print(f"DROP TABLE {QUESTDB_DOWNSAMPLING_STATE_TABLE}")
        if not boxes and not state:
            print("No FritzFluxDB tables found")
            return
        if not args.apply:
            print("Preview only; pass --apply to execute")
            return
        if not _confirm_action():
            return
        for box in boxes:
            await _drop_box(admin, box, all_views[box], record_disabled=False)
        if state:
            await admin.query(f"DROP TABLE {_questdb_identifier(QUESTDB_DOWNSAMPLING_STATE_TABLE)};")
        print("FritzFluxDB tables and rollups dropped")
        return

    if args.command == "partitions":
        for kind, name, count in await admin.partitions(args.table):
            print(f"{name}: {count} rows ({kind})")
        return

    views = await admin.require_box(args.table)
    if args.command == "drop-box":
        _require_managed_views(args.table, views)
        for name, *_ in views:
            print(f"DROP MATERIALIZED VIEW {name}")
        print(f"DROP TABLE {args.table}")
        if not args.apply:
            print("Preview only; pass --apply to execute")
            return
        if not _confirm_action():
            return
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
        if not _confirm_action():
            return
        drop_error = None
        try:
            for name, _ in selected:
                await admin.query(
                    f"ALTER TABLE {_questdb_identifier(args.table)} "
                    f"DROP PARTITION LIST {_questdb_literal(name)};"
                )
        except AdminError as exc:
            drop_error = exc
        if views:
            for name, *_ in views:
                await admin.query(f"REFRESH MATERIALIZED VIEW {_questdb_identifier(name)} FULL;")
            print("Full refresh queued for dependent views; refresh runs asynchronously")
        if drop_error:
            raise drop_error
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
        if not _confirm_action():
            return
        for name in selected:
            await admin.query(f"REFRESH MATERIALIZED VIEW {_questdb_identifier(name)} {suffix};")
        print("Refresh queued; check `list` for asynchronous completion")


def _require_managed_views(table: str, views: list[tuple]) -> None:
    if any(not _ROLLUP_VIEW.fullmatch(view[0]) or not view[0].startswith(table + "_rollup_") for view in views):
        raise AdminError(f"Unknown dependent materialized view on {table!r}; refusing the operation")


async def _drop_box(admin: QuestDBAdmin, table: str, views: list[tuple], *, record_disabled: bool) -> None:
    for name, *_ in views:
        await admin.query(f"DROP MATERIALIZED VIEW {_questdb_identifier(name)};")
    await admin.query(f"DROP TABLE {_questdb_identifier(table)};")
    if record_disabled:
        state = await admin.query(
            "SELECT table_name FROM tables() "
            f"WHERE table_name = {_questdb_literal(QUESTDB_DOWNSAMPLING_STATE_TABLE)};"
        )
        if state:
            await admin.query(
                f"INSERT INTO {_questdb_identifier(QUESTDB_DOWNSAMPLING_STATE_TABLE)} "
                "(timestamp, measurement, profile, schema_version, enabled, raw_days, "
                "rollup_interval, rollup_view, raw_ttl_managed) VALUES ("
                f"now(), {_questdb_literal(table)}, 'disabled', "
                f"{QUESTDB_DOWNSAMPLING_SCHEMA_VERSION}, false, 0, '', '', false);"
            )


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
    except AdminError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
