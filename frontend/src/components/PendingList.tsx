import { useState, useEffect, useCallback } from 'react'
import api from '../api/client'
import {
  listPending,
  deletePending,
  batchDeletePending,
  updatePending,
  publishPending,
  reorderPending,
  type PendingItem,
} from '../api/pending'

interface EditingItem {
  id: number
  title: string
  description: string
  price: string
  platform: string
}

export default function PendingList({ projectId, onNavigate }: { projectId: number; onNavigate?: (path: string) => void }) {
  const [items, setItems] = useState<PendingItem[]>([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editData, setEditData] = useState<EditingItem | null>(null)
  const [publishing, setPublishing] = useState(false)
  const [toast, setToast] = useState<{ type: 'success' | 'error'; msg: string } | null>(null)
  const [dragIdx, setDragIdx] = useState<number | null>(null)

  // 加载待发布列表
  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await listPending(projectId)
      if (res.ok) {
        setItems(res.data || [])
      }
    } catch (err: any) {
      showToast('error', err?.response?.data?.message || '加载失败')
    }
    setLoading(false)
  }, [projectId])

  useEffect(() => { load() }, [load])

  // Toast
  const showToast = (type: 'success' | 'error', msg: string) => {
    setToast({ type, msg })
    setTimeout(() => setToast(null), 3000)
  }

  // 选择
  const toggleSelect = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }

  const toggleAll = () => {
    if (selected.size === items.length) {
      setSelected(new Set())
    } else {
      setSelected(new Set(items.map((i) => i.id)))
    }
  }

  // 编辑
  const startEdit = (item: PendingItem) => {
    setEditingId(item.id)
    setEditData({
      id: item.id,
      title: item.title || '',
      description: item.description || '',
      price: item.price != null ? String(item.price) : '',
      platform: item.platform || '',
    })
  }

  const cancelEdit = () => {
    setEditingId(null)
    setEditData(null)
  }

  const saveEdit = async () => {
    if (!editData) return
    try {
      const res = await updatePending(editData.id, {
        title: editData.title,
        description: editData.description || undefined,
        price: editData.price ? parseFloat(editData.price) : undefined,
        platform: editData.platform || undefined,
      })
      if (res.ok) {
        showToast('success', '更新成功')
        setEditingId(null)
        setEditData(null)
        load()
      } else {
        showToast('error', res.message || '更新失败')
      }
    } catch (err: any) {
      showToast('error', err?.response?.data?.message || '更新失败')
    }
  }

  // 删除
  const handleDelete = async (id: number) => {
    if (!confirm('确定删除该项？')) return
    try {
      const res = await deletePending(id)
      if (res.ok) {
        showToast('success', '已删除')
        load()
      } else {
        showToast('error', res.message || '删除失败')
      }
    } catch (err: any) {
      showToast('error', err?.response?.data?.message || '删除失败')
    }
  }

  const handleBatchDelete = async () => {
    if (selected.size === 0) return
    if (!confirm(`确定删除选中的 ${selected.size} 项？`)) return
    try {
      const res = await batchDeletePending(projectId, Array.from(selected))
      if (res.ok) {
        const data = res.data as Record<string, unknown> | null
        showToast('success', `已删除 ${(data?.deleted_count as number) || selected.size} 项`)
        setSelected(new Set())
        load()
      } else {
        showToast('error', res.message || '删除失败')
      }
    } catch (err: any) {
      showToast('error', err?.response?.data?.message || '删除失败')
    }
  }

  // 一键上架
  const handlePublish = async () => {
    if (!confirm('确定一键上架所有待发布商品？')) return
    setPublishing(true)
    try {
      const res = await publishPending(projectId)
      if (res.ok) {
        const pubData = res.data as Record<string, unknown> | null
        showToast('success', `已上架 ${(pubData?.published_count as number) || 0} 项商品`)
        load()
      } else {
        showToast('error', res.message || '上架失败')
      }
    } catch (err: any) {
      showToast('error', err?.response?.data?.message || '上架失败')
    }
    setPublishing(false)
  }

  // 排序
  const moveUp = async (idx: number) => {
    if (idx <= 0) return
    const newItems = [...items]
    ;[newItems[idx - 1], newItems[idx]] = [newItems[idx], newItems[idx - 1]]
    setItems(newItems)
    await saveOrder(newItems)
  }

  const moveDown = async (idx: number) => {
    if (idx >= items.length - 1) return
    const newItems = [...items]
    ;[newItems[idx + 1], newItems[idx]] = [newItems[idx], newItems[idx + 1]]
    setItems(newItems)
    await saveOrder(newItems)
  }

  const saveOrder = async (ordered: PendingItem[]) => {
    const reorderData = ordered.map((item, idx) => ({ id: item.id, sort_order: idx }))
    try {
      await reorderPending(projectId, reorderData)
    } catch (_) {}
  }

  // 拖拽排序
  const handleDragStart = (idx: number) => setDragIdx(idx)
  const handleDragOver = (e: React.DragEvent, idx: number) => {
    e.preventDefault()
    if (dragIdx === null || dragIdx === idx) return
    const newItems = [...items]
    const [moved] = newItems.splice(dragIdx, 1)
    newItems.splice(idx, 0, moved)
    setItems(newItems)
    setDragIdx(idx)
  }
  const handleDragEnd = () => {
    setDragIdx(null)
    saveOrder(items)
  }

  // Render
  if (loading) {
    return (
      <div className="loading">
        <div className="spinner" />
        加载中...
      </div>
    )
  }

  return (
    <div className="pending-list">
      {/* Toast */}
      {toast && (
        <div className={`toast toast-${toast.type}`}>{toast.msg}</div>
      )}

      {/* Header */}
      <div className="page-header">
        <h2>📦 待发布资料库</h2>
        <div style={{ display: 'flex', gap: 8 }}>
          {selected.size > 0 && (
            <button className="btn btn-danger btn-sm" onClick={handleBatchDelete}>
              🗑 删除选中 ({selected.size})
            </button>
          )}
          {items.length > 0 && (
            <button
              className="btn btn-success"
              disabled={publishing}
              onClick={handlePublish}
            >
              {publishing ? '发布中...' : '🚀 一键上架'}
            </button>
          )}
        </div>
      </div>

      {/* Empty state */}
      {items.length === 0 ? (
        <div className="card empty-state">
          <span style={{ fontSize: 64 }}>📭</span>
          <p style={{ marginTop: 16, fontSize: 16 }}>待发布库为空</p>
          <p style={{ color: '#94a3b8', fontSize: 14, marginBottom: 20 }}>
            请先在分类页面中选择图片，再将其加入待发布库
          </p>
          <button className="btn btn-primary" onClick={() => onNavigate?.(`/project/${projectId}/categories`)}>
            前往选择图片 →
          </button>
        </div>
      ) : (
        <div className="card" style={{ padding: 0 }}>
          {/* Toolbar */}
          <div style={{ padding: '12px 16px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 12 }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', fontSize: 13, color: 'var(--text-secondary)' }}>
              <input
                type="checkbox"
                checked={selected.size === items.length && items.length > 0}
                onChange={toggleAll}
                style={{ width: 16, height: 16, accentColor: 'var(--accent)' }}
              />
              全选
            </label>
            <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>
              共 {items.length} 项 | 已选 {selected.size}
            </span>
          </div>

          {/* Items list */}
          <div style={{ maxHeight: 'calc(100vh - 260px)', overflowY: 'auto' }}>
            {items.map((item, idx) => {
              if (!item || typeof item !== 'object') return null
              const isEditing = editingId === item.id
              const isDragging = dragIdx === idx
              const image = item.image ?? null

              return (
                <div
                  key={item.id}
                  draggable
                  onDragStart={() => handleDragStart(idx)}
                  onDragOver={(e) => handleDragOver(e, idx)}
                  onDragEnd={handleDragEnd}
                  style={{
                    display: 'flex',
                    alignItems: 'flex-start',
                    gap: 12,
                    padding: '12px 16px',
                    borderBottom: '1px solid var(--border)',
                    background: isDragging ? 'var(--bg-hover)' : selected.has(item.id) ? 'rgba(59,130,246,0.05)' : 'transparent',
                    cursor: 'grab',
                    transition: 'background 0.15s',
                  }}
                >
                  {/* Checkbox & Drag handle */}
                  <div style={{ display: 'flex', alignItems: 'center', gap: 4, paddingTop: 4 }}>
                    <input
                      type="checkbox"
                      checked={selected.has(item.id)}
                      onChange={() => toggleSelect(item.id)}
                      style={{ width: 16, height: 16, accentColor: 'var(--accent)' }}
                    />
                    <span style={{ cursor: 'grab', color: 'var(--text-muted)', fontSize: 14 }}>⠿</span>
                  </div>

                  {/* Thumbnail */}
                  <div
                    style={{
                      width: 80, height: 80, flexShrink: 0,
                      borderRadius: 6, overflow: 'hidden',
                      background: 'var(--bg-primary)',
                      border: '1px solid var(--border)',
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                    }}
                  >
                    {image?.thumbnail_path || image?.cleaned_path ? (
                      <img
                        src={`/uploads/${image.thumbnail_path || image.cleaned_path}`}
                        alt={item.title || '商品图片'}
                        style={{ width: '100%', height: '100%', objectFit: 'cover' }}
                        onError={(e) => {
                          (e.target as HTMLImageElement).style.display = 'none'
                          ;(e.target as HTMLImageElement).nextElementSibling?.classList.remove('hidden')
                        }}
                      />
                    ) : null}
                    <span className={image?.thumbnail_path || image?.cleaned_path ? 'hidden' : ''} style={{ fontSize: 28 }}>🖼</span>
                  </div>

                  {/* Content */}
                  <div style={{ flex: 1, minWidth: 0 }}>
                    {isEditing ? (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                        <input
                          value={editData!.title}
                          onChange={(e) => setEditData({ ...editData!, title: e.target.value })}
                          placeholder="商品标题"
                          style={{ width: '100%' }}
                        />
                        <div style={{ display: 'flex', gap: 8 }}>
                          <input
                            value={editData!.price}
                            onChange={(e) => setEditData({ ...editData!, price: e.target.value })}
                            placeholder="价格"
                            type="number"
                            step="0.01"
                            style={{ width: 140 }}
                          />
                          <input
                            value={editData!.platform}
                            onChange={(e) => setEditData({ ...editData!, platform: e.target.value })}
                            placeholder="平台 (如: 淘宝/京东)"
                            style={{ flex: 1 }}
                          />
                        </div>
                        <textarea
                          value={editData!.description}
                          onChange={(e) => setEditData({ ...editData!, description: e.target.value })}
                          placeholder="商品描述"
                          rows={2}
                        />
                        <div style={{ display: 'flex', gap: 6 }}>
                          <button className="btn btn-primary btn-sm" onClick={saveEdit}>💾 保存</button>
                          <button className="btn btn-secondary btn-sm" onClick={cancelEdit}>取消</button>
                        </div>
                      </div>
                    ) : (
                      <>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                          <span style={{ fontWeight: 600, fontSize: 15 }}>{item.title || '未命名商品'}</span>
                          <span
                            className={`badge ${item.status === 'released' ? 'badge-success' : item.status === 'ready' ? 'badge-warning' : 'badge-draft'}`}
                          >
                            {item.status === 'released' ? '已上架' : item.status === 'ready' ? '待上架' : '草稿'}
                          </span>
                        </div>
                        <div style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 2 }}>
                          {item.description || '暂无描述'}
                        </div>
                        <div style={{ display: 'flex', gap: 16, fontSize: 12, color: 'var(--text-muted)' }}>
                          {item.price != null && <span>💰 ¥{Number(item.price).toFixed(2)}</span>}
                          {item.platform && <span>🏪 {item.platform}</span>}
                          {image?.width != null && image?.height != null && (
                            <span>
                              📐 {image.width}x{image.height} | {image.format || '-'} | {image.file_size ? (image.file_size / 1024).toFixed(1) + 'KB' : '-'}
                            </span>
                          )}
                        </div>
                      </>
                    )}
                  </div>

                  {/* Actions */}
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 4, flexShrink: 0 }}>
                    {!isEditing && (
                      <>
                        <button className="btn btn-secondary btn-sm" onClick={() => startEdit(item)} title="编辑">
                          ✏️
                        </button>
                        <button className="btn btn-secondary btn-sm" onClick={() => moveUp(idx)} title="上移" disabled={idx === 0}>
                          ⬆
                        </button>
                        <button className="btn btn-secondary btn-sm" onClick={() => moveDown(idx)} title="下移" disabled={idx === items.length - 1}>
                          ⬇
                        </button>
                        <button className="btn btn-danger btn-sm" onClick={() => handleDelete(item.id)} title="删除">
                          🗑
                        </button>
                      </>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
