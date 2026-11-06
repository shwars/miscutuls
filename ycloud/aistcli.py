#!/usr/bin/env python3
"""aistcli - command line utility for Yandex AI Studio.

Works through the OpenAI SDK against the AI Studio OpenAI-compatible API.

Configuration:
    Reads lowercase ``folder_id`` and ``api_key`` from a ``.env`` file
    located either next to this script or in the current working directory,
    or from environment variables (environment takes precedence).

Usage:
    python aistcli.py list models [--json]
    python aistcli.py list files [--limit N] [--json]
    python aistcli.py list indices [--json]
    python aistcli.py rm files [-y]
    python aistcli.py rm indices [-y]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

BASE_URL = "https://ai.api.cloud.yandex.net/v1"
DEFAULT_FILES_LIMIT = 10

CONFIG_KEYS = ("folder_id", "api_key")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def load_config() -> tuple[str, str, str]:
    """Return (folder_id, api_key, source).

    ``.env`` files are searched next to the script first, then in the
    current working directory. Real environment variables always win over
    values from ``.env``.
    """
    script_dir = Path(__file__).resolve().parent
    load_dotenv(script_dir / ".env", override=False)
    load_dotenv(Path.cwd() / ".env", override=False)

    values: dict[str, str] = {}
    source = "environment"
    for key in CONFIG_KEYS:
        value = os.getenv(key)
        if value:
            values[key] = value.strip()

    missing = [key for key in CONFIG_KEYS if not values.get(key)]
    if missing:
        searched = [script_dir / ".env", Path.cwd() / ".env"]
        print(
            "Error: missing configuration value(s): " + ", ".join(missing)
            + "\nSet them in one of the .env files ("
            + " or ".join(str(p) for p in searched)
            + ") or as environment variables.",
            file=sys.stderr,
        )
        sys.exit(1)

    dotenv_path = script_dir / ".env"
    if dotenv_path.exists() and all(
        f"{key}=" in dotenv_path.read_text(encoding="utf-8") for key in CONFIG_KEYS
    ):
        source = str(dotenv_path)
    else:
        cwd_env = Path.cwd() / ".env"
        if cwd_env.exists():
            source = str(cwd_env)
    return values["folder_id"], values["api_key"], source


def create_client(folder_id: str, api_key: str) -> OpenAI:
    return OpenAI(
        base_url=BASE_URL,
        api_key=api_key,
        project=folder_id,
    )


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

def _format_ts(ts: int | float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(row: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()

    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt(row))


def emit(rows: list[dict], columns: list[tuple[str, str]], as_json: bool) -> None:
    """Print rows either as a table or as JSON.

    ``columns`` is a list of (header, dict-key) pairs used for table output.
    """
    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not rows:
        print("Nothing found.")
        return
    headers = [c[0] for c in columns]
    table_rows = [[str(item.get(c[1], "-")) for c in columns] for item in rows]
    print_table(headers, table_rows)


def to_dict(obj) -> dict:
    """Convert an SDK object to a plain dict for JSON output."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return dict(obj)


# --------------------------------------------------------------------------
# list commands
# --------------------------------------------------------------------------

def cmd_list_models(client: OpenAI, args) -> None:
    try:
        response = client.models.list()
        models = list(response)
    except Exception as exc:  # noqa: BLE001 - report API errors clearly
        print(f"Error: could not query models: {exc}", file=sys.stderr)
        sys.exit(1)

    if not models:
        print("Error: no models returned by the API.", file=sys.stderr)
        sys.exit(1)

    rows = [to_dict(model) for model in models]
    emit(rows, [("ID", "id"), ("Owned by", "owned_by"), ("Created", "created")], args.json)


def cmd_list_files(client: OpenAI, args) -> None:
    try:
        response = client.files.list(limit=args.limit)
        files = list(response)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: could not list files: {exc}", file=sys.stderr)
        sys.exit(1)

    rows = []
    table = []
    for f in files:
        d = to_dict(f)
        table.append([
            d.get("id", "-"), d.get("filename", "-"), str(d.get("bytes", "-")),
            d.get("purpose", "-"), _format_ts(d.get("created_at")),
        ])
        rows.append(d)
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not table:
        print("Nothing found.")
        return
    print_table(["ID", "Filename", "Bytes", "Purpose", "Created"], table)


def cmd_list_indices(client: OpenAI, args) -> None:
    try:
        response = client.vector_stores.list()
        stores = list(response)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: could not list indices: {exc}", file=sys.stderr)
        sys.exit(1)

    rows = []
    table = []
    for vs in stores:
        d = to_dict(vs)
        counts = d.get("file_counts") or {}
        table.append([
            d.get("id", "-"), d.get("name", "-"), d.get("status", "-"),
            str(counts.get("completed", 0)), str(counts.get("total", 0)),
            _format_ts(d.get("created_at")),
        ])
        rows.append(d)
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not table:
        print("Nothing found.")
        return
    print_table(
        ["ID", "Name", "Status", "Files", "Total files", "Created"],
        table,
    )


# --------------------------------------------------------------------------
# rm commands
# --------------------------------------------------------------------------

def confirm(item: str, assume_yes: bool, state: dict) -> bool:
    """Ask for confirmation unless -y was given or 'a' (all) was answered."""
    if assume_yes or state.get("all"):
        return True
    while True:
        try:
            answer = input(f"Delete {item}? [y/N/a] ").strip().lower()
        except EOFError:
            print("\nAborted.", file=sys.stderr)
            sys.exit(1)
        if answer in ("y", "yes"):
            return True
        if answer == "a":
            state["all"] = True
            return True
        if answer in ("", "n", "no"):
            return False
        print("Please answer y, n, or a.")


def _paginate(iterator) -> list:
    """Drain a paginated SDK iterator into a full list."""
    return list(iterator)


def cmd_rm_files(client: OpenAI, args) -> None:
    try:
        files = _paginate(client.files.list())
    except Exception as exc:  # noqa: BLE001
        print(f"Error: could not list files: {exc}", file=sys.stderr)
        sys.exit(1)

    if not files:
        print("No files to delete.")
        return

    print(f"Found {len(files)} file(s).")
    state: dict = {}
    deleted, failed, skipped = 0, 0, 0
    for f in files:
        label = f"{f.id} ({f.filename})" if f.filename else f.id
        if not confirm(f"file {label}", args.yes, state):
            skipped += 1
            continue
        try:
            client.files.delete(file_id=f.id)
            print(f"Deleted: {label}")
            deleted += 1
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to delete {label}: {exc}", file=sys.stderr)
            failed += 1

    print(f"Done: {deleted} deleted, {skipped} skipped, {failed} failed.")


def cmd_rm_indices(client: OpenAI, args) -> None:
    try:
        stores = _paginate(client.vector_stores.list())
    except Exception as exc:  # noqa: BLE001
        print(f"Error: could not list indices: {exc}", file=sys.stderr)
        sys.exit(1)

    if not stores:
        print("No indices to delete.")
        return

    print(f"Found {len(stores)} index/indices.")
    state: dict = {}
    deleted, failed, skipped = 0, 0, 0
    for vs in stores:
        label = f"{vs.id} ({vs.name})" if vs.name else vs.id
        if not confirm(f"index {label}", args.yes, state):
            skipped += 1
            continue
        try:
            client.vector_stores.delete(vector_store_id=vs.id)
            print(f"Deleted: {label}")
            deleted += 1
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to delete {label}: {exc}", file=sys.stderr)
            failed += 1

    print(f"Done: {deleted} deleted, {skipped} skipped, {failed} failed.")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aistcli",
        description="Command line utility for Yandex AI Studio (OpenAI-compatible API).",
    )
    sub = parser.add_subparsers(dest="group", required=True)

    list_parser = sub.add_parser("list", help="list resources")
    list_sub = list_parser.add_subparsers(dest="resource", required=True)

    models_p = list_sub.add_parser("models", help="list available models")
    models_p.add_argument("--json", action="store_true", help="output raw JSON")
    models_p.set_defaults(func=cmd_list_models)

    files_p = list_sub.add_parser("files", help="list uploaded files")
    files_p.add_argument("--limit", type=int, default=DEFAULT_FILES_LIMIT,
                         help=f"number of files to show (default {DEFAULT_FILES_LIMIT})")
    files_p.add_argument("--json", action="store_true", help="output raw JSON")
    files_p.set_defaults(func=cmd_list_files)

    idx_p = list_sub.add_parser("indices", help="list file search indices (vector stores)")
    idx_p.add_argument("--json", action="store_true", help="output raw JSON")
    idx_p.set_defaults(func=cmd_list_indices)

    rm_parser = sub.add_parser("rm", help="delete resources")
    rm_sub = rm_parser.add_subparsers(dest="resource", required=True)

    rm_files_p = rm_sub.add_parser("files", help="delete all files")
    rm_files_p.add_argument("-y", "--yes", action="store_true",
                            help="assume yes for all deletions, do not ask")
    rm_files_p.set_defaults(func=cmd_rm_files)

    rm_idx_p = rm_sub.add_parser("indices", help="delete all file search indices")
    rm_idx_p.add_argument("-y", "--yes", action="store_true",
                          help="assume yes for all deletions, do not ask")
    rm_idx_p.set_defaults(func=cmd_rm_indices)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    folder_id, api_key, source = load_config()
    print(f"Using folder_id: {folder_id} (config source: {source})", file=sys.stderr)

    client = create_client(folder_id, api_key)
    args.func(client, args)


if __name__ == "__main__":
    main()
