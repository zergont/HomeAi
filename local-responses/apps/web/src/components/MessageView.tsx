import React from 'react'

export default function MessageView({ items }: { items: { role: string; content: string; created_at?: string }[] }) {
  const ordered = [...items].sort((a, b) => {
    const ta = a.created_at || ''
    const tb = b.created_at || ''
    // Descending: newer first
    return tb.localeCompare(ta)
  })
  return (
    <div className="space-y-2">
      {ordered.map((m, i) => (
        <div key={`${m.created_at || i}_${i}`} className="p-2 rounded border bg-white">
          <div className="text-xs text-gray-500">{m.role} {m.created_at ? `· ${m.created_at}` : ''}</div>
          <div className="whitespace-pre-wrap">{m.content}</div>
        </div>
      ))}
    </div>
  )
}
