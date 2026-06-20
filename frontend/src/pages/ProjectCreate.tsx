import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { createProject } from '../api/projects'

export default function ProjectCreate() {
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [idea, setIdea] = useState('')
  const [loading, setLoading] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) return

    setLoading(true)
    try {
      const res = await createProject({ name: name.trim(), idea: idea.trim() || undefined })
      if (res.ok) {
        navigate(`/project/${res.data.id}`)
      } else {
        alert(res.error?.detail || '创建失败')
      }
    } catch {
      alert('网络错误')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ maxWidth: 600 }}>
      <div className="page-header">
        <h2>创建新项目</h2>
      </div>

      <div className="card">
        <form onSubmit={handleSubmit}>
          <div className="form-group">
            <label>项目名称 *</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="例如：商品采集与发布系统"
              autoFocus
            />
          </div>

          <div className="form-group">
            <label>软件想法</label>
            <textarea
              value={idea}
              onChange={(e) => setIdea(e.target.value)}
              placeholder="用一句话描述你想做的软件，例如：我想做一个商品采集、图片处理和多平台发布的软件"
              rows={4}
            />
            <p style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 4 }}>
              支持只输入一句话想法，AI 会自动补充分析
            </p>
          </div>

          <div style={{ display: 'flex', gap: 12 }}>
            <button type="submit" className="btn btn-primary" disabled={loading || !name.trim()}>
              {loading ? '创建中...' : '创建项目'}
            </button>
            <button type="button" className="btn btn-secondary" onClick={() => navigate('/')}>
              取消
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
