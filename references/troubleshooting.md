# 排错速查

按「先定位是哪一层，再对症」的顺序排查。签到链路只有四层：
**找凭据文件 → 取明文令牌 → 调接口 → 判结果**。

## 一、快速定位：跑 doctor 和 status

```bash
python scripts/workbuddy_checkin.py doctor    # 离线：凭据格式 + 运行时能力
python scripts/workbuddy_checkin.py status    # 联网：真实服务端认证
```

- `doctor` 返回 `AUTH_READY` → 凭据与运行时都没问题。
- `doctor` 通过但 `status` 非 200 → 问题在服务端侧（令牌过期 / 账号权限）。
- 两个都失败 → 先看下面的原因码表。

> `doctor` **不解密、不联网**，通过它不代表服务端认证已通过。

## 二、按原因码排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `NO_AUTH` | 凭据文件不存在 | 打开 WorkBuddy 桌面端登录一次；输出里的 `looked_in` 会列出全部探测路径；或用 `WORKBUDDY_AUTH_FILE` 指定路径 |
| `MISSING_SESSION` | 文件在，但缺 `auth.accessToken` 或 `account.uid` | 客户端登录态不完整，重新登录 |
| `RUNTIME_NOT_FOUND` | 找不到 WorkBuddy 可执行文件 | 确认已安装；或用 `WORKBUDDY_EXE` 指定完整路径 |
| `RUNTIME_UNAVAILABLE` | 客户端运行时没有 `electron_browser_workbuddy_storage` 绑定 | 客户端版本过旧或不是 WorkBuddy；升级客户端 |
| `KEY_MISMATCH` | 凭据与所选客户端的密钥不匹配（多版本客户端共存） | 用 `WORKBUDDY_EXE` 指向写出该凭据的那个客户端 |
| `UNSUPPORTED_ENVELOPE` | 加密格式不是 `sym-v1 / suite 1` | 客户端换了加密方案，需要更新脚本 |
| `DECRYPT_FAILED` | GCM 认证解密失败 | 凭据文件损坏，重新登录客户端 |
| `HELPER_TIMEOUT` | 解密子进程超时 | 通常伴随客户端卡死；重启客户端后重试 |
| `INVALID_FORMAT` | 凭据或信封结构非法 | 检查是否被别的工具改写；重新登录客户端 |
| `AUTH_REJECTED`（HTTP 401） | 令牌过期 | 打开客户端刷新登录态，次日自动恢复 |
| `FORBIDDEN`（HTTP 403） | 账号权限或活动条件不满足 | 检查账号是否具备该活动资格；**不能据此判断登录过期** |
| `NETWORK` | 网络不可达 | 电脑刚开机/唤醒时常见，脚本会按 5/15/30/60/90 秒退避重试 |
| `TIMEOUT` | 本次运行时间预算耗尽 | 网络太慢或反复失败；可调大 `WORKBUDDY_BUDGET_SECONDS` |
| `INACTIVE` | 签到活动当前不在进行中 | 正常状态，无需处理 |

## 三、几个高危误区

**1. 不要用响应体子串判断 401。**
响应体带随机 `requestId`（UUID），里面偶发包含 `401` 字样。用 `grep 401` 之类的做法会
以约 0.5% 的概率误判「令牌过期」，在调 `daily-checkin` 之前就退出——当天积分没领到、
连签中断。一律用真实的 HTTP 状态码判断。

**2. 不要假设 `today_checked_in` 字段可靠。**
实测该字段在签到成功后仍可能为 `false`。幂等性必须靠 `daily-checkin` 的
`code=10001` 兜底，而不是只看这个字段。

**3. 不要靠模拟点击。**
Electron 的 `isTrusted` 过滤会让脚本注入的点击事件无效。任何「找按钮坐标点一下」
的方案都会失效。

**4. 401 先怀疑令牌来源，而不是请求路径或请求头。**
最常见的原因是读到了**过期的历史会话**（例如旧版 `state.vscdb` 里的旧令牌），
表现为「能拿到 token 但接口 401」。先确认凭据来源是当前登录态的那份文件——
用 `doctor` 看它实际选了哪个路径。

**5. 不要为了省事把令牌写进日志。**
排查时最容易犯的错。本 skill 会把已使用的令牌加入脱敏集合，但你自己临时加的
`print(token)` 不会被脱敏。

## 四、日志与观察点

运行后脚本同目录生成 `signin.log`：

```
[2026-10-08 14:30:06] {"result": "ALREADY", "report": "今日已签到（连续 2 天，累计 200 积分）", ...}
[2026-10-09 00:05:12] {"result": "CLAIMED", "report": "成功领取 100 积分（连续 3 天，累计 300 积分）", ...}
```

`result` 取值：`CLAIMED`（领取成功）/ `ALREADY`（今日已签）/ `INACTIVE`（活动未开始）
/ `AUTH_REJECTED` / `FORBIDDEN` / `NETWORK` / `TIMEOUT` / `NO_AUTH` / `ERROR`。

`needs_attention: true` 表示需要人工处理。ERROR 类结果**同时**写 stdout 和日志——
即使命令名拼错（比如把 `silent` 写成 `slient`），也不会静默丢掉失败记录。

## 五、验证「是不是真的签上了」

别只看脚本报了什么，去看服务端状态：

```bash
python scripts/workbuddy_checkin.py status
```

关注返回里的 `data.today_checked_in`、`data.streak_days`、`data.total_credits`
和 `data.checkin_dates`（实际签到日期数组）。这才是服务端的真实账本。
