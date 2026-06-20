import React, { type ReactNode } from 'react'

interface ProjectCardProps {
  title: string
  children: ReactNode
}

export function ProjectCard({ title, children }: ProjectCardProps) {
  return (
    <div className="preview-section">
      <h4>{title}</h4>
      <div className="preview-card">{children}</div>
    </div>
  )
}
