/** API 统一响应类型 */
export interface ApiResponse<T = unknown> {
  ok: boolean;
  data: T;
  message: string;
  error: { code: string; detail: string } | null;
}

import axios from 'axios';

const api = axios.create({
  baseURL: '/api',
  timeout: 120000,
  headers: { 'Content-Type': 'application/json' },
});

api.interceptors.response.use(
  (res) => res,
  (err) => {
    console.error('API Error:', err);
    return Promise.reject(err);
  }
);

export default api;
