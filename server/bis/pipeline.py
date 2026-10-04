"""Lazy façade over the SVG pipeline package ``bis.svg`` (workstream A, PLAN §4 A).

The server imports ``bis.svg`` on first use so it starts (and serves presets, samples, jobs, …) even while the
pipeline is unavailable. Tests inject a fake module with the same API (see ``bis.testing.FakeSvg``).
All calls are synchronous and CPU-bound — callers run them in a worker thread.
"""
from __future__ import annotations

import importlib
import logging
import threading
from pathlib import Path
from typing import Any

from .models import GeometryBundle, Layer, Project

log = logging.getLogger("bis.pipeline")


class SvgPipelineUnavailable(RuntimeError):
    pass


class SvgPipeline:
    def __init__(self, module: Any | None = None, module_name: str = "bis.svg") -> None:
        self._module = module
        self._module_name = module_name
        self._error: str | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------------------------------ module
    @property
    def module(self) -> Any:
        if self._module is None:
            with self._lock:
                if self._module is None:
                    try:
                        self._module = importlib.import_module(self._module_name)
                        self._error = None
                    except Exception as e:  # ImportError or an error inside the package
                        self._error = f"{type(e).__name__}: {e}"
                        log.warning("SVG pipeline %s unavailable: %s", self._module_name, self._error)
                        raise SvgPipelineUnavailable(
                            f"SVG pipeline ({self._module_name}) is not available: {self._error}"
                        ) from e
        return self._module

    def available(self) -> bool:
        try:
            self.module
            return True
        except SvgPipelineUnavailable:
            return False

    @property
    def error(self) -> str | None:
        return self._error

    # ------------------------------------------------------------------------------------------ API (PLAN §4 A)
    def import_svg(self, svg_bytes: bytes, filename: str, project_dir: Path, strategy: str = "smart") -> Any:
        return self.module.import_svg(svg_bytes, filename, project_dir, strategy)

    def split_layers(self, project_dir: Path, project: Project, strategy: str) -> list[Layer]:
        return _layers(self.module.split_layers(project_dir, project, strategy))

    def merge_layers(self, project_dir: Path, project: Project, layer_ids: list[str]) -> list[Layer]:
        return _layers(self.module.merge_layers(project_dir, project, layer_ids))

    def split_layer(self, project_dir: Path, project: Project, layer_id: str, mode: str = "elements") -> list[Layer]:
        return _layers(self.module.split_layer(project_dir, project, layer_id, mode))

    def move_elements(self, project_dir: Path, project: Project, element_ids: list[str],
                      to_layer_id: str | None) -> list[Layer] | None:
        """A's z-order-aware move when the pipeline provides one; None = caller uses its fallback."""
        fn = getattr(self.module, "move_elements", None)
        if fn is None:
            return None
        return _layers(fn(project_dir, project, element_ids, to_layer_id))

    def layer_auto_modes(self, project_dir: Path, project: Project) -> dict[str, str] | None:
        """{layer id: the mode A's tiling heuristic picks for the layer's art}; None when the pipeline has no
        such op (tests' fake pipeline). A layer whose mode differs was set by the user."""
        fn = getattr(self.module, "layer_auto_modes", None)
        if fn is None:
            return None
        return {str(k): str(v) for k, v in fn(project_dir, project).items()}

    def layer_shapes(self, project_dir: Path, project: Project, mode: str | None = None) -> dict[str, Any] | None:
        """{layer id: bis.stacking.LayerShape} (maxRadius + XY footprint + pieces, as built in `mode`; None = each
        layer's own) for the overlap-aware stack; None when the pipeline has no such op (tests' fake pipeline)."""
        fn = getattr(self.module, "layer_shapes", None)
        if fn is None:
            return None
        return dict(fn(project_dir, project, mode))

    def element_soft_alpha(self, project_dir: Path, project: Project) -> dict[str, bool] | None:
        """{raster element id: soft alpha?} (``Element.softAlpha``, PLAN §11 round 9) from A's element store; None
        when the pipeline has no such op (tests' fake pipeline)."""
        fn = getattr(self.module, "element_soft_alpha", None)
        if fn is None:
            return None
        return {str(k): bool(v) for k, v in fn(project_dir, project).items()}

    def build_geometry(self, project_dir: Path, project: Project, url_prefix: str) -> GeometryBundle:
        bundle = self.module.build_geometry(project_dir, project, url_prefix)
        return bundle if isinstance(bundle, GeometryBundle) else GeometryBundle.model_validate(_plain(bundle))

    def geometry_path(self, project_dir: Path, bundle: GeometryBundle) -> Path:
        return Path(self.module.geometry_path(project_dir, bundle))

    def thumbnail_png(self, svg_bytes: bytes, size: int = 256) -> bytes:
        try:
            return self.module.thumbnail_png(svg_bytes, size)
        except SvgPipelineUnavailable:
            return resvg_thumbnail(svg_bytes, size)
        except Exception as e:  # a thumbnail must never break a listing or an import
            log.warning("bis.svg.thumbnail_png failed (%s); falling back to resvg", e)
            return resvg_thumbnail(svg_bytes, size)

    def layer_thumbnail_png(self, project_dir: Path, project: Project, layer_id: str, size: int = 96) -> bytes:
        return self.module.layer_thumbnail_png(project_dir, project, layer_id, size)


def _plain(obj: Any) -> Any:
    from .util import to_jsonable

    return to_jsonable(obj)


def _layers(value: Any) -> list[Layer]:
    return [v if isinstance(v, Layer) else Layer.model_validate(_plain(v)) for v in value]


def resvg_thumbnail(svg_bytes: bytes, size: int = 256) -> bytes:
    """Fallback thumbnail (square, aspect-fit, transparent) straight from resvg + Pillow."""
    import io

    import resvg_py
    from PIL import Image

    png = bytes(resvg_py.svg_to_bytes(svg_string=svg_bytes.decode("utf-8", "replace"), width=size))
    with Image.open(io.BytesIO(png)) as im:
        im = im.convert("RGBA")
        im.thumbnail((size, size), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(im, ((size - im.width) // 2, (size - im.height) // 2))
        buf = io.BytesIO()
        canvas.save(buf, "PNG", optimize=True)
        return buf.getvalue()
