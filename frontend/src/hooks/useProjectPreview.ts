import { useState, useCallback } from 'react'

interface PreviewData {
  features: string[]
  stats: Record<string, string>
  techDescription: string
  uiPreview: string[]
  loading: boolean
}

export function useProjectPreview() {
  const [preview, setPreview] = useState<PreviewData>({
    features: [],
    stats: {},
    techDescription: '',
    uiPreview: [],
    loading: false,
  })

  const setLoading = useCallback((loading: boolean) => {
    setPreview((prev) => ({ ...prev, loading }))
  }, [])

  const setFeatures = useCallback((features: string[]) => {
    setPreview((prev) => ({ ...prev, features }))
  }, [])

  const setStats = useCallback((stats: Record<string, string>) => {
    setPreview((prev) => ({ ...prev, stats }))
  }, [])

  const setTechDescription = useCallback((desc: string) => {
    setPreview((prev) => ({ ...prev, techDescription: desc }))
  }, [])

  const setUiPreview = useCallback((images: string[]) => {
    setPreview((prev) => ({ ...prev, uiPreview: images }))
  }, [])

  const loadPreviewFromApi = useCallback(async (requirement: Record<string, string>) => {
    setPreview((prev) => ({ ...prev, loading: true }))
    try {
      const response = await fetch('/api/projects/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(requirement),
      })
      const data = await response.json()
      if (data.features) setFeatures(data.features)
      if (data.stats) setStats(data.stats)
      if (data.techDescription) setTechDescription(data.techDescription)
      if (data.uiPreview) setUiPreview(data.uiPreview)
    } catch (error) {
      console.error('Failed to generate preview:', error)
    } finally {
      setPreview((prev) => ({ ...prev, loading: false }))
    }
  }, [setFeatures, setStats, setTechDescription, setUiPreview])

  const reset = useCallback(() => {
    setPreview({
      features: [],
      stats: {},
      techDescription: '',
      uiPreview: [],
      loading: false,
    })
  }, [])

  return {
    ...preview,
    setLoading,
    setFeatures,
    setStats,
    setTechDescription,
    setUiPreview,
    loadPreviewFromApi,
    reset,
  }
}
