#!/usr/bin/env python3
"""AI 平台每日签到面板 — WorkBuddy / TRAE / Qoder 一键签到 + 积分总览。

用法:
  python3 server.py                          # 启动 Web 面板 (默认 http://127.0.0.1:8787，常驻)
  python3 server.py --idle-shutdown 1        # 轻量模式：关标签页即停（空闲 1 分钟自动关闭，平时不常驻；刷新不会误杀）
  python3 server.py --open                   # 启动并自动打开浏览器
  python3 server.py --resident               # 显式常驻（同默认）
  python3 server.py --checkin-now            # 无界面执行一次签到(供定时任务调用)后退出
  python3 server.py --port 9000              # 指定端口

监听 0.0.0.0，同一 WiFi 下的手机/平板可直接用 Mac 的局域网 IP 访问（启动时会打印该地址）。
"""

import json
import os
import random
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
HISTORY_PATH = BASE_DIR / "history.json"
STATE_PATH = BASE_DIR / "last_status.json"
DAILY_PATH = BASE_DIR / "daily_baseline.json"
INDEX_PATH = BASE_DIR / "index.html"

WORKBUDDY_AUTH_FILE = Path.home() / "Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"
# ZCode 的 Cookie 根目录。ZCode 可能存在多个 Cookies 库（内嵌浏览器、会话主库、
# 各类分区），用户在不同入口登录时新会话会落在不同库里，因此下面统一扫描 ZCode
# 目录下的所有 Cookies 库，取最新的有效会话来免去手动复制粘贴。
ZCODE_DIR = Path.home() / "Library/Application Support/ZCode"
IAB_COOKIE_DB = ZCODE_DIR / "session/Partitions/zcode-embedded-browser/Cookies"

DEFAULT_CONFIG = {
    "port": 8787,
    "trae_session": "",
    "trae_device_id": "",
    "qoder_pat": "",
    "qoder_region": "china",  # china | global
    "auto_checkin_on_open": True,
    "cookie_source": "zcode",  # zcode | chrome | safari | manual
    "wb_auto_launch": True,  # WorkBuddy 客户端未运行时面板自动拉起（自愈；不想自动开客户端可改 false）
}

ctx = ssl.create_default_context()

# 面板只会请求三平台的官方 API；白名单外的目标一律拒绝（SSRF 防护：
# 配置/页面内容即使被篡改也无法让本服务访问任意内网或外部地址）
ALLOWED_API_HOSTS = {
    "www.workbuddy.cn", "www.workbuddy.ai",
    "api.trae.cn", "www.trae.cn",
    "openapi.qoder.com.cn", "openapi.qoder.sh", "qoder.cn",
}

# 加密安全的随机源：设备号等标识用它生成；风控退避的随机延时也走它
_rng = random.SystemRandom()


def _rand_digits(n):
    """生成 n 位随机数字串（TRAE 设备号等标识用途）。"""
    return "".join(secrets.choice("0123456789") for _ in range(n))

# ===== 运行模式（轻量 / 常驻）=====
# 轻量模式：IDLE_TIMEOUT>0，空闲超过该秒数自动关闭（平时不常驻，只在签到前后/按需短暂运行）
# 常驻模式：IDLE_TIMEOUT=0，一直运行（开机自启、随时可访问）
IDLE_TIMEOUT = 0
LAST_ACTIVITY = {"t": 0}  # 最近一次客户端请求时间，用于空闲判定
BOOT_TIME = 0
RESIDENT = True
LIGHTWEIGHT_IDLE_SECONDS = 60  # 轻量模式下“关标签页即停”的空闲阈值（秒），运行时切回轻量用此值


def lan_ip():
    """本机局域网 IP（仅做路由查询，不发数据包）；拿不到返回 None。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 1))  # 保留地址，不会真的发包
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    os.chmod(CONFIG_PATH, 0o600)


def http_json(url, method="GET", headers=None, body=None, timeout=20):
    """返回 (status, 解析后的JSON或None, 原始文本)。仅允许白名单内的 https 域名。"""
    from urllib.parse import urlsplit
    u = urlsplit(url)
    if u.scheme != "https" or (u.hostname or "") not in ALLOWED_API_HOSTS:
        return 0, None, "blocked: url host not in allowlist"
    headers = dict(headers or {})
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            text = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
        text = e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, None, f"network error: {e}"
    try:
        return status, json.loads(text), text
    except Exception:
        return status, None, text


# ============ WorkBuddy（凭据自动读取本地 WorkBuddy 客户端） ============

WB_TOKEN_ENCRYPTED = ("未连上运行中的 WorkBuddy 客户端：新版客户端的本地凭据已加密，"
                      "面板需通过客户端的本地服务查询。请打开 WorkBuddy 客户端并保持登录，"
                      "然后刷新面板即可自动恢复。")


# WorkBuddy 客户端后台守护的本地 IPC 入口（客户端启动时生成，含一次性票据）
WBIPC_ENDPOINT = Path.home() / ".workbuddy/wbipc/endpoint.json"


def wb_ipc_fetch_all(requests, timeout=12):
    """通过 WorkBuddy 客户端守护进程的本地 IPC（wbipc）代发认证请求。

    WorkBuddy 5.8.3 起本地凭据文件里的 accessToken 改为加密信封，面板无法直接
    解密；但客户端运行时会在本机暴露 wbipc 服务，其中 wb.request 管道的
    http.fetch 方法会自动附带解密后的登录态转发到官方接口。面板借此查询额度
    与签到，前提是 WorkBuddy 客户端保持运行（endpoint.json 由客户端维护）。

    协议（逆向自 WorkBuddy 5.8.3 客户端）：换行分隔 JSON 帧；
    session_hello(带 ticket_id+client_nonce) → session_challenge(server_nonce)
    → session_prove(HMAC-SHA256(ticket, 转录)) → session_hello_ack → JSON-RPC。

    requests: [{path, method, body(bytes|None)}] → [(status, json|None, text)]；
    客户端未运行/握手失败返回 None，由调用方降级处理。
    """
    import base64 as _b64
    import hashlib as _hl
    import hmac as _hm
    import secrets as _sc
    import socket as _sk
    s = None
    try:
        ep_cfg = json.loads(WBIPC_ENDPOINT.read_text())
        endpoint, ticket = ep_cfg["endpoint"], ep_cfg["ticket"]
    except Exception:
        return None
    tid = _hl.sha256(ticket.encode()).hexdigest()[:16]

    def _proof(role, proto, cn, sn):
        out = b""
        for field in ("wbipc-s" if role == "server" else "wbipc-c",
                      str(proto), endpoint, cn, sn):
            b = field.encode()
            out += len(b).to_bytes(4, "big") + b
        return _b64.urlsafe_b64encode(
            _hm.new(ticket.encode(), out, _hl.sha256).digest()).rstrip(b"=").decode()

    try:
        s = _sk.socket(_sk.AF_UNIX, _sk.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(endpoint)
        f = s.makefile("rwb")

        def send(o):
            f.write(json.dumps(o).encode() + b"\n")
            f.flush()

        def recv():
            line = f.readline()
            return json.loads(line) if line else None

        cn = _b64.urlsafe_b64encode(_sc.token_bytes(16)).rstrip(b"=").decode()
        send({"type": "session_hello", "protocol_min": 1, "protocol_max": 1,
              "client_nonce": cn, "ticket_id": tid,
              "client": {"kind": "checkin-panel", "id": "panel", "version": "1.0"}})
        ch = recv()
        if not ch or ch.get("type") != "session_challenge":
            raise RuntimeError("wbipc handshake failed")
        send({"type": "session_prove",
              "client_proof": _proof("client", 1, cn, ch["server_nonce"])})
        ack = recv()
        if not ack or ack.get("type") != "session_hello_ack":
            raise RuntimeError("wbipc auth failed")
        send({"jsonrpc": "2.0", "id": 1, "method": "broker/GetPipe",
              "params": {"pipe": "wb.request"}})
        channel = ((recv() or {}).get("result") or {}).get("channel")
        if not channel:
            raise RuntimeError("wb.request pipe unavailable")
        out = []
        for i, rq in enumerate(requests, start=2):
            params = {"method": rq.get("method", "POST"), "path": rq["path"],
                      "headers": {"content-type": "application/json",
                                  "accept": "application/json"}}
            body = rq.get("body")
            if body is not None:
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                params["body_b64"] = _b64.b64encode(raw).decode()
            send({"jsonrpc": "2.0", "id": i, "method": channel + "/http.fetch",
                  "mode": "call", "params": params})
            r = recv()
            res = (r or {}).get("result")
            if not res:
                raise RuntimeError("wbipc fetch error")
            text = _b64.b64decode(res.get("body_b64", "")).decode("utf-8", "replace")
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            out.append((res.get("status"), parsed, text))
        return out
    except Exception:
        return None
    finally:
        try:
            s.close()
        except Exception:
            pass


WB_LAUNCH_GUARD = {"t": 0}  # 上次自动拉起 WorkBuddy 客户端的时间，用于防循环


def wb_ipc_alive(timeout=2):
    """探测 WorkBuddy 客户端的 wbipc 服务是否在运行（只连一下就断）。"""
    import socket as _sk
    try:
        cfg = json.loads(WBIPC_ENDPOINT.read_text())
        s = _sk.socket(_sk.AF_UNIX, _sk.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(cfg["endpoint"])
        s.close()
        return True
    except Exception:
        return False


def wb_ensure_client(wait_s=45):
    """自愈：WorkBuddy 客户端未运行时自动拉起，并等它的本地服务就绪。

    10 分钟内最多主动拉起一次（防止用户刚手动退出又被反复拉起）；
    拉起后轮询探测 endpoint（客户端每次启动会重写 endpoint.json）。
    """
    import subprocess as _sp
    if wb_ipc_alive():
        return True
    now = time.time()
    if now - WB_LAUNCH_GUARD["t"] > 600:
        WB_LAUNCH_GUARD["t"] = now
        try:
            _sp.Popen(["open", "-a", "WorkBuddy"],
                      stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        except Exception:
            return False
        deadline = now + wait_s
    else:
        deadline = now + 5  # 近期已拉起过（可能还在启动）：只做短探测
    while time.time() < deadline:
        time.sleep(2)
        if wb_ipc_alive():
            return True
    return wb_ipc_alive()


def _wb_token_expiry_from_file():
    """凭据文件里的 expiresAt 字段是明文，即使 accessToken 已加密也能读到有效期。"""
    try:
        v = json.loads(WORKBUDDY_AUTH_FILE.read_text())["auth"].get("expiresAt")
        if v:
            return datetime.fromtimestamp(v / 1000).strftime("%Y-%m-%d")
    except Exception:
        pass
    return None


def _wb_parse_resource(data):
    """解析 get-user-resource 响应里的 credits 资源包（wbipc 与直连通道共用）。"""
    packs, total_remain, total_size, total_used = [], 0.0, 0.0, 0.0
    if isinstance(data, dict) and data.get("code") == 0:
        accounts = (((data.get("data") or {}).get("Response") or {}).get("Data") or {}).get("Accounts") or []
        for a in accounts:
            if a.get("CapacityUnit") != "credits" or a.get("Status") not in (0, None):
                continue
            size = a.get("CapacitySize") or 0
            used = a.get("CapacityUsed") or 0
            remain = max(0.0, a.get("CapacityRemain") or 0)
            total_size += size
            total_used += used
            total_remain += remain
            packs.append({
                "name": short_name(a.get("PackageName") or a.get("ProductName") or "资源包"),
                "remain": remain,
                "total": size,
                "used": used,
                "expiry": _wb_expiry(a),
            })
    return packs, total_remain, total_size, total_used


def workbuddy_creds():
    if not WORKBUDDY_AUTH_FILE.exists():
        return None
    try:
        d = json.loads(WORKBUDDY_AUTH_FILE.read_text())
        token = d["auth"]["accessToken"]
        # 新版客户端把 accessToken 存成 {"$wbEncrypted":1,"envelope":"..."}。
        # 旧代码直接拿它拼 "Bearer " + token 会抛 TypeError，这里识别后降级提示。
        if not isinstance(token, str) or not token.strip():
            return None
        domain = d["auth"].get("domain") or "www.workbuddy.cn"
        uid = d.get("account", {}).get("uid", "")
        expires_at = d["auth"].get("expiresAt")
        base = "https://www.workbuddy.ai" if "workbuddy.ai" in domain else "https://www.workbuddy.cn"
        return {"token": token, "domain": domain, "uid": uid, "expires_at": expires_at, "base": base}
    except Exception:
        return None


def wb_headers(creds):
    return {
        "Authorization": "Bearer " + creds["token"],
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2",
        "X-User-Id": creds["uid"],
        "X-Domain": creds["domain"],
    }


def _wb_expiry(item):
    for k in ("ExpiredTime", "CycleEndTime"):
        v = item.get(k)
        if not v:
            continue
        if isinstance(v, (int, float)) and v > 10**12:  # ms epoch
            return datetime.fromtimestamp(v / 1000).strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v):
            return v  # CycleEndTime 本身带时分秒
    return None


def workbuddy_status(cfg=None):
    # 首选：借 WorkBuddy 客户端守护进程的本地 wbipc 服务转发认证请求
    #（5.8.3 起 accessToken 在本地加密存储，客户端自身是唯一可行的读取通道）
    # 状态查询是被动展示：只使用现有服务，不主动拉起客户端
    #（拉起仅在签到动作时发生，避免用户手动退出 WorkBuddy 后被面板反复弹起）
    res = wb_ipc_fetch_all([
        {"path": "/v2/billing/meter/checkin-activity-status", "method": "POST", "body": b"{}"},
        {"path": "/v2/billing/meter/get-user-resource", "method": "POST", "body": b"{}"},
    ])
    if res and res[0][0] == 200:
        act = {}
        if isinstance(res[0][1], dict) and res[0][1].get("code") == 0:
            act = res[0][1].get("data") or {}
        packs, total_remain, total_size, total_used = _wb_parse_resource(res[1][1])
        return {
            "ok": True,
            "configured": True,
            "checked_in_today": bool(act.get("today_checked_in")),
            "streak_days": act.get("streak_days"),
            "daily_credit": act.get("daily_credit"),
            "activity_name": act.get("theme_name"),
            "activity_end": (act.get("end_time") or "")[:10] or None,
            "credits_remain": total_remain,
            "credits_total": total_size,
            "credits_used": total_used,
            "packages": packs,
            "credential_expires": _wb_token_expiry_from_file(),
        }
    # 回退：旧版客户端（accessToken 明文）直连官方接口
    creds = workbuddy_creds()
    if not creds:
        raw = None
        try:
            raw = json.loads(WORKBUDDY_AUTH_FILE.read_text())["auth"].get("accessToken")
        except Exception:
            pass
        if isinstance(raw, dict):
            msg = ("WorkBuddy 客户端在运行但尚未登录：请在客户端完成登录后刷新面板。"
                   if wb_ipc_alive() else WB_TOKEN_ENCRYPTED)
            return {"ok": False, "configured": True, "encrypted_token": True, "error": msg}
        return {"ok": False, "error": "未找到本地 WorkBuddy 凭据文件（请确认 WorkBuddy 客户端已登录）", "configured": False}
    base, h = creds["base"], wb_headers(creds)

    st, data, _ = http_json(base + "/v2/billing/meter/checkin-activity-status", "POST", h, {})
    act = {}
    if st == 200 and isinstance(data, dict) and data.get("code") == 0:
        act = data.get("data") or {}

    st, data, _ = http_json(base + "/v2/billing/meter/get-user-resource", "POST", h, {})
    packs, total_remain, total_size, total_used = [], 0.0, 0.0, 0.0
    if st == 200 and isinstance(data, dict) and data.get("code") == 0:
        accounts = (((data.get("data") or {}).get("Response") or {}).get("Data") or {}).get("Accounts") or []
        for a in accounts:
            if a.get("CapacityUnit") != "credits" or a.get("Status") not in (0, None):
                continue
            size = a.get("CapacitySize") or 0
            used = a.get("CapacityUsed") or 0
            remain = max(0.0, a.get("CapacityRemain") or 0)
            total_size += size
            total_used += used
            total_remain += remain
            packs.append({
                "name": short_name(a.get("PackageName") or a.get("ProductName") or "资源包"),
                "remain": remain,
                "total": size,
                "used": used,
                "expiry": _wb_expiry(a),
            })
    token_exp = None
    if creds.get("expires_at"):
        token_exp = datetime.fromtimestamp(creds["expires_at"] / 1000).strftime("%Y-%m-%d")
    return {
        "ok": True,
        "configured": True,
        "checked_in_today": bool(act.get("today_checked_in")),
        "streak_days": act.get("streak_days"),
        "daily_credit": act.get("daily_credit"),
        "activity_name": act.get("theme_name"),
        "activity_end": (act.get("end_time") or "")[:10] or None,
        "credits_remain": total_remain,
        "credits_total": total_size,
        "credits_used": total_used,
        "packages": packs,
        "credential_expires": token_exp,
    }


def _wb_token_encrypted():
    """凭据文件里的 accessToken 是否已是加密信封（新版客户端的静态加密格式）。"""
    try:
        return isinstance(json.loads(WORKBUDDY_AUTH_FILE.read_text())["auth"].get("accessToken"), dict)
    except Exception:
        return False


def workbuddy_checkin(cfg=None):
    # 首选 wbipc 通道（同 workbuddy_status），旧版客户端回退直连；
    # 客户端未运行时自动拉起（定时签到 09:30 也能无感完成）
    res = wb_ipc_fetch_all([{"path": "/v2/billing/meter/daily-checkin",
                             "method": "POST", "body": b"{}"}])
    if res is None and (cfg or load_config()).get("wb_auto_launch", True):
        if wb_ensure_client(wait_s=45):
            res = wb_ipc_fetch_all([{"path": "/v2/billing/meter/daily-checkin",
                                     "method": "POST", "body": b"{}"}])
    if res:
        st, data, _ = res[0]
        if st == 200 and isinstance(data, dict) and data.get("code") == 0:
            credit = (data.get("data") or {}).get("credit", (data.get("data") or {}).get("today_credit"))
            return {"ok": True, "status": "claimed", "credit": credit, "message": f"签到成功，获得 {credit} credits"}
        if isinstance(data, dict):
            msg = data.get("msg") or ""
            if data.get("code") in (10001, 10002) or "已签到" in msg:
                return {"ok": True, "status": "already", "message": "今天已签到"}
        return {"ok": False, "error": f"HTTP {st}: {_snip(data)}"}
    creds = workbuddy_creds()
    if not creds:
        return {"ok": False, "error": WB_TOKEN_ENCRYPTED if _wb_token_encrypted() else "未找到本地 WorkBuddy 凭据"}
    st, data, _ = http_json(creds["base"] + "/v2/billing/meter/daily-checkin", "POST", wb_headers(creds), {})
    if st == 200 and isinstance(data, dict) and data.get("code") == 0:
        credit = (data.get("data") or {}).get("credit", (data.get("data") or {}).get("today_credit"))
        return {"ok": True, "status": "claimed", "credit": credit, "message": f"签到成功，获得 {credit} credits"}
    if isinstance(data, dict):
        msg = data.get("msg") or ""
        if data.get("code") in (10001, 10002) or "已签到" in msg:
            return {"ok": True, "status": "already", "message": "今天已签到"}
    return {"ok": False, "error": f"HTTP {st}: {_snip(data)}"}


# ============ TRAE（需要 config 里的 X-Cloudide-Session 会话串） ============

TRAE_BASE = "https://api.trae.cn"


def grab_browser_session(source="zcode"):
    """从指定浏览器源提取 TRAE 会话串；拿不到返回 None。

    支持源：
    - zcode: ZCode 内置浏览器（明文存储，直接可读）
    - chrome: Chrome（macOS Keychain 加密，无法直接读，返回 None）
    - safari: Safari（macOS Keychain 加密，无法直接读，返回 None）
    - manual: 手动模式（返回 None，由用户手动粘贴）
    """
    if source == "zcode":
        return _grab_zcode_session()
    elif source in ("chrome", "safari"):
        # macOS 上 Chrome/Safari 的 Cookie 被 Keychain 加密，无法直接读取
        # 返回 None，提示用户切换到 manual 模式或继续使用 ZCode
        return None
    elif source == "manual":
        return None
    return None


def _zcode_cookie_dbs():
    """枚举 ZCode 下的所有 Cookies 库（含内嵌浏览器与各分区），按路径稳定排序。"""
    if not ZCODE_DIR.exists():
        return []
    return [p for p in sorted(ZCODE_DIR.rglob("Cookies")) if p.is_file()]


def _grab_cookie_from_zcode(cookie_name, host_sub, with_expiry=False):
    """在 ZCode 的全部 Cookie 库中找指定名字、域名含 host_sub 的 Cookie。

    取“最新有效”的一条：优先未过期的，其中再取到期时间最晚的；若全部过期则退回
    到期时间最晚的一条（仍可能被服务端接受）。Cookie 库若为加密存储则读不到值，返回 None。
    用参数化查询只按名字过滤，域名在 Python 里做子串匹配，避免把通配符拼进 SQL。
    with_expiry=True 时返回 (值, 剩余天数或None)，供提前预警使用。
    """
    import shutil as _sh
    import sqlite3 as _sq
    import tempfile as _tf
    cands = []
    for db in _zcode_cookie_dbs():
        tmp = Path(_tf.mkdtemp()) / "Cookies"
        try:
            for suf in ("", "-wal", "-shm"):
                src = Path(str(db) + suf)
                if src.exists():
                    _sh.copy(src, Path(str(tmp) + suf))
            con = _sq.connect(str(tmp))
            rows = con.execute(
                "SELECT host_key, value, expires_utc FROM cookies WHERE name=?",
                (cookie_name,)).fetchall()
            con.close()
        except Exception:
            continue
        for host, val, exp in rows:
            if host_sub not in (host or ""):
                continue
            val = (val or "").strip()
            if val:
                cands.append((int(exp or 0), val))
    if not cands:
        return (None, None) if with_expiry else None
    now = int((time.time() + 11644473600) * 1000000)  # WebKit 纪元(1601)微秒
    best_exp, val = max(fresh or cands, key=lambda c: c[0]) if (fresh := [c for c in cands if c[0] == 0 or c[0] > now]) else max(cands, key=lambda c: c[0])
    if with_expiry:
        days = None
        if best_exp:
            days = round((best_exp / 1e6 - 11644473600 - time.time()) / 86400, 1)
        return val, days
    return val


def _grab_zcode_session():
    """在 ZCode 各 Cookie 库提取最新的 TRAE 会话串；拿不到返回 None。"""
    return _grab_cookie_from_zcode("X-Cloudide-Session", "trae.cn")


def grab_trae_session():
    """兼容旧函数名，调用新函数。"""
    return grab_browser_session("zcode")


def trae_token_with_heal(cfg):
    """获取 TRAE JWT；当前会话失效时先尝试从配置浏览器源无感换取新会话。

    返回 (token, err, needs_relogin)。needs_relogin=True 表示自动修复失败，
    前端应引导用户重新登录。
    """
    session = (cfg.get("trae_session") or "").strip()
    if session:
        token, err = trae_get_token(session)
        if token:
            return token, None, False
    else:
        token, err = None, "尚未配置 TRAE 登录"

    source = cfg.get("cookie_source", "zcode")
    fresh = grab_browser_session(source)
    if fresh and fresh != session:
        tok2, err2 = trae_get_token(fresh)
        if tok2:
            cfg["trae_session"] = fresh
            cfg["trae_session_expires"] = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
            save_config(cfg)
            return tok2, None, False
    return None, (err or "无法获取 TRAE 会话"), True


def trae_headers(token, device_id, session=""):
    h = {
        "Authorization": "Cloud-IDE-JWT " + token,
        "x-device-id": device_id,
        "X-User-Region": "cn",
        "Content-Type": "application/json",
        "Referer": "https://www.trae.cn/",
        "Origin": "https://www.trae.cn",
        "User-Agent": "TraeCheckin/1.0",
    }
    if session:
        h["Cookie"] = "X-Cloudide-Session=" + session
    return h


def trae_get_token(session):
    st, data, _ = http_json(TRAE_BASE + "/cloudide/api/v3/common/GetUserToken", "POST",
                            {"Cookie": "X-Cloudide-Session=" + session,
                             "Referer": "https://www.trae.cn/", "Origin": "https://www.trae.cn",
                             "User-Agent": "TraeCheckin/1.0",
                             "Accept": "application/json, text/plain, */*"}, b"")
    if st == 401 or st == 403:
        return None, "会话已过期（X-Cloudide-Session 约 14 天有效），请重新登录 trae.cn 并更新"
    if st == 200 and isinstance(data, dict):
        tok = (data.get("Result") or {}).get("Token")
        if tok:
            return tok, None
    return None, f"换取 JWT 失败 HTTP {st}: {_snip(data)}"


def _trae_find_expiry(item):
    def _fmt(v):
        if isinstance(v, (int, float)) and v > 10**9:
            ts = v / 1000 if v > 10**11 else v  # TRAE 返回秒级，其他平台常见毫秒级
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v):
            return v[:10]
        return None
    for k, v in item.items():
        if re.search(r"expire|end_time|deadline", k, re.I):
            d = _fmt(v)
            if d:
                return d
    info = item.get("entitlement_base_info") or {}
    for k, v in info.items():
        if re.search(r"expire|end_time|deadline", k, re.I):
            d = _fmt(v)
            if d:
                return d
    return None


def _trae_expiry_dt(item):
    """到期时间，精确到分钟（官方页同款）。"""
    v = item.get("expire_time") or (item.get("entitlement_base_info") or {}).get("end_time")
    try:
        ts = float(v)
        if ts > 10**11:
            ts /= 1000
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def trae_entitlements(token, device_id, session):
    st, data, _ = http_json(TRAE_BASE + "/trae/api/v2/pay/user_current_entitlement_list", "POST",
                            trae_headers(token, device_id, session), {})
    packs, total, limit_sum, used_sum = [], 0.0, 0.0, 0.0
    if st == 200 and isinstance(data, dict):
        for p in data.get("user_entitlement_pack_list") or []:
            info = p.get("entitlement_base_info") or {}
            quota = info.get("quota") or {}
            limit = _num(quota.get("credits_limit"))
            if limit is None:
                continue
            used = _num((p.get("usage") or {}).get("credits_amount")) or 0.0
            limit_sum += limit
            used_sum += used
            remain = max(0.0, limit - used)
            total += remain
            # 包名用官方展示名（签到奖励/每月登录赠送…），过期时间精确到分钟
            name = (p.get("display_desc")
                    or ((info.get("product_extra") or {}).get("package_extra") or {}).get("package_name")
                    or info.get("name") or "积分包")
            packs.append({"name": name,
                          "remain": remain, "total": limit, "used": used,
                          "expiry": _trae_expiry_dt(p)})
    return total, packs, limit_sum, used_sum


def trae_status(cfg):
    token, err, needs_relogin = trae_token_with_heal(cfg)
    if not token:
        return {"ok": False, "configured": True, "needs_relogin": needs_relogin, "error": err}
    session = (cfg.get("trae_session") or "").strip()
    device_id = cfg.get("trae_device_id") or _rand_digits(16)

    st, data, _ = http_json(TRAE_BASE + "/trae/api/v2/ug/checkin_credits/status", "POST",
                            trae_headers(token, device_id, session), {})
    checked, credits_info = None, {}
    if st == 200 and isinstance(data, dict):
        d = data.get("Result") if isinstance(data.get("Result"), dict) else data
        for k in ("checked_in", "today_checked_in", "is_checkin", "checked"):
            if k in d:
                checked = bool(d[k])
                break
        credits_info = {k: v for k, v in d.items() if "credit" in k.lower()}

    total, packs, limit_sum, used_sum = trae_entitlements(token, device_id, session)
    return {"ok": True, "configured": True, "checked_in_today": checked,
            "credits_remain": total, "credits_total": limit_sum, "credits_used": used_sum,
            "packages": packs,
            "credential_expires": cfg.get("trae_session_expires")}


def trae_checkin(cfg):
    token, err, needs_relogin = trae_token_with_heal(cfg)
    if not token:
        return {"ok": False, "needs_relogin": needs_relogin, "error": err}
    session = (cfg.get("trae_session") or "").strip()
    device_id = cfg.get("trae_device_id") or _rand_digits(16)

    def claim_once(dev):
        st, data, _ = http_json(TRAE_BASE + "/trae/api/v2/ug/checkin_credits/claim", "POST",
                                trae_headers(token, dev, session), {})
        if st == 200 and isinstance(data, dict):
            code = data.get("code")
            if code == 9074:  # 风控：参与用户太多，换随机设备号重试
                return "risk"
            if code == 0:
                return "claimed"
            return {"ok": False, "error": f"code={code}: {_snip(data)}"}
        return {"ok": False, "error": f"HTTP {st}: {_snip(data)}"}

    def status_now(dev):
        st, data, _ = http_json(TRAE_BASE + "/trae/api/v2/ug/checkin_credits/status", "POST",
                                trae_headers(token, dev, session), {})
        if st == 200 and isinstance(data, dict):
            return data

    for _ in range(5):
        st0 = status_now(device_id) or {}
        if st0.get("checked_in") or st0.get("did_checked_in"):
            credit = (st0.get("credits") or 0) + (st0.get("extra_credits") or 0)
            return {"ok": True, "status": "already", "credit": credit, "message": f"今天已签到（到账 {credit} credits）"}
        res = claim_once(device_id)
        if res == "risk":
            device_id = _rand_digits(16)
            time.sleep(1 + _rng.random() * 2)
            continue
        if isinstance(res, dict):
            return res
        st1 = status_now(device_id) or {}
        credit = (st1.get("credits") or 0) + (st1.get("extra_credits") or 0)
        return {"ok": True, "status": "claimed", "credit": credit, "message": f"签到成功，到账 {credit} credits"}
    return {"ok": False, "error": "多次触发风控，签到未成功"}


# ============ Qoder（需要 config 里的 PAT） ============

def qoder_endpoints(region):
    if region == "global":
        return {"openapi": "https://openapi.qoder.sh"}
    return {"openapi": "https://openapi.qoder.com.cn"}


def qoder_base_headers():
    return {"accept": "application/json", "accept-encoding": "identity",
            "user-agent": "qoder/1.1.47", "cosy-version": "1.0.1", "cosy-clienttype": "5"}


def qoder_job_token(pat, region):
    url = qoder_endpoints(region)["openapi"] + "/api/v1/jobToken/exchange"
    st, data, _ = http_json(url, "POST", qoder_base_headers(), {"personal_token": pat})
    if st == 200 and isinstance(data, dict) and data.get("token"):
        return data["token"], None
    return None, f"PAT 换取 token 失败 HTTP {st}: {_snip(data)}"


def qoder_usage(token, region):
    url = qoder_endpoints(region)["openapi"] + "/api/v2/quota/usage"
    h = qoder_base_headers()
    h["authorization"] = "Bearer " + token
    st, data, _ = http_json(url, "GET", h)
    if st != 200 or not isinstance(data, dict):
        return None
    packs = []
    for key, label, note in (("userQuota", "Qoder 套餐额度", None),
                             ("orgResourcePackage", "Qoder 组织资源包", "领取后30天有效"),
                             ("addOnQuota", "Qoder 额外额度", "领取后30天有效")):
        q = data.get(key)
        if not isinstance(q, dict):
            continue
        rem = _num(q.get("remaining"))
        if not rem:
            continue
        packs.append({"name": label, "remain": rem, "total": _num(q.get("total")),
                      "used": _num(q.get("used")), "note": note})
    expiry = None
    try:
        v = float(data.get("expiresAt") or 0)
        if 0 < v < 10**11:  # 253402214400000 表示永不过期，过滤
            expiry = datetime.fromtimestamp(v / 1000 if v > 10**11 else v).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        pass
    return {"packages": packs, "expiry": expiry}


def grab_qoder_web_cookie():
    """在 ZCode 各 Cookie 库读 qoder.cn 的网页会话（取最新有效一条；可选增强数据源）。

    用户可能在 ZCode 内嵌浏览器或其它入口登录 qoder.cn，新会话落库位置不定，故扫描
    全部 ZCode Cookies 库，避免只读旧库导致登录后仍显示未登录。"""
    return _grab_cookie_from_zcode("qoder_session_cookie", "qoder.cn")


def qoder_web_packages():
    """用内置浏览器的 qoder.cn 登录态抓逐包额度（含真实到期日）。

    返回 (packs, reason)：成功 -> (列表, None)；失败 -> (None, 原因)。
    原因取值：'no-cookie'（未从浏览器拿到会话）、'expired'（会话被服务端判失效，
    多为约 7 天过期）、'http-xxx'、'empty'（返回体无逐包数据）。
    网页会话失效时面板退回 openapi 汇总数据（无逐包到期日），不影响签到，
    并在卡片上给出重新登录提示。"""
    cookie = grab_qoder_web_cookie()
    if not cookie:
        return None, "no-cookie"
    h = {"accept": "application/json", "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X)",
         "cookie": "qoder_session_cookie=" + cookie}
    st, data, _ = http_json("https://qoder.cn/api/v2/me/usages/big_model_credits", "GET", h)
    if st in (401, 403):
        return None, "expired"
    if st != 200 or not isinstance(data, dict):
        return None, f"http-{st}"

    def detail(section, label):
        q = data.get(section) or {}
        out = []
        for d in q.get("quota_detail") or []:
            if not (d.get("limit_value") or 0) > 0:
                continue
            exp = d.get("expires_at")
            out.append({
                "name": label,
                "remain": d.get("remaining_value") or 0,
                "total": d.get("limit_value") or 0,
                "used": d.get("used_value") or 0,
                "expiry": datetime.fromtimestamp(exp / 1000).strftime("%Y-%m-%d %H:%M:%S") if exp else None,
            })
        return out

    packs = detail("plan_quota", "Qoder 套餐额度") + detail("resource_package_quota", "Qoder 获赠资源包")
    return (packs, None) if packs else (None, "empty")


def qoder_status(cfg):
    pat = (cfg.get("qoder_pat") or "").strip()
    if not pat:
        return {"ok": True, "configured": False, "needs_pat": True, "error": "尚未配置 Qoder PAT"}
    region = cfg.get("qoder_region") or "china"
    token, err = qoder_job_token(pat, region)
    if not token:
        return {"ok": False, "configured": True, "needs_pat": "401" in (err or "") or "403" in (err or ""), "error": err}
    usage = qoder_usage(token, region)
    packs = (usage or {}).get("packages") or []
    # 优先用网页会话拿逐包明细（含真实到期日）；拿不到就用 openapi 汇总，
    # 并把失败原因透出给前端，以便提示「网页会话已过期，点此重新登录」。
    web_packs, web_reason = qoder_web_packages()
    web_days = None
    if web_packs:
        packs = web_packs
        # 网页会话固定约 7 天过期，提前 3 天在卡片上预警，方便用户点一下续期
        _, web_days = _grab_cookie_from_zcode("qoder_session_cookie", "qoder.cn", with_expiry=True)
    # 顺带查今天签到活动的领取状态（无活动或已领都视为无需再签）
    checked = None
    openapi = qoder_endpoints(region)["openapi"]
    h = qoder_base_headers()
    h["authorization"] = "Bearer " + token
    h["cosy-clienttype"] = "10"
    st, data, _ = http_json(openapi + "/sash/api/v1/me/campaigns", "GET", h)
    camps = (data or {}).get("campaigns") if isinstance(data, dict) else None
    if isinstance(camps, list):
        benefit = next((c for c in camps if c.get("actionType") == "CLAIM_BENEFIT"), None)
        if benefit:
            checked = benefit.get("claimStatus") == "CLAIMED"
    return {"ok": True, "configured": True, "checked_in_today": checked,
            "credits_remain": sum(p["remain"] for p in packs) if packs else 0,
            "credits_total": sum(p.get("total") or 0 for p in packs),
            "credits_used": sum(p.get("used") or 0 for p in packs),
            "packages": packs,
            "needs_web_relogin": web_packs is None,
            "web_reason": web_reason,
            "web_session_days_left": web_days,
            "credential_expires": cfg.get("qoder_pat_expires")}


def qoder_checkin(cfg):
    pat = (cfg.get("qoder_pat") or "").strip()
    if not pat:
        return {"ok": False, "error": "尚未配置 Qoder PAT"}
    region = cfg.get("qoder_region") or "china"
    openapi = qoder_endpoints(region)["openapi"]
    token, err = qoder_job_token(pat, region)
    if not token:
        return {"ok": False, "error": err}

    h = qoder_base_headers()
    h["authorization"] = "Bearer " + token
    h["cosy-clienttype"] = "10"  # 活动接口只认桌面端标识，用 5 会一直返回空列表
    st, data, _ = http_json(openapi + "/sash/api/v1/me/campaigns", "GET", h)
    campaigns = (data or {}).get("campaigns") if isinstance(data, dict) else None
    if not isinstance(campaigns, list):
        return {"ok": False, "error": f"获取活动列表失败 HTTP {st}: {_snip(data)}"}
    benefit = next((c for c in campaigns if c.get("actionType") == "CLAIM_BENEFIT"), None)
    if not benefit:
        return {"ok": True, "status": "no-campaign", "message": "今天没有可领取的签到活动"}
    amount = (benefit.get("benefit") or {}).get("amount", 100)
    if benefit.get("claimStatus") == "CLAIMED":
        return {"ok": True, "status": "already", "credit": amount, "message": "今天已签到"}

    ch = dict(h)
    ch["origin"] = openapi
    st, data, _ = http_json(openapi + f"/sash/api/v1/me/campaigns/{benefit['campaignId']}/claim", "POST", ch, {})
    if st == 200 and isinstance(data, dict) and data.get("status") == "CLAIMED":
        amount = (data.get("benefit") or {}).get("amount") or amount
        replayed = bool(data.get("replayed"))
        return {"ok": True, "status": "already" if replayed else "claimed", "credit": amount,
                "message": ("今天已签到" if replayed else f"签到成功，获得 {amount} credits")}
    return {"ok": False, "error": f"领取失败 HTTP {st}: {_snip(data)}"}


# ============ 汇总 / 历史 ============

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _snip(data):
    try:
        s = json.dumps(data, ensure_ascii=False)
    except Exception:
        s = str(data)
    return s[:180]


def short_name(name):
    """缩短资源包名：去掉平台前缀和冗余字样，卡片标题已标明平台。"""
    if not name:
        return name
    for prefix in ("CodeBuddy个人版国内运营", "CodeBuddy个人版", "CodeBuddy个人", "CodeBuddy", "Qoder "):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name or name


PLATFORMS = ["workbuddy", "trae", "qoder"]
CHECKIN_FN = {"workbuddy": lambda cfg: workbuddy_checkin(cfg),
              "trae": trae_checkin, "qoder": qoder_checkin}
STATUS_FN = {"workbuddy": lambda cfg: workbuddy_status(cfg),
             "trae": trae_status, "qoder": qoder_status}


# 各平台积分消耗优先级说明（TRAE 为官方明示；WB/Qoder 为 2026-10-06 实测观察）
PRIORITY_RULES = {
    "workbuddy": "实测(10-06)：消耗落在最早到期的包上；官方未明示，逐包结算有延迟、行可能重排",
    "trae": "官方规则：优先用快到期的；到期相同优先用赠送/专属积分",
    "qoder": "实测(10-06)：首次消耗正好落在最早到期的获赠包；套餐与赠送的先后未验证（当前套餐额度为 0）",
}


def next_consumed_pack(packs):
    """按“最早到期优先”找出下一个会被消耗的包（仅统计还有剩余的）。"""
    cands = [p for p in packs or [] if (p.get("remain") or 0) > 0 and p.get("expiry")]
    if not cands:
        return None
    return sorted(cands, key=lambda p: p["expiry"])[0]


def load_daily():
    """读取每日消耗基线。结构：{date, baselines:{平台:累计已用}, last_used:{平台:累计已用}}。"""
    try:
        d = json.loads(DAILY_PATH.read_text())
        if isinstance(d, dict):
            d.setdefault("baselines", {})
            d.setdefault("last_used", {})
            return d
    except Exception:
        pass
    return {"date": "", "baselines": {}, "last_used": {}}


def save_daily(d):
    try:
        DAILY_PATH.write_text(json.dumps(d, ensure_ascii=False))
    except Exception:
        pass


def collect_status(cfg=None):
    cfg = cfg or load_config()
    out = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "platforms": {}}
    daily = load_daily()
    today = today_str()
    # 跨天：用昨天最后一次观测到的累计已用作为今天基线（最贴近 0 点的真实值，
    # 能覆盖“凌晨到面板首次刷新前”这段时间的消耗）；新装则基线从今天首次观测值起算。
    if daily.get("date") != today:
        daily = {"date": today, "baselines": dict(daily.get("last_used") or {}), "last_used": {}}
    used_today = {}
    for name in PLATFORMS:
        try:
            out["platforms"][name] = STATUS_FN[name](cfg)
        except Exception as e:
            out["platforms"][name] = {"ok": False, "error": str(e)[:200], "configured": None}
        st = out["platforms"][name]
        # 今日消耗 = 当前累计已用 − 当日基线（credits_used 为终身累计、单调不减）
        if st.get("ok") and st.get("credits_used") is not None:
            used = float(st["credits_used"])
            daily.setdefault("baselines", {})
            if name not in daily["baselines"]:
                daily["baselines"][name] = used  # 今天首次见到该平台，定基线
            st["today_consumed"] = max(0.0, round(used - daily["baselines"][name], 4))
            daily["last_used"][name] = used
            used_today[name] = st["today_consumed"]
        else:
            st["today_consumed"] = None  # 未配置 / 读取失败，无法统计
            if st.get("credits_used") is not None:
                daily.setdefault("last_used", {})[name] = float(st["credits_used"])
        if st.get("ok") and st.get("checked_in_today"):
            st["today_checkin"] = today_checkin_info(name)  # None = 在官方客户端完成，历史无记录
        st["next_pack"] = next_consumed_pack(st.get("packages"))
        st["priority_rule"] = PRIORITY_RULES.get(name)
    save_daily(daily)
    total_today = round(sum(v for v in used_today.values() if v), 4)
    out["today_consumed"] = {
        "total": total_today,
        "by_platform": {k: round(v, 4) for k, v in used_today.items()},
    }
    out["mode"] = {"idle_timeout": IDLE_TIMEOUT, "resident": RESIDENT}
    STATE_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    return out


def today_checkin_info(platform, history=None):
    """从历史里查今天该平台真正执行签到(claimed)的时间和来源；没有则 None（多半是在官方客户端签的）。"""
    if history is None:
        try:
            history = json.loads(HISTORY_PATH.read_text())
        except Exception:
            return None
    today = today_str()
    by_map = {"cron": "定时任务", "auto": "面板自动补签", "manual": "面板手动点击"}
    for rec in history:
        if not (rec.get("time") or "").startswith(today):
            continue
        r = (rec.get("results") or {}).get(platform)
        if r and r.get("ok") and r.get("status") == "claimed":
            return {"at": rec["time"][11:16], "by": by_map.get(rec.get("source"), "面板")}
    return None


def run_checkin(only=None, source="manual"):
    cfg = load_config()
    results = {}
    for name in PLATFORMS:
        if only and name != only:
            continue
        try:
            results[name] = CHECKIN_FN[name](cfg)
        except Exception as e:
            results[name] = {"ok": False, "error": str(e)[:200]}
        time.sleep(0.8 + _rng.random() * 1.2)  # 平台间随机间隔，降低风控风险
    # 给"已签过"的结果补上今天是谁在几点签的，消除"到底签没签上"的疑惑
    for name, r in results.items():
        if r.get("ok") and r.get("status") == "already":
            info = today_checkin_info(name)
            r["signed"] = info or {"by": "官方客户端"}
    record = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "source": source, "results": results}
    history = []
    if HISTORY_PATH.exists():
        try:
            history = json.loads(HISTORY_PATH.read_text())
        except Exception:
            history = []
    # 结果与上一条完全相同（重复打开面板/重复点击的空跑）则不重复记录，
    # 避免历史里堆满一模一样的"已签"
    def outcome_sig(r):
        return (bool(r.get("ok")), r.get("status"), (r.get("error") or "")[:80] if not r.get("ok") else None)
    prev = history[0].get("results", {}) if history else {}
    same_as_prev = all(
        name in prev and outcome_sig(prev[name]) == outcome_sig(r)
        for name, r in results.items()
    ) and len(prev) >= len(results)
    if not same_as_prev:
        history.insert(0, record)
    HISTORY_PATH.write_text(json.dumps(history[:100], ensure_ascii=False, indent=1))
    os.chmod(HISTORY_PATH, 0o600)
    collect_status()
    return record


# ============ HTTP 服务 ============

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def do_GET(self):
        LAST_ACTIVITY["t"] = time.time()  # 任意请求都视为有活动（含浏览器每 5 分钟自动刷新）
        if self.path in ("/", "/index.html"):
            self._send(200, INDEX_PATH.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self._json(collect_status())
        elif self.path == "/api/history":
            try:
                self._json(json.loads(HISTORY_PATH.read_text()))
            except Exception:
                self._json([])
        elif self.path == "/link.svg":
            # 仅放行此公有图标；config.json/history.json 等同目录机密文件绝不暴露
            p = BASE_DIR / "link.svg"
            if p.is_file():
                self._send(200, p.read_bytes(), "image/svg+xml")
            else:
                self._json({"error": "not found"}, 404)
        elif self.path == "/api/info":
            self._json({"idle_timeout": IDLE_TIMEOUT, "resident": RESIDENT,
                        "uptime": int(time.time() - BOOT_TIME) if BOOT_TIME else 0})
        elif self.path == "/api/test_trae_session":
            """测试当前 TRAE 会话是否有效（用于设置弹窗的「测试会话」按钮）。"""
            cfg = load_config()
            session = (cfg.get("trae_session") or "").strip()
            if not session:
                self._json({"ok": False, "valid": False, "message": "尚未配置 TRAE 会话"})
                return
            token, err = trae_get_token(session)
            if token:
                self._json({"ok": True, "valid": True, "message": "会话有效 ✓"})
            else:
                self._json({"ok": False, "valid": False, "message": f"会话无效：{err or '未知错误'}"})
        elif self.path == "/api/heartbeat":
            # 心跳：页面“可见”时定期上报（见 index.html），保持轻量模式服务存活。
            # 标签页隐藏/关闭后心跳停止上报，服务端在空闲阈值（--idle-shutdown）后自动关停，
            # 从而实现“关标签页即停”；刷新页面会立即重新上报，重载间隙 < 空闲阈值，不会误杀。
            LAST_ACTIVITY["t"] = time.time()
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        global IDLE_TIMEOUT, RESIDENT
        LAST_ACTIVITY["t"] = time.time()
        length = int(self.headers.get("Content-Length") or 0)
        body = {}
        if length:
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                body = {}
        if self.path == "/api/checkin":
            only = body.get("platform") if body.get("platform") in PLATFORMS else None
            source = body.get("source") if body.get("source") in ("auto", "manual") else "manual"
            self._json(run_checkin(only, source))
        elif self.path == "/api/grab_trae":
            cfg = load_config()
            token, err, _ = trae_token_with_heal(cfg)
            if token:
                msg = "已自动获取 TRAE 最新登录 ✓"
            else:
                msg = "还没有拿到新登录：请确认已在打开的页面完成登录（出现积分页面即成功）"
            self._json({"ok": bool(token), "message": msg, "error": err})
        elif self.path == "/api/grab_qoder":
            # Qoder 逐包明细依赖内置浏览器里的 qoder.cn 网页会话（约 7 天过期）。
            # 前端在卡片上点「重新登录」后轮询本接口：用户在新标签页登录成功即可拿到会话。
            packs, reason = qoder_web_packages()
            if packs:
                msg = "已自动获取 Qoder 网页登录 ✓"
            elif reason == "expired":
                msg = "还没有拿到新的登录：请在打开的 qoder.cn 页面完成登录（看到额度页面即成功）"
            elif reason == "no-cookie":
                msg = "还没检测到 qoder.cn 登录态：请先在打开的页面完成登录"
            else:
                msg = f"暂时拿不到 Qoder 逐包明细（{reason}）"
            self._json({"ok": bool(packs), "message": msg, "reason": reason})
        elif self.path == "/api/config":
            cfg = load_config()
            for k in ("trae_session", "trae_device_id", "qoder_pat", "qoder_region",
                      "trae_session_expires", "qoder_pat_expires", "cookie_source"):
                if k in body:
                    cfg[k] = str(body[k]).strip()
            # 新写入 TRAE 会话时按 14 天估算有效期（可手动覆盖）
            if body.get("trae_session") and not (cfg.get("trae_session_expires") or "").strip():
                cfg["trae_session_expires"] = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
            if "auto_checkin_on_open" in body:
                cfg["auto_checkin_on_open"] = bool(body["auto_checkin_on_open"])
            if body.get("clear_trae_session"):
                cfg["trae_session"] = ""
                cfg["trae_session_expires"] = ""
            if body.get("clear_qoder_pat"):
                cfg["qoder_pat"] = ""
            save_config(cfg)
            self._json({"ok": True, "configured": {
                "trae": bool(cfg["trae_session"]), "qoder": bool(cfg["qoder_pat"]),
                "cookie_source": cfg.get("cookie_source", "zcode")}})
        elif self.path == "/api/mode":
            # 运行时切换运行模式：resident=true→常驻（IDLE_TIMEOUT=0，关标签页也保持运行）；
            # resident=false→轻量（关标签页即停，空闲 LIGHTWEIGHT_IDLE_SECONDS 后自关）。
            # 切换立即生效，无需重启；看门狗线程始终运行并按当前 IDLE_TIMEOUT 判定。
            want = body.get("resident")
            if want is True:
                IDLE_TIMEOUT = 0
                RESIDENT = True
            elif want is False:
                IDLE_TIMEOUT = LIGHTWEIGHT_IDLE_SECONDS
                RESIDENT = False
            else:
                self._json({"error": "resident 必须是 true/false"}, 400)
                return
            self._json({"ok": True, "mode": {"idle_timeout": IDLE_TIMEOUT, "resident": RESIDENT}})
        elif self.path == "/api/shutdown":
            # 一键停止（轻量模式的“关”）：先回包再关服务，避免连接半开
            self._json({"ok": True, "message": "服务已停止"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        else:
            self._json({"error": "not found"}, 404)


def _parse_mode_args():
    """解析运行模式参数，返回 (idle_seconds, resident, open_browser, port)。"""
    idle = 0
    resident = False
    open_browser = False
    port = DEFAULT_CONFIG["port"]
    if "--resident" in sys.argv:
        resident = True
    if "--open" in sys.argv:
        open_browser = True
    if "--idle-shutdown" in sys.argv:
        try:
            idle = int(sys.argv[sys.argv.index("--idle-shutdown") + 1]) * 60
        except (ValueError, IndexError):
            idle = 0
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except (ValueError, IndexError):
            pass
    return idle, resident, open_browser, port


def main():
    global IDLE_TIMEOUT, RESIDENT, BOOT_TIME
    if "--checkin-now" in sys.argv:
        rec = run_checkin(source="cron")
        print(json.dumps(rec, ensure_ascii=False, indent=1))
        return
    idle, resident, open_browser, port = _parse_mode_args()
    IDLE_TIMEOUT = idle
    RESIDENT = resident or idle == 0  # 没给 --idle-shutdown 也没给 --resident → 默认常驻
    cfg = load_config()
    # 监听所有网卡，同 WiFi 下的手机也能访问；凭据仍只在本机读取，页面不含 token
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    LAST_ACTIVITY["t"] = time.time()
    BOOT_TIME = time.time()
    tag = f"轻量模式（空闲 {idle // 60} 分钟自动关闭）" if idle > 0 else "常驻模式"
    print(f"签到面板已启动: http://127.0.0.1:{port} · {tag}")
    ip = lan_ip()
    if ip:
        print(f"手机/平板访问: http://{ip}:{port}（需与本机同一 WiFi；首次启动请在系统弹窗允许 Python 接收传入连接）")
    if open_browser:
        try:
            webbrowser.open(f"http://127.0.0.1:{port}/")
        except Exception:
            pass
    # 看门狗始终运行；仅当 IDLE_TIMEOUT>0（轻量模式）时才在空闲超时后关停。
    # 这样无论以哪种模式启动，运行时都能通过 /api/mode 在“轻量/常驻”间切换。
    def _watchdog():
        while True:
            time.sleep(30)
            if IDLE_TIMEOUT > 0 and time.time() - LAST_ACTIVITY["t"] > IDLE_TIMEOUT:
                print("空闲超时，自动关闭面板服务（轻量模式）")
                httpd.shutdown()
                break
    threading.Thread(target=_watchdog, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
