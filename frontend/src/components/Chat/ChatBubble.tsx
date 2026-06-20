import React from 'react'

interface ChatBubbleProps {
  type: 'system' | 'user'
  content: string
  timestamp?: Date
}

export function ChatBubble({ type, content, timestamp }: ChatBubbleProps) {
  return (
    <div className={`chat-message ${type}`}>
      <div className="chat-bubble">
        <div style={{ whiteSpace: 'pre-wrap' }}>{content}</div>
        {timestamp && (
          <div style={{
            fontSize: 11,
            color: 'var(--text-muted)',
            marginTop: 6,
          }}>
            {timestamp.toLocaleTimeString('zh-CN', {
              hour: '2-digit',
              minute: '2-digit',
            })}
          </div>
        )}
      </div>
    </div>
  )
}
