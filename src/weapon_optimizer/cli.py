"""Agent-facing JSON-only command-line entry point for the read-only optimizer."""

from __future__ import annotations

import argparse
import json
import math
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Sequence

from .data import Catalog, DataError, WEAPON_CATEGORY, load_live, load_snapshot, save_snapshot


class _CLIError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_arguments", details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class _ParserExit(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


def _emit(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")


class _JSONParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _CLIError(message)

    def print_help(self, file=None) -> None:
        _emit({"help": self.format_help()})

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if message:
            raise _CLIError(message.strip())
        raise _ParserExit(status)


def _nonnegative_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if result < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return result


def _positive_int(value: str) -> int:
    result = _nonnegative_int(value)
    if result == 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def _positive_float(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number") from None
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return result


def _weight(value: str) -> str:
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("must be a decimal number") from None
    if not result.is_finite() or result < 0:
        raise argparse.ArgumentTypeError("must be finite and nonnegative")
    return value


def _add_live_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session", help="24-character hexadecimal SPT profile ID sent as PHPSESSID; never written to snapshots")
    parser.add_argument(
        "--insecure-loopback",
        action="store_true",
        help="disable TLS verification only for a numeric loopback HTTPS server",
    )


def _add_source_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--snapshot", type=Path, help="complete snapshot produced by the snapshot command")
    group.add_argument("--server", help="runtime server base URL; reads only items and English locale")
    _add_live_options(parser)


def _parser() -> _JSONParser:
    parser = _JSONParser(prog="spt-build", description="Read-only SPT virtual attachment-tree optimizer; all output is JSON.")
    commands = parser.add_subparsers(dest="command", required=True)

    snapshot = commands.add_parser("snapshot", help="explicitly save complete runtime items, locale, and reproducibility metadata")
    snapshot.add_argument("--server", required=True)
    _add_live_options(snapshot)
    snapshot.add_argument("--output", type=Path, required=True, help="explicit destination; there is no implicit snapshot path")

    weapons = commands.add_parser("weapons", help="list weapon template IDs and localized names without selecting a weapon")
    _add_source_options(weapons)
    weapons.add_argument("--query", default="", help="case-insensitive substring in ID, localized name, or internal name")

    optimize = commands.add_parser("optimize", help="optimize an exact weapon template ID without changing the game")
    _add_source_options(optimize)
    optimize.add_argument("--weapon", required=True, help="exact weapon template ID; names are never auto-selected")
    optimize.add_argument("--objective", choices=("recoil", "ergo", "weighted"), default="weighted")
    optimize.add_argument("--recoil-weight", type=_weight, default="1")
    optimize.add_argument("--ergo-weight", type=_weight, default="1")
    optimize.add_argument("--min-capacity", type=_nonnegative_int, default=0)
    optimize.add_argument("--launcher", choices=("any", "required", "forbidden"), default="any")
    optimize.add_argument("--solutions", type=_positive_int, default=3)
    optimize.add_argument("--time-limit", type=_positive_float, default=30.0)
    optimize.add_argument("--workers", type=_positive_int, default=1)
    optimize.add_argument("--mode", choices=("top", "pareto"), default="top")
    return parser


def _catalog(args: argparse.Namespace) -> Catalog:
    if getattr(args, "snapshot", None) is not None:
        if args.session is not None or args.insecure_loopback:
            raise _CLIError("--session and --insecure-loopback can only be used with --server")
        return load_snapshot(args.snapshot)
    if not args.session:
        raise _CLIError("--server requires an explicit --session PHPSESSID")
    return load_live(args.server, args.session, insecure_loopback=args.insecure_loopback)


def _weapons(catalog: Catalog, query: str = "") -> list[dict[str, str]]:
    needle = query.casefold()
    weapons = []
    for tid, item in catalog.items.items():
        if item.get("_type") != "Item" or not catalog.is_a(tid, WEAPON_CATEGORY):
            continue
        name = catalog.name(tid)
        if needle and not any(needle in value.casefold() for value in (tid, name, str(item.get("_name", "")))):
            continue
        weapons.append({"template_id": tid, "name": name})
    return sorted(weapons, key=lambda entry: (entry["name"].casefold(), entry["template_id"]))


def _weapon_id(catalog: Catalog, requested: str) -> str:
    if requested in catalog.items:
        if catalog.items[requested].get("_type") != "Item" or not catalog.is_a(requested, WEAPON_CATEGORY):
            raise _CLIError(f"template {requested!r} is not a weapon", code="invalid_weapon")
        return requested
    candidates = _weapons(catalog, requested)
    raise _CLIError(
        "--weapon must be an exact weapon template ID; names and partial matches are never auto-selected",
        code="invalid_weapon",
        details={"requested": requested, "candidates": candidates},
    )


def _run(args: argparse.Namespace) -> dict[str, Any]:
    catalog = _catalog(args)
    if args.command == "snapshot":
        source = save_snapshot(catalog, args.output)
        return {
            "status": "ok",
            "output": str(args.output),
            "item_count": len(catalog.items),
            "locale_entry_count": len(catalog.locale),
            "source": source,
        }
    if args.command == "weapons":
        return {"weapons": _weapons(catalog, args.query), "source": catalog.source}

    weapon_id = _weapon_id(catalog, args.weapon)
    if args.objective == "weighted" and Decimal(args.recoil_weight) == 0 and Decimal(args.ergo_weight) == 0:
        raise _CLIError("weighted objective requires at least one positive weight")
    from .optimizer import optimize

    result = optimize(
        catalog,
        weapon_id,
        objective=args.objective,
        recoil_weight=args.recoil_weight,
        ergo_weight=args.ergo_weight,
        min_capacity=args.min_capacity,
        launcher=args.launcher,
        limit=args.solutions,
        time_limit=args.time_limit,
        workers=args.workers,
        mode=args.mode,
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Emit one JSON result/error and return a conventional process exit code."""
    args: argparse.Namespace | None = None
    try:
        args = _parser().parse_args(argv)
        _emit(_run(args))
        return 0
    except _ParserExit as exc:
        return exc.status
    except _CLIError as exc:
        error: dict[str, Any] = {"code": exc.code, "message": str(exc)}
        if exc.details is not None:
            error["details"] = exc.details
        _emit({"error": error})
        return 2
    except DataError as exc:
        message = str(exc)
        if args is not None and args.session:
            message = message.replace(args.session, "<redacted>")
        _emit({"error": {"code": "data_error", "message": message}})
        return 1
    except ValueError as exc:
        _emit({"error": {"code": "invalid_arguments", "message": str(exc)}})
        return 2
    except KeyboardInterrupt:
        _emit({"error": {"code": "interrupted", "message": "operation interrupted"}})
        return 130
    except Exception as exc:
        message = str(exc)
        if args is not None and args.session:
            message = message.replace(args.session, "<redacted>")
        _emit({"error": {"code": "internal_error", "message": message}})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
