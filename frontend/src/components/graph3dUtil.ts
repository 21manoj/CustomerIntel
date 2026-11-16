// Shapes and helpers shared by the 3D graph component and the pages that feed it.
export interface G3Node {
  id: string
  color: string
  size: number          // relative volume; 1 is an ordinary point
  tip: string           // HTML shown on hover (already escaped)
  text?: string[]       // lines drawn above the point, always facing the viewer
}
export interface G3Link {
  source: string
  target: string
  color: string
  tip: string
  width?: number
}

export function esc(s: string): string {
  return s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c] as string)
}
export function tipBox(html: string): string {
  return `<div style="max-width:320px;padding:6px 8px;border-radius:6px;background:#101816;color:#fcfdfb;font:12px system-ui,sans-serif">${html}</div>`
}

// The DAG layout refuses a graph with a loop. True when following arrows from `from` reaches `to`.
export function reaches(links: { source: string; target: string }[], from: string, to: string): boolean {
  const out = new Map<string, string[]>()
  links.forEach((l) => out.set(l.source, [...(out.get(l.source) ?? []), l.target]))
  const seen = new Set<string>()
  const stack = [from]
  while (stack.length) {
    const n = stack.pop() as string
    if (n === to) return true
    if (seen.has(n)) continue
    seen.add(n)
    stack.push(...(out.get(n) ?? []))
  }
  return false
}
export function hasCycle(links: { source: string; target: string }[]): boolean {
  return links.some((l) => reaches(links, l.target, l.source))
}

