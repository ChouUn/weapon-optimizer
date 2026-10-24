"""Read-only runtime data access and attachment-slot compatibility.

Unknown filter IDs match nothing. A concrete template ID matches only itself;
a category (``_type == 'Node'``) matches its descendant item templates. Filters
are ANDed, while the IDs inside a Filter are ORed. No candidate is discarded
because of its individual statistics.
"""

from __future__ import annotations

import hashlib
import ipaddress
import simplejson as json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MOD_CATEGORY = "5448fe124bdc2da5018b4567"
MAGAZINE_CATEGORY = "5448bc234bdc2d3c308b4569"
LAUNCHER_CATEGORY = "55818b014bdc2ddc698b456b"
WEAPON_CATEGORY = "5422acb9af1c889c16000029"
SNAPSHOT_SCHEMA_VERSION = 2
HASH_FORMAT = "sha256 of UTF-8 JSON, object order preserved, compact separators, ensure_ascii=false"

_RICH_TEXT = re.compile(
    r"</?(?:b|i|u|s|color|size|material|quad|font|mark|alpha|align|cspace|"
    r"mspace|indent|line-height|line-indent|link|margin|nobr|noparse|page|"
    r"pos|rotate|space|sprite|style|sub|sup|voffset|width|br|allcaps|"
    r"uppercase|lowercase|smallcaps|font-weight|gradient)(?:\s[^<>]*|=[^<>]*)?/?>|<#[0-9a-f]{6,8}>",
    re.IGNORECASE,
)


class DataError(ValueError):
    """The requested runtime data or complete snapshot is unavailable/invalid."""


class Catalog:
    """An explicit items/locale snapshot; the supplied dictionaries are not edited."""

    def __init__(
        self,
        items: dict[str, dict[str, Any]],
        locale: dict[str, str] | None = None,
        source: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(items, dict):
            raise DataError("items must be an object keyed by template ID")
        if locale is not None and not isinstance(locale, dict):
            raise DataError("locale must be an object")
        if source is not None and not isinstance(source, dict):
            raise DataError("source must be an object")
        self.items = items
        self.locale = locale if locale is not None else {}
        self.source = source if source is not None else {}
        self._ancestors: dict[str, tuple[str, ...]] = {}
        self._candidate_cache: dict[tuple[Any, ...], tuple[str, ...]] = {}
        for tid, item in items.items():
            if not isinstance(tid, str) or not tid or not isinstance(item, dict):
                raise DataError("each item needs a nonempty string ID and an object")
            if item.get("_id", tid) != tid:
                raise DataError(f"template key {tid!r} disagrees with its _id")
            if not isinstance(item.get("_props", {}), dict):
                raise DataError(f"template {tid!r} has invalid _props")
        for tid in items:
            self.ancestors(tid)
        self._item_ids = tuple(sorted(tid for tid in items if items[tid].get("_type") == "Item"))
        descendants: dict[str, list[str]] = {}
        for tid in self._item_ids:
            for ancestor in self._ancestors[tid]:
                descendants.setdefault(ancestor, []).append(tid)
        self._descendants = {tid: tuple(ids) for tid, ids in descendants.items()}

    def props(self, tid: str) -> dict[str, Any]:
        try:
            return self.items[tid].get("_props", {})
        except KeyError:
            raise DataError(f"unknown template ID: {tid}") from None

    def conflicts(self, tid: str) -> tuple[str, ...]:
        """Resolve JSON.NET string[] fields: case-insensitive, later key wins."""
        result = ()
        for key, value in self.props(tid).items():
            if not isinstance(key, str) or key.casefold() != "conflictingitems":
                continue
            if value is None:
                result = ()
            elif isinstance(value, list) and all(isinstance(target, str) for target in value):
                result = tuple(value)
            else:
                raise DataError(f"{tid}: {key} must be a list of template IDs or null")
        return result

    def name(self, tid: str) -> str:
        if tid not in self.items:
            raise DataError(f"unknown template ID: {tid}")
        label = self.locale.get(f"{tid} Name")
        if not isinstance(label, str) or not label.strip():
            label = self.items[tid].get("_name") or tid
        return _RICH_TEXT.sub("", str(label)).strip()

    def ancestors(self, tid: str) -> tuple[str, ...]:
        """Return self followed by parents, rejecting cycles and missing parents."""
        if tid not in self.items:
            raise DataError(f"unknown template ID: {tid}")
        if tid in self._ancestors:
            return self._ancestors[tid]
        path: list[str] = []
        seen: set[str] = set()
        current = tid
        while current and current not in self._ancestors:
            if current in seen:
                raise DataError(f"ancestor cycle: {' -> '.join([*path, current])}")
            if current not in self.items:
                owner = path[-1] if path else tid
                raise DataError(f"template {owner!r} references unknown ancestor {current!r}")
            seen.add(current)
            path.append(current)
            parent = self.items[current].get("_parent")
            if parent is not None and not isinstance(parent, str):
                raise DataError(f"template {current!r} has an invalid _parent")
            current = parent or ""
        tail = self._ancestors.get(current, ())
        for node in reversed(path):
            tail = (node, *tail)
            self._ancestors[node] = tail
        return self._ancestors[tid]

    def is_a(self, tid: str, ancestor: str) -> bool:
        return ancestor in self.ancestors(tid)

    def is_mod(self, tid: str) -> bool:
        return self.is_a(tid, MOD_CATEGORY)

    def slots(self, tid: str) -> list[dict[str, Any]]:
        slots = self.props(tid).get("Slots")
        if slots is None:
            return []
        if not isinstance(slots, list) or any(not isinstance(slot, dict) for slot in slots):
            raise DataError(f"template {tid!r} has invalid Slots")
        return slots

    def _filter_matches(self, ids: tuple[str, ...]) -> set[str]:
        matches: set[str] = set()
        for tid in ids:
            item = self.items.get(tid)
            if item is None:
                continue
            if item.get("_type") == "Item":
                matches.add(tid)
            elif item.get("_type") == "Node":
                matches.update(self._descendants.get(tid, ()))
        return matches

    def candidates(self, slot: dict[str, Any]) -> tuple[str, ...]:
        """Return all compatible Item templates, without statistic-based pruning."""
        if not isinstance(slot, dict):
            raise DataError("slot must be an object")
        context = slot.get("_name", slot.get("_id", "<unnamed>"))
        props = slot.get("_props", {})
        if not isinstance(props, dict):
            raise DataError(f"slot {context!r} has invalid _props")
        filters = props.get("filters")
        if filters is None or filters == []:
            return self._item_ids
        if not isinstance(filters, list):
            raise DataError(f"slot {context!r} filters must be a list or null")
        groups: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        for group in filters:
            if not isinstance(group, dict):
                raise DataError(f"slot {context!r} contains an invalid filter")
            lists = []
            for key in ("Filter", "ExcludedFilter"):
                ids = group.get(key)
                if ids is None:
                    ids = []
                if not isinstance(ids, list) or any(not isinstance(tid, str) for tid in ids):
                    raise DataError(f"slot {context!r} {key} must be a list of IDs or null")
                lists.append(tuple(sorted(set(ids))))
            groups.append((lists[0], lists[1]))
        signature = tuple(groups)
        if signature in self._candidate_cache:
            return self._candidate_cache[signature]
        accepted: set[str] | None = None
        for included, excluded in groups:
            allowed = self._filter_matches(included)
            allowed.difference_update(self._filter_matches(excluded))
            accepted = allowed if accepted is None else accepted.intersection(allowed)
        result = tuple(sorted(accepted if accepted is not None else ()))
        self._candidate_cache[signature] = result
        return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=False, separators=(",", ":"), ensure_ascii=False, allow_nan=False, use_decimal=True).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_snapshot_bytes(value)).hexdigest()


def _reject_constant(value: str) -> None:
    raise DataError(f"non-finite JSON value: {value}")


def _decode_json(payload: bytes, endpoint: str) -> Any:
    try:
        return json.loads(payload.decode("utf-8-sig"), parse_constant=_reject_constant, use_decimal=True)
    except (UnicodeDecodeError, json.JSONDecodeError):
        try:
            decoded = zlib.decompress(payload)
        except zlib.error:
            raise DataError(f"{endpoint} did not return JSON or zlib-compressed JSON") from None
        try:
            return json.loads(decoded.decode("utf-8-sig"), parse_constant=_reject_constant, use_decimal=True)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DataError(f"{endpoint} returned invalid compressed JSON") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _server_url(server: str, insecure_loopback: bool) -> str:
    try:
        parsed = urllib.parse.urlsplit(server)
        port = parsed.port
    except ValueError:
        raise ValueError("server URL has an invalid host or port") from None
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ValueError("server must be an absolute http:// or https:// URL")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("server URL cannot contain credentials, a query, or a fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("server URL has an invalid port")
    if insecure_loopback:
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError("--insecure-loopback requires a numeric loopback address (for example 127.0.0.1 or [::1])")
        if parsed.scheme != "https":
            raise ValueError("--insecure-loopback applies only to an https:// server")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def load_live(
    server: str,
    session: str,
    *,
    insecure_loopback: bool = False,
    timeout: float = 30.0,
) -> Catalog:
    """GET only /client/items and /client/locale/en; never fall back to disk."""
    server = _server_url(server, insecure_loopback)
    if not isinstance(session, str) or re.fullmatch(r"[0-9a-fA-F]{24}", session) is None:
        raise ValueError("--session must be a 24-character hexadecimal SPT profile ID")
    if not isinstance(timeout, (float, int)) or timeout <= 0:
        raise ValueError("request timeout must be positive")
    context = ssl.create_default_context()
    if insecure_loopback:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        _NoRedirect(),
    )
    fetched: dict[str, dict[str, Any]] = {}
    values: dict[str, Any] = {}
    started_at = _now()
    for section, endpoint in (("items", "/client/items"), ("locale", "/client/locale/en")):
        request = urllib.request.Request(
            server + endpoint,
            headers={"Cookie": f"PHPSESSID={session}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            raise DataError(f"GET {endpoint} returned HTTP {exc.code}; redirects are not followed") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = str(getattr(exc, "reason", exc)).replace(session, "<redacted>")
            raise DataError(f"GET {endpoint} failed: {reason}") from None
        envelope = _decode_json(payload, endpoint)
        if not isinstance(envelope, dict) or not {"err", "errmsg", "data"} <= envelope.keys():
            raise DataError(f"GET {endpoint} did not return an {{err, errmsg, data}} envelope")
        if envelope["err"] != 0:
            message = str(envelope["errmsg"]).replace(session, "<redacted>")
            raise DataError(f"GET {endpoint} failed (err={envelope['err']}): {message}")
        if not isinstance(envelope["data"], dict) or not envelope["data"]:
            raise DataError(f"GET {endpoint} data must be a nonempty object")
        values[section] = envelope["data"]
        fetched[section] = {"endpoint": endpoint, "retrieved_at": _now(), "sha256": _digest(envelope["data"])}
    source = {
        "kind": "runtime-get",
        "server": server,
        "locale": "en",
        "started_at": started_at,
        "completed_at": _now(),
        "hash_format": HASH_FORMAT,
        "items": fetched["items"],
        "locale_data": fetched["locale"],
        "item_count": len(values["items"]),
        "locale_entry_count": len(values["locale"]),
        "consistency": "two sequential GET responses; not a server-atomic snapshot",
    }
    return Catalog(values["items"], values["locale"], source)


def save_snapshot(catalog: Catalog, path: str | Path) -> dict[str, Any]:
    """Write a complete snapshot only to the explicitly supplied output path."""
    source = dict(catalog.source)
    source.setdefault("snapshot_created_at", _now())
    source["hash_format"] = HASH_FORMAT
    source["content_sha256"] = {"items": _digest(catalog.items), "locale": _digest(catalog.locale)}
    document = {"schema_version": SNAPSHOT_SCHEMA_VERSION, "items": catalog.items, "locale": catalog.locale, "source": source}
    target = Path(path).resolve()
    project_root = Path(__file__).resolve().parents[2]
    workspace_root = project_root.parents[1]
    allowed_roots = [project_root]
    temp_root = workspace_root / ".tmp"
    if temp_root.resolve() == temp_root:
        allowed_roots.append(temp_root)
    if not any(target.is_relative_to(root) for root in allowed_roots):
        raise DataError("snapshot output must stay inside the optimizer project or workspace .tmp directory")
    try:
        # Resolved containment handles symlinks/junctions; exclusive creation
        # also refuses every existing destination, including existing symlinks.
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False, use_decimal=True)
            stream.write("\n")
    except (OSError, ValueError) as exc:
        raise DataError(f"cannot write snapshot {str(path)!r}: {exc}") from None
    return source


def load_snapshot(path: str | Path) -> Catalog:
    """Read the full saved data; do not consult production files or a server."""
    try:
        with Path(path).open("r", encoding="utf-8-sig") as stream:
            document = json.load(stream, parse_constant=_reject_constant, use_decimal=True)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DataError(f"cannot read snapshot {str(path)!r}: {exc}") from None
    if not isinstance(document, dict) or document.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise DataError(f"snapshot must be an object with schema_version {SNAPSHOT_SCHEMA_VERSION}; older snapshots lack order-sensitive integrity")
    if not {"items", "locale", "source"} <= document.keys():
        raise DataError("snapshot must contain complete items, locale, and source objects")
    if not isinstance(document["items"], dict) or not document["items"]:
        raise DataError("snapshot items must be a nonempty object")
    if not isinstance(document["locale"], dict) or not document["locale"]:
        raise DataError("snapshot locale must be a nonempty object")
    if not isinstance(document["source"], dict):
        raise DataError("snapshot source must be an object")
    digests = document["source"].get("content_sha256")
    if not isinstance(digests, dict):
        raise DataError("snapshot source is missing its content_sha256 digests")
    for section in ("items", "locale"):
        if digests.get(section) != _digest(document[section]):
            raise DataError(f"snapshot {section} SHA-256 mismatch")
    return Catalog(document["items"], document["locale"], document["source"])
