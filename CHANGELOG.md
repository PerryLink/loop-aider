# Changelog

All notable changes to loop-aider will be documented in this file.

---

## [v0.1.0] — 2026-06-13

### 新增 (Added)

#### 核心架构
- **11 阶段工作流** — Part 1 需求分析（3 阶段）+ Part 2 实施验证（8 阶段）
- **文件驱动状态机** — state.json 原子读写 + JSON 备份 + 锁文件并发保护
- **完整调度器** — 串联所有模块实现自主闭环循环
- **Jinja2 模板系统** — 按 phase 渲染 Aider prompt 模板

#### 安全协议
- **5 道 Pre-call Gates**:
  - G1 内容安全门 — prompt 恶意内容扫描（全模式硬拦截）
  - G2 计划确认门 — Part 1→Part 2 过渡确认
  - G3 依赖安装门 — pip/npm 等安装拦截审核
  - G4 危险操作门 — 五层检测（L0 灾难/L1 不可逆/L2 高影响/L3 Shell逃逸/L4 路径保护）
  - G5 文件变更门 — 变更范围预览和确认
- **5 道 Post-call Audits**:
  - A1 输出有效性审计 — 检查 Aider 是否产出实质性代码
  - A2 文件变更审计 — 对比任务预期 vs 实际变更
  - A3 stop_signal 审计 — 检查阶段完成信号
  - A4 banned_behaviors 审计 — 后门/漏洞/越权等 13 项禁止行为检测
  - A5 整体合规审计 — 汇总评级
- **四种信任模式** — safe (L1) / auto (L2) / unsafe (L3) / interactive (L1+)
- **PhaseBlockedError / PhasePausedError** — Gate 异常类型

#### 路由系统
- **三层路由决策**:
  - P0 检测 — 致命设计问题 → Part 1 回退
  - P1 决策树 — 5 正面条件 + 4 否定条件 → REPEAT_PHASE
  - P2 检测 — 实施级问题 → 自动修复
- **RoutingDecision** 数据类 — 完整的路由决策结果封装
- **路由历史追踪** — 重复检测和上限保护

#### 收敛引擎
- **convergence_counter 操作表** — P5/P6 合并规则
- **Part 1 语义收敛** — 三阶段产物 + 无设计问题判定
- **优先级操作表** — 8 种状态自动匹配操作
- **should_terminate()** — 综合终止判定（收敛/超限/P0/手动）

#### 修复系统
- **RepairContext 生命周期** — null → active → consumed
- **自动修复策略生成** — 按问题类型匹配修复方案
- **并行修复支持** — 多问题批次拆分
- **修复重试机制** — 自定义尝试次数上限

#### CLI 入口
- `run` — 启动自主循环（支持 --goal / --model / --safe / --max-cycles 等参数）
- `status` — 查看 state.json 状态摘要
- `resume` — 从中断状态恢复
- `init` — 初始化工作目录

#### 跨平台支持
- **Windows** — shell=True subprocess + ReplaceFileW 原子文件操作
- **Linux/macOS** — shell=False 直接执行 + os.replace 原子重命名
- **文件锁** — O_CREAT|O_EXCL + 指数退避 + 死锁超时检测
- **进程树终止** — taskkill (Win) / SIGTERM (POSIX)

#### 构建与分发
- **PyInstaller spec** — 单文件编译 + hidden imports + 模板数据打包
- **build.py** — 跨平台构建自动化脚本（编译 + 清理 + 验证）
- **GitHub Actions CI** — 三平台矩阵（ubuntu/macos/windows）+ 测试 + 构建 + Artifact

#### 测试
- **M1-M4 单元测试** — 243 个测试用例覆盖所有核心模块
- **M5 集成测试** — 8 个 Golden Test 场景覆盖关键路径

---

### 变更 (Changed)

- 初始发布版本，无历史变更。

---

### 已修复 (Fixed)

- 初始发布版本，无历史修复记录。

---

### 已知限制 (Known Limitations)

- Aider 版本要求 >= 0.77.0（推荐 >= 0.86.0 获得完整兼容性）
- prompt_templates 目录需基础模板文件方可实现完整特性
- 修复系统目前仅支持 P2 级别问题自动修复
- 大型项目（>100 文件）的收敛检测可能需要更长周期

---

[v0.1.0]: https://github.com/user/loop-aider/releases/tag/v0.1.0
