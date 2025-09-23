import React from 'react'
import Badge from './Badge'

type Event = { event: string; data: any; ts: string }

function BudgetView({ b }: { b: any }) {
  if (!b) return null
  const base = b.C_eff ?? b.C_base ?? b.C_loaded ?? b.C_max
  const source = b.source || (b.C_loaded ? 'lmstudio.loaded_context_length' : (b.C_max ? 'lmstudio.max_context_length' : 'default'))
  const keys = ['C_eff','C_loaded','C_max','R_out','R_sys','Safety','B_total_in','core_sys_pad','core_reserved','B_work','effective_max_output_tokens']
  return (
    <div className="text-xs mt-1">
      <div className="mb-1">Base window: <span className="font-mono">{String(base)}</span> <span className="text-gray-500">({source})</span></div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-0.5">
        {keys.map(k => (
          <div key={k} className="flex justify-between"><span className="text-gray-500">{k}</span><span className="font-mono ml-2">{String(b[k])}</span></div>
        ))}
      </div>
    </div>
  )
}

function AssemblyView({ s }: { s: any }) {
  if (!s) return null
  const tok = s.tokens || {}
  const caps = s.caps || {}
  const b = s.budget || {}
  return (
    <div className="text-xs mt-1">
      <div className="font-semibold">Context assembly</div>
      <div className="grid grid-cols-3 gap-2">
        <div>core: <span className="font-mono">{tok.core}</span> / cap <span className="font-mono">{caps.core_cap}</span></div>
        <div>tools: <span className="font-mono">{tok.tools}</span> / cap <span className="font-mono">{caps.tools_cap}</span></div>
        <div>l3: <span className="font-mono">{tok.l3}</span> / cap <span className="font-mono">{caps.l3}</span></div>
        <div>l2: <span className="font-mono">{tok.l2}</span> / cap <span className="font-mono">{caps.l2}</span></div>
        <div>l1: <span className="font-mono">{tok.l1}</span> / cap <span className="font-mono">{caps.l1}</span></div>
        <div>total_in: <span className="font-mono">{tok.total_in}</span></div>
      </div>
      {Array.isArray(s.squeezes) && s.squeezes.length > 0 && (
        <div className="mt-1">squeezes: <span className="font-mono">{s.squeezes.join(', ')}</span></div>
      )}
    </div>
  )
}

function MemoryView({ m }: { m: any }) {
  if (!m) return null
  const caps = m.caps || {}
  const free = m.free_pct || {}
  return (
    <div className="text-xs mt-1">
      <div className="grid grid-cols-3 gap-3">
        {['l1','l2','l3'].map(k => (
          <div key={k} className="border rounded p-1">
            <div className="font-semibold uppercase">{k}</div>
            <div>tokens: <span className="font-mono">{m[`${k}_tokens`] ?? m.tokens?.[k]}</span></div>
            <div>cap: <span className="font-mono">{caps[k]}</span></div>
            <div>free%: <span className="font-mono">{typeof free[k] === 'number' ? (free[k]*100).toFixed(1)+'%' : '—'}</span></div>
          </div>
        ))}
      </div>
      {Array.isArray(m.actions) && m.actions.length > 0 && (
        <div className="mt-1">actions: <span className="font-mono">{m.actions.join(', ')}</span></div>
      )}
    </div>
  )
}

export default function EventsConsole({ events }: { events: Event[] }) {
  return (
    <div className="h-64 overflow-auto border rounded p-2 bg-white">
      {events.map((e, i) => (
        <div key={i} className="text-sm whitespace-pre-wrap">
          <Badge kind={e.event as any}>{e.event}</Badge>
          <span className="ml-2 text-gray-500">{e.ts}</span>
          {e.event === 'meta' && e.data?.metadata?.context_budget ? (
            <div className="ml-2 inline-block">
              <BudgetView b={e.data.metadata.context_budget} />
              <AssemblyView s={e.data.metadata.context_assembly} />
              <MemoryView m={e.data.metadata.memory} />
            </div>
          ) : e.event === 'meta.update' && (e.data?.memory || e.data?.metadata?.memory) ? (
            <div className="ml-2 inline-block">
              <MemoryView m={e.data.memory || e.data.metadata.memory} />
            </div>
          ) : (
            <span className="ml-2">{e.event === 'delta' ? e.data.text : JSON.stringify(e.data)}</span>
          )}
        </div>
      ))}
    </div>
  )
}
