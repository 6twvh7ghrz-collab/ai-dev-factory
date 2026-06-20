import { useState, useEffect, useCallback, useRef } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { getProject, updateRequirements, deleteProject, type ProjectDetail } from '../api/projects'
import { analyzeRequirements, generateModules, generateTasks, getAnalysis, listModules } from '../api/analysis'
import { listTasks, type Task, completeTask, retryTask } from '../api/tasks'
import {
  listBugs, createBug, analyzeBug, generateFixPrompt,
  updateBugStatus, saveExecutionResult, saveTestResult, getBug,
  type Bug, type BugStatusLog
} from '../api/bugs'
import {
  startExecutor, pauseExecutor, resumeExecutor, stopExecutor,
  getExecutorStatus, getExecutorQueue, getExecutionLogs, getExecutions, getPreflight,
  previewAICommand, executeReadonlyAICommand, executeAICommand,
  getProjectExecutionConfig,
  getStartDecision,
  type ExecutorStatus, type ExecutorRun, type QueueStatus, type StartResponse, type PreflightResponse,
  type AICommandPreviewResponse,
  type AICommandExecuteReadonlyResponse,
  type AICommandExecuteResponse,
  type ShowStatusData, type DiagnoseBlockerData,
  type ProjectExecutionConfig,
  type StartDecisionResponse, type StartDecision,
} from '../api/executor'
import {
  generatePlanPreview,
  approvalPreview, approve, rejectPlan,
  type PlannerPreviewResponse, type PlanPreview, type TaskPlanItem,
  type ApprovalPreviewResponse, type ApproveResponse,
  type TaskApprovalItem,
} from '../api/planner'

const STATUS_LABELS: Record<string, string> = {
  draft: '草稿', analyzing: '分析中', generated: '方案已生成',
  developing: '开发中', testing: '测试中', completed: '已完成', paused: '已暂停',
}

const TASK_STATUS: Record<string, string> = {
  pending: '待执行', claiming: '正在领取', executing: '开发中',
  testing: '测试中', repairing: '自动修复中', waiting_merge: '等待合并',
  waiting_test: '等待测试', test_failed: '测试失败', completed: '已完成',
  paused: '已暂停', blocked: '已阻塞', cancelled: '已取消',
  failed: '执行失败', analyzing: '分析中',
}

const TASK_STATUS_COLOR: Record<string, string> = {
  pending: 'badge-draft', claiming: 'badge-info', executing: 'badge-accent',
  testing: 'badge-warning', repairing: 'badge-danger', waiting_merge: 'badge-info',
  completed: 'badge-success', paused: 'badge-draft', blocked: 'badge-critical',
  cancelled: 'badge-draft', failed: 'badge-danger', test_failed: 'badge-danger',
}

const READINESS_STATUS_LABEL: Record<string, string> = {
  draft: '草稿',
  needs_planning: '等待工程规划',
  ready: '已准备，可执行',
  executing: '执行中',
  testing: '测试中',
  completed: '已完成',
  blocked: '已阻塞',
}

const READINESS_STATUS_COLOR: Record<string, string> = {
  draft: 'badge-draft',
  needs_planning: 'badge-warning',
  ready: 'badge-success',
  executing: 'badge-accent',
  testing: 'badge-warning',
  completed: 'badge-info',
  blocked: 'badge-critical',
}

const EXECUTOR_STATUS_LABEL: Record<string, string> = {
  idle: '待机', starting: '启动中', scanning: '扫描任务',
  claiming: '领取任务', executing: '开发中', testing: '测试中',
  repairing: '修复中', paused: '已暂停', stopping: '正在停止',
  completed: '已完成', blocked: '已阻塞', failed: '执行失败',
}

const EXECUTOR_STATUS_COLOR: Record<string, string> = {
  idle: 'badge-draft', starting: 'badge-info', scanning: 'badge-info',
  claiming: 'badge-accent', executing: 'badge-accent', testing: 'badge-warning',
  repairing: 'badge-danger', paused: 'badge-warning', stopping: 'badge-warning',
  completed: 'badge-success', blocked: 'badge-critical', failed: 'badge-danger',
}

// 执行器终态集合：进入这些状态后需要刷新任务列表
const terminalRunStatuses = new Set(['completed', 'blocked', 'failed', 'paused'])

// 自然语言指令中文映射
const INTENT_LABELS: Record<string, string> = {
  start_development: '开始自动开发',
  generate_plan: '生成工程规划',
  diagnose_blocker: '检查阻塞原因',
  show_status: '查看执行状态',
  pause_executor: '暂停执行',
  resume_executor: '恢复执行',
  stop_executor: '停止执行',
  unknown: '无法识别',
}

// V1.1 只读意图白名单
const READONLY_INTENTS = new Set(['show_status', 'diagnose_blocker'])
// V1.2 已启用执行按钮的写意图
const ENABLED_WRITE_INTENTS = new Set(['start_development'])
// 尚未开放的写意图
const DISABLED_WRITE_INTENTS = new Set(['generate_plan', 'pause_executor', 'resume_executor', 'stop_executor'])

// 状态中文映射
const RUN_STATUS_LABELS: Record<string, string> = {
  idle: '空闲', starting: '启动中', scanning: '扫描中', claiming: '领取中',
  executing: '执行中', testing: '测试中', repairing: '修复中',
  paused: '已暂停', completed: '已完成', blocked: '已阻塞', failed: '失败',
  stopping: '停止中',
}

// V1.2.2 启动决策标签和颜色
const DECISION_LABELS: Record<StartDecision, string> = {
  EXECUTE_READY_TASKS: '可执行',
  PLAN_EXISTING_TASKS: '待规划',
  GENERATE_TASKS: '待生成任务',
  BIND_WORKSPACE: '待绑定工作区',
  WAIT_DEPENDENCIES: '等待依赖',
  REQUEST_APPROVAL: '需人工审批',
  ALREADY_RUNNING: '运行中',
  PROJECT_COMPLETED: '已完成',
  BLOCK_UNSAFE: '安全阻塞',
}

const DECISION_COLORS: Record<StartDecision, string> = {
  EXECUTE_READY_TASKS: '#16a34a',
  PLAN_EXISTING_TASKS: '#d97706',
  GENERATE_TASKS: '#6b7280',
  BIND_WORKSPACE: '#6b7280',
  WAIT_DEPENDENCIES: '#d97706',
  REQUEST_APPROVAL: '#ef4444',
  ALREADY_RUNNING: '#2563eb',
  PROJECT_COMPLETED: '#16a34a',
  BLOCK_UNSAFE: '#ef4444',
}

const DECISION_SUGGESTIONS: Record<StartDecision, string> = {
  EXECUTE_READY_TASKS: '开始执行',
  PLAN_EXISTING_TASKS: '开始规划',
  GENERATE_TASKS: '生成新任务',
  BIND_WORKSPACE: '绑定工作区',
  WAIT_DEPENDENCIES: '查看依赖',
  REQUEST_APPROVAL: '确认风险',
  ALREADY_RUNNING: '查看状态',
  PROJECT_COMPLETED: '新增需求',
  BLOCK_UNSAFE: '查看原因',
}

/** 只读执行结果卡片组件 */
function ReadonlyResultCard({ result }: { result: AICommandExecuteReadonlyResponse }) {
  if (!result.data) return null
  const { intent, data } = result

  if (intent === 'show_status') {
    const d = data as ShowStatusData
    return (
      <div style={{ marginTop: 12, padding: 12, background: 'rgba(34,197,94,0.08)', borderRadius: 8, border: '1px solid rgba(34,197,94,0.2)' }}>
        <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 10, color: '#16a34a' }}>📊 项目执行状态</div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, fontSize: 13 }}>
          <div><span style={{ color: 'var(--text-muted)' }}>项目名称：</span><span>{d.project_name}</span></div>
          <div><span style={{ color: 'var(--text-muted)' }}>运行状态：</span>
            <span style={{ fontWeight: 600, color: d.run_status === 'idle' ? 'var(--text-muted)' : 'var(--accent)' }}>
              {RUN_STATUS_LABELS[d.run_status] || d.run_status}
            </span>
          </div>
          <div><span style={{ color: 'var(--text-muted)' }}>当前任务：</span>
            <span>{d.current_task ? `#${d.current_task.task_id} ${d.current_task.title}` : '无'}</span>
          </div>
          <div><span style={{ color: 'var(--text-muted)' }}>Worker：</span><span>{d.worker_count}个活跃</span></div>
          <div><span style={{ color: 'var(--text-muted)' }}>待执行：</span><span>{d.pending_count}</span></div>
          <div><span style={{ color: 'var(--text-muted)' }}>可执行：</span><span style={{ color: '#16a34a' }}>{d.ready_count}</span></div>
          <div><span style={{ color: 'var(--text-muted)' }}>待规划：</span><span style={{ color: 'var(--warning)' }}>{d.needs_planning_count}</span></div>
          <div><span style={{ color: 'var(--text-muted)' }}>已完成：</span><span>{d.completed_count}</span></div>
          <div><span style={{ color: 'var(--text-muted)' }}>已阻塞：</span><span style={{ color: '#ef4444' }}>{d.blocked_count}</span></div>
          <div><span style={{ color: 'var(--text-muted)' }}>总任务：</span><span>{d.total_count}</span></div>
          <div><span style={{ color: 'var(--text-muted)' }}>活跃租约：</span><span>{d.active_leases}</span></div>
          <div><span style={{ color: 'var(--text-muted)' }}>资源锁：</span><span>{d.active_resource_locks}</span></div>
        </div>
        {d.last_error && (
          <div style={{ marginTop: 8, padding: 6, background: 'rgba(239,68,68,0.1)', borderRadius: 4, fontSize: 12, color: '#ef4444' }}>
            最近错误：{d.last_error}
          </div>
        )}
      </div>
    )
  }

  if (intent === 'diagnose_blocker') {
    const d = data as DiagnoseBlockerData
    return (
      <div style={{ marginTop: 12, padding: 12, background: 'rgba(245,158,11,0.08)', borderRadius: 8, border: '1px solid rgba(245,158,11,0.2)' }}>
        <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 6, color: '#d97706' }}>🔍 阻塞诊断结果</div>
        <div style={{ fontSize: 13, marginBottom: 10, padding: 6, background: 'rgba(245,158,11,0.15)', borderRadius: 4 }}>
          状态：<span style={{ fontWeight: 600, color: d.status === 'clear' ? '#16a34a' : d.status === 'ready' ? '#2563eb' : '#d97706' }}>
            {d.status === 'clear' ? '畅通' : d.status === 'ready' ? '就绪' : '阻塞'}
          </span>
          <span style={{ marginLeft: 8 }}>{d.summary}</span>
        </div>

        {/* 分类统计 */}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 4, fontSize: 12, marginBottom: 8 }}>
          {d.categories.needs_planning > 0 && (
            <div>📝 待规划：<span style={{ fontWeight: 600, color: 'var(--warning)' }}>{d.categories.needs_planning}</span></div>
          )}
          {d.categories.dependency_incomplete > 0 && (
            <div>🔗 依赖未完成：<span style={{ fontWeight: 600, color: '#ef4444' }}>{d.categories.dependency_incomplete}</span></div>
          )}
          {d.categories.active_lease > 0 && (
            <div>🔒 活跃Lease：<span style={{ fontWeight: 600, color: 'var(--warning)' }}>{d.categories.active_lease}</span></div>
          )}
          {d.categories.missing_files > 0 && (
            <div>📁 缺少文件：<span style={{ fontWeight: 600 }}>{d.categories.missing_files}</span></div>
          )}
          {d.categories.manual_approval > 0 && (
            <div>👤 需人工：<span style={{ fontWeight: 600 }}>{d.categories.manual_approval}</span></div>
          )}
          {d.categories.missing_prompt > 0 && (
            <div>🤖 缺提示词：<span style={{ fontWeight: 600 }}>{d.categories.missing_prompt}</span></div>
          )}
        </div>

        <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 6 }}>
          总计 {d.total_pending} 个待处理任务，{d.runnable_count} 个可执行，{d.blocked_count} 个阻塞
        </div>

        {/* 阻塞任务列表（前10个） */}
        {d.tasks && d.tasks.length > 0 && (
          <div style={{ maxHeight: 200, overflowY: 'auto' }}>
            <table style={{ width: '100%', fontSize: 12, borderCollapse: 'collapse' }}>
              <thead>
                <tr style={{ borderBottom: '1px solid var(--border)' }}>
                  <th style={{ textAlign: 'left', padding: '4px 6px', color: 'var(--text-muted)' }}>任务</th>
                  <th style={{ textAlign: 'left', padding: '4px 6px', color: 'var(--text-muted)' }}>阻塞原因</th>
                  <th style={{ textAlign: 'left', padding: '4px 6px', color: 'var(--text-muted)' }}>准备状态</th>
                </tr>
              </thead>
              <tbody>
                {d.tasks.slice(0, 10).map(t => (
                  <tr key={t.task_id} style={{ borderBottom: '1px solid rgba(128,128,128,0.1)' }}>
                    <td style={{ padding: '4px 6px' }}>#{t.task_id} {t.title}</td>
                    <td style={{ padding: '4px 6px', color: '#ef4444' }}>{t.reason}</td>
                    <td style={{ padding: '4px 6px' }}>
                      <span style={{
                        padding: '1px 6px', borderRadius: 3, fontSize: 11,
                        background: t.readiness_status === 'ready' ? 'rgba(34,197,94,0.15)' : 'rgba(245,158,11,0.15)',
                        color: t.readiness_status === 'ready' ? '#16a34a' : '#d97706',
                      }}>
                        {t.readiness_status === 'ready' ? '就绪' : t.readiness_status === 'needs_planning' ? '待规划' : t.readiness_status}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    )
  }

  return null
}

/** V1.4 规划预览卡片组件（含审批功能） */
function PlanPreviewCard({
  preview,
  callRecord,
  previewId,
  expiresAt,
  onClose,
  onApprovalPreview,
  onReject,
  approvalLoading,
  approvalError,
}: {
  preview: PlanPreview;
  callRecord: PlannerPreviewResponse['call_record'];
  previewId?: string;
  expiresAt?: string;
  onClose: () => void;
  onApprovalPreview: (selectedTaskIds: number[]) => void;
  onReject: () => void;
  approvalLoading: boolean;
  approvalError: string;
}) {
  const [selectedTasks, setSelectedTasks] = useState<Set<number>>(new Set())

  const toggleTask = (taskId: number) => {
    setSelectedTasks(prev => {
      const next = new Set(prev)
      if (next.has(taskId)) {
        next.delete(taskId)
      } else {
        next.add(taskId)
      }
      return next
    })
  }

  const isHighRiskTask = (task: TaskPlanItem) => task.requires_approval
  const isMediumRiskTask = (task: TaskPlanItem) => task.risk_level === 'MEDIUM'

  const safeCount = preview.tasks.filter(t => !isHighRiskTask(t) && !isMediumRiskTask(t)).length
  const mediumRiskCount = preview.tasks.filter(t => isMediumRiskTask(t)).length
  const highRiskCount = preview.tasks.filter(t => isHighRiskTask(t)).length

  return (
    <div className="card" style={{
      marginBottom: 12, padding: 16,
      borderLeft: '4px solid #2563eb',
      background: 'rgba(37,99,235,0.04)',
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <div>
          <span style={{ fontWeight: 600, fontSize: 16, color: '#2563eb' }}>
            📐 AI 工程规划预览
          </span>
          {previewId && (
            <span style={{ fontSize: 11, color: 'var(--text-muted)', marginLeft: 8 }}>
              ID: {previewId.slice(0, 8)}...
            </span>
          )}
          {expiresAt && (
            <span style={{ fontSize: 11, color: 'var(--text-muted)', marginLeft: 8 }}>
              过期: {new Date(expiresAt).toLocaleString()}
            </span>
          )}
        </div>
        <button
          onClick={onClose}
          style={{
            padding: '4px 12px', fontSize: 12,
            border: '1px solid var(--border)', borderRadius: 4,
            background: 'var(--bg-secondary)', cursor: 'pointer',
          }}
        >
          关闭
        </button>
      </div>

      {/* 项目总体方案 */}
      <div style={{ marginBottom: 12 }}>
        <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 4 }}>项目总体方案</div>
        <div style={{ fontSize: 13, color: 'var(--text-muted)', lineHeight: 1.6 }}>
          {preview.project_summary}
        </div>
      </div>

      {/* 推荐架构 */}
      <div style={{ marginBottom: 12 }}>
        <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 4 }}>推荐架构</div>
        <div style={{ fontSize: 13, color: 'var(--text-muted)', lineHeight: 1.6 }}>
          {preview.recommended_architecture}
        </div>
      </div>

      {/* 执行顺序 */}
      {preview.execution_order && preview.execution_order.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 4 }}>执行顺序</div>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            {preview.execution_order.map((tid, i) => (
              <span key={i} style={{
                padding: '2px 10px', fontSize: 12,
                background: 'rgba(37,99,235,0.1)', borderRadius: 4,
                color: '#2563eb', fontWeight: 500,
              }}>
                #{tid}{i < preview.execution_order.length - 1 ? ' →' : ''}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* 任务统计 */}
      <div style={{ marginBottom: 8, fontSize: 13 }}>
        <span style={{ color: '#16a34a' }}>低风险: {safeCount}</span>
        <span style={{ margin: '0 8px', color: 'var(--border)' }}>|</span>
        <span style={{ color: '#d97706' }}>中风险: {mediumRiskCount}</span>
        <span style={{ margin: '0 8px', color: 'var(--border)' }}>|</span>
        <span style={{ color: '#ef4444' }}>高风险: {highRiskCount}</span>
        <span style={{ margin: '0 8px', color: 'var(--border)' }}>|</span>
        <span>已选: {selectedTasks.size}</span>
      </div>

      {/* 每个任务的方案（含勾选框） */}
      <div style={{ marginBottom: 12 }}>
        <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 8 }}>
          任务规划详情
          <button
            onClick={() => {
              const allSafe = preview.tasks.filter(t => !isHighRiskTask(t) && !isMediumRiskTask(t)).map(t => t.task_id)
              setSelectedTasks(new Set(allSafe))
            }}
            style={{ marginLeft: 8, fontSize: 11, padding: '1px 8px', cursor: 'pointer' }}
          >
            全选低风险
          </button>
          <button
            onClick={() => setSelectedTasks(new Set())}
            style={{ marginLeft: 4, fontSize: 11, padding: '1px 8px', cursor: 'pointer' }}
          >
            取消全选
          </button>
        </div>
        {preview.tasks.map(task => (
          <TaskPlanItemCard
            key={task.task_id}
            task={task}
            selected={selectedTasks.has(task.task_id)}
            onToggle={() => toggleTask(task.task_id)}
          />
        ))}
      </div>

      {/* 全局风险 */}
      {preview.global_risks && preview.global_risks.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 4, color: '#ef4444' }}>⚠️ 全局风险</div>
          <ul style={{ margin: 0, paddingLeft: 20, fontSize: 13 }}>
            {preview.global_risks.map((r, i) => (
              <li key={i} style={{ color: 'var(--text-muted)', marginBottom: 2 }}>{r}</li>
            ))}
          </ul>
        </div>
      )}

      {/* 审批事项 */}
      {preview.approval_items && preview.approval_items.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 4, color: '#d97706' }}>📝 需要审批的事项</div>
          <ul style={{ margin: 0, paddingLeft: 20, fontSize: 13 }}>
            {preview.approval_items.map((a, i) => (
              <li key={i} style={{ color: 'var(--text-muted)', marginBottom: 2 }}>{a}</li>
            ))}
          </ul>
        </div>
      )}

      {/* 错误信息 */}
      {approvalError && (
        <div style={{
          marginBottom: 12, padding: '8px 12px',
          background: 'rgba(239,68,68,0.1)', borderRadius: 6, fontSize: 13,
          color: '#ef4444',
        }}>
          ❌ {approvalError}
        </div>
      )}

      {/* 下一步 */}
      <div style={{
        marginBottom: 12, padding: '8px 12px',
        background: 'rgba(37,99,235,0.08)', borderRadius: 6, fontSize: 13,
      }}>
        <span style={{ fontWeight: 600 }}>下一步：</span>
        <span style={{ color: 'var(--text-muted)' }}>
          {preview.next_step === 'review_plan' && '请审查规划方案'}
          {preview.next_step === 'approve_and_execute' && '审批后执行'}
          {preview.next_step === 'request_manual_review' && '需要人工审查'}
        </span>
      </div>

      {/* 模型调用记录 */}
      {callRecord && (
        <details style={{ fontSize: 12, color: 'var(--text-muted)' }}>
          <summary style={{ cursor: 'pointer' }}>模型调用记录</summary>
          <div style={{ marginTop: 4, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 2 }}>
            <div>Provider: {callRecord.provider}</div>
            <div>Model: {callRecord.model}</div>
            <div>Request ID: {callRecord.request_id}</div>
            <div>Tokens: in={callRecord.input_tokens} out={callRecord.output_tokens}</div>
            <div>Started: {callRecord.started_at}</div>
            <div>Finished: {callRecord.finished_at}</div>
            <div>Success: {callRecord.success ? '✅' : '❌'}</div>
          </div>
        </details>
      )}

      {/* 底部按钮 V1.4 */}
      <div style={{ marginTop: 12, display: 'flex', gap: 8, alignItems: 'center' }}>
        <button
          className="btn btn-accent"
          onClick={() => onApprovalPreview(Array.from(selectedTasks))}
          disabled={approvalLoading || selectedTasks.size === 0}
          style={{
            padding: '6px 16px', fontSize: 13,
            opacity: approvalLoading || selectedTasks.size === 0 ? 0.5 : 1,
            cursor: approvalLoading || selectedTasks.size === 0 ? 'not-allowed' : 'pointer',
          }}
        >
          {approvalLoading ? '审批中...' : `审批选中任务 (${selectedTasks.size})`}
        </button>
        <button
          className="btn btn-outline"
          onClick={onReject}
          disabled={approvalLoading}
          style={{ padding: '6px 16px', fontSize: 13 }}
        >
          拒绝本次规划
        </button>
        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
          选择低风险任务后可审批转ready，高风险任务保持needs_planning
        </span>
      </div>
    </div>
  )
}

/** V1.4 单个任务的规划卡片（含勾选框） */
function TaskPlanItemCard({
  task,
  selected,
  onToggle,
}: {
  task: TaskPlanItem;
  selected: boolean;
  onToggle: () => void;
}) {
  const isHighRisk = task.requires_approval
  return (
    <div style={{
      marginBottom: 8, padding: 10,
      border: selected
        ? '2px solid #2563eb'
        : isHighRisk
          ? '1px solid rgba(239,68,68,0.3)'
          : '1px solid var(--border)',
      borderRadius: 6,
      background: selected
        ? 'rgba(37,99,235,0.06)'
        : isHighRisk
          ? 'rgba(239,68,68,0.04)'
          : 'var(--bg-secondary)',
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <input
            type="checkbox"
            checked={selected}
            onChange={onToggle}
            disabled={isHighRisk}
            title={isHighRisk ? '此任务需要额外人工方案，暂不能进入自动执行' : '选择此任务进行审批'}
            style={{ cursor: isHighRisk ? 'not-allowed' : 'pointer' }}
          />
          <span style={{ fontWeight: 600, fontSize: 13 }}>
            #{task.task_id} {task.title}
          </span>
        </div>
        <div style={{ display: 'flex', gap: 6 }}>
          {isHighRisk && (
            <span style={{
              padding: '1px 8px', fontSize: 11,
              background: 'rgba(239,68,68,0.15)', color: '#ef4444',
              borderRadius: 4, fontWeight: 500,
            }}>
              需要人工审批
            </span>
          )}
          <span style={{
            padding: '1px 8px', fontSize: 11,
            background: task.recommended_status === 'ready'
              ? 'rgba(34,197,94,0.15)' : 'rgba(245,158,11,0.15)',
            color: task.recommended_status === 'ready' ? '#16a34a' : '#d97706',
            borderRadius: 4, fontWeight: 500,
          }}>
            {task.recommended_status === 'ready' ? '建议就绪' : '待规划'}
          </span>
        </div>
      </div>

      {isHighRisk && (
        <div style={{ fontSize: 11, color: '#ef4444', marginBottom: 4, padding: '2px 8px', background: 'rgba(239,68,68,0.06)', borderRadius: 4 }}>
          ⚠️ 此任务需要额外人工方案，暂不能进入自动执行
        </div>
      )}

      {/* 实现策略 */}
      <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 6 }}>
        {task.implementation_strategy}
      </div>

      {/* 数据来源策略 */}
      {task.data_source_strategy && (
        <div style={{ fontSize: 12, marginBottom: 6 }}>
          <span style={{ fontWeight: 500 }}>数据策略：</span>
          <span style={{ color: '#2563eb' }}>{task.data_source_strategy.primary}</span>
          {task.data_source_strategy.fallbacks && task.data_source_strategy.fallbacks.length > 0 && (
            <span style={{ color: 'var(--text-muted)' }}>
              {' '}｜ 备用：{task.data_source_strategy.fallbacks.join(' / ')}
            </span>
          )}
        </div>
      )}

      {/* 建议文件 */}
      {task.files_to_modify_suggestion && task.files_to_modify_suggestion.length > 0 && (
        <div style={{ fontSize: 12, marginBottom: 4 }}>
          <span style={{ fontWeight: 500 }}>建议文件：</span>
          <span style={{ color: 'var(--text-muted)' }}>{task.files_to_modify_suggestion.join(', ')}</span>
        </div>
      )}

      {/* 测试策略 */}
      {task.test_strategy && task.test_strategy.length > 0 && (
        <div style={{ fontSize: 12, marginBottom: 4 }}>
          <span style={{ fontWeight: 500 }}>测试策略：</span>
          <span style={{ color: 'var(--text-muted)' }}>{task.test_strategy.join('; ')}</span>
        </div>
      )}

      {/* 风险 */}
      {task.risks && task.risks.length > 0 && (
        <div style={{ fontSize: 12 }}>
          <span style={{ fontWeight: 500, color: '#ef4444' }}>风险：</span>
          <ul style={{ margin: '2px 0 0 0', paddingLeft: 18 }}>
            {task.risks.map((r, i) => (
              <li key={i} style={{ color: '#ef4444', fontSize: 11 }}>{r}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

const BUG_STATUS: Record<string, string> = {
  reported: '已报告', analyzing: '分析中', analyzed: '已完成分析',
  fix_ready: '修复指令已生成', fixing: '修复中',
  waiting_test: '等待测试', resolved: '已解决',
  reopened: '已重新打开', closed: '已关闭',
}

const BUG_SEVERITY: Record<string, string> = {
  critical: '严重', high: '高', medium: '中', low: '低',
}

const BUG_STEP_ORDER = ['reported', 'analyzing', 'analyzed', 'fix_ready', 'fixing', 'waiting_test', 'resolved']
const BUG_STEP_LABELS = ['已报告', 'AI分析', '生成指令', '执行修复', '回归测试', '已解决']

type Tab = 'requirements' | 'analysis' | 'modules' | 'tasks' | 'bugs'

export default function ProjectDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const projectId = Number(id)

  const [project, setProject] = useState<ProjectDetail | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [activeTab, setActiveTab] = useState<Tab>('requirements')
  const [loading, setLoading] = useState(false)
  const [completingTaskId, setCompletingTaskId] = useState<number | null>(null)
  const [submittingBug, setSubmittingBug] = useState(false)
  const [analysisData, setAnalysisData] = useState<Record<string, unknown> | null>(null)
  const [modulesData, setModulesData] = useState<any[] | null>(null)
  const [tasks, setTasks] = useState<Task[]>([])
  const [bugs, setBugs] = useState<Bug[]>([])
  const [toast, setToast] = useState<{ msg: string; type: 'success' | 'error' } | null>(null)

  // ── 执行器状态 ──
  const [executorStatus, setExecutorStatus] = useState<ExecutorStatus | null>(null)
  const [executorQueue, setExecutorQueue] = useState<QueueStatus | null>(null)
  const [executorLoading, setExecutorLoading] = useState(false)
  const [executorAction, setExecutorAction] = useState<string | null>(null)
  const [showStartDialog, setShowStartDialog] = useState(false)
  const [preflight, setPreflight] = useState<PreflightResponse | null>(null)
  const [retryingTaskId, setRetryingTaskId] = useState<number | null>(null)
  // ── 自然语言指令 ──
  const [aiCommandText, setAiCommandText] = useState('')
  const [aiCommandResult, setAiCommandResult] = useState<AICommandPreviewResponse | null>(null)
  const [aiCommandLoading, setAiCommandLoading] = useState(false)
  // V1.1 只读执行
  const [readonlyResult, setReadonlyResult] = useState<AICommandExecuteReadonlyResponse | null>(null)
  const [readonlyLoading, setReadonlyLoading] = useState(false)
  // V1.2 写指令执行
  const [writeResult, setWriteResult] = useState<AICommandExecuteResponse | null>(null)
  const [writeLoading, setWriteLoading] = useState(false)
  const [showLogDialog, setShowLogDialog] = useState(false)
  const [logLines, setLogLines] = useState<Array<{time: string; text: string}>>([])
  // ── 项目执行配置 ──
  const [execConfig, setExecConfig] = useState<ProjectExecutionConfig | null>(null)
  // ── V1.2.2 启动决策 ──
  const [startDecision, setStartDecision] = useState<StartDecisionResponse | null>(null)
  const [decisionLoading, setDecisionLoading] = useState(false)
  // ── V1.4 工程规划预览与审批 ──
  const [planPreviewResult, setPlanPreviewResult] = useState<PlannerPreviewResponse | null>(null)
  const [planPreviewLoading, setPlanPreviewLoading] = useState(false)
  const [showPlanConfirm, setShowPlanConfirm] = useState(false)
  const [approvalLoading, setApprovalLoading] = useState(false)
  const [approvalError, setApprovalError] = useState('')
  const [showApprovalConfirm, setShowApprovalConfirm] = useState(false)
  const [approvalPreviewData, setApprovalPreviewData] = useState<ApprovalPreviewResponse | null>(null)
  const [approvalResult, setApprovalResult] = useState<ApproveResponse | null>(null)
  const [pendingSelectedTasks, setPendingSelectedTasks] = useState<number[]>([])
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const runStartTimeRef = useRef<number | null>(null)
  const [runDuration, setRunDuration] = useState('--')
  const fastPollingRef = useRef(false)
  const fastPollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Bug form
  const [bugTitle, setBugTitle] = useState('')
  const [bugError, setBugError] = useState('')
  const [bugSteps, setBugSteps] = useState('')
  const [bugExpected, setBugExpected] = useState('')
  const [bugActual, setBugActual] = useState('')

  // Bug workspace
  const [selectedBugId, setSelectedBugId] = useState<number | null>(null)
  const [selectedBug, setSelectedBug] = useState<Bug | null>(null)
  const [analyzingBugId, setAnalyzingBugId] = useState<number | null>(null)
  const [generatingFixId, setGeneratingFixId] = useState<number | null>(null)
  const [savingExecId, setSavingExecId] = useState<number | null>(null)
  const [savingTestId, setSavingTestId] = useState<number | null>(null)
  const [updatingStatusId, setUpdatingStatusId] = useState<number | null>(null)
  const [executionText, setExecutionText] = useState('')
  const [filesChangedText, setFilesChangedText] = useState('')
  const [testResultText, setTestResultText] = useState('')
  const [remainingIssuesText, setRemainingIssuesText] = useState('')
  const [testNotes, setTestNotes] = useState('')
  const [testChecklist, setTestChecklist] = useState<Record<string, boolean>>({})
  const [failReason, setFailReason] = useState('')
  const [showFailDialog, setShowFailDialog] = useState(false)
  const [bugStatusLogs, setBugStatusLogs] = useState<BugStatusLog[]>([])

  const showToast = (msg: string, type: 'success' | 'error' = 'success') => {
    setToast({ msg, type })
    setTimeout(() => setToast(null), 3000)
  }

  // 监听子组件触发的 toast 事件
  useEffect(() => {
    const handler = (e: Event) => {
      const { msg, type } = (e as CustomEvent).detail
      showToast(msg, type)
    }
    window.addEventListener('app-toast', handler)
    return () => window.removeEventListener('app-toast', handler)
  }, [])

  const fetchProject = useCallback(async () => {
    try {
      setLoadError(null)
      const res = await getProject(projectId)
      if (res.ok) setProject(res.data)
      else setLoadError(res.error?.detail || '项目加载失败')
    } catch {
      setLoadError('网络错误，请检查后端服务是否运行')
    }
  }, [projectId])

  const fetchAnalysis = useCallback(async () => {
    try {
      const res = await getAnalysis(projectId)
      if (res.ok) setAnalysisData(res.data as Record<string, unknown> | null)
    } catch { /* no analysis yet */ }
  }, [projectId])

  const fetchModules = useCallback(async () => {
    try {
      const res = await listModules(projectId)
      if (res.ok) setModulesData(res.data as any[] | null)
    } catch { /* no modules yet */ }
  }, [projectId])

  const fetchTasks = useCallback(async () => {
    try {
      const res = await listTasks(projectId)
      if (res.ok) setTasks(res.data)
    } catch { /* no tasks yet */ }
  }, [projectId])

  const fetchBugs = useCallback(async () => {
    try {
      const res = await listBugs(projectId)
      if (res.ok) setBugs(res.data)
    } catch { /* no bugs yet */ }
  }, [projectId])

  const fetchExecConfig = useCallback(async () => {
    try {
      const res = await getProjectExecutionConfig(projectId)
      if (res.ok) setExecConfig(res.data)
    } catch { /* config not available */ }
  }, [projectId])

  // V1.2.2 获取启动决策
  const fetchStartDecision = useCallback(async () => {
    try {
      const res = await getStartDecision(projectId)
      if (res.ok && res.data) setStartDecision(res.data)
    } catch { /* silent */ }
  }, [projectId])

  useEffect(() => {
    fetchProject()
    fetchAnalysis()
    fetchModules()
    fetchTasks()
    fetchBugs()
    fetchExecConfig()
    fetchStartDecision()
  }, [fetchProject, fetchAnalysis, fetchModules, fetchTasks, fetchBugs, fetchExecConfig, fetchStartDecision])

  // ── 执行器状态轮询 ──
  // 记录上次触发任务列表刷新的 run_id，防止同一 run 重复刷
  const lastTaskFetchRunIdRef = useRef<string | null>(null)

  const pollExecutor = useCallback(async () => {
    try {
      const [statusRes, queueRes, preflightRes] = await Promise.all([
        getExecutorStatus(),
        getExecutorQueue(projectId).catch(() => null),
        getPreflight(projectId).catch(() => null),
      ])
      if (statusRes.ok) {
        setExecutorStatus(statusRes.data)
        // 计算运行时长
        const run = statusRes.data.loop?.run
        const isRunning = run && run.started_at && run.status !== 'idle' && run.status !== 'completed' && run.status !== 'blocked' && run.status !== 'failed'
        const isTerminal = run && terminalRunStatuses.has(run.status)
        if (isRunning) {
          if (!runStartTimeRef.current && run.started_at) {
            runStartTimeRef.current = new Date(run.started_at).getTime()
          }
          const elapsed = Math.floor((Date.now() - (runStartTimeRef.current || 0)) / 1000)
          const h = Math.floor(elapsed / 3600)
          const m = Math.floor((elapsed % 3600) / 60)
          const s = elapsed % 60
          setRunDuration(`${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`)
          // 执行期间也刷新任务列表（获取实时变更）
          fetchTasks()
        } else {
          runStartTimeRef.current = null
          setRunDuration('--')
          // 执行器进入终态时，立即刷新任务列表和 preflight
          if (isTerminal && lastTaskFetchRunIdRef.current !== run.run_id) {
            lastTaskFetchRunIdRef.current = run.run_id
            await fetchTasks()
            // 再次拉取 preflight（终态后 can_start 等字段会变化）
            try {
              const pfRes = await getPreflight(projectId)
              if (pfRes.ok) setPreflight(pfRes.data)
            } catch { /* silent */ }
            // V1.2.2: 刷新启动决策
            try {
              const sdRes = await getStartDecision(projectId)
              if (sdRes.ok && sdRes.data) setStartDecision(sdRes.data)
            } catch { /* silent */ }
          }
          // 如果 run 已经终止，退出快速轮询
          if (fastPollingRef.current && run) {
            fastPollingRef.current = false
            if (fastPollTimerRef.current) {
              clearTimeout(fastPollTimerRef.current)
              fastPollTimerRef.current = null
            }
            restartNormalPolling()
          }
        }
      }
      if (queueRes && queueRes.ok) setExecutorQueue(queueRes.data)
      if (preflightRes && preflightRes.ok) setPreflight(preflightRes.data)
    } catch { /* 轮询静默失败 */ }
  }, [projectId, fetchTasks, terminalRunStatuses])

  const restartNormalPolling = () => {
    if (pollingRef.current) {
      clearInterval(pollingRef.current)
    }
    pollingRef.current = setInterval(pollExecutor, 2500)
  }

  const startFastPolling = () => {
    fastPollingRef.current = true
    // 将轮询间隔缩短到 1s
    if (pollingRef.current) {
      clearInterval(pollingRef.current)
    }
    pollingRef.current = setInterval(pollExecutor, 1000)
    // 30s 后恢复慢速轮询
    if (fastPollTimerRef.current) {
      clearTimeout(fastPollTimerRef.current)
    }
    fastPollTimerRef.current = setTimeout(() => {
      fastPollingRef.current = false
      restartNormalPolling()
    }, 30000)
  }

  useEffect(() => {
    pollExecutor()
    pollingRef.current = setInterval(pollExecutor, 2500)
    return () => {
      if (pollingRef.current) {
        clearInterval(pollingRef.current)
        pollingRef.current = null
      }
      if (fastPollTimerRef.current) {
        clearTimeout(fastPollTimerRef.current)
        fastPollTimerRef.current = null
      }
    }
  }, [pollExecutor])

  // ── 执行器控制 ──
  // V1.2.2: 先获取启动决策，再根据决策分流处理
  const handleStartExecutor = async () => {
    setExecutorLoading(true)
    setExecutorAction('start')
    try {
      // 先获取启动决策
      const decisionRes = await getStartDecision(projectId)
      if (!decisionRes.ok || !decisionRes.data) {
        showToast('获取启动决策失败', 'error')
        setExecutorLoading(false)
        setExecutorAction(null)
        return
      }

      const decision = decisionRes.data
      setStartDecision(decision)

      // 根据决策类型分流处理
      switch (decision.decision) {
        case 'EXECUTE_READY_TASKS':
          // 真正启动执行器
          await doStartExecutor()
          break
        case 'ALREADY_RUNNING':
          showToast(`项目已在运行中 (状态: ${decision.details.active_run_status})`)
          setShowStartDialog(false)
          await pollExecutor()
          if (!fastPollingRef.current) startFastPolling()
          break
        case 'PLAN_EXISTING_TASKS':
          // V1.3: 弹出规划确认窗口
          setShowStartDialog(false)
          setShowPlanConfirm(true)
          break
        case 'GENERATE_TASKS':
          showToast(`${decision.summary}\n请先通过AI生成开发任务`, 'error')
          setShowStartDialog(false)
          break
        case 'BIND_WORKSPACE':
          showToast(`${decision.summary}\n请先在项目执行配置中绑定代码工作区`, 'error')
          setShowStartDialog(false)
          break
        case 'WAIT_DEPENDENCIES':
          showToast(`${decision.summary}\n请等待依赖任务完成后再启动`, 'error')
          setShowStartDialog(false)
          break
        case 'REQUEST_APPROVAL':
          showToast(`⚠️ 高风险项目：${decision.summary}\n需要人工确认后才能执行`, 'error')
          setShowStartDialog(false)
          break
        case 'PROJECT_COMPLETED':
          showToast(`${decision.summary}\n可以创建新需求继续迭代`, 'error')
          setShowStartDialog(false)
          break
        case 'BLOCK_UNSAFE':
          showToast(`🚫 安全阻塞：${decision.summary}`, 'error')
          setShowStartDialog(false)
          break
        default:
          showToast(`未知决策类型: ${decision.decision}`, 'error')
      }
    } catch {
      showToast('启动失败，请检查后端服务', 'error')
    } finally {
      setExecutorLoading(false)
      setExecutorAction(null)
    }
  }

  // 真正启动执行器（仅 EXECUTE_READY_TASKS 时调用）
  const doStartExecutor = async () => {
    try {
      const res = await startExecutor(projectId)
      const data: StartResponse | undefined = res.ok ? res.data : undefined

      if (!res.ok || !data) {
        const code = data?.code || res.error?.code
        const detail = data?.reason || res.error?.detail || '启动失败'
        if (code === 'PROVIDER_UNAVAILABLE') {
          showToast(`AI服务不可用: ${detail}`, 'error')
        } else if (code === 'WORKSPACE_FORBIDDEN') {
          showToast(`工作区安全验证失败: ${detail}`, 'error')
        } else {
          showToast(detail, 'error')
        }
        return
      }

      if (data.already_running) {
        showToast(`执行器已在运行中 (状态: ${data.status || '运行中'})`)
        setShowStartDialog(false)
        await pollExecutor()
        if (!fastPollingRef.current) startFastPolling()
      } else if (data.started) {
        showToast(`自动开发已启动 (run_id: ${data.run_id?.slice(0, 12)}...)`)
        setShowStartDialog(false)
        await pollExecutor()
        startFastPolling()
      } else {
        showToast(`启动异常: ${data.message || '未知状态'}`, 'error')
      }
    } catch {
      showToast('启动失败，请检查后端服务', 'error')
    }
  }

  const handlePauseExecutor = async () => {
    setExecutorLoading(true)
    setExecutorAction('pause')
    try {
      const res = await pauseExecutor()
      if (res.ok) showToast('已暂停')
      else showToast(res.error?.detail || '暂停失败', 'error')
    } catch {
      showToast('暂停失败', 'error')
    } finally {
      setExecutorLoading(false)
      setExecutorAction(null)
      await pollExecutor()
    }
  }

  const handleResumeExecutor = async () => {
    setExecutorLoading(true)
    setExecutorAction('resume')
    try {
      const res = await resumeExecutor()
      if (res.ok) showToast('已继续')
      else showToast(res.error?.detail || '继续失败', 'error')
    } catch {
      showToast('继续失败', 'error')
    } finally {
      setExecutorLoading(false)
      setExecutorAction(null)
      await pollExecutor()
    }
  }

  const handleStopExecutor = async () => {
    if (!confirm('确定要安全停止自动开发循环吗？当前任务将完成后再停止。')) return
    setExecutorLoading(true)
    setExecutorAction('stop')
    try {
      const res = await stopExecutor()
      if (res.ok) showToast('已发送停止信号')
      else showToast(res.error?.detail || '停止失败', 'error')
    } catch {
      showToast('停止失败', 'error')
    } finally {
      setExecutorLoading(false)
      setExecutorAction(null)
      await pollExecutor()
    }
  }

  // ── V1.4 工程规划预览与审批 ──
  const handleGeneratePlanPreview = async () => {
    setShowPlanConfirm(false)
    setPlanPreviewLoading(true)
    setPlanPreviewResult(null)
    setApprovalResult(null)
    setApprovalError('')
    try {
      const res = await generatePlanPreview(projectId)
      if (res.ok && res.data) {
        setPlanPreviewResult(res.data)
        showToast('规划预览已生成', 'success')
      } else {
        const code = res.data?.code || res.error?.code || 'UNKNOWN'
        const detail = res.data?.message || res.error?.detail || '规划预览生成失败'
        showToast(`${detail} (${code})`, 'error')
      }
    } catch {
      showToast('网络错误，请检查后端服务', 'error')
    } finally {
      setPlanPreviewLoading(false)
    }
  }

  const handleClosePlanPreview = () => {
    setPlanPreviewResult(null)
    setShowPlanConfirm(false)
    setApprovalPreviewData(null)
    setApprovalResult(null)
    setApprovalError('')
    setShowApprovalConfirm(false)
  }

  // ── V1.4 审批处理 ──

  const handleApprovalPreview = async (selectedTaskIds: number[]) => {
    if (!planPreviewResult?.preview_id) {
      showToast('缺少预览ID，请重新生成规划', 'error')
      return
    }
    setPendingSelectedTasks(selectedTaskIds)
    setApprovalLoading(true)
    setApprovalError('')
    try {
      const res = await approvalPreview({
        project_id: projectId,
        preview_id: planPreviewResult.preview_id,
        selected_task_ids: selectedTaskIds,
      })
      if (res.ok && res.data) {
        setApprovalPreviewData(res.data)
        setShowApprovalConfirm(true)
      } else {
        const code = (res.data as unknown as Record<string, unknown>)?.code || res.error?.code || 'UNKNOWN'
        const detail = (res.data as unknown as Record<string, unknown>)?.message || res.error?.detail || '审批预检失败'
        setApprovalError(`${detail} (${code})`)
        showToast(`${detail}`, 'error')
      }
    } catch {
      setApprovalError('网络错误，请检查后端服务')
      showToast('网络错误', 'error')
    } finally {
      setApprovalLoading(false)
    }
  }

  const handleApprove = async () => {
    if (!planPreviewResult?.preview_id || !approvalPreviewData?.confirmation_token) {
      showToast('缺少审批信息', 'error')
      return
    }
    setShowApprovalConfirm(false)
    setApprovalLoading(true)
    setApprovalError('')
    try {
      const res = await approve({
        project_id: projectId,
        preview_id: planPreviewResult.preview_id,
        selected_task_ids: pendingSelectedTasks,
        confirmation_token: approvalPreviewData.confirmation_token,
      })
      if (res.ok && res.data) {
        setApprovalResult(res.data)
        showToast(
          `审批完成：${res.data.approved_task_ids.length}个任务转ready，${res.data.kept_needs_planning_task_ids.length}个保持needs_planning`,
          'success',
        )
        // 刷新任务列表和决策
        fetchTasks()
        pollExecutor()
        fetchStartDecision()
      } else {
        const code = (res.data as unknown as Record<string, unknown>)?.code || res.error?.code || 'UNKNOWN'
        const detail = (res.data as unknown as Record<string, unknown>)?.message || res.error?.detail || '审批失败'
        setApprovalError(`${detail} (${code})`)
        showToast(`${detail}`, 'error')
      }
    } catch {
      setApprovalError('网络错误，请检查后端服务')
      showToast('网络错误', 'error')
    } finally {
      setApprovalLoading(false)
    }
  }

  const handleRejectPlan = async () => {
    if (!planPreviewResult?.preview_id) {
      showToast('缺少预览ID', 'error')
      return
    }
    setApprovalLoading(true)
    try {
      const res = await rejectPlan({
        project_id: projectId,
        preview_id: planPreviewResult.preview_id,
      })
      if (res.ok) {
        showToast('规划已拒绝', 'success')
        handleClosePlanPreview()
      } else {
        showToast('拒绝失败', 'error')
      }
    } catch {
      showToast('网络错误', 'error')
    } finally {
      setApprovalLoading(false)
    }
  }

  // ── 自然语言指令解析 ──
  const handlePreviewAICommand = async () => {
    if (!aiCommandText.trim() || aiCommandLoading) return
    setAiCommandLoading(true)
    setAiCommandResult(null)
    setReadonlyResult(null)
    setWriteResult(null)
    try {
      const res = await previewAICommand(projectId, aiCommandText.trim())
      if (res.ok && res.data) {
        setAiCommandResult(res.data)
      } else {
        showToast(res.error?.detail || '解析失败', 'error')
      }
    } catch {
      showToast('网络错误，请检查后端服务', 'error')
    } finally {
      setAiCommandLoading(false)
    }
  }

  // V1.1 执行只读指令
  const handleExecuteReadonly = async () => {
    if (!aiCommandResult || !aiCommandText.trim() || readonlyLoading) return
    setReadonlyLoading(true)
    setReadonlyResult(null)
    try {
      const res = await executeReadonlyAICommand(
        projectId,
        aiCommandText.trim(),
        aiCommandResult.intent,
      )
      if (res.ok && res.data) {
        setReadonlyResult(res.data)
        showToast('查询完成', 'success')
      } else {
        showToast(res.data?.message || res.error?.detail || '执行失败', 'error')
        setReadonlyResult(res.data || null)
      }
    } catch {
      showToast('网络错误，请检查后端服务', 'error')
    } finally {
      setReadonlyLoading(false)
    }
  }

  // V1.2 执行写指令（start_development）
  const handleExecuteWrite = async () => {
    if (!aiCommandResult || !aiCommandText.trim() || writeLoading) return
    const token = aiCommandResult.confirmation_token
    if (!token) {
      showToast('缺少确认令牌，请重新解析指令', 'error')
      return
    }
    setWriteLoading(true)
    setWriteResult(null)
    try {
      const res = await executeAICommand(
        projectId,
        aiCommandText.trim(),
        aiCommandResult.intent,
        token,
      )
      if (res.ok && res.data) {
        setWriteResult(res.data)
        if (res.data.executed) {
          showToast('自动开发已启动', 'success')
          // 刷新执行器状态
          setTimeout(() => pollExecutor(), 1000)
        } else if (res.data.code === 'ALREADY_RUNNING') {
          showToast('项目已在运行中', 'success')
        } else {
          showToast(res.data.message || '操作完成', 'success')
        }
      } else {
        showToast(res.data?.message || res.error?.detail || '执行失败', 'error')
        setWriteResult(res.data || null)
      }
    } catch {
      showToast('网络错误，请检查后端服务', 'error')
    } finally {
      setWriteLoading(false)
    }
  }

  const handleViewLogs = async () => {
    setShowLogDialog(true)
    try {
      const res = await getExecutionLogs({ limit: 100 })
      if (res.ok && res.data) {
        setLogLines(res.data.map((l: any) => ({
          time: l.detail ? `[${l.step_name}]` : '',
          text: `${l.step_name} | ${l.step_status} | exit=${l.exit_code ?? '--'}`,
        })))
      }
    } catch {
      setLogLines([{ time: '', text: '无法获取日志' }])
    }
  }

  const handleViewTaskResult = async (taskId: number) => {
    try {
      const res = await getExecutions({ task_id: taskId, limit: 5 })
      if (res.ok && res.data && res.data.length > 0) {
        const exec = res.data[0]
        const info = [
          `状态: ${exec.status}`,
          `Worker: ${exec.worker_id || '--'}`,
          `测试: ${exec.test_result || '--'}`,
          `修复次数: ${exec.repair_count}`,
          `耗时: ${exec.duration_ms ? (exec.duration_ms / 1000).toFixed(1) + 's' : '--'}`,
          `安全通过: ${exec.safety_passed ? '是' : '否'}`,
          `修改文件: ${Array.isArray(exec.files_modified) ? exec.files_modified.join(', ') : '--'}`,
        ]
        alert(`执行记录 #${exec.id}\n${info.join('\n')}`)
      } else {
        showToast('暂无执行记录', 'error')
      }
    } catch {
      showToast('获取执行记录失败', 'error')
    }
  }

  const handleRetryTask = async (taskId: number) => {
    if (retryingTaskId) return
    setRetryingTaskId(taskId)
    try {
      const res = await retryTask(taskId)
      if (res.ok) {
        showToast(`任务 #${taskId} 已重置为待执行状态`)
        await fetchTasks()
        await pollExecutor()
      } else {
        showToast(res.error?.detail || '重试失败', 'error')
      }
    } catch {
      showToast('网络错误，请检查后端服务', 'error')
    } finally {
      setRetryingTaskId(null)
    }
  }

  const handleConfirmComplete = async (taskId: number) => {
    // 只在测试通过或等待人工验收时可用
    if (!confirm('确认将此任务标记为已完成？仅当系统已测试通过或等待人工验收时才应使用。')) return
    if (completingTaskId) return
    setCompletingTaskId(taskId)
    try {
      const res = await completeTask(taskId)
      if (res.ok) {
        showToast('任务已完成')
        await fetchTasks()
        await fetchProject()
        await pollExecutor()
      } else {
        showToast(res.error?.detail || '操作失败', 'error')
      }
    } catch {
      showToast('网络错误，请检查后端服务', 'error')
    } finally {
      setCompletingTaskId(null)
    }
  }

  const handleAnalyze = async () => {
    setLoading(true)
    try {
      const res = await analyzeRequirements(projectId)
      if (res.ok) {
        showToast('需求分析完成')
        await fetchProject()
        await fetchAnalysis()
      } else {
        showToast(res.error?.detail || '分析失败', 'error')
      }
    } catch {
      showToast('AI 服务调用失败', 'error')
    } finally {
      setLoading(false)
    }
  }

  const handleGenerateModules = async () => {
    setLoading(true)
    try {
      const res = await generateModules(projectId)
      if (res.ok) {
        showToast('模块和MVP规划生成完成')
        await fetchProject()
        await fetchModules()
      } else {
        showToast(res.error?.detail || '生成失败', 'error')
      }
    } catch {
      showToast('AI 服务调用失败', 'error')
    } finally {
      setLoading(false)
    }
  }

  const handleGenerateTasks = async () => {
    setLoading(true)
    try {
      const res = await generateTasks(projectId)
      if (res.ok) {
        showToast('开发任务生成完成')
        await fetchProject()
        await fetchTasks()
      } else {
        showToast(res.error?.detail || '生成失败', 'error')
      }
    } catch {
      showToast('AI 服务调用失败', 'error')
    } finally {
      setLoading(false)
    }
  }

  const handleCompleteTask = async (taskId: number) => {
    if (completingTaskId) return
    setCompletingTaskId(taskId)
    try {
      const res = await completeTask(taskId)
      if (res.ok) {
        showToast('任务已完成')
        await fetchTasks()
        await fetchProject()
      } else {
        showToast(res.error?.detail || '操作失败', 'error')
      }
    } catch {
      showToast('网络错误，请检查后端服务', 'error')
    } finally {
      setCompletingTaskId(null)
    }
  }

  const handleCreateBug = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!bugTitle.trim() || submittingBug) return
    setSubmittingBug(true)
    try {
      const res = await createBug(projectId, {
        title: bugTitle.trim(),
        error_message: bugError.trim() || undefined,
        reproduction_steps: bugSteps.trim() || undefined,
        expected_result: bugExpected.trim() || undefined,
        actual_result: bugActual.trim() || undefined,
      })
      if (res.ok) {
        showToast(`Bug已保存，编号：BUG-${String(res.data.id).padStart(4, '0')}`)
        setBugTitle(''); setBugError(''); setBugSteps(''); setBugExpected(''); setBugActual('')
        await fetchBugs()
        // 自动选中新创建的Bug
        if (res.data) {
          setSelectedBugId(res.data.id)
          setSelectedBug(res.data)
        }
      } else {
        showToast(res.error?.detail || '创建失败', 'error')
      }
    } catch (err: any) {
      const msg = err?.response?.data?.error?.detail || err?.message || '网络错误，请检查后端服务'
      showToast(msg, 'error')
    } finally {
      setSubmittingBug(false)
    }
  }

  const handleSelectBug = async (bugId: number) => {
    if (selectedBugId === bugId) {
      setSelectedBugId(null)
      setSelectedBug(null)
      setBugStatusLogs([])
      return
    }
    setSelectedBugId(bugId)
    try {
      const res = await getBug(bugId)
      if (res.ok) {
        setSelectedBug(res.data)
        setBugStatusLogs(res.data.status_logs || [])
        // 初始化执行结果输入
        if (res.data.execution_result) setExecutionText(res.data.execution_result)
        else setExecutionText('')
        if (res.data.files_changed) setFilesChangedText(res.data.files_changed)
        else setFilesChangedText('')
        if (res.data.test_result) setTestResultText(res.data.test_result)
        else setTestResultText('')
        if (res.data.remaining_issues) setRemainingIssuesText(res.data.remaining_issues)
        else setRemainingIssuesText('')
      } else {
        // 降级到列表中的数据
        const bug = bugs.find(b => b.id === bugId)
        if (bug) setSelectedBug(bug)
      }
    } catch {
      const bug = bugs.find(b => b.id === bugId)
      if (bug) setSelectedBug(bug)
    }
  }

  const handleAnalyzeBug = async (bugId: number) => {
    if (analyzingBugId) return
    setAnalyzingBugId(bugId)
    try {
      const res = await analyzeBug(bugId)
      if (res.ok) {
        showToast('Bug分析完成')
        await fetchBugs()
        if (selectedBugId === bugId) {
          setSelectedBug(res.data)
        }
      } else {
        showToast(res.error?.detail || '分析失败', 'error')
      }
    } catch (err: any) {
      const msg = err?.response?.data?.error?.detail || err?.message || 'AI 服务调用失败'
      showToast(msg, 'error')
    } finally {
      setAnalyzingBugId(null)
    }
  }

  const handleGenerateFixPrompt = async (bugId: number) => {
    if (generatingFixId) return
    setGeneratingFixId(bugId)
    try {
      const res = await generateFixPrompt(bugId)
      if (res.ok) {
        showToast('CODEX修复指令已生成')
        await fetchBugs()
        if (selectedBugId === bugId) {
          setSelectedBug(res.data)
        }
      } else {
        showToast(res.error?.detail || '生成修复指令失败', 'error')
      }
    } catch (err: any) {
      const msg = err?.response?.data?.error?.detail || err?.message || '生成修复指令失败'
      showToast(msg, 'error')
    } finally {
      setGeneratingFixId(null)
    }
  }

  const handleCopyFixPrompt = async (text: string) => {
    if (!text) {
      showToast('没有可复制的内容', 'error')
      return
    }
    try {
      await navigator.clipboard.writeText(text)
      showToast('已复制到剪贴板')
    } catch (e: any) {
      showToast(`复制失败：${e?.message || '浏览器不支持'}，请手动复制`, 'error')
    }
  }

  const handleMarkFixing = async (bugId: number) => {
    if (updatingStatusId) return
    setUpdatingStatusId(bugId)
    try {
      const res = await updateBugStatus(bugId, 'fixing', '开始执行修复')
      if (res.ok) {
        showToast('Bug状态已更新为「修复中」')
        await fetchBugs()
        if (selectedBugId === bugId) setSelectedBug(res.data)
      } else {
        showToast(res.error?.detail || '状态更新失败', 'error')
      }
    } catch (err: any) {
      const msg = err?.response?.data?.error?.detail || err?.message || '状态更新失败'
      showToast(msg, 'error')
    } finally {
      setUpdatingStatusId(null)
    }
  }

  const handleSaveExecutionResult = async (bugId: number) => {
    if (savingExecId) return
    if (!executionText.trim()) {
      showToast('请输入CODEX执行结果', 'error')
      return
    }
    setSavingExecId(bugId)
    try {
      const res = await saveExecutionResult(bugId, {
        execution_result: executionText.trim(),
        files_changed: filesChangedText.trim() || undefined,
        test_result: testResultText.trim() || undefined,
        remaining_issues: remainingIssuesText.trim() || undefined,
      })
      if (res.ok) {
        showToast('CODEX执行结果已保存，状态已更新为「等待测试」')
        await fetchBugs()
        if (selectedBugId === bugId) setSelectedBug(res.data)
      } else {
        showToast(res.error?.detail || '保存执行结果失败', 'error')
      }
    } catch (err: any) {
      const msg = err?.response?.data?.error?.detail || err?.message || '保存执行结果失败'
      showToast(msg, 'error')
    } finally {
      setSavingExecId(null)
    }
  }

  const handleTestPass = async (bugId: number) => {
    if (savingTestId) return
    setSavingTestId(bugId)
    try {
      const res = await saveTestResult(bugId, {
        passed: true,
        test_notes: testNotes.trim() || '回归测试通过',
        checklist: Object.entries(testChecklist).filter(([, v]) => v).map(([k]) => k),
      })
      if (res.ok) {
        showToast('Bug已标记为「已解决」')
        await fetchBugs()
        if (selectedBugId === bugId) setSelectedBug(res.data)
      } else {
        showToast(res.error?.detail || '更新测试结果失败', 'error')
      }
    } catch (err: any) {
      const msg = err?.response?.data?.error?.detail || err?.message || '更新测试结果失败'
      showToast(msg, 'error')
    } finally {
      setSavingTestId(null)
    }
  }

  const handleTestFail = async (bugId: number) => {
    if (!failReason.trim()) {
      setShowFailDialog(true)
      return
    }
    if (savingTestId) return
    setSavingTestId(bugId)
    try {
      const res = await saveTestResult(bugId, {
        passed: false,
        test_notes: failReason.trim(),
      })
      if (res.ok) {
        showToast('Bug已重新打开')
        setShowFailDialog(false)
        setFailReason('')
        await fetchBugs()
        if (selectedBugId === bugId) setSelectedBug(res.data)
      } else {
        showToast(res.error?.detail || '更新测试结果失败', 'error')
      }
    } catch (err: any) {
      const msg = err?.response?.data?.error?.detail || err?.message || '更新测试结果失败'
      showToast(msg, 'error')
    } finally {
      setSavingTestId(null)
    }
  }

  const handleReopenBug = async (bugId: number) => {
    if (updatingStatusId) return
    setUpdatingStatusId(bugId)
    try {
      const res = await updateBugStatus(bugId, 'reopened', '手动重新打开Bug')
      if (res.ok) {
        showToast('Bug已重新打开')
        await fetchBugs()
        if (selectedBugId === bugId) setSelectedBug(res.data)
      } else {
        showToast(res.error?.detail || '重新打开失败', 'error')
      }
    } catch (err: any) {
      const msg = err?.response?.data?.error?.detail || err?.message || '重新打开失败'
      showToast(msg, 'error')
    } finally {
      setUpdatingStatusId(null)
    }
  }

  const handleCloseBug = async (bugId: number) => {
    if (updatingStatusId) return
    if (!confirm('确认关闭此Bug？关闭后仍可重新打开。')) return
    setUpdatingStatusId(bugId)
    try {
      const res = await updateBugStatus(bugId, 'closed', 'Bug已关闭')
      if (res.ok) {
        showToast('Bug已关闭')
        await fetchBugs()
        if (selectedBugId === bugId) setSelectedBug(res.data)
      } else {
        showToast(res.error?.detail || '关闭失败', 'error')
      }
    } catch (err: any) {
      const msg = err?.response?.data?.error?.detail || err?.message || '关闭失败'
      showToast(msg, 'error')
    } finally {
      setUpdatingStatusId(null)
    }
  }

  const getBugStepIndex = (status: string): number => {
    if (status === 'resolved' || status === 'closed') return 5
    if (status === 'reopened') return 0
    return BUG_STEP_ORDER.indexOf(status)
  }

  // Bug 操作可用性检查：返回 { canDo: boolean, reason: string }
  const canAnalyzeBug = (b: Bug): { canDo: boolean; reason: string } => {
    if (b.status !== 'reported' && b.status !== 'reopened')
      return { canDo: false, reason: `当前状态「${BUG_STATUS[b.status]}」不允许分析` }
    return { canDo: true, reason: '' }
  }
  const canGenerateFix = (b: Bug): { canDo: boolean; reason: string } => {
    if (b.status !== 'analyzed' && b.status !== 'fix_ready' && b.status !== 'reopened')
      return { canDo: false, reason: `当前状态「${BUG_STATUS[b.status]}」不允许生成修复指令` }
    if (!b.probable_cause?.length)
      return { canDo: false, reason: '尚未完成AI分析，缺少根本原因' }
    return { canDo: true, reason: '' }
  }
  const canMarkFixing = (b: Bug): { canDo: boolean; reason: string } => {
    if (b.status !== 'fix_ready')
      return { canDo: false, reason: `当前状态「${BUG_STATUS[b.status]}」不允许标记为修复中` }
    if (!b.fix_prompt)
      return { canDo: false, reason: '尚未生成CODEX修复指令' }
    return { canDo: true, reason: '' }
  }
  const canSaveExecution = (b: Bug): { canDo: boolean; reason: string } => {
    if (b.status !== 'fixing')
      return { canDo: false, reason: `当前状态「${BUG_STATUS[b.status]}」不允许保存执行结果` }
    return { canDo: true, reason: '' }
  }
  const canTestPass = (b: Bug): { canDo: boolean; reason: string } => {
    if (b.status !== 'waiting_test')
      return { canDo: false, reason: `当前状态「${BUG_STATUS[b.status]}」不允许提交测试结果` }
    return { canDo: true, reason: '' }
  }
  const canReopen = (b: Bug): { canDo: boolean; reason: string } => {
    if (b.status !== 'resolved' && b.status !== 'closed')
      return { canDo: false, reason: `当前状态「${BUG_STATUS[b.status]}」不允许重新打开` }
    return { canDo: true, reason: '' }
  }

  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      showToast('已复制到剪贴板')
    } catch {
      showToast('复制失败，请手动复制', 'error')
    }
  }

  const handleDeleteProject = async () => {
    if (!confirm('确认删除此项目？所有数据将丢失且无法恢复。')) return
    try {
      const res = await deleteProject(projectId)
      if (res.ok) navigate('/')
      else showToast(res.error?.detail || '删除失败', 'error')
    } catch {
      showToast('网络错误，删除失败', 'error')
    }
  }

  if (loadError) return (
    <div className="empty-state">
      <p style={{ color: 'var(--danger)', fontSize: 16 }}>{loadError}</p>
      <button className="btn btn-primary" onClick={() => navigate('/')} style={{ marginTop: 12 }}>
        返回项目列表
      </button>
    </div>
  )

  if (!project) return <div className="loading"><div className="spinner" />加载中...</div>

  const tabs: { key: Tab; label: string }[] = [
    { key: 'requirements', label: '需求输入' },
    { key: 'analysis', label: 'AI分析' },
    { key: 'modules', label: '模块与MVP' },
    { key: 'tasks', label: '开发任务' },
    { key: 'bugs', label: 'Bug分析' },
  ]

  return (
    <div>
      {toast && <div className={`toast toast-${toast.type}`}>{toast.msg}</div>}

      <div className="page-header">
        <div>
          <h2>{project.name}</h2>
          <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
            <span className={`badge badge-${project.status}`}>
              {STATUS_LABELS[project.status] || project.status}
            </span>
            <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
              更新于 {new Date(project.updated_at).toLocaleString()}
            </span>
          </div>
        </div>
        <button className="btn btn-danger btn-sm" onClick={handleDeleteProject}>删除项目</button>
      </div>

      {/* Tab Navigation */}
      <div style={{ display: 'flex', gap: 4, marginBottom: 20, borderBottom: '1px solid var(--border)', paddingBottom: 8 }}>
        {tabs.map((tab) => (
          <button
            key={tab.key}
            className={`btn btn-sm ${activeTab === tab.key ? 'btn-primary' : 'btn-secondary'}`}
            onClick={() => setActiveTab(tab.key)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Requirements Tab */}
      {activeTab === 'requirements' && (
        <div className="card">
          <div className="card-title">软件需求</div>
          <RequirementForm project={project} projectId={projectId} onSaved={fetchProject} />
        </div>
      )}

      {/* Analysis Tab */}
      {activeTab === 'analysis' && (
        <div>
          <div style={{ marginBottom: 16 }}>
            <button
              className="btn btn-primary"
              onClick={handleAnalyze}
              disabled={loading}
            >
              {loading ? '分析中...' : '🤖 AI 需求分析'}
            </button>
          </div>
          {analysisData ? (
            <AnalysisResult data={analysisData} />
          ) : (
            <div className="empty-state">
              <p>请先填写需求，然后点击 AI 需求分析</p>
            </div>
          )}
        </div>
      )}

      {/* Modules Tab */}
      {activeTab === 'modules' && (
        <div>
          <div style={{ marginBottom: 16 }}>
            <button
              className="btn btn-primary"
              onClick={handleGenerateModules}
              disabled={loading}
            >
              {loading ? '生成中...' : '🤖 生成模块和MVP规划'}
            </button>
          </div>
          {!analysisData ? (
            <div className="empty-state"><p>请先完成需求分析</p></div>
          ) : !modulesData || modulesData.length === 0 ? (
            <div className="empty-state"><p>点击上方按钮生成模块和MVP规划</p></div>
          ) : (
            <ModulesView modules={modulesData} />
          )}
        </div>
      )}

      {/* Tasks Tab — 自动开发控制台 */}
      {activeTab === 'tasks' && (
        <div>
          {/* ── 项目执行配置卡 ── */}
          {execConfig && (
            <div className="card" style={{ marginBottom: 12, padding: 12 }}>
              <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 8, color: execConfig.execution_enabled ? 'var(--success)' : 'var(--text-muted)' }}>
                {execConfig.execution_enabled ? '✅ 自动执行已启用' : '⚠️ 此项目尚未授权AI自动执行'}
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, fontSize: 13 }}>
                <div>
                  <span style={{ color: 'var(--text-muted)' }}>执行环境：</span>
                  <span style={{ fontWeight: 600 }}>
                    {execConfig.execution_mode === 'sandbox' ? '沙箱' : execConfig.execution_mode}
                  </span>
                </div>
                <div>
                  <span style={{ color: 'var(--text-muted)' }}>工作区：</span>
                  <span style={{ fontWeight: 600 }}>{execConfig.workspace_name}</span>
                </div>
                <div>
                  <span style={{ color: 'var(--text-muted)' }}>自动执行：</span>
                  <span style={{ fontWeight: 600, color: execConfig.execution_enabled ? 'var(--success)' : 'var(--danger)' }}>
                    {execConfig.execution_enabled ? '已启用' : '未启用'}
                  </span>
                </div>
                <div>
                  <span style={{ color: 'var(--text-muted)' }}>确认要求：</span>
                  <span style={{ fontWeight: 600 }}>
                    {execConfig.requires_confirmation ? '需要确认' : '无需确认'}
                  </span>
                </div>
                <div>
                  <span style={{ color: 'var(--text-muted)' }}>可执行任务：</span>
                  <span style={{ fontWeight: 600 }}>
                    {tasks.filter(t => t.readiness_status === 'ready' && t.status === 'pending').length}
                  </span>
                </div>
                {execConfig.allowed_models && execConfig.allowed_models.length > 0 && (
                  <div>
                    <span style={{ color: 'var(--text-muted)' }}>允许模型：</span>
                    <span style={{ fontWeight: 600 }}>{execConfig.allowed_models.join(', ')}</span>
                  </div>
                )}
              </div>
              {!execConfig.configured && (
                <div style={{ marginTop: 8, fontSize: 12, color: 'var(--text-muted)' }}>
                  {execConfig.message}
                </div>
              )}
            </div>
          )}

          {/* ── V1.2.2 当前下一步决策卡 ── */}
          {startDecision && startDecision.ok && (
            <div className="card" style={{
              marginBottom: 12, padding: 14,
              borderLeft: `4px solid ${DECISION_COLORS[startDecision.decision] || 'var(--border)'}`,
              background: startDecision.decision === 'EXECUTE_READY_TASKS'
                ? 'rgba(34,197,94,0.06)'
                : startDecision.decision === 'BLOCK_UNSAFE'
                ? 'rgba(239,68,68,0.06)'
                : startDecision.decision === 'REQUEST_APPROVAL'
                ? 'rgba(239,68,68,0.06)'
                : 'rgba(245,158,11,0.06)',
            }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                <div>
                  <span style={{ fontWeight: 600, fontSize: 15 }}>
                    当前状态：
                  </span>
                  <span style={{
                    fontWeight: 600, fontSize: 15,
                    color: DECISION_COLORS[startDecision.decision] || 'var(--text-muted)',
                  }}>
                    {DECISION_LABELS[startDecision.decision] || startDecision.decision}
                  </span>
                </div>
                <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                  建议动作：{DECISION_SUGGESTIONS[startDecision.decision] || '查看详情'}
                </span>
              </div>
              <div style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 8 }}>
                {startDecision.summary}
              </div>
              {/* 任务统计 */}
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', gap: 6, fontSize: 12 }}>
                <div style={{ textAlign: 'center' }}>
                  <div style={{ color: 'var(--text-muted)' }}>待处理</div>
                  <div style={{ fontWeight: 600 }}>{startDecision.details.pending}</div>
                </div>
                <div style={{ textAlign: 'center' }}>
                  <div style={{ color: 'var(--text-muted)' }}>已就绪</div>
                  <div style={{ fontWeight: 600, color: '#16a34a' }}>{startDecision.details.ready}</div>
                </div>
                <div style={{ textAlign: 'center' }}>
                  <div style={{ color: 'var(--text-muted)' }}>待规划</div>
                  <div style={{ fontWeight: 600, color: '#d97706' }}>{startDecision.details.needs_planning}</div>
                </div>
                <div style={{ textAlign: 'center' }}>
                  <div style={{ color: 'var(--text-muted)' }}>可执行</div>
                  <div style={{ fontWeight: 600, color: '#16a34a' }}>{startDecision.details.runnable}</div>
                </div>
                <div style={{ textAlign: 'center' }}>
                  <div style={{ color: 'var(--text-muted)' }}>已完成</div>
                  <div style={{ fontWeight: 600 }}>{startDecision.details.completed}</div>
                </div>
                <div style={{ textAlign: 'center' }}>
                  <div style={{ color: 'var(--text-muted)' }}>已阻塞</div>
                  <div style={{ fontWeight: 600, color: '#ef4444' }}>{startDecision.details.blocked}</div>
                </div>
              </div>
              {/* 详细信息（折叠） */}
              <details style={{ marginTop: 8, fontSize: 12, color: 'var(--text-muted)' }}>
                <summary style={{ cursor: 'pointer' }}>详细信息</summary>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 4, marginTop: 6 }}>
                  <div>执行配置：{startDecision.details.execution_enabled ? '✅ 已启用' : '❌ 未启用'}</div>
                  <div>工作区：{startDecision.details.workspace_path || '未配置'}</div>
                  <div>工作区存在：{startDecision.details.workspace_exists ? '是' : '否'}</div>
                  <div>Git有效：{startDecision.details.git_valid ? '是' : '否'}</div>
                  <div>Git Clean：{startDecision.details.git_clean ? '是' : '否'}</div>
                  <div>高风险：{startDecision.details.is_high_risk ? '⚠ 是' : '否'}</div>
                  <div>活跃Run：{startDecision.details.active_run ? `是 (${startDecision.details.active_run_status})` : '否'}</div>
                  <div>决策码：{startDecision.decision}</div>
                </div>
              </details>
              {/* V1.3: PLAN_EXISTING_TASKS 时显示"开始规划"按钮 */}
              {startDecision.decision === 'PLAN_EXISTING_TASKS' && (
                <div style={{ marginTop: 10 }}>
                  <button
                    className="btn btn-accent"
                    onClick={() => setShowPlanConfirm(true)}
                    disabled={planPreviewLoading}
                    style={{ padding: '6px 20px', fontSize: 14 }}
                  >
                    {planPreviewLoading ? '规划中...' : '开始规划'}
                  </button>
                </div>
              )}
            </div>
          )}

          {/* ── V1.4 规划确认对话框 ── */}
          {showPlanConfirm && (
            <div className="card" style={{
              marginBottom: 12, padding: 14,
              borderLeft: '4px solid #d97706',
              background: 'rgba(245,158,11,0.06)',
            }}>
              <div style={{ fontWeight: 600, fontSize: 15, marginBottom: 8, color: '#d97706' }}>
                📋 确认工程规划预览
              </div>
              <div style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 10 }}>
                <div>• 本次只生成规划预览，不会修改任何开发任务数据</div>
                <div>• 不会修改代码</div>
                <div>• 不会修改任务状态</div>
                <div>• 规划预览将持久化保存（24小时有效）</div>
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                <button
                  className="btn btn-accent"
                  onClick={handleGeneratePlanPreview}
                  disabled={planPreviewLoading}
                  style={{ padding: '6px 20px', fontSize: 14 }}
                >
                  {planPreviewLoading ? '生成中...' : '确认生成规划预览'}
                </button>
                <button
                  className="btn btn-outline"
                  onClick={handleClosePlanPreview}
                  style={{ padding: '6px 20px', fontSize: 14 }}
                >
                  取消
                </button>
              </div>
            </div>
          )}

          {/* ── V1.4 规划预览结果（含审批）── */}
          {planPreviewResult && planPreviewResult.ok && planPreviewResult.preview && (
            <PlanPreviewCard
              preview={planPreviewResult.preview}
              callRecord={planPreviewResult.call_record}
              previewId={planPreviewResult.preview_id}
              expiresAt={planPreviewResult.expires_at}
              onClose={handleClosePlanPreview}
              onApprovalPreview={handleApprovalPreview}
              onReject={handleRejectPlan}
              approvalLoading={approvalLoading}
              approvalError={approvalError}
            />
          )}

          {/* ── V1.4 审批确认对话框 ── */}
          {showApprovalConfirm && approvalPreviewData && (
            <div className="card" style={{
              marginBottom: 12, padding: 14,
              borderLeft: '4px solid #16a34a',
              background: 'rgba(34,197,94,0.06)',
            }}>
              <div style={{ fontWeight: 600, fontSize: 15, marginBottom: 8, color: '#16a34a' }}>
                ✅ 确认审批写回
              </div>
              <div style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 10 }}>
                <div>本次将：</div>
                <div>• <strong>{(approvalPreviewData.writeback_preview as Record<string, unknown>)?.will_write_ready as number ?? 0}个低风险任务</strong>转为 <span style={{ color: '#16a34a' }}>ready</span></div>
                <div>• <strong>{(approvalPreviewData.writeback_preview as Record<string, unknown>)?.will_keep_needs_planning as number ?? 0}个中/高风险任务</strong>继续 <span style={{ color: '#d97706' }}>needs_planning</span></div>
                <div style={{ marginTop: 6 }}>
                  <span style={{ fontWeight: 600, color: '#16a34a' }}>安全任务（转ready）：</span>
                  {approvalPreviewData.safe_tasks?.map(t => (
                    <span key={t.task_id} style={{
                      marginLeft: 4, padding: '1px 6px', fontSize: 11,
                      background: 'rgba(34,197,94,0.1)', borderRadius: 3,
                    }}>#{t.task_id} {t.title}</span>
                  ))}
                </div>
                {approvalPreviewData.medium_risk_tasks && approvalPreviewData.medium_risk_tasks.length > 0 && (
                  <div style={{ marginTop: 4 }}>
                    <span style={{ fontWeight: 600, color: '#d97706' }}>中风险任务（需确认，保持needs_planning）：</span>
                    {approvalPreviewData.medium_risk_tasks.map(t => (
                      <span key={t.task_id} style={{
                        marginLeft: 4, padding: '1px 6px', fontSize: 11,
                        background: 'rgba(245,158,11,0.1)', borderRadius: 3,
                      }}>#{t.task_id} {t.title}</span>
                    ))}
                  </div>
                )}
                {approvalPreviewData.high_risk_tasks && approvalPreviewData.high_risk_tasks.length > 0 && (
                  <div style={{ marginTop: 4 }}>
                    <span style={{ fontWeight: 600, color: '#ef4444' }}>高风险任务（保持needs_planning）：</span>
                    {approvalPreviewData.high_risk_tasks.map(t => (
                      <span key={t.task_id} style={{
                        marginLeft: 4, padding: '1px 6px', fontSize: 11,
                        background: 'rgba(239,68,68,0.1)', borderRadius: 3,
                      }}>#{t.task_id} {t.title}</span>
                    ))}
                  </div>
                )}
                {approvalPreviewData.blocked_tasks && approvalPreviewData.blocked_tasks.length > 0 && (
                  <div style={{ marginTop: 4 }}>
                    <span style={{ fontWeight: 600, color: '#6b7280' }}>已阻止任务：</span>
                    {approvalPreviewData.blocked_tasks.map(t => (
                      <span key={t.task_id} style={{
                        marginLeft: 4, padding: '1px 6px', fontSize: 11,
                        background: 'rgba(107,114,128,0.1)', borderRadius: 3,
                      }}>#{t.task_id} {t.title}</span>
                    ))}
                  </div>
                )}
                <div style={{ marginTop: 6, color: '#d97706' }}>
                  ⚠️ 不会启动开发执行器 | 不会修改项目源码
                </div>
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                <button
                  className="btn btn-accent"
                  onClick={handleApprove}
                  disabled={approvalLoading}
                  style={{ padding: '6px 20px', fontSize: 14, background: '#16a34a' }}
                >
                  {approvalLoading ? '审批中...' : '确认审批'}
                </button>
                <button
                  className="btn btn-outline"
                  onClick={() => { setShowApprovalConfirm(false); setApprovalPreviewData(null) }}
                  style={{ padding: '6px 20px', fontSize: 14 }}
                >
                  取消
                </button>
              </div>
            </div>
          )}

          {/* ── V1.4 审批结果 ── */}
          {approvalResult && (
            <div className="card" style={{
              marginBottom: 12, padding: 14,
              borderLeft: '4px solid #16a34a',
              background: 'rgba(34,197,94,0.06)',
            }}>
              <div style={{ fontWeight: 600, fontSize: 15, marginBottom: 8, color: '#16a34a' }}>
                ✅ 审批完成
              </div>
              <div style={{ fontSize: 13 }}>
                <div>✅ 已转 ready：{approvalResult.approved_task_ids?.join(', ') || '无'}</div>
                <div>⚠️ 保持 needs_planning：{approvalResult.kept_needs_planning_task_ids?.join(', ') || '无'}</div>
                <div>📊 当前可执行任务：{approvalResult.runnable_count}</div>
                <div style={{ color: '#d97706' }}>⚠️ 未启动开发执行器 (executed=false)</div>
              </div>
            </div>
          )}

          {/* ── 执行器状态卡 ── */}
          {executorStatus && (
            <div className="executor-status-card">
              <div className="executor-status-header">
                <span className="executor-model-badge">🤖 当前模型：DeepSeek V4 Flash</span>
                {(() => {
                  const runStatus = executorStatus.loop?.run?.status ?? 'idle'
                  const colorClass = EXECUTOR_STATUS_COLOR[runStatus] || 'badge-draft'
                  const label = EXECUTOR_STATUS_LABEL[runStatus] || runStatus
                  return (
                    <span className={`badge ${colorClass}`}>
                      执行器：{label}
                    </span>
                  )
                })()}
              </div>
              <div className="executor-status-grid">
                <div className="executor-stat">
                  <span className="executor-stat-label">当前Worker</span>
                  <span className="executor-stat-value">
                    {executorStatus.workers.active_count}/{executorStatus.workers.max_workers}
                  </span>
                </div>
                <div className="executor-stat">
                  <span className="executor-stat-label">当前任务</span>
                  <span className="executor-stat-value">
                    {executorStatus.loop?.run?.current_task_id 
                      ? `#${executorStatus.loop.run.current_task_id} (${executorStatus.loop.run.current_step || '--'})`
                      : '--'}
                  </span>
                </div>
                <div className="executor-stat">
                  <span className="executor-stat-label">本轮完成</span>
                  <span className="executor-stat-value" style={{ color: 'var(--success)' }}>
                    {executorStatus.loop?.run?.tasks_completed ?? 0}
                  </span>
                </div>
                <div className="executor-stat">
                  <span className="executor-stat-label">本轮阻塞</span>
                  <span className="executor-stat-value" style={{ color: 'var(--warning)' }}>
                    {executorStatus.loop?.run?.tasks_blocked ?? 0}
                  </span>
                </div>
                <div className="executor-stat">
                  <span className="executor-stat-label">本轮修复</span>
                  <span className="executor-stat-value" style={{ color: 'var(--info)' }}>
                    {executorStatus.loop?.run?.tasks_repaired ?? 0} / {executorStatus.loop?.run?.tasks_failed ?? 0}失败
                  </span>
                </div>
                <div className="executor-stat">
                  <span className="executor-stat-label">运行时长</span>
                  <span className="executor-stat-value">{runDuration}</span>
                </div>
              </div>
              {/* 最近错误 / 阻塞原因 */}
              {executorStatus.loop?.run?.last_error && (
                <div className="executor-resource-bar" style={{ background: 'rgba(239,68,68,0.12)', borderLeft: '3px solid var(--danger)' }}>
                  ⚠️ 最近错误：{executorStatus.loop.run.last_error}
                </div>
              )}
              {executorStatus.loop?.run?.pause_reason && (
                <div className="executor-resource-bar" style={{ background: 'rgba(245,158,11,0.12)', borderLeft: '3px solid var(--warning)' }}>
                  ⏸ 暂停原因：{executorStatus.loop.run.pause_reason}
                </div>
              )}
              {executorStatus.loop?.run?.status === 'blocked' && executorStatus.loop?.run?.last_error && (
                <div className="executor-resource-bar" style={{ background: 'rgba(239,68,68,0.12)', borderLeft: '3px solid var(--danger)' }}>
                  🚫 阻塞原因：{executorStatus.loop.run.last_error}
                </div>
              )}
              {executorStatus.resource_locks && executorStatus.resource_locks.count > 0 && (
                <div className="executor-resource-bar">
                  🔒 活跃资源锁：{executorStatus.resource_locks.count} 个
                  {executorStatus.resource_locks.locks.map((l: any, i: number) => (
                    <span key={i} className="badge badge-draft" style={{ marginLeft: 6, fontSize: 11 }}>
                      {l.lock_type}:{l.resource_key?.slice(0, 20)}
                    </span>
                  ))}
                </div>
              )}
              {executorStatus.merge_queue && executorStatus.merge_queue.is_merging && (
                <div className="executor-resource-bar" style={{ background: 'rgba(6,182,212,0.1)' }}>
                  🔀 合并进行中...
                </div>
              )}
            </div>
          )}

          {/* ── 任务准备状态警报 ── */}
          {tasks.length > 0 && (() => {
            const notReadyCount = tasks.filter(t => t.readiness_status !== 'ready').length;
            const readyCount = tasks.filter(t => t.readiness_status === 'ready').length;
            if (notReadyCount === tasks.length) {
              return (
                <div className="executor-resource-bar" style={{ background: 'rgba(245,158,11,0.12)', borderLeft: '3px solid var(--warning)', marginBottom: 12 }}>
                  <strong>{notReadyCount}个任务尚未完成工程规划，不能自动执行</strong>
                </div>
              );
            }
            if (notReadyCount > 0 && readyCount === 0) {
              return (
                <div className="executor-resource-bar" style={{ background: 'rgba(245,158,11,0.12)', borderLeft: '3px solid var(--warning)', marginBottom: 12 }}>
                  {notReadyCount}个任务尚未完成工程规划，不能自动执行
                </div>
              );
            }
            if (notReadyCount > 0 && readyCount > 0) {
              return (
                <div className="executor-resource-bar" style={{ background: 'rgba(59,130,246,0.1)', borderLeft: '3px solid var(--accent)', marginBottom: 12 }}>
                  {readyCount}个任务已准备可执行，{notReadyCount}个仍需规划
                </div>
              );
            }
            return null;
          })()}

          {/* ── 执行控制栏 ── */}
          <div className="executor-control-bar">
            <button
              className="btn btn-primary"
              onClick={handleGenerateTasks}
              disabled={loading}
              title="生成开发任务（AI分析后生成CODEX指令）"
            >
              {loading ? '生成中...' : '🤖 生成开发任务'}
            </button>

            {(() => {
              const runStatus = executorStatus?.loop?.run?.status ?? 'idle'
              const isTerminal = ['completed', 'blocked', 'failed', 'idle', 'paused'].includes(runStatus)
              const isPaused = executorStatus?.loop?.is_paused
              const pauseReason = executorStatus?.loop?.run?.pause_reason
              // 暂停状态（用户主动暂停或 budget_exceeded）显示继续/停止
              if (isPaused) {
                return (
                  <>
                    <span style={{ color: 'var(--warning)', fontSize: 13, marginRight: 8 }}>
                      {pauseReason?.includes('budget') ? '⚠ 预算耗尽' : '⏸ 已暂停'}
                    </span>
                    {!pauseReason?.includes('budget') && (
                      <button
                        className="btn btn-warning"
                        onClick={handleResumeExecutor}
                        disabled={executorLoading}
                      >
                        {executorAction === 'resume' ? '继续中...' : '▶ 继续'}
                      </button>
                    )}
                    <button
                      className="btn btn-danger"
                      onClick={handleStopExecutor}
                      disabled={executorLoading}
                    >
                      {executorAction === 'stop' ? '停止中...' : '⏹ 停止'}
                    </button>
                    <button
                      className="btn btn-success"
                      onClick={() => setShowStartDialog(true)}
                      disabled={executorLoading || tasks.length === 0}
                      title="启动新的自动开发循环"
                    >
                      🔄 重新启动
                    </button>
                  </>
                )
              }
              if (isTerminal && !isPaused) {
                return (
                  <button
                    className="btn btn-success"
                    onClick={() => setShowStartDialog(true)}
                    disabled={executorLoading}
                    title="启动自动开发循环（V1.2.2：统一决策入口）"
                  >
                    ▶ 开始自动开发
                  </button>
                )
              }
              return null
            })()}
            {executorStatus?.loop?.running && !executorStatus?.loop?.is_paused && (
              <>
                <button
                  className="btn btn-warning"
                  onClick={handlePauseExecutor}
                  disabled={executorLoading}
                >
                  {executorAction === 'pause' ? '暂停中...' : '⏸ 暂停'}
                </button>
                <button
                  className="btn btn-danger"
                  onClick={handleStopExecutor}
                  disabled={executorLoading}
                >
                  {executorAction === 'stop' ? '停止中...' : '⏹ 安全停止'}
                </button>
              </>
            )}

            <button
              className="btn btn-secondary"
              onClick={handleViewLogs}
              title="查看执行器日志"
            >
              📄 查看日志
            </button>
          </div>

          {/* ── 安全确认对话框 ── */}
          {showStartDialog && (
            <div className="executor-dialog-overlay" onClick={() => setShowStartDialog(false)}>
              <div className="executor-dialog" onClick={e => e.stopPropagation()}>
                <h3 style={{ marginBottom: 16, color: 'var(--accent)' }}>⚡ 启动自动开发确认</h3>
                <div className="executor-dialog-grid">
                  <div><strong>当前项目：</strong>{project.name}</div>
                  <div><strong>工作区：</strong>{execConfig?.workspace_path || 'C:\\Users\\本机\\Desktop\\executor-sandbox-v2'}</div>
                  <div><strong>模型：</strong>{execConfig?.allowed_models?.join(', ') || 'DeepSeek V4 Flash'}</div>
                  <div><strong>最大Worker：</strong>{execConfig?.max_workers || 1}（首次试运行）</div>
                  <div><strong>最大任务数：</strong>{execConfig?.max_tasks || 1}</div>
                  <div><strong>最大修改文件：</strong>1</div>
                  <div><strong>最大自动修复：</strong>1</div>
                </div>
                <div className="executor-safety-rules">
                  <h4>🛡 安全规则（首次试运行）</h4>
                  <ul>
                    <li>MAX_PARALLEL_WORKERS=1</li>
                    <li>MAX_TASKS_PER_RUN=1</li>
                    <li>MAX_FILES_CHANGED=1</li>
                    <li>MAX_REPAIR_ATTEMPTS_PER_TASK=1</li>
                  </ul>
                  <h4 style={{ marginTop: 12, color: 'var(--danger)' }}>🚫 严格禁止</h4>
                  <ul>
                    <li>数据库迁移</li>
                    <li>删除文件</li>
                    <li>修改.env</li>
                    <li>修改部署配置</li>
                    <li>修改权限认证</li>
                    <li>安装新依赖</li>
                    <li>操作真实业务项目</li>
                  </ul>
                </div>
                <div style={{ display: 'flex', gap: 8, marginTop: 16 }}>
                  <button
                    className="btn btn-primary"
                    onClick={handleStartExecutor}
                    disabled={executorLoading}
                  >
                    {executorLoading ? '启动中...' : '✅ 确认启动'}
                  </button>
                  <button className="btn btn-secondary" onClick={() => setShowStartDialog(false)}>
                    取消
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* ── 日志对话框 ── */}
          {showLogDialog && (
            <div className="executor-dialog-overlay" onClick={() => setShowLogDialog(false)}>
              <div className="executor-dialog executor-log-dialog" onClick={e => e.stopPropagation()}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 12 }}>
                  <h3>📄 执行器日志</h3>
                  <button className="btn btn-sm btn-secondary" onClick={() => setShowLogDialog(false)}>关闭</button>
                </div>
                <div className="executor-log-area">
                  {logLines.length === 0 ? (
                    <p style={{ color: 'var(--text-muted)' }}>暂无日志</p>
                  ) : (
                    logLines.map((l, i) => (
                      <div key={i} className="executor-log-line">
                        {l.time && <span style={{ color: 'var(--text-muted)', marginRight: 8 }}>{l.time}</span>}
                        <span>{l.text}</span>
                      </div>
                    ))
                  )}
                </div>
              </div>
            </div>
          )}

          {/* ── 自然语言指令入口 ── */}
          <div className="card" style={{ marginTop: 16, padding: 16 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
              <span style={{ fontWeight: 600, fontSize: 15 }}>💬 自然语言指令</span>
              <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>V1.2</span>
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              <input
                type="text"
                value={aiCommandText}
                onChange={(e) => { setAiCommandText(e.target.value); setAiCommandResult(null); setReadonlyResult(null); setWriteResult(null) }}
                onKeyDown={(e) => { if (e.key === 'Enter') handlePreviewAICommand() }}
                placeholder="告诉AI工厂你想做什么，例如：检查为什么不能执行"
                style={{ flex: 1, padding: '8px 12px', borderRadius: 6, border: '1px solid var(--border)', fontSize: 14 }}
                maxLength={1000}
              />
              <button
                className="btn btn-primary"
                onClick={handlePreviewAICommand}
                disabled={aiCommandLoading || !aiCommandText.trim()}
              >
                {aiCommandLoading ? '解析中...' : '解析指令'}
              </button>
            </div>
            {aiCommandResult && (
              <div className="ai-command-result" style={{ marginTop: 12, padding: 12, background: 'rgba(59,130,246,0.08)', borderRadius: 8, border: '1px solid rgba(59,130,246,0.2)' }}>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, fontSize: 13 }}>
                  <div>
                    <span style={{ color: 'var(--text-muted)' }}>识别意图：</span>
                    <span style={{ fontWeight: 600, color: 'var(--accent)' }}>
                      {INTENT_LABELS[aiCommandResult.intent] || aiCommandResult.intent}
                    </span>
                  </div>
                  <div>
                    <span style={{ color: 'var(--text-muted)' }}>置信度：</span>
                    <span style={{ fontWeight: 600 }}>
                      {(aiCommandResult.confidence * 100).toFixed(0)}%
                    </span>
                  </div>
                  <div>
                    <span style={{ color: 'var(--text-muted)' }}>准备动作：</span>
                    <span style={{ fontWeight: 600 }}>{aiCommandResult.message}</span>
                  </div>
                  <div>
                    <span style={{ color: 'var(--text-muted)' }}>当前状态：</span>
                    <span style={{ fontWeight: 600, color: 'var(--warning)' }}>
                      {aiCommandResult.executed ? '已执行' : '仅预览，尚未执行'}
                    </span>
                  </div>
                </div>

                {/* V1.1: 只读意图显示"确认查询"按钮 */}
                {READONLY_INTENTS.has(aiCommandResult.intent) && (
                  <div style={{ marginTop: 10, display: 'flex', gap: 8, alignItems: 'center' }}>
                    <button
                      className="btn btn-primary"
                      onClick={handleExecuteReadonly}
                      disabled={readonlyLoading}
                      style={{ fontSize: 13 }}
                    >
                      {readonlyLoading ? '查询中...' : '确认查询'}
                    </button>
                    <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                      此操作为只读查询，不会修改任何数据
                    </span>
                  </div>
                )}

                {/* V1.2: 已启用的写意图（start_development）显示"确认开始"按钮 */}
                {ENABLED_WRITE_INTENTS.has(aiCommandResult.intent) && (
                  <div style={{ marginTop: 10 }}>
                    {/* 项目信息和风险提示 */}
                    <div style={{ marginBottom: 10, padding: 8, background: 'rgba(34,197,94,0.06)', borderRadius: 6, fontSize: 13 }}>
                      <div><span style={{ color: 'var(--text-muted)' }}>项目：</span><span style={{ fontWeight: 600 }}>{project?.name}</span></div>
                      <div style={{ marginTop: 4 }}>
                        <span style={{ color: 'var(--text-muted)' }}>风险：</span>
                        <span style={{ fontWeight: 600, color: '#16a34a' }}>低</span>
                        <span style={{ fontSize: 12, color: 'var(--text-muted)', marginLeft: 8 }}>
                          （仅沙箱项目，不修改正式代码）
                        </span>
                      </div>
                    </div>
                    <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                      <button
                        className="btn btn-accent"
                        onClick={handleExecuteWrite}
                        disabled={writeLoading}
                        style={{ fontSize: 13, background: '#16a34a', borderColor: '#16a34a' }}
                      >
                        {writeLoading ? '启动中...' : '确认开始'}
                      </button>
                      <button
                        className="btn btn-outline"
                        onClick={() => { setAiCommandResult(null); setWriteResult(null) }}
                        disabled={writeLoading}
                        style={{ fontSize: 13 }}
                      >
                        取消
                      </button>
                    </div>
                  </div>
                )}

                {/* V1.2 写指令执行结果 */}
                {writeResult && (
                  <div style={{ marginTop: 8, padding: 8, borderRadius: 4, fontSize: 13,
                    background: writeResult.executed ? 'rgba(34,197,94,0.08)' : writeResult.code === 'ALREADY_RUNNING' ? 'rgba(59,130,246,0.08)' : 'rgba(239,68,68,0.08)',
                    border: `1px solid ${writeResult.executed ? 'rgba(34,197,94,0.2)' : writeResult.code === 'ALREADY_RUNNING' ? 'rgba(59,130,246,0.2)' : 'rgba(239,68,68,0.2)'}` }}>
                    <div style={{ fontWeight: 600, marginBottom: 4 }}>
                      {writeResult.executed ? '✅ 启动成功' : writeResult.code === 'ALREADY_RUNNING' ? 'ℹ 已在运行' : '❌ 启动失败'}
                    </div>
                    <div>{writeResult.message}</div>
                    {writeResult.run_id && <div style={{ marginTop: 4, fontSize: 12, color: 'var(--text-muted)' }}>Run ID: {writeResult.run_id}</div>}
                    {writeResult.task_id && <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>首个任务: #{writeResult.task_id}</div>}
                  </div>
                )}

                {/* 尚未开放的写意图提示 */}
                {DISABLED_WRITE_INTENTS.has(aiCommandResult.intent) && (
                  <div style={{ marginTop: 8, padding: 8, background: 'rgba(245,158,11,0.1)', borderRadius: 4, fontSize: 12, color: 'var(--warning)' }}>
                    ⚠ 已识别该动作，但当前版本尚未开放执行。
                  </div>
                )}

                {/* unknown 提示 */}
                {aiCommandResult.intent === 'unknown' && (
                  <div style={{ marginTop: 8, padding: 8, background: 'rgba(156,163,175,0.1)', borderRadius: 4, fontSize: 12, color: 'var(--text-muted)' }}>
                    无法识别该指令，请尝试：查看状态、检查阻塞原因、暂停执行 等
                  </div>
                )}
              </div>
            )}

            {/* ── 只读执行结果卡片 ── */}
            {readonlyResult && readonlyResult.data && (
              <ReadonlyResultCard result={readonlyResult} />
            )}
          </div>

          {/* ── 任务列表 ── */}
          {tasks.length === 0 ? (
            <div className="empty-state" style={{ marginTop: 16 }}>
              <p>请先完成需求分析，再生成开发任务</p>
            </div>
          ) : (
            <div style={{ marginTop: 12 }}>
              <div className="executor-queue-summary">
                <span>📋 任务队列：{tasks.length} 个任务</span>
                {executorQueue && (
                  <span style={{ marginLeft: 12 }}>
                    待执行: {executorQueue.pending} | 开发中: {executorQueue.executing} | 
                    测试中: {executorQueue.testing} | 修复中: {executorQueue.repairing} | 
                    已完成: {executorQueue.completed}
                  </span>
                )}
              </div>
              {tasks.map((t) => {
                const isCompleted = t.status === 'completed'
                const isActive = ['executing', 'testing', 'repairing', 'claiming', 'waiting_merge'].includes(t.status)
                return (
                  <div key={t.id} className={`task-item ${isActive ? 'task-item-active' : ''}`}>
                    <div className={`task-status-dot task-dot-${t.status}`} />
                    <div style={{ flex: 1 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                        <strong style={{ fontSize: 14 }}>{t.title}</strong>
                        <span className={`badge badge-${t.priority}`}>{t.priority}</span>
                        <span className={`badge ${TASK_STATUS_COLOR[t.status] || 'badge-draft'}`}>
                          {TASK_STATUS[t.status] || t.status}
                        </span>
                        <span className="badge badge-draft">{t.task_type}</span>
                        <span className={`badge ${READINESS_STATUS_COLOR[t.readiness_status] || 'badge-draft'}`}
                          title={`任务准备状态：${READINESS_STATUS_LABEL[t.readiness_status] || t.readiness_status}`}>
                          {READINESS_STATUS_LABEL[t.readiness_status] || t.readiness_status}
                        </span>
                      </div>
                      {t.description && (
                        <p style={{ fontSize: 13, color: 'var(--text-muted)', marginTop: 4 }}>{t.description}</p>
                      )}
                      {isActive && (
                        <div className="task-detail-row">
                          {t.files_to_modify && t.files_to_modify.length > 0 && (
                            <span title="修改文件数量">
                              📁 {t.files_to_modify.length} 文件
                            </span>
                          )}
                        </div>
                      )}
                    </div>
                    <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                      {t.codex_prompt && (
                        <button
                          className="btn btn-sm btn-secondary"
                          onClick={() => copyToClipboard(t.codex_prompt || '')}
                          title="复制CODEX提示词作为手工备用"
                        >
                          📋 复制CODEX指令
                        </button>
                      )}
                      {/* 已完成：显示"查看结果" */}
                      {isCompleted && (
                        <button
                          className="btn btn-sm btn-secondary"
                          onClick={() => handleViewTaskResult(t.id)}
                        >
                          📊 查看结果
                        </button>
                      )}
                      {/* 失败任务：显示"重试任务" */}
                      {['failed', 'test_failed', 'blocked'].includes(t.status) && preflight && !preflight.active_run && (
                        <button
                          className="btn btn-sm btn-warning"
                          onClick={() => handleRetryTask(t.id)}
                          disabled={retryingTaskId === t.id}
                        >
                          {retryingTaskId === t.id ? '重试中...' : '🔄 重试任务'}
                        </button>
                      )}
                      {/* 达标任务：显示"确认完成" */}
                      {(t.status === 'waiting_test' || t.status === 'test_failed') && (
                        <button
                          className="btn btn-sm btn-success"
                          onClick={() => handleConfirmComplete(t.id)}
                          disabled={completingTaskId === t.id}
                        >
                          {completingTaskId === t.id ? '...' : '✅ 确认完成'}
                        </button>
                      )}
                      {/* 活跃任务不显示操作按钮 */}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}

      {/* Bugs Tab */}
      {activeTab === 'bugs' && (
        <div>
          {/* 报告 Bug 表单 */}
          <div className="card">
            <div className="card-title">报告 Bug</div>
            <form onSubmit={handleCreateBug}>
              <div className="form-group">
                <label>Bug 标题 *</label>
                <input value={bugTitle} onChange={(e) => setBugTitle(e.target.value)} placeholder="简要描述问题" />
              </div>
              <div className="grid-2">
                <div className="form-group">
                  <label>复现步骤</label>
                  <textarea value={bugSteps} onChange={(e) => setBugSteps(e.target.value)} rows={2} placeholder="如何触发这个Bug" />
                </div>
                <div className="form-group">
                  <label>预期结果</label>
                  <textarea value={bugExpected} onChange={(e) => setBugExpected(e.target.value)} rows={2} placeholder="期望发生什么" />
                </div>
              </div>
              <div className="grid-2">
                <div className="form-group">
                  <label>实际结果</label>
                  <textarea value={bugActual} onChange={(e) => setBugActual(e.target.value)} rows={2} placeholder="实际发生了什么" />
                </div>
                <div className="form-group">
                  <label>错误信息</label>
                  <textarea value={bugError} onChange={(e) => setBugError(e.target.value)} rows={2} placeholder="控制台报错或网络错误" />
                </div>
              </div>
              <button type="submit" className="btn btn-primary" disabled={!bugTitle.trim() || submittingBug}>
                {submittingBug ? '提交中...' : '提交 Bug'}
              </button>
            </form>
          </div>

          {/* Bug 列表 + 修复工作区 */}
          {bugs.length > 0 && (
            <div style={{ marginTop: 16 }}>
              <h3 style={{ marginBottom: 12, fontSize: 16 }}>Bug 记录</h3>
              {bugs.map((b) => (
                <div key={b.id}>
                  {/* Bug 列表卡片 */}
                  <div
                    className={`card bug-list-card ${selectedBugId === b.id ? 'bug-list-card-active' : ''}`}
                    style={{ cursor: 'pointer' }}
                    onClick={() => handleSelectBug(b.id)}
                  >
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <span style={{ fontSize: 12, color: 'var(--text-muted)', fontFamily: 'monospace' }}>
                          BUG-{b.id.toString().padStart(4, '0')}
                        </span>
                        <strong style={{ fontSize: 14 }}>{b.title}</strong>
                        {b.severity && (
                          <span className={`badge badge-${b.severity}`}>
                            {BUG_SEVERITY[b.severity] || b.severity}
                          </span>
                        )}
                        <span className={`badge badge-bug-${b.status}`}>
                          {BUG_STATUS[b.status] || b.status}
                        </span>
                      </div>
                      <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                        {new Date(b.updated_at).toLocaleString()}
                      </span>
                    </div>
                  </div>

                  {/* Bug 修复工作区 - 只在选中时显示 */}
                  {selectedBugId === b.id && selectedBug && (
                    <div className="card bug-workspace">
                      {/* Bug 状态栏 */}
                      <div className="bug-status-bar">
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                            <span style={{ fontSize: 18, fontWeight: 700, color: 'var(--accent)' }}>
                              BUG-{b.id.toString().padStart(4, '0')}
                            </span>
                            {b.severity && (
                              <span className={`badge badge-${b.severity}`} style={{ fontSize: 13, padding: '3px 10px' }}>
                                {BUG_SEVERITY[b.severity] || b.severity}
                              </span>
                            )}
                            <span className={`badge badge-bug-${b.status}`} style={{ fontSize: 13, padding: '3px 10px' }}>
                              {BUG_STATUS[b.status] || b.status}
                            </span>
                            {b.is_blocking === 'yes' && (
                              <span className="badge badge-critical" style={{ fontSize: 11 }}>阻塞上线</span>
                            )}
                          </div>
                          <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                            创建于 {new Date(b.created_at).toLocaleString()}
                            {b.resolved_at && ` | 解决于 ${new Date(b.resolved_at).toLocaleString()}`}
                          </div>
                        </div>

                        {/* 步骤条 */}
                        <div className="bug-stepper">
                          {BUG_STEP_LABELS.map((label, i) => {
                            const currentStep = getBugStepIndex(b.status)
                            const isActive = i === currentStep
                            const isDone = i < currentStep
                            return (
                              <div key={i} className={`bug-step ${isDone ? 'done' : isActive ? 'active' : ''}`}>
                                <div className="bug-step-dot">
                                  {isDone ? '✓' : i + 1}
                                </div>
                                <span className="bug-step-label">{label}</span>
                              </div>
                            )
                          })}
                        </div>
                      </div>

                      {/* 原始报告 */}
                      <div className="bug-section">
                        <h4 className="bug-section-title">原始报告</h4>
                        <div className="grid-2" style={{ gap: 8 }}>
                          {b.reproduction_steps && (
                            <div><label>复现步骤</label><p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>{b.reproduction_steps}</p></div>
                          )}
                          {b.expected_result && (
                            <div><label>预期结果</label><p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>{b.expected_result}</p></div>
                          )}
                          {b.actual_result && (
                            <div><label>实际结果</label><p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>{b.actual_result}</p></div>
                          )}
                          {b.error_message && (
                            <div><label>错误信息</label><pre className="bug-error-msg">{b.error_message}</pre></div>
                          )}
                        </div>
                      </div>

                      {/* 操作按钮区 */}
                      {(b.status === 'reported' || b.status === 'reopened') && (
                        <div style={{ margin: '12px 0' }}>
                          <button
                            className="btn btn-primary"
                            onClick={() => handleAnalyzeBug(b.id)}
                            disabled={analyzingBugId === b.id}
                            title={canAnalyzeBug(b).reason || '开始AI分析Bug'}
                          >
                            {analyzingBugId === b.id ? '正在分析...' : '🤖 开始 AI 分析'}
                          </button>
                        </div>
                      )}

                      {b.status === 'analyzing' && (
                        <div style={{ margin: '12px 0', padding: 12, background: 'rgba(59,130,246,0.1)', borderRadius: 8, textAlign: 'center' }}>
                          <div className="spinner" style={{ display: 'inline-block', verticalAlign: 'middle', marginRight: 8 }} />
                          <span style={{ color: 'var(--accent)' }}>AI 正在分析中，请稍候...</span>
                        </div>
                      )}

                      {/* AI 分析结果 */}
                      {(b.status === 'analyzed' || b.status === 'fix_ready' || b.status === 'fixing' || b.status === 'waiting_test' || b.status === 'resolved' || b.status === 'reopened') && b.probable_cause?.length > 0 && (
                        <div className="bug-section">
                          <h4 className="bug-section-title">AI 分析结果</h4>

                          {/* 问题判断 */}
                          <div className="bug-analysis-grid">
                            <div className="bug-analysis-item">
                              <span className="bug-analysis-label">Bug类型</span>
                              <span>{b.bug_type || '未分类'}</span>
                            </div>
                            <div className="bug-analysis-item">
                              <span className="bug-analysis-label">严重等级</span>
                              <span className={`badge badge-${b.severity || 'medium'}`}>{BUG_SEVERITY[b.severity || 'medium'] || b.severity}</span>
                            </div>
                            <div className="bug-analysis-item">
                              <span className="bug-analysis-label">影响模块</span>
                              <span>{b.affected_module || '未知'}</span>
                            </div>
                            <div className="bug-analysis-item">
                              <span className="bug-analysis-label">阻塞上线</span>
                              <span>{b.is_blocking === 'yes' ? '是' : b.is_blocking === 'no' ? '否' : '未知'}</span>
                            </div>
                          </div>

                          {/* 根本原因 */}
                          <div style={{ marginTop: 12 }}>
                            <strong style={{ fontSize: 13, color: 'var(--warning)' }}>根本原因</strong>
                            <ul style={{ paddingLeft: 20, fontSize: 13, color: 'var(--text-secondary)', marginTop: 4 }}>
                              {b.probable_cause.map((c, i) => <li key={i}>{c}</li>)}
                            </ul>
                          </div>

                          {/* 可能涉及的位置 */}
                          {b.affected_files?.length > 0 && (
                            <div style={{ marginTop: 8 }}>
                              <strong style={{ fontSize: 13 }}>可能涉及的位置</strong>
                              <ul style={{ paddingLeft: 20, fontSize: 13, color: 'var(--text-secondary)', marginTop: 4 }}>
                                {b.affected_files.map((f, i) => <li key={i}><code style={{ fontSize: 12 }}>{f}</code></li>)}
                              </ul>
                            </div>
                          )}

                          {/* 修复方案 */}
                          {b.fix_plan?.length > 0 && (
                            <div style={{ marginTop: 8 }}>
                              <strong style={{ fontSize: 13, color: 'var(--success)' }}>修复方案</strong>
                              <ol style={{ paddingLeft: 20, fontSize: 13, color: 'var(--text-secondary)', marginTop: 4 }}>
                                {b.fix_plan.map((s, i) => <li key={i}>{s}</li>)}
                              </ol>
                            </div>
                          )}

                          {/* 回归风险 */}
                          {b.regression_risks?.length > 0 && (
                            <div style={{ marginTop: 8 }}>
                              <strong style={{ fontSize: 13, color: 'var(--danger)' }}>回归风险</strong>
                              <ul style={{ paddingLeft: 20, fontSize: 13, color: 'var(--text-secondary)', marginTop: 4 }}>
                                {b.regression_risks.map((r, i) => <li key={i}>{r}</li>)}
                              </ul>
                            </div>
                          )}

                          {/* 测试方法 */}
                          {b.test_steps?.length > 0 && (
                            <div style={{ marginTop: 8 }}>
                              <strong style={{ fontSize: 13 }}>测试方法</strong>
                              <ul style={{ paddingLeft: 20, fontSize: 13, color: 'var(--text-secondary)', marginTop: 4 }}>
                                {b.test_steps.map((s, i) => <li key={i}>{s}</li>)}
                              </ul>
                            </div>
                          )}
                        </div>
                      )}

                      {/* 生成 CODEX 修复指令按钮 */}
                      {(b.status === 'analyzed' || b.status === 'fix_ready' || b.status === 'reopened') && (
                        <div style={{ margin: '12px 0', display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
                          {(() => {
                            const fix = canGenerateFix(b)
                            return <>
                              {(b.status as string) !== 'fix_ready' && (b.status as string) !== 'fixing' && (b.status as string) !== 'waiting_test' && (
                                <button
                                  className="btn btn-primary"
                                  onClick={() => handleGenerateFixPrompt(b.id)}
                                  disabled={generatingFixId === b.id || !fix.canDo}
                                  title={fix.reason || '生成CODEX修复指令'}
                                >
                                  {generatingFixId === b.id ? '生成中...' : '📝 生成 CODEX 修复指令'}
                                </button>
                              )}
                              {b.status === 'fix_ready' && (
                                <button
                                  className="btn btn-primary"
                                  onClick={() => handleGenerateFixPrompt(b.id)}
                                  disabled={generatingFixId === b.id}
                                  title="重新生成修复指令"
                                >
                                  {generatingFixId === b.id ? '重新生成中...' : '🔄 重新生成'}
                                </button>
                              )}
                              {b.status === 'reopened' && b.probable_cause?.length > 0 && (
                                <button
                                  className="btn btn-primary"
                                  onClick={() => handleGenerateFixPrompt(b.id)}
                                  disabled={generatingFixId === b.id}
                                  title="重新生成修复指令"
                                >
                                  {generatingFixId === b.id ? '重新生成中...' : '🔄 重新生成修复指令'}
                                </button>
                              )}
                              {!fix.canDo && (
                                <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>{fix.reason}</span>
                              )}
                            </>
                          })()}
                        </div>
                      )}

                      {/* 修复指令展示和操作区 */}
                      {b.fix_prompt && (b.status === 'fix_ready' || b.status === 'fixing' || b.status === 'waiting_test' || b.status === 'resolved' || b.status === 'reopened') && (
                        <div className="bug-section">
                          <h4 className="bug-section-title">CODEX 修复指令</h4>
                          <pre className="bug-fix-prompt">{b.fix_prompt}</pre>
                          <div style={{ marginTop: 8, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                            <button
                              className="btn btn-secondary btn-sm"
                              onClick={() => handleCopyFixPrompt(b.fix_prompt!)}
                            >
                              📋 复制修复指令
                            </button>
                            {b.status === 'fix_ready' && (() => {
                              const fix = canMarkFixing(b)
                              return (
                                <button
                                  className="btn btn-warning btn-sm"
                                  onClick={() => handleMarkFixing(b.id)}
                                  disabled={updatingStatusId === b.id || !fix.canDo}
                                  title={fix.reason || '标记为修复中'}
                                >
                                  {updatingStatusId === b.id ? '更新中...' : '🔧 标记为修复中'}
                                </button>
                              )
                            })()}
                          </div>
                        </div>
                      )}

                      {/* CODEX 执行结果输入区 */}
                      {(b.status === 'fixing' || b.status === 'waiting_test' || (b.execution_result && (b.status === 'resolved' || b.status === 'reopened'))) && (
                        <div className="bug-section">
                          <h4 className="bug-section-title">CODEX 执行结果</h4>
                          <div className="form-group">
                            <label>执行结果 *</label>
                            <textarea
                              value={executionText}
                              onChange={(e) => setExecutionText(e.target.value)}
                              rows={6}
                              placeholder="粘贴 CODEX 返回的完整执行结果"
                              disabled={b.status === 'waiting_test' || b.status === 'resolved' || b.status === 'reopened'}
                            />
                          </div>
                          <div className="grid-2">
                            <div className="form-group">
                              <label>修改文件清单</label>
                              <input
                                value={filesChangedText}
                                onChange={(e) => setFilesChangedText(e.target.value)}
                                placeholder="如：src/App.tsx, src/api/bugs.ts"
                                disabled={b.status === 'waiting_test' || b.status === 'resolved' || b.status === 'reopened'}
                              />
                            </div>
                            <div className="form-group">
                              <label>测试结果</label>
                              <input
                                value={testResultText}
                                onChange={(e) => setTestResultText(e.target.value)}
                                placeholder="测试是否通过"
                                disabled={b.status === 'waiting_test' || b.status === 'resolved' || b.status === 'reopened'}
                              />
                            </div>
                          </div>
                          <div className="form-group">
                            <label>未解决问题</label>
                            <textarea
                              value={remainingIssuesText}
                              onChange={(e) => setRemainingIssuesText(e.target.value)}
                              rows={2}
                              placeholder="修复后仍然存在的问题（如有）"
                              disabled={b.status === 'waiting_test' || b.status === 'resolved' || b.status === 'reopened'}
                            />
                          </div>
                          {b.status === 'fixing' && (() => {
                            const exec = canSaveExecution(b)
                            return (
                              <button
                                className="btn btn-primary"
                                onClick={() => handleSaveExecutionResult(b.id)}
                                disabled={savingExecId === b.id || !executionText.trim() || !exec.canDo}
                                title={exec.reason || '保存执行结果'}
                              >
                                {savingExecId === b.id ? '保存中...' : '💾 保存执行结果'}
                              </button>
                            )
                          })()}
                          {b.executed_at && (
                            <p style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 4 }}>
                              执行时间：{new Date(b.executed_at).toLocaleString()}
                            </p>
                          )}
                        </div>
                      )}

                      {/* 回归测试区 */}
                      {b.status === 'waiting_test' && (
                        <div className="bug-section">
                          <h4 className="bug-section-title">回归测试</h4>
                          <div className="bug-checklist">
                            {[
                              { key: 'orig_fixed', label: '原问题已经无法复现' },
                              { key: 'normal_flow', label: '正常流程通过' },
                              { key: 'error_handling', label: '异常流程有正确提示' },
                              { key: 'refresh_ok', label: '页面刷新后数据正常' },
                              { key: 'db_ok', label: '数据库数据正确' },
                              { key: 'no_side_effect', label: '没有影响其他模块' },
                            ].map(item => (
                              <label key={item.key} className="bug-check-item">
                                <input
                                  type="checkbox"
                                  checked={!!testChecklist[item.key]}
                                  onChange={(e) => setTestChecklist(prev => ({ ...prev, [item.key]: e.target.checked }))}
                                />
                                <span>{item.label}</span>
                              </label>
                            ))}
                          </div>
                          <div className="form-group" style={{ marginTop: 8 }}>
                            <label>回归测试说明</label>
                            <textarea
                              value={testNotes}
                              onChange={(e) => setTestNotes(e.target.value)}
                              rows={2}
                              placeholder="描述测试过程和结果"
                            />
                          </div>
                          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                            <button
                              className="btn btn-success"
                              onClick={() => handleTestPass(b.id)}
                              disabled={savingTestId === b.id}
                            >
                              {savingTestId === b.id ? '提交中...' : '✓ 测试通过'}
                            </button>
                            <button
                              className="btn btn-danger"
                              onClick={() => { setShowFailDialog(true) }}
                              disabled={savingTestId === b.id}
                            >
                              ✗ 测试失败
                            </button>
                          </div>

                          {/* 测试失败对话框 */}
                          {showFailDialog && (
                            <div className="bug-fail-dialog">
                              <strong>请填写失败原因</strong>
                              <textarea
                                value={failReason}
                                onChange={(e) => setFailReason(e.target.value)}
                                rows={3}
                                placeholder="描述测试失败的具体原因"
                                autoFocus
                              />
                              <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                                <button
                                  className="btn btn-danger"
                                  onClick={() => handleTestFail(b.id)}
                                  disabled={!failReason.trim() || savingTestId === b.id}
                                >
                                  {savingTestId === b.id ? '提交中...' : '确认失败并重新打开'}
                                </button>
                                <button className="btn btn-secondary" onClick={() => { setShowFailDialog(false); setFailReason('') }}>
                                  取消
                                </button>
                              </div>
                            </div>
                          )}
                        </div>
                      )}

                      {/* 已解决 / 已关闭 / 已重新打开 状态的操作 */}
                      {(b.status === 'resolved' || b.status === 'closed') && (() => {
                        const reopen = canReopen(b)
                        return (
                          <div style={{ margin: '12px 0', display: 'flex', gap: 8, alignItems: 'center' }}>
                            <button
                              className="btn btn-warning btn-sm"
                              onClick={() => handleReopenBug(b.id)}
                              disabled={updatingStatusId === b.id || !reopen.canDo}
                              title={reopen.reason || '重新打开Bug'}
                            >
                              🔄 重新打开
                            </button>
                            {b.status === 'resolved' && (
                              <button
                                className="btn btn-secondary btn-sm"
                                onClick={() => handleCloseBug(b.id)}
                                disabled={updatingStatusId === b.id}
                                title="关闭Bug"
                              >
                                关闭 Bug
                              </button>
                            )}
                          </div>
                        )
                      })()}

                      {b.status === 'reopened' && (
                        <div style={{ margin: '12px 0', padding: 12, background: 'rgba(239,68,68,0.1)', borderRadius: 8 }}>
                          <p style={{ color: 'var(--danger)', fontSize: 13, marginBottom: 8 }}>
                            此 Bug 已被重新打开，之前的分析和执行记录已保留。您可以重新进行 AI 分析或直接重新生成修复指令。
                          </p>
                          <div style={{ display: 'flex', gap: 8 }}>
                            <button
                              className="btn btn-primary"
                              onClick={() => handleAnalyzeBug(b.id)}
                              disabled={analyzingBugId === b.id}
                            >
                              {analyzingBugId === b.id ? '正在分析...' : '🤖 重新 AI 分析'}
                            </button>
                            {b.probable_cause?.length > 0 && (
                              <button
                                className="btn btn-secondary"
                                onClick={() => handleGenerateFixPrompt(b.id)}
                                disabled={generatingFixId === b.id}
                              >
                                {generatingFixId === b.id ? '生成中...' : '📝 重新生成修复指令'}
                              </button>
                            )}
                            <button
                              className="btn btn-secondary btn-sm"
                              onClick={() => handleCloseBug(b.id)}
                              disabled={updatingStatusId === b.id}
                            >
                              关闭 Bug
                            </button>
                          </div>
                        </div>
                      )}

                      {/* 状态变更记录 */}
                      {bugStatusLogs.length > 0 && selectedBugId === b.id && (
                        <div className="bug-section">
                          <h4 className="bug-section-title">状态变更记录</h4>
                          <div className="bug-timeline">
                            {bugStatusLogs.map((log) => (
                              <div key={log.id} className="bug-timeline-item">
                                <div className="bug-timeline-dot" />
                                <div className="bug-timeline-content">
                                  <span className="bug-timeline-status">
                                    {log.from_status_label ? `${log.from_status_label} → ` : ''}{log.to_status_label}
                                  </span>
                                  {log.reason && <span style={{ color: 'var(--text-secondary)', fontSize: 12 }}> — {log.reason}</span>}
                                  <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
                                    {new Date(log.created_at).toLocaleString()}
                                  </div>
                                </div>
                              </div>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/** 需求输入表单 */
function RequirementForm({ project, projectId, onSaved }: {
  project: ProjectDetail; projectId: number; onSaved: () => void
}) {
  const [form, setForm] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)

  // need_* 字段默认值应为 'unknown' 而非空字符串
  const needKeys = ['need_login', 'need_database', 'need_ai', 'need_third_party', 'need_upload', 'need_export']

  useEffect(() => {
    const init: Record<string, string> = {
      idea: project.idea || '',
      description: project.description || '',
      target_users: project.target_users || '',
      core_features: project.core_features || '',
      scenarios: project.scenarios || '',
      platform: project.platform || '',
      tech_requirements: project.tech_requirements || '',
      exclude_features: project.exclude_features || '',
      additional_notes: project.additional_notes || '',
    }
    for (const key of needKeys) {
      init[key] = String((project as unknown as Record<string, unknown>)[key] || '') || 'unknown'
    }
    setForm(init)
  }, [project])

  const handleSave = async () => {
    setSaving(true)
    try {
      const res = await updateRequirements(projectId, form)
      if (res.ok) {
        onSaved()
        // 通过全局 toast 通知
        window.dispatchEvent(new CustomEvent('app-toast', { detail: { msg: '需求保存成功', type: 'success' } }))
      } else {
        window.dispatchEvent(new CustomEvent('app-toast', { detail: { msg: res.error?.detail || '保存失败', type: 'error' } }))
      }
    } catch (err: any) {
      const serverError = err?.response?.data?.error?.detail || err?.message
      window.dispatchEvent(new CustomEvent('app-toast', { detail: { msg: serverError || '网络错误，请检查后端服务', type: 'error' } }))
    } finally {
      setSaving(false)
    }
  }

  const updateField = (key: string, value: string) => {
    setForm((prev) => ({ ...prev, [key]: value }))
  }

  return (
    <div>
      <div className="form-group">
        <label>软件想法 / 一句话描述</label>
        <textarea value={form.idea || ''} onChange={(e) => updateField('idea', e.target.value)} rows={2}
          placeholder="我想做一个商品采集、图片处理和多平台发布的软件" />
      </div>

      <div className="grid-2">
        <div className="form-group">
          <label>目标用户</label>
          <input value={form.target_users || ''} onChange={(e) => updateField('target_users', e.target.value)}
            placeholder="电商卖家、自媒体运营者" />
        </div>
        <div className="form-group">
          <label>核心功能</label>
          <input value={form.core_features || ''} onChange={(e) => updateField('core_features', e.target.value)}
            placeholder="商品采集、图片处理、多平台发布" />
        </div>
      </div>

      <div className="grid-2">
        <div className="form-group">
          <label>使用场景</label>
          <input value={form.scenarios || ''} onChange={(e) => updateField('scenarios', e.target.value)}
            placeholder="用户需要从多个平台采集商品信息并一键发布" />
        </div>
        <div className="form-group">
          <label>平台类型</label>
          <select value={form.platform || ''} onChange={(e) => updateField('platform', e.target.value)}>
            <option value="">请选择</option>
            <option value="web">网页端</option>
            <option value="desktop">桌面端</option>
            <option value="mobile">移动端</option>
          </select>
        </div>
      </div>

      <div className="grid-3">
        {[
          { key: 'need_login', label: '需要登录' },
          { key: 'need_database', label: '需要数据库' },
          { key: 'need_ai', label: '需要AI' },
          { key: 'need_third_party', label: '需要第三方接口' },
          { key: 'need_upload', label: '需要上传文件' },
          { key: 'need_export', label: '需要导出结果' },
        ].map((item) => (
          <div key={item.key} className="form-group">
            <label>{item.label}</label>
            <select value={form[item.key] || ''} onChange={(e) => updateField(item.key, e.target.value)}>
              <option value="unknown">未确定</option>
              <option value="yes">是</option>
              <option value="no">否</option>
            </select>
          </div>
        ))}
      </div>

      <div className="form-group">
        <label>技术要求</label>
        <textarea value={form.tech_requirements || ''} onChange={(e) => updateField('tech_requirements', e.target.value)}
          rows={2} placeholder="用户确定的技术要求" />
      </div>

      <div className="grid-2">
        <div className="form-group">
          <label>不希望出现的功能</label>
          <textarea value={form.exclude_features || ''} onChange={(e) => updateField('exclude_features', e.target.value)}
            rows={2} placeholder="明确排除的功能" />
        </div>
        <div className="form-group">
          <label>其他补充说明</label>
          <textarea value={form.additional_notes || ''} onChange={(e) => updateField('additional_notes', e.target.value)}
            rows={2} placeholder="任何补充信息" />
        </div>
      </div>

      <button className="btn btn-primary" onClick={handleSave} disabled={saving}>
        {saving ? '保存中...' : '💾 保存需求'}
      </button>
    </div>
  )
}

/** 分析结果展示 */
function AnalysisResult({ data }: { data: Record<string, unknown> }) {
  return (
    <div className="card">
      {data.product_definition != null && (
        <div className="analysis-section">
          <h4>💡 产品定义</h4>
          <p>{String(data.product_definition)}</p>
        </div>
      )}
      {data.problem != null && (
        <div className="analysis-section">
          <h4>🎯 解决的问题</h4>
          <p>{String(data.problem)}</p>
        </div>
      )}
      {(data.target_users as string[])?.length > 0 && (
        <div className="analysis-section">
          <h4>👥 目标用户</h4>
          <ul>{(data.target_users as string[]).map((u, i) => <li key={i}>{u}</li>)}</ul>
        </div>
      )}
      {(data.usage_scenarios as string[])?.length > 0 && (
        <div className="analysis-section">
          <h4>📋 使用场景</h4>
          <ul>{(data.usage_scenarios as string[]).map((s, i) => <li key={i}>{s}</li>)}</ul>
        </div>
      )}
      {(data.core_value as string[])?.length > 0 && (
        <div className="analysis-section">
          <h4>⭐ 核心价值</h4>
          <ul>{(data.core_value as string[]).map((v, i) => <li key={i}>{v}</li>)}</ul>
        </div>
      )}
      {(data.business_flow as string[])?.length > 0 && (
        <div className="analysis-section">
          <h4>🔄 业务流程</h4>
          <ol>{(data.business_flow as string[]).map((f, i) => <li key={i}>{f}</li>)}</ol>
        </div>
      )}
      {(data.modules as unknown[])?.length > 0 && (
        <div className="analysis-section">
          <h4>📦 系统模块</h4>
          {(data.modules as Record<string, unknown>[]).map((m, i) => (
            <div key={i} className="card" style={{ marginBottom: 8, padding: 12 }}>
              <strong>{String(m.name)}</strong>
              <p style={{ fontSize: 13, color: 'var(--text-muted)', margin: '4px 0' }}>{String(m.description)}</p>
              {(m.features as string[])?.length > 0 && (
                <ul style={{ paddingLeft: 16, fontSize: 13 }}>
                  {(m.features as string[]).map((f, j) => <li key={j}>{f}</li>)}
                </ul>
              )}
            </div>
          ))}
        </div>
      )}
      {(data.risks as string[])?.length > 0 && (
        <div className="analysis-section">
          <h4>⚠️ 项目风险</h4>
          <ul>{(data.risks as string[]).map((r, i) => <li key={i}>{r}</li>)}</ul>
        </div>
      )}
    </div>
  )
}

/** 模块和MVP视图 - 使用后端真实数据 */
const STAGE_LABELS: Record<string, string> = {
  mvp: 'MVP 必做',
  phase2: '二期',
  later: '后续',
}

function ModulesView({ modules }: { modules: any[] }) {
  return (
    <div>
      <h3 style={{ marginBottom: 12, fontSize: 16 }}>系统模块</h3>
      <div className="grid-2">
        {modules.map((mod) => (
          <div key={mod.id} className="card">
            <div className="card-title" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              {mod.name}
              {mod.version_stage && (
                <span className={`badge badge-${mod.version_stage === 'mvp' ? 'success' : mod.version_stage === 'phase2' ? 'warning' : 'draft'}`}>
                  {STAGE_LABELS[mod.version_stage] || mod.version_stage}
                </span>
              )}
            </div>
            <p style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 8 }}>
              {mod.description || ''}
            </p>
            {mod.features && mod.features.length > 0 && (
              <ul style={{ paddingLeft: 16, fontSize: 13, color: 'var(--text-secondary)' }}>
                {mod.features.map((f: any) => (
                  <li key={f.id} style={{ marginBottom: 4 }}>
                    {f.name}
                    {f.description ? ` — ${f.description}` : ''}
                    {f.version_stage && (
                      <span className={`badge badge-sm badge-${f.version_stage === 'mvp' ? 'success' : f.version_stage === 'phase2' ? 'warning' : 'draft'}`} style={{ marginLeft: 6 }}>
                        {STAGE_LABELS[f.version_stage] || f.version_stage}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
