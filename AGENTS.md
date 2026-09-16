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

当前 P0.1-P0.4 已实现,P0-A 30 分钟常驻与 P0-C 强杀恢复已通过;下一步是
`docs/开发计划.md` 的 P0-B、P0-D、P0-E。Windows `service` 与 `doctor` 均为现有命令;
P0 尚未通过 Windows 重启、断网和 24 小时长稳,不得写成完整无人值守已验收。

真实 soak 运行的标识、时间和环境状态不记录在仓库中。运行期间不得停止/重启 service、
断网或重启 Windows；只用 `xianyu-agent soak status` 读取进度。查看
`docs/architecture.md` 与本文件顶部的“仓库结构速查”对齐。
每个 Phase 的验证证据记录在 `docs/验证记录.md`;测试套件为最新权威证据(`uv run pytest -q`)。
