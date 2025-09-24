import React, { useMemo, useState, useEffect, ReactNode } from 'react'

type Ev = { event: string; data: any; ts: string }

const Section = ({ title, children }: { title: string; children: ReactNode }) => (
  <div className="mb-1">
    <div className="text-gray-600 font-semibold">{title}</div>
    <div className="pl-2">{children}</div>
  </div>
)

export default function EventsConsole({ events }: { events: Ev[] }) {
  const [showDeltas, setShowDeltas] = useState(false)
  const [showRaw, setShowRaw] = useState(false)
  const [resetBadge, setResetBadge] = useState(false)

  useEffect(() => {
    const lastMeta = events[events.length - 1]?.data?.reset
    if (lastMeta) {
      setResetBadge(true)
      setTimeout(() => setResetBadge(false), 4000)
    }
  }, [events])

  // Find the last event that actually carries metadata with context fields
  const lastMeta = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i]
      const md = e?.data?.metadata
      if (md && (md.context_budget || md.context_assembly)) return e
    }
    // fallback: try last 'meta'
    for (let i = events.length - 1; i >= 0; i--) {
      if (events[i].event === 'meta') return events[i]
    }
    return null
  }, [events])

  const ctxBudget = lastMeta?.data?.metadata?.context_budget
  const ctxAsm = lastMeta?.data?.metadata?.context_assembly
  const order: string[] = Array.isArray(ctxAsm?.order) ? ctxAsm.order : ["core", "tools", "l3", "l2", "l1"]

  // Memory levels progress bars
  const fillPct = ctxAsm?.fill_pct || {}
  const tokens = ctxAsm?.tokens || {}
  const caps = ctxAsm?.caps || {}
  const memLevels = ["l1", "l2", "l3"]
  const barColor = (pct: number) =>
    pct > 85 ? "bg-red-400" : pct > 50 ? "bg-yellow-400" : "bg-gray-300"

  // Last assistant before user
  const lastAsst = ctxAsm?.last_assistant_before_user

  const renderEvent = (e: Ev, i: number) => {
    const badge = (txt: string, cls: string) => (
      <span className={`inline-block text-[10px] px-2 py-0.5 rounded ${cls}`}>{txt}</span>
    )

    const header = (
      <div className="flex items-center gap-2 text-gray-500">
        <span>[{e.ts}]</span>
        <span className="font-mono">{e.event}</span>
      </div>
    )

    const Raw = () => (
      <details className="mt-1">
        <summary className="cursor-pointer text-xs text-gray-500">JSON</summary>
        <pre className="pl-2 whitespace-pre-wrap break-words text-[11px]">{JSON.stringify(e.data, null, 2)}</pre>
      </details>
    )

    if (e.event === 'delta' && !showDeltas) return null

    switch (e.event) {
      case 'error': {
        return (
          <div key={i} className="mb-2 border rounded p-2 bg-red-50">
            {header}
            <div className="mt-1">{badge('ERROR', 'bg-red-600 text-white')}</div>
            <div className="text-sm text-red-700 whitespace-pre-wrap">{e.data?.message || 'Unknown error'}</div>
            {showRaw && <Raw />}
          </div>
        )
      }
      case 'meta': {
        const md = e.data?.metadata || {}
        const prov = e.data?.provider || {}
        return (
          <div key={i} className="mb-2 border rounded p-2 bg-white">
            {header}
            <div className="mt-1 flex gap-2 items-center">
              {badge('META', 'bg-blue-600 text-white')}
              <span className="text-xs text-gray-700">status: {e.data?.status || 'in_progress'}</span>
              <span className="text-xs text-gray-500">model: {e.data?.model}</span>
              <span className="text-xs text-gray-500">provider: {prov?.name || ''}</span>
            </div>
            {md?.thread_id && (
              <div className="text-xs text-gray-600">thread: {md.thread_id}</div>
            )}
            {showRaw && <Raw />}
          </div>
        )
      }
      case 'delta': {
        return (
          <div key={i} className="mb-1 border rounded p-2 bg-gray-50">
            {header}
            <div className="mt-1 text-xs text-gray-800 whitespace-pre-wrap break-words">{e.data?.text || ''}</div>
            {showRaw && <Raw />}
          </div>
        )
      }
      case 'summary': {
        return (
          <div key={i} className="mb-2 border rounded p-2 bg-amber-50">
            {header}
            <div className="mt-1">{badge('SUMMARY', 'bg-amber-500 text-white')}</div>
            <div className="text-xs text-gray-800 whitespace-pre-wrap">{e.data?.summary || ''}</div>
            {showRaw && <Raw />}
          </div>
        )
      }
      case 'usage': {
        const u = e.data || {}
        return (
          <div key={i} className="mb-2 border rounded p-2 bg-white">
            {header}
            <div className="mt-1">{badge('USAGE', 'bg-gray-700 text-white')}</div>
            <div className="text-xs text-gray-700">input: {u.input_tokens} • output: {u.output_tokens} • total: {u.total_tokens}</div>
            {showRaw && <Raw />}
          </div>
        )
      }
      case 'done': {
        return (
          <div key={i} className="mb-2 border rounded p-2 bg-green-50">
            {header}
            <div className="mt-1">{badge('DONE', 'bg-green-600 text-white')}</div>
            <div className="text-xs text-gray-700">status: {e.data?.status}</div>
            {showRaw && <Raw />}
          </div>
        )
      }
      default: {
        return (
          <div key={i} className="mb-2 border rounded p-2 bg-white">
            {header}
            {showRaw && <Raw />}
          </div>
        )
      }
    }
  }

  const freeOutCap = ctxAsm?.free_out_cap

  return (
    <div className="space-y-2">
      {resetBadge && (
        <div className="bg-yellow-100 border border-yellow-400 text-yellow-800 px-4 py-2 rounded mb-2">
          Retry due to length... (ответ был обрезан, повторная попытка)
        </div>
      )}
      <div className="flex items-center gap-4 text-xs">
        <label className="flex items-center gap-1 cursor-pointer">
          <input type="checkbox" checked={showDeltas} onChange={e => setShowDeltas(e.target.checked)} />
          Показывать delta
        </label>
        <label className="flex items-center gap-1 cursor-pointer">
          <input type="checkbox" checked={showRaw} onChange={e => setShowRaw(e.target.checked)} />
          Показывать JSON
        </label>
      </div>

      <div className="h-64 overflow-auto p-2 border rounded bg-white text-xs">
        {events.map(renderEvent)}
        {events.length === 0 && <div className="text-gray-400">Пока нет событий</div>}
      </div>

      {(ctxBudget || ctxAsm) && (
        <div className="p-2 border rounded bg-white text-xs">
          <div className="font-semibold mb-1">Final metadata</div>
          {ctxBudget && (
            <div className="mb-2">
              <div className="text-gray-600">context_budget</div>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mt-1">
                {(Object.entries(ctxBudget) as [string, any][]).map(([k, v]) => (
                  <div key={k} className="rounded bg-gray-50 px-2 py-1">
                    <span className="text-gray-500">{k}</span>: {String(v)}
                    {k === 'effective_max_output_tokens' && typeof freeOutCap === 'number' && (
                      <span className="ml-2 text-gray-600">(free_out_cap: {freeOutCap})</span>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
          {ctxAsm && (
            <>
              <div>
                <div className="text-gray-600 mb-1">context_assembly</div>
                <Section title="order">
                  <div className="flex flex-wrap gap-1">
                    {order.map((o) => (
                      <span key={o} className="px-2 py-0.5 rounded bg-gray-100">{o}</span>
                    ))}
                  </div>
                </Section>
                <Section title="tokens">
                  <div className="grid grid-cols-2 md:grid-cols-6 gap-2">
                    {(Object.entries(ctxAsm.tokens || {}) as [string, any][]).map(([k, v]) => (
                      <div key={k} className="rounded bg-gray-50 px-2 py-1"><span className="text-gray-500">{k}</span>: {String(v)}</div>
                    ))}
                  </div>
                </Section>
                <Section title="caps">
                  <div className="grid grid-cols-2 md:grid-cols-6 gap-2">
                    {(Object.entries(ctxAsm.caps || {}) as [string, any][]).map(([k, v]) => (
                      <div key={k} className="rounded bg-gray-50 px-2 py-1"><span className="text-gray-500">{k}</span>: {String(v)}</div>
                    ))}
                  </div>
                </Section>
                <Section title="squeezes">
                  {Array.isArray(ctxAsm.squeezes) && ctxAsm.squeezes.length > 0 ? (
                    <ul className="list-disc pl-5">
                      {ctxAsm.squeezes.map((s: string, i: number) => (
                        <li key={i}>{s}</li>
                      ))}
                    </ul>
                  ) : (
                    <span className="text-gray-500">—</span>
                  )}
                </Section>
                {(typeof ctxAsm.squeezed === 'boolean' || typeof ctxAsm.current_user_only_mode === 'boolean') && (
                  <div className="mt-1 text-gray-700">
                    squeezed: {String(ctxAsm.squeezed)}; current_user_only_mode: {String(ctxAsm.current_user_only_mode)}
                  </div>
                )}
              </div>
              <Section title="Memory levels">
                {memLevels.map(lvl => (
                  <div key={lvl} className="mb-1">
                    <div className="flex items-center gap-2">
                      <span className="w-8 uppercase">{lvl}</span>
                      <div className="flex-1 h-3 rounded bg-gray-200 overflow-hidden">
                        <div
                          className={`h-3 ${barColor(fillPct[lvl] || 0)}`}
                          style={{ width: `${fillPct[lvl] || 0}%` }}
                        />
                      </div>
                      <span className="ml-2">{tokens[lvl] || 0}/{caps[lvl] || 0} ток. — {fillPct[lvl] || 0}%</span>
                    </div>
                  </div>
                ))}
              </Section>
              <Section title="Last assistant before user">
                {lastAsst?.preview ? (
                  <div className="border rounded bg-gray-50 px-2 py-1 text-xs">
                    <span className="text-gray-500 mr-2">Preview:</span>
                    <span className="font-mono">{lastAsst.preview}</span>
                  </div>
                ) : (
                  <span className="text-gray-400">нет данных</span>
                )}
              </Section>
            </>
          )}
          {lastMeta?.data?.metadata?.retry && (
            <div className="mt-2 text-blue-700">Retry: {JSON.stringify(lastMeta.data.metadata.retry)}</div>
          )}
          {ctxAsm?.think_truncated && (
            <div className="mt-1 text-orange-700">think_truncated: {String(ctxAsm.think_truncated)}</div>
          )}
          {lastMeta?.data?.metadata?.tool_runs_first_attempt !== undefined && (
            <div className="mt-1 text-green-700">tool_runs_first_attempt: {String(lastMeta.data.metadata.tool_runs_first_attempt)}</div>
          )}
        </div>
      )}
    </div>
  )
}
