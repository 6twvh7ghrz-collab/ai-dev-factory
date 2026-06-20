"""CommandNormalizer - 自然语言指令标准化 V1

使用确定性规则将用户自然语言映射到标准化指令。
V1 不调用任何 AI 模型，纯规则匹配。

支持：
- 精确短语匹配
- 关键词组合匹配
- 否定词识别与保护
- UNKNOWN 兜底
"""
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional, List, Tuple


class CommandIntent(str, Enum):
    START_DEVELOPMENT = "start_development"
    GENERATE_PLAN = "generate_plan"
    DIAGNOSE_BLOCKER = "diagnose_blocker"
    SHOW_STATUS = "show_status"
    PAUSE_EXECUTOR = "pause_executor"
    RESUME_EXECUTOR = "resume_executor"
    STOP_EXECUTOR = "stop_executor"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class NormalizedCommand:
    intent: CommandIntent
    confidence: float
    source: str          # "exact_match" | "keyword_match" | "fallback"
    original_text: str
    normalized_text: str  # 清理后的文本
    requires_confirmation: bool
    message: str


# ── 否定词列表（只用于阻止启动类动作） ──
_NEGATION_WORDS = {
    "不要", "别", "暂时不", "先不要", "取消",
    "不是", "勿", "禁止", "暂不", "先别",
    "不要开始", "不用", "先不用",
}
# "停止" 本身是一个合法指令 (STOP_EXECUTOR)，不在否定词列表中

# ── 意图规则：(意图, 置信度, 关键词/短语列表) ──
# 精确匹配 → 置信度 0.95, source=exact_match
# 关键词组合 → 置信度 0.80, source=keyword_match

_EXACT_RULES: List[Tuple[CommandIntent, List[str]]] = [
    (CommandIntent.START_DEVELOPMENT, [
        "开始开发", "让ai干", "让ai去做", "开始自动开发",
        "自动开发", "启动开发", "让ai开始干活", "开始执行",
        "启动自动开发", "开始干活", "让ai开始开发",
    ]),
    (CommandIntent.GENERATE_PLAN, [
        "帮我规划", "生成任务", "拆解需求", "生成开发任务",
        "生成工程规划", "规划任务", "制定计划", "任务规划",
    ]),
    (CommandIntent.DIAGNOSE_BLOCKER, [
        "为什么不能执行", "卡住了", "检查阻塞原因",
        "为什么卡住了", "为什么阻塞", "为什么跑不了",
        "检查为什么不能执行", "为什么不动", "诊断阻塞",
        "什么原因阻塞", "查阻塞", "看阻塞",
    ]),
    (CommandIntent.SHOW_STATUS, [
        "现在做到哪里了", "查看状态", "执行情况",
        "当前进度", "做到哪了", "进度如何", "看看进度",
        "查看进度", "任务状态", "开发进度",
    ]),
    (CommandIntent.PAUSE_EXECUTOR, [
        "暂停", "先停一下", "暂停执行", "暂停一下",
        "停一下", "先暂停", "暂停开发",
    ]),
    (CommandIntent.RESUME_EXECUTOR, [
        "继续", "恢复执行", "继续执行", "恢复",
        "继续开发", "接着干",
    ]),
    (CommandIntent.STOP_EXECUTOR, [
        "停止执行", "结束任务", "终止", "终止执行",
        "停止开发",
    ]),
]

_KEYWORD_RULES: List[Tuple[CommandIntent, List[List[str]]]] = [
    # (意图, [必须出现的关键词组列表，组内为OR，组间为AND])
    (CommandIntent.START_DEVELOPMENT, [
        ["开始", "启动", "开发", "执行", "干"],
        ["开发", "执行", "自动", "干活", "任务"],
    ]),
    (CommandIntent.GENERATE_PLAN, [
        ["规划", "生成", "拆解", "制定"],
        ["任务", "计划", "需求", "方案"],
    ]),
    (CommandIntent.DIAGNOSE_BLOCKER, [
        ["为什么", "检查", "查看", "诊断", "查"],
        ["不能执行", "卡住", "阻塞", "不动", "跑不了"],
    ]),
    (CommandIntent.SHOW_STATUS, [
        ["进度", "状态", "情况", "做到", "查看", "看看"],
        ["哪", "如何", "怎样", "进度"],
    ]),
    (CommandIntent.PAUSE_EXECUTOR, [
        ["暂停", "停", "休息"],
    ]),
    (CommandIntent.RESUME_EXECUTOR, [
        ["继续", "恢复", "接着"],
    ]),
    (CommandIntent.STOP_EXECUTOR, [
        ["停止", "结束", "终止"],
    ]),
]


class CommandNormalizer:
    """自然语言指令标准化器 V1"""

    def __init__(self):
        pass

    def normalize(self, text: str) -> NormalizedCommand:
        """将用户自然语言输入标准化为指令

        Args:
            text: 用户原始输入

        Returns:
            NormalizedCommand: 标准化后的指令
        """
        if not text or not text.strip():
            return NormalizedCommand(
                intent=CommandIntent.UNKNOWN,
                confidence=0.0,
                source="fallback",
                original_text=text or "",
                normalized_text="",
                requires_confirmation=False,
                message="输入为空，无法识别指令",
            )

        original = text.strip()
        # 清理：去标点、统一大小写、去多余空白
        cleaned = self._clean_text(original)

        # 1. 检查否定词
        if self._has_negation(cleaned):
            return NormalizedCommand(
                intent=CommandIntent.UNKNOWN,
                confidence=0.0,
                source="fallback",
                original_text=original,
                normalized_text=cleaned,
                requires_confirmation=False,
                message="检测到否定表达，不执行任何操作",
            )

        # 2. 精确短语匹配
        for intent, phrases in _EXACT_RULES:
            for phrase in phrases:
                cleaned_phrase = self._clean_text(phrase)
                if cleaned_phrase in cleaned or cleaned == cleaned_phrase:
                    return NormalizedCommand(
                        intent=intent,
                        confidence=0.95,
                        source="exact_match",
                        original_text=original,
                        normalized_text=cleaned,
                        requires_confirmation=True,
                        message=self._intent_message(intent, "exact_match"),
                    )

        # 3. 关键词组合匹配
        for intent, keyword_groups in _KEYWORD_RULES:
            if self._match_keyword_groups(cleaned, keyword_groups):
                return NormalizedCommand(
                    intent=intent,
                    confidence=0.80,
                    source="keyword_match",
                    original_text=original,
                    normalized_text=cleaned,
                    requires_confirmation=True,
                    message=self._intent_message(intent, "keyword_match"),
                )

        # 4. UNKNOWN 兜底
        return NormalizedCommand(
            intent=CommandIntent.UNKNOWN,
            confidence=0.0,
            source="fallback",
            original_text=original,
            normalized_text=cleaned,
            requires_confirmation=False,
            message=f"无法识别指令：「{original[:50]}」",
        )

    def _clean_text(self, text: str) -> str:
        """清理文本：去标点、统一小写、去多余空白"""
        # 去标点（保留中文）
        cleaned = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbfa-zA-Z0-9]', '', text)
        # 统一小写
        cleaned = cleaned.lower()
        return cleaned

    def _has_negation(self, text: str) -> bool:
        """检查文本是否包含否定词，且否定词后跟着启动/开发类动作

        关键逻辑：
        - 否定词必须在文本开头或前面有空格/标点
        - 否定词后面必须跟着启动类动作（start_development 或 generate_plan）
        - "为什么不能执行" 中的 "不能" 不触发否定保护（因为前面有"为什么"）
        - "停止执行" 本身是合法指令，不触发否定保护
        """
        for neg in _NEGATION_WORDS:
            idx = text.find(neg)
            if idx < 0:
                continue

            # 否定词必须在开头，或前面有非字母数字的边界
            if idx > 0:
                prev_char = text[idx - 1]
                # 中文上下文：前面字符如果是汉字或问号，说明"不"是复合词的一部分
                if '\u4e00' <= prev_char <= '\u9fff' or prev_char == '?':
                    continue

            after_neg = text[idx + len(neg):]
            # 检查否定词后面是否有启动/开发类动作
            for _, phrases in _EXACT_RULES:
                for phrase in phrases:
                    cleaned_phrase = self._clean_text(phrase)
                    if cleaned_phrase in after_neg:
                        return True

            # 也检查关键词组合（只检查启动和规划类）
            for intent, keyword_groups in _KEYWORD_RULES:
                if intent in (CommandIntent.START_DEVELOPMENT, CommandIntent.GENERATE_PLAN):
                    if self._match_keyword_groups(after_neg, keyword_groups):
                        return True
        return False

    def _match_keyword_groups(self, text: str,
                              keyword_groups: List[List[str]]) -> bool:
        """检查文本是否匹配关键词组（组间AND，组内OR）"""
        for group in keyword_groups:
            if not any(kw in text for kw in group):
                return False
        return True

    def _intent_message(self, intent: CommandIntent,
                        source: str) -> str:
        """生成意图对应的消息"""
        messages = {
            CommandIntent.START_DEVELOPMENT:
                "准备启动自动开发，确认后才会执行",
            CommandIntent.GENERATE_PLAN:
                "准备生成工程规划，确认后才会执行",
            CommandIntent.DIAGNOSE_BLOCKER:
                "准备检查阻塞原因，确认后才会执行",
            CommandIntent.SHOW_STATUS:
                "准备查看执行状态，确认后才会执行",
            CommandIntent.PAUSE_EXECUTOR:
                "准备暂停执行，确认后才会执行",
            CommandIntent.RESUME_EXECUTOR:
                "准备恢复执行，确认后才会执行",
            CommandIntent.STOP_EXECUTOR:
                "准备停止执行，确认后才会执行",
            CommandIntent.UNKNOWN:
                "无法识别指令",
        }
        return messages.get(intent, "未知指令")
