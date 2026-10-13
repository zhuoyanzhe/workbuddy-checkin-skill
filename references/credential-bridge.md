# 凭据格式与 sym-v1 解密桥

本文记录「为什么不能直接读 token」以及脚本是怎么绕过去的。**不需要**在正常使用前阅读，
只在凭据相关报错、或想理解实现原理时查阅。

## 一、凭据文件在哪

| 平台 | 路径 |
|---|---|
| Windows | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info` |
| macOS | `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info` |
| Linux（CodeBuddy CLI） | `~/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info` |

### 目录名为什么叫 CodeBuddyExtension

**这不代表本机需要安装 CodeBuddy。** 拆开 WorkBuddy 自己的 `app.asar` 可以看到：

```js
USER_DATA_DIR_NAME      = `.codebuddy`
EXTENSION_DATA_DIR_NAME = `CodeBuddyExtension`
```

`CodeBuddyExtension` 是二进制里**写死的目录名常量**，与品牌无关；文件名反倒按产品名拼出来
（`getSharedAuthDirectory() + `${productName}.info``），所以那个目录里躺着的正是
`workbuddy-desktop.info`。同一份 asar 里的日志收集逻辑也同时读
`AppData\Local\WorkBuddy\logs` 与 `AppData\Local\CodeBuddyExtension\Logs`——两个目录都属于
WorkBuddy 自己。**装了 WorkBuddy 就会有这个目录。**

### 候选路径与兜底顺序

目录名是硬编码常量，上游一旦改名就会失效，所以脚本按顺序探测多个候选、命中即用。
真机命中了哪一个，看 `doctor` 输出的 `auth_file` 字段：

1. `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info` ← 当前版本
2. `%LOCALAPPDATA%\WorkBuddyExtension\…` ← 品牌化改名后的可能形态（本机已存在同名目录，
   供 VSCode 扩展分支使用）
3. `%LOCALAPPDATA%\WorkBuddy\…` ← 再兜一层
4. macOS 为 `~/Library/Application Support/<前缀>/…`，前缀规则同上
5. `~/.workbuddy/auth/workbuddy-desktop.info` ← 最终兜底

覆盖：设 `WORKBUDDY_AUTH_FILE` 指向任意路径，跳过全部探测。

> 同目录下的 `workbuddy-desktop-ai.info` 属于**国际版**（`workbuddy.ai`），不是本 skill
> 的处理对象——它的签发域、字段格式与国内版都可能不同。本 skill 只认国内版那一份。

顶层结构：

```jsonc
{
  "account": { "uid": "...", "uin": "...", "type": "personal", ... },
  "auth": {
    "accessToken": "<字符串 或 加密信封>",
    "refreshToken": "...",
    "endpoint": "https://copilot.tencent.com"
  }
}
```

脚本只读 `auth.accessToken` / `account.uid` / `auth.domain` / `account.enterpriseId`
和 `auth.endpoint`。**refreshToken 不读、不解密、不续期**——登录与续期始终由客户端负责。

## 二、加密信封长什么样

当前客户端（实测 5.7.x）写出的 `accessToken` 不是字符串，而是：

```jsonc
{
  "$wbEncrypted": 1,
  "envelope": "eyJzdWl0ZSI6MSwia2V5SWQiOiI5MTI3ZGVhMWI0NDAyMGE3Iiwibm9uY2UiOiIuLi4i..."
}
```

`envelope` 是 base64 编码的 JSON：

```jsonc
{
  "suite": 1,
  "keyId": "9127dea1b44020a7",
  "nonce": "<base64, 12 字节>",
  "authTag": "<base64, 16 字节>",
  "ciphertext": "<base64>"
}
```

判定规则（脚本会逐项校验，任一不符直接报错，不做猜测）：

- `$wbEncrypted === 1`，且对象**只有** `$wbEncrypted` 和 `envelope` 两个键；
- `envelope` 是 **canonical** base64（`base64.b64decode` 再编码必须与原串完全一致）；
- 解出的对象**恰好**是 `suite / keyId / nonce / authTag / ciphertext` 五个键；
- `keyId` 匹配 `^[0-9a-f]{16}$`；`nonce` 12 字节、`authTag` 16 字节。

## 三、密钥从哪来：客户端原生绑定

密钥**不在磁盘上的任何文件里**。它由客户端 Electron 运行时的一个原生绑定在进程内提供：

```js
process._linkedBinding('electron_browser_workbuddy_storage').loggerGet()
// → '{"version":1,"atRestSecretKey":"<base64, 32 字节>","atRestDeveloperPublicKey":"..."}'
```

这也解释了为什么「翻遍用户目录找不到密钥」——它压根没落盘。

## 四、解密流程

```
Python 主进程                          WorkBuddy.exe（Electron，隐藏子进程）
    │                                          │
    │  stdin: {"version":1,"operation":"decrypt",
    │          "value":{加密信封}}  ─────────▶  │
    │                                          │  loggerGet() 取 atRestSecretKey
    │                                          │  key = sha256(atRestSecretKey)
    │                                          │  校验 sha256(key)[:16] == keyId
    │                                          │  AES-256-GCM 解密 + 校验 authTag
    │  stdout: {"version":1,"ok":true,         │
    │           "accessToken":"<明文>"}  ◀─────│
    ▼                                          ▼
 构造 Authorization: Bearer <明文>         进程退出，密钥随之消失
```

关键技术点：

1. **AAD 构造是定长拼接**，非自由格式：

   ```
   "WB-AAD\0" | 0x01 | len32("WBEV1") | len32("sym-v1") | 0x00000001 | len32(keyId) | 0x02 0x00 0x00
   ```

   其中 `len32(s)` 是「4 字节大端长度 + UTF-8 字节」。AAD 错一个字节，GCM 校验就失败。

2. **`keyId` 是密钥的指纹**：`sha256(key).hexdigest()[:16]`。先比对再解密，能在客户端版本
   与凭据不匹配时给出 `KEY_MISMATCH` 而不是笼统的解密失败。

3. **子进程约束**（防泄漏、防卡死）：
   - `shell=False`，可执行文件用绝对路径，Windows 加 `CREATE_NO_WINDOW`；
   - 环境变量剔除 `NODE_*` / `ELECTRON_*` / `WORKBUDDY_*`，只注入 `ELECTRON_RUN_AS_NODE=1`；
   - stdin/stdout 各限 64 KiB，stderr 限 8 KiB，超量直接 kill；
   - 超时上限 10 秒，且不得超过本轮剩余时间预算；
   - 加密封装体只走 stdin，**不进 argv、不进环境变量**。

4. **失败只用固定原因码**，不把原始异常或捕获内容带进日志：

   | 原因码 | 含义 |
   |---|---|
   | `INVALID_FORMAT` | 凭据/信封结构非法 |
   | `UNSUPPORTED_ENVELOPE` | 不是 `suite 1`（例如未来的新格式） |
   | `RUNTIME_NOT_FOUND` | 找不到 WorkBuddy 可执行文件 |
   | `RUNTIME_UNAVAILABLE` | 客户端运行时没有需要的原生绑定 |
   | `KEY_MISMATCH` | 凭据与所选客户端的密钥不匹配 |
   | `DECRYPT_FAILED` | 认证解密失败（GCM tag 校验不过） |
   | `HELPER_TIMEOUT` | 子进程超时 |
   | `HELPER_PROTOCOL` | 子进程返回不符合协议 |

## 五、运行时可执行文件怎么找

顺序：`WORKBUDDY_EXE` 环境变量 → 默认安装目录 → 注册表兜底。

- 默认目录：`%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe`，以及 `ProgramFiles` / `ProgramFiles(x86)`。
- 注册表兜底：遍历 `HKLM/HKCU`（含 `KEY_WOW64_64KEY` / `KEY_WOW64_32KEY` 两个视图）的
  `...\CurrentVersion\Uninstall`，匹配产品名 `WorkBuddy[ <版本>]`，读数 `InstallLocation` 与
  `DisplayIcon`。**只读**，不执行卸载命令，不修改注册表。正确解析带引号、带逗号、带图标索引
  和 `REG_EXPAND_SZ` 的路径值。

## 六、已知边界

- 只支持 `sym-v1 / suite 1`。客户端若更换加密方案，会明确返回 `UNSUPPORTED_ENVELOPE` 而不是猜。
- `atRestSecretKey` 全零视为不可用（哨兵值），直接判 `RUNTIME_UNAVAILABLE`。
- 客户端私有绑定属于内部接口，未来版本可能变化；本 skill 不承诺跨版本自动兼容。
- 域名只取凭据里的 `auth.endpoint`，缺省回落 `https://copilot.tencent.com`。若凭据里
  没有该字段而目标又不是国内域名，接口会返回 401——这是「令牌读到了但接口 401」的一个成因。
