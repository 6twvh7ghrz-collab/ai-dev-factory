import { useParams, useNavigate } from 'react-router-dom'
import PendingList from '../components/PendingList'

export default function PendingReleasePage() {
  const { id } = useParams<{ id: string }>()
  const projectId = Number(id)
  const navigate = useNavigate()

  return (
    <div className="pending-release-page">
      <PendingList
        projectId={projectId}
        onNavigate={(path) => navigate(path)}
      />
    </div>
  )
}
