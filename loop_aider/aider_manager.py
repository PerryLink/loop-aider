"""
loop_aider/aider_manager.py — Aider CLI Subprocess Manager

Core module that encapsulates ALL interaction with the Aider CLI.  This is the
bridge between loop-aider's state machine and the LLM-powered coding engine.

ARCHITECTURE:
    check_health()     →  Verify Aider is installed, compatible, and ready.
    run_phase()        →  The MAIN pipeline: render Jinja2 template →
                           PhaseGuard pre-call gates → build subprocess cmd →
                           execute Aider → diff_parser → PhaseGuard post-call
                           audit → return AiderResult.

DATA TYPES:
    HealthStatus:      Enum of possible Aider health states.
    AiderResult:       Dataclass with all outputs from one Aider invocation.

PLATFORM SUPPORT:
    Windows:           shell=True mandatory (Aider is typically a .bat/.cmd shim).
    Linux/macOS:       shell=False (direct execution, better performance).
    All platforms:     UTF-8 encoding enforced for stdin/stdout/stderr pipes.
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import shlex
import shutil
import logging
import tempfile
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

# Lazy imports to avoid circular deps — imported inside methods
# from .phase_guard import PhaseGuard, TrustLevel, PhaseBlockedError, PhasePausedError
# from .diff_parser import parse_diff_output


# ---------------------------------------------------------------------------
# Data Types
# ---------------------------------------------------------------------------

class HealthStatus(Enum):
    """Aider CLI health check result."""
    OK = "ok"                                       # >= 0.86.0 — full support
    COMPATIBLE_WITH_WARNINGS = "compatible_with_warnings"  # >= 0.77.0, < 0.86.0
    INCOMPATIBLE = "incompatible"                   # < 0.77.0 — refuse to run
    NOT_FOUND = "not_found"                         # aider not in PATH or broken

@dataclass
class AiderResult:
    """
    Complete result of one Aider subprocess invocation.

    All fields are populated after run_phase() completes (or partially
    populated on timeout/error).
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

    @property
    def success(self) -> bool:
        """True if Aider exited with code 0 and did not time out."""
        return self.exit_code == 0 and not self.timed_out

    @property
    def total_lines_changed(self) -> int:
        """Sum of added + removed lines."""
        return self.added_lines + self.removed_lines


# ---------------------------------------------------------------------------
# Helper: Aider version parsing
# ---------------------------------------------------------------------------

def parse_version(version_str: str) -> tuple[int, ...]:
    """
    Parse a version string like "0.86.1" or "aider 0.86.1" into a tuple.

    Returns (0, 0, 0) if parsing fails.
    """
    # Strip prefix like "aider " or "aider-chat "
    cleaned = re.sub(r'^[a-zA-Z_-]+\s*', '', version_str.strip())
    # Extract version numbers
    match = re.search(r'(\d+)\.(\d+)\.(\d+)', cleaned)
    if match:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    # Try two-part version
    match = re.search(r'(\d+)\.(\d+)', cleaned)
    if match:
        return (int(match.group(1)), int(match.group(2)), 0)
    return (0, 0, 0)


# ---------------------------------------------------------------------------
# AiderManager
# ---------------------------------------------------------------------------

class AiderManager:
    """
    Aider CLI subprocess encapsulation manager.

    Core responsibilities:
      1. Health check — version detection + compatibility validation.
      2. Phase execution — the main run_phase() pipeline.
      3. Timeout management — kill and retry on hang.
      4. Cross-platform subprocess adaptation — Windows shell=True, POSIX direct.

    Usage:
        mgr = AiderManager(config)
        health = mgr.check_health()
        if health != HealthStatus.OK:
            ...  # handle incompatibility

        result = mgr.run_phase(
            phase="part_2_2",
            template_vars={"goal": "...", "task_list_json": "...", ...},
            files=["src/main.py"],
        )
    """

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(
        self,
        config: dict,
        template_dir: Optional[str] = None,
    ):
        """
        Initialize AiderManager.

        Parameters:
            config:       Configuration dict (merged from config.yml + state.json.config).
            template_dir: Path to the Jinja2 prompt_templates directory.
                          If None, auto-detect relative to this module.
        """
        self.config = config
        self.aider_path = config.get("aider_path", "aider")
        self.timeout = config.get("aider_timeout_seconds", 600)
        self.retry_count = config.get("aider_retry_count", 2)
        self.model = config.get("model", None)      # None → use Aider's default
        self.mode = config.get("mode", "auto")

        # Cached health/version info
        self._health: Optional[HealthStatus] = None
        self._version: Optional[str] = None
        self._version_tuple: tuple[int, ...] = (0, 0, 0)

        # Jinja2 template directory
        if template_dir:
            self.template_dir = Path(template_dir)
        else:
            # Auto-detect: look for prompt_templates/ next to this module
            module_dir = Path(__file__).resolve().parent
            candidate = module_dir / "prompt_templates"
            if candidate.is_dir():
                self.template_dir = candidate
            else:
                # Fallback: search relative to cwd
                candidate = Path(".aider/loop-aider/prompt_templates")
                if candidate.is_dir():
                    self.template_dir = candidate
                else:
                    self.template_dir = candidate  # will fail with clear error later

        # Jinja2 environment — initialized lazily in _init_jinja2()
        self._jinja2_env = None
        self._jinja2_available = False

        # PhaseGuard — initialized lazily in _init_phase_guard()
        self._phase_guard = None

        # Logger
        self.logger = logging.getLogger("loop_aider.aider_manager")

    # ------------------------------------------------------------------
    # Lazy Initializers
    # ------------------------------------------------------------------

    def _init_jinja2(self):
        """Initialize the Jinja2 environment for template rendering."""
        if self._jinja2_env is not None:
            return
        try:
            from jinja2 import Environment, FileSystemLoader, TemplateNotFound
            self._jinja2_available = True
            if self.template_dir.is_dir():
                self._jinja2_env = Environment(
                    loader=FileSystemLoader(str(self.template_dir)),
                    autoescape=False,           # We're rendering prompts, not HTML
                    trim_blocks=True,
                    lstrip_blocks=True,
                    keep_trailing_newline=True,
                )
                # Register custom filters / globals
                # now: 返回 datetime 对象（模板中可调用 .isoformat() 等方法）
                from datetime import datetime, timezone as tz
                self._jinja2_env.globals['now'] = lambda: datetime.now(tz.utc)
                # now_iso: 返回 ISO 8601 格式字符串（便捷用法）
                self._jinja2_env.globals['now_iso'] = lambda: datetime.now(tz.utc).isoformat()
            else:
                self.logger.warning(
                    "Jinja2 template directory not found: %s", self.template_dir
                )
                self._jinja2_env = None
        except ImportError:
            self.logger.warning("Jinja2 not installed — template rendering disabled")
            self._jinja2_available = False
            self._jinja2_env = None

    def _init_phase_guard(self):
        """Initialize the PhaseGuard instance."""
        if self._phase_guard is not None:
            return
        from .phase_guard import PhaseGuard, TrustLevel
        mode_map = {
            "safe": TrustLevel.SAFE,
            "auto": TrustLevel.AUTO,
            "unsafe": TrustLevel.UNSAFE,
            "interactive": TrustLevel.INTERACTIVE,
        }
        trust = mode_map.get(self.mode, TrustLevel.AUTO)
        self._phase_guard = PhaseGuard(
            config=self.config,
            mode=trust,
            interactive_timeout_minutes=self.config.get(
                "interactive_timeout_minutes", 30
            ),
        )

    # ------------------------------------------------------------------
    # Health Check
    # ------------------------------------------------------------------

    def check_health(self) -> HealthStatus:
        """
        Detect Aider CLI availability and version compatibility.

        Runs `aider --version`, parses the version string, and returns
        the compatibility level per the support matrix:
            >= 0.86.0 → OK
            >= 0.77.0 → COMPATIBLE_WITH_WARNINGS
            < 0.77.0  → INCOMPATIBLE (refuse to run)
            not found → NOT_FOUND

        Results are cached in self._health and self._version.
        """
        if self._health is not None:
            return self._health

        # 1. Check if aider is reachable
        aider_exe = shutil.which(self.aider_path)
        if aider_exe is None:
            self.logger.error("Aider not found in PATH: %s", self.aider_path)
            self._health = HealthStatus.NOT_FOUND
            return self._health

        # 2. Run aider --version
        try:
            result = subprocess.run(
                [aider_exe, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            self.logger.error("Aider --version timed out")
            self._health = HealthStatus.NOT_FOUND
            return self._health
        except FileNotFoundError:
            self.logger.error("Aider executable not found: %s", aider_exe)
            self._health = HealthStatus.NOT_FOUND
            return self._health
        except Exception as exc:
            self.logger.error("Unexpected error checking Aider: %s", exc)
            self._health = HealthStatus.NOT_FOUND
            return self._health

        version_output = result.stdout.strip() or result.stderr.strip()
        if not version_output:
            self.logger.error("Aider --version returned empty output")
            self._health = HealthStatus.NOT_FOUND
            return self._health

        # 3. Parse version
        self._version = version_output
        self._version_tuple = parse_version(version_output)
        self.logger.info(
            "Detected Aider version: %s → parsed as %s",
            version_output, ".".join(str(v) for v in self._version_tuple)
        )

        # 4. Compatibility check
        if self._version_tuple < (0, 77, 0):
            self._health = HealthStatus.INCOMPATIBLE
        elif self._version_tuple < (0, 86, 0):
            self._health = HealthStatus.COMPATIBLE_WITH_WARNINGS
        else:
            self._health = HealthStatus.OK

        return self._health

    def get_version(self) -> str:
        """Return cached Aider version string, or 'unknown'."""
        if self._version is None:
            self.check_health()
        return self._version or "unknown"

    def get_health(self) -> HealthStatus:
        """Return cached health status."""
        if self._health is None:
            self.check_health()
        return self._health or HealthStatus.NOT_FOUND

    # ------------------------------------------------------------------
    # Template Rendering
    # ------------------------------------------------------------------

    def render_template(self, template_name: str, variables: dict) -> str:
        """
        Render a Jinja2 template with the given variables.

        Parameters:
            template_name: The .j2 filename (e.g., "part_2_2_implementation.j2").
            variables:     Dict of template variables.

        Returns:
            Rendered prompt string.

        Raises:
            FileNotFoundError: Template file not found.
            RuntimeError:      Jinja2 not available or rendering error.
        """
        self._init_jinja2()
        if not self._jinja2_available or self._jinja2_env is None:
            raise RuntimeError(
                "Jinja2 is not available. Please install it: pip install jinja2"
            )

        try:
            template = self._jinja2_env.get_template(template_name)
        except Exception as exc:
            raise FileNotFoundError(
                f"Template '{template_name}' not found in {self.template_dir}: {exc}"
            )

        # Add standard variables if not provided
        defaults = {
            "now_iso": lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "now": lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        merged = {**defaults, **variables}

        try:
            rendered = template.render(**merged)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to render template '{template_name}': {exc}"
            ) from exc

        self.logger.debug(
            "Rendered template '%s' — %d chars", template_name, len(rendered)
        )
        return rendered

    def get_template_for_phase(self, phase: str) -> str:
        """
        Map a phase name to its Jinja2 template filename.

        Returns the .j2 filename (without path).
        """
        PHASE_TEMPLATE_MAP = {
            "part_1_1":  "part_1_1_requirements.j2",
            "part_1_2":  "part_1_2_direction.j2",
            "part_1_3":  "part_1_3_solution.j2",
            "part_2_1":  "part_2_1_plan.j2",
            "part_2_2":  "part_2_2_implementation.j2",
            "part_2_3":  "part_2_3_code_review.j2",
            "part_2_4":  "part_2_4_test_strategy.j2",
            "part_2_5":  "part_2_5_test_plan.j2",
            "part_2_6":  "part_2_6_test_execution.j2",
            "part_2_7":  "part_2_7_audit.j2",
            "part_2_8":  "part_2_8_verification.j2",
            "routing":   "routing.j2",
        }
        return PHASE_TEMPLATE_MAP.get(phase, f"{phase}.j2")

    # ------------------------------------------------------------------
    # Command Building
    # ------------------------------------------------------------------

    def _build_command(
        self,
        message: str,
        files: Optional[list[str]] = None,
        model: Optional[str] = None,
    ) -> list[str]:
        """
        Build the Aider CLI command as a list of arguments.

        Core arguments:
            --yes               Non-interactive auto-confirm mode.
            --message "..."      The single-turn prompt.
            --no-auto-commits   Prevent Aider from auto-committing; loop-aider
                                 handles Git commits itself.

        Optional arguments:
            --model <model>     Override the default model.
            <files>...          Context files for Aider to read.
        """
        cmd = [
            self.aider_path,
            "--yes",
            "--message", message,
            "--no-auto-commits",
        ]

        # Model override
        effective_model = model or self.model
        if effective_model:
            cmd.extend(["--model", effective_model])

        # Context files — Aider reads these for context, may also edit them
        if files:
            cmd.extend(files)

        return cmd

    def _build_cmd_string(self, cmd: list[str]) -> str:
        """
        Convert command list to a safe shell string (for logging and Windows).

        Uses shlex.quote() for POSIX safety; subprocess.list2cmdline() for
        Windows (which handles quoting differently).
        """
        if sys.platform == "win32":
            return subprocess.list2cmdline(cmd)
        else:
            return ' '.join(shlex.quote(arg) for arg in cmd)

    # ------------------------------------------------------------------
    # --message-file 安全方案
    # ------------------------------------------------------------------

    def _safe_message_arg(self, rendered_prompt: str,
                          state_dir: str = ".aider/loop-aider") -> tuple[list[str], str | None]:
        """
        将 prompt 写入临时文件，命令行仅传递文件路径。

        避免 prompt 内容出现在 ps aux / /proc 等进程列表中。
        Aider CLI 原生支持 --message-file 参数（>= 0.77.0）。
        若版本不支持，则回退到 --message 直接传递（输出警告）。

        Args:
            rendered_prompt: 渲染后的完整 prompt 文本。
            state_dir:       临时文件存放目录。

        Returns:
            (消息参数列表, 临时文件路径或 None)
            - 成功: (["--message-file", "/path/to/tmp"], tmp_path)
            - 回退: (["--message", prompt_text], None)
        """
        if self._version and self._version >= (0, 77, 0):
            os.makedirs(state_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix="loop-aider-msg-",
                suffix=".txt",
                dir=state_dir,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(rendered_prompt)
            return ["--message-file", tmp_path], tmp_path
        else:
            self.logger.warning(
                "Aider 版本过低，--message-file 不可用，prompt 将暴露在命令行参数中"
            )
            return ["--message", rendered_prompt], None

    def _cleanup_msg_file(self, tmp_path: str | None):
        """
        安全删除临时 prompt 文件（覆写随机字节后删除）。

        Aider 调用完成后调用，确保 prompt 内容不在磁盘上残留。

        Args:
            tmp_path: 临时文件路径，None 则无操作。
        """
        if tmp_path and os.path.exists(tmp_path):
            size = min(os.path.getsize(tmp_path), 4096)
            with open(tmp_path, "wb") as f:
                f.write(os.urandom(size))
            os.remove(tmp_path)

    # ------------------------------------------------------------------
    # Subprocess Execution (cross-platform)
    # ------------------------------------------------------------------

    def _execute_subprocess(
        self,
        cmd: list[str],
        timeout: int,
        cwd: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        """
        Execute Aider as a subprocess with cross-platform handling.

        Windows:    shell=True forced (Aider is often a .bat/.cmd shim).
        POSIX:      shell=False (direct execution, safer, faster).

        Returns subprocess.CompletedProcess.
        Raises subprocess.TimeoutExpired on timeout.
        """
        if sys.platform == "win32":
            # Windows: use shell=True with list2cmdline for quoting
            # Note: subprocess.run with a string + shell=True uses cmd.exe
            # which handles quoting via list2cmdline automatically.
            cmd_str = subprocess.list2cmdline(cmd)
            self.logger.debug("Windows subprocess: %s", cmd_str)
            return subprocess.run(
                cmd_str,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                encoding='utf-8',
                errors='replace',
            )
        else:
            # POSIX (Linux / macOS): direct execution
            self.logger.debug("POSIX subprocess: %s", cmd)
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                encoding='utf-8',
                errors='replace',
            )

    # ------------------------------------------------------------------
    # Output Parsing (diff_parser integration)
    # ------------------------------------------------------------------

    def _parse_output(self, result: subprocess.CompletedProcess) -> AiderResult:
        """
        Parse Aider subprocess output into a structured AiderResult.

        Extracts:
            - affected_files    (from unified diff headers in stdout)
            - added_lines       (lines starting with '+' but not '+++')
            - removed_lines     (lines starting with '-' but not '---')
            - file_diffs        (per-file diff fragments)
            - warnings/errors   (heuristically from stderr and stdout patterns)
            - models_used       (from Aider's model announcement)
            - tokens_used       (from Aider's cost/token summary)
        """
        aider_result = AiderResult(
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            exit_code=result.returncode,
        )

        stdout = aider_result.stdout

        # ---- affected_files ----
        aider_result.affected_files = self._extract_affected_files(stdout)

        # ---- file_diffs (per-file diff fragments) ----
        aider_result.file_diffs = self._extract_file_diffs(stdout)

        # ---- added_lines / removed_lines ----
        aider_result.added_lines, aider_result.removed_lines = \
            self._count_diff_lines(stdout)

        # ---- warnings from stderr ----
        if result.stderr:
            for line in result.stderr.strip().split('\n'):
                line = line.strip()
                if line:
                    if any(kw in line.lower() for kw in ('warn', 'warning')):
                        aider_result.warnings.append(line)
                    elif any(kw in line.lower() for kw in ('error', 'fail', 'traceback')):
                        aider_result.errors.append(line)

        # ---- warnings from stdout patterns ----
        for line in stdout.split('\n'):
            stripped = line.strip()
            if re.search(r'(?i)\b(?:warning|deprecated|deprecation)\b', stripped):
                if stripped not in aider_result.warnings:
                    aider_result.warnings.append(stripped)

        # ---- models_used ----
        model_match = re.search(
            r'(?i)(?:using|model)[:\s]+([a-zA-Z0-9_.-]+(?:-\d{8})?)', stdout
        )
        if model_match:
            aider_result.models_used.append(model_match.group(1))

        # ---- tokens_used ----
        token_match = re.search(
            r'(?i)(\d[\d,]*)\s*(?:tokens|tok)', stdout
        )
        if token_match:
            try:
                aider_result.tokens_used = int(token_match.group(1).replace(',', ''))
            except ValueError:
                pass

        # ---- errors from exit code ----
        if result.returncode != 0:
            if not aider_result.errors:
                aider_result.errors.append(
                    f"Aider exited with code {result.returncode}"
                )

        return aider_result

    def _extract_affected_files(self, stdout: str) -> list[str]:
        """
        Extract affected file paths from Aider's unified diff output.

        Parses patterns:
            diff --git a/<path> b/<path>
            --- a/<path>
            +++ b/<path>

        Returns deduplicated, normalized list.
        """
        files: set[str] = set()

        # Pattern 1: diff --git a/path b/path
        for match in re.finditer(r'^diff\s+--git\s+a/(.+?)\s+b/(.+?)$', stdout, re.MULTILINE):
            files.add(match.group(1))

        # Pattern 2: --- a/path
        for match in re.finditer(r'^---\s+a/(.+?)$', stdout, re.MULTILINE):
            f = match.group(1)
            if f != '/dev/null':
                files.add(f)

        # Pattern 3: +++ b/path
        for match in re.finditer(r'^\+\+\+\s+b/(.+?)$', stdout, re.MULTILINE):
            f = match.group(1)
            if f != '/dev/null':
                files.add(f)

        return sorted(files)

    def _extract_file_diffs(self, stdout: str) -> dict[str, str]:
        """
        Split unified diff output into per-file fragments.

        Returns: {file_path: diff_snippet}
        """
        file_diffs: dict[str, str] = {}
        current_file: Optional[str] = None
        current_lines: list[str] = []

        for line in stdout.split('\n'):
            # Detect start of a new file diff
            file_match = re.match(r'^diff\s+--git\s+a/(.+?)\s+b/(.+?)$', line)
            if file_match:
                # Save previous file
                if current_file and current_lines:
                    file_diffs[current_file] = '\n'.join(current_lines)
                current_file = file_match.group(1)
                current_lines = [line]
                continue

            if current_file is not None:
                current_lines.append(line)

        # Save last file
        if current_file and current_lines:
            file_diffs[current_file] = '\n'.join(current_lines)

        return file_diffs

    def _count_diff_lines(self, stdout: str) -> tuple[int, int]:
        """
        Count added and removed lines in a unified diff.

        Added:   lines starting with '+' but not '+++'
        Removed: lines starting with '-' but not '---'
        """
        added = 0
        removed = 0

        for line in stdout.split('\n'):
            if line.startswith('+') and not line.startswith('+++'):
                added += 1
            elif line.startswith('-') and not line.startswith('---'):
                removed += 1

        return added, removed

    # ==================================================================
    # CORE METHOD: run_phase()
    # ==================================================================

    def run_phase(
        self,
        phase: str,
        template_vars: dict,
        files: Optional[list[str]] = None,
        model: Optional[str] = None,
        user_approved_plan: bool = False,
        task_files: Optional[list[str]] = None,
        declared_files: Optional[list[str]] = None,
        approved_dependencies: Optional[list[str]] = None,
        state: Optional[dict] = None,
        contracts: Optional[dict] = None,
        historical_diff: str = "",
    ) -> AiderResult:
        """
        Execute a complete phase: render template → pre-call gates →
        subprocess call → parse output → post-call audit.

        This is THE central method of loop-aider's Aider integration.
        Every phase execution flows through here.

        ┌─────────────────────────────────────────────────────────────────┐
        │                        run_phase() Pipeline                      │
        ├─────────────────────────────────────────────────────────────────┤
        │ 1. Determine template filename from phase name.                  │
        │ 2. Render Jinja2 template → rendered_prompt (string).            │
        │ 3. PhaseGuard.run_all_pre_call_gates(rendered_prompt, ...)       │
        │    ├─ G1 blocked? → raise PhaseBlockedError (hard stop)          │
        │    ├─ G2/G4/G5 paused? → raise PhasePausedError (needs user)    │
        │    └─ all passed → continue                                      │
        │ 4. Build Aider CLI command.                                      │
        │ 5. Execute subprocess (with retry).                              │
        │ 6. Parse stdout → AiderResult (affected_files, diffs, stats).    │
        │ 7. PhaseGuard.run_all_post_call_audits(aider_result, ...)        │
        │    └─ Attach audit issues/warnings to AiderResult.               │
        │ 8. Return AiderResult.                                           │
        └─────────────────────────────────────────────────────────────────┘

        Parameters:
        ───────────
        phase:                  Phase name, e.g. "part_2_2".
        template_vars:          Dict of variables for Jinja2 rendering.
                                Must include at minimum: goal, phase, cycle.
        files:                  List of file paths to pass to Aider as context.
                                None or [] means Aider works on the whole repo.
        model:                  Override the default model for this call.
        user_approved_plan:     Has the user confirmed the solution? (for G2 gate)
        task_files:             Files expected to be modified per task plan. (for A2 audit)
        declared_files:         Files declared for modification. (for G5 gate)
        approved_dependencies:  List of pre-approved dependency packages. (for G3 gate)
        state:                  The full state.json dict. (for G2 gate context)
        contracts:              phase_contracts dict. (for A1/A4 audit)
                                If None, extracted from state.
        historical_diff:        Accumulated diff from prior cycles as string.
                                (for A5 regression detection)

        Returns:
        ───────
        AiderResult with all fields populated, including:
            - stdout/stderr/exit_code/duration_ms from subprocess
            - affected_files/file_diffs/added_lines/removed_lines from parsing
            - audit_issues (attached as an extra attribute for routing)

        Raises:
        ──────
        PhaseBlockedError:  A Pre-call Gate hard-blocked execution.
        PhasePausedError:   A Pre-call Gate requires user confirmation.
        RuntimeError:       Template rendering failed or Aider not healthy.
        subprocess.TimeoutExpired: Aider exceeded timeout and retries exhausted.
        """
        start_time = time.monotonic()

        # ------------------------------------------------------------------
        # Step 0: Pre-flight checks
        # ------------------------------------------------------------------
        health = self.check_health()
        if health == HealthStatus.INCOMPATIBLE:
            raise RuntimeError(
                f"Aider version {self.get_version()} is incompatible. "
                f"loop-aider requires Aider >= 0.77.0. "
                f"Please upgrade: pip install --upgrade aider-chat"
            )
        if health == HealthStatus.NOT_FOUND:
            raise RuntimeError(
                "Aider CLI not found. Please install Aider: "
                "pip install aider-chat"
            )

        # ------------------------------------------------------------------
        # Step 1: Determine template and render prompt
        # ------------------------------------------------------------------
        template_name = self.get_template_for_phase(phase)
        template_vars.setdefault("phase", phase)
        template_vars.setdefault("cycle", state.get("progress", {}).get("cycle", 1)
                                 if state else 1)

        self.logger.info(
            "run_phase: phase=%s cycle=%d template=%s",
            phase, template_vars.get("cycle", "?"), template_name
        )

        try:
            rendered_prompt = self.render_template(template_name, template_vars)
        except FileNotFoundError as exc:
            self.logger.error("Template not found: %s", exc)
            raise
        except RuntimeError as exc:
            self.logger.error("Template rendering failed: %s", exc)
            raise

        self.logger.debug(
            "Rendered prompt for phase '%s': %d characters", phase, len(rendered_prompt)
        )

        # ------------------------------------------------------------------
        # Step 2: Pre-call Gates
        # ------------------------------------------------------------------
        self._init_phase_guard()

        # Resolve contracts from state if not explicitly provided
        if contracts is None and state is not None:
            contracts = (
                state.get("phase_contracts", {}).get("contracts", {})
            )

        gate_results = self._phase_guard.run_all_pre_call_gates(
            prompt=rendered_prompt,
            phase=phase,
            user_approved_plan=user_approved_plan,
            task_files=task_files,
            declared_files=declared_files,
            approved_dependencies=approved_dependencies,
            state=state,
        )

        # Check for blocked gates
        blocked = {k: v for k, v in gate_results.items() if v.blocked}
        if blocked:
            from .phase_guard import PhaseBlockedError
            self.logger.error(
                "Pre-call gates BLOCKED phase %s: %s",
                phase, list(blocked.keys())
            )
            raise PhaseBlockedError(gate_results)

        # Check for paused gates
        paused = {k: v for k, v in gate_results.items() if v.paused}
        if paused:
            from .phase_guard import PhasePausedError
            self.logger.info(
                "Pre-call gates PAUSED phase %s: %s — requires user confirmation",
                phase, list(paused.keys())
            )
            raise PhasePausedError(gate_results)

        self.logger.info(
            "All pre-call gates PASSED for phase %s", phase
        )

        # ------------------------------------------------------------------
        # Step 3: Build command
        # ------------------------------------------------------------------
        effective_model = model or self.model or template_vars.get("model")
        cmd = self._build_command(
            message=rendered_prompt,
            files=files,
            model=effective_model,
        )
        cmd_str = self._build_cmd_string(cmd)
        self.logger.info(
            "Aider command (%d args, ~%d chars): %s",
            len(cmd), len(cmd_str), cmd_str[:200] + ("..." if len(cmd_str) > 200 else "")
        )

        # ------------------------------------------------------------------
        # Step 4: Execute subprocess (with retry)
        # ------------------------------------------------------------------
        aider_result: Optional[AiderResult] = None
        last_error: Optional[Exception] = None
        retry_count = self.retry_count

        for attempt in range(retry_count + 1):
            if attempt > 0:
                wait_seconds = min(5 * (2 ** (attempt - 1)), 60)
                self.logger.warning(
                    "Retrying Aider call (attempt %d/%d) after %ds...",
                    attempt + 1, retry_count + 1, wait_seconds
                )
                time.sleep(wait_seconds)

            try:
                subprocess_result = self._execute_subprocess(
                    cmd=cmd,
                    timeout=self.timeout,
                    cwd=template_vars.get("cwd"),  # optional working directory override
                )
            except subprocess.TimeoutExpired as exc:
                last_error = exc
                self.logger.error(
                    "Aider timed out after %ds (attempt %d/%d)",
                    self.timeout, attempt + 1, retry_count + 1
                )
                continue
            except Exception as exc:
                last_error = exc
                self.logger.error(
                    "Aider subprocess failed (attempt %d/%d): %s",
                    attempt + 1, retry_count + 1, exc
                )
                continue

            # Parse output
            aider_result = self._parse_output(subprocess_result)
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            aider_result.duration_ms = elapsed_ms
            aider_result.phase = phase
            aider_result.cycle = template_vars.get("cycle", 0)

            if aider_result.success:
                break  # Success — no more retries needed
            else:
                last_error = RuntimeError(
                    f"Aider exited with code {aider_result.exit_code}"
                )
                self.logger.warning(
                    "Aider returned non-zero exit code %d (attempt %d/%d)",
                    aider_result.exit_code, attempt + 1, retry_count + 1
                )
                # Continue retrying if attempts remain

        # If all attempts failed, return the last result (or a timeout marker)
        if aider_result is None:
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            aider_result = AiderResult(
                stdout="",
                stderr=str(last_error) if last_error else "",
                exit_code=-1,
                duration_ms=elapsed_ms,
                timed_out=isinstance(last_error, subprocess.TimeoutExpired),
                phase=phase,
                cycle=template_vars.get("cycle", 0),
                errors=[f"Aider execution failed after {retry_count + 1} attempt(s): {last_error}"],
            )

        self.logger.info(
            "Aider completed: exit_code=%d duration_ms=%d affected_files=%d "
            "added=%d removed=%d",
            aider_result.exit_code, aider_result.duration_ms,
            len(aider_result.affected_files),
            aider_result.added_lines, aider_result.removed_lines,
        )

        # ------------------------------------------------------------------
        # Step 5: Post-call Diff Audit
        # ------------------------------------------------------------------
        if contracts:
            audit_result = self._phase_guard.run_all_post_call_audits(
                aider_result=aider_result,
                phase=phase,
                contracts=contracts,
                task_files=task_files,
                historical_diff=historical_diff,
                artifacts_dir=template_vars.get(
                    "artifacts_dir",
                    ".aider/loop-aider/artifacts"
                ),
            )

            # Attach audit results to the AiderResult for downstream consumption
            aider_result.audit_passed = audit_result.passed
            aider_result.audit_issues = audit_result.issues
            aider_result.audit_warnings = audit_result.warnings

            if not audit_result.passed:
                p0_count = sum(1 for i in audit_result.issues if i.severity == "P0")
                p1_count = sum(1 for i in audit_result.issues if i.severity == "P1")
                self.logger.warning(
                    "Post-call audit FAILED: %d P0 issue(s), %d P1 issue(s), "
                    "%d P2 issue(s)",
                    p0_count, p1_count,
                    sum(1 for i in audit_result.issues if i.severity == "P2")
                )
            else:
                self.logger.info("Post-call audit PASSED")
        else:
            self.logger.warning(
                "No phase contracts provided — skipping post-call audit"
            )

        # ------------------------------------------------------------------
        # Step 6: Return result
        # ------------------------------------------------------------------
        self.logger.info(
            "run_phase complete: phase=%s duration=%dms success=%s",
            phase, aider_result.duration_ms, aider_result.success
        )
        return aider_result

    # ==================================================================
    # Convenience / Utility Methods
    # ==================================================================

    def get_template_vars(self, base_vars: dict) -> dict:
        """
        Return a complete template_vars dict with defaults filled in.

        Base variables typically come from state.json fields.
        This method fills in standard defaults.
        """
        defaults = {
            "goal": "",
            "existing_code": "",
            "context_summary": "",
            "phase": "",
            "cycle": 1,
            "model": self.model or "default",
            "aider_version": self.get_version(),
            "mode": self.mode,
        }
        merged = {**defaults, **base_vars}
        return merged

    def inspect_last_result(self, result: AiderResult) -> str:
        """
        Produce a human-readable summary of an AiderResult for logging/display.
        """
        lines = [
            f"=== Aider Result: {result.phase} (cycle {result.cycle}) ===",
            f"  Exit code:   {result.exit_code}",
            f"  Duration:    {result.duration_ms}ms ({result.duration_ms / 1000:.1f}s)",
            f"  Timed out:   {result.timed_out}",
            f"  Files modified: {len(result.affected_files)}",
        ]
        for f in result.affected_files:
            lines.append(f"    - {f}")
        lines.append(f"  Lines: +{result.added_lines} / -{result.removed_lines}")
        lines.append(f"  Models: {', '.join(result.models_used) if result.models_used else 'unknown'}")
        lines.append(f"  Tokens: {result.tokens_used or 'unknown'}")
        if result.warnings:
            lines.append(f"  Warnings ({len(result.warnings)}):")
            for w in result.warnings[:5]:
                lines.append(f"    - {w}")
            if len(result.warnings) > 5:
                lines.append(f"    ... and {len(result.warnings) - 5} more")
        if result.errors:
            lines.append(f"  Errors ({len(result.errors)}):")
            for e in result.errors[:5]:
                lines.append(f"    - {e}")
            if len(result.errors) > 5:
                lines.append(f"    ... and {len(result.errors) - 5} more")
        if hasattr(result, 'audit_passed'):
            lines.append(f"  Audit: {'PASSED' if result.audit_passed else 'FAILED'}")
            if hasattr(result, 'audit_issues') and result.audit_issues:
                lines.append(f"    Issues: {len(result.audit_issues)}")
        lines.append("=" * 50)
        return '\n'.join(lines)
