import { useState, useCallback } from 'react'

export type AppMode = 'simple' | 'professional'
export type FlowStage = 'input' | 'preview' | 'developing' | 'complete' | 'bug-report'

interface ProjectFlow {
  mode: AppMode
  stage: FlowStage
  requirement: Record<string, string> | null
  project: Record<string, unknown> | null
  projectId: number | null
}

export function useProjectFlow() {
  const [flow, setFlow] = useState<ProjectFlow>({
    mode: 'simple',
    stage: 'input',
    requirement: null,
    project: null,
    projectId: null,
  })

  const goToInput = useCallback(() => {
    setFlow((prev) => ({ ...prev, stage: 'input', requirement: null }))
  }, [])

  const goToPreview = useCallback((requirement: Record<string, string>) => {
    setFlow((prev) => ({ ...prev, stage: 'preview', requirement }))
  }, [])

  const goToDeveloping = useCallback((project: Record<string, unknown>) => {
    setFlow((prev) => ({
      ...prev,
      stage: 'developing',
      project,
      projectId: (project.id as number) ?? null,
    }))
  }, [])

  const goToComplete = useCallback((projectId: number) => {
    setFlow((prev) => ({ ...prev, stage: 'complete', projectId }))
  }, [])

  const goToBugReport = useCallback(() => {
    setFlow((prev) => ({ ...prev, stage: 'bug-report' }))
  }, [])

  const toggleMode = useCallback((mode: AppMode) => {
    setFlow((prev) => ({ ...prev, mode }))
  }, [])

  return {
    ...flow,
    goToInput,
    goToPreview,
    goToDeveloping,
    goToComplete,
    goToBugReport,
    toggleMode,
  }
}
