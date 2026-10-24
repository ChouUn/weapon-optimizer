"""Exact finite attachment-tree models; no heuristic candidate pruning."""
from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from fractions import Fraction

from ortools.sat.python import cp_model

from .evaluate import evaluate_build, magazine_capacity

WEAPON = "5422acb9af1c889c16000029"
MAGAZINE = "5448bc234bdc2d3c308b4569"
LAUNCHER = "55818b014bdc2ddc698b456b"
AMMO = "5485a8684bdc2da71d8b4567"
# Leave ample headroom for CP-SAT's sum/domain overflow checks.
SAFE_INTEGER = (1 << 60) - 1


def _number(value) -> Fraction:
    try:
        result = Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"Expected a finite decimal number, got {value!r}") from exc
    return result


def _scale(values):
    scale = math.lcm(*(value.denominator for value in values))
    integers = [int(value * scale) for value in values]
    if scale > SAFE_INTEGER or sum(abs(v) for v in integers) > SAFE_INTEGER:
        raise ValueError("Template numeric precision exceeds exact CP-SAT integer range; no rounding applied")
    return scale, integers


def _graph(catalog, root):
    """Validate the type DAG before expanding it; never silently truncate cycles."""
    edges = {}
    visiting = []
    active = set()

    def visit(tid):
        if tid in active:
            cycle = visiting[visiting.index(tid):] + [tid]
            raise ValueError("Cyclic attachment templates: " + " -> ".join(cycle))
        if tid in edges:
            return
        active.add(tid)
        visiting.append(tid)
        slots = catalog.slots(tid)
        names = [s["_name"] for s in slots]
        if len(names) != len(set(names)):
            raise ValueError(f"Duplicate slot names in template {tid}")
        children = [(slot, tuple(tid for tid in catalog.candidates(slot) if not catalog.is_a(tid, AMMO))) for slot in slots]
        # Mark completed only after descendants: an optional cycle is still infinite.
        for slot, ids in children:
            for child in ids:
                visit(child)
        edges[tid] = children
        visiting.pop()
        active.remove(tid)

    visit(root)
    return edges


class _Model:
    def __init__(self, catalog, root, min_capacity, launcher):
        self.catalog = catalog
        self.root = root
        self.model = cp_model.CpModel()
        props = catalog.props(root)
        base_recoil = _number(props.get("RecoilForceUp", 0)) + _number(props.get("RecoilForceBack", 0))
        self.recoil_direction = (base_recoil > 0) - (base_recoil < 0)
        edges = _graph(catalog, root)
        self.variables = {}
        self.paths = defaultdict(dict)
        providers = defaultdict(dict)
        allowed = defaultdict(set)
        pending = deque([((), root)])
        # A slot-name path is one physical position. Different possible ancestors
        # share that position but retain their own conditional compatibility rules.
        while pending:
            path, tid = pending.popleft()
            key = (path, tid)
            if key in self.variables:
                continue
            var = self.model.new_bool_var(f"{len(self.variables)}:{tid}")
            self.variables[key] = var
            self.paths[path][tid] = var
            for slot, ids in edges[tid]:
                child_path = path + (slot["_name"],)
                providers[child_path][key] = slot
                for child in ids:
                    allowed[(child_path, child)].add(key)
                    pending.append((child_path, child))
        self.model.add(self.variables[((), root)] == 1)
        for path, parents in providers.items():
            children = list(self.paths[path].values())
            total = sum(children)
            self.model.add(total <= sum(self.variables[key] for key in parents))
            self.model.add(total >= sum(self.variables[key] for key, slot in parents.items() if slot.get("_required", False)))
            if children:
                self.model.add_at_most_one(children)
        for key, parents in allowed.items():
            self.model.add(self.variables[key] <= sum(self.variables[parent] for parent in parents))

        by_type = defaultdict(list)
        for (path, tid), var in self.variables.items():
            by_type[tid].append(var)
        present = {}
        for tid, variables in by_type.items():
            present[tid] = self.model.new_bool_var(f"present:{tid}")
            self.model.add_max_equality(present[tid], variables)
        pairs = set()
        for tid in by_type:
            # The client compares exact template IDs here, not item categories.
            for other in catalog.conflicts(tid):
                if other not in by_type:
                    continue
                if other == tid:
                    self.model.add(sum(by_type[tid]) <= 1)
                else:
                    pairs.add(tuple(sorted((tid, other))))
        for left, right in pairs:
            self.model.add(present[left] + present[right] <= 1)

        capacities = []
        launchers = []
        for (path, tid), var in self.variables.items():
            if path == ("mod_magazine",):
                if not catalog.is_a(tid, MAGAZINE):
                    self.model.add(var == 0)
                else:
                    capacities.append(magazine_capacity(catalog, tid) * var)
            if path and catalog.is_a(tid, LAUNCHER):
                launchers.append(var)
        self.model.add(sum(capacities) >= min_capacity)
        if launcher == "required":
            self.model.add(sum(launchers) >= 1)
        elif launcher == "forbidden":
            self.model.add(sum(launchers) == 0)

        keys = [key for key in self.variables if key[0]]
        base_ergo = _number(catalog.props(root).get("Ergonomics", 0))
        multiplier = base_ergo / max(Fraction(1), base_ergo)
        recoil_values = [_number(catalog.props(tid).get("Recoil", 0)) if catalog.is_mod(tid) else Fraction(0) for path, tid in keys]
        ergo_values = [multiplier * _number(catalog.props(tid).get("Ergonomics", 0)) if catalog.is_mod(tid) else Fraction(0) for path, tid in keys]
        self.recoil_scale, recoil_ints = _scale(recoil_values)
        self.ergo_scale, ergo_all = _scale([base_ergo, Fraction(100), *ergo_values])
        ergo_base, ergo_cap, *ergo_ints = ergo_all
        self.recoil = sum(coefficient * self.variables[key] for coefficient, key in zip(recoil_ints, keys))
        ergo_expr = ergo_base + sum(coefficient * self.variables[key] for coefficient, key in zip(ergo_ints, keys))
        ergo_min = ergo_base + sum(min(0, c) for c in ergo_ints)
        ergo_max = ergo_base + sum(max(0, c) for c in ergo_ints)
        raw_ergo = self.model.new_int_var(ergo_min, ergo_max, "raw_ergo")
        self.model.add(raw_ergo == ergo_expr)
        self.ergo = self.model.new_int_var(min(ergo_min, ergo_cap), min(ergo_max, ergo_cap), "effective_ergo")
        self.model.add_min_equality(self.ergo, [raw_ergo, ergo_cap])
        self.part_count = sum(self.variables[key] for key in keys)
        self.type_count = len(edges)
        self.conflict_pairs = len(pairs)

    def objective(self, kind, wr, we):
        if kind == "recoil":
            return -self.recoil_direction * self.recoil, self.recoil_scale, self.ergo
        if kind == "ergo":
            return self.ergo, self.ergo_scale, -self.recoil_direction * self.recoil
        scale, coefficients = _scale([wr / self.recoil_scale, we / self.ergo_scale])
        cr, ce = coefficients
        # All arithmetic remains exact for the decimal template model.
        # Check actual conservative expression bounds, not a precision cutoff.
        max_r = sum(abs(_number(self.catalog.props(tid).get("Recoil", 0))) for path, tid in self.variables if path and self.catalog.is_mod(tid))
        max_e = Fraction(100) + sum(abs(_number(self.catalog.props(tid).get("Ergonomics", 0))) for path, tid in self.variables if path and self.catalog.is_mod(tid))
        if (abs(wr) * max_r + abs(we) * max_e) * scale > SAFE_INTEGER:
            raise ValueError("Weighted objective exceeds exact integer range; use simpler weights")
        return -cr * self.recoil_direction * self.recoil + ce * self.ergo, scale, self.ergo * self.recoil_scale - self.recoil_direction * self.recoil * self.ergo_scale

    def extract(self, solver):
        chosen = {path: tid for (path, tid), var in self.variables.items() if solver.boolean_value(var)}
        nodes = {path: {"template_id": tid, "name": self.catalog.name(tid), "slot": path[-1] if path else None, "children": []} for path, tid in chosen.items()}
        for path in sorted(chosen, key=lambda p: (len(p), p)):
            if path:
                nodes[path[:-1]]["children"].append(nodes[path])
        return nodes[()]


def optimize(catalog, weapon_id, *, objective="weighted", recoil_weight="1", ergo_weight="1", min_capacity=0, launcher="any", limit=3, time_limit=30.0, workers=1, mode="top"):
    """Return ranked builds or proven Pareto points, with explicit proof scope.

    Limits apply to the complete call (model construction included). An unproven
    incumbent ends enumeration: it cannot safely define a top-K or Pareto cut.
    """
    if objective not in {"weighted", "recoil", "ergo"} or mode not in {"top", "pareto"}:
        raise ValueError("Invalid objective or mode")
    if launcher not in {"any", "required", "forbidden"}:
        raise ValueError("Invalid launcher constraint")
    if not isinstance(min_capacity, int) or isinstance(min_capacity, bool) or min_capacity < 0:
        raise ValueError("min_capacity must be a nonnegative integer")
    if not isinstance(limit, int) or limit < 1 or not isinstance(workers, int) or workers < 1:
        raise ValueError("limit and workers must be positive integers")
    if not math.isfinite(time_limit) or time_limit <= 0:
        raise ValueError("time_limit must be finite and positive")
    wr, we = _number(recoil_weight), _number(ergo_weight)
    if wr < 0 or we < 0 or (objective == "weighted" and not (wr or we)):
        raise ValueError("Weights must be nonnegative and weighted objective needs a positive weight")
    if weapon_id not in catalog.items or not catalog.is_a(weapon_id, WEAPON) or catalog.items[weapon_id].get("_type") != "Item":
        raise ValueError(f"Not a weapon template: {weapon_id}")
    start = time.monotonic()
    deadline = start + time_limit
    problem = _Model(catalog, weapon_id, min_capacity, launcher)
    kind = "recoil" if mode == "pareto" else objective
    primary, scale, secondary = problem.objective(kind, wr, we)
    solutions = []
    exhausted = False
    terminal = "unknown"
    first_value = None
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = workers
    solver.parameters.random_seed = 0
    solver.parameters.log_search_progress = False

    def solve(expression):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return cp_model.UNKNOWN
        solver.parameters.max_time_in_seconds = remaining
        problem.model.maximize(expression)
        return solver.solve(problem.model)

    for rank in range(1, limit + 1):
        status = solve(primary)
        if status == cp_model.MODEL_INVALID:
            raise ValueError("Invalid exact optimization model: " + problem.model.validate())
        if status == cp_model.INFEASIBLE:
            exhausted = True
            terminal = "optimal" if solutions else "infeasible"
            break
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            terminal = "unknown"
            break
        value = int(solver.value(primary))
        bound = solver.best_objective_bound
        primary_proven = status == cp_model.OPTIMAL
        if first_value is None:
            first_value = value
        build = problem.extract(solver)
        assignment = [(var, solver.boolean_value(var)) for var in problem.variables.values()]
        secondary_proven = False
        minimal_parts_proven = False
        # One assumption gates all temporary ties. Disable it before the next
        # rank, where builds with a worse primary score must remain legal.
        if primary_proven:
            gate = problem.model.new_bool_var(f"tie:{rank}")
            problem.model.add(primary == value).only_enforce_if(gate)
            problem.model.add_assumption(gate)
            for var, chosen in assignment:
                problem.model.add_hint(var, int(chosen))
            tie_status = solve(secondary)
            if tie_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                build = problem.extract(solver)
                assignment = [(var, solver.boolean_value(var)) for var in problem.variables.values()]
                secondary_proven = tie_status == cp_model.OPTIMAL
                if secondary_proven:
                    problem.model.add(secondary == int(solver.value(secondary))).only_enforce_if(gate)
                    problem.model.clear_hints()
                    for var, chosen in assignment:
                        problem.model.add_hint(var, int(chosen))
                    part_status = solve(-problem.part_count)
                    if part_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                        build = problem.extract(solver)
                        assignment = [(var, solver.boolean_value(var)) for var in problem.variables.values()]
                        minimal_parts_proven = part_status == cp_model.OPTIMAL
        problem.model.clear_assumptions()
        if primary_proven:
            problem.model.add(gate == 0)
        problem.model.clear_hints()
        stats = evaluate_build(catalog, build)
        reduction = -problem.recoil_direction * _number(stats["recoil_modifier_percent"])
        score = float(wr * reduction + we * _number(stats["ergonomics_effective"]))
        proof = {"status": "optimal" if primary_proven else "feasible", "scope": "pareto_point" if mode == "pareto" and primary_proven and secondary_proven else "all_builds" if rank == 1 else "remaining_builds", "objective_value": value / scale, "best_bound": bound / scale, "gap": max(0.0, (bound - value) / max(1.0, abs(value))), "original_global_optimum": mode == "top" and primary_proven and value == first_value, "tie_break_proven": secondary_proven, "minimal_parts_proven": minimal_parts_proven}
        solutions.append({"rank": rank, "build": build, "stats": stats, "score": score, "proof": proof})
        terminal = "optimal" if primary_proven else "feasible"
        if not primary_proven or (mode == "pareto" and not secondary_proven):
            break
        if mode == "pareto":
            # Evaluate exact integer ergonomics from the selected assignment,
            # rather than re-quantizing JSON float output for the strict cut.
            selected = {var.index: chosen for var, chosen in assignment}
            # Raw template recomputation retains exact decimal arithmetic.
            base = _number(catalog.props(weapon_id).get("Ergonomics", 0))
            raw = base + base / max(Fraction(1), base) * sum((_number(catalog.props(tid).get("Ergonomics", 0)) for (path, tid), var in problem.variables.items() if path and selected[var.index] and catalog.is_mod(tid)), Fraction(0))
            effective = int(min(Fraction(100), raw) * problem.ergo_scale)
            problem.model.add(problem.ergo >= effective + 1)
        else:
            problem.model.add_bool_or([var.Not() if chosen else var for var, chosen in assignment if var.index != problem.variables[((), weapon_id)].index])
    ranking_proven = bool(solutions) and all(s["proof"]["status"] == "optimal" and (mode != "pareto" or s["proof"]["tie_break_proven"]) for s in solutions)
    if solutions and terminal == "unknown":
        terminal = "feasible"
    if mode == "pareto" and solutions and not ranking_proven:
        terminal = "feasible"
    return {
        "schema_version": 1,
        "status": terminal,
        "weapon": {"template_id": weapon_id, "name": catalog.name(weapon_id)},
        "source": catalog.source,
        "objective": {
            "kind": kind,
            "recoil_weight": str(wr),
            "ergo_weight": str(we),
            "score_formula": "recoil_weight * recoil_reduction_percent + ergo_weight * min(100, ergonomics_raw)",
            "recoil_reduction_definition": "100 * (base_recoil - recoil_total) / abs(base_recoil); zero when base_recoil is zero",
            "numeric_model": "exact template decimals; excludes IEEE-754 accumulation noise",
        },
        "constraints": {
            "min_capacity": min_capacity,
            "launcher": launcher,
            "state": "unfolded, unloaded; no ammunition, skill, durability or dynamic recoil modifiers",
        },
        "search": {
            "mode": mode,
            "requested_solutions": limit,
            "returned_solutions": len(solutions),
            "ranking_proven": ranking_proven,
            "exhausted": exhausted,
            "elapsed_seconds": round(time.monotonic() - start, 4),
            "reachable_templates": problem.type_count,
            "attachment_positions": len(problem.paths),
            "selection_variables": len(problem.variables),
            "conflict_pairs": problem.conflict_pairs,
        },
        "solutions": solutions,
    }
