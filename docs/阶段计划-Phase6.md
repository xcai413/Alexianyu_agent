# Phase 6 阶段计划:库存、卡密与自动发货

> 基准:**GitHub Issue #5(Phase 6 库存、卡密与自动发货)**。本地 `docs/开发计划.md` 已过期,
> 不作为基准;本文件只是把 Issue #5 拆成可独立提交的分片,并记录当前事实状态。
> 用户优先级(2026-09-19 原话):虚拟卡券、自动发货、多规格发货、调用 API 发货。

对照项目(License 边界见 Issue #5 §3):

- `xcai413/Ydisks-Xianyu-Helper`(Apache-2.0):库存/卡券/发货行为规格主参考,
  `internal/application/cards/`、`internal/automation/card_delivery.go`、`internal/automation/api_delivery.go`。
- `xcai413/xianyu-super-butler`(AGPL-3.0):仅行为/UX 参考,不复制源码,
  `app/services/delivery_block_rules.py`(发货拦截规则族)。

## 一、当前事实状态(2026-09-21)

已存在、且必须保持兼容:

| 能力 | 位置 | 状态 |
|------|------|------|
| 卡密组 CRUD 与原子扣减 | `domain/inventory/cards.py`、表 `cards` / `card_consumptions` | 可用,测试覆盖 |
| Phase 6 发货链路 | `application/delivery/`(OrderPaid → 守卫 → 策略 → 来源 → 发送 → 收尾) | 已接通,真实订单待验收 |
| 规格→库存来源绑定 | `domain/item/sku_binding.py`(`target_kind` 预留扩展点) | 已预留,当前只支持 `card` |
| 机密库与审计 | `infrastructure/secrets/fernet_vault.py`、`audit_logs` | 可用 |
| 订单领域 | `domain/order/orders.py`、表 `orders` | 可用(只读同步) |

库存池/单元/预留、DeliveryPolicy、DeliveryJob/Attempt、DeliveryGuard、UNCERTAIN 语义、
API 发货 Provider、OutboundGuard 与发货恢复服务均已实现。

## 二、新增表(与 Issue #5 §20 对齐)

```
inventory_pools        库存池(text/data/image/api + 范围 + 延迟)
inventory_units        data 池的可消费单元(一条卡密一个 unit)
inventory_reservations 预留记录(reserve/release/consume/uncertain)
```

后续分片再加:`delivery_policies`、`delivery_jobs`、`delivery_attempts`、
`delivery_guard_rules`、`personal_blacklist`。

旧 `cards` / `card_consumptions` 表不删除(Issue #5 §28 兼容要求),迁移只做数据映射。

## 二点五、进度(2026-09-19)

| 分片 | 状态 | 说明 |
|------|------|------|
| 6A 库存领域 + 表 + 原子预留 | 已提交 | 三张表 + 比较并设置式预留,SQLite 本地已验 |
| 6B 库存应用服务 + CLI | 已提交 | xianyu-agent inventory ...,真实库冒烟通过 |
| 6C 多规格发货 | 已提交 | target_kind=pool 精确命中,多数量一次性预留 |
| 6D 发货策略 + 安全模板 | 已提交 | 四级优先级;模板必须含 {card},白名单变量 |
| 6E DeliveryJob / Attempt | 已提交 | 唯一键收敛重复事件;四类失败判定 |
| 6F DeliveryGuard | 已提交 | 五条规则,fail closed,真实库冒烟通过 |
| 6G 执行器 + 订单再校验 + 恢复 | 已提交 | 预留→发送→收尾;结果不明冻结 |
| 6H 调用 API 发货 | 已提交 | 出站守卫 + SecretVault + MockTransport 覆盖;生产密钥装配已接通 |
| 6I 接进订单事件 | 已提交 | delivery_engine=auto:有策略走阶段 6,否则旧链路 |
| 6B2 策略型池内容 | 已提交 | text 池发文案、image 池存地址;执行器不预留库存,停用池与 image 池 fail closed |

### 已知缺口(下一步)

1. **图片发货端口**尚未落地:text 池已经能按池内文案发货,image 池只保存图片地址;
   发送层目前只有文本通道(出站帧的 contentType=1),协议笔记里没有出站图片帧的记录,
   所以执行器对 image 池 fail closed,不把图片地址当文本发出去。补齐前需要一份真实出站图片帧
   的脱敏证据(与 P1 的消息校准同一批工作)。
2. **MCP 适配**:CLI 与 Web 已共用应用服务;MCP 仍需补结构化发货配置工具。
3. **三库 CI 覆盖**:库存预留在 CI 已跑三库;发货策略/任务/守卫/API 配置表仍需扩大数据库矩阵。
4. **真实环境验证**:阶段 6 自动发货与外部 API 发卡尚未在受控测试订单上完成。

## 三、分片计划(每片一个提交,各带测试)

### 6A 库存领域模型 + 表 + 原子预留仓储

- `domain/inventory/{state,pool,unit,reservation}.py`:池类型、单元/预留状态机、输入校验。
- 新表与仓储 `infrastructure/database/repositories/inventory.py`。
- 原子性做法(三库一致):先按 id 顺序读取候选单元,再逐个 `UPDATE ... WHERE state='available'`
  比较并设置,任何一行 rowcount≠1 即整体回滚——不依赖 `FOR UPDATE`(SQLite 不支持)。
- `quantity > 1` 必须 ALL_OR_NOTHING(Issue #5 §8)。
- 测试:纯领域状态机 + SQLite 仓储原子性/并发不双花/回滚。

### 6B 库存应用服务 + CLI / REST

- `application/inventory/{service,import_service,reservation_service,query_service}.py`。
- CLI/REST 只调用应用服务,禁止直写数据库。
- 兼容迁移入口:把已有 `cards` 映射成 `data` 池与单元。

### 6C 多规格发货

- `sku_binding.target_kind` 增加 `pool`,规格组合 → 库存池 + 数量。
- 多数量订单一次性预留(ALL_OR_NOTHING)。

### 6D 发货策略与安全模板

- `DeliveryPolicy` + 解析优先级 SKU → Item → Account → Global(可审计)。
- Safe Renderer:白名单变量替换,禁止 eval/表达式(Jinja 任意对象访问也不允许)。

### 6E DeliveryJob / DeliveryAttempt

- `(account_id, order_id, 语义键)` 唯一约束,重复 OrderPaid / 轮询 / 恢复重放收敛到同一 Job。
- 状态机 `PENDING→CHECKING→RESERVING→RESERVED→DELIVERING→CONFIRMING→COMPLETED` 与
  `BLOCKED/FAILED_RETRYABLE/FAILED_FINAL/UNCERTAIN/CANCELLED`。
- Retry 分类:只有"可证明未产生外部副作用"的失败才自动重试;**请求已发出但结果未知 → UNCERTAIN,
  不自动重发、不自动释放库存**(Issue #5 §15/§16)。

### 6F DeliveryGuard

- 规则:`personal_blacklist`(Owner/Account/Item)、`buyer_credit_zero`、`buyer_has_order`、
  `buyer_has_order_global`、`buyer_unconfirmed`。
- 每条规则 `enabled/priority/block_reason/excluded_item_ids/config`;规则异常 fail closed(§12)。
- 参考 butler 的规则族与优先级语义(不复制代码)。

### 6G 发货前再校验 + 执行器接线 + 恢复

- 外部发送前重新读订单状态(付款态/退款态/异常/是否已发货),失败即 BLOCKED 并写审计(§13)。
- 执行器接 `OrderPaid`,把现有 `services/delivery_service.py` 收敛到新链路。
- 进程重启恢复:PENDING 重排、RESERVED 保留预留、FAILED_RETRYABLE 按策略、UNCERTAIN 冻结(§22)。

### 6H API 发货(调用 API 发货)

- `infrastructure/delivery/api_provider.py`:按发货单位调用外部 API,配置含 url/method/headers/body/
  timeout/response extractor。
- Secret 只存 `secret_ref`,经 SecretVault 解析;Secret 不得出现在普通 JSON、日志、错误信息。
- OutboundGuard:URL scheme 校验、阻断 loopback/link-local/metadata、重定向重新校验。
- 语义:远端可能已产生副作用 → UNCERTAIN;响应无内容 → UNCERTAIN。

### 6I REST / MCP / CLI + 审计脱敏

- 三个入口共用应用服务;审计记录 actor/account/order/job/attempt/decision/result,卡密只留 id 与掩码。

### 6J 三库一致性验证

- SQLite 本地通过;MySQL / PostgreSQL 走既有 `TEST_DATABASE_BACKEND` CI job(Issue #5 §21)。
- 用例:Pool CRUD、Unit 导入、reserve(1)、并发 reserve、reserve(N)、不足回滚、重复 OrderPaid、
  Job 唯一性、UNCERTAIN 持久化、恢复、Guard 查询。

## 四、依赖与阻塞

- 6J 的 MySQL/PostgreSQL 需要 CI job 环境;本地只跑 SQLite,不写成"三库已验证"。
- 6H 依赖已有 SecretVault;OutboundGuard 需在 6H 内新建。
- 本阶段不做:完整 Web 页面(#9)、自动评价(#6)、Automation Engine(#7)、通知通道(#8)。

## 五、与用户优先级的对应

| 用户要求 | 对应分片 |
|---|---|
| 虚拟卡券 | 6A / 6B(TEXT 池、DATA 池单元导入) |
| 自动发货 | 6D / 6E / 6G |
| 多规格发货 | 6C(规格 → 池 + 数量) |
| 调用 API 发货 | 6H |
