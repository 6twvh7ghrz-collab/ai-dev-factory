import api from '../api/client'
import {
  createProject,
  getProject,
  listProjects,
  updateRequirements,
  type Project,
  type ProjectDetail,
} from '../api/projects'

export interface ProjectPreviewData {
  features: string[]
  stats: {
    time: string
    cost: string
    includes: string
  }
  techDescription: string
  uiPreview: string[]
}

export interface CreateProjectRequest {
  name: string
  idea: string
  problem?: string
  timeline?: string
  budget?: string
}

/**
 * 获取项目预览（通过 AI 生成）
 */
export async function getProjectPreview(
  requirement: Record<string, string>
): Promise<ProjectPreviewData> {
  const response = await api.post('/projects/preview', requirement)
  return response.data
}

/**
 * 创建项目（封装）
 */
export async function createProjectFromRequirement(
  data: CreateProjectRequest
): Promise<{ id: number; projectId: number; createdAt: string }> {
  const response = await createProject({
    name: data.name,
    idea: data.idea,
  })

  if (!response.ok) {
    throw new Error(response.error?.detail || '创建项目失败')
  }

  return {
    id: response.data.id,
    projectId: response.data.id,
    createdAt: response.data.created_at,
  }
}

/**
 * 获取项目进度
 */
export async function getProjectProgress(
  projectId: number
): Promise<{
  percentage: number
  steps: Array<{ id: string; name: string; completed: boolean; timestamp: string | null }>
  currentStep: string
}> {
  const response = await api.get(`/projects/${projectId}/progress`)
  return response.data
}

/**
 * 获取项目完成信息
 */
export async function getProjectCompleteInfo(
  projectId: number
): Promise<{
  files: string[]
  downloadUrl: string
  deployUrl: string
}> {
  const response = await api.get(`/projects/${projectId}/complete`)
  return response.data
}

export { createProject, getProject, listProjects, updateRequirements }
export type { Project, ProjectDetail }
