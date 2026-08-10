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

### rule — 回复规则

`rule add --name -n --type keyword|regex|default --pattern -p --reply -r [--account] [--priority]`、
`rule list [--account]`、`rule enable/disable/delete <id>`、`rule test --content -c [--account]`(干跑)

### card — 卡密

`card add --account <id> --name -n --content -c [--type text|data|image|api] [--price]`、
`card list [--account]`、`card restock <id> --content`、`card consume <id>`、`card enable/disable/delete <id>`

### pool — 账号池

`pool start-all --seconds N [--refresh] [--purge-interval]`(前台实时看板)、
`pool start --account <id> ...`、`pool stop/stop-all`(DB 标记)、`pool status`

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
| `order_list` / `order_get` | 订单 |
| `rule_create` / `rule_list` / `rule_set_enabled` / `rule_delete` / `rule_test` | 规则 |
| `card_create` / `card_list` / `card_restock` / `card_consume` / `card_set_enabled` / `card_delete` | 卡密 |
| `maintenance_purge_messages` | 消息清理 |
| `pool_status` | 账号池状态 |

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
