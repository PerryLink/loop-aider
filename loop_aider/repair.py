"""
loop_aider/repair.py —— Repair 系统（Milestone 4）

在 Router 判定需要修复（P2 问题）时，Repair 系统管理修复上下文
并协调 Aider 执行局部修复。

核心设计：
    repair_context 生命周期: null → active → consumed
        null:    无修复需求（正常运行）
        active:  检测到 P2 问题，created_at 记录时间戳，fixes 记录修复操作
        consumed: 修复完成，时间戳标记，counter 递增，转为 null

    并行 repair 支持:
        当多个 P2 问题互不依赖时，可并行发起修复（通过 issues 列表追踪）。

Usage:
    from loop_aider.repair import RepairManager, RepairContext

    mgr = RepairManager()
    ctx = mgr.create_context(state, p2_issues)
    result = mgr.run_repair(state, ctx, aider_manager)
    mgr.consume_context(state, ctx)
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Data Types
# ---------------------------------------------------------------------------

@dataclass
class RepairContext:
    """
    修复上下文——记录一次修复操作的完整生命周期。

    Attributes:
        id:             唯一修复 ID（基于时间戳）。
        status:         当前状态（null / active / consumed）。
        created_at:     创建时间（ISO 8601）。
        consumed_at:    消费时间（ISO 8601）。
        issues:         需要修复的 P2 问题描述列表。
        fix_strategies: 每个问题对应的修复策略。
        files_affected: 受影响的文件路径列表。
        attempt_count:  修复尝试次数。
        max_attempts:   最大尝试次数。
        result:         修复结果描述。
    """
    id: str = ""
    status: str = "null"  # "null" | "active" | "consumed"
    created_at: str = ""
    consumed_at: str = ""
    issues: list[str] = field(default_factory=list)
    fix_strategies: list[str] = field(default_factory=list)
    files_affected: list[str] = field(default_factory=list)
    attempt_count: int = 0
    max_attempts: int = 3
    result: str = ""

    @property
    def is_active(self) -> bool:
        """修复上下文是否处于活跃状态。"""
        return self.status == "active"

    @property
    def is_consumed(self) -> bool:
        """修复上下文是否已被消费。"""
        return self.status == "consumed"

    @property
    def is_null(self) -> bool:
        """修复上下文是否为空（未创建或无修复需求）。"""
        return self.status == "null"


# ---------------------------------------------------------------------------
# RepairManager Core
# ---------------------------------------------------------------------------

class RepairManager:
    """
    修复管理器——管理修复上下文的完整生命周期。

    生命周期状态机:
        null ──create_context()──→ active
        active ──run_repair()──→ active（可多次运行）
        active ──consume_context()──→ consumed
        consumed ──自动转换──→ null（下一轮开始时）

    并行修复支持:
        多个非冲突的 P2 问题可合并到一个 RepairContext 中并行处理。

    Attributes:
        config: 配置字典。
        logger: 日志记录器。
    """

    def __init__(self, config: Optional[dict] = None):
        """初始化 RepairManager。

        Args:
            config: 配置字典，含 repair_max_attempts 等参数。
        """
        self.config = config or {}
        self.max_attempts = self.config.get("repair_max_attempts", 3)
        self.logger = logging.getLogger("loop_aider.repair")
        self._repair_counter = 0

    # ========================================================================
    # 生命周期管理
    # ========================================================================

    def create_context(
        self,
        state: dict,
        p2_issues: list[str],
        fix_strategies: Optional[list[str]] = None,
        files_affected: Optional[list[str]] = None,
    ) -> RepairContext:
        """
        创建一个新的修复上下文（null → active）。

        从 P2 问题列表生成修复计划和策略。

        Args:
            state:          完整的 state.json 字典。
            p2_issues:      P2 问题描述列表。
            fix_strategies: 对应的修复策略列表（可选）。
            files_affected: 受影响的文件路径列表（可选）。

        Returns:
            新创建的活跃 RepairContext。
        """
        self._repair_counter += 1
        now = datetime.now(timezone.utc).isoformat()

        # 生成修复策略（如未提供）
        if fix_strategies is None:
            fix_strategies = [
                self._generate_fix_strategy(issue)
                for issue in p2_issues
            ]

        ctx = RepairContext(
            id=f"repair-{self._repair_counter:04d}-{int(time.time())}",
            status="active",
            created_at=now,
            consumed_at="",
            issues=list(p2_issues),
            fix_strategies=list(fix_strategies),
            files_affected=list(files_affected or []),
            attempt_count=0,
            max_attempts=self.max_attempts,
            result="",
        )

        # 写入 state
        progress = state.setdefault("progress", {})
        progress["repair_context"] = ctx.__dict__

        self.logger.info(
            "创建修复上下文 %s: %d 个 P2 问题, %d 个文件",
            ctx.id, len(ctx.issues), len(ctx.files_affected)
        )
        return ctx

    def consume_context(self, state: dict, ctx: RepairContext,
                        success: bool = True) -> RepairContext:
        """
        消费修复上下文（active → consumed）。

        修复完成后调用，记录消费时间并更新状态。

        Args:
            state:   完整的 state.json 字典（原地修改）。
            ctx:     当前的修复上下文。
            success: 修复是否成功。

        Returns:
            已消费的 RepairContext。
        """
        now = datetime.now(timezone.utc).isoformat()
        ctx.status = "consumed"
        ctx.consumed_at = now
        ctx.result = "success" if success else f"failed after {ctx.attempt_count} attempts"

        # 更新 state
        progress = state.setdefault("progress", {})
        progress["repair_context"] = ctx.__dict__

        self.logger.info(
            "消费修复上下文 %s: %s (attempts=%d/%d)",
            ctx.id, "SUCCESS" if success else "FAILED",
            ctx.attempt_count, ctx.max_attempts
        )

        # 修复完成后清空 repair_context（为下一轮准备）
        self.reset_context(state)

        return ctx

    def reset_context(self, state: dict):
        """重置修复上下文为 null。

        在每次成功修复或新周期开始时调用。

        Args:
            state: 完整的 state.json 字典（原地修改）。
        """
        progress = state.setdefault("progress", {})
        progress["repair_context"] = None
        self.logger.debug("repair_context 重置为 null")

    def is_repair_active(self, state: dict) -> bool:
        """检查是否有活跃的修复上下文。

        Args:
            state: 完整的 state.json 字典。

        Returns:
            True 表示存在活跃修复上下文。
        """
        progress = state.get("progress", {})
        ctx_data = progress.get("repair_context", None)
        if ctx_data is None:
            return False
        if isinstance(ctx_data, dict):
            return ctx_data.get("status") == "active"
        return False

    def get_context(self, state: dict) -> Optional[RepairContext]:
        """从 state 中恢复当前修复上下文。

        Args:
            state: 完整的 state.json 字典。

        Returns:
            RepairContext 或 None（如果 repair_context 为 null）。
        """
        progress = state.get("progress", {})
        ctx_data = progress.get("repair_context", None)
        if ctx_data is None:
            return None
        if isinstance(ctx_data, dict):
            return RepairContext(**ctx_data)
        return None

    # ========================================================================
    # 修复执行
    # ========================================================================

    def run_repair(
        self,
        state: dict,
        ctx: RepairContext,
        aider_manager,
    ) -> bool:
        """
        执行修复：通过 Aider 运行修复 prompt。

        生成修复 prompt（包含问题描述和修复策略），
        通过 AiderManager 发起单次修复调用，更新 attempt_count。

        Args:
            state:          完整的 state.json 字典。
            ctx:            当前的修复上下文。
            aider_manager:  AiderManager 实例。

        Returns:
            True 表示修复成功（Aider 正常退出且无新的 P0/P1 问题）。
        """
        if ctx.attempt_count >= ctx.max_attempts:
            self.logger.error(
                "修复尝试次数已达上限 (%d/%d)", ctx.attempt_count, ctx.max_attempts
            )
            return False

        ctx.attempt_count += 1

        # 构建修复 prompt
        prompt = self._build_repair_prompt(ctx)

        try:
            result = aider_manager.run_phase(
                phase=state.get("progress", {}).get("phase", "part_2_2"),
                template_vars={
                    "goal": state.get("config", {}).get("user_request", ""),
                    "repair_mode": True,
                    "repair_prompt": prompt,
                },
                files=ctx.files_affected if ctx.files_affected else None,
            )

            # 检查修复结果
            if result.success:
                # 检查是否引入新的 P0/P1 问题
                audit_issues = getattr(result, "audit_issues", [])
                new_p0_p1 = [
                    i for i in audit_issues
                    if getattr(i, "severity", "") in ("P0", "P1")
                ]
                if new_p0_p1:
                    self.logger.warning(
                        "修复引入了 %d 个新的 P0/P1 问题", len(new_p0_p1)
                    )
                    return False

                self.logger.info(
                    "修复成功 (attempt %d/%d)", ctx.attempt_count, ctx.max_attempts
                )
                return True
            else:
                self.logger.warning(
                    "修复失败 (attempt %d/%d): exit_code=%d",
                    ctx.attempt_count, ctx.max_attempts, result.exit_code
                )
                return False

        except Exception as exc:
            self.logger.error("修复异常 (attempt %d): %s", ctx.attempt_count, exc)
            return False

    # ========================================================================
    # 并行修复支持
    # ========================================================================

    def can_parallelize(self, issues: list[str]) -> bool:
        """
        判断多个 P2 问题是否可以并行修复。

        条件:
            - 每个问题涉及的源文件互不重叠
            - 每个问题的修复策略简单独立（非架构级）

        Args:
            issues: P2 问题描述列表。

        Returns:
            True 表示可以并行修复。
        """
        # 默认: 少于 2 个问题时无需并行
        if len(issues) < 2:
            return False
        # 简单判定: 问题数量 <= 3 时可以尝试并行
        can_parallel = len(issues) <= 3
        self.logger.debug(
            "并行修复判定: %d 个问题, can_parallel=%s", len(issues), can_parallel
        )
        return can_parallel

    def split_issues_into_batches(
        self, issues: list[str], max_per_batch: int = 2
    ) -> list[list[str]]:
        """
        将问题列表拆分为可并行的批次。

        Args:
            issues:        P2 问题描述列表。
            max_per_batch: 每批最大问题数。

        Returns:
            分批后的问题列表。
        """
        batches: list[list[str]] = []
        for i in range(0, len(issues), max_per_batch):
            batches.append(issues[i:i + max_per_batch])
        self.logger.debug("拆分 %d 个问题为 %d 个批次", len(issues), len(batches))
        return batches

    # ========================================================================
    # 内部辅助方法
    # ========================================================================

    def _generate_fix_strategy(self, issue: str) -> str:
        """根据问题描述生成修复策略。

        Args:
            issue: P2 问题描述。

        Returns:
            修复策略字符串。
        """
        issue_lower = issue.lower()

        if "syntax" in issue_lower or "语法" in issue_lower:
            return "检查并修正语法错误"
        elif "import" in issue_lower or "导入" in issue_lower:
            return "检查导入路径并修正缺失的依赖"
        elif "stop_signal" in issue_lower or "信号" in issue_lower:
            return "在输出末尾添加正确的 stop_signal 标记"
        elif "test" in issue_lower or "测试" in issue_lower:
            return "运行测试并修正失败的测试用例"
        elif "style" in issue_lower or "风格" in issue_lower or "lint" in issue_lower:
            return "运行代码格式化工具并修正风格问题"
        elif "file" in issue_lower or "文件" in issue_lower:
            return "检查文件变更是否符合预期，回退非预期变更"
        else:
            return f"审查并修复以下问题: {issue}"

    def _build_repair_prompt(self, ctx: RepairContext) -> str:
        """构建修复 prompt。

        Args:
            ctx: 当前的修复上下文。

        Returns:
            修复 prompt 字符串。
        """
        lines = [
            "## Repair Task (P2 Issue Fix)",
            "",
            f"Attempt {ctx.attempt_count}/{ctx.max_attempts}",
            "",
            "### Issues to Fix:",
        ]
        for i, issue in enumerate(ctx.issues, 1):
            strategy = ctx.fix_strategies[i - 1] if i <= len(ctx.fix_strategies) else ""
            lines.append(f"{i}. {issue}")
            if strategy:
                lines.append(f"   Strategy: {strategy}")
            lines.append("")

        if ctx.files_affected:
            lines.append("### Affected Files:")
            for f in ctx.files_affected:
                lines.append(f"- {f}")
            lines.append("")

        lines.extend([
            "### Instructions:",
            "1. Fix ONLY the issues listed above.",
            "2. Do NOT introduce new features or refactor unrelated code.",
            "3. After fixing, output 'REPAIR COMPLETE' on the last line.",
            "4. Keep changes minimal and targeted.",
            "",
            "Please fix the issues now.",
        ])

        return "\n".join(lines)
