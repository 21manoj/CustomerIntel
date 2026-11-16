import { useEffect, useState } from 'react'
import { getVertical } from './client'
import type { VerticalInfo, VerticalLabels } from './types'

// The platform's own words. A screen shows these until the tenant's vertical answers, and keeps
// them if the request fails, so a label is never blank. A vertical renames them in its catalog
// ("labels"); screens ask for the label and never for the vertical.
export const DEFAULT_LABELS: VerticalLabels = {
  account: 'Account',
  accounts: 'Accounts',
  revenue: 'Revenue',
  recurring_revenue: 'ARR',
  renewal: 'Renewal',
}

export function useVertical(customerId: number | null | undefined): { info: VerticalInfo | null; labels: VerticalLabels; loaded: boolean } {
  const [info, setInfo] = useState<VerticalInfo | null>(null)
  const [loaded, setLoaded] = useState(false)
  useEffect(() => {
    setInfo(null)
    setLoaded(false)
    if (customerId == null) return
    let live = true
    getVertical(customerId)
      .then((v) => live && setInfo(v))
      .catch(() => {})
      .finally(() => live && setLoaded(true))
    return () => {
      live = false
    }
  }, [customerId])
  return { info, labels: { ...DEFAULT_LABELS, ...(info?.labels ?? {}) }, loaded }
}
