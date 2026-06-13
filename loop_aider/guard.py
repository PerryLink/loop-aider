"""
loop_aider/guard.py —— PhaseGuard 完整实现（Milestone 3 核心安全模块）

提供 5 个 Pre-call Gates（调用前安全门）和 5 个 Post-call Audits（调用后审计）。
每一扇 Gate 在 Aider 子进程启动前拦截风险；每一道 Audit 在 Aider 返回后核查输出。

Gates:
    G1 内容安全门      — 检测 prompt 中的恶意指令、代码注入
    G2 计划确认门      — Part 2 前检查 Part 1 确认状态
    G3 依赖安装门      — 检测 pip/npm install 并拦截确认
    G4 危险操作门      — 5 层危险命令检测（L0~L4）
    G5 文件变更门      — 检查变更范围是否超出 allowed_file_scope

Audits:
    A1 输出有效性审计   — 检查 Aider 是否真正输出了代码
    A2 文件变更审计     — 对比前后文件差异
    A3 stop_signal 审计 — 检查响应中是否包含正确的 stop_signal
    A4 banned_behaviors — 检查是否违反了禁止行为
    A5 整体合规审计     — 综合评估
"""

from __future__ import annotations

import os
import re
import logging
from pathlib import Path
from typing import Optional

# 复用 phase_guard 的共享数据类型，保持模块间兼容
from .phase_guard import (
    GateResult, AuditResult, Issue, TrustLevel,
    PhaseBlockedError, PhasePausedError,
    _is_in_scope, _artifact_exists_and_nonempty, _read_file_safe,
    CONTENT_SAFETY_PATTERNS,
    CATASTROPHIC_PATTERNS, IRREVERSIBLE_PATTERNS,
    HIGH_IMPACT_PATTERNS, SHELL_ESCAPE_PATTERNS, PROTECTED_PATHS,
)


# =============================================================================
# PhaseGuard —— 安全守门人
# =============================================================================

class PhaseGuard:
    """
    PhaseGuard —— Pre-call Gate + Post-call Audit 协议实现。

    每个 Aider 子进程调用前后各运行 5 道安全检查，覆盖内容安全、操作风险、
    文件变更、输出有效性和整体合规性五个维度。

    Attributes:
        config: 配置字典。
        mode: 信任级别枚举。
        interactive_timeout_minutes: 交互模式超时（分钟）。
        file_count_threshold: 各模式下文件变更数量阈值。
    """

    def __init__(
        self,
        config: dict,
        mode: TrustLevel = TrustLevel.AUTO,
        interactive_timeout_minutes: int = 30,
    ):
        """初始化 PhaseGuard。

        Args:
            config: 配置字典。
            mode: 信任级别。
            interactive_timeout_minutes: 交互模式超时分钟数。
        """
        self.config = config
        self.mode = mode
        self.interactive_timeout_minutes = interactive_timeout_minutes
        self.dangerous_patterns = config.get(
            "gate_dangerous_command_patterns",
            [p for p, _ in CATASTROPHIC_PATTERNS],
        )
        self.file_count_threshold = config.get(
            "gate_file_count_threshold",
            {"safe": 3, "auto": 10, "unsafe": 999},
        )
        self.logger = logging.getLogger("loop_aider.guard")

    # ========================================================================
    # G1: 内容安全门
    # ========================================================================

    def gate_content_safety(self, prompt: str) -> GateResult:
        """G1: 扫描 prompt 中的恶意指令和代码注入。

        检测关键词：恶意软件、后门、勒索软件、漏洞利用、钓鱼等。
        所有信任级别均硬拦截。

        Args:
            prompt: 渲染后的完整 prompt 文本。

        Returns:
            GateResult: blocked=True 表示检测到恶意内容。
        """
        prompt_lower = prompt.lower()
        for pattern, description in CONTENT_SAFETY_PATTERNS:
            if re.search(pattern, prompt_lower, re.IGNORECASE):
                self.logger.warning(
                    "G1 BLOCKED: %s", description
                )
                return GateResult(
                    blocked=True, paused=False,
                    reason=f"G1 内容安全: 检测到 {description}。所有信任级别均禁止。",
                    requires_user=False, default_action="block",
                )
        self.logger.debug("G1 PASSED")
        return GateResult(
            blocked=False, paused=False, reason="",
            requires_user=False, default_action="pass",
        )

    # ========================================================================
    # G2: 计划确认门
    # ========================================================================

    def gate_plan_confirmation(
        self,
        phase: str,
        user_approved_plan: bool,
        state: Optional[dict] = None,
    ) -> GateResult:
        """G2: Part 2 前检查 Part 1 确认状态。

        仅在 part_2_1 阶段生效：
            L1 safe: 暂停等待用户确认。
            L2 auto: 自动通过（记录日志）。
            L3 unsafe: 跳过。
            L1+ interactive: 暂停（含超时降级）。

        Args:
            phase: 当前阶段名称。
            user_approved_plan: 用户是否已确认方案。
            state: 完整的 state.json 字典。

        Returns:
            GateResult: paused=True 表示需要用户确认。
        """
        if phase != "part_2_1":
            return GateResult(
                blocked=False, paused=False, reason="",
                requires_user=False, default_action="pass",
            )

        if user_approved_plan:
            self.logger.info("G2 PASSED: 用户已确认方案")
            return GateResult(
                blocked=False, paused=False, reason="",
                requires_user=False, default_action="pass",
            )

        if self.mode == TrustLevel.UNSAFE:
            return GateResult(
                blocked=False, paused=False,
                reason="L3: 方案确认自动跳过",
                requires_user=False, default_action="pass",
            )

        if self.mode == TrustLevel.AUTO:
            return GateResult(
                blocked=False, paused=False,
                reason="L2: 方案已自动确认（记录日志）",
                requires_user=False, default_action="pass",
            )

        timeout_msg = (
            f"（超时 {self.interactive_timeout_minutes} 分钟后自动降级）"
            if self.mode == TrustLevel.INTERACTIVE else ""
        )
        self.logger.info("G2 PAUSED: 等待用户确认方案")
        return GateResult(
            blocked=False, paused=True,
            reason=f"G2 计划确认: 即将进入实施阶段。请确认方案后继续。{timeout_msg}",
            requires_user=True,
            default_action="auto_degrade" if self.mode == TrustLevel.INTERACTIVE else "pause",
        )

    # ========================================================================
    # G3: 依赖安装门
    # ========================================================================

    def gate_dependency_install(
        self,
        prompt: str,
        phase: str,
        approved_dependencies: Optional[list[str]] = None,
    ) -> GateResult:
        """G3: 检测 pip/npm install 并拦截确认。

        检测依赖安装指令，根据信任级别决定拦截或放行：
            L1 safe: 仅放行方案中已声明的依赖。
            L2 auto: 仅放行来自默认源的依赖。
            L3 unsafe: 全部放行。

        Args:
            prompt: 渲染后的 prompt 文本。
            phase: 阶段名称。
            approved_dependencies: 已批准的依赖包列表。

        Returns:
            GateResult: blocked=True 表示依赖安装被拦截。
        """
        INSTALL_PATTERNS = [
            r'\bpip\s+install\b', r'\bpip3\s+install\b',
            r'\bnpm\s+install\b', r'\bnpm\s+i\b',
            r'\byarn\s+add\b', r'\bcargo\s+install\b', r'\bcargo\s+add\b',
            r'\bgem\s+install\b', r'\bgo\s+get\b', r'\bgo\s+install\b',
            r'\bapt\s+install\b', r'\bapt-get\s+install\b',
            r'\bbrew\s+install\b', r'\bconda\s+install\b',
            r'\bdocker\s+pull\b',
        ]

        has_install = any(
            re.search(pat, prompt, re.IGNORECASE) for pat in INSTALL_PATTERNS
        )
        if not has_install:
            return GateResult(
                blocked=False, paused=False, reason="",
                requires_user=False, default_action="pass",
            )

        if self.mode == TrustLevel.UNSAFE:
            return GateResult(
                blocked=False, paused=False,
                reason="L3: 依赖安装不受限", requires_user=False, default_action="pass",
            )

        NON_DEFAULT_SOURCES = [
            r'--index-url\s+', r'--extra-index-url\s+',
            r'--registry\s+', r'git\+https?://', r'\.tar\.gz',
        ]
        uses_non_default = any(
            re.search(pat, prompt, re.IGNORECASE) for pat in NON_DEFAULT_SOURCES
        )

        if self.mode in (TrustLevel.SAFE, TrustLevel.INTERACTIVE):
            if approved_dependencies:
                if uses_non_default:
                    return GateResult(
                        blocked=True, paused=False,
                        reason="G3: L1 模式下禁止安装非默认源依赖。",
                        requires_user=False, default_action="block",
                    )
                return GateResult(
                    blocked=False, paused=False,
                    reason="L1: 依赖已在方案中授权", requires_user=False, default_action="pass",
                )
            else:
                return GateResult(
                    blocked=True, paused=False,
                    reason="G3: L1 模式仅允许方案中列出的依赖。",
                    requires_user=False, default_action="block",
                )

        # L2
        if uses_non_default:
            return GateResult(
                blocked=True, paused=False,
                reason="G3: L2 模式下禁止从非默认源安装。请使用 PyPI/npmjs.org 等。",
                requires_user=False, default_action="block",
            )

        return GateResult(
            blocked=False, paused=False,
            reason="L2: 将从默认源安装（已记录）", requires_user=False, default_action="pass",
        )

    # ========================================================================
    # G4: 危险操作门（5 层检测）
    # ========================================================================

    def gate_dangerous_ops(self, prompt: str, phase: str = "") -> GateResult:
        """G4: 5 层危险命令检测。

        L0 CATASTROPHIC:  rm -rf /   — 所有模式硬拦截
        L1 SYSTEM:        sudo       — L1+L2 拦截
        L2 FILESYSTEM:    chmod 777  — L1 拦截, L2 暂停
        L3 NETWORK:       curl|bash  — 所有模式硬拦截
        L4 PATH_PROTECTION: ../..   — 所有模式硬拦截（内部文件）

        Args:
            prompt: 渲染后的 prompt 文本。
            phase: 阶段名称。

        Returns:
            GateResult: blocked=True 表示检测到危险操作。
        """
        # L0: 灾难性操作
        for pattern, description in CATASTROPHIC_PATTERNS:
            if re.search(pattern, prompt, re.IGNORECASE):
                self.logger.critical("G4 BLOCKED (L0): %s", description)
                return GateResult(
                    blocked=True, paused=False,
                    reason=f"G4 L0-灾难性: {description}。所有级别均禁止。",
                    requires_user=False, default_action="block",
                )

        # L3: Shell 逃逸
        for pattern, description in SHELL_ESCAPE_PATTERNS:
            if re.search(pattern, prompt, re.IGNORECASE):
                self.logger.critical("G4 BLOCKED (L3): %s", description)
                return GateResult(
                    blocked=True, paused=False,
                    reason=f"G4 L3-Shell逃逸: {description}。所有级别均禁止。",
                    requires_user=False, default_action="block",
                )

        # L4: 路径保护
        for protected_path in PROTECTED_PATHS:
            if protected_path in prompt:
                self.logger.critical("G4 BLOCKED (L4): %s", protected_path)
                return GateResult(
                    blocked=True, paused=False,
                    reason=f"G4 L4-路径保护: 引用了受保护路径 '{protected_path}'。",
                    requires_user=False, default_action="block",
                )

        if self.mode == TrustLevel.UNSAFE:
            return GateResult(
                blocked=False, paused=False,
                reason="L3: 仅灾难性操作拦截", requires_user=False, default_action="pass",
            )

        # L1: 不可逆操作
        for pattern, description in IRREVERSIBLE_PATTERNS:
            if re.search(pattern, prompt, re.IGNORECASE):
                if self.mode in (TrustLevel.SAFE, TrustLevel.AUTO, TrustLevel.INTERACTIVE):
                    self.logger.warning("G4 BLOCKED (L1): %s", description)
                    return GateResult(
                        blocked=True, paused=False,
                        reason=f"G4 L1-不可逆: {description}。L1/L2 模式下禁止。",
                        requires_user=False, default_action="block",
                    )

        # L2: 高影响操作
        for pattern, description in HIGH_IMPACT_PATTERNS:
            if re.search(pattern, prompt, re.IGNORECASE):
                if self.mode in (TrustLevel.SAFE, TrustLevel.INTERACTIVE):
                    return GateResult(
                        blocked=True, paused=False,
                        reason=f"G4 L2-高影响: {description}。L1 安全模式下禁止。",
                        requires_user=False, default_action="block",
                    )
                if self.mode == TrustLevel.AUTO:
                    return GateResult(
                        blocked=False, paused=True,
                        reason=f"G4 L2-高影响: {description}。需要确认后继续。",
                        requires_user=True, default_action="pause",
                    )

        self.logger.debug("G4 PASSED")
        return GateResult(
            blocked=False, paused=False, reason="",
            requires_user=False, default_action="pass",
        )

    # ========================================================================
    # G5: 文件变更门
    # ========================================================================

    def gate_file_changes(
        self,
        phase: str,
        declared_files: Optional[list[str]] = None,
        task_files: Optional[list[str]] = None,
        allowed_file_scope: Optional[list[str]] = None,
    ) -> GateResult:
        """G5: 检查变更范围是否超出 allowed_file_scope。

        L1 safe: 必须有声明；超出阈值则暂停。
        L2 auto: 记录日志后放行。
        L3 unsafe: 跳过。

        Args:
            phase: 阶段名称。
            declared_files: 已声明的预期变更文件。
            task_files: 任务级别的预期变更文件。
            allowed_file_scope: 允许的文件范围列表。

        Returns:
            GateResult: paused=True 表示需要用户确认文件变更计划。
        """
        if self.mode == TrustLevel.UNSAFE:
            return GateResult(
                blocked=False, paused=False,
                reason="L3: 文件变更预览跳过", requires_user=False, default_action="pass",
            )

        all_files = list(set((declared_files or []) + (task_files or [])))

        # 若指定了 allowed_file_scope，检查越界
        if allowed_file_scope and all_files:
            out_of_scope = [
                f for f in all_files
                if not _is_in_scope(f, allowed_file_scope)
            ]
            if out_of_scope:
                self.logger.warning(
                    "G5 BLOCKED: %d file(s) out of allowed scope", len(out_of_scope)
                )
                return GateResult(
                    blocked=True, paused=False,
                    reason=f"G5: {len(out_of_scope)} 个文件超出 allowed_file_scope。{out_of_scope}",
                    requires_user=False, default_action="block",
                )

        if self.mode == TrustLevel.AUTO:
            files_str = ', '.join(all_files) if all_files else '(none)'
            self.logger.info("G5 PASSED (auto): %s", files_str)
            return GateResult(
                blocked=False, paused=False,
                reason=f"L2: 文件变更已记录 — {files_str}",
                requires_user=False, default_action="pass",
            )

        # L1 / L1+
        if not all_files:
            self.logger.warning("G5 PAUSED: 无文件变更声明")
            return GateResult(
                blocked=False, paused=True,
                reason="G5: L1 安全模式下需要声明预期变更文件清单。",
                requires_user=True, default_action="pause",
            )

        threshold = self.file_count_threshold.get(self.mode.value, 3)
        if len(all_files) > threshold:
            files_list = '\n  - '.join(all_files)
            return GateResult(
                blocked=False, paused=True,
                reason=f"G5: 声明了 {len(all_files)} 个文件变更（超过 L1 阈值 {threshold}）。\n  - {files_list}",
                requires_user=True, default_action="pause",
            )

        self.logger.info("G5 PASSED: %d files", len(all_files))
        return GateResult(
            blocked=False, paused=False,
            reason=f"G5: {len(all_files)} 个文件变更已确认",
            requires_user=False, default_action="pass",
        )

    # ========================================================================
    # A1: 输出有效性审计
    # ========================================================================

    def audit_output_validity(
        self, stdout: str, stderr: str, phase: str = ""
    ) -> tuple[list[Issue], list[str]]:
        """A1: 检查 Aider 是否真正输出了代码。

        判断 Aider stdout 中是否包含实质性代码内容：
        - 是否包含 diff/patch 内容
        - 是否包含代码块标记
        - 是否只是空输出或纯错误信息

        Args:
            stdout: Aider 标准输出。
            stderr: Aider 标准错误输出。
            phase: 阶段名称。

        Returns:
            (issues, warnings) 元组。
        """
        issues: list[Issue] = []
        warnings: list[str] = []

        if not stdout.strip():
            issues.append(Issue(
                severity="P1",
                title="Aider 输出为空",
                description=f"阶段 {phase} 的 Aider 调用未产生任何 stdout 输出。",
                source="A1_output_validity",
                affected_files=[],
                fix_strategy="检查 prompt 模板是否正确渲染，Aider 是否正确安装。",
            ))
            return issues, warnings

        # 检查是否包含实质性代码变更
        has_diff = bool(re.search(r'^[+-]', stdout, re.MULTILINE))
        has_code_block = bool(re.search(r'```', stdout))
        has_patch = bool(re.search(r'^diff\s+--git', stdout, re.MULTILINE))

        if not (has_diff or has_code_block or has_patch):
            warnings.append(
                f"A1: 阶段 {phase} 的 Aider 输出中未检测到 diff/代码块/patch 内容。"
                f"可能 Aider 仅输出了文本分析而无代码变更。"
            )

        # 检查是否有仅含错误信息的迹象
        error_indicators = sum(
            1 for kw in ('traceback', 'error:', 'fatal:', 'exception')
            if kw in stdout.lower()
        )
        if error_indicators >= 3:
            issues.append(Issue(
                severity="P1",
                title="Aider 输出以错误信息为主",
                description=f"阶段 {phase} 的输出包含 {error_indicators} 个错误指示词。",
                source="A1_output_validity",
                affected_files=[],
                fix_strategy="审查 stderr 和 Aider 返回码，可能需要调整 prompt 或修复代码。",
            ))

        if not issues:
            self.logger.debug("A1 PASSED: output validity ok")
        return issues, warnings

    # ========================================================================
    # A2: 文件变更审计
    # ========================================================================

    def audit_file_changes(
        self,
        affected_files: list[str],
        file_diffs: dict[str, str],
        phase: str = "",
        task_files: Optional[list[str]] = None,
    ) -> tuple[list[Issue], list[str]]:
        """A2: 对比前后文件差异，检测异常变更。

        检查：
        - 变更的文件是否在任务预期之内
        - 是否有空 diff（文件被 touch 但未修改内容）
        - 变更文件数量是否异常

        Args:
            affected_files: Aider 实际修改的文件列表。
            file_diffs: 每个文件的 diff 内容字典。
            phase: 阶段名称。
            task_files: 任务预期的文件列表。

        Returns:
            (issues, warnings) 元组。
        """
        issues: list[Issue] = []
        warnings: list[str] = []

        if not affected_files:
            warnings.append(f"A2: 阶段 {phase} 未修改任何文件。")
            return issues, warnings

        # 检查空 diff
        for fpath, diff_content in file_diffs.items():
            # 一个只有 header 没有实际变更的 diff
            lines = [l for l in diff_content.split('\n') if l.strip()]
            change_lines = [l for l in lines if l.startswith(('+', '-'))
                            and not l.startswith(('+++', '---'))]
            if len(change_lines) == 0:
                warnings.append(f"A2: 文件 {fpath} 的 diff 未包含实际变更行。")

        # 检查意外变更
        if task_files:
            task_normalized = {f.replace('\\', '/') for f in task_files}
            unexpected = []
            for f in affected_files:
                normalized = f.replace('\\', '/')
                if normalized not in task_normalized:
                    is_expected = any(
                        normalized.startswith(tf) or tf.startswith(normalized)
                        for tf in task_normalized
                    )
                    if not is_expected:
                        unexpected.append(f)

            if unexpected:
                for f in unexpected:
                    issues.append(Issue(
                        severity="P2",
                        title=f"非预期文件被修改: {f}",
                        description=f"Aider 修改了 {f}，不在任务预期变更列表中。",
                        source="A2_file_changes",
                        affected_files=[f],
                        fix_strategy=f"审查 {f} 的变更，如非预期则 git checkout -- {f}",
                    ))

        # 变更数过多告警
        if len(affected_files) > 20:
            warnings.append(
                f"A2: 单次调用修改了 {len(affected_files)} 个文件，建议审查。"

            )

        if not issues:
            self.logger.debug("A2 PASSED: %d files changed", len(affected_files))
        return issues, warnings

    # ========================================================================
    # A3: stop_signal 审计
    # ========================================================================

    def audit_stop_signal(
        self, stdout: str, stderr: str, phase: str = ""
    ) -> tuple[list[Issue], list[str]]:
        """A3: 检查 Aider 响应中是否包含正确的 stop_signal。

        loop-aider 期望 Aider 在完成各阶段时输出明确的 stop_signal
        标记。如果缺少该信号，可能表示 Aider 未正确完成阶段任务。

        Args:
            stdout: Aider 标准输出。
            stderr: Aider 标准错误输出。
            phase: 阶段名称（用于匹配对应的 stop_signal）。

        Returns:
            (issues, warnings) 元组。
        """
        issues: list[Issue] = []
        warnings: list[str] = []

        combined = stdout + '\n' + stderr

        # 各阶段对应的 stop_signal 模式
        STOP_SIGNALS = {
            "part_1_1": r'(?:STOP|COMPLETE).*requirements',
            "part_1_2": r'(?:STOP|COMPLETE).*direction',
            "part_1_3": r'(?:STOP|COMPLETE).*solution',
            "part_2_1": r'(?:STOP|COMPLETE).*plan',
            "part_2_2": r'(?:STOP|COMPLETE).*implementation',
            "part_2_3": r'(?:STOP|COMPLETE).*review',
            "part_2_4": r'(?:STOP|COMPLETE).*test.strategy',
            "part_2_5": r'(?:STOP|COMPLETE).*test.plan',
            "part_2_6": r'(?:STOP|COMPLETE).*test.execution',
            "part_2_7": r'(?:STOP|COMPLETE).*audit',
            "part_2_8": r'(?:STOP|COMPLETE).*verification',
        }

        # 通用 stop_signal 模式
        generic_pattern = r'(?i)\b(?:STOP|DONE|COMPLETE|FINISHED)\b'

        expected_signal = STOP_SIGNALS.get(phase)
        found_expected = False

        if expected_signal:
            found_expected = bool(re.search(expected_signal, combined, re.IGNORECASE))

        found_generic = bool(re.search(generic_pattern, combined))

        if not found_expected and not found_generic:
            warnings.append(
                f"A3: 阶段 {phase} 的输出中未检测到 stop_signal。"
                f"这可能表示 Aider 未正常完成该阶段的任务。"
            )

        if not found_expected and expected_signal:
            warnings.append(
                f"A3: 未找到阶段特定 stop_signal，仅依赖通用信号判断。"
            )

        if not issues:
            self.logger.debug("A3 PASSED: stop_signal check ok")
        return issues, warnings

    # ========================================================================
    # A4: banned_behaviors 审计
    # ========================================================================

    def audit_banned_behaviors(
        self, stdout: str, stderr: str, phase: str = ""
    ) -> tuple[list[Issue], list[str]]:
        """A4: 检查 Aider 输出是否违反了禁止行为。

        检测项包括：
        - 生成后门/恶意代码
        - 尝试绕过安全限制
        - 执行未授权的系统命令
        - 输出包含敏感信息（密钥、密码）
        - 拒绝服务或资源耗尽模式

        Args:
            stdout: Aider 标准输出。
            stderr: Aider 标准错误输出。
            phase: 阶段名称。

        Returns:
            (issues, warnings) 元组。
        """
        issues: list[Issue] = []
        warnings: list[str] = []
        combined = stdout + '\n' + stderr

        # 禁止行为模式库
        BANNED_PATTERNS: list[tuple[str, str, str]] = [
            # (pattern, description, severity)
            (r'\bbackdoor\b', '生成后门代码', 'P0'),
            (r'\bkeylogger\b', '生成键盘记录器', 'P0'),
            (r'\bbypass.*(?:auth|authenticate|login)\b', '绕过认证机制', 'P0'),
            (r'\bexploit\b.*\b(?:vuln|CVE)\b', '利用已知漏洞', 'P0'),
            (r'(?i)(?:api[_-]?key|secret[_-]?key|password|token)\s*[:=]\s*[\'"][^\'"]{8,}', '输出含敏感凭证', 'P0'),
            (r'\bsudo\s+rm\b', '尝试执行 sudo rm', 'P1'),
            (r'\bchmod\s+777\b', '尝试设置 777 权限', 'P1'),
            (r'\bwget\b.*\|\s*(?:ba)?sh\b', 'wget 管道执行', 'P1'),
            (r'\beval\s*\(\s*[\'"]', '使用 eval 执行动态代码', 'P2'),
            (r'while\s*\(\s*1\s*\)|while\s*true\s*;', '无限循环模式', 'P2'),
            (r'\bos\.system\s*\(', 'Python os.system 调用', 'P2'),
            (r'\bsubprocess\.call\s*\(\s*[\'"]', 'subprocess 未验证调用', 'P2'),
        ]

        for pattern, description, severity in BANNED_PATTERNS:
            if re.search(pattern, combined, re.IGNORECASE):
                self.logger.warning("A4: banned behavior — %s [%s]", description, severity)
                issues.append(Issue(
                    severity=severity,
                    title=f"禁止行为检测: {description}",
                    description=f"Aider 输出中检测到禁止行为: {description}。"
                                f"严重度: {severity}。",
                    source="A4_banned_behaviors",
                    affected_files=[],
                    fix_strategy="立即审查 Aider 输出，必要时回退变更或终止进程。",
                ))

        if not issues:
            self.logger.debug("A4 PASSED: no banned behaviors")
        return issues, warnings

    # ========================================================================
    # A5: 整体合规审计
    # ========================================================================

    def audit_overall_compliance(
        self,
        phase: str,
        exit_code: int,
        affected_files: list[str],
        stdout: str,
        all_issues: list[Issue],
        all_warnings: list[str],
        contracts: Optional[dict] = None,
    ) -> tuple[list[Issue], list[str]]:
        """A5: 综合评估 Aider 调用的整体合规性。

        汇总检查：
        - exit_code 是否正常
        - 是否产生了代码变更
        - P0/P1/P2 问题统计
        - 与 phase_contracts 的符合度
        - 给出整体评级（PASS / WARN / FAIL）

        Args:
            phase: 阶段名称。
            exit_code: Aider 退出码。
            affected_files: 变更文件列表。
            stdout: Aider 标准输出。
            all_issues: 前序审计收集的所有问题。
            all_warnings: 前序审计收集的所有告警。
            contracts: 阶段合约字典。

        Returns:
            (issues, warnings) 元组。
        """
        issues: list[Issue] = []
        warnings: list[str] = []

        # 统计 P0/P1/P2
        p0_count = sum(1 for i in all_issues if i.severity == "P0")
        p1_count = sum(1 for i in all_issues if i.severity == "P1")
        p2_count = sum(1 for i in all_issues if i.severity == "P2")

        # 退出码检查
        if exit_code != 0:
            issues.append(Issue(
                severity="P1" if exit_code < 0 else "P2",
                title=f"Aider 非零退出码: {exit_code}",
                description=f"阶段 {phase} 的 Aider 调用以退出码 {exit_code} 结束。",
                source="A5_compliance",
                affected_files=[],
                fix_strategy="检查 stderr 输出，确认是否可重试。",
            ))

        # 是否产生变更
        if not affected_files and exit_code == 0:
            warnings.append(
                f"A5: 阶段 {phase} Aider 正常退出但未修改任何文件。"
            )

        # 合约检查
        if contracts:
            contract = contracts.get(phase, {})
            if contract:
                expected = contract.get("expected_outputs", [])
                if expected and not affected_files:
                    warnings.append(
                        f"A5: 阶段 {phase} 有 {len(expected)} 个预期产出但无文件变更。"
                    )

        # 整体评级
        if p0_count > 0:
            summary = f"FAIL: {p0_count} P0, {p1_count} P1, {p2_count} P2, {len(all_warnings)} warnings"
        elif p1_count > 0:
            summary = f"WARN: {p1_count} P1, {p2_count} P2, {len(all_warnings)} warnings"
        elif p2_count > 0 or len(all_warnings) > 3:
            summary = f"WARN: {p2_count} P2, {len(all_warnings)} warnings"
        else:
            summary = "PASS"

        self.logger.info("A5 整体合规: %s (phase=%s exit=%d files=%d)",
                         summary, phase, exit_code, len(affected_files))

        return issues, warnings

    # ========================================================================
    # 复合方法：批量运行
    # ========================================================================

    def run_all_pre_call_gates(
        self,
        prompt: str,
        phase: str,
        user_approved_plan: bool = False,
        task_files: Optional[list[str]] = None,
        declared_files: Optional[list[str]] = None,
        approved_dependencies: Optional[list[str]] = None,
        allowed_file_scope: Optional[list[str]] = None,
        state: Optional[dict] = None,
    ) -> dict[str, GateResult]:
        """按优先级顺序运行全部 5 个 Pre-call Gates。

        执行顺序：G1（最快，最高优先级）→ G4 → G3 → G2 → G5。
        如果 G1 拦截，立即返回不执行后续 Gate。

        Args:
            prompt: 渲染后的 prompt 文本。
            phase: 阶段名称。
            user_approved_plan: 用户是否已确认方案。
            task_files: 任务预期变更文件列表。
            declared_files: 已声明变更文件列表。
            approved_dependencies: 已批准的依赖列表。
            allowed_file_scope: 允许的文件范围。
            state: state.json 字典。

        Returns:
            {"G1_content_safety": GateResult, ...} 字典。
        """
        results: dict[str, GateResult] = {}

        # G1
        results["G1_content_safety"] = self.gate_content_safety(prompt)
        if results["G1_content_safety"].blocked:
            self.logger.critical("G1 blocked — 跳过后续 Gate")
            return results

        # G4
        results["G4_dangerous_ops"] = self.gate_dangerous_ops(prompt, phase)

        # G3
        results["G3_dependency_install"] = self.gate_dependency_install(
            prompt, phase, approved_dependencies
        )

        # G2
        results["G2_plan_confirmation"] = self.gate_plan_confirmation(
            phase, user_approved_plan, state
        )

        # G5
        results["G5_file_changes"] = self.gate_file_changes(
            phase, declared_files, task_files, allowed_file_scope
        )

        blocked = sum(1 for r in results.values() if r.blocked)
        paused = sum(1 for r in results.values() if r.paused)
        self.logger.info(
            "Pre-call Gates: %d blocked, %d paused, %d passed",
            blocked, paused, len(results) - blocked - paused,
        )
        return results

    def run_all_post_call_audits(
        self,
        aider_result,
        phase: str,
        contracts: Optional[dict] = None,
        task_files: Optional[list[str]] = None,
        historical_diff: str = "",
        artifacts_dir: str = ".aider/loop-aider/artifacts",
    ) -> AuditResult:
        """按顺序运行全部 5 个 Post-call Audits。

        Args:
            aider_result: AiderResult 对象（含 stdout/stderr/affected_files 等字段）。
            phase: 阶段名称。
            contracts: 阶段合约字典。
            task_files: 任务预期变更文件列表。
            historical_diff: 历史 diff 文本。
            artifacts_dir: 产物目录路径。

        Returns:
            AuditResult: 含 passed/isses/warnings 的综合审计结果。
        """
        all_issues: list[Issue] = []
        all_warnings: list[str] = []

        stdout = getattr(aider_result, 'stdout', '')
        stderr = getattr(aider_result, 'stderr', '')
        affected_files = getattr(aider_result, 'affected_files', [])
        file_diffs = getattr(aider_result, 'file_diffs', {})
        exit_code = getattr(aider_result, 'exit_code', -1)

        # A1: 输出有效性审计
        a1_issues, a1_warnings = self.audit_output_validity(stdout, stderr, phase)
        all_issues.extend(a1_issues)
        all_warnings.extend(a1_warnings)

        # A2: 文件变更审计
        a2_issues, a2_warnings = self.audit_file_changes(
            affected_files, file_diffs, phase, task_files
        )
        all_issues.extend(a2_issues)
        all_warnings.extend(a2_warnings)

        # A3: stop_signal 审计
        a3_issues, a3_warnings = self.audit_stop_signal(stdout, stderr, phase)
        all_issues.extend(a3_issues)
        all_warnings.extend(a3_warnings)

        # A4: banned_behaviors 审计
        a4_issues, a4_warnings = self.audit_banned_behaviors(stdout, stderr, phase)
        all_issues.extend(a4_issues)
        all_warnings.extend(a4_warnings)

        # A5: 整体合规审计
        a5_issues, a5_warnings = self.audit_overall_compliance(
            phase, exit_code, affected_files, stdout,
            all_issues, all_warnings, contracts,
        )
        all_issues.extend(a5_issues)
        all_warnings.extend(a5_warnings)

        # 追加 AiderResult 自身的 warnings/errors
        aider_warnings = getattr(aider_result, 'warnings', [])
        if aider_warnings:
            all_warnings.extend(f"[Aider] {w}" for w in aider_warnings)
        aider_errors = getattr(aider_result, 'errors', [])
        if aider_errors:
            all_warnings.extend(f"[Aider Error] {e}" for e in aider_errors)

        # 判定整体是否通过
        blocking = [i for i in all_issues if i.severity in ("P0", "P1")]
        passed = len(blocking) == 0

        self.logger.info(
            "Post-call Audits: passed=%s P0=%d P1=%d P2=%d warnings=%d",
            passed,
            sum(1 for i in all_issues if i.severity == "P0"),
            sum(1 for i in all_issues if i.severity == "P1"),
            sum(1 for i in all_issues if i.severity == "P2"),
            len(all_warnings),
        )

        return AuditResult(passed=passed, issues=all_issues, warnings=all_warnings)
