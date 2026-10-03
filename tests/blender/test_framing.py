"""Perspective auto-framing maths (blender_worker/framing.py) — pure numpy, no Blender needed."""
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
