/** AI 配置 API */
import api, { ApiResponse } from './client';

export interface AiConfig {
  id: number;
  provider: string;
  model: string;
  api_key_masked: string;
  base_url: string | null;
  is_active: boolean;
}

export async function listAiConfigs() {
  const res = await api.get<ApiResponse<AiConfig[]>>('/settings/ai');
  return res.data;
}

export async function saveAiConfig(data: { provider: string; model: string; api_key: string; base_url?: string }) {
  const res = await api.put<ApiResponse<{ id: number }>>('/settings/ai', data);
  return res.data;
}

export async function testAiConnection(data: { provider: string; model: string; api_key: string; base_url?: string }) {
  const res = await api.post<ApiResponse<{ response: string }>>('/settings/ai/test', data);
  return res.data;
}
