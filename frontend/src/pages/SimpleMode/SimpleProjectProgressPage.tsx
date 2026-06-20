import { useParams } from 'react-router-dom'
import ModeToggle from '../../components/SimpleMode/ModeToggle'

export default function SimpleProjectProgressPage() {
  const { projectId } = useParams<{ projectId: string }>()

  return (
    <div>
      <div className="page-header">
        <h2>简化模式 - 开发进度</h2>
        <ModeToggle projectId={projectId ? Number(projectId) : null} />
      </div>
      <div className="card">
        <p style={{ color: 'var(--text-muted)' }}>项目 #{projectId} 的进度页面（开发中）</p>
      </div>
    </div>
  )
}
