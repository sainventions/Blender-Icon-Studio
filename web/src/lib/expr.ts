// Tiny, safe arithmetic evaluator for numeric fields (Icon Composer-style expressions):
//   "35*3"  → 105        "*2"  → current × 2        "+0.1" → current + 0.1      "(1+2)/4" → 0.75
// Supports + − × ÷ (also x and ×), parentheses, unary minus, decimals. Returns null when invalid.

type Tok = { t: 'num'; v: number } | { t: 'op'; v: string } | { t: 'paren'; v: '(' | ')' }

function tokenize(src: string): Tok[] | null {
  const out: Tok[] = []
  let i = 0
  while (i < src.length) {
    const c = src[i]
    if (c === ' ') {
      i++
      continue
    }
    if (/[0-9.]/.test(c)) {
      let j = i
      while (j < src.length && /[0-9.eE]/.test(src[j])) j++
      const v = Number(src.slice(i, j))
      if (!Number.isFinite(v)) return null
      out.push({ t: 'num', v })
      i = j
      continue
    }
    if ('+-*/×÷x'.includes(c)) {
      out.push({ t: 'op', v: c === '×' || c === 'x' ? '*' : c === '÷' ? '/' : c })
      i++
      continue
    }
    if (c === '(' || c === ')') {
      out.push({ t: 'paren', v: c })
      i++
      continue
    }
    return null
  }
  return out
}

function parse(tokens: Tok[]): number | null {
  let pos = 0
  const peek = () => tokens[pos]
  const expr = (): number | null => {
    let left = term()
    if (left == null) return null
    while (peek()?.t === 'op' && (peek()!.v === '+' || peek()!.v === '-')) {
      const op = tokens[pos++].v
      const right = term()
      if (right == null) return null
      left = op === '+' ? left + right : left - right
    }
    return left
  }
  const term = (): number | null => {
    let left = factor()
    if (left == null) return null
    while (peek()?.t === 'op' && (peek()!.v === '*' || peek()!.v === '/')) {
      const op = tokens[pos++].v
      const right = factor()
      if (right == null) return null
      left = op === '*' ? left * right : right === 0 ? NaN : left / right
    }
    return left
  }
  const factor = (): number | null => {
    const tk = peek()
    if (!tk) return null
    if (tk.t === 'op' && tk.v === '-') {
      pos++
      const v = factor()
      return v == null ? null : -v
    }
    if (tk.t === 'op' && tk.v === '+') {
      pos++
      return factor()
    }
    if (tk.t === 'num') {
      pos++
      return tk.v
    }
    if (tk.t === 'paren' && tk.v === '(') {
      pos++
      const v = expr()
      if (peek()?.t !== 'paren' || peek()!.v !== ')') return null
      pos++
      return v
    }
    return null
  }
  const v = expr()
  if (v == null || pos !== tokens.length || !Number.isFinite(v)) return null
  return v
}

/** Evaluate `input` relative to `current` (leading operator applies to the current value). */
export function evaluateExpression(input: string, current: number, opts: { percent?: boolean } = {}): number | null {
  let src = input.trim().replace(/,/g, '.')
  if (!src) return null
  const isPercent = opts.percent && src.endsWith('%')
  if (src.endsWith('%')) src = src.slice(0, -1)
  // Plain decimal (String(1e-7) would be "1e-7", which the tokenizer cannot read back).
  const cur = Number.isFinite(current) ? current.toFixed(12).replace(/\.?0+$/, '') : '0'
  if (/^[*/×÷x]/.test(src)) src = `(${cur})${src}`
  else if (/^[+]/.test(src) || (/^-/.test(src) && src.length > 1 && /^-\s/.test(src))) src = `(${cur})${src}`
  const toks = tokenize(src)
  if (!toks) return null
  const v = parse(toks)
  if (v == null) return null
  return isPercent ? v / 100 : v
}
