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
