# xianyu-agent

> 为 AI Agent 直接控制、可持续无人值守运行的轻量化闲鱼(Goofish)运营系统。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](pyproject.toml)

## 这是什么

xianyu-agent 是一套**为 AI Agent 而设计**的闲鱼(Goofish / 闲鱼)账号自动化运营系统。
它不是给人用的 Web 后台,而是让 Codex / Claude / Cursor 这类 Agent 通过 MCP、CLI、
终端驾驶舱直接操控的运营工具。

核心定位:

- **多账号并行** — 账号池调度,3-10 个账号同时在线
- **三件套接口** — MCP Server + Typer CLI + Codex Skills
- **驾驶舱 TUI** — Textual 终端看板,实时看账号/消息/订单/库存
- **可插拔 AI** — 规则回复 / AI 回复可切换,OpenAI 兼容 API
- **自托管、轻量** — 单进程 Python + SQLite,不依赖 Docker、不依赖 MySQL

## 不用做什么

本项目**明确不做**(避免蔓延):

- 滑块验证码自动求解、Cookie 浏览器自动续期
- React / Web 管理后台(用 TUI 驾驶舱替代)
- 淘宝联盟推广子系统
- 18 个定时任务的全集(MVP 只保留心跳 + 日志清理 + guardrails)
- EXE 打包 / Nuitka / CI/CD 多架构镜像
- 多租户 / 多用户系统(单管理员)

## 快速开始

### 环境要求

- Python 3.11 或更高
- [uv](https://docs.astral.sh/uv/) 包管理器
- Windows / macOS / Linux 均可

### 安装

```powershell
# 1. 克隆
git clone <repo> xianyu-agent
cd xianyu-agent

# 2. 同步依赖
uv sync

# 3. 复制环境变量
cp .env.example .env
# 编辑 .env,填入实际配置(Fernet 密钥首次启动会自动生成)

# 4. 初始化数据库
uv run alembic upgrade head

# 5. 验证
uv run xianyu-agent --version
```

### 添加第一个闲鱼账号

```powershell
# 登录(用 DevTools 抓 Cookie)
uv run xianyu-agent auth login --account demo

# 检查状态
uv run xianyu-agent auth status

# 启动账号
uv run xianyu-agent account enable demo
uv run xianyu-agent pool start demo

# 看实时消息
uv run xianyu-agent message list --account demo --since 10m

# 起驾驶舱
uv run xianyu-agent dashboard
```

## 架构

四层严格分离,每层可独立测试:

| 层 | 路径 | 职责 |
|----|------|------|
| **协议层** | `src/xianyu_agent/protocol/` | WebSocket / mtop 解析、签名、断线重连 |
| **领域层** | `src/xianyu_agent/domain/`   | 纯业务逻辑(账号/消息/订单/卡密/规则),无 IO |
| **服务层** | `src/xianyu_agent/services/` | 协调器:账号池、Worker、回复引擎、发货 |
| **接口层** | `src/xianyu_agent/cli/`、`tui/`、`mcp/`、`skills/` | CLI 是单一事实源;TUI/MCP/Skill 包装 CLI |

详细架构与状态流见 [`docs/architecture.md`](docs/architecture.md)。

## 接口形态

```powershell
# CLI(单一事实源)
xianyu-agent <command> [subcommand] [args]
xianyu-agent auth login --account demo --cookie 'unb=...; _m_h5_tk=...'
xianyu-agent account enable --id demo
xianyu-agent rule add --name 在吗 --type keyword --pattern 还在吗 --reply "在的,亲"
xianyu-agent card add --account demo --name 卡 --content "CODE-1"
xianyu-agent pool start-all --seconds 3600
xianyu-agent message list --since 10m
xianyu-agent order list --account demo --status paid
xianyu-agent maintenance purge-messages --older-than 24

# MCP Server(给 Agent)
uv run xianyu-agent mcp serve --port 8090

# TUI 驾驶舱
xianyu-agent dashboard
```

完整命令清单见 [`docs/api-reference.md`](docs/api-reference.md)。
真实闲鱼联调需要 `XIANYU_WS_URL` 与账号 Cookie(见 `docs/protocol-notes.md`)。

## 致谢

本项目综合参考多个开源项目,详见 [`NOTICE`](NOTICE)。

- `fancyboi999/goofish-cli`(Apache-2.0) — CLI/MCP/Skill 结构
- `madguyevans-creator/resale-agent-skill-hub`(MIT) — 分层与 guardrails
- `Kaguya233qwq/myfish`(MIT) — Python async 框架
- `usagi-org/ai-goofish-monitor`(MIT) — 监控模式

## License

MIT,详见 [LICENSE](LICENSE)。

## 免责声明

本项目仅供技术学习与研究使用。使用者需自行承担使用风险,遵守闲鱼平台规则与
相关法律法规。请勿用于违反平台规则或商业用途。
