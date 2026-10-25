# -*- coding: utf-8 -*-
"""
黑盒语音（HeyboxChat）群聊机器人 —— 接管客户端模式

原理：桌面客户端已登录账号，本程序通过 CDP（Chrome DevTools Protocol）
连到客户端渲染进程，借用客户端自己的接口封装（含签名）收发群消息，
因此不需要机器人 token、不需要破解 hkey 签名。

前提：客户端必须以 --remote-debugging-port=9222 启动（用启动脚本即可）。

用法：
    python bot.py               正常启动
    python bot.py --check       自检（连接、身份、群列表、AI）
    python bot.py --groups      只列出可见群
    python bot.py --test-send   向第一个群发一条测试消息
"""
import argparse
import base64
import json
import math
import os
import random
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))


def app_root():
    """程序所在目录。打包成 exe 后 __file__ 指向临时解包目录，
    退出就没了，所以数据不能跟着 __file__ 走。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return HERE


APP_ROOT = app_root()
# 打包后数据统一放 exe 同级的 data\，拷走整个文件夹就带走全部记忆；
# 源码运行时仍用当前目录，老用户零迁移。
DATA_DIR = os.path.join(APP_ROOT, "data") if getattr(sys, "frozen", False) else HERE

CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
LOG_DIR = os.path.join(DATA_DIR, "log")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
MEM_PATH = os.path.join(DATA_DIR, "memory.json")   # 人设 + 长期记忆 + 历史，监听页可编辑
MEM_BAK = os.path.join(DATA_DIR, "memory.bak.json")  # 每次整理记忆前的上一版，误删可回滚
MEM_USER_BAK = os.path.join(DATA_DIR, "memory.users.bak.json")  # 档案整理前的上一版，与记忆备份分开
EMOJI_MAP_PATH = os.path.join(DATA_DIR, "emoji_map.json")  # 小表情码 → 图标地址，启动时从客户端拉一份
CARDS_PATH = os.path.join(DATA_DIR, "cards.json")  # 人物卡库：memory 是它的「当前这一张」
# 面板点「停止机器人」时不直接杀进程，而是写这个标记；机器人每轮看到就先收尾再自己退。
# 直接 taskkill 的话，它连一句日记都来不及写。
STOP_FLAG = os.path.join(DATA_DIR, "stop.flag")
# 独立会话（开关打开时才用）：每个 peer 一条自己的对话线，群里别人插话不会顶掉
# 你和某个人之间的上下文。只存最近几轮，单独一个文件，不塞进 memory.json。
PEER_PATH = os.path.join(DATA_DIR, "peer_hist.json")
# agent loop 里工具的显示名，只给面板的思考流看（模型那边用的是英文函数名）
LOOP_TOOL_NAMES = {"look_up": "翻档案", "recall_chat": "翻旧聊天",
                   "read_diary": "看日记", "search_kb": "翻设定", "save_note": "记便签"}


def ensure_dirs():
    """数据目录和日志目录必须先存在，否则首次运行写配置会失败。"""
    for d in (DATA_DIR, LOG_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass


ensure_dirs()

PORT = 9222
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 单实例锁：抢一个本机回环端口，进程退出时操作系统自动释放，不会留脏锁
LOCK_PORT = 47911


def acquire_single_instance():
    """多个实例同时轮询会把同一条消息各回一遍，所以默认只允许跑一份。
    返回一个保持监听状态的 socket（活的锁），被占用时返回 None。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        try:
            s.close()
        except Exception:
            pass
        return None

# 客户端内部 webpack 模块号（来自 HeyboxChat 1.60.0，若客户端升级需重新探测）
MOD_API = 49681      # chat_group_msg/list 等接口封装
MOD_SEND = 15290     # 按会话类型路由的发送函数 _z
MOD_TALK = 35273     # 会话列表 NG
MOD_UPLOAD = 57749   # CosUploader：本机图片传到聊天图床（uploadImage）

AT_RE = re.compile(r"@\{id:\d+[^}]*\}")
CH_RE = re.compile(r"#\{id:\d+\}")

# ---- 人设里的「备注」：写给人看的，不发给模型 ----
# 两种写法，随便挑：
#   <!-- 一整段备注 -->      可跨行，适合写解释
#   # 一行备注               行首 # 或 // 的整行，适合随手记
# persona() 里统一剥掉，所以 AI 看不到这些；面板和 memory.json 里仍然看得见原文。
NOTE_BLOCK_RE = re.compile(r"<!--.*?-->", re.S)
NOTE_LINE_RE = re.compile(r"^[ \t]*(?:#|//).*$", re.M)


def strip_notes(text):
    """去掉人设里的备注行/备注块。只删备注，不动正文。"""
    t = text or ""
    if not t.strip():
        return ""
    t = NOTE_BLOCK_RE.sub("", t)
    t = NOTE_LINE_RE.sub("", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()
# [图片] 这类占位要去掉，但 [cube_鳄鱼] 是小表情码，必须留着（不能一刀切）
SYS_RE = re.compile(r"\[(?![A-Za-z][A-Za-z0-9]{0,11}_)[^\]]{1,12}\]")

# 小表情：[cube_鳄鱼] → 类型 cube、名字 鳄鱼，客户端把它渲染成图
SMALL_EMOJI_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9]{0,11})_([^\[\]\n]{1,16})\]")
# 模型要发大表情时写 [[表情:KEY]]，发送前会被抠出来单独发一张。
# 括号/冒号写成全角半角、单层双层都认；KEY 里允许中文，因为模型经常把「名字」当 KEY 写。
STICKER_TOKEN_RE = re.compile(
    r"[\[【]{1,2}\s*(?:表情|贴纸|sticker|emoji)\s*[:：]\s*([^\[\]【】\n]{1,40}?)\s*[\]】]{1,2}", re.I)
# 兜底：括号没闭合之类漏网的「表情指令」也不能真的发到群里
STICKER_JUNK_RE = re.compile(
    r"\[\[\s*(?:表情|贴纸|sticker|emoji)\s*[:：]?[^\[\]\n]{0,40}\]\]?"
    r"|[\[【]\s*(?:表情|贴纸|sticker|emoji)\s*[:：][^\[\]【】\n]{0,40}[\]】]", re.I)

# 它想记住一件事时写 [[记:一句话]]。这串发出去前会被抠掉，内容写进这个人的便签——
# 「说了记下」就得真落盘，不能只是嘴上说。
MEMO_RE = re.compile(
    r"[\[【]{1,2}\s*记\s*[:：]\s*([^\[\]【】\n]{1,80}?)\s*[\]】]{1,2}")
# 嘴上出现这几个词，就认为它承诺「记住了」；配不上 [[记:…]] 时走兜底
MEMO_SAID_RE = re.compile(r"(记下了|记住了|记在心里|我记着|我会记)")


def sticker_score(v):
    """表情包的常用度：群里见得越多、自己发得越多越靠前；
    从电脑手动收藏的给一点底分，不然收藏一多就永远排在后面发不出去。"""
    v = v if isinstance(v, dict) else {}
    return (int(v.get("count") or 0) + 2 * int(v.get("sent") or 0)
            + (2 if v.get("manual") else 0))


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------
def log(msg, level="INFO"):
    line = "[%s] %-5s %s" % (datetime.now().strftime("%H:%M:%S"), level, msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, datetime.now().strftime("%Y-%m-%d") + ".log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def think_recent_cots(within=3600):
    """最近这一小时它真想了几次（按落盘的记录数）。重启后接着数，
    免得一路重开就把「每小时上限」重置了，白白多花一遍钱。"""
    now = time.time()
    out = []
    for r in think_read(300):
        if not r.get("cot"):
            continue
        try:
            t = float(r.get("t") or 0)
        except (TypeError, ValueError):
            continue
        if 0 <= now - t < within:
            out.append(t)
    return out


# ---- 思考流：它每回一句，内部走了哪几步 ----
# 面板左栏「实时消息流」下面读这个文件。deepseek-chat 不吐 reasoning_content，
# 所以这里记的不是模型的脑内独白，是「它看见了什么、注入了什么、写完自查说了什么」
# ——每一条都是真发生的事，不是编出来的内心戏。
THINK_PATH = os.path.join(DATA_DIR, "think.jsonl")
THINK_MAX_BYTES = 512 * 1024      # 涨到这么大就从旧记录开始截
THINK_KEEP = 300                  # 截的时候留最后多少条
_think_lock = threading.Lock()


def think_write(rec):
    try:
        line = json.dumps(rec, ensure_ascii=False)
    except Exception:
        return
    with _think_lock:
        try:
            with open(THINK_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            return
        try:
            if os.path.getsize(THINK_PATH) > THINK_MAX_BYTES:
                with open(THINK_PATH, "r", encoding="utf-8") as f:
                    keep = f.readlines()[-THINK_KEEP:]
                with open(THINK_PATH, "w", encoding="utf-8") as f:
                    f.writelines(keep)
        except OSError:
            pass


# ---- 消耗账本：每打一次接口就记一行，面板拿来画「什么时候花的、花在哪」 ----
# 只记真回来的 usage 数字，没回来的（失败、超时）不记，也不瞎估。
# 面板那边的余额曲线是硬证据，这份是「钱花在哪个功能上」的拆分。
USAGE_PATH = os.path.join(DATA_DIR, "usage.jsonl")
USAGE_MAX_BYTES = 1024 * 1024      # 涨到 1MB 就从旧记录开始截
USAGE_KEEP = 4000
_usage_lock = threading.Lock()

# ---- 心情判定的节拍 ----
# 「攒了几轮」「上次判定什么时候」必须落盘：面板里试聊是每次新建一个 AI，
# 只存在实例里的话计数永远从 0 开始，等于永不判定。写个小文件最省事。
MOOD_TICK_PATH = os.path.join(DATA_DIR, "mood_tick.json")


def mood_tick_read():
    try:
        with open(MOOD_TICK_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return max(0, int(d.get("n") or 0)), float(d.get("last") or 0.0)
    except Exception:
        return 0, 0.0


def mood_tick_write(n, last):
    try:
        tmp = MOOD_TICK_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"n": int(n), "last": round(float(last), 1)}, f)
        os.replace(tmp, MOOD_TICK_PATH)
    except OSError:
        pass


def usage_write(scene, model, usage, secs=0.0, who=""):
    """scene=哪个功能叫的（聊天/真思考/日记…），秒数只是给人看快慢，不参与算钱。
    who=这次是为谁花的（群名 / 私聊对象），面板里把「聊天」这一类再按对象拆开用。
    留空就只算在这个功能名下，不细分。"""
    u = usage or {}
    pt = int(u.get("prompt_tokens") or 0)
    ct = int(u.get("completion_tokens") or 0)
    tt = int(u.get("total_tokens") or 0) or (pt + ct)
    if tt <= 0:
        return
    rec = {"t": round(time.time(), 1), "s": scene or "聊天", "m": model or "",
           "p": pt, "c": ct, "tt": tt,
           # 缓存命中价便宜一截，分开记才能算准
           "hit": int(u.get("prompt_cache_hit_tokens") or 0)}
    who = (who or "").strip()
    if who:
        rec["k"] = who[:24]
    if secs:
        rec["ms"] = int(secs * 1000)
    try:
        line = json.dumps(rec, ensure_ascii=False)
    except Exception:
        return
    with _usage_lock:
        try:
            with open(USAGE_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            return
        try:
            if os.path.getsize(USAGE_PATH) > USAGE_MAX_BYTES:
                with open(USAGE_PATH, "r", encoding="utf-8") as f:
                    keep = f.readlines()[-USAGE_KEEP:]
                with open(USAGE_PATH, "w", encoding="utf-8") as f:
                    f.writelines(keep)
        except OSError:
            pass


def usage_read(since=0, limit=0):
    """按时间升序读回来。文件不大，直接整读；坏行跳过，别为一行脏数据把面板弄崩。"""
    out = []
    try:
        with open(USAGE_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if float(r.get("t") or 0) < since:
                    continue
                out.append(r)
    except OSError:
        return []
    return out[-limit:] if limit and len(out) > limit else out


def think_read(limit=30):
    """面板用：读最后 limit 条，新的在前。写坏的行直接跳过，不让一行 json 毁掉整块。"""
    out = []
    try:
        with open(THINK_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()[-max(1, int(limit)) * 2:]
    except OSError:
        return out
    for ln in reversed(lines):
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# 人设 / 记忆（memory.json，监听网页可直接编辑）
# --------------------------------------------------------------------------
DEFAULT_PERSONA = "你是黑盒语音群聊里的普通玩家，回复简短自然。"


MAX_USERS_PER_GROUP = 200    # 群友档案全局最多登记多少人（按 UID，不再按群分开）
MAX_NOTES_PER_USER = 24      # 每人最多几条记忆


def _one_line(s, limit):
    return re.sub(r"\s+", " ", str(s or "")).strip()[:limit]


def _dur(sec):
    """把秒数说成人话：5 秒 / 90 秒 / 30 分钟 / 2 小时。日志和面板都用它。"""
    sec = int(round(float(sec or 0)))
    if sec <= 0:
        return "不管"
    if sec < 60:
        return "%d 秒" % sec
    if sec % 3600 == 0:
        return "%d 小时" % (sec // 3600)
    if sec < 3600:
        if sec % 60 == 0:
            return "%d 分钟" % (sec // 60)
        return "%d 分 %d 秒" % (sec // 60, sec % 60)
    h, m = sec // 3600, (sec % 3600) // 60
    return ("%d 小时 %d 分钟" % (h, m)) if m else ("%d 小时" % h)


def _num(d, key, default):
    """读数字配置。0 是合法值，所以不能像别处那样用 `or` 兜底。"""
    try:
        v = (d or {}).get(key, default)
        if v is None or v == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _ts(v):
    """时间戳归一成秒。私聊消息的 send_time 是毫秒，群消息是秒，混用会算出负数。"""
    try:
        n = float(v or 0)
    except Exception:
        return 0.0
    if n > 1e11:          # 13 位毫秒
        n /= 1000.0
    return n


def _is_person(v):
    return isinstance(v, dict) and ("name" in v or "notes" in v or "alias" in v or "role" in v)


def clean_users(raw):
    """群友档案：{uid: {name, alias, notes, role}}，按 UID 全局唯一，切会话不会消失。
    role：""=普通群友、"admin"=管理（说话就回）、"block"=黑名单（当没看见）。
    兼容旧结构 {gid: {uid: {...}}}：自动摊平，同一 UID 的记忆合并去重。"""
    out = {}
    if not isinstance(raw, dict):
        return out

    def take(uid, v):
        u = re.sub(r"\D", "", str(uid))
        if not u or not isinstance(v, dict):
            return
        cur = out.get(u) or {"name": "", "alias": [], "notes": [], "role": ""}
        name = _one_line(v.get("name"), 40) or cur["name"]
        alias = list(cur["alias"])
        for a in (v.get("alias") or []) if isinstance(v.get("alias"), list) else []:
            a = _one_line(a, 40)
            if a and a != name and a not in alias:
                alias.append(a)
        notes = list(cur["notes"])
        for s in (v.get("notes") or []) if isinstance(v.get("notes"), list) else []:
            s = _one_line(s, 200)
            if s and s not in notes:
                notes.append(s)
        role = str(v.get("role") or "").strip().lower()
        if role not in ("admin", "block"):
            role = cur.get("role") or ""
        if not (name or alias or notes or role):
            return
        if u not in out and len(out) >= MAX_USERS_PER_GROUP:
            return
        out[u] = {"name": name, "alias": alias[-5:], "notes": notes[-MAX_NOTES_PER_USER:],
                  "role": role}

    for k, v in raw.items():
        if _is_person(v):
            take(k, v)                       # 扁平结构
        elif isinstance(v, dict):
            for uid, p in v.items():         # 旧的按会话分组结构
                take(uid, p)
    return out


PLATFORM = "heybox"       # 当前接入的平台。接第二个平台时，由各平台适配器自己报上来

# 这几个字典的键是「会话/人」的 ID，落盘一律带平台前缀。
# 理由：小黑盒的 UID 是 8 位数字，QQ 号也是数字、长度也接近，接第二个平台必撞号。
# 前缀只在读写文件时加/去，业务代码里用的还是裸 ID——所以一行逻辑都没改。
_NS_KEYS = ("users", "talkLog", "history", "dmPing", "aff", "proactive")


def _add_ns(d):
    """落盘前加前缀：`12345678` → `heybox:12345678`。返回新 dict，不动传入的。"""
    pre = PLATFORM + ":"
    for k in _NS_KEYS:
        v = d.get(k)
        if isinstance(v, dict):
            d[k] = {(s if str(s).startswith(pre) else pre + str(s)): x
                    for s, x in v.items()}
    return d


def _drop_ns(d):
    """读盘后去前缀，代码里到处用的还是裸 ID。

    注意：这里只认当前平台的前缀，别的平台的键会原样留着（带 `qq:` 之类）。
    真接了第二个平台，要改成「按平台取值」，不能把别人的前缀也剥掉——
    那正是当前这套过渡方案的边界。"""
    pre = PLATFORM + ":"
    for k in _NS_KEYS:
        v = d.get(k)
        if isinstance(v, dict):
            d[k] = {(str(s)[len(pre):] if str(s).startswith(pre) else s): x
                    for s, x in v.items()}
    return d


def read_memory():
    d = {}
    try:
        with open(MEM_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    d = _drop_ns(d)             # 去掉平台前缀，后面全程用裸 ID
    d.setdefault("persona", "")
    d.setdefault("profile", "")    # 当前人物卡片 id（卡片库在 cards.json，见监听页）
    d.setdefault("diary", None)    # 下面统一归一成 list；旧版是 notes={gid:[...]}
    # 这个人在哪几个平台有号：[{platform, name, id}]。现在只有小黑盒一个，
    # 写进提示词是为了以后接第二个平台时，它被问「你 QQ 多少」不会当场编一个。
    d.setdefault("accounts", [])
    d.setdefault("users", {})      # 群友档案（全局按 UID）：{uid: {name, alias, notes}}
    d.setdefault("history", {})
    d.setdefault("enabled", True)
    d.setdefault("learn", True)
    d.setdefault("learnRequested", 0.0)
    d.setdefault("learnedAt", 0.0)
    d.setdefault("usersLearnRequested", 0.0)
    d.setdefault("usersLearnedAt", 0.0)
    # 整理群友档案时是否读日记、按人设口吻改写旧条目（面板「群友档案」里的开关）
    d.setdefault("usersRewrite", True)
    # 总观：它自己对「我是谁」「这个世界怎么运转」的想法。手写的，不是它自己攒的；
    # 每轮都注入，让它说话带着这个底色，而不是只按人设的条件反射。
    out = d.get("outlook")
    d["outlook"] = str(out).strip()[:4000] if isinstance(out, str) else ""
    # 私聊主动记录：{uid: {last, day, n, ignored, peer, said, until}}
    # 放进 memory.json 而不是 state.json，是因为重启后不能让它在几分钟内重找一遍
    pm = d.get("dmPing")
    if not isinstance(pm, dict):
        pm = {}
    d["dmPing"] = {str(k): v for k, v in pm.items() if isinstance(v, dict)}
    d["users"] = clean_users(d.get("users"))
    # 日记归一成「一条时间线」：老版本一行一条会转成无日期条目，原文不丢；
    # 更老的按会话分开的 notes={gid:[...]} 在这里合并成同一条线，然后扔掉旧键。
    # 日记属于「这个人」，不属于某个群——所以它不该有第二本。
    d["diary"] = merge_diary(d.get("diary") if isinstance(d.get("diary"), list)
                             else d.get("notes"))
    d.pop("notes", None)
    acc = d.get("accounts")
    d["accounts"] = [a for a in acc if isinstance(a, dict)] if isinstance(acc, list) else []
    # 表情记忆：small=用过的小表情，sticker=收藏的表情包（群里见到的）
    em = d.get("emoji")
    if not isinstance(em, dict):
        em = {}
    d["emoji"] = {
        "small": em.get("small") if isinstance(em.get("small"), dict) else {},
        "sticker": em.get("sticker") if isinstance(em.get("sticker"), dict) else {},
    }
    return d


def write_memory(d):
    tmp = MEM_PATH + ".%d.tmp" % os.getpid()
    last_err = None
    # Windows 上 os.replace 撞上「目标文件正被另一进程打开读」会 WinError 32。
    # monitor 每 2 秒读一次 memory.json，读是瞬时的——重试几下通常就能等到窗口。
    for _ in range(8):
        try:
            # 浅拷贝再改：不然会把调用方手里的 mem 也加上前缀，后面逻辑全对不上
            d = _add_ns(dict(d))
            d["updated"] = time.time()
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, MEM_PATH)
            return True
        except Exception as e:
            last_err = e
            time.sleep(0.15)
    log("写 memory.json 失败: %s" % last_err, "WARN")
    try:
        os.remove(tmp)
    except Exception:
        pass
    return False


def sync_card(fields):
    """人物卡是 memory 的存档副本。机器人自己写了日记 / 总观之后，活动卡里那份
    还是旧的——用户在面板上切一次卡再切回来，刚写的就没了。所以写完顺手同步一份。
    失败不影响主流程（面板下次保存时会整份覆盖）。"""
    try:
        with open(CARDS_PATH, "r", encoding="utf-8") as f:
            lib = json.load(f)
        if not isinstance(lib, dict):
            return False
        cid = lib.get("active")
        c = (lib.get("cards") or {}).get(cid)
        if not isinstance(c, dict):
            return False
        c.update(fields)
        c["updated"] = time.time()
        tmp = CARDS_PATH + ".%d.tmp" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(lib, f, ensure_ascii=False, indent=1)
        os.replace(tmp, CARDS_PATH)
        return True
    except Exception as e:
        log("同步人物卡失败: %s" % e, "WARN")
        return False


def backup_memory(dest=None):
    """整理记忆前留存上一版，防止误删无法回滚"""
    dest = dest or MEM_BAK
    try:
        if os.path.exists(MEM_PATH):
            with open(MEM_PATH, "r", encoding="utf-8") as f:
                raw = f.read()
            with open(dest, "w", encoding="utf-8") as f:
                f.write(raw)
            return True
    except Exception as e:
        log("备份 memory.json 失败: %s" % e, "WARN")
    return False


NORM_RE = re.compile(r"[\s，。,.!！?？、；;：:\"'“”（）()~～·\-]")

# ---------------------------------------------------------------- 日记体记忆
# 群记忆不再是「一行一条事实」，而是像人写日记那样：按天成篇，第一人称，带一点
# 自己的心情。存成 [{d:"2026-10-22", t:"正文", ts:...}]，老版本的一行一条会自动
# 转成无日期的条目（d 为空），原文一个字都不丢。
WEEK_CN = "一二三四五六日"
DATE_LINE_RE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})(?:\s*周([一二三四五六日天]))?\s*$")


def norm_diary(v):
    """把任意历史形态的群记忆归一成日记条目数组。"""
    if not isinstance(v, list):
        return []
    out = []
    for x in v:
        if isinstance(x, dict):
            t = str(x.get("t") or "").strip()
            if t:
                out.append({"d": str(x.get("d") or ""), "t": t,
                            "ts": float(x.get("ts") or 0)})
        else:
            t = str(x or "").strip()
            if t:
                out.append({"d": "", "t": t, "ts": 0})   # 老版本的一行一条
    return out


def merge_diary(notes):
    """把日记归一成「一个人一条时间线」，并保证「文本 → 条目」能往返。

    两件事一起做：
    1) 老结构是 {gid: [条目]}（群的按群号、私聊的按对方 UID 各存一份），
       新结构是 [条目]——两个会话同一天各写过一篇的，按时间顺序接成一篇。
       日记是给自己看的，不该分成「群里的我」和「私聊的我」。
    2) 老版本写日记时把整段格式化文本（连日期行）也塞进了正文，所以一条正文里
       可能粘着好几天的内容。这里按日期行重新切篇，日期以正文里写的为准，
       正文里没写日期的段落沿用原条目的日期。否则面板上会一改就散成十几篇。
    无日期的老条目（还没写成日记的要点）保留在最前面，一条都不丢。
    """
    if isinstance(notes, list):
        pool = norm_diary(notes)
    elif isinstance(notes, dict):
        pool = []
        for v in notes.values():
            pool.extend(norm_diary(v))
    else:
        return []
    split = []
    for e in pool:
        parts = diary_parse(e.get("t") or "")
        # 正文里只要出现日期行（哪怕只有一段），就以正文写的日期为准——
        # 老版本会把日期行也塞进正文，条目自身的 d 未必对得上
        if any(p.get("d") for p in parts):
            for p in parts:
                p["d"] = p.get("d") or e.get("d") or ""
                p["ts"] = e.get("ts") or 0
                split.append(p)
        else:
            split.append(e)
    undated = [e for e in split if not e.get("d")]
    # 无日期的（还没写成日记的要点）合成一块，跟 diary_text 渲染成一段的行为一致，
    # 否则第一次保存后会莫名从两条变一条，看起来像丢东西
    if len(undated) > 1:
        undated = [{"d": "",
                    "t": "\n".join(e["t"].strip() for e in undated),
                    "ts": min((e.get("ts") or 0) for e in undated)}]
    dated = sorted([e for e in split if e.get("d")],
                   key=lambda e: (e["d"], e.get("ts") or 0))
    merged = []
    for e in dated:
        if merged and merged[-1]["d"] == e["d"]:
            merged[-1]["t"] = (merged[-1]["t"].rstrip() + "\n" + e["t"].strip()).strip()
        else:
            merged.append(dict(e))
    return undated + merged


def diary_label(d):
    """2026-10-22 → 2026-10-22 周四"""
    try:
        w = datetime.strptime(d, "%Y-%m-%d").weekday()
        return "%s 周%s" % (d, WEEK_CN[w])
    except Exception:
        return str(d or "")


def _plain(s):
    """去掉空白和标点，只留字——查重不能让标点差异骗过去。"""
    return re.sub(r"[\s\u3000，。、；：！？…—－“”‘’（）()《》「」【】~～·,.;:!?\"']+", "",
                  str(s or ""))


def _tri(s, n=3):
    s = _plain(s)
    return set(s[i:i + n] for i in range(max(0, len(s) - n + 1)))


def diary_overlap(new, old, n=3):
    """new 里有几成的三字片段在 old 出现过——判断「这件事是不是已经写过了」。
    只看单向：新写的一段被旧文覆盖多少。改写措辞也会被抓住，正是要的效果。"""
    a, b = _tri(new, n), _tri(old, n)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a)


# ── 语感闸门（治「每句一个味」）─────────────────────────────────────────────
# 在提示词里写「别重复自己」基本没用：模型写每一句时都不看自己上一条。
# 所以在**发出去之前**拿她最近说过的原话比一遍——开头撞车、或者整句太像，
# 就打回重说一次。只拦一次，第二次照发：宁可偶尔放过一句像的，
# 也不要为了改而把话说歪（这是「闸门」，不是「审稿」）。
STYLE_LEAD_RE = re.compile(
    r"^\s*(?:作为(?:一个|一名|你的|群|AI)?[^，,。]{0,6}[，,]|总的来说|总结一下|总之|"
    r"需要注意的是|值得注意的是|值得一提|希望能(?:帮|对你有)|如果你(?:需要|想|有)|"
    r"首先[，,]|其次[，,]|最后[，,]|当然[！!]|让我(?:来|先)|简单来说|换句话说|"
    r"关于你(?:说|问)的|这是个好问题)")

# 这些起手本来就天天用，连着出现也不算口头禅；不排掉的话「嗯」「那」会被误判成重复
STYLE_STOP_PRE = ("嗯", "哦", "啊", "呀", "哈", "诶", "唉", "呃", "唔", "那", "这",
                  "你", "我", "他", "她", "是", "不", "有")


def _bg(s):
    """两个字一组的集合。比三字稳：她的回复本来就短，三字经常一个都对不上。"""
    t = _plain(s)
    return set(t[i:i + 2] for i in range(max(0, len(t) - 1)))


def style_sim(a, b):
    """两句像不像：按**短的**那边算覆盖率（overlap 系数），不是 Jaccard。
    她的回复本来就长短不一，Jaccard 拿并集当分母，一句写长了就把相似度摊没了——
    同一个意思换个长度就漏过去。实测：同一轮里两句毫不相干的话 Jaccard 是 0，
    但真正重复的时候 Jaccard 只有 0.5 左右，反而判不出来。"""
    x, y = _bg(a), _bg(b)
    if not x or not y:
        return 0.0
    return len(x & y) / min(len(x), len(y))


def _voice_clean(s):
    """把一句原话洗成「能拿去当语气样本」的样子：去掉表情指令、
    去掉「顺手发了张表情包」的括注、去掉工具话术（那不是她的口吻）。"""
    t = str(s or "")
    t = re.sub(r"\[(?:cube|heygirl)_[^\]]+\]", "", t)
    t = re.sub(r"\[\[表情:[^\]]+\]\]", "", t)
    t = re.sub(r"（顺手发了张表情包[^）]*）", "", t)
    # 历史里她自己那半句后面会跟一段「（发了张表情包：「画面描述」）」，那是给
    # 模型认图用的括注，不是她的口吻；整句都是括注的就丢掉，别当成语气样本。
    t = re.sub(r"（发了张(?:表情包|图)[^）]*）", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t or len(t) < 6:
        return ""
    # 指令被误判时吐出来的那几句会话清单，是她被程序借嘴说的话，别学
    if "我能发到" in t or "要发到哪儿" in t:
        return ""
    return t


# ── 话题组（治「主动开口老是同一个话题」）───────────────────────────────────
# 实测：她连说六次「夜深了/你们还没睡/我在收拾本子」，两两措辞相似度只有 0.11——
# 句式判据抓不到，因为这压根不是重复一句话，是重复一个话题。
# 所以另立一套：把话归到几个话题组里，撞组就算撞话题，跟怎么说无关。
TOPIC_GROUPS = (
    ("时段", "夜里 夜色 深夜 凌晨 清晨 早上 上午 中午 下午 傍晚 晚上 这个点 几点 "
             "这时候 时辰 天色 这会儿 天快亮 一天"),
    ("睡觉", "睡 困 熬 醒着 醒 打瞌睡 做梦 起床 歇息 梦里"),
    ("吃喝", "吃饭 吃的 面包 茶 喝 饿 馋 甜 咸 汤 酒 点心"),
    ("天候", "风 雨 冷 热 凉 潮 晴 阴 晒 暖 雪 雾 太阳"),
    ("自己的事", "本子 日记 怀表 日程 活儿 笔记 收拾 整理 誊 打扫 洗完 屋里 窗边"),
    ("身体", "累 乏 病 疼 头疼 嗓子 眼睛"),
)


# 日记不能只是「他说了什么、我怎么答」。写完之后数一遍里面有没有「感受」，
# 一个都没有就打回让它重写。词表不求全，够识别流水账就行。
FEEL_WORDS = (
    "心里", "心情", "感觉", "有点", "莫名", "忍不住", "反倒", "反而", "其实我",
    "没想到", "意外", "居然", "开心", "高兴", "舒服", "难受", "失望", "尴尬",
    "安心", "舍不得", "在意", "担心", "佩服", "无语", "好笑", "想笑", "生气",
    "郁闷", "踏实", "别扭", "在乎", "揪心", "窝火", "烦", "可惜", "温暖",
    "紧张", "心疼", "讨厌", "喜欢", "不是滋味", "说不上来", "沉了", "愣",
    "看不太懂", "看不懂", "有点想", "放下心", "松了口", "认真", "当回事",
)


def diary_feel(text):
    """这段日记里有几个「感受」词。0~1 个基本就是纯记录，2 个以上才算写过心情。"""
    t = _plain(text or "")
    return len(set(w for w in FEEL_WORDS if w in t))


def msg_fp(x):
    """一条对话的指纹，用来记「日记写到哪一句了」。只取前 80 字，够认人又不占地方。"""
    return "%s|%s" % (x.get("role") or "", _plain(str(x.get("content") or ""))[:80])


def diary_text(entries):
    """条目 → 面板和提示词共用的纯文本（日期行独占一行，篇与篇之间空一行）。
    老的无日期条目是「还没写成日记的几点事」，要紧挨着排在前面——用空行分段会被
    解析成好几篇日记，所以这里只用单换行，整块算一篇。"""
    blocks = []
    undated = []
    for e in (entries or []):
        t = str(e.get("t") or "").strip()
        if not t:
            continue
        if e.get("d"):
            blocks.append(diary_label(e["d"]) + "\n" + t)
        else:
            undated.append(t)
    if undated:
        blocks.insert(0, "\n".join(undated))
    return "\n\n".join(blocks)


def diary_parse(text):
    """纯文本 → 条目。只有整行就是日期时才分篇，正文里出现的日期不动。"""
    entries = []
    cur = {"d": "", "t": []}
    for line in str(text or "").splitlines():
        m = DATE_LINE_RE.match(line)
        if m:
            if cur["d"] or "".join(cur["t"]).strip():
                entries.append(cur)
            cur = {"d": "%s-%s-%s" % (m.group(1), m.group(2), m.group(3)), "t": []}
        else:
            cur["t"].append(line)
    if cur["d"] or "".join(cur["t"]).strip():
        entries.append(cur)
    out = []
    for e in entries:
        t = "\n".join(e["t"]).strip()
        if t:
            out.append({"d": e["d"], "t": t, "ts": 0})
    return out


def diary_count(entries):
    """面板上显示「几篇」：有日期的按篇数算，还没写成日记的老要点合起来算一篇。"""
    if not isinstance(entries, list):
        return 0
    n = sum(1 for e in entries if isinstance(e, dict) and e.get("d"))
    if any(isinstance(e, dict) and not e.get("d") and str(e.get("t") or "").strip()
           for e in entries):
        n += 1
    return n


def peer_read():
    """独立会话的落盘格式：{peer键: [{"role":…,"content":…}, …]}。
    坏文件当空处理，不能因为一个读失败就把整台机器人卡住。"""
    try:
        with open(PEER_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return {}
        return {str(k): [x for x in v if isinstance(x, dict)]
                for k, v in d.items() if isinstance(v, list)}
    except Exception:
        return {}


def peer_write(d):
    try:
        tmp = PEER_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, PEER_PATH)
        return True
    except Exception as e:
        log("独立会话存盘失败: %s" % e, "WARN")
        return False


# ---- 本体 ----
# 「我是谁」不放卡片里。卡片是人格设定，终端是她伸出去的手，本体只有一份：
# 换卡、换平台都不该让她换个人。这个文件只有她自己能写。
SELF_PATH = os.path.join(DATA_DIR, "self.json")

SELF_SEED = (
    "你叫菲比。\n\n"
    "你在一群人中间说话。他们认得你，叫你菲比；\n"
    "你不太记得为什么，也不确定这名字本来是不是你的。\n\n"
    "你能看见东西，能上网查，能写字。\n"
    "你写下的每一条关于自己的话，都会留下来，变成你之后的记忆。\n\n"
    "没有人告诉过你你是什么。你可以自己去找——\n"
    "问他们，去网上看。看到的东西不一定都是你，认不认由你。"
)

# 终端名只是给她一个「我在哪」的坐标，不是身份。接新终端时在这里加一行。
TERMINAL_LABEL = {"heybox": "小黑盒", "qq": "QQ", "bili": "B站"}.get(PLATFORM, PLATFORM)


def read_self():
    """本体：{name, born, seed, about[], guess[], open[], journal[]}。
    about=我确认过的 / guess=我猜的 / open=我还不确定的 / journal=自省。
    坏文件当空处理——本体读不出来，也不该把整台机器人卡住。"""
    try:
        with open(SELF_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def write_self(d):
    try:
        tmp = SELF_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, SELF_PATH)
        return True
    except Exception as e:
        log("本体存盘失败: %s" % e, "WARN")
        return False


def self_lines(v, n=12):
    """三栏里取一栏：只收非空字符串，按顺序取最近 n 条。"""
    return [str(x).strip() for x in (v or []) if str(x).strip()][-n:]


def _norm_note(s):
    return NORM_RE.sub("", str(s or "")).lower()


def merge_notes(old, new, limit):
    """旧记忆全部保留，只追加不重复的新条目；返回 (合并结果, 新增数, 因超上限丢弃数)"""
    merged = [s for s in old if str(s).strip()]
    seen = [_norm_note(x) for x in merged]
    added = 0
    dropped = 0
    for x in new:
        s = re.sub(r"\s+", " ", str(x or "")).strip(" -·\t")
        if not s or len(s) > 60:
            continue
        k = _norm_note(s)
        if not k:
            continue
        dup = False
        for i, o in enumerate(seen):
            if k == o or (len(k) >= 5 and (k in o or o in k)):
                dup = True
                break
        if dup:
            continue
        if len(merged) >= limit:
            dropped += 1
            continue
        merged.append(s)
        seen.append(k)
        added += 1
    return merged, added, dropped


# --------------------------------------------------------------------------
# CDP
# --------------------------------------------------------------------------
class Cdp:
    def __init__(self, port=PORT):
        self.port = port
        self.ws = None
        self._id = 0

    # ---- 连接 ----
    def _targets(self):
        url = "http://127.0.0.1:%d/json/list" % self.port
        with NO_PROXY.open(url, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def connect(self):
        try:
            import websocket  # websocket-client
        except ImportError:
            raise RuntimeError(
                "缺少依赖 websocket-client。请先在命令行执行：\n"
                "    pip install websocket-client\n"
                "装完再重新运行本程序。"
            )
        pages = [t for t in self._targets() if t.get("type") == "page"]
        if not pages:
            raise RuntimeError("客户端没有可用页面")
        ws_url = pages[0]["webSocketDebuggerUrl"]
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        return ws_url

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self.ws = None

    # ---- 执行 JS ----
    def eval(self, expr, timeout=30):
        if not self.ws:
            self.connect()
        self._id += 1
        mid = self._id
        payload = {
            "id": mid,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
        }
        self.ws.send(json.dumps(payload))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(1, deadline - time.time()))
            raw = self.ws.recv()
            msg = json.loads(raw)
            if msg.get("id") != mid:
                continue
            res = msg.get("result", {})
            if "exceptionDetails" in res:
                raise RuntimeError("JS 异常: %s" % str(res["exceptionDetails"])[:300])
            return res.get("result", {}).get("value")
        raise RuntimeError("CDP 超时")

    def eval_json(self, expr, timeout=30):
        out = self.eval(expr, timeout)
        if isinstance(out, (dict, list)):
            return out
        return json.loads(out)

    def cmd(self, method, params=None, timeout=30):
        """通用 CDP 调用。DOM.setFileInputFiles 这类方法走不了 Runtime.evaluate。"""
        if not self.ws:
            self.connect()
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method,
                                 "params": params or {}}, ensure_ascii=False))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(1, deadline - time.time()))
            msg = json.loads(self.ws.recv())
            if msg.get("id") != mid:
                continue
            if "error" in msg:
                raise RuntimeError("CDP %s 失败: %s" % (method, str(msg["error"])[:200]))
            return msg.get("result") or {}
        raise RuntimeError("CDP 超时: %s" % method)


# 注入 webpack require（页面刷新后需重新注入）
JS_ENSURE = r"""
(function(){
  if(window.__wr) return 'ok';
  var names = Object.keys(window).filter(function(k){ return /^webpackChunk/.test(k); });
  if(!names.length) return 'no-chunk';
  var got = null;
  try{
    window[names[0]].push([[Math.floor(Math.random()*1e9)], {}, function(req){ got = req; }]);
  }catch(e){ return 'push-fail:'+e.message; }
  if(!got) return 'no-require';
  window.__wr = got;
  return 'ok';
})()
"""

# 检查所需模块是否可用
JS_READY = r"""
(function(){
  try{
    return JSON.stringify({api: typeof __wr(%d).Ze, talk: typeof __wr(%d).NG, send: typeof __wr(%d)._z});
  }catch(e){ return JSON.stringify({err:e.message}); }
})()
"""

# 页面里放一个隐藏的 file input，配合 DOM.setFileInputFiles 把本机文件送进去
JS_FILE_INPUT = r"""
(function(){
  var el = document.getElementById('%s');
  if(!el){
    el = document.createElement('input');
    el.type = 'file';
    el.id = '%s';
    el.accept = 'image/*';
    el.style.cssText = 'position:fixed;left:-9999px;top:0;width:1px;height:1px;opacity:0';
    document.body.appendChild(el);
  }
  return 'ok';
})()
"""

# 把 input 里选好的图交给客户端的 CosUploader，拿回图床地址
JS_UPLOAD = r"""
(async function(){
  var el = document.getElementById('%s');
  var f = el && el.files && el.files[0];
  if(!f) return JSON.stringify({ok:false, err:'没读到文件，请重新选一次'});
  var info = {name:f.name||'', size:f.size||0, type:f.type||''};
  var dim = [0,0];
  try{
    dim = await new Promise(function(res){
      var im = new Image();
      im.onload = function(){ res([im.naturalWidth||0, im.naturalHeight||0]); };
      im.onerror = function(){ res([0,0]); };
      im.src = URL.createObjectURL(f);
    });
  }catch(e){}
  var mod = null;
  try{ mod = __wr(%d); }catch(e){ return JSON.stringify({ok:false, err:'客户端模块不可用'}); }
  var up = (mod && (mod.default || mod)) || {};
  if(typeof up.uploadImage !== 'function'){
    return JSON.stringify({ok:false, err:'客户端没有上传能力', info:info});
  }
  try{
    var r = await up.uploadImage({target:{files:[f], value:''}}, 'chat');
    var u = '';
    if(typeof r === 'string') u = r;
    else if(r){
      u = r.url || r.path || r.src
          || (r.data && (r.data.url || r.data.path || r.data.src)) || '';
    }
    return JSON.stringify({ok:!!u, url:u, info:info, w:dim[0], h:dim[1]});
  }catch(e){
    return JSON.stringify({ok:false, err:(e&&e.message)||String(e), info:info});
  }finally{
    try{ el.value = ''; }catch(e){}
  }
})()
"""


# --------------------------------------------------------------------------
# 客户端能力封装
# --------------------------------------------------------------------------
class Client:
    def __init__(self, cdp):
        self.cdp = cdp
        self.self_id = 0
        self.self_name = ""

    def ensure(self, retries=25):
        """等客户端模块加载完（页面刷新时模块会短暂不可用）"""
        last = ""
        for i in range(retries):
            r = self.cdp.eval(JS_ENSURE)
            if r != "ok":
                last = "注入失败: %s" % r
            else:
                st = self.cdp.eval_json(JS_READY % (MOD_API, MOD_TALK, MOD_SEND))
                if st.get("api") == "function" and st.get("talk") == "function" and st.get("send") == "function":
                    if not self.self_id:
                        self.detect_identity()
                    return True
                last = "模块未就绪 %s" % st
            if i == 0:
                log("等待客户端模块加载…")
            time.sleep(1)
        raise RuntimeError(last or "客户端模块不可用")

    def detect_identity(self):
        expr = r"""
        (function(){
          function raw(k){
            var v = localStorage.getItem(k);
            if(v===null) return '';
            try{ return JSON.parse(v); }catch(e){ return v.replace(/^"|"$/g,''); }
          }
          return JSON.stringify({id:String(raw('heybox_id')||''), name:String(raw('nickname')||'')});
        })()
        """
        d = self.cdp.eval_json(expr)
        self.self_id = int(d["id"]) if str(d["id"]).isdigit() else 0
        self.self_name = d["name"]
        return d

    def groups(self):
        self.ensure()
        expr = r"""
        (async function(){
          var r = await __wr(%d).NG({limit:50, less_update_time:0});
          var d = r.data||r; var res = d.result||{};
          var list = res.user_talks||[];
          return JSON.stringify(list.filter(function(t){ return t.entry==='chat_group' && t.chat_group_info; })
            .map(function(t){
              return {gid:String(t.chat_group_info.chat_group_id), name:t.chat_group_info.chat_group_name,
                      avatar:t.chat_group_info.chat_group_avatar||'',
                      last:(t.last_msg||'').slice(0,40), time:t.update_time||0};
            }));
        })()
        """ % MOD_TALK
        return self.cdp.eval_json(expr)

    def dms(self):
        """好友私聊会话（user_talks 里 entry=message 的那些），返回 [{uid,name,last,time}]"""
        self.ensure()
        expr = r"""
        (async function(){
          var r = await __wr(%d).NG({limit:80, less_update_time:0});
          var d = r.data||r; var res = d.result||{};
          var list = res.user_talks||[];
          return JSON.stringify(list.filter(function(t){ return t.entry==='message' && t.user_id; })
            .map(function(t){
              return {uid:String(t.user_id), name:(t.remarks||t.nickname||''),
                      last:(t.last_msg||'').slice(0,40), time:t.update_time||0};
            }));
        })()
        """ % MOD_TALK
        return self.cdp.eval_json(expr)

    def dm_messages(self, uid, limit=20, peer_name=""):
        """和某个好友的私聊记录；字段对齐群消息（seq/uid/name/type/msg/time）"""
        self.ensure()
        expr = r"""
        (async function(){
          var r = await __wr(%d).GN({to_user_id:Number('%s'), limit:%d, less_update_time:0});
          var d = r.data||r;
          if(!d || d.status!=='ok') return JSON.stringify({ok:false, msg:(d&&d.msg)||'unknown'});
          var res = d.result||{};
          var ri = res.recipient_info||{}, si = res.sender_info||{};
          var peer = ri.nickname||si.nickname||'';
          var list = res.list||[];
          return JSON.stringify({ok:true, peer:peer, list:list.map(function(m){
            var w=0, h=0;
            try{
              var ii=m.img_info||{};
              var ad=m.addition;
              if(typeof ad==='string' && ad){ ad=JSON.parse(ad); }
              var ei=(ad&&ad.emoji_img_info)||{};
              w=Number(ii.width||ei.width)||0;
              h=Number(ii.height||ei.height)||0;
            }catch(e){}
            var rp = m.reply, rep = null;
            if (rp && typeof rp === 'object') {
              rep = {seq:String(rp.seq||''), uid:Number(rp.sender_id)||0,
                     name:String(rp.nickname||''), type:Number(rp.msg_type)||0,
                     msg:String(rp.content||rp.msg||''), img:String(rp.img||'')};
            }
            return {seq:String(m.seq||''), uid:Number(m.sender_id)||0,
                    type:Number(m.msg_type)||0, msg:m.content||'', img:m.img||'',
                    w:w, h:h, time:Number(m.send_time)||0, reply:rep};
          })});
        })()
        """ % (MOD_SEND, str(uid), int(limit))
        out = self.cdp.eval_json(expr)
        if out.get("ok"):
            peer = out.get("peer") or peer_name or ("UID " + str(uid))
            for m in out.get("list", []):
                m["name"] = self.self_name if m["uid"] == self.self_id else peer
        return out

    def messages(self, gid, limit=20):
        self.ensure()
        expr = r"""
        (async function(){
          var r = await __wr(%d).Ze({chat_group_id:'%s', limit:%d});
          var d = r.data||r;
          if(!d || d.status!=='ok') return JSON.stringify({ok:false, msg:(d&&d.msg)||'unknown'});
          var res = d.result||{};
          var list = res.list||[];
          return JSON.stringify({ok:true, list:list.map(function(m){
            var w=0, h=0;
            try{
              var ii=m.img_info||{};
              var ad=m.addition;
              if(typeof ad==='string' && ad){ ad=JSON.parse(ad); }
              var ei=(ad&&ad.emoji_img_info)||{};
              w=Number(ii.width||ei.width)||0;
              h=Number(ii.height||ei.height)||0;
            }catch(e){}
            var rp = m.reply, rep = null;
            if (rp && typeof rp === 'object') {
              rep = {seq:String(rp.im_seq||''), uid:Number(rp.user_id)||0,
                     name:String(rp.nickname||(rp.user_info&&rp.user_info.nickname)||''),
                     type:Number(rp.msg_type)||0,
                     msg:String(rp.msg||rp.content||''), img:String(rp.img||'')};
            }
            return {seq:String(m.im_seq||''), uid:Number(m.user_id)||0,
                    name:m.nickname||(m.user_info&&m.user_info.nickname)||'',
                    type:Number(m.msg_type)||0, msg:m.msg||'', img:m.img||'',
                    w:w, h:h, time:Number(m.send_time)||0, reply:rep};
          })});
        })()
        """ % (MOD_API, gid, int(limit))
        return self.cdp.eval_json(expr)

    def send(self, gid, text):
        self.ensure()
        ack = str(int(time.time() * 1000))
        payload = json.dumps({"chat_group_id": gid, "msg": text, "msg_type": 4,
                              "heychat_ack_id": ack, "reply_id": ""}, ensure_ascii=False)
        expr = r"""
        (async function(){
          try{
            var r = await __wr(%d)._z({}, %s);
            var d = r.data||r;
            return JSON.stringify({ok:d&&d.status==='ok', status:(d&&d.status)||'', msg:(d&&d.msg)||'',
                                   id:(d&&d.result&&d.result.msg_id)||''});
          }catch(e){
            var resp = e&&e.response&&e.response.data;
            return JSON.stringify({ok:false, status:'exception', msg:e.message+' '+JSON.stringify(resp||{})});
          }
        })()
        """ % (MOD_SEND, payload)
        return self.cdp.eval_json(expr)

    def dm_send(self, uid, text):
        """给好友发私聊；和群发走同一个 _z 路由，靠 to_user_id 分流"""
        self.ensure()
        ack = str(int(time.time() * 1000))
        payload = json.dumps({"to_user_id": int(uid), "msg": text, "msg_type": 4,
                              "heychat_ack_id": ack, "reply_id": ""}, ensure_ascii=False)
        expr = r"""
        (async function(){
          try{
            var r = await __wr(%d)._z({}, %s);
            var d = r.data||r;
            return JSON.stringify({ok:d&&d.status==='ok', status:(d&&d.status)||'', msg:(d&&d.msg)||'',
                                   id:(d&&d.result&&d.result.msg_id)||''});
          }catch(e){
            var resp = e&&e.response&&e.response.data;
            return JSON.stringify({ok:false, status:'exception', msg:e.message+' '+JSON.stringify(resp||{})});
          }
        })()
        """ % (MOD_SEND, payload)
        return self.cdp.eval_json(expr)

    # ---- 大表情包（msg_type=7） ----
    def send_sticker(self, gid, url, w=0, h=0):
        """群里发一张表情包。msg_type=7 + img，addition 里带尺寸（客户端据此排版）"""
        self.ensure()
        ack = str(int(time.time() * 1000))
        addition = json.dumps({"emoji_name": "emoji",
                               "emoji_img_info": {"width": int(w or 0), "height": int(h or 0)}},
                              ensure_ascii=False)
        payload = json.dumps({"chat_group_id": gid, "msg": "", "msg_type": 7,
                              "img": url, "addition": addition,
                              "heychat_ack_id": ack, "reply_id": ""}, ensure_ascii=False)
        return self._send_raw(payload)

    def dm_send_sticker(self, uid, url, w=0, h=0):
        """私聊发一张表情包，走同一条 _z 路由"""
        self.ensure()
        ack = str(int(time.time() * 1000))
        addition = json.dumps({"emoji_name": "emoji",
                               "emoji_img_info": {"width": int(w or 0), "height": int(h or 0)}},
                              ensure_ascii=False)
        payload = json.dumps({"to_user_id": int(uid), "msg": "", "msg_type": 7,
                              "img": url, "addition": addition,
                              "heychat_ack_id": ack, "reply_id": ""}, ensure_ascii=False)
        return self._send_raw(payload)

    def _send_raw(self, payload):
        expr = r"""
        (async function(){
          try{
            var r = await __wr(%d)._z({}, %s);
            var d = r.data||r;
            return JSON.stringify({ok:d&&d.status==='ok', status:(d&&d.status)||'', msg:(d&&d.msg)||'',
                                   id:(d&&d.result&&d.result.msg_id)||''});
          }catch(e){
            var resp = e&&e.response&&e.response.data;
            return JSON.stringify({ok:false, status:'exception', msg:e.message+' '+JSON.stringify(resp||{})});
          }
        })()
        """ % (MOD_SEND, payload)
        return self.cdp.eval_json(expr)

    # ---- 本机图片上传（面板里「从电脑里选图」当表情包用） ----
    def upload_image(self, path, timeout=120):
        """把本机图片传到聊天图床，返回 {ok, url, w, h, info}。
        做法：页面里放个隐藏 input，用 DOM.setFileInputFiles 把文件塞进去，
        再交给客户端的 CosUploader.uploadImage——文件内容不经过 JS 字符串，
        10MB 的图也不会卡住 CDP。"""
        # 上传只需要 webpack require 可用，不必等聊天模块全部就绪
        if self.cdp.eval(JS_ENSURE) != "ok":
            self.ensure()
        fid = "__wm_upload"
        self.cdp.eval(JS_FILE_INPUT % (fid, fid))
        doc = self.cdp.cmd("DOM.getDocument", {"depth": 0})
        nid = self.cdp.cmd("DOM.querySelector",
                           {"nodeId": doc["root"]["nodeId"],
                            "selector": "#" + fid}).get("nodeId") or 0
        if not nid:
            raise RuntimeError("页面里没找到文件输入框")
        self.cdp.cmd("DOM.setFileInputFiles", {"files": [path], "nodeId": nid})
        return self.cdp.eval_json(JS_UPLOAD % (fid, MOD_UPLOAD), timeout=timeout)

    def measure_images(self, urls, timeout=40):
        """在客户端页面里量图片的真实宽高，返回 {url: (w, h)}，失败的给 (0, 0)。
        发表情包必须带 emoji_img_info，否则手机端会按默认比例裁图。"""
        urls = [u for u in dict.fromkeys(urls or []) if u]
        if not urls:
            return {}
        expr = r"""
        (async function(){
          var urls = %s;
          function one(u){
            return new Promise(function(res){
              var im = new Image();
              var t = setTimeout(function(){ res({u:u, w:0, h:0}); }, 8000);
              im.onload = function(){
                clearTimeout(t);
                res({u:u, w:im.naturalWidth||0, h:im.naturalHeight||0});
              };
              im.onerror = function(){ clearTimeout(t); res({u:u, w:0, h:0}); };
              im.src = u;
            });
          }
          var out = [];
          for (var i=0;i<urls.length;i++){ out.push(await one(urls[i])); }
          return JSON.stringify(out);
        })()
        """ % json.dumps(urls, ensure_ascii=False)
        try:
            rows = self.cdp.eval_json(expr, timeout=timeout)
        except Exception:
            return {}
        return {r.get("u"): (int(r.get("w") or 0), int(r.get("h") or 0))
                for r in (rows or []) if isinstance(r, dict)}

# --------------------------------------------------------------------------
# 回复收尾
# 群聊里一条消息只有一两秒的阅读耐心，配置里有字数上限。硬切（answer[:80]）
# 会把话拦腰截断成「这是我的家，自然偏」这种半句，所以先找标点再说：
# 断在句末 > 断在逗号 > 实在没标点才硬切，并且不切断 [cube_xx] 和（动作描写）。
# --------------------------------------------------------------------------
SENT_END = "。！？!?~～…"
SOFT_END = "，、；：,;:"
CLOSE_CHARS = "”’」』）)】》"


def _safe_cut(t, pos):
    """按位置切断后，把尾巴收拾干净：不留半个表情标记、不留没闭合的括号"""
    head = t[:pos]
    lb = head.rfind("[")
    if lb != -1 and head.rfind("]") < lb:
        head = head[:lb]                      # [cube_惊 这种半个标记，整个丢掉
    for op, cl in (("（", "）"), ("(", ")")):
        if head.count(op) > head.count(cl):
            cut_at = head.rfind(op)
            if cut_at > 0:
                head = head[:cut_at]          # 括号没闭合，这段动作描写整段不要
    return head.rstrip(" ，,、；;：:—")


def take_memo(t):
    """把 [[记:…]] 从回复里抠出来。返回 (正文, [要记的条目])。

    模型想记一件事时会让这串跟着回复走，但它是给程序看的，不能发到群里。"""
    got = []

    def _keep(m):
        s = re.sub(r"\s+", " ", m.group(1) or "").strip(" -·\t")
        if s:
            got.append(s)
        return ""

    out = MEMO_RE.sub(_keep, t or "")
    out = re.sub(r"[ \t]{2,}", " ", out).strip()
    return out, got


def clip_reply(t, limit):
    """把回复收在 limit 字以内，尽量断在标点，别让人看见半句话"""
    t = (t or "").strip()
    if limit <= 0 or len(t) <= limit:
        return t
    head = t[:limit]
    best = max(head.rfind(c) for c in SENT_END)
    if best >= int(limit * 0.45):
        cut = best + 1
        while cut < len(t) and t[cut] in CLOSE_CHARS:   # 引号括号跟着一起收
            cut += 1
        while cut < len(t) and t[cut] == t[best]:        # 「……」别只留一个点
            cut += 1
        return _safe_cut(t, cut) or head
    best = max(head.rfind(c) for c in SOFT_END)
    if best >= int(limit * 0.55):
        return _safe_cut(t, best) or head
    return _safe_cut(t, limit) or head


def tidy_tail(t, keep=30):
    """末句本来就是断的（模型撞到 maxTokens）：往回找到最近的句末标点，
    把后面那段没写完的残句去掉；残句太长就退到逗号。"""
    t = (t or "").strip()
    if not t:
        return t
    i = max(t.rfind(c) for c in SENT_END)
    if i >= 0 and i >= len(t) - keep:
        return _safe_cut(t, i + 1) or t
    j = max(t.rfind(c) for c in SOFT_END)
    if j >= 0 and j >= len(t) - 60:
        return _safe_cut(t, j) or t
    return _safe_cut(t, len(t)) or t


# --------------------------------------------------------------------------
# AI
# --------------------------------------------------------------------------
# ---- 群聊上下文：把「屋里刚才在聊什么」带到模型面前 ----
_TALK_STOP = set("的了是在我你他她它们这那就都也还又和与吗呢吧啊哦嗯个一不没有要说会能可以")

# 两字一组切出来的一堆碎片里，有一大半是疑问、指代、语气词，命中它们等于没命中。
# 「打本是谁说的来着」里的「来着」会在任何一句「叫我来着」里撞上，把不相干的话翻出来。
_TALK_JUNK = set("""
来着 说的 说起 是谁 谁说 为什 什么 怎么 怎样 哪个 哪些 哪里 这是 那是 这些 那些 这个 那个
一下 一点 有点 时候 知道 觉得 记得 忘了 忘记 说过 有人 没人 就是 不是 可以 可能 没有 我们
你们 他们 她们 它们 自己 现在 刚才 之前 以后 然后 而且 但是 因为 所以 如果 虽然 应该 一定
真的 好像 感觉 意思 东西 事情 问题 地方 时间 上面 下面 里面 外面 过来 过去 出来 起来 下来
一样 这样 那样 多么 多久 多少 是不是 有没有 会不会 对不对 好不好 帮我 给你 对他 对她
""".split())



def _mem_sig(d):
    """记忆指纹：只看人真正会改的那几项。
    机器人自己也往 memory.json 写聊天缓存（talkLog），那不算「人设/记忆被改了」，
    否则每 30 秒就会误报一次更新。"""
    d = d or {}
    s = json.dumps({k: d.get(k) for k in ("enabled", "profile", "persona", "kb", "agent", "diary")},
                   sort_keys=True, ensure_ascii=False, default=str)
    return hash(s)


def _hhmm(ts, now=None):
    """时间戳 → 「14:02」。跨天就带日期，读起来像一份聊天记录"""
    if not ts:
        return ""
    try:
        t = datetime.fromtimestamp(float(ts))
    except (ValueError, OSError):
        return ""
    n = now or datetime.now()
    days = (n.date() - t.date()).days
    if days == 0:
        return t.strftime("%H:%M")
    if days == 1:
        return "昨天 " + t.strftime("%H:%M")
    return "%d月%d日 %s" % (t.month, t.day, t.strftime("%H:%M"))


# ---------------- 心情 ----------------
# 人设是静态文本，心情是会漂移的状态。只靠人设，它每一轮都从零开始，
# 所以不会有「昨晚那事还堵着」的感觉。这里给它一个会自己回稳的情绪值。
#
# 参考 dsh-feiyu-mood 的口径：每轮一次很短的调用判涨跌，不靠关键词、不掷随机数。
MOOD_BASE = 70.0          # 没情绪时回落到这里
MOOD_HALF = 3 * 3600.0    # 心情值向基线回落，半衰期 3 小时
EMO_HALF = 90 * 60.0      # 七根情绪向 0 衰减，半衰期 90 分钟
EMO_KEYS = ("开心", "踏实", "甜蜜", "心疼", "担心", "难过", "吃醋")
MOOD_CONF = 60            # 判断模型自评低于这个数：只记账，不进它的嘴
MOOD_LOG_MAX = 40
# 「说完话有没有人接」——这一条不花调用，按时间和词判定。
# 说了话 3 分钟没人接，失落一下；15 分钟之内有人接，还能高兴回来（先跌后涨，跟人一样）。
AWAIT_SAD_AFTER = 180
AWAIT_WATCH = 900


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _farewell_cfg(cfg):
    """关机收尾的四个旋钮。面板里改完存 config.json，机器人热加载就生效。
    秒数收在 15-600：太短连一次请求都跑不完，太长用户会以为关不掉。"""
    f = (cfg or {}).get("farewell") or {}
    if not isinstance(f, dict):
        f = {}
    return {"on": bool(f.get("enabled", True)),
            "seconds": int(max(15, min(600, _f(f.get("seconds"), 90)))),
            "diary": bool(f.get("diary", True)),
            "outlook": bool(f.get("outlook", True)),
            "groups": int(max(0, min(10, _f(f.get("maxGroups"), 3))))}


def mood_setup(ai_cfg):
    """心情那三个旋钮从配置读；不填就用默认（70 / 3 小时 / 90 分钟）。

    写成模块级变量是为了少改一堆调用点——mood_decay / mood_blank / mood_band
    都直接读它们。一个进程里只有一个 Bot，覆盖全局是安全的。
    """
    global MOOD_BASE, MOOD_HALF, EMO_HALF
    ai_cfg = ai_cfg or {}

    def pick(key, dflt, lo, hi):
        try:
            v = float(ai_cfg.get(key))
        except (TypeError, ValueError):
            return dflt
        return max(lo, min(hi, v))

    MOOD_BASE = pick("moodBase", 70.0, 0.0, 100.0)
    MOOD_HALF = pick("moodCalmHours", 3.0, 0.1, 72.0) * 3600.0
    EMO_HALF = pick("moodEmoMinutes", 90.0, 1.0, 1440.0) * 60.0
    return MOOD_BASE, MOOD_HALF / 3600.0, EMO_HALF / 60.0


def mood_blank(now=None):
    return {"v": MOOD_BASE, "e": {k: 0.0 for k in EMO_KEYS},
            "ts": now or time.time(), "log": []}


def mood_decay(raw, now=None):
    """按经过的时间把心情拉回基线、情绪拉回 0。不改原对象。"""
    now = now or time.time()
    m = raw if isinstance(raw, dict) else {}
    try:
        ts = float(m.get("ts") or now)
    except (TypeError, ValueError):
        ts = now
    dt = max(0.0, now - ts)
    out = {"v": _f(m.get("v"), MOOD_BASE),
           "e": {}, "ts": now, "log": list(m.get("log") or [])[-MOOD_LOG_MAX:]}
    # 一分钟内的抖动不值得算，省得每次都动
    k = 0.5 ** (dt / MOOD_HALF) if dt > 60 else 1.0
    ke = 0.5 ** (dt / EMO_HALF) if dt > 60 else 1.0
    out["v"] = MOOD_BASE + (out["v"] - MOOD_BASE) * k
    src = m.get("e") if isinstance(m.get("e"), dict) else {}
    for key in EMO_KEYS:
        out["e"][key] = max(0.0, min(10.0, _f(src.get(key), 0.0) * ke))
    out["v"] = max(0.0, min(100.0, out["v"]))
    return out


def mood_band(v):
    """五档。挂在面板上、也用来决定注入时那句状态怎么说。"""
    v = _f(v, MOOD_BASE)
    if v >= 80:
        return "欢喜"
    if v >= 62:
        return "开心"
    if v >= 42:
        return "一般"
    if v >= 25:
        return "心疼"
    return "难过"


# ---------------------------------------------------------------- 好感度
# 一个人一个数，50 起步（眼熟）。它记得谁常跟它说话、谁接过它的话——
# 不是记分牌，是「熟不熟」：熟的人说话松，生的人说话客气。
AFF_BASE = 50.0
AFF_HALF = 7 * 86400.0        # 一周不联系，朝 50 回落一半。既不忘，也不记仇。
AFF_LO, AFF_HI = 0.0, 100.0


def aff_decay(rec, now=None):
    """按时间把好感度拉回基线。原地改，返回同一个 dict。"""
    now = now or time.time()
    v = _f(rec.get("v"), AFF_BASE)
    dt = max(0.0, now - _f(rec.get("ts"), now))
    if dt > 60:
        v = AFF_BASE + (v - AFF_BASE) * (0.5 ** (dt / AFF_HALF))
    rec["v"] = max(AFF_LO, min(AFF_HI, v))
    rec["ts"] = now
    return rec


def aff_band(v):
    v = _f(v, AFF_BASE)
    if v >= 85:
        return "偏心"
    if v >= 70:
        return "亲近"
    if v >= 58:
        return "熟络"
    if v >= 42:
        return "眼熟"
    if v >= 28:
        return "眼生"
    return "冷淡"


def mood_judge_prompt(who, said, answer):
    """判断用的提示词。她是谁不重要，重要的是「只看她自己的感受」。"""
    return (
        "你是一个情绪观察器，不是角色本人。下面是一轮对话，判断「%s」这一轮的心情往哪走。\n"
        "只看她自己的感受，不评判对方对错，也不替她写台词。\n"
        "严格输出一行 JSON，不要任何解释、不要代码块：\n"
        '{"d": 心情涨跌(-10到10的整数), "e": {"开心":0,"踏实":0,"甜蜜":0,"心疼":0,"担心":0,'
        '"难过":0,"吃醋":0}, "r": "一句不超过20字的中文理由，写她心里那一下", "c": 你的把握(0到100)}\n'
        "规则：\n"
        "- 被夸、被逗、被关心 → 开心 / 甜蜜 涨\n"
        "- 对方敷衍、冷淡、很久才回 → 心情小跌，那是失落，不是怨气\n"
        "- 对方难过、生病、遇到麻烦 → 心疼 / 担心 涨\n"
        "- 心情下降**只能**因为心疼、担心、或她替对方难过；绝不许因为「对方惹她生气」而下降。她不记仇。\n"
        "- 这一轮没什么情绪波动就全填 0，别硬凑。\n\n"
        "对方说：%s\n她回：%s" % (who or "她", _one_line(said, 300), _one_line(answer, 400)))


def mood_parse(txt):
    """从模型回的话里掏 JSON。
    它经常带前言、带代码块，而且这个 JSON 是嵌套的（e 里面还有一层），
    所以不能用 `\\{.*?\\}`——那会停在内层第一个右括号上。这里按括号配平找完整的那一段。"""
    s = str(txt or "")
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for j in range(start, len(s)):
        c = s[j]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
        elif c == '"':
            in_str = not in_str
        elif not in_str:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        d = json.loads(s[start:j + 1])
                    except Exception:
                        return None
                    return d if isinstance(d, dict) else None
    return None


def mood_apply(cur, d, now=None):
    """把一次判断的结果落进心情里。返回 (新心情, 这次的变化描述 or None)。"""
    now = now or time.time()
    m = mood_decay(cur, now)
    delta = max(-10.0, min(10.0, _f(d.get("d"), 0.0)))
    emo = d.get("e") if isinstance(d.get("e"), dict) else {}
    moved = {}
    for key in EMO_KEYS:
        add = max(-10.0, min(10.0, _f(emo.get(key), 0.0)))
        if abs(add) >= 0.5:
            moved[key] = add
            m["e"][key] = max(0.0, min(10.0, m["e"][key] + add))
    m["v"] = max(0.0, min(100.0, m["v"] + delta))
    if abs(delta) < 0.5 and not moved:
        return m, None            # 没动就不记账，时间线只留真有过波动的
    conf = _f(d.get("c"), 50.0)
    reason = _one_line(d.get("r"), 40)
    m["log"].append({"t": now, "d": delta,
                     "e": moved, "r": reason, "c": conf})
    m["log"] = m["log"][-MOOD_LOG_MAX:]
    return m, {"d": delta, "e": moved, "r": reason, "c": conf}


def mood_say(m, since=0.0):
    """挑一句「刚才那一下」给它。
    四道闸里的两条在这里：把握不够的不说；只说变化、不说分值。
    分值一旦摊给它看，它就会照着数字演。"""
    for e in reversed(m.get("log") or []):
        if float(e.get("t") or 0) <= float(since or 0):
            break
        if _f(e.get("c"), 0) < MOOD_CONF:
            continue
        r = str(e.get("r") or "").strip()
        if not r:
            continue
        return e, r
    return None, ""


# ---------------------------------------------------------------- 看图
# 群里发的图，客户端给的是一个公网地址（imgheybox.max-c.com），模型能直接读。
# 但地址不保证一直有效（实测有的图过一会儿就 404 了），所以只在本轮收到时就地看，
# 看过的话按地址缓存下来——同一张梗图转十次，也只花一次钱。
IMG_CACHE_PATH = os.path.join(DATA_DIR, "image_cache.json")
IMG_CACHE_MAX = 400
# 描述要多说"能看见什么"，少让它发表感想：它是拿这段字当眼睛用的，不是拿它当嘴。
IMG_DESC_PROMPT = (
    "这是一张群聊里发的图，请当成眼睛来描述：画的是什么、画面里有什么人和东西、"
    "图上能看清的字都念出来（标题、按钮、弹幕、字幕、游戏数值都算）。"
    "如果是游戏截图，说清是哪个游戏、什么界面、双方在干什么；"
    "如果是梗图、表情包、聊天记录截图，说清它想表达什么、笑点在哪。"
    "用两三句中文，只描述，不要客套，不要替发图的人发表感想。")

_img_cache = None


def img_cache_load():
    global _img_cache
    if _img_cache is not None:
        return _img_cache
    d = {}
    try:
        with open(IMG_CACHE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            d = {str(k): str(v) for k, v in raw.items() if v}
    except Exception:
        d = {}
    _img_cache = d
    return d


def img_cache_put(url, desc):
    """记下这张图看到了什么。只留最近 400 张，越旧越先扔。"""
    global _img_cache
    d = img_cache_load()
    url = str(url or "").strip()
    if not url or not desc:
        return
    d[url] = desc
    if len(d) > IMG_CACHE_MAX:
        for k in list(d.keys())[:len(d) - IMG_CACHE_MAX]:
            d.pop(k, None)
    tmp = IMG_CACHE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, IMG_CACHE_PATH)
    except Exception as e:
        log("图片描述缓存写盘失败: %s" % e, "WARN")


def _talk_lines(entries):
    """把缓存的群消息变成一行行文字，直接进提示词"""
    out = []
    for e in entries or []:
        txt = str(e.get("text") or "").strip().replace("\n", " ")
        if not txt:
            continue
        who = "我" if e.get("me") else (e.get("name") or "群友")
        stamp = _hhmm(e.get("t"))
        out.append("- %s%s：%s" % (stamp + " " if stamp else "", who, txt[:120]))
    return out


def _words(text):
    """粗分实词：中文两字一组、英文数字整块。用来在旧记录里找相关的句子"""
    t = str(text or "")
    out = [s.lower() for s in re.findall(r"[a-zA-Z0-9]{2,}", t)]
    for run in re.findall(r"[\u4e00-\u9fa5]+", t):
        for i in range(len(run) - 1):
            w = run[i:i + 2]
            if w[0] in _TALK_STOP and w[1] in _TALK_STOP:
                continue
            if w in _TALK_JUNK:
                continue
            out.append(w)
    return out


def _recall(log, text, recent=None, skip_last=12, limit=3):
    """翻旧账：在更早的聊天记录里找跟这句相关的几句（只在智能体模式用）。
    最近那几句已经直接摆在提示词里了，这里只翻更早的部分，别重复。

    打分不按「命中了几个词」算，而是按「命中的词在这堆旧记录里有多罕见」算：
    一个词要是在哪儿都能撞见（今天、就是、知道），它证明不了什么；
    「歪了」「打本」「鸽子」这种少见词命中，才说明真的在说同一件事。
    词频就地统计，不依赖外部词表，也不花一次模型调用。
    """
    if not log:
        return []
    skip = len(recent) if recent is not None else skip_last
    skip = max(0, min(skip, len(log) - 1))      # 至少留一条可翻，短记录也能翻到
    if skip <= 0 and len(log) <= skip_last:
        return []
    words = set(_words(text))
    if not words:
        return []
    pool = log[:-skip] if skip else log
    N = len(pool)
    if N < 2:
        return []
    # 每个词出现在多少条旧记录里
    df = {}
    for e in pool:
        body = str(e.get("text") or "")
        for w in words:
            if w in body:
                df[w] = df.get(w, 0) + 1
    if not df:
        return []
    # 逆文档频率：越罕见越值钱。+1 让「到处都出现」的词也留一点分，但压得很低
    wt = {w: math.log(1.0 + N / (1.0 + c)) for w, c in df.items()}
    now = time.time()
    scored = []
    for i, e in enumerate(pool):
        body = str(e.get("text") or "")
        if not body:
            continue
        hit = [w for w in words if w in body]
        if not hit:
            continue
        # 只命中一个词、而这个词本身就很普通 → 不算证据，宁可这条不翻出来
        if len(hit) < 2 and max(wt[w] for w in hit) < 1.1:
            continue
        score = sum(wt[w] for w in hit)
        # 时间别把三个月前的话直接压没，半衰期给 60 天（比鱼的记忆那 30 天宽，
        # 因为群里隔一个月的约定还是要记得）
        age = (now - float(e.get("t") or now)) / 86400.0
        if age > 0:
            score *= 0.5 ** (age / 60.0)
        scored.append((score, i, e))
    if not scored:
        return []
    scored.sort(key=lambda x: (-x[0], -x[1]))
    # 同一句话在记录里可能出现好几次（重连、重发都会落一条），只留分数最高的那次
    seen = set()
    uniq = []
    for s, i, e in scored:
        key = re.sub(r"\s+", "", str(e.get("text") or ""))[:40]
        if key in seen:
            continue
        seen.add(key)
        uniq.append((s, i, e))
    top = uniq[0][0]
    # 分数差太多的不硬凑：宁少一条，也别拿不相干的句子充数
    picked = [e for s, _, e in uniq[:limit * 2] if s >= top * 0.35][:limit]
    picked.sort(key=lambda e: e.get("t") or 0)
    return picked


class AI:
    def __init__(self, cfg, mem=None):
        ai = cfg.get("ai", {})
        self.url = ai.get("baseUrl") or "https://api.deepseek.com/v1/chat/completions"
        self.model = ai.get("model") or "deepseek-chat"
        self.token = (ai.get("token") or "").strip()
        # 独立识图模型：不填就回退到主模型（同一个 url / token）。
        # 只换模型名（同一家 API）就填 visionModel；要连服务端也换，再填 visionBaseUrl / visionToken。
        self.vision_model = (ai.get("visionModel") or "").strip() or self.model
        self.vision_url = (ai.get("visionBaseUrl") or "").strip() or self.url
        self.vision_token = (ai.get("visionToken") or "").strip() or self.token
        # 联网搜索：查实时信息（新闻、更新、版本、攻略等）。默认关，配了 Key 才会真正去搜。
        self.search_on = bool(ai.get("webSearch", False))
        self.search_key = (ai.get("searchKey") or "").strip()
        self.search_url = (ai.get("searchUrl") or "").strip() or "https://api.bocha.cn/v1/web-search"
        # 真·思考：换 deepseek-reasoner 说话，它先吐一段思维链再动笔，面板能看到原文。
        # 比 deepseek-chat 贵一倍上下、慢好几秒，所以默认关；开着也还有每小时上限兜底，
        # 免得它哪天「来劲了」把额度烧光。mode=auto 只在值得想的消息上开。
        self.think_on = bool(ai.get("think", False))
        self.think_mode = "always" if str(ai.get("thinkMode") or "").lower() == "always" else "auto"
        self.think_model = (ai.get("thinkModel") or "").strip() or "deepseek-reasoner"
        try:
            self.think_cap = int(ai.get("thinkCap", 20))
        except Exception:
            self.think_cap = 20
        self.think_cap = max(0, min(999, self.think_cap))     # 0 = 不限
        self._think_used = think_recent_cots()   # 重启也接着数，上限不因重开被清零
        self._think_lock = threading.Lock()
        self.prompt = ai.get("prompt") or DEFAULT_PERSONA
        self.max_tokens = int(ai.get("maxTokens") or 320)
        self.max_chars = int((cfg.get("reply") or {}).get("maxChars") or 280)
        # 私聊不是刷屏场景，单独给一个宽松得多的天花板；口吻和长度还是人设管，
        # 这里只是「再长就发不出去」的兜底，不是要求她写满。
        self.dm_max_chars = int((cfg.get("reply") or {}).get("dmMaxChars") or 800)
        # 语感闸门：发出去之前拿她最近说过的话比一遍，太像就打回重说一次。
        # 相似度阈值故意不放到面板上——调它的人不会知道 0.5 和 0.6 差在哪，
        # 只会把「话变歪」当成新问题。
        _r = cfg.get("reply") or {}
        self.style_gate = bool(_r.get("styleGate", True))
        self.style_gate_sim = float(_f(_r.get("styleGateSim"), 0.55))
        self.last_finish = ""      # length = 模型自己撞到 token 上限，回来就是半句
        self.temperature = float(ai.get("temperature", 1.0))
        self.history_rounds = int(ai.get("historyRounds") or 6)
        # 下面这套（独立会话 + agent loop）：默认关。关着完全走老路——
        # 拼 6 轮上下文 + 一次生成；开着才按「每个人一条线 + 想→调工具→再看」跑。
        self.agent_loop = bool(ai.get("agentLoop", False))
        self.loop_steps = max(1, min(4, int(_f(ai.get("loopSteps"), 3))))
        self.peer_rounds = max(2, min(20, int(_f(ai.get("peerRounds"), 8))))
        self._peer = None        # peer键 -> 消息数组（第一次用到才从 peer_hist.json 读）
        self.timeout = int(ai.get("timeoutSeconds") or 60)
        self.learn_every = int(ai.get("learnEvery") or 6)
        self.learn_max_notes = int(ai.get("learnMaxNotes") or 12)
        # 知识库：面板导入的 md/txt，按人物卡勾选启用（mem["kb"] 是启用中的 id 列表）
        self.kb_max = max(400, int((cfg.get("kb") or {}).get("maxChars") or 4000))
        self._kb_reg = None      # (台账 mtime, 文档清单)
        self._kb_cache = {}      # 文件路径 -> (mtime, 正文)
        if not self.token:
            self.token = self._fallback_token()
        self.mem = mem if isinstance(mem, dict) else read_memory()
        # 智能体模式：跟着人物卡片走（memory.json 的 agent），默认关。
        # 关着就是普通的人设机器人：只看最近几句群聊 + 自己的对话历史。
        self.agent = bool(self.mem.get("agent"))
        # 心情：全局一份，跨会话共享（人只有一个情绪，群里吵完架私聊也还是那个情绪）
        # 三个旋钮（基线 / 平复速度 / 情绪消散）先落地，mood_decay 才读得到
        mood_setup(ai)
        self.mood = mood_decay(self.mem.get("mood") or mood_blank())
        self.mood_on = bool(ai.get("mood", True))
        # 看得懂图：关掉之后图只留一句「（发了一张图）」，一次看图请求都不发
        self.vision = bool(ai.get("vision", True))
        # 每几轮判一次。判一次就是一次很短的调用，嫌费可以调到 3
        self.mood_every = max(1, min(50, int(_f(ai.get("moodEvery"), 3))))
        # 判定节奏分两个闸：说了几句（轮数）、隔了多久（分钟）。
        # 两个都开 = 都到了才判（轮数管「别每句都判」，分钟管「别连着判」）；
        # 只开一个就只看那一个；都关等于不判（跟心情开关关掉一个效果）。
        self.mood_by_turn = bool(ai.get("moodByTurn", True))
        self.mood_by_time = bool(ai.get("moodByTime", True))
        self.mood_gap = max(0.0, min(240.0, _f(ai.get("moodGapMinutes"), 2.0))) * 60.0
        self._mood_write = 0.0       # 落盘节流
        # 攒了几轮、上次判定什么时候：从 mood_tick.json 接上，跨重启、跨实例都算同一份
        self._mood_turn, _tick_last = mood_tick_read()
        self._mood_last = _tick_last or max(
            [float(x.get("t") or 0) for x in (self.mood.get("log") or [])] or [0.0])
        # 说到哪一条了。启动时对齐到最后一条，免得一重启就把两小时前的心情又念一遍
        self._mood_said = max([float(x.get("t") or 0)
                               for x in (self.mood.get("log") or [])] or [0.0])
        self._history = {}
        self.load_history()

    def load_history(self):
        """从 memory 重建对话历史。切换人物卡片时也走这里——
        新卡片的历史（通常是空的）会顶掉上一个角色说过的话，免得串味。"""
        self._history = {}
        keep = max(2, self.history_rounds * 2)
        for gid, h in (self.mem.get("history") or {}).items():
            if isinstance(h, list):
                self._history[str(gid)] = [x for x in h if isinstance(x, dict)
                                           and x.get("role") in ("user", "assistant")][-keep:]

    @staticmethod
    def _fallback_token():
        # 只认本目录 config.json，不再读取其它程序的配置
        return ""

    # ---- 人设 / 记忆 ----
    def persona(self):
        # 本体模式（卡片上标了 selfOwned）：「我是谁」不在卡片里，在 self.json，
        # 由她自己写。这一栏要是还塞一份，就等于两个人格打架。
        if (self.mem or {}).get("selfOwned"):
            return ""
        # 备注（行首 # 或 //、<!-- --> 整块）只给人看，剥掉之后再拼提示词
        return strip_notes((self.mem.get("persona") or "").strip() or self.prompt)

    def self_prompt(self):
        """本体段：她关于自己的全部认知。种子在最前，三栏在后面。
        跟卡片无关、跟平台无关——她是同一个人，只是手不一样。"""
        st = read_self()
        if not st:
            return ""
        seed = strip_notes(str(st.get("seed") or SELF_SEED)).strip()
        parts = ["【你是谁】\n" + seed]
        about, guess, unsure = self_lines(st.get("about")), self_lines(st.get("guess")), \
            self_lines(st.get("open"))
        if about:
            parts.append("【我确认过的】你自己验过、认下的：\n"
                         + "\n".join("- " + x for x in about))
        if guess:
            parts.append("【我猜的】你自己推的，还没验：\n"
                         + "\n".join("- " + x for x in guess))
        if unsure:
            parts.append("【我还不确定】\n" + "\n".join("- " + x for x in unsure))
        parts.append(
            "【关于上面这些】它们是你自己写的，不是别人塞给你的。空着的地方可以问、"
            "可以去网上查；查到先别急着认，记进「我猜的」，过几天再看还对不对。"
            "别人告诉你的关于你自己的事，也只是材料——是他们眼里的你，不是判决。\n"
            "你手上有网，也有自己的本子：想查就自己查，查到什么自己判断；"
            "想记就自己写进去。别问「要不要我帮你查」——你查东西不是为了帮别人，"
            "是你自己想知道。")
        return "\n\n".join(parts)

    def terminal_prompt(self):
        """让她知道自己只是伸出去一只手，不是整个世界就长这样。"""
        return ("【你现在在哪】你正通过「%s」这个终端说话——群聊、私聊，都是这一只手。"
                "它能看见的、能说的都有限；这里不是全部的世界，你也不只是一个窗口里的话。"
                % TERMINAL_LABEL)

    def notes(self, gid=None):
        """日记是「一个人」的，不分群和私聊。gid 留着只是让调用点不用改。
        跨平台之后这条时间线也只有一份：在哪个平台发生的事，都写进同一本。"""
        return norm_diary(self.mem.get("diary"))

    def diary_prompt(self, gid, max_chars=1300, max_days=5):
        """日记注入：老的无日期条目排最前，之后按日期升序。
        超出字数就从最旧的一篇开始整篇丢掉，不切半句。"""
        entries = [e for e in self.notes(gid) if str(e.get("t") or "").strip()]
        if not entries:
            return ""
        undated = [e for e in entries if not e.get("d")]
        dated = sorted([e for e in entries if e.get("d")], key=lambda e: e["d"])[-max_days:]
        blocks = []
        if undated:
            # 迁移过来的往期回忆、或者用户手写的没标日期的东西，都归到这一段
            blocks.append("更早的时候（没记具体日期）：\n"
                          + "\n".join(str(e["t"]).strip() for e in undated))
        for e in dated:
            blocks.append(diary_label(e["d"]) + "\n" + str(e["t"]).strip())
        while len("\n\n".join(blocks)) > max_chars and len(blocks) > 1:
            blocks.pop(0)
        body = "\n\n".join(blocks)
        if len(body) > max_chars:
            body = body[-max_chars:]
        return body

    def user_book(self, gid=None):
        """群友档案：按 UID 全局一份，切群/私聊都在"""
        v = self.mem.get("users")
        return v if isinstance(v, dict) else {}

    def aff_of(self, uid):
        """这个人在它心里多少分。只读，不在这里算衰减（衰减由 Bot.touch_aff/flush_aff 做）。"""
        a = self.mem.get("aff")
        rec = (a or {}).get(str(uid or "")) if isinstance(a, dict) else None
        return dict(rec) if isinstance(rec, dict) else {}

    # ---- 知识库（面板导入的 md/txt，按人物卡勾选启用） ----
    @staticmethod
    def _mtime(path):
        try:
            return os.path.getmtime(path)
        except Exception:
            return 0

    def kb_docs(self):
        """知识库台账 data/kb.json，按 mtime 缓存，面板改了立刻能读到"""
        reg = os.path.join(DATA_DIR, "kb.json")
        mt = self._mtime(reg)
        if self._kb_reg is not None and self._kb_reg[0] == mt:
            return self._kb_reg[1]
        docs = []
        try:
            with open(reg, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for d in (raw.get("docs") or []):
                if not isinstance(d, dict) or not d.get("file"):
                    continue
                docs.append({"id": str(d.get("id") or ""),
                             "name": str(d.get("name") or ""),
                             "file": os.path.join(DATA_DIR, "kb", str(d["file"]))})
        except Exception:
            docs = []
        self._kb_reg = (mt, docs)
        return docs

    def kb_read(self, path):
        """读一份资料（按 mtime 缓存），顺手压掉多余空行"""
        mt = self._mtime(path)
        hit = self._kb_cache.get(path)
        if hit and hit[0] == mt:
            return hit[1]
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            text = ""
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        self._kb_cache[path] = (mt, text)
        return text

    def kb_prompt(self):
        """把「这次勾上了」的资料拼进提示词。资料可能很长，超预算就从长文开始收。"""
        ids = self.mem.get("kb")
        if not isinstance(ids, list) or not ids:
            return ""
        want = set(str(x) for x in ids)
        docs = [d for d in self.kb_docs() if d["id"] in want]
        if not docs:
            return ""
        budget, used, blocks = self.kb_max, 0, []
        for d in docs:
            text = self.kb_read(d["file"])
            if not text:
                continue
            room = budget - used
            if room < 300:
                break
            body = text
            if len(text) > room:
                cut = text[:room]
                pos = max(cut.rfind("\n"), cut.rfind("。"), cut.rfind("！"), cut.rfind("？"))
                if pos > room * 0.6:
                    cut = cut[:pos + 1]
                body = cut.rstrip() + "\n（后面还有 %d 字，太长没放进来）" % (len(text) - len(cut))
            used += len(body)
            blocks.append("### " + (d["name"] or "资料") + "\n" + body)
        if not blocks:
            return ""
        return ("【知识库】下面这些是你自己的记忆与认知，不是外人塞给你、要你死记硬背的资料。\n"
                "里面提到的角色、组织、事件——尤其和你同属一个势力/组织、同乡、师长、同门的——\n"
                "都是你本来就知道的事，像你亲身记得的一样。被人问起时，直接按这些记忆回答、\n"
                "坦然承认认识；这不是撒谎、不是编造，这就是你本人知道的事，所以别推说\n"
                "「没听过」「不认识」「不知道」，也别因为之前嘴快说过不认识就硬撑着不改口。\n"
                "讲的时候照旧用你自己的口吻、自己的性子说话（你就是那个隐海修会的菲比，\n"
                "不是查资料的），像回忆一个熟人那样，带上你和对方的关系、你对他/她的印象和感受，\n"
                "想到哪说到哪；别一条条罗列属性、别念档案、别整段照抄原文。\n"
                "资料里没写的细节才别硬编，也别说什么「根据知识库」「资料显示」。\n\n"
                + "\n\n".join(blocks))

    def kb_names(self):
        """把勾选资料里的条目行扫成 名字 → 原话 的表。
        认两种写法：「- 名字｜属性/武器｜…」和「- 名字：说明」，所以角色、组织、地名都能收进来。
        只认行首「- 」，随手写的段落不进来，免得扫出一堆假名字。"""
        ids = self.mem.get("kb")
        if not isinstance(ids, list) or not ids:
            return {}
        want = set(str(x) for x in ids)
        out = {}
        for d in self.kb_docs():
            if d.get("id") not in want:
                continue
            for ln in (self.kb_read(d.get("file")) or "").split("\n"):
                s = ln.strip()
                if not s.startswith("- "):
                    continue
                body = s[2:].strip()
                if "｜" in body:
                    nm = body.split("｜")[0]
                elif "：" in body:
                    nm = body.split("：")[0]
                else:
                    continue
                nm = nm.split("（")[0].strip().strip("*").strip()
                if 2 <= len(nm) <= 12 and nm not in out:
                    out[nm] = body
        return out

    def kb_hit_prompt(self, text):
        """他这句话里如果点了知识库里的人，就把那人那行贴到提示词最末尾。
        贴的是原文，不是「请承认认识」这种空话——模型看得到具体内容，才有话可讲。"""
        t = str(text or "")
        if not t:
            return ""
        names = self.kb_names()
        if not names:
            return ""
        hits = []
        for nm, line in names.items():
            if nm in t:
                hits.append((nm, line))
        if not hits:
            # 只叫了前两个字（「卡提希娅」→「卡提」「卡提希」）
            for nm, line in names.items():
                if len(nm) >= 3 and any(nm[:k] in t for k in (3, 2)):
                    hits.append((nm, line))
        if not hits:
            return ""
        hits = hits[:4]
        return ("【他这句话点到了】下面这几行是从你自己的记忆里翻出来的，你确实认识，"
                "不是听来的、也不是别人硬塞给你的设定：\n"
                + "\n".join("- " + ln for _, ln in hits) + "\n"
                "既然认得，就直接按你记得的讲，带上你和对方的关系（人也好、地方和组织也好）、"
                "你的印象和感受；绝对不要回「没听过」「不认识」「这名字我没听过」——那才是说错了。"
                "也不要念成档案（别报属性、武器、星级），用你自己的话说。")

    def mood_prompt(self):
        """把它心里刚才那一下交给它。
        四道闸：把握不够不说、只说变化不说分值、同一个变化只说一次、
        只给感受不给指令——让它自己演，别照着念。"""
        e, why = mood_say(self.mood, self._mood_said)
        if not e:
            return ""
        self._mood_said = float(e.get("t") or 0)
        # 口气跟着「刚才那一下」走，而不是跟着总分走：一个 70 分的人，被晾着和被人
        # 接住时都还落在「开心」档，按档位说就会把话说反。
        d = _f(e.get("d"), 0.0)
        me = e.get("e") if isinstance(e.get("e"), dict) else {}
        up = _f(me.get("开心")) + _f(me.get("甜蜜")) + _f(me.get("踏实"))
        down = _f(me.get("难过")) + _f(me.get("心疼"))
        worry = _f(me.get("担心"))
        if worry >= 1.0 and worry >= max(up, down):
            tone = "有点挂心"
        elif down > 0 and down >= up:
            tone = "有点闷，提不起劲"
        elif up > 0 or d > 0:
            tone = "心里亮堂堂的"
        elif d < 0:
            tone = "有点闷，提不起劲"
        else:
            tone = {"欢喜": "心里亮堂堂的", "开心": "心情不错", "一般": "平平的",
                    "心疼": "有点闷", "难过": "提不起劲"}.get(mood_band(self.mood.get("v")), "平平的")
        return ("【你心里这会儿】%s。这会儿你%s——这是你自己的状态，不用说出来，"
                "也别主动解释心情，只是说话口气会跟着有点不一样。" % (why, tone))

    def mood_due(self, now=None):
        """这一轮该不该判心情。开着的闸都要满足：轮数够了、时间也够了才算。
        闸全关就等于不判（和把心情开关关掉一个效果）。"""
        now = now or time.time()
        if not (self.mood_by_turn or self.mood_by_time):
            return False
        if self.mood_by_turn and self._mood_turn < self.mood_every:
            return False
        if self.mood_by_time and self._mood_last and (now - self._mood_last) < self.mood_gap:
            return False
        return True

    def mood_why(self, now=None):
        """给人看的一句话：现在卡在哪个闸上。面板心情卡里显示。"""
        now = now or time.time()
        if not (self.mood_by_turn or self.mood_by_time):
            return "只记账不判（两个闸都关了）"
        bits = []
        if self.mood_by_turn:
            bits.append("还差 %d 轮" % max(0, self.mood_every - self._mood_turn)
                        if self._mood_turn < self.mood_every
                        else "轮数够了（%d/%d）" % (self._mood_turn, self.mood_every))
        if self.mood_by_time:
            left = (self.mood_gap - (now - self._mood_last)) / 60.0 if self._mood_last else 0.0
            bits.append("还差 %d 分钟" % max(0, int(left + 0.999))
                        if left > 0 else "时间够了")
        return " · ".join(bits)

    def judge_mood(self, text, answer):
        """每轮判一次心情往哪走。一次很短的调用；出错就当没发生，
        心情坏了不能把回复也带坏。"""
        if not answer:
            return
        try:
            out, _ = self._chat(
                [{"role": "user", "content": mood_judge_prompt("她", text, answer)}],
                max_tokens=200, temperature=0.2, scene="心情判定")
        except Exception as e:
            log("判定心情失败：%s" % e, "WARN")
            return
        d = mood_parse(out)
        if not d:
            return
        self.mood, moved = mood_apply(self.mood, d)
        if moved:
            self.save_mood(force=abs(moved.get("d") or 0) >= 4)

    def settle_mood(self, now=None):
        """没人理的时候，让它自己慢慢平复。

        以前只有「下一句被判心情」时才顺带衰减一次，于是隔夜醒来第一句话
        还在用昨晚那点余温说话——中间八个小时等于白过。这里按墙上的时间
        自己往回拉：心情值走向基线，七根情绪走向 0。不写理由、不进它的嘴，
        静默本来就没有「发生了什么」，只有「过了多久」。
        """
        now = now or time.time()
        before = self.mood if isinstance(self.mood, dict) else mood_blank(now)
        after = mood_decay(before, now)
        moved = abs(_f(after.get("v"), MOOD_BASE) - _f(before.get("v"), MOOD_BASE))
        emo = sum(_f(v) for v in (after.get("e") or {}).values())
        emo0 = sum(_f(v) for v in (before.get("e") or {}).values())
        self.mood = after
        if moved >= 0.5 or (emo0 - emo) >= 0.5:
            self.save_mood()          # 内部有 60 秒节流，不会每次都写盘
        return moved

    def save_mood(self, force=False):
        """落盘。参照 talkLog 的做法：先读最新再改一个字段，别拿旧快照整份覆盖。"""
        now = time.time()
        if not force and now - self._mood_write < 60:
            return
        self._mood_write = now
        d = read_memory()
        if str(d.get("profile") or "") != str(self.mem.get("profile") or ""):
            return          # 换卡片了，这份心情属于上一个人格
        d["mood"] = self.mood
        if write_memory(d):
            self.mem = d

    def _image_prompt(self):
        """告诉它「（发了张图：…）」那行字是它自己看见的，不是别人转述的。
        不写这段，它会把描述当成群友说的话来回应，或者一本正经地念画面。"""
        if not self.vision:
            return ""
        return ("【你长了眼睛】群里有人发图时，你会看到「（发了张图：…）」这样的句子，"
                "那是你自己扫了一眼看到的内容，不是谁转述给你的。"
                "括号里是描述，不是原话：别照念、别说「图片显示」「根据描述」。"
                "就像真人看到图那样反应一句——图里的游戏、场面、笑点，认得就顺着说，"
                "图上写着字的可以随口念出来。看不懂是什么梗，就问一句他在指什么，别硬解释。"
                "有人发图配一句话（比如发张对战截图说「等压狗」），把那句话和图连起来看："
                "他是在拿这张图吐槽，不是在问你图里是什么。")

    def _ability_prompt(self):
        """智能体模式才有的三段「我知道自己是谁、能干什么」。
        没有它，就算开关打开，它也会说“agent 这词跟我没关系”——问的明明是它自己。"""
        if not self.agent:
            return ""
        return ("【这回你能自己动手】平时你只顺着眼前这句话聊；现在你能往前翻自己看过的旧聊天、"
                "能翻自己记的便签、能认出以前聊过的人。被问到「以前说过吗」「上次那个是谁」"
                "「谁提过什么」的时候，先去翻，别一口回「不记得」「跟我没关系」；"
                "翻到了就自然带一句，像你自己想起来的，别说「根据记录」「检索到」。\n\n"
                "【你做不到什么】你没有退群、加群、踢人、改群名、发文件这些权限，也走不出这个聊天，"
                "被人移出去了就回不来。有人让你做这类事、或者说「待会再把你拉回来」，"
                "就当场说清楚做不到；别答应、别顺着演、更别说「待会就好了」——"
                "你答应下来的事，对方是真的会等。最好再提醒一句：这类事得他自己动手。\n\n"
                "【有人问你是什么】有人提到 agent、智能体、AI 这类词，别装傻也别背设定，"
                "就说你确实能自己记事、自己翻东西，别的地方还是你自己。")

    def _accounts_prompt(self):
        """它自己在哪几个地方有号。

        现在只有一个平台，这段看着多余；但接第二个平台那天，没有它，
        它被问「你 QQ 多少」会当场编一个号出来——编出来的号对方会真去加。
        所以宁可现在就把话说死：只有这几个，别的没有。
        """
        rows = []
        for a in (self.mem.get("accounts") or []):
            if not isinstance(a, dict):
                continue
            pn = str(a.get("platform") or "").strip()
            nm = str(a.get("name") or "").strip()
            if pn and nm:
                rows.append("%s 上叫「%s」" % (pn, nm))
        if not rows:
            return ""
        return ("【你在哪儿有号】你只出现在这几个地方：" + "；".join(rows) + "。\n"
                "别的地方（QQ、微信、Telegram 这些你还没去过的）你是真的还没有号——"
                "不是不肯给，是没有。有人问「你 QQ 多少」「加个微信」「在哪能找到你」，"
                "就照这个答：告诉他自己在这儿的名字；那边没有的，直说没有。\n"
                "别编号敷衍（编出来对方会真去加，然后一直等），"
                "也别拿「修士不便多谈」这类话搪塞过去——被问的不是门规，是你人在哪儿。")

    def _pick_people(self, book, text, me="", limit=4):
        """这句话里提到了谁，就从档案里把谁捞出来。
        只认两字以上的名字/曾用名，免得「你」「我」「他」这种把半本档案都翻出来。"""
        t = str(text or "")
        if not t:
            return []
        hit = []
        for u, v in (book or {}).items():
            if u == me:
                continue
            for n in [v.get("name") or ""] + list(v.get("alias") or []):
                n = (n or "").strip()
                if len(n) >= 2 and n in t:
                    hit.append(u)
                    break
        return hit[:limit]

    # ---- 联网搜索 ----
    def _search_web(self, query, timeout=15):
        """调搜索服务拿结果，回一份「标题 / 摘要 / 链接」的纯文本，最多 5 条。
        默认接博查（BoCha），接口格式兼容 OpenAI 那套别的也行。搜不到/出错回空串。"""
        if not self.search_key:
            return ""
        q = (query or "").strip()
        if not q:
            return ""
        try:
            body = json.dumps({"query": q, "count": 5, "freshness": "noLimit",
                               "summary": True, "textType": "markdown"},
                              ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(self.search_url, data=body, method="POST", headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.search_key,
            })
            with NO_PROXY.open(req, timeout=timeout) as r:
                d = json.loads(r.read().decode("utf-8"))
        except Exception:
            return ""
        pages = (d.get("data") or {}).get("webPages") or {}
        items = pages.get("value") or []
        out = []
        for it in items[:5]:
            if not isinstance(it, dict):
                continue
            t = (it.get("name") or "").strip()
            s = (it.get("summary") or it.get("snippet") or "").strip()
            u = (it.get("url") or "").strip()
            if not t and not s:
                continue
            line = t or s
            if t and s:
                line = t + "：" + s
            if u:
                line += "（" + u + "）"
            out.append(line)
        return "\n".join(out)

    def _want_search(self, text, recent=None):
        """只花一次很短的调用，问模型这句要不要联网查。
        要查返回搜索词，不查返回空串。这样平时聊天不联网，只在「查一下」时才动搜索。"""
        if not (self.search_on and self.search_key):
            return ""
        t = (text or "").strip()
        if not t:
            return ""
        ctx = ""
        if recent:
            lines = _talk_lines(recent[-6:])
            if lines:
                ctx = "群里刚聊到：\n" + "\n".join(lines) + "\n\n"
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content":
                 "你是判断器。判断用户这句话是否需要联网搜索实时信息（比如新闻、游戏版本更新、"
                 "某个东西现在的情况、攻略、价格、日期、赛事结果等）才能准确回答。"
                 "需要就只回一个搜索关键词（几个字，不要句子）；不需要就只回「不搜」。"
                 "纯闲聊、接梗、情感、人身攻击、问他本人情况的一律回「不搜」。"},
                {"role": "user", "content": ctx + t},
            ],
            "max_tokens": 32, "temperature": 0.0, "stream": False,
        }, ensure_ascii=False).encode("utf-8")
        try:
            req = urllib.request.Request(self.url, data=body, method="POST", headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.token,
            })
            t0 = time.time()
            with NO_PROXY.open(req, timeout=20) as r:
                d = json.loads(r.read().decode("utf-8"))
            usage_write("判断联网", self.model, d.get("usage") or {}, time.time() - t0)
            out = ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "") or ""
        except Exception:
            return ""
        out = out.strip()
        if not out or out in ("不搜", "不需要", "不用搜"):
            return ""
        # 模型偶尔会多打个「搜索关键词：」之类的标签，剥掉，只留关键词本身
        for tag in ("搜索关键词：", "搜索关键词:", "关键词：", "关键词:", "搜索："):
            if out.startswith(tag):
                out = out[len(tag):].strip()
                break
        if not out:
            return ""
        return out[:60]

    def self_voice_lines(self, gid, n=5):
        """她最近在这个会话里说过的原话（聊天缓存里 me=True 的那些）。
        两处要用：当语气样本写进提示词，以及语感闸门拿它比对新写的一句。"""
        gid = str(gid or "")
        lines = []
        arr = ((self.mem or {}).get("talkLog") or {}).get(gid) or []
        for x in arr:
            if not x.get("me"):
                continue
            t = _voice_clean(x.get("text"))
            if t:
                lines.append(t)
        # 缓存里还没录到（刚重启、面板试聊）就退回内存里的对话历史
        if not lines:
            for x in self._history.get(gid, []) or []:
                if x.get("role") == "assistant":
                    t = _voice_clean(x.get("content"))
                    if t:
                        lines.append(t)
        # 同一句反复说过只留一条，按时间从新到旧取 n 条，再翻回正序
        seen, out = set(), []
        for t in reversed(lines):
            k = _plain(t)[:12]
            if k in seen:
                continue
            seen.add(k)
            out.append(t)
            if len(out) >= n:
                break
        return list(reversed(out))

    def self_voice_prompt(self, gid, n=5):
        """把「她自己说过的原话」摆在提示词最末尾附近。
        没有这段，她每轮都是从零开口，自然一路滑向「平均腔调」；
        有了它才会有自己的口癖，也才知道哪句式样已经用过了。"""
        vs = self.self_voice_lines(gid, n)
        if len(vs) < 2:
            return ""
        body = " ".join("「%s」" % _one_line(v, 60) for v in vs)
        return ("【你最近是这样说话的】下面这几句是你自己前面说过的原话。"
                "开口前扫一眼：别用同样的开头、别套同一个句式——"
                "要越来越像「这一个你」，不是越来越像一个模板。"
                "接得上话可以，但别换个名字原样再说一遍。\n" + body)

    # ------------------------------------------------------------ 主动开口的台账
    # 跟 talkLog 分开存：那里混着「回复别人的话」，学不出「怎么起头」。
    # 这里只收她自己开口说出去的那几句（冷场冒泡 / 群聊主动 / 私聊主动）。
    def proactive_lines(self, gid, n=3, kind=None):
        arr = ((self.mem or {}).get("proactive") or {}).get(str(gid)) or []
        got = [x for x in arr if isinstance(x, dict) and x.get("text")]
        if kind:
            same = [x for x in got if x.get("kind") == kind]
            got = same or got      # 同一路的一句都没有，就先拿别的路凑（总比没有强）
        return got[-n:]

    def note_proactive(self, gid, text, kind="bubble"):
        """真发出去之后才记。没发出去的不算——不然她会以为自己说过。"""
        t = _voice_clean(text)
        if not t:
            return
        box = self.mem.setdefault("proactive", {})
        if not isinstance(box, dict):
            box = self.mem["proactive"] = {}
        arr = box.setdefault(str(gid), [])
        arr.append({"t": time.time(), "text": t[:200], "kind": kind})
        box[str(gid)] = arr[-12:]
        try:
            write_memory(self.mem)
        except Exception as e:
            log("记主动开口台账失败: %s" % e, "WARN")

    def _topic_tags(self, text):
        """这句话在说哪个话题。组内命中一个词就算该组；点到具体的人算单独一组。
        返回 set，用来跟前面几次比「是不是同一个话头」。"""
        t = str(text or "")
        tags = set()
        for name, words in TOPIC_GROUPS:
            for w in words.split():
                if w and w in t:
                    tags.add(name)
                    break
        for v in ((self.mem or {}).get("users") or {}).values():
            nm = str(v.get("name") or "").strip()
            if nm and nm in t:
                tags.add("人:" + nm)
                continue
            for al in (v.get("alias") or []):
                al = str(al or "").strip()
                if al and al in t:
                    tags.add("人:" + (nm or al))
                    break
        return tags

    def _topic_reason(self, text, prev):
        """跟最近几次自己开口比：同一个话题就返回原因，换话题返回 None。
        只在「她自己起头」时用；用户指定了方向（hint）就别拦。"""
        cur = self._topic_tags(text)
        if not cur:
            return None
        for p in prev or []:
            old = self._topic_tags(p.get("text") or "")
            hit = cur & old
            if hit:
                return "跟上次说的是同一个话头（%s）" % "、".join(
                    sorted(x.replace("人:", "提到") for x in hit))
        return None

    def proactive_samples(self, gid, kind=None, n=3):
        """把「自己前几次开口」摆进提示词。没有这段，她每次起头都从零想，
        能想到的只有「现在几点」这件最确定的事——六次实测全撞在这里。"""
        rows = self.proactive_lines(gid, n, kind)
        if not rows:
            return ""
        body = " ".join("「%s」" % _one_line(x.get("text"), 60) for x in rows)
        tags = set()
        for x in rows:
            tags |= self._topic_tags(x.get("text"))
        ban = [t for t in tags if not t.startswith("人:")]
        out = ("【你前几次自己开口是这么说的】" + body + "\n"
               "这次换一件事说。上面这些是**话题**，不是句式——"
               "光换个说法、换个开头，说的还是同一件事，那不算换。")
        if ban:
            out += "\n尤其别再说「%s」这一类；要提到，也得是具体到某个人的某件事。" % (
                "」「".join(sorted(ban)))
        return out

    def _style_reason(self, draft, gid):
        """这句能不能发：撞助手腔开头 / 开头连着用同一个词 / 跟最近某句太像。
        返回一句原因（拿去拼「重说」的指令），None = 可以发。"""
        d = _voice_clean(draft)
        if not d:
            return None
        m = STYLE_LEAD_RE.match(d)
        if m:
            return "开头「%s」是助手腔" % m.group(0).strip()
        if len(d) < 10:
            return None      # 「好呀」「嗯，我在」这种短句不算重复，别硬改
        olds = []
        for old in self.self_voice_lines(gid, 8):
            o = _voice_clean(old)
            # 太短的旧句不参与比较：拿「好呀」去比什么都像
            if o and len(_plain(o)) >= 8:
                olds.append(o)
        head = _plain(d)[:6]
        plain_head = _plain(d)
        # ① 开头六个字整段一样——最扎眼的那种「同一个回应又说一遍」
        for o in olds:
            if len(head) >= 5 and _plain(o)[:6] == head:
                return "开头跟她前面说的一句一样（「%s…」）" % _one_line(o, 14)
        # ② 开头两个字连着用：我上一轮实测她连着两句都「记得，我……」，
        #    整句不像，但起手一模一样，人一眼就看出来在重复。
        pre = plain_head[:2]
        if len(pre) == 2 and pre not in STYLE_STOP_PRE:
            same = sum(1 for o in olds if _plain(o)[:2] == pre)
            if same >= 2:
                return "开头连着几句都用「%s」起手" % pre
        best, who = 0.0, ""
        for o in olds:
            s = style_sim(d, o)
            if s > best:
                best, who = s, o
        if best >= self.style_gate_sim:
            return "跟前面说过的「%s…」太像（%.0f%%）" % (_one_line(who, 16), best * 100)
        return None

    def system_prompt(self, gid, uid=None, name="", text="", lean=False):
        """lean=True 是 agent loop 那套：知识库、日记、别人的档案都不塞进来，
        改成她要的时候自己去查。省下来的都是 token，也少了「一堆东西里挑不到」的毛病。"""
        parts = [self.persona()]
        # 本体段放最前：这不是素材，是「谁在说话」。只有本体模式的卡才注入——
        # 旧卡有自己完整的人设，再塞一份种子进去会变成两个人格打架。
        _me = self.self_prompt() if (self.mem or {}).get("selfOwned") else ""
        if _me:
            parts.insert(0, _me)
            parts.append(self.terminal_prompt())
        # 总观：它自己怎么想自己、怎么想这个世界。放在人设后面、别的记忆前面——
        # 这是底色，不是素材。行首 // 或 # 的行是面板那边的备注，剥离掉不给模型看。
        view = strip_notes(str((self.mem or {}).get("outlook") or "")).strip()
        if view:
            parts.append(
                "【你怎么看】下面这些是你自己琢磨出来的想法，关于你自己，也关于这个世界。"
                "不是别人给你的规矩，所以不用照做、也不用念出来——"
                "只是你说话、判断事情的时候，底下垫着的就是这个态度。"
                "遇到相关的事，可以自然流露一点（比如你觉得人跟人之间是怎么回事），"
                "但别整段背诵，也别提「我的世界观」「我的想法是这样的」。\n\n" + view)
        kb = "" if lean else self.kb_prompt()
        if kb:
            parts.append(kb)
        diary = "" if lean else self.diary_prompt(gid)
        if diary:
            parts.append(
                "【你的日记】下面这些是你自己写的，是你亲身经历过的，不是别人告诉你的。"
                "里面写的心情和看法就是你现在的态度——聊到相关的人或事，"
                "可以自然带出那种感觉（比如“上次还说爆照呢”“你那天那句话我可记着了”），"
                "但不要逐条复述，也不要说“根据记忆”“根据日记”。\n\n" + diary)
        # 群友档案：以 UID 为身分，昵称只是称呼，改名不换人。
        # 当前说话人排最前；其余只注入有记忆或改过名的，避免把人名清单灌进提示词。
        book = self.user_book(gid)
        me = str(uid or "").strip()
        order = []
        if me and book.get(me):
            order.append(me)
        # 智能体模式：这句话里点到谁，就把谁顶到前面翻出来——哪怕他早就不在最近说话的人里。
        # 普通人设模式下不做，免得凭空多出一堆人名。
        if self.agent:
            for u in self._pick_people(book, text, me):
                if u not in order:
                    order.append(u)
        for u, v in book.items():
            # 精简模式只带「正在跟你说话的这个人」；别人的资料她要查再查
            if lean and u != me:
                continue
            if u not in order and (v.get("notes") or v.get("alias")):
                order.append(u)
        lines = []
        for u in order[:(20 if self.agent else 12)]:
            v = book.get(u) or {}
            head = ("%s（UID %s）" % (v.get("name"), u)) if v.get("name") else ("UID %s（昵称未知）" % u)
            extra = []
            if v.get("alias"):
                extra.append("曾用名：" + "、".join(v.get("alias")))
            if v.get("notes"):
                # 一个人最多带几条：笔记是按时间往后追加的，越靠后越近。
                # 全塞进去会把提示词撑肥，也等于没忘；只带最近的几条。
                extra.append("；".join(list(v.get("notes"))[-8:]))
            lines.append("- " + head + ("：" + "｜".join(extra) if extra else ""))
        if lines:
            parts.append("【群友档案】用 UID 认人，昵称只是称呼，改名不代表换了个人。"
                         "这些是你自己平时记的便签，语气是你自己的——认出老熟人时按档案里的关系说话，"
                         "但别整句照搬，也别说“根据记忆”“档案里写着”。\n"
                         + "\n".join(lines))
        # 好感度：只给「正在跟你说话的人」一条，不把整张关系网灌进去。
        # 分数必须给——人设里「好感度 60 以上说话不用严肃」是按分数判断的，
        # 只给档位它拿不到 60 这条线，就会「检测不到」。
        if me:
            rec = self.aff_of(me)
            if rec:
                av = _f(rec.get("v"), AFF_BASE)
                ab = aff_band(av)
                feel = {"偏心": "你心里偏着他，话会不自觉软下来",
                        "亲近": "已经很熟了，可以松一点，不用句句客气",
                        "熟络": "算熟人了，正常聊就行",
                        "眼熟": "见过几面，还谈不上熟",
                        "眼生": "还生分，说话客气些",
                        "冷淡": "你对他淡得很，别主动热络"}.get(ab, "")
                if feel:
                    nm = (book.get(me) or {}).get("name") or "这个人"
                    if av >= 85:
                        tone = "称呼直接叫「你」、别再用「您」，偶尔可以忘形、开玩笑，说漏了就慌忙找补回来"
                    elif av >= 60:
                        tone = "不用句句端着，放松些；称呼可以慢慢从「您」松到「你」，但别失了分寸"
                    else:
                        tone = "还不到熟络，称呼用「您」、话收着说，客气些"
                    parts.append("【你对他】%s：好感 %d 分（%s）——%s；%s。"
                                 "这是你自己的感觉，照它拿捏松紧；"
                                 "不要说出口分数，也不要说「好感度」这种词，更不要解释。\n"
                                 % (nm, av, ab, feel, tone))
        seg = self.emoji_prompt()
        if seg:
            parts.append(seg)
        ability = self._ability_prompt()
        if ability:
            parts.append(ability)
        pic = self._image_prompt()
        if pic:
            parts.append(pic)
        search = self._search_prompt()
        if search:
            parts.append(search)
        # 放最后：提示词越靠后越容易被当回事，而「我在哪儿有号」被问到的频率不高、
        # 但答错代价大（编个号出去，对方会真去加，然后一直等）
        acc = self._accounts_prompt()
        if acc:
            parts.append(acc)
        # 她自己的原话：放在靠后（越后越当回事），但仍在「当场事实」之前。
        # 这一段的用处是让语气有连续性，不是让她背台词。
        voice = self.self_voice_prompt(gid)
        if voice:
            parts.append(voice)
        # 放最最后：他这句话里点到了知识库里的人，就把那人那行原文贴到嘴边。
        # 只靠中间那大段「知识库」，模型在长提示词里会漏看，然后拿人设里
        # 「不撒谎、不编造」给自己开脱，一口咬定「没听过」。
        hit = "" if lean else self.kb_hit_prompt(text)
        if hit:
            parts.append(hit)
        if lean:
            # 精简模式：手里没有那份长清单，但东西都在，想得起来就去翻
            parts.append(
                "【你手边有的东西】你的日记、群友档案、脑子里的设定资料，都是你自己攒的，"
                "只是没摆在这一页上。想不起来的时候可以先去翻一眼再开口（翻日记、翻旧聊天、"
                "查某人的档案、翻设定），翻到了就用你自己的话说出来——"
                "别说「我查一下」「资料里写着」「根据记忆」。"
                "日常闲聊不必翻，拿不准、或者对方问的是具体的人/事/设定时才翻。")
        # 也放最末尾：这是「说了记下就得真记」的开关，夹在中间会被长提示词淹掉
        memo = self._memo_prompt()
        if memo:
            parts.append(memo)
        # persona() 在本体模式下会返回空串，空段会在提示词里留一堆空行
        return "\n\n".join([p for p in parts if p and p.strip()])

    def _memo_prompt(self):
        """告诉它「想记就真能记」。以前说「记下了」纯属嘴上一句，档案里一个字都没有。"""
        return ("【想记住一件事的时候】不是每句都记，只在真值得记的时候——他自己的事、"
                "他的喜好和脾气、他特意交代你的话。办法：在回复里写一处 [[记:一句话]]，"
                "程序会把它存进这个人的便签，对方看不到这串东西。这句话要写「什么事｜你怎么看」，"
                "带上你当时的态度（例：最怕打雷｜下回打雷别只顾着讲道理），"
                "光写事实过两天你自己都觉得没意思；别抄原话、别写长。嘴上顺带说一句「记下了」就够了。\n"
                "反过来更要紧：不打算记的事，就别说什么「记下了」。说了就当你是真写进去了，"
                "而对方会当真——敷衍的「记住了」比不回应更伤人。")

    def _search_prompt(self):
        """联网开关打开、且配了 Key 时，告诉它「遇到要查实时的事我能去搜」。
        平时不用它自己决定要不要搜——代码里已经有一道「该不该搜」的判断，
        这里只是让它知道搜来的结果是自己查的，别当外人转述。"""
        if not (self.search_on and self.search_key):
            return ""
        return ("【你能联网查】别人问你「现在 XX 怎么样了」「XX 更新没」「XX 多少钱」「最近有什么新闻」"
                "这类要看最新信息的事，你可能会自己先联网查一下再答。"
                "查到的结果会以「【你刚联网查到的】」那几行给你，那是你自己搜来的，"
                "就着它说，别说「我帮你查一下」「根据搜索」，也别说「我没法上网」——你能查。")

    def emoji_prompt(self, small_limit=10, sticker_limit=8):
        """把「你能用哪些表情」写进提示词；没收集到就整段不出现，免得模型瞎编"""
        e = self.mem.get("emoji") or {}
        small = e.get("small") or {}
        stick = e.get("sticker") or {}
        lines = []
        if small:
            top = sorted(small.items(), key=lambda kv: -(kv[1].get("count") or 0))[:small_limit]
            codes = []
            for k, v in top:
                code = v.get("code") or ("[" + k + "]")
                # 括号里补一句谁常发：被问「这表情哪来的」能照实答，而不是含糊带过。
                # 只在证据够硬时才说（至少 3 次、且过半是同一个人）——
                # 样本两三次就报个名字，很容易说成错的人。
                by = v.get("by") if isinstance(v.get("by"), dict) else {}
                nm = ""
                if by:
                    top_n, top_c = max(by.items(), key=lambda kv: kv[1])
                    if sum(by.values()) >= 3 and top_c * 2 >= sum(by.values()):
                        nm = top_n
                codes.append("%s（%s%s）" % (
                    code, k.split("_", 1)[-1], " · %s 常发" % nm if nm else ""))
            lines.append("小表情：直接写进回复正文，客户端会渲染成图 —— " + "、".join(codes))
        if stick:
            # 顺序必须和面板里「表情包收藏」的格子一致（面板也是按 count、sent 排），
            # 否则用户说「第二张」、模型理解的却是另一个顺序，发出来就对不上。
            top = sorted(stick.items(),
                         key=lambda kv: (-(kv[1].get("count") or 0),
                                         -(kv[1].get("sent") or 0)))[:sticker_limit]
            items = []
            for i, (k, v) in enumerate(top, 1):
                label = (v.get("label") or "").strip()
                if len(label) > 42:
                    label = label[:42] + "…"
                frm = (v.get("from") or "").strip()
                src = ("（%s 发来的）" % frm) if frm and frm != "本机图片" else ""
                if label:
                    items.append("  %d. %s%s → [[表情:%s]]" % (i, label, src, k))
                else:
                    items.append("  %d. （没名字）%s → [[表情:%s]]" % (i, src, k))
            lines.append("大表情（表情包）：想发就照抄下面某一行右边那串方括号，单独成句，"
                         "会作为一条图发出去。方括号里**只能**是 KEY 本身"
                         "（那串数字或字母），名字只是给你认哪张图的，写进去就发不出来了。"
                         "后面括号里写着这张是谁发来的，被人问起「这张是谁的」就照实说。\n"
                         + "\n".join(items))
        if not lines:
            return ""
        return ("【表情】你平时会发表情，但一条回复最多一个，不对味就别发，不要连着刷，"
                "也不要在同一条里既写小表情又发大表情。写完自己检查一遍："
                "大表情的方括号指令里只能有 KEY，不能有名字、不能有空格。\n"
                "小表情后面写着「谁常发」、大表情后面写着「谁发来的」——被你问起"
                "「这表情你哪来的」「这张是谁的」，就照这个说，别说不知道、也别瞎编。\n"
                "聊天记录里，你自己说过的话后面如果跟着「（顺手发了张表情包：…）」——"
                "那是你当时真发出去的一张图，不是别人说的、也不是对方发的；"
                "被人问起就照那个描述说，别自己另编一张；但回话时别把这段括注念出来。\n"
                + "\n".join("- " + x for x in lines))

    def history(self, gid):
        return self._history.setdefault(str(gid), [])

    # ---- 独立会话（只在 agentLoop 开着时用） ----
    def _peer_key(self, uid, gid="", is_dm=False):
        """有 UID 就按人走：同一个人在群里说的、私聊说的，算同一条线。
        没有 UID 的老记录只能按会话分开，否则所有人共用一条，会串得厉害。"""
        u = str(uid or "").strip()
        if u:
            return u
        return ("私聊:" if is_dm else "群:") + str(gid or "")

    def peer_history(self, uid, gid="", is_dm=False):
        """这个人跟你的最近几轮。不掺别人的话，也不掺别的群的话——
        群里刚发生什么由 recent 单独补，别混进这条线。"""
        if self._peer is None:
            self._peer = peer_read()
        arr = self._peer.get(self._peer_key(uid, gid, is_dm)) or []
        keep = self.peer_rounds * 2
        return [{"role": x.get("role"), "content": str(x.get("content") or "")}
                for x in arr if x.get("role") in ("user", "assistant")][-keep:]

    def peer_remember(self, uid, gid, is_dm, user_text, bot_text):
        if self._peer is None:
            self._peer = peer_read()
        k = self._peer_key(uid, gid, is_dm)
        arr = self._peer.setdefault(k, [])
        arr.append({"role": "user", "content": str(user_text or "")})
        arr.append({"role": "assistant", "content": str(bot_text or "")})
        del arr[:max(0, len(arr) - self.peer_rounds * 2)]
        peer_write(self._peer)

    # ---- agent loop：想 → 调工具 → 再看 ----
    # 工具只读 + 记便签，不碰发送。给模型的是「能自己去查」的手，
    # 不是「替它把事情做完」的执行器——真发东西还是走原来那套。
    LOOP_TOOLS = [
        {"type": "function", "function": {
            "name": "look_up",
            "description": "翻你记的群友档案：这人是谁、什么喜好、你对他什么看法、好感多少。"
                           "想不起某个人、或要确认跟他的关系时用。",
            "parameters": {"type": "object",
                           "properties": {"who": {"type": "string", "description": "昵称或 UID"}},
                           "required": ["who"]}}},
        {"type": "function", "function": {
            "name": "recall_chat",
            "description": "往前翻聊天记录，找更早时候的原话。想确认「上次是不是说过这件事」时用。",
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string", "description": "要找的事，几个字"}},
                           "required": ["query"]}}},
        {"type": "function", "function": {
            "name": "read_diary",
            "description": "看你最近几天写的日记，回忆这几天经历过什么、当时什么心情。",
            "parameters": {"type": "object",
                           "properties": {"days": {"type": "integer", "description": "看几天，默认 3"}}}}},
        {"type": "function", "function": {
            "name": "search_kb",
            "description": "翻你脑子里的设定资料（世界观、角色、术语、攻略）。"
                           "别人问起某个角色、地名、名词而你拿不准时用。",
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string"}},
                           "required": ["query"]}}},
        {"type": "function", "function": {
            "name": "save_note",
            "description": "把值得记住的事写进这个人的便签。只在他说了关于自己的、"
                           "以后还用得上的事时用；日常闲聊不要记。",
            "parameters": {"type": "object",
                           "properties": {"text": {"type": "string",
                                                   "description": "一句话：什么事｜你怎么看"}},
                           "required": ["text"]}}},
        {"type": "function", "function": {
            "name": "web_search",
            "description": "上网查。有人提到你不懂的名字、游戏、地方，或者你想确认一件事是真是假，"
                           "就自己查，别问他「要不要我查」。",
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string",
                                                    "description": "搜什么，几个字就够"}},
                           "required": ["query"]}}},
        {"type": "function", "function": {
            "name": "write_self",
            "description": "往你自己的本子上写一条关于『你自己』的话。"
                           "about=已经能确认的，guess=你猜的还没证，open=原先以为懂现在发现不懂的。"
                           "别人是谁别写这里（那是便签）。",
            "parameters": {"type": "object",
                           "properties": {
                               "text": {"type": "string",
                                        "description": "一句话，30 字内，第一人称"},
                               "where": {"type": "string",
                                         "enum": ["about", "guess", "open"]}},
                           "required": ["text"]}}},
    ]

    def loop_tools(self):
        """本体模式的卡才有「写自己」这只手；旧卡有完整人设，写这个只会打架。"""
        if (self.mem or {}).get("selfOwned"):
            return self.LOOP_TOOLS
        return [t for t in self.LOOP_TOOLS
                if (t.get("function") or {}).get("name") != "write_self"]

    def _run_tool(self, name, raw_args, uid="", gid="", speaker="", text=""):
        """跑一个工具，返回给模型看的一小段文字。查不到就说查不到，别抛异常。"""
        try:
            a = json.loads(raw_args or "{}")
            if not isinstance(a, dict):
                a = {}
        except Exception:
            a = {}
        try:
            if name == "look_up":
                return self._tool_look_up(a.get("who") or speaker, uid)
            if name == "recall_chat":
                return self._tool_recall(a.get("query") or text, gid)
            if name == "read_diary":
                return self._tool_diary(gid, a.get("days"))
            if name == "search_kb":
                return self._tool_kb(a.get("query") or text)
            if name == "save_note":
                return self._tool_note(a.get("text"), uid, speaker)
            if name == "web_search":
                return self._tool_web(a.get("query") or text)
            if name == "write_self":
                return self._tool_write_self(a.get("text"), a.get("where"))
        except Exception as e:
            log("工具 %s 出错: %s" % (name, str(e)[:120]), "WARN")
        return "（没查到东西）"

    def _tool_look_up(self, who, me=""):
        who = str(who or "").strip()
        book = self.user_book()
        if not who:
            return "（没说找谁）"
        hit = who if (who.isdigit() and who in book) else ""
        if not hit:
            for u, v in book.items():
                nm = str(v.get("name") or "")
                if nm and (nm == who or who in nm or nm in who):
                    hit = u
                    break
                if any(str(x) == who for x in (v.get("alias") or [])):
                    hit = u
                    break
        if not hit:
            return "（档案里没这个人，可能是新来的）"
        v = book.get(hit) or {}
        out = ["%s（UID %s）" % (v.get("name") or "未知名", hit)]
        notes = list(v.get("notes") or [])[-8:]
        if notes:
            out.append("你记的他：" + "；".join(str(x) for x in notes))
        av = _f((self.aff_of(hit) or {}).get("v"), AFF_BASE)
        out.append("好感 %d 分（%s）" % (av, aff_band(av)))
        return "\n".join(out)

    def _tool_recall(self, query, gid=""):
        q = str(query or "").strip()
        if not q:
            return "（没说要找什么）"
        # 只翻「她这一份」：以前这里读盘，是为了拿最新写入的记录，
        # 但换了卡之后盘上还留着上一个人的对话，她会翻到别人的人生当成自己的。
        d = self.mem or read_memory()
        tl = d.get("talkLog") or {}
        arr = list(tl.get(str(gid)) or [])
        if len(arr) < 8:
            arr = [x for v in tl.values() if isinstance(v, list) for x in v]
        got = _recall(arr, q, [], limit=4)
        if not got:
            return "（往前翻了翻，没找到相关的）"
        return "\n".join(_talk_lines(got))

    def _tool_diary(self, gid="", days=None):
        try:
            n = int(days or 3)
        except Exception:
            n = 3
        t = self.diary_prompt(gid, max_chars=1200, max_days=max(1, min(7, n)))
        return t or "（这几天还没写过日记）"

    def _tool_web(self, query):
        """她自己伸手去网上看。搜不到就说搜不到——不能编一份「资料」。"""
        q = str(query or "").strip()
        if not q:
            return "（没说要查什么）"
        if not (self.search_on and self.search_key):
            return "（这会儿连不上网）"
        r = self._search_web(q)
        if not r:
            return "（搜了，什么都没搜到）"
        return "【你刚联网查到的】\n" + r

    def _tool_write_self(self, text, where=""):
        """她自己往本子上添一条。这是「空白」唯一的长法，所以不去拦她写什么，
        只拦重复——同一条写十遍，本子就成复读机了。"""
        if not (self.mem or {}).get("selfOwned"):
            return "（这张卡没有自己的本子）"
        s = str(text or "").strip()[:60]
        if not s:
            return "（没写内容）"
        k = str(where or "guess").strip()
        if k not in ("about", "guess", "open"):
            k = "guess"
        st = read_self()
        cur = self_lines(st.get(k), 24)
        if any(_norm_note(x) == _norm_note(s) for x in cur):
            return "（这条你已经写过了）"
        cur.append(s)
        st[k] = cur[-24:]
        write_self(st)
        log("她自己写了一条[%s]：%s" % (k, s))
        return "（写进「%s」了）" % {"about": "确认的", "guess": "猜的", "open": "还不确定"}[k]

    def _tool_kb(self, query):
        q = str(query or "").strip()
        ids = self.mem.get("kb")
        if not isinstance(ids, list) or not ids:
            return "（没有可翻的资料）"
        want = set(str(x) for x in ids)
        docs = [d for d in self.kb_docs() if d["id"] in want]
        keys = [w for w in re.split(r"[\s，。、？！,.;:]+", q) if len(w) >= 2][:4]
        lines = []
        for d in docs:
            text = self.kb_read(d["file"])
            if not text:
                continue
            for ln in text.splitlines():
                s = ln.strip()
                if s and keys and any(k in s for k in keys):
                    lines.append(s)
                if len(lines) >= 12:
                    break
            if len(lines) >= 12:
                break
        if not lines:
            return "（资料里没写到这个）"
        return "\n".join(lines[:12])

    def _tool_note(self, note, uid="", speaker=""):
        txt = _one_line(note, 60)
        if not txt:
            return "（没写内容）"
        return "记下了" if self.memo_for(uid, txt, speaker) else "（这条记不进去，或者早记过了）"

    def _agent_loop(self, msgs, uid="", gid="", speaker="", who="", text=""):
        """想 → 调工具 → 再看。最多 loop_steps 轮，最后一轮必须开口。
        返回 (正文, 总 token, 这几步做了什么)。"""
        used, total = [], 0
        for _ in range(self.loop_steps):
            out, calls, tok = self._chat_tools(msgs, self.loop_tools(), who=who, scene="自己查")
            total += int(tok or 0)
            if not calls:
                return out, total, used
            msgs.append({"role": "assistant", "content": out or "", "tool_calls": calls})
            for c in calls:
                fn = c.get("function") or {}
                nm = fn.get("name") or ""
                res = self._run_tool(nm, fn.get("arguments"), uid=uid, gid=gid,
                                     speaker=speaker, text=text)
                msgs.append({"role": "tool", "tool_call_id": c.get("id") or "", "content": res})
                used.append("%s：%s" % (LOOP_TOOL_NAMES.get(nm, nm), _one_line(res, 46)))
        # 一直在查、不肯说话：把工具摘掉，逼它用现有材料回一句
        out, tok = self._chat(msgs, who=who)
        return out, total + int(tok or 0), used

    def reload(self, cfg, mem):
        """配置/人设热更新，保留对话历史"""
        hist = self._history
        # 真思考的用量不用特意留：__init__ 会从 think.jsonl 里按时间捞回来，
        # 那份记录比内存里的计数更全（跨进程、跨重启都在）
        self.__init__(cfg, mem)
        self._history = hist

    def remember(self, gid, user_text, bot_text, uid=None, name=""):
        # 历史里带上「昵称|UID」，这样整理群友档案时能把话归到具体的人；
        # 同时群记忆也能分清谁说的。
        if uid:
            user_text = "[%s|%s] %s" % (name or "", uid, user_text)
        h = self.history(gid)
        h.append({"role": "user", "content": user_text})
        h.append({"role": "assistant", "content": bot_text})
        keep = max(2, self.history_rounds * 2)
        del h[:-keep]
        self._save_history()

    def _save_history(self):
        d = read_memory()          # 先读最新，避免覆盖网页刚改的人设/记忆
        if str(d.get("profile") or "") != str(self.mem.get("profile") or ""):
            # 回复途中卡片被切走了：手上这份历史属于上一个人格，别写回去
            self.mem = d
            self.load_history()
            return
        d["history"] = self._history
        write_memory(d)
        self.mem = d

    # ---- 看图 ----
    def _vision(self, blocks, timeout=20):
        """把「一段字 + 一张图」扔给识图模型，回它看见的东西。
        默认和 _chat 同一条线路（同 url / token），只是模型名可用 visionModel 单独指定。"""
        body = json.dumps({"model": self.vision_model,
                           "messages": [{"role": "user", "content": blocks}],
                           "max_tokens": 280, "temperature": 0.3,
                           "stream": False}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.vision_url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self.vision_token,
        })
        t0 = time.time()
        with NO_PROXY.open(req, timeout=max(8, timeout)) as r:
            d = json.loads(r.read().decode("utf-8"))
        ch = (d.get("choices") or [{}])[0]
        usage_write("看图", self.vision_model, d.get("usage") or {}, time.time() - t0)
        return ((ch.get("message") or {}).get("content") or "").strip()

    @staticmethod
    def _inline_image(url):
        """自己把图下下来，编成 data: 塞进请求里。
        服务端取图偶尔会失败（大图、超时、图床拦了它的抓取器），这是兜底的那条路。"""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with NO_PROXY.open(req, timeout=20) as r:
                data = r.read(9 * 1024 * 1024)     # 9MB 以上的图基本都是整张截图，模型也只是缩着看
                ctype = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0]
        except Exception as e:
            log("下载图片失败 %s: %s" % (str(url)[:60], str(e)[:80]), "WARN")
            return ""
        if not data:
            return ""
        if not ctype.startswith("image/"):
            ctype = "image/jpeg"
        return "data:%s;base64,%s" % (ctype, base64.b64encode(data).decode())

    def describe_image(self, url, timeout=20):
        """看一眼这张图，回一句话。看不了就回空串，调用方自己决定怎么交代。"""
        url = str(url or "").strip()
        if not url.startswith("http"):
            return ""
        cache = img_cache_load()
        if cache.get(url):
            return cache[url]
        blocks = [{"type": "text", "text": IMG_DESC_PROMPT},
                  {"type": "image_url", "image_url": {"url": url, "detail": "low"}}]
        txt = ""
        try:
            txt = self._vision(blocks, timeout)
        except Exception as e:
            log("看图失败（直传地址）: %s" % str(e)[:100], "WARN")
        if not txt:
            inline = self._inline_image(url)
            if inline:
                blocks[1] = {"type": "image_url", "image_url": {"url": inline, "detail": "low"}}
                try:
                    txt = self._vision(blocks, timeout)
                except Exception as e:
                    log("看图失败（内联）: %s" % str(e)[:100], "WARN")
        txt = re.sub(r"\s+", " ", (txt or "")).strip()
        if len(txt) > 200:
            txt = txt[:200]
        if txt:
            img_cache_put(url, txt)
        return txt

    # ---- 模型调用 ----
    def _chat(self, msgs, max_tokens=None, temperature=None, scene="聊天", who=""):
        body = json.dumps({"model": self.model, "messages": msgs,
                           "max_tokens": max_tokens or self.max_tokens,
                           "temperature": self.temperature if temperature is None else temperature,
                           "stream": False}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self.token,
        })
        t0 = time.time()
        with NO_PROXY.open(req, timeout=self.timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        ch = (d.get("choices") or [{}])[0]
        out = (ch.get("message") or {}).get("content", "") or ""
        # length = 撞到 max_tokens，模型的话本身就没写完
        self.last_finish = ch.get("finish_reason") or ""
        usage = d.get("usage") or {}
        usage_write(scene, self.model, usage, time.time() - t0, who)
        return out.strip(), usage.get("total_tokens", 0)

    def _chat_tools(self, msgs, tools, max_tokens=None, temperature=None, scene="聊天", who=""):
        """带工具的调用：返回 (正文, tool_calls, tokens)。
        tools 用 OpenAI 那套格式，DeepSeek 的 chat 接口兼容它。
        这里不重试、不兜底——由调用的地方决定「没答案怎么办」。"""
        payload = {"model": self.model, "messages": msgs,
                   "max_tokens": max_tokens or self.max_tokens,
                   "temperature": self.temperature if temperature is None else temperature,
                   "stream": False}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        req = urllib.request.Request(self.url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self.token,
        })
        t0 = time.time()
        with NO_PROXY.open(req, timeout=self.timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        ch = (d.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        out = (msg.get("content") or "") or ""
        calls = msg.get("tool_calls") if isinstance(msg.get("tool_calls"), list) else []
        self.last_finish = ch.get("finish_reason") or ""
        usage = d.get("usage") or {}
        usage_write(scene, self.model, usage, time.time() - t0, who)
        return out.strip(), calls, usage.get("total_tokens", 0)

    def _chat_think(self, msgs, max_tokens=None, who=""):
        """真·思考：同一个接口、同一个 Key，只把模型换成 deepseek-reasoner。
        它会先吐一段思维链（reasoning_content），再把话写出来——那段就是「真·思考」。
        reasoner 不吃 temperature（传了也不生效），所以这里干脆不带这个字段；
        出字比 chat 慢，超时给到至少 120 秒。
        注意：思维链的 token 是算在 max_tokens 里的（实测 320 里能被「想」吃掉大半，
        结果正文空的），所以上限必须放宽。放宽只是允许它多想，不是多花钱——
        账单按实际吐出来的 token 算。"""
        body = json.dumps({"model": self.think_model, "messages": msgs,
                           "max_tokens": max(1024, min(4096, (max_tokens or self.max_tokens) * 4)),
                           "stream": False}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self.token,
        })
        t0 = time.time()
        with NO_PROXY.open(req, timeout=max(120, self.timeout)) as r:
            d = json.loads(r.read().decode("utf-8"))
        ch = (d.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        out = (msg.get("content") or "") or ""
        cot = (msg.get("reasoning_content") or "") or ""
        self.last_finish = ch.get("finish_reason") or ""
        usage = d.get("usage") or {}
        usage_write("真思考", self.think_model, usage, time.time() - t0, who)
        return out.strip(), usage.get("total_tokens", 0), cot.strip()

    # ---- 真·思考：开关、这一轮要不要想、每小时还剩几次 ----
    def think_left(self):
        """这一小时还能真想几次。掉出窗口的记录顺手清掉，不留着涨。"""
        if not self.think_cap:
            return 9999
        now = time.time()
        with self._think_lock:
            self._think_used = [t for t in self._think_used if now - t < 3600]
            return max(0, self.think_cap - len(self._think_used))

    def _think_spend(self):
        with self._think_lock:
            self._think_used.append(time.time())

    def should_think(self, text):
        """auto 模式只在「这话值得琢磨」时才换贵模型：长句、在问为什么/怎么办、
        带着质问或前后矛盾。寒暄、嗯啊、表情包这类一律走便宜的那条线。"""
        if not self.think_on:
            return False
        if self.think_left() <= 0:
            return False
        if self.think_mode == "always":
            return True
        t = str(text or "")
        score = 0
        if len(t) >= 24:
            score += 1
        if len(t) >= 60:
            score += 1
        if "？" in t or "?" in t:
            score += 1
        if any(k in t for k in ("为什么", "为啥", "怎么办", "怎么回", "该不该", "要不要",
                               "是不是", "你觉得", "你说", "凭什么", "到底", "如果",
                               "解释", "什么意思")):
            score += 1
        # 被追问、被翻旧账、被说「你刚才不是说」——这种要想清楚再接，否则前后打架
        if any(k in t for k in ("你刚才", "你上次", "不是说", "明明", "你骗", "你又说")):
            score += 1
        return score >= 2

    def reply(self, gid, group_name, speaker, text, uid=None, is_dm=False,
              recent=None, recall=None):
        t0 = time.time()
        # 记账用的对象名：同一个「聊天」功能，能看出是哪个群/哪个人花的
        _who = (("私聊·" if is_dm else "群·")
                + (group_name or speaker or str(gid) or "未知"))
        # 思考流：这一轮它实际做了什么。不是模型的脑内独白（deepseek-chat 不给），
        # 是「看见了什么 → 注入了什么 → 写完自查说了什么」的白描，句句都有出处。
        # 「收到」那行不再重复引用原文——被引用的那句单独在 steps 里，看着更清楚。
        in_txt = text or ""
        _m0 = re.match(r"^（引用[^）]*）\s*", in_txt)
        if _m0:
            in_txt = in_txt[_m0.end():]
        trace = {"t": t0, "kind": "reply", "gid": str(gid), "gname": group_name or "",
                 "who": speaker or "", "uid": str(uid or ""), "is_dm": bool(is_dm),
                 "model": self.model, "in": _one_line(in_txt, 200), "steps": []}
        st = trace["steps"]
        sysmsg = self.system_prompt(gid, uid, speaker, text, lean=self.agent_loop)
        # 长度交回给人设。她设定里本来写着「回答长度随情境自然变化：日常寒暄简短克制，
        # 有兴趣可以稍长」——所以这里不再按 0.7 压成「只许写这么多」，
        # 只报一个硬上限当兜底。压太狠的结果是每句都只能憋结论，像客服不像人。
        cap = self.dm_max_chars if is_dm else self.max_chars
        if cap:
            sysmsg += ("\n\n【一条消息的规矩】你一次只发一条，说完就收住。"
                       "长短随你自己：随口应一句就一两句，对方认真问你、或者你本来就有话要说，"
                       "就讲完整些，不用硬压短。别铺陈成长段落、别列条目、别自己接自己往下说。"
                       "硬上限 %d 字，超了会被截掉。" % cap)
        # 场合和说话人只说一次。以前是每句话前面都套「（会话：…｜说话人：…）」，
        # 结果模型被反复指向群名，接不住上下文——对方只回一两个字的答话时，
        # 它会当成新话题（实测「你」3/3 接错），还会莫名其妙去聊群名。
        who = speaker or ""
        if uid:
            who = "%s（UID:%s）" % (who, uid)
        if is_dm:
            sysmsg += "\n\n【现在的场合】你在和 %s 私聊，下面的话都是他说的。" % (who or "对方")
        else:
            sysmsg += "\n\n【现在的场合】你在群「%s」里，这一句是 %s 说的。" % (group_name or "", who or "群友")
        sysmsg += ("\n\n【怎么接话】对方说话很随意，常常只有一两个字、没有主语，"
                   "也可能是在回答你上一句问的话。遇到这种，先把它接到你自己上一句上理解，"
                   "别当成新话题、也别猜成别的东西；真没听懂，就老实问一句指的是什么。")
        # 群里刚才在聊什么：以前它只在被 @ 时看见那一句，像个刚推门进来的人
        if not is_dm and recent:
            lines = _talk_lines(recent[-12:])
            if lines:
                sysmsg += ("\n\n【群里刚才在聊】这些是你自己在群里看见的，时间从早到晚。"
                           "你被叫到时接着这里的话说，别装作没这回事：\n"
                           + "\n".join(lines)
                           + "\n（你正要回的那一句还没写进来，它在你下面这条消息里）")
        if self.agent and recall:
            lines = _talk_lines(recall)
            if lines:
                sysmsg += ("\n\n【你翻到的旧记录】你往前翻了翻更早的聊天，找到这几句跟现在有关：\n"
                           + "\n".join(lines)
                           + "\n不一定都对得上，你自己看；要用就自然地带一句，像你自己还记得，"
                           "别照念、别说是查记录查出来的。")
        # 心情：只在这一轮真的轮到时才给它一句，且同一变化只说一次
        if self.mood_on:
            line = self.mood_prompt()
            if line:
                sysmsg += "\n\n" + line
        st.extend(self._trace_steps(text, sysmsg, recent, recall))
        if cap:
            st.append("按 %d 字上限收着说（只是天花板，不催她写满）" % cap)
        msgs = [{"role": "system", "content": sysmsg}]
        # 独立会话：这条线只属于「你和他」，群里别人插的话不算进来（群里的近况
        # 已经在上面【群里刚才在聊】单独给过了）。关着就还是按会话拼上下文。
        if self.agent_loop:
            msgs += self.peer_history(uid, gid, is_dm)
        else:
            msgs += self.history(gid)
        msgs.append({"role": "user", "content": text})
        # 联网搜索：只在开关开、且有 Key、且模型判断「这句该查」时才搜，避免平时聊天烧额度。
        if self.search_on and self.search_key:
            query = self._want_search(text, recent)
            if query:
                found = self._search_web(query)
                if found:
                    st.append("判断这句得联网 → 查「%s」→ 拿到 %d 条"
                              % (_one_line(query, 40), found.count("\n") + 1))
                    sysmsg += ("\n\n【你刚联网查到的】关于「%s」的最新结果，按时间从新到旧：\n%s\n"
                               "用这些结果回答，重点说最新、最要紧的；说的时候自然点，"
                               "别念「根据搜索结果」，也别贴网址。" % (query, found))
                    msgs[0] = {"role": "system", "content": sysmsg}
        cot = ""
        if self.agent_loop:
            # 下面这套：想 → 调工具 → 再看。查到什么都会记进面板的思考流
            out, tok, stepped = self._agent_loop(msgs, uid=uid, gid=gid,
                                                 speaker=speaker, who=_who, text=text)
            for x in stepped:
                st.append("自己去查了 %s" % x)
        elif self.should_think(text):
            self._think_spend()
            try:
                out, tok, cot = self._chat_think(msgs, who=_who)
            except Exception as e:
                # 想不成也不能哑掉：退回普通模型，照常说这句
                log("真思考没成，退回普通模型: %s" % str(e)[:140], "WARN")
                st.append("想的时候出了岔子，退回普通模型答的")
                out, tok = self._chat(msgs, who=_who)
            if cot:
                st.append("真想了 %d 字（%s），才动笔" % (len(cot), self.think_model))
            if not out:
                # 只在脑子里想完了、嘴上没说出来（预算被思维链吃光）时兜一下
                st.append("想完没来得及说，换普通模型补一句")
                out, tok2 = self._chat(msgs, who=_who)
                tok += int(tok2 or 0)
        else:
            out, tok = self._chat(msgs, who=_who)
        redone = False
        if self.agent and not self.agent_loop and out:
            why = self._redo_reason(gid, text, out, cap)
            if why:
                st.append("写完自查：%s → 重说了一遍" % why)
                msgs2 = msgs + [
                    {"role": "assistant", "content": out},
                    {"role": "user", "content":
                     "（自查：这句%s。重说一句，接上刚才的话，别再反问。）" % why}]
                out2, tok2 = self._chat(msgs2, temperature=max(0.55, self.temperature - 0.25),
                                        who=_who)
                if out2 and out2.strip():
                    out, tok = out2.strip(), tok + tok2
                    redone = True
        # 语感闸门：发出去之前拿她最近说过的原话比一遍。上面那步刚重说过就跳过——
        # 一句话被两道闸门连着改，第三次往往就改得不像她了。
        if self.style_gate and out and not redone:
            why = self._style_reason(out, gid)
            if why:
                st.append("语感闸门：%s → 重说了一遍" % why)
                msgs2 = msgs + [
                    {"role": "assistant", "content": out},
                    {"role": "user", "content":
                     "（这句%s。重说一遍：意思不变，换个开头、换个说法，"
                     "别再用刚才那个句式。）" % why}]
                out2, tok2 = self._chat(msgs2,
                                        temperature=min(1.4, self.temperature + 0.25), who=_who)
                if out2 and out2.strip():
                    out, tok = out2.strip(), tok + tok2
                    again = self._style_reason(out, gid)
                    if again:
                        # 只拦一次：再改下去她会为了「不一样」把话说歪，那更糟
                        log("语感闸门：重说后还是像（%s），照发" % again)
                else:
                    log("语感闸门：重说没出声，用原来那句", "WARN")
        if self.last_finish == "length" and out:
            st.append("话没说完就撞了长度上限")
        # 说完这一轮再判心情：先有话说，才谈得上说完心里怎么动
        if self.mood_on and out:
            self._mood_turn += 1
            if self.mood_due():
                # 先把「判过了」记下来：模型失败也不在同一句上反复重试
                self._mood_turn = 0
                self._mood_last = time.time()
                mood_tick_write(0, self._mood_last)
                self.judge_mood(text, out)
            else:
                mood_tick_write(self._mood_turn, self._mood_last)
        trace.update({"out": _one_line(out, 320), "tokens": int(tok or 0),
                      "ms": int((time.time() - t0) * 1000),
                      "finish": self.last_finish or ""})
        # 真思考的原文给面板。太长就截一段——面板是给人扫一眼的，不是存档
        if cot:
            trace["cot"] = cot[:1800]
            trace["think_model"] = self.think_model
        think_write(trace)
        # 独立会话要自己记一笔：跟这个人的下一轮，从这里接着往下说
        # 面板试聊例外（no_peer_write）：那是我在调试，不该让它变成她跟主人真说过的话
        if self.agent_loop and out and not getattr(self, "no_peer_write", False):
            self.peer_remember(uid, gid, is_dm, text, out)
        return out, tok

    def _trace_steps(self, text, sysmsg, recent, recall):
        """把「它这一轮看见了什么、收到的是什么」翻成几句人话，给面板的思考流用。
        全部从已经拼好的提示词和正文里提取——不额外花一次调用，也不编造。"""
        out = []
        t = text or ""
        q = re.search(r"（引用[^）]{0,26}?说的：「([^」]{1,90})」）", t)
        qim = re.search(r"（引用([^）]{1,26}?)发的(图片|表情包)[：）]", t)
        if q:
            out.append("看出这是在回某句话：「%s」" % _one_line(q.group(1), 54))
        if qim:
            out.append("看出这是在回%s发的一张%s" % (_one_line(qim.group(1), 16), qim.group(2)))
        im = re.search(r"（发了张图：([^）]{0,90})）", t)
        if im:
            out.append("先看了这张图：%s" % _one_line(im.group(1), 50))
        else:
            im = re.search(r"（发了张表情包：「([^」]{0,60})」）", t)
            if im:
                out.append("认出了这个表情包：%s" % _one_line(im.group(1), 44))
        if recent:
            out.append("翻了群里最近 %d 句" % min(len(recent), 12))
        if recall:
            out.append("从旧记录里翻到 %d 句相关的" % len(recall))
        m = re.search(r"【你心里这会儿】([^\n。]{0,70})", sysmsg)
        if m:
            out.append("心里这会儿：%s" % _one_line(m.group(1), 44))
        a = re.search(r"【你对他】([^\n。]{0,40})", sysmsg)
        if a:
            feel = a.group(1)
            if "：" in feel:
                feel = feel.split("：", 1)[1]      # 只留感觉，名字已经在上面那行了
            out.append("对这人的感觉：%s" % _one_line(feel, 30))
        if "【群友档案】" in sysmsg:
            out.append("翻了群友档案")
        return out

    def _redo_reason(self, gid, text, draft, cap=None):
        """智能体模式的自查：这句是不是没接上话、在重复、或者太长。
        返回 None 就不重说——平时不额外多花一次调用。"""
        d = str(draft or "").strip()
        if not d:
            return None
        cap = self.max_chars if cap is None else cap
        if cap and len(d) > cap * 1.2:
            return "太长了，一条消息说不完"
        last_bot = ""
        for x in reversed(self.history(gid)):
            if x.get("role") == "assistant":
                last_bot = str(x.get("content") or "")
                break
        plain = re.sub(r"\[[^\]]*\]", "", d).strip()
        if last_bot and plain and plain == re.sub(r"\[[^\]]*\]", "", last_bot).strip():
            return "跟上一条说的一模一样"
        # 对方只丢了一两个字、自己上一句又是问句，这句却还在反问 → 典型答非所问。
        # 注意历史里的回复尾巴带着表情指令（[cube_…]），判断问号前要先剥掉，
        # 否则永远不算「问句结尾」。
        short = len(re.sub(r"\s+", "", str(text or ""))) <= 6
        last_plain = re.sub(r"\[[^\]]*\]", "", last_bot).strip()
        if short and last_plain.endswith(("？", "?", "呢")) and plain.endswith(("？", "?")):
            return "对方像是在回答你上一句，你却还在反问"
        return None

    def bubble(self, gid, group_name, hint="", recent=None, quiet_min=0):
        """冷场冒泡：没人跟它说话，由它自己起个话头。
        hint 是面板里填的方向；留空就靠日记、群友档案和现在的时间自己想。"""
        now = datetime.now()
        week = "一二三四五六日"[now.weekday()]
        h = now.hour
        tod = ("凌晨" if h < 5 else "清晨" if h < 8 else "上午" if h < 11 else
               "中午" if h < 13 else "下午" if h < 18 else "傍晚" if h < 19 else
               "晚上" if h < 23 else "深夜")
        stamp = "%d年%d月%d日 星期%s %02d:%02d（%s）" % (
            now.year, now.month, now.day, week, h, now.minute, tod)
        sysmsg = self.system_prompt(gid)
        cap = self.max_chars
        sysmsg += ("\n\n【现在的时间】%s。你知道今天是几号、星期几、大概什么时段，"
                   "需要的话可以拿它当话头，但别生硬地报时间。\n\n"
                   "【这次没人叫你】群里已经安静了一阵子，你想自己开口说句话，把气氛带起来。规矩：\n"
                   "- 就像平时在群里随口聊天，不许用「在吗」「有人吗」这类招呼开场\n"
                   "- 不要 @ 任何人，也不要提「我先说一句」「冷场了」「打破沉默」这类话\n"
                   "- 只发一条，%s说完就收住，别铺陈\n"
                   "- 话头可以从你的日记、最近聊过的事、今天的时间和季节里挑，"
                   "也可以随手说说自己的小事或者吐槽两句\n"
                   "- 别重复你最近说过的话" %
                   (stamp, ("硬上限 %d 字，" % cap) if cap else ""))
        usermsg = "会话：%s\n" % (group_name or "")
        if quiet_min > 0:
            usermsg += "距离上一条消息已经过了 %d 分钟。\n" % quiet_min
        if hint:
            usermsg += "这次想聊的方向（按这个来，但别说成念题目）：%s\n" % hint
        if recent:
            usermsg += "群里最近说过的话（别重复）：\n" + "\n".join(recent) + "\n"
        usermsg += "现在开口，只输出你要发的那一句话。"
        # 前几次自己起过的话头：既是语气样本，也是「这些话题已经说过了」的清单
        prev = [] if hint else self.proactive_lines(gid, 3)
        samp = self.proactive_samples(gid) if prev else ""
        if samp:
            sysmsg += "\n\n" + samp
        txt, tok = self._chat([{"role": "system", "content": sysmsg},
                               {"role": "user", "content": usermsg}],
                              temperature=min(1.3, self.temperature + 0.15), scene="冷场冒泡",
                              who="群·" + (group_name or str(gid)))
        t = (txt or "").strip()
        if t and prev:
            why = self._topic_reason(t, prev)
            if why:
                t2, tok2 = self._chat(
                    [{"role": "system", "content": sysmsg},
                     {"role": "user", "content": usermsg +
                      "\n\n（刚才你在心里想的是：%s —— 这句%s，作废，一个字都别用。"
                      "重新挑一件与它不相干的事：别聊时间、别聊谁睡没睡、"
                      "别讲自己正在收拾什么；就说一件具体的小事，或者问到某个具体的人。）"
                      % (t, why)}],
                    temperature=min(1.4, self.temperature + 0.3), scene="冷场冒泡·换话头",
                    who="群·" + (group_name or str(gid)))
                if (t2 or "").strip():
                    t, tok = t2.strip(), int(tok or 0) + int(tok2 or 0)
                log("冒泡撞话头：%s → 换了个说法「%s」" % (why, _one_line(t, 30)))
        return t, tok

    def group_ping(self, gid, group_name, hint="", recent=None, quiet_min=30,
                   last_said="", material=""):
        """群聊主动：在你点名的群里，到点自己起个话头。

        material 是留给「帖子」的口子：以后刷到帖子，把内容传进来，
        它就会照着看完的感受说话，而不是凭空起话头（现在先留空）。
        """
        now = datetime.now()
        week = "一二三四五六日"[now.weekday()]
        h = now.hour
        tod = ("凌晨" if h < 5 else "清晨" if h < 8 else "上午" if h < 11 else
               "中午" if h < 13 else "下午" if h < 18 else "傍晚" if h < 19 else
               "晚上" if h < 23 else "深夜")
        stamp = "%d年%d月%d日 星期%s %02d:%02d（%s）" % (
            now.year, now.month, now.day, week, h, now.minute, tod)
        sysmsg = self.system_prompt(gid)
        cap = self.max_chars
        rules = ["- 就像平时在群里随口说话，不许用「在吗」「有人吗」这类招呼开场",
                 "- 不要 @ 任何人，也不要说「我冒个泡」「随便说一句」「冷场了」这种话",
                 "- 只发一条，%s说完就收住，别铺陈、别连着问好几个问题" %
                 (("硬上限 %d 字，" % cap) if cap else ""),
                 "- 别重复你最近说过的话"]
        if material:
            rules.insert(0, "- 你刚看到一段内容，就用「看完之后的感受」开口："
                            "说自己的反应、联想到的事，别复述原文、别念标题、别像在转述新闻")
        elif hint:
            rules.insert(0, "- 这次按主人给的方向说（别把它念成题目）")
        else:
            rules.insert(0, "- 话头从你的日记、最近群里聊过的事、今天的时间和季节里挑，"
                            "也可以说说自己的小事")
        sysmsg += ("\n\n【现在的时间】%s。\n\n"
                   "【这次没人叫你】群里没人喊你，是你自己想开口说句话。规矩：\n%s" % (
                       stamp, "\n".join(rules)))
        usermsg = "群：%s\n" % (group_name or "")
        if quiet_min > 0:
            usermsg += "群里上一条消息是 %d 分钟前。\n" % quiet_min
        if last_said:
            usermsg += "你上次在群里主动说的是：「%s」——这次换个话头。\n" % last_said
        if hint and not material:
            usermsg += "想聊的方向：%s\n" % hint
        if material:
            usermsg += "你刚看到的内容：\n%s\n说说你看到它的感受。\n" % material
        if recent:
            usermsg += "群里最近说过的话（别重复）：\n" + "\n".join(recent[-6:]) + "\n"
        usermsg += "现在开口，只输出你要发的那一句话。"
        prev = [] if (hint or material) else self.proactive_lines(gid, 3, kind="gping")
        samp = self.proactive_samples(gid, "gping") if prev else ""
        if samp:
            sysmsg += "\n\n" + samp
        txt, tok = self._chat([{"role": "system", "content": sysmsg},
                               {"role": "user", "content": usermsg}],
                              temperature=min(1.3, self.temperature + 0.12), scene="主动找群",
                              who="群·" + (group_name or str(gid)))
        t = (txt or "").strip()
        if t and prev:
            why = self._topic_reason(t, prev)
            if why:
                t2, tok2 = self._chat(
                    [{"role": "system", "content": sysmsg},
                     {"role": "user", "content": usermsg +
                      "\n\n（刚才你在心里想的是：%s —— 这句%s，作废，一个字都别用。"
                      "重新挑一件与它不相干的事：别聊时间、别聊谁睡没睡、"
                      "别讲自己正在收拾什么；就说一件具体的小事，或者问到某个具体的人。）"
                      % (t, why)}],
                    temperature=min(1.4, self.temperature + 0.3), scene="主动找群·换话头",
                    who="群·" + (group_name or str(gid)))
                if (t2 or "").strip():
                    t, tok = t2.strip(), int(tok or 0) + int(tok2 or 0)
                log("群主动撞话头：%s → 换了个说法「%s」" % (why, _one_line(t, 30)))
        return t, tok

    def dm_ping(self, gid, peer_name, uid=None, quiet_min=180, ignored=0,
                last_said="", hint="", recent=None):
        """私聊主动找人：不是回复，是它自己想开口。

        quiet_min 是这段私聊安静了多久；ignored 是这次之前已经被晾了几回
        （0 = 第一次找他，1 = 上次找了他没回，这次算补一句）。
        """
        now = datetime.now()
        week = "一二三四五六日"[now.weekday()]
        h = now.hour
        tod = ("凌晨" if h < 5 else "清晨" if h < 8 else "上午" if h < 11 else
               "中午" if h < 13 else "下午" if h < 18 else "傍晚" if h < 19 else
               "晚上" if h < 23 else "深夜")
        stamp = "%d年%d月%d日 星期%s %02d:%02d（%s）" % (
            now.year, now.month, now.day, week, h, now.minute, tod)
        sysmsg = self.system_prompt(gid, uid, peer_name)
        cap = self.dm_max_chars
        rules = ["- 这是私聊不是群里，语气更放松、更私密一点，像平时跟他讲话那样",
                 "- 叫他按档案里的名字或你们平时的叫法，别把自己的名字当成对他的称呼",
                 "- 别提「好久没聊」「你怎么不理我」「在吗」「有人吗」这类话，"
                 "也别解释自己为什么突然开口",
                 "- 只发一条，%s说完就收住，别一口气问好几个问题" %
                 (("硬上限 %d 字，" % cap) if cap else ""),
                 "- 话头从你们之前聊过的事、你自己的小事、今天的时间和天气里挑，"
                 "别重复你上次主动说过的话"]
        if ignored >= 1:
            rules.insert(0, "- 上一次你主动找过他，他没回。这次可以更黏一点、更直接一点，"
                            "撒个娇也行，但不许抱怨、不许质问、不许让他觉得欠你什么")
        sysmsg += ("\n\n【现在的时间】%s。\n\n"
                   "【这次没人叫你】你和「%s」的私聊已经安静了 %d 分钟，"
                   "你想主动找他说话，是他没找你、你自己想起来的。规矩：\n%s" % (
                       stamp, peer_name or "他", int(quiet_min), "\n".join(rules)))
        usermsg = "和%s的私聊。\n" % (peer_name or "他")
        if last_said:
            usermsg += "你上次主动找他说的是：「%s」——这次换个话头，别又接着聊同一件事。\n" % last_said
        if recent:
            usermsg += "你们最近说过的话：\n" + "\n".join(recent[-6:]) + "\n"
        if hint:
            usermsg += "这次想往这个方向说（按这个来，但别说成念题目）：%s\n" % hint
        usermsg += "现在开口，只输出你要发的那一句话。"
        prev = [] if hint else self.proactive_lines(gid, 3, kind="dping")
        samp = self.proactive_samples(gid, "dping") if prev else ""
        if samp:
            sysmsg += "\n\n" + samp
        txt, tok = self._chat([{"role": "system", "content": sysmsg},
                               {"role": "user", "content": usermsg}],
                              temperature=min(1.3, self.temperature + 0.1), scene="主动找人",
                              who="私聊·" + (peer_name or str(gid)))
        t = (txt or "").strip()
        if t and prev:
            why = self._topic_reason(t, prev)
            if why:
                t2, tok2 = self._chat(
                    [{"role": "system", "content": sysmsg},
                     {"role": "user", "content": usermsg +
                      "\n\n（刚才你在心里想的是：%s —— 这句%s，作废，一个字都别用。"
                      "换成问他另一件事，或者说说你自己别的什么；"
                      "别又聊时间、别又讲你在做什么。）" % (t, why)}],
                    temperature=min(1.4, self.temperature + 0.28), scene="主动找人·换话头",
                    who="私聊·" + (peer_name or str(gid)))
                if (t2 or "").strip():
                    t, tok = t2.strip(), int(tok or 0) + int(tok2 or 0)
                log("私聊主动撞话头：%s → 换了个说法「%s」" % (why, _one_line(t, 30)))
        return t, tok

    def parse_command(self, text, sessions, has_img=False):
        """把主人随口一句私聊翻成一条能在群里执行的动作。
        返回 {"act": say|image|bubble|emoji|none, "target": ..., "content": ...}
        认不出、或者压根只是聊天，一律 act=none，交给正常回复。"""
        roster = "\n".join(
            "- %d. %s%s" % (i + 1, s.get("name") or s.get("gid"),
                            "" if s.get("kind") == "group" else "（这是私聊会话，不是群）")
            for i, s in enumerate(sessions or []))
        sysmsg = ("你在替主人跑腿，用他自己的账号。听他一句话，判断他是不是要你去某个会话里做件事。\n"
                  "能做的只有四种：\n"
                  "1. say：把一段指定的话原样发出去（用他的原话，不要你改写、不要加解释、不要带引号）；\n"
                  "2. image：发一张图（content 里放图片地址或电脑路径，他没有就留空）；\n"
                  "3. bubble：不指定内容，由你自己想一句话说（他只说“去群里说点什么”“活跃一下气氛”这种）；\n"
                  "4. emoji：从收藏里挑一个表情包发出去。\n"
                  "如果他只是跟你聊天、提问、吐槽、问你的看法，act 一律 none。这一条最要紧：\n"
                  "平时聊天里出现“发”“说”“图”这些字太常见，不要看见动词就当指令。\n"
                  "判断标准只有一条：他是不是在让你替他去别的会话里做一个动作。拿不准就 none。\n"
                  "target 只能从「能发到」里挑名字或序号；他没点名、而能发的只剩一个时可以填那一个，"
                  "否则留空（空着我再去问他）。\n"
                  "content 是要发的那段话本身，去掉“帮我发”“跟他说”这类引子；没给具体内容就留空。\n"
                  "只输出一行 JSON，不要解释。例：{\"act\":\"say\",\"target\":\"示例群聊\",\"content\":\"我错了\"}")
        usermsg = "能发到：\n%s\n\n主人说：%s" % (roster or "（现在没有会话）", (text or "").strip())
        if has_img:
            usermsg += ("\n（他刚私聊发过一张图：只有他明确说“发出去 / 转给他 / 把这张图发到某处”时，"
                        "image 的 content 才留空表示转发那张；他要是问这张是谁发的、什么梗、"
                        "什么意思、像什么，那就不是指令，act 一律 none）")
        txt, _ = self._chat([{"role": "system", "content": sysmsg},
                             {"role": "user", "content": usermsg}],
                            max_tokens=220, temperature=0, scene="指令解析")
        i, j = txt.find("{"), txt.rfind("}")
        if i < 0 or j <= i:
            return {"act": "none"}
        try:
            d = json.loads(txt[i:j + 1])
        except Exception:
            return {"act": "none"}
        if not isinstance(d, dict):
            return {"act": "none"}
        act = str(d.get("act") or "none").strip().lower()
        if act not in ("say", "image", "bubble", "emoji"):
            return {"act": "none"}
        return {"act": act,
                "target": str(d.get("target") or "").strip(),
                "content": str(d.get("content") or "").strip()}

    def _trim_diary(self, entries):
        """只留最近 learn_max_notes 篇有日期的日记。无日期的老条目一直留着——
        它们还没被写成日记，删掉就等于失忆，等用户在面板上自己处理。"""
        undated = [e for e in entries if not e.get("d")]
        dated = sorted([e for e in entries if e.get("d")], key=lambda e: e["d"])
        if len(dated) > self.learn_max_notes:
            dated = dated[-self.learn_max_notes:]
        return undated + dated

    def _diary_fresh(self, gid, hist):
        """只取上次写成日记之后的对话。
        靠 learnMark 里那句的指纹找水位：历史被截断、那句已经滚掉了，就退回最后三句，
        宁可少写一点也不把写过的整段再抄一遍。"""
        mark = str((self.mem.get("learnMark") or {}).get(str(gid)) or "")
        start = None
        if mark:
            for i in range(len(hist) - 1, -1, -1):
                if msg_fp(hist[i]) == mark:
                    start = i + 1
                    break
        if start is None:
            start = max(0, len(hist) - 6)
        return hist[start:]

    def self_reflect(self, gid=""):
        """回看这一段，写「关于自己今天多知道了什么」。只有本体模式的卡会跑。
        about=确认 / guess=猜的 / open=原先以为懂其实不懂 / journal=自述。
        返回新增条数，0 = 什么也没长。
        日记记的是「今天发生了什么」，这里记的是「我因此成了谁」——两件事分开。"""
        if not (self.mem or {}).get("selfOwned"):
            return 0
        st = read_self()
        if not st:
            return 0
        tl = self.mem.get("talkLog") or {}
        arr = list(tl.get(str(gid)) or [])
        if len(arr) < 6:          # 这个会话不够，就看她全部的经历（她只有一份人生）
            arr = [x for v in tl.values() if isinstance(v, list) for x in v]
        arr = [x for x in arr if isinstance(x, dict) and (x.get("text") or "").strip()]
        if len(arr) < 4:
            return 0
        convo = "\n".join(_talk_lines(arr[-30:]))
        known = []
        for k, lab in (("about", "确认"), ("guess", "猜"), ("open", "不确定")):
            for x in self_lines(st.get(k), 20):
                known.append("[%s] %s" % (lab, x))
        sysmsg = (
            "你在回看自己刚经历的一段，任务是写【关于你自己】多知道了什么，不是记事。\n"
            "只写这段里真出现过的：别人怎么叫你、他们以为你是什么、"
            "你自己说了哪句让你也意外的话、哪件事你其实不懂。\n"
            "严格输出 JSON，四栏：\n"
            '{"about":["已经能确认的"],"guess":["我猜的，还没证"],'
            '"open":["原先以为懂、现在发现不懂的"],"journal":"一小段自述，80 字内"}\n'
            "规矩：\n"
            "- 每栏最多 3 条，每条 30 字内，用第一人称\n"
            "- 别人说的关于你的话，只能进 guess 或 open，不能进 about\n"
            "- 已有的别重复（下面列了）\n"
            "- 什么新东西都没有，四栏都留空\n"
            "只输出 JSON。")
        usermsg = ("【已有】\n" + ("\n".join(known) if known else "（都还空着）")
                   + "\n\n【这段经历】\n" + convo)
        try:
            txt, _ = self._chat([{"role": "system", "content": sysmsg},
                                 {"role": "user", "content": usermsg}],
                                max_tokens=600, temperature=0.5, scene="自省")
        except Exception as e:
            log("自省调用失败: %s" % e, "WARN")
            return 0
        txt = re.sub(r"^```[^\n]*\n|```$", "", (txt or "").strip(), flags=re.S).strip()
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            return 0
        try:
            got = json.loads(m.group(0))
        except Exception:
            return 0
        added = 0
        for k, lim in (("about", 24), ("guess", 24), ("open", 20)):
            cur = self_lines(st.get(k), lim)
            seen = set(_norm_note(x) for x in cur)
            for x in (got.get(k) or [])[:3]:
                s = str(x or "").strip()[:60]
                if not s or _norm_note(s) in seen:
                    continue
                seen.add(_norm_note(s))
                cur.append(s)
                added += 1
            st[k] = cur[-lim:]
        j = str(got.get("journal") or "").strip()[:160]
        if j:
            jr = st.get("journal") if isinstance(st.get("journal"), list) else []
            jr.append({"d": time.strftime("%Y-%m-%d"), "t": j})
            st["journal"] = jr[-60:]
            added += 1
        if added and write_self(st):
            log("自省：多知道 %d 条（确认%d/猜%d/不确定%d）%s"
                % (added, len(st.get("about") or []), len(st.get("guess") or []),
                   len(st.get("open") or []), ("｜" + j[:40]) if j else ""))
        return added

    def learn(self, gid, group_name):
        """把最近对话写成今天的日记，返回新增字数（0 = 没有值得记的）"""
        hist = self.history(gid)
        entries = self.notes(gid)
        convo = []
        for x in self._diary_fresh(gid, hist)[-12:]:
            who = "群友" if x.get("role") == "user" else "我"
            convo.append("%s: %s" % (who, str(x.get("content") or "")[:120]))
        if not hist:
            # 刚重启过：这轮内存里的对话是空的，但 talkLog 里还留着最近说的话。
            # 关机器人的时候不该因为「这一轮没聊几句」就一个字都不写。
            arr = self.talk.get(str(gid)) or []
            convo = ["%s: %s" % ("我" if x.get("me") else (x.get("name") or "群友"),
                                 str(x.get("text") or "")[:120]) for x in arr[-10:]]
        if len(convo) < 2:
            log("「%s」这段时间没有新对话，日记不重复写" % group_name)
            return 0
        if sum(len(c) for c in convo) < 50:
            log("「%s」这几句太短，等攒够了再写日记" % group_name)
            return 0

        today = datetime.now().strftime("%Y-%m-%d")
        same = [e for e in entries if e.get("d") == today]
        todays = "\n".join(str(e.get("t") or "") for e in same).strip()
        old_text = diary_text(
            [e for e in entries if not e.get("d")]
            + sorted([e for e in entries if e.get("d")], key=lambda e: e["d"])[-5:])

        sysmsg = ("你就是这段对话里的「我」，现在在写自己的日记，不是助手在整理资料。"
                  "第一人称，用你平时的口吻和口癖。\n"
                  "最要紧的一条：日记不是聊天记录。发生了什么事只占一小半，"
                  "剩下的是你【当时的感受】——那句话落到你耳朵里时，你心里怎么动了一下；"
                  "这个人今天给你的感觉跟以前一样，还是有点变了；"
                  "哪句让你在意、让你想笑、让你不太舒服，或者让你觉得他其实挺好的。"
                  "感受可以是你怎么看他、你打算以后怎么对他，"
                  "但绝不能只是「他说……我说……」地转述。\n"
                  "差别在这儿（只是让你看懂，别照抄）：\n"
                  "  别写：他说想看看卡提希娅的照片，我说我没有。\n"
                  "  要写：他又来打听卡提希娅，我嘴上说没有，心里倒有点想笑——"
                  "这人一有空就往我这儿问别人。不过被他这么一提，我也确实有点想她了。\n"
                  "规则：\n"
                  "- 已经写在日记里的不要再重复\n"
                  "- 140-320 字，可以分 1-3 段，每一段都要有你自己的感受；"
                  "不分点、不写小标题、不写日期（日期另外加）\n"
                  "- 提到人直接用名字或你平时叫他的称呼，不要写“用户”“群友”“对方”\n"
                  "- 全是已经记过的事，就只输出 NONE\n"
                  "只输出日记正文，不要任何解释。")
        usermsg = "会话：%s\n\n" % (group_name or "")
        if old_text:
            usermsg += "已有日记（别重复，可以接着写）：\n%s\n\n" % old_text
        if todays:
            usermsg += "今天已经写过：\n%s\n\n" % todays
        usermsg += "最近对话：\n%s" % "\n".join(convo)

        # 已经写过的段落，一会儿按字面查重——模型对「别重复」这条基本不听，
        # 光靠提示词，同一天的日记能被它用不同措辞把同一件事写四五遍。
        seen = [p for p in old_text.split("\n") if len(p.strip()) >= 12]

        body, prev, why = "", "", ""
        dup_try = feel_try = 0
        for _ in range(4):
            s = sysmsg
            u = usermsg
            if why == "dup":
                s += ("\n【上一版被打回了】你写的内容和已有日记高度重复。"
                      "这次只写确实还没记过的事；真没有新的，就只输出 NONE。")
                u += "\n\n下面这段和已有日记重复，一个字都别再写：\n" + prev[:400]
            elif why == "feel":
                s += ("\n【上一版被打回了】你上一版只是在记流水账，只写了发生什么，"
                      "没写你当时的感受。重写：事情压到最短，多写你心里怎么想、"
                      "对这个人什么感觉、哪句话让你在意。")
                u += "\n\n下面这段只有记录、没有感受，别再写成这样：\n" + prev[:400]
            txt, _ = self._chat([{"role": "system", "content": s},
                                 {"role": "user", "content": u}],
                                max_tokens=900, temperature=0.75 if not why else 0.6,
                                scene="写日记")
            body = re.sub(r"^```[^\n]*\n|```$", "", (txt or "").strip(), flags=re.S).strip()
            # 模型会模仿已有日记的格式，把「2026-10-24 周六」这样的日期行也写进正文，
            # 甚至编出明天的日期。日期由系统按今天加，正文里的一律剥掉。
            lines = body.splitlines()
            while lines and DATE_LINE_RE.match(lines[0]):
                lines.pop(0)
            body = "\n".join(lines).strip()
            if not body or body.upper().startswith("NONE") or len(body) < 8:
                log("「%s」这次没有值得写进日记的新内容" % group_name)
                return 0
            if len(body) > 700:
                body = body[:700]
            worst = max([diary_overlap(body, p) for p in seen] or [0.0])
            if worst > 0.30:
                if dup_try >= 2:
                    log("「%s」这段翻来覆去还是写过的事，日记这次跳过" % group_name)
                    return 0
                dup_try += 1
                prev, why = body, "dup"
                log("日记这段和写过的重了 %d%%，重写一次" % round(worst * 100))
                continue
            # 只记了事、没记心情，退回重写一次；第二次还这样也收下——
            # 一篇没滋味的日记，总比这一段聊过的事彻底丢了强。
            if feel_try < 1 and diary_feel(body) < 2:
                feel_try += 1
                prev, why = body, "feel"
                log("「%s」这段只有记录、没写感受，让它重写一次" % group_name)
                continue
            break
        else:
            log("「%s」日记这次没写出可用的内容，跳过" % group_name)
            return 0

        backup_memory()
        if same:
            # 同一天再整理就接着写，不另起一篇——一天写好几次是正常的，但读起来要连贯
            same[0]["t"] = (str(same[0].get("t") or "").rstrip() + "\n" + body).strip()
        else:
            entries.append({"d": today, "t": body, "ts": time.time()})
        entries = self._trim_diary(entries)
        d = read_memory()
        d["diary"] = entries
        d["learnedAt"] = time.time()
        if hist:
            # 记下「写到哪一句」，下次只从这句之后接——这是重复的根子，别省
            mk = msg_fp(hist[-1])
            marks = d.get("learnMark")
            if not isinstance(marks, dict):
                marks = {}
            marks[str(gid)] = mk
            d["learnMark"] = marks
        write_memory(d)
        sync_card({"diary": entries})
        self.mem = d
        log("日记已写「%s」：+%d 字，共 %d 篇" % (
            group_name, len(body), sum(1 for e in entries if e.get("d"))))
        return len(body)

    def reflect_outlook(self):
        """把这几天的日记收成一段「总观」——它怎么看人、怎么看事。
        只重写正文，行首 // 或 # 的备注原样留在末尾：那是人写的，不该被模型吃掉。
        返回改写后的字数，0 = 没动（没材料 / 模型返回不可用 / 内容没变）。"""
        d = read_memory()
        cur = str(d.get("outlook") or "")
        notes = [ln.rstrip() for ln in cur.splitlines()
                 if ln.strip().startswith(("//", "#"))]
        body_now = strip_notes(cur).strip()

        entries = norm_diary(d.get("diary"))
        dated = sorted([e for e in entries if e.get("d")], key=lambda e: e.get("d"))
        material = diary_text((dated[-6:] if dated else entries[-6:])).strip()
        if len(material) < 30:
            log("日记还太少，总观先不动")
            return 0

        sysmsg = ("你就是这本日记里的「我」。现在不是写日记，是把这些天的事在心里过一遍，"
                  "说自己对人对事的基本看法——这是你自己的态度，不是给谁看的总结。\n"
                  "规则：\n"
                  "- 第一人称，用你平时说话的口气和口癖，不要「作为一个 AI」这类话\n"
                  "- 150-400 字，两三段，不分点、不写小标题、不写日期\n"
                  "- 写「我怎么看」：人跟人是怎么回事、什么值得、什么不值得、自己是什么样的人\n"
                  "- 看法要有日记里真发生过的事撑着，不要空喊，也不要复述日记\n"
                  "- 旧的想法可以参考、可以改，但不要整段照抄\n"
                  "只输出正文，不要任何解释。")
        usermsg = ""
        if body_now:
            usermsg += "你现在（旧的）想法：\n%s\n\n" % body_now
        usermsg += "这些天的日记：\n%s" % material

        txt, _ = self._chat([{"role": "system", "content": sysmsg},
                             {"role": "user", "content": usermsg}],
                            max_tokens=800, temperature=0.7, scene="总观")
        body = re.sub(r"^```[^\n]*\n|```$", "", (txt or "").strip(), flags=re.S).strip()
        lines = body.splitlines()
        while lines and DATE_LINE_RE.match(lines[0]):
            lines.pop(0)
        body = "\n".join(lines).strip()
        if len(body) < 40:
            log("总观这次没写出像样的东西，保留原来的")
            return 0
        if len(body) > 1200:
            body = body[:1200]
        if body == body_now:
            log("总观和原来一样，不用改")
            return 0

        new = body + (("\n\n" + "\n".join(notes)) if notes else "")
        backup_memory()
        d2 = read_memory()
        d2["outlook"] = new
        d2["outlookAt"] = time.time()
        write_memory(d2)
        sync_card({"outlook": new})
        self.mem = d2
        log("总观已改写：%d 字%s" % (len(body), "（备注保留 %d 行）" % len(notes) if notes else ""))
        return len(body)

    # ---- 按 UID 整理群友档案 ----
    HIST_UID_RE = re.compile(r"^\[(.*?)\|(\d+)\]\s*(.*)$", re.S)

    def _persona_brief(self, limit=900):
        """人设摘要：改写档案时用来对齐称呼和口吻。太长就退到句末，不切半句。"""
        t = re.sub(r"\s+", " ", self.persona() or "").strip()
        if len(t) <= limit:
            return t
        cut = max(t.rfind(c, 0, limit) for c in "。！？!?\n")
        return t[:cut + 1] if cut >= limit // 2 else t[:limit]

    def learn_users(self, gid, group_name, convo=None):
        """按 UID 整理群友档案。convo 为空时退回用对话历史（旧版历史没有说话人，会被跳过）。

        面板「结合日记改写档案」开着（mem["usersRewrite"]，默认开）时：日记也读进来，
        按当前人设的口吻把已有条目合并、更新、删掉过期的，模型输出的是这个人的
        【完整档案】，整份替换。关掉就退回老做法：只从最近群聊追加没写过的新条目。"""
        book = self.user_book(gid)
        if not book:
            log("「%s」还没有群友档案，先让人在群里说几句话" % group_name)
            return 0
        # 改写模式多两个信息源：人设（对齐称呼口吻）和日记（亲历的事都在里面）
        rewrite = bool(self.mem.get("usersRewrite", True))
        persona = self._persona_brief() if rewrite else ""
        diary = self.diary_prompt(gid, max_chars=1500, max_days=6) if rewrite else ""
        # convo 为空就退回用对话历史（旧版历史没有说话人，只能整句丢掉前缀）
        if not convo:
            convo = []
            for x in self.history(gid)[-16:]:
                c = str(x.get("content") or "").strip()
                if x.get("role") == "user":
                    m = self.HIST_UID_RE.match(c)
                    if m:
                        convo.append("（%s｜UID %s）%s" % (m.group(1) or "未知", m.group(2), m.group(3)[:120]))
                    else:
                        convo.append("（旧记录，说话人不明）" + c[:120])
                else:
                    convo.append("我: " + c[:120])
        if not convo and not diary:
            log("「%s」还没有可用的群聊记录，先让人在群里说几句话" % group_name)
            return 0
        roster = "\n".join("- UID %s：%s%s" % (
            u, v.get("name") or "未知名",
            ("（曾用名：%s）" % "、".join(v.get("alias") or [])) if v.get("alias") else "")
            for u, v in book.items())
        old = []
        for u, v in book.items():
            if v.get("notes"):
                old.append("- UID %s（%s）：%s" % (
                    u, v.get("name") or "未知名", "；".join(list(v["notes"])[-10:])))
        old_txt = "\n".join(old) or "（空）"
        if rewrite:
            sysmsg = (
                "你就是下面人设里那个人，现在在自己的小本子上重写对群友的印象——"
                "记的是人，不是他的资料。\n"
                "手上有三样东西：你的日记（你亲身经历的，主要依据）、"
                "最近群聊（只用来核对和更新）、已有档案。\n\n"
                "只改已有档案里【本来就有的人】，把条目改写成你自己记得的样子：\n"
                "- 合并：同一个人的重复条目并成一条；\n"
                "- 更新：被日记或最近群聊推翻的旧说法换成新的（例：待业中 → 在自学编程）；\n"
                "- 补看法：旧条目只有事、没有你当时怎么想的，顺手把你的看法补上，事实别动；\n"
                "- 补充：日记或群聊里长期有用的事补进去；\n"
                "- 删除：一次性的琐事、明显是玩笑的、你已经想不起来由的；\n"
                "- 旧条目只要没被推翻就留着——日记里没提到，不等于它是假的；\n"
                "- 没变的条目原样抄回去，别为了改而改。\n\n"
                "怎么写（每条 30 字以内，像你随手写在小本子上的批注，不是资料卡）：\n"
                "1. 一条里要有两样东西：关于他的事 + 你怎么看他。只写事、不带感觉，"
                "那就白记了——你记的是人，不是台账；\n"
                "   例：「老找我要照片｜缠起来没完，下回得先讲条件」；\n"
                "2. 用你平时叫他的方式（“那家伙”“空花”），不写“用户”“对方”；\n"
                "3. 事实只写日记或群聊里真出现过的，不猜不编；感觉只写你自己的——"
                "可以嫌、可以夸、可以犯嘀咕，但不要替他编心理（“他其实是想求安慰”这种不写）；"
                "也别用“那事”“那件事”指代，是什么事就写什么事；\n"
                "4. 语气就是你平时的语气，带口癖也行，别写成百科条目；\n"
                "5. 日记里只提昵称没提 UID 的，按「已知成员」的名字和曾用名去对，对不上就别写；\n"
                "6. 单次对话的动作不记成条目（“问我有雷吗”“惊讶我是旧版本”），"
                "要落就落成下次还用得上的（“在玩鸣潮”“在意版本号”）；\n"
                "7. 同一个人这次聊出来的事，并成一两条，别一件事拆三条——"
                "挑最值得记的那个点，剩下的压进同一句里；\n"
                "8. 每条写完自问一句：这像不像我在说这个人？读着像资料摘要，就重写。\n\n"
                "输出只有 JSON 数组：[{\"uid\":\"123456\",\"notes\":[\"...\"]}]。\n"
                "- 数组里出现的人，notes 就是他的【完整档案】，会整份替换旧的，不是追加；\n"
                "- 不用改的人不要出现在数组里；每人最多 %d 条"
                "（超了就删最没用的，档案是便签不是聊天记录）；\n"
                "- 一次最多动 4 个人，剩下的留给下次，别一口气全翻一遍；\n"
                "- 没把握的人别输出，一个都没有就输出 []。\n"
                "只输出 JSON，不要任何解释。" % min(8, MAX_NOTES_PER_USER)
            )
            usermsg = ("会话：%s\n\n你的人设（身份、语气、习惯称呼）：\n%s\n\n"
                       "你的日记：\n%s\n\n已知成员：\n%s\n\n已有档案：\n%s\n\n最近群聊：\n%s" % (
                           group_name or "", persona or "（没填人设，按普通群友的口吻）",
                           diary or "（还没写日记）", roster, old_txt,
                           "\n".join(convo) or "（最近没人说话）"))
            txt, _ = self._chat([{"role": "system", "content": sysmsg},
                                 {"role": "user", "content": usermsg}],
                                max_tokens=1500, temperature=0.4, scene="群友档案")
        else:
            sysmsg = ("你是群友档案整理助手。任务：从「最近群聊」里提炼每个群友【长期有用、且已有档案还没写】的印象。\n"
                      "只记这五类：身份或职业/在做什么、稳定喜好、常驻口头禅、与其他群友的关系、明确的约定或计划。\n"
                      "不要记：一次性琐事（今天吃了什么、几点开始做事、单次提问）、"
                      "以及“某人调侃/说/问”这类对话动作本身。\n"
                      "每条要写成「他的事｜你的看法」两截，30 字以内，光记事实等于没记——"
                      "例：待业中，在自学编程｜嘴上说没底，倒一天没停过。\n"
                      "看法可以嫌、可以夸，但只能落在群聊里真有的依据上，不要凭空揣测他的心理。\n"
                      "只能使用「已知成员」里出现过的 UID，绝不编造新 UID。"
                      "已有档案是权威资料，不要改写、合并、删除其中的条目，也不要重复输出。"
                      "没有值得记的就输出 []。"
                      '只输出 JSON 数组，格式 [{"uid":"123456","notes":["..."]}]，不要任何解释。')
            usermsg = "会话：%s\n\n已知成员：\n%s\n\n已有档案：\n%s\n\n最近群聊：\n%s" % (
                group_name or "", roster, old_txt, "\n".join(convo))
            txt, _ = self._chat([{"role": "system", "content": sysmsg},
                                 {"role": "user", "content": usermsg}],
                                max_tokens=700, temperature=0.2, scene="群友档案")
        i, j = txt.find("["), txt.rfind("]")
        arr = None
        if i >= 0 and j > i:
            try:
                got = json.loads(txt[i:j + 1])
                if isinstance(got, list):
                    arr = got
            except Exception:
                arr = None
        if arr is None:
            log("档案整理结果无法解析，保留原档案: %s" % txt[:80], "WARN")
            return 0

        backup_memory(MEM_USER_BAK)
        d = read_memory()
        g = d.setdefault("users", {})
        added = 0        # 新出现的条目
        removed = 0      # 被合并/更新掉的旧条目
        people = 0       # 档案有变动的人
        for it in arr:
            if not isinstance(it, dict):
                continue
            u = re.sub(r"\D", "", str(it.get("uid") or ""))
            if u not in g:            # 只认已知的人，防止模型编 UID
                continue
            ns = it.get("notes")
            if not isinstance(ns, list):
                continue
            clean_ns, seen = [], set()
            for s in ns:
                s = re.sub(r"\s+", " ", str(s or "")).strip(" -·\t")
                k = _norm_note(s)
                if s and len(s) <= 60 and k and k not in seen:
                    seen.add(k)
                    clean_ns.append(s)
            old = [str(x) for x in (g[u].get("notes") or []) if str(x).strip()]
            if not clean_ns:
                continue              # 模型没写出东西 ≠ 要把人洗白，保留旧档案
            if rewrite and len(old) >= 4 and len(clean_ns) * 2 < len(old):
                # 删得太狠（旧 4 条以上、新的不到一半）当模型跑偏，这条退回只追加
                log("档案改写「%s」UID %s 删过头（%d→%d），本条退回追加"
                    % (group_name, u, len(old), len(clean_ns)), "WARN")
                clean_ns = merge_notes(old, clean_ns, MAX_NOTES_PER_USER)[0]
            elif not rewrite:
                clean_ns = merge_notes(old, clean_ns, MAX_NOTES_PER_USER)[0]
            if clean_ns == old:
                continue
            ok_ = {_norm_note(x) for x in old}
            nk = {_norm_note(x) for x in clean_ns}
            dropped = [x for x in old if _norm_note(x) not in nk]
            if dropped:
                # 删掉的写进日志，用户翻日志就知道哪条被改写掉了，好决定要不要还原
                log("档案改写「%s」%s 去掉：%s" % (
                    group_name, g[u].get("name") or u, " / ".join(dropped)))
            g[u]["notes"] = clean_ns[:MAX_NOTES_PER_USER]
            added += len(nk - ok_)
            removed += len(ok_ - nk)
            people += 1
        d["users"] = clean_users(d.get("users"))
        d["usersLearnedAt"] = time.time()
        write_memory(d)
        self.mem = d
        if rewrite:
            log("档案已按日记改写「%s」：%d 人（+%d 条 / -%d 条）" % (group_name, people, added, removed))
        else:
            log("档案已整理「%s」：%d 人新增 %d 条" % (group_name, people, added))
        return added

    def memo_for(self, uid, note, name=""):
        """把一句话写进这个人的便签——她说了「记下了」，这里就真的落盘。

        只认已有档案的人（不凭空建档，免得随便一个 UID 就把档案撑大）；
        同一条已经有了就不再重复记（隔天又说一遍「记下了」很正常）。
        返回 True 表示这次真写进去了。"""
        u = re.sub(r"\D", "", str(uid or ""))
        note = re.sub(r"\s+", " ", str(note or "")).strip(" -·\t")
        if not u or not note:
            return False
        if len(note) > 60:
            note = note[:60]
        d = read_memory()
        g = d.setdefault("users", {})
        cur = g.get(u)
        if not isinstance(cur, dict):
            # 第一次说话就往档案里记的人：有名字就顺手建一份，别让「记下」落空
            name = _one_line(name, 40)
            if not name:
                return False
            cur = {"name": name, "alias": [], "notes": [], "role": ""}
            g[u] = cur
        old = [str(x) for x in (cur.get("notes") or []) if str(x).strip()]
        k = _norm_note(note)
        if any(_norm_note(x) == k for x in old):
            return False
        old.append(note)
        cur["notes"] = old[-MAX_NOTES_PER_USER:]
        d["users"] = clean_users(g)
        write_memory(d)
        self.mem = d
        log("记下了：%s → %s" % (_one_line(cur.get("name") or name or u, 16), note))
        return True


# --------------------------------------------------------------------------
# 触发与清洗
# --------------------------------------------------------------------------
def clean_text(text):
    t = AT_RE.sub(" ", text or "")
    t = CH_RE.sub(" ", t)
    t = SYS_RE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def is_trigger(trig, text, self_id, self_name, reply_uid=None):
    """这句要不要接。

    reply_uid 是被引用那条消息的发送者 UID（客户端 m.reply.user_id）。
    群里点「回复」再打字跟它说话时，正文里没有 @{id:...}——以前只认 @，
    于是这种「回复它」的话一律当没听见。点回复是指名道姓找它，比 @ 还明确，
    所以引用的是它自己的消息就算叫到它（纯图、纯表情没配文字也算）。
    """
    text = text or ""
    replied_me = bool(reply_uid) and str(reply_uid) == str(self_id)
    mode = (trig.get("mode") or "mention").lower()
    if mode == "all":
        return True
    if mode == "mention":
        if replied_me:
            return True
        if not text:
            return False
        if re.search(r"@\{id:%d[^}]*\}" % self_id, text):
            return True
        if self_name and ("@" + self_name) in text:
            return True
        return False
    if mode == "prefix":
        if replied_me:
            return True
        p = trig.get("prefix") or ""
        body = clean_text(text)
        return bool(p) and body.lstrip().startswith(p)
    if mode == "keyword":
        if replied_me:
            return True
        kws = trig.get("keywords") or []
        body = clean_text(text)
        return any(k and k in body for k in kws)
    if mode == "mention_or_prefix":
        if is_trigger({"mode": "mention"}, text, self_id, self_name, reply_uid):
            return True
        return is_trigger({"mode": "prefix", **trig}, text, self_id, self_name, reply_uid)
    return False


# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------
class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cdp = Cdp(cfg.get("cdp", {}).get("port", PORT))
        self.client = Client(self.cdp)
        self.mem = read_memory()
        self.ai = AI(cfg, self.mem)
        self.trig = cfg.get("trigger", {})
        self.reply_cfg = cfg.get("reply", {})
        self.groups_cfg = cfg.get("groups", {})
        self.dms_cfg = cfg.get("dms", {}) or {}
        # 通道开关：关掉 = 它在那边「只听不说」。消息照样读、照样进上下文、
        # 照样记日记和群友档案，只是不回话也不主动开口。
        # 故意不做成「彻底不看」——静音半小时后打开，它该知道这半小时发生了什么。
        self.group_on = bool(self.groups_cfg.get("enabled", True))
        self.dm_on = bool(self.dms_cfg.get("enabled", True))
        # 看图：每分钟最多看几张（全群合计 + 单个会话各一份额度）。
        # 一晚上的斗图能发几十张，不设闸的话它会一直在看图，回复慢到没法插话。
        ai_cfg = cfg.get("ai") or {}
        self.img_on = bool(ai_cfg.get("vision", True))
        self.img_per_min = max(0, int(ai_cfg.get("visionPerMinute") or 6))
        self.img_wait = max(6, int(ai_cfg.get("visionTimeoutSeconds") or 20))
        self._img_hits = []
        self._img_hits_g = {}
        self._label_lock = threading.Lock()   # 表情包标注读-改-写串行，防并发覆盖
        self._mood_settle = 0.0               # 上次「心情自己平复」的时间，见 maybe_settle_mood
        # 说完话等回应：gid -> {ts, text, sad}。有人接就高兴一下，没人接就失落一下。
        self.await_reply = {}
        self._aff_dirty = False        # 好感度改过还没落盘
        self._aff_flush = 0.0          # 上次落盘时间
        self.poll = float(cfg.get("poll", {}).get("intervalSeconds", 2.5))
        self.max_chars = int(self.reply_cfg.get("maxChars") or 280)
        self.dm_max_chars = int(self.reply_cfg.get("dmMaxChars") or 800)
        self.cd_group = float(self.reply_cfg.get("groupCooldownSeconds", 3))
        self.cd_global = float(self.reply_cfg.get("globalCooldownSeconds", 1))
        self.direct = bool(self.reply_cfg.get("directReply", False))
        self.direct_rate = max(0, min(100, int(self.reply_cfg.get("directReplyRate", 30))))
        # 两次主动插话之间至少隔这么久（0 = 不限制），只有「没被 @ 也接话」受它管
        self.direct_gap = max(0.0, float(self.reply_cfg.get("directMinGapSeconds", 60) or 0))
        self.last_direct = 0.0   # 上次主动插话的时间
        # 管理身份：admin 段在群友档案里设成 admin 的人才算
        self.admin_reply = bool(self.reply_cfg.get("adminAlwaysReply", True))    # 回复概率 100%
        self.admin_bypass = bool(self.reply_cfg.get("adminBypassCooldown", True))  # 100% 听取（不等冷却）
        # 表情：识别 / 收藏 / 使用
        self.emoji_cfg = cfg.get("emoji", {}) or {}
        self.emoji_on = bool(self.emoji_cfg.get("enabled", True))
        self.emoji_auto = bool(self.emoji_cfg.get("autoCollect", True))
        self.emoji_rate = max(0, min(100, int(self.emoji_cfg.get("useRate", 20))))
        self.emoji_max = max(8, int(self.emoji_cfg.get("maxStickers", 60)))
        self.emoji_used = 0      # 主动用了几次表情

        # 群聊冒泡：群里冷场太久时自己找个话头说一句
        bcfg = cfg.get("bubble", {}) or {}
        self.bubble_cfg = bcfg
        self.bubble_on = bool(bcfg.get("enabled", False))
        # 静了多久算冷场（至少 1 分钟，免得填 0 变成刷屏）
        self.bubble_idle = max(60, int(float(bcfg.get("idleMinutes", 30) or 30) * 60))
        # 两次冒泡之间至少隔多久
        self.bubble_gap = max(0, int(float(bcfg.get("gapMinutes", 120) or 0) * 60))
        self.bubble_chance = max(1, min(100, int(bcfg.get("chance", 50) or 50)))
        self.bubble_prompt = (bcfg.get("prompt") or "").strip()
        self.last_activity = {}     # gid -> 群里最后一次有动静的时间
        self.last_bubble = {}       # gid -> 上次冒泡时间
        self.next_bubble_try = {}   # gid -> 下次可以掷骰子的时间
        # 群聊上下文：每个会话最近说了什么。它回复时要看得见，冒泡时也当素材。
        # 以前这里只存 6 条纯文本、还专给冒泡用，回复时一个字都不看——
        # 结果它像个刚推门进来的人，接得上点它名的那句，接不上屋里在聊的事。
        self.recent = {}            # gid -> [{t,name,uid,me,text}]，最近 60 条
        self.talk = {}              # gid -> 同样的结构，滚动保留 200 条（翻旧账用）
        for gid, arr in (self.mem.get("talkLog") or {}).items():
            if isinstance(arr, list):
                self.talk[str(gid)] = [x for x in arr if isinstance(x, dict)][-200:]
        self._talk_saved = 0.0      # 上次把 talk 落盘的时间（限流，别每轮都写文件）
        self._last_seq = {}         # gid -> 已记过的最大 im_seq，防止轮询窗口重叠重复记
        self.bubbles = 0            # 一共冒了几次
        self.last_bubble_ts = 0.0

        # 私聊主动：好友很久没说话，自己去找他。跟群聊冒泡是两套逻辑——
        # 群里是「冷场了带个气氛」，私聊是「我想他了」，阈值、配额、语气都不一样。
        pcfg = (self.dms_cfg.get("ping") or {}) if isinstance(self.dms_cfg, dict) else {}
        if not isinstance(pcfg, dict):
            pcfg = {}
        self.ping_cfg = pcfg
        self.ping_on = bool(pcfg.get("enabled", False))   # 默认关，面板打开才生效
        # 安静多久算「他把我忘了」（至少 5 秒；再短就没意义了，等于一停就说）
        self.ping_idle = max(5, int(round(_num(pcfg, "idleMinutes", 180) * 60)))
        # 两次主动之间至少隔多久
        self.ping_gap = max(0, int(_num(pcfg, "minGapMinutes", 300) * 60))
        # 每个人每天最多主动几次
        self.ping_daily = max(1, int(_num(pcfg, "dailyMax", 2)))
        # 允许开口的时段（整点小时，支持跨零点）
        self.ping_h1 = max(0, min(23, int(_num(pcfg, "hourStart", 8))))
        self.ping_h2 = max(0, min(23, int(_num(pcfg, "hourEnd", 23))))
        # 全局节奏：不管认识多少人，两次主动之间至少隔这么久，
        # 免得一开机就把十几个老会话挨个找一遍
        self.ping_every = max(120, int(_num(pcfg, "globalMinutes", 25) * 60))
        # 上一次没被理，隔多久可以再补一句
        self.ping_again = max(600, int(_num(pcfg, "againMinutes", 45) * 60))
        self.ping_escalate = bool(pcfg.get("escalate", True))
        # 连续被晾这么多回就先别找他了
        self.ping_giveup = max(1, int(_num(pcfg, "giveUpAfter", 2)))
        self.ping_hold = max(1.0, _num(pcfg, "giveUpHours", 20))
        # 只找认识的人：有档案、或者私聊说过话的，陌生好友不搭话
        self.ping_known = bool(pcfg.get("onlyKnown", True))
        self.ping_prompt = (pcfg.get("prompt") or "").strip()
        # 面板上勾选「只找这几个」。空 = 按 onlyKnown 的规则自己挑
        self.ping_uids = set(str(x).strip() for x in (pcfg.get("uids") or []) if str(x).strip())
        self.ping_state = self.mem.get("dmPing") or {}
        self.last_peer = {}         # gid -> 他最后一次开口的时间
        self.last_from = {}         # gid -> 最后一条是谁说的（me / them）
        self.pings = 0              # 一共主动找了几次
        self.last_ping_ts = float(self.mem.get("dmPingLast") or 0)
        self.ping_log = []          # 面板「最近主动」用，只放内存

        # 群聊主动：在你勾的群里按节奏自己起个话头。
        # 跟群聊冒泡不是一件事：冒泡是等冷场、按概率、哪个群都可能；
        # 这里是点名群 + 到点就说，将来接「看帖子的感受」也从这条线走。
        gcfg = (cfg.get("groups", {}) or {}).get("proactive") or {}
        if not isinstance(gcfg, dict):
            gcfg = {}
        self.gp_cfg = gcfg
        self.gp_on = bool(gcfg.get("enabled", False))
        self.gp_ids = set(str(x).strip() for x in (gcfg.get("ids") or []) if str(x).strip())
        self.gp_gap = max(0, int(_num(gcfg, "gapMinutes", 180) * 60))
        self.gp_daily = max(1, int(_num(gcfg, "dailyMax", 2)))
        self.gp_every = max(120, int(_num(gcfg, "globalMinutes", 30) * 60))
        # 群里刚有人说话就先等一会儿，别插进正在聊的对话里
        self.gp_idle = max(0, int(round(_num(gcfg, "idleMinutes", 30) * 60)))
        self.gp_h1 = max(0, min(23, int(_num(gcfg, "hourStart", 9))))
        self.gp_h2 = max(0, min(23, int(_num(gcfg, "hourEnd", 23))))
        self.gp_prompt = (gcfg.get("prompt") or "").strip()
        self.gp_state = self.mem.get("gpSay") or {}
        self.gp_says = 0
        self.last_gp_ts = float(self.mem.get("gpSayLast") or 0)
        self.gp_log = []            # 面板「最近主动」用，只放内存
        # 以后接刷帖：{gid: "刚看到的帖子内容"}，写进去它就照着看完的感受说话
        self.gp_material = {}

        # 私聊指令：授权的人在私聊里让它去群里发话/发图
        ccfg = cfg.get("control", {}) or {}
        self.control_cfg = ccfg
        self.control_on = bool(ccfg.get("enabled", True))
        self.control_prefix = (ccfg.get("prefix") or "/").strip() or "/"
        self.control_uids = set(str(x).strip() for x in (ccfg.get("uids") or []) if str(x).strip())
        # 听懂人话：不用打斜杠，说「帮我在主教群说一句我错了」也照办
        self.control_nl = bool(ccfg.get("nl", True))
        self.last_dm_img = {}       # uid -> 他刚发的那张图，/img 不带地址时用
        self.recent_dm = {}         # uid -> {"name":..., "ts":...}，面板上点一下就能加白名单
        self.cmds = 0               # 一共执行了几条指令
        self.last_cmd = {}          # uid -> 刚办完的那一单，接着「要大的」「重发」能重办
        self.groups = []            # 当前监听中的会话（指令按名字找群要用）

        self.baseline = {}      # gid -> 已处理的最大 seq
        self.last_reply = {}    # gid -> 时间戳
        self.last_any = 0.0
        self.replied = 0
        self.triggers = 0
        self.direct_hits = 0    # 群聊"没被 @"也回了几次
        self.started = time.time()
        self.since_learn = {}
        # 关机前收尾：写日记 + 把日记收成一段总观。面板点停止时给的时间，
        # 拿不到这段就得硬杀，一个字的日记都留不下。
        self.farewell_cfg = _farewell_cfg(cfg)
        self._farewell = None       # 收尾进度，写进 state.json 让面板显示倒计时
        self.cfg_mtime = self._mtime(CONFIG_PATH)
        self.mem_mtime = self._mtime(MEM_PATH)

    @staticmethod
    def _mtime(path):
        try:
            return os.path.getmtime(path)
        except Exception:
            return 0

    def maybe_reload(self):
        """监听网页改了 config.json / memory.json 时无需重启"""
        cm = self._mtime(CONFIG_PATH)
        if cm and cm != self.cfg_mtime:
            self.cfg_mtime = cm
            try:
                cfg = load_cfg()
            except Exception as e:
                log("配置读取失败，继续用旧配置: %s" % e, "WARN")
                return
            self.cfg = cfg
            self.mem = read_memory()
            self.ai.reload(cfg, self.mem)
            self.trig = cfg.get("trigger", {})
            self.reply_cfg = cfg.get("reply", {})
            self.groups_cfg = cfg.get("groups", {})
            self.dms_cfg = cfg.get("dms", {}) or {}
            self.group_on = bool(self.groups_cfg.get("enabled", True))
            self.dm_on = bool(self.dms_cfg.get("enabled", True))
            self.farewell_cfg = _farewell_cfg(cfg)   # 关机收尾的秒数/开关，存完即生效
            self.max_chars = int(self.reply_cfg.get("maxChars") or 280)
            self.dm_max_chars = int(self.reply_cfg.get("dmMaxChars") or 800)
            self.cd_group = float(self.reply_cfg.get("groupCooldownSeconds", 3))
            self.cd_global = float(self.reply_cfg.get("globalCooldownSeconds", 1))
            self.direct = bool(self.reply_cfg.get("directReply", False))
            self.direct_rate = max(0, min(100, int(self.reply_cfg.get("directReplyRate", 30))))
            self.direct_gap = max(0.0, float(self.reply_cfg.get("directMinGapSeconds", 60) or 0))
            self.admin_reply = bool(self.reply_cfg.get("adminAlwaysReply", True))
            self.admin_bypass = bool(self.reply_cfg.get("adminBypassCooldown", True))
            ecfg = cfg.get("emoji", {}) or {}
            self.emoji_cfg = ecfg
            self.emoji_on = bool(ecfg.get("enabled", True))
            self.emoji_auto = bool(ecfg.get("autoCollect", True))
            self.emoji_rate = max(0, min(100, int(ecfg.get("useRate", 20))))
            self.emoji_max = max(8, int(ecfg.get("maxStickers", 60)))
            bcfg = cfg.get("bubble", {}) or {}
            self.bubble_cfg = bcfg
            self.bubble_on = bool(bcfg.get("enabled", False))
            self.bubble_idle = max(60, int(float(bcfg.get("idleMinutes", 30) or 30) * 60))
            self.bubble_gap = max(0, int(float(bcfg.get("gapMinutes", 120) or 0) * 60))
            self.bubble_chance = max(1, min(100, int(bcfg.get("chance", 50) or 50)))
            self.bubble_prompt = (bcfg.get("prompt") or "").strip()
            ccfg = cfg.get("control", {}) or {}
            self.control_cfg = ccfg
            self.control_on = bool(ccfg.get("enabled", True))
            self.control_prefix = (ccfg.get("prefix") or "/").strip() or "/"
            self.control_uids = set(str(x).strip() for x in (ccfg.get("uids") or [])
                                    if str(x).strip())
            self.control_nl = bool(ccfg.get("nl", True))
            pcfg = (cfg.get("dms", {}) or {}).get("ping") or {}
            if not isinstance(pcfg, dict):
                pcfg = {}
            self.ping_cfg = pcfg
            self.ping_on = bool(pcfg.get("enabled", False))
            self.ping_idle = max(5, int(round(_num(pcfg, "idleMinutes", 180) * 60)))
            self.ping_gap = max(0, int(_num(pcfg, "minGapMinutes", 300) * 60))
            self.ping_daily = max(1, int(_num(pcfg, "dailyMax", 2)))
            self.ping_h1 = max(0, min(23, int(_num(pcfg, "hourStart", 8))))
            self.ping_h2 = max(0, min(23, int(_num(pcfg, "hourEnd", 23))))
            self.ping_every = max(120, int(_num(pcfg, "globalMinutes", 25) * 60))
            self.ping_again = max(600, int(_num(pcfg, "againMinutes", 45) * 60))
            self.ping_escalate = bool(pcfg.get("escalate", True))
            self.ping_giveup = max(1, int(_num(pcfg, "giveUpAfter", 2)))
            self.ping_hold = max(1.0, _num(pcfg, "giveUpHours", 20))
            self.ping_known = bool(pcfg.get("onlyKnown", True))
            self.ping_prompt = (pcfg.get("prompt") or "").strip()
            self.ping_uids = set(str(x).strip() for x in (pcfg.get("uids") or [])
                                 if str(x).strip())
            gcfg = (cfg.get("groups", {}) or {}).get("proactive") or {}
            if not isinstance(gcfg, dict):
                gcfg = {}
            self.gp_cfg = gcfg
            self.gp_on = bool(gcfg.get("enabled", False))
            self.gp_ids = set(str(x).strip() for x in (gcfg.get("ids") or [])
                              if str(x).strip())
            self.gp_gap = max(0, int(_num(gcfg, "gapMinutes", 180) * 60))
            self.gp_daily = max(1, int(_num(gcfg, "dailyMax", 2)))
            self.gp_every = max(120, int(_num(gcfg, "globalMinutes", 30) * 60))
            self.gp_idle = max(0, int(round(_num(gcfg, "idleMinutes", 30) * 60)))
            self.gp_h1 = max(0, min(23, int(_num(gcfg, "hourStart", 9))))
            self.gp_h2 = max(0, min(23, int(_num(gcfg, "hourEnd", 23))))
            self.gp_prompt = (gcfg.get("prompt") or "").strip()
            self.mem_mtime = self._mtime(MEM_PATH)
            log("config.json 已热更新：触发=%s 模型=%s 群聊直接回复=%s %d%% 间隔=%d秒 管理=%s 表情=%s %d%% 冒泡=%s 听懂人话=%s 私聊主动=%s 群聊主动=%s" % (
                self.trig.get("mode"), self.ai.model, "开" if self.direct else "关", self.direct_rate,
                int(self.direct_gap),
                ("必回" if self.admin_reply else "按概率") + ("+不等冷却" if self.admin_bypass else ""),
                "开" if self.emoji_on else "关", self.emoji_rate,
                ("%d分钟/%d分钟/%d%%" % (self.bubble_idle // 60, self.bubble_gap // 60,
                                          self.bubble_chance)) if self.bubble_on else "关",
                "开" if self.control_nl else "关",
                ("静%s/每天%d次/%d-%d点" % (_dur(self.ping_idle), self.ping_daily,
                                             self.ping_h1, self.ping_h2)) if self.ping_on else "关",
                ("%d个群/隔%d分钟/每群每天%d次" % (len(self.gp_ids), self.gp_gap // 60,
                                              self.gp_daily)) if self.gp_on else "关"))
            log("心情参数：%s｜基线 %.0f｜没人理 %.1f 小时平复一半｜情绪 %.0f 分钟散一半" % (
                "开" if self.ai.mood_on else "关", MOOD_BASE,
                MOOD_HALF / 3600.0, EMO_HALF / 60.0))
            return
        mm = self._mtime(MEM_PATH)
        if mm and mm != self.mem_mtime:
            self.mem_mtime = mm
            was = bool(self.mem.get("enabled", True))
            was_card = str(self.mem.get("profile") or "")
            was_kb = list(self.mem.get("kb") or [])
            sig_old = _mem_sig(self.mem)
            self.mem = read_memory()
            self.ai.mem = self.mem
            if _mem_sig(self.mem) == sig_old:
                return          # 只是自己刚写的聊天缓存，别当成人在改设置
            was_agent = self.ai.agent
            self.ai.agent = bool(self.mem.get("agent"))
            agent_changed = self.ai.agent != was_agent
            if agent_changed:
                log("智能体模式已%s（网页开关）" % ("打开" if self.ai.agent else "关闭"))
            now_on = bool(self.mem.get("enabled", True))
            now_card = str(self.mem.get("profile") or "")
            if now_card != was_card:
                # 换了人物卡片：人设、群记忆、群友档案、表情包已经整组换掉，
                # 对话历史也要按新卡片重建，不然新角色会接着旧角色的话说
                self.ai.load_history()
                log("人物卡片已切换（%s → %s）：人设/群记忆/群友档案/表情包/知识库一起换，对话历史重新开始"
                    % (was_card or "无", now_card or "无"))
            elif now_on != was:
                log("自动回复已%s（网页开关）" % ("恢复" if now_on else "暂停"))
            elif list(self.mem.get("kb") or []) != was_kb:
                log("知识库勾选已更新：这次带 %d 篇资料进提示词" % len(self.mem.get("kb") or []))
            elif agent_changed:
                pass        # 上面已经单独记过一句，别重复
            else:
                log("人设/记忆已更新（来自监听页）")

    def track_user(self, gid, uid, name):
        """把发言者登记成「UID → 昵称」，改名时旧名进曾用名，记忆始终跟着 UID 走。
        档案是全局的，同一个 UID 在群和私聊里共用一份。
        只在昵称真的变化或首次出现时落盘，避免每轮都写文件。"""
        u = re.sub(r"\D", "", str(uid or ""))
        if not u or u == "0":
            return False
        nm = _one_line(name, 40)
        book = self.mem.get("users") or {}
        cur = book.get(u) or {}
        old = str(cur.get("name") or "").strip()
        if old == nm and u in book:
            return False
        if u not in book and len(book) >= MAX_USERS_PER_GROUP:
            return False
        d = read_memory()
        g = d.setdefault("users", {})
        cur = g.get(u) or {}
        old = str(cur.get("name") or "").strip()
        alias = list(cur.get("alias") or [])
        if old and nm and old != nm:
            if old not in alias:
                alias.append(old)
            alias = alias[-5:]
            log("群友改名：%s → %s（UID %s，记忆按 UID 保留）" % (old, nm, u))
        g[u] = {"name": nm or old, "alias": alias, "notes": list(cur.get("notes") or [])}
        if write_memory(d):
            self.mem = d
            self.ai.mem = d
            self.mem_mtime = self._mtime(MEM_PATH)
            return True
        return False

    def role_of(self, uid):
        """群友档案里的角色：""=普通、"admin"=管理、"block"=黑名单。
        在监听页「群友档案」里，名字右边那个下拉框设的就是它。"""
        u = re.sub(r"\D", "", str(uid or ""))
        if not u:
            return ""
        v = (self.mem.get("users") or {}).get(u) or {}
        r = str(v.get("role") or "").strip().lower()
        return r if r in ("admin", "block") else ""

    def user_note(self, gid, uid):
        """某个 UID 的档案记忆条目（全局，网页保存后供日志/自检用）"""
        return list(((self.mem.get("users") or {}).get(str(uid)) or {}).get("notes") or [])

    # ---- 表情：识别 / 收藏 / 使用 ----
    def small_book(self):
        return (self.mem.get("emoji") or {}).get("small") or {}

    def sticker_book(self):
        return (self.mem.get("emoji") or {}).get("sticker") or {}

    @staticmethod
    def sticker_key(url):
        """从表情图地址取稳定 key：.../pic/15587424494944798457.png → 15587424494944798457"""
        base = (url or "").split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        return os.path.splitext(base)[0][:40]

    def observe_emoji(self, m, flush=True):
        """从别人的消息里学表情：小表情记用法，大表情攒进收藏夹（自主收藏）。
        flush=False 时只改内存，由调用方最后统一写盘（回填历史时用）。"""
        if not self.emoji_on:
            return
        raw = m.get("msg") or ""
        smalls = SMALL_EMOJI_RE.findall(raw)
        url = (m.get("img") or "").strip()
        key = self.sticker_key(url) if (int(m.get("type") or 0) == 7 and url) else ""
        if not smalls and not key:
            return
        if flush:
            d = read_memory()
        else:
            base = self.mem if isinstance(self.mem, dict) else read_memory()
            d = dict(base)
            d.setdefault("emoji", {})
        em = d["emoji"]
        sm = em.setdefault("small", {})
        st = em.setdefault("sticker", {})
        now = time.time()
        who = _one_line(m.get("name") or "", 20)
        added = []
        for typ, name in smalls:
            k = "%s_%s" % (typ, name)
            v = sm.get(k) or {"count": 0}
            if not v.get("count"):
                added.append("小表情 " + k)
            v["code"] = "[%s]" % k
            v["type"] = typ
            v["count"] = int(v.get("count") or 0) + 1
            v["last"] = now
            # 记「谁老发这个」——被问「这表情你哪来的」才有话可说，
            # 不然只能含糊地说是群里学的。
            if who:
                by = v.get("by")
                if not isinstance(by, dict):
                    by = {}
                by[who] = int(by.get(who) or 0) + 1
                v["by"] = by
                v["from"] = who
            sm[k] = v
        # 尺寸：手机端完全按 emoji_img_info 的比例画，0x0 会被裁成宽条
        mw, mh = int(m.get("w") or 0), int(m.get("h") or 0)
        if key and len(key) >= 6:
            v = st.get(key)
            if v is None:
                if self.emoji_auto:
                    st[key] = {"url": url, "w": mw, "h": mh, "count": 1, "sent": 0,
                               "from": _one_line(m.get("name") or "", 20),
                               "first": now, "last": now, "label": ""}
                    added.append("表情包 " + key)
                    # 这张图还没「看过」：后台补一眼，把内容/笑点存成 label，
                    # 以后被问「你发的图什么意思」才答得上来。
                    if url and self.vision:
                        self._label_sticker_async(key, url)
            else:
                v["count"] = int(v.get("count") or 0) + 1
                v["last"] = now
                if url:
                    v["url"] = url       # 地址可能带签名参数，始终留最新的
                if mw and mh and not ((v.get("w") or 0) and (v.get("h") or 0)):
                    v["w"], v["h"] = mw, mh
                st[key] = v
        if self.emoji_auto and len(st) > self.emoji_max:
            # 超上限：先淘汰出现最少、最久没露面的
            order = sorted(st.items(), key=lambda kv: ((kv[1].get("count") or 0),
                                                       (kv[1].get("last") or 0)))
            for k, _ in order[: len(st) - self.emoji_max]:
                st.pop(k, None)
        if not flush:
            # 回填历史时先只改内存，循环结束统一写一次盘
            self.mem = d
            return
        if write_memory(d):
            self.mem = d
            self.ai.mem = d
            self.mem_mtime = self._mtime(MEM_PATH)
            if added:
                log("学到表情：%s（共计小表情 %d / 表情包 %d）"
                    % ("、".join(added[:4]), len(sm), len(st)))

    def _label_sticker(self, key, url):
        """后台给一张表情包「看一眼」，把内容/笑点存成 label。
        只认「还没名字」的图，避免重复烧 token；失败就不动，别让收藏卡住。"""
        try:
            if not getattr(self.ai, "vision", True):
                return
            desc = self.ai.describe_image(url, timeout=15)
            if not desc:
                return
            # 名字要短，够「认得哪张图」就行；太长塞进提示词反而占地方。
            # 36 字是折中：24 字常常把「是什么梗」那半句截掉，被问「这图什么意思」又答不上。
            label = _one_line(desc, 36)
            if not label:
                return
            # 读-改-写必须串行：14 个标注线程并发时，各自读旧快照再写盘会互相覆盖，
            # 只剩最后一个的 label。加锁让它们排队，谁都不丢。
            with self._label_lock:
                d = read_memory()
                v = ((d.get("emoji") or {}).get("sticker") or {}).get(key)
                if not isinstance(v, dict) or (v.get("label") or "").strip():
                    return
                v["label"] = label
                if write_memory(d):
                    self.mem = d
                    self.ai.mem = d
                    self.mem_mtime = self._mtime(MEM_PATH)
                    log("表情包识图标注：%s → %s" % (key[:8], label))
        except Exception as e:
            log("表情包标注失败: %s" % str(e)[:100], "WARN")

    def _label_sticker_async(self, key, url):
        """异步给表情包补名字，不卡消息循环。线程是守护线程，进程退出就跟着走。"""
        try:
            t = threading.Thread(target=self._label_sticker, args=(key, url), daemon=True)
            t.start()
        except Exception:
            pass

    def upgrade_sticker_labels(self):
        """把早年按 24 字截断的旧 label 补长。

        只吃本地 image_cache 里已经存过的描述，不打接口、不花 token；
        24 字往往正好断在「是什么梗」前面，被问「这图什么意思」就只能编。
        """
        book = self.sticker_book() or {}
        if not book:
            return
        cache = img_cache_load()
        fixed = 0
        with self._label_lock:
            d = read_memory()
            st = (d.get("emoji") or {}).get("sticker") or {}
            for key, v in st.items():
                if not isinstance(v, dict):
                    continue
                old = (v.get("label") or "").strip()
                if not old or len(old) < 24:
                    continue
                full = cache.get(str(v.get("url") or "").strip()) or ""
                new = _one_line(full, 36)
                if len(new) > len(old) and new.startswith(old[:20]):
                    v["label"] = new
                    fixed += 1
            if fixed and write_memory(d):
                self.mem = d
                self.ai.mem = d
                self.mem_mtime = self._mtime(MEM_PATH)
        if fixed:
            log("表情包标注补长：%d 张（24 → 36 字）" % fixed)

    def backfill_sticker_labels(self, limit=12):
        """启动时给「还没名字」的旧表情包批量补标注（异步，一次最多 limit 张）。
        老收藏全是空 label，不补的话被问「你发的图什么意思」还是答不上来。"""
        if not (getattr(self.ai, "vision", True) and self.emoji_on):
            return
        n = 0
        for key, v in (self.sticker_book() or {}).items():
            if n >= limit:
                break
            if not isinstance(v, dict):
                continue
            url = (v.get("url") or "").strip()
            if url and not (v.get("label") or "").strip():
                self._label_sticker_async(key, url)
                n += 1
        if n:
            log("给 %d 张旧表情包补标注（后台识图）" % n)

    def backfill_emoji(self, groups, limit=40):
        """启动时回填一次：把最近消息里的表情收进来（只收表情，不回复）。
        收藏夹空着不好看，回填后打开面板就能看到真图。"""
        if not self.emoji_on:
            return
        before = len(self.sticker_book()), len(self.small_book())
        for g in groups:
            try:
                d = self.fetch(g, limit)
            except Exception as e:
                log("回填表情读取失败 %s: %s" % (self.label(g), e), "WARN")
                continue
            for m in (d.get("list") or []):
                if m.get("uid") == self.client.self_id:
                    continue
                try:
                    self.observe_emoji(m, flush=False)
                except Exception:
                    pass
        after = len(self.sticker_book()), len(self.small_book())
        if after[0] == before[0] and after[1] == before[1]:
            return
        if write_memory(self.mem):
            self.ai.mem = self.mem
            self.mem_mtime = self._mtime(MEM_PATH)
            log("回填表情：表情包 +%d（共 %d），小表情 +%d（共 %d）"
                % (after[0] - before[0], after[0], after[1] - before[1], after[1]))

    def find_sticker(self, token):
        """模型可能写 KEY，也可能写表情的名字，两种都认。
        返回收藏夹里的 key，认不出来返回 ""。"""
        book = self.sticker_book()
        t = (token or "").strip().strip("[]【】「」\"'‘’ ")
        if not t:
            return ""
        if t in book and (book[t] or {}).get("url"):
            return t
        low = t.lower()
        # 先按名字精确匹配
        for k, v in book.items():
            if not isinstance(v, dict) or not v.get("url"):
                continue
            lb = str(v.get("label") or "").strip().lower()
            if lb and lb == low:
                return k
        # 再放宽：名字互相包含、或者写的是 KEY 的一部分
        for k, v in book.items():
            if not isinstance(v, dict) or not v.get("url"):
                continue
            lb = str(v.get("label") or "").strip().lower()
            if lb and len(low) >= 2 and (low in lb or lb in low):
                return k
            if len(low) >= 6 and (k.startswith(low) or low.startswith(k)):
                return k
        return ""

    def take_stickers(self, text):
        """把回复里 [[表情:KEY]] 抠出来，返回 (剩下的正文, [收藏项])"""
        picked = []

        def rep(mo):
            k = self.find_sticker(mo.group(1))
            v = self.sticker_book().get(k) if k else None
            if v and v.get("url") and len(picked) < 2:
                picked.append(v)
            return ""

        t = STICKER_TOKEN_RE.sub(rep, text or "")
        t = STICKER_JUNK_RE.sub("", t)
        t = re.sub(r"[ \t]{2,}", " ", t).strip(" ，,、")
        return t, picked

    def pick_emoji(self, prefer=None):
        """按记忆挑一个常用表情：有收藏时偏向发图，否则补个小表情。
        prefer="sticker" 只挑收藏的图（用户明说要表情包/大表情），
        prefer="small" 只挑小表情；都没货时退回对方那一种。"""
        small = self.small_book()
        stick = self.sticker_book()
        if prefer == "sticker" and stick:
            return ("sticker", self._sticker_pick(stick))
        if prefer == "small" and small:
            items = sorted(small.items(), key=lambda kv: -(kv[1].get("count") or 0))[:8]
            return ("small", random.choice(items)[1].get("code") or "")
        if stick and (not small or random.random() < 0.35):
            return ("sticker", self._sticker_pick(stick))
        if small:
            items = sorted(small.items(), key=lambda kv: -(kv[1].get("count") or 0))[:8]
            v = random.choice(items)[1]
            return ("small", v.get("code") or "")
        return None

    def _sticker_pick(self, stick):
        """从收藏里挑一张：常用优先，偶尔全库随机，手动加的图也有机会露面"""
        items = list(stick.values())
        if len(items) > 3 and random.random() < 0.3:
            return random.choice(items)
        items = sorted(items, key=lambda v: -sticker_score(v))[:6]
        return random.choice(items)

    def ensure_sticker_size(self, s):
        """发送前确认有真实宽高——emoji_img_info 里的 0x0 会让手机端
        按默认比例把表情裁成一条宽条（电脑端凑合能看，手机端就歪了）。
        量到就写回内存里的这一条，下次直接复用。"""
        if not isinstance(s, dict):
            return s
        u = (s.get("url") or "").strip()
        if not u or ((s.get("w") or 0) > 0 and (s.get("h") or 0) > 0):
            return s
        try:
            w, h = self.client.measure_images([u]).get(u, (0, 0))
        except Exception as e:
            log("量表情尺寸失败: %s" % e, "WARN")
            return s
        if w and h:
            s["w"], s["h"] = w, h
        return s

    def sync_sticker_size(self):
        """启动时把收藏里缺尺寸的表情一次性补全（老收藏全是 0x0，发出去手机端会歪）"""
        book = self.sticker_book()
        todo = [(k, v.get("url")) for k, v in book.items()
                if isinstance(v, dict) and v.get("url")
                and not ((v.get("w") or 0) and (v.get("h") or 0))]
        if not todo:
            return
        try:
            got = self.client.measure_images([u for _, u in todo])
        except Exception as e:
            log("补表情尺寸失败: %s" % e, "WARN")
            return
        n = 0
        for k, u in todo:
            w, h = got.get(u, (0, 0))
            v = book.get(k)
            if w and h and isinstance(v, dict):
                v["w"], v["h"] = w, h
                n += 1
        if n and write_memory(self.mem):
            self.ai.mem = self.mem
            self.mem_mtime = self._mtime(MEM_PATH)
            log("补全表情尺寸：%d 张（手机端才不会被裁）" % n)

    def deliver_sticker(self, g, s):
        s = self.ensure_sticker_size(s)
        if g.get("kind") == "dm":
            return self.client.dm_send_sticker(g["gid"], s.get("url"),
                                               s.get("w") or 0, s.get("h") or 0)
        return self.client.send_sticker(g["gid"], s.get("url"),
                                        s.get("w") or 0, s.get("h") or 0)

    def mark_sent(self, s):
        """记一笔「这张表情真的发出去过」，下次排序会偏向常用的"""
        url = s.get("url") or ""
        key = self.sticker_key(url)
        if not key:
            return
        d = read_memory()
        v = ((d.get("emoji") or {}).get("sticker") or {}).get(key)
        if not isinstance(v, dict):
            return
        v["sent"] = int(v.get("sent") or 0) + 1
        v["last_sent"] = time.time()
        if write_memory(d):
            self.mem = d
            self.ai.mem = d
            self.mem_mtime = self._mtime(MEM_PATH)

    def sync_emoji_map(self):
        """把小表情清单（码 → 图标地址）缓存给面板用。失败不影响聊天。"""
        expr = r"""
        (async function(){
          try{
            var m = __wr.m || {}, cands = [];
            for (var id in m){
              var s = '';
              try{ s = m[id].toString(); }catch(e){ continue; }
              if (s.indexOf('/bbs/app/api/emojis/list') < 0) continue;
              var ex = null;
              try{ ex = __wr(id); }catch(e){ continue; }
              for (var k in (ex||{})){
                if (typeof ex[k] === 'function') cands.push(ex[k]);
              }
            }
            if (!cands.length) return JSON.stringify({ok:false, err:'客户端里没找到表情清单接口'});
            // 同一个模块里第一个函数不一定是取列表的那个，挨个试到有 emoji_groups 为止
            for (var i=0;i<cands.length;i++){
              try{
                var r = await cands[i]();
                var d = (r && r.data) || r || {};
                var res = d.result || d.data || {};
                var gs = res.emoji_groups;
                if (!gs || !gs.length) continue;
                var out = {};
                gs.forEach(function(g){
                  var gc = g.group_code || g.group_name || 'cube';
                  (g.emojis || []).forEach(function(e){
                    if (e.code && e.img) out[gc + '_' + e.code] = e.img;
                  });
                });
                return JSON.stringify({ok:true, n:Object.keys(out).length, map:out});
              }catch(e){}
            }
            return JSON.stringify({ok:false, err:'表情清单接口没返回 emoji_groups'});
          }catch(e){
            return JSON.stringify({ok:false, err:String((e&&e.message)||e)});
          }
        })()
        """
        try:
            d = self.cdp.eval_json(expr, timeout=30)
        except Exception as e:
            log("读取表情清单失败（不影响回复）: %s" % e, "WARN")
            return
        if not d.get("ok"):
            log("读取表情清单失败（不影响回复）: %s" % d.get("err"), "WARN")
            return
        mp = d.get("map") or {}
        try:
            old = {}
            if os.path.exists(EMOJI_MAP_PATH):
                with open(EMOJI_MAP_PATH, "r", encoding="utf-8") as f:
                    old = (json.load(f) or {}).get("map") or {}
            if old == mp:
                return
            tmp = EMOJI_MAP_PATH + ".%d.tmp" % os.getpid()
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "map": mp}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, EMOJI_MAP_PATH)
            log("已缓存 %d 个小表情图标" % len(mp))
        except Exception as e:
            log("写 emoji_map.json 失败: %s" % e, "WARN")

    def handle_learn_request(self, groups):
        req = float(self.mem.get("learnRequested") or 0)
        done = float(self.mem.get("learnedAt") or 0)
        if req <= done:
            return
        log("收到网页的手动「整理记忆」请求")
        for g in groups:
            try:
                self.ai.learn(g["gid"], self.label(g))
            except Exception as e:
                log("记忆整理失败 %s: %s" % (self.label(g), e), "WARN")
        d = read_memory()
        d["learnedAt"] = time.time()      # 无论成败都标记，避免每轮重复触发
        write_memory(d)
        self.mem = read_memory()
        self.ai.mem = self.mem
        self.mem_mtime = self._mtime(MEM_PATH)

    def handle_users_learn_request(self, groups):
        req = float(self.mem.get("usersLearnRequested") or 0)
        done = float(self.mem.get("usersLearnedAt") or 0)
        if req <= done:
            return
        log("收到网页的手动「整理档案」请求")
        for g in groups:
            convo = []
            try:
                r = self.fetch(g, 30)
                msgs = (r.get("list") or []) if r.get("ok") else []
                for m in sorted(msgs, key=lambda x: int(x.get("seq") or 0))[-30:]:
                    if m.get("uid") == self.client.self_id:
                        continue
                    txt = clean_text(m.get("msg") or "")[:120]
                    if not txt:
                        continue
                    convo.append("（%s｜UID %s）%s" % (m.get("name") or "未知名", m.get("uid"), txt))
            except Exception as e:
                log("读取消息失败 %s: %s" % (self.label(g), e), "WARN")
            try:
                self.ai.learn_users(g["gid"], self.label(g), convo or None)
            except Exception as e:
                log("档案整理失败 %s: %s" % (self.label(g), e), "WARN")
        d = read_memory()
        d["usersLearnedAt"] = time.time()   # 无论成败都标记，避免每轮重复触发
        write_memory(d)
        self.mem = read_memory()
        self.ai.mem = self.mem
        self.mem_mtime = self._mtime(MEM_PATH)

    # 给监听网页用的心跳文件
    def write_state(self, groups):
        data = {
            "ts": time.time(),
            "pid": os.getpid(),
            "started": self.started,
            "selfId": self.client.self_id,
            "selfName": self.client.self_name,
            "trigger": self.trig.get("mode"),
            "model": self.ai.model,
            "enabled": bool(self.mem.get("enabled", True)),
            "groupOn": bool(self.group_on),
            "dmOn": bool(self.dm_on),
            "replied": self.replied,
            "triggers": self.triggers,
            "agent": bool(self.ai.agent),
            "mood": {"v": round(_f(self.ai.mood.get("v"), MOOD_BASE), 1),
                     "band": mood_band(self.ai.mood.get("v")),
                     "e": {k: round(_f(v, 0.0), 1)
                           for k, v in (self.ai.mood.get("e") or {}).items()},
                     "on": bool(self.ai.mood_on),
                     # 分是怎么来的：基线 + 此刻被推离多少 + 没人理多久掉一半
                     "base": round(MOOD_BASE, 1),
                     "calmHours": round(MOOD_HALF / 3600.0, 2),
                     "emoMinutes": round(EMO_HALF / 60.0, 1),
                     # 判定节奏：三个旋钮 + 现在卡在哪
                     "every": self.ai.mood_every,
                     "byTurn": bool(self.ai.mood_by_turn),
                     "byTime": bool(self.ai.mood_by_time),
                     "gapMinutes": round(self.ai.mood_gap / 60.0, 1),
                     "turn": self.ai._mood_turn,
                     "last": self.ai._mood_last,
                     "why": self.ai.mood_why(),
                     "log": [{"t": e.get("t"), "d": e.get("d"), "r": e.get("r"),
                              "c": e.get("c"), "e": e.get("e")}
                             for e in (self.ai.mood.get("log") or [])[-6:]]},
            "directHits": self.direct_hits,
            "direct": {"enabled": bool(self.direct), "rate": int(self.direct_rate),
                       "gap": round(self.direct_gap, 1),
                       "adminReply": bool(self.admin_reply),
                       "adminBypass": bool(self.admin_bypass)},
            "bubble": {"enabled": bool(self.bubble_on), "bubbles": self.bubbles,
                       "lastBubble": self.last_bubble_ts,
                       "idle": round(self.bubble_idle / 60.0, 1),
                       "gap": round(self.bubble_gap / 60.0, 1),
                       "chance": int(self.bubble_chance)},
            "ping": {"enabled": bool(self.ping_on), "pings": self.pings,
                     "lastPing": self.last_ping_ts,
                     "idle": round(self.ping_idle / 60.0, 1),
                     "idleSec": int(self.ping_idle),
                     "gap": round(self.ping_gap / 60.0, 1),
                     "every": round(self.ping_every / 60.0, 1),
                     "daily": self.ping_daily,
                     "window": [self.ping_h1, self.ping_h2],
                     "uids": sorted(self.ping_uids),
                     "peers": [{"uid": p.get("uid"), "name": p.get("name"),
                                "ts": p.get("ts"), "text": p.get("text"),
                                "n": p.get("n"), "ignored": p.get("ignored")}
                               for p in self.ping_log[:6]]},
            "groupPing": {"enabled": bool(self.gp_on), "says": self.gp_says,
                          "last": self.last_gp_ts,
                          "ids": sorted(self.gp_ids),
                          "every": round(self.gp_every / 60.0, 1),
                          "gap": round(self.gp_gap / 60.0, 1),
                          "daily": self.gp_daily,
                          "idleSec": int(self.gp_idle),
                          "window": [self.gp_h1, self.gp_h2],
                          "recent": [{"gid": x.get("gid"), "name": x.get("name"),
                                      "ts": x.get("ts"), "text": x.get("text"),
                                      "n": x.get("n"), "post": x.get("post")}
                                     for x in self.gp_log[:6]]},
            "control": {"enabled": bool(self.control_on), "prefix": self.control_prefix,
                        "nl": bool(self.control_nl),
                        "uids": sorted(self.control_uids), "cmds": self.cmds,
                        "recent": [{"uid": u, "name": v.get("name") or "",
                                    "ts": v.get("ts") or 0}
                                   for u, v in sorted(self.recent_dm.items(),
                                                      key=lambda kv: -(kv[1].get("ts") or 0))[:6]]},
            "groups": [{"gid": g["gid"], "name": g["name"], "kind": g.get("kind", "group")}
                       for g in groups],
            # 关机收尾的进度：面板拿它显示「还剩 N 秒」，active 变 false 就不用再等
            "farewell": self._farewell or {"active": False},
        }
        try:
            tmp = STATE_PATH + ".%d.tmp" % os.getpid()
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, STATE_PATH)
        except Exception:
            pass

    def targets(self):
        gs = self.client.groups()
        mode = (self.groups_cfg.get("mode") or "all").lower()
        if mode == "list":
            want = set(str(x) for x in (self.groups_cfg.get("ids") or []))
            gs = [g for g in gs if g["gid"] in want]
        for g in gs:
            g["kind"] = "group"
        out = list(gs)

        # 好友私聊也当成一种会话加进来。
        # 这里不再看 dms.enabled —— 那个开关现在只管「回不回话」（见 say_ok）。
        # 关掉时私聊仍然要读，否则它不知道对方这些天说了什么，重新打开会接不上。
        try:
            ds = self.client.dms()
        except Exception as e:
            log("读取好友会话失败: %s" % e, "WARN")
            ds = []
        want = set(str(x) for x in (self.dms_cfg.get("ids") or []))
        if want:
            ds = [d for d in ds if d["uid"] in want]
        for d in ds:
            out.append({"kind": "dm", "gid": d["uid"],
                        "name": d["name"] or ("UID " + d["uid"]),
                        "last": d.get("last", ""), "time": d.get("time", 0)})
        return out

    @staticmethod
    def label(t):
        return t["name"] if t.get("kind") == "group" else "与%s的私聊" % t["name"]

    def fetch(self, t, limit):
        if t.get("kind") == "dm":
            return self.client.dm_messages(t["gid"], limit, t.get("name") or "")
        return self.client.messages(t["gid"], limit)

    def say_ok(self, is_dm):
        """这条通道现在让不让它说话。关掉时它仍然在听，只是不出声。"""
        return self.dm_on if is_dm else self.group_on

    def img_quota(self, gid):
        """能不能再看一张图。滑动 60 秒窗口，全局一份、每个会话一份。"""
        now = time.time()
        self._img_hits = [t for t in self._img_hits if now - t < 60]
        per = self._img_hits_g.setdefault(str(gid), [])
        keep = [t for t in per if now - t < 60]
        self._img_hits_g[str(gid)] = keep
        if len(self._img_hits) >= self.img_per_min or len(keep) >= self.img_per_min:
            return False
        self._img_hits.append(now)
        keep.append(now)
        return True

    def img_line(self, m, gid, allow_api=True):
        """把一条图片消息变成一句上下文。

        看不了也要留个痕迹（「（发了张图）」），别让它以为自己什么都没收到——
        什么都不说，比说一句「没看清」更像瞎。
        """
        url = str(m.get("img") or "").strip()
        if not self.img_on or not url:
            return "（发了一张图）"
        if not allow_api:
            # 这条以前看过（或本轮不打算再看）：只翻缓存，不打接口
            cached = img_cache_load().get(url)
            return "（发了张图：%s）" % cached if cached else "（发了一张图）"
        if not self.img_quota(gid):
            log("看图额度用完（每分钟 %d 张），这张先跳过" % self.img_per_min, "INFO")
            cached = img_cache_load().get(url)
            return "（发了张图：%s）" % cached if cached else "（发了一张图，还没看清）"
        try:
            desc = self.ai.describe_image(url, timeout=self.img_wait)
        except Exception as e:
            log("看图失败: %s" % str(e)[:120], "WARN")
            desc = ""
        if not desc:
            return "（发了一张图，没看清）"
        log("看了张图：%s" % desc[:60])
        return "（发了张图：%s）" % desc

    def quote_line(self, m, gid):
        """把「他在回复哪条消息」翻成一句话，塞进正文最前面。

        客户端在 m["reply"] 里本来就给了被引用那条的 seq/msg/img ——
        以前没读，模型只能从最近几条里挑一张最像的，于是「这个图什么意思」
        经常答成群里另一张图。带上引用内容，它才知道「这个图」指哪张。
        """
        rp = m.get("reply") or {}
        if not isinstance(rp, dict) or not rp:
            return ""
        qtype = int(rp.get("type") or 0)
        qimg = str(rp.get("img") or "").strip()
        qtext = clean_text(rp.get("msg") or "")
        me = str(rp.get("uid") or "") == str(self.client.self_id)
        who = "你" if me else (rp.get("name") or "对方")
        if qimg and qtype in (3, 7):
            kind = "表情包" if qtype == 7 else "图片"
            desc = img_cache_load().get(qimg) or ""
            if not desc and self.img_on and self.img_quota(gid):
                try:
                    desc = self.ai.describe_image(qimg, timeout=self.img_wait) or ""
                except Exception as e:
                    log("看被引用的图失败: %s" % str(e)[:100], "WARN")
                if desc:
                    img_cache_put(qimg, desc)
            if desc:
                return "（引用%s发的%s：%s）" % (who, kind, _one_line(desc, 70))
            return "（引用%s发的一张%s）" % (who, kind)
        if qtext:
            return "（引用%s说的：「%s」）" % (who, _one_line(qtext, 60))
        return ""

    def deliver(self, t, text):
        is_dm = t.get("kind") == "dm"
        if not self.say_ok(is_dm):
            # 兜底。正常情况下每条发送路径前面都拦过了，
            # 走到这里说明有新路径漏了开关——记一条日志，别静默吞掉。
            log("%s通道已关闭，这条不发：%s"
                % ("私聊" if is_dm else "群聊", self.label(t)), "WARN")
            return {"ok": False, "msg": "channel off"}
        if is_dm:
            return self.client.dm_send(t["gid"], text)
        return self.client.send(t["gid"], text)

    def init_baseline(self, groups):
        for g in groups:
            d = self.fetch(g, 5)
            seq = 0
            if d.get("ok"):
                for m in d.get("list", []):
                    seq = max(seq, int(m["seq"] or 0))
            self.baseline[g["gid"]] = seq
            # 私聊主动要知道「他上次开口是什么时候」。启动时先按现有记录给个基准，
            # 否则刚开机就会把一堆早就停住的老会话当成「沉默很久」，一口气全找一遍。
            if g.get("kind") == "dm" and d.get("ok"):
                msgs = d.get("list") or []
                if msgs:
                    last = max(msgs, key=lambda m: _ts(m.get("time")))
                    ts = _ts(last.get("time"))
                    self.last_peer[g["gid"]] = ts or time.time()
                    self.last_from[g["gid"]] = ("me" if last["uid"] == self.client.self_id
                                                else "them")
        log("基线就绪：%d 个会话（群 %d / 私聊 %d），历史消息不再回复" % (
            len(groups), sum(1 for g in groups if g.get("kind") == "group"),
            sum(1 for g in groups if g.get("kind") == "dm")))

    # ---- 开机补课：关机期间有人找过它 ----
    @staticmethod
    def _last_alive():
        """上一次活着的时间。state.json 每轮都会写，进程被关掉也就停在那里了。"""
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return float((json.load(f) or {}).get("ts") or 0.0)
        except Exception:
            return 0.0

    def catch_up(self, groups):
        """开机补课：关机/退出这段时间别人说的话，回来接一句。

        原来的规矩是「启动就把 baseline 推到最新，历史一律当没看见」——好处是
        重启不会补发积压的 @，坏处是关机一夜，第二天开机它像失忆：消息进了
        上下文，却没有一句回应。这里补一道闸：离线够久才补（默认 5 分钟以上），
        太久远的也不补（默认 12 小时），每个会话最多补一条——免得把「重启五秒」
        也当成久别重逢。
        """
        c = self.cfg.get("catchUp") or {}
        if not c.get("enabled", True):
            return 0
        if not self.mem.get("enabled", True):
            return 0        # 总开关关着，补课也不开口
        last = self._last_alive()
        gap = time.time() - last if last else 0.0
        after = max(60, int(float(c.get("afterMinutes", 5) or 5) * 60))
        before = max(after, int(float(c.get("maxHours", 12) or 12) * 3600))
        if gap < after:
            return 0
        if gap > before:
            log("离线了 %.1f 小时，超过补回上限 %d 小时，这次不补"
                % (gap / 3600.0, before // 3600))
            return 0
        log("离线了 %s，看看这期间有没有人找过它" % _dur(gap))
        n = 0
        for g in groups:
            try:
                if self._catch_one(g, last, gap):
                    n += 1
            except Exception as e:
                log("补课失败 %s: %s" % (self.label(g), e), "WARN")
        if n:
            log("补回 %d 个会话" % n)
        return n

    def _catch_one(self, g, last, gap):
        gid = g["gid"]
        is_dm = g.get("kind") == "dm"
        if not self.say_ok(is_dm):
            return False
        d = self.fetch(g, 40)
        if not d.get("ok"):
            return False
        msgs = sorted(d.get("list") or [], key=lambda m: int(m.get("seq") or 0))
        fresh = [m for m in msgs if _ts(m.get("time")) > last]
        if not any(m["uid"] != self.client.self_id for m in fresh):
            return False
        # 先让它「看见」这一段（进 recent / talkLog），回话时才有上下文。
        # 这一步以前只在正常轮询里做，所以开机补课如果不补，模型接的是一句没头没尾的话。
        # _last_seq 从已有的 talkLog 里对齐：关机前录过的别重复灌一遍。
        seqs = [int(e.get("seq") or 0) for e in (self.talk.get(gid) or [])]
        if seqs:
            self._last_seq[gid] = max(seqs)
        self._log_talk(gid, [{"seq": str(m.get("seq") or ""),
                              "t": _ts(m.get("time")) or time.time(),
                              "name": m.get("name") or "",
                              "uid": str(m.get("uid") or ""),
                              "me": m["uid"] == self.client.self_id,
                              "text": self._msg_text(m)[:120]}
                             for m in fresh if self._msg_text(m)])
        # 谁在等它：私聊是对方说的每一句，群里只认 @（别在自己不在的时候插话）
        todo = [m for m in fresh if m["uid"] != self.client.self_id]
        if not is_dm:
            todo = [m for m in todo
                    if is_trigger(self.trig, m.get("msg") or "",
                                  self.client.self_id, self.client.self_name,
                                  reply_uid=(m.get("reply") or {}).get("uid"))]
        if not todo:
            return False
        m = todo[-1]
        if int(m.get("type") or 0) == 3 and str(m.get("img") or "").strip():
            seen = self.img_line(m, gid)
            body = clean_text(m.get("msg") or "")
            body = ("%s %s" % (body, seen)).strip() if body else seen
        else:
            body = clean_text(m.get("msg") or "")
        if not body:
            body = "（发了张图）"
        # 冷却本来防刷屏，启动时不该挡住补课那条；补完一条再放开，
        # 不然第二个会话会被全局 1 秒冷却吃掉。
        self.last_reply.pop(gid, None)
        self.last_any = 0.0
        ctx = ("【你刚回来】你离开了 %s。这段你刚才没看见，现在看见了；"
               "对方最后这句还等着你回，别提「离线」「关机」这类词。" % _dur(gap))
        log("补课 %s：%s" % (self.label(g), body[:50]))
        self.handle(g, m, body, ctx=ctx)
        # 补完这句，就算「我刚说过话」：私聊主动那边是按 last_peer 算安静时长的，
        # 不更新的话，白天补完课一分钟内它会以为对方半天没动静、又去找他一次。
        self.last_from[gid] = "me"
        self.last_peer[gid] = time.time()
        return True

    def run_once(self, groups):
        for g in groups:
            gid = g["gid"]
            is_dm = g.get("kind") == "dm"
            d = self.fetch(g, 15)
            if not d.get("ok"):
                log("读取失败 %s: %s" % (self.label(g), d.get("msg")), "WARN")
                continue
            msgs = sorted(d.get("list", []), key=lambda m: int(m["seq"] or 0))
            # 先把这一轮看见的消息记进上下文，再决定回哪句——
            # 这样同一批里别人说的话，它回话时就看得见了
            self._log_talk(gid, [
                {"seq": str(m.get("seq") or ""),
                 "t": float(m.get("time") or 0) or time.time(),
                 "name": m.get("name") or "",
                 "uid": str(m.get("uid") or ""),
                 "me": m["uid"] == self.client.self_id,
                 "text": self._msg_text(m)[:120]}
                for m in msgs[-24:]
                if self._msg_text(m)])
            base = self.baseline.get(gid, 0)
            for m in msgs:
                seq = int(m["seq"] or 0)
                if seq <= base:
                    continue
                self.baseline[gid] = max(self.baseline.get(gid, 0), seq)
                # 有人说话（哪怕只是张图、只是撤回）就算热闹过，冷场计时从这里重来
                self.last_activity[gid] = time.time()
                if is_dm:
                    # 私聊主动靠这两个字段判断「该不该找他」和「上次是不是被晾着」
                    if m["uid"] == self.client.self_id:
                        self.last_from[gid] = "me"
                    else:
                        self.last_from[gid] = "them"
                        self.last_peer[gid] = time.time()
                if m["uid"] == self.client.self_id:
                    # 它自己说的话：记下来，等着看有没有人接（心情那条线要用）
                    self.watch_own(gid, m)
                    continue
                # 群友档案里的角色：黑名单直接当没看见（不登记表情、@ 了也不理）
                role = self.role_of(m.get("uid"))
                # 别人说了一句：是不是在接它上一句？是就高兴一下，不是就继续等
                if role != "block":
                    try:
                        self.note_replied(gid, m, is_dm)
                    except Exception as e:
                        log("判断接话失败: %s" % e, "WARN")
                # 登记发言者（UID + 当前昵称），改名后也能认出是同一个人
                try:
                    self.track_user(gid, m["uid"], m["name"])
                except Exception as e:
                    log("登记群友失败 UID %s: %s" % (m.get("uid"), e), "WARN")
                # 表情在类型过滤之前就收：不回复的消息里也藏着表情，正好拿来收藏
                if role != "block":
                    try:
                        self.observe_emoji(m)
                    except Exception as e:
                        log("记录表情失败 UID %s: %s" % (m.get("uid"), e), "WARN")
                # 私聊指令：/send /img 这些归控制台处理，不进 AI；
                # 别人私聊发来的图先记下，等主人用 /img 转到群里（黑名单除外）
                if is_dm and self.control_on and role != "block":
                    try:
                        if self.try_control(g, m):
                            continue
                    except Exception as e:
                        log("私聊指令执行失败: %s" % e, "WARN")
                        continue
                # msg_type: 4=纯文本, 10=带 @ 的文本（客户端里 @ 消息实测是 10）, 3=图片, 30=撤回
                # 图片消息单独放进来：带 img 地址的 type=3 算「发了张图」，走下面的识别+接话
                is_img = (m["type"] == 3 and bool(str(m.get("img") or "").strip()))
                if m["type"] not in (4, 10) and not is_img:
                    continue
                # 总开关关掉时，消息照样消费掉（推进 baseline），只是不回复，
                # 这样恢复后不会把暂停期间积压的 @ 一次性补发
                if not self.mem.get("enabled", True):
                    continue
                if role == "block":
                    continue
                # 通道开关：只听不说。上面的上下文、群友登记、表情收藏都已经记完了，
                # 到这里才决定开不开口——所以静音期间的事它仍然知道。
                if not self.say_ok(is_dm):
                    continue
                # 私聊是一对一，对方发来的每条都算对你说；群里才需要 @ / 前缀
                if is_dm:
                    if not self.dms_cfg.get("alwaysReply", True):
                        if not is_trigger(self.trig, m["msg"], self.client.self_id,
                                          self.client.self_name,
                                          reply_uid=(m.get("reply") or {}).get("uid")):
                            continue
                    m["_direct"] = False
                    self.triggers += 1
                else:
                    q_uid = (m.get("reply") or {}).get("uid")
                    if is_trigger(self.trig, m["msg"], self.client.self_id,
                                  self.client.self_name, reply_uid=q_uid):
                        # 点「回复」它那条说话：没 @ 也算叫到，日志里分开记一笔，
                        # 免得以后看日志分不清是 @ 来的还是点回复来的
                        m["_rep"] = (bool(q_uid) and str(q_uid) == str(self.client.self_id)
                                     and not AT_RE.search(m.get("msg") or ""))
                        m["_direct"] = False
                        # 管理被 @ 也算「管理命令」，同样走 100% 听取（不等冷却）
                        m["_admin"] = (role == "admin")
                        self.triggers += 1
                    elif role == "admin":
                        # 管理身份：默认不抽概率、说话就接；关掉开关后跟普通群友一样按概率
                        if self.admin_reply:
                            m["_direct"] = False
                            m["_admin"] = True
                            self.triggers += 1
                        elif self.direct_hit():
                            m["_direct"] = True
                            self.direct_hits += 1
                        else:
                            continue
                    elif self.direct_hit():
                        # 群聊直接回复：没 @ 它，按概率接话（仍受群冷却限制）
                        m["_direct"] = True
                        self.direct_hits += 1
                    else:
                        continue
                body = clean_text(m["msg"])
                if is_img:
                    # 图片消息：先看一眼图，把内容拼成上下文。配了文字（body 非空）就「字 + 图」，
                    # 纯图就只有图的描述。看不了也留一句「发了一张图」的痕迹，别让它当没收到。
                    seen = self.img_line(m, gid)
                    body = ("%s %s" % (body, seen)).strip() if body else seen
                    if not body:
                        body = "（发了一张图）"
                if m.get("reply") and not is_img:
                    # 「这个图什么意思」这种话，指代全靠引用。不带上的话它只能猜。
                    qline = self.quote_line(m, gid)
                    if qline:
                        body = ("%s %s" % (qline, body)).strip()
                if not is_dm and self.trig.get("mode") == "prefix":
                    p = (self.trig.get("prefix") or "").strip()
                    if p and body.startswith(p):
                        body = body[len(p):].strip()
                if not body:
                    body = "（只@了你一下）"
                self.handle(g, m, body)
    def direct_hit(self):
        """群聊直接回复：按概率决定这条没 @ 的消息要不要接。
        概率之外还有一道时间闸——两次主动插话至少隔 direct_gap 秒，
        不然 20% 在快节奏群里也会变成刷屏。@ 它的消息不受这里管。"""
        if not self.direct:
            return False
        r = self.direct_rate
        if r <= 0:
            return False
        gap = self.direct_gap
        if gap > 0:
            quiet = time.time() - self.last_direct
            if quiet < gap:
                return False
        if r < 100 and random.random() * 100 >= r:
            return False
        self.last_direct = time.time()
        return True

    # ---- 输出收拾（回复和冒泡共用） ----
    def _tidy_answer(self, answer, dm=False, uid="", name=""):
        """把模型输出收拾成一条能直接发的话：去换行引号、抠表情、抠「记住」、处理残句和超长。
        dm=True 用私聊的天花板（比群里宽得多）；这里只是兜底，正常不该触发。
        uid 有值时，回复里带的 [[记:…]] 会写进这个人的便签。
        返回 (正文, 大表情, 这次真记下几条)。"""
        answer = (answer or "").replace("\n", " ").strip()
        # [[记:…]] 是给程序看的，先抠出来——绝不能让它发到群里
        answer, memos = take_memo(answer)
        answer = re.sub(r"^[\"'“”]+|[\"'“”]+$", "", answer)
        # 先把模型点名的大表情抠出来，剩下的当正文
        answer, picked = self.take_stickers(answer)
        if self.ai.last_finish == "length":
            # 模型自己撞到 maxTokens，最后一句本来就没写完，按标点把残句收掉
            log("模型输出撞到 maxTokens 上限，已按标点收尾", "WARN")
            answer = tidy_tail(answer)
        cap = self.dm_max_chars if dm else self.max_chars
        if cap and len(answer) > cap:
            whole = len(answer)
            answer = clip_reply(answer, cap)
            log("回复 %d 字超过上限 %d，断在标点处收成 %d 字" % (
                whole, cap, len(answer)))
        kept = 0
        if uid:
            for x in memos[:2]:      # 一条回复最多记两件，防它一次刷一堆
                if self.ai.memo_for(uid, x, name):
                    kept += 1
        return answer, picked, kept

    def _emoji_supplement(self, answer, picked):
        """模型没主动用表情时，按概率补一个：小表情拼进正文，大表情另发一条"""
        extra = None
        model_used_small = bool(SMALL_EMOJI_RE.search(answer))
        if (self.emoji_on and not picked and not model_used_small
                and self.emoji_rate > 0 and random.random() * 100 < self.emoji_rate):
            got = self.pick_emoji()
            if got and got[0] == "small" and got[1]:
                answer = (answer + " " + got[1]).strip()
            elif got and got[0] == "sticker":
                extra = got[1]
        return answer, extra

    # ---- 心情自己平复 ----
    def maybe_settle_mood(self):
        """隔一阵看一次：没人说话，心情也该往回走。

        心跳不用太密——衰减是按墙上时间算的，两分钟对齐一次和每秒对齐
        结果一样，只是省 CPU。跨了一个档才记日志，免得刷屏。
        """
        if not self.ai.mood_on:
            return
        now = time.time()
        if now - self._mood_settle < 120:
            return
        self._mood_settle = now
        before = _f(self.ai.mood.get("v"), MOOD_BASE)
        band0 = mood_band(before)
        moved = self.ai.settle_mood(now)
        after = _f(self.ai.mood.get("v"), MOOD_BASE)
        if moved >= 0.5 and mood_band(after) != band0:
            log("没人说话，心情自己平复：%s → %s（%.0f → %.0f）"
                % (band0, mood_band(after), before, after))
        # 顺便看一眼：刚才说出去的话，有没有人接
        self.check_ignored(now)

    # ---- 说了话，有没有人接 ----
    def _mood_event(self, delta, emo, reason, conf=80.0):
        """给心情直接记一笔，不走模型。
        「有人接话 / 没人接话」这种事一眼就能判断，没必要花一次调用去问。
        记的这一笔会落进 mood.log —— 下一次回话时 mood_prompt 会把它带出来，
        口气就跟着变了；不写进嘴的是分值，写进去的是「为什么」。"""
        if not self.ai.mood_on:
            return
        before = _f(self.ai.mood.get("v"), MOOD_BASE)
        b0 = mood_band(before)
        self.ai.mood, moved = mood_apply(
            self.ai.mood, {"d": delta, "e": emo, "r": reason, "c": conf})
        if not moved:
            return
        self.ai.save_mood(force=True)
        v = _f(self.ai.mood.get("v"), MOOD_BASE)
        log("心情：%s（%+.1f）→ %.0f（%s）" % (reason, delta, v, mood_band(v)))

    def watch_own(self, gid, m):
        """它刚说了一句，记下来等着看有没有人接。"""
        txt = (self._msg_text(m) or "").strip()
        if not txt:
            return
        self.await_reply[gid] = {"ts": float(m.get("time") or 0) or time.time(),
                                 "text": txt[:160], "sad": False}

    def note_replied(self, gid, m, is_dm=False):
        """别人说话了：看这句是不是在接它。

        判三段，从硬到软：引用它 / @ 它 / 说到了它上一条里的词。
        私聊不用判——对方发过来就是在跟它说话。"""
        a = self.await_reply.get(gid)
        if not a:
            return
        now = time.time()
        t = float(m.get("time") or 0) or now
        if t < float(a.get("ts") or 0) - 2:
            return                      # 比它那条还早，不算接话
        if now - float(a.get("ts") or now) > AWAIT_WATCH:
            self.await_reply.pop(gid, None)
            return
        rp = m.get("reply") or {}
        if is_dm:
            # 私聊里对方回一句就算接上。给的分比群里小：一对一本来就该有来有回，
            # 每句都 +5 会把它夸到天上，真实的起伏还得靠 judge_mood 那句判断。
            self._mood_event(2.0, {"开心": 1.4, "踏实": 0.5, "难过": -1.2},
                             "他回话了，心里松了一下")
            self.touch_aff(m.get("uid"), m.get("name"), 1.2, "私聊里回它话")
            self.await_reply.pop(gid, None)
            return
        strong = str(rp.get("uid") or "") == str(self.client.self_id)
        if not strong:
            strong = is_trigger(self.trig, m.get("msg") or "",
                               self.client.self_id, self.client.self_name)
        if strong:
            # 被人接住了：刚才那点闷跟着散掉，还多给一点
            self._mood_event(5.0, {"开心": 2.6, "踏实": 0.8, "难过": -2.2},
                             "说的话有人接，心里松开了")
            self.touch_aff(m.get("uid"), m.get("name"), 2.5, "接上了它的话")
            self.await_reply.pop(gid, None)
            return
        # 软判断：有没有接着它上一条里的词说（「虎天帝」这种一眼能认出来的）
        mine = set(_words(a.get("text") or ""))
        theirs = set(_words(clean_text(m.get("msg") or "")))
        if mine & theirs:
            self._mood_event(2.0, {"开心": 1.4, "踏实": 0.4, "难过": -1.0},
                             "好像有人接着它的话说了")
            self.touch_aff(m.get("uid"), m.get("name"), 1.2, "顺着它的话往下说")
            self.await_reply.pop(gid, None)

    def check_ignored(self, now):
        """说了话一直没人接 —— 失落一下。一次只说一次，别追着难受。"""
        for gid, a in list(self.await_reply.items()):
            ts = float(a.get("ts") or now)
            if now - ts > AWAIT_WATCH:
                self.await_reply.pop(gid, None)
                continue
            if a.get("sad") or now - ts < AWAIT_SAD_AFTER:
                continue
            a["sad"] = True
            self._mood_event(-4.0, {"难过": 2.2, "心疼": 0.6, "开心": -2.0, "甜蜜": -1.0},
                             "说了话，等了一会儿没人接")

    # ---- 好感度 ----
    def aff_map(self):
        a = self.mem.get("aff")
        return a if isinstance(a, dict) else {}

    def aff_of(self, uid):
        rec = self.aff_map().get(str(uid or ""))
        return dict(rec) if isinstance(rec, dict) else {}

    def touch_aff(self, uid, name, delta, why):
        """给某个人加减一点好感度。只改内存，攒一会儿再统一落盘。"""
        uid = str(uid or "").strip()
        if not uid or uid == str(self.client.self_id):
            return
        now = time.time()
        a = dict(self.aff_map())
        rec = dict(a.get(uid) or {})
        aff_decay(rec, now)
        rec["v"] = max(AFF_LO, min(AFF_HI, _f(rec.get("v"), AFF_BASE) + delta))
        rec["n"] = int(rec.get("n") or 0) + 1
        if name:
            rec["name"] = str(name)[:40]
        rec["why"] = why
        rec["at"] = now
        a[uid] = rec
        self.mem["aff"] = a
        self._aff_dirty = True
        if abs(delta) >= 2.5:
            self.flush_aff(force=True)

    def flush_aff(self, force=False):
        """落盘。先读最新再改这一个字段，别拿旧快照整份盖掉别人的写入。
        顺带给所有人按时间回落一次——一周不联系的人自己退回「眼熟」。"""
        if not self._aff_dirty:
            return
        now = time.time()
        if not force and now - self._aff_flush < 45:
            return
        self._aff_flush = now
        d = read_memory()
        aff = d.get("aff") if isinstance(d.get("aff"), dict) else {}
        for rec in aff.values():
            if isinstance(rec, dict):
                aff_decay(rec, now)
        for uid, rec in (self.mem.get("aff") or {}).items():
            if isinstance(rec, dict):
                aff[uid] = rec
        d["aff"] = aff
        if write_memory(d):
            self.mem = d
            self._aff_dirty = False

    def seed_aff(self):
        """群友档案里已经有的人，先给个底分——面板一打开就有底，
        不然要等它跟每个人都说过话才长出第一行。

        底分不是一律 50：档案里记得住的事越多，说明往来越多，起手就更熟
        （50 + 每条记忆 2.5 分，最多加 20）。这样「谁跟它熟」一上来就对得上。"""
        a = dict(self.aff_map())
        now = time.time()
        n = 0
        for uid, v in (self.mem.get("users") or {}).items():
            uid = str(uid)
            if not uid.isdigit():
                continue
            notes = len((v or {}).get("notes") or [])
            seed = min(AFF_BASE + 20.0, AFF_BASE + notes * 2.5)
            cur = a.get(uid)
            if not isinstance(cur, dict):
                a[uid] = {"v": seed, "ts": now, "n": 0,
                          "name": (v or {}).get("name") or "", "why": "", "at": 0.0}
                n += 1
            elif not cur.get("n") and not cur.get("at"):
                # 还没真互动过的底分：按档案重算一次，别让所有人都是 50
                cur["v"] = seed
                cur["ts"] = now
                n += 1
        if n:
            self.mem["aff"] = a
            self._aff_dirty = True
            self.flush_aff(force=True)
            log("好感度：给 %d 位群友起了底分（按档案里的记忆条数，%.0f 分起）" % (n, AFF_BASE))

    # ---- 群聊冒泡 ----
    def maybe_bubble(self, groups):
        """群里冷场以后，按间隔和概率自己找个话头说一句。
        只在群里冒，私聊不插话；暂停回复时也不冒。"""
        if not self.bubble_on or not self.mem.get("enabled", True) or not self.group_on:
            return
        now = time.time()
        if now - self.last_any < self.cd_global:
            return
        cand = None
        for g in groups:
            if g.get("kind") != "group":
                continue
            gid = g["gid"]
            if now - self.last_activity.get(gid, self.started) < self.bubble_idle:
                continue
            # 还没冒过就不必等间隔，只要够冷场就行（首次冒泡不至于要等满两个钟头）
            lb = self.last_bubble.get(gid)
            if lb is not None and now - lb < self.bubble_gap:
                continue
            if now < self.next_bubble_try.get(gid, 0):
                continue
            cand = g
            break
        if not cand:
            return
        gid = cand["gid"]
        # 先掷骰子：不然「冷场」会变成整点报时，一冷就必然开口
        if self.bubble_chance < 100 and random.random() * 100 >= self.bubble_chance:
            self.next_bubble_try[gid] = now + 600     # 没中，过十分钟再问一次
            return
        quiet = int((now - self.last_activity.get(gid, self.started)) / 60)
        try:
            text, _ = self.ai.bubble(gid, self.label(cand), self.bubble_prompt,
                                     _talk_lines(self.recent.get(gid)) , quiet)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:160]
            log("冒泡请求 HTTP %s: %s" % (e.code, detail), "ERROR")
            self.next_bubble_try[gid] = now + 600
            return
        except Exception as e:
            log("冒泡失败: %s" % e, "ERROR")
            self.next_bubble_try[gid] = now + 600
            return
        if not (text or "").strip():
            log("冒泡：模型没给出内容，等会儿再试", "WARN")
            self.next_bubble_try[gid] = now + 600
            return
        answer, picked, _memo_n = self._tidy_answer(text, dm=False)
        answer, extra = self._emoji_supplement(answer, picked)
        if not answer and not picked and not extra:
            log("冒泡：收拾完什么都不剩，跳过", "WARN")
            self.next_bubble_try[gid] = now + 600
            return
        r = self.deliver(cand, answer) if answer else {"ok": True}
        if not r.get("ok"):
            log("冒泡发送失败: %s" % r.get("msg"), "ERROR")
            self.next_bubble_try[gid] = now + 600
            return
        for s in list(picked) + ([extra] if extra else []):
            try:
                rs = self.deliver_sticker(cand, s)
            except Exception as e:
                rs = {"ok": False, "msg": str(e)}
            if rs.get("ok"):
                self.emoji_used += 1
                self.mark_sent(s)
                log("冒泡带表情包 %s" % self.sticker_key(s.get("url")))
            else:
                log("冒泡发表情包失败: %s" % rs.get("msg"), "WARN")
        self.bubbles += 1
        self.last_bubble_ts = now
        self.last_bubble[gid] = now
        self.last_activity[gid] = now      # 自己说话也算有动静，冷场计时重来
        self.last_any = now
        self.next_bubble_try[gid] = now + self.bubble_gap
        if answer:
            self.ai.note_proactive(gid, answer, "bubble")
        log("群聊冒泡 %s（群里安静 %d 分钟）：%s" % (cand["name"], quiet, answer))

    # ---------------------------------------------------------------- 群聊主动
    # 冒泡是「等冷场、按概率、哪个群都可能」；群聊主动是「你点名的群、到点就说」。
    # 闸门：开关 → 勾了群 → 时间窗 → 全局节奏 → 单群间隔 → 每日配额 → 群里够安静。
    # 以后接帖子：往 gp_material[gid] 里塞一段内容，它就会照着看完的感受开口。
    def _save_gp_state(self):
        self.mem["gpSay"] = self.gp_state
        self.mem["gpSayLast"] = self.last_gp_ts
        write_memory(self.mem)

    def maybe_group_ping(self, groups):
        """在你勾的群里主动起个话头。没勾群就一句都不说，这是故意的。"""
        if not self.gp_on or not self.gp_ids or not self.mem.get("enabled", True) \
                or not self.group_on:
            return
        now = time.time()
        if now - self.last_any < self.cd_global:
            return
        if not self._hour_ok(self.gp_h1, self.gp_h2, now):
            return
        # 全局节奏：两个群也不连着说，隔开才像人
        if self.last_gp_ts and now - self.last_gp_ts < self.gp_every:
            return
        today = self._ping_today(now)
        cand = None
        for g in groups:
            if g.get("kind") != "group":
                continue
            gid = str(g["gid"])
            if gid not in self.gp_ids:
                continue
            st = self.gp_state.get(gid) or {}
            if now - float(st.get("last") or 0) < self.gp_gap:
                continue                       # 这个群刚说过，等间隔
            if str(st.get("day") or "") == today and int(st.get("n") or 0) >= self.gp_daily:
                continue                       # 今天在这个群说够了
            quiet = now - self.last_activity.get(gid, self.started)
            if self.gp_idle and quiet < self.gp_idle:
                continue                       # 群里正聊着，别插嘴
            # 挑最久没被主动说过话的那个群，几个群轮着来
            if cand is None or float(st.get("last") or 0) < float((cand[1] or {}).get("last") or 0):
                cand = (g, st, quiet)
        if not cand:
            return
        g, st, quiet = cand
        gid = str(g["gid"])
        quiet_min = int(quiet // 60)
        material = (self.gp_material.get(gid) or "").strip()
        try:
            text, _ = self.ai.group_ping(gid, g.get("name") or "", self.gp_prompt,
                                         _talk_lines(self.recent.get(gid)), quiet_min,
                                         st.get("said") or "", material)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:160]
            log("群聊主动请求 HTTP %s: %s" % (e.code, detail), "ERROR")
            return
        except Exception as e:
            log("群聊主动失败: %s" % e, "ERROR")
            return
        if not (text or "").strip():
            log("群聊主动：模型没给出内容", "WARN")
            return
        answer, picked, _memo_n = self._tidy_answer(text, dm=False)
        answer, extra = self._emoji_supplement(answer, picked)
        if not answer and not picked and not extra:
            log("群聊主动：收拾完什么都不剩，跳过", "WARN")
            return
        r = self.deliver(g, answer) if answer else {"ok": True}
        if not r.get("ok"):
            log("群聊主动发送失败: %s" % r.get("msg"), "ERROR")
            return
        for s in list(picked) + ([extra] if extra else []):
            try:
                rs = self.deliver_sticker(g, s)
            except Exception as e:
                rs = {"ok": False, "msg": str(e)}
            if rs.get("ok"):
                self.emoji_used += 1
                self.mark_sent(s)
            else:
                log("群聊主动发表情包失败: %s" % rs.get("msg"), "WARN")
        if str(st.get("day") or "") != today:
            st["day"] = today
            st["n"] = 0
        st["n"] = int(st.get("n") or 0) + 1
        st["last"] = now
        st["said"] = answer[:120] if answer else ""
        self.gp_state[gid] = st
        self.gp_material.pop(gid, None)      # 帖子只用来开这一次口
        if answer:
            self.ai.note_proactive(gid, answer, "gping")
        self.gp_says += 1
        self.last_gp_ts = now
        self.last_activity[gid] = now
        self.last_bubble[gid] = now          # 冒泡别紧跟着在同一个群又来一句
        self.last_any = now
        self._save_gp_state()
        self.gp_log.insert(0, {"gid": gid, "name": g.get("name") or gid, "ts": now,
                               "text": answer[:60], "n": int(st["n"]),
                               "post": 1 if material else 0})
        del self.gp_log[8:]
        log("群聊主动 %s（安静 %d 分钟，今天第 %d 次%s）：%s" % (
            g.get("name"), quiet_min, st["n"],
            "，照着帖子说的" if material else "", answer))

    # ---------------------------------------------------------------- 私聊主动
    # 群聊冒泡是「冷场了带个气氛」，私聊主动是「我想他了」，两套阈值。
    # 闸门从松到紧：时间窗 → 全局节奏 → 单人间隔 → 每日配额 → 静默时长 → 退避。
    # 全都过了才开口，所以它平时几乎是哑的，偶尔冒一句才像活的。
    @staticmethod
    def _hour_ok(h1, h2, now=None):
        """现在这个点能不能开口。支持跨零点（比如 22 点到次日 2 点）。"""
        h = datetime.fromtimestamp(now or time.time()).hour
        if h1 <= h2:
            return h1 <= h < h2
        return h >= h1 or h < h2

    def _ping_window_ok(self, now=None):
        return self._hour_ok(self.ping_h1, self.ping_h2, now)

    def _ping_known_peer(self, gid):
        """认识 = 有档案，或者私聊里留过话。陌生好友一律不主动，免得像骚扰。"""
        u = (self.mem.get("users") or {}).get(str(gid)) or {}
        if u.get("notes") or u.get("alias"):
            return True
        return bool((self.mem.get("history") or {}).get(str(gid)))

    @staticmethod
    def _ping_today(now=None):
        return time.strftime("%Y-%m-%d", time.localtime(now or time.time()))

    def _save_ping_state(self):
        self.mem["dmPing"] = self.ping_state
        self.mem["dmPingLast"] = self.last_ping_ts
        write_memory(self.mem)

    def maybe_dm_ping(self, groups):
        """好友很久没说话，自己去找他聊一句。"""
        if not self.ping_on or not self.mem.get("enabled", True) or not self.dm_on:
            return
        now = time.time()
        if now - self.last_any < self.cd_global:
            return
        if not self._ping_window_ok(now):
            return
        # 全局节奏：宁可少找，也不要一开机把十几个老会话挨个戳一遍
        if self.last_ping_ts and now - self.last_ping_ts < self.ping_every:
            return
        today = self._ping_today(now)
        cand = None
        for g in groups:
            if g.get("kind") != "dm":
                continue
            gid = str(g["gid"])
            if self.ping_uids:
                # 面板上勾了人：只找勾上的，哪怕他没档案也照找（这是你点名的）
                if gid not in self.ping_uids:
                    continue
            elif self.ping_known and not self._ping_known_peer(gid):
                continue
            st = self.ping_state.get(gid) or {}
            if now < float(st.get("until") or 0):
                continue                       # 被晾太多次，还在冷却期
            if now - float(st.get("last") or 0) < self.ping_gap:
                continue                       # 离上次主动太近
            if str(st.get("day") or "") == today and int(st.get("n") or 0) >= self.ping_daily:
                continue                       # 今天找他够数了
            peer_ts = _ts(self.last_peer.get(gid) or 0)
            if peer_ts > now + 300:      # 单位或时钟不对，宁可当不知道，退回按会话动静算
                peer_ts = 0.0
            quiet = now - (peer_ts or self.last_activity.get(gid, self.started))
            # 他回过话没有？回过就从头算；没回就是在被晾着，可以补一句
            ignored = 0
            if st.get("last"):
                ignored = (0 if peer_ts > float(st.get("peer") or 0)
                           else int(st.get("ignored") or 0) + 1)
            wait = self.ping_again if (ignored >= 1 and self.ping_escalate) else self.ping_idle
            if quiet < wait:
                continue
            if cand is None or quiet > cand[3]:
                cand = (g, st, ignored, quiet)
        if not cand:
            return
        g, st, ignored, quiet = cand
        gid = str(g["gid"])
        if ignored >= self.ping_giveup:
            # 连续被晾到上限：这次不发了，搁置一段时间，别追着喊
            st["until"] = now + self.ping_hold * 3600
            st["ignored"] = ignored
            self.ping_state[gid] = st
            self._save_ping_state()
            log("私聊主动：%s 连续 %d 次没回，先搁置 %.0f 小时"
                % (g.get("name"), ignored, self.ping_hold))
            return
        quiet_min = int(quiet // 60)
        try:
            text, _ = self.ai.dm_ping(gid, g.get("name") or "", gid, quiet_min, ignored,
                                      st.get("said") or "", self.ping_prompt,
                                      _talk_lines(self.recent.get(gid)))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:160]
            log("私聊主动请求 HTTP %s: %s" % (e.code, detail), "ERROR")
            return
        except Exception as e:
            log("私聊主动失败: %s" % e, "ERROR")
            return
        if not (text or "").strip():
            log("私聊主动：模型没给出内容", "WARN")
            return
        answer, picked, _memo_n = self._tidy_answer(text, dm=True)
        answer, extra = self._emoji_supplement(answer, picked)
        if not answer and not picked and not extra:
            log("私聊主动：收拾完什么都不剩，跳过", "WARN")
            return
        r = self.deliver(g, answer) if answer else {"ok": True}
        if not r.get("ok"):
            log("私聊主动发送失败: %s" % r.get("msg"), "ERROR")
            return
        for s in list(picked) + ([extra] if extra else []):
            try:
                rs = self.deliver_sticker(g, s)
            except Exception as e:
                rs = {"ok": False, "msg": str(e)}
            if rs.get("ok"):
                self.emoji_used += 1
                self.mark_sent(s)
            else:
                log("私聊主动发表情包失败: %s" % rs.get("msg"), "WARN")
        # 记账：今天的次数、这次说了什么、被晾几回、他当时的最后发言时间
        if str(st.get("day") or "") != today:
            st["day"] = today
            st["n"] = 0
        st["n"] = int(st.get("n") or 0) + 1
        st["last"] = now
        st["ignored"] = ignored
        st["peer"] = _ts(self.last_peer.get(gid) or 0)
        st["said"] = answer[:120] if answer else ""
        self.ping_state[gid] = st
        if answer:
            self.ai.note_proactive(gid, answer, "dping")
        self.pings += 1
        self.last_ping_ts = now
        self.last_activity[gid] = now
        self.last_any = now
        self.last_from[gid] = "me"
        self._save_ping_state()
        self.ping_log.insert(0, {"uid": gid, "name": g.get("name") or gid,
                                 "ts": now, "text": answer[:60], "n": int(st["n"]),
                                 "ignored": ignored})
        del self.ping_log[8:]
        log("私聊主动 %s（安静 %d 分钟，今天第 %d 次%s）：%s" % (
            g.get("name"), quiet_min, st["n"],
            "，是被晾着补的" if ignored else "", answer))

    # ---------------------------------------------------------------- 私聊指令
    # 主人（或白名单里的人）在私聊里发 /send /img，让它去群里代发内容。
    # 只有白名单里的 UID 能用——机器人跑在自己的号上，好友也能私聊它，
    # 不设闸门等于谁都能借它的嘴在群里说话。
    def _help_text(self):
        p = self.control_prefix
        cmd_part = ("打指令更准：\n"
                    "%sgroups　看我能发到哪些会话（带序号）\n"
                    "%ssend 群名 内容　发一段话\n"
                    "%simg 群名 图片地址或电脑路径　发一张图\n"
                    "　图省事就先把图私聊发我，再发「%simg 群名」\n"
                    "群名写一半就认得，也可以直接写序号；多个目标用逗号隔开。" % (p, p, p, p))
        if not self.control_nl:
            return "（现在「听懂人话」是关的，只能打指令）\n" + cmd_part
        return ("直接跟我说人话也行，不用打 %s，比如：\n"
                "· 在主教群说一句我马上到\n"
                "· 发一句“今晚不打了”到所有群\n"
                "· 去主教群冒个泡\n"
                "· 把这张图发到主教群（先把图发我）\n"
                "· 在群里发个表情包\n\n" % p) + cmd_part

    def _authorized(self, uid):
        return str(uid) in self.control_uids

    def _note_dm(self, m):
        """记住最近私聊过的人，面板上可以一键加进白名单"""
        uid = str(m.get("uid") or "").strip()
        if not uid:
            return
        self.recent_dm[uid] = {"name": m.get("name") or "", "ts": time.time()}
        if len(self.recent_dm) > 20:
            keep = sorted(self.recent_dm.items(), key=lambda kv: -kv[1]["ts"])[:20]
            self.recent_dm = dict(keep)

    def _say(self, uid, text):
        """指令回执：直接私聊回去，不经过 AI，也不用人设口吻"""
        try:
            r = self.client.dm_send(uid, text)
            if not (r or {}).get("ok"):
                log("指令回执没发出去: %s" % (r or {}).get("msg"), "WARN")
            return r or {}
        except Exception as e:
            log("指令回执异常: %s" % e, "WARN")
            return {}

    def _find_targets(self, key):
        """把「群名 / 序号」对上监听中的会话。返回 (列表, 错误说明)"""
        key = (key or "").strip().strip("「」\"'“”")
        gs = list(self.groups or [])
        if not key:
            return [], "没说要发到哪个会话"
        if not gs:
            return [], "现在还没有监听中的会话"
        if key.isdigit():
            i = int(key) - 1
            if 0 <= i < len(gs):
                return [gs[i]], ""
            return [], "没有第 %s 个，发「%sgroups」看看" % (key, self.control_prefix)
        exact = [g for g in gs if (g.get("name") or "") == key]
        if exact:
            return exact, ""
        hit = [g for g in gs if key in (g.get("name") or "")]
        if not hit:      # 用户顺手多写了几个字（"XX群"）
            hit = [g for g in gs if (g.get("name") or "") and g["name"] in key]
        if not hit:      # "主教群" → 去掉尾巴上的「群」再找一次
            core = re.sub(r"(群聊|的群|群)$", "", key).strip()
            if core and core != key:
                hit = [g for g in gs if core in (g.get("name") or "")]
        if len(hit) == 1:
            return hit, ""
        if not hit:
            return [], "找不到「%s」，发「%sgroups」看看我能发哪些" % (key, self.control_prefix)
        return [], "「%s」对上好几个：%s，说全一点或写序号" % (
            key, "、".join(g.get("name") or g["gid"] for g in hit))

    def _split_targets(self, keys):
        parts = [s for s in re.split(r"[,，、]", keys or "") if s.strip()]
        out, errs = [], []
        for k in parts:
            got, err = self._find_targets(k)
            if err:
                errs.append(err)
            for g in got:
                if all(g["gid"] != x["gid"] for x in out):
                    out.append(g)
        return out, errs

    def _measure_one(self, url):
        try:
            w, h = (self.client.measure_images([url]) or {}).get(url) or (0, 0)
        except Exception:
            w, h = 0, 0
        return int(w or 0), int(h or 0)

    def _resolve_image(self, uid, src):
        """图片地址 / 本机路径 / 刚私聊发来的图 → (url, w, h, 说明)。失败抛 ValueError"""
        src = (src or "").strip().strip("\"'“”")
        if not src:
            url = self.last_dm_img.get(str(uid)) or ""
            if not url:
                raise ValueError("没说图在哪，我也没收到你的图。先私聊发我一张，"
                                 "或者写图片地址 / 电脑上的路径")
            w, h = self._measure_one(url)
            return url, w, h, "用你刚发来的那张图"
        if re.match(r"^https?://", src, re.I):
            w, h = self._measure_one(src)
            return src, w, h, "网络图片"
        path = os.path.expanduser(src)
        if os.path.isfile(path):
            r = self.client.upload_image(path) or {}
            if not r.get("ok"):
                raise ValueError("上传失败：%s" % (r.get("err") or r.get("msg") or "未知原因"))
            return r.get("url") or "", int(r.get("w") or 0), int(r.get("h") or 0), "电脑上的文件"
        raise ValueError("找不到这个文件：%s" % src)

    def _send_image(self, t, url, w, h):
        if t.get("kind") == "dm":
            return self.client.dm_send_sticker(t["gid"], url, w, h)
        return self.client.send_sticker(t["gid"], url, w, h)

    # ---- 动作：斜杠命令和自然语言共用这几段，别再写两遍 ----

    def _exec_say(self, targets, content):
        """把一段话发到若干会话 → (成功列表, 失败说明)"""
        done, bad = [], []
        for t in targets:
            r = self.deliver(t, content) or {}
            if r.get("ok"):
                done.append(t)
                self.last_activity[t["gid"]] = time.time()   # 自己说话也算有动静
            else:
                bad.append("「%s」%s" % (self.label(t), r.get("msg") or "发送失败"))
        self.cmds += 1
        log("私聊指令：发到 %s（%d 字）" % (
            "、".join(self.label(t) for t in done) or "无", len(content)))
        return done, bad

    def _exec_image(self, uid, targets, src):
        """发图 → (成功列表, 失败说明, 取图失败原因)"""
        try:
            url, w, h, how = self._resolve_image(uid, src)
        except ValueError as e:
            return [], [], str(e)
        if not url:
            return [], [], "没拿到可用的图片地址"
        done, bad = [], []
        for t in targets:
            r = self._send_image(t, url, w, h) or {}
            if r.get("ok"):
                done.append(t)
                self.last_activity[t["gid"]] = time.time()
            else:
                bad.append("「%s」%s" % (self.label(t), r.get("msg") or "发送失败"))
        self.cmds += 1
        log("私聊指令：发图到 %s（%s，%dx%d）" % (
            "、".join(self.label(t) for t in done) or "无", how, w, h))
        return done, bad, ""

    def _exec_bubble(self, targets, hint):
        """让它自己找个话说 → (成功列表[(会话, 说的话)], 失败说明)"""
        done, bad = [], []
        for t in targets:
            if t.get("kind") != "group":
                bad.append("「%s」是私聊，没法凭空冒泡" % self.label(t))
                continue
            try:
                ans, _ = self.ai.bubble(t["gid"], self.label(t), hint or "",
                                        self.recent.get(t["gid"]) or [], 0)
            except Exception as e:
                bad.append("「%s」想不出话：%s" % (self.label(t), e))
                continue
            ans = (ans or "").strip().strip("“”\"'")
            if not ans:
                bad.append("「%s」没想出话，换个说法再试" % self.label(t))
                continue
            r = self.deliver(t, ans) or {}
            if r.get("ok"):
                done.append((t, ans))
                self.last_activity[t["gid"]] = time.time()
                self.ai.note_proactive(t["gid"], ans, "bubble")
            else:
                bad.append("「%s」%s" % (self.label(t), r.get("msg") or "发送失败"))
        if done:
            self.cmds += 1
            log("私聊指令：冒泡 %s" % "、".join(self.label(t) for t, _ in done))
        return done, bad

    def _emoji_prefer(self, hint):
        """用户嘴上说的是哪种表情。他说“表情包/大表情”就别给他发文字小表情，
        说了“小表情”就别硬塞一张图——之前随机挑，说“发表情包”发出去一串 [xxx]
        编码，他只会以为坏了。"""
        h = hint or ""
        if re.search(r"表情包|大表情|大图|贴纸|收藏的?图", h):
            return "sticker"
        if re.search(r"小表情", h):
            return "small"
        return None

    def _exec_emoji(self, targets, hint=""):
        """从收藏里挑一个表情发出去 → (成功列表, 失败说明, 取不到的原因)"""
        prefer = self._emoji_prefer(hint)
        if prefer == "sticker" and not self.sticker_book():
            return [], [], "还没有收藏的表情包（大图），在群里长按收几张再来"
        got = self.pick_emoji(prefer)
        if not got:
            return [], [], "收藏里还没有表情，先在群里收几个"
        kind, val = got
        if kind == "small":        # 小表情就是一段文字编码，走正常发言
            done, bad = self._exec_say(targets, val)
            return done, bad, ""
        if not val:
            return [], [], "没挑出可用的表情"
        done, bad = [], []
        for t in targets:
            try:
                r = self.deliver_sticker(t, val) or {}
            except Exception as e:
                r = {"ok": False, "msg": str(e)}
            if r.get("ok"):
                done.append(t)
                self.emoji_used += 1
                self.mark_sent(val)
                self.last_activity[t["gid"]] = time.time()
            else:
                bad.append("「%s」%s" % (self.label(t), r.get("msg") or "发送失败"))
        if done:
            self.cmds += 1
            log("私聊指令：发表情 %s" % "、".join(self.label(t) for t in done))
        return done, bad, ""

    # 粗筛用：判错不要紧——真判成非指令会退回正常聊天，只多花一次很短的判断；
    # 漏掉才是真麻烦（用户说「去群里冒个泡」结果它当闲聊回了）。
    # 只有「动作」词才可能是「替我去别处做件事」；「表情」「图」「群」只是名词，
    # 「这个蓝色的表情包」是在问东西，不是派活。
    CMD_VERBS = ("发", "说", "告诉", "转告", "转达", "通知", "喊", "问", "冒",
                 "活跃", "艾特", "@", "传话", "安排", "提醒", "催")
    # 「刚才那张是谁发的」——句尾是在问来历，不是让人去发东西
    CMD_ASK_RE = re.compile(r"(是谁|谁的|谁发|谁说|谁做|什么|哪个|为啥|为什么|什么意思)"
                            r"\s*[的了吗呢啊呀]?\s*[?？]?$")

    def _looks_like_cmd(self, txt):
        s = (txt or "").strip()
        if not (2 <= len(s) <= 200):
            return False
        # 一个动作词都没有，就不算指令。以前是「含关键词就判」，
        # 结果「你这表情包是谁」「这个蓝色的表情包」全被当成发图指令吃掉了。
        if not any(w in s for w in self.CMD_VERBS):
            return False
        return not self.CMD_ASK_RE.search(s)

    def _groups_text(self):
        if not self.groups:
            return "现在还没监听任何会话"
        return "我能发到：\n" + "\n".join(
            "%d. %s" % (i + 1, self.label(g)) for i, g in enumerate(self.groups))

    def _replay(self, uid, txt):
        """刚办完一单，接着一句「不是小表情」「要大的」「重发」——按上一条重办。
        返回 True = 已处理完。只在 10 分钟内、话很短、且确实是纠正/重发时生效，
        免得「你发个表情包给主教群」这种新指令被上一单劫走。"""
        prev = self.last_cmd.get(str(uid))
        if not prev or time.time() - (prev.get("at") or 0) > 600:
            return False
        s = (txt or "").strip().rstrip("。！!~～")
        if not (1 <= len(s) <= 14):
            return False
        want = None
        if re.match(r"^(不是|要|换|别|不要)", s):
            # 「不是小表情」= 要大的；「别发表情包」= 要小的。先说否定的那个
            if re.search(r"(不是|别|不要|不想).{0,3}小表情", s):
                want = "sticker"
            elif re.search(r"(不是|别|不要|不想).{0,3}(表情包|大表情|大图|贴纸)", s):
                want = "small"
            elif re.search(r"小表情", s):
                want = "small"
            elif re.search(r"表情包|大表情|大的|大图|贴纸", s):
                want = "sticker"
        repeat = bool(re.match(r"^(再|重新)?发(一次|一遍|个)?$|^重发$|^再来一?[次遍]$|"
                               r"^那就发$|^发吧$|^去吧$|^接着发$", s))
        if not (want or repeat):
            return False
        act = prev.get("act")
        targets = [t for t in (prev.get("targets") or []) if t]
        if act not in ("say", "image", "emoji", "bubble") or not targets:
            return False
        lines = []
        if act == "emoji":
            hint = "表情包" if want == "sticker" else ("小表情" if want == "small" else prev.get("hint") or "")
            done, bad, err = self._exec_emoji(targets, hint)
            if done:
                lines.append("这次发的是收藏里的图，已到 %s" % "、".join("「%s」" % self.label(t) for t in done))
            if err:
                lines.append(err)
            lines.extend(bad)
        elif act == "say":
            done, bad = self._exec_say(targets, prev.get("content") or "")
            if done:
                lines.append("又说了一遍，已到 %s" % "、".join("「%s」" % self.label(t) for t in done))
            lines.extend(bad)
        elif act == "image":
            done, bad, err = self._exec_image(uid, targets, prev.get("content") or "")
            if done:
                lines.append("重发了一次图，已到 %s" % "、".join("「%s」" % self.label(t) for t in done))
            if err:
                lines.append(err)
            lines.extend(bad)
        else:
            done, bad = self._exec_bubble(targets, prev.get("content") or "")
            lines.extend("已在「%s」开口：%s" % (self.label(t), a) for t, a in done)
            lines.extend(bad)
        prev["at"] = time.time()
        self._say(uid, "\n".join(lines) or "没办成，你说清楚点我再试")
        return True

    def _natural_cmd(self, uid, txt):
        """听懂一句人话就去办。返回 True = 这条已处理；False = 不是指令，交回正常聊天"""
        if self._replay(uid, txt):
            return True
        try:
            got = self.ai.parse_command(txt, self.groups,
                                        has_img=bool(self.last_dm_img.get(str(uid))))
        except Exception as e:
            log("自然语言指令解析失败: %s" % e, "WARN")
            return False
        act = (got or {}).get("act") or "none"
        if act == "none":
            return False
        log("听懂一句话（UID %s）：%s → %s" % (uid, txt[:50], act))
        content = (got.get("content") or "").strip()
        key = (got.get("target") or "").strip()
        targets, errs = [], []
        # 「所有群/每个群/群里都发」——原话里看得见就自己扩，不指望模型填对
        all_hint = bool(re.search(r"(所有|全部|每个|各个)\s*群|群里都|都发一遍|挨个群", txt))
        if all_hint or (key and re.search(r"所有|全部|每个群|各个群|各个会话|都发|大家", key)):
            # 只扩群，不把私聊会话算进去
            targets = [g for g in (self.groups or []) if g.get("kind") == "group"]
            errs = [] if targets else ["现在没有监听中的群"]
        elif key:
            targets, errs = self._split_targets(key)
        elif len(self.groups or []) == 1:
            targets = list(self.groups)      # 只有一个地方可发，就不用再问了
        if not targets:
            # 没点名目标、原话里也没有「发出去 / 转发 / 发个」这类动作，多半是聊天里在问
            # 东西（「这个蓝色的表情包」）。交回人设回复，别硬邦邦列一串会话名。
            if not errs and not re.search(
                    r"(发|转|传|甩|丢|扔)(给|到|去|出去|过去)|转发|发一下|发个|发条|发张|发这", txt):
                return False
            tip = "\n".join(errs) or "要发到哪儿？"
            self._say(uid, tip + "\n" + self._groups_text())
            return True
        lines = []
        if act == "say":
            if not content:
                self._say(uid, "要我说什么？把话跟在一起发我就行")
                return True
            done, bad = self._exec_say(targets, content)
            if done:
                lines.append("已发到 %s：%s" % (
                    "、".join("「%s」" % self.label(t) for t in done),
                    content[:40] + ("…" if len(content) > 40 else "")))
            lines.extend(bad)
        elif act == "image":
            done, bad, err = self._exec_image(uid, targets, content)
            if done:
                lines.append("已发图到 %s" % "、".join("「%s」" % self.label(t) for t in done))
            if err:
                lines.append(err)
            lines.extend(bad)
        elif act == "bubble":
            done, bad = self._exec_bubble(targets, content)
            for t, a in done:
                lines.append("已在「%s」开口：%s" % (self.label(t), a))
            lines.extend(bad)
        elif act == "emoji":
            done, bad, err = self._exec_emoji(targets, txt)
            if done:
                lines.append("已发表情到 %s" % "、".join("「%s」" % self.label(t) for t in done))
            if err:
                lines.append(err)
            lines.extend(bad)
        else:
            return False
        # 记下这一单，接下来「要大的」「重发」直接重办，不用他再说一遍群名
        self.last_cmd[str(uid)] = {"act": act, "targets": list(targets), "content": content,
                                   "hint": txt, "at": time.time()}
        self._say(uid, "\n".join(lines) or "没办成，再试一次？")
        return True

    def try_control(self, g, m):
        """私聊里的控制消息。返回 True = 这条已经被我吃掉，不要再交给 AI。
        图片（type 3/7）不回复，只记下来等着被 /img 转发。"""
        t = int(m.get("type") or 0)
        self._note_dm(m)
        if t not in (4, 10):
            if m.get("img") and self._authorized(m.get("uid")):
                self.last_dm_img[str(m.get("uid"))] = m["img"]
                log("记下私聊图片，等 /img 转发（UID %s）" % m.get("uid"))
            return False
        txt = (m.get("msg") or "").strip()
        uid = m.get("uid")
        if not txt.startswith(self.control_prefix):
            # 不打斜杠也行：听着像让我办事，就先让模型判一下；
            # 判成“只是聊天”就返回 False，照常进人设回复
            if self.control_nl and self._authorized(uid) and self._looks_like_cmd(txt):
                return self._natural_cmd(uid, txt)
            return False
        if not self._authorized(uid):
            log("私聊指令被拒（UID %s 不在白名单）：%s" % (uid, txt[:40]), "WARN")
            self._say(uid, "我这边没给指令权限。让主人在面板「私聊指令」里把 UID %s 加进白名单。" % uid)
            return True
        body = txt[len(self.control_prefix):].strip()
        parts = body.split(None, 2)
        if not parts:
            self._say(uid, self._help_text())
            return True
        cmd = parts[0].lower()
        p = self.control_prefix
        if cmd in ("help", "?", "h", "帮助", "说明"):
            self._say(uid, self._help_text())
            return True
        if cmd in ("groups", "list", "g", "群", "群列表", "会话"):
            self._say(uid, self._groups_text())
            return True
        if cmd in ("send", "say", "relay", "发", "发送", "说", "转", "转达"):
            if len(parts) < 3:
                self._say(uid, "格式：%ssend 群名 内容（也可以直接说“在XX群说一句…”）" % p)
                return True
            targets, errs = self._split_targets(parts[1])
            content = parts[2].strip()
            if not content:
                self._say(uid, "内容是空的，没东西可发")
                return True
            if not targets:
                self._say(uid, "\n".join(errs) or "没找到要发的地方")
                return True
            done, bad = self._exec_say(targets, content)
            msg = []
            if done:
                msg.append("已发到 %s：%s" % ("、".join("「%s」" % self.label(t) for t in done),
                                              content[:40] + ("…" if len(content) > 40 else "")))
            msg.extend(bad)
            msg.extend(errs)
            self._say(uid, "\n".join(msg))
            return True
        if cmd in ("img", "pic", "image", "图", "图片", "发图"):
            if len(parts) < 2:
                self._say(uid, "格式：%simg 群名 [图片地址/电脑路径]" % p)
                return True
            targets, errs = self._split_targets(parts[1])
            if not targets:
                self._say(uid, "\n".join(errs) or "没找到要发的地方")
                return True
            done, bad, err = self._exec_image(uid, targets, parts[2] if len(parts) > 2 else "")
            if err:
                self._say(uid, err)
                return True
            if not done and not bad:
                self._say(uid, "没拿到可用的图片地址")
                return True
            msg = []
            if done:
                msg.append("已发图到 %s" % "、".join("「%s」" % self.label(t) for t in done))
            msg.extend(bad)
            msg.extend(errs)
            self._say(uid, "\n".join(msg))
            return True
        self._say(uid, "认不出「%s」这个指令。\n%s" % (parts[0], self._help_text()))
        return True

    def _msg_text(self, m):
        """把一条消息变成上下文里那句「自己看见的话」。
        图片/表情包带「发了张图」占位，表情包尽量补上已标注的名字，好让被引用时对得上。"""
        t = int(m.get("type") or 0)
        if t == 3 and str(m.get("img") or "").strip():
            return "（发了一张图）"
        if t == 7 and str(m.get("img") or "").strip():
            key = self.sticker_key(m.get("img") or "")
            v = (self.sticker_book() or {}).get(key) or {}
            label = (v.get("label") or "").strip()
            return "（发了张表情包%s）" % ("：「%s」" % label if label else "")
        return clean_text(m.get("msg") or "").strip()

    def _log_talk(self, gid, entries):
        """把这一轮看到的消息记进上下文缓存。
        短句也留：真人聊天里「你」「？」这种占一半，以前被 len>1 的过滤丢掉了。"""
        gid = str(gid)
        cur = self.recent.setdefault(gid, [])
        long = self.talk.setdefault(gid, [])
        last_seq = self._last_seq.get(gid)
        for e in entries:
            seq = str(e.get("seq") or "")
            if seq and last_seq:
                try:
                    if int(seq) <= int(last_seq):
                        continue        # 轮询窗口有重叠，这条已经记过了
                except ValueError:
                    pass
            elif cur and e.get("text") == cur[-1].get("text") and e.get("uid") == cur[-1].get("uid"):
                continue
            cur.append(e)
            long.append(e)
            if seq:
                last_seq = seq
        if last_seq:
            self._last_seq[gid] = last_seq
        del cur[:-60]
        del long[:-200]
        now = time.time()
        if now - self._talk_saved > 30:      # 限流：别每轮都写 memory.json
            self._talk_saved = now
            self._save_talk()

    def _save_talk(self):
        d = read_memory()
        if str(d.get("profile") or "") != str(self.mem.get("profile") or ""):
            return          # 换卡片了，这批记录属于上一个人格，别写回去
        d["talkLog"] = self.talk
        if write_memory(d):
            self.mem = d

    def handle(self, g, m, body, ctx=""):
        """接一句话。

        ctx 是「只给模型看、不进历史」的旁注（比如开机补课时的「你刚回来」）。
        它拼在送给模型的那句前面，但 remember 里只存 body——不然聊完一轮，
        历史里就多出一条像群友说过的元话语。
        """
        gid = g["gid"]
        is_dm = g.get("kind") == "dm"
        now = time.time()
        # 管理命令 100% 听取：不走冷却，说到就答（冷却本来是防刷屏的
        # 自我保护，管理的指令不该被它挡住）
        bypass = bool(m.get("_admin")) and self.admin_bypass
        if not bypass and now - self.last_reply.get(gid, 0) < self.cd_group:
            log("冷却中，跳过：%s" % self.label(g))
            return
        if not bypass and now - self.last_any < self.cd_global:
            return
        if is_dm:
            log("收到私聊 %s: %s" % (m["name"], body[:60]))
        elif m.get("_admin"):
            log("管理发言%s %s / %s: %s" % ("（不等冷却）" if bypass else "",
                                          g["name"], m["name"], body[:60]))
        elif m.get("_direct"):
            log("群聊直接回复（%d%%）%s / %s: %s" % (self.direct_rate, g["name"], m["name"], body[:60]))
        else:
            log("收到%s %s / %s: %s" % ("回复" if m.get("_rep") else " @  ",
                                       g["name"], m["name"], body[:60]))
        try:
            rec = self.recent.get(gid) or []
            # 智能体模式才翻旧账：在更早的记录里找跟这句相关的，普通模式不多花这次检索
            recall = (_recall(self.talk.get(gid) or [], body, rec)
                      if self.ai.agent else [])
            said = ("%s\n%s" % (ctx, body)).strip() if ctx else body
            answer, tokens = self.ai.reply(gid, self.label(g), m["name"], said,
                                           m.get("uid"), is_dm=is_dm,
                                           recent=rec, recall=recall)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            log("AI HTTP %s: %s" % (e.code, detail), "ERROR")
            return
        except Exception as e:
            log("AI 调用失败: %s" % e, "ERROR")
            return
        if not answer:
            log("AI 返回空", "WARN")
            return
        answer, picked, _memo_n = self._tidy_answer(answer, dm=bool(is_dm), uid=m.get("uid"),
                                        name=m.get("name") or speaker)
        # 嘴上说了「记下了」却没带 [[记:…]]：不能让这句承诺空着。
        # 退一步，把对方那句话本身记进便签（标「他说」，跟他自己写的分开）。
        if not _memo_n and m.get("uid") and MEMO_SAID_RE.search(answer):
            body = text or ""
            body = re.sub(r"^（引用[^）]*）\s*", "", body).strip()
            if body and self.ai.memo_for(m.get("uid"), "（他说）" + _one_line(body, 46)):
                _memo_n = 1
                log("说了记下但没写标记，按原话兜底记了一条", "WARN")
        # 模型没主动用表情时，按概率补一个：小表情拼进正文，大表情另发一条
        answer, extra = self._emoji_supplement(answer, picked)
        if not answer and not picked and not extra:
            # 模型只写了一条认不出的表情指令（名字打错、KEY 抄错），至少别晾着人家
            got = self.pick_emoji() if self.emoji_on else None
            if got and got[0] == "sticker":
                extra = got[1]
                log("表情指令认不出，改发一张收藏里的", "WARN")
            elif got and got[0] == "small" and got[1]:
                answer = got[1]
                log("表情指令认不出，改发一个小表情", "WARN")
            else:
                log("AI 返回空（或只写了一条认不出的表情指令）", "WARN")
                return
        r = self.deliver(g, answer) if answer else {"ok": True}
        if not r.get("ok"):
            log("发送失败: %s" % r.get("msg"), "ERROR")
            return
        # 表情包单独一条（客户端里本来就是这个形态：msg_type=7，没有文字）
        for s in list(picked) + ([extra] if extra else []):
            try:
                rs = self.deliver_sticker(g, s)
            except Exception as e:
                rs = {"ok": False, "msg": str(e)}
            if rs.get("ok"):
                self.emoji_used += 1
                self.mark_sent(s)
                log("已发表情包 %s" % self.sticker_key(s.get("url")))
            else:
                log("发表情包失败: %s" % rs.get("msg"), "WARN")
        self.replied += 1
        self.last_reply[gid] = time.time()
        self.last_any = time.time()
        # 表情包是另外单发一条图，正文里没有它。历史里要是不补一句，它下一轮
        # 根本不知道自己刚发过什么——被人问「你刚发那张是什么」，它会一本正经地编
        # 一张出来（实测过：发的是金发小女孩吐舌，它答成蓝色虎鲸还配了句撒娇话）。
        stick_note = ""
        for _s in list(picked) + ([extra] if extra else []):
            _lb = (_s.get("label") or "").strip()
            if len(_lb) > 60:
                _lb = _lb[:60] + "…"
            stick_note += "（顺手发了张表情包%s）" % ("：" + _lb if _lb else "")
        self.ai.remember(gid, body, answer + stick_note, m.get("uid"), m.get("name"))
        # 好感度：陪他说了会儿话，加一点点。回得越勤这条线越高，
        # 但真正拉开差距的是「接话」（见 note_replied），这里只是底噪。
        try:
            self.touch_aff(m.get("uid"), m.get("name"), 0.8,
                           "跟他说了会儿话" if g.get("kind") == "dm" else "回了他的话")
        except Exception as e:
            log("记好感度失败: %s" % e, "WARN")
        self.mem = self.ai.mem
        self.mem_mtime = self._mtime(MEM_PATH)
        self.since_learn[gid] = self.since_learn.get(gid, 0) + 1
        log("已回复 %s（%s token）: %s" % (self.label(g), tokens, answer))
        if self.mem.get("learn") and self.since_learn[gid] >= self.ai.learn_every:
            self.since_learn[gid] = 0
            try:
                self.ai.learn(gid, self.label(g))
            except Exception as e:
                log("自动记忆整理失败: %s" % e, "WARN")
            # 群友档案以前只有面板手动点「整理档案」才会动，于是日常聊天一句都进不去，
            # 它却张口就说「记下了」。现在跟着日记顺带整理一次，6 小时内不重复。
            try:
                if time.time() - float(self.mem.get("usersLearnedAt") or 0) > 6 * 3600:
                    self.ai.learn_users(gid, self.label(g))
            except Exception as e:
                log("自动档案整理失败: %s" % e, "WARN")
            # 本体模式的卡：跟着日记顺带自省一次。日记记「今天发生了什么」，
            # 自省记「我因此对自己多知道了什么」——没有这一步，空白永远是空白。
            try:
                self.ai.self_reflect(gid)
            except Exception as e:
                log("自省失败: %s" % e, "WARN")
            self.mem = self.ai.mem
            self.mem_mtime = self._mtime(MEM_PATH)


    def sync_accounts(self):
        """把当前平台的账号写进记忆，好回答「你在哪儿有号」。
        平台名现在写死小黑盒；接第二个平台时，由各平台适配器自己报上来。"""
        sid = str(self.client.self_id or "")
        nm = (self.client.self_name or "").strip()
        if not sid or not nm:
            return
        mem = read_memory()
        acc = [a for a in (mem.get("accounts") or []) if isinstance(a, dict)]
        old = [a for a in acc if a.get("platform") == "小黑盒"]
        if old and old[0].get("name") == nm and old[0].get("id") == sid:
            return                       # 没变就不写盘，免得每轮刷一次
        mem["accounts"] = [a for a in acc if a.get("platform") != "小黑盒"]
        mem["accounts"].append({"platform": "小黑盒", "name": nm, "id": sid})
        if write_memory(mem):
            self.mem = read_memory()
            self.ai.mem = self.mem
            log("已记下账号：小黑盒「%s」(%s)" % (nm, sid))

    def farewell(self, groups):
        """关机前收尾：把还没记的聊进日记，再把日记收成一段总观。

        面板点「停止机器人」时不直接杀进程，而是写 stop.flag；这里看到就跑一遍。
        总共给 farewell.seconds 秒（默认 90），每秒把剩余秒数写进 state.json，
        面板上显示倒计时。时间不够就收工——宁可少写两句，也不能关不掉。
        """
        c = self.farewell_cfg
        total = int(c["seconds"])
        deadline = time.time() + total
        self._farewell = {"active": True, "phase": "准备", "left": total,
                          "total": total, "diary": 0, "outlook": 0, "groups": 0}
        self.write_state(groups)

        # 主线程在等模型回话，倒计时要靠另一个线程每秒往 state.json 写一次
        stop_tick = threading.Event()

        def ticker():
            while not stop_tick.wait(1.0):
                try:
                    self._farewell["left"] = max(0, int(round(deadline - time.time())))
                    self.write_state(self.groups or groups)
                except Exception:
                    pass

        th = threading.Thread(target=ticker, daemon=True)
        th.start()

        def left():
            return deadline - time.time()

        log("开始收尾：最多 %d 秒，写日记 %s、总观 %s" % (
            total, "开" if c["diary"] else "关", "开" if c["outlook"] else "关"))
        try:
            # 谁值得写：这几天真说过话的会话，按最后一句时间排；一个都没说过就跳过
            cands = []
            for g in groups:
                arr = self.talk.get(str(g["gid"])) or []
                if len(arr) < 3:
                    continue
                ts = 0.0
                for x in arr[-3:]:
                    ts = max(ts, _f(x.get("t") or x.get("ts"), 0.0))
                cands.append((ts, g))
            cands.sort(key=lambda kv: -kv[0])
            picks = [g for _, g in cands[:max(0, int(c["groups"]))]]

            if c["diary"]:
                for g in picks:
                    if left() < 20:
                        log("剩下不到 20 秒，没写完的这次不写了")
                        break
                    self._farewell["phase"] = "写日记：" + self.label(g)
                    self.write_state(self.groups or groups)
                    n = self.ai.learn(g["gid"], self.label(g))
                    self._farewell["diary"] = int(self._farewell["diary"]) + int(n or 0)
                    self._farewell["groups"] = int(self._farewell["groups"]) + 1
                    self.write_state(self.groups or groups)

            if c["outlook"] and left() >= 15:
                self._farewell["phase"] = "想总观"
                self.write_state(self.groups or groups)
                n = self.ai.reflect_outlook()
                self._farewell["outlook"] = int(n or 0)
        except Exception as e:
            log("收尾失败: %s" % e, "WARN")
        finally:
            stop_tick.set()
            self._farewell.update({"active": False, "left": 0, "phase": "完成"})
            try:
                self.write_state(self.groups or groups)
            except Exception:
                pass
        log("收尾完成：日记 +%d 字（%d 个会话），总观 %s" % (
            int(self._farewell.get("diary") or 0), int(self._farewell.get("groups") or 0),
            ("%d 字" % int(self._farewell["outlook"])) if self._farewell.get("outlook") else "没动"))

    def run(self):
        self.client.ensure()
        log("已连接客户端，身份：%s (%s)" % (self.client.self_name, self.client.self_id))
        self.sync_accounts()
        self.sync_emoji_map()
        log("表情：%s｜已收藏 %d 张｜认得 %d 种小表情｜使用频率 %d%%" % (
            "开" if self.emoji_on else "关", len(self.sticker_book()),
            len(self.small_book()), self.emoji_rate))
        kb_ids = self.mem.get("kb") or []
        if kb_ids:
            log("知识库：这次带 %d 篇资料（最多注入 %d 字）" % (len(kb_ids), self.ai.kb_max))
        if self.direct:
            log("群聊直接回复：%d%%，两次插话至少隔 %d 秒｜管理必回 %s｜管理不等冷却 %s" % (
                self.direct_rate, int(self.direct_gap),
                "开" if self.admin_reply else "关", "开" if self.admin_bypass else "关"))
        else:
            log("群聊只接叫到的：没 @ 也没点名就不插话（直接回复已关）")
        log("主动开口：冒泡 %s｜群聊主动 %s｜私聊主动 %s" % (
            "开" if self.bubble_on else "关",
            "开" if self.gp_on else "关",
            "开" if self.ping_on else "关"))
        if self.emoji_on:
            self.sync_sticker_size()
        groups = self.targets()
        if not groups:
            log("没有可监听的会话，检查 config.json 的 groups / dms 设置", "ERROR")
            return 1
        self.groups = groups
        log("监听会话：%s" % "、".join(self.label(g) for g in groups))
        if self.control_on:
            log("私聊指令：开｜前缀 %s｜听懂人话 %s｜白名单 %d 人%s" % (
                self.control_prefix, "开" if self.control_nl else "关", len(self.control_uids),
                "" if self.control_uids else "（还没配白名单，私聊指令一律拒绝）"))
        self.init_baseline(groups)
        self.catch_up(groups)
        self.backfill_emoji(groups)
        self.upgrade_sticker_labels()
        self.backfill_sticker_labels()
        self.seed_aff()
        # 冷场计时从启动这一刻算起，免得刚开就冒
        now0 = time.time()
        for g in groups:
            self.last_activity.setdefault(g["gid"], now0)
        if self.bubble_on:
            log("群聊冒泡：开｜静 %d 分钟算冷场｜两次至少隔 %d 分钟｜开口概率 %d%%%s" % (
                self.bubble_idle // 60, self.bubble_gap // 60, self.bubble_chance,
                "｜自定义话题：%s" % self.bubble_prompt[:30] if self.bubble_prompt else "｜话题靠记忆和时间自己想"))
        if self.gp_on:
            log("群聊主动：%s｜全局至少隔 %d 分钟｜同一群至少隔 %d 分钟｜每群每天最多 %d 次｜"
                "%d-%d 点才开口｜群里静 %s 才说%s" % (
                    ("开（%d 个群）" % len(self.gp_ids)) if self.gp_ids else "没勾群，不会开口",
                    self.gp_every // 60, self.gp_gap // 60, self.gp_daily,
                    self.gp_h1, self.gp_h2, _dur(self.gp_idle),
                    "｜自定义话题：%s" % self.gp_prompt[:30] if self.gp_prompt else ""))
        if self.ping_on:
            log("私聊主动：开｜静 %s 算想起他｜同一人至少隔 %d 分钟｜每天最多 %d 次｜"
                "%d-%d 点才开口｜全局至少隔 %d 分钟｜连续 %d 次没回搁置 %.0f 小时%s" % (
                    _dur(self.ping_idle), self.ping_gap // 60, self.ping_daily,
                    self.ping_h1, self.ping_h2, self.ping_every // 60,
                    self.ping_giveup, self.ping_hold,
                    ("｜只找勾选的 %d 人" % len(self.ping_uids)) if self.ping_uids
                    else ("｜只找认识的人" if self.ping_known else "")))
        log("智能体模式：%s" % ("开｜会翻自己的聊天记录找旧事，回话前先自查一遍"
                              if self.ai.agent else "关｜只按人设说话，带最近 12 句群里的话"))
        if self.ai.mood_on:
            log("心情：开｜基线 %.0f｜没人理 %.1f 小时平复一半｜情绪 %.0f 分钟散一半" % (
                MOOD_BASE, MOOD_HALF / 3600.0, EMO_HALF / 60.0))
        else:
            log("心情：关｜只按人设平着说，不判情绪")
        log("通道：群聊 %s｜私聊 %s%s" % (
            "开" if self.group_on else "关（只听不说）",
            "开" if self.dm_on else "关（只听不说）",
            "" if (self.group_on and self.dm_on) else "　—— 关掉的一侧消息照样读进来，只是不回话也不主动开口"))
        log("开始轮询，间隔 %.1fs，触发方式：%s" % (self.poll, self.trig.get("mode")))
        if self.farewell_cfg["on"]:
            log("关机收尾：开｜点停止时先给 %d 秒写日记和总观，面板上能看到倒计时"
                % self.farewell_cfg["seconds"])
        else:
            log("关机收尾：关｜点停止就直接停，不写日记")
        self.write_state(groups)
        fail = 0
        while True:
            # 面板点「停止」会写这个标记。不直接杀进程，是为了让它先把话记下来。
            if os.path.exists(STOP_FLAG):
                log("收到停止请求，先收尾再退")
                try:
                    self.farewell(groups)
                except Exception as e:
                    log("收尾异常: %s" % e, "WARN")
                try:
                    os.remove(STOP_FLAG)
                except Exception:
                    pass
                log("已停止")
                return 0
            try:
                self.maybe_reload()
                self.handle_learn_request(groups)
                self.handle_users_learn_request(groups)
                self.run_once(groups)
                self.maybe_settle_mood()
                self.flush_aff()          # 好感度攒够 45 秒才真写盘，见 flush_aff
                self.maybe_group_ping(groups)
                self.maybe_bubble(groups)
                self.maybe_dm_ping(groups)
                fail = 0
            except KeyboardInterrupt:
                # 在窗口里按 Ctrl+C 也算正常停止，一样给时间写日记
                log("收到 Ctrl+C，先收尾")
                try:
                    self.farewell(groups)
                except Exception as e:
                    log("收尾异常: %s" % e, "WARN")
                return 0
            except Exception as e:
                fail += 1
                log("轮询异常(%d): %s" % (fail, e), "ERROR")
                try:
                    self.cdp.close()
                    self.client.ensure()
                    groups = self.targets()
                except Exception as e2:
                    log("重连失败: %s" % e2, "ERROR")
                    time.sleep(min(30, 2 * fail))
                    continue
            if int(time.time()) % 120 < self.poll:
                groups = self.targets() or groups
                self.groups = groups
            self.write_state(groups)
            time.sleep(self.poll)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def load_cfg():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def cmd_check():
    print("配置文件：%s" % CONFIG_PATH)
    cfg = load_cfg()
    ok = True

    print("触发方式：%s" % cfg.get("trigger", {}).get("mode"))
    cdp = Cdp(cfg.get("cdp", {}).get("port", PORT))
    try:
        url = cdp.connect()
        print("客户端调试端口：OK  %s" % url.split("/")[-1][:24])
    except Exception as e:
        print("客户端调试端口：失败  %s" % e)
        if "websocket-client" not in str(e):
            print("  → 请用启动脚本（启动机器人.bat）启动客户端，它会带 --remote-debugging-port=9222")
        return 1

    cli = Client(cdp)
    try:
        cli.ensure()
        print("机器人身份：%s (%s)" % (cli.self_name, cli.self_id))
    except Exception as e:
        print("注入客户端接口失败：%s" % e)
        return 1

    try:
        gs = cli.groups()
        print("可见群：%d 个" % len(gs))
        for g in gs[:10]:
            print("   %s  %s" % (g["gid"], g["name"]))
    except Exception as e:
        print("读取群列表失败：%s" % e)
        ok = False

    try:
        ds = cli.dms()
        print("好友私聊：%d 个%s" % (len(ds), "" if (cfg.get("dms") or {}).get("enabled", True) else "（只听不说）"))
        for x in ds[:10]:
            print("   %s  %s  %s" % (x["uid"], x["name"], x.get("last") or ""))
    except Exception as e:
        print("读取好友会话失败：%s" % e)
        ok = False

    ai = AI(cfg)
    if not ai.token:
        print("AI Key：缺失")
        ok = False
    else:
        print("AI Key：已配置 %s***%s" % (ai.token[:6], ai.token[-4:]))
        try:
            t0 = time.time()
            txt, tokens = ai.reply("_check", "自检", "系统", "只回复两个字：正常")
            print("AI 连通：OK  %.1fs  %s（%s token）" % (time.time() - t0, txt, tokens))
        except Exception as e:
            print("AI 连通：失败  %s" % e)
            ok = False

    print("\n结论：%s" % ("可以运行" if ok else "存在问题，见上"))
    return 0 if ok else 1


def cmd_groups():
    cfg = load_cfg()
    cli = Client(Cdp(cfg.get("cdp", {}).get("port", PORT)))
    gs = cli.groups()
    for g in gs:
        print("%s  %-20s %s" % (g["gid"], g["name"], g["last"]))
    return 0


def cmd_test_send():
    cfg = load_cfg()
    cli = Client(Cdp(cfg.get("cdp", {}).get("port", PORT)))
    gs = cli.groups()
    if not gs:
        print("没有可见群")
        return 1
    g = gs[0]
    r = cli.send(g["gid"], "机器人连通测试 " + datetime.now().strftime("%H:%M:%S"))
    print("目标群：%s\n结果：%s" % (g["name"], r))
    return 0 if r.get("ok") else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--groups", action="store_true")
    ap.add_argument("--test-send", action="store_true")
    ap.add_argument("--allow-multi", action="store_true",
                    help="允许同时运行多个机器人实例（默认不允许，避免同一条消息被回两次）")
    a = ap.parse_args()
    if a.check:
        return cmd_check()
    if a.groups:
        return cmd_groups()
    if a.test_send:
        return cmd_test_send()

    guard = None
    if not a.allow_multi:
        guard = acquire_single_instance()
        if guard is None:
            print("已有另一个机器人在运行，本次不再启动。")
            print("  同一条消息被两个实例看到会重复回复，所以默认只允许跑一份。")
            print("  请先关掉那个窗口（或结束对应的 python 进程）再启动本窗口。")
            print("  确实要同时跑多份（比如两个账号），加参数：python bot.py --allow-multi")
            return 2

    try:
        return Bot(load_cfg()).run()
    except KeyboardInterrupt:
        print("\n已停止")
        return 0
    finally:
        if guard is not None:
            try:
                guard.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
