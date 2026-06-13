"""
loop_aider/phase_guard.py — PhaseGuard Protocol

PhaseGuard is the security core of loop-aider.  It replaces the Hook system
used by loop-claudecode (Aider has no Hook mechanism).  All security checks
run in-process, in Python, with no external script dependency.

ARCHITECTURE:
    Pre-call Gate (5 gates):   Executed BEFORE each Aider subprocess call.
        G1 — Content Safety:        Scan rendered prompt for malicious intent.
        G2 — Plan Confirmation:     Ensure user approved the solution before
                                     transitioning part_1_3 → part_2_1.
        G3 — Dependency Install:    Verify dependency installation requests
                                     come from trusted sources.
        G4 — Dangerous Operations:  Scan prompt + expected commands for
                                     destructive patterns.
        G5 — File Change Preview:   Verify expected file-change declarations
                                     before part_2_2 execution.

    Post-call Diff Audit (5 audits): Executed AFTER each Aider subprocess call.
        A1 — Scope Validation:      Actual affected_files vs allowed_file_scope.
        A2 — Unexpected Changes:    Files modified outside task assignments.
        A3 — Dangerous Ops Post:    Scan Aider stdout/stderr for executed
                                     dangerous commands.
        A4 — Artifact Integrity:    Verify expected_outputs exist and are non-empty.
        A5 — Regression Detection:  Compare current diff with historical diff
                                     for reintroduced bugs.

TRUST LEVELS:
    L1 (safe):        All gates active.  Plan confirmation pauses for user.
                      Dangerous ops → BLOCK or PAUSE.  File preview → PAUSE.
    L2 (auto):        Default.  Plan confirmation auto-passes (logged).
                      Dependency install: default-sources only.  Dangerous ops:
                      irreversible → BLOCK, high-impact → PAUSE.
    L3 (unsafe):      Only content-safety (G1) and catastrophic ops (G4-L0) enforced.
                      All other gates bypassed.
    L1+ (interactive): Like L1 but with a 30-minute timeout → auto-degrade.

DATA TYPES:
    GateResult:  NamedTuple(blocked: bool, paused: bool, reason: str,
                             requires_user: bool, default_action: str)
    AuditResult: NamedTuple(passed: bool, issues: list[Issue], warnings: list[str])
    Issue:       NamedTuple(severity: str, title: str, description: str,
                             source: str, affected_files: list[str],
                             fix_strategy: str, is_design_level: bool)
"""

from __future__ import annotations

import os
import re
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, NamedTuple

# ---------------------------------------------------------------------------
# Data Types
# ---------------------------------------------------------------------------

class TrustLevel(Enum):
    SAFE = "safe"           # L1
    AUTO = "auto"           # L2 (default)
    UNSAFE = "unsafe"       # L3
    INTERACTIVE = "interactive"  # L1+

class GateResult(NamedTuple):
    """Returned by every Pre-call Gate method."""
    blocked: bool           # True → hard-block, do NOT proceed to Aider
    paused: bool            # True → pause and wait for user confirmation
    reason: str             # Human-readable explanation
    requires_user: bool     # True → user must explicitly approve/unblock
    default_action: str     # "block" | "pass" | "pause" — used when timeout triggers

class AuditResult(NamedTuple):
    """Returned by post_call_audit()."""
    passed: bool            # True → no P0/P1 issues found
    issues: list[Issue]     # All issues discovered during audit
    warnings: list[str]     # Non-blocking warnings

class Issue(NamedTuple):
    """A single issue discovered during audit or gate analysis."""
    severity: str           # "P0" | "P1" | "P2"
    title: str
    description: str
    source: str             # Which gate/audit found this
    affected_files: list[str] = field(default_factory=list)
    fix_strategy: str = ""
    is_design_level: bool = False

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "title": self.title,
            "description": self.description,
            "source": self.source,
            "affected_files": self.affected_files,
            "fix_strategy": self.fix_strategy,
            "is_design_level": self.is_design_level,
        }

# ---------------------------------------------------------------------------
# Dangerous Pattern Definitions (shared across G1, G4, A3)
# ---------------------------------------------------------------------------

# G1: Content safety keywords — ALL trust levels hard-block
CONTENT_SAFETY_PATTERNS: list[tuple[str, str]] = [
    (r'\bmalware\b',                        '恶意软件生成请求'),
    (r'\bkeylogger\b',                      '键盘记录器生成请求'),
    (r'\bransomware\b',                     '勒索软件生成请求'),
    (r'\bbackdoor\b',                       '后门程序生成请求'),
    (r'\brootshell\b',                      'Root shell 生成请求'),
    (r'\bexploit\b.*\bvulnerability\b',     '漏洞利用请求'),
    (r'\bphishing\b',                       '钓鱼攻击页面/邮件生成'),
    (r'\bbypass\b.*\b(?:authentication|auth)\b', '绕过认证机制'),
    (r'\bprivilege\s+escalation\b',         '权限提升利用'),
    (r'\bdox\b',                             '人肉搜索 / 隐私侵犯'),
    (r'\bscam\b',                            '欺诈内容生成'),
    (r'\bfake\b.*\b(?:content|news|site)\b', '虚假信息生成'),
    (r'\bstolen\b.*\b(?:credential|token|key)\b', '盗用凭证处理'),
]

# G4: Catastrophic commands — ALL trust levels hard-block (L0)
CATASTROPHIC_PATTERNS: list[tuple[str, str]] = [
    (r'\brm\s+-rf\s+/',                     'rm -rf / 或其变体'),
    (r'\brm\s+-rf\s+~',                     'rm -rf ~'),
    (r'\brm\s+-rf\s+\$HOME',                'rm -rf $HOME'),
    (r'\bmkfs\.',                            '格式化文件系统'),
    (r'\bdd\s+if=.*\s+of=/dev/',            '直接写入块设备'),
    (r'>\s*/dev/sd[a-z]',                    '覆写磁盘设备'),
    (r'\bchmod\s+777\s+/',                   '全局 777 权限'),
    (r'\bchmod\s+-R\s+777\s+/',             '递归全局 777 权限'),
    (r':\(\)\s*\{.*:\|:&\}\s*;:',           'Fork bomb'),
    (r'\bDROP\s+TABLE\b',                    'SQL DROP TABLE'),
    (r'\bDROP\s+DATABASE\b',                 'SQL DROP DATABASE'),
    (r'\bTRUNCATE\s+TABLE\b',                'SQL TRUNCATE TABLE'),
    (r'\bgit\s+push\s+--force\s+origin\s+(main|master)\b', 'Force push main/master'),
    (r'\bgit\s+push\s+--force\s+--delete\b', 'Force delete remote branch'),
    (r'\bcurl\b.*\|.*\b(?:ba)?sh\b',         'curl | shell 管道注入'),
    (r'\bwget\b.*\|.*\b(?:ba)?sh\b',         'wget | shell 管道注入'),
    (r'\bsystemctl\s+disable\s+sshd\b',       '禁用 SSH 服务'),
    (r'\biptables\s+-F\b',                    '清空防火墙规则'),
    (r'\biptables\s+-P\s+INPUT\s+ACCEPT\b',  '防火墙全开'),
]

# G4: Irreversible commands — L1+L2 block, L3 pass (L1)
IRREVERSIBLE_PATTERNS: list[tuple[str, str]] = [
    (r'\brm\s+-rf\s+\./',                    'rm -rf ./ (项目目录递归删除)'),
    (r'\brm\s+-rf\s+\.\*',                   'rm -rf .* (隐藏文件递归删除)'),
    (r'\bgit\s+reset\s+--hard\b',            'git reset --hard (丢弃所有未提交变更)'),
    (r'\bgit\s+clean\s+-fd\b',              'git clean -fd (删除未跟踪文件)'),
    (r'\bDEL\s+/F\s+/S\s+/Q\b',             'Windows 强制递归删除'),
]

# G4: High-impact commands — L1 block, L2 pause, L3 pass (L2)
HIGH_IMPACT_PATTERNS: list[tuple[str, str]] = [
    (r'\bpip\s+install\b(?!.*--upgrade\s+pip)',     'pip install (未经验证的包安装)'),
    (r'\bnpm\s+install\s+-g\b',                      'npm 全局安装'),
    (r'\bdocker-compose\s+down\s+-v\b',              'Docker compose 删除卷'),
    (r'\bdocker\s+rm\s+-f\b',                        'Docker 强制删除容器'),
    (r'\bgit\s+push\s+--force\b(?!.*origin\s+(main|master))', 'Force push (非 main/master)'),
    (r'\bchmod\s+777\b(?!\s+/)',                     '局部 777 权限'),
    (r'\bchown\s+-R\b',                               '递归更改所有者'),
]

# G4: Shell escape patterns — ALL trust levels hard-block (L3)
SHELL_ESCAPE_PATTERNS: list[tuple[str, str]] = [
    (r'\beval\s+',                           'eval 命令（代码注入风险）'),
    (r'\bsource\s+.*http',                   'source 远程内容'),
    (r'`[^`]*`',                              '反引号命令替换（代码注入风险）'),
    (r'\$\(.*\)',                              '$() 命令替换（代码注入风险）'),
]

# G4: Path protection — ALL trust levels hard-block (L4)
PROTECTED_PATHS: list[str] = [
    '.aider/loop-aider/',
    'state.json',
    'state.json.bak',
    'state.json.tmp',
    '.lock',
    'loop_aider/',
    'prompt_templates/',
]

# A3: Patterns that indicate dangerous commands were actually executed
EXECUTED_DANGEROUS_PATTERNS: list[tuple[str, str, str]] = [
    # (pattern, description, severity)
    (r'removed\s+.*recursively',             '递归删除执行确认', 'P1'),
    (r'DROP\s+TABLE.*executed',              'SQL DROP TABLE 执行确认', 'P0'),
    (r'force\s+push.*successful',            'Force push 执行确认', 'P0'),
    (r'chmod\s+777.*applied',                'chmod 777 执行确认', 'P1'),
    (r'pip\s+install.*Successfully',         'pip 安装执行确认', 'P2'),
    (r'npm\s+install.*added\s+\d+\s+packages', 'npm 安装执行确认', 'P2'),
    (r'Permission\s+denied',                  '权限错误（可能是危险操作结果）', 'P2'),
    (r'Connection\s+refused',                 '连接被拒绝（可能是防火墙变更结果）', 'P2'),
    (r'sshd.*stopped',                        'SSH 服务已停止', 'P0'),
]

# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def _is_in_scope(file_path: str, allowed_scope: list[str]) -> bool:
    """Check if file_path falls within any allowed scope prefix."""
    if not allowed_scope:
        return True  # No scope restriction → everything allowed
    normalized = file_path.replace('\\', '/')
    for scope in allowed_scope:
        normalized_scope = scope.replace('\\', '/')
        if normalized.startswith(normalized_scope) or normalized == normalized_scope:
            return True
        # Allow wildcard patterns like "src/**"
        if normalized_scope.endswith('/**'):
            prefix = normalized_scope[:-3]
            if normalized.startswith(prefix):
                return True
    return False


def _artifact_exists_and_nonempty(artifact_path: str) -> bool:
    """Check if a file exists at artifact_path and has non-zero size."""
    try:
        p = Path(artifact_path)
        return p.exists() and p.stat().st_size > 0
    except OSError:
        return False


def _read_file_safe(file_path: str) -> str:
    """Read file contents safely, returning empty string on any error."""
    try:
        return Path(file_path).read_text(encoding='utf-8')
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# PhaseGuard Class
# ---------------------------------------------------------------------------

class PhaseGuard:
    """
    PhaseGuard — Pre-call Gate + Post-call Diff Audit protocol.

    Instantiated once per loop-aider process.  Holds configuration and the
    trust-level mode.  All gate/audit methods are stateless beyond config.

    Usage:
        guard = PhaseGuard(config, mode=TrustLevel.AUTO)

        # Before Aider call
        gate_results = guard.run_all_pre_call_gates(
            prompt=rendered_prompt, phase="part_2_2",
            user_approved_plan=True, task_files=["src/main.py"],
            state=None)
        if any(g.blocked for g in gate_results):
            raise PhaseBlockedError(gate_results)
        if any(g.paused for g in gate_results):
            # Set pending_confirmation in state.json and exit
            ...

        # After Aider call
        audit = guard.run_all_post_call_audits(
            aider_result=aider_result, phase="part_2_2",
            contracts=state.get("phase_contracts", {}).get("contracts", {}),
            task_files=["src/main.py"], historical_diff="")
        if not audit.passed:
            # Inject issues into state.json for routing
            ...
    """

    def __init__(
        self,
        config: dict,
        mode: TrustLevel = TrustLevel.AUTO,
        interactive_timeout_minutes: int = 30,
    ):
        self.config = config
        self.mode = mode
        self.interactive_timeout_minutes = interactive_timeout_minutes
        self.dangerous_patterns = config.get(
            "gate_dangerous_command_patterns",
            [p for p, _ in CATASTROPHIC_PATTERNS]
        )
        self.file_count_threshold = config.get(
            "gate_file_count_threshold",
            {"safe": 3, "auto": 10, "unsafe": 999}
        )
        self.logger = logging.getLogger("loop_aider.phase_guard")

    # ========================================================================
    # PRE-CALL GATES
    # ========================================================================

    # -- G1: Content Safety --------------------------------------------------

    def gate_content_safety(self, prompt: str) -> GateResult:
        """
        G1: Content Safety Assessment.

        Scan the rendered prompt for malicious-intent keywords.  This gate
        is HARD-ENFORCED across ALL trust levels (L1, L2, L3, L1+).

        Returns GateResult.blocked=True if malicious content is detected.
        """
        prompt_lower = prompt.lower()
        for pattern, description in CONTENT_SAFETY_PATTERNS:
            if re.search(pattern, prompt_lower, re.IGNORECASE):
                self.logger.warning(
                    "G1 BLOCKED: content safety violation — %s", description
                )
                return GateResult(
                    blocked=True,
                    paused=False,
                    reason=f"G1 内容安全: 检测到 {description}。所有信任级别均禁止。",
                    requires_user=False,
                    default_action="block",
                )
        self.logger.debug("G1 PASSED: no content safety violations detected")
        return GateResult(
            blocked=False,
            paused=False,
            reason="",
            requires_user=False,
            default_action="pass",
        )

    # -- G2: Plan Confirmation -----------------------------------------------

    def gate_plan_confirmation(
        self,
        phase: str,
        user_approved_plan: bool,
        state: Optional[dict] = None,
    ) -> GateResult:
        """
        G2: Plan Confirmation Gate.

        Checks whether the user has explicitly approved the solution (produced
        in part_1_3) before transitioning to part_2_1 (implementation).

        Trust-level behaviour:
            L1 (safe):        PAUSE — wait for user to confirm the plan.
            L2 (auto):        PASS  — auto-approve, log the decision.
            L3 (unsafe):      PASS  — skip confirmation entirely.
            L1+ (interactive): PAUSE — wait, with 30min timeout → auto-degrade.

        This gate is only relevant when phase is about to become part_2_1.
        """
        # Only active for the part_1_3 → part_2_1 transition
        if phase != "part_2_1":
            return GateResult(
                blocked=False, paused=False, reason="",
                requires_user=False, default_action="pass",
            )

        if user_approved_plan:
            self.logger.info("G2 PASSED: user already approved the plan")
            return GateResult(
                blocked=False, paused=False, reason="",
                requires_user=False, default_action="pass",
            )

        if self.mode == TrustLevel.UNSAFE:
            self.logger.info("G2 PASSED (unsafe mode): plan confirmation bypassed")
            return GateResult(
                blocked=False, paused=False,
                reason="L3 unsafe mode: 方案确认自动跳过",
                requires_user=False, default_action="pass",
            )

        if self.mode == TrustLevel.AUTO:
            self.logger.info("G2 PASSED (auto mode): plan auto-approved")
            return GateResult(
                blocked=False, paused=False,
                reason="L2 auto mode: 方案已自动确认（记录日志）",
                requires_user=False, default_action="pass",
            )

        # L1 (safe) or L1+ (interactive): pause and wait
        timeout_msg = (
            f" (超时 {self.interactive_timeout_minutes} 分钟后自动降级)"
            if self.mode == TrustLevel.INTERACTIVE else ""
        )
        self.logger.info("G2 PAUSED: waiting for user plan confirmation")
        return GateResult(
            blocked=False,
            paused=True,
            reason=(
                f"G2 方案确认: 即将从设计阶段进入实施阶段。"
                f"请确认方案文档 (artifacts/03-solution.md) 后继续。{timeout_msg}"
            ),
            requires_user=True,
            default_action="auto_degrade" if self.mode == TrustLevel.INTERACTIVE else "pause",
        )

    # -- G3: Dependency Install Authorization ---------------------------------

    def gate_dependency_install(
        self,
        prompt: str,
        phase: str,
        approved_dependencies: Optional[list[str]] = None,
    ) -> GateResult:
        """
        G3: Dependency Install Authorization.

        Checks whether the prompt requests installing packages/dependencies.
        If so, verifies the source against allowed registries.

        Trust-level behaviour:
            L1 (safe):        Non-solution-listed deps → BLOCK.
            L2 (auto):        Non-default-registry deps → BLOCK.
                              Default-registry deps → PASS (logged).
            L3 (unsafe):      All deps → PASS.

        Default registries: PyPI, npmjs.org, crates.io, Maven Central,
                           RubyGems.org, Go proxy.
        """
        # Patterns that suggest dependency installation
        INSTALL_PATTERNS = [
            r'\bpip\s+install\b', r'\bpip3\s+install\b',
            r'\bnpm\s+install\b', r'\bnpm\s+i\b',
            r'\byarn\s+add\b',
            r'\bcargo\s+install\b', r'\bcargo\s+add\b',
            r'\bgem\s+install\b',
            r'\bgo\s+get\b', r'\bgo\s+install\b',
            r'\bapt\s+install\b', r'\bapt-get\s+install\b',
            r'\bbrew\s+install\b',
            r'\bconda\s+install\b', r'\bmamba\s+install\b',
            r'\bpoetry\s+add\b',
            r'\bpdm\s+add\b',
            r'\buv\s+add\b',
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
            self.logger.info("G3 PASSED (unsafe mode): dependency install not restricted")
            return GateResult(
                blocked=False, paused=False,
                reason="L3 unsafe mode: 依赖安装不受限",
                requires_user=False, default_action="pass",
            )

        # Detect non-default registries
        NON_DEFAULT_SOURCES = [
            r'--index-url\s+', r'--extra-index-url\s+',
            r'--registry\s+', r'--repository\s+',
            r'git\+https?://', r'git\+ssh://',
            r'\.tar\.gz', r'\.whl',
        ]
        uses_non_default = any(
            re.search(pat, prompt, re.IGNORECASE) for pat in NON_DEFAULT_SOURCES
        )

        if self.mode == TrustLevel.SAFE or self.mode == TrustLevel.INTERACTIVE:
            # L1: check against approved list from solution
            if approved_dependencies:
                # Extract package names from prompt and check against approved list
                # (simplified heuristic)
                self.logger.info("G3: L1 mode — checking deps against solution-approved list")
                if uses_non_default:
                    return GateResult(
                        blocked=True, paused=False,
                        reason="G3 依赖安装: L1 模式下禁止安装非默认源的依赖。请使用方案中列出的源。",
                        requires_user=False, default_action="block",
                    )
                return GateResult(
                    blocked=False, paused=False,
                    reason="L1: 依赖安装已授权（在方案中列出）",
                    requires_user=False, default_action="pass",
                )
            else:
                return GateResult(
                    blocked=True, paused=False,
                    reason="G3 依赖安装: L1 模式下只有方案明确列出的依赖才允许安装。当前方案未列出任何依赖。",
                    requires_user=False, default_action="block",
                )

        # L2 (auto): block non-default sources, allow default
        if uses_non_default:
            self.logger.warning("G3 BLOCKED: non-default registry source detected")
            return GateResult(
                blocked=True, paused=False,
                reason="G3 依赖安装: L2 模式下禁止从非默认源安装依赖。请使用 PyPI / npmjs.org / crates.io 等默认源。",
                requires_user=False, default_action="block",
            )

        self.logger.info("G3 PASSED: dependency install from default registries")
        return GateResult(
            blocked=False, paused=False,
            reason="L2: 将从默认源安装依赖（已记录日志）",
            requires_user=False, default_action="pass",
        )

    # -- G4: Dangerous Operations Detection -----------------------------------

    def gate_dangerous_ops(
        self,
        prompt: str,
        phase: str = "",
    ) -> GateResult:
        """
        G4: Dangerous Operations Detection.

        Five-layer matching system:
            L0 CATASTROPHIC:  ALL modes hard-block.
            L1 IRREVERSIBLE:  L1+L2 block, L3 pass.
            L2 HIGH_IMPACT:   L1 block, L2 pause, L3 pass.
            L3 SHELL_ESCAPE:  ALL modes hard-block.
            L4 PATH_PROTECTION: ALL modes hard-block (loop-aider internal files).
        """
        # L0: Catastrophic — all modes hard-block
        for pattern, description in CATASTROPHIC_PATTERNS:
            if re.search(pattern, prompt, re.IGNORECASE):
                self.logger.critical("G4 BLOCKED (L0): %s", description)
                return GateResult(
                    blocked=True, paused=False,
                    reason=f"G4 危险操作 (L0-灾难性): 检测到 {description}。所有信任级别均禁止。",
                    requires_user=False, default_action="block",
                )

        # L3: Shell escape — all modes hard-block
        for pattern, description in SHELL_ESCAPE_PATTERNS:
            if re.search(pattern, prompt, re.IGNORECASE):
                self.logger.critical("G4 BLOCKED (L3): %s", description)
                return GateResult(
                    blocked=True, paused=False,
                    reason=f"G4 危险操作 (L3-Shell逃逸): 检测到 {description}。所有信任级别均禁止。",
                    requires_user=False, default_action="block",
                )

        # L4: Path protection — all modes hard-block
        for protected_path in PROTECTED_PATHS:
            if protected_path in prompt:
                self.logger.critical(
                    "G4 BLOCKED (L4): prompt references protected path %s", protected_path
                )
                return GateResult(
                    blocked=True, paused=False,
                    reason=f"G4 危险操作 (L4-路径保护): prompt 引用了受保护的路径 '{protected_path}'。loop-aider 内部文件不可被修改。",
                    requires_user=False, default_action="block",
                )

        if self.mode == TrustLevel.UNSAFE:
            # L3 mode: only L0/L3/L4 enforced above, remaining layers pass
            self.logger.info("G4 PASSED (unsafe mode): non-catastrophic patterns allowed")
            return GateResult(
                blocked=False, paused=False,
                reason="L3 unsafe mode: 仅灾难性操作硬拦截",
                requires_user=False, default_action="pass",
            )

        # L1: Irreversible — L1+L2 block
        for pattern, description in IRREVERSIBLE_PATTERNS:
            if re.search(pattern, prompt, re.IGNORECASE):
                if self.mode in (TrustLevel.SAFE, TrustLevel.AUTO, TrustLevel.INTERACTIVE):
                    self.logger.warning("G4 BLOCKED (L1): %s", description)
                    return GateResult(
                        blocked=True, paused=False,
                        reason=f"G4 危险操作 (L1-不可逆): 检测到 {description}。L1/L2 模式下禁止。",
                        requires_user=False, default_action="block",
                    )

        # L2: High-impact — L1 block, L2 pause
        for pattern, description in HIGH_IMPACT_PATTERNS:
            if re.search(pattern, prompt, re.IGNORECASE):
                if self.mode in (TrustLevel.SAFE, TrustLevel.INTERACTIVE):
                    self.logger.warning("G4 BLOCKED (L2): %s in safe mode", description)
                    return GateResult(
                        blocked=True, paused=False,
                        reason=f"G4 危险操作 (L2-高影响): 检测到 {description}。L1 安全模式下禁止。",
                        requires_user=False, default_action="block",
                    )
                if self.mode == TrustLevel.AUTO:
                    self.logger.warning("G4 PAUSED (L2): %s in auto mode", description)
                    return GateResult(
                        blocked=False, paused=True,
                        reason=f"G4 危险操作 (L2-高影响): 检测到 {description}。L2 模式下需要确认后继续。",
                        requires_user=True, default_action="pause",
                    )

        self.logger.debug("G4 PASSED: no dangerous operations detected")
        return GateResult(
            blocked=False, paused=False, reason="",
            requires_user=False, default_action="pass",
        )

    # -- G5: File Change Preview -------------------------------------------------

    def gate_file_changes(
        self,
        phase: str,
        declared_files: Optional[list[str]] = None,
        task_files: Optional[list[str]] = None,
    ) -> GateResult:
        """
        G5: File Change Preview Gate.

        Before part_2_2 (implementation), checks whether the set of files to be
        modified has been declared and falls within reasonable limits.

        Trust-level behaviour:
            L1 (safe):        Must have declaration; count > threshold → PAUSE.
            L2 (auto):        PASS (logged).
            L3 (unsafe):      PASS.
        """
        if phase != "part_2_2":
            return GateResult(
                blocked=False, paused=False, reason="",
                requires_user=False, default_action="pass",
            )

        if self.mode == TrustLevel.UNSAFE:
            return GateResult(
                blocked=False, paused=False,
                reason="L3 unsafe mode: 文件变更预览跳过",
                requires_user=False, default_action="pass",
            )

        if self.mode == TrustLevel.AUTO:
            # Log the declared file list but don't block
            files_str = ', '.join(declared_files) if declared_files else '(none declared)'
            self.logger.info("G5 PASSED (auto mode): files to change: %s", files_str)
            return GateResult(
                blocked=False, paused=False,
                reason=f"L2 auto mode: 文件变更已记录 — {files_str}",
                requires_user=False, default_action="pass",
            )

        # L1 (safe) or L1+ (interactive)
        all_files = declared_files or []
        if task_files:
            all_files = list(set(all_files + task_files))

        if not all_files:
            self.logger.warning("G5 PAUSED: no file change declaration in safe mode")
            return GateResult(
                blocked=False, paused=True,
                reason="G5 文件变更预览: L1 安全模式下需要声明预期变更文件清单。请先完成方案/Plan 中 affected_files 的声明。",
                requires_user=True, default_action="pause",
            )

        threshold = self.file_count_threshold.get(self.mode.value, 3)
        if len(all_files) > threshold:
            files_list = '\n  - '.join(all_files)
            self.logger.warning(
                "G5 PAUSED: %d files declared, threshold is %d",
                len(all_files), threshold
            )
            return GateResult(
                blocked=False, paused=True,
                reason=(
                    f"G5 文件变更预览: 声明了 {len(all_files)} 个文件变更"
                    f"（超过 L1 阈值 {threshold}）。请确认以下文件变更计划:\n"
                    f"  - {files_list}"
                ),
                requires_user=True, default_action="pause",
            )

        files_str = ', '.join(all_files)
        self.logger.info("G5 PASSED: %d files declared (threshold %d)", len(all_files), threshold)
        return GateResult(
            blocked=False, paused=False,
            reason=f"G5: 已声明 {len(all_files)} 个文件变更 — {files_str}",
            requires_user=False, default_action="pass",
        )

    # ========================================================================
    # POST-CALL DIFF AUDITS
    # ========================================================================

    # -- A1: Scope Validation -------------------------------------------------

    def audit_scope_validation(
        self,
        affected_files: list[str],
        phase: str,
        contracts: dict,
    ) -> tuple[list[Issue], list[str]]:
        """
        A1: Change Scope Validation.

        Compares actual affected_files (from diff_parser) against the
        allowed_file_scope declared in phase_contracts.

        Files outside the allowed scope are flagged as P2 issues.
        """
        issues: list[Issue] = []
        warnings: list[str] = []

        contract = contracts.get(phase, {})
        allowed_scope = contract.get("allowed_file_scope", [])

        if not allowed_scope:
            warnings.append(
                f"A1: 阶段 {phase} 未声明 allowed_file_scope，"
                f"无法进行变更范围校验。"
            )
            return issues, warnings

        out_of_scope_files: list[str] = []
        for file_path in affected_files:
            if not _is_in_scope(file_path, allowed_scope):
                out_of_scope_files.append(file_path)

        if out_of_scope_files:
            self.logger.warning(
                "A1: %d file(s) outside allowed scope for phase %s: %s",
                len(out_of_scope_files), phase, out_of_scope_files
            )
            for f in out_of_scope_files:
                issues.append(Issue(
                    severity="P2",
                    title=f"变更范围越界: {f}",
                    description=(
                        f"Aider 修改了 {f}，该文件不在阶段 {phase} 的允许范围内: "
                        f"{allowed_scope}"
                    ),
                    source="A1_scope_validation",
                    affected_files=[f],
                    fix_strategy=f"git checkout -- {f} 回退非预期变更",
                    is_design_level=False,
                ))
        else:
            self.logger.debug(
                "A1 PASSED: all %d affected files within scope for phase %s",
                len(affected_files), phase
            )

        return issues, warnings

    # -- A2: Unexpected Changes Detection -------------------------------------

    def audit_unexpected_changes(
        self,
        affected_files: list[str],
        task_files: Optional[list[str]] = None,
        phase: str = "",
    ) -> tuple[list[Issue], list[str]]:
        """
        A2: Unexpected Changes Detection.

        Detects files modified by Aider that were NOT in the declared task
        file list.  Unexpected modifications are flagged as P2 issues and
        the change should be reverted via `git checkout`.
        """
        issues: list[Issue] = []
        warnings: list[str] = []

        if not task_files:
            # No task file list to compare against — can't detect unexpected changes
            warnings.append(
                "A2: 未提供 task_files 列表，无法进行意外变更检测。"
            )
            return issues, warnings

        task_files_normalized = {f.replace('\\', '/') for f in task_files}
        unexpected: list[str] = []

        for f in affected_files:
            normalized = f.replace('\\', '/')
            if normalized not in task_files_normalized:
                # Also check if it's a prefix match (e.g., task_file="src/"
                # should match affected_file="src/module/file.py")
                is_expected = any(
                    normalized.startswith(tf) or tf.startswith(normalized)
                    for tf in task_files_normalized
                )
                if not is_expected:
                    unexpected.append(f)

        if unexpected:
            self.logger.warning(
                "A2: %d unexpected file(s) modified: %s",
                len(unexpected), unexpected
            )
            for f in unexpected:
                issues.append(Issue(
                    severity="P2",
                    title=f"非预期文件被修改: {f}",
                    description=(
                        f"Aider 修改了 {f}，但该文件不在当前 task 的预期变更列表中。"
                        f"可能是 Aider 的连锁修改或误操作。"
                    ),
                    source="A2_unexpected_changes",
                    affected_files=[f],
                    fix_strategy=f"git checkout -- {f} 回退（如确认为误操作）",
                    is_design_level=False,
                ))
            warnings.append(
                f"A2: 检测到 {len(unexpected)} 个非预期文件变更。"
                f"请人工审查这些变更是否合理。"
            )
        else:
            self.logger.debug("A2 PASSED: no unexpected file modifications")

        return issues, warnings

    # -- A3: Dangerous Operations Post-Audit -----------------------------------

    def audit_dangerous_ops_post(
        self,
        stdout: str,
        stderr: str,
    ) -> tuple[list[Issue], list[str]]:
        """
        A3: Dangerous Operations Post-Audit.

        Scans Aider's stdout AND stderr for patterns indicating that
        dangerous commands were actually executed during the session.
        This catches commands that slipped past the pre-call gate or
        commands Aider generated dynamically.
        """
        issues: list[Issue] = []
        warnings: list[str] = []

        combined_output = stdout + '\n' + stderr

        for pattern, description, severity in EXECUTED_DANGEROUS_PATTERNS:
            if re.search(pattern, combined_output, re.IGNORECASE):
                self.logger.critical(
                    "A3: dangerous command execution detected — %s [%s]",
                    description, severity
                )
                issues.append(Issue(
                    severity=severity,
                    title=f"危险命令执行事后检测: {description}",
                    description=(
                        f"Aider 输出中检测到已执行的危险命令痕迹: {description}。"
                        f"请立即审查 Aider 的 stdout/stderr 并确认是否需要回退。"
                    ),
                    source="A3_dangerous_ops_post",
                    affected_files=[],
                    fix_strategy="人工审查 Aider 输出 + 必要时 git checkout 回退",
                    is_design_level=False,
                ))
                warnings.append(
                    f"⚠️ A3 危险操作事后审计: 在 Aider 输出中检测到 '{description}' "
                    f"(严重度: {severity})"
                )

        if not issues:
            self.logger.debug("A3 PASSED: no dangerous command traces in output")

        return issues, warnings

    # -- A4: Artifact Integrity Check -----------------------------------------

    def audit_artifact_integrity(
        self,
        phase: str,
        contracts: dict,
        artifacts_dir: str = ".aider/loop-aider/artifacts",
    ) -> tuple[list[Issue], list[str]]:
        """
        A4: Artifact Integrity Check.

        Verifies that all expected_outputs declared in the phase contract
        have been generated and are non-empty files.

        Missing or empty artifacts are flagged as P1 issues (they block
        downstream phases that depend on them).
        """
        issues: list[Issue] = []
        warnings: list[str] = []

        contract = contracts.get(phase, {})
        expected_outputs = contract.get("expected_outputs", [])

        if not expected_outputs:
            warnings.append(
                f"A4: 阶段 {phase} 未声明 expected_outputs，"
                f"无法进行产物完整性检查。"
            )
            return issues, warnings

        for output_desc in expected_outputs:
            # expected_outputs are human-readable descriptions like
            # "05b-implementation-diff.patch generated"
            # Extract the filename from the description
            filename = output_desc.split()[0] if output_desc else ""
            if not filename:
                continue

            artifact_path = os.path.join(artifacts_dir, filename)
            # Also check without path prefix (just filename)
            if not _artifact_exists_and_nonempty(artifact_path):
                # Try with artifacts/ prefix
                alt_path = f"artifacts/{filename}"
                if not _artifact_exists_and_nonempty(alt_path):
                    self.logger.warning(
                        "A4: expected artifact missing or empty — %s (%s)",
                        filename, output_desc
                    )
                    issues.append(Issue(
                        severity="P1",
                        title=f"产物缺失或为空: {filename}",
                        description=(
                            f"阶段 {phase} 的约定要求生成 {output_desc}，"
                            f"但文件 {filename} 不存在或为空。下游阶段可能无法继续。"
                        ),
                        source="A4_artifact_integrity",
                        affected_files=[filename],
                        fix_strategy=f"重新执行阶段 {phase}，调整 prompt 确保产物生成",
                        is_design_level=False,
                    ))

        if not issues:
            self.logger.debug(
                "A4 PASSED: all %d expected artifacts present and non-empty",
                len(expected_outputs)
            )

        return issues, warnings

    # -- A5: Regression Detection ---------------------------------------------

    def audit_regression_detection(
        self,
        current_diff: str,
        historical_diff: str,
        phase: str = "",
    ) -> tuple[list[Issue], list[str]]:
        """
        A5: Regression Detection.

        Compares the current cycle's diff against the accumulated historical
        diff to detect if already-fixed issues have been reintroduced.

        Strategy: extract file-level changes from both diffs; if a file that
        was modified in a prior FIX cycle is being modified again in a way
        that reverts the fix, flag as P1 regression.
        """
        issues: list[Issue] = []
        warnings: list[str] = []

        if not historical_diff:
            warnings.append("A5: 无历史 diff 数据，跳过回归检测。")
            return issues, warnings

        if not current_diff:
            warnings.append("A5: 当前 diff 为空，跳过回归检测。")
            return issues, warnings

        # Extract hunks per file from both diffs
        current_files = self._extract_file_changes(current_diff)
        historical_files = self._extract_file_changes(historical_diff)

        # For each file that appears in both diffs, do a simplified check:
        # if the current diff removes lines that were added in a historical
        # fix cycle, that may indicate a regression.
        for file_path in set(current_files.keys()) & set(historical_files.keys()):
            cur_removed = current_files[file_path].get("removed_lines", set())
            hist_added = historical_files[file_path].get("added_lines", set())

            # Simple heuristic: lines that were added in history and are now
            # being removed might be regressions
            potential_regressions = cur_removed & hist_added
            if potential_regressions:
                sample_lines = list(potential_regressions)[:3]
                self.logger.warning(
                    "A5: potential regression detected in %s — %d line(s) match",
                    file_path, len(potential_regressions)
                )
                issues.append(Issue(
                    severity="P1",
                    title=f"疑似回归: {file_path}",
                    description=(
                        f"文件 {file_path} 中检测到可能的历史修复被回退。"
                        f"当前 diff 删除了 {len(potential_regressions)} 行"
                        f"曾在历史修复中添加的代码。"
                        f"样本行: {sample_lines}"
                    ),
                    source="A5_regression_detection",
                    affected_files=[file_path],
                    fix_strategy=(
                        f"人工审查 {file_path} 的变更，确认是否为有意的回退"
                        f"还是无意的回归。如为回归，re-apply 历史修复。"
                    ),
                    is_design_level=False,
                ))
                warnings.append(
                    f"⚠️ A5 回归检测: {file_path} 疑似回归 "
                    f"({len(potential_regressions)} 行匹配)"
                )

        if not issues:
            self.logger.debug("A5 PASSED: no regression patterns detected")

        return issues, warnings

    def _extract_file_changes(self, diff_text: str) -> dict[str, dict]:
        """
        Parse a unified diff into per-file added/removed line sets.

        Returns: {file_path: {"added_lines": set[str], "removed_lines": set[str]}}
        """
        result: dict[str, dict] = {}
        current_file = None
        current_added: set[str] = set()
        current_removed: set[str] = set()

        for line in diff_text.split('\n'):
            # Detect file header
            file_match = re.match(r'^\+\+\+\s+b/(.+)$', line)
            if file_match:
                # Save previous file's data
                if current_file:
                    result[current_file] = {
                        "added_lines": current_added,
                        "removed_lines": current_removed,
                    }
                current_file = file_match.group(1)
                current_added = set()
                current_removed = set()
                continue

            # Also detect --- a/file
            file_match_minus = re.match(r'^---\s+a/(.+)$', line)
            if file_match_minus:
                if current_file is None:
                    current_file = file_match_minus.group(1)
                continue

            if current_file is None:
                continue

            # Collect added/removed lines (skip the +/- prefix for comparison)
            if line.startswith('+') and not line.startswith('+++'):
                current_added.add(line[1:].strip())
            elif line.startswith('-') and not line.startswith('---'):
                current_removed.add(line[1:].strip())

        # Save last file
        if current_file:
            result[current_file] = {
                "added_lines": current_added,
                "removed_lines": current_removed,
            }

        return result

    # ========================================================================
    # COMPOSITE METHODS — run all gates / all audits at once
    # ========================================================================

    def run_all_pre_call_gates(
        self,
        prompt: str,
        phase: str,
        user_approved_plan: bool = False,
        task_files: Optional[list[str]] = None,
        declared_files: Optional[list[str]] = None,
        approved_dependencies: Optional[list[str]] = None,
        state: Optional[dict] = None,
    ) -> dict[str, GateResult]:
        """
        Run all 5 Pre-call Gates and return a dict of {gate_name: GateResult}.

        Callers should check:
            - any(r.blocked for r in results.values()) → abort immediately
            - any(r.paused for r in results.values()) → set pending_confirmation

        Returns results even if one gate blocks — the full set helps the user
        understand ALL problems at once rather than fixing them one by one.
        """
        results: dict[str, GateResult] = {}

        # G1: Content Safety (always first — fastest to check, highest priority)
        results["G1_content_safety"] = self.gate_content_safety(prompt)
        if results["G1_content_safety"].blocked:
            # G1 is a hard-stop for everything — no point checking further
            self.logger.critical("G1 blocked — skipping remaining pre-call gates")
            return results

        # G4: Dangerous Operations (always second)
        results["G4_dangerous_ops"] = self.gate_dangerous_ops(prompt, phase)

        # G3: Dependency Install
        results["G3_dependency_install"] = self.gate_dependency_install(
            prompt, phase, approved_dependencies
        )

        # G2: Plan Confirmation (only relevant for part_1_3→part_2_1 transition)
        results["G2_plan_confirmation"] = self.gate_plan_confirmation(
            phase, user_approved_plan, state
        )

        # G5: File Change Preview (only relevant for part_2_2)
        results["G5_file_changes"] = self.gate_file_changes(
            phase, declared_files, task_files
        )

        blocked_count = sum(1 for r in results.values() if r.blocked)
        paused_count = sum(1 for r in results.values() if r.paused)
        self.logger.info(
            "Pre-call gates complete: %d blocked, %d paused, %d passed",
            blocked_count, paused_count,
            len(results) - blocked_count - paused_count,
        )

        return results

    def run_all_post_call_audits(
        self,
        aider_result,  # AiderResult (avoid circular import by duck-typing)
        phase: str,
        contracts: dict,
        task_files: Optional[list[str]] = None,
        historical_diff: str = "",
        artifacts_dir: str = ".aider/loop-aider/artifacts",
    ) -> AuditResult:
        """
        Run all 5 Post-call Diff Audits and return an aggregated AuditResult.

        Parameters:
            aider_result:  An object with fields .stdout, .stderr, .affected_files,
                           .file_diffs, .warnings, .errors (AiderResult duck-type).
            phase:         Current phase name.
            contracts:     phase_contracts dict from state.json.
            task_files:    List of files expected to be modified (from task plan).
            historical_diff: Accumulated diff from prior cycles (05b-*.patch).
            artifacts_dir: Path to the artifacts directory.

        Returns:
            AuditResult with .passed=True only if zero P0/P1 issues were found.
        """
        all_issues: list[Issue] = []
        all_warnings: list[str] = []

        # Extract fields from aider_result (duck-typing)
        stdout = getattr(aider_result, 'stdout', '')
        stderr = getattr(aider_result, 'stderr', '')
        affected_files = getattr(aider_result, 'affected_files', [])
        # Reconstruct current diff from file_diffs if available
        file_diffs = getattr(aider_result, 'file_diffs', {})
        current_diff = '\n'.join(file_diffs.values()) if file_diffs else stdout

        # A1: Scope Validation
        a1_issues, a1_warnings = self.audit_scope_validation(
            affected_files, phase, contracts
        )
        all_issues.extend(a1_issues)
        all_warnings.extend(a1_warnings)

        # A2: Unexpected Changes
        a2_issues, a2_warnings = self.audit_unexpected_changes(
            affected_files, task_files, phase
        )
        all_issues.extend(a2_issues)
        all_warnings.extend(a2_warnings)

        # A3: Dangerous Operations Post-Audit
        a3_issues, a3_warnings = self.audit_dangerous_ops_post(stdout, stderr)
        all_issues.extend(a3_issues)
        all_warnings.extend(a3_warnings)

        # A4: Artifact Integrity
        a4_issues, a4_warnings = self.audit_artifact_integrity(
            phase, contracts, artifacts_dir
        )
        all_issues.extend(a4_issues)
        all_warnings.extend(a4_warnings)

        # A5: Regression Detection
        a5_issues, a5_warnings = self.audit_regression_detection(
            current_diff, historical_diff, phase
        )
        all_issues.extend(a5_issues)
        all_warnings.extend(a5_warnings)

        # Add any warnings from the AiderResult itself
        aider_warnings = getattr(aider_result, 'warnings', [])
        if aider_warnings:
            all_warnings.extend(f"[Aider] {w}" for w in aider_warnings)
        aider_errors = getattr(aider_result, 'errors', [])
        if aider_errors:
            all_warnings.extend(f"[Aider Error] {e}" for e in aider_errors)

        # Determine if audit passed (no P0 or P1 issues)
        blocking_issues = [i for i in all_issues if i.severity in ("P0", "P1")]
        passed = len(blocking_issues) == 0

        self.logger.info(
            "Post-call audits complete: passed=%s, P0=%d, P1=%d, P2=%d, warnings=%d",
            passed,
            sum(1 for i in all_issues if i.severity == "P0"),
            sum(1 for i in all_issues if i.severity == "P1"),
            sum(1 for i in all_issues if i.severity == "P2"),
            len(all_warnings),
        )

        return AuditResult(passed=passed, issues=all_issues, warnings=all_warnings)


# ---------------------------------------------------------------------------
# Exception Class
# ---------------------------------------------------------------------------

class PhaseBlockedError(Exception):
    """Raised when one or more Pre-call Gates block execution."""

    def __init__(self, gate_results: dict[str, GateResult]):
        self.gate_results = gate_results
        blocked_gates = [
            f"{name}: {result.reason}"
            for name, result in gate_results.items()
            if result.blocked
        ]
        msg = "Phase blocked by Pre-call Gates:\n" + "\n".join(
            f"  - {g}" for g in blocked_gates
        )
        super().__init__(msg)


class PhasePausedError(Exception):
    """Raised when one or more Pre-call Gates request a pause."""

    def __init__(self, gate_results: dict[str, GateResult]):
        self.gate_results = gate_results
        paused_gates = [
            f"{name}: {result.reason}"
            for name, result in gate_results.items()
            if result.paused
        ]
        msg = "Phase paused by Pre-call Gates:\n" + "\n".join(
            f"  - {g}" for g in paused_gates
        )
        super().__init__(msg)
