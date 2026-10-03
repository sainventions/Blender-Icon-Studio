// Single import point for the 3D viewport workstream (D2): web/src/viewport + web/src/lib/appearance.
// The editor imports Viewport / LightDial only from here, so the dependency on D2's modules stays in one place.
export { Viewport, LightDial, DEFAULT_LIGHT_ANGLE, normalizeAngle } from '../../viewport'
export type { ViewportProps, LightDialProps } from '../../viewport'
export { resolveAppearance } from '../../lib/appearance'
