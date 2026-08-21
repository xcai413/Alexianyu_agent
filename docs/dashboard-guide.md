# 驾驶舱使用指南

## 启动

```powershell
uv run xianyu-agent dashboard
# 或指定刷新间隔
uv run xianyu-agent dashboard --refresh 2
```

要求:终端支持 ANSI(Windows Terminal / PowerShell 7 均可)。

## 布局

```
┌─ 闲鱼运营驾驶舱 ────────────────────────────────┐
│ daemon: online (01:20:10) | 账号 2 | 漂移 0 | 在售商品 10 │ ← 状态条
│ 运行告警: acc-1 Worker 心跳过期 | 风险: 金额超限       │ ← 红色告警
├───────────────┬────────────────────────────────┤
│ 账号池        │ 消息流                         │
│ acc-1 Y conn  │ 10:31:56 acc-1 in 买家: 你好…   │
├───────────────┼────────────────────────────────┤
│ 订单          │ 卡密                           │
│ O-1 paid 9.9  │ 卡A acc-1 text 2 2 Y           │
├────────────────────────────────────────────────┤
│ 在售商品: acc-1 | 标题 | 9.9 | 在售 | item-1   │
├────────────────────────────────────────────────┤
│ [q]退出  [!]紧急模式  [r]刷新                  │
└────────────────────────────────────────────────┘
```

账号、消息、订单、卡密和在售商品五区数据，以及 daemon 总状态、运行时长、账号期望/实际状态
和在售商品数按 `--refresh` 间隔从 SQLite 刷新(任何进程写入都会实时出现,不限于本终端启动的
Worker)。在售商品区显示账号、标题、价格、已售数量、商品 ID 与最后同步时间；它只读取本地
镜像，首次需运行 `item sync --account <id>` 建立商品镜像。

## 快捷键

| 键 | 动作 |
|----|------|
| `q` | 退出 |
| `!` | 切换紧急模式(暂停自动操作标记,写 audit_logs) |
| `r` | 手动刷新 |
| 方向键 | 在表格内移动行 |

## 读告警

红色风险条同时显示运行状态告警和最近一条 guardrail 拦截事件。运行告警包括 daemon
不在线、账号期望/实际不一致及 Worker 心跳超过 90 秒;guardrail 格式:

`风险: <账号> (<规则>) <原因> @ <时间>`

规则取值:`message_gate`(频率/静默)、`order_amount`(金额超限)、`circuit_breaker`(熔断)。
完整历史查 `audit_logs` 表。

`FAIL_SYS_USER_VALIDATE` 属于账号认证风险，不是普通 guardrail。账号表会显示实际状态
`risk_cooling` 与“验证”列，红色风险条显示剩余冷却或“冷却结束，需手动 auth refresh”。该状态下
该账号的期望状态会自动变为 `stopped`，其他账号照常运行。

## 排障

- 四区全空:先 `auth login` / `account add` 建账号,`card add` 建卡。
- 账号池显示 `error` + 错误:按 `pool status` 查 `last_error`;常见是
  `XIANYU_WS_URL not configured` 或 `no cookie`。
- 风险条一直亮:在 `audit_logs` 里看是哪个规则;夜间静默可调
  `XIANYU_GUARDRAIL_QUIET_HOURS`。
- 显示 `FAIL_SYS_USER_VALIDATE`:不要连续扫码或刷新。先在闲鱼 App 完成实际出现的验证，等冷却
  结束后只执行一次 `auth refresh --account <id>`；`auth status` 显示 `ws_token_valid=Y` 后再启动
  Worker。
- `漂移` 非 0:查看账号表的“期望/实际/一致”列,并执行 `xianyu-agent doctor` 获取诊断。
