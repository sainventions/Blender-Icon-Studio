"""Benchmark the persistent worker (not a pytest test): warm draft / preview latency at 512 px and VRAM
stability over 20 renders. Draft/preview tiers only (GPU rules).

    .venv/Scripts/python.exe tests/blender/bench_worker.py [--icon Maps] [--size 512] [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import worker_client as wc  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--icon", default="Maps")
    ap.add_argument("--size", type=int, default=512)
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "bis_bench"))
    a = ap.parse_args(argv)
    size = min(512, a.size)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    import make_fixtures
    index = make_fixtures.make([a.icon], HERE / "_fixtures")
    e = index[a.icon]
    proj = json.loads(Path(e["project"]).read_text(encoding="utf-8"))
    base_mem = wc.gpu_memory_used_mib()
    t0 = time.perf_counter()
    w = wc.Worker(warmup="full")
    print(f"startup {w.startup_seconds:.2f}s (warm-up {w.ready['warmupSeconds']}s, {w.ready.get('warmupTimings')})")
    report = {"startup": round(w.startup_seconds, 2), "warmup": w.ready["warmupSeconds"]}
    try:
        def render(q, i, **kw):
            return w.result("render", {"project": proj, "geometryPath": e["geometryPath"], "quality": q, "size": size,
                                       "out": str(out / f"{q}_{i}.png"), **kw})
        for q, n in (("draft", 8), ("preview", 5)):
            render(q, "first")
            secs = [render(q, i)["seconds"] for i in range(n)]
            report[q] = {"min": round(min(secs), 3), "median": round(statistics.median(secs), 3),
                         "max": round(max(secs), 3)}
            print(f"{q:8s} @{size}px: min {min(secs):.3f}s  median {statistics.median(secs):.3f}s  max {max(secs):.3f}s")
        # slider drag: value-only change (in-place material update, no recompile)
        p2 = json.loads(json.dumps(proj))
        secs = []
        for i in range(6):
            for L in p2["layers"]:
                L["material"]["params"] = {"tint": 0.3 + 0.1 * i, "frost": 0.05 * i}
            p2["lighting"] = {"angle": -60 + 20 * i}
            secs.append(w.result("render", {"project": p2, "geometryPath": e["geometryPath"], "quality": "draft",
                                            "size": size, "out": str(out / f"drag_{i}.png")})["seconds"])
        report["draftSliderDrag"] = round(statistics.median(secs), 3)
        print(f"draft slider drag (values only) median {statistics.median(secs):.3f}s")
        time.sleep(1.0)
        m0 = wc.gpu_memory_used_mib()
        for i in range(20):
            render("preview" if i % 2 else "draft", f"vram{i % 2}",
                   appearance=["light", "dark", "clear-dark", "tinted-dark"][i % 4])
        time.sleep(1.0)
        m1 = wc.gpu_memory_used_mib()
        report["vram"] = {"desktopBaseline": base_mem, "before20": m0, "after20": m1,
                          "growth": None if m0 is None or m1 is None else m1 - m0}
        print(f"VRAM (whole GPU): baseline {base_mem} MiB, before 20 renders {m0} MiB, after {m1} MiB")
    finally:
        w.close()
    report["total"] = round(time.perf_counter() - t0, 1)
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
