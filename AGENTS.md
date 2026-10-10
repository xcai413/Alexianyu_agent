# 闲鱼运营 Agent 化项目 — Agent 协作指南

## 项目一句话

为 AI Agent 直接控制、可持续无人值守运行的轻量化闲鱼(Goofish)运营系统。

## 仓库结构速查

| 路径 | 用途 |
|------|------|
| `src/xianyu_agent/protocol/` | 闲鱼 WebSocket / mtop 协议层(纯 IO) |
| `src/xianyu_agent/domain/`   | 纯业务逻辑(无 IO,无 asyncio) |
| `src/xianyu_agent/services/` | 协调器:账号池、Worker、回复引擎、发货 |
| `src/xianyu_agent/cli/`      | Typer CLI — **单一事实源** |
| `src/xianyu_agent/mcp/`      | MCP Server(包装 CLI) |
| `src/xianyu_agent/tui/`      | Textual 驾驶舱 |
| `src/xianyu_agent/skills/`   | Codex Skill 文件 |
| `src/xianyu_agent/db/`       | SQLAlchemy 模型 + Alembic 迁移 |
| `tests/`                     | pytest 单测 + 集成测试 |
| `docs/`                      | 架构、协议笔记、API 参考 |
| `data/`                      | 运行时数据(SQLite、Cookie、卡密、日志) |

## 协作规则(必读)

### 一切基于事实

- 不要臆测闲鱼协议细节。先查 `docs/protocol-notes.md`,没有就明说没有,不要凭印象写代码。
- 改代码前先看现有实现;改完跑相关测试再交付。
- 任何"应该"、"大约"、"差不多"都不行,要么有测试,要么标 TODO 并附具体疑问。

### 中文优先

- 与用户对话、写 AGENTS.md / README / Skill 文件、commit message,默认使用简体中文。
- 代码标识符用英文。
- 用户/买家昵称、内容文案保持原始语言。

### 不要过度发散

- 严格按计划执行;阶段边界停下等用户 review。
- 不在没要求时新增功能、重命名、引入新依赖。
- 一个 PR/一次提交只做一件事。

### Cookie / Token 安全

- `.env` 不进 git;Cookie / Token 走 SQLite 加密列(Fernet)。
- 打印 / 日志 / commit message 中**绝不出现**完整 Cookie 或 Token 值;脱敏到前 8 位。

## 开发流程

### 环境

- Python >= 3.11(测试过 3.14)
- 包管理:uv(同步 `pyproject.toml`)
- Windows / PowerShell 优先

```powershell
uv sync                  # 装依赖 + 创建 .venv
uv run xianyu-agent --version
uv run pytest            # 跑测试
uv run ruff check .      # 静态检查
uv run ruff format .     # 格式化
```

### 数据库迁移

```powershell
# 改完 models.py 后:
uv run alembic revision --autogenerate -m "add foo table"
uv run alembic upgrade head
```

### 提交规范

```
<type>(<scope>): <subject>

<body>
```

- `feat` / `fix` / `refactor` / `test` / `docs` / `chore`
- subject 中文,动词开头,<= 50 字

## 当前阶段

**终极目标(用户 2026-09-19 定稿)**:人类与 Agent 共治的闲鱼运营系统,以可持续无人值守
运行为交付标准;Web 管理后台以 23Star/xianyu-super-butler 为基准(Issue #9);
无人值守期间 Agent 可通过 MCP 或 CLI 监控与介入。实现与既有规格冲突时,先登记到
docs/目标与冲突决策.md 由用户决策,不擅自翻既有非目标。

当前能力面(2026-09-21):P0.1-P0.4、发货引擎(Phase 6)、Web 控制台、多副本租约与容器化
M1–M5 均已实现;擦亮/求花的持久化调度、待人工处理队列、运维告警 webhook、运营分析、快捷回复与
买家备注也已落地。P0 尚未通过 Windows 重启(B4)、断网(B2)和 24 小时长稳(B5/D6),
不得写成完整无人值守已验收。

Agent 侧入口:`mcp serve` 用 `fastapi-mcp` 从同一个 FastAPI 应用生成 tools,因此 Web 管理端点
也会出现在 tools 列表里——但每条都要求管理员会话,没有会话一律 401/403(边界由
`tests/unit/test_mcp_surface.py` 与 `test_api_auth_boundary.py` 固化);`attention list` 汇总卡住等人的
事项,`automation resolve` 收尾结果不明的执行,`delivery job resolve` 收尾结果不明的发货并同步库存。
参考项目的功能移植进度与剩余项(商品侧改价/改库存待协议证据,评价买家、免拼免运费等有意不做)见
`docs/参考项目功能对齐矩阵.md`;真实验收的执行顺序见 `docs/待真实验证清单.md` 顶部。

真实 soak 运行的标识、时间和环境状态不记录在仓库中。运行期间不得停止/重启 service、
断网或重启 Windows；只用 `xianyu-agent soak status` 读取进度。查看
`docs/architecture.md` 与本文件顶部的“仓库结构速查”对齐。
每个 Phase 的验证证据记录在 `docs/验证记录.md`;测试套件为最新权威证据(`uv run pytest -q`)。

### 已完成:Issue #4 的 Phase 3(消息客服 / 自动回复 / AI)

账号级会话领域模型、人工接管与回复暂停、商品级与单次回复、回复决策审计与入站过滤、
可配置 Reply Pipeline、AI 提供商配置与密钥库装配、受限会话与商品上下文、有界重试,以及
AI 回复输出安全闸(`domain/ai/safety_policy.py`)均已实现。议价保护在
`domain/ai/bargain_policy.py`,轮次持久化在 `conversations.bargain_round`;Provider 契约统一为
`generate(AIRequest) -> AIResponse`,七类实现位于 `infrastructure/ai/providers/`,
`provider_type` 必须显式配置。真实入站消息校准仍属 P1,真实外发动作仍需明确授权。

### 进行中:参考项目功能移植与真实验收(工作分支 `issue-9-web-foundation`)

按 `docs/参考项目功能对齐矩阵.md` 逐项移植参考项目能力,代码侧已完成 58 轮(卡券与发卡源、
发货模板库、订单改价、备份、日志与多渠道通知、账号任务、商品级自动化、分析面板、图片消息与
已读上报、MCP/鉴权边界、人工收尾等)。对照表里已经没有"等排期"的行:剩余只有**待真机校准**
(见 `docs/待真实验证清单.md`)、**待协议证据**(商品侧改价/改库存:两个参考项目都没有公开接口)与
**有意不做**(发起退款、卡券图片、通用规则表、自动重试结果不明的外发、评价买家、免拼免运费、
闲鱼账号密码登录、多用户与角色、界面可写的系统设置、市场行情搜索、日志清空与本地数据批量修补),
每条理由写在矩阵对应行的决策列。

下一步是真实环境验收,按 `docs/待真实验证清单.md` 顶部的执行顺序进行;清单里所有真实动作
仍由用户执行并记录结论。P0-B、P0-D、P0-E 仍待真实环境验收,不因开发进度而改变。
