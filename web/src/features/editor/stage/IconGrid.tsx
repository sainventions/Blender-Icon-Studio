// Apple-style icon construction grid overlay (for the Blender render view; the 3D viewport draws its own).
export function IconGrid({ className }: { className?: string }) {
  // Coordinates in a 1024 canvas, after Apple's app icon template.
  const c = 512
  const line = 'rgb(255 255 255 / 0.28)'
  const faint = 'rgb(255 255 255 / 0.14)'
  return (
    <svg viewBox="0 0 1024 1024" className={className} preserveAspectRatio="xMidYMid meet" aria-hidden>
      <g fill="none" strokeWidth={2} vectorEffect="non-scaling-stroke">
        <rect x="0.5" y="0.5" width="1023" height="1023" stroke={faint} />
        <line x1="0" y1="0" x2="1024" y2="1024" stroke={faint} />
        <line x1="1024" y1="0" x2="0" y2="1024" stroke={faint} />
        <line x1={c} y1="0" x2={c} y2="1024" stroke={faint} />
        <line x1="0" y1={c} x2="1024" y2={c} stroke={faint} />
        <circle cx={c} cy={c} r={190} stroke={line} />
        <circle cx={c} cy={c} r={322} stroke={line} />
        <circle cx={c} cy={c} r={460} stroke={faint} />
        <rect x={c - 322} y={c - 404} width={644} height={808} rx={2} stroke={faint} />
        <rect x={c - 404} y={c - 322} width={808} height={644} rx={2} stroke={faint} />
        <rect x={c - 404} y={c - 404} width={808} height={808} stroke={line} strokeDasharray="6 8" />
      </g>
    </svg>
  )
}
