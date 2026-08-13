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

## 当前状态与开发优先级

扫码登录和正式在售商品只读同步已经过真实账号验证。WS 现由无时限 daemon 持有,
`pool` 命令可跨进程控制账号 Worker;Windows 任务计划与 Watchdog 已通过真实强杀恢复测试。
Windows 重启恢复、断网恢复和 24 小时长稳仍未验收。

WS 已补齐 IM token、`/reg`、`ackDiff`、推送 ACK 和 `syncPushPackage` 解包;但当前真实
Cookie 已返回 Session 过期,仍需重新扫码后完成真实买家消息校准,不能将 transport
connected 当作消息订阅通过。

当前最高优先级是 **P0:WS 常驻 daemon + 跨进程账号池控制 + Windows 自恢复**。
在 P0 的真实断网、杀进程、重启和 24 小时验收完成前,不会把系统描述为已具备无人值守能力。
完整阶段顺序与验收标准见 [`docs/开发计划.md`](docs/开发计划.md),已完成证据见
[`docs/验证记录.md`](docs/验证记录.md)。

P0.1-P0.4 已实现。开发时可前台运行 daemon;Windows 常驻使用 `service`,健康诊断使用
`doctor`:

```powershell
uv run alembic upgrade head
uv run xianyu-agent daemon run      # 终端 A:常驻运行
uv run xianyu-agent daemon status   # 终端 B:查询
uv run xianyu-agent daemon stop     # 终端 B:有序停止

uv run xianyu-agent service install --startup user --start-now
uv run xianyu-agent service status
uv run xianyu-agent service stop
uv run xianyu-agent service start
uv run xianyu-agent doctor
uv run xianyu-agent doctor --output json
uv run xianyu-agent soak start --account xcaicai --hours 24
uv run xianyu-agent soak status
```

daemon 运行后,可在其他终端跨进程控制单个账号:

```powershell
uv run xianyu-agent pool stop --account xcaicai
uv run xianyu-agent pool start --account xcaicai
uv run xianyu-agent pool restart --account xcaicai
uv run xianyu-agent pool status
```

真实入站校准使用安全观察模式:

```powershell
uv run xianyu-agent pool stop --account xcaicai
uv run xianyu-agent auth qr-login --account xcaicai
uv run xianyu-agent auth status --account xcaicai
uv run xianyu-agent protocol capture --account xcaicai --seconds 300 --target-messages 1
```

扫码成功后会立即换取并加密缓存 IM token;若该步骤失败,命令返回非零并提示 `auth refresh`。
同一账号有跨进程连接锁;capture 不自动回复/发货,证据文件只包含不可逆脱敏结构,离线验收会
用实际状态/帧/事件/错误记录交叉核对 summary。

命令只有收到 daemon 的 `succeeded` 回执才显示 OK;daemon 离线或等待超时会返回非零退出码。
`service install` 会创建 `XianyuAgent-Daemon` 与每分钟巡检的 `XianyuAgent-Watchdog` 两个
任务。`service stop` 会写入暂停门,防止人工停机被 Watchdog 误拉起。当前真实环境已安装
当前用户登录启动模式;强杀恢复已通过,Windows 重启恢复尚未验证。

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
uv run xianyu-agent daemon run  # 终端 A
uv run xianyu-agent pool start --account demo  # 终端 B

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
| **领域层** | `src/xianyu_agent/domain/`   | 纯业务逻辑(账号/商品/消息/订单/卡密/规则),无 IO |
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
xianyu-agent item sync --account demo       # 只读同步闲鱼在售商品到本地镜像
xianyu-agent item list --account demo
xianyu-agent pool start-all --wait 15
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
当前 `pool start/start-all/stop/restart` 是常驻 daemon 的账号级控制命令;长期连接需先运行
`daemon run`,或安装并启动 Windows `service`。完整验收状态见
[`docs/开发计划.md`](docs/开发计划.md)。

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
