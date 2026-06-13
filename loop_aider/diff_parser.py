"""
loop_aider/diff_parser.py -- Aider Output Parser (Triple-Strategy)

从 Aider subprocess 的 stdout/stderr 中提取结构化变更信息。
采用三策略回退机制，逐一尝试直到成功解析。

策略1 (优先): unified diff 解析 -- `---`/`+++`/`@@` 模式。
策略2 (备选): git diff 格式解析 -- `diff --git a/... b/...` 模式。
策略3 (兜底): aider 输出格式解析 -- 英文/中文混合标记全文推测。

Key Features:
    - Extract affected_files, added_lines, removed_lines, file_diffs
    - Detect new files (--- /dev/null), deleted files (+++ /dev/null)
    - Detect binary file changes
    - Extract warnings and errors from Aider stderr/stdout
    - Timeout detection

Usage:
    from loop_aider.diff_parser import DiffParser, parse_diff

    parser = DiffParser()
    result = parser.parse(stdout, stderr, duration_ms=5000, exit_code=0,
                          phase="part_2_2", cycle=3)
    # 或使用便捷函数:
    result = parse_diff(stdout, stderr, duration_ms=5000, exit_code=0,
                        phase="part_2_2", cycle=3)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Data Types
# ---------------------------------------------------------------------------

@dataclass
class DiffChunk:
    """单个文件的 diff 片段."""
    file_path: str
    diff_lines: list[str] = field(default_factory=list)
    is_new_file: bool = False
    is_deleted_file: bool = False
    is_binary: bool = False
    added_lines: int = 0
    removed_lines: int = 0

    @property
    def diff_text(self) -> str:
        """返回完整 diff 文本."""
        return '\n'.join(self.diff_lines)


# 复用 AiderManager 中定义的 AiderResult，避免循环导入
# 这里定义一个本地版本的 AiderResult，供 parse_diff() 返回
@dataclass
class AiderResult:
    """
    一次 Aider 调用的完整结构化结果。

    字段与 AiderManager.AiderResult 保持一致，以支持互操作。
    """
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    duration_ms: int = 0
    affected_files: list[str] = field(default_factory=list)
    added_lines: int = 0
    removed_lines: int = 0
    file_diffs: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    models_used: list[str] = field(default_factory=list)
    tokens_used: int = 0
    timed_out: bool = False
    phase: str = ""
    cycle: int = 0
    # 额外字段，向下兼容 AiderManager 的扩展字段
    is_binary: bool = False

    @property
    def success(self) -> bool:
        """Aider 是否成功退出（exit_code == 0 且未超时）."""
        return self.exit_code == 0 and not self.timed_out

    @property
    def total_lines_changed(self) -> int:
        """新增 + 删除行总数."""
        return self.added_lines + self.removed_lines


# ---------------------------------------------------------------------------
# DiffParser -- Triple-Strategy Output Parsing
# ---------------------------------------------------------------------------

class DiffParser:
    """
    Aider 输出解析器，三策略回退。

    策略1 (优先): unified diff 解析 -- `---`/`+++`/`@@` 模式。
    策略2 (备选): git diff 格式解析 -- `diff --git a/... b/...` 模式。
    策略3 (兜底): aider 输出格式解析 -- 英文/中文混合标记全文推测。

    若所有策略均失败，标记 <unknown> 文件，但保留原始 stdout。
    """

    # 常见源代码文件扩展名，用于策略3路径推测
    _KNOWN_EXTENSIONS = (
        '.py', '.js', '.ts', '.jsx', '.tsx', '.go', '.rs', '.java',
        '.c', '.cpp', '.h', '.hpp', '.cs', '.rb', '.php', '.swift',
        '.kt', '.scala', '.sh', '.bash', '.zsh', '.ps1', '.bat',
        '.html', '.css', '.scss', '.less', '.json', '.yaml', '.yml',
        '.toml', '.xml', '.md', '.rst', '.txt', '.sql', '.graphql',
        '.proto', '.vue', '.svelte', '.astro',
    )

    def __init__(self):
        """初始化 DiffParser."""
        pass

    # ------------------------------------------------------------------
    # Main Entry Point
    # ------------------------------------------------------------------

    def parse(
        self,
        stdout: str,
        stderr: str = "",
        duration_ms: int = 0,
        exit_code: int = -1,
        phase: str = "",
        cycle: int = 0,
    ) -> AiderResult:
        """
        解析 Aider 子进程输出，返回结构化 AiderResult。

        Args:
            stdout:      Aider 标准输出文本。
            stderr:      Aider 标准错误输出文本。
            duration_ms: 子进程执行时长（毫秒）。
            exit_code:   子进程退出码。
            phase:       当前 phase 名。
            cycle:       当前 cycle 号。

        Returns:
            完整的 AiderResult，包含 affected_files、added_lines、
            removed_lines、file_diffs 等结构化信息。
        """
        result = AiderResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_ms=duration_ms,
            phase=phase,
            cycle=cycle,
        )

        # 检测超时
        result.timed_out = self._detect_timeout(stdout, stderr, exit_code)

        # 尝试所有策略解析 diff
        chunks = self._parse_diff(stdout)
        if chunks:
            # 聚合结果
            result.affected_files = self._extract_affected_files_from_chunks(chunks)
            result.file_diffs = self._build_file_diffs(chunks)
            result.added_lines = sum(c.added_lines for c in chunks)
            result.removed_lines = sum(c.removed_lines for c in chunks)

            # 检查是否有二进制文件
            result.is_binary = any(c.is_binary for c in chunks)

        # 提取 warnings / errors
        result.warnings = self._extract_warnings(stdout, stderr)
        result.errors = self._extract_errors(stdout, stderr, exit_code)

        # 提取模型信息
        result.models_used = self._extract_model_info(stdout)

        # 提取 token 使用量
        result.tokens_used = self._extract_token_usage(stdout)

        return result

    # ------------------------------------------------------------------
    # Diff Parsing -- Triple Strategy
    # ------------------------------------------------------------------

    def _parse_diff(self, stdout: str) -> list[DiffChunk]:
        """
        三策略回退解析 diff。

        依次尝试:
            策略1: unified diff (--- / +++ / @@)
            策略2: git diff (diff --git a/... b/...)
            策略3: 文本路径推测

        Returns:
            DiffChunk 列表，无匹配则返回空列表。
        """
        if not stdout or not stdout.strip():
            return []

        # 策略1: unified diff 解析 (--- / +++ / @@)
        chunks = self._parse_unified_diff(stdout)
        if chunks:
            return chunks

        # 策略2: git diff 格式解析 (diff --git a/... b/...)
        chunks = self._parse_git_diff(stdout)
        if chunks:
            return chunks

        # 策略3: 全文路径推测
        chunks = self._parse_by_path_guessing(stdout)
        return chunks

    # ------------------------------------------------------------------
    # Strategy 1: Unified Diff Parsing
    # ------------------------------------------------------------------

    def _parse_unified_diff(self, stdout: str) -> list[DiffChunk]:
        """
        策略1: 解析 unified diff 格式。

        通过 ---, +++, @@ 标记识别文件边界，分割为独立 chunk。

        Args:
            stdout: Aider 输出文本。

        Returns:
            DiffChunk 列表，无匹配则返回空列表。
        """
        lines = stdout.split('\n')
        chunks: list[DiffChunk] = []
        current_chunk: Optional[DiffChunk] = None
        in_header = False

        for line in lines:
            # 检测文件头: --- a/path 或 --- /dev/null
            match_minus = re.match(r'^---\s+([^\s]+)$', line)
            if match_minus:
                file_ref = match_minus.group(1)
                # 跳过 /dev/null (新文件标记)
                if file_ref == '/dev/null':
                    in_header = True
                    if current_chunk is None:
                        current_chunk = DiffChunk(file_path='<new_file>')
                        current_chunk.is_new_file = True
                    continue
                # 提取路径 (去除 a/ 或 b/ 前缀)
                path = self._normalize_path(file_ref)
                if current_chunk is not None and current_chunk.diff_lines:
                    self._finalize_chunk(current_chunk)
                    chunks.append(current_chunk)
                current_chunk = DiffChunk(file_path=path)
                current_chunk.diff_lines.append(line)
                continue

            # 检测 +++ b/path 或 +++ /dev/null
            match_plus = re.match(r'^\+\+\+\s+([^\s]+)$', line)
            if match_plus:
                file_ref = match_plus.group(1)
                if file_ref == '/dev/null':
                    # 文件被删除
                    if current_chunk is not None:
                        current_chunk.is_deleted_file = True
                    else:
                        current_chunk = DiffChunk(
                            file_path='<deleted_file>',
                            is_deleted_file=True,
                        )
                else:
                    path = self._normalize_path(file_ref)
                    if current_chunk is not None:
                        current_chunk.file_path = path
                    else:
                        current_chunk = DiffChunk(file_path=path)
                if current_chunk is not None:
                    current_chunk.diff_lines.append(line)
                continue

            # 检测 @@ hunk header
            if re.match(r'^@@\s+-\d+', line):
                if current_chunk is not None:
                    current_chunk.diff_lines.append(line)
                continue

            # 计数新增/删除行
            if current_chunk is not None:
                current_chunk.diff_lines.append(line)
                if line.startswith('+') and not line.startswith('+++'):
                    current_chunk.added_lines += 1
                elif line.startswith('-') and not line.startswith('---'):
                    current_chunk.removed_lines += 1

        # 保存最后一个 chunk
        if current_chunk is not None:
            self._finalize_chunk(current_chunk)
            chunks.append(current_chunk)

        # 验证 chunks 是否有效
        valid_chunks = [
            c for c in chunks
            if c.file_path not in ('<new_file>', '<deleted_file>', '<unknown>')
               or c.is_new_file or c.is_deleted_file
        ]
        if not valid_chunks:
            return []

        return valid_chunks

    # ------------------------------------------------------------------
    # Strategy 2: Git Diff Format Parsing
    # ------------------------------------------------------------------

    def _parse_git_diff(self, stdout: str) -> list[DiffChunk]:
        """
        策略2: 解析 git diff 格式。

        通过 `diff --git a/path b/path` 标记识别文件边界。

        Args:
            stdout: Aider 输出文本。

        Returns:
            DiffChunk 列表，无匹配则返回空列表。
        """
        lines = stdout.split('\n')
        chunks: list[DiffChunk] = []
        current_chunk: Optional[DiffChunk] = None

        for line in lines:
            # 检测 diff --git a/path b/path
            match = re.match(
                r'^diff\s+--git\s+a/(.+?)\s+b/(.+?)$', line
            )
            if match:
                # 保存上一个 chunk
                if current_chunk is not None and current_chunk.diff_lines:
                    self._finalize_chunk(current_chunk)
                    chunks.append(current_chunk)
                path = match.group(1)
                current_chunk = DiffChunk(file_path=path)
                current_chunk.diff_lines.append(line)
                continue

            # 检测二进制文件标记
            if re.match(r'^Binary\s+files\s+', line):
                if current_chunk is not None:
                    current_chunk.is_binary = True
                    current_chunk.diff_lines.append(line)
                continue

            if current_chunk is not None:
                current_chunk.diff_lines.append(line)
                # 计数
                if line.startswith('+') and not line.startswith('+++'):
                    current_chunk.added_lines += 1
                elif line.startswith('-') and not line.startswith('---'):
                    current_chunk.removed_lines += 1

        # 保存最后一个 chunk
        if current_chunk is not None and current_chunk.diff_lines:
            self._finalize_chunk(current_chunk)
            chunks.append(current_chunk)

        return chunks

    # ------------------------------------------------------------------
    # Strategy 3: Path Guessing (Fallback)
    # ------------------------------------------------------------------

    def _parse_by_path_guessing(self, stdout: str) -> list[DiffChunk]:
        """
        策略3: 全文路径推测。

        在输出文本中搜索已知扩展名的文件路径。

        Args:
            stdout: Aider 输出文本。

        Returns:
            包含一个 chunk 的列表，或空列表。
        """
        # 构建扩展名正则
        ext_pattern = '|'.join(
            re.escape(ext) for ext in self._KNOWN_EXTENSIONS
        )
        # 匹配类似路径的模式
        path_pattern = re.compile(
            r'\b([\w.\-/]+(?:' + ext_pattern + r'))\b',
            re.IGNORECASE,
        )

        matches = path_pattern.findall(stdout)
        if not matches:
            return []

        # 去重并过滤合理的路径
        seen: set[str] = set()
        paths: list[str] = []
        for m in matches:
            path = m if isinstance(m, str) else m[0]
            path = path.strip('/')
            if path not in seen and self._is_likely_file_path(path):
                seen.add(path)
                paths.append(path)

        if not paths:
            return []

        # 构建单个 chunk（策略3无法精确分割多文件）
        chunk = DiffChunk(file_path=paths[0] if len(paths) == 1 else '<multiple>')
        chunk.diff_lines = stdout.split('\n')

        # 简单计数
        for line in chunk.diff_lines:
            if line.startswith('+') and not line.startswith('+++'):
                chunk.added_lines += 1
            elif line.startswith('-') and not line.startswith('---'):
                chunk.removed_lines += 1

        # 如果有多个路径，在注释中记录
        if len(paths) > 1:
            chunk.diff_lines.insert(
                0, f'# Multiple files detected: {", ".join(paths)}'
            )

        return [chunk]

    # ------------------------------------------------------------------
    # Extraction Helpers
    # ------------------------------------------------------------------

    def _extract_affected_files_from_chunks(
        self, chunks: list[DiffChunk]
    ) -> list[str]:
        """从 DiffChunk 列表中提取去重后的受影响文件路径."""
        files: list[str] = []
        seen: set[str] = set()
        for chunk in chunks:
            path = chunk.file_path
            if path and path not in ('<new_file>', '<deleted_file>', '<unknown>', '<multiple>'):
                normalized = self._normalize_path(path)
                if normalized not in seen:
                    seen.add(normalized)
                    files.append(normalized)
        return sorted(files)

    def _build_file_diffs(
        self, chunks: list[DiffChunk]
    ) -> dict[str, str]:
        """构建 {file_path: diff_text} 字典."""
        result: dict[str, str] = {}
        for chunk in chunks:
            path = chunk.file_path
            if path and path not in ('<new_file>', '<deleted_file>', '<unknown>', '<multiple>'):
                normalized = self._normalize_path(path)
                result[normalized] = chunk.diff_text
        return result

    def _detect_timeout(
        self, stdout: str, stderr: str, exit_code: int
    ) -> bool:
        """
        检测是否发生超时。

        条件: exit_code 为负值，或输出包含超时关键词。

        Args:
            stdout:   Aider 标准输出。
            stderr:   Aider 标准错误。
            exit_code: 退出码。

        Returns:
            True 表示发生了超时。
        """
        if exit_code < 0:
            return True
        combined = (stdout + stderr).lower()
        timeout_keywords = [
            'timed out', 'timeout', 'killed', 'terminated',
            '超过时间限制', '超时', '连接超时',
        ]
        return any(kw in combined for kw in timeout_keywords)

    def _extract_warnings(self, stdout: str, stderr: str) -> list[str]:
        """
        从 stdout 和 stderr 中提取警告信息。

        匹配以下模式:
            - 包含 "warning" 或 "warn" 关键词的行
            - 包含 "deprecated" / "deprecation" 的行
            - 中文警告关键词

        Args:
            stdout: Aider 标准输出。
            stderr: Aider 标准错误。

        Returns:
            去重后的警告列表。
        """
        warnings: list[str] = []
        seen: set[str] = set()

        # 从 stderr 提取
        for line in stderr.split('\n'):
            line = line.strip()
            if not line:
                continue
            if self._is_warning_line(line):
                key = line[:200]
                if key not in seen:
                    seen.add(key)
                    warnings.append(line)

        # 从 stdout 提取（含警告关键词的行）
        for line in stdout.split('\n'):
            line = line.strip()
            if not line:
                continue
            if self._is_warning_line(line):
                key = line[:200]
                if key not in seen:
                    seen.add(key)
                    warnings.append(line)

        return warnings

    def _extract_errors(
        self, stdout: str, stderr: str, exit_code: int
    ) -> list[str]:
        """
        从输出中提取错误信息。

        匹配模式:
            - 包含 "error" / "fail" / "traceback" 关键词的行
            - 非零退出码时添加标准错误信息
            - 中文错误关键词

        Args:
            stdout:    Aider 标准输出。
            stderr:    Aider 标准错误。
            exit_code: 退出码。

        Returns:
            去重后的错误列表。
        """
        errors: list[str] = []
        seen: set[str] = set()

        # 从 stderr 提取
        for line in stderr.split('\n'):
            line = line.strip()
            if not line:
                continue
            if self._is_error_line(line):
                key = line[:200]
                if key not in seen:
                    seen.add(key)
                    errors.append(line)

        # 从 stdout 提取
        for line in stdout.split('\n'):
            line = line.strip()
            if not line:
                continue
            if self._is_error_line(line):
                key = line[:200]
                if key not in seen:
                    seen.add(key)
                    errors.append(line)

        # 非零退出码
        if exit_code != 0 and exit_code != -1:
            err_msg = f"Aider exited with non-zero code: {exit_code}"
            if err_msg not in seen:
                errors.append(err_msg)

        return errors

    def _extract_model_info(self, stdout: str) -> list[str]:
        """
        从 Aider 输出中提取使用的模型名称。

        匹配模式:
            - "Using model: <name>"
            - "Model: <name>"
            - "使用模型: <name>"

        Args:
            stdout: Aider 标准输出。

        Returns:
            模型名称列表。
        """
        models: list[str] = []
        patterns = [
            # "Using model: <name>" -- Aider main model announcement
            r'(?i)using\s+model[:\s]+([a-zA-Z0-9_.-]+(?:-\d{8})?)',
            # "Model: <name>" -- standalone model declaration (with colon)
            r'(?i)\bmodel\s*:\s*([a-zA-Z0-9_.-]+(?:-\d{8})?)',
            # 中文: "使用模型: <name>"
            r'使用模型[:\s]*([a-zA-Z0-9_.-]+)',
        ]
        for pat in patterns:
            for match in re.finditer(pat, stdout):
                model = match.group(1)
                if model not in models:
                    models.append(model)
        return models

    def _extract_token_usage(self, stdout: str) -> int:
        """
        从输出中提取 token 使用量。

        匹配模式:
            - "1234 tokens"
            - "Tokens: 1,234"
            - 中文 "令牌: 1234"

        Args:
            stdout: Aider 标准输出。

        Returns:
            Token 数量，未找到返回 0。
        """
        patterns = [
            r'(?i)(\d[\d,]*)\s*(?:tokens|tok)',
            r'(?i)(?:tokens|tok)[:\s]+(\d[\d,]*)',
            r'令牌[:\s]*(\d[\d,]*)',
        ]
        for pat in patterns:
            match = re.search(pat, stdout)
            if match:
                try:
                    return int(match.group(1).replace(',', ''))
                except ValueError:
                    continue
        return 0

    # ------------------------------------------------------------------
    # Path Utilities
    # ------------------------------------------------------------------

    def _normalize_path(self, path: str) -> str:
        """
        标准化文件路径。

        移除 a/ 或 b/ 前缀，统一使用正斜杠。

        Args:
            path: 原始文件路径。

        Returns:
            标准化后的路径。
        """
        # 移除 git diff 路径前缀
        path = re.sub(r'^[ab]/', '', path)
        # 统一斜杠
        path = path.replace('\\', '/')
        # 移除前后的引号和空白
        path = path.strip('\'"').strip()
        return path

    def _is_likely_file_path(self, path: str) -> bool:
        """
        判断文本是否像文件路径。

        Args:
            path: 候选路径文本。

        Returns:
            True 表示可能是文件路径。
        """
        if not path:
            return False
        # 必须包含已知扩展名
        if not any(path.endswith(ext) for ext in self._KNOWN_EXTENSIONS):
            return False
        # 不能全是数字或特殊字符
        if re.match(r'^[\d\W_]+$', path):
            return False
        return True

    # ------------------------------------------------------------------
    # Line Classification
    # ------------------------------------------------------------------

    @staticmethod
    def _is_warning_line(line: str) -> bool:
        """判断一行文本是否为警告."""
        lower = line.lower()
        warn_keywords = [
            'warning', 'warn', 'deprecated', 'deprecation',
            '注意', '警告', '提醒',
        ]
        return any(kw in lower for kw in warn_keywords)

    @staticmethod
    def _is_error_line(line: str) -> bool:
        """判断一行文本是否为错误."""
        lower = line.lower()
        error_keywords = [
            'error', 'fail', 'traceback', 'exception',
            '错误', '失败', '异常', '堆栈跟踪',
        ]
        return any(kw in lower for kw in error_keywords)

    @staticmethod
    def _finalize_chunk(chunk: DiffChunk):
        """收尾处理一个 DiffChunk（过滤仅 header 行的空 diff）."""
        # 如果只有 header 行没有实际内容，标记为空
        content_lines = [
            l for l in chunk.diff_lines
            if not re.match(r'^(diff\s+--git|---\s|\+\+\+\s|@@\s|index\s)', l)
        ]
        if not content_lines:
            chunk.added_lines = 0
            chunk.removed_lines = 0


# ---------------------------------------------------------------------------
# Module-level Convenience Function
# ---------------------------------------------------------------------------

def parse_diff(
    stdout: str,
    stderr: str = "",
    duration_ms: int = 0,
    exit_code: int = -1,
    phase: str = "",
    cycle: int = 0,
) -> AiderResult:
    """
    便捷函数: 一行调用完成 diff 解析。

    等价于:
        parser = DiffParser()
        result = parser.parse(stdout, stderr, duration_ms, exit_code,
                              phase, cycle)

    Args:
        stdout:      Aider 标准输出文本。
        stderr:      Aider 标准错误输出文本。
        duration_ms: 子进程执行时长（毫秒）。
        exit_code:   子进程退出码。
        phase:       当前 phase 名。
        cycle:       当前 cycle 号。

    Returns:
        完整的 AiderResult。
    """
    parser = DiffParser()
    return parser.parse(
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
        exit_code=exit_code,
        phase=phase,
        cycle=cycle,
    )
