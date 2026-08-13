# API 参考

## CLI(`xianyu-agent <group> <command>`)

### auth — Cookie / token

| 命令 | 说明 |
|------|------|
| `auth login --account <id> --cookie "<串>"` | 建账号并加密存 Cookie(Fernet) |
| `auth qr-login --account <id> [--timeout] [--qr-out]` | 扫码登录:生成二维码(终端 ASCII + PNG)→ 轮询 → 自动保存 Cookie |
| `auth status --account <id>` | 脱敏 Cookie/IM token/设备 ID/过期状态;不输出完整身份与凭据 |
| `auth refresh --account <id>` | 用当前 Cookie 换取并加密缓存 IM accessToken;Worker 运行时拒绝 |
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

`pool start --account <id> [--wait 15]`、`pool stop --account <id> [--wait 15]`、
`pool restart --account <id> [--wait 15]`、`pool start-all/stop-all [--wait 15]`、`pool status`

当前语义:

- `start/stop/restart` 写入 `worker_commands`,由常驻 daemon 跨进程执行。
- 默认等待 15 秒;只有命令为 `succeeded` 才返回成功。失败返回退出码 1;daemon 离线或超时
  返回退出码 2,命令保留在数据库供 Agent 查询。
- `--wait 0` 只提交不等待,输出会明确标注“未等待执行”。
- `pool status` 显示 `enabled`、`desired` 与数据库实际状态;CLI 进程不再伪造内存 Worker 状态。

### daemon — P0.1 常驻运行(当前可用)

| 命令 | 当前语义 |
|------|----------|
| `daemon run` | 前台无时限运行单实例 daemon,管理全部启用账号;Ctrl+C 停止 |
| `daemon status` | 查询 daemon 心跳、PID、版本、运行时长及各账号期望/实际状态;90 秒未刷新标记 stale |
| `daemon stop` | 通过 SQLite 请求活跃 daemon 有序停止 |
| `daemon restart` | 请求活跃 daemon 有序停止并在同一启动进程内重建实例和账号池 |
| `daemon logs --tail N` | 读取 `data/logs/xianyu-agent.log` 末尾 N 行并二次脱敏 |

`daemon run` 启动时会加载 `enabled=true + desired_state=running` 的账号,并周期性对账。
`account enable` 只允许运行,不会自动上线;需显式执行 `pool start`。
同一数据目录的第二个 daemon 会因文件锁返回退出码 1。
同一账号的第二个 WS/登录/刷新进程会因账号级锁返回退出码 2。

### service — P0.3 Windows 常驻(当前可用)

| 命令 | 当前语义 |
|------|----------|
| `service install [--startup user\|system] [--start-now]` | 安装/更新两个 Windows 任务;默认保持暂停,加 `--start-now` 才立即运行 |
| `service start [--wait 20]` | 清除暂停门,启动主任务并等待 daemon online |
| `service stop [--wait 20]` | 写入暂停门,优雅停止 daemon;超时才强制结束任务 |
| `service status` | 显示主任务、Watchdog、最后结果及统一 daemon 健康状态 |
| `service uninstall [-y]` | 停止 daemon 并删除两个任务及生成的 XML |

默认 `--startup user` 在当前用户登录后运行;`system` 为系统启动模式,需要管理员权限。
Watchdog 每分钟检查数据库心跳与 PID,人工停止时由 `data/runtime/service.paused` 阻止自动拉起。

### doctor — P0.4 健康检查(当前可用)

`doctor [--output table|json]` 检查数据库连接与写锁、Alembic head、Fernet 密钥、每账号
Cookie 可解密性、日志目录、重复/遗留 daemon、账号期望/实际状态和 Windows 双任务。
存在 FAIL 时返回退出码 1;只有 WARN 时返回 0。输出不包含明文 Cookie、Token 或 Fernet key。

P0.2 已完成:daemon 进程级控制与 `pool` 账号级控制已分离。

### protocol — 协议调试

`protocol capture --account <id> [--seconds 300] [--target-messages 1] [--output x.jsonl]`
(P1 前台观察模式;达到目标消息数可提前结束;
要求 daemon 中同账号 stopped;消息落库但不回复/发货;原始帧仅输出不可逆脱敏结构)、
`protocol connect --account <id> --seconds N`(前台连 WS 打印事件)、
`protocol inject --account <id> --fixture x.jsonl [--count]`(离线注入帧,走完整 Worker 流水线)、
`protocol watch --account <id>`(实时观察)

capture 未连接或未达到目标消息数返回退出码 2;收到消息但必需字段缺失返回退出码 3;
只有目标达到且字段完整时返回 0。

### dashboard / maintenance / mcp

`dashboard [--refresh 2.0]`、`maintenance purge-messages --older-than 24`、
`mcp serve [--port]`

### soak — P0-E 可恢复长稳验收

| 命令 | 语义 |
|------|------|
| `soak start --account <id> [--hours 24]` | 要求 daemon/账号健康后创建持久化验收运行 |
| `soak status [--output table\|json]` | 查看采样数、异常、重连、实例变化、消息/回复和敏感日志统计 |
| `soak stop [-y]` | 提前停止,该次状态为 skipped,不会记为通过 |

Windows Watchdog 每分钟采样;进度保存在 `task_logs`,原始脱敏样本写到受 `.gitignore` 保护的
`data/logs/p0-e-soak-<run_id>.jsonl`。通过门包括 24 小时时长、≥90% 采样覆盖、无异常样本、
无 daemon 实例变化、无重复成功回复、无真实 Cookie/Token/Fernet 值泄漏。

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
