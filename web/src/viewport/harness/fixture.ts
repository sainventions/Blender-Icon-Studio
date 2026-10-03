// Synthetic Project + GeometryBundle for the viewport harness: Bézier shapes with holes (ring, rounded star with a
// centre hole, a sharp-cornered bolt, three "individual" sparks) and generated layer textures — no backend needed.
import type {
  GeometryBundle,
  Layer,
  LayerGeometry,
  Paint,
  Project,
  Spline,
  SplinePoint,
  SvgElement,
  Vec2,
} from '../../types'

// ---------------------------------------------------------------------------------------------- spline builder
const lerp = (a: Vec2, b: Vec2, t: number): Vec2 => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]

class PathBuilder {
  private knots: SplinePoint[] = []

  moveTo(p: Vec2): this {
    this.knots = [{ co: p, hl: p, hr: p }]
    return this
  }

  lineTo(p: Vec2): this {
    const last = this.knots[this.knots.length - 1]
    last.hr = lerp(last.co, p, 1 / 3)
    this.knots.push({ co: p, hl: lerp(p, last.co, 1 / 3), hr: p })
    return this
  }

  cubicTo(c1: Vec2, c2: Vec2, p: Vec2): this {
    const last = this.knots[this.knots.length - 1]
    last.hr = c1
    this.knots.push({ co: p, hl: c2, hr: p })
    return this
  }

  close(hole = false, parent = -1): Spline {
    const k = this.knots
    const first = k[0]
    const last = k[k.length - 1]
    if (Math.hypot(first.co[0] - last.co[0], first.co[1] - last.co[1]) < 1e-9) {
      first.hl = last.hl
      k.pop()
    } else {
      last.hr = lerp(last.co, first.co, 1 / 3)
      first.hl = lerp(first.co, last.co, 1 / 3)
    }
    return { closed: true, hole, parent, depth: hole ? 1 : 0, points: k }
  }
}

function circle(cx: number, cy: number, r: number, hole = false, parent = -1): Spline {
  const k = 0.5523 * r
  const b = new PathBuilder().moveTo([cx + r, cy])
  b.cubicTo([cx + r, cy + k], [cx + k, cy + r], [cx, cy + r])
  b.cubicTo([cx - k, cy + r], [cx - r, cy + k], [cx - r, cy])
  b.cubicTo([cx - r, cy - k], [cx - k, cy - r], [cx, cy - r])
  b.cubicTo([cx + k, cy - r], [cx + r, cy - k], [cx + r, cy])
  return b.close(hole, parent)
}

function roundedStar(cx: number, cy: number, ro: number, ri: number, n: number, round: number): Spline {
  const verts: Vec2[] = []
  for (let i = 0; i < n * 2; i++) {
    const a = Math.PI / 2 + (i * Math.PI) / n
    const r = i % 2 === 0 ? ro : ri
    verts.push([cx + Math.cos(a) * r, cy + Math.sin(a) * r])
  }
  const m = verts.length
  const corner = (i: number) => {
    const v = verts[i]
    const prev = verts[(i - 1 + m) % m]
    const next = verts[(i + 1) % m]
    const t = i % 2 === 0 ? round : round * 0.6
    return { v, p1: lerp(v, prev, t), p2: lerp(v, next, t) }
  }
  const c0 = corner(0)
  const b = new PathBuilder().moveTo(c0.p1)
  for (let i = 0; i < m; i++) {
    const c = corner(i)
    if (i > 0) b.lineTo(c.p1)
    b.cubicTo(lerp(c.p1, c.v, 0.6), lerp(c.p2, c.v, 0.6), c.p2)
  }
  b.lineTo(c0.p1)
  return b.close()
}

function polygon(pts: Vec2[]): Spline {
  const b = new PathBuilder().moveTo(pts[0])
  for (let i = 1; i < pts.length; i++) b.lineTo(pts[i])
  return b.close()
}

// ---------------------------------------------------------------------------------------------- textures
type Painter = (ctx: CanvasRenderingContext2D, size: number) => void

async function textureUrl(paint: Painter, size = 1024): Promise<string> {
  const canvas = document.createElement('canvas')
  canvas.width = size
  canvas.height = size
  const ctx = canvas.getContext('2d')!
  paint(ctx, size)
  const blob = await new Promise<Blob | null>((res) => canvas.toBlob(res, 'image/png'))
  return blob ? URL.createObjectURL(blob) : canvas.toDataURL('image/png')
}

const px = (x: number, s: number) => ((x + 1) / 2) * s
const py = (y: number, s: number) => ((1 - y) / 2) * s

function linear(top: string, bottom: string): Painter {
  return (ctx, s) => {
    const g = ctx.createLinearGradient(0, 0, 0, s)
    g.addColorStop(0, top)
    g.addColorStop(1, bottom)
    ctx.fillStyle = g
    ctx.fillRect(0, 0, s, s)
  }
}

function radial(cx: number, cy: number, r: number, inner: string, outer: string): Painter {
  return (ctx, s) => {
    const g = ctx.createRadialGradient(px(cx, s), py(cy, s), 0, px(cx, s), py(cy, s), (r / 2) * s)
    g.addColorStop(0, inner)
    g.addColorStop(1, outer)
    ctx.fillStyle = g
    ctx.fillRect(0, 0, s, s)
  }
}

function dots(items: { x: number; y: number; r: number; color: string }[]): Painter {
  return (ctx, s) => {
    for (const d of items) {
      ctx.fillStyle = d.color
      ctx.beginPath()
      ctx.arc(px(d.x, s), py(d.y, s), ((d.r * 1.8) / 2) * s, 0, Math.PI * 2)
      ctx.fill()
    }
  }
}

// ---------------------------------------------------------------------------------------------- assembly
interface LayerDef {
  id: string
  name: string
  splines: Spline[] // silhouette
  regions?: { id: string; splines: Spline[]; color: string }[]
  color: string
  painter: Painter
  safeRadius: number
  bbox: [number, number, number, number]
  material: Layer['material']
  mode?: Layer['mode']
  shadow?: number
}

function solid(color: string): Paint {
  return { type: 'solid', color, opacity: 1 }
}

export interface Fixture {
  project: Project
  geometry: GeometryBundle
}

export type FixtureVariant = 'A' | 'B'

export async function buildFixture(variant: FixtureVariant = 'A'): Promise<Fixture> {
  const B = variant === 'B'
  const sparks = [
    { id: 'e3', x: -0.56, y: 0.56, r: 0.07, color: B ? '#a3e635' : '#ff4fd8' },
    { id: 'e4', x: -0.36, y: 0.72, r: 0.05, color: B ? '#fb923c' : '#22d3ee' },
    { id: 'e5', x: -0.72, y: 0.34, r: 0.04, color: B ? '#f472b6' : '#facc15' },
  ]
  const bolt: Vec2[] = [
    [0.36, 0.2],
    [0.05, -0.2],
    [0.22, -0.2],
    [0.1, -0.58],
    [0.5, -0.1],
    [0.32, -0.1],
    [0.47, 0.2],
  ]
  const defs: LayerDef[] = [
    {
      id: 'L0',
      name: 'Halo',
      splines: [circle(0, 0, 0.66), circle(0, 0, 0.47, true, 0)],
      color: B ? '#34d399' : '#38bdf8',
      painter: B ? linear('#a7f3d0', '#059669') : linear('#bae6fd', '#2563eb'),
      safeRadius: 0.09,
      bbox: [-0.66, -0.66, 0.66, 0.66],
      material: B ? { preset: 'frosted_glass', params: {} } : { preset: 'liquid_glass', params: {} },
    },
    {
      id: 'L1',
      name: 'Star',
      splines: [roundedStar(0, 0.02, 0.5, 0.24, 5, 0.22), circle(0, 0.02, 0.075, true, 0)],
      color: '#fbbf24',
      painter: B ? radial(0, 0.02, 0.5, '#f8fafc', '#94a3b8') : radial(0, 0.1, 0.55, '#fff7c2', '#f59e0b'),
      safeRadius: 0.06,
      bbox: [-0.48, -0.39, 0.48, 0.52],
      material: B ? { preset: 'chrome', params: { tint: 0.15 } } : { preset: 'glossy_plastic', params: {} },
    },
    {
      id: 'L2',
      name: 'Bolt',
      splines: [polygon(bolt)],
      color: '#e879f9',
      painter: B ? linear('#fde68a', '#ea580c') : linear('#f5d0fe', '#a21caf'),
      safeRadius: 0.05,
      bbox: [0.05, -0.58, 0.5, 0.2],
      material: B ? { preset: 'candy', params: {} } : { preset: 'liquid_glass', params: { tint: 0.6 } },
    },
    {
      id: 'L3',
      name: 'Sparks',
      splines: sparks.map((s) => circle(s.x, s.y, s.r)),
      regions: sparks.map((s) => ({ id: s.id, splines: [circle(s.x, s.y, s.r)], color: s.color })),
      color: '#ff4fd8',
      painter: dots(sparks),
      safeRadius: 0.035,
      bbox: [-0.76, 0.3, -0.31, 0.77],
      material: B ? { preset: 'iridescent', params: {} } : { preset: 'neon', params: { strength: 3 } },
      mode: 'individual',
      shadow: 0.2,
    },
  ]

  const urls = await Promise.all(defs.map((d) => textureUrl(d.painter)))
  // Variant B adds a raster card (an <image> element without vector regions).
  const cardUrl = B
    ? await textureUrl((ctx, size) => {
        const g = ctx.createLinearGradient(0, 0, size, size)
        g.addColorStop(0, '#ff6a88')
        g.addColorStop(1, '#ffcc70')
        ctx.fillStyle = g
        ctx.beginPath()
        ctx.roundRect(size * 0.04, size * 0.04, size * 0.92, size * 0.92, size * 0.22)
        ctx.fill()
        ctx.fillStyle = '#ffffff'
        ctx.font = `800 ${size * 0.42}px Inter, Segoe UI, sans-serif`
        ctx.textAlign = 'center'
        ctx.textBaseline = 'middle'
        ctx.fillText('3D', size / 2, size * 0.53)
      }, 256)
    : null
  const elements: SvgElement[] = []
  const layers: Layer[] = []
  const geometry: Record<string, LayerGeometry> = {}

  defs.forEach((d, i) => {
    const regions = (d.regions ?? [{ id: `e${i}`, splines: d.splines, color: d.color }]).map((r) => ({
      elementId: r.id,
      paint: solid(r.color),
      opacity: 1,
      zSub: 0,
      splines: r.splines,
    }))
    for (const r of regions) {
      elements.push({
        id: r.elementId,
        name: `${d.name} ${r.elementId}`,
        kind: 'path',
        paint: r.paint,
        opacity: 1,
        bbox: d.bbox,
        area: 0.05,
        groupPath: [],
        wasStroke: false,
        role: 'fill',
      })
    }
    layers.push({
      id: d.id,
      name: d.name,
      elementIds: regions.map((r) => r.elementId),
      visible: true,
      locked: false,
      mode: d.mode ?? 'combined',
      fill: { type: 'auto' },
      opacity: 1,
      blendMode: 'normal',
      glass: true,
      transform: { x: 0, y: 0, scale: 1 },
      depth: { z: i * 0.13, thickness: 0.1, bevel: 0.045, bevelSegments: 6, inflate: 0 },
      material: d.material,
      shadow: { kind: 'neutral', opacity: d.shadow ?? 0.5 },
    })
    geometry[d.id] = {
      layerId: d.id,
      hash: `fixture-${variant}-${d.id}`,
      silhouette: d.splines,
      regions,
      safeRadius: d.safeRadius,
      bbox: d.bbox,
      texture: urls[i],
      texturePath: '',
      svg: '',
      images: [],
    }
  })

  if (cardUrl) {
    const bbox: [number, number, number, number] = [0.46, 0.46, 0.86, 0.86]
    elements.push({
      id: 'img0',
      name: 'Badge image',
      kind: 'image',
      paint: solid('#ff6a88'),
      opacity: 1,
      bbox,
      area: 0.04,
      groupPath: [],
      wasStroke: false,
      role: 'image',
    })
    layers.push({
      id: 'L4',
      name: 'Badge',
      elementIds: ['img0'],
      visible: true,
      locked: false,
      mode: 'combined',
      fill: { type: 'auto' },
      opacity: 1,
      blendMode: 'normal',
      glass: true,
      transform: { x: 0, y: 0, scale: 1 },
      depth: { z: 0.52, thickness: 0.1, bevel: 0.045, bevelSegments: 6, inflate: 0 },
      material: { preset: 'glossy_plastic', params: {} },
      shadow: { kind: 'neutral', opacity: 0.4 },
    })
    geometry.L4 = {
      layerId: 'L4',
      hash: `fixture-${variant}-L4`,
      silhouette: [],
      regions: [],
      safeRadius: 0.05,
      bbox,
      texture: '',
      texturePath: '',
      svg: '',
      images: [{ elementId: 'img0', path: '', url: cardUrl, bbox, opacity: 1 }],
    }
  }

  const now = new Date().toISOString()
  const project: Project = {
    version: 1,
    id: `fixture-${variant}`,
    name: variant === 'B' ? 'Synthetic B (chrome)' : 'Synthetic A (glass)',
    createdAt: now,
    updatedAt: now,
    source: { filename: 'synthetic.svg', viewBox: [0, 0, 500, 500], warnings: [], plateDetected: true },
    strategy: 'smart',
    elements,
    layers,
    canvas: {
      platform: 'ios',
      shape: 'squircle',
      cornerRadius: 0.225,
      plate: {
        visible: true,
        fill: B
          ? {
              type: 'linear',
              start: [0, 1],
              end: [0, -1],
              stops: [
                { offset: 0, color: '#1f2937', opacity: 1 },
                { offset: 1, color: '#030712', opacity: 1 },
              ],
            }
          : {
              type: 'linear',
              start: [-0.4, 1],
              end: [0.4, -1],
              stops: [
                { offset: 0, color: '#4f7cff', opacity: 1 },
                { offset: 0.55, color: '#5b3fd6', opacity: 1 },
                { offset: 1, color: '#2a1470', opacity: 1 },
              ],
            },
        material: { preset: 'satin', params: {} },
        thickness: 0.16,
        bevel: 0.04,
      },
      art: { scale: 1, x: 0, y: 0 },
    },
    lighting: {
      preset: 'studio',
      angle: -45,
      elevation: 50,
      intensity: 1,
      rim: 1,
      fill: 1,
      environment: 1,
      shadowSoftness: 0.5,
    },
    camera: { view: 'front', tiltX: 0, tiltY: 0, fov: 30, zoom: 1, explode: 1 },
    appearance: 'light',
    appearances: {
      dark: { plateFill: { type: 'system-dark' }, layers: {} },
      mono: { plateFill: null, layers: {} },
      tint: { color: '#3b82f6', strength: 0.8 },
    },
    render: {
      quality: 'draft',
      size: null,
      colorMode: 'neutral',
      backdrop: 'transparent',
      backdropColor: '#1c1c22',
      autoPreview: true,
    },
  }

  return {
    project,
    geometry: {
      projectId: project.id,
      hash: `fixture-${variant}`,
      viewBox: [0, 0, 500, 500],
      plate: { bbox: [-1, -1, 1, 1], shape: 'squircle' },
      layers: geometry,
    },
  }
}

/** Revoke the blob URLs of a fixture's generated textures. */
export function disposeFixture(f: Fixture | null): void {
  if (!f) return
  for (const lg of Object.values(f.geometry.layers)) if (lg.texture.startsWith('blob:')) URL.revokeObjectURL(lg.texture)
}
