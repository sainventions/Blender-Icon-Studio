// Single import point for the 3D viewport workstream (D2): web/src/viewport + web/src/lib/appearance.
// The editor imports Viewport / LightDial only from here, so the dependency on D2's modules stays in one place.
export { Viewport, LightDial, DEFAULT_LIGHT_ANGLE, normalizeAngle } from '../../viewport'
export type { ViewportProps, LightDialProps } from '../../viewport'
export { resolveAppearance } from '../../lib/appearance'
// World height of a layer's height-field bodies (the live mirror of blender_worker scene._body_height). The stacking model
// (features/editor/stackModel.ts) imports the pure geometry module directly so its node tests need no Viewport.
export { layerBodyHeight, layerScale } from '../../viewport/geometry/layerGeometry'
// The round-edge radius a body really gets (round 8: a minimum vertical wall) — the Depth section's read-out.
export { rimBevel } from '../../viewport/geometry/heightfield'
