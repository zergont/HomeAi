import React, { useEffect, useMemo, useState } from 'react'
import { getProfile, putProfile, type Profile } from '../lib/api'

const empty: Profile = {
  display_name: '', preferred_language: '', tone: '', timezone: '', region_coarse: '', work_hours: '',
  ui_format_prefs: {}, goals_mood: '', decisions_tasks: '', brevity: '', format_defaults: {}, interests_topics: [],
  workflow_tools: [], os: '', runtime: '', hardware_hint: '', source: '', confidence: 50,
}

export default function ProfilePage() {
  const [p, setP] = useState<Profile>(empty)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string|null>(null)

  const coreTokens = p.core_tokens ?? 0
  const coreCap = p.core_cap ?? Math.ceil(coreTokens * 1.1)

  useEffect(() => { load() }, [])

  const load = async () => {
    setLoading(true); setError(null)
    try {
      const data = await getProfile()
      setP({ ...empty, ...data })
    } catch (e: any) {
      setError(e?.message || 'Failed to load profile')
    } finally { setLoading(false) }
  }

  const save = async () => {
    setSaving(true); setError(null)
    try {
      const data = await putProfile(p)
      setP({ ...p, ...data })
    } catch (e: any) {
      setError(e?.message || 'Failed to save profile')
    } finally { setSaving(false) }
  }

  const reset = () => setP(empty)

  const onChange = (k: keyof Profile, v: any) => setP((prev: Profile) => ({ ...prev, [k]: v }))

  return (
    <div className="max-w-4xl mx-auto p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Profile</h1>
        <div className="flex gap-2">
          <button className="px-2 py-1 border rounded" onClick={load} disabled={loading}>Load</button>
          <button className="px-2 py-1 border rounded" onClick={save} disabled={saving}>Save</button>
          <button className="px-2 py-1 border rounded" onClick={reset}>Reset</button>
        </div>
      </div>
      {error && <div className="p-2 bg-red-100 border text-sm text-red-700">{error}</div>}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Section title="Basics">
          <Field label="Display name" value={p.display_name || ''} onChange={v=>onChange('display_name', v)} />
          <Field label="Preferred language" value={p.preferred_language || ''} onChange={v=>onChange('preferred_language', v)} />
          <Field label="Tone" value={p.tone || ''} onChange={v=>onChange('tone', v)} />
          <Field label="Timezone" value={p.timezone || ''} onChange={v=>onChange('timezone', v)} />
          <Field label="Region (coarse)" value={p.region_coarse || ''} onChange={v=>onChange('region_coarse', v)} />
          <Field label="Work hours" value={p.work_hours || ''} onChange={v=>onChange('work_hours', v)} />
          <Field label="Brevity" value={p.brevity || ''} onChange={v=>onChange('brevity', v)} />
        </Section>

        <Section title="System env">
          <Field label="OS" value={p.os || ''} onChange={v=>onChange('os', v)} />
          <Field label="Runtime" value={p.runtime || ''} onChange={v=>onChange('runtime', v)} />
          <Field label="Hardware hint" value={p.hardware_hint || ''} onChange={v=>onChange('hardware_hint', v)} />
          <Field label="Source" value={p.source || ''} onChange={v=>onChange('source', v)} />
          <Field label="Confidence" value={String(p.confidence ?? '')} onChange={v=>onChange('confidence', Number(v)||0)} />
        </Section>

        <Section title="Preferences (JSON)">
          <JsonField label="UI format prefs" value={p.ui_format_prefs} onChange={v=>onChange('ui_format_prefs', v)} />
          <JsonField label="Format defaults" value={p.format_defaults} onChange={v=>onChange('format_defaults', v)} />
        </Section>

        <Section title="Topics & Tools (JSON)">
          <JsonField label="Interests topics" value={p.interests_topics} onChange={v=>onChange('interests_topics', v)} />
          <JsonField label="Workflow tools" value={p.workflow_tools} onChange={v=>onChange('workflow_tools', v)} />
        </Section>

        <Section title="Goals & Decisions">
          <TextArea label="Goals / Mood" value={p.goals_mood || ''} onChange={v=>onChange('goals_mood', v)} />
          <TextArea label="Decisions / Tasks" value={p.decisions_tasks || ''} onChange={v=>onChange('decisions_tasks', v)} />
        </Section>
      </div>

      <div className="p-3 border rounded bg-white">
        <h3 className="font-semibold mb-2">Token preview</h3>
        <div className="text-sm">core_tokens: <b>{coreTokens}</b> | core_cap: <b>{coreCap}</b></div>
      </div>
    </div>
  )
}

function Section(props: { title: string; children: any }) {
  return (
    <div className="p-3 border rounded bg-white">
      <h3 className="font-semibold mb-2">{props.title}</h3>
      <div className="space-y-2">{props.children}</div>
    </div>
  )
}

function Field({ label, value, onChange }: { label: string; value: string; onChange: (v: string)=>void }) {
  return (
    <label className="block text-sm">
      <div className="text-gray-600 mb-1">{label}</div>
      <input className="w-full border rounded px-2 py-1" value={value} onChange={e=>onChange(e.target.value)} />
    </label>
  )
}

function TextArea({ label, value, onChange }: { label: string; value: string; onChange: (v: string)=>void }) {
  return (
    <label className="block text-sm">
      <div className="text-gray-600 mb-1">{label}</div>
      <textarea className="w-full border rounded px-2 py-1" rows={4} value={value} onChange={e=>onChange(e.target.value)} />
    </label>
  )
}

function JsonField({ label, value, onChange }: { label: string; value: any; onChange: (v: any)=>void }) {
  const [text, setText] = useState<string>(JSON.stringify(value ?? null, null, 2))
  useEffect(()=>{ setText(JSON.stringify(value ?? null, null, 2)) }, [value])
  const parse = (s: string) => {
    try { return JSON.parse(s) } catch { return s }
  }
  return (
    <label className="block text-sm">
      <div className="text-gray-600 mb-1">{label}</div>
      <textarea className="w-full border rounded px-2 py-1 font-mono text-xs" rows={6}
        value={text}
        onChange={e=>{ const v = e.target.value; setText(v); onChange(parse(v)) }}
      />
    </label>
  )
}
