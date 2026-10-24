"""Validate a complete, unloaded attachment tree and evaluate template stats."""

from __future__ import annotations

from collections import Counter
from fractions import Fraction
from typing import Any

from .data import Catalog


_WEAPON = "5422acb9af1c889c16000029"
_MAGAZINE = "5448bc234bdc2d3c308b4569"
_LAUNCHER = "55818b014bdc2ddc698b456b"
_AMMO = "5485a8684bdc2da71d8b4567"
_CYLINDER = "610720f290b75a49ff2e5e25"


def _number(props: dict, key: str, template_id: str) -> Fraction:
    value = props.get(key, 0)
    if isinstance(value, bool):
        raise ValueError(f"{template_id}: {key} must be a finite number")
    try:
        return Fraction(str(value))
    except (TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
        raise ValueError(f"{template_id}: {key} must be a finite number") from exc


def magazine_capacity(catalog: Catalog, template_id: str) -> int:
    """Read Magazine.MaxCount, including the CylinderMagazine override."""
    if not catalog.is_a(template_id, _MAGAZINE):
        raise ValueError(f"{template_id}: item is not a magazine")
    if catalog.is_a(template_id, _CYLINDER):
        # CylinderMagazine and SpringDrivenCylinder count direct camora_ slots.
        return sum(
            isinstance(slot.get("_name"), str) and slot["_name"].startswith("camora_")
            for slot in catalog.slots(template_id)
        )
    cartridges = catalog.props(template_id).get("Cartridges", [])
    if not isinstance(cartridges, list) or not cartridges:
        raise ValueError(f"{template_id}: magazine has no Cartridges[0]")
    first_cartridges = cartridges[0]
    if not isinstance(first_cartridges, dict):
        raise ValueError(f"{template_id}: invalid Cartridges[0]")
    capacity = first_cartridges.get("_max_count")
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
        raise ValueError(f"{template_id}: magazine capacity must be a nonnegative integer")
    return capacity


def evaluate_build(catalog: Catalog, build: dict) -> dict[str, Any]:
    """Evaluate unfolded template stats without ammo, skills, or game mutation.

    ConflictingItems contains exact template IDs, not category filters: the
    client's Slot.CheckConflictingItems compares both directions by TemplateId.
    Weapon.GetCurrentMagazine only uses the root's direct mod_magazine slot;
    its virtual MaxCount is evaluated without nested or linked weapon magazines.
    """
    if not isinstance(build, dict):
        raise ValueError("build root must be a node object")
    if "slot" not in build or build["slot"] is not None:
        raise ValueError("build root slot must be null")
    root_id = build.get("template_id")
    if not isinstance(root_id, str) or root_id not in catalog.items:
        raise ValueError(f"unknown root template: {root_id!r}")
    if not catalog.is_a(root_id, _WEAPON):
        raise ValueError(f"root template is not a weapon: {root_id}")

    seen: set[int] = set()
    template_counts: Counter[str] = Counter()
    recoil_modifiers: list[Fraction] = []
    ergonomics_modifiers: list[Fraction] = []
    parts: list[dict[str, Any]] = []
    has_launcher = False
    current_capacity = 0
    pending = [(build, root_id, True)]

    while pending:
        node, path, is_root = pending.pop()
        if not isinstance(node, dict):
            raise ValueError(f"{path}: child must be a node object")
        unknown_fields = node.keys() - {"template_id", "slot", "children", "name"}
        if unknown_fields:
            raise ValueError(f"{path}: unknown build node fields: {sorted(map(str, unknown_fields))}")
        identity = id(node)
        if identity in seen:
            raise ValueError(f"{path}: build contains a cycle or a shared node")
        seen.add(identity)
        template_id = node.get("template_id")
        if not isinstance(template_id, str) or template_id not in catalog.items:
            raise ValueError(f"{path}: unknown template {template_id!r}")
        if catalog.items[template_id].get("_type") != "Item":
            raise ValueError(f"{path}: template is not an item: {template_id}")
        if not is_root and catalog.is_a(template_id, _AMMO):
            raise ValueError(f"{path}: ammunition is outside the unloaded build model")
        children = node.get("children")
        if not isinstance(children, list):
            raise ValueError(f"{path}: children must be a list")
        template_counts[template_id] += 1
        props = catalog.props(template_id)
        if not is_root:
            is_mod = catalog.is_mod(template_id)
            recoil = _number(props, "Recoil", template_id) if is_mod else Fraction(0)
            ergonomics = _number(props, "Ergonomics", template_id) if is_mod else Fraction(0)
            recoil_modifiers.append(recoil)
            ergonomics_modifiers.append(ergonomics)
            has_launcher |= catalog.is_a(template_id, _LAUNCHER)
            parts.append(
                {
                    "template_id": template_id,
                    "slot": node["slot"],
                    "path": path,
                    "is_mod": is_mod,
                    "recoil_modifier_percent": float(recoil),
                    "ergonomics_modifier": float(ergonomics),
                }
            )

        slots: dict[str, dict] = {}
        for slot in catalog.slots(template_id):
            name = slot.get("_name")
            if not isinstance(name, str) or not name or name in slots:
                raise ValueError(f"{path}: invalid or duplicate template slot {name!r}")
            slots[name] = slot
        occupied: set[str] = set()
        next_nodes = []
        for child in children:
            if not isinstance(child, dict):
                raise ValueError(f"{path}: child must be a node object")
            slot_name = child.get("slot")
            if not isinstance(slot_name, str) or slot_name not in slots:
                raise ValueError(f"{path}: unknown parent slot {slot_name!r}")
            if slot_name in occupied:
                raise ValueError(f"{path}: slot {slot_name!r} contains more than one item")
            occupied.add(slot_name)
            child_id = child.get("template_id")
            if not isinstance(child_id, str) or child_id not in catalog.items:
                raise ValueError(f"{path}/{slot_name}: unknown template {child_id!r}")
            if child_id not in catalog.candidates(slots[slot_name]):
                raise ValueError(f"{path}/{slot_name}: template {child_id} is not allowed")
            if is_root and slot_name == "mod_magazine":
                current_capacity = magazine_capacity(catalog, child_id)
            next_nodes.append((child, f"{path}/{slot_name}", False))
        for slot_name, slot in slots.items():
            if slot.get("_required", False) and slot_name not in occupied:
                raise ValueError(f"{path}: required slot {slot_name!r} is empty")
        pending.extend(reversed(next_nodes))

    # Count instances rather than deduplicating template IDs: two copies of a
    # self-conflicting template conflict, but one does not conflict with itself.
    for template_id in template_counts:
        for conflicting_id in catalog.conflicts(template_id):
            if template_counts[conflicting_id] > (1 if conflicting_id == template_id else 0):
                raise ValueError(f"conflicting templates: {template_id} and {conflicting_id}")

    root_props = catalog.props(root_id)
    recoil_modifier = sum(recoil_modifiers, Fraction(0))
    recoil_multiplier = 1 + recoil_modifier / 100
    base_up = _number(root_props, "RecoilForceUp", root_id)
    base_back = _number(root_props, "RecoilForceBack", root_id)
    base_ergo = _number(root_props, "Ergonomics", root_id)
    ergonomics_raw = base_ergo * (1 + sum(ergonomics_modifiers, Fraction(0)) / max(Fraction(1), base_ergo))
    return {
        "recoil_modifier_percent": float(recoil_modifier),
        "recoil_vertical": float(base_up * recoil_multiplier),
        "recoil_horizontal": float(base_back * recoil_multiplier),
        "recoil_total": float((base_up + base_back) * recoil_multiplier),
        "ergonomics_raw": float(ergonomics_raw),
        "ergonomics_effective": float(min(Fraction(100), ergonomics_raw)),
        "magazine_capacity": current_capacity,
        "has_launcher": has_launcher,
        "part_count": len(parts),
        "parts": parts,
    }
