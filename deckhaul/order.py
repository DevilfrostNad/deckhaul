"""Load order: groups, ordering rules and automatic sorting.

In ETS2 the top of the in-game list has the highest priority: a mod higher
up overrides files of the mods below it. So small, specific mods go up and
large base content (maps, trucks) goes down.
"""

from __future__ import annotations

import fnmatch
import heapq
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# (group id, title, categories). Top of the list first.
GROUPS: List[Tuple[str, str, Tuple[str, ...]]] = [
    ("top", "Закреплённые сверху", ()),
    ("graphics", "Графика и погода", ("graphics", "weather_setup")),
    ("sound", "Звук", ("sound",)),
    ("physics", "Физика", ("physics",)),
    ("ui", "Интерфейс и прочее", ("ui", "other", "models", "movers", "walkers", "prefabs")),
    ("tuning", "Тюнинг и салоны", ("tuning_parts", "interior")),
    ("traffic", "Трафик", ("ai_traffic",)),
    ("cargo", "Грузы", ("cargo_pack",)),
    ("paint", "Покраски", ("paint_job",)),
    ("trailer", "Прицепы", ("trailer",)),
    ("truck", "Грузовики", ("truck",)),
    ("map", "Карты", ("map",)),
    ("bottom", "Закреплённые снизу", ()),
]
GROUP_INDEX = {g[0]: i for i, g in enumerate(GROUPS)}
_CAT_TO_GROUP = {c: g[0] for g in GROUPS for c in g[2]}

RULES_FILE = os.path.join(os.path.dirname(__file__), "rules.json")


@dataclass
class Rule:
    id: str
    text: str
    above: List[str] = field(default_factory=list)
    below: List[str] = field(default_factory=list)
    pin: Optional[str] = None      # "top" | "bottom" | group id
    match: List[str] = field(default_factory=list)
    source: str = "builtin"

    def to_dict(self):
        return self.__dict__.copy()


def _matches(pattern: str, mod) -> bool:
    p = pattern.lower()
    if p.startswith("cat:"):
        return mod.category == p[4:]
    if p.startswith("key:"):
        return mod.key.lower() == p[4:]
    for value in (mod.key, mod.name, os.path.basename(mod.path)):
        if value and fnmatch.fnmatch(value.lower(), p):
            return True
    return False


def _any(patterns: List[str], mod) -> bool:
    return any(_matches(p, mod) for p in patterns)


def load_rules(user_file: Optional[str] = None) -> List[Rule]:
    rules: List[Rule] = []
    for path, source in ((RULES_FILE, "builtin"), (user_file, "user")):
        if not path or not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for item in json.load(fh).get("rules", []):
                item = dict(item)
                item["source"] = source
                rules.append(Rule(**{k: v for k, v in item.items() if k in Rule.__dataclass_fields__}))
    return rules


def save_user_rules(user_file: str, rules: List[Rule]) -> None:
    data = {"rules": [
        {k: v for k, v in r.to_dict().items() if k != "source"}
        for r in rules if r.source == "user"
    ]}
    with open(user_file, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def group_of(mod, rules: List[Rule], overrides: Dict[str, str]) -> str:
    if mod.key in overrides and overrides[mod.key] in GROUP_INDEX:
        return overrides[mod.key]
    for r in rules:
        if r.pin and r.match and _any(r.match, mod):
            return r.pin
    return _CAT_TO_GROUP.get(mod.category, "ui")


def _edges(mods: List, rules: List[Rule]) -> List[Tuple[int, int, Rule]]:
    edges = []
    for r in rules:
        if not (r.above and r.below):
            continue
        ups = [i for i, m in enumerate(mods) if _any(r.above, m)]
        downs = [i for i, m in enumerate(mods) if _any(r.below, m)]
        for a in ups:
            for b in downs:
                if a != b and not (_any(r.above, mods[b]) and _any(r.below, mods[a])):
                    edges.append((a, b, r))
    return edges


@dataclass
class Placement:
    key: str
    group: str
    problem: Optional[str] = None


def analyse(mods: List, rules: List[Rule], overrides: Dict[str, str]) -> List[Placement]:
    """Explain what is wrong with the given order (top first) without changing it."""
    out = []
    furthest = 0
    furthest_name = ""
    for m in mods:
        g = group_of(m, rules, overrides)
        gi = GROUP_INDEX[g]
        p = Placement(m.key, g)
        if gi < furthest:
            p.problem = (
                f"группа «{GROUPS[gi][1]}» должна стоять выше группы «{GROUPS[furthest][1]}»"
                f" (например, выше «{furthest_name}»)"
            )
        elif gi > furthest:
            furthest, furthest_name = gi, m.name
        out.append(p)
    pos = {m.key: i for i, m in enumerate(mods)}
    for a, b, r in _edges(mods, rules):
        if pos[mods[a].key] > pos[mods[b].key]:
            pl = out[a]
            msg = f"должен стоять выше «{mods[b].name}»: {r.text}"
            pl.problem = msg if not pl.problem else pl.problem + "; " + msg
    return out


def auto_sort(mods: List, rules: List[Rule], overrides: Dict[str, str]):
    """Return (sorted mods, list of rule cycles that could not be satisfied).

    Group order first, then pairwise rules. Inside a group the user's
    current order is kept, so manual tweaks survive re-sorting.
    """
    n = len(mods)
    base_rank = sorted(range(n), key=lambda i: (GROUP_INDEX[group_of(mods[i], rules, overrides)], i))
    rank = {idx: r for r, idx in enumerate(base_rank)}

    succ: Dict[int, List[int]] = {i: [] for i in range(n)}
    indeg = [0] * n
    for a, b, _r in _edges(mods, rules):
        succ[a].append(b)
        indeg[b] += 1

    heap = [(rank[i], i) for i in range(n) if indeg[i] == 0]
    heapq.heapify(heap)
    result = []
    while heap:
        _, i = heapq.heappop(heap)
        result.append(i)
        for j in succ[i]:
            indeg[j] -= 1
            if indeg[j] == 0:
                heapq.heappush(heap, (rank[j], j))
    cycles = []
    if len(result) < n:
        stuck = [i for i in base_rank if i not in set(result)]
        cycles = [mods[i].name for i in stuck]
        result += stuck
    return [mods[i] for i in result], cycles
