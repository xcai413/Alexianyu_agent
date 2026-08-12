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
│ 账号 2  | 在售商品 10 | 模式: 正常 | 刷新: 10:32 │  ← 状态条
│ 风险: acc-1 (order_amount) 金额超限 @ 10:30:01  │  ← 红色告警(有 guardrail 事件时)
├───────────────┬────────────────────────────────┤
│ 账号池        │ 消息流                         │
│ acc-1 Y conn  │ 10:31:56 acc-1 in 买家: 你好…   │
├───────────────┼────────────────────────────────┤
│ 订单          │ 卡密                           │
│ O-1 paid 9.9  │ 卡A acc-1 text 2 2 Y           │
├───────────────┴────────────────────────────────┤
│ [q]退出  [!]紧急模式  [r]刷新                  │
└────────────────────────────────────────────────┘
```

四区数据与状态条中的在售商品数每 2 秒从 SQLite 刷新(任何进程写入都会实时出现,
不限于本终端启动的 Worker)。首次需运行 `item sync --account <id>` 建立商品镜像。

## 快捷键

| 键 | 动作 |
|----|------|
| `q` | 退出 |
| `!` | 切换紧急模式(暂停自动操作标记,写 audit_logs) |
| `r` | 手动刷新 |
| 方向键 | 在表格内移动行 |

## 读告警

红色风险条显示最近一条 guardrail 拦截事件,格式:

`风险: <账号> (<规则>) <原因> @ <时间>`

规则取值:`message_gate`(频率/静默)、`order_amount`(金额超限)、`circuit_breaker`(熔断)。
完整历史查 `audit_logs` 表。

## 排障

- 四区全空:先 `auth login` / `account add` 建账号,`card add` 建卡。
- 账号池显示 `error` + 错误:按 `pool status` 查 `last_error`;常见是
  `XIANYU_WS_URL not configured` 或 `no cookie`。
- 风险条一直亮:在 `audit_logs` 里看是哪个规则;夜间静默可调
  `XIANYU_GUARDRAIL_QUIET_HOURS`。
