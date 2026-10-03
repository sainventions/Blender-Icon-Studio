// What the importer found as the icon's background: a separate plate, nothing (art on transparency) or full-bleed art.
import type { SourceInfo } from '../types'
import { sourcePlateKind } from '../lib/labels'
import { Badge } from './ui'

export function SourcePlateBadge({ source }: { source: Pick<SourceInfo, 'plateDetected' | 'fullBleed'> }) {
  const kind = sourcePlateKind(source)
  if (kind === 'full-bleed')
    return (
      <Badge tone="accent" tip="The artwork itself forms the icon shape (no separate plate in the source)">
        Full-bleed
      </Badge>
    )
  if (kind === 'plate') return <Badge tone="ok">Plate detected</Badge>
  return <Badge tip="No background plate in the source: the art sits on transparency">No plate</Badge>
}
