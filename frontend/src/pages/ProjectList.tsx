import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { listProjects, type Project } from '../api/projects'

const STATUS_LABELS: Record<string, string> = {
  draft: '草稿',
  analyzing: '分析中',
  generated: '方案已生成',
  developing: '开发中',
  testing: '测试中',
  completed: '已完成',
  paused: '已暂停',
}

export default function ProjectList() {
  const navigate = useNavigate()
  const [projects, setProjects] = useState<Project[]>([])
  const [loading, setLoading] = useState(true)

  const fetchProjects = async () => {
    setLoading(true)
    try {
      const res = await listProjects()
      if (res.ok) setProjects(res.data)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { fetchProjects() }, [])

  return (
    <div>
      <div className="page-header">
        <h2>我的项目</h2>
        <button className="btn btn-primary" onClick={() => navigate('/create')}>
          + 创建新项目
        </button>
      </div>

      {loading ? (
        <div className="loading"><div className="spinner" />加载中...</div>
      ) : projects.length === 0 ? (
        <div className="empty-state">
          <p>还没有项目，点击上方按钮创建第一个项目</p>
        </div>
      ) : (
        <div>
          {projects.map((p) => (
            <div
              key={p.id}
              className="card project-card"
              onClick={() => navigate(`/project/${p.id}`)}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div>
                  <h3 style={{ fontSize: 16, marginBottom: 4 }}>{p.name}</h3>
                  <p style={{ fontSize: 13, color: 'var(--text-muted)', margin: 0 }}>
                    {p.idea || '暂无描述'}
                  </p>
                </div>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  <span className={`badge badge-${p.status}`}>
                    {STATUS_LABELS[p.status] || p.status}
                  </span>
                  <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                    {new Date(p.updated_at).toLocaleDateString()}
                  </span>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
