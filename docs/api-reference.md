# API 参考

## CLI(`xianyu-agent <group> <command>`)

### auth — Cookie / token

| 命令 | 说明 |
|------|------|
| `auth login --account <id> --cookie "<串>"` | 建账号并加密存 Cookie(Fernet) |
| `auth qr-login --account <id> [--timeout] [--qr-out]` | 扫码登录:生成二维码(终端 ASCII + PNG)→ 轮询 → 自动保存 Cookie |
| `auth status --account <id>` | 指纹(脱敏):unb / 有无 _m_h5_tk |
| `auth refresh --account <id>` | 手动刷新占位(Phase 1:重新 login) |
| `auth list` | 账号列表 |
| `auth show/enable/disable/delete --account <id>` | 管理 |

### account — 账号 CRUD

`account add --id <id> [--nickname] [--remark]`、`list`、`show --id`、`enable/disable --id`、`delete --id [-y]`

### message — 消息

`message list --account <id> --since 10m|2h|1d [--limit] [--direction] [--chat-id]`、`message show <id>`

### order — 订单

`order list --account <id> [--status] [--limit]`、`order show <id>`(含发货内容/失败原因)

### item — 在售商品镜像(只读)

`item sync --account <id> [--page-size 20] [--max-pages 100]` 从闲鱼“在售”商品列表创建或更新本地镜像;
该命令只发送商品列表读取请求,不会修改闲鱼端的标题、价格、上下架状态或库存。

`item list --account <id> [--all] [--limit]` 查看本地商品镜像;`item show <local_id>` 查看详情。
完整同步成功后,本次未出现的历史镜像会标记为非在售;请求失败时原有镜像不会被改动。

### rule — 回复规则

`rule add --name -n --type keyword|regex|default --pattern -p --reply -r [--account] [--priority]`、
`rule list [--account]`、`rule enable/disable/delete <id>`、`rule test --content -c [--account]`(干跑)

### card — 卡密

`card add --account <id> --name -n --content -c [--type text|data|image|api] [--price]`、
`card list [--account]`、`card restock <id> --content`、`card consume <id>`、`card enable/disable/delete <id>`

### pool — 账号池

`pool start-all --seconds N [--refresh] [--purge-interval]`(前台实时看板)、
`pool start --account <id> ...`、`pool stop/stop-all`(DB 标记)、`pool status`

当前语义:

- `start/start-all` 只在当前终端进程内持有 WS 连接,达到 `--seconds` 或进程退出后停止。
- `stop/stop-all` 只更新数据库状态,不能停止另一个进程中的 Worker。
- `pool status` 显示的是数据库状态和当前调用进程可见的状态,不是后台服务存活证明。

### daemon — P0.1 常驻运行(当前可用)

| 命令 | 当前语义 |
|------|----------|
| `daemon run` | 前台无时限运行单实例 daemon,管理全部启用账号;Ctrl+C 停止 |
| `daemon status` | 查询最近 daemon 的心跳、PID、版本、状态与错误;90 秒未刷新标记 stale |
| `daemon stop` | 通过 SQLite 请求活跃 daemon 有序停止 |
| `daemon restart` | 请求活跃 daemon 有序停止并在同一启动进程内重建实例和账号池 |
| `daemon logs --tail N` | 读取 `data/logs/xianyu-agent.log` 末尾 N 行并二次脱敏 |

`daemon run` 启动时会加载全部 `accounts.enabled=true` 的账号,并周期性对账启用状态。
同一数据目录的第二个 daemon 会因文件锁返回退出码 1。

### service / doctor — P0.3-P0.4 规划命令(当前不可用)

| 计划命令 | 目标语义 |
|----------|----------|
| `service install/start/stop/status/uninstall` | 管理 Windows 任务计划中的自启动与失败恢复 |
| `doctor` | 检查数据库、迁移、密钥、Cookie、日志、重复实例和任务计划 |

P0.2 完成后,`pool start/stop/restart` 才会改为向 daemon 提交持久化账号级命令。
目前 `daemon` 只支持进程级控制,不要把现有 `pool stop` 解释成能控制 daemon 中单个 Worker。

### protocol — 协议调试

`protocol connect --account <id> --seconds N`(前台连 WS 打印事件)、
`protocol inject --account <id> --fixture x.jsonl [--count]`(离线注入帧,走完整 Worker 流水线)、
`protocol watch --account <id>`(实时观察)

### dashboard / maintenance / mcp

`dashboard [--refresh 2.0]`、`maintenance purge-messages --older-than 24`、
`mcp serve [--port]`

## MCP Server

启动:`xianyu-agent mcp serve`(默认 `127.0.0.1:8090`,SSE 端点 `/sse`)

由 FastAPI 端点自动生成的 tools(operation_id):

| 工具 | 说明 |
|------|------|
| `account_create` / `account_list` / `account_get` | 账号 |
| `account_set_enabled` / `account_delete` | 启停/删除 |
| `auth_login` / `auth_status` | Cookie 录入 / 指纹 |
| `message_list` | 最近消息 |
| `item_sync` / `item_list` / `item_get` | 只读在售商品同步 / 本地镜像查询 |
| `order_list` / `order_get` | 订单 |
| `rule_create` / `rule_list` / `rule_set_enabled` / `rule_delete` / `rule_test` | 规则 |
| `card_create` / `card_list` / `card_restock` / `card_consume` / `card_set_enabled` / `card_delete` | 卡密 |
| `maintenance_purge_messages` | 消息清理 |
| `pool_status` | 账号池状态 |

daemon/service 的 MCP tools 尚未实现;只有账号级跨进程控制通过真实验收后才会暴露。

Codex 接入示例(`~/.codex/config.toml` 或桌面端 MCP 配置):

```toml
[mcp_servers.xianyu-agent]
command = "uv"
args = ["run", "xianyu-agent", "mcp", "serve"]
```

## Codex Skills(`src/xianyu_agent/skills/`)

| Skill | 场景 |
|-------|------|
| `xianyu-triage` | 新消息意图分诊(询价/砍价/催发/售后/广告)→ 下一步动作 |
| `xianyu-reply-draft` | 起草真人感回复(结合订单/商品上下文) |
| `xianyu-delivery` | 发货编排:查单/查库存/补发/排查失败 |
| `xianyu-pool` | 账号池运维:状态诊断/启停/心跳 |

## HTTP 约定

所有端点返回 `{"success": true, "data": ...}` 或 `{"detail": "<错误>"}`(400)。
认证:Phase 8 起为局域网内部服务,未加鉴权(明确边界)。
