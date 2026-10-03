"""Runs docs/research/svg_prototype.py over every test SVG and every strategy and validates:
  1. normalisation fidelity: resvg(original) vs resvg(normalized picosvg output)
  2. recomposition fidelity: layers rendered separately and alpha-composited in layer order
  3. spline round-trip: silhouette splines -> SVG path (nonzero AND evenodd) vs silhouette raster
Writes contact sheets + JSON + summary.json into $BIS_SVG_OUT (default: %TEMP%/bis_svg_regression).
Needs: resvg-py, pillow, numpy (plus the prototype's deps).
"""
import io, os, re, sys, glob, json, time, traceback
import numpy as np
from PIL import Image, ImageDraw
import resvg_py

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import svg_prototype as sp  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.join(HERE, "svgtests")
import tempfile
OUT = os.environ.get("BIS_SVG_OUT", os.path.join(tempfile.gettempdir(), "bis_svg_regression"))
os.makedirs(OUT, exist_ok=True)
RES = 192


def render(svg_text, w=RES, h=None):
    png = resvg_py.svg_to_bytes(svg_string=svg_text, width=w, height=h or w)
    return Image.open(io.BytesIO(bytes(png))).convert("RGBA")


def checker(size, n=12):
    img = Image.new("RGBA", size, (255, 255, 255, 255))
    d = ImageDraw.Draw(img)
    for y in range(0, size[1], n):
        for x in range(0, size[0], n):
            if (x // n + y // n) % 2:
                d.rectangle([x, y, x + n - 1, y + n - 1], fill=(225, 225, 225, 255))
    return img


def on_white(img):
    return Image.alpha_composite(Image.new("RGBA", img.size, (255, 255, 255, 255)), img)


def diff_pct(a, b, thr=24):
    A = np.asarray(on_white(a).convert("RGB"), dtype=np.int16)
    B = np.asarray(on_white(b).convert("RGB"), dtype=np.int16)
    d = np.abs(A - B).max(axis=2)
    return round(float((d > thr).mean() * 100), 3)


def spline_mask(splines, vb, fill_rule, w, h):
    # splines are in icon space (y up, centred, longer side = 1): map back to the SVG viewBox
    s = max(vb[2], vb[3]); cx = vb[0] + vb[2] / 2; cy = vb[1] + vb[3] / 2
    d = sp.splines_to_d(splines)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb[0]} {vb[1]} {vb[2]} {vb[3]}">'
           f'<path fill="#000" fill-rule="{fill_rule}" transform="matrix({s} 0 0 {-s} {cx} {cy})" d="{d}"/></svg>')
    return np.asarray(render(svg, w, h))[:, :, 3].astype(int)


def main():
    summary = {}
    for path in sorted(glob.glob(os.path.join(TESTS, "*.svg"))):
        name = os.path.basename(path).replace(".svg", "")
        src = open(path, encoding="utf-8").read()
        summary[name] = {}
        for strategy in ("smart", "group", "color", "element"):
            t0 = time.perf_counter()
            try:
                res = sp.process(src, strategy)
            except Exception as ex:  # noqa
                summary[name][strategy] = {"error": f"{type(ex).__name__}: {ex}", "tb": traceback.format_exc()[-1500:]}
                print(name, strategy, "ERROR", ex)
                continue
            dt = time.perf_counter() - t0
            vb = res["view_box"]
            W = RES
            H = max(1, int(round(RES * vb[3] / vb[2])))
            orig = render(src, W, H)
            norm = render(res["normalized_svg"], W, H)
            comp = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            thumbs = []
            spline_iou = []
            for L in res["layers"]:
                li = render(L["svg"], W, H)
                comp = Image.alpha_composite(comp, li)
                thumbs.append((L["name"], li))
                sil_svg = re.sub(r'fill="[^"]*"', 'fill="#000"', re.sub(r' opacity="[^"]*"', "", L["svg"]))
                alpha = np.asarray(render(sil_svg, W, H))[:, :, 3].astype(int)
                for rule in ("nonzero", "evenodd"):
                    m = spline_mask(L["silhouette"], vb, rule, W, H)
                    # % of pixels whose coverage differs by > 25% (pure AA noise excluded)
                    spline_iou.append(round(float((np.abs(m - alpha) > 64).mean() * 100), 4))
            r = {
                "seconds": round(dt, 3),
                "n_elements": len(res["elements"]),
                "layers": [(L["name"], len(L["members"]), L["paints"]) for L in res["layers"]],
                "normalize_diff_pct": diff_pct(orig, norm),
                "recompose_diff_pct": diff_pct(orig, comp),
                "max_spline_err_pct": max(spline_iou) if spline_iou else None,
                "warnings": res["warnings"],
                "info": res["split_info"],
            }
            summary[name][strategy] = r
            json.dump(res, open(os.path.join(OUT, f"{name}.{strategy}.json"), "w"), indent=1)
            if strategy in ("smart", "group"):
                pad = 6
                n = 2 + len(thumbs)
                sheet = Image.new("RGBA", (n * (W + pad) + pad, H + 2 * pad + 14), (255, 255, 255, 255))
                dr = ImageDraw.Draw(sheet)
                tiles = [("original", orig), ("recomposed", comp)] + thumbs
                for k, (lab, im) in enumerate(tiles):
                    x = pad + k * (W + pad)
                    bg = checker((W, H))
                    sheet.paste(Image.alpha_composite(bg, im), (x, pad))
                    dr.text((x, H + pad + 1), lab[:28], fill=(0, 0, 0, 255))
                sheet.convert("RGB").save(os.path.join(OUT, f"{name}.{strategy}.sheet.png"))
            print(f"{name:24s} {strategy:8s} {dt:6.2f}s layers={len(res['layers'])} "
                  f"norm={r['normalize_diff_pct']}% recomp={r['recompose_diff_pct']}% spline_err={r['max_spline_err_pct']}%")
    json.dump(summary, open(os.path.join(OUT, "summary.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
