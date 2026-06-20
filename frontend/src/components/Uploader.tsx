import { useState, useRef } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import api from '../api/client'

interface UploadFile {
  file: File
  progress: number
  status: 'pending' | 'uploading' | 'success' | 'error'
  error?: string
  imageId?: number
}

export default function Uploader({ projectId }: { projectId: number }) {
  const navigate = useNavigate()
  const [files, setFiles] = useState<UploadFile[]>([])
  const [uploading, setUploading] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(e.target.files || [])
    const newFiles: UploadFile[] = selected.map((f) => ({
      file: f,
      progress: 0,
      status: 'pending' as const,
    }))
    setFiles((prev) => [...prev, ...newFiles])
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  const removeFile = (idx: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== idx))
  }

  const uploadAll = async () => {
    const pending = files.filter((f) => f.status === 'pending' || f.status === 'error')
    if (pending.length === 0) return

    setUploading(true)
    const updated = [...files]

    for (let i = 0; i < updated.length; i++) {
      if (updated[i].status !== 'pending' && updated[i].status !== 'error') continue

      updated[i] = { ...updated[i], status: 'uploading' as const, progress: 0 }
      setFiles([...updated])

      try {
        const formData = new FormData()
        formData.append('file', updated[i].file)

        const res = await api.post(
          `/projects/${projectId}/images/upload`,
          formData,
          {
            headers: { 'Content-Type': 'multipart/form-data' },
            onUploadProgress: (event) => {
              if (event.total) {
                updated[i].progress = Math.round((event.loaded * 100) / event.total)
                setFiles([...updated])
              }
            },
          }
        )

        updated[i] = {
          ...updated[i],
          status: 'success' as const,
          progress: 100,
          imageId: res.data?.data?.id,
        }
        setFiles([...updated])
      } catch (err: any) {
        updated[i] = {
          ...updated[i],
          status: 'error' as const,
          error: err?.response?.data?.message || err.message || '上传失败',
        }
        setFiles([...updated])
      }
    }

    setUploading(false)
  }

  const successCount = files.filter((f) => f.status === 'success').length
  const allDone = files.length > 0 && files.every((f) => f.status === 'success')

  return (
    <div className="uploader">
      <h2>图片上传</h2>

      <div className="upload-zone" onClick={() => fileInputRef.current?.click()}>
        <div className="upload-zone-content">
          <span style={{ fontSize: 48 }}>📤</span>
          <p>点击选择图片或拖拽文件到此处</p>
          <p style={{ fontSize: 12, color: '#94a3b8' }}>支持 JPG / PNG / WebP，单文件最大 20MB</p>
        </div>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept="image/jpeg,image/png,image/webp"
          onChange={handleFileSelect}
          style={{ display: 'none' }}
        />
      </div>

      {files.length > 0 && (
        <div className="file-list">
          <div className="file-list-header">
            <span>已选择 {files.length} 个文件</span>
            <span>成功 {successCount} 个</span>
          </div>
          {files.map((f, idx) => (
            <div key={idx} className={`file-item ${f.status}`}>
              <span className="file-name">📷 {f.file.name}</span>
              <span className="file-size">{(f.file.size / 1024).toFixed(1)} KB</span>
              <div className="file-progress-wrap">
                {f.status === 'uploading' && (
                  <div className="progress-bar">
                    <div className="progress-fill" style={{ width: `${f.progress}%` }} />
                  </div>
                )}
                <span className="file-status">
                  {f.status === 'pending' && '⏳ 等待'}
                  {f.status === 'uploading' && `${f.progress}%`}
                  {f.status === 'success' && '✅ 成功'}
                  {f.status === 'error' && `❌ ${f.error || '失败'}`}
                </span>
              </div>
              {f.status !== 'uploading' && (
                <button className="btn-remove" onClick={() => removeFile(idx)}>
                  ✕
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      <div className="upload-actions">
        <button
          className="btn btn-primary"
          disabled={files.length === 0 || uploading || allDone}
          onClick={uploadAll}
        >
          {uploading ? '上传中...' : '开始上传'}
        </button>
        {allDone && (
          <button
            className="btn btn-secondary"
            onClick={() =>
              navigate(`/project/${projectId}/categories`)
            }
          >
            前往分类展示 →
          </button>
        )}
      </div>
    </div>
  )
}
