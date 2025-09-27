import React from 'react'

export default function MessageView({ items }: { items: { role: string; content: string; created_at?: string }[] }) {
  const reversed = [...items].reverse()
  return (
    <div className="space-y-2">
      {reversed.map((m, i) => (
        <div key={i} className="p-2 rounded border bg-white">
            <div className="text-xs text-gray-500">{m.role} {m.created_at ? `· ${m.created_at}` : ''}</div>
            <div className="whitespace-pre-wrap">{m.content}</div>
        </div>
      ))}
    </div>
  )
}
