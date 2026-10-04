"""Auto-framing (bpy-free, numpy only): the CAD iso view and perspective views.

The plain front view keeps its fixed orthographic framing (``ortho_scale = 2.24 / zoom``; full-bleed 2.0).

CAD iso view (PLAN §11 View): the head-on view is the CAD "top" view of the icon lying flat (stack axis +Z
toward the viewer, art +Y up on screen). ``camera.iso`` t in 0..1 turns an ORTHOGRAPHIC camera along the
shortest rotation (quaternion slerp) from that head-on basis to the standard isometric basis: view direction
(1, −1, 1)/√3 (camera front-right-top: elevation 35.264° above the icon plane, azimuth 45°), screen up = the
stack axis +Z (camera x = (1, 1, 0)/√2, y = (−1, 1, 2)/√6) — the plate becomes a diamond, the art's +Y
points up-right and layers rise straight up on screen at their REAL z distances (no explode).
:func:`ortho_plan` fits the subject: every visible piece's convex hull at its back and front z (the plate's
outline spans −1..1; without a plate the canvas square −1..1 at z = 0 stands in), projected onto the camera's
x/y axes; the frame is centred on the projected box (lens shift) and sized so its larger side leaves
``FRONT_MARGIN`` on each side (the front view's own border: 2.0 / 2.24), so t → 0 continues the front view
exactly. Animations fit the UNION of the sampled frames' boxes about
one target: a fixed ortho scale and shift for the whole clip (no zoom breathing, no jitter).

Perspective views (tilted camera, animation frames, marketing heroes) are framed from the *world-space
subject*: the convex hull of every visible piece at its back and front z (plate outline at
``z = −thickness .. 0``, each layer's silhouette over its real z span after float offsets).

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
FRONT_MARGIN = (1.0 - 2.0 / 2.24) / 2.0     # the front view's border (plate edge 5.4 % from the frame)
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
    return {"kind": "persp", "target": [float(c) for c in target], "dist": float(dist), "tan": float(fit["tan"]),
            "shift": [float(fit["shift"][0]), float(fit["shift"][1])], "near": float(near_depth)}


def camera_pose(framing: dict, tilt_x: float, tilt_y: float) -> tuple[np.ndarray, np.ndarray]:
    """-> (camera position, rotation columns x, y, z) for one frame of a plan."""
    d, rot = orbit_basis(tilt_x, tilt_y)
    return np.asarray(framing["target"], dtype=np.float64) + d * float(framing["dist"]), rot


# ------------------------------------------------------------------------------------------------
# CAD iso view: orthographic, real distances
# ------------------------------------------------------------------------------------------------
ISO_ROT = np.column_stack([np.array([1.0, 1.0, 0.0]) / math.sqrt(2.0), np.array([-1.0, 1.0, 2.0]) / math.sqrt(6.0),
                           np.array([1.0, -1.0, 1.0]) / math.sqrt(3.0)])     # columns: camera x, y, z at iso 1


def _quat(R: np.ndarray) -> np.ndarray:
    """Unit quaternion (w, x, y, z) of a rotation matrix (Shepperd)."""
    t = float(np.trace(R))
    if t > 0:
        s = math.sqrt(t + 1.0) * 2.0
        q = [0.25 * s, (R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s]
    else:
        i = int(np.argmax(np.diag(R)))
        j, k = (i + 1) % 3, (i + 2) % 3
        s = math.sqrt(1.0 + R[i, i] - R[j, j] - R[k, k]) * 2.0
        q = [0.0] * 4
        q[0] = (R[k, j] - R[j, k]) / s
        q[1 + i] = 0.25 * s
        q[1 + j] = (R[j, i] + R[i, j]) / s
        q[1 + k] = (R[k, i] + R[i, k]) / s
    q = np.asarray(q)
    return q / np.linalg.norm(q)


def _quat_matrix(q: np.ndarray) -> np.ndarray:
    w, x, y, z = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


_ISO_Q = _quat(ISO_ROT)


def iso_basis(iso: float) -> tuple[np.ndarray, np.ndarray]:
    """camera.iso 0..1 -> (unit direction from the target toward the camera, 3×3 rotation whose columns are the
    camera's x, y, z axes): the slerp from the head-on basis (identity) to :data:`ISO_ROT`."""
    t = max(0.0, min(1.0, float(iso or 0.0)))
    half = math.acos(max(-1.0, min(1.0, float(_ISO_Q[0]))))       # half the rotation angle
    if half < 1e-9:
        return np.array([0.0, 0.0, 1.0]), np.eye(3)
    axis = _ISO_Q[1:] / math.sin(half)
    q = np.concatenate([[math.cos(t * half)], axis * math.sin(t * half)])
    R = _quat_matrix(q)
    return R[:, 2].copy(), R


def canvas_square(z: float = 0.0) -> np.ndarray:
    """The canvas −1..1 at height z (4 points): the front view's reference frame."""
    return np.array([(-1.0, -1.0, z), (1.0, -1.0, z), (1.0, 1.0, z), (-1.0, 1.0, z)])


def ortho_box(points: np.ndarray, target: np.ndarray, iso: float) -> Optional[tuple]:
    """(u0, v0, u1, v1) of the points projected onto the iso camera's x / y axes (relative to target)."""
    if len(points) == 0:
        return None
    _, rot = iso_basis(iso)
    pc = (np.asarray(points, dtype=np.float64) - target) @ rot
    return float(pc[:, 0].min()), float(pc[:, 1].min()), float(pc[:, 0].max()), float(pc[:, 1].max())


def ortho_plan(frames: Sequence[tuple[np.ndarray, float]], zoom: float = 1.0, margin: float = FRONT_MARGIN) -> dict:
    """Framing of the CAD iso view for one still or a whole clip. ``frames`` = [(points, iso)].
    -> {'kind': 'ortho', 'target', 'dist', 'scale' (Blender ortho_scale), 'shift' (sx, sy), 'clip'}."""
    allp = [np.asarray(f[0], dtype=np.float64) for f in frames if len(f[0])]
    pts = np.vstack(allp) if allp else np.zeros((0, 3))
    target = bounds_center(pts)
    box = union(ortho_box(np.asarray(p, dtype=np.float64), target, iso) for p, iso in frames)
    if box is None:
        return {"kind": "ortho", "target": [0.0, 0.0, 0.0], "dist": 10.0, "scale": 2.24 / max(0.05, float(zoom)),
                "shift": [0.0, 0.0], "clip": 100.0}
    u0, v0, u1, v1 = box
    half = max(1e-6, (u1 - u0) / 2.0, (v1 - v0) / 2.0)
    scale = 2.0 * half / max(0.05, 1.0 - 2.0 * margin) / max(0.05, float(zoom))
    reach = float(np.linalg.norm(pts - target, axis=1).max()) if len(pts) else 1.0
    dist = reach + 2.0                       # in front of everything for every frame (ortho: no perspective)
    return {"kind": "ortho", "target": [float(c) for c in target], "dist": dist, "scale": float(scale),
            "shift": [float((u0 + u1) / 2.0 / scale), float((v0 + v1) / 2.0 / scale)], "clip": dist + reach + 2.0}


def ortho_pose(plan_: dict, iso: float) -> tuple[np.ndarray, np.ndarray]:
    """-> (camera position, rotation columns x, y, z) of the iso camera for one frame of an ortho plan."""
    d, rot = iso_basis(iso)
    return np.asarray(plan_["target"], dtype=np.float64) + d * float(plan_["dist"]), rot
