import React from 'react'
import Badge from './Badge'

type Event = { event: string; data: any; ts: string }

export default function EventsConsole({ events }: { events: Event[] }) {
  return (
    <div className="h-64 overflow-auto border rounded p-2 bg-white">
      {events.map((e, i) => (
        <div key={i} className="text-sm whitespace-pre-wrap">
          <Badge kind={e.event as any}>{e.event}</Badge>
          <span className="ml-2 text-gray-500">{e.ts}</span>
          <span className="ml-2">{e.event === 'delta' ? e.data.text : JSON.stringify(e.data)}</span>
        </div>
      ))}
    </div>
  )
}
