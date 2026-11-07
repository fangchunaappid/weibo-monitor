# -*- coding: utf-8 -*-
"""临时诊断：单次抓取 + 打印出口 IP，用于统计 GitHub runner IP 的通过率。"""
import importlib.util
import urllib.request


def get_ip():
    for url in ("https://ifconfig.me/ip", "https://api.ipify.org"):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                return r.read().decode().strip()
        except Exception:  # noqa: BLE001
            continue
    return "?"


spec = importlib.util.spec_from_file_location("wm", "weibo_monitor.py")
wm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wm)

ip = get_ip()
for uid in ("3962719063", "6246096815"):
    try:
        posts = wm.fetch_posts(uid)
        print(f"RESULT ip={ip} uid={uid} OK posts={len(posts)}")
    except Exception as e:  # noqa: BLE001
        print(f"RESULT ip={ip} uid={uid} FAIL {str(e)[:100]}")
