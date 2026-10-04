"""Round 9 (PLAN §11): raster layers. A raster with a CRISP alpha silhouette is a real body like vector art (normal
depth defaults, overlap-aware stack); only SOFT-alpha rasters (glows, shines, shadows - the worker's
``materials.art_alpha_is_soft``: ≥ 25 % of the covered pixels partly transparent) are flat cards. Soft cards that sit
on body layers BELOW them (baked highlights: Find Device's sweep) import hidden with a source warning; the others
(a glow under the art: Vanced Neon) stay visible. Import, re-split, merge / move, looks, and projects imported before
round 9 (softness measured from the stored image files)."""
from __future__ import annotations

import base64
import io
import json
import re
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from conftest import CORPUS_DIR, ROOT, assert_rule_stack

import bis.svg as svg
from bis import stacking
from bis.svg import raster
from bis.svg.elements import ElementStore, STORE_FILE
from bis.svg.layers import BAKED_WARNING, DEFAULT_BEVEL, DEFAULT_INFLATE, DEFAULT_THICKNESS

BODY = (DEFAULT_THICKNESS, DEFAULT_BEVEL, DEFAULT_INFLATE)
CARD = (stacking.IMAGE_CARD["thickness"], stacking.IMAGE_CARD["bevel"], 0.0)


def _depth(L):
    return L.depth.thickness, L.depth.bevel, L.depth.inflate


def _bundle(pdir, project):
    return svg.build_geometry(pdir, project, f"/files/projects/{project.id}", texture_size=64)


# ---------------------------------------------------------------------------------------------- synthetic art
def _png(alpha: np.ndarray, rgb=(255, 255, 255)) -> str:
    a = np.zeros((*alpha.shape, 4), np.uint8)
    a[..., :3] = rgb
    a[..., 3] = np.clip(alpha, 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(a, "RGBA").save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


_N = 64
_YY, _XX = np.mgrid[0:_N, 0:_N] + 0.5
_R = np.hypot(_XX - _N / 2, _YY - _N / 2)
GLOW = 255 * np.exp(-(_R / 14.0) ** 2)                       # a soft halo: every covered pixel partly transparent
SHINE = np.where(_R < 30, 40 + 80 * (_XX / _N), 0)            # a baked 16-47 % highlight
CRISP = 255 * np.clip(28.0 - _R + 0.5, 0, 1)                  # an opaque disc, anti-aliased edge only


def synth(under: bool = True, disc: bool = True, over: bool = True, crisp: bool = True, over2: bool = False) -> bytes:
    """Plate + (soft glow UNDER the disc) + orange vector disc + (soft shine OVER the disc) + (crisp raster disc in
    the corner, on nothing)."""
    p = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 500 500">',
         '<rect x="0" y="0" width="500" height="500" rx="110" fill="#1d4ed8"/>']
    if under:
        p.append(f'<image x="60" y="60" width="380" height="380" href="{_png(GLOW, (255, 220, 120))}"/>')
    if disc:
        p.append('<circle cx="250" cy="250" r="110" fill="#f59e0b"/>')
    if over:
        p.append(f'<image x="150" y="150" width="200" height="200" href="{_png(SHINE)}"/>')
    if over2:
        p.append(f'<image x="170" y="170" width="160" height="160" href="{_png(SHINE, (255, 240, 200))}"/>')
    if crisp:
        p.append(f'<image x="380" y="380" width="90" height="90" href="{_png(CRISP, (220, 30, 60))}"/>')
    p.append("</svg>")
    return "\n".join(p).encode()


# ---------------------------------------------------------------------------------------------- the measure
def test_alpha_softness_is_the_workers_rule():
    """Share of COVERED pixels (alpha > 0.02) that are partly transparent (alpha < 0.98), whole image - the worker's
    ``art_alpha_is_soft`` (8-bit alpha a read as a · (1/255) float32: covered from 6, opaque from 250)."""
    def rgba(alpha):
        a = np.zeros((*np.shape(alpha), 4), np.uint8)
        a[..., 3] = alpha
        return a

    assert raster.SOFT_ALPHA == 0.25
    assert raster.alpha_softness(rgba(np.full((4, 4), 255))) == 0.0
    assert raster.alpha_softness(rgba(np.zeros((4, 4)))) == 0.0                     # nothing covered
    assert raster.alpha_softness(rgba([[5, 6, 249, 250]])) == pytest.approx(2 / 3)    # 5 uncovered, 250 opaque
    assert raster.alpha_softness(rgba([[0, 128, 255, 255]])) == pytest.approx(1 / 3)
    assert raster.alpha_softness(np.zeros((3, 3, 3), np.uint8)) == 0.0               # no alpha channel: opaque
    a16 = np.zeros((1, 2, 4), np.uint16)
    a16[..., 3] = [65535, 30000]
    assert raster.alpha_softness(a16) == pytest.approx(0.5)
    assert raster.alpha_softness(rgba(GLOW)) > 0.9 and raster.alpha_softness(rgba(SHINE)) > 0.9
    assert raster.alpha_softness(rgba(CRISP)) < 0.15
    assert raster.is_soft_alpha(0.25) and not raster.is_soft_alpha(0.2499) and not raster.is_soft_alpha(None)
    assert raster.file_alpha_softness(Path("does/not/exist.png")) is None


def test_soft_alpha_rule_matches_the_worker_source():
    """The measure is a PORT of ``blender_worker/materials.py`` ``art_alpha_is_soft`` (bpy-only module: its source is
    read). A drift (threshold or the covered / partly-transparent cut-offs) would import a raster as a body that the
    worker renders as a soft blended card, or the other way round (round 8: the half_height port went stale silently)."""
    src = (ROOT / "blender_worker" / "materials.py").read_text(encoding="utf-8")
    m = re.search(r"^SOFT_ALPHA\s*=\s*([0-9.]+)", src, re.M)
    assert m and float(m.group(1)) == raster.SOFT_ALPHA
    body = src[src.index("def art_alpha_is_soft"):]
    body = body[:body.index("\ndef ")]
    assert re.search(r"\bcov\s*=\s*a\s*>\s*0\.02\b", body), "worker: covered = alpha > 0.02"
    assert re.search(r"\bcov\s*&\s*\(\s*a\s*<\s*0\.98\s*\)", body), "worker: partly transparent = alpha < 0.98"
    assert re.search(r"frac\s*>=\s*SOFT_ALPHA", body)


def _png16(alpha16: np.ndarray) -> bytes:
    a = np.zeros((*alpha16.shape, 4), np.uint16)
    a[..., :3] = 50000
    a[..., 3] = alpha16
    import cv2

    ok, buf = cv2.imencode(".png", cv2.cvtColor(a, cv2.COLOR_RGBA2BGRA))
    assert ok
    return buf.tobytes()


def test_sixteen_bit_png_alpha_is_read_at_full_precision(import_icon):
    """Blender reads a 16-bit PNG's alpha as a float (a / 65535); PIL keeps only the high byte. A ring at alpha 64200
    (0.9796, partly transparent for the worker) became 250/255 = 0.9804 (opaque) on the server: the raster imported as
    a crisp BODY while the worker blended it as a soft card (measured in Blender: worker 0.553, server 0.0)."""
    yy, xx = np.mgrid[0:64, 0:64] + 0.5
    r = np.hypot(xx - 32, yy - 32)
    a16 = np.where(r < 20, 65535, np.where(r < 30, 64200, 0)).astype(np.uint16)
    data = _png16(a16)
    want = float(np.count_nonzero((r >= 20) & (r < 30))) / np.count_nonzero(r < 30)
    assert raster.data_alpha_softness(data) == pytest.approx(want)
    assert raster.alpha_softness(raster.decode_rgba(data)) == 0.0          # the 8-bit decode loses it
    a16[r < 20] = 64400                                                     # 0.98268: opaque for the worker too
    assert raster.data_alpha_softness(_png16(a16)) == pytest.approx(want)
    svg_src = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 500 500">'
               '<rect x="0" y="0" width="500" height="500" rx="110" fill="#1d4ed8"/>'
               f'<image x="150" y="150" width="200" height="200" href="data:image/png;base64,'
               f'{base64.b64encode(data).decode()}"/></svg>').encode()
    res, project, pdir = import_icon(svg_src, name="png16.svg")
    store = svg.read_store(pdir)
    img = next(e for e in store.elems if e.image)
    assert img.image["alphaSoftness"] == pytest.approx(want)
    assert raster.file_alpha_softness(pdir / img.image["file"]) == img.image["alphaSoftness"]
    assert [e.softAlpha for e in project.elements if e.kind == "image"] == [True]
    assert [_depth(L) for L in project.layers] == [CARD]


# (element id → (softness range, softAlpha)) - the worker measured Find Device 1.0, Vanced Neon's glow 0.99,
# iMessage 0.02, Outlook 0.003, Feit 0 (blender_worker/materials.py)
SOFTNESS = {
    "Feit": {"img0": ((0.0, 0.0), False)},
    "Find Device": {"img0": ((0.99, 1.0), True)},
    "Outlook": {"img0": ((0.0, 0.01), False)},
    "Vanced Neon": {"img0": ((0.98, 1.0), True), "img1": ((0.05, 0.2), False)},
    "iMessage": {"img0": ((0.01, 0.03), False)},
}
# per layer bottom → top: (element kinds, flat card?, visible), and whether the baked-highlight warning is given
LAYERS = {
    "Feit": ([(["image"], False, True)], False),
    "Find Device": ([(["path"], False, True), (["path"], False, True), (["image"], True, False),
                     (["path", "path"], False, True)], True),
    "Outlook": ([(["image"], False, True)], False),
    "Vanced Neon": ([(["image"], True, True), (["image"], False, True)], False),
    "iMessage": ([(["image"], False, True)], False),
}


@pytest.mark.parametrize("name", sorted(SOFTNESS))
def test_corpus_raster_softness(name, import_icon):
    """Measured at import over the stored image (the file the worker loads), kept in the element store and exposed as
    ``Element.softAlpha``; identical to a fresh measure of the file."""
    res, project, pdir = import_icon(CORPUS_DIR / f"{name}.svg")
    store = svg.read_store(pdir)
    got = {e.id: e for e in store.elems if e.image}
    assert set(got) == set(SOFTNESS[name])
    soft = {e.id: e.softAlpha for e in res.elements}
    for eid, ((lo, hi), flag) in SOFTNESS[name].items():
        s = got[eid].image["alphaSoftness"]
        assert lo - 1e-9 <= s <= hi + 1e-9, (name, eid, s)
        assert raster.file_alpha_softness(pdir / got[eid].image["file"]) == s
        assert soft[eid] is flag
    assert all(e.softAlpha is None for e in res.elements if e.kind == "path")


@pytest.mark.parametrize("name", sorted(LAYERS))
def test_corpus_raster_layers(name, import_icon):
    """Crisp rasters import as bodies with the vector defaults, soft ones as flat cards; Find Device's sweep (soft, on
    the green dome and the white outline below it) imports hidden with the warning; Vanced Neon's glow (under the
    logo) stays a visible card. The stack is the overlap-aware rule stack; hidden layers take no slot (QA r11 N12:
    Find Device's dot sits on the green dome, not one sweep higher)."""
    res, project, pdir = import_icon(CORPUS_DIR / f"{name}.svg")
    want, warned = LAYERS[name]
    kinds = {e.id: e.kind for e in project.elements}
    assert [([kinds[i] for i in L.elementIds], _depth(L) == CARD, L.visible) for L in project.layers] == want
    for L, (_k, card, _v) in zip(project.layers, want):
        assert _depth(L) == (CARD if card else BODY), (name, L.name)
    baked = [w for w in res.warnings if BAKED_WARNING in w]
    assert len(baked) == (1 if warned else 0), res.warnings
    assert project.source.warnings == res.warnings
    if warned:
        hidden = next(L for L in project.layers if not L.visible)
        assert f"'{hidden.name}'" in baked[0]
    cards = stacking.card_elements(project.elements)
    assert [stacking.is_card_layer(L, cards) for L in project.layers] == [c for _k, c, _v in want]
    bundle = _bundle(pdir, project)
    shapes = assert_rule_stack(project, bundle)
    hs = stacking.heights(project.layers, shapes, project.canvas.art.scale)
    for L, H, (_k, card, _v) in zip(project.layers, hs, want):
        if card:
            assert H == pytest.approx(CARD[0])
        else:   # a real body: thickness + the dome over its traced outline
            assert H > DEFAULT_THICKNESS + 0.005, (name, L.name, H)
    if name == "Vanced Neon":   # the logo body stacks on the glow card it overlaps
        assert project.layers[1].depth.z == pytest.approx(CARD[0] + stacking.geometry_rules()["stackGap"])
    if name == "Find Device":   # dome, ring (on the dome), hidden sweep (on both), dot (in the ring's hole, on the dome)
        gap = stacking.geometry_rules()["stackGap"]
        z = [L.depth.z for L in project.layers]
        assert z[2] == pytest.approx(z[1] + hs[1] + gap, abs=1e-5)          # unhiding shows the sweep on the ring
        assert z[3] == pytest.approx(z[0] + hs[0] + gap, abs=1e-5)          # the dot: one gap over the dome
        assert z[3] < z[2]                                                  # ... not over the hidden sweep


def test_synthetic_soft_and_crisp_rasters(import_icon):
    """A glow UNDER a vector disc stays a visible card, a shine OVER it is hidden (warning), a crisp raster beside the
    art is a body on the base; without the disc nothing is hidden."""
    res, project, pdir = import_icon(synth(), name="synth.svg")
    soft = {e.id: e.softAlpha for e in project.elements if e.kind == "image"}
    assert soft == {"img0": True, "img1": True, "img2": False}
    layers = [(L.elementIds, _depth(L), L.visible) for L in project.layers]
    assert layers == [(["img0"], CARD, True), (["e1"], BODY, True), (["img1"], CARD, False),
                      (["img2"], BODY, True)], layers
    assert project.layers[3].depth.z == 0.0                                  # beside the art: on the base
    assert [w for w in res.warnings if BAKED_WARNING in w] == [
        f"layer '{project.layers[2].name}': {BAKED_WARNING} (unhide the layer to keep it)"]
    assert_rule_stack(project, _bundle(pdir, project))
    # nothing below the shine (no vector disc; the glow under it is a card, not a body): it stays visible
    res, project, pdir = import_icon(synth(disc=False, crisp=False), name="nodisc.svg")
    assert [(L.elementIds, _depth(L), L.visible) for L in project.layers] == [
        (["img0"], CARD, True), (["img1"], CARD, True)]
    assert not [w for w in res.warnings if BAKED_WARNING in w]
    # two shines on nothing but each other: nothing below them to replace
    res, project, pdir = import_icon(synth(under=False, disc=False, crisp=False, over2=True), name="two.svg")
    assert project.layers and all(L.visible for L in project.layers)


def test_resplit_hides_the_baked_overlay_again(import_icon):
    """A re-split builds fresh default layers: the baked sweep is hidden again and its warning is kept once."""
    res, project, pdir = import_icon(CORPUS_DIR / "Find Device.svg")
    for L in project.layers:
        L.visible = True
    for strategy in ("smart", "element", "smart"):
        layers = svg.split_layers(pdir, project, strategy)
        hidden = [L for L in layers if not L.visible]
        assert [L.elementIds for L in hidden] == [["img0"]], strategy
        assert _depth(hidden[0]) == CARD
        assert sum(BAKED_WARNING in w for w in project.source.warnings) == 1, project.source.warnings
        project.layers = layers


def test_structural_edits_keep_the_round9_card_rule(import_icon):
    """Merging the hidden sweep card with vector art gives a VISIBLE body (the vector layer's depth); a layer left with
    the sweep only becomes a card; merging a soft glow with a crisp raster gives a body; soft cards merged together
    stay a card (keeping the bottom layer's visibility); moving a crisp raster into a card makes it a body."""
    res, project, pdir = import_icon(CORPUS_DIR / "Find Device.svg")
    card = next(L for L in project.layers if not L.visible)
    k = [L.id for L in project.layers].index(card.id)
    vec = project.layers[k + 1]
    merged = svg.merge_layers(pdir, project, [card.id, vec.id])
    m = next(L for L in merged if card.elementIds[0] in L.elementIds)
    assert m.visible and _depth(m) == _depth(vec)
    p = project.model_copy(deep=True)
    p.layers = merged
    assert_rule_stack(p, _bundle(pdir, p))
    out = svg.move_elements(pdir, p, list(vec.elementIds), None)            # the vector art out again
    left = next(L for L in out if L.elementIds == card.elementIds)
    assert _depth(left) == CARD and left.visible                            # a card again (never re-hidden by edits)

    res, vn, vdir = import_icon(CORPUS_DIR / "Vanced Neon.svg")
    one = svg.merge_layers(vdir, vn, [L.id for L in vn.layers])
    assert len(one) == 1 and _depth(one[0]) == _depth(vn.layers[1]) == BODY and one[0].visible

    res, sp, sdir = import_icon(synth(under=False, disc=True, over=True, crisp=True, over2=True), name="s2.svg")
    cards = [L for L in sp.layers if _depth(L) == CARD]
    assert len(cards) == 2 and not any(L.visible for L in cards)           # both shines sit on the disc
    two = svg.merge_layers(sdir, sp, [L.id for L in cards])
    c2 = next(L for L in two if set(L.elementIds) == {"img0", "img1"})
    assert _depth(c2) == CARD and not c2.visible
    moved = svg.move_elements(sdir, sp, ["img2"], cards[-1].id)
    t = next(L for L in moved if L.id == cards[-1].id)
    assert _depth(t) == BODY and t.visible                                  # crisp art joined: a shown body


def test_store_from_before_round9_is_measured_from_the_files(import_icon):
    """An element store without ``alphaSoftness`` (imported before round 9) is measured from its image files on load
    (the worker reads the same files); an unreadable file counts as crisp, like the worker."""
    res, project, pdir = import_icon(CORPUS_DIR / "Vanced Neon.svg")
    fresh = {e.id: e.image["alphaSoftness"] for e in svg.read_store(pdir).elems if e.image}
    path = pdir / STORE_FILE
    raw = json.loads(path.read_text(encoding="utf-8"))
    for e in raw["elements"]:
        if e.get("image"):
            e["image"].pop("alphaSoftness", None)
    raw["elements"][[e["id"] for e in raw["elements"]].index("img1")]["image"]["file"] = "images/missing.png"
    path.write_text(json.dumps(raw), encoding="utf-8")
    store = ElementStore.load(pdir)
    got = {e.id: e.image["alphaSoftness"] for e in store.elems if e.image}
    assert got == {"img0": fresh["img0"], "img1": None}
    assert svg.element_soft_alpha(pdir, project) == {"img0": True, "img1": False}
    layers = svg.split_layers(pdir, project, "smart")
    assert [_depth(L) for L in layers] == [CARD, BODY]


# ---------------------------------------------------------------------------------------------- server
def _real_client(tmp_path):
    from fastapi.testclient import TestClient

    from bis.blender import FakeBridge
    from bis.main import create_app
    from bis.testing import make_test_settings

    app = create_app(make_test_settings(tmp_path, auto_preview=False), bridge=FakeBridge())
    return TestClient(app)


def test_api_rasters_softalpha_looks_and_legacy_projects(tmp_path):
    """Over the API: ``Element.softAlpha`` and the hidden sweep come with the import; looks give crisp raster layers
    the look's depth and keep soft ones flat; a project saved before round 9 (no softAlpha) gets it back on load."""
    PRESETS = json.loads((ROOT / "shared" / "presets.json").read_text(encoding="utf-8"))
    with _real_client(tmp_path) as c:
        fd = c.post("/api/projects", json={"sample": "Find Device"}).json()
        assert {e["id"]: e["softAlpha"] for e in fd["elements"]}["img0"] is True
        assert [l["visible"] for l in fd["layers"]] == [True, True, False, True]
        assert any(BAKED_WARNING in w for w in fd["source"]["warnings"])
        vn = c.post("/api/projects", json={"sample": "Vanced Neon"}).json()
        for look in ("clay", "crystal", "liquid-glass"):
            st = PRESETS["looks"][look]["style"]["layerDefaults"]["depth"]
            r = c.post(f"/api/projects/{vn['id']}/style", json={"look": look})
            assert r.status_code == 200, r.text
            glow, logo = r.json()["layers"]
            assert (glow["depth"]["inflate"], glow["depth"]["thickness"]) == (0.0, 0.02), look
            assert (logo["depth"]["thickness"], logo["depth"]["inflate"]) == (st["thickness"], st["inflate"]), look
        im = c.post("/api/projects", json={"sample": "iMessage"}).json()
        store = c.app.state.store
        path = store.dir(im["id"]) / "project.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        for e in raw["elements"]:
            e.pop("softAlpha", None)
        path.write_text(json.dumps(raw), encoding="utf-8")
        got = c.get(f"/api/projects/{im['id']}").json()
        assert {e["id"]: e["softAlpha"] for e in got["elements"] if e["kind"] == "image"} == {"img0": False}
        assert got["updatedAt"] == im["updatedAt"]                       # filled in on load, not an edit
