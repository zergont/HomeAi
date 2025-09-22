export type SSEEvent = { event: string; data: any }

export function parseSSEChunks() {
  let buffer = ''
  return (chunk: string): SSEEvent[] => {
    buffer += chunk
    const events: SSEEvent[] = []
    let idx: number
    while ((idx = buffer.indexOf('\n\n')) !== -1) {
      const block = buffer.slice(0, idx)
      buffer = buffer.slice(idx + 2)
      let ev = ''
      let data = ''
      for (const line of block.split('\n')) {
        if (line.startsWith('event: ')) ev = line.slice(7)
        else if (line.startsWith('data: ')) data = line.slice(6)
      }
      if (ev) {
        try {
          events.push({ event: ev, data: JSON.parse(data) })
        } catch {}
      }
    }
    return events
  }
}
