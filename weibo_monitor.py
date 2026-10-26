#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微博博主更新监控 -> 飞书群机器人推送

用法:
    python weibo_monitor.py          # 单次检查(GitHub Actions 定时触发用)
    python weibo_monitor.py --loop   # 本机持续轮询

配置:
    config.json    填博主 uids 数组(支持同时监控多个博主)
    环境变量(优先级更高):
        WB_UIDS         逗号分隔的多个博主 uid
        WB_UID          单个博主 uid(本地测试用, 兼容旧配置)
        WEIBO_COOKIE    可选, 浏览器登录微博后的 Cookie(默认用自动游客身份, 无需登录)
        FEISHU_WEBHOOK  飞书自定义机器人 webhook 地址
        FEISHU_SECRET   飞书机器人签名校验密钥(未开启则不填)

状态:
    state.json 记录各博主已推送的最新微博 id, 换博主或删掉对应条目则重新以当前最新一条为基线(不推送旧微博)

GitHub Actions 部署:
    1. 建议公开仓库(Actions 免费额度不限时); webhook 等敏感信息放 Secrets, 泄露风险低
    2. 仓库 Settings -> Secrets and variables -> Actions 添加 FEISHU_WEBHOOK(及可选 FEISHU_SECRET)
    3. config.json 填入博主 uid 后推送到仓库
    4. Actions 页手动 workflow_dispatch 跑一次验证; 之后每 10 分钟自动检查
"""

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
STATE_FILE = os.path.join(BASE_DIR, "state.json")

UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36")
MAX_PAGES = 3          # 最多向前翻几页找新微博(防止一次漏太多)
MAX_BODY_LEN = 1800    # 消息正文截断长度
TZ8 = timezone(timedelta(hours=8))

log = logging.getLogger("weibo-monitor")

# 游客身份缓存(进程内): genvisitor2 一次换取 SUB/SUBP, 会话失效后重新生成
_visitor = {"sub": "", "subp": ""}


def _visitor_cookies():
    if _visitor["sub"]:
        return f"SUB={_visitor['sub']}; SUBP={_visitor['subp']}"
    url = "https://visitor.passport.weibo.cn/visitor/genvisitor2?cb=visitor_callback&from=weibo.cn"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        txt = resp.read().decode("utf-8", "replace")
    m = re.search(r"\((\{.*\})\)", txt)
    data = (json.loads(m.group(1)).get("data") or {}) if m else {}
    if not data.get("sub"):
        raise RuntimeError(f"游客身份获取失败: {txt[:150]}")
    _visitor["sub"] = data["sub"]
    _visitor["subp"] = data.get("subp", "")
    log.info("已获取微博游客身份")
    return f"SUB={_visitor['sub']}; SUBP={_visitor['subp']}"


def _reset_visitor():
    _visitor["sub"] = _visitor["subp"] = ""


def _api_headers(cookie):
    h = {
        "User-Agent": UA,
        "Referer": "https://m.weibo.cn/",
        "MWeibo-Pwa": "1",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*",
    }
    if cookie:
        h["Cookie"] = cookie
    return h


# ---------------------------------------------------------------- 基础工具

def load_config():
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    env = os.environ
    raw = (env.get("WB_UIDS") or env.get("WB_UID")
           or ",".join(cfg.get("uids") or [])
           or cfg.get("uid") or "")
    cfg["uids"] = [u.strip() for u in str(raw).split(",") if u.strip()]
    cfg["cookie"] = (env.get("WEIBO_COOKIE") or cfg.get("cookie") or "").strip()
    feishu = cfg.setdefault("feishu", {})
    feishu["webhook"] = (env.get("FEISHU_WEBHOOK") or feishu.get("webhook") or "").strip()
    feishu["secret"] = (env.get("FEISHU_SECRET") or feishu.get("secret") or "").strip()
    return cfg


def load_state():
    state = {"bloggers": {}}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            log.warning("state.json 损坏, 将重建基线")
            return state
        if "bloggers" not in data and data.get("uid"):
            # 旧版单博主结构自动迁移
            data = {"bloggers": {data["uid"]: {
                "last_id": data.get("last_id"),
                "screen_name": data.get("screen_name"),
            }}}
            log.info("已将 state.json 迁移为多博主结构")
        if isinstance(data.get("bloggers"), dict):
            state["bloggers"] = data["bloggers"]
    return state


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- 微博抓取

def fetch_posts(uid, user_cookie="", page=1):
    """返回某页的 mblog 列表(按接口原始顺序)。
    user_cookie 优先; 无则用自动游客身份, 会话失效(432/ok=-100)时重新生成重试。"""
    url = (f"https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}"
           f"&containerid=107603{uid}&page={page}")
    last_err = None
    for attempt in range(3):
        try:
            cookie = user_cookie or _visitor_cookies()
            req = urllib.request.Request(url, headers=_api_headers(cookie))
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok") == 1:
                cards = (data.get("data") or {}).get("cards") or []
                return [c["mblog"] for c in cards if c.get("mblog")]
            last_err = f"接口返回 ok={data.get('ok')} msg={str(data)[:150]}"
            if data.get("ok") == -100 and not user_cookie:
                _reset_visitor()  # 游客会话失效, 重新生成
            else:
                # ok=0/其它: uid 无效或被风控, 配置登录 Cookie 可提高稳定性
                break
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code == 432 and not user_cookie:
                _reset_visitor()
            else:
                break
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            if attempt == 2:
                break
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"拉取微博失败: {last_err}")


def is_pinned(mb):
    title = (mb.get("title") or {}).get("text", "")
    return title == "置顶" or bool(mb.get("isTop"))


def clean_text(raw):
    t = re.sub(r"<br\s*/?>", "\n", raw or "")
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def fmt_time(s):
    if not s:
        return ""
    s = str(s).strip()
    # 相对时间("刚刚/今天 12:30/5分钟前")或中文日期直接展示
    if not re.match(r"^[A-Z][a-z]{2} ", s):
        return s
    try:
        dt = datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
        return dt.astimezone(TZ8).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return s


def fetch_full_text(pid, user_cookie=""):
    """长微博全文(接口默认截断为 ...全文), 失败返回 None 降级用截断文本"""
    url = f"https://m.weibo.cn/statuses/show?id={pid}"
    try:
        cookie = user_cookie or _visitor_cookies()
        req = urllib.request.Request(url, headers=_api_headers(cookie))
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("ok") == 1:
            return clean_text((data.get("data") or {}).get("text"))
    except Exception as e:  # noqa: BLE001
        log.warning("全文抓取失败 id=%s: %s, 降级用截断文本", pid, e)
    return None


def post_summary(uid, mb, user_cookie=""):
    """把 mblog 转成 (id_int, 时间, 正文markdown)"""
    pid = str(mb.get("id") or mb.get("mid") or "")
    text = clean_text(mb.get("text"))
    if mb.get("isLongText"):
        full = fetch_full_text(pid, user_cookie)
        if full:
            text = full
    # 去掉接口截断残留的 "…全文" 尾巴
    text = re.sub(r"(?:…|⋯|\.{2,}|。{2,})?\s*全文$", "", text).strip()
    ret = mb.get("retweeted_status")
    if ret:
        ret_user = (ret.get("user") or {}).get("screen_name", "")
        ret_text = clean_text(ret.get("text"))[:300]
        ret_text = re.sub(r"(?:…|⋯|\.{2,}|。{2,})?\s*全文$", "", ret_text).strip()
        text = f"{text}\n\n———— 转发 @{ret_user}: {ret_text}".strip()

    pics = []
    for p in (mb.get("pics") or [])[:9]:
        url = (p.get("large") or p).get("url")
        if url:
            pics.append(url)
    if pics:
        text += "\n\n" + " ".join(f"[图{i + 1}]({u})" for i, u in enumerate(pics))

    bid = mb.get("bid")
    link = f"https://weibo.com/{uid}/{bid}" if bid else f"https://m.weibo.cn/detail/{pid}"
    if len(text) > MAX_BODY_LEN:
        text = text[:MAX_BODY_LEN] + " ..."
    text += f"\n\n[原微博]({link})"
    return int(pid), fmt_time(mb.get("created_at")), text


# ---------------------------------------------------------------- 飞书推送

def push_feishu(webhook, secret, name, time_str, body):
    """发一条卡片消息, 失败抛异常"""
    header = {"template": "blue", "title": {"tag": "plain_text", "content": f"📢 {name} 更新了微博"}}
    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**{time_str}**\n{body}"}},
    ]
    card = {"config": {"wide_screen_mode": True}, "header": header, "elements": elements}
    payload = {"msg_type": "interactive", "card": card}
    if secret:
        ts = str(int(time.time()))
        sign = base64.b64encode(
            hmac.new(f"{ts}\n{secret}".encode("utf-8"), b"", hashlib.sha256).digest()
        ).decode()
        payload["timestamp"] = ts
        payload["sign"] = sign

    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if result.get("code") not in (0, None) and result.get("StatusCode") != 0:
        raise RuntimeError(f"飞书推送失败: {result}")


# ---------------------------------------------------------------- 主流程

def check_once(cfg):
    uids = cfg["uids"]
    if not uids:
        log.error("未配置博主 uid: 请在 config.json 的 uids 数组或环境变量 WB_UIDS 中填写")
        return 2
    state = load_state()
    worst = 0
    for uid in uids:
        try:
            worst = max(worst, check_blogger(cfg, uid, state))
        except Exception as e:  # noqa: BLE001
            log.error("[%s] 检查异常: %s", uid, e)
            worst = max(worst, 1)
    return worst


def check_blogger(cfg, uid, state):
    """检查单个博主, 返回退出码: 0正常 1有失败 2配置错误"""
    blogger = state["bloggers"].setdefault(uid, {})
    last_id = int(blogger["last_id"]) if blogger.get("last_id") else 0
    name = cfg.get("screen_name") or blogger.get("screen_name") or f"博主(uid={uid})"

    if not last_id:
        # 首次运行(或换了博主): 以当前最新一条为基线, 不推送历史微博
        posts = fetch_posts(uid, cfg["cookie"], 1)
        normal = [mb for mb in posts if mb.get("id") and not is_pinned(mb)]
        if not normal:
            log.warning("[%s] 未取到任何微博, 下次再尝试建立基线", name)
            return 0
        newest = max(normal, key=lambda m: int(str(m["id"])))
        blogger["last_id"] = str(int(str(newest["id"])))
        if newest.get("user", {}).get("screen_name"):
            blogger["screen_name"] = newest["user"]["screen_name"]
        save_state(state)
        log.info("[%s] 首次运行, 已建立基线 last_id=%s, 之后的更新才会推送",
                 name, blogger["last_id"])
        return 0

    seen = {}  # pid -> (pid, time, text), 防翻页重叠导致重复推送
    for page in range(1, MAX_PAGES + 1):
        posts = fetch_posts(uid, cfg["cookie"], page)
        if not posts:
            break
        normal = [mb for mb in posts if not is_pinned(mb)]
        page_new = []
        for mb in normal:
            if not mb.get("id") or int(str(mb["id"])) <= last_id:
                continue  # 只为真正的新微博构建摘要(全文抓取有额外请求)
            s = post_summary(uid, mb, cfg["cookie"])
            seen[s[0]] = s
            page_new.append(s)
        fresh = sorted(seen.values())
        # 本页已翻到旧微博 -> 后面只会更旧, 停止翻页
        if not page_new or len(page_new) < len(normal):
            break

    if not fresh:
        log.info("[%s] 无新微博 (last_id=%s)", name, last_id)
        return 0

    log.info("[%s] 发现 %d 条新微博, 推送飞书...", name, len(fresh))
    webhook = cfg["feishu"]["webhook"]
    if not webhook:
        log.error("[%s] 未配置 FEISHU_WEBHOOK, 无法推送 ( Secrets 或 config.json 里填)", name)
        return 2
    pushed = 0
    for pid, t, text in fresh:
        try:
            push_feishu(webhook, cfg["feishu"]["secret"], name, t, text)
            pushed += 1
            log.info("[%s] 已推送 id=%s", name, pid)
            blogger["last_id"] = str(pid)
            save_state(state)  # 逐条落盘, 中断也不丢
            time.sleep(2)  # 控制频率
        except Exception as e:  # noqa: BLE001
            log.error("[%s] 推送 id=%s 失败: %s (下次运行将重试)", name, pid, e)
            break
    if pushed < len(fresh):
        return 1
    log.info("[%s] 全部推送完成", name)
    return 0


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    cfg = load_config()
    if "--loop" in sys.argv:
        lo, hi = (cfg.get("interval_minutes") or [8, 12])[:2]
        while True:
            try:
                check_once(cfg)
            except Exception as e:  # noqa: BLE001
                log.error("本轮检查异常: %s", e)
            wait = random.uniform(lo * 60, hi * 60)
            log.info("等待 %.1f 分钟后进行下一轮", wait / 60)
            time.sleep(wait)
    else:
        sys.exit(check_once(cfg))


if __name__ == "__main__":
    main()
