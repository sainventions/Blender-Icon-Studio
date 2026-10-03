"""GPU setup: Cycles on OptiX only (CUDA + CPU entries disabled) + OptiX denoiser (PLAN D4/D5).

``bpy.ops.wm.read_factory_settings()`` resets ``compute_device_type`` to 'NONE' — the worker therefore
uses ``read_homefile(use_empty=True)`` and calls :func:`enable_optix` again after any reset.
"""
from __future__ import annotations

import bpy

from .util import log

_STATE = {"device": "NONE", "gpu": "", "devices": []}


def enable_optix(scene: bpy.types.Scene | None = None, strict: bool = True) -> dict:
    """Select the OptiX backend and enable only OptiX devices. Returns system info."""
    cprefs = bpy.context.preferences.addons["cycles"].preferences
    try:
        cprefs.compute_device_type = "OPTIX"
    except TypeError as ex:  # no OptiX build/driver
        if strict:
            raise RuntimeError(f"OptiX backend unavailable: {ex}") from ex
        log("OptiX unavailable:", ex)
    cprefs.refresh_devices()
    gpu_names = []
    for d in cprefs.devices:
        d.use = d.type == "OPTIX"
        if d.use:
            gpu_names.append(d.name)
    if strict and not gpu_names:
        raise RuntimeError("No OptiX device found — refusing to fall back to CUDA/CPU (user requirement).")
    _STATE.update(device="OPTIX" if gpu_names else "NONE", gpu=gpu_names[0] if gpu_names else "",
                  devices=[{"name": d.name, "type": d.type, "use": bool(d.use)} for d in cprefs.devices])
    if scene is not None:
        configure_scene(scene)
    return info()


def configure_scene(scene: bpy.types.Scene) -> None:
    """Per-scene Cycles device + denoiser settings (scene settings are reset by read_homefile)."""
    scene.cycles.device = "GPU"
    scene.cycles.use_denoising = True
    try:
        scene.cycles.denoiser = "OPTIX"
    except TypeError:
        log("OptiX denoiser not available; keeping", scene.cycles.denoiser)
    scene.cycles.denoising_input_passes = "RGB_ALBEDO_NORMAL"
    scene.render.use_persistent_data = False          # D4


def device_ok() -> bool:
    cprefs = bpy.context.preferences.addons["cycles"].preferences
    return cprefs.compute_device_type == "OPTIX" and any(d.use and d.type == "OPTIX" for d in cprefs.devices) \
        and not any(d.use and d.type != "OPTIX" for d in cprefs.devices)


def info() -> dict:
    return {
        "version": ".".join(str(v) for v in bpy.app.version),
        "versionString": bpy.app.version_string,
        "hash": bpy.app.build_hash.decode() if isinstance(bpy.app.build_hash, bytes) else str(bpy.app.build_hash),
        "device": _STATE["device"],
        "gpu": _STATE["gpu"],
        "devices": list(_STATE["devices"]),
    }
