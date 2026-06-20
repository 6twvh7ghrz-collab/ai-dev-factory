import React from 'react'

interface ProjectStatsData {
  time?: string
  cost?: string
  includes?: string
  [key: string]: string | undefined
}

interface ProjectStatsProps {
  stats: ProjectStatsData
}

const STAT_LABELS: Record<string, string> = {
  time: '预计时间',
  cost: '预估成本',
  includes: '交付内容',
}

export function ProjectStats({ stats }: ProjectStatsProps) {
  if (!stats) {
    return <p style={{ color: 'var(--text-muted)', fontSize: 13 }}>暂无统计数据</p>
  }

  const entries = Object.entries(stats).filter(([, v]) => v !== undefined && v !== '')

  if (entries.length === 0) {
    return <p style={{ color: 'var(--text-muted)', fontSize: 13 }}>暂无统计数据</p>
  }

  return (
    <div className="progress-stats">
      {entries.map(([key, value]) => (
        <div key={key} className="progress-stat">
          <div className="progress-stat-value">{value}</div>
          <div className="progress-stat-label">
            {STAT_LABELS[key] || key}
          </div>
        </div>
      ))}
    </div>
  )
}
