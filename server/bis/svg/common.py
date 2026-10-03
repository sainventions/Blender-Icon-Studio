"""Small shared helpers for the SVG pipeline: XML namespaces, number parsing/formatting,
the SVG -> art space mapping, hashing and atomic file writes."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Tuple

from picosvg.svg_transform import Affine2D

#: Bump whenever the output of the pipeline changes for the same input (invalidates caches).
PIPELINE_VERSION = "bis-svg-1.4"

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
XLINK_HREF = f"{{{XLINK_NS}}}href"

BBoxT = Tuple[float, float, float, float]


def local(tag) -> str:
    """Local name of an lxml tag ('' for comments / processing instructions)."""
    return tag.split("}", 1)[-1] if isinstance(tag, str) else ""


def ns_of(tag) -> str:
    return tag[1:].split("}", 1)[0] if isinstance(tag, str) and tag.startswith("{") else ""


def q(tag: str) -> str:
    """Qualified SVG tag name."""
    return f"{{{SVG_NS}}}{tag}"


_NUM_RE = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(%|px|pt|pc|mm|cm|in|em|ex)?$")
_UNIT = {"pt": 4 / 3, "pc": 16.0, "mm": 3.7795275591, "cm": 37.795275591, "in": 96.0, "em": 16.0, "ex": 8.0}


def num(s, default: float = 0.0, pct_of: Optional[float] = None) -> float:
    """Parse an SVG length / number. `%` is resolved against `pct_of` (1.0 if None)."""
    if s is None:
        return default
    m = _NUM_RE.match(str(s).strip())
    if not m:
        return default
    v = float(m.group(1))
    unit = m.group(2)
    if unit == "%":
        return v / 100.0 * (pct_of if pct_of is not None else 1.0)
    return v * _UNIT.get(unit, 1.0)


def fmt(v: float, nd: int = 4) -> str:
    """Compact float formatting for SVG output."""
    s = f"{v:.{nd}f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "", "-") else s


def rnd(v: float, nd: int = 6) -> float:
    """round() without negative zero (keeps JSON / UI output clean)."""
    return round(float(v), nd) + 0.0


def num_list(s: Optional[str]) -> list[float]:
    if not s:
        return []
    out = []
    for tok in re.split(r"[\s,]+", s.strip()):
        if tok:
            try:
                out.append(float(tok))
            except ValueError:
                pass
    return out


# ----------------------------------------------------------------------------------------------
# Art space
# ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ArtSpace:
    """SVG user space (viewBox, y down) -> art space (centre origin, y up, longer side = 2)."""

    view_box: Tuple[float, float, float, float]

    @property
    def cx(self) -> float:
        return self.view_box[0] + self.view_box[2] / 2.0

    @property
    def cy(self) -> float:
        return self.view_box[1] + self.view_box[3] / 2.0

    @property
    def size(self) -> float:
        """Longer viewBox side in SVG units."""
        return max(self.view_box[2], self.view_box[3])

    @property
    def k(self) -> float:
        """Art units per SVG unit."""
        return 2.0 / self.size

    @property
    def matrix(self) -> Affine2D:
        k = self.k
        return Affine2D(k, 0.0, 0.0, -k, -self.cx * k, self.cy * k)

    @property
    def inverse(self) -> Affine2D:
        s = 1.0 / self.k
        return Affine2D(s, 0.0, 0.0, -s, self.cx, self.cy)

    def pt(self, p: Sequence[float]) -> Tuple[float, float]:
        k = self.k
        return ((p[0] - self.cx) * k, (self.cy - p[1]) * k)

    def bbox(self, b: Sequence[float]) -> BBoxT:
        """SVG bbox (minx, miny, maxx, maxy) -> art bbox (y flipped, so min/max swap)."""
        x0, y1 = self.pt((b[0], b[1]))
        x1, y0 = self.pt((b[2], b[3]))
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    def length(self, v: float) -> float:
        return v * self.k

    def area_fraction(self, a_svg: float) -> float:
        """SVG area -> fraction of the art square (the square of the longer side)."""
        return a_svg / (self.size * self.size)

    def square_view_box(self) -> Tuple[float, float, float, float]:
        """viewBox of the art square (−1..1 on both axes) in SVG units."""
        s = self.size
        return (self.cx - s / 2.0, self.cy - s / 2.0, s, s)

    @property
    def area(self) -> float:
        return self.view_box[2] * self.view_box[3]

    @property
    def diag(self) -> float:
        return math.hypot(self.view_box[2], self.view_box[3])


def affine_tuple(m: Affine2D) -> Tuple[float, float, float, float, float, float]:
    return (m.a, m.b, m.c, m.d, m.e, m.f)


def compose(*ms: Affine2D) -> Affine2D:
    """Compose affines left-to-right: compose(A, B)(p) = B(A(p))."""
    out = Affine2D.identity()
    for m in ms:
        out = Affine2D.compose_ltr((out, m))
    return out


def is_similarity(m: Affine2D, tol: float = 1e-6) -> bool:
    """True if the linear part is a uniform scale + rotation (+ optional reflection)."""
    c1 = (m.a, m.b)
    c2 = (m.c, m.d)
    n1, n2 = math.hypot(*c1), math.hypot(*c2)
    if n1 <= 0 or n2 <= 0:
        return False
    return abs(n1 - n2) <= tol * max(n1, n2) * 10 and abs(c1[0] * c2[0] + c1[1] * c2[1]) <= tol * n1 * n2 * 10


def matrix_scale(m: Affine2D) -> float:
    """Geometric-mean scale factor of an affine (sqrt |det|)."""
    return math.sqrt(abs(m.a * m.d - m.b * m.c))


# ----------------------------------------------------------------------------------------------
# hashing / io
# ----------------------------------------------------------------------------------------------
def sha1(*parts: Any) -> str:
    h = hashlib.sha1()
    for p in parts:
        if isinstance(p, bytes):
            h.update(p)
        elif isinstance(p, str):
            h.update(p.encode("utf-8"))
        else:
            h.update(json.dumps(p, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:  # Windows: target briefly open in another process
                if attempt == 19:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def dedupe(items: Iterable[str], limit: int = 40) -> list[str]:
    """Order-preserving de-duplication with a cap (the UI shows these to the user)."""
    seen: dict[str, int] = {}
    for it in items:
        seen[it] = seen.get(it, 0) + 1
    out = [k if n == 1 else f"{k} (×{n})" for k, n in seen.items()]
    if len(out) > limit:
        out = out[:limit] + [f"… {len(out) - limit} more warnings"]
    return out
