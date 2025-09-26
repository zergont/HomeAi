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

  const lastMeta = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i]
      const md = e?.data?.metadata
      if (md && (md.context_budget || md.context_assembly)) return e
    }
    for (let i = events.length - 1; i >= 0; i--) {
      if (events[i].event === 'meta') return events[i]
    }
    return null
  }, [events])

  const ctxBudget = lastMeta?.data?.metadata?.context_budget
  const ctxAsm = lastMeta?.data?.metadata?.context_assembly
  const order: string[] = Array.isArray(ctxAsm?.order) ? ctxAsm.order : ["core", "tools", "l3", "l2", "l1"]

  const fillPct = ctxAsm?.fill_pct || {}
  const tokens = ctxAsm?.tokens || {}
  const caps = ctxAsm?.caps || {}
  const memLevels = ["l1", "l2", "l3"]
  const barColor = (pct: number) => pct > 85 ? "bg-red-400" : pct > 50 ? "bg-yellow-400" : "bg-gray-300"
  const freeOutCap = ctxAsm?.free_out_cap

  // Tokens section data
  const asm = lastMeta?.data?.metadata?.context_assembly ?? {}
  const bud = lastMeta?.data?.metadata?.context_budget ?? {}
  const bd = asm.tokens_breakdown ?? {}
  const promptTok = typeof asm.prompt_tokens_precise === 'number'
    ? asm.prompt_tokens_precise
    : (typeof asm.prompt_tokens_estimate === 'number' ? asm.prompt_tokens_estimate : undefined)
  const sc = asm.summary_counters ?? {}
  const inc = asm.includes ?? {}

  // HF-29D warning: L1 filled but no summaries produced
  const warnNoSumm = asm?.token_count_mode === 'proxy' && (asm?.l1_pairs_count ?? 0) > 8 && (sc?.l1_to_l2 ?? 0) === 0

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
      case 'error':
        return (
          <div key={i} className="mb-2 border rounded p-2 bg-red-50">
            {header}
            <div className="mt-1">{badge('ERROR', 'bg-red-600 text-white')}</div>
            <div className="text-sm text-red-700 whitespace-pre-wrap">{e.data?.message || 'Unknown error'}</div>
            {showRaw && <Raw />}
          </div>
        )
      case 'meta': {
        const prov = e.data?.provider || {}
        return (
            <div key={i} className="mb-2 border rounded p-2 bg-white">
              {header}
              <div className="mt-1 flex gap-2 items-center">
                {badge('META', 'bg-blue-600 text-white')}
                <span className="text-xs text-gray-700">status: {e.data?.status || 'in_progress'}</span>
                <span className="text-xs text-gray-500">model: {e.data?.model}</span>
                <span className="text-xs text-gray-500">provider: {prov?.name || ''}</span>
                {asm?.token_count_mode && <span className="text-[10px] px-2 py-0.5 rounded bg-gray-200">{asm.token_count_mode}</span>}
              </div>
              {e.data?.metadata?.thread_id && (
                <div className="text-xs text-gray-600">thread: {e.data.metadata.thread_id}</div>
              )}
              {showRaw && <Raw />}
            </div>
        )
      }
      case 'delta':
        return (
          <div key={i} className="mb-1 border rounded p-2 bg-gray-50">
            {header}
            <div className="mt-1 text-xs text-gray-800 whitespace-pre-wrap break-words">{e.data?.text || ''}</div>
            {showRaw && <Raw />}
          </div>
        )
      case 'summary':
        return (
          <div key={i} className="mb-2 border rounded p-2 bg-amber-50">
            {header}
            <div className="mt-1">{badge('SUMMARY', 'bg-amber-500 text-white')}</div>
            <div className="text-xs text-gray-800 whitespace-pre-wrap">{e.data?.summary || ''}</div>
            {showRaw && <Raw />}
          </div>
        )
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
      case 'done':
        return (
          <div key={i} className="mb-2 border rounded p-2 bg-green-50">
            {header}
            <div className="mt-1">{badge('DONE', 'bg-green-600 text-white')}</div>
            <div className="text-xs text-gray-700">status: {e.data?.status}</div>
            {showRaw && <Raw />}
          </div>
        )
      default:
        return (
          <div key={i} className="mb-2 border rounded p-2 bg-white">
            {header}
            {showRaw && <Raw />}
          </div>
        )
    }
  }

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
          {warnNoSumm && (
            <div className="mb-2 px-2 py-1 rounded bg-amber-100 text-amber-700 border border-amber-300 text-[11px]">
              Внимание: L1 заполнен, но саммари (L2) не создано. Проверь summarizer / L2 pipeline.
            </div>
          )}
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
              <Section title="Memory levels">
                {memLevels.map(lvl => (
                  <div key={lvl} className="mb-1">
                    <div className="flex items-center gap-2">
                      <span className="w-8 uppercase">{lvl}</span>
                      <div className="flex-1 h-3 rounded bg-gray-200 overflow-hidden">
                        <div className={`h-3 ${barColor(fillPct[lvl] || 0)}`} style={{ width: `${fillPct[lvl] || 0}%` }} />
                      </div>
                      <span className="ml-2">{tokens[lvl] || 0}/{caps[lvl] || 0} ток. — {fillPct[lvl] || 0}%</span>
                    </div>
                  </div>
                ))}
                <div className="text-[11px] text-gray-500 mt-1">L1 — последние пары без сжатия</div>
              </Section>
              {typeof asm.l1_pairs_count === 'number' && (
                <div className="mt-2 border rounded p-2 bg-gray-50">
                  <div className="font-semibold">L1 (fill-to-cap)</div>
                  <div>Pairs in L1: <b>{asm.l1_pairs_count}</b></div>
                  {Array.isArray(inc.l1_pairs) && inc.l1_pairs.length > 0 && (
                    <div className="text-xs opacity-80 break-all">IDs: {inc.l1_pairs.map((p:any)=>`${p.u}→${p.a}`).join(', ')}</div>
                  )}
                  {(sc.l1_to_l2 || sc.l2_to_l3) && (
                    <div className="mt-1">
                      <span>L1→L2: <b>{sc.l1_to_l2 ?? 0}</b></span>
                      <span className="ml-3">L2→L3: <b>{sc.l2_to_l3 ?? 0}</b></span>
                    </div>
                  )}
                </div>
              )}
              {ctxAsm?.summary_created && (
                <Section title="Summaries created this request">
                  <div className="grid grid-cols-2 gap-2">
                    <div className="rounded bg-gray-50 px-2 py-1">Summary L2 created: {ctxAsm.summary_created.l2 || 0}</div>
                    <div className="rounded bg-gray-50 px-2 py-1">Summary L3 created: {ctxAsm.summary_created.l3 || 0}</div>
                  </div>
                </Section>
              )}
              {Array.isArray(ctxAsm?.compaction_steps) && ctxAsm.compaction_steps.length > 0 && (
                <Section title="Compaction steps">
                  <ul className="list-disc pl-5">
                    {ctxAsm.compaction_steps.map((s: string, i: number) => (
                      <li key={i}>{s.replace('l1_to_l2', 'L1→L2').replace('l2_to_l3','L2→L3').replace('tail_reduce','Хвост').replace('drop_tools','Убраны инструменты').replace('shrink_core','Урезан Core')}</li>
                    ))}
                  </ul>
                </Section>
              )}
            </div>
          )}
          {(asm.token_count_mode || bd.total || typeof promptTok === 'number' || typeof asm.free_out_cap === 'number' || typeof bud.effective_max_output_tokens === 'number') && (
            <div className="mt-3 border rounded p-2">
              <div className="font-semibold mb-1">Tokens</div>
              {asm.token_count_mode && <div>Mode: <b>{asm.token_count_mode}</b></div>}
              {typeof promptTok === 'number' && <div>Total prompt (SDK): <b>{promptTok}</b></div>}
              {typeof asm.free_out_cap === 'number' && <div>Free out cap: <b>{asm.free_out_cap}</b></div>}
              {typeof bud.effective_max_output_tokens === 'number' && <div>Max output (effective): <b>{bud.effective_max_output_tokens}</b></div>}
              {bd.total && (
                <div className="mt-2">
                  <div className="opacity-70">Breakdown (tokens):</div>
                  <div>system: {bd.system ?? 0}</div>
                  <div>L3: {bd.l3 ?? 0}</div>
                  <div>L2: {bd.l2 ?? 0}</div>
                  <div>L1: {bd.l1 ?? 0}</div>
                  <div>user: {bd.user ?? 0}</div>
                  <div className="font-semibold">Total: {bd.total ?? 0}</div>
                </div>
              )}
              {sc && (
                <div className="mt-2">
                  <div className="font-semibold">Summaries:</div>
                  <div>L1→L2: <b>{sc.l1_to_l2 ?? 0}</b></div>
                  <div>L2→L3: <b>{sc.l2_to_l3 ?? 0}</b></div>
                </div>
              )}
              {inc && (
                <div className="mt-2">
                  <div className="font-semibold">Payload contains:</div>
                  <div>L3 ids: {Array.isArray(inc.l3_ids) ? inc.l3_ids.join(', ') : '—'}</div>
                  <div>L2 pairs: {Array.isArray(inc.l2_pairs) ? inc.l2_pairs.map((p:any)=>`${p.id}[${p.u}→${p.a}]`).join(', ') : '—'}</div>
                  <div>L1 pairs: {Array.isArray(inc.l1_pairs) ? inc.l1_pairs.map((p:any)=>`${p.u}→${p.a}`).join(', ') : '—'}</div>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
