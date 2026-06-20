import api from '../api/client'

export interface AnalysisResult {
  product_definition?: string
  problem?: string
  target_users?: string[]
  core_value?: string[]
  modules?: Array<{ name: string; description: string; features?: string[] }>
  risks?: string[]
  techDescription?: string
}

/**
 * 触发 AI 需求分析
 */
export async function analyzeProject(projectId: number): Promise<AnalysisResult> {
  const response = await api.post(`/projects/${projectId}/analyze`)
  return response.data
}

/**
 * 获取已有分析结果
 */
export async function getProjectAnalysis(projectId: number): Promise<AnalysisResult | null> {
  try {
    const response = await api.get(`/projects/${projectId}/analysis`)
    if (response.data?.ok && response.data?.data) {
      return response.data.data
    }
    return null
  } catch {
    return null
  }
}

/**
 * 生成模块规划
 */
export async function generateProjectModules(
  projectId: number
): Promise<Array<{ name: string; description: string }>> {
  const response = await api.post(`/projects/${projectId}/modules`)
  return response.data
}

/**
 * 生成开发任务
 */
export async function generateProjectTasks(
  projectId: number
): Promise<Array<{ id: number; name: string; status: string }>> {
  const response = await api.post(`/projects/${projectId}/tasks`)
  return response.data
}
