"""Human-readable attachment trees using exact Chinese/English locale entries."""
from __future__ import annotations

import re
from typing import Any

from .data import Catalog, DataError

_SLOT_LABELS = {
    "mod_charge": "拉机柄",
    "mod_core": "核心",
    "mod_gas_block": "导气组件",
    "mod_handguard": "护木",
    "mod_foregrip": "前握把",
    "mod_launcher": "下挂榴弹",
    "mod_magazine": "弹匣",
    "mod_mount": "基座",
    "mod_muzzle": "枪口",
    "mod_pistol_grip": "手枪式握把",
    "mod_reciever": "机匣盖",
    "mod_receiver": "机匣盖",
    "mod_scope": "瞄具",
    "mod_sight_front": "准星",
    "mod_sight_rear": "照门",
    "mod_stock": "枪托",
    "mod_tactical": "战术附件",
    "mod_barrel": "枪管",
    "mod_bipod": "两脚架",
    "mod_catch": "卡笋",
    "mod_hammer": "击锤",
    "mod_trigger": "扳机",
    "mod_mag_shaft": "弹匣井",
    "mod_pistolgrip": "手枪式握把",
    "mod_nvg": "夜视仪",
    "mod_equipment": "装备附件",
}


def slot_label(slot: str | None) -> str:
    if slot is None:
        return "武器"
    normalized = re.sub(r"_\d+$", "", slot)
    return _SLOT_LABELS.get(normalized, slot)


def render_build(catalog: Catalog, build: dict[str, Any], *, language: str = "ch") -> str:
    """Preserve the input assembly; only slot labels and locale-derived names vary."""
    lines: list[str] = []
    seen: set[int] = set()

    def visit(node: dict[str, Any], prefix: str, last: bool, root: bool = False) -> None:
        if not isinstance(node, dict) or id(node) in seen:
            raise DataError("build must be an attachment tree without shared nodes or cycles")
        seen.add(id(node))
        tid = node.get("template_id")
        children = node.get("children")
        slot = node.get("slot")
        if not isinstance(tid, str) or not isinstance(children, list):
            raise DataError("each build node needs a template_id and children array")
        if (root and slot is not None) or (not root and not isinstance(slot, str)):
            raise DataError("build root slot must be null; attachment slots must be strings")
        short_name = catalog.name(tid, language=language, short=True)
        full_name = catalog.name(tid, language=language)
        branch = "" if root else ("└── " if last else "├── ")
        lines.append(f"{prefix}{branch}[{slot_label(slot)}] {short_name}〔{full_name}〕")
        child_prefix = prefix if root else prefix + ("    " if last else "│   ")
        for index, child in enumerate(children):
            visit(child, child_prefix, index == len(children) - 1)

    visit(build, "", True, True)
    return "\n".join(lines)


def render_result(catalog: Catalog, result: dict[str, Any], *, language: str = "ch", rank: int | None = None) -> str:
    solutions = result.get("solutions")
    if not isinstance(solutions, list):
        raise DataError("build result needs a solutions array")
    if rank is not None:
        if rank < 1 or rank > len(solutions):
            raise DataError(f"solution rank must be between 1 and {len(solutions)}")
        solutions = [solutions[rank - 1]]
    if not solutions:
        return f"状态：{result.get('status', 'unknown')}；没有可交付方案。\n"
    blocks = []
    acquisition_mode = result.get("constraints", {}).get("acquisition", "templates")
    for index, solution in enumerate(solutions, 1):
        stats = solution.get("stats", {})
        proof = solution.get("proof", {})
        header = f"配装 #{solution.get('rank', index)}｜状态：{proof.get('status', result.get('status', 'unknown'))}"
        if proof.get("status") != "optimal":
            header += "（未证明最优）"
        summary = (
            f"人机：{stats.get('ergonomics_effective', '?')}｜"
            f"垂直后坐力：{stats.get('recoil_vertical', '?')}｜"
            f"水平后坐力：{stats.get('recoil_horizontal', '?')}｜"
            f"弹匣：{stats.get('magazine_capacity', '?')} 发"
        )
        if result.get("constraints", {}).get("require_suppressor", False):
            summary += "｜枪口要求：消音器子类型"
        availability = (
            "获取性：按已记录来源筛选；角色等级、商人及任务解锁状态未读取。"
            if acquisition_mode == "required"
            else "获取性：未筛选，仅供模板研究。"
        )
        blocks.append("\n".join((header, summary, availability, render_build(catalog, solution["build"], language=language))))
    return "\n\n".join(blocks) + "\n"
