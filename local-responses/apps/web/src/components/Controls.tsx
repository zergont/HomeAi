import React, { useEffect, useMemo, useState } from 'react'
import { getLMStudioModelsV0, getContextInfo, type LMV0Model } from '../lib/api'

type Props = {
  model: string; setModel: (v:string)=>void
  system: string; setSystem: (v:string)=>void
  input: string; setInput: (v:string)=>void
  temperature: number; setTemperature: (v:number)=>void
  maxTokens: number; setMaxTokens: (v:number)=>void
  createThread: boolean; setCreateThread: (v:boolean)=>void
  threadId: string | null
  stream: boolean; setStream: (v:boolean)=>void
  onSend: ()=>void
  onCancel: ()=>void
  onClear: ()=>void
}

export default function Controls(p: Props) {
  const [models, setModels] = useState<LMV0Model[]>([])
  const [modelsErr, setModelsErr] = useState<string|null>(null)
  const [loading, setLoading] = useState<boolean>(false)

  const [ctxLoaded, setCtxLoaded] = useState<number|null>(null)
  const [ctxMax, setCtxMax] = useState<number|null>(null)
  const [state, setState] = useState<string| null>(null)

  const selectedId = p.model

  // начальная загрузка списка моделей
  useEffect(() => {
    let cancelled = false
    async function load() {
      setLoading(true); setModelsErr(null)
      try {
        const res = await getLMStudioModelsV0()
        if (!cancelled) setModels(res.data || [])
      } catch (e:any) {
        if (!cancelled) setModelsErr('Не удалось получить модели')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
  }, [])

  // автообновление списка моделей каждые 10 секунд (без индикации загрузки)
  useEffect(() => {
    let cancelled = false
    const tick = async () => {
      try {
        const res = await getLMStudioModelsV0()
        if (!cancelled) setModels(res.data || [])
      } catch {}
    }
    const id = setInterval(tick, 10000)
    // сразу один мягкий опрос через 2с (после возможной автозагрузки модели)
    const once = setTimeout(tick, 2000)
    return () => { cancelled = true; clearInterval(id); clearTimeout(once) }
  }, [])

  // автообновление статуса выбранной модели, пока не loaded или нет числа контекста
  useEffect(() => {
    let cancelled = false
    let timer: any
    async function refresh(loop = true) {
      if (!selectedId) return
      try {
        const info = await getContextInfo(selectedId)
        if (cancelled) return
        const st = info.state || null
        const cl = typeof info.context_length === 'number' ? info.context_length : null
        const mx = typeof info.max_context_length === 'number' ? info.max_context_length : null
        setState(st)
        setCtxLoaded(cl)
        setCtxMax(mx)
        if (loop && (st !== 'loaded' || cl == null)) {
          timer = setTimeout(() => refresh(true), 3000)
        }
      } catch {
        if (!cancelled && loop) {
          timer = setTimeout(() => refresh(true), 5000)
        }
      }
    }
    // стартуем цикл при выборе модели
    setState(null); setCtxLoaded(null); setCtxMax(null)
    refresh(true)
    return () => { cancelled = true; if (timer) clearTimeout(timer) }
  }, [selectedId])

  // подсказка обновить сразу после смены треда (обычно после первого запроса к незагруженной модели)
  useEffect(() => {
    let cancelled = false
    async function once() {
      if (!selectedId || !p.threadId) return
      try { const info = await getContextInfo(selectedId); if (!cancelled) {
        setState(info.state || null)
        setCtxLoaded(typeof info.context_length === 'number' ? info.context_length : null)
        setCtxMax(typeof info.max_context_length === 'number' ? info.max_context_length : null)
      }} catch {}
    }
    once()
    return () => { cancelled = true }
  }, [p.threadId])

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <label className="block text-sm">Model
          <select
            className="mt-1 w-full border rounded p-2"
            value={selectedId || ''}
            onChange={e => p.setModel(e.target.value || '')}
          >
            <option value="" disabled>{loading ? 'Загрузка…' : (modelsErr ? 'Не удалось загрузить' : 'Выберите модель')}</option>
            {models.map(m => (
              <option key={m.id} value={m.id} className={m.state !== 'loaded' ? 'text-gray-400' : ''}>
                {m.id}{m.state !== 'loaded' ? ' (not-loaded)' : ''}
              </option>
            ))}
          </select>
          <div className="text-xs text-gray-500 mt-1">
            {selectedId ? (
              state === 'loaded' && typeof ctxLoaded === 'number'
                ? `Loaded context: ~${ctxLoaded} tokens` + (typeof ctxMax === 'number' ? ` • Max: ${ctxMax}` : '')
                : state && state !== 'loaded'
                  ? 'Модель не загружена'
                  : 'Context: …'
            ) : 'Context: —'}
          </div>
        </label>
        <label className="block text-sm">Max output tokens
          <input type="number" className="mt-1 w-full border rounded p-2" value={p.maxTokens} onChange={e=>p.setMaxTokens(parseInt(e.target.value||'0'))} />
        </label>
      </div>
      <label className="block text-sm">System (optional)
        <textarea className="mt-1 w-full border rounded p-2 h-24" value={p.system} onChange={e=>p.setSystem(e.target.value)} />
      </label>
      <label className="block text-sm">Input
        <textarea className="mt-1 w-full border rounded p-2 h-32" value={p.input} onChange={e=>p.setInput(e.target.value)} />
      </label>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3 items-center">
        <label className="block text-sm">Temperature: {p.temperature.toFixed(2)}
          <input type="range" min={0} max={2} step={0.1} value={p.temperature} onChange={e=>p.setTemperature(parseFloat(e.target.value))} className="w-full" />
        </label>
        <label className="inline-flex items-center gap-2"><input type="checkbox" checked={p.createThread} onChange={e=>p.setCreateThread(e.target.checked)} /> Create thread</label>
        <label className="inline-flex items-center gap-2"><input type="checkbox" checked={p.stream} onChange={e=>p.setStream(e.target.checked)} /> Stream</label>
      </div>
      <div className="flex items-center gap-2">
        <button className="px-3 py-2 bg-blue-600 text-white rounded" onClick={p.onSend} disabled={!p.model}>Send</button>
        <button className="px-3 py-2 bg-red-600 text-white rounded" onClick={p.onCancel}>Cancel</button>
        <button className="px-3 py-2 bg-gray-500 text-white rounded" onClick={p.onClear}>Clear</button>
        <div className="ml-auto text-sm">thread_id: <input readOnly className="border rounded p-1 w-72" value={p.threadId || ''} onFocus={e=>e.currentTarget.select()} /></div>
      </div>
    </div>
  )
}
