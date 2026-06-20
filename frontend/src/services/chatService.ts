import api from '../api/client'

export interface ChatRequirement {
  idea: string
  problem?: string
  timeline?: string
  budget?: string
}

export interface ChatResponse {
  message: string
  nextStep?: string
  suggestions?: string[]
}

/**
 * 发送聊天需求到后端
 */
export async function sendChatRequirement(data: ChatRequirement): Promise<ChatResponse> {
  const response = await api.post('/chat/requirement', data)
  return response.data
}

/**
 * 获取聊天建议
 */
export async function getChatSuggestions(idea: string): Promise<string[]> {
  const response = await api.post('/chat/suggestions', { idea })
  return response.data?.suggestions ?? []
}

/**
 * 提交需求确认
 */
export async function confirmRequirement(data: ChatRequirement): Promise<{ projectId: number }> {
  const response = await api.post('/chat/confirm', data)
  return response.data
}
