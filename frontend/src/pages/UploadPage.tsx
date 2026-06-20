import { useState } from 'react'
import { useParams } from 'react-router-dom'
import Uploader from '../components/Uploader'

export default function UploadPage() {
  const { id } = useParams<{ id: string }>()
  const projectId = Number(id)

  return (
    <div className="upload-page">
      <Uploader projectId={projectId} />
    </div>
  )
}
