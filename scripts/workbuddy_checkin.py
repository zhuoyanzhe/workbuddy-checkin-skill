"""WorkBuddy 每日积分自动签到 —— workbuddy-checkin-skill 的核心脚本。

配套说明见 ../SKILL.md；凭据格式与解密桥原理见 ../references/credential-bridge.md。

凭据解密桥、接口路径与响应契约参考并复用自 88lin/workbuddy-auto-signin（MIT 协议）：
  https://github.com/88lin/workbuddy-auto-signin
本文件砍掉了成长中心（旅行礼物 / 派 Buddy / 盲盒 / 连登兑换 / 断登补签）等逻辑，
只保留「查状态 → 未签则领」这条主线，便于读者通读源码。

接口（从桌面端逆向所得，非官方）：
  POST {endpoint}/v2/billing/meter/checkin-activity-status   查询签到状态
  POST {endpoint}/v2/billing/meter/daily-checkin             领取今日积分（幂等）

响应契约：
  - 领取成功 : HTTP 2xx，body 含 credit 字段，如 {"credit": 100}
  - 今日已签 : HTTP 400 + {"code":10001,"msg":"今天已签到，请明天再来"}，幂等，不算失败
  - 认证拒绝 : HTTP 401；HTTP 403 单独作为权限/业务拒绝处理

凭据来源：
  - 明文  : 旧版客户端写出的 workbuddy-desktop.info（auth.accessToken 是字符串）
  - 加密  : 新版客户端写出的 $wbEncrypted / sym-v1 / suite 1 字段信封。
            解密方式是把固定 JS 喂给客户端自带的 Electron 运行时（隐藏子进程，
            走 stdin/stdout 管道），由它用原生绑定取出 at-rest 密钥并做 AES-256-GCM
            认证解密。密钥全程留在子进程内，不落盘、不进命令行、不进环境变量。

用法：
  python workbuddy_checkin.py doctor        # 离线检查凭据格式与运行时能力，不解密、不联网
  python workbuddy_checkin.py status        # 只查签到状态（调试）
  python workbuddy_checkin.py claim         # 只领取签到（调试，幂等）
  python workbuddy_checkin.py daily         # 查状态 → 未签则领，结果打到 stdout
  python workbuddy_checkin.py silent        # 同 daily，结果写 signin.log（配合 pythonw 静默运行）
  python workbuddy_checkin.py silent-poll   # 同 silent，但「已签且无事发生」时静默不写日志

任何模式下都不会打印令牌，输出可安全分享。
"""

import base64
import json
import ntpath
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime

# ---------------------------------------------------------------- 常量

DEFAULT_ENDPOINT = "https://copilot.tencent.com"

# 凭据文件名由客户端用「产品名」拼出（`${productName}.info` → `workbuddy-desktop.info`），
# 而外层目录名是二进制里写死的常量 EXTENSION_DATA_DIR_NAME，当前取值 `CodeBuddyExtension`，
# 与品牌无关 —— 所以目录叫 CodeBuddyExtension 并不代表本机需要安装 CodeBuddy。
# 本机已观测到 WorkBuddyExtension 目录同时存在（VSCode 扩展分支在用），说明上游随时可能
# 品牌化改名，故把可能的前缀全部列为候选、命中即用，避免改名后静默失效。
AUTH_FILENAME = "workbuddy-desktop.info"
AUTH_DIR_RELATIVE = os.path.join("Data", "Public", "auth", AUTH_FILENAME)
AUTH_DIR_PREFIXES = ("CodeBuddyExtension", "WorkBuddyExtension", "WorkBuddy")

# 伪 HTTP 码：区分「没拿到响应」的两种原因
CODE_NO_NETWORK = -1   # 连不上 / 超时
CODE_BUDGET_OUT = -2   # 本次运行的时间预算已耗尽，主动放弃后续请求

# 计划任务跑满 ExecutionTimeLimit 会被系统直接杀掉，届时日志还没写，当天记录整条丢失。
# 自设更小的预算，确保总能走到写日志那一步；上限与 install-task.ps1 的时限一一对应。
DEFAULT_BUDGET_SECONDS = 240.0
MAX_BUDGET_SECONDS = 540.0
REQUEST_TIMEOUT = 30
NETWORK_RETRY_DELAYS = (5, 15, 30, 60, 90)   # 刚开机 / 刚唤醒，网络要几十秒才就绪
SERVER_RETRY_DELAYS = (3, 10)                # 5xx 是服务端抖动，短促重试即可

AUTH_HELPER_TIMEOUT = 10.0
AUTH_INPUT_LIMIT = 65536
AUTH_OUTPUT_LIMIT = 65536
TOKEN_LIMIT = 32768

POLL_ACTIONS = ("silent-poll",)
_sensitive_values = set()
_started_at = None
_budget_seconds = DEFAULT_BUDGET_SECONDS

AUTH_REASONS = {
    "MISSING_SESSION": "本地未找到有效登录会话，请先登录 WorkBuddy 桌面端",
    "INVALID_FORMAT": "登录凭据格式无效，请检查凭据文件与客户端版本",
    "UNSUPPORTED_ENVELOPE": "尚不支持此加密凭据格式，请更新脚本；重新登录不会改变加密格式",
    "RUNTIME_NOT_FOUND": "未找到 WorkBuddy 客户端，请用 WORKBUDDY_EXE 指定其可执行文件",
    "INVALID_RUNTIME_PATH": "WORKBUDDY_EXE 不是可用的可执行文件，请核对路径",
    "RUNTIME_UNAVAILABLE": "客户端运行时不支持所需的原生存储接口，请检查客户端与脚本版本",
    "KEY_MISMATCH": "凭据与所选客户端的密钥不匹配，请用 WORKBUDDY_EXE 指定对应客户端",
    "DECRYPT_FAILED": "加密凭据认证失败，请检查客户端版本及凭据是否完整",
    "HELPER_TIMEOUT": "凭据处理超时，已停止子进程，请稍后重试",
    "HELPER_PROTOCOL": "客户端凭据助手返回无效结果，请检查客户端与脚本版本",
}

# 固定代码只出现在 argv；加密封装体经 stdin 传入。at-rest 密钥不离开子进程。
# 仅支持 sym-v1 / field / suite 1。
AUTH_HELPER_JS = r"""
'use strict';
const crypto = require('crypto');
const inputLimit = 65536;
const failure = reason => { throw {reason}; };
const object = x => x !== null && typeof x === 'object' && !Array.isArray(x);
function base64(value, length) {
  if (typeof value !== 'string' || value.length > inputLimit) failure('INVALID_FORMAT');
  const bytes = Buffer.from(value, 'base64');
  if (bytes.toString('base64') !== value || (length !== undefined && bytes.length !== length))
    failure('INVALID_FORMAT');
  return bytes;
}
function utf8(bytes) {
  const text = bytes.toString('utf8');
  if (!Buffer.from(text, 'utf8').equals(bytes)) failure('INVALID_FORMAT');
  return text;
}
function decode(value) {
  if (!object(value) || Object.keys(value).sort().join(',') !== '$wbEncrypted,envelope' || value.$wbEncrypted !== 1)
    failure('UNSUPPORTED_ENVELOPE');
  let envelope;
  try { envelope = JSON.parse(utf8(base64(value.envelope))); }
  catch (e) { failure(e.reason || 'INVALID_FORMAT'); }
  if (!object(envelope) || !Number.isInteger(envelope.suite)) failure('INVALID_FORMAT');
  if (envelope.suite !== 1) failure('UNSUPPORTED_ENVELOPE');
  if (Object.keys(envelope).sort().join(',') !== 'authTag,ciphertext,keyId,nonce,suite' ||
      typeof envelope.keyId !== 'string' || !/^[0-9a-f]{16}$/.test(envelope.keyId)) failure('INVALID_FORMAT');
  return {keyId: envelope.keyId, nonce: base64(envelope.nonce, 12),
    tag: base64(envelope.authTag, 16), ciphertext: base64(envelope.ciphertext)};
}
function nativeStorage() {
  try {
    const storage = process._linkedBinding('electron_browser_workbuddy_storage');
    if (typeof storage.loggerGet !== 'function') failure('RUNTIME_UNAVAILABLE');
    return storage;
  } catch (_) { failure('RUNTIME_UNAVAILABLE'); }
}
function decrypt(envelope) {
  let payload;
  try { payload = JSON.parse(nativeStorage().loggerGet()); }
  catch (_) { failure('RUNTIME_UNAVAILABLE'); }
  let key;
  let plaintext;
  try {
    if (!object(payload) || payload.version !== 1) failure('RUNTIME_UNAVAILABLE');
    let secret;
    try { secret = base64(payload.atRestSecretKey, 32); }
    catch (_) { failure('RUNTIME_UNAVAILABLE'); }
    const empty = secret.every(b => b === 0);
    secret.fill(0);
    if (empty) failure('RUNTIME_UNAVAILABLE');
    key = crypto.createHash('sha256').update(payload.atRestSecretKey, 'utf8').digest();
    payload = null;
    if (crypto.createHash('sha256').update(key).digest('hex').slice(0, 16) !== envelope.keyId)
      failure('KEY_MISMATCH');
    const lp = s => {
      const bytes = Buffer.from(s, 'utf8');
      const length = Buffer.alloc(4); length.writeUInt32BE(bytes.length);
      return Buffer.concat([length, bytes]);
    };
    const aad = Buffer.concat([Buffer.from('WB-AAD\0', 'ascii'), Buffer.from([1]),
      lp('WBEV1'), lp('sym-v1'), Buffer.from([0, 0, 0, 1]), lp(envelope.keyId), Buffer.from([2, 0, 0])]);
    try {
      const cipher = crypto.createDecipheriv('aes-256-gcm', key, envelope.nonce, {authTagLength: 16});
      cipher.setAAD(aad); cipher.setAuthTag(envelope.tag);
      plaintext = Buffer.concat([cipher.update(envelope.ciphertext), cipher.final()]);
    } catch (_) { failure('DECRYPT_FAILED'); }
    const token = utf8(plaintext);
    if (!token.length || token.length > 32768 || !/^[A-Za-z0-9._~+\/-]+=*$/.test(token))
      failure('INVALID_FORMAT');
    return token;
  } finally {
    if (key) key.fill(0);
    if (plaintext) plaintext.fill(0);
  }
}
let chunks = [], size = 0;
function reply(value) {
  process.stdout.write(JSON.stringify({version: 1, ...value}), () => process.exit(value.ok ? 0 : 1));
}
process.stdin.on('data', chunk => {
  size += chunk.length;
  if (size > inputLimit) reply({ok: false, reason: 'INVALID_FORMAT'});
  else chunks.push(chunk);
});
process.stdin.on('error', () => reply({ok: false, reason: 'HELPER_PROTOCOL'}));
process.stdin.on('end', () => {
  try {
    const request = JSON.parse(utf8(Buffer.concat(chunks))); chunks = [];
    if (!object(request) || request.version !== 1) failure('HELPER_PROTOCOL');
    if (request.operation === 'probe') {
      nativeStorage();
      if (!crypto.getCiphers().includes('aes-256-gcm')) failure('RUNTIME_UNAVAILABLE');
      reply({ok: true, electron: process.versions.electron || 'unknown'});
    } else if (request.operation === 'decrypt') {
      reply({ok: true, accessToken: decrypt(decode(request.value))});
    } else failure('HELPER_PROTOCOL');
  } catch (e) {
    const reasons = ['INVALID_FORMAT','UNSUPPORTED_ENVELOPE','RUNTIME_UNAVAILABLE',
      'KEY_MISMATCH','DECRYPT_FAILED','HELPER_PROTOCOL'];
    reply({ok: false, reason: reasons.includes(e.reason) ? e.reason : 'HELPER_PROTOCOL'});
  }
});
"""


# ---------------------------------------------------------------- 凭据读取

class AuthError(Exception):
    """跨凭据边界只允许固定的、不含敏感信息的消息。"""

    def __init__(self, reason, result="AUTH_ERROR"):
        self.reason = reason
        self.result = result
        super().__init__(AUTH_REASONS.get(reason, "本地未找到有效登录会话，请先登录客户端"))

    def output(self):
        return {"result": self.result, "reason": self.reason,
                "report": str(self), "needs_attention": True}


def _valid_token(token):
    return (isinstance(token, str) and 0 < len(token) <= TOKEN_LIMIT
            and re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token) is not None)


def _validate_session(session):
    if not isinstance(session, dict):
        raise AuthError("INVALID_FORMAT")
    for field in ("auth", "account"):
        if session.get(field) is None:
            raise AuthError("MISSING_SESSION", "NO_SESSION")
        if not isinstance(session[field], dict):
            raise AuthError("INVALID_FORMAT")
    if session["auth"].get("accessToken") in (None, "") or session["account"].get("uid") in (None, ""):
        raise AuthError("MISSING_SESSION", "NO_SESSION")
    endpoint = session["auth"].get("endpoint")
    if endpoint in (None, ""):
        endpoint = DEFAULT_ENDPOINT
    if not isinstance(endpoint, str) or re.search(r"[\s\x00-\x1f\x7f]", endpoint):
        raise AuthError("INVALID_FORMAT")
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password \
                or parsed.query or parsed.fragment:
            raise ValueError()
    except ValueError:
        raise AuthError("INVALID_FORMAT") from None


def _token_format(value):
    """返回 'plaintext' 或 'sym-v1'；不支持的格式直接抛错，不猜测。"""
    if isinstance(value, str):
        if not _valid_token(value):
            raise AuthError("INVALID_FORMAT")
        return "plaintext"
    if not isinstance(value, dict):
        raise AuthError("INVALID_FORMAT")
    if (set(value) != {"$wbEncrypted", "envelope"} or
            type(value.get("$wbEncrypted")) is not int or value["$wbEncrypted"] != 1):
        raise AuthError("UNSUPPORTED_ENVELOPE")
    try:
        encoded = value["envelope"]
        if not isinstance(encoded, str) or not 0 < len(encoded) <= AUTH_INPUT_LIMIT - 1024:
            raise ValueError()
        raw = base64.b64decode(encoded, validate=True)
        if base64.b64encode(raw).decode("ascii") != encoded:
            raise ValueError()
        envelope = json.loads(raw.decode("utf-8"))
        if not isinstance(envelope, dict) or type(envelope.get("suite")) is not int:
            raise ValueError()
        if envelope["suite"] != 1:
            raise AuthError("UNSUPPORTED_ENVELOPE")
        if set(envelope) != {"suite", "keyId", "nonce", "authTag", "ciphertext"}:
            raise ValueError()
        if not isinstance(envelope["keyId"], str) or not re.fullmatch(r"[0-9a-f]{16}", envelope["keyId"]):
            raise ValueError()
        for field, length in (("nonce", 12), ("authTag", 16), ("ciphertext", None)):
            data = envelope[field]
            if not isinstance(data, str):
                raise ValueError()
            decoded = base64.b64decode(data, validate=True)
            if base64.b64encode(decoded).decode("ascii") != data or (length is not None and len(decoded) != length):
                raise ValueError()
        return "sym-v1"
    except (ValueError, TypeError, KeyError):
        raise AuthError("INVALID_FORMAT") from None


# ---------------------------------------------------------------- 客户端运行时发现

def _registry_text(registry, key, field):
    """只读字符串型注册表值；REG_EXPAND_SZ 交给 Windows 展开。"""
    try:
        value, kind = registry.QueryValueEx(key, field)
        if not isinstance(value, str) or kind not in (registry.REG_SZ, registry.REG_EXPAND_SZ):
            return None
        if kind == registry.REG_EXPAND_SZ:
            value = registry.ExpandEnvironmentStrings(value)
        return value
    except OSError:
        return None


def _registry_runtime_path(value, icon=False):
    """解析 InstallLocation 或 DisplayIcon；绝不解释卸载命令。"""
    if not isinstance(value, str) or re.search(r"[\x00-\x1f\x7f]", value):
        return None
    value = value.strip()
    if value.startswith('"'):
        suffix = r"(?:\s*,\s*-?\d+)?" if icon else ""
        match = re.fullmatch(r'"([^"]+)"' + suffix, value)
        if not match:
            return None
        value = match[1]
    elif '"' in value:
        return None
    elif icon:
        value = re.sub(r",\s*-?\d+$", "", value).rstrip()
    if not ntpath.splitdrive(value)[0] or not ntpath.isabs(value):
        return None
    if not icon:
        value = ntpath.join(value, "WorkBuddy.exe")
    value = ntpath.normpath(value)
    if ntpath.basename(value).casefold() != "workbuddy.exe":
        return None
    return value


def _windows_registry_runtimes():
    """从用户/本机卸载项里惰性产出已存在的客户端路径（Windows 兜底）。"""
    if sys.platform != "win32":
        return
    try:
        import winreg
    except ImportError:
        return
    subkey = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    seen = set()
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | view) as root:
                    count = winreg.QueryInfoKey(root)[0]
                    for index in range(count):
                        try:
                            with winreg.OpenKey(root, winreg.EnumKey(root, index),
                                                0, winreg.KEY_READ | view) as entry:
                                name = _registry_text(winreg, entry, "DisplayName")
                                if not name or not re.fullmatch(
                                        r"WorkBuddy(?:\s+\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.-]+)?)?",
                                        name.strip(), re.IGNORECASE):
                                    continue
                                for field in ("InstallLocation", "DisplayIcon"):
                                    path = _registry_runtime_path(
                                        _registry_text(winreg, entry, field), icon=field == "DisplayIcon")
                                    if path and ntpath.normcase(path) not in seen:
                                        seen.add(ntpath.normcase(path))
                                        if os.path.isfile(path):
                                            yield path
                        except OSError:
                            continue
            except OSError:
                continue


def find_workbuddy_runtime():
    override = os.environ.get("WORKBUDDY_EXE")
    if override:
        path = os.path.abspath(os.path.expanduser(override))
        if not os.path.isfile(path) or (os.name != "nt" and not os.access(path, os.X_OK)):
            raise AuthError("INVALID_RUNTIME_PATH")
        return path
    home = os.path.expanduser("~")
    candidates = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        candidates.append(os.path.join(local, "Programs", "WorkBuddy", "WorkBuddy.exe"))
        for name in ("ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(name):
                candidates.append(os.path.join(os.environ[name], "WorkBuddy", "WorkBuddy.exe"))
    for path in candidates:
        if path and os.path.isfile(path) and (os.name == "nt" or os.access(path, os.X_OK)):
            return os.path.abspath(path)
    if sys.platform == "win32":
        with closing(_windows_registry_runtimes()) as entries:
            registered = next(entries, None)
        if registered:
            return registered
    raise AuthError("RUNTIME_NOT_FOUND")


def _run_auth_helper(exe, request):
    """限死每根管道的容量与总超时；异常里绝不带上捕获到的数据。"""
    data = json.dumps(dict(request, version=1), ensure_ascii=True).encode("ascii")
    if len(data) > AUTH_INPUT_LIMIT:
        raise AuthError("INVALID_FORMAT")
    timeout = min(AUTH_HELPER_TIMEOUT, _budget_left())
    if timeout <= 0:
        raise AuthError("HELPER_TIMEOUT")
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("NODE_", "ELECTRON_", "WORKBUDDY_"))}
    env["ELECTRON_RUN_AS_NODE"] = "1"
    try:
        process = subprocess.Popen([exe, "-e", AUTH_HELPER_JS], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, shell=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        raise AuthError("RUNTIME_UNAVAILABLE") from None
    output = {}
    failed = threading.Event()

    def read_pipe(name, pipe, limit):
        try:
            captured = pipe.read(limit + 1)
            if len(captured) > limit:
                failed.set()
                process.kill()
            else:
                output[name] = captured
        except OSError:
            failed.set()

    def write_pipe():
        try:
            process.stdin.write(data)
            process.stdin.close()
        except OSError:
            failed.set()

    workers = [threading.Thread(target=read_pipe, args=("stdout", process.stdout, AUTH_OUTPUT_LIMIT), daemon=True),
               threading.Thread(target=read_pipe, args=("stderr", process.stderr, 8192), daemon=True),
               threading.Thread(target=write_pipe, daemon=True)]
    deadline = time.monotonic() + timeout
    try:
        for worker in workers:
            worker.start()
        try:
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise AuthError("HELPER_TIMEOUT") from None
        for worker in workers:
            worker.join(max(0, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in workers):
            raise AuthError("HELPER_TIMEOUT")
        if failed.is_set():
            raise AuthError("HELPER_PROTOCOL")
        try:
            reply = json.loads(output.get("stdout", b"").decode("utf-8"))
        except (ValueError, UnicodeError):
            raise AuthError("HELPER_PROTOCOL") from None
        if not isinstance(reply, dict) or type(reply.get("version")) is not int or reply["version"] != 1:
            raise AuthError("HELPER_PROTOCOL")
        if reply.get("ok") is False and process.returncode == 1:
            reason = reply.get("reason")
            raise AuthError(reason if isinstance(reason, str) and reason in AUTH_REASONS else "HELPER_PROTOCOL")
        if reply.get("ok") is not True or process.returncode != 0:
            raise AuthError("HELPER_PROTOCOL")
        if request["operation"] == "decrypt":
            if set(reply) != {"version", "ok", "accessToken"} or not _valid_token(reply.get("accessToken")):
                raise AuthError("HELPER_PROTOCOL")
        elif (set(reply) != {"version", "ok", "electron"} or not isinstance(reply.get("electron"), str)
              or re.fullmatch(r"[0-9A-Za-z.+-]{1,64}", reply["electron"]) is None):
            raise AuthError("HELPER_PROTOCOL")
        return reply
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for worker in workers:
            if worker.ident is not None:
                worker.join(1)
        if not any(worker.is_alive() for worker in workers):
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()


def resolve_session(session):
    """把 session 里的 accessToken 换成可直接用的明文令牌（只在内存中）。"""
    _validate_session(session)
    token = session["auth"]["accessToken"]
    if _token_format(token) != "plaintext":
        token = _run_auth_helper(find_workbuddy_runtime(),
                                 {"operation": "decrypt", "value": token})["accessToken"]
    _sensitive_values.add(token)
    return dict(session, auth=dict(session["auth"], accessToken=token))


def run_doctor(session):
    _validate_session(session)
    kind = _token_format(session["auth"]["accessToken"])
    out = {"result": "AUTH_READY", "credential_format": kind, "needs_attention": False,
           "report": "本地凭据格式有效；未验证服务端登录状态", "online_checked": False}
    if kind == "sym-v1":
        runtime = find_workbuddy_runtime()
        probe = _run_auth_helper(runtime, {"operation": "probe"})
        out.update(runtime=runtime, electron_version=probe["electron"],
                   report="已识别加密凭据，运行时具备解密能力；未提取密钥、未验证解密或服务端登录状态")
    return out


# ---------------------------------------------------------------- 凭据文件定位

def find_auth_file():
    override = os.environ.get("WORKBUDDY_AUTH_FILE")
    if override:
        return (override if os.path.exists(override) else None), [override]
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    mac = os.path.join(home, "Library", "Application Support")
    candidates = []
    for prefix in AUTH_DIR_PREFIXES:
        candidates.append(os.path.join(local, prefix, AUTH_DIR_RELATIVE))   # Windows 桌面端
        candidates.append(os.path.join(mac, prefix, AUTH_DIR_RELATIVE))     # macOS 桌面端
    candidates.append(os.path.join(home, ".workbuddy", "auth", AUTH_FILENAME))  # 兜底
    for c in candidates:
        if os.path.exists(c):
            return c, candidates
    return None, candidates


def load_session(auth_file):
    with open(auth_file, "r", encoding="utf-8") as f:
        return json.load(f)


def load_session_retry(auth_file, attempts=3, delay=2.0):
    """客户端刷新 token 时会短暂独占凭据文件，恰好在那一刻读就是 PermissionError。
    锁是瞬时的，等两秒再试即可；重试仍失败才真正报错。"""
    last = None
    for i in range(attempts):
        try:
            return load_session(auth_file)
        except PermissionError as e:
            last = e
            if i < attempts - 1:
                time.sleep(delay)
    raise last


def build_headers(session):
    _validate_session(session)
    auth = session.get("auth") or {}
    account = session.get("account") or {}
    token = auth.get("accessToken")
    uid = account.get("uid")
    if not _valid_token(token):
        raise AuthError("INVALID_FORMAT")
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer %s" % token,
        "Content-Type": "application/json",
        "X-User-Id": uid,
        "User-Agent": "WorkBuddy",
    }
    if account.get("enterpriseId"):
        headers["X-Enterprise-Id"] = account["enterpriseId"]
        headers["X-Tenant-Id"] = account["enterpriseId"]
    if auth.get("domain"):
        headers["X-Domain"] = auth["domain"]
    return headers


# ---------------------------------------------------------------- HTTP

def _request(url, headers, method="GET", payload=None, timeout=REQUEST_TIMEOUT):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, {"raw": raw[:500]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:500]}
    except urllib.error.URLError as e:
        return CODE_NO_NETWORK, {"error": str(e.reason)}
    except Exception as e:
        return CODE_NO_NETWORK, {"error": str(e)}


def _retry_delays(code):
    """只有「本机网络没就绪」和「服务端 5xx」值得重试；4xx 重试一百次也是同一个答案。"""
    if code == CODE_NO_NETWORK:
        return NETWORK_RETRY_DELAYS
    if isinstance(code, int) and code >= 500:
        return SERVER_RETRY_DELAYS
    return ()


def post(url, headers, payload=None, retry=True):
    if _budget_left() <= 1:
        return CODE_BUDGET_OUT, {"error": "已达本次运行时间预算，跳过剩余请求"}
    code, body = _request(url, headers, method="POST", payload=payload,
                          timeout=max(1, min(REQUEST_TIMEOUT, _budget_left())))
    if not retry:
        return code, body
    attempts = {}
    while True:
        delays = _retry_delays(code)
        if not delays:
            return code, body
        used = attempts.get(delays, 0)
        if used >= len(delays):
            return code, body
        delay = delays[used]
        attempts[delays] = used + 1
        # 一轮重试最坏要占 delay + 一整个超时，预算不够就别开始：宁可现在如实返回失败，
        # 也不能跑穿任务时限被系统强杀——那样连日志都写不出来。
        if _budget_left() <= delay + REQUEST_TIMEOUT:
            return code, body
        time.sleep(delay)
        code, body = _request(url, headers, method="POST", payload=payload,
                              timeout=max(1, min(REQUEST_TIMEOUT, _budget_left())))


# ---------------------------------------------------------------- 时间预算 / 输出

def _start_budget(action=None):
    global _started_at, _budget_seconds
    _started_at = time.monotonic()
    default, maximum = ((180.0, 240.0) if action in POLL_ACTIONS
                        else (DEFAULT_BUDGET_SECONDS, MAX_BUDGET_SECONDS))
    raw = os.environ.get("WORKBUDDY_BUDGET_SECONDS")
    value = default
    if raw:
        try:
            parsed = float(raw)
            if parsed > 0:
                value = min(parsed, maximum)
        except (TypeError, ValueError):
            pass
    _budget_seconds = value


def _budget_left():
    if _started_at is None:
        return _budget_seconds
    return _budget_seconds - (time.monotonic() - _started_at)


def _env_flag(name):
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


def _dumps(out):
    try:
        return json.dumps(out, ensure_ascii=False, default=str)
    except Exception:
        return repr(out)


def emit(out, action):
    """silent 类模式写日志文件，其余打印到 stdout；本函数保证不抛异常。

    用前缀判断而非等号：pythonw 下 sys.stdout 是 None，print 会静默成功，
    等于把最需要人处理的结果扔进黑洞。
    """
    if isinstance(out, dict):
        out = dict(out)
        out.setdefault("needs_attention", out.get("result") in (
            "ERROR", "UNKNOWN", "NETWORK", "TIMEOUT", "NO_AUTH", "NO_SESSION",
            "AUTH_ERROR", "AUTH_REJECTED", "FORBIDDEN"))
    payload = _dumps(out)
    for value in _sensitive_values:
        payload = payload.replace(value, "[REDACTED]")
    is_error = isinstance(out, dict) and out.get("result") == "ERROR"
    if not str(action).startswith("silent"):
        try:
            print(payload)
            if not is_error:
                return
        except Exception:
            pass  # stdout 不可用（无窗口 / 编码问题）时退到日志，至少不把结果丢掉

    line = "[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), payload)
    default_log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signin.log")
    for path in (os.environ.get("WORKBUDDY_SIGNIN_LOG") or default_log, default_log):
        try:
            with open(path, "a", encoding="utf-8") as lf:
                lf.write(line)
            return
        except Exception:
            continue


# ---------------------------------------------------------------- 业务逻辑

def _is_already_checked_in(body):
    if not isinstance(body, dict):
        return False
    if body.get("code") == 10001:
        return True
    msg = str(body.get("msg") or "")
    return "已签" in msg or "already" in msg.lower()


def _dig(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else default


def _fmt_credit(v):
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def run_status(headers, endpoint):
    url = endpoint + "/v2/billing/meter/checkin-activity-status"
    code, body = post(url, headers, retry=True)
    if code in (401, 403):
        return code, body, None
    data = body.get("data") if isinstance(body, dict) else None
    return code, body, data


def run_claim(headers, endpoint):
    """直接调用领取接口并解释结果。幂等：今日已领会返回 400 + code=10001，按成功处理。

    单独成函数，是为了让 `claim` 调试命令能**真正打到写接口**——只查状态再短路，
    等于从没验证过领取请求的构造是否正确。返回 (退出码, 输出字典, 是否空跑)。
    """
    ccode, cbody = post(endpoint + "/v2/billing/meter/daily-checkin", headers, retry=True)
    if ccode in (401, 403):
        return 1, {"result": "AUTH_REJECTED" if ccode == 401 else "FORBIDDEN", "http": ccode,
                   "report": "领取时服务端拒绝（HTTP %s）" % ccode, "claim_body": cbody}
    if ccode == CODE_NO_NETWORK:
        return 1, {"result": "NETWORK", "report": "领取请求网络不可达：%s" % _dumps(cbody)[:160]}
    if ccode == CODE_BUDGET_OUT:
        return 1, {"result": "TIMEOUT", "report": "本次运行时间预算耗尽，未能领取"}

    # 幂等：HTTP 400 + code 10001 表示今天已经领过，不算失败
    if ccode == 400 and _is_already_checked_in(cbody):
        return 0, {"result": "ALREADY", "report": "今日已签到（服务端返回已签，接口幂等）",
                   "claim_body": cbody}, True

    if 200 <= ccode < 300:
        credit = _dig(cbody, "credit")
        if credit is None and isinstance(cbody, dict):
            credit = _dig(_dig(cbody, "data"), "credit")
        # 领取后回查一次状态，拿到最新的连签天数和累计积分
        _, _, fresh = run_status(headers, endpoint)
        streak = _dig(fresh, "streak_days")
        total = _dig(fresh, "total_credits")
        is_bonus = _dig(fresh, "is_streak_day")
        report = "成功领取 %s 积分" % (_fmt_credit(credit) if credit is not None else "今日")
        if is_bonus:
            report += "，且为连签奖励日"
        if streak is not None:
            report += "（连续 %s 天" % streak
            report += "，累计 %s 积分）" % _fmt_credit(total) if total is not None else "）"
        return 0, {"result": "CLAIMED", "report": report, "credit": credit,
                   "streak_days": streak, "total_credits": total, "claim_body": cbody}, False

    return 1, {"result": "ERROR", "http": ccode,
               "report": "领取失败：%s" % _dumps(cbody)[:200], "claim_body": cbody}


def run_daily(headers, endpoint):
    """查状态 → 未签则领。返回 (退出码, 输出字典, 是否空跑)。"""
    code, sbody, data = run_status(headers, endpoint)
    if code == 401:
        return 1, {"result": "AUTH_REJECTED", "http": code,
                   "report": "服务端拒绝认证（HTTP 401），请检查客户端登录状态与脚本版本"}
    if code == 403:
        return 1, {"result": "FORBIDDEN", "http": code,
                   "report": "服务端拒绝此操作（HTTP 403），请检查账号权限或活动条件"}
    if code == CODE_NO_NETWORK:
        return 1, {"result": "NETWORK", "report": "网络不可达，未能查询签到状态：%s"
                   % _dumps(sbody)[:160]}
    if code == CODE_BUDGET_OUT:
        return 1, {"result": "TIMEOUT", "report": "本次运行时间预算耗尽，未能查询签到状态"}
    if not (200 <= code < 300):
        return 1, {"result": "ERROR", "http": code,
                   "report": "查询签到状态失败：%s" % _dumps(sbody)[:200]}

    if _dig(data, "today_checked_in"):
        # 已签到：用状态接口里的数据拼一句人话，不再发领取请求
        streak = _dig(data, "streak_days")
        total = _dig(data, "total_credits")
        credit = _dig(data, "today_credit")
        report = "今日已签到" + ("（连续 %s 天" % streak if streak is not None else "（")
        report += "，累计 %s 积分）" % _fmt_credit(total) if total is not None else "）"
        if credit is not None:
            report += "，今日 %s 积分" % _fmt_credit(credit)
        return 0, {"result": "ALREADY", "report": report, "streak_days": streak,
                   "total_credits": total, "status_body": data}, True

    if not _dig(data, "active", True):
        return 0, {"result": "INACTIVE", "report": "签到活动当前不在进行中",
                   "status_body": data}, True

    return run_claim(headers, endpoint)


# ---------------------------------------------------------------- 入口

def main():
    """薄壳：只负责取命令 + 兜住一切异常，保证 silent 模式下结果必定落盘。"""
    try:
        for stream in (sys.stdout, sys.stderr):
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    action = sys.argv[1] if len(sys.argv) > 1 else "daily"
    try:
        return _run(action)
    except AuthError as e:
        emit(e.output(), action)
        return 1
    except Exception as e:
        emit({"result": "ERROR", "report": "脚本运行异常（%s）" % type(e).__name__,
              "needs_attention": True}, action)
        return 2


def _run(action):
    _start_budget(action)
    known = ("doctor", "status", "claim", "daily", "silent", "silent-poll")
    if action not in known:
        emit({"result": "ERROR", "report": "未知命令：%s（可用：%s）" % (action, " / ".join(known))}, action)
        return 2

    auth_file, looked_in = find_auth_file()
    env_override = os.environ.get("WORKBUDDY_AUTH_FILE")
    if not auth_file or not os.path.exists(auth_file):
        if env_override:
            report = "WORKBUDDY_AUTH_FILE 指向的文件不存在：%s" % env_override
        else:
            report = ("未找到 WorkBuddy 登录凭据。请先在本机登录 WorkBuddy 桌面端；"
                      "或设置环境变量 WORKBUDDY_AUTH_FILE 指向 workbuddy-desktop.info。")
        emit({"result": "NO_AUTH", "report": report, "looked_in": looked_in, "needs_attention": True}, action)
        return 2

    try:
        session = load_session_retry(auth_file)
    except json.JSONDecodeError:
        emit({"result": "ERROR", "report": "登录凭据文件不是合法 JSON，请重新登录 WorkBuddy 桌面端"}, action)
        return 2
    except Exception as e:
        emit({"result": "ERROR", "report": "读取登录凭据失败（%s），请重新登录 WorkBuddy 桌面端"
              % type(e).__name__}, action)
        return 2

    try:
        if action == "doctor":
            result = run_doctor(session)
            result["auth_file"] = auth_file
            emit(result, action)
            return 0
        session = resolve_session(session)
        headers = build_headers(session)
    except AuthError as e:
        emit(e.output(), action)
        return 1

    endpoint = ((session.get("auth") or {}).get("endpoint") or DEFAULT_ENDPOINT).rstrip("/")

    if action == "status":
        code, body, _ = run_status(headers, endpoint)
        emit({"step": "status", "http": code, "body": body,
              "needs_attention": not (isinstance(code, int) and 200 <= code < 300)}, action)
        return 0 if (isinstance(code, int) and 200 <= code < 300) else 1

    if action == "claim":
        # 调试用：直接打领取接口，验证写请求的构造（幂等，已签返回 ALREADY）
        code, out, _ = run_claim(headers, endpoint)
        emit({**out, "step": "claim"}, action)
        return code

    # daily / silent / silent-poll
    code, out, quiet = run_daily(headers, endpoint)
    if action in POLL_ACTIONS and quiet and not _env_flag("WORKBUDDY_SIGNIN_LOG_EMPTY"):
        return code
    emit(out, action)
    return code


if __name__ == "__main__":
    sys.exit(main())
