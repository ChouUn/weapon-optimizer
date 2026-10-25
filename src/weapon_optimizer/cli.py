"""Read-only optimizer CLI with JSON results and explicit human-readable trees."""

from __future__ import annotations

import argparse
import simplejson as json
import math
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Sequence

from .data import Catalog, DataError, WEAPON_CATEGORY, load_live, load_snapshot, save_snapshot, solve_context
from .acquisition import Availability
from .evaluate import evaluate_build
from .local import enrich_catalog
from .presentation import render_result


class _CLIError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_arguments", details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class _ParserExit(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


def _emit(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), use_decimal=True) + "\n")


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
    parser.add_argument("--installation", type=Path, help="explicit local installation root for Chinese names, compatibility, and acquisition evidence")


def _parser() -> _JSONParser:
    parser = _JSONParser(prog="spt-build", description="Read-only SPT attachment-tree optimizer; JSON or localized tree output.")
    commands = parser.add_subparsers(dest="command", required=True)

    snapshot = commands.add_parser("snapshot", help="explicitly save complete runtime items, locale, and reproducibility metadata")
    snapshot.add_argument("--server", required=True)
    _add_live_options(snapshot)
    snapshot.add_argument("--output", type=Path, required=True, help="explicit destination; there is no implicit snapshot path")
    snapshot.add_argument("--installation", type=Path, help="also capture local Chinese names and acquisition/compatibility evidence")

    prepare = commands.add_parser("prepare", help="enrich a complete offline snapshot from an explicit local installation")
    prepare.add_argument("--snapshot", type=Path, required=True)
    prepare.add_argument("--installation", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)

    render = commands.add_parser("render", help="render a saved build result with exact localized short/full names")
    _add_source_options(render)
    render.add_argument("--build", type=Path, required=True)
    render.add_argument("--solution", type=_positive_int, help="one-based solution rank; default renders all solutions")
    render.add_argument("--language", choices=("ch", "en"), default="ch")

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
    optimize.add_argument("--require-suppressor", action="store_true", help="require an installed attachment in the Silencer subtype, including descendants")
    optimize.add_argument("--solutions", type=_positive_int, default=3)
    optimize.add_argument("--time-limit", type=_positive_float, default=30.0)
    optimize.add_argument("--workers", type=_positive_int, default=1)
    optimize.add_argument("--mode", choices=("top", "pareto"), default="top")
    optimize.add_argument("--acquisition", choices=("required", "templates"), default="required", help="require evidenced acquisition sources; templates explicitly skips acquisition checks")
    optimize.add_argument("--format", choices=("json", "tree"), default="json")
    optimize.add_argument("--language", choices=("ch", "en"), default="ch", help="tree display language with English fallback")
    return parser


def _catalog(args: argparse.Namespace) -> Catalog:
    if getattr(args, "snapshot", None) is not None:
        if getattr(args, "session", None) is not None or getattr(args, "insecure_loopback", False):
            raise _CLIError("--session and --insecure-loopback can only be used with --server")
        catalog = load_snapshot(args.snapshot)
    else:
        if not args.session:
            raise _CLIError("--server requires an explicit --session PHPSESSID")
        catalog = load_live(args.server, args.session, insecure_loopback=args.insecure_loopback)
    installation = getattr(args, "installation", None)
    return enrich_catalog(catalog, installation) if installation is not None else catalog


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


def _run(args: argparse.Namespace) -> dict[str, Any] | str:
    catalog = _catalog(args)
    if args.command in {"snapshot", "prepare"}:
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
    if args.command == "render":
        try:
            with args.build.open("r", encoding="utf-8-sig") as stream:
                result = json.load(stream, use_decimal=True)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DataError(f"cannot read build result {args.build}: {exc}") from None
        if not isinstance(result, dict):
            raise DataError("build result must be an object")
        solutions = result.get("solutions")
        if not isinstance(solutions, list):
            raise DataError("build result needs a solutions array")
        availability = Availability(catalog, catalog.acquisition) if result.get("constraints", {}).get("acquisition") == "required" else None
        for solution in solutions:
            if not isinstance(solution, dict) or not isinstance(solution.get("build"), dict):
                raise DataError("saved solution needs a build tree")
            stats = evaluate_build(catalog, solution["build"], require_suppressor=result.get("constraints", {}).get("require_suppressor", False))
            comparable = json.loads(json.dumps(stats, use_decimal=True), use_decimal=True)
            if comparable != solution.get("stats"):
                raise DataError("saved build statistics do not match the selected snapshot")
            if availability is not None:
                for part in stats["parts"]:
                    if part["template_id"] not in availability.allowed_ids:
                        raise DataError(f"saved build attachment has no evidenced acquisition source: {part['template_id']}")
        acquisition_mode = result.get("constraints", {}).get("acquisition", "templates")
        current_context = solve_context(catalog, acquisition_mode, availability.allowed_ids if availability is not None else None)
        if result.get("solve_context") != current_context:
            raise DataError("saved proof does not match this search context; optimize again with the selected snapshot")
        return render_result(catalog, result, language=args.language, rank=args.solution)

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
        require_suppressor=args.require_suppressor,
        limit=args.solutions,
        time_limit=args.time_limit,
        workers=args.workers,
        mode=args.mode,
        acquisition=args.acquisition,
    )
    return render_result(catalog, result, language=args.language) if args.format == "tree" else result


def main(argv: Sequence[str] | None = None) -> int:
    """Emit JSON by default, or an explicitly requested tree, with process status."""
    args: argparse.Namespace | None = None
    try:
        args = _parser().parse_args(argv)
        result = _run(args)
        if isinstance(result, str):
            sys.stdout.write(result)
        else:
            _emit(result)
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
        if args is not None and getattr(args, "session", None):
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
        if args is not None and getattr(args, "session", None):
            message = message.replace(args.session, "<redacted>")
        _emit({"error": {"code": "internal_error", "message": message}})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
