"""
loop_aider — Aider CLI 的自主驾驶层。

Aider 是引擎，loop-aider 是方向盘。
用户设定一个目标（goal），loop-aider 自主完成
"需求分析 → 方案设计 → 实施编码 → 测试验证 → 查漏补缺"
的全闭环流程。

Public API:
    cli.main()              — CLI 命令行入口点（run/status/resume/init）。
    Config                  — 全局配置数据类（四种信任模式）。
    StateMachine            — 文件驱动阶段状态机（state.json 原子读写）。
    AiderManager            — Aider subprocess 封装管理器。
    PhaseGuard              — 5 Pre-call Gates + 5 Post-call Audits 安全协议（phase_guard.py / guard.py）。
    GitManager              — Git 仓库管理器（语义化提交、diff、变更追踪）。
    FileLock                — 跨平台文件锁并发保护。
    Router                  — P0/P1/P2 三层路由决策系统（router.py）。
    ConvergenceEngine       — 收敛引擎——追踪并判定循环终止（convergence.py）。
    RepairManager           — 修复管理器——P2 问题局部修复（repair.py）。
    Scheduler               — 完整调度器——串联所有模块自主闭环（scheduler.py）。
"""

__version__ = "0.2.0"
__author__ = "loop-aider contributors"
