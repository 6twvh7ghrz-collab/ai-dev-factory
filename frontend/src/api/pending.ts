/** 待发布资料库 API */
import api, { ApiResponse } from './client';

export interface PendingItem {
  id: number;
  image_id: number;
  title: string;
  description: string | null;
  price: number | null;
  platform: string | null;
  sort_order: number;
  status: string;
  image: {
    id: number;
    original_filename: string;
    cleaned_path: string;
    thumbnail_path: string;
    width: number | null;
    height: number | null;
    format: string | null;
    file_size: number;
  } | null;
  created_at: string | null;
  updated_at: string | null;
}

/** 获取待发布列表 */
export async function listPending(projectId: number) {
  const res = await api.get<ApiResponse<PendingItem[]>>(`/projects/${projectId}/pending`);
  return res.data;
}

/** 添加图片到待发布库 */
export async function addToPending(projectId: number, imageIds: number[]) {
  const res = await api.post<ApiResponse>(`/projects/${projectId}/pending`, { image_ids: imageIds });
  return res.data;
}

/** 更新待发布项 */
export async function updatePending(pendingId: number, data: {
  title?: string;
  description?: string;
  price?: number;
  platform?: string;
  sort_order?: number;
}) {
  const res = await api.put<ApiResponse>(`/pending/${pendingId}`, data);
  return res.data;
}

/** 删除待发布项 */
export async function deletePending(pendingId: number) {
  const res = await api.delete<ApiResponse>(`/pending/${pendingId}`);
  return res.data;
}

/** 批量删除 */
export async function batchDeletePending(projectId: number, ids: number[]) {
  const res = await api.post<ApiResponse>(`/projects/${projectId}/pending/batch-delete`, { ids });
  return res.data;
}

/** 一键上架 */
export async function publishPending(projectId: number) {
  const res = await api.post<ApiResponse>(`/projects/${projectId}/pending/publish`);
  return res.data;
}

/** 重新排序 */
export async function reorderPending(projectId: number, items: { id: number; sort_order: number }[]) {
  const res = await api.post<ApiResponse>(`/projects/${projectId}/pending/reorder`, items);
  return res.data;
}
