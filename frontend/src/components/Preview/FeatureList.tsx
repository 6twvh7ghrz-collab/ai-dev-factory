import React from 'react'

interface FeatureListProps {
  features: string[]
}

export function FeatureList({ features }: FeatureListProps) {
  if (!features || features.length === 0) {
    return <p style={{ color: 'var(--text-muted)', fontSize: 13 }}>暂无功能列表</p>
  }

  return (
    <ul className="preview-list">
      {features.map((feature, idx) => (
        <li key={idx}>{feature}</li>
      ))}
    </ul>
  )
}
