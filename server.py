#!/usr/bin/env python3
"""AI 平台每日签到面板 — WorkBuddy / TRAE / Qoder 一键签到 + 积分总览。

用法:
  python3 server.py                # 启动 Web 面板 (默认 http://127.0.0.1:8787)
  python3 server.py --checkin-now  # 无界面执行一次签到(供定时任务调用)后退出
  python3 server.py --port 9000    # 指定端口
"""

import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
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
# ZCode 内置浏览器的 Cookie 库（明文存储）：用户在其中登录过 trae.cn 后，
# 面板可以直接从这里无感获取最新的 X-Cloudide-Session，免去手动复制粘贴。
IAB_COOKIE_DB = Path.home() / "Library/Application Support/ZCode/session/Partitions/zcode-embedded-browser/Cookies"

DEFAULT_CONFIG = {
    "port": 8787,
    "trae_session": "",
    "trae_device_id": "",
    "qoder_pat": "",
    "qoder_region": "china",  # china | global
    "auto_checkin_on_open": True,
}

ctx = ssl.create_default_context()


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
    """返回 (status, 解析后的JSON或None, 原始文本)。"""
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

def workbuddy_creds():
    if not WORKBUDDY_AUTH_FILE.exists():
        return None
    try:
        d = json.loads(WORKBUDDY_AUTH_FILE.read_text())
        token = d["auth"]["accessToken"]
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


def workbuddy_status():
    creds = workbuddy_creds()
    if not creds:
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


def workbuddy_checkin():
    creds = workbuddy_creds()
    if not creds:
        return {"ok": False, "error": "未找到本地 WorkBuddy 凭据"}
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


def grab_trae_session():
    """从 ZCode 内置浏览器 Cookie 库提取最新的 TRAE 会话串；拿不到返回 None。"""
    if not IAB_COOKIE_DB.exists():
        return None
    import shutil as _sh
    import sqlite3 as _sq
    import tempfile as _tf
    tmp = Path(_tf.mkdtemp()) / "Cookies"
    try:
        _sh.copy(IAB_COOKIE_DB, tmp)
        con = _sq.connect(str(tmp))
        row = con.execute(
            "SELECT value, encrypted_value FROM cookies "
            "WHERE host_key LIKE '%trae.cn' AND name='X-Cloudide-Session' "
            "ORDER BY creation_utc DESC LIMIT 1").fetchone()
        con.close()
    except Exception:
        return None
    if not row:
        return None
    val, enc = row
    return (val or "").strip() or None  # Cookie 库若为加密存储则无法直接读取


def trae_token_with_heal(cfg):
    """获取 TRAE JWT；当前会话失效时先尝试从内置浏览器无感换取新会话。

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

    fresh = grab_trae_session()
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
    device_id = cfg.get("trae_device_id") or "".join(random.choices("0123456789", k=16))

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
    device_id = cfg.get("trae_device_id") or "".join(random.choices("0123456789", k=16))

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
            device_id = "".join(random.choices("0123456789", k=16))
            time.sleep(1 + random.random() * 2)
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
    """从 ZCode 内置浏览器 Cookie 库读 qoder.cn 的网页会话（可选增强数据源）。"""
    if not IAB_COOKIE_DB.exists():
        return None
    import shutil as _sh
    import sqlite3 as _sq
    import tempfile as _tf
    tmp = Path(_tf.mkdtemp()) / "Cookies"
    try:
        _sh.copy(IAB_COOKIE_DB, tmp)
        con = _sq.connect(str(tmp))
        row = con.execute(
            "SELECT value FROM cookies WHERE name='qoder_session_cookie' "
            "AND host_key LIKE '%qoder.cn' ORDER BY expires_utc DESC LIMIT 1").fetchone()
        con.close()
    except Exception:
        return None
    return (row[0] or "").strip() if row else None


def qoder_web_packages():
    """用内置浏览器的 qoder.cn 登录态抓逐包额度（含真实到期日）。
    网页会话约 7 天一换：拿不到就返回 None，面板退回 openapi 汇总数据，不影响签到。"""
    cookie = grab_qoder_web_cookie()
    if not cookie:
        return None
    h = {"accept": "application/json", "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X)",
         "cookie": "qoder_session_cookie=" + cookie}
    st, data, _ = http_json("https://qoder.cn/api/v2/me/usages/big_model_credits", "GET", h)
    if st != 200 or not isinstance(data, dict):
        return None

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
    return packs or None


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
    # 优先用网页会话拿逐包明细（含真实到期日）；拿不到就用 openapi 汇总
    web = qoder_web_packages()
    if web:
        packs = web
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
CHECKIN_FN = {"workbuddy": lambda cfg: workbuddy_checkin(),
              "trae": trae_checkin, "qoder": qoder_checkin}
STATUS_FN = {"workbuddy": lambda cfg: workbuddy_status(),
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
        time.sleep(0.8 + random.random() * 1.2)  # 平台间随机间隔，降低风控风险
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
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
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
        elif self.path == "/api/config":
            cfg = load_config()
            for k in ("trae_session", "trae_device_id", "qoder_pat", "qoder_region",
                      "trae_session_expires", "qoder_pat_expires"):
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
                "trae": bool(cfg["trae_session"]), "qoder": bool(cfg["qoder_pat"])}})
        else:
            self._json({"error": "not found"}, 404)


def main():
    if "--checkin-now" in sys.argv:
        rec = run_checkin(source="cron")
        print(json.dumps(rec, ensure_ascii=False, indent=1))
        return
    port = DEFAULT_CONFIG["port"]
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    cfg = load_config()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"签到面板已启动: http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
