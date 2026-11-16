import { useEffect, useRef, useState } from 'react'
import ForceGraph3D from '3d-force-graph'
import { tipBox, type G3Link, type G3Node } from './graph3dUtil'
import { CanvasTexture, LinearFilter, Sprite, SpriteMaterial } from 'three'

// A thin wrapper over 3d-force-graph with its DAG layout turned on (left to right), shared by the
// journey view and the causal-links view. Callers describe points and arrows; this draws them.
const REL = 5
function label(lines: string[], radius: number, textures: CanvasTexture[]): Sprite {
  const scale = 4 // drawn large, shown small, so the text stays sharp when zoomed in
  const sizes = lines.map((_, i) => (i === 0 ? 13 : 10) * scale)
  const canvas = document.createElement('canvas')
  const ctx = canvas.getContext('2d') as CanvasRenderingContext2D
  const font = (i: number) => `${i === 0 ? 600 : 400} ${sizes[i]}px system-ui, -apple-system, "Segoe UI", sans-serif`
  const widths = lines.map((t, i) => {
    ctx.font = font(i)
    return ctx.measureText(t).width
  })
  const pad = 6 * scale
  canvas.width = Math.ceil(Math.max(...widths) + pad * 2)
  canvas.height = Math.ceil(sizes.reduce((a, s) => a + s * 1.3, 0) + pad * 2)
  ctx.fillStyle = 'rgba(252,253,251,0.9)'
  ctx.fillRect(0, 0, canvas.width, canvas.height)
  ctx.textAlign = 'center'
  ctx.textBaseline = 'top'
  let y = pad
  lines.forEach((t, i) => {
    ctx.font = font(i)
    ctx.fillStyle = i === 0 ? '#101816' : '#4d5955'
    ctx.fillText(t, canvas.width / 2, y)
    y += sizes[i] * 1.3
  })
  const texture = new CanvasTexture(canvas)
  texture.minFilter = LinearFilter
  textures.push(texture)
  const sprite = new Sprite(new SpriteMaterial({ map: texture, depthWrite: false, transparent: true }))
  const h = canvas.height / scale / 1.5
  sprite.scale.set((canvas.width / canvas.height) * h, h, 1)
  sprite.position.set(0, radius + h / 2 + 2, 0)
  return sprite
}

export default function Graph3D({
  nodes,
  links,
  dag,
  levelDistance = 70,
  onSelect,
}: {
  nodes: G3Node[]
  links: G3Link[]
  dag: boolean
  levelDistance?: number
  onSelect: (id: string | null) => void
}) {
  const mount = useRef<HTMLDivElement>(null)
  const [failed, setFailed] = useState(false)
  const select = useRef(onSelect)
  useEffect(() => {
    select.current = onSelect
  }, [onSelect])

  useEffect(() => {
    const el = mount.current
    if (!el || nodes.length === 0) return
    let fg: InstanceType<typeof ForceGraph3D> | null = null
    let observer: ResizeObserver | null = null
    let early = 0
    const textures: CanvasTexture[] = []
    try {
      fg = new ForceGraph3D(el, { controlType: 'orbit' })
        .backgroundColor('#fcfdfb')
        .width(el.clientWidth)
        .height(el.clientHeight)
        .showNavInfo(false)
        .nodeRelSize(REL)
        .nodeOpacity(1)
        .nodeVal((n) => (n as G3Node).size)
        .nodeColor((n) => (n as G3Node).color)
        .nodeLabel((n) => tipBox((n as G3Node).tip))
        .nodeThreeObjectExtend(true)
        .nodeThreeObject((n: object) => {
          const d = n as G3Node
          // Returning nothing keeps the plain sphere.
          return d.text ? label(d.text, REL * Math.cbrt(d.size), textures) : (undefined as never)
        })
        .linkColor((l) => (l as unknown as G3Link).color)
        .linkOpacity(0.9)
        .linkWidth((l) => (l as unknown as G3Link).width ?? 0.6)
        .linkDirectionalArrowLength(4)
        .linkDirectionalArrowRelPos(1)
        .linkLabel((l) => tipBox((l as unknown as G3Link).tip))
        .onNodeClick((n) => select.current((n as G3Node).id))
        .onBackgroundClick(() => select.current(null))
        .d3VelocityDecay(0.3)
      // Earlier on the left, what followed on the right.
      if (dag) fg.dagMode('lr').dagLevelDistance(levelDistance).onDagError(() => undefined)
      // The library moves these objects, so it gets its own copies.
      fg.graphData({ nodes: nodes.map((n) => ({ ...n })), links: links.map((l) => ({ ...l })) })
      const built = fg
      built.onEngineStop(() => built.zoomToFit(400, 40))
      // Frame it early as well; the layout can take several seconds to settle fully.
      early = window.setTimeout(() => built.zoomToFit(400, 40), 1500)
      observer = new ResizeObserver(() => built.width(el.clientWidth).height(el.clientHeight))
      observer.observe(el)
    } catch {
      // WebGL is missing or blocked; say so instead of leaving an empty box.
      setFailed(true)
    }
    return () => {
      window.clearTimeout(early)
      observer?.disconnect()
      fg?._destructor()
      textures.forEach((t) => t.dispose())
      el.replaceChildren()
    }
  }, [nodes, links, dag, levelDistance])

  if (failed) {
    return (
      <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
        This browser could not draw the 3D view (WebGL is unavailable). The list below shows the same information.
      </p>
    )
  }
  return (
    <>
      <div ref={mount} className="h-[560px] w-full overflow-hidden rounded-lg border border-slate-200 bg-white" />
      <p className="pointer-events-none absolute bottom-2 left-3 text-xs text-slate-400">
        Drag to rotate · scroll to zoom · right-drag to pan · click a point for detail
      </p>
    </>
  )
}
