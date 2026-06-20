/** 项目管理 API */
import api, { ApiResponse } from './client';

export interface Project {
  id: number;
  name: string;
  idea: string | null;
  description: string | null;
  status: string;
  current_stage: string;
  created_at: string;
  updated_at: string;
}

export interface ProjectDetail extends Project {
  target_users: string | null;
  core_features: string | null;
  scenarios: string | null;
  platform: string | null;
  need_login: string | null;
  need_database: string | null;
  need_ai: string | null;
  need_third_party: string | null;
  need_upload: string | null;
  need_export: string | null;
  tech_requirements: string | null;
  exclude_features: string | null;
  additional_notes: string | null;
}

export async function createProject(data: { name: string; idea?: string; description?: string }) {
  const res = await api.post<ApiResponse<Project>>('/projects', data);
  return res.data;
}

export async function listProjects() {
  const res = await api.get<ApiResponse<Project[]>>('/projects');
  return res.data;
}

export async function getProject(id: number) {
  const res = await api.get<ApiResponse<ProjectDetail>>(`/projects/${id}`);
  return res.data;
}

export async function updateProject(id: number, data: Partial<ProjectDetail>) {
  const res = await api.put<ApiResponse<ProjectDetail>>(`/projects/${id}`, data);
  return res.data;
}

export async function deleteProject(id: number) {
  const res = await api.delete<ApiResponse<null>>(`/projects/${id}`);
  return res.data;
}

export async function updateRequirements(id: number, data: Record<string, string | null>) {
  const res = await api.put<ApiResponse<ProjectDetail>>(`/projects/${id}/requirements`, data);
  return res.data;
}
