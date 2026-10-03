# Blender Icon Studio — project notes

Local app that turns flat SVG icons into layered 3D / glass / Liquid-Glass icons rendered by Blender 5.0.
Read `docs/PLAN.md` first (architecture, binding decisions, ownership, protocols). Research: `docs/research/`.

## Environment (Windows 11, RTX 3070 Ti 8 GB, i7-12700K)
- Blender: `C:/Program Files/Blender Foundation/Blender 5.0/blender.exe` — **always Blender 5.0, always OptiX**.
  Headless: `"<blender>" -b --factory-startup --python <script> -- <args>`. Blender's Python is 3.11 with bpy +
  numpy only — code under `blender_worker/` must not import anything else (no pydantic, no project venv modules).
- Backend Python: `.venv/Scripts/python.exe` (3.13). Run tests: `.venv/Scripts/python.exe -m pytest -q`.
- Server: `.venv/Scripts/python.exe -m uvicorn bis.main:app --app-dir server --port 8420`.
- Web: `cd web && npm run dev` (5173, proxies to 8420) · `npx tsc -b --noEmit` · `npx vite build`.
- The user's test corpus is `samle icons/` (68 real icons; the folder name typo is intentional — do not rename).

## GPU rules (user requirement)
- Cycles must use the **OptiX** device only (disable CUDA + CPU device entries) and the OptiX denoiser.
- The GPU is shared with the desktop (~3.5–4.7 GB already used). **Never render big while testing**: draft/preview
  tiers at ≤ 512 px (tests: ≤ 256 px, ≤ 32 spp). Final/ultra tiers only via explicit user action.
- `use_persistent_data = False`. Do not enable caustics/MNEE in tests (164 s kernel compile).

## Conventions
- Data contract: `server/bis/models.py` ⇄ `web/src/types.ts` (camelCase field names = JSON keys) and
  `shared/presets.json`. Change them only additively and keep both sides in sync.
- Art space: centre origin, y up, longer viewBox side = 2.0 (−1..1). Blender: icon in XY plane, camera +Z.
- Runtime data goes to `workspace/` (gitignored). Throwaway files go to the session scratchpad, not the repo.
- Quote paths: the repo path contains spaces.
