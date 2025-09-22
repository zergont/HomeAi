import React from 'react'

export default function Badge({ kind, children }: { kind: 'meta'|'delta'|'usage'|'done'|'ping'|'error', children: React.ReactNode }) {
  const cls = {
    meta: 'badge badge-meta',
    delta: 'badge badge-delta',
    usage: 'badge badge-usage',
    done: 'badge badge-done',
    ping: 'badge badge-ping',
    error: 'badge badge-error',
  }[kind]
  return <span className={cls}>{children}</span>
}
