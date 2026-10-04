"""Auto-framing maths (blender_worker/framing.py): perspective views and the CAD iso view (PLAN §11 View) — pure
numpy, no Blender needed."""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from blender_worker import framing as F  # noqa: E402


def _squircle(n=64):
    t = np.linspace(0, 2 * np.pi, n, endpoint=False)
    c, s = np.cos(t), np.sin(t)
    return np.column_stack([np.sign(c) * np.abs(c) ** 0.4, np.sign(s) * np.abs(s) ** 0.4])


def _subject(explode=1.0):
    plate = F.prism(F.hull2d(_squircle()), -0.16, 0.0)
    layers = [F.prism(F.hull2d(_squircle() * 0.35 + off), z * explode, z * explode + 0.1)
              for off, z in (((0.3, 0.3), 0.0), ((-0.3, 0.2), 0.13), ((0.0, -0.35), 0.26), ((0.35, -0.3), 0.39))]
    return np.vstack([plate] + layers)


def _project(points, plan, tx, ty):
    pos, rot = F.camera_pose(plan, tx, ty)
    pc = (points - pos) @ rot
    u, v = pc[:, 0] / -pc[:, 2], pc[:, 1] / -pc[:, 2]
    T = plan["tan"]
    sx, sy = plan["shift"]
    # normalised frame coordinates in [-1, 1] after the lens shift
    return (u - 2 * T * sx) / T, (v - 2 * T * sy) / T


def test_hull_is_convex_and_complete():
    pts = np.random.default_rng(1).normal(size=(500, 2))
    h = F.hull2d(pts)
    assert 3 <= len(h) < 40
    # every point lies inside (or on) the CCW hull
    for i in range(len(h)):
        a, b = h[i], h[(i + 1) % len(h)]
        cross = (b[0] - a[0]) * (pts[:, 1] - a[1]) - (b[1] - a[1]) * (pts[:, 0] - a[0])
        assert (cross >= -1e-9).all()


def test_still_is_centred_with_margin():
    pts = _subject(3.0)
    for tx, ty in ((20, -25), (0, 0), (-15, 40), (35, 10)):
        plan = F.plan([(pts, tx, ty)], fov=30)
        u, v = _project(pts, plan, tx, ty)
        assert abs((u.min() + u.max()) / 2) < 1e-6 and abs((v.min() + v.max()) / 2) < 1e-6
        ext = max(u.max() - u.min(), v.max() - v.min()) / 2
        assert math.isclose(ext, 1 - 2 * F.MARGIN, rel_tol=1e-6)


def test_animation_union_never_leaves_frame_and_is_shared():
    frames = []
    for k in range(24):
        t = k / 24
        e = 0.5 - 0.5 * math.cos(2 * math.pi * t)
        frames.append((_subject(1 + 2.2 * e), 22 * e, -32 * e))
    plan = F.plan(frames, fov=30)
    worst = 0.0
    for pts, tx, ty in frames:
        u, v = _project(pts, plan, tx, ty)
        worst = max(worst, np.abs(u).max(), np.abs(v).max())
    assert worst <= 1 - 2 * F.MARGIN + 1e-6          # the union fits every frame inside the margin
    assert worst > 1 - 2 * F.MARGIN - 1e-3           # and touches it in at least one (tight fit)


def test_turntable_target_is_fixed():
    pts = _subject()
    plan = F.plan([(pts, 8, 360 * k / 8) for k in range(8)], fov=30)
    for k in range(8):
        pos, rot = F.camera_pose(plan, 8, 360 * k / 8)
        d = np.linalg.norm(pos - np.asarray(plan["target"]))
        assert math.isclose(d, plan["dist"], rel_tol=1e-9)
        # the camera looks at the target
        fwd = -rot[:, 2]
        to = (np.asarray(plan["target"]) - pos) / d
        assert float(fwd @ to) > 0.9999


def test_zoom_crops_like_the_front_view():
    pts = _subject()
    p1 = F.plan([(pts, 20, -25)], fov=30, zoom=1.0)
    p2 = F.plan([(pts, 20, -25)], fov=30, zoom=2.0)
    assert math.isclose(p2["tan"], p1["tan"] / 2, rel_tol=1e-9)


def test_tall_exploded_stack_keeps_the_camera_outside():
    """16 layers × explode 4 at a wide fov: the fov-only orbit distance would put the camera inside the stack
    (points behind the lens, a ~170° lens fit). The plan backs off just enough; ordinary stacks keep the
    fov-only distance (unchanged perspective)."""
    def stack(n, ex):
        plate = F.prism(F.hull2d(_squircle()), -0.16, 0.0)
        return np.vstack([plate] + [F.prism(F.hull2d(_squircle() * 0.5), i * 0.13 * ex + 0.002, i * 0.13 * ex + 0.102)
                                    for i in range(n)])
    for fov, tx, ty in ((90, 0, 0), (90, 20, -25), (60, 0, 0)):
        pts = stack(16, 4.0)
        plan = F.plan([(pts, tx, ty)], fov=fov)
        pos, rot = F.camera_pose(plan, tx, ty)
        depth = -((pts - pos) @ rot)[:, 2]
        assert depth.min() >= F.NEAR_K * F.orbit_distance(fov) - 1e-9, (fov, tx, ty, depth.min())
        assert math.degrees(2 * math.atan(plan["tan"])) < 115, (fov, tx, ty, plan)
        u, v = _project(pts, plan, tx, ty)
        assert max(np.abs(u).max(), np.abs(v).max()) <= 1 - 2 * F.MARGIN + 1e-6
    for fov, tx, ty in ((30, 20, -25), (90, 0, 0), (90, 20, -25), (60, -12, 40)):
        plan = F.plan([(_subject(), tx, ty)], fov=fov)
        assert math.isclose(plan["dist"], F.orbit_distance(fov), rel_tol=1e-12), (fov, plan["dist"])


def test_empty_subject_has_a_sane_plan():
    plan = F.plan([(np.zeros((0, 3)), 20, -25)], fov=30)
    assert plan["tan"] > 0 and np.isfinite(plan["shift"]).all() and plan["dist"] == F.orbit_distance(30)


# ------------------------------------------------------------------------------------------------ CAD iso view
def _ortho(points, plan, iso):
    """Normalised frame coordinates in [-1, 1] of points seen by the iso camera of an ortho plan."""
    pos, rot = F.ortho_pose(plan, iso)
    pc = (points - pos) @ rot
    half = plan["scale"] / 2
    sx, sy = plan["shift"]
    return (pc[:, 0] - sx * plan["scale"]) / half, (pc[:, 1] - sy * plan["scale"]) / half, -pc[:, 2]


def test_iso_basis_runs_from_head_on_to_isometric():
    d0, r0 = F.iso_basis(0.0)
    assert np.allclose(r0, np.eye(3)) and np.allclose(d0, (0, 0, 1))        # head-on: the plain front view
    d1, r1 = F.iso_basis(1.0)
    assert np.allclose(d1, np.array([1, -1, 1]) / math.sqrt(3))
    assert math.isclose(math.degrees(math.asin(d1[2])), 35.264, abs_tol=1e-3)   # pitch above the icon plane
    assert math.isclose(math.degrees(math.atan2(d1[0], -d1[1])), 45.0, abs_tol=1e-9)   # yaw
    assert np.allclose(r1[:, 0], np.array([1, 1, 0]) / math.sqrt(2))         # screen x: no roll
    assert r1[2, 1] > 0.8                                                     # the stack axis points up on screen
    assert np.allclose(F.iso_basis(-3)[1], r0)                                # clamped to 0..1
    assert np.allclose(F.iso_basis(7)[1], r1)
    prev = 91.0
    for t in np.linspace(0, 1, 21):
        d, r = F.iso_basis(t)
        assert np.allclose(r.T @ r, np.eye(3), atol=1e-12) and math.isclose(np.linalg.det(r), 1.0)
        assert np.allclose(r[:, 2], d)
        elev = math.degrees(math.asin(d[2]))
        assert elev < prev + 1e-9                                             # one monotone sweep, no detour
        if 0 < t < 1:
            # the slerp turns at a constant rate: the elevation drops ~linearly 90 -> 35.264
            assert abs(elev - (90 - t * (90 - 35.264))) < 3.0, (t, elev)
        prev = elev


def test_iso_shows_real_distances():
    """Orthographic: a layer gap of h appears as h · cos(elevation) on screen — never spread apart."""
    for t in (0.0, 0.5, 1.0):
        d, rot = F.iso_basis(t)
        for h in (0.05, 0.3):
            a = np.array([0.2, -0.1, 0.0]) @ rot
            b = np.array([0.2, -0.1, h]) @ rot
            screen = math.hypot(b[0] - a[0], b[1] - a[1])
            assert math.isclose(screen, h * math.sqrt(1 - d[2] ** 2), abs_tol=1e-12)
    # isometric: z foreshortens by sqrt(2/3) like every other axis
    _, r1 = F.iso_basis(1.0)
    for axis in np.eye(3):
        assert math.isclose(math.hypot(*(axis @ r1)[:2]), math.sqrt(2 / 3), rel_tol=1e-12)


def test_iso_zero_continues_the_front_framing():
    pts = np.vstack([_subject(), F.canvas_square(0.0)])
    plan = F.ortho_plan([(pts, 0.0)])
    assert plan["kind"] == "ortho"
    assert math.isclose(plan["scale"], 2.24, rel_tol=1e-9)                 # the front view's ortho scale
    u, v, _ = _ortho(pts, plan, 0.0)
    assert math.isclose(u.max() - u.min(), 2.0 / 1.12, rel_tol=1e-9)        # canvas fills 2.0 / 2.24 of the frame
    assert abs(u.min() + u.max()) < 1e-9 and abs(v.min() + v.max()) < 1e-9
    assert math.isclose(F.ortho_plan([(pts, 0.0)], zoom=2.0)["scale"], 1.12, rel_tol=1e-9)


def test_iso_still_is_centred_and_fits():
    pts = np.vstack([_subject(), F.canvas_square(0.0)])
    for t in (0.25, 0.5, 1.0):
        plan = F.ortho_plan([(pts, t)])
        u, v, depth = _ortho(pts, plan, t)
        assert abs(u.min() + u.max()) < 1e-9 and abs(v.min() + v.max()) < 1e-9
        assert math.isclose(max(u.max() - u.min(), v.max() - v.min()) / 2, 1 - 2 * F.FRONT_MARGIN, rel_tol=1e-9)
        assert depth.min() > 1.0 and depth.max() < plan["clip"]               # in front of the camera, inside the clip


def test_iso_sweep_shares_one_framing():
    """The 'iso' animation (head-on -> iso -> head-on): one ortho scale + shift for the whole clip, every frame
    inside the margin, the widest one touching it."""
    pts = np.vstack([_subject(), F.canvas_square(0.0)])
    isos = [0.5 - 0.5 * math.cos(2 * math.pi * k / 16) for k in range(16)]
    plan = F.ortho_plan([(pts, t) for t in isos])
    worst = 0.0
    for t in isos:
        u, v, _ = _ortho(pts, plan, t)
        worst = max(worst, np.abs(u).max(), np.abs(v).max())
    assert worst <= 1 - 2 * F.FRONT_MARGIN + 1e-9
    assert worst > 1 - 2 * F.FRONT_MARGIN - 1e-3
    assert np.allclose(F.ortho_pose(plan, 0.0)[0] - plan["target"], [0, 0, plan["dist"]])


def test_empty_iso_subject_has_a_sane_plan():
    plan = F.ortho_plan([(np.zeros((0, 3)), 0.5)])
    assert plan["scale"] > 0 and np.isfinite(plan["shift"]).all()
