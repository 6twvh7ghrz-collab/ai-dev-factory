"""
PlannerPreviewService V1.6 - AI 工程规划预览 + 手工预览（持久化版本）

职责：
  1. 读取项目目标和 needs_planning 任务
  2. 调用 DeepSeek 生成结构化工程方案
  3. 支持手工预览入口（create_manual_preview）
  4. 持久化规划预览到 planning_previews 表
  5. 电商平台任务实施风险评估
  6. 支持预览有效期管理、快照校验

禁止：
  - 修改 development_tasks
  - 修改 readiness_status
  - 补写 files_to_modify
  - 创建 executor_run / lease / resource_lock
  - 启动 Worker
  - 写入项目代码
"""
import json
import re
import time
import uuid
import hashlib
import sqlite3
import logging
import threading
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.core.security import decrypt_value, mask_key
from app.planner.planning_risk_policy import POLICY_VERSION

logger = logging.getLogger("planner.preview")

# ── 常量 ──

MAX_TASKS_PER_PLAN = 12
MODEL_TIMEOUT_SECONDS = 120
MAX_RETRIES = 1
PREVIEW_VALIDITY_HOURS = 24  # 规划预览有效期

# 高风险电商平台关键词
E_COMMERCE_PLATFORMS = {
    "拼多多": {
        "primary": "官方 API（多多进宝 / pdd.open）",
        "fallbacks": ["CSV 导出后导入", "第三方合规数据服务", "浏览器插件（需用户登录）"],
        "anti_bot_risk": "high",
        "account_risk": "high",
        "legal_terms_risk": "反爬条款严格，需使用官方 API",
    },
    "抖音": {
        "primary": "抖音开放平台 API",
        "fallbacks": ["CSV / Excel 导出导入", "第三方合规数据服务", "人工辅助采集"],
        "anti_bot_risk": "high",
        "account_risk": "high",
        "legal_terms_risk": "非官方采集可能违反平台协议",
    },
    "小红书": {
        "primary": "小红书开放平台 API（如有）或人工辅助",
        "fallbacks": ["第三方合规数据服务", "CSV 导入", "浏览器插件（需用户登录）"],
        "anti_bot_risk": "critical",
        "account_risk": "critical",
        "legal_terms_risk": "反爬非常严格，账号封禁风险高",
    },
    "1688": {
        "primary": "阿里巴巴开放平台 API（1688 开放平台）",
        "fallbacks": ["CSV 导出导入", "第三方合规数据服务"],
        "anti_bot_risk": "medium",
        "account_risk": "medium",
        "legal_terms_risk": "建议优先使用官方 API",
    },
    "淘宝": {
        "primary": "淘宝开放平台 API（TOP）",
        "fallbacks": ["CSV 导出导入", "第三方合规数据服务"],
        "anti_bot_risk": "high",
        "account_risk": "high",
        "legal_terms_risk": "非官方采集违反平台协议",
    },
    "京东": {
        "primary": "京东开放平台 API（宙斯）",
        "fallbacks": ["CSV 导出导入", "第三方合规数据服务"],
        "anti_bot_risk": "medium",
        "account_risk": "medium",
        "legal_terms_risk": "建议优先使用官方 API",
    },
    "天猫": {
        "primary": "天猫开放平台 API",
        "fallbacks": ["CSV 导出导入", "第三方合规数据服务"],
        "anti_bot_risk": "high",
        "account_risk": "high",
        "legal_terms_risk": "非官方采集违反平台协议",
    },
    "快手": {
        "primary": "快手开放平台 API",
        "fallbacks": ["CSV 导出导入", "第三方合规数据服务", "人工辅助采集"],
        "anti_bot_risk": "high",
        "account_risk": "high",
        "legal_terms_risk": "非官方采集可能违反平台协议",
    },
}

# 电商/爬虫相关风险关键词
E_COMMERCE_RISK_KEYWORDS = [
    "采集", "爬虫", "爬取", "抓取", "数据获取", "自动获取",
    "自动登录", "模拟登录", "selenium", "playwright", "puppeteer",
    "价格监控", "竞品分析", "商品数据",
]

# ── 数据类 ──


@dataclass
class PlanCallRecord:
    """规划调用记录"""
    provider: str = ""
    model: str = ""
    request_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    success: bool = False
    error: str = ""
    input_summary: str = ""
    output_summary: str = ""


# ── 并发保护 ──

# {project_id: threading.Lock}
_planning_locks: Dict[int, threading.Lock] = {}
_locks_lock = threading.Lock()


def _acquire_planning_lock(project_id: int) -> bool:
    """获取项目规划锁，返回 True 表示成功获取"""
    with _locks_lock:
        if project_id in _planning_locks:
            return False
        _planning_locks[project_id] = threading.Lock()
    _planning_locks[project_id].acquire()
    return True


def _release_planning_lock(project_id: int):
    """释放项目规划锁"""
    with _locks_lock:
        if project_id in _planning_locks:
            try:
                _planning_locks[project_id].release()
            except RuntimeError:
                pass
            del _planning_locks[project_id]


def is_planning_in_progress(project_id: int) -> bool:
    """检查项目是否正在规划中"""
    with _locks_lock:
        return project_id in _planning_locks


# ── Schema 定义 ──

PLAN_SCHEMA = {
    "type": "object",
    "required": [
        "project_summary", "recommended_architecture",
        "execution_order", "tasks", "global_risks",
        "approval_items", "next_step"
    ],
    "properties": {
        "project_summary": {"type": "string"},
        "recommended_architecture": {"type": "string"},
        "execution_order": {
            "type": "array",
            "items": {"type": "integer"}
        },
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "task_id", "title", "recommended_status",
                    "implementation_strategy", "files_to_modify_suggestion",
                    "test_strategy", "dependencies", "risks",
                    "requires_approval", "data_source_strategy"
                ],
                "properties": {
                    "task_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "recommended_status": {
                        "type": "string",
                        "enum": ["needs_planning", "ready"]
                    },
                    "implementation_strategy": {"type": "string"},
                    "files_to_modify_suggestion": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "test_strategy": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "dependencies": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "risks": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "requires_approval": {"type": "boolean"},
                    "risk_level": {
                        "type": "string",
                        "enum": ["LOW", "MEDIUM", "HIGH", "BLOCKED"]
                    },
                    "risk_signals": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "risk_reason": {"type": "string"},
                    "data_source_strategy": {
                        "type": "object",
                        "properties": {
                            "primary": {"type": "string"},
                            "fallbacks": {
                                "type": "array",
                                "items": {"type": "string"}
                            }
                        }
                    }
                }
            }
        },
        "global_risks": {
            "type": "array",
            "items": {"type": "string"}
        },
        "approval_items": {
            "type": "array",
            "items": {"type": "string"}
        },
        "next_step": {
            "type": "string",
            "enum": ["review_plan", "approve_and_execute", "request_manual_review"]
        }
    }
}


def validate_plan_schema(plan: dict) -> Optional[str]:
    """简单验证规划 JSON 结构。返回 None 表示通过，否则返回错误信息。"""
    required_keys = PLAN_SCHEMA["required"]
    for key in required_keys:
        if key not in plan:
            return f"缺少必要字段: {key}"

    if not isinstance(plan.get("tasks"), list):
        return "tasks 必须是数组"

    for i, task in enumerate(plan["tasks"]):
        if not isinstance(task, dict):
            return f"tasks[{i}] 必须是对象"
        for tk in PLAN_SCHEMA["properties"]["tasks"]["items"]["required"]:
            if tk not in task:
                return f"tasks[{i}] 缺少字段: {tk}"

    return None


# ── 主服务 ──


class PlannerPreviewService:
    """AI 工程规划预览服务

    复用 ModelAdapter 的模式，但使用独立的 prompt 和输出 schema。
    不修改任何业务数据。
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._openai = None
        self._api_key: Optional[str] = None
        self._model: str = ""
        self._provider: str = ""
        self._base_url: str = ""

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_client(self) -> bool:
        """确保 OpenAI 客户端已初始化"""
        if self._openai is not None:
            return True

        try:
            from openai import OpenAI
        except ImportError:
            self._error_detail = "openai 包未安装"
            return False

        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT provider, model, api_key_encrypted, base_url "
                "FROM ai_configs WHERE is_active=1 LIMIT 1"
            ).fetchone()
        finally:
            conn.close()

        if not row:
            self._error_detail = "未找到活跃的 AI 配置"
            return False

        self._provider = row["provider"]
        self._model = row["model"]
        encrypted = row["api_key_encrypted"] or ""
        self._base_url = row["base_url"] or "https://api.deepseek.com"

        if not encrypted:
            self._error_detail = "API Key 为空"
            return False

        try:
            self._api_key = decrypt_value(encrypted)
        except Exception as e:
            self._error_detail = f"API Key 解密失败: {e}"
            return False

        self._openai = OpenAI(api_key=self._api_key, base_url=self._base_url)
        return True

    def generate_preview(
        self,
        project_id: int,
        task_ids: Optional[List[int]] = None,
        force_regenerate: bool = False,
    ) -> Dict[str, Any]:
        """生成工程规划预览

        Args:
            project_id: 项目 ID
            task_ids: 要规划的任务 ID 列表，None 表示规划所有 needs_planning 任务
            force_regenerate: 强制重新生成，即使已有未过期预览

        Returns:
            dict with ok, code, preview, preview_id, expires_at, call_record etc.
        """
        # 1. 检查是否已有未过期预览（非强制重新生成时）
        if not force_regenerate:
            existing = self._get_existing_valid_preview(project_id)
            if existing:
                return existing

        # 并发保护
        if not _acquire_planning_lock(project_id):
            return {
                "ok": False,
                "code": "PLANNING_ALREADY_IN_PROGRESS",
                "project_id": project_id,
                "message": "该项目已有规划请求正在进行中",
                "preview": None,
                "preview_id": None,
                "expires_at": None,
                "call_record": None,
            }

        try:
            return self._generate_preview_internal(project_id, task_ids, force_regenerate)
        finally:
            _release_planning_lock(project_id)

    def _generate_preview_internal(
        self,
        project_id: int,
        task_ids: Optional[List[int]] = None,
        force_regenerate: bool = False,
    ) -> Dict[str, Any]:
        """内部实现"""
        conn = self._get_conn()
        try:
            cur = conn.cursor()

            # 1. 获取项目信息
            cur.execute(
                "SELECT id, name, description, status FROM projects WHERE id = ?",
                (project_id,)
            )
            proj = cur.fetchone()
            if not proj:
                return {
                    "ok": False,
                    "code": "PROJECT_NOT_FOUND",
                    "project_id": project_id,
                    "message": f"项目 #{project_id} 不存在",
                    "preview": None,
                    "preview_id": None,
                    "expires_at": None,
                    "call_record": None,
                }

            # 2. 获取 needs_planning 任务
            if task_ids:
                placeholders = ",".join("?" * len(task_ids))
                cur.execute(
                    f"""SELECT id, title, description, status, readiness_status,
                               codex_prompt, acceptance_criteria, dependencies,
                               implementation_steps, files_to_modify, test_steps
                        FROM development_tasks
                        WHERE project_id = ? AND id IN ({placeholders})
                        ORDER BY id""",
                    (project_id, *task_ids)
                )
            else:
                cur.execute(
                    """SELECT id, title, description, status, readiness_status,
                               codex_prompt, acceptance_criteria, dependencies,
                               implementation_steps, files_to_modify, test_steps
                        FROM development_tasks
                        WHERE project_id = ? AND status = 'pending'
                          AND readiness_status = 'needs_planning'
                        ORDER BY id
                        LIMIT ?""",
                    (project_id, MAX_TASKS_PER_PLAN)
                )

            tasks = [dict(row) for row in cur.fetchall()]

            if not tasks:
                return {
                    "ok": False,
                    "code": "NO_NEEDS_PLANNING_TASKS",
                    "project_id": project_id,
                    "message": "该项目没有待规划的任务",
                    "preview": None,
                    "preview_id": None,
                    "expires_at": None,
                    "call_record": None,
                }

        finally:
            conn.close()

        # 3. 确保模型客户端
        if not self._ensure_client():
            return {
                "ok": False,
                "code": "MODEL_NOT_AVAILABLE",
                "project_id": project_id,
                "message": f"模型适配器初始化失败: {self._error_detail}",
                "preview": None,
                "preview_id": None,
                "expires_at": None,
                "call_record": None,
            }

        # 4. 构建 prompt
        system_prompt = self._build_planner_system_prompt()
        user_prompt = self._build_planner_user_prompt(
            project_name=proj["name"],
            project_description=proj["description"] or "",
            tasks=tasks,
        )

        # 5. 调用模型（最多重试 1 次）
        call_record = PlanCallRecord(
            provider=self._provider,
            model=self._model,
        )

        for attempt in range(MAX_RETRIES + 1):
            result = self._call_model(system_prompt, user_prompt, call_record)

            if not result:
                continue

            # 验证 JSON 结构
            validation_error = validate_plan_schema(result)
            if validation_error:
                if attempt < MAX_RETRIES:
                    logger.warning(
                        f"Plan JSON validation failed (attempt {attempt+1}): {validation_error}"
                    )
                    # 重试时附加错误信息
                    user_prompt = (
                        f"上次生成的规划 JSON 格式无效，错误：{validation_error}\n\n"
                        f"请严格按照 JSON Schema 重新生成。\n\n"
                        f"原任务：\n{user_prompt}"
                    )
                    continue
                else:
                    call_record.error = f"JSON 校验失败: {validation_error}"
                    call_record.success = False
                    return {
                        "ok": False,
                        "code": "PLANNER_OUTPUT_INVALID",
                        "project_id": project_id,
                        "message": f"模型输出 JSON 校验失败（已重试 {MAX_RETRIES} 次）: {validation_error}",
                        "preview": None,
                        "preview_id": None,
                        "expires_at": None,
                        "call_record": {
                            "provider": call_record.provider,
                            "model": call_record.model,
                            "request_id": call_record.request_id,
                            "started_at": call_record.started_at,
                            "finished_at": call_record.finished_at,
                            "input_tokens": call_record.input_tokens,
                            "output_tokens": call_record.output_tokens,
                            "success": call_record.success,
                            "error": call_record.error,
                        },
                    }

            # 通过校验
            call_record.success = True

            # 6. 电商平台风险增强
            result = self._enhance_ecommerce_risks(result, tasks)

            # 7. 记录到 ai_generation_logs
            self._log_to_db(project_id, call_record)

            # 8. 持久化规划预览到 planning_previews 表
            preview_id = str(uuid.uuid4())
            task_ids_list = [t["id"] for t in tasks]
            project_snapshot_hash = self._compute_snapshot_hash({
                "id": proj["id"],
                "name": proj["name"],
                "description": proj["description"],
                "status": proj["status"],
            })
            tasks_snapshot_hash = self._compute_tasks_snapshot_hash(tasks)
            risk_summary = self._extract_risk_summary(result)
            expires_at = (datetime.now() + timedelta(hours=PREVIEW_VALIDITY_HOURS)).isoformat()

            # 如果强制重新生成，先 invalidate 旧预览
            if force_regenerate:
                self._invalidate_existing_previews(project_id)

            self._persist_preview(
                preview_id=preview_id,
                project_id=project_id,
                provider=call_record.provider,
                model=call_record.model,
                task_ids_json=json.dumps(task_ids_list, ensure_ascii=False),
                preview_json=json.dumps(result, ensure_ascii=False),
                risk_summary_json=json.dumps(risk_summary, ensure_ascii=False),
                project_snapshot_hash=project_snapshot_hash,
                tasks_snapshot_hash=tasks_snapshot_hash,
                request_id=call_record.request_id,
                expires_at=expires_at,
            )

            return {
                "ok": True,
                "code": "PLAN_PREVIEW_READY",
                "executed": False,
                "project_id": project_id,
                "project_name": proj["name"],
                "preview_id": preview_id,
                "expires_at": expires_at,
                "preview": result,
                "call_record": {
                    "provider": call_record.provider,
                    "model": call_record.model,
                    "request_id": call_record.request_id,
                    "started_at": call_record.started_at,
                    "finished_at": call_record.finished_at,
                    "input_tokens": call_record.input_tokens,
                    "output_tokens": call_record.output_tokens,
                    "success": call_record.success,
                    "error": call_record.error,
                },
            }

        # 所有重试都失败
        return {
            "ok": False,
            "code": "PLANNER_CALL_FAILED",
            "project_id": project_id,
            "message": f"模型调用失败: {call_record.error}",
            "preview": None,
            "preview_id": None,
            "expires_at": None,
            "call_record": {
                "provider": call_record.provider,
                "model": call_record.model,
                "request_id": call_record.request_id,
                "started_at": call_record.started_at,
                "finished_at": call_record.finished_at,
                "input_tokens": call_record.input_tokens,
                "output_tokens": call_record.output_tokens,
                "success": call_record.success,
                "error": call_record.error,
            },
        }

    def _call_model(
        self,
        system_prompt: str,
        user_prompt: str,
        call_record: PlanCallRecord,
    ) -> Optional[dict]:
        """调用 DeepSeek API 并解析响应"""
        call_record.started_at = time.strftime("%Y-%m-%dT%H:%M:%S")

        try:
            response = self._openai.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=8192,
                timeout=MODEL_TIMEOUT_SECONDS,
            )
        except Exception as e:
            call_record.error = f"API 调用失败: {e}"
            call_record.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            return None

        call_record.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        call_record.request_id = response.id or ""
        call_record.input_tokens = response.usage.prompt_tokens if response.usage else 0
        call_record.output_tokens = response.usage.completion_tokens if response.usage else 0

        raw_content = response.choices[0].message.content if response.choices else ""

        # 解析 JSON
        parsed = self._parse_json_response(raw_content)
        return parsed

    def _parse_json_response(self, raw: str) -> Optional[dict]:
        """从模型响应中解析 JSON"""
        if not raw:
            return None

        # 尝试直接解析
        try:
            return json.loads(raw.strip())
        except json.JSONDecodeError:
            pass

        # 从 ```json ... ``` 提取
        json_match = re.search(r'```json\s*(.*?)\s*```', raw, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # 从 ``` ... ``` 提取
        code_match = re.search(r'```\s*(.*?)\s*```', raw, re.DOTALL)
        if code_match:
            try:
                return json.loads(code_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # 提取 { ... } 块
        bracket_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if bracket_match:
            try:
                return json.loads(bracket_match.group(0))
            except json.JSONDecodeError:
                pass

        return None

    def _build_planner_system_prompt(self) -> str:
        """构建规划系统提示词"""
        return """你是一个资深的软件工程架构师。请根据提供的项目信息和任务列表，生成结构化的工程规划方案。

## 输出格式要求
你必须返回严格的 JSON 对象，格式如下：

```json
{
  "project_summary": "项目总体概述，1-2段",
  "recommended_architecture": "推荐的技术架构描述",
  "execution_order": [26, 27, 28],
  "tasks": [
    {
      "task_id": 26,
      "title": "任务标题",
      "recommended_status": "needs_planning",
      "implementation_strategy": "实现策略描述",
      "files_to_modify_suggestion": ["文件路径1", "文件路径2"],
      "test_strategy": ["测试策略1", "测试策略2"],
      "dependencies": ["依赖说明"],
      "risks": ["风险1", "风险2"],
      "requires_approval": true,
      "data_source_strategy": {
        "primary": "主要数据来源策略",
        "fallbacks": ["备用策略1", "备用策略2"]
      }
    }
  ],
  "global_risks": ["全局风险1"],
  "approval_items": ["需要审批的事项"],
  "next_step": "review_plan"
}
```

## 重要规则
1. recommended_status 只能是 "needs_planning" 或 "ready"
2. 涉及外部平台数据采集的任务，必须保持 recommended_status="needs_planning" 和 requires_approval=true
3. 不要建议任何全自动爬虫方案，优先考虑官方 API、用户辅助、合规数据服务
4. 对于电商平台（拼多多、抖音、小红书、1688、淘宝、京东等），必须评估反爬风险、账号风险、合规风险
5. 高风险任务必须标注 requires_approval=true
6. execution_order 中的任务 ID 必须与 tasks 数组中的 task_id 一致
7. 不要在 JSON 之外返回任何文本

## 平台数据采集策略
- 拼多多 → 多多进宝 API / CSV导入
- 抖音 → 抖音开放平台 API
- 小红书 → 人工辅助 / 第三方合规服务
- 1688 → 阿里巴巴开放平台 API
- 淘宝 → 淘宝开放平台 TOP API
- 京东 → 京东宙斯开放平台 API

不要建议使用 Selenium/Playwright 等自动化工具进行数据采集。"""

    def _build_planner_user_prompt(
        self,
        project_name: str,
        project_description: str,
        tasks: List[dict],
    ) -> str:
        """构建用户提示词"""
        tasks_text = []
        for t in tasks:
            task_info = f"""
任务 #{t['id']}: {t['title']}
  说明: {t.get('description') or '无'}
  当前状态: {t.get('status', 'unknown')}
  准备状态: {t.get('readiness_status', 'draft')}
  提示词: {t.get('codex_prompt') or '无'}
  验收标准: {t.get('acceptance_criteria') or '无'}
  依赖: {t.get('dependencies') or '无'}
"""
            tasks_text.append(task_info)

        return f"""请为以下项目生成工程规划方案：

## 项目信息
名称: {project_name}
描述: {project_description or '无描述'}

## 需要规划的任务
{chr(10).join(tasks_text)}

请严格按照 JSON Schema 生成工程规划方案。"""

    def _enhance_ecommerce_risks(
        self,
        plan: dict,
        tasks: List[dict],
    ) -> dict:
        """对电商平台相关任务增强风险评估"""
        for task in plan.get("tasks", []):
            title = task.get("title", "").lower()
            desc = task.get("implementation_strategy", "").lower()

            # 检查是否为电商平台相关
            matched_platform = None
            for platform, strategy in E_COMMERCE_PLATFORMS.items():
                if platform in title or platform in desc:
                    matched_platform = platform
                    break

            # 检查是否为爬虫相关
            is_scraper = any(
                kw in title or kw in desc
                for kw in E_COMMERCE_RISK_KEYWORDS
            )

            if matched_platform and is_scraper:
                strategy = E_COMMERCE_PLATFORMS[matched_platform]

                # 增强 data_source_strategy
                if not task.get("data_source_strategy"):
                    task["data_source_strategy"] = {}
                ds = task["data_source_strategy"]
                if not ds.get("primary"):
                    ds["primary"] = strategy["primary"]
                if not ds.get("fallbacks"):
                    ds["fallbacks"] = strategy["fallbacks"]

                # 增强风险
                existing_risks = set(task.get("risks", []))
                platform_risks = [
                    f"平台风控风险 ({strategy['anti_bot_risk']}): 反爬机制严格",
                    f"账号安全风险 ({strategy['account_risk']}): 非官方采集可能导致封号",
                    f"合规风险: {strategy['legal_terms_risk']}",
                    "维护成本: 平台接口可能频繁变动，需持续适配",
                ]
                for r in platform_risks:
                    if r not in existing_risks:
                        task.setdefault("risks", []).append(r)

                # 高风险任务标记
                task["recommended_status"] = "needs_planning"
                task["requires_approval"] = True
                task["risk_level"] = "HIGH"  # V1.8: 显式风险等级

                # 添加到审批列表
                approval_text = (
                    f"任务 #{task.get('task_id')} ({task.get('title')}) 涉及 {matched_platform} 数据采集，"
                    f"存在平台风控和合规风险，需要人工审批"
                )
                if approval_text not in plan.get("approval_items", []):
                    plan.setdefault("approval_items", []).append(approval_text)

            # 纯爬虫任务（非特定平台）
            elif is_scraper:
                task["recommended_status"] = "needs_planning"
                task["requires_approval"] = True
                task["risk_level"] = "HIGH"  # V1.8: 显式风险等级
                task.setdefault("risks", []).append("数据采集类任务存在法律和平台合规风险")
                approval_text = (
                    f"任务 #{task.get('task_id')} ({task.get('title')}) 涉及数据采集，"
                    f"需要人工评估合规性"
                )
                if approval_text not in plan.get("approval_items", []):
                    plan.setdefault("approval_items", []).append(approval_text)

        return plan

    def _log_to_db(self, project_id: int, record: PlanCallRecord):
        """记录模型调用到 ai_generation_logs"""
        try:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO ai_generation_logs
                       (project_id, generation_type, model, input_summary, output_summary,
                        success, error_message, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        project_id,
                        "planner_preview",
                        record.model,
                        record.input_summary or f"规划预览: {record.started_at}",
                        f"tokens: in={record.input_tokens} out={record.output_tokens}",
                        record.success,
                        record.error or None,
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"记录规划日志失败: {e}")

    def mask_sensitive(self, text: str) -> str:
        """脱敏 API Key"""
        if self._api_key and self._api_key in text:
            text = text.replace(self._api_key, mask_key(self._api_key))
        return text

    # ── V1.4 持久化相关方法 ──

    @staticmethod
    def _compute_snapshot_hash(data: dict) -> str:
        """计算数据的稳定 SHA-256 快照哈希"""
        serialized = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _compute_tasks_snapshot_hash(tasks: List[dict]) -> str:
        """计算任务列表的快照哈希（只取关键字段）"""
        snapshots = []
        for t in tasks:
            snapshot = {
                "task_id": t.get("id"),
                "title": t.get("title", ""),
                "status": t.get("status", ""),
                "readiness_status": t.get("readiness_status", ""),
                "dependencies": t.get("dependencies", ""),
                "files_to_modify": t.get("files_to_modify", ""),
                "implementation_steps": t.get("implementation_steps", ""),
                "test_steps": t.get("test_steps", ""),
                "acceptance_criteria": t.get("acceptance_criteria", ""),
            }
            # 如果有 updated_at 则加入
            if "updated_at" in t:
                snapshot["updated_at"] = str(t["updated_at"])
            snapshots.append(snapshot)
        serialized = json.dumps(snapshots, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _extract_risk_summary(preview: dict) -> dict:
        """从规划预览中提取风险摘要"""
        tasks = preview.get("tasks", [])
        risk_counts = {"low": 0, "medium": 0, "high": 0, "blocked": 0}
        high_risk_task_ids = []
        for task in tasks:
            risks = task.get("risks", [])
            requires_approval = task.get("requires_approval", False)
            recommended = task.get("recommended_status", "")
            # 简单风险分级
            if recommended == "needs_planning" and requires_approval:
                # 检查是否涉及电商平台
                title = task.get("title", "").lower()
                is_ecommerce = any(p in title for p in E_COMMERCE_PLATFORMS.keys())
                if is_ecommerce:
                    risk_counts["high"] += 1
                    high_risk_task_ids.append(task.get("task_id"))
                else:
                    risk_counts["medium"] += 1
            elif recommended == "ready":
                risk_counts["low"] += 1
            else:
                risk_counts["low"] += 1

        return {
            "total_tasks": len(tasks),
            "risk_counts": risk_counts,
            "high_risk_task_ids": high_risk_task_ids,
            "global_risks": preview.get("global_risks", []),
        }

    def _persist_preview(
        self,
        preview_id: str,
        project_id: int,
        provider: str,
        model: str,
        task_ids_json: str,
        preview_json: str,
        risk_summary_json: str,
        project_snapshot_hash: str,
        tasks_snapshot_hash: str,
        request_id: str,
        expires_at: str,
    ):
        """持久化规划预览到 planning_previews 表"""
        try:
            conn = self._get_conn()
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                conn.execute(
                    """INSERT INTO planning_previews
                       (preview_id, project_id, provider, model, status, schema_version,
                        project_snapshot_hash, tasks_snapshot_hash, task_ids_json,
                        preview_json, risk_summary_json, request_id,
                        created_at, expires_at, updated_at)
                       VALUES (?, ?, ?, ?, 'generated', '1.0',
                               ?, ?, ?, ?, ?, ?,
                               ?, ?, ?)""",
                    (
                        preview_id, project_id, provider, model,
                        project_snapshot_hash, tasks_snapshot_hash, task_ids_json,
                        preview_json, risk_summary_json, request_id,
                        datetime.now().isoformat(), expires_at, datetime.now().isoformat(),
                    ),
                )
                conn.commit()
                logger.info(f"规划预览已持久化: preview_id={preview_id}")
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"持久化规划预览失败（非致命）: {e}")

    def _get_existing_valid_preview(self, project_id: int) -> Optional[Dict[str, Any]]:
        """获取项目已有的未过期规划预览"""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            try:
                cur = conn.cursor()
                cur.execute(
                    """SELECT * FROM planning_previews
                       WHERE project_id = ?
                         AND status = 'generated'
                         AND expires_at > ?
                       ORDER BY created_at DESC
                       LIMIT 1""",
                    (project_id, datetime.now().isoformat()),
                )
                row = cur.fetchone()
                if not row:
                    return None

                preview = json.loads(row["preview_json"])
                return {
                    "ok": True,
                    "code": "PLAN_PREVIEW_READY",
                    "executed": False,
                    "project_id": project_id,
                    "preview_id": row["preview_id"],
                    "expires_at": row["expires_at"],
                    "preview": preview,
                    "call_record": {
                        "provider": row["provider"],
                        "model": row["model"],
                        "request_id": row["request_id"],
                        "from_cache": True,
                    },
                }
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"查询已有规划预览失败: {e}")
            return None

    def _invalidate_existing_previews(self, project_id: int):
        """将项目的所有 generated 状态预览标记为 invalidated"""
        try:
            conn = self._get_conn()
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                conn.execute(
                    """UPDATE planning_previews
                       SET status = 'invalidated', updated_at = ?
                       WHERE project_id = ? AND status = 'generated'""",
                    (datetime.now().isoformat(), project_id),
                )
                conn.commit()
                logger.info(f"已失效项目 #{project_id} 的旧规划预览")
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"失效旧规划预览失败: {e}")

    # ── V1.6 手工规划预览 ──

    def create_manual_preview(
        self,
        project_id: int,
        preview_data: dict,
        created_by: str = "user",
    ) -> Dict[str, Any]:
        """
        V1.6: 创建手工规划预览（不调用 AI 模型）。

        复用正式 Preview 的校验和持久化逻辑，经过同样的：
        - Schema 校验
        - Task 存在性校验
        - Project 归属校验
        - 快照生成
        - Snapshot hash
        - 风险评估
        - 过期时间
        - 正式数据库事务

        Args:
            project_id: 项目 ID
            preview_data: 手工构造的规划数据（必须符合 PLAN_SCHEMA）
            created_by: 创建者标识

        Returns:
            dict with ok, code, preview_id, etc.
        """
        # 1. Schema 校验
        validation_error = validate_plan_schema(preview_data)
        if validation_error:
            return {
                "ok": False,
                "code": "PLANNER_OUTPUT_INVALID",
                "project_id": project_id,
                "message": f"手工规划 JSON 校验失败: {validation_error}",
                "preview": None,
                "preview_id": None,
                "expires_at": None,
                "call_record": None,
            }

        # 2. 获取项目信息
        conn = self._get_conn()
        try:
            cur = conn.cursor()

            cur.execute(
                "SELECT id, name, description, status FROM projects WHERE id = ?",
                (project_id,),
            )
            proj = cur.fetchone()
            if not proj:
                return {
                    "ok": False,
                    "code": "PROJECT_NOT_FOUND",
                    "project_id": project_id,
                    "message": f"项目 #{project_id} 不存在",
                    "preview": None,
                    "preview_id": None,
                    "expires_at": None,
                    "call_record": None,
                }

            # 3. 验证所有 task_id 存在且属于该项目
            task_ids_in_preview = [t["task_id"] for t in preview_data.get("tasks", [])]
            if not task_ids_in_preview:
                return {
                    "ok": False,
                    "code": "NO_TASKS_IN_PREVIEW",
                    "project_id": project_id,
                    "message": "规划预览中没有任务",
                    "preview": None,
                    "preview_id": None,
                    "expires_at": None,
                    "call_record": None,
                }

            placeholders = ",".join("?" * len(task_ids_in_preview))
            cur.execute(
                f"""SELECT id, title, description, status, readiness_status,
                           dependencies, files_to_modify, implementation_steps,
                           test_steps, acceptance_criteria, updated_at
                    FROM development_tasks
                    WHERE id IN ({placeholders})
                    ORDER BY id""",
                task_ids_in_preview,
            )
            tasks = [dict(row) for row in cur.fetchall()]

            found_ids = {t["id"] for t in tasks}
            missing_ids = set(task_ids_in_preview) - found_ids
            if missing_ids:
                return {
                    "ok": False,
                    "code": "TASK_NOT_FOUND",
                    "project_id": project_id,
                    "message": f"任务不存在: {sorted(missing_ids)}",
                    "preview": None,
                    "preview_id": None,
                    "expires_at": None,
                    "call_record": None,
                }

            # 4. 验证所有任务属于该项目
            cur.execute(
                f"""SELECT id FROM development_tasks
                    WHERE project_id = ? AND id IN ({placeholders})""",
                (project_id, *task_ids_in_preview),
            )
            project_task_ids = {row["id"] for row in cur.fetchall()}
            cross_project_ids = found_ids - project_task_ids
            if cross_project_ids:
                return {
                    "ok": False,
                    "code": "TASK_PROJECT_MISMATCH",
                    "project_id": project_id,
                    "message": f"任务不属于项目 #{project_id}: {sorted(cross_project_ids)}",
                    "preview": None,
                    "preview_id": None,
                    "expires_at": None,
                    "call_record": None,
                }

            # 5. 电商平台风险增强
            preview_data = self._enhance_ecommerce_risks(preview_data, tasks)

            # 6. 生成快照哈希
            project_snapshot_hash = self._compute_snapshot_hash({
                "id": proj["id"],
                "name": proj["name"],
                "description": proj["description"],
                "status": proj["status"],
            })
            tasks_snapshot_hash = self._compute_tasks_snapshot_hash(tasks)
            risk_summary = self._extract_risk_summary(preview_data)
            expires_at = (datetime.now() + timedelta(hours=PREVIEW_VALIDITY_HOURS)).isoformat()

        finally:
            conn.close()

        # 7. 持久化预览
        preview_id = str(uuid.uuid4())
        task_ids_list = [t["id"] for t in tasks]

        self._persist_preview(
            preview_id=preview_id,
            project_id=project_id,
            provider=None,
            model=None,
            task_ids_json=json.dumps(task_ids_list, ensure_ascii=False),
            preview_json=json.dumps(preview_data, ensure_ascii=False),
            risk_summary_json=json.dumps(risk_summary, ensure_ascii=False),
            project_snapshot_hash=project_snapshot_hash,
            tasks_snapshot_hash=tasks_snapshot_hash,
            request_id=f"manual-{preview_id[:8]}",
            expires_at=expires_at,
        )

        return {
            "ok": True,
            "code": "PLAN_PREVIEW_READY",
            "executed": False,
            "project_id": project_id,
            "project_name": proj["name"],
            "preview_id": preview_id,
            "expires_at": expires_at,
            "preview": preview_data,
            "call_record": {
                "provider": None,
                "model": None,
                "preview_source": "manual",
                "created_by": created_by,
                "policy_version": POLICY_VERSION,
                "from_cache": False,
                "success": True,
            },
        }


# ── 全局单例 ──

_planner_service: Optional[PlannerPreviewService] = None


def get_planner_preview_service(db_path: str = None) -> PlannerPreviewService:
    """获取全局 PlannerPreviewService 单例"""
    global _planner_service
    if _planner_service is None:
        if db_path is None:
            db_path = str(
                Path(__file__).resolve().parent.parent.parent / "data" / "ai_factory.db"
            )
        _planner_service = PlannerPreviewService(db_path)
    return _planner_service
