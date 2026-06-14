# loop-aider — Autonomous Driving Layer for Aider CLI

**Aider is the engine, loop-aider is the steering wheel.** Set a goal, walk away. It drives the full dev loop — requirements to verified completion — hands-free.

[![Python](https://img.shields.io/badge/python-%3E%3D3.10-blue)](https://www.python.org/downloads/)
[![Version](https://img.shields.io/badge/version-0.2.0-blue)](https://github.com/PerryLink/loop-aider/releases)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

[English](#english) | [中文](#中文)

**An autonomous loop that drives Aider CLI through end-to-end development — set a goal and walk away.**

---

## English

### Features

- **Full Autonomous Loop** — 11-phase workflow (3 design + 8 execution), goal-to-completion with zero manual steps
- **PhaseGuard Safety Protocol** — 5 Pre-call Gates (content safety, confirmation, dependency auth, dangerous commands, file change preview) + 5 Post-call Audits (scope validation, unexpected changes, dangerous ops review, artifact integrity, regression check)
- **P0/P1/P2 Routing** — P0 fatal → back to Part 1 redesign / P1 core defect → decide design-level vs implementation-level fix / P2 quality issue → repair mode
- **Convergence Engine** — Operation-table-driven convergence counter: new issues reset → counter, no issues +1, reaches threshold → auto-terminate
- **File-Driven State Machine** — `state.json` with 4-step atomic write (serialize→tmp→fsync→atomic replace), Ctrl+C safe, `resume` from any interruption
- **Four Trust Modes** — safe (L1 all gates) / auto (L2 default) / unsafe (L3 catastrophic-only) / interactive (L1+ collaborative with 30min timeout)
- **Jinja2 Template System** — 11 phase `.j2` templates + `macros.j2` shared macros + `template_registry.json` for variable injection
- **Cross-Platform** — Windows / Linux / macOS with PyInstaller single-file binary (~15-25MB), no Python required for end users
- **DiffParser Multi-Strategy** — 3-strategy fallback: `diff --git` regex → `---/+++` line matching → full-text path guessing
- **Semantic Git Commits** — Aider uses `--no-auto-commits`, loop-aider creates `[loop-aider] phase=X cycle=Y` commits after each phase

---

### Quick Start

#### Prerequisites

- **Python** >= 3.10
- **Aider CLI** >= 0.77.0 (>= 0.86.0 recommended)
- **Git** >= 2.30

#### Install

```bash
git clone https://github.com/PerryLink/loop-aider.git
cd loop-aider
pip install -r requirements.txt

# Ensure Aider is available
pip install aider-chat
aider --version   # Confirm >= 0.77.0
```

#### Run

```bash
# Initialize workspace
python -m loop_aider.cli init

# Start autonomous loop with a natural-language goal
python -m loop_aider.cli run --goal "Build a cross-platform weather CLI in Python"

# Specify model and max cycles
python -m loop_aider.cli run --goal "..." --model sonnet --max-cycles 10

# Check current status
python -m loop_aider.cli status

# Resume from interruption
python -m loop_aider.cli resume
```

### Trust Modes

| Mode | Flag | G1 | G2 | G3 | G4 | G5 |
|------|------|----|----|----|----|----|
| **safe** | `--safe` | Block | Pause | Block | Block | Pause |
| **auto** | default | Block | Pass | Block | Partial | Pass |
| **unsafe** | `--unsafe` | Block | Skip | Pass | Catastrophic | Skip |
| **interactive** | `--interactive` | Block | Pause(timeout) | Block | Block | Pause |

```bash
# Safety mode — all gates active
python -m loop_aider.cli run --goal "..." --safe

# Interactive mode — key decisions pause for confirmation (30min timeout)
python -m loop_aider.cli run --goal "..." --interactive

# Unsafe mode — only catastrophic operations blocked
python -m loop_aider.cli run --goal "..." --unsafe
```

### Advanced Configuration

Override defaults in `.aider/loop-aider/config.yml`:

```yaml
mode: auto
max_cycles: 10
convergence_rounds: 3
aider_timeout_seconds: 900
```

### CLI Reference

```
python -m loop_aider.cli <command> [OPTIONS]

Commands:
  init      Initialize workspace and create state file
  run       Start autonomous loop with a goal
  status    Show current loop status
  resume    Resume from last interruption

Options:
  --goal TEXT           Natural-language goal description
  --model MODEL         Aider model (e.g., sonnet, opus, gpt-4o)
  --max-cycles N        Maximum loop cycles (default: 5)
  --convergence-rounds N  Rounds needed for convergence (default: 2)
  --safe / --auto / --unsafe / --interactive
  --no-pause            Don't pause for user confirmation
  --json-output         Output results as JSON
```

---

### FAQ

**Q: How is loop-aider different from running Aider directly?**
A: Aider is a single-turn coding assistant — you give it a prompt, it edits code, you review. loop-aider wraps Aider in a full autonomous loop with requirement analysis, solution design, safety gates (5 pre-call + 5 post-call), issue routing (P0/P1/P2), convergence detection, and semantic commits. You set a goal and walk away.

**Q: Does loop-aider need its own LLM API key?**
A: No. loop-aider itself is rules-based (regex + conditions) — it makes no external LLM API calls. All AI capability comes through the Aider subprocess, so you only need to configure Aider's API key via environment variables or Aider config files.

**Q: Will it make unwanted changes to my repo?**
A: In `--safe` mode, every major decision pauses for your approval. In `--auto` mode (default), only dangerous operations trigger a pause. All changes are committed with semantic messages per phase, so you can always `git log` and revert. The PhaseGuard post-call audits also detect unexpected file changes.

**Q: Does it work on Windows?**
A: Yes, with full support. loop-aider includes Windows-specific adaptations: `subprocess shell=True`, `ReplaceFileW` for atomic file operations, and path handling for Windows separators. PyInstaller builds produce a native `.exe`.

**Q: What Aider version do I need?**
A: >= 0.77.0 minimum, >= 0.86.0 recommended for full feature support. On startup, loop-aider runs `aider --version` and refuses to start if the version is too old, printing upgrade instructions.

---

### Related Projects

- **[loop-hermes](https://github.com/PerryLink/loop-hermes)** — Autonomous development loop for Hermes Agent (SDK-based)
- **[loop-claudecode](https://github.com/PerryLink/loop-claudecode)** — Autonomous loop for Claude Code CLI
- **[loop-copilot](https://github.com/PerryLink/loop-copilot)** — Autonomous loop for GitHub Copilot
- **[loop-cursor](https://github.com/PerryLink/loop-cursor)** — Autonomous loop for Cursor IDE agent
- **[loop-deepseek](https://github.com/PerryLink/loop-deepseek)** — Autonomous loop for DeepSeek Coder
- **[loop-opencode](https://github.com/PerryLink/loop-opencode)** — Autonomous loop for OpenCode CLI
- **[loop-codex](https://github.com/PerryLink/loop-codex)** — Autonomous loop for OpenAI Codex CLI
- **[loop-ollama](https://github.com/PerryLink/loop-ollama)** — Autonomous loop for Ollama local models
- **[loop-superpowers](https://github.com/PerryLink/loop-superpowers)** — Autonomous loop for Superpowers-enhanced agents
- **[loop-everything](https://github.com/PerryLink/loop-everything)** — Meta-loop orchestrating multiple AI coding tools

---

## 中文

**loop-aider** 是 Aider CLI 的自主驾驶层——Aider 是引擎，loop-aider 是方向盘。设定一个目标，它就能驱动完成从需求分析到验证完成的完整开发循环。

### 功能特性

- **全自主循环** — 11 阶段工作流（3 设计 + 8 执行），目标到完成零手动操作
- **PhaseGuard 安全协议** — 5 个预调用门禁（内容安全、确认、依赖认证、危险命令、文件变更预览）+ 5 个调用后审计（范围验证、意外变更、危险操作审查、产物完整性、回归检查）
- **P0/P1/P2 路由** — P0 致命→回退 Part 1 重新设计 / P1 核心缺陷→决定设计级或实现级修复 / P2 质量问题→修复模式
- **收敛引擎** — 操作表驱动的收敛计数器：新问题出现→重置计数器，无问题+1，达到阈值→自动终止
- **文件驱动状态机** — `state.json` 带 4 步原子写入，Ctrl+C 安全，可从任意中断点 `resume`
- **四种信任模式** — safe（L1 全部门禁）/ auto（L2 默认）/ unsafe（L3 仅灾难性）/ interactive（L1+ 协作模式，30 分钟超时）
- **Jinja2 模板系统** — 11 个阶段 `.j2` 模板 + `macros.j2` 共享宏 + `template_registry.json` 变量注入
- **跨平台** — Windows / Linux / macOS，PyInstaller 单文件二进制 (~15-25MB)，终端用户无需安装 Python
- **DiffParser 多策略** — 3 策略回退：`diff --git` 正则 → `---/+++` 行匹配 → 全文路径猜测
- **语义化 Git 提交** — Aider 使用 `--no-auto-commits`，loop-aider 在每个阶段后创建 `[loop-aider] phase=X cycle=Y` 提交

---

### 快速开始

#### 环境要求

- **Python** >= 3.10
- **Aider CLI** >= 0.77.0（推荐 >= 0.86.0）
- **Git** >= 2.30

#### 安装

```bash
git clone https://github.com/PerryLink/loop-aider.git
cd loop-aider
pip install -r requirements.txt

# 确保 Aider 可用
pip install aider-chat
aider --version   # 确认 >= 0.77.0
```

#### 运行

```bash
# 初始化工作区
python -m loop_aider.cli init

# 使用自然语言目标启动自主循环
python -m loop_aider.cli run --goal "用 Python 构建一个跨平台天气 CLI"

# 指定模型和最大循环数
python -m loop_aider.cli run --goal "..." --model sonnet --max-cycles 10

# 查看当前状态
python -m loop_aider.cli status

# 从中断处恢复
python -m loop_aider.cli resume
```

### 信任模式

| 模式 | 参数 | G1 | G2 | G3 | G4 | G5 |
|------|------|----|----|----|----|----|
| **safe** | `--safe` | 阻止 | 暂停 | 阻止 | 阻止 | 暂停 |
| **auto** | 默认 | 阻止 | 通过 | 阻止 | 部分 | 通过 |
| **unsafe** | `--unsafe` | 阻止 | 跳过 | 通过 | 仅灾难性 | 跳过 |
| **interactive** | `--interactive` | 阻止 | 暂停(超时) | 阻止 | 阻止 | 暂停 |

```bash
# 安全模式 — 所有门禁生效
python -m loop_aider.cli run --goal "..." --safe

# 交互模式 — 关键决策暂停等待确认（30 分钟超时）
python -m loop_aider.cli run --goal "..." --interactive

# 不安全模式 — 仅阻止灾难性操作
python -m loop_aider.cli run --goal "..." --unsafe
```

### 高级配置

在 `.aider/loop-aider/config.yml` 中覆盖默认值：

```yaml
mode: auto
max_cycles: 10
convergence_rounds: 3
aider_timeout_seconds: 900
```

### CLI 参考

```
python -m loop_aider.cli <命令> [选项]

命令:
  init      初始化工作区并创建状态文件
  run       使用目标启动自主循环
  status    显示当前循环状态
  resume    从上次中断处恢复

选项:
  --goal TEXT           自然语言目标描述
  --model MODEL         Aider 模型（如 sonnet, opus, gpt-4o）
  --max-cycles N        最大循环次数（默认: 5）
  --convergence-rounds N  收敛所需轮数（默认: 2）
  --safe / --auto / --unsafe / --interactive
  --no-pause            不暂停等待用户确认
  --json-output         以 JSON 格式输出结果
```

---

### 常见问题

**Q: loop-aider 和直接运行 Aider 有什么不同？**
A: Aider 是单轮编程助手——你给它一个提示，它编辑代码，你审查。loop-aider 将 Aider 封装在一个完整的自主循环中，包含需求分析、方案设计、安全门禁（5 预调用 + 5 调用后）、问题路由（P0/P1/P2）、收敛检测和语义化提交。你设定目标后即可离开。

**Q: loop-aider 需要自己的 LLM API 密钥吗？**
A: 不需要。loop-aider 本身是基于规则的（正则 + 条件判断）——它不进行外部 LLM API 调用。所有 AI 能力通过 Aider 子进程提供，你只需要通过环境变量或 Aider 配置文件配置 Aider 的 API 密钥。

**Q: 它会对我的仓库进行不必要的更改吗？**
A: 在 `--safe` 模式下，每个重大决定都会暂停等待你的批准。在 `--auto` 模式（默认）下，只有危险操作会触发暂停。所有更改都会按阶段以语义化消息提交，因此你可以随时 `git log` 并回滚。PhaseGuard 调用后审计也会检测意外的文件变更。

**Q: 它在 Windows 上能工作吗？**
A: 可以，完全支持。loop-aider 包含 Windows 特定适配：`subprocess shell=True`、用于原子文件操作的 `ReplaceFileW`，以及 Windows 路径分隔符处理。PyInstaller 构建可生成原生 `.exe` 文件。

**Q: 我需要哪个版本的 Aider？**
A: 最低 >= 0.77.0，推荐 >= 0.86.0 以获得完整功能支持。启动时，loop-aider 会运行 `aider --version`，如果版本过旧则拒绝启动并打印升级说明。

---

### 相关项目

- **[loop-hermes](https://github.com/PerryLink/loop-hermes)** — Hermes Agent 自主开发循环（基于 SDK）
- **[loop-claudecode](https://github.com/PerryLink/loop-claudecode)** — Claude Code CLI 自主循环
- **[loop-copilot](https://github.com/PerryLink/loop-copilot)** — GitHub Copilot 自主循环
- **[loop-cursor](https://github.com/PerryLink/loop-cursor)** — Cursor IDE 代理自主循环
- **[loop-deepseek](https://github.com/PerryLink/loop-deepseek)** — DeepSeek Coder 自主循环
- **[loop-opencode](https://github.com/PerryLink/loop-opencode)** — OpenCode CLI 自主循环
- **[loop-codex](https://github.com/PerryLink/loop-codex)** — OpenAI Codex CLI 自主循环
- **[loop-ollama](https://github.com/PerryLink/loop-ollama)** — Ollama 本地模型自主循环
- **[loop-superpowers](https://github.com/PerryLink/loop-superpowers)** — Superpowers 增强代理自主循环
- **[loop-everything](https://github.com/PerryLink/loop-everything)** — 编排多个 AI 编程工具的元循环

---

## License

Apache License 2.0 — Copyright 2026 Perry Link.

See [LICENSE](LICENSE) for the full text.
