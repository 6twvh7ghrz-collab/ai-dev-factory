import { useState, useEffect } from 'react'
import { listAiConfigs, saveAiConfig, testAiConnection, type AiConfig } from '../api/aiConfig'

const PROVIDERS = [
  { name: 'OpenAI', baseUrl: 'https://api.openai.com/v1' },
  { name: 'DeepSeek', baseUrl: 'https://api.deepseek.com/v1' },
  { name: 'Moonshot', baseUrl: 'https://api.moonshot.cn/v1' },
  { name: '硅基流动 (SiliconFlow)', baseUrl: 'https://api.siliconflow.cn/v1' },
  { name: '其他兼容接口', baseUrl: '' },
]

export default function AiSettings() {
  const [configs, setConfigs] = useState<AiConfig[]>([])
  const [provider, setProvider] = useState('DeepSeek')
  const [model, setModel] = useState('deepseek-chat')
  const [apiKey, setApiKey] = useState('')
  const [baseUrl, setBaseUrl] = useState('https://api.deepseek.com/v1')
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; msg: string } | null>(null)
  const [toast, setToast] = useState<{ msg: string; type: 'success' | 'error' } | null>(null)

  const showToast = (msg: string, type: 'success' | 'error' = 'success') => {
    setToast({ msg, type })
    setTimeout(() => setToast(null), 3000)
  }

  const fetchConfigs = async () => {
    const res = await listAiConfigs()
    if (res.ok) setConfigs(res.data)
  }

  useEffect(() => { fetchConfigs() }, [])

  const handleProviderChange = (name: string) => {
    setProvider(name)
    const p = PROVIDERS.find((x) => x.name === name)
    if (p) setBaseUrl(p.baseUrl)
  }

  const handleSave = async () => {
    if (!provider || !model || !apiKey) return
    setSaving(true)
    try {
      const res = await saveAiConfig({ provider, model, api_key: apiKey, base_url: baseUrl || undefined })
      if (res.ok) {
        showToast('AI配置保存成功')
        await fetchConfigs()
      } else {
        showToast(res.error?.detail || '保存失败', 'error')
      }
    } catch {
      showToast('网络错误，请检查后端服务', 'error')
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async () => {
    if (!provider || !model || !apiKey) return
    setTesting(true)
    setTestResult(null)
    try {
      const res = await testAiConnection({ provider, model, api_key: apiKey, base_url: baseUrl || undefined })
      if (res.ok) {
        setTestResult({ ok: true, msg: `连接成功：${res.data.response}` })
      } else {
        setTestResult({ ok: false, msg: res.error?.detail || res.message || '连接失败' })
      }
    } catch (err: any) {
      // 从 axios 错误响应中提取真实错误信息
      const serverError = err?.response?.data?.error?.detail
        || err?.response?.data?.message
        || err?.message
      const status = err?.response?.status
      let msg = '网络错误'
      if (status === 422) {
        msg = '请求参数验证失败，请检查表单填写是否完整'
      } else if (status === 0 || !err?.response) {
        msg = '无法连接到服务器，请确认后端服务已启动（localhost:8000）'
      } else if (serverError) {
        msg = `连接失败：${serverError}`
      } else {
        msg = `请求失败（HTTP ${status || '未知'}）`
      }
      setTestResult({ ok: false, msg })
    } finally {
      setTesting(false)
    }
  }

  return (
    <div style={{ maxWidth: 700 }}>
      {toast && <div className={`toast toast-${toast.type}`}>{toast.msg}</div>}

      <div className="page-header">
        <h2>AI 配置</h2>
      </div>

      {/* 已有配置 */}
      {configs.length > 0 && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-title">已配置的 AI 服务</div>
          {configs.map((c) => (
            <div key={c.id} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '8px 0', borderBottom: '1px solid var(--border)' }}>
              <div>
                <strong>{c.provider}</strong> / {c.model}
                {c.is_active && <span className="badge badge-completed" style={{ marginLeft: 8 }}>激活</span>}
              </div>
              <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>{c.api_key_masked}</span>
            </div>
          ))}
        </div>
      )}

      {/* 新建/编辑配置 */}
      <div className="card">
        <div className="card-title">配置 AI 服务</div>
        <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 16 }}>
          支持 OpenAI 兼容接口（DeepSeek、Moonshot、硅基流动等）。API Key 将加密保存。
        </p>

        <div className="grid-2">
          <div className="form-group">
            <label>AI 提供商</label>
            <select value={provider} onChange={(e) => handleProviderChange(e.target.value)}>
              {PROVIDERS.map((p) => (
                <option key={p.name} value={p.name}>{p.name}</option>
              ))}
            </select>
          </div>
          <div className="form-group">
            <label>模型名称</label>
            <input value={model} onChange={(e) => setModel(e.target.value)}
              placeholder="deepseek-chat / gpt-4o / moonshot-v1-8k" />
          </div>
        </div>

        <div className="form-group">
          <label>API Key</label>
          <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)}
            placeholder="sk-xxxxx" />
        </div>

        <div className="form-group">
          <label>Base URL</label>
          <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="https://api.openai.com/v1" />
        </div>

        {testResult && (
          <div style={{
            padding: 10, borderRadius: 6, marginBottom: 12, fontSize: 13,
            background: testResult.ok ? 'var(--success)' : 'var(--danger)', color: 'white'
          }}>
            {testResult.msg}
          </div>
        )}

        <div style={{ display: 'flex', gap: 12 }}>
          <button className="btn btn-primary" onClick={handleSave} disabled={saving || !apiKey}>
            {saving ? '保存中...' : '💾 保存配置'}
          </button>
          <button className="btn btn-secondary" onClick={handleTest} disabled={testing || !apiKey}>
            {testing ? '测试中...' : '🔗 测试连接'}
          </button>
        </div>
      </div>
    </div>
  )
}
