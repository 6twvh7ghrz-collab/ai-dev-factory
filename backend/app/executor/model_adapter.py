"""内置 DeepSeek 模型适配器 - 直接调用 AI API 生成代码

替代外部脚本模式（fix_normalize_title.py），实现：
  TaskWorker → ModelAdapter → DeepSeek API → 结构化JSON → 文件写入 → 验证

输出格式要求：
{
  "files": [
    {"path": "module_demo.py", "content": "..."},
    {"path": "test_module_demo.py", "content": "..."}
  ],
  "test_command": "python -m pytest test_module_demo.py -v"
}

写入前验证：
- JSON 合法
- 文件路径在白名单
- Python 语法可编译
- 测试文件名符合 test_*.py
- 至少包含 4 个 test_ 函数
"""
import json
import re
import hashlib
import time
import sqlite3
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field

from app.core.security import decrypt_value


@dataclass
class ModelCallResult:
    """模型调用结果"""
    success: bool
    files_written: List[str] = field(default_factory=list)
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    request_id: str = ""
    model: str = ""
    provider: str = ""
    error: str = ""
    http_status: int = 0
    started_at: str = ""
    finished_at: str = ""
    response_json: Optional[Dict] = None


class ModelAdapter:
    """内置 DeepSeek 模型适配器"""

    # 最大自动修复尝试次数
    MAX_RETRY_ON_INVALID_OUTPUT = 1

    def __init__(self, db_path: str, sandbox_path: str):
        self.db_path = db_path
        self.sandbox_path = Path(sandbox_path).resolve()
        self._openai = None
        self._api_key: Optional[str] = None
        self._model: str = ""
        self._provider: str = ""
        self._base_url: str = ""

    def _ensure_client(self) -> bool:
        """确保 OpenAI 客户端已初始化"""
        if self._openai is not None:
            return True

        try:
            from openai import OpenAI
        except ImportError:
            self._error_detail = "openai 包未安装"
            return False

        # 从数据库读取加密配置
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
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

    def generate_code(
        self,
        prompt: str,
        allowed_files: List[str],
        test_files: Optional[List[str]] = None,
        error_feedback: Optional[str] = None,
    ) -> ModelCallResult:
        """
        调用 DeepSeek API 生成代码并写入沙箱。

        Args:
            prompt: 任务描述/提示词
            allowed_files: 允许修改的文件列表
            test_files: 预期的测试文件列表
            error_feedback: 上次失败的错误反馈（用于自动修复）

        Returns:
            ModelCallResult
        """
        if not self._ensure_client():
            return ModelCallResult(
                success=False,
                error=f"模型适配器初始化失败: {self._error_detail}",
            )

        started_at = time.strftime("%Y-%m-%dT%H:%M:%S")

        # 构建系统提示
        system_prompt = self._build_system_prompt(allowed_files, test_files, error_feedback)

        # 构建用户提示
        user_prompt = prompt
        if error_feedback:
            user_prompt = (
                f"上次生成失败，错误信息：\n{error_feedback}\n\n"
                f"原任务：\n{prompt}\n\n"
                f"请修复上述错误，重新生成代码。"
            )

        # 调用 API
        try:
            response = self._openai.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=8192,
            )
        except Exception as e:
            return ModelCallResult(
                success=False,
                error=f"DeepSeek API 调用失败: {e}",
                started_at=started_at,
                finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                model=self._model,
                provider=self._provider,
            )

        finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        request_id = response.id or ""
        input_tokens = response.usage.prompt_tokens if response.usage else 0
        output_tokens = response.usage.completion_tokens if response.usage else 0

        # 解析响应
        raw_content = response.choices[0].message.content if response.choices else ""
        parsed = self._parse_response(raw_content)

        result = ModelCallResult(
            success=False,
            model_calls=1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            request_id=request_id,
            model=self._model,
            provider=self._provider,
            started_at=started_at,
            finished_at=finished_at,
            response_json=parsed,
        )

        if not parsed:
            result.error = f"无法从模型响应中解析出有效 JSON。原始响应（前500字符）: {raw_content[:500]}"
            return result

        # 验证 files 字段
        files = parsed.get("files", [])
        if not files or not isinstance(files, list):
            result.error = "响应中缺少 files 字段或不是数组"
            return result

        # 验证并写入文件
        validation_error = self._validate_and_write_files(
            files, allowed_files, test_files or []
        )
        if validation_error:
            result.error = validation_error
            return result

        result.success = True
        result.files_written = [f["path"] for f in files]
        return result

    @staticmethod
    def _detect_language(allowed_files: List[str]) -> str:
        """根据文件扩展名检测项目语言"""
        exts = set()
        for f in allowed_files:
            ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
            if ext:
                exts.add(ext)
        if exts & {".ts", ".tsx"}:
            return "typescript"
        if exts & {".js", ".jsx"}:
            return "javascript"
        if exts & {".py"}:
            return "python"
        return "generic"

    def _build_system_prompt(
        self,
        allowed_files: List[str],
        test_files: Optional[List[str]] = None,
        error_feedback: Optional[str] = None,
    ) -> str:
        """构建系统提示词（V1.8B: 支持多语言）"""
        file_list = "\n".join(f"  - {f}" for f in allowed_files)
        test_file_list = "\n".join(f"  - {f}" for f in (test_files or []))
        lang = self._detect_language(allowed_files)

        if lang == "typescript":
            lang_guide = """你是一个专业的 TypeScript/Electron 代码生成助手。请根据任务描述生成代码。

## 技术栈
- TypeScript 5.3 (strict mode)
- Electron 28 (contextIsolation: true, nodeIntegration: false)
- React 18 + Vite 5
- Sharp 0.33 (主进程 only)
- better-sqlite3 11
- Vitest 1 (测试框架)

## 严格规则
1. 只能修改上述"允许修改的文件"列表中的文件
2. 新文件创建在允许列表中声明的路径
3. 代码必须通过 TypeScript strict 编译
4. Electron 安全：renderer 不直接访问 fs/child_process/Sharp
5. preload 使用 contextBridge 暴露白名单 API
6. 主进程校验所有输入/输出路径
7. 不要在响应中包含 JSON 之外的任何内容
8. content 字段中必须是转义后的完整文件内容
9. JSON 字符串内使用 \\n 表示换行，使用 \\" 表示双引号"""
            test_cmd_hint = '"test_command": "npm test"'
        elif lang == "python":
            lang_guide = """你是一个专业的 Python 代码生成助手。请根据任务描述生成代码。

## 严格规则
1. 只能修改上述"允许修改的文件"列表中的文件
2. 测试文件必须以 test_ 开头，且包含至少 4 个 test_ 函数
3. 代码必须是完整的 Python 文件，可以直接运行
4. 代码必须符合 Python 语法，可以被编译
5. 不要在响应中包含 JSON 之外的任何内容
6. 不要使用外部依赖，只用标准库
7. content 字段中必须是转义后的完整文件内容"""
            test_cmd_hint = '"test_command": "python -m pytest <测试文件> -v"'
        else:
            lang_guide = """你是一个专业的代码生成助手。请根据任务描述生成代码。

## 严格规则
1. 只能修改上述"允许修改的文件"列表中的文件
2. 代码必须完整、可直接运行
3. 不要在响应中包含 JSON 之外的任何内容
4. content 字段中必须是转义后的完整文件内容"""
            test_cmd_hint = '"test_command": "<测试命令>"'

        prompt = f"""{lang_guide}

## 输出格式
你必须返回一个严格的 JSON 对象，格式如下：
```json
{{
  "files": [
    {{
      "path": "<文件名>",
      "content": "<完整文件内容>"
    }}
  ],
  {test_cmd_hint}
}}
```

## 允许修改的文件
{file_list}

## 测试文件要求
{test_file_list}
"""

        if error_feedback:
            prompt += f"\n\n## 上次错误反馈\n{error_feedback}\n请修复上述问题。\n"

        return prompt

    def _parse_response(self, raw: str) -> Optional[Dict]:
        """解析模型响应中的 JSON"""
        if not raw:
            return None

        # 尝试直接解析
        try:
            return json.loads(raw.strip())
        except json.JSONDecodeError:
            pass

        # 尝试从 ```json ... ``` 代码块中提取
        json_match = re.search(r'```json\s*(.*?)\s*```', raw, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # 尝试从 ``` ... ``` 代码块中提取
        code_match = re.search(r'```\s*(.*?)\s*```', raw, re.DOTALL)
        if code_match:
            try:
                return json.loads(code_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # 尝试提取 { ... } 块
        bracket_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if bracket_match:
            try:
                return json.loads(bracket_match.group(0))
            except json.JSONDecodeError:
                pass

        return None

    def _validate_and_write_files(
        self,
        files: List[Dict],
        allowed_files: List[str],
        test_files: List[str],
    ) -> Optional[str]:
        """验证并写入文件。返回 None 表示成功，否则返回错误信息。

        V1.8B: 根据项目语言区分验证规则
        - Python: 测试文件需 test_ 前缀 + 至少 4 个 test_ 函数
        - TypeScript/JS: 只做路径白名单和内容非空验证
        """
        allowed_set = set(allowed_files)
        lang = self._detect_language(allowed_files)
        is_python = lang == "python"
        # test_set 只保留明确的测试文件（以 test_ 开头），仅 Python 项目严格验证
        test_set = set(f for f in test_files if f.startswith("test_"))
        test_file_count = 0
        test_functions_count = 0

        for file_entry in files:
            if not isinstance(file_entry, dict):
                return f"files 元素必须是对象，得到: {type(file_entry)}"

            file_path = file_entry.get("path", "")
            content = file_entry.get("content", "")

            # 1. 文件路径必须在白名单内
            if file_path not in allowed_set:
                return (
                    f"文件 '{file_path}' 不在允许列表中。"
                    f"允许的文件: {', '.join(sorted(allowed_set))}"
                )

            # 2. 判断是否为测试文件（仅 Python 项目严格验证）
            is_test_file = file_path.startswith("test_") and file_path in test_set
            if is_test_file and is_python:
                test_file_count += 1
                func_count = len(re.findall(r'^def\s+(test_\w+)', content, re.MULTILINE))
                test_functions_count += func_count

            # 3. 内容非空检查（所有语言）
            if not content or not content.strip():
                return f"文件 '{file_path}' 内容为空"

            # 4. 按文件类型进行语法验证
            validation_error = self._validate_file_content(content, file_path)
            if validation_error:
                return validation_error

            # 5. 写入文件
            target = self.sandbox_path / file_path
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding='utf-8')
            except Exception as e:
                return f"写入文件 '{file_path}' 失败: {e}"

        # 6. Python 项目：验证测试文件数量和函数数量
        if is_python:
            if test_set and test_file_count == 0:
                return "没有生成任何测试文件"
            if test_set and test_functions_count < 4:
                return (
                    f"测试文件中只有 {test_functions_count} 个 test_ 函数，"
                    f"需要至少 4 个"
                )

        return None  # 成功

    @staticmethod
    def _validate_file_content(content: str, file_path: str) -> Optional[str]:
        """按文件类型进行语法验证。

        规则：
        - .py  → Python compile/ast.parse
        - .json → json.loads
        - .ts/.tsx/.js/.jsx → 不执行 Python 语法检查，只验证编码和基本有效性
        - .txt/.md → 只验证编码和文件大小
        - .html/.css → 基础文本检查
        - 其他类型 → 不执行 Python 语法检查

        Returns:
            None 表示验证通过，否则返回错误信息字符串。
        """
        import ast

        ext = Path(file_path).suffix.lower() if file_path else ""

        # 空内容警告但不拒绝（有些文件初始可能为空）
        if not content or not content.strip():
            return None

        # ── .py 文件：Python 语法检查 ──
        if ext == ".py":
            try:
                ast.parse(content, file_path)
            except SyntaxError as e:
                return f"文件 '{file_path}' Python 语法错误: {e}"
            return None

        # ── .json 文件：JSON 解析检查 ──
        if ext == ".json":
            try:
                json.loads(content)
            except json.JSONDecodeError as e:
                return f"文件 '{file_path}' JSON 解析错误: {e}"
            return None

        # ── .ts / .tsx / .js / .jsx → 跳过 Python 编译，交给 TypeScript/构建工具 ──
        if ext in (".ts", ".tsx", ".js", ".jsx"):
            # 仅检查是否为纯文本（编码检查由 write_text 保证）
            try:
                content.encode("utf-8")
            except UnicodeEncodeError as e:
                return f"文件 '{file_path}' 编码错误: {e}"
            return None

        # ── .txt / .md → 仅检查编码和基本合理性 ──
        if ext in (".txt", ".md"):
            try:
                content.encode("utf-8")
            except UnicodeEncodeError as e:
                return f"文件 '{file_path}' 编码错误: {e}"
            # 检查文件大小合理性（不超过 10MB）
            if len(content.encode("utf-8")) > 10 * 1024 * 1024:
                return f"文件 '{file_path}' 过大（超过 10MB）"
            return None

        # ── .html / .css → 基础文本检查 ──
        if ext in (".html", ".css"):
            try:
                content.encode("utf-8")
            except UnicodeEncodeError as e:
                return f"文件 '{file_path}' 编码错误: {e}"
            return None

        # ── 其他类型 → 不做 Python 语法检查 ──
        return None

    @staticmethod
    def mask_sensitive(text: str, api_key: str = "") -> str:
        """脱敏处理，移除 API Key"""
        if api_key and api_key in text:
            text = text.replace(api_key, "sk-****")
        return text
