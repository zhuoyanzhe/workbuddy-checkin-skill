---
name: workbuddy-checkin-skill
description: WorkBuddy 每日积分自动签到。读取本机 WorkBuddy 桌面端登录态（明文 JSON 或 sym-v1 加密信封），调用官方签到接口领取每日积分（100 积分/天，连续第 7 天 1000 积分），幂等可重复执行，并可注册每日定时任务实现无人值守。当用户提到「WorkBuddy 签到」「领积分」「每日积分」「自动签到」「签到断连」「Buddy 加油站」「checkin」「credits」时使用本 skill。
version: "1.0.0"
license: MIT
agent_created: true
---

# WorkBuddy 每日积分自动签到

自动领取 WorkBuddy「Buddy 加油站」的每日积分（100 积分/天，连续第 7 天 1000 积分）。
全流程在本机完成：读取本地登录态 → 调用官方签到接口。无后端、无第三方、令牌不落盘。

## 它做什么 / 不做什么

做：

1. 读取本机 WorkBuddy 桌面端的登录态，得到 `accessToken`。
2. 调用官方接口查状态 → 未签到则领取今日积分。
3. 幂等：今日已签到直接跳过，同一天重复运行不产生副作用。
4. 可选：注册系统定时任务，实现每天无人值守自动领取。

不做：

- 不模拟点击 GUI 按钮。Electron 的 `isTrusted` 过滤会让注入的点击无效，纯 UI 自动化必然失效。
- 不把令牌上传到任何第三方，不写入日志，不回显到终端。
- 不修改 WorkBuddy 客户端本体，不自动下载安装任何运行时。

## 原理

### 1. 凭据：先看格式，再谈解密

WorkBuddy 桌面端登录后会把会话写到本机。`auth.accessToken` 有**两种形态**，脚本都支持：

| 字段格式 | 表现 | 脚本处理 |
|---|---|---|
| 明文 | `auth.accessToken` 是字符串 | 直接使用，纯 Python 即可 |
| 加密 | `auth.accessToken` 是 `{"$wbEncrypted":1,"envelope":"..."}` | 交给客户端自带的 Electron 运行时在**隐藏子进程**内解密（见下） |

本机实测当前客户端（5.7.x）写的是**加密**形态。用 `doctor` 看 `credential_format`
字段的实际取值即可确认，不要靠猜。

> 同目录下若还存在 `workbuddy-desktop-ai.info`，那是**国际版**（`workbuddy.ai`）的登录态，
> 不是国内版。本 skill 只处理国内版 `workbuddy-desktop.info`；要用别的凭据文件，
> 设 `WORKBUDDY_AUTH_FILE` 显式指定。

加密信封 `sym-v1 / suite 1` 的解密链：

1. 把固定 JS 通过 stdin 喂给 `WorkBuddy.exe`（带 `ELECTRON_RUN_AS_NODE=1`，`CREATE_NO_WINDOW`）。
2. JS 内用原生绑定 `process._linkedBinding('electron_browser_workbuddy_storage').loggerGet()` 取出 key payload，得到 `atRestSecretKey`。
3. 以 `key = sha256(atRestSecretKey)` 做 **AES-256-GCM 认证解密**（AAD 为 `WB-AAD\0` 起始的定长结构），并校验 `keyId` 一致性。
4. 明文令牌经 stdout 管道回传，**密钥只留在子进程内**，不落盘、不进命令行、不进环境变量。

细节与字段格式见 `references/credential-bridge.md`。

### 2. 接口

```
查状态:  POST {endpoint}/v2/billing/meter/checkin-activity-status
领积分:  POST {endpoint}/v2/billing/meter/daily-checkin
```

- `endpoint` 默认取凭据里的 `auth.endpoint`，缺省回落到 `https://copilot.tencent.com`。
- 请求头对齐桌面端的 `buildHeaders`：`Authorization: Bearer <token>`、`X-User-Id: <account.uid>`；
  有 `auth.domain` 时加 `X-Domain`；企业账号另加 `X-Enterprise-Id` / `X-Tenant-Id`。
- 请求体为 `{}`。

### 3. 幂等与失败分类

- `daily-checkin` 返回 **HTTP 400 + `code:10001`（今天已签到，请明天再来）** 视为成功，不算失败。
- **不要用响应体子串判断 401**：响应体里带随机 `requestId`，偶发包含 `401` 会误判为令牌过期，
  导致当天不签到、连签中断。一律用真实 HTTP 状态码判定。
- 只有「本机网络不可达」和「服务端 5xx」才重试；4xx 是业务规则，重试无意义。

## 文件结构

```
workbuddy-checkin-skill/
├── SKILL.md                   # 本文件：技能说明（WorkBuddy 读它来触发与执行）
├── README.md                  # 仓库落地页：面向人的安装与用法
├── SECURITY-AUDIT.md          # 安全审计报告（0 个 P0/P1）
├── LICENSE                    # MIT
├── scripts/
│   ├── workbuddy_checkin.py   # 核心脚本：凭据 → 查状态 → 领取（幂等）
│   └── install-task.ps1       # Windows 计划任务一键安装 / 卸载
└── references/
    ├── credential-bridge.md   # 凭据格式与 sym-v1 解密桥原理
    └── troubleshooting.md     # 排错速查
```

运行后在脚本同目录生成 `signin.log`（只含积分/连签/成功失败，绝不含令牌）。

## 依赖

| 依赖 | 用途 | 说明 |
|---|---|---|
| WorkBuddy 桌面端（已登录过至少一次） | 提供本地登录态 | 必须，也是解密桥的运行时来源 |
| Python 3 | 运行核心脚本 | 任意 3.x，**无第三方包** |
| pythonw.exe（Windows 可选） | 无窗口静默运行 | 定时任务推荐，避免弹黑框 |

## 快速开始

先做一次离线自检，确认凭据格式与解密能力：

```bash
python scripts/workbuddy_checkin.py doctor
```

再查一次状态（只读，不领取）：

```bash
python scripts/workbuddy_checkin.py status
```

立即签到一次（幂等，今日已签会返回 `ALREADY`）：

```bash
python scripts/workbuddy_checkin.py daily
```

`doctor` 通过只说明格式与运行时没问题，**不代表**服务端认证已通过；以 `status` 返回 HTTP 200 为准。

## 实测验证矩阵

以下为 2026-10-08 在本机（Windows + WorkBuddy 桌面端 5.7.x / Electron 37.10.3）的真实结果：

| 验证项 | 命令 | 结果 |
|---|---|---|
| 加密凭据识别与运行时探测 | `doctor` | `AUTH_READY`，`credential_format=sym-v1` |
| 解密 + 真实服务端认证 | `status` | **HTTP 200**，`today_checked_in=true`、`streak_days=2`、`total_credits=200` |
| 明文凭据格式识别（旧版客户端路径） | `doctor` | `AUTH_READY`，`credential_format=plaintext` |
| 写接口构造与幂等分支 | `claim` | **HTTP 400 + `code:10001`**「今天已签到，请明天再来」→ 按 `ALREADY` 处理，退出码 0 |
| 无人值守链路 | 计划任务实跑 | `LastTaskResult=0`，`signin.log` 正常落盘 |

**尚未验证的部分**（如实说明）：`未签 → HTTP 200 + credit` 这条「真正领到分」的分支，
在当天已签到的账号上无法复现。可这样确认：次日首次运行后看 `signin.log` 是否出现
`"result": "CLAIMED"`。`claim` 已实测到服务端对写请求的**鉴权通过**（鉴权失败会返回 401，
而不是 10001），因此请求构造正确性已经得到证明。

## 在 WorkBuddy 里用（Agent 自动化）

把本目录作为工作目录新建任务，粘贴：

```
学习 SKILL.md，并帮我配置 WorkBuddy 每日自动签到，返回执行结果。
```

WorkBuddy 会读取 `SKILL.md`、执行 `scripts/workbuddy_checkin.py`、并按需调用 `automation_update`
创建 recurring 自动化。多时间点可用单条 RRULE 表达：

```jsonc
{
  "name": "WorkBuddy 每日积分签到",
  "scheduleType": "recurring",
  "rrule": "FREQ=DAILY;BYHOUR=0,9,13,18,21;BYMINUTE=5",
  "status": "ACTIVE",
  "prompt": "运行 python scripts/workbuddy_checkin.py daily，读取输出并汇报：领取了多少积分 / 今日已签到 / 令牌失效需打开客户端刷新。"
}
```

> 注意：自动化会用一次 Agent 对话，**成本高于本地定时任务**。追求零成本请用下面的系统级定时。

## 设置定时任务（零 Token，推荐）

电脑非全天开机时，配置多个时间点幂等补签——只要任一时刻开机就能签上。

### Windows（任务计划程序）

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-task.ps1
```

会注册两个任务：`WorkBuddyAutoSignin`（每天 00:05）与 `WorkBuddyAutoSigninPoll`
（01/05/09/13/17/21 各一次，已签则静默）。卸载：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-task.ps1 -Uninstall
```

用多个独立 Daily 触发器而不是「每天一次 + 每 N 小时重复」，是因为重复实例一旦错过
（关机/睡眠）就永久跳过，独立触发器则每次错过都有机会补跑。

### macOS / Linux（crontab）

```bash
crontab -e
5 0,9,13,18,21 * * * /usr/bin/python3 /path/to/scripts/workbuddy_checkin.py silent >> /path/to/signin.log 2>&1
```

### macOS（launchd）

写 `~/Library/LaunchAgents/com.user.workbuddy-checkin-skill.plist`，`StartCalendarInterval`
用数组配置多时间点，然后 `launchctl load` 该 plist。

## 命令一览

| 命令 | 作用 | 输出 |
|---|---|---|
| `doctor` | 离线检查凭据格式与运行时能力 | stdout |
| `status` | 只查签到状态 | stdout |
| `claim` | 只领取签到 | stdout |
| `daily` | 查状态 → 未签则领 | stdout |
| `silent` | 同 `daily`，结果写 `signin.log` | 日志 |
| `silent-poll` | 同 `silent`，但「已签且无事发生」时不写日志 | 日志（按需） |

## 环境变量

| 变量 | 作用 |
|---|---|
| `WORKBUDDY_EXE` | 指定 WorkBuddy 可执行文件（自动探测失败时用） |
| `WORKBUDDY_AUTH_FILE` | 指定凭据文件路径 |
| `WORKBUDDY_SIGNIN_LOG` | 自定义日志路径 |
| `WORKBUDDY_BUDGET_SECONDS` | 单次运行时间预算（默认 240s，上限 540s） |
| `WORKBUDDY_SIGNIN_LOG_EMPTY=1` | 让 `silent-poll` 空跑也写日志 |

## 排错

见 `references/troubleshooting.md`。最常见的三条：

- `RUNTIME_NOT_FOUND`：没找到客户端。装好并登录 WorkBuddy 桌面端，或用 `WORKBUDDY_EXE` 指定。
- `NO_AUTH`：凭据文件不存在。先确认客户端已登录；输出里的 `looked_in` 会列出全部探测路径。
  若确实是把路径搬走了，用 `WORKBUDDY_AUTH_FILE` 显式指定。`doctor` 的 `auth_file`
  字段可看到当前实际命中的是哪一个。
- `AUTH_REJECTED`（HTTP 401）：令牌过期。打开客户端刷新登录态，次日自动恢复。

## 安全说明

> **凭据即账号密码**：解密出的 `accessToken` 等同账号密码。

- 令牌仅在内存中使用，管道即时消费，**不落盘、不回显、不写日志**。
- 解密成功的令牌会加入脱敏集合，任何输出里出现都会被替换为 `[REDACTED]`。
- 密钥只存在于解密子进程内，父进程拿不到，也不写任何临时文件。
- 网络访问仅发往凭据里声明的官方 endpoint（默认 `copilot.tencent.com`），不访问第三方。
- 请勿用于他人账户、批量注册刷分或任何违反 WorkBuddy 用户协议的用途。

完整审计结论见 [`SECURITY-AUDIT.md`](SECURITY-AUDIT.md)（P2 安全，0 个 P0/P1，零第三方依赖）。

## 所需权限

| 权限 | 范围 |
|---|---|
| 本地代码执行 | 仅本 skill 的 `workbuddy_checkin.py` / `install-task.ps1`，用户手动或定时触发，非后台常驻 |
| 本地文件读取 | 仅用户目录下的 WorkBuddy 登录态文件 |
| 子进程启动 | 仅 WorkBuddy 客户端可执行文件（用于解密加密凭据），隐藏窗口、限时 10s、管道限容量 |
| 网络访问 | 仅凭据中声明的官方签到接口 |
| 定时任务 | 仅在用户显式运行 `install-task.ps1` 时写入 |

## 免责声明

签到接口系从桌面端 `app.asar` 逆向所得，**非官方接口**，服务端改版即可能失效。使用者自行承担风险。
