# xianyu-agent 架构

## 一句话

为 AI Agent 直接控制的闲鱼运营系统:Python + SQLAlchemy,协议 / 领域 / 服务 / 接口四层分离,
CLI 是单一事实源,TUI / Web 控制台 / MCP / Skill 都包装同一套领域逻辑。

数据库支持 SQLite(单机默认)与 MySQL / PostgreSQL(容器与多副本),daemon 通过账号租约与持久化
调度队列支持多副本横向扩展;默认 `XIANYU_AUTOMATION_MODE=observe`,任何真实外发都要显式开启
并满足门禁。

## 四层架构

| 层 | 路径 | 职责 | 依赖 |
|----|------|------|------|
| 协议层 | `src/xianyu_agent/protocol/` | WS 连接/重连/心跳、mtop 帧解析、token 签名 | 仅依赖 `events.py` |
| 领域层 | `src/xianyu_agent/domain/` | 账号/商品/消息/订单/卡密/规则的纯业务逻辑 | 仅依赖 DB 模型 |
| 服务层 | `src/xianyu_agent/services/` | Worker、账号池、回复引擎、发货、guardrails、AI Provider | 串接协议 + 领域 |
| 接口层 | `cli/` `tui/` `interfaces/web/` `api/` `skills/` | Typer CLI / Textual 驾驶舱 / Web 控制台 / FastAPI(自动生成 MCP tools)/ Codex Skill | 全部包装领域与服务 |

`mcp serve` 用 `fastapi-mcp` 从 `api/app.py` 的 FastAPI 应用自动生成 tools,所以新增的管理端点
会自动对 Agent 可见;不存在单独的 `mcp/` 业务包。

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

按 `XIANYU_DELIVERY_ENGINE` 二选一(`auto` 时有启用策略走 Phase 6,否则回落旧链路):

**旧卡密链路(Phase 4)**

1. `OrderPaid` 事件 → 订单入库
2. guardrails 金额检查 → 超限则拦截并写审计事件
3. `DeliveryService` 选卡 → `domain/cards.consume_card` 原子扣减
4. 发送卡密 → 订单置 delivered + `card_consumptions` 记录

**发货引擎(Phase 6,优先)**

1. `OrderPaid` → `DeliveryOrderValidator` 重读订单(只有 `paid` 放行)
2. `DeliveryGuardService` 守卫判定(fail closed)→ 命中规则进 `blocked` / `needs_confirmation`
3. `DeliveryPolicyService` 按范围解析策略(规格 > 商品 > 账号 > 全局)
4. 校验库存池范围(账号/商品/规格)与策略、池的延迟取较大者
5. `data` 池:原子预留 → 取卡密 → 渲染模板 → 发送;`api` 池:过出站守卫 → 取密钥 → 调发卡 API;
   `text` / `image` 池:内容就在池上,不占单元
6. 按结果分类:确定成功才核销库存,明确没发出才退库存,结果不明冻结为 `uncertain`
7. 策略打开 `platform_confirm` 时,再调 `/confirmation`(虚拟发货)并携带已发送的凭证
8. 全过程写 `delivery_jobs` / `delivery_attempts` / `audit_logs`;`attention list` 汇总卡住等人的项

## 状态持久化(全部可跨进程查询)

| 表 | 内容 | 谁写 |
|----|------|------|
| `accounts` | 账号与启停状态 | CLI / auth |
| `worker_status` | Worker 心跳(30s)与状态 | WsClient |
| `items` | 账号在售商品只读镜像 | item sync |
| `messages` | 聊天消息(幂等按 message_id) | Worker |
| `orders` | 订单本地镜像、数量与状态 | Worker / `order sync` / DeliveryService |
| `cards` / `card_consumptions` | 卡密库存与消费 | CLI / DeliveryService |
| `inventory_pools` / `inventory_units` / `inventory_reservations` | 库存池、逐条单元与原子预留 | CLI / `DeliveryExecutor` |
| `delivery_policies` / `delivery_jobs` / `delivery_attempts` | 发货策略、任务状态机与尝试记录 | CLI / `DeliveryExecutor` |
| `delivery_guard_rules` / `personal_blacklist` | 发货守卫规则与个人黑名单 | CLI / `DeliveryGuardService` |
| `delivery_templates` / `api_delivery_configs` | 可复用发货模板与外部发卡 API 结构 | CLI / Web |
| `secret_vault_entries` | 加密密钥库(密钥值只用 Fernet 密文) | CLI / Web |
| `platform_automation_configs` / `platform_automation_runs` | 擦亮/求花配置与逐目标执行事实 | CLI / Web / 调度器 |
| `scheduler_work` | 持久化调度队列(租约抢占) | daemon 调度轮 |
| `alert_dispatches` | 运维告警的发送事实(同一事项只发一次) | daemon + webhook |
| `conversations` / `quick_replies` / `buyer_notes` | 会话状态、快捷回复与买家备注 | Worker / CLI / Web |
| `reply_rules` / `reply_logs` | 规则与回复记录 | CLI / ReplyEngine |
| `ai_providers` | AI Provider 配置(密钥走 SecretVault) | CLI / Web |
| `daemon_instances` / `worker_commands` / `runtime_leases` | 常驻实例、跨进程命令与租约 | daemon / CLI |
| `transactional_outbox` / `consumer_inbox` | 事务性外发与消费幂等 | Worker / dispatcher |
| `items` 相关(`item_variants` / `item_spec_bindings` / `item_materials` / `batch_publish_*`) | 规格、绑定、素材与批量铺货 | CLI / Web |
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
- **订单只读镜像**:`order sync` 读取卖家已售订单并按 `(账号,订单号)` 幂等合并；订单是历史记录，
  本次快照中缺失的本地订单不会被删除或改状态。商品“已售”只汇总 `paid`、`delivered`、
  `completed` 订单的数量，退款、取消和未知状态不计入。
- **guardrails 事件进 audit_logs**:任何进程(含 TUI)都能看到被拦原因。

## 扩展点

- **新 AI Provider**:实现 `AIProvider` 协议,`build_provider()` 注册即可。
- **新 guardrail 规则**:在 `Guardrails` 加 `check_*` 方法,Worker/DeliveryService 调用点接入。
- **新协议字段**:只改 `protocol/parser.py` 的字段编号映射,领域层不受影响。
- **真实闲鱼联调**:配 `XIANYU_WS_URL` + `auth login` 录入 cookie;字段编号按
  `docs/protocol-notes.md` 校准。

## P0 常驻控制面

连接生命周期归属于 `RuntimeDaemon`,不再由执行 `pool start` 的前台进程持有;`pool` CLI 只写
`worker_commands` 并等 daemon 回执:

```text
daemon / service / pool / doctor CLI (P0.1-P0.4 已实现)
       │
       ▼
SQLite 控制面 ──> RuntimeDaemon ──> AccountPool ──> AccountWorker × N ──> 闲鱼 WS
                         ▲
                         └── Windows 主任务 + 每分钟 Watchdog:启动与失败恢复
```

TUI 与 Web 控制台都接入 daemon 总状态和 Worker 状态差异;MCP 由 FastAPI 自动生成,因此同一批
端点 Agent 也能直接调用。

当前已实现:

- `daemon_instances`:实例 ID、PID、版本、启动时间、心跳、停止/重启请求、最后错误。
- `data/runtime/daemon.lock`:操作系统文件锁,同一数据目录只允许一个 daemon。
- `data/logs/xianyu-agent.log`:5 MiB × 6 个文件的轮转日志,写入前脱敏 Cookie、Token、
  Fernet key、卡密和消息正文。
- daemon 周期性对账 `accounts.enabled + desired_state`;新启用账号默认 stopped,只有
  `pool start` 后启动 Worker,禁用账号自动停止。
- `accounts.desired_state`:区分“账号允许运行”和“当前期望在线”。
- `worker_commands`:账号级 `start/stop/restart` 的 pending/running/succeeded/failed 全过程,
  包含领取 daemon、结果、错误和完成时间。
- `pool` CLI 等待 daemon 回执;daemon 不在线、执行失败或等待超时均不会伪造成功。
- `XianyuAgent-Daemon`:当前用户登录或系统启动时隐藏运行固定解释器与工作目录。
- `XianyuAgent-Watchdog`:独立每分钟检查 daemon 的数据库心跳和 PID;不健康时启动主任务。
- `data/runtime/service.paused`:人工 `service stop` 的暂停门,避免 Watchdog 将明确停机当作故障。
- daemon 健康判定由数据库状态、90 秒心跳阈值和 PID 三项共同决定,供 CLI、pool 和
  Watchdog 复用。
- `services/observability.py` 从 SQLite 构造 daemon 与账号 Worker 的统一运行快照;
  `daemon status`、`doctor` 和 TUI 共用,避免跨进程界面伪造内存 Worker 状态。
- `doctor` 只读检查数据库写锁、Alembic head、Fernet、Cookie 可解密性、日志目录、
  daemon 单实例、账号状态漂移、Windows 双任务,以及**自动化配置一致性**:启用了却没登记真实
  校准的动作、指向不存在或已停用库存池的启用策略、引用缺失或停用模板的策略、范围指向不存在账号的
  库存池、非 https 的告警地址。它不打印明文 Cookie/Token,也不回显告警地址正文。
- `ws_credentials` 仅保存 Fernet 加密的 IM accessToken、稳定设备 ID 与过期时间。
- `FAIL_SYS_USER_VALIDATE` 是独立的人工验证熔断：只停止触发它的账号，将风险代码、检测时间、
  冷却截止和“需人工恢复”写入 `worker_status` 与审计日志；冷却期间不再自动请求 IM Token。
  冷却结束后仍必须由管理员手动执行一次成功的 `auth refresh` 才能解除，其他账号不受影响。
- WS 只有在 transport 握手、`/reg` 与 `ackDiff` 全部完成后才进入 connected;推送先 ACK,
  再解包 `syncPushPackage` 并标准化事件。
- `XIANYU_AUTOMATION_MODE=observe` 为默认值,只落库和审计;显式 active 才允许规则回复/发货。
- 擦亮与求花还必须同时满足账号启用、动作真实校准已批准、动作配置已启用。daemon 每 30 秒执行
  一次有界调度轮;确定性任务键、数据库租约和逐目标幂等记录保证多副本只执行一次。求花默认
  延迟 1440 分钟并在 `23:00-08:00` 静默,结果不确定时记 `uncertain` 且不自动重发。

尚未完成(P0.5):断网、Windows 重启和完整真实长稳验收。详细顺序见 `docs/开发计划.md`。

## 自动化、通知与可观测

- **平台自动化调度**:`platform_automation_configs` 保存账号 × 动作(擦亮/求花)的开关、时间、延迟
  与静默时段;真实校准是启用的硬门禁。daemon 每 30 秒跑一次有界调度轮,任务写入 `scheduler_work`,
  确定性任务键(前缀 + SHA-256 摘要)与租约保证多副本只执行一次。
- **立即执行一次**:`automation run-now` 与 `POST /platform-automation/run-now` 把任务排进同一队列,
  要求全局 active、动作已校准且已启用、当前不在静默时段;它不绕过任何门禁,也不存在第二条执行路径。
- **待人工处理队列**:`attention list` 与 `GET /operations/attention` 汇总卡住等人的事实
  (发货任务 `uncertain`/`needs_confirmation`/`blocked`、平台动作 `uncertain`、账号需人工验证),
  每项附带收尾命令;清单只读。
- **运维告警**:配置 `XIANYU_ALERT_WEBHOOK_URL` 后,daemon 每 60 秒把**新出现**的待处理事项 POST
  过去;同一事项只发一次(`alert_dispatches` 唯一键),地址要过出站守卫,发送失败只记日志。
- **日志与风险**:`daemon logs --stats` 只给级别行数;Web 的 `logs/summary` 只回级别计数与脱敏后的
  告警行,`risk-events` 展示风控熔断的打开/解除。
- **分析面板**:`analytics report` 与 `GET /dashboard/analytics` 给出多窗口经营指标、自动化健康与
  镜像水位(在售数、最近同步时间、待人工处理数),全部来自本地镜像。

## 已知边界(如实)

- `pool` 已能跨进程控制 daemon 中的账号 Worker;Windows 用户登录启动与强杀恢复已通过。
- Windows 重启、断网恢复、真实消息订阅和 24 小时长稳尚未验收,因此仍不能宣称完整无人值守能力。
- guardrails 熔断计数为进程内;多进程部署时语义需扩展。
- 发送分两条路径:回复/发货走已校准的 `MessageSend/sendByReceiverScope` 信封(文本与图片同源),
  `WsClient.send_text` 的原始文本帧只作为旧发货链路的兼容回落。图片消息与已读上报已按参考项目
  实现,但仍需真实校准(见 `docs/待真实验证清单.md` F9/F10)。
- 参考项目里仍有四项没有移植,原因是证据不足或需要用户决策:评价买家(两个来源接口冲突)、
  免拼免运费(参考请求体自相矛盾)、商品编辑与上下架(两个参考项目都没有公开接口)。详见
  `docs/参考项目功能对齐矩阵.md`。
