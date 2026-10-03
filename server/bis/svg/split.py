"""Layer splitting on a z-order constraint DAG.

Layers are stacked in depth, which is only faithful if one global order of layers reproduces the
paint order wherever elements overlap. For every pair i < j (paint order) whose geometries overlap,
cluster(i) must sit below cluster(j). A merge is legal iff the contracted graph stays acyclic; the
final layer order is a topological sort. Non-overlapping elements impose no constraint, which lets
same-colour / same-group merges jump over unrelated elements.

Strategies: ``element`` (forced units only), ``group`` (top-level groups after unwrapping shared
wrappers), ``color`` (same paint), ``smart`` (same-paint touching/sibling merges, then cost-based
agglomeration to the layer budget, then connected-component split of a lone glyph) and ``single``.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
import shapely
import shapely.errors

from .colors import delta_e, rgb_to_lab
from .elements import Elem


@dataclass
class SplitParams:
    max_layers: int = 4                  # foreground layer budget (Icon Composer caps groups at 4)
    max_layers_mono: int = 3             # monochrome glyph / line icons read best with 2-3 planes
    min_layers: int = 2
    adjacency_pct: float = 0.75          # "touching" if gap <= this % of the viewBox diagonal
    same_color_de: float = 6.0           # CIE76 ΔE under which two solid paints count as "same"
    keep_fill_stroke_together: bool = True
    w_color: float = 0.8                 # agglomeration cost weights
    w_gap: float = 2.0
    w_group: float = 1.0
    w_z: float = 0.3
    w_level: float = 0.5                 # keep things that sit ON other things on their own plane
    tiny_bias: float = 0.65              # cost x (1 - tiny_bias*(1 - sqrt(area share))): small bits merge first
    auto_merge_cost: float = 0.25        # merge even under budget when the cost is this low
    lone_merge_cost: float = 0.45        # phase C: merge similar-sized parts of a lone cluster below this


# ----------------------------------------------------------------------------------------------
# pairwise analysis
# ----------------------------------------------------------------------------------------------
def compute_analysis(elems: Sequence[Elem], view_box, tol: float
                     ) -> Tuple[List[Tuple[int, int]], np.ndarray, List[Tuple[int, int]]]:
    """-> (overlap edges (i<j in paint order), symmetric gap matrix in SVG units,
    'inside' pairs (i, j): j (painted above i) lies >= 85 % inside i - j sits ON i)."""
    n = len(elems)
    gaps = np.zeros((n, n))
    edges: List[Tuple[int, int]] = []
    inside: List[Tuple[int, int]] = []
    if n < 2:
        return edges, gaps, inside
    eps_area = view_box[2] * view_box[3] * 1e-6
    geoms = np.array([e.geom(tol) for e in elems], dtype=object)
    shapely.prepare(geoms)
    empty = shapely.is_empty(geoms)
    gareas = shapely.area(geoms)
    for i in range(n - 1):
        rest = geoms[i + 1:]
        d = shapely.distance(geoms[i], rest)
        d = np.where(empty[i] | empty[i + 1:] | np.isnan(d), np.inf, d)
        touch = np.nonzero(d == 0)[0]
        if len(touch):
            try:
                areas = shapely.area(shapely.intersection(geoms[i], rest[touch]))
            except shapely.errors.GEOSException:
                grid = max(tol * 1e-3, 1e-9)
                areas = shapely.area(shapely.intersection(geoms[i], rest[touch], grid_size=grid))
            for k, a in zip(touch, areas):
                j = i + 1 + int(k)
                if a > eps_area:
                    edges.append((i, j))
                    if gareas[j] > 0 and a >= 0.85 * gareas[j]:
                        inside.append((i, j))
        gaps[i, i + 1:] = d
        gaps[i + 1:, i] = d
    return edges, gaps, inside


class Analysis:
    """Gap matrix + 'is painted below' edges restricted to a subset of elements (local indices),
    plus each element's stacking level (0 = sits on nothing in the subset, 1 = sits on a level-0
    element, ...) from the 'inside' relation."""

    def __init__(self, elems: Sequence[Elem], gaps: np.ndarray, edges: Sequence[Tuple[int, int]],
                 idxs: Sequence[int], view_box, inside: Sequence[Tuple[int, int]] = ()):
        self.idxs = list(idxs)
        self.local = {g: k for k, g in enumerate(self.idxs)}
        self.els = [elems[i] for i in self.idxs]
        ix = np.asarray(self.idxs, dtype=int)
        self.gap = gaps[np.ix_(ix, ix)] if len(ix) else np.zeros((0, 0))
        self.edges = [(self.local[i], self.local[j]) for i, j in edges if i in self.local and j in self.local]
        self.diag = math.hypot(view_box[2], view_box[3])
        self.view_box = view_box
        below: Dict[int, List[int]] = {}
        for i, j in inside:
            if i in self.local and j in self.local:
                below.setdefault(self.local[j], []).append(self.local[i])
        self.level = [0] * len(self.idxs)
        for j in range(len(self.idxs)):  # paint order: supports are always earlier
            if j in below:
                self.level[j] = 1 + max(self.level[i] for i in below[j])

    def consistent(self, assign: Sequence[int]) -> bool:
        return is_acyclic(self.edges, assign)

    def order_clusters(self, assign: Sequence[int], priority: Optional[Dict[int, float]] = None) -> List[int]:
        clusters = sorted(set(assign))
        if priority is None:
            priority = {c: float(np.mean([i for i, a in enumerate(assign) if a == c])) for c in clusters}
        return topo_order(self.edges, assign, clusters, priority)


def is_acyclic(edges: Sequence[Tuple[int, int]], assign: Sequence[int]) -> bool:
    succ: Dict[int, Set[int]] = {}
    indeg: Dict[int, int] = {c: 0 for c in set(assign)}
    for i, j in edges:
        a, b = assign[i], assign[j]
        if a != b and b not in succ.setdefault(a, set()):
            succ[a].add(b)
            indeg[b] += 1
    stack = [c for c, d in indeg.items() if d == 0]
    seen = 0
    while stack:
        c = stack.pop()
        seen += 1
        for nb in succ.get(c, ()):
            indeg[nb] -= 1
            if indeg[nb] == 0:
                stack.append(nb)
    return seen == len(indeg)


def topo_order(edges, assign, clusters: Sequence[int], priority: Dict[int, float]) -> List[int]:
    """Kahn's algorithm picking the ready cluster with the smallest priority (stable ordering)."""
    succ = {c: set() for c in clusters}
    indeg = {c: 0 for c in clusters}
    for i, j in edges:
        a, b = assign[i], assign[j]
        if a != b and a in succ and b in indeg and b not in succ[a]:
            succ[a].add(b)
            indeg[b] += 1
    ready = [(priority[c], c) for c in clusters if indeg[c] == 0]
    heapq.heapify(ready)
    out = []
    while ready:
        _, c = heapq.heappop(ready)
        out.append(c)
        for nb in succ[c]:
            indeg[nb] -= 1
            if indeg[nb] == 0:
                heapq.heappush(ready, (priority[nb], nb))
    if len(out) != len(clusters):  # cyclic (should not happen for validated splits)
        out = sorted(clusters, key=lambda c: priority[c])
    return out


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------
def _relabel(assign, a, b):
    return [a if x == b else x for x in assign]


def _group_affinity(m1: dict, m2: dict) -> float:
    """Shared ancestor depth / max group depth (1 = siblings in a group, 0 = unrelated groups).
    Two loose top-level shapes get 0.5: the document root is a weak group."""
    a1, a2 = m1.get("index_path", []), m2.get("index_path", [])
    if len(a1) <= 1 and len(a2) <= 1:
        return 0.5
    n = 0
    for x, y in zip(a1[:-1], a2[:-1]):
        if x != y:
            break
        n += 1
    mx = max(len(a1), len(a2)) - 1
    return n / mx if mx > 0 else 0.0


def affinity_matrix(metas: Sequence[dict]) -> np.ndarray:
    """Pairwise :func:`_group_affinity` (vectorised: parent-group prefixes as integer codes)."""
    n = len(metas)
    paths = [list(m.get("index_path", [])) for m in metas]
    lens = np.array([len(p) for p in paths], dtype=float)
    depth = max((len(p) - 1 for p in paths), default=0)
    out = np.zeros((n, n))
    if n == 0:
        return out
    if depth > 0:
        codes = np.full((n, depth), -1, dtype=np.int64)
        ids: Dict[tuple, int] = {}
        for i, p in enumerate(paths):
            for d in range(len(p) - 1):  # prefixes of the parent path a[:-1]
                codes[i, d] = ids.setdefault(tuple(p[:d + 1]), len(ids))
        common = np.zeros((n, n))
        run = np.ones((n, n), dtype=bool)
        for d in range(depth):
            c = codes[:, d]
            run &= (c[:, None] == c[None, :]) & (c[:, None] >= 0)
            common += run
        mx = np.maximum(lens[:, None], lens[None, :]) - 1
        out = np.where(mx > 0, common / np.maximum(mx, 1), 0.0)
    shallow = lens <= 1
    out[np.ix_(shallow, shallow)] = 0.5
    np.fill_diagonal(out, 0.0)
    return out


def _use_prefix(meta) -> tuple:
    for depth, a in enumerate(meta.get("ancestors", [])):
        if a.get("use_of"):
            return tuple(meta.get("index_path", [])[:depth + 1])
    return ()


def forced_units(els: Sequence[Elem], params: Optional[SplitParams] = None) -> List[int]:
    """Initial clusters: things that must stay together to composite correctly (fill + its
    stroke outline, a retained opacity group, one <use> instance)."""
    params = params or SplitParams()
    parent = list(range(len(els)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    first_by: Dict[tuple, int] = {}
    for i, e in enumerate(els):
        keys = []
        if e.opacity_group:
            keys.append(("og", e.opacity_group))
        if params.keep_fill_stroke_together:
            keys.append(("base", e.base))
        if e.meta.get("use_of"):
            keys.append(("use", _use_prefix(e.meta)))
        for k in keys:
            if k in first_by:
                ra, rb = find(first_by[k]), find(i)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
            else:
                first_by[k] = i
    return [find(i) for i in range(len(els))]


def same_paint(a: Elem, b: Elem, p: SplitParams) -> bool:
    if abs(a.total_opacity - b.total_opacity) > 0.05:
        return False
    if a.paint["type"] != "solid" or b.paint["type"] != "solid":
        return a.paint["key"] == b.paint["key"]
    return delta_e(a.paint["rgb"], b.paint["rgb"]) <= p.same_color_de


def _same_parent_group(a: Elem, b: Elem) -> bool:
    pa, pb = a.meta.get("index_path", []), b.meta.get("index_path", [])
    return len(pa) > 1 and len(pb) > 1 and pa[:-1] == pb[:-1]


def components(an: Analysis, members: Sequence[int], adj: float) -> List[List[int]]:
    comps, todo = [], set(members)
    while todo:
        stack = [todo.pop()]
        comp = []
        while stack:
            i = stack.pop()
            comp.append(i)
            for j in list(todo):
                if an.gap[i][j] <= adj:
                    todo.remove(j)
                    stack.append(j)
        comps.append(sorted(comp))
    return comps


def _unit_preserving(comps: List[List[int]], units: Sequence[int]) -> List[List[int]]:
    """Merge components that share a forced unit (fill+stroke, opacity group, <use> instance)."""
    label = {}
    for k, c in enumerate(comps):
        for i in c:
            label[i] = k
    parent = list(range(len(comps)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    first: Dict[int, int] = {}
    for i, u in enumerate(units):
        if i not in label:
            continue
        if u in first:
            a, b = find(label[first[u]]), find(label[i])
            if a != b:
                parent[max(a, b)] = min(a, b)
        else:
            first[u] = i
    merged: Dict[int, List[int]] = {}
    for k, c in enumerate(comps):
        merged.setdefault(find(k), []).extend(c)
    return [sorted(v) for v in merged.values()]


def _group_parts(an: Analysis, comps: List[List[int]], max_fg: int, p: SplitParams) -> List[List[int]]:
    """Agglomerate connected parts: cheap = close together, similar (log) size, same level.
    Merges while over budget or while the cheapest merge is below `lone_merge_cost`."""
    groups = [list(c) for c in comps]
    if len(groups) < 2:
        return groups
    areas = [max(e.area, 1e-12) for e in an.els]

    def stats(g):
        la = sum(math.log(areas[i]) for i in g) / len(g)
        lv = sum(an.level[i] for i in g) / len(g)
        return la, lv

    def cost(g1, g2):
        (a1, l1), (a2, l2) = stats(g1), stats(g2)
        gap = float(an.gap[np.ix_(g1, g2)].min())
        gap = gap if np.isfinite(gap) else an.diag
        return (2.0 * min(gap, an.diag) / an.diag + 0.9 * min(abs(a1 - a2), math.log(100.0)) / math.log(10.0)
                + p.w_level * min(abs(l1 - l2), 2.0) / 2.0)

    while len(groups) > 1:
        best = None
        for x in range(len(groups)):
            for y in range(x + 1, len(groups)):
                c = cost(groups[x], groups[y])
                if best is None or c < best[0]:
                    best = (c, x, y)
        c, x, y = best
        if len(groups) <= max_fg and c > p.lone_merge_cost:
            break
        groups[x] = sorted(groups[x] + groups.pop(y))
    return groups


class ClusterState:
    """Clusters with (1) an incrementally maintained 'painted below' DAG - a merge is legal iff
    it creates no cycle - and (2) cached linkage stats so merge costs are O(1)."""

    def __init__(self, an: Analysis, assign: List[int], p: SplitParams):
        self.an, self.els, self.p = an, an.els, p
        self.assign = list(assign)
        self.members: Dict[int, List[int]] = {}
        for i, c in enumerate(self.assign):
            self.members.setdefault(c, []).append(i)
        self.ver = {c: 0 for c in self.members}
        self.succ: Dict[int, Set[int]] = {c: set() for c in self.members}
        self.pred: Dict[int, Set[int]] = {c: set() for c in self.members}
        for i, j in an.edges:
            a, b = self.assign[i], self.assign[j]
            if a != b:
                self.succ[a].add(b)
                self.pred[b].add(a)
        labs = [rgb_to_lab(e.rgb) for e in self.els]
        wts = [max(e.area, 1e-9) for e in self.els]
        self.lab = {c: [sum(labs[i][k] * wts[i] for i in m) for k in range(3)] for c, m in self.members.items()}
        self.w = {c: sum(wts[i] for i in m) for c, m in self.members.items()}
        self.zsum = {c: float(sum(m)) for c, m in self.members.items()}
        lv = an.level
        self.lsum = {c: sum(lv[i] * wts[i] for i in m) for c, m in self.members.items()}
        self.total_w = float(sum(wts)) or 1.0
        aff_e = affinity_matrix([e.meta for e in self.els])
        # cluster x cluster linkage (min gap, max affinity): two vectorised reductions
        cl = list(self.members)
        k = len(cl)
        gap_c = np.full((len(self.els), k), np.inf)
        aff_c = np.zeros((len(self.els), k))
        for t, c in enumerate(cl):
            gap_c[:, t] = an.gap[:, self.members[c]].min(axis=1)
            aff_c[:, t] = aff_e[:, self.members[c]].max(axis=1)
        gap_cc = np.empty((k, k))
        aff_cc = np.empty((k, k))
        for t, c in enumerate(cl):
            gap_cc[t] = gap_c[self.members[c]].min(axis=0)
            aff_cc[t] = aff_c[self.members[c]].max(axis=0)
        self.gap: Dict[Tuple[int, int], float] = {}
        self.aff: Dict[Tuple[int, int], float] = {}
        for x in range(k):
            for y in range(x + 1, k):
                a, b = cl[x], cl[y]
                key = (min(a, b), max(a, b))
                self.gap[key] = float(gap_cc[x, y])
                self.aff[key] = float(aff_cc[x, y])

    def cost(self, a: int, b: int) -> float:
        k = (min(a, b), max(a, b))
        la = [v / self.w[a] for v in self.lab[a]]
        lb = [v / self.w[b] for v in self.lab[b]]
        de = math.sqrt(sum((x - y) ** 2 for x, y in zip(la, lb)))
        zdist = abs(self.zsum[a] / len(self.members[a]) - self.zsum[b] / len(self.members[b])) / max(1, len(self.els))
        diag = self.an.diag
        gap = self.gap[k] if np.isfinite(self.gap[k]) else diag
        dlevel = abs(self.lsum[a] / self.w[a] - self.lsum[b] / self.w[b])
        base = (self.p.w_color * min(de, 100.0) / 100.0 + self.p.w_gap * min(gap, diag) / diag
                + self.p.w_group * (1 - self.aff[k]) + self.p.w_z * zdist
                + self.p.w_level * min(dlevel, 2.0) / 2.0)
        share = min(self.w[a], self.w[b]) / self.total_w
        return base * (1.0 - self.p.tiny_bias * (1.0 - math.sqrt(share)))

    def _reach(self, src: int, dst: int) -> bool:
        stack = [c for c in self.succ[src] if c != dst]
        seen = set(stack)
        while stack:
            c = stack.pop()
            for nb in self.succ[c]:
                if nb == dst:
                    return True
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        return False

    def try_merge(self, a: int, b: int) -> bool:
        if a == b or self._reach(a, b) or self._reach(b, a):
            return False
        for i in self.members[b]:
            self.assign[i] = a
        self.members[a] += self.members.pop(b)
        self.lab[a] = [x + y for x, y in zip(self.lab[a], self.lab.pop(b))]
        self.w[a] += self.w.pop(b)
        self.zsum[a] += self.zsum.pop(b)
        self.lsum[a] += self.lsum.pop(b)
        for c in self.succ[b]:
            self.pred[c].discard(b)
            if c != a:
                self.pred[c].add(a)
        for c in self.pred[b]:
            self.succ[c].discard(b)
            if c != a:
                self.succ[c].add(a)
        self.succ[a] = (self.succ[a] | self.succ.pop(b)) - {a, b}
        self.pred[a] = (self.pred[a] | self.pred.pop(b)) - {a, b}
        self.gap.pop((min(a, b), max(a, b)), None)
        self.aff.pop((min(a, b), max(a, b)), None)
        for c in self.members:
            if c == a:
                continue
            ka, kb = (min(a, c), max(a, c)), (min(b, c), max(b, c))
            self.gap[ka] = min(self.gap[ka], self.gap.pop(kb))
            self.aff[ka] = max(self.aff[ka], self.aff.pop(kb))
        self.ver[a] += 1
        del self.ver[b]
        return True


# ----------------------------------------------------------------------------------------------
# strategies
# ----------------------------------------------------------------------------------------------
def split(an: Analysis, strategy: str = "smart", params: Optional[SplitParams] = None
          ) -> Tuple[List[List[int]], dict]:
    """Split the analysed elements into layers. Returns (layers as lists of LOCAL indices,
    bottom -> top, debug info)."""
    params = params or SplitParams()
    els = an.els
    n = len(els)
    info: dict = {"strategy": strategy}
    if n == 0:
        return [], info
    adj = an.diag * params.adjacency_pct / 100.0
    assign = forced_units(els, params)

    if strategy == "element":
        pass

    elif strategy == "single":
        assign = [0] * n

    elif strategy == "group":
        paths = [e.meta.get("index_path", []) for e in els]
        depth = 0
        while all(len(p) > depth + 1 for p in paths) and len({p[depth] for p in paths}) == 1:
            depth += 1
        key_first: Dict[tuple, int] = {}
        for i, p in enumerate(paths):
            key = ("g", p[depth]) if len(p) > depth + 1 else ("bare", i)
            if key in key_first:
                assign = _relabel(assign, assign[key_first[key]], assign[i])
            else:
                key_first[key] = i
        info["unwrapDepth"] = depth
        if not an.consistent(assign):
            # weaving groups: fall back to forced units for the offending groups
            info["note"] = "group order is inconsistent with paint order; split further"
            assign = forced_units(els, params)

    elif strategy == "color":
        st = ClusterState(an, assign, params)
        for i in range(n):
            for j in range(i):
                if st.assign[i] != st.assign[j] and same_paint(els[i], els[j], params):
                    if st.try_merge(st.assign[j], st.assign[i]):
                        break
        assign = st.assign

    elif strategy == "smart":
        assign, sinfo = _smart(an, assign, params, adj)
        info.update(sinfo)
    else:
        raise ValueError(f"unknown split strategy '{strategy}'")

    order = an.order_clusters(assign)
    layers = [[i for i in range(n) if assign[i] == c] for c in order]
    info["consistent"] = an.consistent(assign)
    info["layers"] = len(layers)
    return layers, info


def _smart(an: Analysis, assign: List[int], params: SplitParams, adj: float) -> Tuple[List[int], dict]:
    els = an.els
    n = len(els)
    info: dict = {}
    st = ClusterState(an, assign, params)
    # phase A: same paint & touching / overlapping, closest pairs first
    pairs = sorted((an.gap[i][j], i, j) for i in range(n) for j in range(i + 1, n)
                   if an.gap[i][j] <= adj and same_paint(els[i], els[j], params))
    # phase A2: same paint & same (non-root) parent group, any distance (eyes, rays, ...)
    pairs += sorted((an.gap[i][j], i, j) for i in range(n) for j in range(i + 1, n)
                    if _same_parent_group(els[i], els[j]) and same_paint(els[i], els[j], params))
    for _, i, j in pairs:
        a, b = st.assign[i], st.assign[j]
        if a != b:
            st.try_merge(min(a, b), max(a, b))
    n_paints = len({e.paint_key for e in els})
    max_fg = params.max_layers if n_paints > 1 else min(params.max_layers, params.max_layers_mono)
    info["budget"] = max_fg
    # phase B: agglomerate the cheapest legal pair while over budget, or while "obviously cheap"
    heap = []
    alive = list(st.members)
    for x in range(len(alive)):
        for y in range(x + 1, len(alive)):
            a, b = alive[x], alive[y]
            heap.append((st.cost(a, b), a, b, st.ver[a], st.ver[b]))
    heapq.heapify(heap)
    while heap and len(st.members) > 1:
        over_budget = len(st.members) > max_fg
        cost, a, b, va, vb_ = heapq.heappop(heap)
        if a not in st.members or b not in st.members or st.ver[a] != va or st.ver[b] != vb_:
            continue
        if not over_budget and cost > params.auto_merge_cost:
            break
        if st.try_merge(a, b):
            for c in st.members:
                if c != a:
                    lo, hi = min(a, c), max(a, c)
                    heapq.heappush(heap, (st.cost(lo, hi), lo, hi, st.ver[lo], st.ver[hi]))
    if len(st.members) > max_fg:
        info["note"] = "layer budget not reachable without breaking the z-order"
    assign = st.assign
    # phase C: a single foreground cluster (lone glyph / monochrome art) -> split it into
    # connected parts, then re-group parts of similar size and stacking level (petals vs dots)
    fg = sorted(set(assign))
    if len(fg) < params.min_layers and len(fg) == 1 and n > 1:
        comps = _unit_preserving(components(an, list(range(n)), adj), forced_units(els, params))
        groups = _group_parts(an, comps, max_fg, params)
        if len(groups) > 1:
            trial = [0] * n
            for g, comp in enumerate(groups):
                for i in comp:
                    trial[i] = g
            if an.consistent(trial):
                assign = trial
                info["note"] = "single-colour art split into parts of similar size"
    return assign, info
