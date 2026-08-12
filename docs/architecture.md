# xianyu-agent 架构

## 一句话

为 AI Agent 直接控制的闲鱼运营系统:单进程 Python + SQLite,协议 / 领域 / 服务 / 接口四层分离,
CLI 是单一事实源,TUI / MCP / Skill 都包装同一套领域逻辑。

## 四层架构

| 层 | 路径 | 职责 | 依赖 |
|----|------|------|------|
| 协议层 | `src/xianyu_agent/protocol/` | WS 连接/重连/心跳、mtop 帧解析、token 签名 | 仅依赖 `events.py` |
| 领域层 | `src/xianyu_agent/domain/` | 账号/商品/消息/订单/卡密/规则的纯业务逻辑 | 仅依赖 DB 模型 |
| 服务层 | `src/xianyu_agent/services/` | Worker、账号池、回复引擎、发货、guardrails、AI Provider | 串接协议 + 领域 |
| 接口层 | `cli/` `tui/` `mcp/` `skills/` | Typer CLI / Textual 驾驶舱 / FastAPI+MCP / Codex Skill | 全部包装领域与服务 |

```
闲鱼 WS ──> protocol/client ──> events(标准化)──> services/account_worker
                                                        │
                              ┌─────────────────────────┤
                              ▼                         ▼
                    domain(messages/orders)    reply_engine / delivery_service
                              │                         │
                              ▼                         ▼
                         SQLite (xianyu.db)      guardrails(审计事件)

CLI / TUI / MCP ──> domain 层(同一事实源,无重复逻辑)
```

## 数据流(消息 → 自动回复)

1. WS 收到帧 → `protocol/parser.parse_frame` 标准化为 `MessageReceived`
2. `AccountWorker._on_event` 入库(`domain/messages.upsert_inbound`)
3. guardrails 检查(频率 / 静默时段)→ 放行则 `ReplyEngine`
4. 按模式(rule / rule_then_ai / ai)产出回复 → 发送 → `reply_logs`

## 数据流(订单支付 → 自动发货)

1. `OrderPaid` 事件 → 订单入库
2. guardrails 金额检查 → 超限则拦截并写审计事件
3. `DeliveryService` 选卡 → `domain/cards.consume_card` 原子扣减
4. 发送卡密 → 订单置 delivered + `card_consumptions` 记录

## 状态持久化(全部可跨进程查询)

| 表 | 内容 | 谁写 |
|----|------|------|
| `accounts` | 账号与启停状态 | CLI / auth |
| `worker_status` | Worker 心跳(30s)与状态 | WsClient |
| `items` | 账号在售商品只读镜像 | item sync |
| `messages` | 聊天消息(幂等按 message_id) | Worker |
| `orders` | 订单状态机 | Worker / DeliveryService |
| `cards` / `card_consumptions` | 卡密库存与消费 | CLI / DeliveryService |
| `reply_rules` / `reply_logs` | 规则与回复记录 | CLI / ReplyEngine |
| `audit_logs` | 审计与 guardrail 事件 | TUI / guardrails |

## 关键设计决策

- **CLI 是单一事实源**:所有操作有 CLI 等价物;MCP 工具与 TUI 直接调用领域函数(与 CLI 同一逻辑),
  不重复实现。
- **幂等优先**:消息按 `message_id` 去重、订单按 `(account_id, order_id)` upsert、卡密扣减用
  条件 UPDATE 防双花。
- **失败可审计**:发送/发货失败都落库(`reply_logs.success=False`、`orders.delivery_fail_reason`),
  绝不假装成功。
- **状态机防伪**:上游 `delivered` 确认帧只在确有发货内容时更新订单状态,避免掩盖失败。
- **商品只读镜像**:`item sync` 只调用闲鱼商品列表;完整快照成功才将未出现的历史商品标记为非在售,
  请求失败不改动原镜像,且不会向闲鱼写入商品变更。
- **guardrails 事件进 audit_logs**:任何进程(含 TUI)都能看到被拦原因。

## 扩展点

- **新 AI Provider**:实现 `AIProvider` 协议,`build_provider()` 注册即可。
- **新 guardrail 规则**:在 `Guardrails` 加 `check_*` 方法,Worker/DeliveryService 调用点接入。
- **新协议字段**:只改 `protocol/parser.py` 的字段编号映射,领域层不受影响。
- **真实闲鱼联调**:配 `XIANYU_WS_URL` + `auth login` 录入 cookie;字段编号按
  `docs/protocol-notes.md` 校准。

## 已知边界(如实)

- `pool stop` 是 DB 层标记;前台 `start-all` 需在运行终端 Ctrl+C 真正停止。
- guardrails 熔断计数为进程内;多进程部署时语义需扩展。
- 发送协议(`send_text`)目前是原始文本帧,真实路由待联调。
