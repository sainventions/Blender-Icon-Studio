// Animated layered "Liquid Glass" icon for the home hero — pure CSS 3D (no WebGL on the home screen).
import { useId, useMemo } from 'react'
import { squirclePath } from './icons'

export function GlassMotif({ size = 300 }: { size?: number }) {
  const id = useId().replace(/:/g, '')
  const path = useMemo(() => squirclePath(size, 0), [size])
  const s = size / 300 // design units → px

  const glass = (extra: React.CSSProperties): React.CSSProperties => ({
    position: 'absolute',
    background:
      'linear-gradient(160deg, rgba(255,255,255,0.55) 0%, rgba(255,255,255,0.16) 42%, rgba(255,255,255,0.06) 70%, rgba(255,255,255,0.22) 100%)',
    border: '1px solid rgba(255,255,255,0.55)',
    boxShadow: [
      'inset 0 1.5px 0 rgba(255,255,255,0.95)',
      'inset 0 -10px 22px rgba(255,255,255,0.12)',
      'inset 0 0 0 1px rgba(255,255,255,0.08)',
      `0 ${22 * s}px ${40 * s}px -${12 * s}px rgba(24,10,70,0.55)`,
    ].join(','),
    backdropFilter: 'blur(7px) saturate(170%)',
    WebkitBackdropFilter: 'blur(7px) saturate(170%)',
    animation: 'motif-breathe 6.5s ease-in-out infinite',
    ...extra,
  })

  return (
    <div className="relative select-none" style={{ width: size * 1.5, height: size * 1.35 }} aria-hidden>
      {/* ambient glow */}
      <div
        className="absolute left-[8%] top-[6%] h-[70%] w-[62%] rounded-full blur-[60px]"
        style={{ background: 'radial-gradient(circle, rgba(143,125,255,0.55), transparent 65%)', animation: 'motif-glow 9s ease-in-out infinite' }}
      />
      <div
        className="absolute right-[4%] top-[28%] h-[60%] w-[55%] rounded-full blur-[70px]"
        style={{ background: 'radial-gradient(circle, rgba(47,212,240,0.4), transparent 65%)', animation: 'motif-glow 11s ease-in-out -4s infinite reverse' }}
      />

      {/* floor shadow */}
      <div
        className="absolute bottom-[4%] left-1/2 h-[9%] w-[52%] rounded-[50%] blur-[18px]"
        style={{ background: 'radial-gradient(ellipse, rgba(0,0,0,0.75), transparent 70%)', animation: 'motif-shadow 6.5s ease-in-out infinite' }}
      />

      <div className="absolute left-1/2 top-[44%] -translate-x-1/2 -translate-y-1/2" style={{ perspective: 1100 * s, width: size, height: size }}>
        <div
          className="relative h-full w-full"
          style={{ transformStyle: 'preserve-3d', animation: 'motif-tilt 12s ease-in-out infinite' }}
        >
          {/* plate */}
          <div className="absolute inset-0" style={{ transform: 'translateZ(0px)' }}>
            <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="absolute inset-0 overflow-visible">
              <defs>
                <linearGradient id={`pl${id}`} x1="0" y1="0" x2="1" y2="1">
                  <stop offset="0" stopColor="#8b7bff" />
                  <stop offset="0.5" stopColor="#4f46e5" />
                  <stop offset="1" stopColor="#0ea5e9" />
                </linearGradient>
                <radialGradient id={`sh${id}`} cx="0.3" cy="0.18" r="0.9">
                  <stop offset="0" stopColor="#fff" stopOpacity="0.38" />
                  <stop offset="0.45" stopColor="#fff" stopOpacity="0.05" />
                  <stop offset="1" stopColor="#000" stopOpacity="0.25" />
                </radialGradient>
                <linearGradient id={`rim${id}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0" stopColor="#fff" stopOpacity="0.85" />
                  <stop offset="0.5" stopColor="#fff" stopOpacity="0.1" />
                  <stop offset="1" stopColor="#fff" stopOpacity="0.45" />
                </linearGradient>
              </defs>
              <path d={path} fill={`url(#pl${id})`} />
              <path d={path} fill={`url(#sh${id})`} />
              <path d={path} fill="none" stroke={`url(#rim${id})`} strokeWidth={2 * s} />
            </svg>
            {/* specular sweep clipped to the squircle */}
            <div className="absolute inset-0 overflow-hidden" style={{ clipPath: `path('${path}')` }}>
              <div
                className="absolute -top-1/4 left-0 h-[150%] w-[38%]"
                style={{
                  background: 'linear-gradient(90deg, transparent, rgba(255,255,255,0.28) 45%, rgba(255,255,255,0.5) 50%, rgba(255,255,255,0.28) 55%, transparent)',
                  animation: 'motif-sweep 7s ease-in-out 1.2s infinite',
                }}
              />
            </div>
          </div>

          {/* glass layers */}
          <div
            style={glass({
              left: 62 * s,
              top: 44 * s,
              width: 176 * s,
              height: 176 * s,
              borderRadius: '50%',
              ['--z0' as string]: `${34 * s}px`,
              ['--z1' as string]: `${56 * s}px`,
            })}
          />
          <div
            style={glass({
              left: 40 * s,
              top: 186 * s,
              width: 220 * s,
              height: 72 * s,
              borderRadius: 999,
              background:
                'linear-gradient(170deg, rgba(255,255,255,0.62) 0%, rgba(186,230,253,0.22) 50%, rgba(255,255,255,0.12) 100%)',
              animationDelay: '-1.4s',
              ['--z0' as string]: `${66 * s}px`,
              ['--z1' as string]: `${98 * s}px`,
            })}
          />
          <div
            style={glass({
              left: 118 * s,
              top: 92 * s,
              width: 64 * s,
              height: 64 * s,
              borderRadius: '50%',
              background: 'radial-gradient(circle at 35% 30%, #ffffff 0%, rgba(255,255,255,0.75) 35%, rgba(207,250,254,0.35) 100%)',
              animationDelay: '-2.6s',
              ['--z0' as string]: `${90 * s}px`,
              ['--z1' as string]: `${130 * s}px`,
            })}
          />
        </div>
      </div>

      {/* orbiting light */}
      <div className="absolute left-1/2 top-[44%] h-0 w-0">
        <div
          className="absolute h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-white"
          style={{
            boxShadow: '0 0 12px 4px rgba(255,255,255,0.8), 0 0 40px 10px rgba(143,125,255,0.6)',
            ['--r' as string]: `${size * 0.66}px`,
            animation: 'motif-orbit 16s linear infinite',
          }}
        />
      </div>
    </div>
  )
}
