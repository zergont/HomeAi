import React, { useEffect, useRef, useState } from 'react'
import Controls from './components/Controls'
import EventsConsole from './components/EventsConsole'
import MessageView from './components/MessageView'
import StatusIndicators from './components/StatusIndicators'
import { getThreadMessages, postResponses, streamResponses, cancelResponse, type ThreadMessagesResponse } from './lib/api'
import ProfilePage from './pages/Profile'

export default function App() {
  const [tab, setTab] = useState<'chat'|'profile'|'output'>('chat')
  const [model, setModel] = useState('')
  const [system, setSystem] = useState('')
  const [input, setInput] = useState('')
  const [temperature, setTemperature] = useState(0.7)
  const [maxTokens, setMaxTokens] = useState(512)
  const [createThread, setCreateThread] = useState(true)
  const [threadId, setThreadId] = useState<string|null>(null)
  const [stream, setStream] = useState(true)

  const [events, setEvents] = useState<{event:string; data:any; ts:string}[]>([])
  const [assistantText, setAssistantText] = useState('')
  const [history, setHistory] = useState<ThreadMessagesResponse|null>(null)
  const [lastProviderRequest, setLastProviderRequest] = useState<any|null>(null)
  const currentRespId = useRef<string|null>(null)
  const abortCtrl = useRef<AbortController|null>(null)
  const threadIdRef = useRef<string|null>(null)

  // summarization indicator
  const [summarizing, setSummarizing] = useState(false)
  const [summarizeSince, setSummarizeSince] = useState<number|null>(null)
  const [summaryElapsed, setSummaryElapsed] = useState(0)

  useEffect(() => { threadIdRef.current = threadId }, [threadId])

  useEffect(() => {
    let t: any
    if (summarizing && summarizeSince) {
      const tick = () => setSummaryElapsed(Math.floor((Date.now() - summarizeSince) / 1000))
      tick()
      t = setInterval(tick, 500)
    } else {
      setSummaryElapsed(0)
    }
    return () => { if (t) clearInterval(t) }
  }, [summarizing, summarizeSince])

  const onSend = async () => {
    setEvents([]); setAssistantText('')
    const body: any = { model, input, system: system || undefined, temperature, max_output_tokens: maxTokens, create_thread: createThread, thread_id: threadId || undefined }
    if (!stream) {
      try {
        const res = await postResponses(body)
        if (res?.metadata?.thread_id) { setThreadId(res.metadata.thread_id); threadIdRef.current = res.metadata.thread_id }
        // capture provider request from backend metadata
        if (res?.metadata?.provider_request) setLastProviderRequest(res.metadata.provider_request)
        setAssistantText(res.output?.[0]?.content?.[0]?.text || '')
        if (res?.metadata?.summary) {
          // summary ready immediately
          setSummarizing(false)
          setHistory({ thread_id: res.metadata.thread_id, summary: res.metadata.summary, summary_updated_at: res.metadata.summary_updated_at, messages: [], context: undefined })
        } else {
          // start indicator until we fetch history
          setSummarizing(true); setSummarizeSince(Date.now())
        }
        await refreshHistory(res?.metadata?.thread_id || threadIdRef.current)
      } catch (err: any) {
        const msg = (err?.message || 'Request failed') as string
        setEvents(prev => [...prev, { event: 'error', data: { message: msg }, ts: new Date().toLocaleTimeString() }])
        setSummarizing(false)
      }
      return
    }
    abortCtrl.current = new AbortController()
    try {
      await streamResponses(body, (ev) => {
        setEvents(prev => [...prev, { event: ev.event, data: ev.data, ts: new Date().toLocaleTimeString() }])
        if (ev.event === 'meta') {
          const id = ev.data?.metadata?.thread_id; if (id) { setThreadId(id); threadIdRef.current = id } currentRespId.current = ev.data.id
          // capture provider request for Output tab
          if (ev.data?.metadata?.provider_request) setLastProviderRequest(ev.data.metadata.provider_request)
        }
        if (ev.event === 'delta') setAssistantText(prev => prev + (ev.data?.text || ''))
        if (ev.event === 'summary') {
          setSummarizing(false)
          setHistory(prev => ({ thread_id: threadIdRef.current!, summary: ev.data?.summary, summary_updated_at: ev.data?.summary_updated_at, messages: prev?.messages || [], context: prev?.context }))
        }
        if (ev.event === 'done') {
          currentRespId.current = null
          // start summarization indicator; will stop on 'summary' event or after history load
          setSummarizing(true); setSummarizeSince(Date.now())
          refreshHistory(threadIdRef.current)
        }
      }, abortCtrl.current.signal)
    } catch (err: any) {
      const msg = (err?.message || 'Stream request failed') as string
      setEvents(prev => [...prev, { event: 'error', data: { message: msg }, ts: new Date().toLocaleTimeString() }])
      setSummarizing(false)
      currentRespId.current = null
    }
  }

  const onCancel = async () => {
    if (currentRespId.current) {
      try { await cancelResponse(currentRespId.current) } catch {}
    }
    if (abortCtrl.current) abortCtrl.current.abort()
    // stop summarizing indicator on cancel
    setSummarizing(false)
  }

  const onClear = () => { setEvents([]); setAssistantText('') }

  const refreshHistory = async (tid?: string|null) => {
    const id = tid || threadIdRef.current
    if (!id) return
    try {
      const data = await getThreadMessages(id)
      setHistory(data)
      if (data.summary) setSummarizing(false)
    } catch (e: any) {
      setEvents(prev => [...prev, { event: 'error', data: { message: e?.message || 'Failed to load history' }, ts: new Date().toLocaleTimeString() }])
    }
  }

  const copyLastProviderRequest = async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(lastProviderRequest, null, 2))
    } catch {}
  }

  return (
    <div className="max-w-6xl mx-auto p-4 space-y-6">
      <div className="flex items-start justify-between">
        <h1 className="text-2xl font-bold">Local Responses Hub</h1>
        <div className="flex gap-2">
          <button className={`px-2 py-1 border rounded ${tab==='chat'?'bg-gray-200':''}`} onClick={()=>setTab('chat')}>Chat</button>
          <button className={`px-2 py-1 border rounded ${tab==='profile'?'bg-gray-200':''}`} onClick={()=>setTab('profile')}>Profile</button>
          <button className={`px-2 py-1 border rounded ${tab==='output'?'bg-gray-200':''}`} onClick={()=>setTab('output')}>Output</button>
        </div>
        <StatusIndicators />
      </div>

      {tab === 'chat' ? (
        <>
          <Controls
            model={model} setModel={setModel}
            system={system} setSystem={setSystem}
            input={input} setInput={setInput}
            temperature={temperature} setTemperature={setTemperature}
            maxTokens={maxTokens} setMaxTokens={setMaxTokens}
            createThread={createThread} setCreateThread={setCreateThread}
            threadId={threadId}
            stream={stream} setStream={setStream}
            onSend={onSend} onCancel={onCancel} onClear={onClear}
          />

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <h2 className="font-semibold mb-2">События</h2>
              <EventsConsole events={events} />
            </div>
            <div>
              <h2 className="font-semibold mb-2">Ассистент</h2>
              <div className="min-h-64 p-3 border rounded bg-white whitespace-pre-wrap">{assistantText}</div>
            </div>
          </div>

          <div>
            <div className="flex items-center gap-2 mb-2">
              <h2 className="font-semibold">История треда</h2>
              {summarizing && (
                <span className="badge badge-ping">Summarizing… {summaryElapsed}s</span>
              )}
              <button className="px-2 py-1 border rounded disabled:opacity-50" disabled={!threadIdRef.current} onClick={()=>refreshHistory()}>Обновить</button>
            </div>
            <MessageView items={history?.messages || []} />
          </div>
        </>
      ) : tab === 'profile' ? (
        <ProfilePage />
      ) : (
        <div>
          <h2 className="font-semibold mb-2">Последний запрос бэка → LM Studio (JSON)</h2>
          <div className="mb-2 flex items-center gap-2">
            <button className="px-2 py-1 border rounded disabled:opacity-50" onClick={copyLastProviderRequest} disabled={!lastProviderRequest}>Копировать</button>
          </div>
          <pre className="p-3 border rounded bg-white overflow-auto whitespace-pre-wrap">{lastProviderRequest ? JSON.stringify(lastProviderRequest, null, 2) : 'Еще нет данных. Отправьте запрос.'}</pre>
        </div>
      )}
    </div>
  )
}
