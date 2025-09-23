const API_BASE = import.meta.env.VITE_API_BASE_URL as string

export async function postResponses(body: any): Promise<any> {
  const res = await fetch(`${API_BASE}/responses`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function streamResponses(body: any, onChunk: (ev: { event: string; data: any }) => void, signal?: AbortSignal) {
  const res = await fetch(`${API_BASE}/responses?stream=true`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
    body: JSON.stringify(body),
    signal,
  })
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  const { parseSSEChunks } = await import('./sseParser')
  const push = parseSSEChunks()
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    const text = decoder.decode(value, { stream: true })
    for (const ev of push(text)) onChunk(ev)
  }
}

export async function cancelResponse(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/responses/${id}/cancel`, { method: 'POST' })
  if (!res.ok) throw new Error(await res.text())
}

export type ThreadMessagesResponse = {
  thread_id: string
  summary?: string | null
  summary_updated_at?: string | null
  messages: { role: string; content: string; created_at: string }[]
  context?: { system: string; messages: { role: string; content: string }[] }
}

export async function getThreadMessages(threadId: string): Promise<ThreadMessagesResponse> {
  const res = await fetch(`${API_BASE}/threads/${threadId}/messages`)
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function getContextLength(model: string): Promise<number | null> {
  const res = await fetch(`${API_BASE}/providers/lmstudio/context-length?model=${encodeURIComponent(model)}`)
  if (!res.ok) return null
  const data = await res.json()
  return typeof data?.context_length === 'number' ? data.context_length : null
}

export type ApiConfig = {
  providers?: { lmstudio?: { base_url?: string } }
  profile?: any
}

export async function getConfig(): Promise<ApiConfig> {
  const res = await fetch(`${API_BASE}/config`)
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export type LMModelsResponse = { data?: Array<{ id: string; [k: string]: any }> }

export async function getLMStudioModels(): Promise<LMModelsResponse> {
  const res = await fetch(`${API_BASE}/providers/lmstudio/models`)
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export type LMV0Model = { id: string; state?: string; [k: string]: any }
export async function getLMStudioModelsV0(): Promise<{ data?: LMV0Model[] }> {
  const res = await fetch(`${API_BASE}/providers/lmstudio/models/v0`)
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export type ContextInfo = {
  model: string
  context_length: number | null
  loaded_context_length?: number | null
  max_context_length?: number | null
  source?: string | null
  ttl_sec?: number
  state?: string | null
  error?: string
}
export async function getContextInfo(model: string): Promise<ContextInfo> {
  const res = await fetch(`${API_BASE}/providers/lmstudio/context-length?model=${encodeURIComponent(model)}`)
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function getHealth(): Promise<{status: 'ok' | 'error'}> {
    const res = await fetch(`${API_BASE}/health`)
    if (!res.ok) throw new Error('API health check failed')
    return res.json()
}

export async function getLMStudioHealth(): Promise<{status: 'ok' | 'error'}> {
    const res = await fetch(`${API_BASE}/providers/lmstudio/health`)
    if (!res.ok) throw new Error('LM Studio health check failed')
    return res.json()
}

export type Profile = {
  display_name?: string | null
  preferred_language?: string | null
  tone?: string | null
  timezone?: string | null
  region_coarse?: string | null
  work_hours?: string | null
  ui_format_prefs?: any
  goals_mood?: string | null
  decisions_tasks?: string | null
  brevity?: string | null
  format_defaults?: any
  interests_topics?: any
  workflow_tools?: any
  os?: string | null
  runtime?: string | null
  hardware_hint?: string | null
  source?: string | null
  confidence?: number | null
  updated_at?: string | null
  core_tokens?: number
  core_cap?: number
}

export async function getProfile(): Promise<Profile> {
  const res = await fetch(`${API_BASE}/profile`)
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function putProfile(p: Profile): Promise<Profile> {
  const res = await fetch(`${API_BASE}/profile`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(p),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}
