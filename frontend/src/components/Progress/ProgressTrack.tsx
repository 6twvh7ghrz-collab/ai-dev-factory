import React from 'react'

interface ProgressTrackProps {
  percentage: number
  label?: string
}

export function ProgressTrack({ percentage, label }: ProgressTrackProps) {
  const clamped = Math.min(100, Math.max(0, percentage))

  return (
    <div>
      {label && (
        <div style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 6 }}>
          {label}
        </div>
      )}
      <div className="progress-bar-container">
        <div
          className="progress-bar-fill"
          style={{ width: `${clamped}%` }}
        >
          {clamped >= 12 && `${Math.round(clamped)}%`}
        </div>
      </div>
      {!label && (
        <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 4, textAlign: 'right' }}>
          {Math.round(clamped)}%
        </div>
      )}
    </div>
  )
}
