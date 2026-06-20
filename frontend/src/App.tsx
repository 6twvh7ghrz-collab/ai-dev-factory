import { Routes, Route, NavLink, useLocation } from 'react-router-dom'
import ProjectList from './pages/ProjectList'
import ProjectCreate from './pages/ProjectCreate'
import ProjectDetail from './pages/ProjectDetail'
import UploadPage from './pages/UploadPage'
import CategoryPage from './pages/CategoryPage'
import PendingReleasePage from './pages/PendingReleasePage'
import AiSettings from './pages/AiSettings'
import SimpleRequirementPage from './pages/SimpleMode/SimpleRequirementPage'
import SimpleProjectPreviewPage from './pages/SimpleMode/SimpleProjectPreviewPage'
import SimpleProjectProgressPage from './pages/SimpleMode/SimpleProjectProgressPage'
import SimpleProjectCompletePage from './pages/SimpleMode/SimpleProjectCompletePage'

function Sidebar() {
  const location = useLocation()
  const isSimpleMode = location.pathname.startsWith('/simple')

  return (
    <div className="sidebar">
      <div className="sidebar-logo">
        <h1>AI 软件开发工厂</h1>
        <span>V2.0</span>
      </div>
      <nav className="sidebar-nav">
        <NavLink to="/simple/new" end className={({ isActive }) => isActive ? 'active' : ''}>
          💡 简化模式
        </NavLink>
        <NavLink to="/" end className={({ isActive }) => isActive && !isSimpleMode ? 'active' : ''}>
          📋 项目列表
        </NavLink>
        <NavLink to="/settings" className={({ isActive }) => isActive ? 'active' : ''}>
          ⚙️ AI 配置
        </NavLink>
      </nav>
    </div>
  )
}

export default function App() {
  return (
    <div className="app-layout">
      <Sidebar />
      <div className="main-content">
        <Routes>
          {/* 简化模式路由 */}
          <Route path="/simple/new" element={<SimpleRequirementPage />} />
          <Route path="/simple/projects/:projectId/preview" element={<SimpleProjectPreviewPage />} />
          <Route path="/simple/projects/:projectId/progress" element={<SimpleProjectProgressPage />} />
          <Route path="/simple/projects/:projectId/complete" element={<SimpleProjectCompletePage />} />

          {/* 专业模式路由（原有路由不变） */}
          <Route path="/" element={<ProjectList />} />
          <Route path="/create" element={<ProjectCreate />} />
          <Route path="/project/:id" element={<ProjectDetail />} />
          <Route path="/project/:id/upload" element={<UploadPage />} />
          <Route path="/project/:id/categories" element={<CategoryPage />} />
          <Route path="/project/:id/pending" element={<PendingReleasePage />} />
          <Route path="/settings" element={<AiSettings />} />
        </Routes>
      </div>
    </div>
  )
}
