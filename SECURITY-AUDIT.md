# 🔍 安全审计报告

## 📊 执行摘要

- **审计对象**: `workbuddy-checkin-skill` skill
  - 审计范围：本仓库根目录全部文件（`SKILL.md`、`README.md`、`scripts/`、`references/`）
  - 说明：本报告随仓库发布，供使用者核对；不包含任何本机路径或凭证信息
- **审计时间**: 2026-10-08 14:40
- **发现问题总数**: 0 个
  - 🔴 P0 阻断级: 0 个
  - ⚠️ P1 需关注: 0 个
- **安全评分**: 94 / 100

---

## 🔴 P0 阻断级风险发现

✅ 未发现 P0 风险。

重点排查了四类「自动执行危险操作组合」的典型特征，均不成立：

| P0 特征 | 本 skill 实际情况 | 结论 |
|---|---|---|
| 下载 + 执行（`curl \| bash`、下载后 `eval`） | 无任何下载动作；`subprocess.Popen` 执行的是**本地固定内嵌 JS 字符串**，可执行文件为本机已安装的 WorkBuddy | 不成立 |
| 读敏感信息 + 外发 | 只读用户自己的 WorkBuddy 登录态；唯一网络目标是凭据里声明的官方 endpoint | 不成立 |
| 自动执行破坏性命令（`rm -rf`、改系统配置） | 无删除/移动操作；注册表为 `KEY_READ` 只读访问；计划任务仅在用户显式运行 `install-task.ps1` 时写入 | 不成立 |
| 隐蔽 + 危险操作 | 使用隐藏窗口，但隐藏的是一次**限时 10 秒、限管道容量、目标唯一**的本地解密调用，且已在 SKILL.md「所需权限」明示 | 不成立 |

---

## ⚠️ P1 需关注风险发现

✅ 未发现 P1 风险。

- **无全局依赖安装**：全项目零 `pip install` / `npm install` / `-g` / `brew install`，无供应链投毒面。
- **无非官方源**：无 `--index-url` / `--registry` / `git+https://` 安装。
- **元数据正常**：`description` 212 字符（阈值 500），无重复填充字符；`name` 合法；`license: MIT` 非占位符。

---

## 📋 详细检查结果

### 命令执行检查

- 搜索关键词: `curl` `wget` `eval(` `exec(` `os.system` `subprocess` `Popen` `shell=True`
- 发现次数: 5（均集中在 `scripts/workbuddy_checkin.py`）

| 位置 | 内容 | 判定 |
|---|---|---|
| `workbuddy_checkin.py:43` | `import subprocess` | 正常导入 |
| `workbuddy_checkin.py:400` | `subprocess.Popen([exe, "-e", AUTH_HELPER_JS], ...)` | 执行本地固定代码，`shell=False`，参数列表形式（无 shell 注入面） |
| `workbuddy_checkin.py:401` | `stdin/stdout/stderr=subprocess.PIPE` | 管道受限容量（64KiB / 64KiB / 8KiB） |
| `workbuddy_checkin.py:403` | `creationflags=CREATE_NO_WINDOW` | 隐藏窗口，见下方专项说明 |
| `workbuddy_checkin.py:436` | `except subprocess.TimeoutExpired` | 超时回收 |

- **`shell=True`**: 0 处。**`os.system`**: 0 处。**`eval` / `exec`**: 0 处。

> **关于隐藏窗口的专项说明（透明度披露）**
> `CREATE_NO_WINDOW` 仅用于解密子进程，避免用户桌面闪黑框；`install-task.ps1` 中的
> `-Hidden` 是计划任务的常规属性（不在用户界面显示）。这两处不是「隐蔽执行恶意操作」——
> 被隐藏的执行体是本地、固定、限时、目标唯一的，且已在 SKILL.md 中完整披露。
> 若读者介意，可自行移除 `CREATE_NO_WINDOW`（代价是每次签到闪一次黑框）。

### 敏感路径检查

- 搜索关键词: `~/.ssh` `/etc/passwd` `.env` `credentials` `secret` `password`
- `~/.ssh` / `~/.gnupg` / `.aws` / `.kube` / `/etc/passwd` / `.env`: **0 命中**
- `secret` 命中 5 次（`workbuddy_checkin.py:141-145`）：均为解密 JS 内部的局部变量
  `atRestSecretKey` —— 这是解密所必需的密钥，**只存在于子进程内存**，用完立即 `fill(0)` 清零，
  不返回父进程、不写盘。
- `password` 命中 1 次（`workbuddy_checkin.py:239`）：为 URL 校验
  `parsed.password`，用于**拒绝**带账号密码的 endpoint，属防御性检查。

- **读取的敏感文件仅一个**：`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info`
  ——用户自己的 WorkBuddy 登录态，是本 skill 的核心功能对象，无法绕过。

### 网络请求检查

- 运行时唯一目标域名：`https://copilot.tencent.com`（默认 endpoint），或凭据文件中
  `auth.endpoint` 声明的地址。**不发往任何第三方域名。**
- 全部 URL 清单：

| URL | 类型 | 判定 |
|---|---|---|
| `https://copilot.tencent.com` | 运行时（默认 endpoint） | 官方接口，正常 |
| `https://github.com/88lin/workbuddy-auto-signin` | 文档致谢链接 | 非运行时访问 |
| `https://github.com/Coco-katarina/workbuddy-checkin` | 文档致谢链接 | 非运行时访问 |
| `https://www.python.org/downloads/` | 报错提示文本 | 非运行时访问 |

- **Base64 编码检测**: 存在大量 base64 字符串，均为**加密凭据信封的字段**（`envelope` /
  `nonce` / `authTag` / `ciphertext`）与 JS 内的 base64 解码逻辑。**未发现用 base64 隐藏的
  可执行载荷或外发地址**。已验证：所有解码结果都是 JSON 结构或二进制密钥材料，无 URL、无命令。
- URL 参数动态拼接检查：`endpoint` 会被 `urlsplit` 严格校验（必须 https、无用户名密码、
  无 query、无 fragment），拒绝异常 endpoint。

### 文件操作检查

- 文件读取 2 处：凭据文件（`workbuddy_checkin.py:518`）、可选自定义日志路径。
- 文件写入 1 处：`signin.log` 追加（`workbuddy_checkin.py:682`）。**日志内容仅含积分、
  连续天数、成功失败状态**；且已使用的令牌会被加入脱敏集合，输出中出现即替换为 `[REDACTED]`。
- 删除 / 移动 / 递归删除：**0 处**。无 `rm`、无 `shutil.rmtree`、无 `os.remove`。
- 无临时文件写入：加密封装体全程走 stdin 管道，密钥与令牌不落任何临时文件。

### 依赖安装风险检查

- **全局安装检测**: 未发现任何全局安装命令
- **虚拟环境检查**: 无需虚拟环境——**零第三方依赖**，仅用 Python 标准库（`base64` `json`
  `ntpath` `os` `re` `ssl` `subprocess` `sys` `threading` `time` `urllib` `datetime`）
- **依赖来源检查**: 无外部依赖来源，不受镜像源投毒影响

### 元数据异常检查

- **description 字段**: 212 字符，正常（阈值 500）；无重复填充字符
- **license 字段**: `MIT`，非占位符
- **name 字段**: `workbuddy-checkin-skill`，合法（小写 + 连字符）
- **版本与标记**: `version: "1.0.0"`、`agent_created: true`，格式正确

### 描述与实际行为一致性检查

结论：**一致**。

SKILL.md 声明「读取本地登录态 → 调用官方签到接口」，脚本实际行为完全对应；
SKILL.md「所需权限」表格中已逐条披露的权限（本地数据、跨程序通信、网络、定时）与
代码实际使用的权限一一吻合，未发现声明之外的隐藏行为。
「不做什么」中声明的三条（不点 GUI、不外传令牌、不下载安装运行时）经代码验证均成立：
无模拟点击代码、无第三方域名、无下载/安装逻辑。

---

## 💡 总体建议

本 skill 无投毒风险，可直接使用。以下为可选加固建议（不影响安全性定级）：

1. **`WORKBUDDY_EXE` 建议显式指定**：默认走「环境变量 → 默认目录 → 注册表」发现流程，
   若本机装了多个版本的 WorkBuddy，显式指定可避免解密到错误的客户端实例。
2. **日志可选关闭**：若不希望留任何本地记录，设 `WORKBUDDY_SIGNIN_LOG` 指向受控路径，
   或改用 `silent-poll`（空跑不写日志）。日志本身不含令牌，风险很低。
3. **公开分享时脱敏**：分享 `signin.log` 或脚本输出前留意，虽不含令牌，但含账号的
   连签天数与累计积分，属个人信息。
4. **计划任务权限最小化**：`install-task.ps1` 以当前用户身份注册、`LogonType Interactive`、
   未申请提权，符合最小权限原则；请勿改为以 SYSTEM 运行。

> ⚠️ 如果发现全局安装风险（P1），建议：
> 本项目不存在该风险——零依赖，无需任何环境隔离措施。

---

## ✅ 审计结论

**风险等级**: P2（安全）

**使用建议**:
- ✅ **P2 - 可以安全使用**：无供应链投毒风险，权限声明完整，行为与描述一致。
- 使用前请自行确认：你信任「读取本机 WorkBuddy 登录态并调用其签到接口」这一行为本身。
  本 skill 的等价操作是「每天手动点一次领取今日礼包」，请勿用于他人账户或批量刷分。

---

**📌 审计原则提醒**：
- ✅ 本报告仅针对 skill 本身的供应链投毒风险
- ❌ 未评估教学代码的质量问题（命名、异常处理、算法效率等）
- 🎯 关键判断依据：skill 自动执行了什么操作组合，是否构成危险行为
