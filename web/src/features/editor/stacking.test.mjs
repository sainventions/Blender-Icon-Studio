// Overlap-aware real-height stacking of the editor (PLAN §11 round 8) against the SERVER: features/editor/stackModel.ts
// + overlap.ts must give the same body heights, XY overlap lists, re-stacks, stack recognition and collisions as
// server/bis/stacking.py on the user's 68-icon corpus.
//
// fixtures/stack-corpus.json.gz is exported by fixtures/export_stack_fixture.py (server venv): every corpus icon imported
// by the round-8 SVG pipeline (+ the server's own merges of its bottom two / all layers: in-layer stacking), the
// geometry bundle the web sees, and bis.stacking's results computed from that bundle (shapes_from_bundle).
// Run (from web/): node --test src/features/editor/stacking.test.mjs   (Node ≥ 23.6: built-in TS type stripping)
import '../../viewport/tests/helpers.mjs'
import { readFileSync } from 'node:fs'
import { gunzipSync } from 'node:zlib'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const SM = await import('./stackModel.ts')
const OV = await import('./overlap.ts')
const D = await import('./inspector/depth.ts')

const fx = JSON.parse(gunzipSync(readFileSync(new URL('./fixtures/stack-corpus.json.gz', import.meta.url))).toString('utf8'))
const presets = { geometry: fx.rules }
const spl = (s) => ({ closed: true, hole: s.h, parent: -1, depth: 0, points: s.p.map((q) => ({ co: [q[0], q[1]], hl: [q[2], q[3]], hr: [q[4], q[5]] })) })
const project = (ic, layers = ic.layers) => ({ canvas: { art: ic.art }, layers: layers.map((l) => ({ ...l, locked: false })) })
const geoCache = new Map()
function geometry(name) {
  if (!geoCache.has(name)) {
    const layers = {}
    for (const [id, g] of Object.entries(fx.icons[name].geometry))
      layers[id] = {
        layerId: id,
        hash: g.hash,
        maxRadius: g.maxRadius,
        silhouette: g.silhouette.map(spl),
        regions: g.regions.map((r) => ({ ...r, splines: r.splines.map(spl) })),
        images: g.images,
      }
    geoCache.set(name, { layers })
  }
  return geoCache.get(name)
}
const zOf = (p) => p.layers.map((l) => l.depth.z)
const near = (a, b, eps, msg) => assert.ok(Math.abs(a - b) <= eps, `${msg}: ${a} ≉ ${b} (±${eps})`)
const ICONS = Object.keys(fx.icons)

// ------------------------------------------------------------------------------------------------ footprints
const circle = (r, cx = 0, cy = 0, hole = false) => {
  const k = 0.5522847498 * r
  return {
    closed: true,
    hole,
    parent: -1,
    depth: 0,
    points: [
      { co: [cx + r, cy], hl: [cx + r, cy - k], hr: [cx + r, cy + k] },
      { co: [cx, cy + r], hl: [cx + k, cy + r], hr: [cx - k, cy + r] },
      { co: [cx - r, cy], hl: [cx - r, cy + k], hr: [cx - r, cy - k] },
      { co: [cx, cy - r], hl: [cx - k, cy - r], hr: [cx + k, cy - r] },
    ],
  }
}
const lgOf = (id, silhouette, images = []) => ({ layerId: id, hash: `h-${id}-${Math.random()}`, silhouette, regions: [], images, maxRadius: 0.1 })

test('footprints: dwithin = shapely (gap vs clearance, containment, holes, raster cards, the canvas placement)', () => {
  const at = (lg, a = 1, ox = 0, oy = 0) => OV.place(OV.layerFootprint(lg), a, ox, oy)
  const left = at(lgOf('a', [circle(0.2, -0.3)]))
  // two discs 0.05 apart: apart at the 0.03 clearance, overlapping at 0.06
  const right = at(lgOf('b', [circle(0.2, 0.15)]))
  assert.equal(OV.dwithin(left, right, 0.03), false)
  assert.equal(OV.dwithin(left, right, 0.06), true)
  // a dot well inside the disc: no outline within reach, but contained → overlaps
  const dot = at(lgOf('c', [circle(0.03, -0.3)]))
  assert.equal(OV.dwithin(dot, left, 0.03), true)
  assert.equal(OV.dwithin(left, dot, 0), true)
  // a dot in the HOLE of a ring (even-odd): apart; a dot on the ring's material: overlapping
  const ring = at(lgOf('d', [circle(0.4), circle(0.25, 0, 0, true)]))
  assert.equal(OV.dwithin(at(lgOf('e', [circle(0.05)])), ring, 0.03), false)
  assert.equal(OV.dwithin(at(lgOf('f', [circle(0.04, 0.32)])), ring, 0.03), true)
  // a raster image without a region is its bbox card
  const card = at(lgOf('g', [], [{ elementId: 'img', bbox: [0.5, -0.1, 0.7, 0.1] }]))
  assert.equal(OV.dwithin(card, at(lgOf('h', [circle(0.05, 0.75)])), 0.03), true)
  assert.equal(OV.dwithin(card, left, 0.03), false)
  // placement: (p · art.scale + art.xy) · layer.scale + layer.xy (worker / server footprints)
  OV.placement({ scale: 1.2, x: 0.1, y: -0.2 }, { transform: { x: 0.05, y: 0.3, scale: 0.5 } }).forEach((v, i) => near(v, [0.6, 0.1, 0.2][i], 1e-12, 'placement'))
  const moved = at(lgOf('i', [circle(0.2, 0.15)]), 1, 0.2, 0)
  assert.equal(OV.dwithin(left, moved, 0.03), false, 'moved away')
  // unknown footprints overlap everything: the sequential stack
  assert.deepEqual(OV.overlapLists([{ key: 'x', fp: null, a: 1, ox: 0, oy: 0 }, { key: 'y', fp: null, a: 1, ox: 0, oy: 0 }], 0.03), [[], [0]])
  assert.deepEqual(D.allLower(3), [[], [0], [0, 1]])
})

// ------------------------------------------------------------------------------------------------ server parity
test(`corpus parity with server bis.stacking: H, XY overlaps and re-stacks (${ICONS.length} imports + merges)`, () => {
  assert.equal(fx.rules.stackGap, presets.geometry.stackGap)
  assert.ok(ICONS.filter((n) => !n.includes('+')).length === 67, 'every corpus icon with layers (Template has none)')
  let inLayer = 0
  let worstIn = 0
  for (const name of ICONS) {
    const ic = fx.icons[name]
    const g = geometry(name)
    const p = project(ic)
    const H = SM.bodyHeights(p, g)
    // H = max(rule, in-layer stacked height): the rule exactly; the in-layer stack within the piece inradius estimate
    // (the web and the worker: heightfield inradius; the server: GEOS maximum inscribed circle — up to 4e-4 apart)
    const checkH = (layers, want, tag) =>
      layers.forEach((l, i) => {
        const got = H(l)
        const rule = D.ruleHeight(l, ic.geometry[l.id].maxRadius, ic.art.scale * l.transform.scale)
        if (want[i] > rule + 1e-9 || got > rule + 1e-9) {
          inLayer++
          worstIn = Math.max(worstIn, Math.abs(got - want[i]))
          near(got, want[i], 2e-3, `${name} ${tag} ${l.id} in-layer H`)
        } else near(got, want[i], 1e-9, `${name} ${tag} ${l.id} H`)
      })
    checkH(p.layers, ic.H, 'import')
    assert.deepEqual(SM.lowerLists(p, g, fx.rules.stackGap), ic.lower, `${name}: overlap lists`)
    // the import stack IS the rule stack: Re-stack is a no-op
    const re = SM.restackProject(p, g, presets)
    assert.equal(re, p, `${name}: Re-stack of the import changes nothing`)
    zOf(re).forEach((z, i) => near(z, ic.z.restack[i], 1e-5, `${name} restack ${i}`))
    for (const [key, v] of Object.entries(ic.variants)) {
      if (!v.layers) {
        const z = D.restack(p.layers, H, D.stackRules(presets), 0.05, ic.lower).map((l) => l.depth.z)
        z.forEach((zz, i) => near(zz, v.z[i], 1e-5, `${name} ${key} ${i}`))
        continue
      }
      const pv = project(ic, v.layers)
      const Hv = SM.bodyHeights(pv, g)
      pv.layers.forEach((l, i) => {
        const rule = D.ruleHeight(l, ic.geometry[l.id].maxRadius, ic.art.scale * l.transform.scale)
        const got = Hv(l)
        if (v.H[i] > rule + 1e-9 || got > rule + 1e-9) {
          inLayer++
          worstIn = Math.max(worstIn, Math.abs(got - v.H[i]))
          near(got, v.H[i], 2e-3, `${name} ${key} ${l.id} in-layer H`)
        } else near(got, v.H[i], 1e-9, `${name} ${key} ${l.id} H`)
      })
      zOf(SM.restackProject(pv, g, presets)).forEach((z, i) => near(z, v.z[i], 1e-5, `${name} ${key} ${i}`))
    }
  }
  assert.ok(inLayer >= 3, `in-layer stacks covered (${inLayer})`)
  assert.ok(worstIn < 2e-3, `in-layer H within ${worstIn}`)
})

test('stack recognition = server stack_gap (overlap-aware, sequential round-7, legacy 0.13, hand-placed)', () => {
  const same = (a, b) => (a === null || b === null ? a === b : Math.abs(a - b) <= 1e-5)
  for (const name of ICONS) {
    const ic = fx.icons[name]
    const g = geometry(name)
    const p = project(ic)
    const rec = ic.recognise
    assert.ok(same(SM.stackStatus(p, g, presets).gap, rec.import), `${name} import: ${SM.stackStatus(p, g, presets).gap} vs ${rec.import}`)
    for (const k of ['sequential', 'legacy', 'hand', 'flat']) {
      const pk = { ...p, layers: p.layers.map((l, i) => ({ ...l, depth: { ...l.depth, z: rec[k].z[i] } })) }
      const got = SM.stackStatus(pk, g, presets).gap
      assert.ok(same(got, rec[k].gap), `${name} ${k}: ${got} vs ${rec[k].gap}`)
    }
  }
})

test('collisions of a hand-placed stack = server interpenetrations (touching footprints, overlapping z ranges)', () => {
  let n = 0
  for (const name of ICONS) {
    const ic = fx.icons[name]
    const g = geometry(name)
    const p = project(ic)
    for (const k of ['flat', 'n5']) {
      const pk =
        k === 'flat'
          ? { ...p, layers: p.layers.map((l) => ({ ...l, depth: { ...l.depth, z: 0 } })) }
          : project(ic, ic.recognise.n5.layers)
      const got = SM.stackStatus(pk, g, presets).collisions
      // footprints that share an edge are ~1e-6 apart on the exact outlines (the server tests 1e-9 on outlines
      // simplified by 0.002): pairs the server measures closer than the web's TOUCH_CLEARANCE count as touching
      const want = ic.recognise[k].interpenetrations.map(([j, i]) => `${j},${i}`)
      for (const [key, dist] of Object.entries(ic.dist)) {
        const [j, i] = key.split(',').map(Number)
        const zr = (l) => [l.depth.z, l.depth.z + SM.bodyHeights(pk, g)(l)]
        const [a0, a1] = zr(pk.layers[j])
        const [b0, b1] = zr(pk.layers[i])
        if (dist > 1e-9 && dist <= SM.TOUCH_CLEARANCE && Math.min(a1, b1) - Math.max(a0, b0) > D.Z_TOL && !want.includes(key)) want.push(key)
      }
      assert.deepEqual(got.map(([j, i]) => `${j},${i}`).sort(), want.sort(), `${name} ${k}`)
      for (const [j, i, d] of got) {
        const s = ic.recognise[k].interpenetrations.find((x) => x[0] === j && x[1] === i)
        if (s) near(d, s[2], 2e-3, `${name} ${k} depth`)
      }
      n += got.length
    }
  }
  assert.ok(n > 40, `collisions covered (${n})`)
})

// ------------------------------------------------------------------------------------------------ N5: edits
test('N5: thickness / inflate / move / mode edits on a detected stack re-stack it like the server (bodies stay apart)', () => {
  for (const name of ICONS) {
    const ic = fx.icons[name]
    const g = geometry(name)
    const p = project(ic)
    for (const key of ['thick0', 'inflate1', 'move0', 'combined']) {
      const v = ic.variants[key]
      const after = project(ic, v.layers.map((l, i) => ({ ...l, depth: { ...l.depth, z: ic.layers[i].depth.z } })))
      const kept = SM.keepProjectStack(p, after, g, presets)
      zOf(kept).forEach((z, i) => near(z, v.z[i], 1e-5, `${name} ${key} layer ${i}`))
      assert.deepEqual(SM.stackStatus(kept, g, presets).collisions, [], `${name} ${key}: no bodies cutting into each other`)
    }
  }
})

test('N5: a hand-placed stack stays as placed, reports its collisions, and Re-stack clears them', () => {
  let hinted = 0
  for (const name of ICONS) {
    const ic = fx.icons[name]
    const g = geometry(name)
    const hand = project(ic, ic.layers.map((l, i) => ({ ...l, depth: { ...l.depth, z: ic.recognise.hand.z[i] } })))
    const n5 = project(ic, ic.recognise.n5.layers)
    const st0 = SM.stackStatus(hand, g, presets)
    if (st0.gap !== null) continue // the +0.1 lift of a top layer that overlaps nothing still reads as a rule stack
    const kept = SM.keepProjectStack(hand, n5, g, presets)
    assert.equal(kept, n5, `${name}: a custom stack is left alone`)
    const st = SM.stackStatus(n5, g, presets)
    if (ic.recognise.n5.interpenetrations.length) {
      assert.ok(st.collisions.length, `${name}: the thicker bottom layer cuts into the layers above → hint`)
      hinted++
    }
    const fixed = SM.restackProject(n5, g, presets)
    const sf = SM.stackStatus(fixed, g, presets)
    assert.deepEqual(sf.collisions, [], `${name}: Re-stack clears the collisions`)
    assert.equal(sf.gap, fx.rules.stackGap, `${name}: …and makes it a rule stack again`)
  }
  assert.ok(hinted >= 10, `hand-placed collisions covered (${hinted})`)
})

test('editing depth re-uses the cached footprint overlaps (slider drags stay cheap)', () => {
  const name = 'Ti84'
  const ic = fx.icons[name]
  const g = geometry(name)
  let p = project(ic)
  SM.keepProjectStack(p, p, g, presets)
  const t0 = performance.now()
  for (let s = 0; s < 40; s++) {
    const after = { ...p, layers: p.layers.map((l, i) => (i ? l : { ...l, depth: { ...l.depth, thickness: 0.16 + s * 0.005 } })) }
    p = SM.keepProjectStack(p, after, g, presets)
  }
  const ms = (performance.now() - t0) / 40
  assert.ok(ms < 8, `keepProjectStack ${ms.toFixed(2)} ms per commit`)
  assert.equal(SM.stackStatus(p, g, presets).gap, fx.rules.stackGap)
})

test('while the geometry bundle loads: no false collision warning, no re-stack on guessed heights', () => {
  // an unknown footprint overlaps every layer and its height is the bare thickness: side-by-side imports on the base
  // used to report "Collide · Re-stack" until the bundle arrived, and a Re-stack then would drop the domes' heights
  let sideBySide = 0
  for (const name of ICONS.filter((n) => !n.includes('+'))) {
    const ic = fx.icons[name]
    const p = project(ic)
    const g = geometry(name)
    if (p.layers.filter((l) => Math.abs(l.depth.z - fx.rules.stackLift) < 1e-9).length > 1) sideBySide++
    assert.deepEqual(SM.stackStatus(p, null, presets).collisions, [], `${name}: no collisions without the bundle`)
    assert.equal(SM.restackProject(p, null, presets), p, `${name}: Re-stack waits for the bundle`)
    const thicker = { ...p, layers: p.layers.map((l, i) => (i ? l : { ...l, depth: { ...l.depth, thickness: l.depth.thickness + 0.1 } })) }
    assert.equal(SM.keepProjectStack(p, thicker, null, presets), thicker, `${name}: no re-stack on guessed heights`)
    // a layer missing from a (stale) bundle: its pairs are not reported either; the known ones still are
    const some = { layers: Object.fromEntries(Object.entries(g.layers).filter(([id]) => id !== p.layers[0].id)) }
    for (const [j, i] of SM.stackStatus(p, some, presets).collisions) assert.ok(j !== 0 && i !== 0, `${name}: unknown layer 0 not reported`)
  }
  assert.ok(sideBySide >= 10, `side-by-side layers on the base covered (${sideBySide})`)
})
