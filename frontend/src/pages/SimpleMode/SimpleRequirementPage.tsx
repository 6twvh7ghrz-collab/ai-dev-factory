import { useState, useRef, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import ModeToggle from '../../components/SimpleMode/ModeToggle'
import { createProject, updateRequirements, listProjects, type Project } from '../../api/projects'
import { generateSmartQuestions, type SmartQuestion } from '../../api/smartQuestions'
import api from '../../api/client'

/** AI 状态 */
type AiStatus = 'connected' | 'not_configured' | 'error' | 'unknown'
type ExecutorStatus = 'running' | 'paused' | 'idle' | 'stopped' | 'unknown'

/** 对话消息类型（保留原有流程） */
interface ChatMessage {
  role: 'system' | 'user'
  content: string
  quickOptions?: string[]
  questionIndex?: number
}

/** 流程步骤 */
type FlowStep = 'home' | 'greeting' | 'collecting' | 'asking_questions' | 'answering' | 'confirming' | 'saving'

export default function SimpleRequirementPage() {
  const navigate = useNavigate()
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const isComposing = useRef(false)

  // 页面状态
  const [aiStatus, setAiStatus] = useState<AiStatus>('unknown')
  const [executorStatus, setExecutorStatus] = useState<ExecutorStatus>('unknown')
  const [recentProjects, setRecentProjects] = useState<Project[]>([])
  const [projectsLoading, setProjectsLoading] = useState(true)
  const [statusLoading, setStatusLoading] = useState(true)

  // 输入状态
  const [requirementText, setRequirementText] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 对话状态（保留原有流程）
  const [flowStep, setFlowStep] = useState<FlowStep>('home')
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [loading, setLoading] = useState(false)
  const [collectedIdea, setCollectedIdea] = useState('')
  const [questions, setQuestions] = useState<SmartQuestion[]>([])
  const [currentQuestionIndex, setCurrentQuestionIndex] = useState(0)
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [aiSummary, setAiSummary] = useState('')
  const [projectName, setProjectName] = useState('')

  // ========================
  // 状态检查
  // ========================
  useEffect(() => {
    checkStatus()
    loadRecentProjects()
  }, [])

  const checkStatus = async () => {
    setStatusLoading(true)
    try {
      // AI 状态：通过 settings/ai 接口判断（api client baseURL 已是 /api）
      try {
        const res = await api.get('/settings/ai')
        if (res.data?.ok && res.data?.data?.length > 0) {
          setAiStatus('connected')
        } else {
          setAiStatus('not_configured')
        }
      } catch {
        setAiStatus('error')
      }

      // 执行器状态：通过 health 接口判断
      try {
        const healthRes = await api.get('/health')
        if (healthRes.data?.ok) {
          setExecutorStatus('idle')
        } else {
          setExecutorStatus('unknown')
        }
      } catch {
        setExecutorStatus('unknown')
      }
    } finally {
      setStatusLoading(false)
    }
  }

  // ========================
  // 最近项目
  // ========================
  const loadRecentProjects = async () => {
    setProjectsLoading(true)
    try {
      const res = await listProjects()
      if (res.ok && res.data) {
        const sorted = [...res.data]
          .sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime())
          .slice(0, 3)
        setRecentProjects(sorted)
      }
    } catch {
      // 静默失败，不影响主流程
    } finally {
      setProjectsLoading(false)
    }
  }

  // ========================
  // 状态显示文本
  // ========================
  const aiStatusText = () => {
    if (statusLoading) return '检测中...'
    switch (aiStatus) {
      case 'connected': return '已连接'
      case 'not_configured': return '未配置'
      case 'error': return '连接异常'
      default: return '暂时无法获取'
    }
  }

  const aiStatusClass = () => {
    switch (aiStatus) {
      case 'connected': return 'status-dot--ok'
      case 'not_configured': return 'status-dot--warn'
      case 'error': return 'status-dot--err'
      default: return 'status-dot--unknown'
    }
  }

  const executorStatusText = () => {
    if (statusLoading) return '检测中...'
    switch (executorStatus) {
      case 'running': return '已启动'
      case 'paused': return '已暂停'
      case 'idle': return '待机'
      case 'stopped': return '已停止'
      default: return '暂时无法获取'
    }
  }

  const executorStatusClass = () => {
    switch (executorStatus) {
      case 'running': return 'status-dot--ok'
      case 'paused': return 'status-dot--warn'
      case 'idle': return 'status-dot--ok'
      case 'stopped': return 'status-dot--err'
      default: return 'status-dot--unknown'
    }
  }

  // ========================
  // 提交需求
  // ========================
  const handleSubmit = useCallback(async () => {
    const text = requirementText.trim()
    if (!text || submitting) return

    setSubmitting(true)
    setError(null)

    try {
      // 1. 创建项目
      const projectNameFinal = text.slice(0, 30)
      const createRes = await createProject({
        name: projectNameFinal,
        idea: text,
      })

      if (!createRes.ok) {
        throw new Error(createRes.error?.detail || '创建项目失败')
      }

      const projectId = createRes.data.id
      setCollectedIdea(text)
      setProjectName(projectNameFinal)

      // 2. 保存需求
      await updateRequirements(projectId, {
        idea: text,
        description: text,
        target_users: '',
        additional_notes: null,
      })

      // 3. 生成智能追问
      try {
        const res = await generateSmartQuestions(text)
        if (res.ok && res.data?.questions?.length > 0) {
          // 进入对话流程
          setQuestions(res.data.questions)
          setAiSummary(res.data.summary || '')
          setCurrentQuestionIndex(0)
          setFlowStep('answering')
          setMessages([
            {
              role: 'system',
              content: `我理解了，你想做一个「${res.data.summary || '帮助解决问题的软件'}」。\n\n为了帮你做得更准确，我还有几个关键问题想确认一下：`,
            },
            {
              role: 'system',
              content: `**${res.data.questions[0].question}**\n_${res.data.questions[0].hint}_`,
              quickOptions: res.data.questions[0].options,
              questionIndex: 0,
            },
          ])
          setSubmitting(false)
          return
        }
      } catch {
        // 追问失败，直接跳转预览
      }

      // 没有追问，直接进入预览
      navigate(`/simple/projects/${projectId}/preview`)
    } catch (err: any) {
      const msg = err?.response?.data?.error?.detail || err?.message || '提交失败，请检查网络连接'
      setError(msg)
      setSubmitting(false)
    }
  }, [requirementText, submitting, navigate])

  // ========================
  // 键盘事件
  // ========================
  const handleKeyDown = (e: React.KeyboardEvent) => {
    // Ctrl+Enter 或 Cmd+Enter 提交
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault()
      handleSubmit()
    }
    // 中文输入法组合输入期间不提交
    if (isComposing.current) return
  }

  // ========================
  // 示例填充
  // ========================
  const examples = [
    {
      label: '商品管理系统',
      text: '我想做一个商品管理系统，可以录入、编辑、删除商品，并查看库存和价格。',
    },
    {
      label: '图片处理工具',
      text: '我想做一个图片处理工具，可以批量上传图片、清除文字、裁剪尺寸并保存处理结果。',
    },
    {
      label: '课程发布助手',
      text: '我想做一个课程发布助手，可以整理课程内容、生成音频，并管理各个平台的发布任务。',
    },
  ]

  const handleExample = (text: string) => {
    setRequirementText(text)
    setError(null)
    textareaRef.current?.focus()
  }

  // ========================
  // 项目阶段中文名
  // ========================
  const stageLabel = (stage: string) => {
    const map: Record<string, string> = {
      draft: '草稿',
      analyzing: '分析中',
      generated: '方案已生成',
      developing: '开发中',
      testing: '测试中',
      completed: '已完成',
      paused: '已暂停',
      tasks_complete: '任务完成',
    }
    return map[stage] || stage
  }

  const stageClass = (stage: string) => {
    const map: Record<string, string> = {
      draft: 'badge-draft',
      analyzing: 'badge-analyzing',
      generated: 'badge-generated',
      developing: 'badge-developing',
      testing: 'badge-testing',
      completed: 'badge-completed',
      paused: 'badge-paused',
      tasks_complete: 'badge-completed',
    }
    return map[stage] || 'badge-draft'
  }

  // ========================
  // 对话流程中的输入处理（保留原有逻辑）
  // ========================
  const handleAnswerSubmit = useCallback(async () => {
    const text = requirementText.trim()
    if (!text || loading) return

    setError(null)
    setMessages(prev => [...prev, { role: 'user', content: text }])
    setRequirementText('')

    const qIndex = currentQuestionIndex
    const q = questions[qIndex]
    if (!q) return

    setAnswers(prev => ({ ...prev, [q.question]: text }))

    const nextIndex = qIndex + 1
    if (nextIndex < questions.length) {
      setCurrentQuestionIndex(nextIndex)
      const nextQ = questions[nextIndex]
      setMessages(prev => [
        ...prev,
        {
          role: 'system',
          content: `**${nextQ.question}**\n_${nextQ.hint}_`,
          quickOptions: nextQ.options,
          questionIndex: nextIndex,
        },
      ])
    } else {
      setFlowStep('confirming')
      setMessages(prev => [
        ...prev,
        {
          role: 'system',
          content: '所有问题已确认！以下是你的需求摘要。确认无误后点击「确认并生成方案」。',
        },
      ])
    }
  }, [requirementText, loading, currentQuestionIndex, questions])

  const handleAnswerKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleAnswerSubmit()
    }
  }

  const handleQuickOption = useCallback((option: string) => {
    setRequirementText(option)
    setTimeout(() => {
      const qIndex = currentQuestionIndex
      const q = questions[qIndex]
      if (!q) return

      setMessages(prev => [...prev, { role: 'user', content: option }])
      setRequirementText('')

      setAnswers(prev => ({ ...prev, [q.question]: option }))

      const nextIndex = qIndex + 1
      if (nextIndex < questions.length) {
        setCurrentQuestionIndex(nextIndex)
        const nextQ = questions[nextIndex]
        setMessages(prev => [
          ...prev,
          {
            role: 'system',
            content: `**${nextQ.question}**\n_${nextQ.hint}_`,
            quickOptions: nextQ.options,
            questionIndex: nextIndex,
          },
        ])
      } else {
        setFlowStep('confirming')
        setMessages(prev => [
          ...prev,
          {
            role: 'system',
            content: '所有问题已确认！以下是你的需求摘要。确认无误后点击「确认并生成方案」。',
          },
        ])
      }
    }, 50)
  }, [currentQuestionIndex, questions])

  const handleSkip = useCallback(() => {
    const qIndex = currentQuestionIndex
    const q = questions[qIndex]
    if (!q) return

    setAnswers(prev => ({ ...prev, [q.question]: '跳过' }))

    const nextIndex = qIndex + 1
    if (nextIndex < questions.length) {
      setCurrentQuestionIndex(nextIndex)
      const nextQ = questions[nextIndex]
      setMessages(prev => [
        ...prev,
        { role: 'system', content: '好的，跳过这个问题。' },
        {
          role: 'system',
          content: `**${nextQ.question}**\n_${nextQ.hint}_`,
          quickOptions: nextQ.options,
          questionIndex: nextIndex,
        },
      ])
    } else {
      setFlowStep('confirming')
      setMessages(prev => [
        ...prev,
        {
          role: 'system',
          content: '所有问题已确认！以下是你的需求摘要。确认无误后点击「确认并生成方案」。',
        },
      ])
    }
  }, [currentQuestionIndex, questions])

  const handleConfirm = useCallback(async () => {
    setLoading(true)
    setError(null)
    setFlowStep('saving')

    try {
      // 项目已创建，只需跳转
      const answersText = Object.entries(answers)
        .filter(([, v]) => v !== '跳过')
        .map(([k, v]) => `${k}: ${v}`)
        .join('\n')

      // 需要找到 projectId - 从 createProject 结果获取
      // 重新创建项目（如果没有保存 projectId）
      const createRes = await createProject({
        name: projectName || collectedIdea.slice(0, 30),
        idea: collectedIdea,
      })

      if (!createRes.ok) {
        throw new Error(createRes.error?.detail || '创建项目失败')
      }

      const projectId = createRes.data.id

      await updateRequirements(projectId, {
        idea: collectedIdea,
        description: collectedIdea,
        target_users: aiSummary || '',
        additional_notes: answersText || null,
      })

      navigate(`/simple/projects/${projectId}/preview`)
    } catch (err: any) {
      const msg = err?.message || err?.response?.data?.error?.detail || '创建失败'
      setError(msg)
      setFlowStep('confirming')
    } finally {
      setLoading(false)
    }
  }, [projectName, collectedIdea, answers, aiSummary, navigate])

  const handleReset = useCallback(() => {
    setFlowStep('home')
    setMessages([])
    setRequirementText('')
    setError(null)
    setCollectedIdea('')
    setQuestions([])
    setCurrentQuestionIndex(0)
    setAnswers({})
    setAiSummary('')
    setProjectName('')
    loadRecentProjects()
  }, [])

  const buildSummary = () => {
    const parts: string[] = []
    parts.push(`**软件想法：** ${collectedIdea}`)
    if (aiSummary) {
      parts.push(`**AI理解：** ${aiSummary}`)
    }
    const validAnswers = Object.entries(answers).filter(([, v]) => v !== '跳过')
    if (validAnswers.length > 0) {
      parts.push('**补充需求：**')
      validAnswers.forEach(([k, v]) => {
        parts.push(`- ${k.replace(/\*\*/g, '')} → ${v}`)
      })
    }
    return parts.join('\n')
  }

  // ========================
  // 对话流程页面
  // ========================
  if (flowStep !== 'home') {
    return (
      <div>
        <div className="page-header">
          <h2>💡 简化模式</h2>
          <ModeToggle />
        </div>

        <div className="chat-container">
          <div className="chat-messages">
            {messages.map((msg, i) => (
              <div key={i} className={`chat-message ${msg.role}`}>
                <div className="chat-bubble">
                  <div style={{ whiteSpace: 'pre-wrap' }}>{msg.content}</div>

                  {msg.quickOptions && msg.quickOptions.length > 0 && flowStep === 'answering' && (
                    <div className="chat-quick-options">
                      {msg.quickOptions.map((opt, j) => (
                        <button
                          key={j}
                          className="chat-quick-option"
                          onClick={() => handleQuickOption(opt)}
                          disabled={loading}
                        >
                          {opt}
                        </button>
                      ))}
                      <button
                        className="chat-quick-option"
                        onClick={handleSkip}
                        disabled={loading}
                        style={{ color: 'var(--text-muted)' }}
                      >
                        跳过
                      </button>
                    </div>
                  )}
                </div>
              </div>
            ))}

            {loading && (
              <div className="chat-message system">
                <div className="chat-bubble">
                  <div className="loading" style={{ padding: 8 }}>
                    <div className="spinner" style={{ width: 16, height: 16 }} />
                    <span style={{ fontSize: 13 }}>思考中...</span>
                  </div>
                </div>
              </div>
            )}

            <div ref={messagesEndRef} />
          </div>

          {flowStep === 'answering' && (
            <div className="chat-input-row">
              <input
                ref={inputRef}
                value={requirementText}
                onChange={(e) => setRequirementText(e.target.value)}
                onKeyDown={handleAnswerKeyDown}
                placeholder="输入你的回答..."
                disabled={loading}
              />
              <button
                className="btn btn-primary"
                onClick={handleAnswerSubmit}
                disabled={!requirementText.trim() || loading}
              >
                发送
              </button>
            </div>
          )}

          {flowStep === 'confirming' && (
            <div className="card" style={{ marginTop: 16 }}>
              <div className="card-title">📋 需求摘要</div>
              <div style={{ whiteSpace: 'pre-wrap', fontSize: 14, color: 'var(--text-secondary)', lineHeight: 1.8 }}>
                {buildSummary()}
              </div>

              <div className="form-group" style={{ marginTop: 16 }}>
                <label>项目名称（可修改）</label>
                <input
                  value={projectName}
                  onChange={(e) => setProjectName(e.target.value)}
                  placeholder="给项目起个名字"
                />
              </div>

              <div className="chat-actions">
                <button
                  className="btn btn-primary"
                  onClick={handleConfirm}
                  disabled={loading}
                >
                  {loading ? '创建中...' : '✅ 确认并生成方案'}
                </button>
                <button
                  className="btn btn-secondary"
                  onClick={handleReset}
                  disabled={loading}
                >
                  返回首页
                </button>
              </div>
            </div>
          )}

          {error && (
            <div className="card" style={{ marginTop: 12, borderColor: 'var(--danger)' }}>
              <p style={{ color: 'var(--danger)', fontSize: 14 }}>⚠️ {error}</p>
            </div>
          )}
        </div>
      </div>
    )
  }

  // ========================
  // 首页
  // ========================
  return (
    <div className="simple-home">
      {/* 标题区 */}
      <div className="simple-home-header">
        <div className="simple-home-mode-toggle">
          <ModeToggle />
        </div>
        <h1 className="simple-home-title">把你的想法变成可以执行的软件项目</h1>
        <p className="simple-home-subtitle">
          不用懂编程，只需要告诉我你想解决什么问题。
          <br />
          AI会帮你分析需求、规划模块、生成任务并逐步推进开发。
        </p>
      </div>

      {/* 需求输入区 */}
      <div className="simple-home-input-area">
        <textarea
          ref={textareaRef}
          className="simple-home-textarea"
          value={requirementText}
          onChange={(e) => { setRequirementText(e.target.value); setError(null) }}
          onKeyDown={handleKeyDown}
          onCompositionStart={() => { isComposing.current = true }}
          onCompositionEnd={() => { isComposing.current = false }}
          placeholder="例如：我想做一个商品采集和图片清洗软件，可以从多个平台采集商品，处理图片后加入待发布数据……"
          rows={5}
          disabled={submitting}
        />
        <div className="simple-home-input-footer">
          <span className="simple-home-hint">Ctrl + Enter 提交</span>
          <button
            className="btn btn-primary simple-home-submit-btn"
            onClick={handleSubmit}
            disabled={!requirementText.trim() || submitting}
          >
            {submitting ? (
              <>
                <div className="spinner" style={{ width: 16, height: 16, borderWidth: 2 }} />
                正在分析你的想法……
              </>
            ) : (
              '开始规划'
            )}
          </button>
        </div>

        {error && (
          <div className="simple-home-error">
            ⚠️ {error}
          </div>
        )}
      </div>

      {/* 示例入口 */}
      <div className="simple-home-examples">
        <span className="simple-home-examples-label">试试这些想法</span>
        <div className="simple-home-examples-row">
          {examples.map((ex, i) => (
            <button
              key={i}
              className="simple-home-example-btn"
              onClick={() => handleExample(ex.text)}
              disabled={submitting}
            >
              {ex.label}
            </button>
          ))}
          <button
            className="simple-home-example-btn simple-home-example-btn--secondary"
            onClick={() => navigate('/')}
          >
            从已有项目继续
          </button>
        </div>
      </div>

      {/* AI与执行器状态 */}
      <div className="simple-home-status">
        <span className="simple-home-status-item">
          <span className={`status-dot ${aiStatusClass()}`} />
          AI状态：{aiStatusText()}
          {aiStatus === 'not_configured' && (
            <button
              className="simple-home-status-link"
              onClick={() => navigate('/settings')}
            >
              去配置 →
            </button>
          )}
          {aiStatus === 'error' && (
            <button
              className="simple-home-status-link"
              onClick={() => navigate('/settings')}
            >
              检查配置 →
            </button>
          )}
        </span>
        <span className="simple-home-status-item">
          <span className={`status-dot ${executorStatusClass()}`} />
          执行器：{executorStatusText()}
        </span>
      </div>

      {/* 最近项目 */}
      <div className="simple-home-recent">
        <h3 className="simple-home-recent-title">最近项目</h3>
        {projectsLoading ? (
          <div className="simple-home-recent-loading">
            <div className="spinner" style={{ width: 18, height: 18 }} />
            加载中...
          </div>
        ) : recentProjects.length === 0 ? (
          <div className="simple-home-recent-empty">
            <p>还没有项目</p>
            <p className="simple-home-recent-empty-hint">在上方输入需求，创建你的第一个项目</p>
          </div>
        ) : (
          <div className="simple-home-recent-list">
            {recentProjects.map((p) => (
              <div
                key={p.id}
                className="simple-home-recent-card"
                onClick={() => navigate(`/simple/projects/${p.id}/preview`)}
              >
                <div className="simple-home-recent-card-body">
                  <span className="simple-home-recent-card-name">{p.name}</span>
                  <span className={`badge ${stageClass(p.current_stage)}`}>
                    {stageLabel(p.current_stage)}
                  </span>
                </div>
                <div className="simple-home-recent-card-meta">
                  <span>更新于 {new Date(p.updated_at).toLocaleString('zh-CN')}</span>
                  <span className="simple-home-recent-card-arrow">→</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
