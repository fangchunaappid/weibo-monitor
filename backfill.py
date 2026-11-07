#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性补发: 2026-11-07 监控停摆期间漏推的 9 条微博。

背景: state.json 从 11-06 20:26 到 11-07 22:53 未更新; 恢复运行 #4172
只推送了第 1 页(16:29-17:02)的 10 条并把 last_id 推到最新, 导致当天
07:41-15:53 的 9 条不再会被推送。ID 取自 ludinggong-weibo-monitor
归档 data/posts.json 的 11-07 条目, 已逐条用 statuses/show 核对。

用法:
    python backfill.py                          # dry-run, 只打印不推送
    BACKFILL_CONFIRM=YES python backfill.py    # 真正推送到飞书
环境变量: FEISHU_WEBHOOK(必填), FEISHU_SECRET(可选), WEIBO_COOKIE(可选)
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import urllib.request

from weibo_monitor import _api_headers, _visitor_cookies, clean_text, post_summary

UID = "3962719063"
IDS = [
    "5362522746258550",
    "5362545567467860",
    "5362546785649351",
    "5362590853106126",
    "5362600363954266",
    "5362633348746571",
    "5362637085083053",
    "5362638038501610",
    "5362646515449863",
]
NOTE = "> ⏰ **补发**：2026-11-07 监控停摆期间漏发的微博（非实时更新）"


def fetch_status(pid, cookie=""):
    url = "https://m.weibo.cn/statuses/show?id=" + pid
    req = urllib.request.Request(url, headers=_api_headers(cookie or _visitor_cookies()))
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    if data.get("ok") != 1:
        raise RuntimeError("statuses/show 返回 ok=%s" % data.get("ok"))
    return data["data"]


def push_card(webhook, secret, name, time_str, body):
    header = {"template": "orange",
              "title": {"tag": "plain_text", "content": "📢 [补发] %s 的微博" % name}}
    elements = [{"tag": "div", "text": {"tag": "lark_md",
                "content": "%s\n\n**%s**\n%s" % (NOTE, time_str, body)}}]
    card = {"config": {"wide_screen_mode": True}, "header": header, "elements": elements}
    payload = {"msg_type": "interactive", "card": card}
    if secret:
        ts = str(int(time.time()))
        sign = base64.b64encode(hmac.new(("%s\n%s" % (ts, secret)).encode(),
                                         b"", hashlib.sha256).digest()).decode()
        payload["timestamp"] = ts
        payload["sign"] = sign
    req = urllib.request.Request(webhook, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8", "replace"))
    if result.get("code") not in (0, None) and result.get("StatusCode") != 0:
        raise RuntimeError("飞书推送失败: %s" % result)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    confirm = os.environ.get("BACKFILL_CONFIRM", "").strip().upper() == "YES"
    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    secret = os.environ.get("FEISHU_SECRET", "").strip()
    cookie = os.environ.get("WEIBO_COOKIE", "").strip()
    if confirm and not webhook:
        print("ERROR: 缺少 FEISHU_WEBHOOK")
        return 2
    print("模式 = %s ; 待补发 %d 条" % ("真实推送" if confirm else "DRY-RUN(不推送)", len(IDS)))
    ok = 0
    for pid in IDS:
        try:
            mb = fetch_status(pid, cookie)
            _, t, body = post_summary(UID, mb, cookie)
            name = (mb.get("user") or {}).get("screen_name") or "老舅"
            print("  %s  %s  %s" % (pid, t, clean_text(mb.get("text", ""))[:40]))
            if confirm:
                push_card(webhook, secret, name, t, body)
                print("    -> 已补发")
                time.sleep(2)
            ok += 1
        except Exception as e:
            print("  %s 失败: %s" % (pid, e))
    print("完成: %d/%d" % (ok, len(IDS)))
    return 0 if ok == len(IDS) else 1


if __name__ == "__main__":
    sys.exit(main())
