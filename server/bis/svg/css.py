"""Minimal CSS support for SVG ``<style>`` sheets (Illustrator / Figma / Inkscape exports).

Supported: type / ``.class`` / ``#id`` / ``*`` compound selectors joined by descendant (or ``>``,
treated as descendant) combinators, selector lists, specificity + source order, ``!important``
stripped, comments and @-rules dropped. Anything else is ignored with a warning."""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .common import local

Compound = Tuple[Optional[str], List[str], Optional[str]]  # (tag, classes, id)
Rule = Tuple[List[Compound], Tuple[int, int, int], int, Dict[str, str]]


def parse_decls(text: str) -> Dict[str, str]:
    """'fill:#fff; stroke : red !important' -> {'fill': '#fff', 'stroke': 'red'}."""
    out: Dict[str, str] = {}
    for part in (text or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            v = v.replace("!important", "").strip()
            k = k.strip().lower()
            if k and v:
                out[k] = v
    return out


def _strip_at_rules(css: str) -> str:
    out, i = [], 0
    while i < len(css):
        if css[i] == "@":
            j = i
            while j < len(css) and css[j] not in "{;":
                j += 1
            if j < len(css) and css[j] == "{":
                depth, j = 1, j + 1
                while j < len(css) and depth:
                    depth += {"{": 1, "}": -1}.get(css[j], 0)
                    j += 1
            i = j + 1
            continue
        out.append(css[i])
        i += 1
    return "".join(out)


_COMPOUND = re.compile(r"^(\*|[A-Za-z][\w-]*)?((?:[.#][\w-]+)*)$")


def parse_stylesheet(css: str, warnings: List[str]) -> List[Rule]:
    """Parse a stylesheet into rules sorted by (specificity, source order)."""
    css = _strip_at_rules(re.sub(r"/\*.*?\*/", "", css, flags=re.S))
    rules: List[Rule] = []
    for order, m in enumerate(re.finditer(r"([^{}]+)\{([^{}]*)\}", css)):
        decls = parse_decls(m.group(2))
        for sel in m.group(1).split(","):
            sel = sel.strip()
            if not sel:
                continue
            compounds: List[Compound] = []
            ok = True
            for tok in sel.replace(">", " ").split():
                mm = _COMPOUND.match(tok)
                if not mm:
                    ok = False
                    break
                tag = mm.group(1) if mm.group(1) not in (None, "*") else None
                classes = re.findall(r"\.([\w-]+)", mm.group(2))
                ids = re.findall(r"#([\w-]+)", mm.group(2))
                compounds.append((tag, classes, ids[0] if ids else None))
            if not ok or not compounds:
                warnings.append(f"CSS selector '{sel}' is not supported and was ignored")
                continue
            spec = (sum(1 for c in compounds if c[2]), sum(len(c[1]) for c in compounds),
                    sum(1 for c in compounds if c[0]))
            rules.append((compounds, spec, order, decls))
    rules.sort(key=lambda r: (r[1], r[2]))
    return rules


def _compound_matches(el, comp: Compound) -> bool:
    tag, classes, el_id = comp
    if tag and local(el.tag) != tag:
        return False
    if el_id and el.get("id") != el_id:
        return False
    if classes:
        have = set((el.get("class") or "").split())
        if not set(classes) <= have:
            return False
    return True


def selector_matches(el, compounds: List[Compound]) -> bool:
    if not _compound_matches(el, compounds[-1]):
        return False
    anc = el.getparent()
    for comp in reversed(compounds[:-1]):
        while anc is not None and not _compound_matches(anc, comp):
            anc = anc.getparent()
        if anc is None:
            return False
        anc = anc.getparent()
    return True
