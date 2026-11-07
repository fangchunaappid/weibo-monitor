# -*- coding: utf-8 -*-
"""临时诊断：分页探测 guest 态是否被登录墙拦截。"""
import importlib.util
import json
import re
import urllib.request


def visitor():
    u = "https://visitor.passport.weibo.cn/visitor/genvisitor2?cb=visitor_callback&from=weibo.cn"
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
                                                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                                                          "Chrome/126.0.0.0 Mobile Safari/537.36"})
    with urllib.request.urlopen(req, timeout=20) as r:
        txt = r.read().decode("utf-8", "replace")
    m = re.search(r"\((\{.*\})\)", txt)
    d = (json.loads(m.group(1)).get("data") or {}) if m else {}
    return d.get("sub", ""), d.get("subp", "")


sub, subp = visitor()
uid = "3962719063"
for page in range(1, 5):
    url = (f"https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}"
           f"&containerid=107603{uid}&page={page}")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
        "Referer": "https://m.weibo.cn/",
        "MWeibo-Pwa": "1",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*",
        "Cookie": f"SUB={sub}; SUBP={subp}",
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            txt = r.read().decode("utf-8", "replace")
        try:
            ok = json.loads(txt).get("ok")
        except Exception:  # noqa: BLE001
            ok = None
        print(f"PAGE {page} ok={ok} len={len(txt)} head={txt[:80]!r}")
    except Exception as e:  # noqa: BLE001
        print(f"PAGE {page} ERROR {str(e)[:80]}")
