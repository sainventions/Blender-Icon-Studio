// Shared setup of the viewport node tests (node --test, Node ≥ 23.6 built-in TS type stripping).
// Importing this module first registers a resolver so the app sources' extensionless relative imports ('./iso')
// resolve to .ts / .tsx, and gives access to the repository's Python sources (the worker is the reference).
import { register } from 'node:module'
import { readFileSync } from 'node:fs'
import assert from 'node:assert/strict'

register(
  'data:text/javascript,' +
    encodeURIComponent(`
export async function resolve(spec, ctx, next) {
  try {
    return await next(spec, ctx)
  } catch (err) {
    if ((spec.startsWith('./') || spec.startsWith('../')) && !/\\.[cm]?[jt]sx?$/.test(spec)) {
      for (const ext of ['.ts', '.tsx']) {
        try { return await next(spec + ext, ctx) } catch {}
      }
    }
    throw err
  }
}`),
  import.meta.url,
)

const REPO = new URL('../../../../', import.meta.url)
export const read = (p) => readFileSync(new URL(p, REPO), 'utf8')
export const fixture = (name) => JSON.parse(readFileSync(new URL(`./fixtures/${name}`, import.meta.url), 'utf8'))
export const presets = JSON.parse(read('shared/presets.json'))

/** Python module constant(s): `NAME = 0.85` or `A, B = 0.002, 0.008` → number. */
export function pyConst(src, name) {
  const re = new RegExp(`^([A-Z_][A-Z0-9_]*(?:\\s*,\\s*[A-Z_][A-Z0-9_]*)*)\\s*=\\s*([^#\\n]+)`, 'gm')
  for (const m of src.matchAll(re)) {
    const names = m[1].split(',').map((s) => s.trim())
    const k = names.indexOf(name)
    if (k < 0) continue
    const vals = m[2].split(',').map((s) => s.trim())
    const v = Number(vals[k])
    assert.ok(Number.isFinite(v), `${name} = ${vals[k]} is not a plain number`)
    return v
  }
  assert.fail(`${name} not found`)
}

/** A Python dict literal constant of numbers / strings (`NAME = {"a": 1.0, "b": "x"}`) → object. */
export function pyDict(src, name) {
  const m = new RegExp(`^${name}\\s*=\\s*(\\{[^}]*\\})`, 'm').exec(src)
  assert.ok(m, `${name} not found`)
  return JSON.parse(m[1].replace(/'/g, '"'))
}
