import { useState, useEffect, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import ModeToggle from '../../components/SimpleMode/ModeToggle'
import { getProject, updateRequirements, type ProjectDetail } from '../../api/projects'
import { analyzeRequirements, getAnalysis, generateModules, generateTasks } from '../../api/analysis'

type PreviewStage = 'loading' | 'ready' | 'analyzing' | 'analysis_done' | 'generating_modules' | 'generating_tasks' | 'done' | 'error'

export default function SimpleProjectPreviewPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()
  const pid = projectId ? Number(projectId) : 0

  const [project, setProject] = useState<ProjectDetail | null>(null)
  const [analysisData, setAnalysisData] = useState<Record<string, any> | null>(null)
  const [stage, setStage] = useState<PreviewStage>('loading')
  const [error, setError] = useState<string | null>(null)
  const [errorDetail, setErrorDetail] = useState<string | null>(null)
  const [toast, setToast] = useState<{ msg: string; type: 'success' | 'error' } | null>(null)

  const showToast = (msg: string, type: 'success' | 'error' = 'success') => {
    setToast({ msg, type })
    setTimeout(() => setToast(null), 4000)
  }

  // 加载项目和分析数据
  const loadData = useCallback(async () => {
    if (!pid) return
    setStage('loading')
    setError(null)
    setErrorDetail(null)

    try {
      // 加载项目
      const projRes = await getProject(pid)
      if (!projRes.ok) {
        setError('项目不存在')
        setErrorDetail(projRes.error?.detail || '无法加载项目信息')
        setStage('error')
        return
      }
      setProject(projRes.data)

      // 尝试加载已有分析
      try {
        const analysisRes = await getAnalysis(pid)
        if (analysisRes.ok && analysisRes.data) {
          setAnalysisData(analysisRes.data)
          setStage('analysis_done')
          return
        }
      } catch {
        // 没有分析结果，需要触发分析
      }

      setStage('ready')
    } catch (err: any) {
      setError('网络错误')
      setErrorDetail(err?.message || '请检查后端服务是否运行')
      setStage('error')
    }
  }, [pid])

  useEffect(() => {
    loadData()
  }, [loadData])

  // 触发AI分析
  const handleAnalyze = async () => {
    if (!pid || stage === 'analyzing') return
    setStage('analyzing')
    setError(null)
    setErrorDetail(null)

    try {
      const res = await analyzeRequirements(pid)
      if (res.ok) {
        setAnalysisData(res.data as Record<string, any>)
        setStage('analysis_done')
        showToast('AI分析完成！')
        // 刷新项目状态
        const projRes = await getProject(pid)
        if (projRes.ok) setProject(projRes.data)
      } else {
        const detail = res.error?.detail || '分析失败'
        if (detail.includes('未配置') || detail.includes('API Key')) {
          setError('AI 未配置')
        } else if (detail.includes('超时')) {
          setError('AI 请求超时')
        } else {
          setError('AI 分析失败')
        }
        setErrorDetail(detail)
        setStage('error')
      }
    } catch (err: any) {
      const msg = err?.response?.data?.error?.detail || err?.message || '网络错误'
      setError('AI 服务调用失败')
      setErrorDetail(msg)
      setStage('error')
    }
  }

  // 重新分析（修改需求后）
  const handleReAnalyze = async () => {
    setStage('ready')
    setAnalysisData(null)
    // 稍等后触发分析
    setTimeout(() => handleAnalyze(), 300)
  }

  // 确认方案并生成模块和任务
  const handleConfirmAndGenerate = async () => {
    if (!pid) return
    setStage('generating_modules')
    setError(null)
    setErrorDetail(null)

    try {
      // 1. 生成模块
      const modulesRes = await generateModules(pid)
      if (!modulesRes.ok) {
        throw new Error(modulesRes.error?.detail || '模块生成失败')
      }

      setStage('generating_tasks')

      // 2. 生成任务
      const tasksRes = await generateTasks(pid)
      if (!tasksRes.ok) {
        throw new Error(tasksRes.error?.detail || '任务生成失败')
      }

      setStage('done')
      showToast('方案已确认，模块和开发任务已生成！')

      // 3. 验证数据库确实保存了任务
      // （generateTasks API本身已经保存到数据库，这里不做额外验证请求）

      // 任务生成成功，但真实进度页尚未开发完成
      // 暂时不自动跳转，显示完成提示
      setStage('done')
      showToast('开发任务已生成！')

    } catch (err: any) {
      const msg = err?.message || '生成失败'
      setError('生成失败')
      setErrorDetail(msg)
      setStage('error')
    }
  }

  if (stage === 'loading') {
    return (
      <div>
        <div className="page-header">
          <h2>项目预览</h2>
          <ModeToggle projectId={pid || null} />
        </div>
        <div className="loading"><div className="spinner" />加载项目信息...</div>
      </div>
    )
  }

  return (
    <div>
      {toast && <div className={`toast toast-${toast.type}`}>{toast.msg}</div>}

      <div className="page-header">
        <div>
          <h2>📋 项目预览</h2>
          {project && (
            <p style={{ color: 'var(--text-muted)', fontSize: 13, marginTop: 4 }}>
              {project.name}
            </p>
          )}
        </div>
        <ModeToggle projectId={pid || null} />
      </div>

      {/* 未分析状态 */}
      {(stage === 'ready' || stage === 'analyzing') && (
        <div className="card" style={{ textAlign: 'center', padding: 40 }}>
          <h3 style={{ marginBottom: 12, color: 'var(--text-primary)' }}>
            {stage === 'analyzing' ? 'AI 正在分析你的需求...' : '准备分析需求'}
          </h3>
          <p style={{ color: 'var(--text-muted)', marginBottom: 20 }}>
            {stage === 'analyzing'
              ? 'AI 正在分析软件想法、目标用户、核心功能和项目风险，请稍候...'
              : '点击下方按钮，AI 将自动分析你的需求并生成方案预览'}
          </p>
          {stage === 'analyzing' ? (
            <div className="loading">
              <div className="spinner" />
              <span>分析中，通常需要 10-30 秒...</span>
            </div>
          ) : (
            <button className="btn btn-primary" onClick={handleAnalyze}>
              🤖 开始 AI 分析
            </button>
          )}
        </div>
      )}

      {/* 分析结果预览 */}
      {(stage === 'analysis_done' || stage === 'generating_modules' || stage === 'generating_tasks' || stage === 'done') && analysisData && (
        <div>
          {/* 1. 产品定义 */}
          <div className="preview-section">
            <h4>💡 产品定义</h4>
            <div className="card">
              {analysisData.product_definition && (
                <p style={{ fontSize: 15, fontWeight: 500, color: 'var(--text-primary)', marginBottom: 12 }}>
                  {analysisData.product_definition}
                </p>
              )}
              {analysisData.problem && (
                <div style={{ marginTop: 8 }}>
                  <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>解决的核心问题：</span>
                  <p style={{ fontSize: 14, color: 'var(--text-secondary)' }}>{analysisData.problem}</p>
                </div>
              )}
            </div>
          </div>

          {/* 2. 目标用户 + 核心价值 */}
          <div className="preview-grid">
            {analysisData.target_users?.length > 0 && (
              <div className="preview-section">
                <h4>👥 目标用户</h4>
                <div className="card">
                  <ul className="preview-list">
                    {analysisData.target_users.map((u: string, i: number) => (
                      <li key={i}>{u}</li>
                    ))}
                  </ul>
                </div>
              </div>
            )}
            {analysisData.core_value?.length > 0 && (
              <div className="preview-section">
                <h4>⭐ 核心价值</h4>
                <div className="card">
                  <ul className="preview-list">
                    {analysisData.core_value.map((v: string, i: number) => (
                      <li key={i}>{v}</li>
                    ))}
                  </ul>
                </div>
              </div>
            )}
          </div>

          {/* 3. 模块概览 */}
          {analysisData.modules?.length > 0 && (
            <div className="preview-section">
              <h4>📦 系统模块</h4>
              <div className="preview-grid">
                {analysisData.modules.map((m: any, i: number) => (
                  <div key={i} className="preview-card">
                    <h5>{m.name}</h5>
                    <p>{m.description || ''}</p>
                    {m.features?.length > 0 && (
                      <ul className="preview-list" style={{ marginTop: 8 }}>
                        {m.features.map((f: string, j: number) => (
                          <li key={j}>{f}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* 4. 开发风险（简化显示） */}
          {analysisData.risks?.length > 0 && (
            <div className="preview-section">
              <h4>⚠️ 需要注意</h4>
              <div className="card">
                <ul className="preview-list">
                  {analysisData.risks.map((r: string, i: number) => (
                    <li key={i}>{r}</li>
                  ))}
                </ul>
              </div>
            </div>
          )}

          {/* 5. 技术方案概览 */}
          <div className="preview-section">
            <h4>🔧 技术方案</h4>
            <div className="preview-grid">
              <div className="preview-card">
                <h5>前端</h5>
                <p>React + TypeScript（现代化Web界面）</p>
              </div>
              <div className="preview-card">
                <h5>后端</h5>
                <p>Python + FastAPI（高性能API服务）</p>
              </div>
              <div className="preview-card">
                <h5>数据库</h5>
                <p>SQLite（轻量级数据存储）</p>
              </div>
              <div className="preview-card">
                <h5>AI 能力</h5>
                <p>已配置的 AI 模型</p>
              </div>
            </div>
          </div>

          {/* 6. 工作量评估 */}
          <div className="preview-section">
            <h4>⏱️ 工作量评估</h4>
            <div className="card">
              {stage === 'done' ? (
                <p style={{ color: 'var(--text-secondary)' }}>
                  开发任务已生成，请前往进度页面查看详细评估。
                </p>
              ) : (
                <p style={{ color: 'var(--text-muted)' }}>
                  待任务拆解后评估
                </p>
              )}
            </div>
          </div>

          {/* 操作按钮 */}
          <div className="card" style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
            {stage === 'analysis_done' ? (
              <>
                <button
                  className="btn btn-primary"
                  onClick={handleConfirmAndGenerate}
                >
                  ✅ 确认方案并生成开发任务
                </button>
                <button
                  className="btn btn-secondary"
                  onClick={handleReAnalyze}
                >
                  🔄 重新分析
                </button>
                {pid && (
                  <button
                    className="btn btn-secondary"
                    onClick={() => navigate(`/simple/projects/${pid}/preview?edit=1`)}
                  >
                    ✏️ 修改需求
                  </button>
                )}
                {pid && (
                  <button
                    className="btn btn-secondary"
                    onClick={() => navigate(`/project/${pid}`)}
                  >
                    ⚙️ 切换到专业模式
                  </button>
                )}
              </>
            ) : stage === 'generating_modules' ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, width: '100%' }}>
                <div className="spinner" style={{ width: 20, height: 20 }} />
                <span>正在生成模块规划...</span>
              </div>
            ) : stage === 'generating_tasks' ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, width: '100%' }}>
                <div className="spinner" style={{ width: 20, height: 20 }} />
                <span>正在生成开发任务...</span>
              </div>
            ) : stage === 'done' ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12, width: '100%' }}>
                <div style={{ color: 'var(--success)', fontWeight: 600, fontSize: 15 }}>
                  ✅ 开发任务已生成！
                </div>
                <p style={{ color: 'var(--text-secondary)', fontSize: 13, margin: 0 }}>
                  真实进度页面将在下一阶段完成。你可以先前往专业模式查看全部任务。
                </p>
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  {pid && (
                    <button className="btn btn-primary" onClick={() => navigate(`/project/${pid}`)}>
                      ⚙️ 查看专业模式任务
                    </button>
                  )}
                  <button className="btn btn-secondary" onClick={() => window.location.reload()}>
                    🔄 返回项目预览
                  </button>
                </div>
              </div>
            ) : null}
          </div>
        </div>
      )}

      {/* 错误状态 */}
      {stage === 'error' && (
        <div className="card" style={{ textAlign: 'center', padding: 40, borderColor: 'var(--danger)' }}>
          <h3 style={{ color: 'var(--danger)', marginBottom: 8 }}>
            {error || '出错了'}
          </h3>
          {errorDetail && (
            <p style={{ color: 'var(--text-secondary)', fontSize: 14, marginBottom: 20, maxWidth: 500, margin: '0 auto 20px' }}>
              {errorDetail}
            </p>
          )}
          <div style={{ display: 'flex', gap: 8, justifyContent: 'center', flexWrap: 'wrap' }}>
            <button className="btn btn-primary" onClick={handleAnalyze}>
              重试
            </button>
            <button className="btn btn-secondary" onClick={() => navigate(`/simple/new`)}>
              返回需求输入
            </button>
            {pid && (
              <button
                className="btn btn-secondary"
                onClick={() => navigate(`/project/${pid}`)}
              >
                切换到专业模式
              </button>
            )}
            <button
              className="btn btn-secondary"
              onClick={() => navigate('/settings')}
            >
              检查AI配置
            </button>
          </div>
          {errorDetail && (
            <details style={{ marginTop: 16, textAlign: 'left' }}>
              <summary style={{ color: 'var(--text-muted)', fontSize: 12, cursor: 'pointer' }}>
                查看详细错误
              </summary>
              <pre style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 8, whiteSpace: 'pre-wrap' }}>
                {errorDetail}
              </pre>
            </details>
          )}
        </div>
      )}
    </div>
  )
}
