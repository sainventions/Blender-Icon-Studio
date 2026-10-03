// Viewport dev harness (web/harness.html): renders <Viewport/> against a real backend project when one is available
// (/api/projects → first project → /geometry), otherwise against a synthetic fixture. Includes controls for every
// viewport input plus FPS and leak diagnostics. Dev-only; not part of the app bundle.
import {
  StrictMode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from 'react'
import { createRoot } from 'react-dom/client'
import type { AppearanceId, GeometryBundle, Layer, LayerTransform, Presets, Project } from '../../types'
import { LightDial, Viewport } from '../index'
import { buildFixture, disposeFixture, type Fixture, type FixtureVariant } from './fixture'

type Source = FixtureVariant | 'backend'

const APPEARANCES: AppearanceId[] = ['light', 'dark', 'clear-light', 'clear-dark', 'tinted-light', 'tinted-dark']
const MATERIALS = [
  'liquid_glass',
  'clear_glass',
  'frosted_glass',
  'dispersive_crystal',
  'tinted_glass',
  'glossy_plastic',
  'satin',
  'candy',
  'gummy',
  'jelly',
  'chrome',
  'brushed_metal',
  'matte_clay',
  'iridescent',
  'neon',
  'flat',
]
const LIGHTING = ['studio', 'soft', 'dramatic', 'top', 'darkfield', 'sunset', 'neon', 'flat']

async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(url)
  if (!r.ok) throw new Error(`${url}: ${r.status}`)
  return (await r.json()) as T
}

async function loadPresets(): Promise<Presets | null> {
  try {
    return await getJson<Presets>('/api/presets')
  } catch {
    try {
      // Works in builds and when Vite's server.fs.allow includes the repo root.
      const mod = await import('../../../../shared/presets.json')
      return (mod.default ?? mod) as unknown as Presets
    } catch {
      return null // materials3d falls back to built-in defaults mirroring presets.json
    }
  }
}

async function loadBackend(projectId?: string): Promise<{ project: Project; geometry: GeometryBundle }> {
  const list = await getJson<{ id: string; name: string }[]>('/api/projects')
  if (!list.length) throw new Error('backend has no projects')
  // ?project=<id or name> picks a specific one; default: the most recent.
  const want = (projectId ?? new URLSearchParams(location.search).get('project') ?? undefined)?.toLowerCase()
  const pick = (want && list.find((p) => p.id.toLowerCase() === want || p.name.toLowerCase() === want)) || list[0]
  const id = encodeURIComponent(pick.id)
  const [project, geometry] = await Promise.all([
    getJson<Project>(`/api/projects/${id}`),
    getJson<GeometryBundle>(`/api/projects/${id}/geometry`),
  ])
  return { project, geometry }
}

interface DebugHandle {
  gl: { info: { memory: { geometries: number; textures: number }; programs?: unknown[] | null } }
  invalidate: () => void
}
const debugHandle = () => (window as unknown as { __bisViewport?: DebugHandle }).__bisViewport
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

// ---------------------------------------------------------------------------------------------- UI atoms
const S = {
  panel: {
    width: 312,
    flex: '0 0 312px',
    overflowY: 'auto',
    background: '#0d0d11',
    borderRight: '1px solid rgba(255,255,255,0.07)',
    padding: '14px 14px 40px',
  } as CSSProperties,
  h: {
    font: '600 10px/14px Inter, system-ui',
    letterSpacing: '0.08em',
    textTransform: 'uppercase',
    color: '#85858f',
    margin: '18px 0 8px',
  } as CSSProperties,
  row: { display: 'flex', alignItems: 'center', gap: 8, margin: '6px 0' } as CSSProperties,
  label: { width: 92, color: '#b3b3be', flex: '0 0 92px' } as CSSProperties,
  btn: (on: boolean): CSSProperties => ({
    padding: '4px 8px',
    borderRadius: 6,
    border: '1px solid rgba(255,255,255,0.1)',
    cursor: 'pointer',
    background: on ? '#8f7dff' : '#18181e',
    color: on ? '#fff' : '#ececf1',
    font: '500 11px/16px Inter, system-ui',
  }),
  select: {
    flex: 1,
    background: '#18181e',
    color: '#ececf1',
    border: '1px solid rgba(255,255,255,0.1)',
    borderRadius: 6,
    padding: '3px 6px',
  } as CSSProperties,
  mono: {
    font: '11px/16px "JetBrains Mono", Consolas, monospace',
    color: '#b3b3be',
    whiteSpace: 'pre-wrap',
  } as CSSProperties,
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={S.row}>
      <span style={S.label}>{label}</span>
      {children}
    </div>
  )
}

function Slider(p: {
  label: string
  value: number
  min: number
  max: number
  step?: number
  onChange: (v: number) => void
}) {
  return (
    <Row label={p.label}>
      <input
        type="range"
        min={p.min}
        max={p.max}
        step={p.step ?? 0.01}
        value={p.value}
        onChange={(e) => p.onChange(+e.target.value)}
        style={{ flex: 1 }}
      />
      <span style={{ ...S.mono, width: 40, textAlign: 'right' }}>{p.value.toFixed(2)}</span>
    </Row>
  )
}

function Select(p: { label: string; value: string; options: string[]; onChange: (v: string) => void }) {
  return (
    <Row label={p.label}>
      <select style={S.select} value={p.value} onChange={(e) => p.onChange(e.target.value)}>
        {p.options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </Row>
  )
}

// ---------------------------------------------------------------------------------------------- app
// URL params (handy for screenshots): ?source=A|B|backend &appearance=… &explode=0.6 &view=orbit &grid=1
// &select=<layerId> &bare=1 (hide the panel)
const Q = new URLSearchParams(location.search)
const qSource = (Q.get('source') as Source | null) ?? 'backend'
const BARE = Q.get('bare') === '1'

/** Apply ?zoom= / ?angle= / ?shape= / ?colorMode= overrides to a freshly loaded project. */
function withQuery(p: Project): Project {
  const zoom = Number(Q.get('zoom'))
  const angle = Q.get('angle')
  const shape = Q.get('shape') as Project['canvas']['shape'] | null
  const colorMode = Q.get('colorMode') as Project['render']['colorMode'] | null
  const plateFill = Q.get('plateFill')
  const preset = Q.get('lighting')
  const material = Q.get('material') // applies to every layer
  const inflate = Number(Q.get('inflate') ?? 0) || 0
  let canvas = shape ? { ...p.canvas, shape } : p.canvas
  if (plateFill === 'system-light' || plateFill === 'system-dark')
    canvas = { ...canvas, plate: { ...canvas.plate, fill: { type: plateFill } } }
  else if (plateFill)
    canvas = { ...canvas, plate: { ...canvas.plate, fill: { type: 'solid', color: '#' + plateFill, opacity: 1 } } }
  return {
    ...p,
    camera: zoom > 0 ? { ...p.camera, zoom } : p.camera,
    lighting: { ...p.lighting, ...(angle !== null ? { angle: Number(angle) } : {}), ...(preset ? { preset } : {}) },
    layers: p.layers.map((l) => ({
      ...l,
      ...(material ? { material: { preset: material, params: {} } } : {}),
      ...(inflate ? { depth: { ...l.depth, inflate } } : {}),
    })),
    canvas,
    render: colorMode ? { ...p.render, colorMode } : p.render,
  }
}

function Harness() {
  const [presets, setPresets] = useState<Presets | null>(null)
  const [source, setSource] = useState<Source>(qSource)
  const [status, setStatus] = useState('loading…')
  const [project, setProject] = useState<Project | null>(null)
  const [geometry, setGeometry] = useState<GeometryBundle | null>(null)
  const [appearance, setAppearance] = useState<AppearanceId>((Q.get('appearance') as AppearanceId | null) ?? 'light')
  const [selected, setSelected] = useState<string | null>(Q.get('select'))
  const [explode, setExplode] = useState(Number(Q.get('explode') ?? 0) || 0)
  const [view, setView] = useState<'front' | 'orbit'>(Q.get('view') === 'orbit' ? 'orbit' : 'front')
  const [grid, setGrid] = useState(Q.get('grid') === '1')
  const [diag, setDiag] = useState('')
  const fixtureRef = useRef<Fixture | null>(null)

  useEffect(() => {
    void loadPresets().then(setPresets)
  }, [])

  const load = useCallback(async (src: Source, projectId?: string) => {
    setSource(src)
    try {
      let next: Project
      if (src === 'backend') {
        setStatus('fetching /api/projects…')
        const { project: p, geometry: g } = await loadBackend(projectId)
        next = withQuery(p)
        setProject(next)
        setGeometry(g)
        setStatus(`backend · ${p.name} · ${p.layers.length} layers`)
      } else {
        const f = await buildFixture(src)
        const old = fixtureRef.current
        fixtureRef.current = f
        next = withQuery(f.project)
        setProject(next)
        setGeometry(f.geometry)
        setStatus(`synthetic ${src} · ${f.project.layers.length} layers`)
        if (old) setTimeout(() => disposeFixture(old), 8000)
      }
      setSelected((sel) => (sel && next.layers.some((l) => l.id === sel) ? sel : null))
    } catch (err) {
      setStatus(`${String(err)} — using synthetic fixture`)
      if (src === 'backend') void load('A')
    }
  }, [])

  // Prefer a real project from the backend (unless ?source= says otherwise), else the synthetic fixture.
  useEffect(() => {
    void load(qSource)
  }, [load])

  const update = useCallback((fn: (p: Project) => Project) => setProject((p) => (p ? fn(p) : p)), [])
  const updateLayer = useCallback(
    (id: string, fn: (l: Layer) => Layer) =>
      update((p) => ({ ...p, layers: p.layers.map((l) => (l.id === id ? fn(l) : l)) })),
    [update],
  )
  const onLayerTransform = useCallback(
    (id: string, t: LayerTransform) => updateLayer(id, (l) => ({ ...l, transform: t })),
    [updateLayer],
  )

  // Dev automation (screenshots / framing checks): window.__bisHarness.{load, set, state}.
  const stateRef = useRef({ project, explode, view, appearance })
  stateRef.current = { project, explode, view, appearance }
  useEffect(() => {
    if (!import.meta.env.DEV) return
    const w = window as unknown as { __bisHarness?: unknown }
    w.__bisHarness = {
      load: (id: string) => load('backend', id),
      set: (o: { explode?: number; view?: 'front' | 'orbit'; appearance?: AppearanceId; grid?: boolean }) => {
        if (o.explode !== undefined) setExplode(o.explode)
        if (o.view) setView(o.view)
        if (o.appearance) setAppearance(o.appearance)
        if (o.grid !== undefined) setGrid(o.grid)
      },
      update: (fn: (p: Project) => Project) => update(fn),
      state: () => stateRef.current,
    }
    return () => {
      delete w.__bisHarness
    }
  }, [load, update])

  const layer = useMemo(() => project?.layers.find((l) => l.id === selected) ?? null, [project, selected])
  const materialIds = presets ? Object.keys(presets.materials) : MATERIALS
  const lightingIds = presets ? Object.keys(presets.lighting) : LIGHTING

  const measureFps = async () => {
    const h = debugHandle()
    if (!h) return setDiag('no debug handle (dev build only)')
    setDiag('measuring 3 s…')
    let frames = 0
    let running = true
    const t0 = performance.now()
    const tick = () => {
      if (!running) return
      frames++
      h.invalidate()
      requestAnimationFrame(tick)
    }
    requestAnimationFrame(tick)
    await sleep(3000)
    running = false
    const fps = (frames / (performance.now() - t0)) * 1000
    setDiag(`continuous redraw: ${fps.toFixed(1)} fps`)
  }

  const leakTest = async () => {
    const h = debugHandle()
    if (!h) return setDiag('no debug handle')
    setDiag('leak test: 20 project switches…')
    await load('A')
    await sleep(3500)
    const before = { ...h.gl.info.memory, programs: h.gl.info.programs?.length ?? 0 }
    for (let i = 0; i < 20; i++) {
      await load(i % 2 === 0 ? 'B' : 'A')
      await sleep(250)
    }
    await load('A')
    await sleep(3500) // > cache grace periods
    h.invalidate()
    await sleep(200)
    const after = { ...h.gl.info.memory, programs: h.gl.info.programs?.length ?? 0 }
    setDiag(
      `before: geo ${before.geometries} tex ${before.textures} prog ${before.programs}\nafter : geo ${after.geometries} tex ${after.textures} prog ${after.programs}`,
    )
  }

  const memory = () => {
    const h = debugHandle()
    if (!h) return
    const m = h.gl.info.memory
    setDiag(`geometries ${m.geometries} · textures ${m.textures} · programs ${h.gl.info.programs?.length ?? 0}`)
  }

  return (
    <div style={{ display: 'flex', height: '100%' }}>
      <aside style={{ ...S.panel, display: BARE ? 'none' : undefined }}>
        <div style={{ font: '600 14px/20px Inter, system-ui' }}>Viewport harness</div>
        <div style={{ ...S.mono, marginTop: 4 }}>{status}</div>

        <div style={S.h}>Source</div>
        <div style={{ ...S.row, flexWrap: 'wrap' }}>
          {(['A', 'B', 'backend'] as Source[]).map((s) => (
            <button key={s} style={S.btn(source === s)} onClick={() => void load(s)}>
              {s === 'backend' ? 'Backend' : `Synthetic ${s}`}
            </button>
          ))}
        </div>

        <div style={S.h}>Appearance</div>
        <div style={{ ...S.row, flexWrap: 'wrap' }}>
          {APPEARANCES.map((a) => (
            <button key={a} style={S.btn(appearance === a)} onClick={() => setAppearance(a)}>
              {a}
            </button>
          ))}
        </div>

        <div style={S.h}>View</div>
        <div style={S.row}>
          <button style={S.btn(view === 'front')} onClick={() => setView('front')}>
            Front
          </button>
          <button style={S.btn(view === 'orbit')} onClick={() => setView('orbit')}>
            Orbit
          </button>
          <button style={S.btn(grid)} onClick={() => setGrid((g) => !g)}>
            Grid
          </button>
        </div>
        <Slider label="Explode" value={explode} min={0} max={1} onChange={setExplode} />

        {project && (
          <>
            <div style={S.h}>Lighting</div>
            <div style={{ display: 'flex', justifyContent: 'center', margin: '4px 0 8px' }}>
              <LightDial
                angle={project.lighting.angle}
                onChange={(angle) => update((p) => ({ ...p, lighting: { ...p.lighting, angle } }))}
              />
            </div>
            <Select
              label="Preset"
              value={project.lighting.preset}
              options={lightingIds}
              onChange={(preset) => update((p) => ({ ...p, lighting: { ...p.lighting, preset } }))}
            />
            <Slider
              label="Elevation"
              value={project.lighting.elevation}
              min={0}
              max={89}
              step={1}
              onChange={(elevation) => update((p) => ({ ...p, lighting: { ...p.lighting, elevation } }))}
            />
            <Slider
              label="Intensity"
              value={project.lighting.intensity}
              min={0}
              max={2.5}
              onChange={(intensity) => update((p) => ({ ...p, lighting: { ...p.lighting, intensity } }))}
            />
            <Slider
              label="Softness"
              value={project.lighting.shadowSoftness}
              min={0}
              max={1}
              onChange={(shadowSoftness) => update((p) => ({ ...p, lighting: { ...p.lighting, shadowSoftness } }))}
            />

            <div style={S.h}>Plate & render</div>
            <Select
              label="Shape"
              value={project.canvas.shape}
              options={['squircle', 'circle', 'rounded', 'square', 'none']}
              onChange={(shape) =>
                update((p) => ({ ...p, canvas: { ...p.canvas, shape: shape as Project['canvas']['shape'] } }))
              }
            />
            <Select
              label="Material"
              value={project.canvas.plate.material.preset}
              options={materialIds}
              onChange={(preset) =>
                update((p) => ({
                  ...p,
                  canvas: { ...p.canvas, plate: { ...p.canvas.plate, material: { preset, params: {} } } },
                }))
              }
            />
            <Select
              label="Fill"
              value={project.canvas.plate.fill.type}
              options={['linear', 'solid', 'system-light', 'system-dark', 'radial']}
              onChange={(t) =>
                update((p) => ({
                  ...p,
                  canvas: {
                    ...p.canvas,
                    plate: {
                      ...p.canvas.plate,
                      fill:
                        t === 'solid'
                          ? { type: 'solid', color: '#f4f4f7', opacity: 1 }
                          : t === 'radial'
                            ? {
                                type: 'radial',
                                center: [0, 0.3],
                                radius: 1.4,
                                stops: [
                                  { offset: 0, color: '#ff8a5b', opacity: 1 },
                                  { offset: 1, color: '#7a1f5c', opacity: 1 },
                                ],
                              }
                            : t === 'linear'
                              ? {
                                  type: 'linear',
                                  start: [-0.4, 1],
                                  end: [0.4, -1],
                                  stops: [
                                    { offset: 0, color: '#4f7cff', opacity: 1 },
                                    { offset: 0.55, color: '#5b3fd6', opacity: 1 },
                                    { offset: 1, color: '#2a1470', opacity: 1 },
                                  ],
                                }
                              : { type: t as 'system-light' | 'system-dark' },
                    },
                  },
                }))
              }
            />
            <Select
              label="Color mode"
              value={project.render.colorMode}
              options={['neutral', 'standard', 'agx', 'agx-punchy']}
              onChange={(c) =>
                update((p) => ({ ...p, render: { ...p.render, colorMode: c as Project['render']['colorMode'] } }))
              }
            />
            <Select
              label="Backdrop"
              value={project.render.backdrop}
              options={['transparent', 'color', 'wallpaper']}
              onChange={(b) =>
                update((p) => ({ ...p, render: { ...p.render, backdrop: b as Project['render']['backdrop'] } }))
              }
            />

            <Select
              label="All layers"
              value=""
              options={['', ...materialIds]}
              onChange={(preset) =>
                preset &&
                update((p) => ({ ...p, layers: p.layers.map((l) => ({ ...l, material: { preset, params: {} } })) }))
              }
            />
            <div style={S.h}>Layers</div>
            {[...project.layers].reverse().map((l) => (
              <div
                key={l.id}
                style={{
                  ...S.row,
                  cursor: 'pointer',
                  padding: '3px 6px',
                  borderRadius: 6,
                  background: l.id === selected ? 'rgba(143,125,255,0.18)' : 'transparent',
                }}
                onClick={() => setSelected(l.id)}
              >
                <input
                  type="checkbox"
                  checked={l.visible}
                  onClick={(e) => e.stopPropagation()}
                  onChange={(e) => updateLayer(l.id, (x) => ({ ...x, visible: e.target.checked }))}
                />
                <span style={{ flex: 1 }}>{l.name}</span>
                <span style={S.mono}>{l.material.preset}</span>
              </div>
            ))}

            {layer && (
              <>
                <div style={S.h}>Layer · {layer.name}</div>
                <Select
                  label="Material"
                  value={layer.material.preset}
                  options={materialIds}
                  onChange={(preset) => updateLayer(layer.id, (l) => ({ ...l, material: { preset, params: {} } }))}
                />
                <Select
                  label="Mode"
                  value={layer.mode}
                  options={['combined', 'individual']}
                  onChange={(mode) => updateLayer(layer.id, (l) => ({ ...l, mode: mode as Layer['mode'] }))}
                />
                <Slider
                  label="Thickness"
                  value={layer.depth.thickness}
                  min={0.01}
                  max={0.3}
                  onChange={(thickness) => updateLayer(layer.id, (l) => ({ ...l, depth: { ...l.depth, thickness } }))}
                />
                <Slider
                  label="Bevel"
                  value={layer.depth.bevel}
                  min={0}
                  max={0.12}
                  step={0.005}
                  onChange={(bevel) => updateLayer(layer.id, (l) => ({ ...l, depth: { ...l.depth, bevel } }))}
                />
                <Slider
                  label="Z"
                  value={layer.depth.z}
                  min={0}
                  max={0.8}
                  onChange={(z) => updateLayer(layer.id, (l) => ({ ...l, depth: { ...l.depth, z } }))}
                />
                <Slider
                  label="Inflate"
                  value={layer.depth.inflate}
                  min={0}
                  max={1}
                  onChange={(inflate) => updateLayer(layer.id, (l) => ({ ...l, depth: { ...l.depth, inflate } }))}
                />
                <Slider
                  label="Opacity"
                  value={layer.opacity}
                  min={0}
                  max={1}
                  onChange={(opacity) => updateLayer(layer.id, (l) => ({ ...l, opacity }))}
                />
                <Slider
                  label="Shadow"
                  value={layer.shadow.opacity}
                  min={0}
                  max={1}
                  onChange={(opacity) => updateLayer(layer.id, (l) => ({ ...l, shadow: { ...l.shadow, opacity } }))}
                />
                <Row label="Flags">
                  <button
                    style={S.btn(layer.locked)}
                    onClick={() => updateLayer(layer.id, (l) => ({ ...l, locked: !l.locked }))}
                  >
                    locked
                  </button>
                  <button
                    style={S.btn(!layer.glass)}
                    onClick={() => updateLayer(layer.id, (l) => ({ ...l, glass: !l.glass }))}
                  >
                    flat inlay
                  </button>
                </Row>
                <div style={S.mono}>
                  t = ({layer.transform.x.toFixed(3)}, {layer.transform.y.toFixed(3)}) × {layer.transform.scale}
                </div>
              </>
            )}
          </>
        )}

        <div style={S.h}>Diagnostics</div>
        <div style={S.row}>
          <button style={S.btn(false)} onClick={() => void measureFps()}>
            FPS
          </button>
          <button style={S.btn(false)} onClick={memory}>
            Memory
          </button>
          <button style={S.btn(false)} onClick={() => void leakTest()}>
            Leak test
          </button>
        </div>
        <div style={S.mono} data-testid="diag">
          {diag}
        </div>
      </aside>
      <main style={{ flex: 1, minWidth: 0, position: 'relative' }}>
        {project ? (
          <Viewport
            project={project}
            geometry={geometry}
            presets={presets as Presets}
            appearance={appearance}
            selectedLayerId={selected}
            onSelectLayer={setSelected}
            onLayerTransform={onLayerTransform}
            explode={explode}
            view={view}
            showGrid={grid}
          />
        ) : (
          <div style={{ display: 'grid', placeItems: 'center', height: '100%', color: '#85858f' }}>{status}</div>
        )}
      </main>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Harness />
  </StrictMode>,
)
