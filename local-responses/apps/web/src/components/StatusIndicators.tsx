// apps/web/src/components/StatusIndicators.tsx
import React, { useEffect, useState } from 'react'
import { getHealth, getLMStudioHealth } from '../lib/api'

type Status = 'ok' | 'error' | 'pending'

const StatusDot = ({ status, title }: { status: Status, title: string }) => {
  const color = status === 'ok' ? 'bg-green-500' : status === 'error' ? 'bg-red-500' : 'bg-yellow-400'
  return (
    <div className="flex items-center gap-2" title={title}>
      <div className={`w-3 h-3 rounded-full ${color}`} />
      <span className="text-sm">{title}</span>
    </div>
  )
}

export default function StatusIndicators() {
  const [apiStatus, setApiStatus] = useState<Status>('pending')
  const [lmStatus, setLmStatus] = useState<Status>('pending')

  useEffect(() => {
    const check = async () => {
      try {
        await getHealth()
        setApiStatus('ok')
      } catch {
        setApiStatus('error')
      }
      try {
        const res = await getLMStudioHealth()
        setLmStatus(res.status)
      } catch {
        setLmStatus('error')
      }
    }
    check()
    const interval = setInterval(check, 15000) // check every 15s
    return () => clearInterval(interval)
  }, [])

  return (
    <div className="flex items-center gap-4 p-2 bg-gray-50 rounded-lg">
      <StatusDot status={apiStatus} title="Backend API" />
      <StatusDot status={lmStatus} title="Model Server" />
    </div>
  )
}