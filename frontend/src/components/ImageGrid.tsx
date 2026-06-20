import { useState, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import api from '../api/client'

interface ImageItem {
  id: number
  original_filename: string
  thumbnail_path: string | null
  cleaned_path: string | null
  file_size: number | null
  width: number | null
  height: number | null
  format: string | null
  status: string
  sort_order: number
  category_ids: number[]
  created_at: string | null
}

interface Category {
  id: number
  name: string
  description: string | null
  sort_order: number
  image_count: number
}

export default function ImageGrid({ projectId }: { projectId: number }) {
  const navigate = useNavigate()
  const [categories, setCategories] = useState<Category[]>([])
  const [images, setImages] = useState<ImageItem[]>([])
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [activeCategory, setActiveCategory] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [viewMode, setViewMode] = useState<'grid' | 'list'>('grid')

  // Load categories
  useEffect(() => {
    api.get(`/projects/${projectId}/categories`).then((res) => {
      setCategories(res.data?.data || [])
    })
  }, [projectId])

  // Load images
  const loadImages = async (categoryId: number | null) => {
    setLoading(true)
    const params: Record<string, string> = {}
    if (categoryId) params.category_id = String(categoryId)
    const res = await api.get(`/projects/${projectId}/images`, { params })
    const imgs = res.data?.data || []
    setImages(imgs)
    setSelectedIds(new Set(imgs.filter((i: ImageItem) => i.status === 'selected').map((i: ImageItem) => i.id)))
    setLoading(false)
  }

  useEffect(() => {
    loadImages(activeCategory)
  }, [activeCategory])

  const toggleImage = async (imageId: number) => {
    const action = selectedIds.has(imageId) ? 'deselect' : 'select'
    await api.post(`/projects/${projectId}/images/select`, {
      image_ids: [imageId],
      action,
    })

    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (action === 'select') next.add(imageId)
      else next.delete(imageId)
      return next
    })
  }

  const selectAll = async () => {
    const visibleIds = images.map((i) => i.id)
    await api.post(`/projects/${projectId}/images/select-all`, {
      category_id: activeCategory,
      action: 'select',
    })
    setSelectedIds(new Set(visibleIds))
  }

  const deselectAll = async () => {
    await api.post(`/projects/${projectId}/images/select-all`, {
      category_id: activeCategory,
      action: 'deselect',
    })
    setSelectedIds(new Set())
  }

  const moveToPending = () => {
    navigate(`/project/${projectId}/pending`)
  }

  return (
    <div className="image-grid-container">
      <div className="image-grid-header">
        <h2>分类展示与选择</h2>
        <div className="header-actions">
          <div className="view-toggle">
            <button
              className={`btn-sm ${viewMode === 'grid' ? 'active' : ''}`}
              onClick={() => setViewMode('grid')}
            >
              ▦ 网格
            </button>
            <button
              className={`btn-sm ${viewMode === 'list' ? 'active' : ''}`}
              onClick={() => setViewMode('list')}
            >
              ☰ 列表
            </button>
          </div>
          <span className="selected-count">已选: {selectedIds.size} 张</span>
          {selectedIds.size > 0 && (
            <button className="btn btn-primary" onClick={moveToPending}>
              移入待发布资料库 →
            </button>
          )}
        </div>
      </div>

      <div className="category-tabs">
        <button
          className={`cat-tab ${activeCategory === null ? 'active' : ''}`}
          onClick={() => setActiveCategory(null)}
        >
          全部图片
        </button>
        {categories.map((cat) => (
          <button
            key={cat.id}
            className={`cat-tab ${activeCategory === cat.id ? 'active' : ''}`}
            onClick={() => setActiveCategory(cat.id)}
          >
            {cat.name} ({cat.image_count})
          </button>
        ))}
      </div>

      <div className="batch-actions">
        <button className="btn-link" onClick={selectAll}>
          ✅ 全选当前
        </button>
        <button className="btn-link" onClick={deselectAll}>
          ❎ 取消全选
        </button>
      </div>

      {loading ? (
        <div className="loading">加载中...</div>
      ) : images.length === 0 ? (
        <div className="empty-state">
          <span style={{ fontSize: 48 }}>🖼️</span>
          <p>暂无图片，请先上传</p>
          <button className="btn btn-secondary" onClick={() => navigate(`/project/${projectId}/upload`)}>
            前往上传
          </button>
        </div>
      ) : viewMode === 'grid' ? (
        <div className="image-grid">
          {images.map((img) => (
            <div
              key={img.id}
              className={`image-card ${selectedIds.has(img.id) ? 'selected' : ''}`}
              onClick={() => toggleImage(img.id)}
            >
              <div className="image-card-check">
                <input
                  type="checkbox"
                  checked={selectedIds.has(img.id)}
                  onChange={() => {}}
                  onClick={(e) => e.stopPropagation()}
                />
              </div>
              <div className="image-card-preview">
                {img.cleaned_path ? (
                  <img src={img.cleaned_path} alt={img.original_filename} />
                ) : (
                  <div className="no-preview">暂无预览</div>
                )}
              </div>
              <div className="image-card-info">
                <span className="image-name">{img.original_filename}</span>
                <span className="image-meta">
                  {img.width}x{img.height} | {img.format?.toUpperCase()}
                </span>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <table className="image-table">
          <thead>
            <tr>
              <th style={{ width: 40 }}>选择</th>
              <th>文件名</th>
              <th>尺寸</th>
              <th>格式</th>
              <th>大小</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            {images.map((img) => (
              <tr key={img.id} className={selectedIds.has(img.id) ? 'row-selected' : ''}>
                <td>
                  <input
                    type="checkbox"
                    checked={selectedIds.has(img.id)}
                    onChange={() => toggleImage(img.id)}
                  />
                </td>
                <td>{img.original_filename}</td>
                <td>{img.width}x{img.height}</td>
                <td>{img.format?.toUpperCase()}</td>
                <td>{img.file_size ? (img.file_size / 1024).toFixed(1) + ' KB' : '-'}</td>
                <td>
                  <span className={`status-badge ${img.status}`}>
                    {img.status === 'selected' ? '✅ 已选' : img.status}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
