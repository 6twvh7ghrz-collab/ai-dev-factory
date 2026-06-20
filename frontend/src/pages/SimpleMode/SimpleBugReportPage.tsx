import { useState, useEffect } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import ModeToggle from '../../components/SimpleMode/ModeToggle'
import { createBug, type Bug } from '../../api/bugs'

export default function SimpleBugReportPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()
  const pid = projectId ? Number(projectId) : null

  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [expectedResult, setExpectedResult] = useState('')
  const [actualResult, setActualResult] = useState('')
  const [errorMessage, setErrorMessage] = useState('')
  const [reproductionSteps, setReproductionSteps] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submittedBug, setSubmittedBug] = useState<Bug | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showAdvanced, setShowAdvanced] = useState(false)

  useEffect(() => {
    if (!pid || isNaN(pid)) {
      setError('无效的项目 ID')
    }
  }, [pid])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!pid) return
    if (!title.trim()) {
      setError('请填写 Bug 标题')
      return
    }
    if (!description.trim()) {
      setError('请描述遇到的问题')
      return
    }

    setSubmitting(true)
    setError(null)

    try {
      const res = await createBug(pid, {
        title: title.trim(),
        description: description.trim(),
        expected_result: expectedResult.trim() || undefined,
        actual_result: actualResult.trim() || undefined,
        error_message: errorMessage.trim() || undefined,
        reproduction_steps: reproductionSteps.trim() || undefined,
      })

      if (res.ok && res.data) {
        setSubmittedBug(res.data)
      } else {
        setError(res.message || '提交失败，请重试')
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '提交失败，请检查网络连接'
      setError(msg)
    } finally {
      setSubmitting(false)
    }
  }

  // 提交成功后显示确认页
  if (submittedBug) {
    return (
      <div>
        <div className="page-header">
          <h2>简化模式 - Bug 已提交</h2>
          <ModeToggle projectId={pid} />
        </div>
        <div className="card" style={{ textAlign: 'center', padding: '3rem 2rem' }}>
          <div style={{ fontSize: '3rem', marginBottom: '1rem' }}>✅</div>
          <h3 style={{ marginBottom: '0.5rem' }}>Bug 已成功提交！</h3>
          <p style={{ color: 'var(--text-muted)', marginBottom: '0.25rem' }}>
            Bug #{submittedBug.id}: {submittedBug.title}
          </p>
          <p style={{ color: 'var(--text-muted)', fontSize: '0.875rem', marginBottom: '2rem' }}>
            状态: <span style={{ color: 'var(--accent)' }}>{submittedBug.status || '待处理'}</span>
          </p>
          <div style={{ display: 'flex', gap: '0.75rem', justifyContent: 'center', flexWrap: 'wrap' }}>
            <button
              className="btn btn-primary"
              onClick={() => {
                setSubmittedBug(null)
                setTitle('')
                setDescription('')
                setExpectedResult('')
                setActualResult('')
                setErrorMessage('')
                setReproductionSteps('')
              }}
            >
              📝 报告另一个 Bug
            </button>
            <button
              className="btn btn-secondary"
              onClick={() => navigate(`/simple/projects/${pid}/complete`)}
            >
              📋 返回完成页
            </button>
            <Link
              to={`/project/${pid}`}
              className="btn btn-secondary"
              style={{ textDecoration: 'none' }}
            >
              🔧 切换到专业模式
            </Link>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div>
      <div className="page-header">
        <h2>简化模式 - 报告 Bug</h2>
        <ModeToggle projectId={pid} />
      </div>

      <div className="card" style={{ marginBottom: '1rem' }}>
        <p style={{ margin: 0, color: 'var(--text-muted)', fontSize: '0.9rem' }}>
          💡 发现项目有问题？告诉我们发生了什么，AI 会自动分析并提供修复方案。
        </p>
      </div>

      {error && (
        <div className="card" style={{ borderColor: 'var(--danger)', marginBottom: '1rem' }}>
          <p style={{ color: 'var(--danger)', margin: 0 }}>❌ {error}</p>
        </div>
      )}

      <form onSubmit={handleSubmit}>
        <div className="card" style={{ marginBottom: '1rem' }}>
          <div className="form-group">
            <label className="form-label" htmlFor="bug-title">
              Bug 标题 <span style={{ color: 'var(--danger)' }}>*</span>
            </label>
            <input
              id="bug-title"
              className="form-input"
              type="text"
              placeholder="简短描述问题，例如：点击'保存'按钮后页面卡死"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              disabled={submitting}
              autoFocus
            />
          </div>

          <div className="form-group">
            <label className="form-label" htmlFor="bug-desc">
              问题描述 <span style={{ color: 'var(--danger)' }}>*</span>
            </label>
            <textarea
              id="bug-desc"
              className="form-input"
              rows={4}
              placeholder="详细描述你遇到的问题..."
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              disabled={submitting}
            />
          </div>

          <div className="form-group">
            <label className="form-label" htmlFor="bug-actual">
              实际结果
            </label>
            <textarea
              id="bug-actual"
              className="form-input"
              rows={2}
              placeholder="实际发生了什么？（例如：页面卡死不动）"
              value={actualResult}
              onChange={(e) => setActualResult(e.target.value)}
              disabled={submitting}
            />
          </div>

          <div className="form-group">
            <label className="form-label" htmlFor="bug-expected">
              期望结果
            </label>
            <textarea
              id="bug-expected"
              className="form-input"
              rows={2}
              placeholder="你期望发生什么？（例如：点击保存后数据成功存储）"
              value={expectedResult}
              onChange={(e) => setExpectedResult(e.target.value)}
              disabled={submitting}
            />
          </div>
        </div>

        {/* 高级选项 */}
        <div className="card" style={{ marginBottom: '1rem' }}>
          <button
            type="button"
            className="btn btn-text"
            onClick={() => setShowAdvanced(!showAdvanced)}
            style={{ padding: 0, marginBottom: showAdvanced ? '1rem' : 0 }}
          >
            {showAdvanced ? '🔼 收起' : '🔽 展开'} 高级选项（错误信息、复现步骤）
          </button>

          {showAdvanced && (
            <>
              <div className="form-group">
                <label className="form-label" htmlFor="bug-error">
                  错误信息（如有）
                </label>
                <textarea
                  id="bug-error"
                  className="form-input"
                  rows={3}
                  placeholder="粘贴控制台或页面上的错误信息..."
                  value={errorMessage}
                  onChange={(e) => setErrorMessage(e.target.value)}
                  disabled={submitting}
                  style={{ fontFamily: 'monospace', fontSize: '0.85rem' }}
                />
              </div>

              <div className="form-group">
                <label className="form-label" htmlFor="bug-steps">
                  复现步骤
                </label>
                <textarea
                  id="bug-steps"
                  className="form-input"
                  rows={3}
                  placeholder="列出复现步骤：&#10;1. 打开页面...&#10;2. 点击...&#10;3. 观察..."
                  value={reproductionSteps}
                  onChange={(e) => setReproductionSteps(e.target.value)}
                  disabled={submitting}
                />
              </div>
            </>
          )}
        </div>

        {/* 操作按钮 */}
        <div style={{ display: 'flex', gap: '0.75rem', justifyContent: 'flex-end' }}>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => navigate(`/simple/projects/${pid}/complete`)}
            disabled={submitting}
          >
            ← 返回
          </button>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={submitting || !pid}
          >
            {submitting ? '提交中...' : '🐛 提交 Bug'}
          </button>
        </div>
      </form>
    </div>
  )
}
