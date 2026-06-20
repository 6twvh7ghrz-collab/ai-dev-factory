import React, { useState, useRef } from 'react'

interface ChatInputProps {
  placeholder?: string
  disabled?: boolean
  onSubmit: (text: string) => void
}

export function ChatInput({ placeholder = '输入你的回答...', disabled = false, onSubmit }: ChatInputProps) {
  const [text, setText] = useState('')
  const isComposing = useRef(false)

  const handleSubmit = () => {
    const trimmed = text.trim()
    if (!trimmed || disabled) return
    onSubmit(trimmed)
    setText('')
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (isComposing.current) return
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSubmit()
    }
  }

  return (
    <div className="chat-input-row">
      <input
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={handleKeyDown}
        onCompositionStart={() => { isComposing.current = true }}
        onCompositionEnd={() => { isComposing.current = false }}
        placeholder={placeholder}
        disabled={disabled}
      />
      <button
        className="btn btn-primary"
        onClick={handleSubmit}
        disabled={!text.trim() || disabled}
      >
        发送
      </button>
    </div>
  )
}
