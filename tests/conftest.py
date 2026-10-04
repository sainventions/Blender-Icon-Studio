"""Shared pytest setup: puts ``server/`` on sys.path (the backend package is ``bis``) and offers
small fixtures for the SVG pipeline tests. Nothing here is autouse - other suites are unaffected."""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

CORPUS_DIR = ROOT / "samle icons"            # the user's 68 real icons (folder name typo is intentional)
SVGTESTS_DIR = ROOT / "docs" / "research" / "svgtests"


def pytest_configure(config):
    config.addinivalue_line("markers", "corpus: runs over the full 68-icon corpus (slower)")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def corpus_dir() -> Path:
    return CORPUS_DIR


@pytest.fixture(scope="session")
def svgtests_dir() -> Path:
    return SVGTESTS_DIR


def make_project(result, project_id: str = "p1", name: str = "Test"):
    """Assemble a models.Project from an svg ImportResult (what the server does on POST /projects)."""
    from bis.models import Project

    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    return Project(id=project_id, name=name, createdAt=now, updatedAt=now, source=result.source,
                   elements=result.elements, layers=result.layers, canvas=result.canvas)


@pytest.fixture
def import_icon(tmp_path):
    """import_icon(path_or_bytes, strategy='smart') -> (ImportResult, Project, project_dir)."""
    import bis.svg as svg

    counter = {"n": 0}

    def _do(src, strategy: str = "smart", name: str = "icon.svg"):
        counter["n"] += 1
        data = Path(src).read_bytes() if isinstance(src, (str, Path)) else src
        if isinstance(src, (str, Path)):
            name = Path(src).name
        pdir = tmp_path / f"proj{counter['n']}"
        res = svg.import_svg(data, name, pdir, strategy)
        return res, make_project(res, project_id=f"p{counter['n']}", name=Path(name).stem), pdir

    return _do


def assert_rule_stack(project, bundle, gap=None, tol: float = 2e-4, presets=None):
    """The layers of `project` (Project or its JSON) form the overlap-aware real-height stack (PLAN §11 round 8)
    for the geometry `bundle` (GeometryBundle or its JSON - footprints, regions and maxRadius measured from the
    exported splines, independently of the element store the server stacked with): re-stacking with gap `gap`
    (None = the presets' stackGap) reproduces every z, the stack is recognised as a rule stack, and no two layers
    whose footprints touch have overlapping z ranges. Returns the shapes."""
    from bis import stacking
    from bis.models import Project

    proj = project if isinstance(project, Project) else Project.model_validate(project)
    art = proj.canvas.art
    shapes = stacking.shapes_from_bundle(bundle, proj.layers, art.scale)
    want = stacking.geometry_rules(presets)["stackGap"] if gap is None else gap
    copy = [L.model_copy(deep=True) for L in proj.layers]
    zs = stacking.restack(copy, shapes, gap=want, art_scale=art.scale, art_offset=art, presets=presets)
    got = [L.depth.z for L in proj.layers]
    assert got == pytest.approx(zs, abs=tol), (proj.name, got, zs)
    if proj.layers:
        assert stacking.stack_gap(proj.layers, shapes, art_scale=art.scale, art_offset=art, presets=presets) is not None
    assert stacking.interpenetrations(proj.layers, shapes, art_scale=art.scale, art_offset=art) == [], proj.name
    return shapes
