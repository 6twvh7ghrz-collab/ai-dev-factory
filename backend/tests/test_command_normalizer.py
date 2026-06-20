"""CommandNormalizer 回归测试

覆盖：
1. "让AI干" → start_development
2. "开始自动开发" → start_development
3. "为什么卡住了" → diagnose_blocker
4. "查看现在做到哪了" → show_status
5. "暂停执行" → pause_executor
6. "继续执行" → resume_executor
7. "停止执行" → stop_executor
8. "先不要开始开发" 不得返回 start_development
9. 空文本返回 UNKNOWN
10. 未知表达返回 UNKNOWN
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.executor.command_normalizer import CommandNormalizer, CommandIntent


def test_exact_start_development():
    """1. "让AI干" → start_development (exact_match)"""
    n = CommandNormalizer()
    for text in ["让AI干", "让ai干", "让AI去做", "开始自动开发",
                 "开始开发", "自动开发", "启动开发"]:
        result = n.normalize(text)
        assert result.intent == CommandIntent.START_DEVELOPMENT, \
            f"'{text}' → {result.intent}, expected start_development"
        assert result.confidence >= 0.80, \
            f"'{text}' confidence={result.confidence}"


def test_exact_generate_plan():
    """生成任务/规划 → generate_plan"""
    n = CommandNormalizer()
    for text in ["帮我规划", "生成任务", "拆解需求", "制定计划"]:
        result = n.normalize(text)
        assert result.intent == CommandIntent.GENERATE_PLAN, \
            f"'{text}' → {result.intent}"


def test_diagnose_blocker():
    """3. "为什么卡住了" → diagnose_blocker"""
    n = CommandNormalizer()
    for text in ["为什么卡住了", "检查阻塞原因", "为什么不能执行",
                 "检查为什么不能执行", "为什么阻塞"]:
        result = n.normalize(text)
        assert result.intent == CommandIntent.DIAGNOSE_BLOCKER, \
            f"'{text}' → {result.intent}, expected diagnose_blocker"


def test_show_status():
    """4. "查看现在做到哪了" → show_status"""
    n = CommandNormalizer()
    for text in ["查看现在做到哪了", "现在做到哪里了", "查看状态",
                 "执行情况", "当前进度", "做到哪了"]:
        result = n.normalize(text)
        assert result.intent == CommandIntent.SHOW_STATUS, \
            f"'{text}' → {result.intent}, expected show_status"


def test_pause_executor():
    """5. "暂停执行" → pause_executor"""
    n = CommandNormalizer()
    for text in ["暂停执行", "先停一下", "暂停", "暂停一下"]:
        result = n.normalize(text)
        assert result.intent == CommandIntent.PAUSE_EXECUTOR, \
            f"'{text}' → {result.intent}"


def test_resume_executor():
    """6. "继续执行" → resume_executor"""
    n = CommandNormalizer()
    for text in ["继续执行", "恢复执行", "继续", "恢复"]:
        result = n.normalize(text)
        assert result.intent == CommandIntent.RESUME_EXECUTOR, \
            f"'{text}' → {result.intent}"


def test_stop_executor():
    """7. "停止执行" → stop_executor"""
    n = CommandNormalizer()
    for text in ["停止执行", "结束任务", "终止执行"]:
        result = n.normalize(text)
        assert result.intent == CommandIntent.STOP_EXECUTOR, \
            f"'{text}' → {result.intent}"


def test_negation_protection():
    """8. "先不要开始开发" 不得返回 start_development"""
    n = CommandNormalizer()
    negation_texts = [
        "先不要开始开发",
        "不要开始开发",
        "别开始开发",
        "暂时不开发",
        "先不要自动开发",
        "先不要开始",
        "不要执行",
        "停止开始",
    ]
    for text in negation_texts:
        result = n.normalize(text)
        assert result.intent != CommandIntent.START_DEVELOPMENT, \
            f"'{text}' → {result.intent}, should NOT be start_development"


def test_empty_input():
    """9. 空文本返回 UNKNOWN"""
    n = CommandNormalizer()
    result = n.normalize("")
    assert result.intent == CommandIntent.UNKNOWN
    assert result.confidence == 0.0

    result = n.normalize("   ")
    assert result.intent == CommandIntent.UNKNOWN


def test_unknown_expression():
    """10. 未知表达返回 UNKNOWN"""
    n = CommandNormalizer()
    unknown_texts = [
        "今天天气怎么样",
        "帮我点个外卖",
        "hello world",
        "随便说点什么",
        "12345",
    ]
    for text in unknown_texts:
        result = n.normalize(text)
        assert result.intent == CommandIntent.UNKNOWN, \
            f"'{text}' → {result.intent}, expected UNKNOWN"


def test_requires_confirmation():
    """所有非 UNKNOWN 指令都应 requires_confirmation=True"""
    n = CommandNormalizer()
    for text in ["开始开发", "帮我规划", "暂停", "继续", "查看状态",
                 "停止执行", "检查阻塞原因"]:
        result = n.normalize(text)
        if result.intent != CommandIntent.UNKNOWN:
            assert result.requires_confirmation is True, \
                f"'{text}' requires_confirmation should be True"


def test_keyword_combination():
    """关键词组合匹配测试"""
    n = CommandNormalizer()
    # "开始开发吧" 包含 "开始" + "开发"
    result = n.normalize("开始开发吧")
    assert result.intent == CommandIntent.START_DEVELOPMENT

    # "看看进度如何" 包含 "看看" + "如何" (show_status)
    result = n.normalize("看看进度如何")
    assert result.intent == CommandIntent.SHOW_STATUS

    # "查一下为什么卡住了"
    result = n.normalize("查一下为什么卡住了")
    assert result.intent == CommandIntent.DIAGNOSE_BLOCKER


if __name__ == "__main__":
    tests = [
        ("1. 让AI干→start_development", test_exact_start_development),
        ("2. 生成任务→generate_plan", test_exact_generate_plan),
        ("3. 为什么卡住了→diagnose_blocker", test_diagnose_blocker),
        ("4. 查看状态→show_status", test_show_status),
        ("5. 暂停→pause_executor", test_pause_executor),
        ("6. 继续→resume_executor", test_resume_executor),
        ("7. 停止→stop_executor", test_stop_executor),
        ("8. 否定句保护", test_negation_protection),
        ("9. 空文本→UNKNOWN", test_empty_input),
        ("10. 未知表达→UNKNOWN", test_unknown_expression),
        ("11. requires_confirmation", test_requires_confirmation),
        ("12. 关键词组合", test_keyword_combination),
    ]
    passed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  [PASS] {name}")
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {name}: {e}")
        except Exception as e:
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
