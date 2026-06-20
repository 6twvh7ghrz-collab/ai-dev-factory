import { useParams } from 'react-router-dom'
import ModeToggle from '../../components/SimpleMode/ModeToggle'

export default function SimpleProjectCompletePage() {
  const { projectId } = useParams<{ projectId: string }>()

  return (
    <div>
      <div className="page-header">
        <h2>简化模式 - 完成验收</h2>
        <ModeToggle projectId={projectId ? Number(projectId) : null} />
      </div>
      <div className="card">
        <p style={{ color: 'var(--text-muted)' }}>项目 #{projectId} 的完成页面（开发中）</p>
      </div>
    </div>
  )
}
