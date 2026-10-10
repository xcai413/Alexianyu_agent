---
name: xianyu-pool
description: 闲鱼账号池运维 — 启停 Worker、查看在线状态与心跳、处理异常账号。管理多账号时使用。
---

# 闲鱼账号池运维

## 何时使用

- 需要查看/管理多个闲鱼账号的在线状态。
- 账号离线、心跳过期、需要启停时。

## 常用命令

| 目标 | 命令 |
|------|------|
| 查看全部状态 | `xianyu-agent pool status` |
| 让 daemon 启动全部启用账号 | `xianyu-agent pool start-all --wait 15` |
| 让 daemon 启动单个账号 | `xianyu-agent pool start --account <id> --wait 15` |
| 让 daemon 停止单个账号 | `xianyu-agent pool stop --account <id>` |
| 启用/禁用账号 | `xianyu-agent account enable/disable --id <id>` |
| 查看账号详情 | `xianyu-agent account show --id <id>` |
| 暂停/恢复某账号自动回复 | `xianyu-agent account pause-reply --id <id> --minutes 30 --reason "..."` / `xianyu-agent account resume-reply --id <id>` |
| 刷新账号昵称 | `xianyu-agent account refresh-profile --id <id>` |
| 看有没有要人处理的事 | `xianyu-agent attention list` |
| 检查 daemon 与数据库 | `xianyu-agent doctor` |

`pool start/stop` 只是写 `worker_commands` 并等 daemon 回执:daemon 不在线时命令会明确失败,
不会伪装成功。跨进程启动 Worker 的前提是 daemon 已经在跑(`xianyu-agent daemon status`)。

## 诊断指引

1. `pool status` 看 `db` 列:
   - `connected` + 心跳新鲜(≤1 分钟)→ 正常
   - `error` + `last_error` 有值 → 按错误处理
   - `disconnected` + `stop() called` → 被正常停止
2. 常见错误:
   - `FAIL_SYS_USER_VALIDATE` / `risk_recovery_required=Y` → 风控熔断:先 `xianyu-agent auth status -a <id>`,
     冷却结束后必须人工跑一次成功的 `xianyu-agent auth refresh -a <id>` 才能解除
   - `no cookie for account=...` → 先 `xianyu-agent auth login -a <id>`
   - 心跳长时间不更新但状态 connected → 网络假死,`pool restart --account <id>`
3. 重启:`xianyu-agent pool restart --account <id>`(daemon 会重建该账号的 Worker)。
4. 风控期间想整体停手:用 `account pause-reply` 暂停自动回复,而不是把账号停掉——停账号会断开
   连接、错过买家的新消息;暂停只影响自动回复,到点自动恢复。

## 注意

- `pool start-all` 是前台进程;跨进程 `pool stop` 只改 DB 标记,需到运行终端按 Ctrl+C 才能真正停止。
- 账号 `disabled` 不会出现在 `start-all` 的启动列表里。
- 多副本部署时账号归属由租约决定:同一账号任一时刻只有一个 owner,接管失败会记录在
  `pool status` 的 `takeover` 列,需要人工批准的部署要先 `pool takeover-approve`。
