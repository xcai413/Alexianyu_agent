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
| 前台启动全部账号(带实时看板) | `xianyu-agent pool start-all --seconds 3600` |
| 前台启动单个账号 | `xianyu-agent pool start --account <id> --seconds 3600` |
| 标记某账号离线 | `xianyu-agent pool stop --account <id>` |
| 启用/禁用账号 | `xianyu-agent account enable/disable --id <id>` |
| 查看账号详情 | `xianyu-agent account show --id <id>` |

## 诊断指引

1. `pool status` 看 `db` 列:
   - `connected` + 心跳新鲜(≤1 分钟)→ 正常
   - `error` + `last_error` 有值 → 按错误处理
   - `disconnected` + `stop() called` → 被正常停止
2. 常见错误:
   - `XIANYU_WS_URL not configured` → `.env` 未配 WS 地址
   - `no cookie for account=...` → 先 `xianyu-agent auth login`
   - 心跳长时间不更新但状态 connected → 网络假死,重启该账号
3. 重启:`pool stop --account <id>` 后再 `pool start --account <id>`。

## 注意

- `pool start-all` 是前台进程;跨进程 `pool stop` 只改 DB 标记,需到运行终端按 Ctrl+C 才能真正停止。
- 账号 `disabled` 不会出现在 `start-all` 的启动列表里。
