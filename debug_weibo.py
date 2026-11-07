# -*- coding: utf-8 -*-
"""临时诊断：在 GitHub runner（美国 IP）上测试多种免登录访问方式。"""
import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request

UA_M = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36")
UA_D = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
UID = "3962719063"
CTRL = "6246096815"


def get(url, headers, timeout=20):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:  # noqa: BLE001
        return -1, str(e)[:200]


def visitor2():
    u = "https://visitor.passport.weibo.cn/visitor/genvisitor2?cb=visitor_callback&from=weibo.cn"
    _, txt = get(u, {"User-Agent": UA_M})
    m = re.search(r"\((\{.*\})\)", txt)
    d = (json.loads(m.group(1)).get("data") or {}) if m else {}
    return d.get("sub", ""), d.get("subp", "")


def visitor_old():
    u = ("https://passport.weibo.com/visitor/genvisitor?cb=gen_callback"
         "&fp=%7B%22os%22%3A%221%22%2C%22browser%22%3A%22Chrome%22%7D")
    _, txt = get(u, {"User-Agent": UA_M, "Referer": "https://weibo.com/"})
    m = re.search(r"\((\{.*\})\)", txt)
    d = (json.loads(m.group(1)).get("data") or {}) if m else {}
    return d.get("sub", ""), d.get("subp", "")


def site_cookies():
    _, _ = get("https://m.weibo.cn/", {"User-Agent": UA_M})
    req = urllib.request.Request("https://m.weibo.cn/", headers={"User-Agent": UA_M})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            sc = r.headers.get_all("Set-Cookie") or []
    except Exception:  # noqa: BLE001
        sc = []
    jar = {}
    for c in sc:
        k, _, v = c.split(";")[0].partition("=")
        jar[k.strip()] = v.strip()
    return jar


def show(tag, status, txt):
    ok = None
    if txt.strip().startswith("{"):
        try:
            ok = json.loads(txt).get("ok")
        except Exception:  # noqa: BLE001
            pass
    print(f"CASE {tag}: http={status} ok={ok} len={len(txt)} head={txt[:110]!r}")


sub, subp = visitor2()
print("visitor2 sub_len", len(sub), "subp_len", len(subp))
old_sub, old_subp = visitor_old()
print("visitor_old sub_len", len(old_sub), "subp_len", len(old_subp))

ck = f"SUB={sub}; SUBP={subp}"
api = f"https://m.weibo.cn/api/container/getIndex?type=uid&value={UID}&containerid=107603{UID}&page=1"
base = {"User-Agent": UA_M}

# 1 现状
show("1_current", *get(api, {**base, "Cookie": ck}))
# 2 加用户页 Referer + PWA 头
show("2_ref_user", *get(api, {**base, "Cookie": ck,
                                  "Referer": f"https://m.weibo.cn/u/{UID}",
                                  "X-Requested-With": "XMLHttpRequest",
                                  "MWeibo-Pwa": "1"}))
# 3 对照组（另一个账号）
ct = api.replace(UID, CTRL).replace("107603" + UID, "107603" + CTRL)
show("3_control", *get(ct, {**base, "Cookie": ck}))
# 4 旧版 visitor
show("4_old_visitor", *get(api, {**base, "Cookie": f"SUB={old_sub}; SUBP={old_subp}"}))
# 5 站点 Cookie + XSRF 头
jar = site_cookies()
print("site cookies:", sorted(jar.keys()))
combined = "; ".join(f"{k}={v}" for k, v in jar.items())
xsrf = jar.get("XSRF-TOKEN", "")
try:
    xsrf_dec = base64.b64decode(xsrf).decode("utf-8", "replace")
except Exception:  # noqa: BLE001
    xsrf_dec = xsrf
show("5_site_cookie", *get(api, {**base, "Cookie": combined,
                                 "X-XSRF-TOKEN": xsrf_dec,
                                 "Referer": f"https://m.weibo.cn/u/{UID}"}))
# 6 桌面 ajax 接口（无 Cookie）
show("6_ajax_nocookie", *get(
    f"https://weibo.com/ajax/statuses/mymblog?uid={UID}&page=1&feature=0",
    {**base, "User-Agent": UA_D, "Referer": f"https://weibo.com/u/{UID}",
     "X-Requested-With": "XMLHttpRequest"}))
# 7 桌面 ajax 接口（游客 Cookie）
show("7_ajax_visitor", *get(
    f"https://weibo.com/ajax/statuses/mymblog?uid={UID}&page=1&feature=0",
    {**base, "User-Agent": UA_D, "Cookie": ck, "Referer": f"https://weibo.com/u/{UID}",
     "X-Requested-With": "XMLHttpRequest"}))
# 8 个人资料接口
show("8_profile", *get(f"https://weibo.com/ajax/profile/info?uid={UID}", {**base, "Cookie": ck}))
# 9 主页 HTML
show("9_html", *get(f"https://weibo.com/u/{UID}",
                    {**base, "User-Agent": UA_D, "Cookie": ck}))
# 10 m 站时间线（非 container）
show("10_m_timeline", *get(
    f"https://m.weibo.cn/api/statuses/user_timeline?uid={UID}&page=1", {**base, "Cookie": ck}))

# ---- 11+ 直接用监控脚本自身的逻辑复现 ----
import importlib.util
spec = importlib.util.spec_from_file_location("wm", "weibo_monitor.py")
wm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wm)

for i in range(3):
    wm._reset_visitor()
    try:
        posts = wm.fetch_posts(UID)
        print(f"CASE 11.{i} monitor_fetch OK posts={len(posts)}")
    except Exception as e:  # noqa: BLE001
        print(f"CASE 11.{i} monitor_fetch FAIL {str(e)[:180]}")

wm._reset_visitor()
try:
    posts = wm.fetch_posts(CTRL)
    print(f"CASE 12 monitor_fetch control OK posts={len(posts)}")
except Exception as e:  # noqa: BLE001
    print(f"CASE 12 monitor_fetch control FAIL {str(e)[:180]}")

# ---- 13 连续多次同一接口，观察是否限流 ----
for i in range(5):
    ck2 = wm._visitor_cookies() if i == 0 else wm._visitor_cookies()
    st, txt = get(api, {**base, "Cookie": ck2})
    ok = None
    try:
        ok = json.loads(txt).get("ok")
    except Exception:  # noqa: BLE001
        pass
    print(f"CASE 13.{i} rapid http={st} ok={ok} len={len(txt)}")
