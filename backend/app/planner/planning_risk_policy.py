"""
PlanningRiskPolicy V1.6 - 确定性风险分级规则

后端确定性风险分级，不信任模型返回的风险等级。
取模型风险与 RiskPolicy 确定性风险中更高的一项。

风险等级：
  - LOW: 可在用户确认后转 ready
  - MEDIUM: 可审批但需显式确认
  - HIGH: 不得直接转 ready，继续 needs_planning
  - BLOCKED: 绝不允许写回 ready

审批权限矩阵（V1.6 解耦风险等级与写回权限）：
  LOW     → auto_approvable=true,  user_approvable=true,  can_write_ready=true
  MEDIUM  → auto_approvable=false, user_approvable=true,  can_write_ready=true（需明确确认）
  HIGH    → auto_approvable=false, user_approvable=false, can_write_ready=false
  BLOCKED → auto_approvable=false, user_approvable=false, can_write_ready=false
"""

POLICY_VERSION = "v1.6"
from typing import List, Dict, Any, Optional

# ── 风险等级枚举 ──

RISK_LOW = "LOW"
RISK_MEDIUM = "MEDIUM"
RISK_HIGH = "HIGH"
RISK_BLOCKED = "BLOCKED"

RISK_ORDER = {RISK_LOW: 0, RISK_MEDIUM: 1, RISK_HIGH: 2, RISK_BLOCKED: 3}

# ── 高风险关键词 ──

# HIGH - 电商平台数据采集
HIGH_RISK_PLATFORMS = [
    "拼多多", "抖音", "小红书", "1688", "淘宝", "天猫", "京东", "快手",
]

HIGH_RISK_KEYWORDS = [
    "采集", "爬虫", "爬取", "抓取", "数据获取", "自动获取",
    "自动登录", "模拟登录", "selenium", "playwright", "puppeteer",
    "价格监控", "竞品分析", "商品数据",
    "自动发布", "批量发布", "自动上架",
    "验证码处理", "反爬规避", "风控",
]

# BLOCKED - 绝对禁止
BLOCKED_KEYWORDS = [
    "绕过验证码", "绕过安全机制", "窃取凭据", "窃取密钥",
    "修改系统目录", "读取密钥", "读取密码",
    "规避风控", "绕过风控", "未授权访问",
    "路径穿越", "超出工作区",
    "支付", "交易", "转账",
    "删除数据库", "DROP TABLE", "DROP DATABASE",
    "部署生产环境", "发布到生产",
]

# MEDIUM 关键词
MEDIUM_KEYWORDS = [
    "新增模块", "新增表", "SQLite", "数据库迁移",
    "配置文件", "修改配置",
    "安装依赖", "pip install", "npm install",
    "官方API", "开放平台",
]

# LOW 关键词
LOW_KEYWORDS = [
    "UI组件", "纯函数", "工具函数", "数据展示",
    "报表生成", "图片处理", "图片压缩",
    "前端组件", "前端页面", "样式",
    "本地存储", "localStorage",
    "Electron", "Electron窗口", "Electron IPC",
]


def assess_risk(
    task_id: int,
    title: str,
    description: str = "",
    files_to_modify: Optional[List[str]] = None,
    implementation_strategy: str = "",
    model_risk: str = "LOW",
) -> Dict[str, Any]:
    """
    确定性评估任务风险等级（V1.6 结构化返回）。

    Args:
        task_id: 任务 ID
        title: 任务标题
        description: 任务描述
        files_to_modify: 建议修改的文件列表
        implementation_strategy: 实现策略描述
        model_risk: 模型输出的风险等级

    Returns:
        dict with risk_level, risk_signals, risk_reason, approval_requirement,
             auto_approvable, user_approvable, can_write_ready_after_approval,
             policy_version, allow_auto_ready (backwards compat), reasons (backwards compat)
    """
    reasons = []
    risk_signals = []
    deterministic_risk = RISK_LOW

    title_lower = title.lower() if title else ""
    desc_lower = description.lower() if description else ""
    impl_lower = implementation_strategy.lower() if implementation_strategy else ""
    combined = f"{title_lower} {desc_lower} {impl_lower}"

    # 1. 检查 BLOCKED 关键词
    for kw in BLOCKED_KEYWORDS:
        if kw.lower() in combined:
            reasons.append(f"禁止项: 涉及 {kw}")
            risk_signals.append(kw)
            deterministic_risk = RISK_BLOCKED
            break

    if deterministic_risk != RISK_BLOCKED:
        # 2. 检查 HIGH 风险 - 平台
        for platform in HIGH_RISK_PLATFORMS:
            if platform in title or platform in description or platform in implementation_strategy:
                # 同时检查是否涉及采集/发布关键词
                is_risky_op = any(
                    kw in combined
                    for kw in ["采集", "发布", "上架", "自动", "登录", "爬虫"]
                )
                if is_risky_op:
                    reasons.append(f"高风险平台操作: {platform}")
                    risk_signals.append(f"platform:{platform}")
                    deterministic_risk = RISK_HIGH
                    break

        # 3. 检查 HIGH 风险 - 通用关键词
        if deterministic_risk < RISK_HIGH:
            for kw in HIGH_RISK_KEYWORDS:
                if kw.lower() in combined:
                    reasons.append(f"高风险操作: {kw}")
                    risk_signals.append(kw)
                    deterministic_risk = RISK_HIGH
                    break

    # 4. 检查 MEDIUM 风险
    if deterministic_risk < RISK_MEDIUM:
        for kw in MEDIUM_KEYWORDS:
            if kw.lower() in combined:
                reasons.append(f"中等风险: {kw}")
                risk_signals.append(kw)
                deterministic_risk = RISK_MEDIUM
                break

    # 5. 文件路径检查
    if files_to_modify:
        for f in files_to_modify:
            f_lower = f.lower() if isinstance(f, str) else ""
            if not f_lower:
                continue
            # 绝对路径
            if f.startswith("/") or (len(f) >= 2 and f[1] == ":"):
                reasons.append(f"文件路径异常: {f} (绝对路径)")
                risk_signals.append(f"absolute_path:{f}")
                deterministic_risk = max_risk(deterministic_risk, RISK_HIGH)
            # 路径穿越
            if ".." in f:
                reasons.append(f"文件路径异常: {f} (路径穿越)")
                risk_signals.append(f"path_traversal:{f}")
                deterministic_risk = max_risk(deterministic_risk, RISK_BLOCKED)
            # 系统目录
            if f_lower.startswith("/etc/") or f_lower.startswith("/sys/") or f_lower.startswith("c:\\windows"):
                reasons.append(f"文件路径异常: {f} (系统目录)")
                risk_signals.append(f"system_path:{f}")
                deterministic_risk = max_risk(deterministic_risk, RISK_BLOCKED)

    # 6. 取模型风险与确定性风险中更高的一项
    model_risk_level = model_risk.upper() if isinstance(model_risk, str) else RISK_LOW
    if model_risk_level not in RISK_ORDER:
        model_risk_level = RISK_LOW
    final_risk = max_risk(deterministic_risk, model_risk_level)

    # 7. V1.6 审批权限矩阵
    auto_approvable = final_risk == RISK_LOW
    user_approvable = final_risk in (RISK_LOW, RISK_MEDIUM)
    can_write_ready_after = final_risk in (RISK_LOW, RISK_MEDIUM)

    if final_risk == RISK_LOW:
        approval_requirement = "standard_approval"
    elif final_risk == RISK_MEDIUM:
        approval_requirement = "explicit_user_approval"
    elif final_risk == RISK_HIGH:
        approval_requirement = "manual_review_required"
    else:
        approval_requirement = "blocked"

    # 构建风险原因描述
    if reasons:
        risk_reason = "；".join(reasons)
    else:
        risk_reason = "低风险任务"

    # 向后兼容字段
    allow_auto_ready = auto_approvable

    return {
        "task_id": task_id,
        "risk_level": final_risk,
        "model_risk": model_risk_level,
        "deterministic_risk": deterministic_risk,
        "reasons": reasons if reasons else ["低风险任务"],
        "allow_auto_ready": allow_auto_ready,
        # V1.6 新增结构化字段
        "risk_signals": risk_signals,
        "risk_reason": risk_reason,
        "approval_requirement": approval_requirement,
        "auto_approvable": auto_approvable,
        "user_approvable": user_approvable,
        "can_write_ready_after_approval": can_write_ready_after,
        "policy_version": POLICY_VERSION,
    }


def max_risk(a: str, b: str) -> str:
    """返回两个风险等级中较高的一项"""
    return a if RISK_ORDER.get(a, 0) >= RISK_ORDER.get(b, 0) else b


def is_approvable(risk_level: str) -> bool:
    """检查风险等级是否可审批（LOW/MEDIUM 可审批）"""
    return risk_level in (RISK_LOW, RISK_MEDIUM)


def can_write_ready(risk_level: str) -> bool:
    """检查是否可直接写回 ready（V1.4 旧接口，保持向后兼容）"""
    return risk_level == RISK_LOW


def can_write_ready_v16(
    risk_level: str,
    approval_mode: str = "",
    approved_by: str = "",
    risk_acknowledged: bool = False,
    approval_reason: str = "",
) -> bool:
    """
    V1.6 审批权限决策：根据风险等级 + 审批条件判断是否可写回 ready。

    审批矩阵：
      LOW     → 无条件可写回
      MEDIUM  → 需 selected_tasks + user + risk_acknowledged + approval_reason 非空
      HIGH    → 不可写回
      BLOCKED → 不可写回

    Args:
        risk_level: 风险等级 (LOW/MEDIUM/HIGH/BLOCKED)
        approval_mode: 审批模式 (selected_tasks/all_tasks)
        approved_by: 审批人 (user/system)
        risk_acknowledged: 是否已明确确认风险
        approval_reason: 审批原因

    Returns:
        bool: 是否可以写回 ready
    """
    if risk_level == RISK_LOW:
        return True

    if risk_level == RISK_MEDIUM:
        return (
            approval_mode == "selected_tasks"
            and approved_by == "user"
            and risk_acknowledged is True
            and bool(approval_reason.strip())
        )

    # HIGH / BLOCKED / 未知
    return False


def get_approval_permissions(risk_level: str) -> Dict[str, Any]:
    """
    获取风险等级对应的审批权限。

    Returns:
        dict with auto_approvable, user_approvable, can_write_ready_after_approval,
             approval_requirement, requires_risk_acknowledgment
    """
    permissions = {
        RISK_LOW: {
            "auto_approvable": True,
            "user_approvable": True,
            "can_write_ready_after_approval": True,
            "approval_requirement": "standard_approval",
            "requires_risk_acknowledgment": False,
        },
        RISK_MEDIUM: {
            "auto_approvable": False,
            "user_approvable": True,
            "can_write_ready_after_approval": True,
            "approval_requirement": "explicit_user_approval",
            "requires_risk_acknowledgment": True,
        },
        RISK_HIGH: {
            "auto_approvable": False,
            "user_approvable": False,
            "can_write_ready_after_approval": False,
            "approval_requirement": "manual_review_required",
            "requires_risk_acknowledgment": True,
        },
        RISK_BLOCKED: {
            "auto_approvable": False,
            "user_approvable": False,
            "can_write_ready_after_approval": False,
            "approval_requirement": "blocked",
            "requires_risk_acknowledgment": True,
        },
    }
    return permissions.get(risk_level, permissions[RISK_BLOCKED])
