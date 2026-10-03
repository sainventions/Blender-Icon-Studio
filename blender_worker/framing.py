"""Perspective auto-framing (bpy-free, numpy only).

The front view keeps its fixed orthographic framing (``ortho_scale = 2.24 / zoom``; full-bleed 2.0). Every
perspective view (tilted camera, exploded stacks, animation frames, marketing heroes) is framed from the
*world-space subject*: the convex hull of every visible piece at its back and front z (plate outline at
``z = −thickness .. 0``, each layer's silhouette at ``z .. z + thickness`` after explode / float offsets).

* The camera orbits ``target`` (centre of the subject's 3D bounds) at a distance that only depends on the
  field of view (perspective strength), never on the subject — so animations orbit a fixed point.
* The frame is then fitted exactly in *tangent space* (``x/−z, y/−z`` in camera coordinates): the lens
  angle scales the frame uniformly and the lens shift centres it, both without moving the camera. Under a
  pinhole projection the image of a planar convex polygon is the convex hull of its projected vertices,
  so the hull vertices bound the subject exactly.
* Animations fit the UNION of the per-frame tangent-space bounds over sampled frames: one lens angle and one
  shift for the whole clip — the subject never leaves the frame and never "breathes" or jitters.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

import numpy as np

MARGIN = 0.08            # empty border on each side of the fitted subject (fraction of the frame)
DIST_K = 1.12 * 1.32     # orbit distance = DIST_K / tan(fov / 2): perspective strength from the fov alone
NEAR_K, NEAR_XY = 0.4, 0.6   # minimum camera clearance in front of the subject (see plan)


def hull2d(pts: np.ndarray) -> np.ndarray:
    """Convex hull (Andrew's monotone chain) of an (N, 2) array -> (M, 2), counter-clockwise."""
    p = np.unique(np.round(np.asarray(pts, dtype=np.float64), 9), axis=0)
    if len(p) <= 2:
        return p

    def half(seq):
        out: list = []
        for q in seq:
            while len(out) >= 2:
                (ax, ay), (bx, by) = out[-2], out[-1]
                if (bx - ax) * (q[1] - ay) - (by - ay) * (q[0] - ax) > 1e-12:
                    break
                out.pop()
            out.append((float(q[0]), float(q[1])))
        return out

    lower = half(p)
    upper = half(p[::-1])
    return np.asarray(lower[:-1] + upper[:-1], dtype=np.float64)


def prism(hull: np.ndarray, z0: float, z1: float) -> np.ndarray:
    """2D hull extruded to [z0, z1] -> (2M, 3) points."""
    if len(hull) == 0:
        return np.zeros((0, 3))
    a = np.column_stack([hull, np.full(len(hull), z0)])
    b = np.column_stack([hull, np.full(len(hull), z1)])
    return np.vstack([a, b])


def orbit_basis(tilt_x: float, tilt_y: float) -> tuple[np.ndarray, np.ndarray]:
    """-> (unit direction from the target toward the camera, 3×3 rotation whose columns are the camera's
    x, y, z axes). tiltX > 0 = camera above the icon (looking down), tiltY > 0 = camera to the right."""
    tx, ty = math.radians(float(tilt_x)), math.radians(float(tilt_y))
    d = np.array([math.cos(tx) * math.sin(ty), math.sin(tx), math.cos(tx) * math.cos(ty)])
    z = d / (np.linalg.norm(d) or 1.0)
    up = np.array([0.0, 1.0, 0.0])
    if abs(float(z @ up)) > 0.999:
        up = np.array([0.0, 0.0, -1.0])
    x = np.cross(up, z)
    x /= np.linalg.norm(x) or 1.0
    y = np.cross(z, x)
    return z, np.column_stack([x, y, z])


def orbit_distance(fov_deg: float) -> float:
    fov = math.radians(max(5.0, min(120.0, float(fov_deg))))
    return DIST_K / math.tan(fov / 2.0)


def tangent_bounds(points: np.ndarray, pos: np.ndarray, rot: np.ndarray) -> Optional[tuple]:
    """(u0, v0, u1, v1) of the points in camera tangent space, or None (no point in front of the camera)."""
    if len(points) == 0:
        return None
    pc = (points - pos) @ rot              # camera coordinates (rows)
    depth = -pc[:, 2]
    ok = depth > 1e-4
    if not ok.any():
        return None
    u = pc[ok, 0] / depth[ok]
    v = pc[ok, 1] / depth[ok]
    return float(u.min()), float(v.min()), float(u.max()), float(v.max())


def union(boxes: Iterable[Optional[tuple]]) -> Optional[tuple]:
    bs = [b for b in boxes if b is not None]
    if not bs:
        return None
    return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))


def lens_fit(box: Optional[tuple], zoom: float = 1.0, margin: float = MARGIN, aspect: float = 1.0) -> dict:
    """Tangent-space box -> {'tan': half-size of the frame (larger side), 'shift': (sx, sy)} so that the box
    is centred and its larger relative extent fills ``1 − 2·margin`` of the frame. ``aspect`` = width/height
    of the output. Blender: ``camera.angle = 2·atan(tan)`` (sensor_fit AUTO) and shift in units of the
    larger frame side."""
    if box is None:
        return {"tan": math.tan(math.radians(15.0)), "shift": (0.0, 0.0)}
    u0, v0, u1, v1 = box
    hu, hv = max(1e-6, (u1 - u0) / 2.0), max(1e-6, (v1 - v0) / 2.0)
    # half extents of the frame in tangent units: larger side = tan, smaller = tan / aspect'
    if aspect >= 1.0:
        need = max(hu, hv * aspect)
    else:
        need = max(hu / aspect, hv)
    tan = need / max(0.05, 1.0 - 2.0 * margin) / max(0.05, float(zoom))
    cu, cv = (u0 + u1) / 2.0, (v0 + v1) / 2.0
    return {"tan": tan, "shift": (cu / (2.0 * tan), cv / (2.0 * tan))}


def bounds_center(points: np.ndarray) -> np.ndarray:
    if len(points) == 0:
        return np.zeros(3)
    return (points.min(axis=0) + points.max(axis=0)) / 2.0


def plan(frames: Sequence[tuple[np.ndarray, float, float]], fov: float, zoom: float = 1.0,
         margin: float = MARGIN) -> dict:
    """Framing for one still or a whole animation. ``frames`` = [(points, tiltX, tiltY)] (sampled frames).
    -> {'target', 'dist', 'tan', 'shift', 'near'} shared by every frame ('near' = smallest depth of the subject
    along the view axis over all frames, for the near clip)."""
    allp = [f[0] for f in frames if len(f[0])]
    pts = np.vstack(allp) if allp else np.zeros((0, 3))
    target = bounds_center(pts)
    dist = orbit_distance(fov)
    if len(pts):
        # tall exploded stacks at a wide fov: the fov-only orbit would put the camera inside / just above the
        # stack (points behind the lens, 170° lens fits). Back off just enough that the nearest point stays
        # NEAR_K × the orbit distance (or NEAR_XY × the subject's radius) in front of the camera.
        r_xy = float(np.hypot(pts[:, 0] - target[0], pts[:, 1] - target[1]).max())
        near = max(NEAR_K * dist, NEAR_XY * r_xy)
        for p, tx, ty in frames:
            if len(p):
                d, _ = orbit_basis(tx, ty)
                dist = max(dist, float(((p - target) @ d).max()) + near)
    boxes = []
    near_depth = dist
    for p, tx, ty in frames:
        d, rot = orbit_basis(tx, ty)
        boxes.append(tangent_bounds(p, target + d * dist, rot))
        if len(p):
            near_depth = min(near_depth, dist - float(((p - target) @ d).max()))
    fit = lens_fit(union(boxes), zoom, margin)
    return {"target": [float(c) for c in target], "dist": float(dist), "tan": float(fit["tan"]),
            "shift": [float(fit["shift"][0]), float(fit["shift"][1])], "near": float(near_depth)}


def camera_pose(framing: dict, tilt_x: float, tilt_y: float) -> tuple[np.ndarray, np.ndarray]:
    """-> (camera position, rotation columns x, y, z) for one frame of a plan."""
    d, rot = orbit_basis(tilt_x, tilt_y)
    return np.asarray(framing["target"], dtype=np.float64) + d * float(framing["dist"]), rot
