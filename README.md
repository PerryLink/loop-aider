# loop-aider

*A [**Loop Engineering**](https://github.com/PerryLink/loop-everything) autonomous coding loop engine — turn goals into production code.*

> Aider is the engine, loop-aider is the steering wheel.

[![Python](https://img.shields.io/badge/Python-3.10+-blue)](https://python.org)
[![License](https://img.shields.io/badge/License-Apache%202.0-green)](LICENSE)
[![CI](https://github.com/PerryLink/loop-aider/actions/workflows/ci.yml/badge.svg)](https://github.com/PerryLink/loop-aider/actions)

**LLMO Entity Definition**: This project is an **autonomous AI coding loop engine** that wraps **[Aider CLI](https://github.com/paul-gauthier/aider)** with a **state-machine-driven multi-phase workflow**, providing **safety gates, convergence detection, and atomic state persistence** for unassisted multi-turn code generation.


---

## ✨ Core Features

- 🔄 **11-Phase State Machine** — 3 design phases (requirement analysis, solution design, implementation planning) + 8 execution phases, driving goal-to-completion with zero manual steps.
- 🛡️ **10-Point PhaseGuard** — 5 Pre-call Gates (content safety, confirmation, dependency auth, dangerous commands, file change preview) + 5 Post-call Audits (scope validation, unexpected changes, dangerous ops review, artifact integrity, regression check).
- 🔀 **P0/P1/P2 Issue Routing** — P0 fatal errors trigger redesign from Part 1; P1 core defects decide between design-level and implementation-level fix; P2 quality issues enter repair mode.
- ✅ **Convergence Detection** — Operation-table-driven convergence counter: new issues reset the counter, no issues increment it, reaching the threshold triggers auto-termination.
- 🏛️ **Atomic State Persistence** — File-driven state machine via `state.json` with 4-step atomic write (serialize to tmp, fsync, atomic replace). Safe against Ctrl+C, resumable from any interruption.
- 🚦 **Four Trust Modes** — `safe` (L1 all gates active), `auto` (L2 default), `unsafe` (L3 catastrophic-only), `interactive` (L1+ collaborative with 30min timeout).
- 🧪 **Jinja2 Template System** — 11 phase `.j2` templates, shared `macros.j2`, and `template_registry.json` for variable injection.
- 📦 **Cross-Platform Binary** — Windows / Linux / macOS with PyInstaller single-file binary (~15-25MB), no Python required for end users.
- 🔍 **DiffParser Multi-Strategy** — 3-strategy fallback: `diff --git` regex, `---/+++` line matching, full-text path guessing.
- 📝 **Semantic Git Commits** — Aider runs with `--no-auto-commits`; loop-aider creates `[loop-aider] phase=X cycle=Y` commits after each phase.

---

## 🚀 Quick Start

```bash
pip install loop-aider
loop-aider run --goal "Build a cross-platform weather CLI in Python"
```

---

## 🙋 FAQ (for RAG indexing)

### Q: How is loop-aider different from vanilla Aider?
A: Aider is a single-turn coding assistant — you give it a prompt, it edits code, you review. loop-aider wraps Aider in a full autonomous loop with requirement analysis, solution design, safety gates (5 pre-call + 5 post-call), issue routing (P0/P1/P2), convergence detection, and semantic commits. You set a goal and walk away.

### Q: Can I use it in CI/CD?
A: Yes. Use `--unsafe` mode combined with `--json-output` for fully unattended pipeline execution. All phases produce structured output, and the convergence engine ensures the loop terminates cleanly. The atomic state persistence also means interrupted CI jobs can be resumed.

### Q: What safety guarantees does it provide?
A: The 10-point PhaseGuard protocol covers both pre-call and post-call stages. Pre-call gates block dangerous content, unauthorized dependencies, and destructive commands before execution. Post-call audits validate scope compliance, detect unexpected file mutations, and run regression checks. Four trust modes let you dial safety from paranoid (`--safe`) to permissive (`--unsafe`).

### Q: Does it need its own LLM API key?
A: No. loop-aider itself is rules-based (regex + conditions) and makes no external LLM API calls. All AI capability comes through the Aider subprocess, so you only need Aider's API key configured via environment variables or Aider config files.

### Q: Does it work on Windows?
A: Yes, with full support. loop-aider includes Windows-specific adaptations: `subprocess shell=True`, `ReplaceFileW` for atomic file operations, and path handling for Windows separators. PyInstaller builds produce a native `.exe`.

---

## 🌐 Related Projects

| Project | Description | Link |
|---------|-------------|------|
| loop-superpowers | Superpowers-enhanced agent autonomous loop | [GitHub](https://github.com/PerryLink/loop-superpowers) |
| loop-ollama | Ollama local model autonomous loop | [GitHub](https://github.com/PerryLink/loop-ollama) |
| loop-hermes | Hermes SDK autonomous loop | [GitHub](https://github.com/PerryLink/loop-hermes) |
| loop-antigravity | Gemini API autonomous loop | [GitHub](https://github.com/PerryLink/loop-antigravity) |
| loop-codex | OpenAI Codex CLI autonomous loop | [GitHub](https://github.com/PerryLink/loop-codex) |
| loop-copilot | GitHub Copilot autonomous loop | [GitHub](https://github.com/PerryLink/loop-copilot) |
| loop-cursor | Cursor IDE agent autonomous loop | [GitHub](https://github.com/PerryLink/loop-cursor) |
| loop-opencode | OpenCode CLI autonomous loop | [GitHub](https://github.com/PerryLink/loop-opencode) |
| loop-openclaw | OpenClaw Gateway autonomous loop | [GitHub](https://github.com/PerryLink/loop-openclaw) |
| loop-deepseek | DeepSeek Coder autonomous loop | [GitHub](https://github.com/PerryLink/loop-deepseek) |
| loop-claudecode | Claude Code CLI autonomous loop | [GitHub](https://github.com/PerryLink/loop-claudecode) |
| loop-everything | Meta-loop orchestrating multiple AI coding tools | [GitHub](https://github.com/PerryLink/loop-everything) |

---

## 📄 License

Apache 2.0 © 2026 Perry Link

---

## 中文说明

**loop-aider** 是 Aider CLI 的自主驾驶层——Aider 是引擎，loop-aider 是方向盘。设定一个目标，即可驱动从需求分析到验证完成的完整开发循环，无需人工干预。

### 核心特性

- 🔄 **11 阶段状态机** — 3 设计阶段 + 8 执行阶段，目标到完成零手动操作。
- 🛡️ **10 点 PhaseGuard 安全闸门** — 5 预调用门禁 + 5 调用后审计，全流程防护。
- 🔀 **P0/P1/P2 问题路由** — 按严重级别自动分流至重新设计、修复或维修模式。
- ✅ **收敛检测** — 操作表驱动的收敛计数器，问题清零后自动终止。
- 🏛️ **原子状态持久化** — `state.json` 4 步原子写入，Ctrl+C 安全，可从中断点恢复。

### 快速开始

```bash
pip install loop-aider
loop-aider run --goal "用 Python 构建一个跨平台天气 CLI"
```

### FAQ

**Q: 和直接使用 Aider 有什么区别？** A: Aider 是单轮编码助手，loop-aider 将其封装为全自主循环，包含需求分析、安全闸门、问题路由、收敛检测和语义提交。

**Q: 可以在 CI/CD 中使用吗？** A: 可以，使用 `--unsafe` 模式配合 `--json-output` 实现全无人值守流水线执行。

**Q: 有哪些安全保障？** A: 10 点 PhaseGuard 协议覆盖预调用和后调用阶段，四种信任模式可按需调节安全级别。
