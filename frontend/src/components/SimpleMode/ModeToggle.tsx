import { useEffect, useState } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'

export type AppMode = 'simple' | 'professional'

const MODE_STORAGE_KEY = 'ai-dev-factory-mode'

export function getStoredMode(): AppMode {
  const stored = localStorage.getItem(MODE_STORAGE_KEY)
  if (stored === 'simple' || stored === 'professional') return stored
  return 'professional'
}

export function setStoredMode(mode: AppMode) {
  localStorage.setItem(MODE_STORAGE_KEY, mode)
}

interface Props {
  projectId?: number | null
}

export default function ModeToggle({ projectId }: Props) {
  const navigate = useNavigate()
  const location = useLocation()
  const [mode, setMode] = useState<AppMode>(getStoredMode)

  // 根据当前路由自动同步 mode 状态
  useEffect(() => {
    const currentMode = location.pathname.startsWith('/simple') ? 'simple' : 'professional'
    if (currentMode !== mode) {
      setMode(currentMode)
    }
  }, [location.pathname])

  const handleToggle = (newMode: AppMode) => {
    if (newMode === mode) return
    setStoredMode(newMode)
    setMode(newMode)

    // 如果是项目上下文，切换到对应模式的项目页面
    if (projectId) {
      if (newMode === 'simple') {
        navigate(`/simple/projects/${projectId}/preview`)
      } else {
        navigate(`/project/${projectId}`)
      }
    } else {
      if (newMode === 'simple') {
        navigate('/simple/new')
      } else {
        navigate('/')
      }
    }
  }

  return (
    <div className="mode-toggle">
      <button
        className={`mode-toggle-btn ${mode === 'simple' ? 'active' : ''}`}
        onClick={() => handleToggle('simple')}
      >
        💡 简化模式
      </button>
      <button
        className={`mode-toggle-btn ${mode === 'professional' ? 'active' : ''}`}
        onClick={() => handleToggle('professional')}
      >
        ⚙️ 专业模式
      </button>
    </div>
  )
}
