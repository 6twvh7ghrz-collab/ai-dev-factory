import { useState, useCallback } from 'react'

interface ChatMessage {
  role: 'system' | 'user'
  content: string
  quickOptions?: string[]
  questionIndex?: number
}

type FlowStep = 'greeting' | 'collecting' | 'asking' | 'confirming'

interface ChatBotState {
  messages: ChatMessage[]
  flowStep: FlowStep
  collectedData: Record<string, string>
}

export function useChatBot() {
  const [state, setState] = useState<ChatBotState>({
    messages: [
      {
        role: 'system',
        content: '👋 你好！欢迎使用AI软件开发工厂。请告诉我，你想做什么项目？',
      },
    ],
    flowStep: 'greeting',
    collectedData: {},
  })

  const addMessage = useCallback((message: ChatMessage) => {
    setState((prev) => ({
      ...prev,
      messages: [...prev.messages, message],
    }))
  }, [])

  const addSystemMessage = useCallback((content: string, quickOptions?: string[]) => {
    addMessage({ role: 'system', content, quickOptions })
  }, [addMessage])

  const addUserMessage = useCallback((content: string) => {
    addMessage({ role: 'user', content })
  }, [addMessage])

  const setFlowStep = useCallback((step: FlowStep) => {
    setState((prev) => ({ ...prev, flowStep: step }))
  }, [])

  const collectData = useCallback((key: string, value: string) => {
    setState((prev) => ({
      ...prev,
      collectedData: { ...prev.collectedData, [key]: value },
    }))
  }, [])

  const reset = useCallback(() => {
    setState({
      messages: [
        {
          role: 'system',
          content: '👋 你好！欢迎使用AI软件开发工厂。请告诉我，你想做什么项目？',
        },
      ],
      flowStep: 'greeting',
      collectedData: {},
    })
  }, [])

  return {
    ...state,
    addMessage,
    addSystemMessage,
    addUserMessage,
    setFlowStep,
    collectData,
    reset,
  }
}
