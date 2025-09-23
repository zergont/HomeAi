import React, { useMemo } from 'react'

export default function EventsConsole({ events }: { events: { event: string; data: any; ts: string }[] }) {
  const lastMeta = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      if (events[i].event === 'meta' || events[i].event === 'done') return events[i]
    }
    return null
  }, [events])

  const ctxBudget = lastMeta?.data?.metadata?.context_budget
  const ctxAsm = lastMeta?.data?.metadata?.context_assembly
  const order: string[] = Array.isArray(ctxAsm?.order) ? ctxAsm.order : ["core","tools","l3","l2","l1"]

  return (
    <div className="space-y-2">
      <div className="h-64 overflow-auto p-2 border rounded bg-white text-xs whitespace-pre-wrap">
        {events.map((e, i) => (
          <div key={i} className="mb-1">
            <div className="text-gray-500">[{e.ts}] {e.event}</div>
            {e.data && <pre className="pl-2">{JSON.stringify(e.data, null, 2)}</pre>}
          </div>
        ))}
      </div>
      {(ctxBudget || ctxAsm) && (
        <div className="p-2 border rounded bg-white text-xs">
          <div className="font-semibold mb-1">Final metadata</div>
          {ctxBudget && (
            <>
              <div className="text-gray-600">context_budget</div>
              <pre className="pl-2">{JSON.stringify(ctxBudget, null, 2)}</pre>
            </>
          )}
          {ctxAsm && (
            <>
              <div className="text-gray-600 mt-2">context_assembly</div>
              <div className="mb-1">order: [{order.join(', ')}]</div>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
                <div>
                  <div className="font-semibold">tokens</div>
                  <pre>{JSON.stringify(ctxAsm.tokens, null, 2)}</pre>
                </div>
                <div>
                  <div className="font-semibold">caps</div>
                  <pre>{JSON.stringify(ctxAsm.caps, null, 2)}</pre>
                </div>
                <div>
                  <div className="font-semibold">squeezes</div>
                  <pre>{JSON.stringify(ctxAsm.squeezes, null, 2)}</pre>
                </div>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
