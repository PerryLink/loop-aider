"""
loop_aider/git_manager.py —— Git 仓库管理模块

负责工作区状态检查、语义化提交、diff 获取和变更文件列表。
loop-aider 通过 GitManager 在每次 Aider 调用前后管理 Git 状态，
确保变更有清晰的可追溯性。

Usage:
    gm = GitManager(cwd=".")
    is_clean = gm.check_workspace_clean()
    gm.create_semantic_commit(phase="part_2_2", cycle=1, extra="完成实现")
    diff = gm.get_diff()
    files = gm.get_changed_files()
"""

from __future__ import annotations

import os
import re
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# =============================================================================
# GitManager
# =============================================================================

class GitManager:
    """
    Git 仓库管理器。

    封装常用 Git 操作，为 loop-aider 提供版本控制能力。
    所有操作使用 subprocess 调用 git CLI，确保兼容性。

    Attributes:
        cwd: 工作目录（Git 仓库根目录或子目录）。
        logger: 日志记录器。
    """

    def __init__(self, cwd: Optional[str] = None):
        """初始化 GitManager。

        Args:
            cwd: Git 工作目录。默认为当前目录。
        """
        self.cwd = Path(cwd) if cwd else Path.cwd()
        self.logger = logging.getLogger("loop_aider.git_manager")

    # ------------------------------------------------------------------
    # 基础 Git 命令执行
    # ------------------------------------------------------------------

    def _git(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        """执行 git 命令并返回 CompletedProcess。

        Args:
            *args: git 命令参数。
            check: 是否在非零退出码时抛出异常。

        Returns:
            subprocess.CompletedProcess 对象。

        Raises:
            FileNotFoundError: git 未安装。
            subprocess.CalledProcessError: check=True 且命令失败。
        """
        cmd = ["git"] + list(args)
        self.logger.debug("git %s", ' '.join(args))
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(self.cwd),
                encoding='utf-8',
                errors='replace',
                check=check,
            )
        except FileNotFoundError:
            raise FileNotFoundError(
                "Git 未安装或不在 PATH 中。请安装 Git 后重试。"
            )

    # ------------------------------------------------------------------
    # check_workspace_clean
    # ------------------------------------------------------------------

    def check_workspace_clean(self) -> bool:
        """检查工作区是否干净（无未提交变更）。

        执行 `git status --porcelain`，若输出为空则表示干净。

        Returns:
            True 表示工作区干净，无待提交的变更。
        """
        result = self._git("status", "--porcelain")
        output = result.stdout.strip()
        is_clean = len(output) == 0
        if not is_clean:
            # 记录变更文件数
            lines = [l for l in output.split('\n') if l.strip()]
            self.logger.debug("工作区不干净: %d 个变更项", len(lines))
        return is_clean

    # ------------------------------------------------------------------
    # get_changed_files
    # ------------------------------------------------------------------

    def get_changed_files(self, staged_only: bool = False) -> list[str]:
        """获取变更文件列表。

        Args:
            staged_only: True 时仅返回已暂存（staged）的文件。

        Returns:
            变更文件路径列表（相对路径）。
        """
        if staged_only:
            result = self._git("diff", "--name-only", "--cached")
        else:
            result = self._git("diff", "--name-only", "HEAD")
        files = [f.strip() for f in result.stdout.strip().split('\n') if f.strip()]
        return files

    def get_untracked_files(self) -> list[str]:
        """获取未跟踪的新文件列表。

        Returns:
            未跟踪文件路径列表。
        """
        result = self._git("ls-files", "--others", "--exclude-standard")
        files = [f.strip() for f in result.stdout.strip().split('\n') if f.strip()]
        return files

    def get_all_changed_files(self) -> list[str]:
        """获取所有变更文件（含已跟踪变更和未跟踪文件）。

        Returns:
            所有变更和未跟踪文件的合并列表。
        """
        changed = self.get_changed_files(staged_only=False)
        untracked = self.get_untracked_files()
        # 合并去重
        return sorted(set(changed + untracked))

    # ------------------------------------------------------------------
    # get_diff
    # ------------------------------------------------------------------

    def get_diff(self, staged_only: bool = False,
                 file_path: Optional[str] = None) -> str:
        """获取工作区 diff。

        执行 `git diff` 获取未暂存的变更；`git diff --cached` 获取已暂存的变更。

        Args:
            staged_only: True 时仅获取已暂存（staged）的 diff。
            file_path: 指定文件路径，None 表示所有文件。

        Returns:
            unified diff 文本字符串。
        """
        args = ["diff"]
        if staged_only:
            args.append("--cached")
        else:
            args.append("HEAD")
        if file_path:
            args.extend(["--", file_path])
        result = self._git(*args)
        return result.stdout

    def get_diff_stat(self) -> str:
        """获取变更统计摘要（类似 git diff --stat）。

        Returns:
            diff stat 文本字符串。
        """
        result = self._git("diff", "--stat", "HEAD")
        return result.stdout.strip()

    def get_file_diff(self, file_path: str) -> str:
        """获取单个文件的 diff。

        Args:
            file_path: 文件路径（相对路径）。

        Returns:
            该文件的 unified diff 文本。
        """
        result = self._git("diff", "HEAD", "--", file_path)
        return result.stdout

    # ------------------------------------------------------------------
    # create_semantic_commit
    # ------------------------------------------------------------------

    def create_semantic_commit(
        self,
        phase: str,
        cycle: int,
        extra: str = "",
        template: Optional[str] = None,
    ) -> bool:
        """按阶段生成语义化提交信息并执行 git commit。

        提交信息模板格式：
            [loop-aider] phase={phase} cycle={cycle}[: extra]

        Args:
            phase: 阶段名称（如 "part_2_2"）。
            cycle: 循环序号。
            extra: 额外描述文本，追加在 commit message 末尾。
            template: 自定义模板字符串，支持 {phase}/{cycle}/{extra} 占位符。

        Returns:
            True 表示提交成功，False 表示无内容可提交。
        """
        # 先检查是否有待提交的内容
        status_result = self._git("status", "--porcelain")
        if not status_result.stdout.strip():
            self.logger.info(
                "工作区干净，无需提交 (phase=%s cycle=%d)", phase, cycle
            )
            return False

        # 暂存所有变更
        self._git("add", "-A", check=True)

        # 构建提交信息
        if template:
            msg = template.format(phase=phase, cycle=cycle, extra=extra)
        else:
            msg = f"[loop-aider] phase={phase} cycle={cycle}"
            if extra:
                msg += f": {extra}"

        # 执行提交
        self.logger.info("git commit: %s", msg)
        commit_result = self._git("commit", "-m", msg)

        if commit_result.returncode != 0:
            self.logger.error("git commit 失败: %s", commit_result.stderr.strip())
            return False

        # 记录提交 hash
        hash_result = self._git("rev-parse", "--short", "HEAD")
        commit_hash = hash_result.stdout.strip()
        self.logger.info(
            "提交成功: %s [%s] (phase=%s cycle=%d)",
            commit_hash, msg[:60], phase, cycle
        )
        return True

    # ------------------------------------------------------------------
    # 语义化提交消息模板
    # ------------------------------------------------------------------

    @staticmethod
    def build_commit_message(
        phase: str,
        cycle: int,
        extra: str = "",
        affected_files: Optional[list[str]] = None,
    ) -> str:
        """构建语义化提交信息字符串（不执行提交）。

        适用于需要先生成消息再决定是否提交的场景。

        Args:
            phase: 阶段名称。
            cycle: 循环序号。
            extra: 额外描述。
            affected_files: 变更文件列表（用于摘要行）。

        Returns:
            格式化的提交信息字符串。
        """
        lines = [f"[loop-aider] phase={phase} cycle={cycle}"]
        if extra:
            lines[0] += f": {extra}"
        if affected_files:
            max_display = 5
            file_list = affected_files[:max_display]
            remaining = len(affected_files) - max_display
            lines.append("")
            for f in file_list:
                lines.append(f"  - {f}")
            if remaining > 0:
                lines.append(f"  ... and {remaining} more file(s)")
        return '\n'.join(lines)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def get_current_branch(self) -> str:
        """获取当前分支名。

        Returns:
            分支名，获取失败返回 "unknown"。
        """
        result = self._git("rev-parse", "--abbrev-ref", "HEAD")
        branch = result.stdout.strip()
        return branch or "unknown"

    def get_last_commit_hash(self, short: bool = True) -> str:
        """获取最后一次提交的 hash。

        Args:
            short: True 返回短 hash（7 位），False 返回完整 hash。

        Returns:
            提交 hash 字符串，无提交历史则返回空串。
        """
        args = ["rev-parse"]
        if short:
            args.append("--short")
        args.append("HEAD")
        result = self._git(*args)
        return result.stdout.strip()

    def get_commit_log(self, max_count: int = 10) -> list[dict]:
        """获取最近 N 条提交日志。

        Args:
            max_count: 返回的提交条目数上限。

        Returns:
            [{"hash": ..., "date": ..., "message": ...}, ...] 列表。
        """
        result = self._git(
            "log",
            f"-{max_count}",
            "--format=%H|%aI|%s",
        )
        entries = []
        for line in result.stdout.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            parts = line.split('|', 2)
            if len(parts) == 3:
                entries.append({
                    "hash": parts[0],
                    "date": parts[1],
                    "message": parts[2],
                })
        return entries

    def is_git_repo(self) -> bool:
        """检查当前目录是否在 Git 仓库中。

        Returns:
            True 表示在 Git 仓库中。
        """
        result = self._git("rev-parse", "--git-dir")
        return result.returncode == 0
