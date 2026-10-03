"""Deterministic shader-node graph builder with an in-place *update* mode.

Material builders are written once against :class:`Graph`. In ``build`` mode every call creates nodes
and links. In ``update`` mode the *same* builder code runs again, but ``node()`` returns the existing
node with the same sequential name and ``link()`` is a no-op, so only socket values / colour ramps /
images are re-assigned. That keeps EEVEE from recompiling shaders when the user only drags a slider
(topology changes are detected by the caller through a topology key and trigger a full rebuild).
"""
from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

import bpy


class TopologyMismatch(RuntimeError):
    """Raised in update mode when the existing tree does not match the builder's node sequence."""


Socket = bpy.types.NodeSocket
Value = Any  # float | tuple | Socket


def _is_socket(v) -> bool:
    return isinstance(v, bpy.types.NodeSocket)


class Graph:
    def __init__(self, nt: bpy.types.NodeTree, update: bool = False, prefix: str = "bis"):
        self.nt = nt
        self.update = update
        self.prefix = prefix
        self.i = 0

    # -------------------------------------------------------------------------- core
    def node(self, idname: str, label: str = "", **props) -> bpy.types.Node:
        name = f"{self.prefix}{self.i:03d}"
        self.i += 1
        if self.update:
            n = self.nt.nodes.get(name)
            if n is None or n.bl_idname != idname:
                raise TopologyMismatch(f"{name}: expected {idname}, found {getattr(n, 'bl_idname', None)}")
            return n
        n = self.nt.nodes.new(idname)
        n.name = name
        if label:
            n.label = label
        for k, v in props.items():
            setattr(n, k, v)
        return n

    def link(self, src: Socket, dst: Socket) -> None:
        if not self.update:
            self.nt.links.new(src, dst)

    def set(self, sock: Socket, value: Value) -> None:
        """Link if ``value`` is a socket (topology), else assign the default value (always)."""
        if _is_socket(value):
            self.link(value, sock)
            return
        if value is None:
            return
        dv = sock.default_value
        if isinstance(dv, (str, bool)):
            sock.default_value = value
            return
        if isinstance(dv, int):
            sock.default_value = int(value)
            return
        try:
            n = len(dv)  # vector / colour socket
        except TypeError:
            sock.default_value = float(value) if isinstance(dv, float) else value
            return
        if isinstance(value, (int, float)):
            value = (float(value),) * n
        value = tuple(value)
        if len(value) < n:   # rgb -> rgba
            value = value + (1.0,) * (n - len(value))
        sock.default_value = value[:n]

    def inputs(self, node: bpy.types.Node, values: dict) -> bpy.types.Node:
        for k, v in values.items():
            self.set(node.inputs[k], v)
        return node

    # -------------------------------------------------------------------------- helpers
    def value(self, v: float, label: str = "") -> Socket:
        n = self.node("ShaderNodeValue", label)
        n.outputs[0].default_value = float(v)
        return n.outputs[0]

    def rgb(self, c: Sequence[float], label: str = "") -> Socket:
        n = self.node("ShaderNodeRGB", label)
        c = tuple(c)
        n.outputs[0].default_value = (c + (1.0,))[:4] if len(c) == 3 else c[:4]
        return n.outputs[0]

    def math(self, op: str, a: Value, b: Value = None, c: Value = None, clamp: bool = False) -> Socket:
        n = self.node("ShaderNodeMath", operation=op, use_clamp=clamp)
        for i, v in enumerate((a, b, c)):
            if v is not None:
                self.set(n.inputs[i], v)
        return n.outputs[0]

    def vmath(self, op: str, a: Value, b: Value = None, scale: Optional[float] = None) -> Socket:
        n = self.node("ShaderNodeVectorMath", operation=op)
        if a is not None:
            self.set(n.inputs[0], a)
        if b is not None:
            self.set(n.inputs[1], b)
        if scale is not None:
            self.set(n.inputs["Scale"], scale)
        key = "Value" if op in ("DOT_PRODUCT", "LENGTH", "DISTANCE") else "Vector"
        return n.outputs[key]

    def combine(self, x: Value = 0.0, y: Value = 0.0, z: Value = 0.0) -> Socket:
        n = self.node("ShaderNodeCombineXYZ")
        self.set(n.inputs[0], x)
        self.set(n.inputs[1], y)
        self.set(n.inputs[2], z)
        return n.outputs[0]

    def separate(self, v: Value) -> bpy.types.Node:
        n = self.node("ShaderNodeSeparateXYZ")
        self.set(n.inputs[0], v)
        return n

    def mix_rgb(self, fac: Value, a: Value, b: Value, blend: str = "MIX", clamp: bool = False) -> Socket:
        n = self.node("ShaderNodeMix", data_type="RGBA", blend_type=blend, clamp_result=clamp)
        self.set(n.inputs[0], fac)
        self.set(n.inputs[6], a)
        self.set(n.inputs[7], b)
        return n.outputs[2]

    def mix_float(self, fac: Value, a: Value, b: Value) -> Socket:
        n = self.node("ShaderNodeMix", data_type="FLOAT")
        self.set(n.inputs[0], fac)
        self.set(n.inputs[2], a)
        self.set(n.inputs[3], b)
        return n.outputs[0]

    def map_range(self, v: Value, fmin: Value, fmax: Value, tmin: Value, tmax: Value,
                  interp: str = "LINEAR", clamp: bool = True) -> Socket:
        n = self.node("ShaderNodeMapRange", interpolation_type=interp, clamp=clamp)
        self.set(n.inputs["Value"], v)
        self.set(n.inputs["From Min"], fmin)
        self.set(n.inputs["From Max"], fmax)
        self.set(n.inputs["To Min"], tmin)
        self.set(n.inputs["To Max"], tmax)
        return n.outputs["Result"]

    def mix_shader(self, fac: Value, a: Socket, b: Socket) -> Socket:
        n = self.node("ShaderNodeMixShader")
        self.set(n.inputs[0], fac)
        self.link(a, n.inputs[1])
        self.link(b, n.inputs[2])
        return n.outputs[0]

    def add_shader(self, a: Socket, b: Socket) -> Socket:
        n = self.node("ShaderNodeAddShader")
        self.link(a, n.inputs[0])
        self.link(b, n.inputs[1])
        return n.outputs[0]

    def ramp(self, fac: Value, samples: Iterable[tuple[float, Sequence[float], float]],
             interpolation: str = "LINEAR") -> bpy.types.Node:
        """Colour ramp; ``samples`` = (position, linear rgb, alpha). Elements are (re)assigned in both modes."""
        n = self.node("ShaderNodeValToRGB")
        self.set(n.inputs["Fac"], fac)
        set_ramp(n, samples, interpolation)
        return n


def set_ramp(node: bpy.types.Node, samples, interpolation: str = "LINEAR") -> None:
    samples = list(samples)[:32]
    cr = node.color_ramp
    cr.interpolation = interpolation
    els = cr.elements
    while len(els) > max(2, len(samples)):
        els.remove(els[-1])
    while len(els) < len(samples):
        els.new(1.0)
    # assign positions in ascending order (Blender re-sorts on each assignment)
    for i, (pos, rgb, a) in enumerate(sorted(samples, key=lambda s: s[0])):
        e = els[i]
        e.position = max(0.0, min(1.0, float(pos)))
        e.color = (float(rgb[0]), float(rgb[1]), float(rgb[2]), float(a))
    if len(samples) == 1:
        els[1].position = 1.0
        els[1].color = els[0].color


def auto_layout(nt: bpy.types.NodeTree, dx: float = 260.0, dy: float = 200.0) -> None:
    """Cosmetic: place nodes in columns by distance to the output (nicer .blend files)."""
    level: dict[str, int] = {}
    outs = [n for n in nt.nodes if not n.outputs or n.bl_idname in ("ShaderNodeOutputMaterial",
                                                                     "ShaderNodeOutputWorld", "NodeGroupOutput")]
    incoming: dict[str, list] = {}
    for lk in nt.links:
        incoming.setdefault(lk.to_node.name, []).append(lk.from_node)
    stack = [(n, 0) for n in outs]
    guard = 0
    while stack and guard < 20000:
        guard += 1
        n, d = stack.pop()
        if level.get(n.name, -1) >= d:
            continue
        level[n.name] = d
        for src in incoming.get(n.name, []):
            stack.append((src, d + 1))
    rows: dict[int, int] = {}
    for n in nt.nodes:
        d = level.get(n.name, 0)
        r = rows.get(d, 0)
        rows[d] = r + 1
        n.location = (-d * dx, -r * dy)
