import React from 'react'

interface TimelineStep {
  id: string
  name: string
  completed: boolean
  timestamp?: Date | null
}

interface StatusTimelineProps {
  steps: TimelineStep[]
  currentStep?: string
}

export function StatusTimeline({ steps, currentStep }: StatusTimelineProps) {
  if (!steps || steps.length === 0) {
    return <p style={{ color: 'var(--text-muted)', fontSize: 13 }}>暂无步骤</p>
  }

  return (
    <div className="timeline">
      {steps.map((step, idx) => {
        const isActive = currentStep ? step.id === currentStep : idx === steps.findIndex(s => !s.completed)
        const isDone = step.completed
        let itemClass = 'timeline-item'
        if (isDone) itemClass += ' done'
        if (isActive && !isDone) itemClass += ' active'

        return (
          <div key={step.id} className={itemClass}>
            <div className="timeline-dot" />
            <div className="timeline-label">{step.name}</div>
            {step.timestamp && (
              <div className="timeline-desc">
                {new Date(step.timestamp).toLocaleTimeString('zh-CN', {
                  hour: '2-digit',
                  minute: '2-digit',
                  second: '2-digit',
                })}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
