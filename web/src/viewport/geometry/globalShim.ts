// poly2tri's CommonJS entry reads `global.poly2tri` (its noConflict support); browsers only have globalThis.
// Imported before poly2tri so the dev pre-bundle and the production build evaluate it first.
const g = globalThis as unknown as { global?: unknown }
if (g.global === undefined) g.global = globalThis
export {}
