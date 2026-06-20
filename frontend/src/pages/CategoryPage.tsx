import { useParams } from 'react-router-dom'
import ImageGrid from '../components/ImageGrid'

export default function CategoryPage() {
  const { id } = useParams<{ id: string }>()
  const projectId = Number(id)

  return (
    <div className="category-page">
      <ImageGrid projectId={projectId} />
    </div>
  )
}
