"""Export packaging (PLAN §4 C, critic §4.6).

An export job renders a small set of **master renders** (one-shot Blender processes, D8) and derives every
platform asset from them with Pillow:

==============  ==========================================================================================
master          how it is rendered (project overrides only; no worker-protocol changes)
==============  ==========================================================================================
full:<app>      fullBleed square plate, ortho 2.0, 1024 px (iOS 1024, watchOS appiconset, apple-touch, Play)
macos           squircle, camera zoom 1.12·0.8047 → plate = 824/1024 of the frame, transparent margins
circle          circle plate filling the frame, 1088 px (watchOS)
android-fg      plate hidden, plate −1..1 = the 72 dp viewport of the 108 dp layer (zoom 0.7467), 432 px
android-bg      every layer hidden, fullBleed plate, 432 px
android-mono    like android-fg with the clear-light (mono) appearance → white + alpha
rounded         rounded plate at 0.875 of the frame (Windows / web), 512 px
maskable        fullBleed with the art scaled to 80 % (PWA maskable safe zone), 512 px
hero*           CAD-style POV (``camera.iso``, PLAN §11): orthographic, REAL layer distances, auto-framed;
                hero = iso 0.55 (between head-on and isometric), hero-iso = full isometric, 1024 px (2048 ultra)
==============  ==========================================================================================

Every platform master is head-on (``iso`` 0) whatever the project's own POV is.

Output: ``workspace/projects/<id>/exports/<jobId>/`` (unpacked) + ``exports/<jobId>.zip``;
job result ``{zip, url, files:[{name,url}], previews:[{name,url}], targets, appearances, quality, seconds}``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from .icon_format import ICON_ASSET_NAME_MAX
from .models import CameraSpec, ExportRequest, Project
from .presets import ALL_APPEARANCES
from .util import WIN_MAX_PATH, atomic_write_text, files_url, long_paths_enabled, safe_filename

if TYPE_CHECKING:  # pragma: no cover
    from .jobs import JobContext
    from .rendering import RenderService

log = logging.getLogger("bis.export")

TARGET_ORDER = ["ios", "macos", "watchos", "android", "windows", "web", "marketing", "icon", "blend"]
MACOS_PLATE = 824 / 1024          # Tahoe: 824 px body in a 1024 canvas
ANDROID_VIEWPORT = 72 / 108       # adaptive icon: 72 dp visible of the 108 dp layer
WINDOWS_PLATE = 0.875
MASKABLE_ART = 0.8                # PWA maskable safe zone (radius 40 %)
LEGACY_ANDROID = 44 / 48          # legacy launcher icon body inside its 48 dp canvas
HERO_ISO = 0.55                   # marketing hero POV: between head-on (0) and isometric (1), real distances
ANDROID_DENSITIES = {"mdpi": 1.0, "hdpi": 1.5, "xhdpi": 2.0, "xxhdpi": 3.0, "xxxhdpi": 4.0}
MACOS_ICONSET = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)]
ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
FAVICON_SIZES = [16, 32, 48]
EXPORT_NAME_MAX = 40              # file/bundle names derived from the project name (Windows MAX_PATH budget)
# longest relative path written below exports/<jobId>/: icon/<name 40>.icon/Assets/<layer 32> 99.svg
EXPORT_PATH_BUDGET = len("/icon/") + EXPORT_NAME_MAX + len(".icon/Assets/") + ICON_ASSET_NAME_MAX + len(" 99.svg")


def check_path_budget(out: Path) -> None:
    """Fail early (before minutes of rendering) when the export folder is too deep for Windows MAX_PATH."""
    need = len(str(out)) + EXPORT_PATH_BUDGET
    if need > WIN_MAX_PATH and not long_paths_enabled():
        raise ValueError(
            f"The export folder path is too long for Windows ({need} > {WIN_MAX_PATH} characters): {out}. "
            "Move the workspace to a shorter folder (set BIS_WORKSPACE) or enable Win32 long paths."
        )


def zoom_for_fraction(fraction: float) -> float:
    """Front ortho camera zoom so the plate (−1..1) spans `fraction` of the frame (ortho = 2.24/zoom)."""
    return 1.12 * fraction


# ---------------------------------------------------------------------------------------------- masters
@dataclass(frozen=True)
class Master:
    key: str
    label: str
    appearance: str
    native: int
    full_bleed: bool = False
    zoom: float = 1.0
    shape: str | None = None
    plate: bool = True
    layers: bool = True
    art_scale: float = 1.0
    camera: dict | None = None      # explicit camera (marketing); else front ortho with `zoom`
    transparent: bool = True
    hero: bool = False


@dataclass
class ExportPlan:
    targets: list[str]
    appearances: list[str]
    primary: str
    masters: dict[str, Master] = field(default_factory=dict)

    def need(self, m: Master) -> str:
        self.masters.setdefault(m.key, m)
        return m.key


def _full(app: str) -> Master:
    return Master(f"full-{app}", f"Full-bleed {app}", app, 1024, full_bleed=True, transparent=False)


def plan_export(project: Project, req: ExportRequest) -> ExportPlan:
    targets = [t for t in TARGET_ORDER if t in set(req.targets)]
    apps = [a for a in dict.fromkeys(req.appearances) if a in ALL_APPEARANCES] or ["light"]
    primary = "light" if "light" in apps else apps[0]
    plan = ExportPlan(targets, apps, primary)
    for t in targets:
        if t == "ios":
            plan.need(_full(primary))
            if "dark" in apps and primary != "dark":
                plan.need(_full("dark"))
            tinted = next((a for a in ("tinted-dark", "tinted-light") if a in apps), None)
            if tinted:
                plan.need(_full(tinted))
        elif t == "macos":
            plan.need(Master("macos", "macOS", primary, 1024, zoom=zoom_for_fraction(MACOS_PLATE), shape="squircle"))
        elif t == "watchos":
            plan.need(_full("light"))
            plan.need(Master("circle", "watchOS circle", "light", 1088, zoom=zoom_for_fraction(1.0), shape="circle"))
        elif t == "android":
            z = zoom_for_fraction(ANDROID_VIEWPORT)
            plan.need(Master("android-fg", "Android foreground", "light", 432, zoom=z, plate=False))
            plan.need(Master("android-bg", "Android background", "light", 432, full_bleed=True, layers=False,
                             transparent=False))
            plan.need(Master("android-mono", "Android monochrome", "clear-light", 432, zoom=z, plate=False))
            plan.need(_full("light"))
        elif t == "windows":
            plan.need(Master("rounded", "Rounded (Windows/Web)", "light", 512,
                             zoom=zoom_for_fraction(WINDOWS_PLATE), shape="rounded"))
        elif t == "web":
            plan.need(Master("rounded", "Rounded (Windows/Web)", "light", 512,
                             zoom=zoom_for_fraction(WINDOWS_PLATE), shape="rounded"))
            plan.need(_full("light"))
            plan.need(Master("maskable", "Maskable", "light", 512, full_bleed=True, art_scale=MASKABLE_ART,
                             transparent=False))
        elif t == "marketing":
            hero = CameraSpec(view="front", iso=HERO_ISO).model_dump()
            iso = CameraSpec(view="front", iso=1.0).model_dump()
            plan.need(Master(f"hero-{primary}", "Hero", primary, 1024, camera=hero, hero=True))
            plan.need(Master(f"hero-iso-{primary}", "Isometric hero", primary, 1024, camera=iso, hero=True))
            if "dark" in apps and primary != "dark":
                plan.need(Master("hero-dark", "Hero (dark)", "dark", 1024, camera=hero, hero=True))
    return plan


def master_size(m: Master, quality: str, persistent_max: int, cap: int | None) -> int:
    if quality in ("draft", "preview"):
        size = min(m.native, persistent_max)  # GPU rule: interactive tiers stay ≤ 512 px
    elif quality == "ultra" and m.hero:
        size = 2048
    else:
        size = m.native
    if cap:
        size = min(size, cap)
    return max(int(size), 16)


def make_variant(project: Project, m: Master) -> Project:
    """Apply a master's overrides to a deep copy of the project (shape, visibility, art scale)."""
    p = project.model_copy(deep=True)
    if m.shape and p.canvas.shape != "none" and p.canvas.plate.visible:
        p.canvas.shape = m.shape  # type: ignore[assignment]
    if not m.plate:
        p.canvas.plate.visible = False
    if not m.layers:
        for layer in p.layers:
            layer.visible = False
        for ov in (p.appearances.dark, p.appearances.mono):
            for lo in ov.layers.values():
                if lo.visible is not None:
                    lo.visible = False
    if m.art_scale != 1.0:
        k = m.art_scale
        p.canvas.art.scale *= k
        p.canvas.art.x *= k
        p.canvas.art.y *= k
        for layer in p.layers:
            layer.transform.x *= k
            layer.transform.y *= k
    return p


def master_camera(project: Project, m: Master) -> dict:
    """Explicit camera (marketing heroes), else the head-on front ortho camera at the master's zoom."""
    if m.camera is not None:
        return m.camera
    return CameraSpec(view="front", zoom=m.zoom if not m.full_bleed else 1.0, fov=project.camera.fov,
                      iso=0.0).model_dump()


# ---------------------------------------------------------------------------------------------- image helpers
def _img():
    from PIL import Image

    return Image


def load_rgba(path: Path):
    Image = _img()
    with Image.open(path) as im:
        return im.convert("RGBA")


def resize(im, size: int | tuple[int, int]):
    """High-quality downscale with premultiplied alpha (no dark fringes); light sharpening for tiny sizes."""
    Image = _img()
    from PIL import ImageFilter

    wh = (size, size) if isinstance(size, int) else size
    if im.size == wh:
        return im.copy()
    out = im.convert("RGBa").resize(wh, Image.Resampling.LANCZOS, reducing_gap=3.0).convert("RGBA")
    if max(wh) <= 64 and max(im.size) > max(wh) * 2:
        r, g, b, a = out.split()
        rgb = Image.merge("RGB", (r, g, b)).filter(ImageFilter.UnsharpMask(radius=0.6, percent=55, threshold=1))
        out = Image.merge("RGBA", (*rgb.split(), a))
    return out


def flatten(im, color=(0, 0, 0)):
    Image = _img()
    bg = Image.new("RGBA", im.size, tuple(color) + (255,))
    return Image.alpha_composite(bg, im).convert("RGB")


def fit_center(im, size: int, fraction: float):
    """Scale `im` to `fraction` of a size×size transparent canvas, centred."""
    Image = _img()
    inner = max(1, round(size * fraction))
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    scaled = resize(im, inner)
    off = (size - inner) // 2
    canvas.alpha_composite(scaled, (off, off))
    return canvas


def crop_center(im, fraction: float):
    w, h = im.size
    cw, ch = round(w * fraction), round(h * fraction)
    x0, y0 = (w - cw) // 2, (h - ch) // 2
    return im.crop((x0, y0, x0 + cw, y0 + ch))


def shape_mask(size: int, shape: str, radius: float = 0.0, ss: int = 4):
    """Anti-aliased mask (supersampled): circle or rounded square (radius as a fraction of size)."""
    Image = _img()
    from PIL import ImageDraw

    big = Image.new("L", (size * ss, size * ss), 0)
    d = ImageDraw.Draw(big)
    box = [0, 0, size * ss - 1, size * ss - 1]
    if shape == "circle":
        d.ellipse(box, fill=255)
    else:
        d.rounded_rectangle(box, radius=radius * size * ss, fill=255)
    return big.resize((size, size), Image.Resampling.LANCZOS)


def apply_mask(im, mask):
    from PIL import ImageChops

    out = im.copy()
    out.putalpha(ImageChops.multiply(im.getchannel("A"), mask))
    return out


def grayscale(im):
    """iOS 'tinted' slot: grayscale image on black (the system applies the tint)."""
    from PIL import ImageOps

    return ImageOps.autocontrast(ImageOps.grayscale(flatten(im)), cutoff=0.5).convert("RGB")


def monochrome(im):
    """Android themed-icon layer: white, alpha = coverage × luminance stretched to 0.25..1."""
    import numpy as np

    Image = _img()
    rgba = im.convert("RGBA")
    arr = np.asarray(rgba, dtype=np.float32) / 255.0
    a = arr[..., 3]
    lum = arr[..., 0] * 0.2126 + arr[..., 1] * 0.7152 + arr[..., 2] * 0.0722
    covered = a > 0.05
    if covered.any():
        lo, hi = float(lum[covered].min()), float(lum[covered].max())
    else:
        lo, hi = 0.0, 1.0
    stretched = 0.25 + 0.75 * (lum - lo) / (hi - lo) if hi - lo > 0.02 else np.ones_like(lum)
    out_a = np.clip(a * stretched, 0.0, 1.0)
    out = np.empty(arr.shape, dtype=np.uint8)
    out[..., :3] = 255
    out[..., 3] = np.round(out_a * 255).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def save_png(im, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "PNG", optimize=True)
    return path


def write_json(path: Path, obj: Any) -> Path:
    atomic_write_text(path, json.dumps(obj, indent=2) + "\n")
    return path


# ---------------------------------------------------------------------------------------------- packagers
XCODE_INFO = {"author": "xcode", "version": 1}


def package_ios(out: Path, plan: ExportPlan, masters: dict[str, Path]) -> list[Path]:
    d = out / "ios" / "AppIcon.appiconset"
    files: list[Path] = []
    images = []
    light = load_rgba(masters[f"full-{plan.primary}"])
    files.append(save_png(flatten(resize(light, 1024)), d / "AppIcon-1024.png"))
    images.append({"filename": "AppIcon-1024.png", "idiom": "universal", "platform": "ios", "size": "1024x1024"})
    if "full-dark" in masters and plan.primary != "dark":
        dark = load_rgba(masters["full-dark"])
        files.append(save_png(flatten(resize(dark, 1024)), d / "AppIcon-1024-dark.png"))
        images.append({"appearances": [{"appearance": "luminosity", "value": "dark"}],
                       "filename": "AppIcon-1024-dark.png", "idiom": "universal", "platform": "ios",
                       "size": "1024x1024"})
    tinted = next((k for k in ("full-tinted-dark", "full-tinted-light") if k in masters), None)
    if tinted:
        t = load_rgba(masters[tinted])
        files.append(save_png(grayscale(resize(t, 1024)), d / "AppIcon-1024-tinted.png"))
        images.append({"appearances": [{"appearance": "luminosity", "value": "tinted"}],
                       "filename": "AppIcon-1024-tinted.png", "idiom": "universal", "platform": "ios",
                       "size": "1024x1024"})
    files.append(write_json(d / "Contents.json", {"images": images, "info": XCODE_INFO}))
    return files


def package_macos(out: Path, plan: ExportPlan, masters: dict[str, Path]) -> list[Path]:
    Image = _img()
    base = resize(load_rgba(masters["macos"]), 1024)
    iconset = out / "macos" / "AppIcon.iconset"
    appiconset = out / "macos" / "AppIcon.appiconset"
    files: list[Path] = []
    by_px: dict[int, Any] = {}
    entries = []
    for pt, scale in MACOS_ICONSET:
        px = pt * scale
        im = by_px.get(px) or by_px.setdefault(px, resize(base, px))
        name = f"icon_{pt}x{pt}{'@2x' if scale == 2 else ''}.png"
        files.append(save_png(im, iconset / name))
        files.append(save_png(im, appiconset / name))
        entries.append({"filename": name, "idiom": "mac", "scale": f"{scale}x", "size": f"{pt}x{pt}"})
    files.append(write_json(appiconset / "Contents.json", {"images": entries, "info": XCODE_INFO}))
    icns = out / "macos" / "AppIcon.icns"
    extra = [by_px.get(s) or resize(base, s) for s in (16, 32, 64, 128, 256, 512)]
    base.save(icns, format="ICNS", append_images=extra)
    files.append(icns)
    return files


def package_watchos(out: Path, plan: ExportPlan, masters: dict[str, Path]) -> list[Path]:
    d = out / "watchos"
    files: list[Path] = []
    square = flatten(resize(load_rgba(masters["full-light"]), 1024))
    files.append(save_png(square, d / "AppIcon.appiconset" / "AppIcon-1024.png"))
    files.append(write_json(d / "AppIcon.appiconset" / "Contents.json", {
        "images": [{"filename": "AppIcon-1024.png", "idiom": "universal", "platform": "watchos",
                    "size": "1024x1024"}],
        "info": XCODE_INFO,
    }))
    circle = resize(load_rgba(masters["circle"]), 1088)
    files.append(save_png(apply_mask(circle, shape_mask(1088, "circle")), d / "AppIcon-1088-circle.png"))
    return files


ANDROID_ADAPTIVE_XML = """<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@mipmap/ic_launcher_background" />
    <foreground android:drawable="@mipmap/ic_launcher_foreground" />
    <monochrome android:drawable="@mipmap/ic_launcher_monochrome" />
</adaptive-icon>
"""

ANDROID_README = """Android launcher icons, generated by Blender Icon Studio
==========================================================

Copy the `res/` folder into `app/src/main/` (merge with your existing res/).

* mipmap-anydpi-v26/ic_launcher.xml + ic_launcher_round.xml: adaptive icon (API 26+), with
  background = the plate, foreground = the 3D layers (plate hidden), monochrome = themed icon (API 33+).
  Layers are 108 dp; the icon body occupies the central 72 dp (safe zone 66 dp).
* mipmap-<density>/ic_launcher.png, ic_launcher_round.png: legacy icons (API < 26), 48 dp.
* playstore-icon.png: 512 x 512 Google Play listing icon (Play applies the mask).

AndroidManifest.xml:
    <application android:icon="@mipmap/ic_launcher" android:roundIcon="@mipmap/ic_launcher_round" ...>

Adaptive icon XML (already written to mipmap-anydpi-v26/):
"""


def package_android(out: Path, plan: ExportPlan, masters: dict[str, Path]) -> list[Path]:
    Image = _img()
    d = out / "android"
    res = d / "res"
    files: list[Path] = []
    fg = resize(load_rgba(masters["android-fg"]), 432)
    bg = resize(load_rgba(masters["android-bg"]), 432)
    mono = monochrome(resize(load_rgba(masters["android-mono"]), 432))
    composite = Image.alpha_composite(bg, fg)
    visible = crop_center(composite, ANDROID_VIEWPORT)  # what the launcher shows (before its mask)
    for dens, m in ANDROID_DENSITIES.items():
        folder = res / f"mipmap-{dens}"
        layer_px = round(108 * m)
        files.append(save_png(resize(fg, layer_px), folder / "ic_launcher_foreground.png"))
        files.append(save_png(flatten(resize(bg, layer_px), (255, 255, 255)), folder / "ic_launcher_background.png"))
        files.append(save_png(resize(mono, layer_px), folder / "ic_launcher_monochrome.png"))
        legacy_px = round(48 * m)
        body = round(legacy_px * LEGACY_ANDROID)
        sq = apply_mask(resize(visible, body), shape_mask(body, "rounded", radius=0.18))
        rd = apply_mask(resize(visible, body), shape_mask(body, "circle"))
        files.append(save_png(fit_center(sq, legacy_px, body / legacy_px), folder / "ic_launcher.png"))
        files.append(save_png(fit_center(rd, legacy_px, body / legacy_px), folder / "ic_launcher_round.png"))
    anydpi = res / "mipmap-anydpi-v26"
    for name in ("ic_launcher.xml", "ic_launcher_round.xml"):
        p = anydpi / name
        atomic_write_text(p, ANDROID_ADAPTIVE_XML)
        files.append(p)
    files.append(save_png(flatten(resize(load_rgba(masters["full-light"]), 512)), d / "playstore-icon.png"))
    readme = d / "README.txt"
    atomic_write_text(readme, ANDROID_README + "\n" + ANDROID_ADAPTIVE_XML)
    files.append(readme)
    return files


def package_windows(out: Path, plan: ExportPlan, masters: dict[str, Path]) -> list[Path]:
    d = out / "windows"
    base = load_rgba(masters["rounded"])
    files: list[Path] = []
    frames = {s: resize(base, s) for s in ICO_SIZES}
    ico = d / "app.ico"
    ico.parent.mkdir(parents=True, exist_ok=True)
    frames[256].save(ico, format="ICO", sizes=[(s, s) for s in ICO_SIZES],
                     append_images=[frames[s] for s in ICO_SIZES if s != 256])
    files.append(ico)
    for s in (16, 24, 32, 48, 64, 128, 256):
        files.append(save_png(frames[s], d / "png" / f"icon-{s}.png"))
    return files


def package_web(out: Path, plan: ExportPlan, masters: dict[str, Path], source_svg: Path | None,
                name: str) -> list[Path]:
    d = out / "web"
    files: list[Path] = []
    rounded = load_rgba(masters["rounded"])
    tight = crop_center(rounded, min(1.0, WINDOWS_PLATE + 0.03))  # favicons: no wasted margin
    fav = {s: resize(tight, s) for s in FAVICON_SIZES}
    ico = d / "favicon.ico"
    ico.parent.mkdir(parents=True, exist_ok=True)
    fav[48].save(ico, format="ICO", sizes=[(s, s) for s in FAVICON_SIZES], append_images=[fav[16], fav[32]])
    files.append(ico)
    files.append(save_png(fav[32], d / "favicon-32.png"))
    files.append(save_png(flatten(resize(load_rgba(masters["full-light"]), 180)), d / "apple-touch-icon.png"))
    files.append(save_png(resize(rounded, 192), d / "icon-192.png"))
    files.append(save_png(resize(rounded, 512), d / "icon-512.png"))
    files.append(save_png(flatten(resize(load_rgba(masters["maskable"]), 512)), d / "maskable-512.png"))
    if source_svg is not None and source_svg.is_file():
        shutil.copyfile(source_svg, d / "favicon.svg")
        files.append(d / "favicon.svg")
    manifest = {
        "name": name,
        "short_name": name[:12],
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    files.append(write_json(d / "manifest.webmanifest", manifest))
    head = d / "head-snippet.html"
    svg_line = '<link rel="icon" href="/favicon.svg" type="image/svg+xml">\n' if (d / "favicon.svg").is_file() else ""
    atomic_write_text(head, (
        '<link rel="icon" href="/favicon.ico" sizes="48x48">\n'
        f"{svg_line}"
        '<link rel="apple-touch-icon" href="/apple-touch-icon.png">\n'
        '<link rel="manifest" href="/manifest.webmanifest">\n'
    ))
    files.append(head)
    return files


def package_marketing(out: Path, plan: ExportPlan, masters: dict[str, Path]) -> list[Path]:
    d = out / "marketing"
    files = []
    for key, path in masters.items():
        if not key.startswith("hero"):
            continue
        name = {"hero-dark": "hero-dark.png"}.get(key)
        if name is None:
            name = "hero-iso.png" if key.startswith("hero-iso") else "hero.png"
        dst = d / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dst)
        files.append(dst)
    return files


PREVIEW_CANDIDATES = [
    "ios/AppIcon.appiconset/AppIcon-1024.png",
    "ios/AppIcon.appiconset/AppIcon-1024-dark.png",
    "ios/AppIcon.appiconset/AppIcon-1024-tinted.png",
    "macos/AppIcon.iconset/icon_512x512@2x.png",
    "watchos/AppIcon-1088-circle.png",
    "android/res/mipmap-xxxhdpi/ic_launcher_round.png",
    "android/res/mipmap-xxxhdpi/ic_launcher_foreground.png",
    "windows/png/icon-256.png",
    "web/icon-512.png",
    "web/maskable-512.png",
    "marketing/hero.png",
    "marketing/hero-iso.png",
    "marketing/hero-dark.png",
]


def write_readme(out: Path, project: Project, plan: ExportPlan, quality: str) -> Path:
    lines = [
        f"{project.name}: icon export (Blender Icon Studio)",
        "=" * 60,
        f"Quality: {quality} · appearances: {', '.join(plan.appearances)} · targets: {', '.join(plan.targets)}",
        "",
    ]
    notes = {
        "ios": "ios/AppIcon.appiconset: drop into Assets.xcassets (1024 universal + dark + tinted luminosity variants).",
        "macos": "macos/AppIcon.icns, AppIcon.iconset (iconutil) and AppIcon.appiconset: Tahoe 824/1024 body, transparent margins.",
        "watchos": "watchos/AppIcon.appiconset (1024 square, system applies the circle) + AppIcon-1088-circle.png preview.",
        "android": "android/res: adaptive icon (foreground/background/monochrome) + legacy mipmaps; see android/README.txt.",
        "windows": "windows/app.ico: 16…256 px multi-size ICO, plus PNGs.",
        "web": "web/: favicon.ico (16/32/48), apple-touch-icon (180), PWA icons (192/512 + maskable) and manifest/head snippets.",
        "marketing": "marketing/: hero renders from a CAD-style 3/4 view with the real layer depths (hero.png, "
                     "isometric hero-iso.png, hero-dark.png; transparent PNG).",
        "icon": "icon/*.icon: Apple Icon Composer bundle (BETA: community-documented schema, verify in Icon Composer).",
        "blend": "blend/*.blend: the Blender scene with packed textures (open with Blender 5.0).",
    }
    for t in plan.targets:
        lines.append(f"- {notes[t]}")
    p = out / "README.txt"
    atomic_write_text(p, "\n".join(lines) + "\n")
    return p


def zip_dir(src: Path, target: Path, root_name: str, skip: Iterable[str] = ("_masters",)) -> None:
    skip = set(skip)
    tmp = target.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in sorted(src.rglob("*")):
            rel = f.relative_to(src)
            if f.is_dir() or rel.parts[0] in skip:
                continue
            zf.write(f, f"{root_name}/{rel.as_posix()}")
    tmp.replace(target)


# ---------------------------------------------------------------------------------------------- job runner
async def run_export(svc: "RenderService", ctx: "JobContext", pid: str, req: ExportRequest) -> dict[str, Any]:
    out = svc.store.exports_dir(pid) / ctx.job.id
    try:
        return await _run_export(svc, ctx, pid, req, out)
    except BaseException:
        # failed / cancelled: leave no half-written package behind
        await asyncio.to_thread(shutil.rmtree, out, True)
        raise


async def _run_export(svc: "RenderService", ctx: "JobContext", pid: str, req: ExportRequest,
                      out: Path) -> dict[str, Any]:
    t0 = time.perf_counter()
    store = svc.store
    settings = svc.settings
    project = await asyncio.to_thread(store.load, pid)
    plan = plan_export(project, req)
    if not plan.targets:
        raise ValueError("No export targets selected")
    check_path_budget(out)
    quality = req.quality
    if out.exists():
        shutil.rmtree(out)
    masters_dir = out / "_masters"
    masters_dir.mkdir(parents=True)
    steps = len(plan.masters) + ("blend" in plan.targets) + ("icon" in plan.targets)
    span = 0.85 / max(steps, 1)
    step = 0
    rendered: dict[str, Path] = {}
    ctx.progress(0.02, f"Export plan: {len(plan.masters)} master renders · {', '.join(plan.targets)}")

    for key, m in plan.masters.items():
        await ctx.yield_to_live()  # keep live drafts/previews responsive during a long export
        ctx.check()
        start = 0.02 + span * step
        variant = make_variant(project, m)
        gpath = await svc.geometry(variant)
        size = master_size(m, quality, settings.persistent_max_size, settings.export_max_size)
        dst = masters_dir / f"{key}.png"
        ctx.progress(start, f"Rendering {m.label} ({step + 1}/{steps}) · {size}px")
        await svc.render_image(
            ctx, variant, gpath,
            appearance=m.appearance, quality=quality, size=size, out=dst,
            camera=master_camera(project, m), full_bleed=m.full_bleed, transparent=m.transparent,
            oneshot=True, on_progress=ctx.sub(start, start + span, f"{m.label}: "),
        )
        rendered[key] = dst
        step += 1

    name = safe_filename(project.name, max_len=EXPORT_NAME_MAX, default="AppIcon")
    if "blend" in plan.targets:
        await ctx.yield_to_live()
        ctx.check()
        start = 0.02 + span * step
        ctx.progress(start, "Saving .blend…")
        gpath = await svc.geometry(project)
        await svc.save_blend(ctx, project, gpath, out / "blend" / f"{name}.blend", plan.primary,
                             ctx.sub(start, start + span, "Blend: "))
        step += 1
    if "icon" in plan.targets:
        await ctx.yield_to_live()
        ctx.check()
        ctx.progress(0.02 + span * step, "Writing .icon bundle (beta)…")
        bundle, _ = await asyncio.to_thread(store.geometry, project)
        from .icon_format import write_icon_bundle

        await asyncio.to_thread(write_icon_bundle, project, bundle, settings.workspace, out / "icon", name,
                                svc.presets.raw())
        step += 1

    ctx.check()
    ctx.progress(0.88, "Packaging platform assets…")
    source_svg = store.dir(pid) / "source.svg"

    def package() -> None:
        for t in plan.targets:
            if t == "ios":
                package_ios(out, plan, rendered)
            elif t == "macos":
                package_macos(out, plan, rendered)
            elif t == "watchos":
                package_watchos(out, plan, rendered)
            elif t == "android":
                package_android(out, plan, rendered)
            elif t == "windows":
                package_windows(out, plan, rendered)
            elif t == "web":
                package_web(out, plan, rendered, source_svg, project.name)
            elif t == "marketing":
                package_marketing(out, plan, rendered)
        write_readme(out, project, plan, quality)

    await asyncio.to_thread(package)
    shutil.rmtree(masters_dir, ignore_errors=True)
    ctx.check()
    ctx.progress(0.96, "Zipping…")
    zip_path = store.exports_dir(pid) / f"{ctx.job.id}.zip"
    root_name = f"{name} icons"
    await asyncio.to_thread(zip_dir, out, zip_path, root_name)

    ws = settings.workspace
    files = sorted(f for f in out.rglob("*") if f.is_file())
    rel = {f.relative_to(out).as_posix(): f for f in files}
    zip_url = files_url(ws, zip_path)
    return {
        "zip": zip_url,
        "url": zip_url,
        "zipBytes": zip_path.stat().st_size,
        "folder": str(out),
        "files": [{"name": n, "url": files_url(ws, f)} for n, f in rel.items()],
        "previews": [{"name": n, "url": files_url(ws, rel[n])} for n in PREVIEW_CANDIDATES if n in rel],
        "targets": plan.targets,
        "appearances": plan.appearances,
        "quality": quality,
        "masters": len(plan.masters),
        "seconds": round(time.perf_counter() - t0, 3),
    }
