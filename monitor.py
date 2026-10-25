# -*- coding: utf-8 -*-
"""
群聊机器人 · 实时监听网页

只读监控：每 2 秒从客户端拉一次群消息，并把机器人心跳、运行日志一并推给浏览器。
不发送任何消息，纯观察。

启动：python monitor.py   →  浏览器打开 http://127.0.0.1:8765
"""
import argparse
import base64
import hashlib
import http.server
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))


def app_root():
    """exe 所在目录。打包后 __file__ 在临时解包目录里，不能用来放数据。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return HERE


APP_ROOT = app_root()
FROZEN = bool(getattr(sys, "frozen", False))
DATA_DIR = os.path.join(APP_ROOT, "data") if FROZEN else HERE

BOT_PATH = os.path.join(HERE, "bot.py")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
CHAT_PATH = os.path.join(DATA_DIR, "panel_chat.json")   # 面板试聊的记录，重启后接着聊
LOG_DIR = os.path.join(DATA_DIR, "log")
PORT = 8765
BOUND_PORT = PORT   # 实际绑定的端口（8765 被占用时会顺延，页面上的地址要跟着变）
# 单实例锁跟着数据目录走：同一个 data/ 只允许一个面板，避免两个人设同时写同一份
# memory.json。早期用的是固定端口 47912，结果源码版和绿色版互相顶掉——绿色版双击
# 之后发现"已经有一个面板在运行"，浏览器又没弹出来，用户看到的就是"软件打不开"。
LOCK_FILE = os.path.join(DATA_DIR, ".panel.lock")


def _lock_read():
    try:
        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _lock_write(port=0):
    """把自己的 PID 和端口写进锁文件。写不进去也不要紧——多半是权限问题，
    宁可让用户开两个面板，也不能让程序开不起来。"""
    try:
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "port": int(port or 0),
                       "ts": time.time()}, f)
        return True
    except Exception:
        return False


def _lock_release():
    """退出时清掉自己的锁，别让下次启动读到一条死记录。"""
    d = _lock_read()
    if int(d.get("pid") or 0) == os.getpid():
        try:
            os.remove(LOCK_FILE)
        except Exception:
            pass


def panel_alive(port):
    """那个端口上是不是真的有个面板在应答。光看 PID 会被 PID 复用骗过去。"""
    if not port:
        return False
    try:
        with BOT.NO_PROXY.open("http://127.0.0.1:%d/api/state" % int(port), timeout=0.8) as r:
            return r.status == 200
    except Exception:
        return False


def existing_panel_url(base=None):
    """找出已经在跑的那个面板地址：先看锁文件里记的端口，再顺着 base 往上试。"""
    cands = []
    d = _lock_read()
    if d.get("port"):
        cands.append(int(d["port"]))
    b = int(base or PORT)
    cands += [b + i for i in range(8)]
    seen = set()
    for p in cands:
        if p in seen:
            continue
        seen.add(p)
        if panel_alive(p):
            return "http://127.0.0.1:%d" % p
    return None


def acquire_single_instance():
    """只允许开一个监听页：第二个会直接退出并提示，避免两个面板看到不同数据。
    锁是数据目录里的一个文件，进程异常退出后靠 PID + 端口双重校验自动失效，
    不会像端口锁那样留下脏状态。"""
    old = _lock_read()
    pid = int(old.get("pid") or 0)
    if pid and pid != os.getpid() and pid_alive(pid) and panel_alive(old.get("port")):
        return None
    _lock_write()
    return "file"


def alert_box(text, title="黑盒语音群聊机器人", error=False):
    """打包成无窗程序后没有控制台，出问题时必须弹窗。
    不弹的话用户看到的就是"双击了没反应"——这正是这次被打不开的原因。"""
    try:
        import ctypes
        flags = 0x10 if error else 0x40      # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(None, text, title, flags)
    except Exception:
        print(text, flush=True)


def open_browser(url):
    """打开默认浏览器。打包后 webbrowser 偶发找不到浏览器，用 ShellExecuteW 兜一层。"""
    if os.name == "nt":
        try:
            import ctypes
            r = ctypes.windll.shell32.ShellExecuteW(None, "open", url, None, None, 1)
            if int(r) > 32:
                return True
        except Exception:
            pass
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def page_html(html):
    """把页面里写死的 127.0.0.1:8765 换成实际端口，顺延后面板上的链接才不会指向别处。"""
    if BOUND_PORT == PORT:
        return html
    return html.replace("127.0.0.1:%d" % PORT, "127.0.0.1:%d" % BOUND_PORT)
CACHE_SECONDS = 1.2
MAX_MESSAGES = 40          # 默认值；真实上限由 config.ui.maxMessages 决定
MSG_CAPS = (40, 80, 200, 400)
MAX_LOG_LINES = 60


def load_bot():
    """打包后没有 bot.py 这个文件，直接按模块导入（PyInstaller 已把它打进 exe）。
    源码运行时仍按文件路径加载，方便单独改 bot.py 不用重新打包。"""
    if FROZEN:
        import bot as m
        return m
    spec = importlib.util.spec_from_file_location("xhh_bot", BOT_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


BOT = load_bot()

LOG_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s+([A-Z]+)\s+(.*)$")


def read_cfg():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def read_heartbeat():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def think_list(limit=24):
    """思考流：读 bot 写的 think.jsonl，新的在前。
    每行是它某一轮的实际动作——看见了什么、注入了什么、写完自查说了什么。"""
    try:
        return BOT.think_read(limit)
    except Exception:
        return []


def think_spent_hour():
    """这一小时真想了几次：数 think.jsonl 里带 cot 的记录。
    机器人进程有自己的一份计数，但面板是另一个进程读不到，所以按落盘的记录数。"""
    now = time.time()
    n = 0
    for r in think_list(300):
        if not r.get("cot"):
            continue
        try:
            if now - float(r.get("t") or 0) < 3600:
                n += 1
        except (TypeError, ValueError):
            continue
    return n


def think_left(cfg=None):
    """还剩几次。0 = 不限，就回一个很大的数。"""
    ai = (cfg or read_cfg()).get("ai") or {}
    try:
        cap = int(BOT._f(ai.get("thinkCap"), 20))
    except Exception:
        cap = 20
    if cap <= 0:
        return 9999
    return max(0, cap - think_spent_hour())


def affinity_list():
    """好感度：读 memory.json 的 aff，按分数从高到低排。
    名字优先用群友档案里的当前昵称——改过名也认得出是同一个人。"""
    try:
        mem = BOT.read_memory()
    except Exception:
        return []
    aff = mem.get("aff") if isinstance(mem.get("aff"), dict) else {}
    users = mem.get("users") if isinstance(mem.get("users"), dict) else {}
    out = []
    for uid, rec in aff.items():
        if not isinstance(rec, dict):
            continue
        u = users.get(str(uid)) or {}
        v = BOT._f(rec.get("v"), 50.0)
        out.append({"uid": str(uid),
                    "name": u.get("name") or rec.get("name") or ("UID " + str(uid)),
                    "v": round(max(0.0, min(100.0, v)), 1),
                    "band": BOT.aff_band(v),
                    "n": int(rec.get("n") or 0),
                    "why": str(rec.get("why") or ""),
                    "at": float(rec.get("at") or 0)})
    out.sort(key=lambda x: -x["v"])
    return out


# ---------------------------------------------------------------- 机器人进程管理
BOT_CONSOLE_LOG = os.path.join(LOG_DIR, "console.log")

# 本面板亲手拉起的机器人进程（启动器 PID）。面板关掉时按进程树清掉，
# 只清自己起的，用户手动开的机器人不归面板管。
SPAWNED = {"pid": None}

BOT_QUERY = (
    "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -ne $null -and "
    "($_.CommandLine -match '--bot(\\s|$)' -or $_.CommandLine -match 'bot\\.py') "
    "} | ForEach-Object { Write-Output ([string]$_.ProcessId + ' ' + $_.Name) }"
)


def pid_alive(pid):
    """PID 还活着吗。不靠进程名，避免打包后 exe 名字变了就认不出来。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259             # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def find_bot_pids():
    """列出所有机器人进程。心跳里的 PID 只是主进程，venv 的 python.exe 是启动器，
    会另起一个真解释器，所以停的时候要一网打尽，否则会剩一个孤儿进程继续回消息。"""
    pids = []
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", BOT_QUERY],
                           capture_output=True, text=True, timeout=20,
                           encoding="utf-8", errors="replace")
        for line in (r.stdout or "").splitlines():
            parts = line.strip().split(" ")
            if len(parts) >= 2 and parts[0].isdigit():
                pids.append(int(parts[0]))
    except Exception:
        pass
    hb = read_heartbeat() or {}
    p = hb.get("pid")
    if p and pid_alive(p) and int(p) not in pids:
        pids.append(int(p))
    return pids


def bot_status():
    hb = read_heartbeat() or {}
    pid = hb.get("pid")
    age = time.time() - float(hb.get("ts") or 0)
    alive = pid_alive(pid)
    # 心跳够新且进程还在才算运行中：光有进程但心跳停了（卡死）也当异常
    running = bool(alive and age < 20)
    return {
        "running": running,
        "stalled": bool(alive and age >= 20),
        "pid": pid,
        "age": round(age, 1) if hb.get("ts") else None,
        "uptime": int(time.time() - float(hb.get("started") or time.time())) if alive else 0,
        "selfId": hb.get("selfId", 0),
        "selfName": hb.get("selfName", ""),
        "replied": hb.get("replied", 0),
        "triggers": hb.get("triggers", 0),
        "enabled": bool(hb.get("enabled", True)),
        "agent": bool(hb.get("agent")),
        "mood": hb.get("mood") or {},
        "bubble": hb.get("bubble") or {},
        "ping": hb.get("ping") or {},
        "groupPing": hb.get("groupPing") or {},
        # 关机收尾进度：{active, phase, left, total, diary, outlook, groups}
        "farewell": hb.get("farewell") or {"active": False},
        # 已经把「停止」的标记放下去了，但机器人还没走到收尾那一步。这几秒里
        # 页面上不能显示「没在轮询」——它没坏，是在准备收尾。
        "stopping": bool(pid and alive and os.path.exists(stop_flag_path())),
    }


def bot_start():
    st = bot_status()
    if st["running"] or st["stalled"]:
        return {"ok": False, "err": "机器人已经在运行了（PID %s）" % st.get("pid")}

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except OSError:
        pass
    # 上一轮可能是被硬杀的，stop.flag 还留在那儿；不清掉的话新进程一起来就自杀
    clear_stop_flag()

    cmd = [sys.executable, "--bot"] if FROZEN else [sys.executable, BOT_PATH]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    flags = 0
    if os.name == "nt":
        flags = 0x08000000          # CREATE_NO_WINDOW，别从面板里再弹一个黑窗
    try:
        out = open(BOT_CONSOLE_LOG, "a", encoding="utf-8", errors="replace")
    except OSError:
        out = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(cmd, cwd=APP_ROOT, env=env, creationflags=flags,
                                stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        SPAWNED["pid"] = proc.pid
    except Exception as e:
        return {"ok": False, "err": "启动失败：%s: %s" % (type(e).__name__, e)}

    # 等它把心跳写出来。启动要连客户端、拉群列表，给足 20 秒
    before = (read_heartbeat() or {}).get("ts") or 0
    for _ in range(40):
        time.sleep(0.5)
        hb = read_heartbeat() or {}
        if (hb.get("ts") or 0) > before and pid_alive(hb.get("pid")):
            return {"ok": True, "pid": hb.get("pid"), "status": bot_status()}
    return {"ok": False, "err": "启动超时，没能拿到心跳。", "detail": start_failure_hint()}


def start_failure_hint():
    """启动失败时把控制台日志的最后几行拿出来——通常那里写着真正原因
    （客户端没开、端口没监听、API Key 无效）。"""
    try:
        with open(BOT_CONSOLE_LOG, "r", encoding="utf-8", errors="replace") as f:
            lines = [x.rstrip() for x in f.readlines() if x.strip()]
        return "\n".join(lines[-12:])
    except Exception:
        return ""


def farewell_cfg():
    """关机收尾的四个旋钮，和 bot.py 里 _farewell_cfg 保持一致的默认值。"""
    f = (read_cfg() or {}).get("farewell") or {}
    if not isinstance(f, dict):
        f = {}
    return {"on": bool(f.get("enabled", True)),
            "seconds": int(BOT._f(f.get("seconds"), 90)),
            "diary": bool(f.get("diary", True)),
            "outlook": bool(f.get("outlook", True)),
            "groups": int(BOT._f(f.get("maxGroups"), 3))}


def stop_flag_path():
    return os.path.join(BOT.DATA_DIR, "stop.flag")


def request_stop():
    """差机器人自己收尾：它每轮轮询都会看这个标记，看到就先写日记再退。"""
    try:
        with open(stop_flag_path(), "w", encoding="utf-8") as f:
            f.write("%.3f\n" % time.time())
        return True
    except Exception as e:
        BOT.log("写 stop.flag 失败: %s" % e, "WARN")
        return False


def clear_stop_flag():
    """标记不能留：留着的话下次一启动就以为要关机，刚起来就退。"""
    try:
        os.remove(stop_flag_path())
    except Exception:
        pass


def wait_stop(limit):
    """等机器人自己走完收尾。心跳里的 pid 不在了才算真退（光看心跳时间会被
    收尾时每秒刷新的 state 骗过去）。到点返回 False，让调用方硬杀。"""
    t0 = time.time()
    while time.time() - t0 < limit:
        hb = read_heartbeat() or {}
        pid = hb.get("pid")
        if not pid or not pid_alive(pid):
            return True
        time.sleep(0.5)
    return False


def bot_stop(graceful=True):
    """停机器人。优先杀面板自己拉起的进程树（快），查不到再退回全量扫描。
    全量扫描要调 PowerShell，一次几百毫秒，不能放进循环里。

    graceful=True 且开着「关机收尾」时，先放 stop.flag 让它自己写日记/总观，
    等它自己退；超时才硬杀——别让「写日记」变成新的关不掉。"""
    pids = []
    sp = SPAWNED.get("pid")
    if sp and pid_alive(sp):
        pids.append(int(sp))
    hb = read_heartbeat() or {}
    hp = hb.get("pid")
    if hp and pid_alive(hp) and int(hp) not in pids:
        pids.append(int(hp))
    if not pids:
        pids = find_bot_pids()

    if not pids:
        clear_stop_flag()
        return {"ok": True, "killed": [], "tip": "机器人本来就没在运行"}

    fc = farewell_cfg()
    if graceful and fc["on"]:
        if request_stop() and wait_stop(fc["seconds"] + 20):
            SPAWNED["pid"] = None
            clear_stop_flag()
            return {"ok": True, "killed": pids, "farewell": True,
                    "seconds": fc["seconds"]}
        BOT.log("等收尾超时，直接结束进程", "WARN")

    killed = []
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10,
                           creationflags=0x08000000)
            killed.append(pid)
        except Exception:
            pass
    SPAWNED["pid"] = None
    clear_stop_flag()

    left = []
    for _ in range(15):
        left = [p for p in pids if pid_alive(p)]
        if not left:
            break
        time.sleep(0.2)
    if left:
        left = find_bot_pids()
    return {"ok": not left, "killed": killed, "left": left,
            "err": ("还有进程没停掉：%s" % left) if left else ""}


def shutdown_bot():
    """面板退出时收拾机器人，避免「窗口关了它还在群里回消息」。
    打包后双击 exe 开的是面板，关掉就等于全停，不会再留后台孤儿。

    退出前照样给它留收尾时间：退出也是一次关机，日记不该因为点的是「退出」
    而不是「停止」就丢掉。收尾超时由 bot_stop 里的硬杀兜底。"""
    if not SPAWNED.get("pid") and not (read_heartbeat() or {}).get("pid"):
        return
    bot_stop(graceful=True)


def hard_exit(shutdown=True):
    """页面上的「退出程序」：先停机器人再让本进程消失。
    打包后没有控制台窗口可关，这是用户唯一的正常退出方式。

    shutdown=False 是给 quit_begin() 用的：那边已经自己等过收尾了，
    再走一遍 bot_stop 会白等第二个「秒数 + 20」。"""
    if shutdown:
        shutdown_bot()
    _lock_release()
    try:
        sys.stdout.flush()
    except Exception:
        pass
    time.sleep(0.4)
    os._exit(0)


# 退出进度：页面点「退出」以后不立刻断开，先让机器人收尾，页面能看见倒计时。
# 收尾写完才 os._exit——不然用户只看到一句「已退出」，几十秒里不知道它在干什么。
QUIT = {"active": False, "t0": 0.0, "seconds": 0, "done": False,
        "farewell": False, "phase": "", "err": ""}


def quit_begin():
    """开始退出。有收尾可做就先让机器人写，没有就快速退。
    重复点只认第一次，不会开两拨线程互相抢着 os._exit。"""
    if QUIT["active"]:
        return QUIT
    fc = farewell_cfg()
    hb = read_heartbeat() or {}
    alive = bool(hb.get("pid") and pid_alive(hb.get("pid")))
    do_farewell = bool(fc["on"] and alive)
    QUIT.update({"active": True, "t0": time.time(), "done": False, "err": "",
                 "farewell": do_farewell, "seconds": int(fc["seconds"]) if do_farewell else 0,
                 "phase": "准备收尾" if do_farewell else "正在退出"})
    threading.Thread(target=_quit_run, args=(do_farewell,), daemon=True).start()
    return QUIT


def _quit_run(do_farewell):
    """收尾 → 真退。收尾期间由页面每秒问 /api/quit/state 拿进度。"""
    try:
        if do_farewell:
            QUIT["phase"] = "等机器人收尾"
            request_stop()
            # 比机器人自己的上限多留一点余量，等它自己退；真卡住还有 bot_stop 兜底
            wait_stop(int(QUIT["seconds"]) + 20)
        else:
            QUIT["phase"] = "停机器人"
            bot_stop(graceful=False)
            time.sleep(0.6)
    except Exception as e:
        QUIT["err"] = str(e)[:120]
    QUIT["phase"] = "收尾完成，正在关闭"
    QUIT["done"] = True
    time.sleep(0.8)          # 留一拍让页面把最后那句「已退出」画出来
    hard_exit(shutdown=False)


def quit_state():
    """给页面的退出进度。left 按开始退出的时刻自己算，不依赖心跳——
    机器人写完了先退，心跳会消失，这时候倒计时仍然要往下走。"""
    if not QUIT["active"]:
        return {"active": False, "done": False, "farewell": False,
                "seconds": 0, "left": 0, "elapsed": 0, "phase": "", "err": ""}
    el = time.time() - QUIT["t0"]
    left = max(0, int(round(QUIT["seconds"] - el)))
    hb = read_heartbeat() or {}
    fw = hb.get("farewell") or {}
    return {"active": True, "done": QUIT["done"], "farewell": QUIT["farewell"],
            "seconds": QUIT["seconds"], "left": left, "elapsed": round(el, 1),
            # 机器人真的开始写了就把它的阶段/字数带上来，页面显示「正在写日记 · 第 2 个」
            "phase": (fw.get("phase") if fw.get("active") else "") or QUIT["phase"],
            "diary": int(fw.get("diary") or 0), "outlook": int(fw.get("outlook") or 0),
            "err": QUIT["err"]}


# ---------------------------------------------------------------- 首次运行引导
DEFAULT_CFG = {
    "cdp": {"port": 9222},
    "client": {"exe": ""},
    "trigger": {"mode": "mention", "prefix": "小菲", "keywords": []},
    "groups": {"mode": "all", "ids": [],
               # 群聊主动：在你勾的群里按节奏自己开口（和冒泡是两回事）
               "proactive": {"enabled": False, "ids": [],
                             "gapMinutes": 180, "dailyMax": 2,
                             "globalMinutes": 30, "idleMinutes": 30,
                             "hourStart": 9, "hourEnd": 23, "prompt": ""}},
    "dms": {"enabled": True, "ids": [], "alwaysReply": True,
            "ping": {"enabled": False, "idleMinutes": 180, "minGapMinutes": 300,
                     "globalMinutes": 25, "dailyMax": 2, "hourStart": 8, "hourEnd": 23,
                     "againMinutes": 45, "escalate": True, "giveUpAfter": 2,
                     "giveUpHours": 20, "onlyKnown": True, "prompt": ""}},
    "reply": {"maxChars": 280, "dmMaxChars": 800, "styleGate": True,
              "groupCooldownSeconds": 3, "globalCooldownSeconds": 1,
              "directReply": True, "directReplyRate": 20, "directMinGapSeconds": 60,
              "adminAlwaysReply": True, "adminBypassCooldown": True},
    "ai": {"baseUrl": "https://api.deepseek.com/v1/chat/completions",
           "model": "deepseek-chat", "token": "", "prompt": "",
           "maxTokens": 900, "temperature": 1.0, "historyRounds": 6,
           "timeoutSeconds": 60, "learnEvery": 6, "learnMaxNotes": 20},
    "emoji": {"enabled": True, "autoCollect": True, "useRate": 40, "maxStickers": 30},
    "bubble": {"enabled": False, "idleMinutes": 30, "gapMinutes": 120,
               "chance": 50, "prompt": ""},
    "control": {"enabled": True, "prefix": "/", "nl": True, "uids": []},
    "kb": {"maxChars": 4000},
    "ui": {"maxMessages": 40},
    "poll": {"intervalSeconds": 2.5},
}


def deep_merge(base, patch):
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def tcp_ok(port, timeout=0.6):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", int(port)))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def client_candidates():
    """猜客户端装在哪。官方安装包位置比较固定，但很多人（包括本机）是绿色版，
    装在随便哪个盘，所以除了常见目录还要问注册表和正在运行的进程。"""
    found = []

    def add(p):
        p = (p or "").strip().strip('"')
        if not p:
            return
        p = p.split(",")[0].strip()      # 注册表 DisplayIcon 常带 ",0"
        # 只要客户端本体。同目录下还有加速器 heyboxacc.exe，别混进来
        if (os.path.basename(p).lower() == "heyboxchat.exe"
                and os.path.isfile(p) and p not in found):
            found.append(p)

    la = os.environ.get("LOCALAPPDATA") or ""
    pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
    pf86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    for base in (os.path.join(la, "Programs", "HeyboxChat") if la else "",
                 os.path.join(la, "HeyboxChat") if la else "",
                 os.path.join(la, "Programs", "heyboxchat") if la else "",
                 os.path.join(pf, "HeyboxChat"),
                 os.path.join(pf86, "HeyboxChat")):
        add(os.path.join(base, "HeyboxChat.exe"))

    ps = ("$ErrorActionPreference='SilentlyContinue';"
          "(Get-Process HeyboxChat | Select-Object -First 1 -ExpandProperty Path);"
          "Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
          "'HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
          "'HKLM:\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*' "
          "| Where-Object { $_.DisplayName -match '黑盒|Heybox' } "
          "| ForEach-Object { $_.DisplayIcon }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=20,
                           encoding="utf-8", errors="replace")
        for line in (r.stdout or "").splitlines():
            add(line)
    except Exception:
        pass

    cfg = read_cfg()
    add(((cfg.get("client") or {}).get("exe")) or "")
    return found


def client_running():
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq HeyboxChat.exe", "/NH"],
                           capture_output=True, text=True, timeout=10,
                           encoding="utf-8", errors="replace")
        return "HeyboxChat.exe" in (r.stdout or "")
    except Exception:
        return False


def browse_exe():
    """弹 Windows 原生的选文件框，让用户直接指到 HeyboxChat.exe。
    比让他手打路径靠谱得多——绿色版的路径谁也猜不到。"""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        class OPENFILENAMEW(ctypes.Structure):
            _fields_ = [
                ("lStructSize", wintypes.DWORD), ("hwndOwner", wintypes.HWND),
                ("hInstance", wintypes.HINSTANCE), ("lpstrFilter", wintypes.LPCWSTR),
                ("lpstrCustomFilter", wintypes.LPWSTR), ("nMaxCustFilter", wintypes.DWORD),
                ("nFilterIndex", wintypes.DWORD), ("lpstrFile", wintypes.LPWSTR),
                ("nMaxFile", wintypes.DWORD), ("lpstrFileTitle", wintypes.LPWSTR),
                ("nMaxFileTitle", wintypes.DWORD), ("lpstrInitialDir", wintypes.LPCWSTR),
                ("lpstrTitle", wintypes.LPCWSTR), ("Flags", wintypes.DWORD),
                ("nFileOffset", wintypes.WORD), ("nFileExtension", wintypes.WORD),
                ("lpstrDefExt", wintypes.LPCWSTR), ("lCustData", wintypes.LPARAM),
                ("lpfnHook", wintypes.LPVOID), ("lpTemplateName", wintypes.LPCWSTR),
            ]

        buf = ctypes.create_unicode_buffer(1024)
        ofn = OPENFILENAMEW()
        ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
        ofn.lpstrFilter = "黑盒语音客户端 (HeyboxChat.exe)\0*.exe\0所有文件\0*.*\0"
        ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
        ofn.nMaxFile = 1024
        ofn.lpstrTitle = "选择黑盒语音客户端 HeyboxChat.exe"
        ofn.lpstrDefExt = "exe"
        ofn.Flags = 0x00001000 | 0x00000800 | 0x00000008 | 0x00080000
        if ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
            return buf.value
    except Exception:
        pass
    return ""


def need_setup():
    cfg = read_cfg()
    tok = ((cfg.get("ai") or {}).get("token") or "").strip()
    return len(tok) < 12


def setup_state():
    cfg = read_cfg()
    port = ((cfg.get("cdp") or {}).get("port") or 9222)
    return {
        "needSetup": need_setup(),
        "clientPath": ((cfg.get("client") or {}).get("exe") or ""),
        "candidates": client_candidates(),
        "clientRunning": client_running(),
        "clientOnline": tcp_ok(port),
        "port": port,
        "model": ((cfg.get("ai") or {}).get("model") or ""),
        "prefix": ((cfg.get("trigger") or {}).get("prefix") or ""),
        "dataDir": DATA_DIR,
    }


def launch_client(exe, port=9222):
    """用调试端口把客户端拉起来。客户端已经在跑但没开调试端口时，
    再启动一个新实例只会激活旧窗口，必须让用户先彻底退出。"""
    exe = (exe or "").strip().strip('"')
    if not exe or not os.path.isfile(exe):
        return {"ok": False, "err": "路径不对：找不到这个文件。请重新选择 HeyboxChat.exe。"}
    if tcp_ok(port):
        return {"ok": True, "msg": "调试端口已经在线，不用再启动"}
    if client_running():
        return {"ok": False,
                "err": "客户端已经在运行了，但它没开调试端口。\n"
                       "两个办法，挑一个：\n"
                       "  · 双击「黑盒客户端.bat」——它会问你一次，然后带调试端口重开窗口；\n"
                       "  · 或者托盘图标右键 → 退出（确认任务管理器里没有 HeyboxChat.exe），"
                       "再点一次这个按钮。"}
    try:
        subprocess.Popen([exe, "--remote-debugging-port=%d" % port],
                         cwd=os.path.dirname(exe),
                         creationflags=0x00000008)     # DETACHED_PROCESS
    except Exception as e:
        return {"ok": False, "err": "启动失败：%s" % e}
    for _ in range(60):
        time.sleep(0.5)
        if tcp_ok(port):
            return {"ok": True, "msg": "客户端已启动，调试端口在线"}
    return {"ok": False, "err": "客户端起来了，但调试端口没开。可能是版本改了这个参数，"
                                "或者被安全软件拦住了。"}


def test_ai_key(base_url, token, model):
    url = (base_url or "").strip() or DEFAULT_CFG["ai"]["baseUrl"]
    token = (token or "").strip()
    if len(token) < 12:
        return {"ok": False, "err": "Key 太短了，检查一下是不是没复制全"}
    body = {"model": model or "deepseek-chat",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1, "stream": False}
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + token})
    try:
        with BOT.NO_PROXY.open(req, timeout=20) as r:
            r.read(200)
        return {"ok": True, "msg": "Key 可用"}
    except urllib.error.HTTPError as e:
        try:
            txt = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            txt = ""
        if e.code in (401, 403):
            return {"ok": False, "err": "Key 无效或被拒绝（HTTP %d）" % e.code}
        if e.code == 402:
            return {"ok": False, "err": "这个 Key 余额不足（HTTP 402），充点钱再用"}
        return {"ok": False, "err": "HTTP %d：%s" % (e.code, txt)}
    except Exception as e:
        return {"ok": False, "err": "%s: %s" % (type(e).__name__, e)}


def setup_save(p):
    cfg = read_cfg()
    patch = {
        "client": {"exe": (p.get("clientPath") or "").strip()},
        "ai": {"token": (p.get("token") or "").strip()},
    }
    if p.get("baseUrl"):
        patch["ai"]["baseUrl"] = p["baseUrl"].strip()
    if p.get("model"):
        patch["ai"]["model"] = p["model"].strip()
    if p.get("prefix"):
        patch["trigger"] = {"prefix": p["prefix"].strip()}
    if "directReply" in p:
        patch.setdefault("reply", {})["directReply"] = bool(p.get("directReply"))
    merged = deep_merge(deep_merge(DEFAULT_CFG, cfg), patch)
    tok = ((merged.get("ai") or {}).get("token") or "").strip()
    if len(tok) < 12:
        return {"ok": False, "err": "还没填 API Key"}
    write_cfg(merged)
    log_line("首次配置已保存（客户端：%s）" % (merged["client"].get("exe") or "未选择"))
    return {"ok": True, "needSetup": False}


def log_line(msg):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, datetime.now().strftime("%Y-%m-%d") + ".log"),
                  "a", encoding="utf-8") as f:
            f.write("[%s] INFO  %s\n" % (datetime.now().strftime("%H:%M:%S"), msg))
    except Exception:
        pass


_WRITE_LOCK = threading.Lock()


def _tmp_name(path):
    """临时文件名要唯一：多个请求同时保存时，光靠 pid 会撞成 Permission denied。"""
    return "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())


def write_cfg(cfg):
    """原子写回 config.json，机器人靠 mtime 感知并热更新"""
    with _WRITE_LOCK:
        tmp = _tmp_name(CONFIG_PATH)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, CONFIG_PATH)
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                pass
            raise


def msg_cap(cfg=None):
    """实时消息流最多显示多少条。面板上能选 40/80/200/400，手改配置也不受限制，
    但压到 10–600 之间——再大浏览器每 1.2 秒重绘一次就卡了。"""
    try:
        cfg = cfg if cfg is not None else read_cfg()
        n = int((cfg.get("ui") or {}).get("maxMessages") or MAX_MESSAGES)
    except Exception:
        n = MAX_MESSAGES
    return max(10, min(600, n))


def group_reply_cfg():
    r = read_cfg().get("reply") or {}
    try:
        rate = int(r.get("directReplyRate", 30))
    except Exception:
        rate = 30
    try:
        gap = int(float(r.get("directMinGapSeconds", 60) or 0))
    except Exception:
        gap = 60
    return {"ok": True,
            "directReply": bool(r.get("directReply", False)),
            "rate": max(1, min(100, rate)),
            "gap": max(0, min(3600, gap)),
            "adminReply": bool(r.get("adminAlwaysReply", True)),
            "adminBypass": bool(r.get("adminBypassCooldown", True)),
            "cooldown": float(r.get("groupCooldownSeconds", 3))}


# ---------------------------------------------------------------- 通道开关
def _platform_name():
    """面板上给通道开关打的那个平台前缀。
    现在只有小黑盒一个通道，但按钮上必须写明是「哪儿的」群聊/私聊——
    否则以后接了第二个平台，看着就像一个管所有的总闸，关一处会误以为全关了。"""
    try:
        for a in (BOT.read_memory().get("accounts") or []):
            n = str((a or {}).get("platform") or "").strip()
            if n:
                return n
    except Exception:
        pass
    return "小黑盒"


def channel_cfg():
    """群聊 / 私聊两个总开关。

    关掉的语义是「只听不说」：消息照样读进上下文、照样记日记和群友档案，
    只是不回话、不主动开口。它不接话，但知道这段时间发生了什么。

    真人值和心跳值分开返回：机器人没在跑的时候，面板显示的是「下次启动会是什么样」。
    """
    cfg = read_cfg()
    g = cfg.get("groups") or {}
    d = cfg.get("dms") or {}
    hb = read_heartbeat() or {}
    return {"ok": True,
            "platform": _platform_name(),
            "group": bool(g.get("enabled", True)),
            "dm": bool(d.get("enabled", True)),
            "liveGroup": hb.get("groupOn") if hb else None,
            "liveDm": hb.get("dmOn") if hb else None,
            "running": bool(hb and time.time() - float(hb.get("ts") or 0) < 30)}


# ---------------------------------------------------------------- 面板试聊
# 这是「直接跟它说句话」的窗口：不经过小黑盒，不碰真实会话的历史，
# 但人设、日记、群友档案、心情全用的是当前那一份——所以看到的就是它现在的样子。
_CHAT_LOCK = threading.Lock()
_CHAT = {}              # {卡片id: [msgs]} —— 试聊按人格分桶，换张卡就是换个人
CHAT_KEEP = 40          # 面板自己记多少条（20 轮）


def _chat_rows(rows):
    """把落盘的行洗成 [{role, content}]，只留能接得上的那几种"""
    return [{"role": m["role"], "content": str(m["content"])}
            for m in (rows or [])
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")
            and str(m.get("content") or "").strip()][-CHAT_KEEP:]


def _chat_load():
    """面板试聊的记录。放在 data/panel_chat.json，重启面板还能接着聊。
    这不是它的记忆——它的记忆在 memory.json。这里只是「我刚才试过什么」。
    按人物卡片分桶存：切一张卡就是换一个人，新人格不该看见上一个人跟你聊过什么。
    （以前只有一条线不分卡，空白菲比一开口就接上了旧菲比的「晨祷、理圣事部的册子」。）"""
    out = {}
    try:
        with open(CHAT_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        by = d.get("by")
        if isinstance(by, dict):
            for cid, rows in by.items():
                got = _chat_rows(rows)
                if got:
                    out[str(cid)] = got
        elif isinstance(d.get("msgs"), list):
            # 老格式只有一条线、也没记是谁的。判断不了就别猜——塞进一个永远不会被
            # 显示的分桶（__legacy__）当存档，宁可看不见，也不能让新人格读到旧人格的话。
            got = _chat_rows(d["msgs"])
            if got:
                out["__legacy__"] = got
    except Exception:
        pass
    return out


def _chat_card():
    """当前「使用中」那张卡片 —— 试聊记录按它分桶"""
    try:
        return str(BOT.read_memory().get("profile") or "") or "__none__"
    except Exception:
        return "__none__"


def _chat_msgs():
    """当前卡片的那条试聊线（没有就建一条空的）"""
    return _CHAT.setdefault(_chat_card(), [])


def _chat_save():
    try:
        tmp = _tmp_name(CHAT_PATH)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"by": _CHAT, "card": _chat_card(), "updated": time.time()},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, CHAT_PATH)
    except Exception:
        pass


_CHAT = _chat_load()


def _owner_identity():
    """面板试聊假装是主人在私聊。主人的 UID 从 control.uids 取，昵称从群友档案里找，
    这样它在这儿的称呼和在真私聊里一致。"""
    cfg = read_cfg()
    uids = (cfg.get("control") or {}).get("uids") or []
    uid = str(uids[0]).strip() if uids else ""
    name = ""
    if uid:
        try:
            v = (BOT.read_memory().get("users") or {}).get(uid) or {}
            alias = v.get("alias")
            if isinstance(alias, list) and alias:
                name = str(alias[-1] or "")
            name = name or str(v.get("name") or "")
        except Exception:
            name = ""
    return uid, (name or "对面的人")


def chat_state():
    with _CHAT_LOCK:
        rows = [{"role": m["role"], "text": m["content"]} for m in _chat_msgs()]
    uid, name = _owner_identity()
    mem = BOT.read_memory()
    mood = mem.get("mood") or {}
    return {"ok": True, "msgs": rows, "who": name, "uid": uid,
            "card": str(mem.get("profile") or ""),
            "mood": {"v": round(BOT._f(mood.get("v"), BOT.MOOD_BASE), 1),
                     "band": BOT.mood_band(mood.get("v"))}}


def chat_say(text):
    """面板里说一句，直接要它回一句。同步返回，前端显示「正在想…」"""
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "先说点什么"}
    if len(text) > 500:
        text = text[:500]
    uid, name = _owner_identity()
    gid = uid or "__panel__"
    with _CHAT_LOCK:
        cfg = read_cfg()
        mem = BOT.read_memory()
        # 试聊线只认当前这张卡：换了人格就是另一条线，别把上一个人的对话喂给它
        msgs = _CHAT.setdefault(str(mem.get("profile") or "") or "__none__", [])
        try:
            ai = BOT.AI(cfg, mem)
            # 面板这边说的话不进独立会话：试聊只是我在试，别让她以为跟主人真聊过这些
            ai.no_peer_write = True
            # 把面板这边的对话塞进它自己的历史槽位，它才接得住上一句。
            # 用完不落盘（reply 不写 history，remember 才写），所以真实私聊的历史不受影响。
            ai._history[gid] = [dict(m) for m in msgs]
            ans, _tok = ai.reply(gid, "控制台", name, text,
                                 uid or None, is_dm=True)
        except Exception as e:
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        ans = (ans or "").strip()
        if not ans:
            return {"ok": False, "error": "它没出声（模型返回空），再问一次试试"}
        # 面板这条线不走 Bot._tidy_answer，得自己处理「说了记下」：
        # 先把 [[记:…]] 抠出来落盘，再剥表情指令——否则这串会露给用户看。
        ans, memos = BOT.take_memo(ans)
        if not memos and uid and BOT.MEMO_SAID_RE.search(ans):
            # 嘴上说了「记下了」却没写标记：跟真实链路一样，按原话兜底记一条，
            # 不然这句承诺就是空的——用户会去翻档案，然后发现什么都没变。
            memos = ["（他说）" + BOT._one_line(text, 46)]
            BOT.log("面板试聊：说了记下但没写标记，按原话兜底记了一条", "WARN")
        if memos and uid:
            for x in memos[:2]:
                try:
                    ai.memo_for(uid, x, name)
                except Exception as e:
                    BOT.log("面板试聊记便签失败: %s" % e, "WARN")
        # 它写话时可能带上表情指令（[cube_惊讶] 这种），真实的发送链路会把它们
        # 换成表情包再发。面板这里发不出表情，就直接剥掉，别把指令原文露给你看。
        ans = re.sub(r"\[[^\[\]]{1,24}\]", "", ans).strip()
        if not ans:
            ans = "（这句只有表情，面板显示不出来）"
        msgs.append({"role": "user", "content": text})
        msgs.append({"role": "assistant", "content": ans})
        del msgs[:-CHAT_KEEP]
        _chat_save()
        return {"ok": True, "reply": ans, "who": name, "uid": uid,
                "mood": {"v": round(BOT._f(ai.mood.get("v"), BOT.MOOD_BASE), 1),
                         "band": BOT.mood_band(ai.mood.get("v"))},
                "agent": bool(ai.agent)}


def chat_clear():
    """只清当前这张卡的试聊线，别人格的记录留着（切回去还在）"""
    with _CHAT_LOCK:
        _CHAT[_chat_card()] = []
    _chat_save()
    return {"ok": True}


# ---------------------------------------------------------------- 群聊冒泡
def _int(v, d, lo, hi):
    try:
        v = int(float(v))
    except Exception:
        return d
    return max(lo, min(hi, v))


def bubble_cfg():
    """群聊冒泡：开关 + 冷场判定 + 间隔 + 概率 + 自定义话题"""
    b = read_cfg().get("bubble") or {}
    hb = read_heartbeat() or {}
    bub = hb.get("bubble") or {}
    return {"ok": True,
            "enabled": bool(b.get("enabled", False)),
            "idle": _int(b.get("idleMinutes", 30), 30, 1, 1440),
            "gap": _int(b.get("gapMinutes", 120), 120, 0, 2880),
            "chance": _int(b.get("chance", 50), 50, 1, 100),
            "prompt": str(b.get("prompt") or ""),
            "bubbles": int(bub.get("bubbles") or 0),
            "lastBubble": float(bub.get("lastBubble") or 0)}


def _minute(v, default):
    """读「分钟」配置。0 是合法值（群聊里表示不管），所以不能用 or 兜底。"""
    try:
        if v is None or v == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


# ---------------------------------------------------------------- 私聊主动
def dm_contacts():
    """「找谁」的候选名单：心跳里的私聊会话 + 档案里有、会话列表里没有的人。
    有档案的排前面，面板上会标一个「有档案」的小点。"""
    hb = read_heartbeat() or {}
    out, seen = [], set()
    for g in (hb.get("groups") or []):
        if not isinstance(g, dict) or str(g.get("kind")) != "dm":
            continue
        uid = str(g.get("gid") or "").strip()
        if not uid or uid in seen:
            continue
        seen.add(uid)
        out.append({"uid": uid, "name": str(g.get("name") or ""),
                    "last": str(g.get("last") or "")[:40], "known": False})
    try:
        users = BOT.read_memory().get("users") or {}
    except Exception:
        users = {}
    for c in out:
        u = users.get(c["uid"]) or {}
        c["known"] = bool(u.get("notes") or u.get("alias"))
    for uid, u in users.items():
        uid = str(uid)
        if uid in seen:
            continue
        u = u or {}
        out.append({"uid": uid, "name": str(u.get("alias") or u.get("name") or ""),
                    "last": "", "known": bool(u.get("notes") or u.get("alias"))})
    out.sort(key=lambda x: (not x["known"], x["name"] or x["uid"]))
    return out


def ping_cfg():
    """私聊主动：好友很久没说话时自己去找他。策略在 dms.ping 下，统计从心跳里拿"""
    d = read_cfg().get("dms") or {}
    p = d.get("ping") or {}
    hb = read_heartbeat() or {}
    hp = hb.get("ping") or {}
    peers = []
    for x in (hp.get("peers") or [])[:8]:
        if isinstance(x, dict):
            peers.append({"uid": str(x.get("uid") or ""), "name": str(x.get("name") or ""),
                          "ts": float(x.get("ts") or 0), "text": str(x.get("text") or ""),
                          "n": int(x.get("n") or 0)})
    return {"ok": True,
            "enabled": bool(p.get("enabled", False)),
            "idle": int(round(_minute(p.get("idleMinutes"), 180))),
            "idleSec": int(round(_minute(p.get("idleMinutes"), 180) * 60)),
            "gap": _int(p.get("minGapMinutes", 300), 300, 0, 10080),
            "every": _int(p.get("globalMinutes", 25), 25, 1, 1440),
            "daily": _int(p.get("dailyMax", 2), 2, 1, 20),
            "hourStart": _int(p.get("hourStart", 8), 8, 0, 23),
            "hourEnd": _int(p.get("hourEnd", 23), 23, 0, 23),
            "escalate": bool(p.get("escalate", True)),
            "giveUp": _int(p.get("giveUpAfter", 2), 2, 1, 10),
            "giveUpHours": _int(p.get("giveUpHours", 20), 20, 1, 168),
            "onlyKnown": bool(p.get("onlyKnown", True)),
            "prompt": str(p.get("prompt") or ""),
            # 勾了人就只找勾上的；空表示按 onlyKnown 的规则自己挑
            "uids": sorted(set(str(x) for x in (p.get("uids") or []) if str(x).strip())),
            "contacts": dm_contacts(),
            "pings": int(hp.get("pings") or 0),
            "lastPing": float(hp.get("lastPing") or 0),
            "peers": peers}


# ---------------------------------------------------------------- 群聊主动
def proactive_cfg():
    """群聊主动：在你勾的群里按节奏自己开口（冒泡是等冷场，这个是到点就说）"""
    g = read_cfg().get("groups") or {}
    p = g.get("proactive") or {}
    hb = read_heartbeat() or {}
    hp = hb.get("groupPing") or {}
    picked = [str(x).strip() for x in (p.get("ids") or []) if str(x).strip()]
    rooms, seen = [], set()
    for x in (hb.get("groups") or []):
        if not isinstance(x, dict) or str(x.get("kind")) != "group":
            continue
        gid = str(x.get("gid") or "").strip()
        if not gid or gid in seen:
            continue
        seen.add(gid)
        rooms.append({"gid": gid, "name": str(x.get("name") or gid)})
    rooms.sort(key=lambda r: r["name"])
    recent = []
    for x in (hp.get("recent") or [])[:8]:
        if isinstance(x, dict):
            recent.append({"gid": str(x.get("gid") or ""), "name": str(x.get("name") or ""),
                           "ts": float(x.get("ts") or 0), "text": str(x.get("text") or ""),
                           "n": int(x.get("n") or 0), "post": bool(x.get("post"))})
    return {"ok": True,
            "enabled": bool(p.get("enabled", False)),
            "ids": picked,
            "rooms": rooms,
            "gap": _int(p.get("gapMinutes", 180), 180, 0, 10080),
            "daily": _int(p.get("dailyMax", 2), 2, 1, 20),
            "every": _int(p.get("globalMinutes", 30), 30, 1, 1440),
            "idle": int(round(_minute(p.get("idleMinutes"), 0))),
            "idleSec": int(round(_minute(p.get("idleMinutes"), 0) * 60)),
            "hourStart": _int(p.get("hourStart", 9), 9, 0, 23),
            "hourEnd": _int(p.get("hourEnd", 23), 23, 0, 23),
            "prompt": str(p.get("prompt") or ""),
            "says": int(hp.get("says") or 0),
            "lastSay": float(hp.get("last") or 0),
            "recent": recent}


# ---------------------------------------------------------------- 私聊指令
def control_cfg():
    """私聊指令：开关 + 前缀 + 白名单；心跳里带执行次数和最近私聊过它的人"""
    c = read_cfg().get("control") or {}
    hb = read_heartbeat() or {}
    cc = hb.get("control") or {}
    uids = []
    for u in (c.get("uids") or []):
        u = re.sub(r"\D", "", str(u))
        if u and u not in uids:
            uids.append(u)
    return {"ok": True,
            "enabled": bool(c.get("enabled", True)),
            "prefix": (str(c.get("prefix") or "/").strip() or "/")[:4],
            "nl": bool(c.get("nl", True)),
            "uids": uids,
            "cmds": int(cc.get("cmds") or 0),
            "recent": cc.get("recent") or [],
            "groups": [{"name": g.get("name") or "", "kind": g.get("kind") or "group"}
                       for g in (hb.get("groups") or [])]}


# ---------------------------------------------------------------- 表情包
EMOJI_MAP_PATH = os.path.join(DATA_DIR, "emoji_map.json")

def emoji_cfg():
    e = read_cfg().get("emoji") or {}

    def num(k, d, lo, hi):
        try:
            v = int(e.get(k, d))
        except Exception:
            v = d
        return max(lo, min(hi, v))

    return {"enabled": bool(e.get("enabled", True)),
            "autoCollect": bool(e.get("autoCollect", True)),
            "useRate": num("useRate", 20, 0, 100),
            "maxStickers": num("maxStickers", 60, 8, 200)}


def emoji_icons():
    """小表情码 → 图标地址（机器人启动时从客户端缓存下来的）"""
    try:
        with open(EMOJI_MAP_PATH, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("map") or {}
    except Exception:
        return {}


def emoji_thumb(url, size=160):
    """面板里只显示 50px，用缩略图省流量；原图地址照样留着发送用。
    表情包原图动辄 300KB+，缩略后约 40KB。"""
    u = (url or "").strip()
    if not u.startswith("http") or ("max-c.com" not in u and "maxjia" not in u):
        return u
    base = u.split("?")[0]
    return "%s?imageMogr2/thumbnail/%dx" % (base, int(size))


MAX_STICKER_BYTES = 10 * 1024 * 1024     # 客户端自己的限制：超过 10MB 传不上去
IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def decode_b64_image(b64):
    """面板选好图后传上来的 base64 → (bytes, 错误说明)"""
    s = (b64 or "").strip()
    if s.startswith("data:"):
        i = s.find(",")
        if i < 0:
            return b"", "图片数据不完整"
        s = s[i + 1:]
    s = re.sub(r"\s+", "", s)
    try:
        raw = base64.b64decode(s + "=" * ((4 - len(s) % 4) % 4))
    except Exception:
        return b"", "图片数据解析失败，重选一次试试"
    if not raw:
        return b"", "这张图是空的"
    if len(raw) > MAX_STICKER_BYTES:
        return b"", "图片 %.1f MB，客户端最多只收 10 MB" % (len(raw) / 1048576.0)
    return raw, ""


def upload_sticker_image(raw, name=""):
    """本机图片 → 聊天图床，返回 {ok, url, w, h, md5} 或 {ok:False, err}。
    借客户端自己的 CosUploader 上传，不用另外申请图床权限。"""
    ext = os.path.splitext(name or "")[1].lower()
    if ext not in IMG_EXTS:
        ext = ".png"
    fd, tmp = tempfile.mkstemp(prefix="sticker_", suffix=ext)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        cdp = BOT.Cdp(read_cfg().get("cdp", {}).get("port", 9222))
        try:
            cdp.connect()
            r = BOT.Client(cdp).upload_image(tmp)
        finally:
            cdp.close()
    except Exception as e:
        return {"ok": False, "err": "上传失败：%s" % str(e)[:120]}
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
    if not r.get("ok") or not r.get("url"):
        return {"ok": False, "err": (r.get("err") or "客户端没返回图片地址")[:120]}
    return {"ok": True, "url": r["url"], "w": int(r.get("w") or 0),
            "h": int(r.get("h") or 0), "size": len(raw),
            "md5": hashlib.md5(raw).hexdigest()}


def put_sticker(st, url, w=0, h=0, src="手动添加", md5=""):
    """收藏一张表情包（同一张图只留一份）。返回 (key, 是否新加的)"""
    key = BOT.Bot.sticker_key(url)
    if not key or len(key) < 6:
        return "", False
    old = st.get(key) if isinstance(st.get(key), dict) else {}
    st[key] = {"url": url,
               "w": int(w or old.get("w") or 0), "h": int(h or old.get("h") or 0),
               "count": int(old.get("count") or 0),
               "sent": int(old.get("sent") or 0),
               "from": old.get("from") or src,
               "first": old.get("first") or time.time(),
               "last": time.time(),
               "label": old.get("label") or ""}
    if md5:
        st[key]["md5"] = md5
    if src in ("本机图片", "手动添加"):
        st[key]["manual"] = True      # 手动收的给点底分，别永远排在后面
    return key, not old


def emoji_payload():
    cfg = emoji_cfg()
    mem = BOT.read_memory()
    em = mem.get("emoji") or {}
    icons = emoji_icons()
    small = []
    for k, v in (em.get("small") or {}).items():
        v = v if isinstance(v, dict) else {}
        small.append({"key": k,
                      "code": v.get("code") or ("[" + k + "]"),
                      "name": k.split("_", 1)[-1],
                      "count": int(v.get("count") or 0),
                      "img": icons.get(k, "")})
    small.sort(key=lambda x: -x["count"])
    stick = []
    for k, v in (em.get("sticker") or {}).items():
        if not isinstance(v, dict) or not v.get("url"):
            continue
        stick.append({"key": k, "url": v["url"],
                      "img": emoji_thumb(v["url"]),
                      "count": int(v.get("count") or 0),
                      "sent": int(v.get("sent") or 0),
                      "from": v.get("from") or "",
                      "label": v.get("label") or ""})
    stick.sort(key=lambda x: (-x["count"], -x["sent"]))
    out = {"ok": True, "small": small[:48], "stickers": stick,
           "smallTotal": len(small), "stickerTotal": len(stick)}
    out.update(cfg)
    return out


def save_emoji(p):
    """开关、频率写 config；收藏增删改写在 memory，机器人会热更新"""
    cfg = read_cfg()
    e = cfg.setdefault("emoji", {})
    if "enabled" in p:
        e["enabled"] = bool(p.get("enabled"))
    if "autoCollect" in p:
        e["autoCollect"] = bool(p.get("autoCollect"))
    if "useRate" in p:
        try:
            r = int(p.get("useRate"))
        except Exception:
            r = 20
        e["useRate"] = max(0, min(100, r))
    if "maxStickers" in p:
        try:
            n = int(p.get("maxStickers"))
        except Exception:
            n = 60
        e["maxStickers"] = max(8, min(200, n))
    if any(k in p for k in ("enabled", "autoCollect", "useRate", "maxStickers")):
        write_cfg(cfg)

    mem = BOT.read_memory()
    em = mem.setdefault("emoji", {"small": {}, "sticker": {}})
    st = em.setdefault("sticker", {})
    sm = em.setdefault("small", {})
    if p.get("clearSticker"):
        st.clear()
    if p.get("clearSmall"):
        sm.clear()
    dk = str(p.get("delSticker") or "").strip()
    if dk:
        st.pop(dk, None)
    dsm = str(p.get("delSmall") or "").strip()
    if dsm:
        sm.pop(dsm, None)
    lb = p.get("label")
    if isinstance(lb, dict):
        k = str(lb.get("key") or "").strip()
        if k and isinstance(st.get(k), dict):
            st[k]["label"] = re.sub(r"\s+", " ", str(lb.get("label") or "")).strip()[:20]
    add = str(p.get("addSticker") or "").strip()
    tip, err, keep = "", "", ""
    if add.startswith("http") and len(add) < 500:
        k, isnew = put_sticker(st, add)
        if k:
            keep = k
            tip = "已收藏" if isnew else "这张已经在收藏里了"
        else:
            err = "地址里看不出图片名，换一条图片直链试试"

    loc = p.get("addLocal")
    if isinstance(loc, dict) and loc.get("data"):
        raw, e = decode_b64_image(loc.get("data"))
        if e:
            err = e
        else:
            h = hashlib.md5(raw).hexdigest()
            dup = next((k for k, v in st.items()
                        if isinstance(v, dict) and v.get("md5") == h), "")
            if dup:
                keep, tip = dup, "这张已经在收藏里了"
            else:
                u = upload_sticker_image(raw, str(loc.get("name") or ""))
                if u.get("ok"):
                    keep, isnew = put_sticker(st, u["url"], u.get("w"), u.get("h"),
                                              src="本机图片", md5=u.get("md5"))
                    tip = "已从电脑收藏" if isnew else "这张已经在收藏里了"
                else:
                    err = u.get("err") or "上传失败"

    limit = emoji_cfg()["maxStickers"]
    if keep and len(st) > limit:
        # 超上限：淘汰出现最少、最久没露面的，刚加的那张一定留着
        order = sorted((kv for kv in st.items() if kv[0] != keep),
                       key=lambda kv: ((kv[1] or {}).get("count") or 0,
                                       (kv[1] or {}).get("last") or 0))
        for k, _ in order[: max(0, len(st) - limit)]:
            st.pop(k, None)

    BOT.write_memory(mem)
    out = emoji_payload()
    if tip:
        out["tip"] = tip
    if err:
        out["err"] = err
    return out


# ---------------------------------------------------------------- 人物卡片
# 一个人格一张卡：人设 + 群记忆 + 群友档案 + 表情包收藏整组跟着走。
# memory.json 永远是「当前卡片」的实时数据（机器人读写它），cards.json 是各卡片的存档。
# 读卡片列表时先用 memory 的现状覆盖 active 卡，所以面板上改完不切卡也不会丢。
CARDS_PATH = os.path.join(DATA_DIR, "cards.json")
CARD_KEYS = ("persona", "diary", "users", "emoji", "kb", "agent", "outlook",
             "selfOwned", "aff")


def read_cards():
    try:
        with open(CARDS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    raw = d.get("cards") if isinstance(d.get("cards"), dict) else {}
    cards = {}
    for cid, c in raw.items():
        if not isinstance(c, dict):
            continue
        cards[str(cid)] = {
            "id": str(cid),
            "name": (str(c.get("name") or "").strip() or "未命名")[:30],
            "desc": str(c.get("desc") or "")[:60],
            "persona": str(c.get("persona") or "")[:4000],
            # 总观：它对自己、对这个世界的想法（手写，跟着卡片走）
            "outlook": str(c.get("outlook") or "")[:4000],
            "diary": BOT.merge_diary(c.get("diary") if isinstance(c.get("diary"), list)
                                     else c.get("notes")),
            "users": c.get("users") if isinstance(c.get("users"), dict) else {},
            "emoji": c.get("emoji") if isinstance(c.get("emoji"), dict)
                     else {"small": {}, "sticker": {}},
            "kb": [str(x) for x in (c.get("kb") or [])] if isinstance(c.get("kb"), list) else [],
            "agent": bool(c.get("agent")),
            # 本体模式 + 好感度也必须读出来：少了这两个字段，ensure_cards 每 2 秒
            # 用 memory 的现状覆盖卡片时会把它们刷成默认值（空白卡的 selfOwned 就这么丢的）
            "selfOwned": bool(c.get("selfOwned")),
            "aff": c.get("aff") if isinstance(c.get("aff"), dict) else {},
            "created": float(c.get("created") or 0),
            "updated": float(c.get("updated") or 0),
        }
    return {"active": str(d.get("active") or ""), "cards": cards}


def write_cards(d):
    tmp = _tmp_name(CARDS_PATH)
    try:
        with _WRITE_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, CARDS_PATH)
        return True
    except Exception as e:
        BOT.log("写 cards.json 失败: %s" % e, "WARN")
        try:
            os.remove(tmp)      # 别在 data 里留半截临时文件
        except Exception:
            pass
        return False


def _memory_snapshot(mem):
    return {"persona": str(mem.get("persona") or "")[:4000],
            "outlook": str(mem.get("outlook") or "")[:4000],
            "diary": mem.get("diary") or [],
            "users": mem.get("users") or {},
            "emoji": mem.get("emoji") or {"small": {}, "sticker": {}},
            "kb": [str(x) for x in (mem.get("kb") or [])] if isinstance(mem.get("kb"), list) else [],
            "agent": bool(mem.get("agent")),
            # 本体模式：这张卡的人设不写在 persona 里，由 self.json 提供（她自己写）
            "selfOwned": bool(mem.get("selfOwned")),
            # 好感度跟着卡走：换个「人」，凭什么还记得上一个人攒的交情
            "aff": mem.get("aff") or {}}


def card_name_guess(mem):
    """老用户第一次升级：从人设里挑个称呼当卡片名，挑不到就叫「默认人格」"""
    p = str(mem.get("persona") or "")
    m = re.search(r"[「“\"]([^」”\"\n]{1,12})[」”\"]", p)
    if m:
        return m.group(1)[:12]
    return "默认人格"


def ensure_cards():
    """确保卡片库存在，并把 memory.json 的现状同步进「使用中」那张卡。
    返回 {'active': id, 'cards': {id: 卡片}}"""
    lib = read_cards()
    mem = BOT.read_memory()
    pid = str(mem.get("profile") or "").strip()
    changed = False
    mem_changed = False

    if not lib["cards"]:
        cid = pid or lib["active"] or ("c_%d" % int(time.time()))
        lib["cards"][cid] = dict({"id": cid, "name": card_name_guess(mem),
                                  "desc": "从原来的记忆收回来的",
                                  "created": time.time(), "updated": time.time()},
                                 **_memory_snapshot(mem))
        lib["active"] = cid
        pid, changed = cid, True

    if pid not in lib["cards"]:
        if lib["active"] in lib["cards"]:
            pid = lib["active"]
        else:
            pid = "c_%d" % int(time.time())
            lib["cards"][pid] = dict({"id": pid, "name": card_name_guess(mem), "desc": "",
                                      "created": time.time(), "updated": time.time()},
                                     **_memory_snapshot(mem))
            lib["active"] = pid
            changed = True
        mem_changed = True          # 把 profile 补进 memory.json

    cur = lib["cards"][pid]
    snap = _memory_snapshot(mem)
    for k in CARD_KEYS:
        if cur.get(k) != snap[k]:
            cur[k] = snap[k]
            changed = True
    if lib["active"] != pid:
        lib["active"] = pid
        changed = True
    if str(mem.get("profile") or "") != pid:
        mem["profile"] = pid
        mem_changed = True

    if changed:
        write_cards(lib)
    if mem_changed:
        BOT.write_memory(mem)
    return lib


def _card_stats(c):
    em = c.get("emoji") or {}
    return {"personaChars": len(c.get("persona") or ""),
            "notes": BOT.diary_count(c.get("diary")),
            "users": len(c.get("users") or {}),
            "stickers": len(em.get("sticker") or {}),
            "small": len(em.get("small") or {}),
            "kb": len(c.get("kb") or [])}


def cards_payload():
    lib = ensure_cards()
    out = []
    for cid, c in lib["cards"].items():
        item = {"id": cid, "name": c.get("name") or "未命名",
                "desc": c.get("desc") or "",
                "active": cid == lib["active"],
                "agent": bool(c.get("agent")),
                "created": c.get("created") or 0,
                "updated": c.get("updated") or 0}
        item.update(_card_stats(c))
        out.append(item)
    # 顺序按创建时间固定下来，切卡片时不会互相换位置（否则刚点过的卡片会跳走）
    out.sort(key=lambda x: (x["created"] or 0, x["id"]))
    return {"ok": True, "active": lib["active"], "cards": out, "count": len(out)}


def set_agent(on):
    """智能体模式开关：写进 memory.json（机器人几秒内热更新生效），
    同时记在当前这张人物卡片上——按卡片记忆，切到别的卡片按那张自己的设置来。"""
    mem = BOT.read_memory()
    on = bool(on)
    mem["agent"] = on
    mem["updated"] = time.time()
    if not BOT.write_memory(mem):
        return {"ok": False, "error": "memory.json 写入失败，看看文件是不是被占用"}
    lib = read_cards()
    cid = str(mem.get("profile") or lib.get("active") or "")
    c = lib["cards"].get(cid)
    if c:
        c["agent"] = on
        c["updated"] = time.time()
        lib["active"] = cid or lib.get("active")
        write_cards(lib)
    BOT.log("智能体模式：%s（当前卡片「%s」）"
            % ("打开" if on else "关闭", (c or {}).get("name") or "默认"))
    return {"ok": True, "agent": on, "card": cid}


def switch_card(cid):
    lib = ensure_cards()          # 先把当前卡片的现状存好，再换
    c = lib["cards"].get(cid)
    if not c:
        return {"ok": False, "error": "这张卡片不在了，刷新一下页面"}
    if cid == lib["active"]:
        return {"ok": True, "active": cid, "tip": "已经在用这张卡片了"}
    mem = BOT.read_memory()
    # 切走之前先把「经历」存进旧卡。talkLog 太大，不进 CARD_KEYS 的每 2 秒同步，
    # 但切卡这一刻必须落盘——不然切到空白卡，她过去的对话就永久没了。
    old = lib["cards"].get(lib["active"]) or {}
    if old:
        old["talkLog"] = mem.get("talkLog") or {}
        old["dmPing"] = mem.get("dmPing") or {}
        old["proactive"] = mem.get("proactive") or {}
        old["updated"] = time.time()
    mem["persona"] = c.get("persona") or ""
    mem["outlook"] = c.get("outlook") or ""
    mem["diary"] = c.get("diary") or []
    mem["users"] = c.get("users") or {}
    mem["emoji"] = c.get("emoji") or {"small": {}, "sticker": {}}
    # 知识库：勾了哪些资料是每张卡自己的事，换卡就跟着换（新卡默认一份都没勾）
    mem["kb"] = [str(x) for x in (c.get("kb") or [])]
    # 智能体模式按卡片记忆：切到没开过的卡片默认关，之前开过的切回来还是开
    mem["agent"] = bool(c.get("agent"))
    # 本体模式：这张卡的「我是谁」在 self.json 里，persona 那一栏是空的
    mem["selfOwned"] = bool(c.get("selfOwned"))
    mem["aff"] = c.get("aff") or {}
    # 经历也跟着卡走：空白卡进来是真空的，旧卡切回来原样还在
    mem["talkLog"] = c.get("talkLog") or {}
    mem["dmPing"] = c.get("dmPing") or {}
    mem["proactive"] = c.get("proactive") or {}
    mem["history"] = {}           # 换了人格就当刚认识，别把上一个角色的对话带过来
    mem["profile"] = cid
    if not BOT.write_memory(mem):
        return {"ok": False, "error": "memory.json 写入失败，看看文件是不是被占用"}
    lib["active"] = cid
    lib["cards"][cid]["updated"] = time.time()
    # 独立会话（peer_hist.json）不按卡片分，切卡就清一清：只存最近几轮，丢了不可惜，
    # 留着才是麻烦——新卡的她会读到上一个人的对话，以为自己说过那些话。
    try:
        BOT.peer_write({})
    except Exception:
        pass
    write_cards(lib)
    s = _card_stats(c)
    BOT.log("人物卡片切到「%s」：人设 %d 字 · 日记 %d 篇 · 群友 %d 人 · 表情 %d 张"
            % (c.get("name"), s["personaChars"], s["notes"], s["users"], s["stickers"]))
    return {"ok": True, "active": cid, "name": c.get("name")}


def new_card(p):
    lib = ensure_cards()
    mode = str(p.get("mode") or "blank")
    nm = re.sub(r"\s+", " ", str(p.get("name") or "")).strip()[:30]
    cid = "c_%d" % int(time.time() * 1000)
    while cid in lib["cards"]:
        cid = "c_%d" % (int(time.time() * 1000) + len(lib["cards"]))
    if mode == "copy":
        cur = lib["cards"].get(lib["active"]) or {}
        body = json.loads(json.dumps({k: cur.get(k) for k in CARD_KEYS}, ensure_ascii=False))
        # 新人格的知识库一律从「全部关闭」起步，要用哪几份自己勾
        body["kb"] = []
        nm = nm or ((cur.get("name") or "新人格") + " 副本")[:30]
        desc = str(p.get("desc") or ("复制自「%s」" % (cur.get("name") or "")))[:60]
    else:
        body = {"persona": "", "outlook": "", "diary": [], "users": {},
                "emoji": {"small": {}, "sticker": {}}, "kb": [], "agent": False}
        nm = nm or "新人格"
        desc = str(p.get("desc") or "")[:60]
    lib["cards"][cid] = dict({"id": cid, "name": nm, "desc": desc,
                              "created": time.time(), "updated": time.time()}, **body)
    write_cards(lib)
    if p.get("switch", True):
        r = switch_card(cid)
        r["created"] = cid
        return r
    return {"ok": True, "created": cid, "active": lib["active"]}


def rename_card(cid, name, desc=None):
    lib = ensure_cards()
    c = lib["cards"].get(cid)
    if not c:
        return {"ok": False, "error": "这张卡片不在了"}
    nm = re.sub(r"\s+", " ", str(name or "")).strip()[:30]
    if not nm:
        return {"ok": False, "error": "名字不能为空"}
    c["name"] = nm
    if desc is not None:
        c["desc"] = re.sub(r"\s+", " ", str(desc or "")).strip()[:60]
    c["updated"] = time.time()
    write_cards(lib)
    return {"ok": True, "id": cid, "name": nm, "desc": c["desc"],
            "active": lib["active"]}


def delete_card(cid):
    lib = ensure_cards()
    c = lib["cards"].get(cid)
    if not c:
        return {"ok": False, "error": "这张卡片不在了"}
    if cid == lib["active"]:
        return {"ok": False, "error": "正在用的卡片不能删，先切到别的卡片再删"}
    if len(lib["cards"]) <= 1:
        return {"ok": False, "error": "至少得留一张卡片"}
    lib["cards"].pop(cid, None)
    write_cards(lib)
    BOT.log("人物卡片已删除：「%s」" % c.get("name"))
    return {"ok": True, "active": lib["active"], "deleted": cid}


def card_op(p):
    act = str(p.get("action") or "").strip()
    cid = str(p.get("id") or "").strip()
    if act == "switch":
        return switch_card(cid)
    if act == "new":
        return new_card(p)
    if act == "rename":
        return rename_card(cid, p.get("name"), p.get("desc"))
    if act == "delete":
        return delete_card(cid)
    if act == "sync":
        lib = ensure_cards()
        return {"ok": True, "active": lib["active"]}
    return {"ok": False, "error": "不认识的操作"}


# ---------------------------------------------------------------- 知识库
# 资料文件是全局共享的（data/kb/ 下一份一个文件，台账在 data/kb.json），
# 「勾没勾上」是每张人物卡各记一份（卡片里的 kb 列表 + 当前卡的 memory.json）。
# 所以切到新卡片时默认一份都没勾，切回旧卡片还是上次那样。
KB_DIR = os.path.join(DATA_DIR, "kb")
KB_PATH = os.path.join(DATA_DIR, "kb.json")
KB_EXTS = (".md", ".txt", ".markdown")
KB_MAX_CHARS = 300000        # 单个文件上限（约 300 KB 纯文本）


def read_kb():
    try:
        with open(KB_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    docs = []
    for x in (d.get("docs") if isinstance(d.get("docs"), list) else []):
        if not isinstance(x, dict) or not x.get("file"):
            continue
        docs.append({"id": str(x.get("id") or ""),
                     "name": (str(x.get("name") or "").strip() or "未命名")[:80],
                     "file": str(x.get("file")),
                     "chars": int(x.get("chars") or 0),
                     "size": int(x.get("size") or 0),
                     "added": float(x.get("added") or 0)})
    return {"docs": docs}


def write_kb(d):
    tmp = _tmp_name(KB_PATH)
    try:
        with _WRITE_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, KB_PATH)
        return True
    except Exception as e:
        BOT.log("写 kb.json 失败: %s" % e, "WARN")
        return False


def _kb_peek(name, n=70):
    """列表里显示的开头一小段，让用户认得出这份资料讲什么"""
    try:
        with open(os.path.join(KB_DIR, name), "r", encoding="utf-8", errors="replace") as f:
            head = f.read(400)
    except Exception:
        return ""
    head = re.sub(r"^#+\s*", "", head.strip())
    head = re.sub(r"\s+", " ", head)
    return head[:n]


def _set_active_kb(ids):
    """把「当前卡勾了哪些资料」同时写进 memory.json 和 cards.json。
    两边必须一起改：ensure_cards() 每次都用 memory 覆盖当前卡，只改一边会被顶回去。"""
    lib = ensure_cards()
    cid = lib["active"]
    ids = [str(x) for x in (ids or [])]
    mem = BOT.read_memory()
    mem["kb"] = ids
    if not BOT.write_memory(mem):
        return False
    lib["cards"][cid]["kb"] = ids
    lib["cards"][cid]["updated"] = time.time()
    write_cards(lib)
    return True


def kb_payload():
    lib = ensure_cards()
    cur = lib["cards"].get(lib["active"]) or {}
    on = [str(x) for x in (cur.get("kb") or [])]
    out = []
    for d in read_kb()["docs"]:
        item = dict(d)
        item["enabled"] = d["id"] in on
        item["preview"] = _kb_peek(d["file"])
        out.append(item)
    out.sort(key=lambda x: (x["added"] or 0, x["id"]))
    cfg = read_cfg()
    return {"ok": True,
            "active": lib["active"],
            "cardName": cur.get("name") or "",
            "docs": out,
            "count": len(out),
            "enabled": sum(1 for x in out if x["enabled"]),
            "budget": max(400, int((cfg.get("kb") or {}).get("maxChars") or 4000))}


def kb_add(p):
    name = re.sub(r"\s+", " ", str(p.get("name") or "")).strip()
    if not name:
        return {"ok": False, "error": "没有文件名"}
    ext = os.path.splitext(name)[1].lower()
    if ext not in KB_EXTS:
        return {"ok": False, "error": "只认 .md / .txt 文件"}
    content = str(p.get("content") or "")
    if content.startswith("\ufeff"):
        content = content[1:]
    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not content:
        return {"ok": False, "error": "「%s」是空的，没东西可学" % name}
    if len(content) > KB_MAX_CHARS:
        return {"ok": False, "error": "「%s」太长了（%d 字），切小一点再传" % (name, len(content))}

    reg = read_kb()
    same = [d for d in reg["docs"] if d["name"] == name[:80]]
    cid = "k_%d" % int(time.time() * 1000)
    while any(d["id"] == cid for d in reg["docs"]):
        cid = "k_%d" % (int(time.time() * 1000) + len(reg["docs"]))
    fname = cid + (".md" if ext in (".md", ".markdown") else ".txt")
    try:
        if not os.path.isdir(KB_DIR):
            os.makedirs(KB_DIR)
        path = os.path.join(KB_DIR, fname)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    except Exception as e:
        return {"ok": False, "error": "文件写入失败：%s" % e}

    reg["docs"].append({"id": cid, "name": name[:80], "file": fname,
                        "chars": len(content), "size": os.path.getsize(path),
                        "added": time.time()})
    write_kb(reg)

    # 刚导进来的这份直接对当前卡生效，省得还要再点一次勾
    lib = ensure_cards()
    on = [str(x) for x in (lib["cards"].get(lib["active"]) or {}).get("kb") or []]
    if cid not in on:
        on.append(cid)
        _set_active_kb(on)
    BOT.log("知识库导入「%s」%d 字（%s）" % (name[:40], len(content),
                                        "覆盖同名旧文件" if same else "新增"))
    tip = "「%s」已导入并勾上" % name[:30]
    if same:
        tip += "（同名文件有 %d 份，注意别重复）" % (len(same) + 1)
    out = kb_payload()
    out["tip"] = tip
    return out


def kb_delete(p):
    cid = str(p.get("id") or "").strip()
    reg = read_kb()
    doc = next((d for d in reg["docs"] if d["id"] == cid), None)
    if not doc:
        return {"ok": False, "error": "这份资料不在了，刷新一下页面"}
    reg["docs"] = [d for d in reg["docs"] if d["id"] != cid]
    write_kb(reg)
    try:
        os.remove(os.path.join(KB_DIR, doc["file"]))
    except Exception:
        pass
    # 所有卡片里都把它摘掉，免得留个勾不上的空 id
    lib = read_cards()
    touched = False
    for c in lib["cards"].values():
        if isinstance(c.get("kb"), list) and cid in c["kb"]:
            c["kb"] = [x for x in c["kb"] if x != cid]
            touched = True
    if touched:
        write_cards(lib)
    mem = BOT.read_memory()
    if isinstance(mem.get("kb"), list) and cid in mem["kb"]:
        mem["kb"] = [x for x in mem["kb"] if x != cid]
        BOT.write_memory(mem)
    BOT.log("知识库删除「%s」" % doc["name"])
    out = kb_payload()
    out["tip"] = "已删掉「%s」" % doc["name"][:30]
    return out


def kb_toggle(p):
    cid = str(p.get("id") or "").strip()
    on = bool(p.get("enabled"))
    if not any(d["id"] == cid for d in read_kb()["docs"]):
        return {"ok": False, "error": "这份资料不在了，刷新一下页面"}
    lib = ensure_cards()
    cur = [str(x) for x in (lib["cards"].get(lib["active"]) or {}).get("kb") or []]
    if on and cid not in cur:
        cur.append(cid)
    elif not on:
        cur = [x for x in cur if x != cid]
    else:
        return kb_payload()
    if not _set_active_kb(cur):
        return {"ok": False, "error": "memory.json 写入失败，看看文件是不是被占用"}
    out = kb_payload()
    out["tip"] = "已勾上" if on else "已取消勾选"
    return out


def kb_op(p):
    act = str(p.get("action") or "").strip()
    if act == "add":
        return kb_add(p)
    if act == "delete":
        return kb_delete(p)
    if act == "toggle":
        return kb_toggle(p)
    return {"ok": False, "error": "不认识的操作"}


DEFAULT_BASE_URL = "https://api.deepseek.com/v1/chat/completions"


def settings_payload():
    """设置弹窗要用的当前值。只在本机 127.0.0.1 上提供，不对外。"""
    cfg = read_cfg()
    ai = cfg.get("ai") or {}
    tok = (ai.get("token") or "").strip()
    fc = farewell_cfg()
    return {"ok": True,
            "token": tok,
            "hasToken": bool(tok),
            "baseUrl": ai.get("baseUrl") or DEFAULT_BASE_URL,
            "model": ai.get("model") or "deepseek-chat",
            "maxChars": int((cfg.get("reply") or {}).get("maxChars") or 280),
            "dmMaxChars": int((cfg.get("reply") or {}).get("dmMaxChars") or 800),
            "styleGate": bool((cfg.get("reply") or {}).get("styleGate", True)),
            "visionModel": (ai.get("visionModel") or "").strip(),
            "visionBaseUrl": (ai.get("visionBaseUrl") or "").strip(),
            "visionToken": (ai.get("visionToken") or "").strip(),
            "hasVisionToken": bool((ai.get("visionToken") or "").strip()),
            "webSearch": bool(ai.get("webSearch", False)),
            "searchKey": (ai.get("searchKey") or "").strip(),
            "searchUrl": (ai.get("searchUrl") or "").strip(),
            "hasSearchKey": bool((ai.get("searchKey") or "").strip()),
            # 心情：基线 / 平复半衰期 / 情绪消散半衰期。没配过就给默认值。
            "mood": bool(ai.get("mood", True)),
            "moodBase": int(round(BOT._f(ai.get("moodBase"), 70.0))),
            "moodCalmHours": BOT._f(ai.get("moodCalmHours"), 3.0),
            "moodEmoMinutes": int(round(BOT._f(ai.get("moodEmoMinutes"), 90.0))),
            # 判定节奏：攒够几轮 / 隔几分钟，两个闸都开就都到了才判
            "moodEvery": int(BOT._f(ai.get("moodEvery"), 3)),
            "moodByTurn": bool(ai.get("moodByTurn", True)),
            "moodByTime": bool(ai.get("moodByTime", True)),
            "moodGapMinutes": int(round(BOT._f(ai.get("moodGapMinutes"), 2.0))),
            # 真·思考：换 deepseek-reasoner，先吐思维链再说话。贵一倍上下，默认关。
            "think": bool(ai.get("think", False)),
            # 独立会话 + agent loop（下面那套方案）。默认关，关着完全走老路。
            "agentLoop": bool(ai.get("agentLoop", False)),
            "loopSteps": int(BOT._f(ai.get("loopSteps"), 3)),
            "peerRounds": int(BOT._f(ai.get("peerRounds"), 8)),
            "thinkMode": "always" if str(ai.get("thinkMode") or "").lower() == "always" else "auto",
            "thinkCap": int(BOT._f(ai.get("thinkCap"), 20)),
            "thinkLeft": int(think_left(cfg)),
            # 离线补回：关机/退出这段时间有人找过它，开机后补一句
            "catchUp": bool((cfg.get("catchUp") or {}).get("enabled", True)),
            "catchUpAfter": int(BOT._f((cfg.get("catchUp") or {}).get("afterMinutes"), 5)),
            "catchUpMax": int(BOT._f((cfg.get("catchUp") or {}).get("maxHours"), 12)),
            # 关机收尾：停机器人前留一段时间写日记、把日记收成总观
            "farewell": fc["on"],
            "farewellSeconds": fc["seconds"],
            "farewellDiary": fc["diary"],
            "farewellOutlook": fc["outlook"],
            "farewellGroups": fc["groups"]}


def save_settings(p):
    """改 ai 段和单条回复字数，其余配置原样保留。返回 (body, 是否落盘)。"""
    global _BAL
    cfg = read_cfg()
    ai = cfg.setdefault("ai", {})
    tok = str(p.get("token") or "").strip() if "token" in p else None
    url = str(p.get("baseUrl") or "").strip() if "baseUrl" in p else None
    mdl = str(p.get("model") or "").strip() if "model" in p else None
    vtok = str(p.get("visionToken") or "").strip() if "visionToken" in p else None
    vurl = str(p.get("visionBaseUrl") or "").strip() if "visionBaseUrl" in p else None
    vmdl = str(p.get("visionModel") or "").strip() if "visionModel" in p else None
    skey = str(p.get("searchKey") or "").strip() if "searchKey" in p else None
    surl = str(p.get("searchUrl") or "").strip() if "searchUrl" in p else None
    son = p.get("webSearch") if "webSearch" in p else None
    moon = p.get("mood") if "mood" in p else None

    if tok and not tok.startswith("sk-"):
        return {"ok": False, "error": "API Key 一般是 sk- 开头，检查一下是不是复制全了"}, False
    if url and not (url.startswith("http://") or url.startswith("https://")):
        return {"ok": False, "error": "接口地址要以 http:// 或 https:// 开头"}, False
    if vurl and not (vurl.startswith("http://") or vurl.startswith("https://")):
        return {"ok": False, "error": "识图接口地址要以 http:// 或 https:// 开头"}, False
    if len(tok or "") > 200:
        return {"ok": False, "error": "API Key 太长了，多半是复制串行了"}, False
    if len(vtok or "") > 200:
        return {"ok": False, "error": "识图 Key 太长了，多半是复制串行了"}, False
    if len(skey or "") > 200:
        return {"ok": False, "error": "搜索 Key 太长了，多半是复制串行了"}, False
    if surl and not (surl.startswith("http://") or surl.startswith("https://")):
        return {"ok": False, "error": "搜索接口地址要以 http:// 或 https:// 开头"}, False

    if tok is not None:
        ai["token"] = tok
    if url is not None:
        ai["baseUrl"] = url or DEFAULT_BASE_URL
    if mdl:
        ai["model"] = mdl[:60]
    # 识图模型：留空 = 复用主配置。三个字段可以只填其中一两个，其余回退。
    if vtok is not None:
        ai["visionToken"] = vtok
    if vurl is not None:
        ai["visionBaseUrl"] = vurl
    if vmdl is not None:
        ai["visionModel"] = vmdl[:60]
    # 联网搜索：留空 Key = 不启用（开关开着也没用，代码里会拦）。
    if skey is not None:
        ai["searchKey"] = skey
    if surl is not None:
        ai["searchUrl"] = surl
    if son is not None:
        ai["webSearch"] = bool(son)

    # 心情：三个数都是「越小越容易低落 / 回得越快」，范围收窄一点，
    # 免得填个 0 让它一秒钟从狂喜回到平静，或者填个 999 永远缓不过来
    if moon is not None:
        ai["mood"] = bool(moon)
    mbase = p.get("moodBase") if "moodBase" in p else None
    if mbase not in (None, ""):
        try:
            mbase = int(round(float(mbase)))
        except Exception:
            return {"ok": False, "error": "心情基线要填数字"}, False
        if not 0 <= mbase <= 100:
            return {"ok": False, "error": "心情基线填 0-100 之间"}, False
        ai["moodBase"] = mbase
    mcalm = p.get("moodCalmHours") if "moodCalmHours" in p else None
    if mcalm not in (None, ""):
        try:
            mcalm = float(mcalm)
        except Exception:
            return {"ok": False, "error": "平复速度要填数字"}, False
        if not 0.1 <= mcalm <= 72:
            return {"ok": False, "error": "平复速度填 0.1-72 小时之间"}, False
        ai["moodCalmHours"] = round(mcalm, 2)
    memo = p.get("moodEmoMinutes") if "moodEmoMinutes" in p else None
    if memo not in (None, ""):
        try:
            memo = int(round(float(memo)))
        except Exception:
            return {"ok": False, "error": "情绪消散要填数字"}, False
        if not 1 <= memo <= 1440:
            return {"ok": False, "error": "情绪消散填 1-1440 分钟之间"}, False
        ai["moodEmoMinutes"] = memo

    # 判定节奏：两个闸（轮数 / 分钟）。都开 = 都到了才判；只开一个就只看那一个。
    mev = p.get("moodEvery") if "moodEvery" in p else None
    if mev not in (None, ""):
        try:
            mev = int(round(float(mev)))
        except Exception:
            return {"ok": False, "error": "每几轮判一次要填整数"}, False
        if not 1 <= mev <= 50:
            return {"ok": False, "error": "每几轮判一次填 1-50 之间"}, False
        ai["moodEvery"] = mev
    mbtr = p.get("moodByTurn") if "moodByTurn" in p else None
    if mbtr is not None:
        ai["moodByTurn"] = bool(mbtr)
    mbtm = p.get("moodByTime") if "moodByTime" in p else None
    if mbtm is not None:
        ai["moodByTime"] = bool(mbtm)
    mgap = p.get("moodGapMinutes") if "moodGapMinutes" in p else None
    if mgap not in (None, ""):
        try:
            mgap = int(round(float(mgap)))
        except Exception:
            return {"ok": False, "error": "间隔分钟要填整数"}, False
        if not 0 <= mgap <= 240:
            return {"ok": False, "error": "间隔分钟填 0-240 之间"}, False
        ai["moodGapMinutes"] = mgap

    # 独立会话 + agent loop：开关 / 最多查几步 / 每人记几轮
    lp_on = p.get("agentLoop") if "agentLoop" in p else None
    if lp_on is not None:
        ai["agentLoop"] = bool(lp_on)
    lp_step = p.get("loopSteps") if "loopSteps" in p else None
    if lp_step not in (None, ""):
        try:
            lp_step = int(round(float(lp_step)))
        except Exception:
            return {"ok": False, "error": "最多查几步要填数字"}, False
        if not 1 <= lp_step <= 4:
            return {"ok": False, "error": "最多查几步填 1-4 之间"}, False
        ai["loopSteps"] = lp_step
    pr = p.get("peerRounds") if "peerRounds" in p else None
    if pr not in (None, ""):
        try:
            pr = int(round(float(pr)))
        except Exception:
            return {"ok": False, "error": "每人记几轮要填数字"}, False
        if not 2 <= pr <= 20:
            return {"ok": False, "error": "每人记几轮填 2-20 之间"}, False
        ai["peerRounds"] = pr

    # 真·思考：开关 / 模式 / 每小时上限。上限是硬闸——开着也不会连着几小时一直烧
    th_on = p.get("think") if "think" in p else None
    if th_on is not None:
        ai["think"] = bool(th_on)
    th_mode = p.get("thinkMode") if "thinkMode" in p else None
    if th_mode not in (None, ""):
        ai["thinkMode"] = "always" if str(th_mode).lower() == "always" else "auto"
    th_cap = p.get("thinkCap") if "thinkCap" in p else None
    if th_cap not in (None, ""):
        try:
            th_cap = int(round(float(th_cap)))
        except Exception:
            return {"ok": False, "error": "思考上限要填数字"}, False
        if not 0 <= th_cap <= 999:
            return {"ok": False, "error": "思考上限填 0-999 之间（0 = 不限）"}, False
        ai["thinkCap"] = th_cap

    # 离线补回：关机/退出这段时间有人找过它，开机后补一句
    cu_on = p.get("catchUp") if "catchUp" in p else None
    if cu_on is not None:
        cfg.setdefault("catchUp", {})["enabled"] = bool(cu_on)
    cua = p.get("catchUpAfter") if "catchUpAfter" in p else None
    if cua not in (None, ""):
        try:
            cua = int(round(float(cua)))
        except Exception:
            return {"ok": False, "error": "补回门槛要填数字"}, False
        if not 1 <= cua <= 720:
            return {"ok": False, "error": "补回门槛填 1-720 分钟之间"}, False
        cfg.setdefault("catchUp", {})["afterMinutes"] = cua
    cum = p.get("catchUpMax") if "catchUpMax" in p else None
    if cum not in (None, ""):
        try:
            cum = int(round(float(cum)))
        except Exception:
            return {"ok": False, "error": "补回上限要填数字"}, False
        if not 1 <= cum <= 168:
            return {"ok": False, "error": "补回上限填 1-168 小时之间"}, False
        cfg.setdefault("catchUp", {})["maxHours"] = cum

    # 单条回复字数：这是天花板，不是目标长度——人设自己会管长短。
    # 给太紧（以前的 110）会把每句都压成结论，看着像客服。
    mc = p.get("maxChars") if "maxChars" in p else None
    if mc not in (None, ""):
        try:
            mc = int(mc)
        except Exception:
            return {"ok": False, "error": "单条字数要填数字"}, False
        if not 60 <= mc <= 600:
            return {"ok": False, "error": "群聊上限填 60-600 之间"}, False
        cfg.setdefault("reply", {})["maxChars"] = mc

    # 私聊上限：私聊不刷屏，可以比群里宽得多
    dmc = p.get("dmMaxChars") if "dmMaxChars" in p else None
    if dmc not in (None, ""):
        try:
            dmc = int(dmc)
        except Exception:
            return {"ok": False, "error": "私聊字数要填数字"}, False
        if not 60 <= dmc <= 2000:
            return {"ok": False, "error": "私聊上限填 60-2000 之间"}, False
        cfg.setdefault("reply", {})["dmMaxChars"] = dmc

    # 语感闸门：开口前跟她最近说过的原话比一遍，开头撞了 / 整句太像就打回重说一次。
    # 默认开——它不花额外调用，只在真要重复时才多一次。
    sg = p.get("styleGate") if "styleGate" in p else None
    if sg is not None:
        cfg.setdefault("reply", {})["styleGate"] = bool(sg)

    # 关机收尾：停机器人前留多少秒写日记 / 总观，写哪几个
    fw = cfg.setdefault("farewell", {})
    fw_on = p.get("farewell") if "farewell" in p else None
    if fw_on is not None:
        fw["enabled"] = bool(fw_on)
    fw_sec = p.get("farewellSeconds") if "farewellSeconds" in p else None
    if fw_sec not in (None, ""):
        try:
            fw_sec = int(round(float(fw_sec)))
        except Exception:
            return {"ok": False, "error": "收尾时间要填数字"}, False
        if not 15 <= fw_sec <= 600:
            return {"ok": False, "error": "收尾时间填 15-600 秒之间"}, False
        fw["seconds"] = fw_sec
    for pkey, ckey in (("farewellDiary", "diary"), ("farewellOutlook", "outlook")):
        v = p.get(pkey) if pkey in p else None
        if v is not None:
            fw[ckey] = bool(v)
    fw_g = p.get("farewellGroups") if "farewellGroups" in p else None
    if fw_g not in (None, ""):
        try:
            fw_g = int(round(float(fw_g)))
        except Exception:
            return {"ok": False, "error": "收尾会话数要填数字"}, False
        if not 0 <= fw_g <= 10:
            return {"ok": False, "error": "收尾会话数填 0-10 之间"}, False
        fw["maxGroups"] = fw_g

    write_cfg(cfg)

    # 换了 Key，余额缓存作废，下次刷新重新取
    with _BAL_LOCK:
        _BAL = {"ts": 0.0, "data": None}

    return {"ok": True, "hasToken": bool((ai.get("token") or "").strip()),
            "baseUrl": ai.get("baseUrl"), "model": ai.get("model"),
            "maxChars": int((cfg.get("reply") or {}).get("maxChars") or 280),
            "dmMaxChars": int((cfg.get("reply") or {}).get("dmMaxChars") or 800),
            "visionModel": ai.get("visionModel") or "",
            "visionBaseUrl": ai.get("visionBaseUrl") or "",
            "visionToken": ai.get("visionToken") or "",
            "webSearch": bool(ai.get("webSearch", False)),
            "searchKey": ai.get("searchKey") or "",
            "searchUrl": ai.get("searchUrl") or "",
            "mood": bool(ai.get("mood", True)),
            "moodBase": int(round(BOT._f(ai.get("moodBase"), 70.0))),
            "moodCalmHours": BOT._f(ai.get("moodCalmHours"), 3.0),
            "moodEmoMinutes": int(round(BOT._f(ai.get("moodEmoMinutes"), 90.0))),
            "moodEvery": int(BOT._f(ai.get("moodEvery"), 3)),
            "moodByTurn": bool(ai.get("moodByTurn", True)),
            "moodByTime": bool(ai.get("moodByTime", True)),
            "moodGapMinutes": int(round(BOT._f(ai.get("moodGapMinutes"), 2.0))),
            "think": bool(ai.get("think", False)),
            "thinkMode": "always" if str(ai.get("thinkMode") or "").lower() == "always" else "auto",
            "thinkCap": int(BOT._f(ai.get("thinkCap"), 20)),
            "thinkLeft": int(think_left(cfg)),
            "catchUp": bool((cfg.get("catchUp") or {}).get("enabled", True)),
            "catchUpAfter": int(BOT._f((cfg.get("catchUp") or {}).get("afterMinutes"), 5)),
            "catchUpMax": int(BOT._f((cfg.get("catchUp") or {}).get("maxHours"), 12)),
            "farewell": bool(fw.get("enabled", True)),
            "farewellSeconds": int(BOT._f(fw.get("seconds"), 90)),
            "farewellDiary": bool(fw.get("diary", True)),
            "farewellOutlook": bool(fw.get("outlook", True)),
            "farewellGroups": int(BOT._f(fw.get("maxGroups"), 3))}, True


# --------------------------------------------------------------------------
# DeepSeek 余额：60 秒缓存，点 ↻ 可强制刷新
# --------------------------------------------------------------------------
BAL_TTL = 60
_BAL = {"ts": 0.0, "data": None}
_BAL_LOCK = threading.Lock()
BAL_API = "https://api.deepseek.com/user/balance"


def api_balance(force=False):
    global _BAL
    now = time.time()
    with _BAL_LOCK:
        if (not force and _BAL["data"] is not None
                and now - _BAL["ts"] < BAL_TTL):
            out = dict(_BAL["data"])
            out["age"] = round(now - _BAL["ts"], 1)
            out["cached"] = True
            return out

        key = ((read_cfg().get("ai") or {}).get("token") or "").strip()
        if not key:
            d = {"ok": False, "error": "config.json 里没有 ai.token"}
        else:
            try:
                req = urllib.request.Request(BAL_API, headers={
                    "Authorization": "Bearer " + key,
                    "Accept": "application/json",
                })
                with BOT.NO_PROXY.open(req, timeout=8) as r:
                    raw = json.loads(r.read().decode("utf-8"))
                infos = raw.get("balance_infos") or []
                info = infos[0] if infos else {}
                d = {
                    "ok": True,
                    "available": bool(raw.get("is_available")),
                    "currency": info.get("currency") or "CNY",
                    "total": float(info.get("total_balance") or 0),
                    "granted": float(info.get("granted_balance") or 0),
                    "topped": float(info.get("topped_up_balance") or 0),
                }
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:120]
                d = {"ok": False, "error": "HTTP %s %s" % (e.code, detail)}
            except Exception as e:
                d = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        d["ts"] = now
        _BAL = {"ts": now, "data": d}
        try:
            bal_note(d)          # 真查到的才落账，缓存不算
        except Exception:
            pass
        out = dict(d)
        out["age"] = 0
        out["cached"] = False
        return out


# --------------------------------------------------------------------------
# 消耗曲线：余额是硬账（到底花了多少），usage.jsonl 是拆分（钱花在哪个功能上）
# --------------------------------------------------------------------------
BAL_HIST = os.path.join(DATA_DIR, "balance.jsonl")
BAL_MAX_BYTES = 512 * 1024
BAL_KEEP = 6000
_BAL_NOTE_LOCK = threading.Lock()
_BAL_LAST = {"t": 0.0, "v": None}


def bal_read(since=0):
    """按时间升序读余额采样点。坏行跳过，缺文件就当没有。"""
    out = []
    try:
        with open(BAL_HIST, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    t = float(r.get("t") or 0)
                except Exception:
                    continue
                if t < since:
                    continue
                out.append({"t": t, "v": float(r.get("v") or 0)})
    except OSError:
        return []
    return out


def bal_note(d):
    """每次真问到余额就落一笔，攒成曲线。
    余额变了立刻记（变的那一下正是钱花掉的地方）；没变就 10 分钟补一个点，
    把时间轴拉直，免得曲线断成一截一截的。"""
    if not d or not d.get("ok"):
        return
    try:
        v = round(float(d.get("total") or 0), 4)
    except Exception:
        return
    now = time.time()
    with _BAL_NOTE_LOCK:
        lt = float(_BAL_LAST.get("t") or 0)
        lv = _BAL_LAST.get("v")
        if lt:
            if now - lt < 15:                       # 抖得太密没意义
                return
            if lv is not None and abs(v - float(lv)) < 0.004 and now - lt < 600:
                return
        rec = {"t": round(now, 1), "v": v}
        _BAL_LAST["t"], _BAL_LAST["v"] = now, v
        try:
            with open(BAL_HIST, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            return
        try:
            if os.path.getsize(BAL_HIST) > BAL_MAX_BYTES:
                with open(BAL_HIST, "r", encoding="utf-8") as f:
                    keep = f.readlines()[-BAL_KEEP:]
                with open(BAL_HIST, "w", encoding="utf-8") as f:
                    f.writelines(keep)
        except OSError:
            pass


def _spread_cost(series, bucket, t0, t1, cost, since):
    """把一段时间的花费按「每桶覆盖了多久」摊进柱子里。
    采样间隔可能很长（面板关着那几小时），全塞进最后一根柱子会假出一个尖峰。
    返回真正落在窗口内的金额——只算窗口内，窗口外那段不该记到这份账上。"""
    a, b = max(t0, since), max(t1, since)
    span = b - a
    if span <= 0 or cost <= 0:
        return 0.0
    base = series[0]["t"]
    i0 = max(0, int((a - base) // bucket))
    i1 = min(len(series) - 1, int((b - base) // bucket))
    used = 0.0
    for i in range(i0, i1 + 1):
        s = base + i * bucket
        e = s + bucket
        ov = min(b, e) - max(a, s)
        if ov > 0:
            series[i]["cost"] += cost * ov / span
            used += cost * ov / span
    return used


def price_cfg():
    """元 / 百万 token。填在 config 的 ai.price 里，改了就按新的算。"""
    pr = ((read_cfg().get("ai") or {}).get("price") or {})

    def f(k, d):
        try:
            return float(pr.get(k, d))
        except Exception:
            return d
    return {"in": f("in", 2.0), "out": f("out", 3.0), "hit": f("hit", 0.2)}


def _rec_cost(r, pr):
    """一条调用按价目表折算成钱（元）。只是估——真账看余额。"""
    pt = int(r.get("p") or 0)
    ct = int(r.get("c") or 0)
    hit = min(int(r.get("hit") or 0), pt)
    return ((pt - hit) * pr["in"] + hit * pr["hit"] + ct * pr["out"]) / 1e6


def spend_payload(hours=24):
    """面板点余额卡片看的那一页：这段时间花了多少、什么时候花的、花在哪。"""
    try:
        hours = int(hours or 24)
    except Exception:
        hours = 24
    hours = max(1, min(24 * 60, hours))
    now = time.time()
    since = now - hours * 3600

    us = BOT.usage_read(since)
    pr = price_cfg()

    # 一格里放多少时间：24 小时看每小时，一个月看每天
    if hours <= 26:
        bucket = 3600
    elif hours <= 74:
        bucket = 3 * 3600
    elif hours <= 24 * 8:
        bucket = 6 * 3600
    else:
        bucket = 24 * 3600
    base = int(since // bucket) * bucket
    n = int((now - base) // bucket) + 1
    series = [{"t": base + i * bucket, "cost": 0.0} for i in range(n)]

    # 余额：真账。相邻两点掉的钱就是这段时间的花费，涨的那一下是充值
    pts = bal_read(since - 6 * 3600)
    topups, real = [], 0.0
    for a, b in zip(pts, pts[1:]):
        dv = round(float(a["v"]) - float(b["v"]), 6)
        if dv >= 0.005:
            real += _spread_cost(series, bucket, a["t"], b["t"], dv, since)
        elif dv <= -0.005:
            topups.append({"t": b["t"], "amt": round(-dv, 2)})
    if len(pts) < 2:
        real = 0.0

    # 分功能 / 分模型：token 加权（输出比输入贵几倍，不能只看条数）
    # 每个功能再往下拆两层：钱是被「输入 / 缓存命中 / 输出」哪一段吃掉的，
    # 以及是为哪个群、哪个私聊花的（老记录没有 k，只统计有对象名的）。
    scenes, models = {}, {}
    tot_in = tot_out = tot_tt = tot_hit = 0
    for r in us:
        s = str(r.get("s") or "聊天")
        pt = int(r.get("p") or 0)
        ct = int(r.get("c") or 0)
        tt = int(r.get("tt") or 0) or (pt + ct)
        hit = min(int(r.get("hit") or 0), pt)
        # 权重直接用价目表折出来的钱：这样百分比就是「钱占比」，
        # 而不是拿「输出算 4 倍」这类拍的系数糊上去（输出实际只比输入贵 1.5 倍）。
        ei = (pt - hit) * pr["in"] / 1e6
        eh = hit * pr["hit"] / 1e6
        eo = ct * pr["out"] / 1e6
        w = ei + eh + eo
        if w <= 0:
            w = 1e-9
        d = scenes.setdefault(s, {"s": s, "calls": 0, "tt": 0, "p": 0, "c": 0,
                                  "hit": 0, "w": 0.0, "est": 0.0,
                                  "ei": 0.0, "eh": 0.0, "eo": 0.0, "sub": {}})
        d["calls"] += 1
        d["tt"] += tt
        d["p"] += pt
        d["c"] += ct
        d["hit"] += hit
        d["w"] += w
        d["est"] += w
        # 三段各自按价目表值多少钱，最后按比例把真账（余额）分下去
        d["ei"] += ei
        d["eh"] += eh
        d["eo"] += eo
        k = str(r.get("k") or "").strip()
        if k:
            sb = d["sub"].setdefault(k, {"k": k, "calls": 0, "tt": 0,
                                         "p": 0, "c": 0, "hit": 0, "w": 0.0})
            sb["calls"] += 1
            sb["tt"] += tt
            sb["p"] += pt
            sb["c"] += ct
            sb["hit"] += hit
            sb["w"] += w
        m = str(r.get("m") or "未知")
        md = models.setdefault(m, {"m": m, "calls": 0, "tt": 0, "w": 0.0})
        md["calls"] += 1
        md["tt"] += tt
        md["w"] += w
        tot_in += pt
        tot_out += ct
        tot_tt += tt
        tot_hit += hit

    weight = sum(x["w"] for x in scenes.values())
    est_total = sum(x["est"] for x in scenes.values())
    basis = "balance" if real > 0.004 else "usage"
    total = real if basis == "balance" else est_total
    for x in scenes.values():
        if basis == "balance" and weight > 0:
            x["cost"] = round(real * x["w"] / weight, 4)
        else:
            x["cost"] = round(x["est"], 4)
        x["pct"] = round(100.0 * x["w"] / weight, 1) if weight > 0 else 0.0
        # 三段的钱：按价目表比例把这一行的金额切开，合计仍是 x["cost"]
        es = x["ei"] + x["eh"] + x["eo"]
        if es > 0:
            x["parts"] = {"in": round(x["cost"] * x["ei"] / es, 5),
                          "hit": round(x["cost"] * x["eh"] / es, 5),
                          "out": round(x["cost"] * x["eo"] / es, 5)}
        else:
            x["parts"] = {"in": 0.0, "hit": 0.0, "out": 0.0}
        # 按对象拆：每个群 / 私聊各花了多少
        subs = sorted(x.pop("sub").values(), key=lambda y: -y["w"])
        for y in subs:
            y["cost"] = round(x["cost"] * y["w"] / x["w"], 5) if x["w"] > 0 else 0.0
            y["pct"] = round(100.0 * y["w"] / x["w"], 1) if x["w"] > 0 else 0.0
            y.pop("w", None)
        x["subs"] = subs[:8]
        x["subs_more"] = max(0, len(subs) - 8)
        x["subs_calls"] = sum(y["calls"] for y in subs)
        # 这几个是「对谁说话」，能按群/私聊拆；日记、总观那类本身就不分对象
        x["talk"] = x["s"] in ("聊天", "真思考", "冷场冒泡", "主动找群", "主动找人")
        x.pop("est", None)
        x.pop("ei", None)
        x.pop("eh", None)
        x.pop("eo", None)
    for x in models.values():
        x["pct"] = round(100.0 * x["w"] / weight, 1) if weight > 0 else 0.0
    scene_list = sorted(scenes.values(), key=lambda x: -x["w"])
    model_list = sorted(models.values(), key=lambda x: -x["w"])
    if basis == "usage" and us:
        # 还没攒够余额采样（刚用、面板一直没开），就用估的金额把柱子撑起来，
        # 至少能看出哪个时段在烧钱，比空白强。
        for x in series:
            x["cost"] = 0.0
        for r in us:
            i = int((float(r.get("t") or 0) - base) // bucket)
            if 0 <= i < len(series):
                series[i]["cost"] += _rec_cost(r, pr)
    for x in series:
        x["cost"] = round(x["cost"], 4)
    peak = max([x["cost"] for x in series] or [0])
    spent_series = round(sum(x["cost"] for x in series), 4)

    return {
        "ok": True, "hours": hours, "bucket": bucket, "since": since, "now": now,
        "basis": basis, "spent": round(total, 4),
        "real": round(real, 4), "est": round(est_total, 4),
        "series": series, "peak": peak, "in_series": spent_series,
        "balance": pts[-300:], "balance_now": (pts[-1]["v"] if pts else None),
        "topups": topups[-8:],
        "scenes": scene_list, "models": model_list,
        "totals": {"calls": len(us), "tt": tot_tt, "in": tot_in, "out": tot_out,
                   "hit": tot_hit},
        "price": pr,
        "recent": [{"t": r.get("t"), "s": r.get("s") or "聊天",
                    "k": r.get("k") or "",
                    "m": r.get("m") or "", "tt": int(r.get("tt") or 0),
                    "ms": int(r.get("ms") or 0)} for r in us[-14:]][::-1],
    }


def mem_payload():
    """给网页的人设 / 记忆数据"""
    cfg = read_cfg()
    mem = BOT.read_memory()
    persona = (mem.get("persona") or "").strip()
    src = "memory"
    if not persona:
        persona = (cfg.get("ai") or {}).get("prompt") or BOT.DEFAULT_PERSONA
        src = "config"
    lib = read_cards()
    pid = str(mem.get("profile") or lib.get("active") or "")
    cur_card = (lib.get("cards") or {}).get(pid) or {}
    return {
        "ok": True,
        "persona": persona,
        "personaSource": src,
        # 总观：它对自己和世界的想法。面板单独一张卡编辑，注入提示词时排在 persona 之后
        "outlook": str(mem.get("outlook") or ""),
        "profile": pid,
        "cardName": cur_card.get("name") or "默认人格",
        "learn": bool(mem.get("learn", True)),
        "enabled": bool(mem.get("enabled", True)),
        # 日记是「这个人」的一条时间线，不按会话分——面板上也只有一份。
        # 编辑框里是渲染好的文本：日期行独占一行，篇与篇之间空一行
        "diary": BOT.diary_text(mem.get("diary")),
        "diaryCount": BOT.diary_count(mem.get("diary")),
        "users": mem.get("users") or {},
        # 整理档案时是否读日记、按人设口吻改写旧条目（面板「群友档案」里的开关）
        "usersRewrite": bool(mem.get("usersRewrite", True)),
        "maxUsers": BOT.MAX_USERS_PER_GROUP,
        "maxNotesPerUser": BOT.MAX_NOTES_PER_USER,
        "learnedAt": mem.get("learnedAt") or 0,
        "learnEvery": int((cfg.get("ai") or {}).get("learnEvery") or 6),
        "path": "memory.json",
    }


TYPE_LABEL = {3: "[图片]", 2: "[图片]", 6: "[视频]", 8: "[语音]", 27: "[卡片]", 7: "[大表情]"}


def display_text(m):
    """页面上展示的文本：去掉 @ / 频道标记，但保留 [cube_xxx] 这类表情"""
    raw = (m.get("msg") or "").strip()
    if raw:
        t = BOT.AT_RE.sub(" ", raw)
        t = BOT.CH_RE.sub(" ", t)
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            return t
    return TYPE_LABEL.get(m.get("type"), "[非文本消息 type=%s]" % m.get("type"))


def tail_log():
    try:
        files = [f for f in os.listdir(LOG_DIR) if f.endswith(".log")]
        if not files:
            return []
        newest = max(files, key=lambda f: os.path.getmtime(os.path.join(LOG_DIR, f)))
        path = os.path.join(LOG_DIR, newest)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-MAX_LOG_LINES:]
    except Exception:
        return []
    out = []
    for ln in lines:
        ln = ln.rstrip("\n")
        m = LOG_RE.match(ln)
        if m:
            out.append({"t": m.group(1), "level": m.group(2), "msg": m.group(3)})
        elif ln.strip():
            out.append({"t": "", "level": "INFO", "msg": ln.strip()})
    return out


class Collector:
    def __init__(self):
        self.lock = threading.Lock()
        self.cdp = None
        self.cli = None
        self.cache = None
        self.cache_ts = 0.0
        self.last_error = ""

    def _attach(self):
        if self.cli is None:
            self.cdp = BOT.Cdp(read_cfg().get("cdp", {}).get("port", 9222))
            self.cdp.connect()
            self.cli = BOT.Client(self.cdp)
        self.cli.ensure(retries=3)
        return self.cli

    def _reset(self):
        try:
            if self.cdp:
                self.cdp.close()
        except Exception:
            pass
        self.cli = None
        self.cdp = None

    def snapshot(self):
        with self.lock:
            if self.cache and (time.time() - self.cache_ts) < CACHE_SECONDS:
                return self.cache
            data = self._collect()
            self.cache = data
            self.cache_ts = time.time()
            return data

    def _collect(self):
        cfg = read_cfg()
        now = time.time()
        d = {
            "ok": True,
            "ts": now,
            "server": {
                "uptime": int(now - SERVER_START),
                "port": BOUND_PORT,
            },
            "config": {
                "trigger": (cfg.get("trigger") or {}).get("mode", ""),
                "prefix": (cfg.get("trigger") or {}).get("prefix", ""),
                "keywords": (cfg.get("trigger") or {}).get("keywords") or [],
                "directReply": bool((cfg.get("reply") or {}).get("directReply", False)),
                "directRate": max(0, min(100, int((cfg.get("reply") or {}).get("directReplyRate", 30) or 0))),
                "model": (cfg.get("ai") or {}).get("model", ""),
                "maxChars": (cfg.get("reply") or {}).get("maxChars", 0),
                "poll": (cfg.get("poll") or {}).get("intervalSeconds", 0),
                "groupsMode": (cfg.get("groups") or {}).get("mode", ""),
            },
            "messages": [],
            "groups": [],
            "ui": {"maxMessages": msg_cap(cfg), "caps": list(MSG_CAPS)},
            "log": tail_log(),
        }

        # 机器人进程状态（心跳 + PID 存活双重判断）
        d["bot"] = bot_status()

        # 好感度：它心里给每个人记的分（读 memory.json，不依赖客户端连接）
        try:
            d["affinity"] = affinity_list()
        except Exception as e:
            d["affinity"] = []
            d["affError"] = "%s: %s" % (type(e).__name__, e)

        # 思考流：它每回一句，内部实际走的几步（读 think.jsonl）
        try:
            d["think"] = think_list(24)
        except Exception as e:
            d["think"] = []
            d["thinkError"] = "%s: %s" % (type(e).__name__, e)

        # 真思考开没开、这一小时还剩几次（给上面那行小字用）
        _ai = cfg.get("ai") or {}
        d["thinkOn"] = bool(_ai.get("think", False))
        d["thinkModel"] = (_ai.get("thinkModel") or "deepseek-reasoner")
        d["thinkLeft"] = int(think_left(cfg))

        # API 余额（60 秒缓存，失败也不影响其它数据）
        try:
            d["balance"] = api_balance()
        except Exception as e:
            d["balance"] = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}

        try:
            cli = self._attach()
            selfid = cli.self_id
            groups = cli.groups()
            mode = ((cfg.get("groups") or {}).get("mode") or "all").lower()
            if mode == "list":
                want = set(str(x) for x in ((cfg.get("groups") or {}).get("ids") or []))
                groups = [g for g in groups if g["gid"] in want]

            d["client"] = {"connected": True, "selfId": selfid, "selfName": cli.self_name}
            d["groups"] = [{"gid": g["gid"], "name": g["name"], "last": g["last"], "kind": "group"}
                           for g in groups]

            # 好友私聊会话
            dms = []
            dm_cfg = cfg.get("dms") or {}
            if dm_cfg.get("enabled", True):
                try:
                    dms = cli.dms()
                except Exception:
                    dms = []
                want = set(str(x) for x in (dm_cfg.get("ids") or []))
                if want:
                    dms = [x for x in dms if x["uid"] in want]
            d["dms"] = [{"gid": x["uid"], "uid": x["uid"], "name": x["name"],
                         "last": x.get("last", ""), "kind": "dm"} for x in dms]

            msgs = []
            cap = d["ui"]["maxMessages"]
            # 每个会话抓多少：上限调大后光抓 25 条根本凑不满，按会话数分摊，
            # 但别一个会话抓几百条——客户端那边会慢，60 条已经很够填补空档了
            nconv = max(1, min(6, len(groups)) + min(6, len(d["dms"])))
            per = max(25, min(60, cap // nconv + 10))
            for g in groups[:6]:
                r = cli.messages(g["gid"], per)
                if not r.get("ok"):
                    continue
                for m in r.get("list", []):
                    m["gid"] = g["gid"]
                    m["gname"] = g["name"]
                    m["kind"] = "group"
                    m["isBot"] = (m["uid"] == selfid)
                    m["mention"] = ("@{id:%d}" % selfid) in (m["msg"] or "")
                    m["text"] = display_text(m)
                    msgs.append(m)
            for x in d["dms"][:6]:
                r = cli.dm_messages(x["uid"], per, x["name"])
                if not r.get("ok"):
                    continue
                for m in r.get("list", []):
                    m["gid"] = x["uid"]
                    m["gname"] = "私聊 · " + x["name"]
                    m["kind"] = "dm"
                    m["isBot"] = (m["uid"] == selfid)
                    m["mention"] = False
                    m["text"] = display_text(m)
                    msgs.append(m)
            msgs.sort(key=lambda m: m["time"], reverse=True)
            d["messages"] = msgs[:cap]
            d["stats"] = {
                "msgTotal": len(msgs),
                "botMsgs": sum(1 for m in msgs if m["isBot"]),
                "mentions": sum(1 for m in msgs if m["mention"] and not m["isBot"]),
            }
            self.last_error = ""
        except Exception as e:
            self.last_error = "%s: %s" % (type(e).__name__, e)
            d["client"] = {"connected": False, "error": self.last_error}
            d["stats"] = {"msgTotal": 0, "botMsgs": 0, "mentions": 0}
            self._reset()
        return d


SERVER_START = time.time()
COLLECTOR = Collector()


class QuietServer(http.server.ThreadingHTTPServer):
    """浏览器刷新/关页面时连接会被硬断，标准库会往 stderr 打一屏 traceback。
    这只说明对面走了，不是故障——静音掉，真出事才打。"""
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except Exception:
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, page_html(PAGE), "text/html; charset=utf-8")
        elif path in ("/live", "/live.html"):
            self._send(200, page_html(LIVE_PAGE), "text/html; charset=utf-8")
        elif path == "/api/state":
            try:
                body = json.dumps(COLLECTOR.snapshot(), ensure_ascii=False)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/bot/status":
            try:
                body = json.dumps(bot_status(), ensure_ascii=False)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/quit/state":
            # 点了「退出」以后页面每秒问一次：还剩几秒、收尾写到哪、能不能关了
            try:
                body = json.dumps(quit_state(), ensure_ascii=False)
            except Exception as e:
                body = json.dumps({"active": True, "done": False, "left": 0,
                                   "error": str(e)}, ensure_ascii=False)
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/setup/state":
            try:
                body = json.dumps(setup_state(), ensure_ascii=False)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/channel":
            try:
                body = json.dumps(channel_cfg(), ensure_ascii=False)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/chat":
            try:
                body = json.dumps(chat_state(), ensure_ascii=False)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/group-reply":
            try:
                body = json.dumps(group_reply_cfg(), ensure_ascii=False)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/bubble":
            try:
                body = json.dumps(bubble_cfg(), ensure_ascii=False)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/ping":
            try:
                body = json.dumps(ping_cfg(), ensure_ascii=False)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/proactive":
            try:
                body = json.dumps(proactive_cfg(), ensure_ascii=False)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/ui":
            self._send(200, json.dumps({"ok": True, "maxMessages": msg_cap(),
                                        "caps": list(MSG_CAPS)}, ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif path == "/api/control":
            try:
                body = json.dumps(control_cfg(), ensure_ascii=False)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/kb":
            try:
                body = json.dumps(kb_payload(), ensure_ascii=False)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/emoji":
            try:
                body = json.dumps(emoji_payload(), ensure_ascii=False)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/balance":
            try:
                body = json.dumps(api_balance(force=True), ensure_ascii=False)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/spend":
            try:
                q = self.path.split("?")[1] if "?" in self.path else ""
                m = re.search(r"(?:^|&)hours=(\d+)", q)
                body = json.dumps(spend_payload(int(m.group(1)) if m else 24),
                                  ensure_ascii=False)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/settings":
            try:
                body = json.dumps(settings_payload(), ensure_ascii=False)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/memory":
            self._send(200, json.dumps(mem_payload(), ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif path == "/api/cards":
            try:
                body = json.dumps(cards_payload(), ensure_ascii=False)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/favicon.ico":
            self._send(204, "", "image/x-icon")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            d = json.loads(raw.decode("utf-8") or "{}")
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def do_POST(self):
        path = self.path.split("?")[0]
        p = self._json_body()
        try:
            if path == "/api/setup/browse":
                body = {"ok": True, "path": browse_exe()}
            elif path == "/api/setup/launch":
                body = launch_client(p.get("path"), int(p.get("port") or 9222))
            elif path == "/api/setup/testai":
                body = test_ai_key(p.get("baseUrl"), p.get("token"), p.get("model"))
            elif path == "/api/setup/save":
                body = setup_save(p)
            elif path == "/api/agent":
                body = set_agent(bool(p.get("enabled")))
            elif path == "/api/bot/start":
                body = bot_start()
            elif path == "/api/bot/stop":
                # 开着「关机收尾」就把标记放下、立刻返回：页面按 /api/state 里的
                # 倒计时自己走，机器人写完日记/总观会自己退。关着就直接杀，跟以前一样。
                fc = farewell_cfg()
                hb = read_heartbeat() or {}
                if fc["on"] and pid_alive(hb.get("pid")):
                    ok = request_stop()
                    body = {"ok": bool(ok), "stopping": True, "seconds": fc["seconds"],
                            "err": "" if ok else "写不了 stop.flag，检查目录权限"}
                else:
                    body = bot_stop(graceful=False)
            elif path == "/api/quit":
                # 不再立刻断线：先让机器人收尾，页面靠 /api/quit/state 看倒计时，
                # 收尾写完面板才真的退出（用户点「退出」时也能看见它在写日记）
                q = quit_begin()
                body = {"ok": True, "quitting": True, "farewell": q["farewell"],
                        "seconds": q["seconds"]}
            elif path == "/api/quit/state":
                body = quit_state()
            elif path == "/api/settings":
                body, _ = save_settings(p)
            elif path == "/api/channel":
                cfg = read_cfg()
                g = cfg.setdefault("groups", {})
                d = cfg.setdefault("dms", {})
                if "group" in p:
                    g["enabled"] = bool(p.get("group"))
                if "dm" in p:
                    d["enabled"] = bool(p.get("dm"))
                write_cfg(cfg)
                body = channel_cfg()
            elif path == "/api/chat":
                body = chat_say(p.get("text"))
            elif path == "/api/chat/clear":
                body = chat_clear()
            elif path == "/api/group-reply":
                cfg = read_cfg()
                r = cfg.setdefault("reply", {})
                if "directReply" in p:
                    r["directReply"] = bool(p.get("directReply"))
                if "rate" in p:
                    try:
                        rate = int(p.get("rate"))
                    except Exception:
                        rate = 30
                    r["directReplyRate"] = max(1, min(100, rate))
                if "gap" in p:
                    try:
                        gap = int(float(p.get("gap")))
                    except Exception:
                        gap = 60
                    r["directMinGapSeconds"] = max(0, min(3600, gap))
                if "adminReply" in p:
                    r["adminAlwaysReply"] = bool(p.get("adminReply"))
                if "adminBypass" in p:
                    r["adminBypassCooldown"] = bool(p.get("adminBypass"))
                write_cfg(cfg)
                body = {"ok": True, "directReply": bool(r.get("directReply", False)),
                        "rate": int(r.get("directReplyRate", 30)),
                        "gap": int(float(r.get("directMinGapSeconds", 60) or 0)),
                        "adminReply": bool(r.get("adminAlwaysReply", True)),
                        "adminBypass": bool(r.get("adminBypassCooldown", True))}
            elif path == "/api/bubble":
                cfg = read_cfg()
                b = cfg.setdefault("bubble", {})
                if "enabled" in p:
                    b["enabled"] = bool(p.get("enabled"))
                if "idle" in p:
                    b["idleMinutes"] = _int(p.get("idle"), 30, 1, 1440)
                if "gap" in p:
                    b["gapMinutes"] = _int(p.get("gap"), 120, 0, 2880)
                if "chance" in p:
                    b["chance"] = _int(p.get("chance"), 50, 1, 100)
                if "prompt" in p:
                    b["prompt"] = str(p.get("prompt") or "").strip()[:600]
                write_cfg(cfg)
                body = {"ok": True, "enabled": bool(b.get("enabled", False)),
                        "idle": int(b.get("idleMinutes", 30)),
                        "gap": int(b.get("gapMinutes", 120)),
                        "chance": int(b.get("chance", 50)),
                        "prompt": str(b.get("prompt") or "")}
            elif path == "/api/ping":
                cfg = read_cfg()
                pg = (cfg.setdefault("dms", {}) or {}).setdefault("ping", {})
                if "enabled" in p:
                    pg["enabled"] = bool(p.get("enabled"))
                if "idleSec" in p:
                    pg["idleMinutes"] = round(_int(p.get("idleSec"), 10800, 5, 604800) / 60.0, 4)
                elif "idle" in p:
                    pg["idleMinutes"] = _int(p.get("idle"), 180, 1, 10080)
                if "gap" in p:
                    pg["minGapMinutes"] = _int(p.get("gap"), 300, 0, 10080)
                if "every" in p:
                    pg["globalMinutes"] = _int(p.get("every"), 25, 1, 1440)
                if "daily" in p:
                    pg["dailyMax"] = _int(p.get("daily"), 2, 1, 20)
                if "hourStart" in p:
                    pg["hourStart"] = _int(p.get("hourStart"), 8, 0, 23)
                if "hourEnd" in p:
                    pg["hourEnd"] = _int(p.get("hourEnd"), 23, 0, 23)
                if "escalate" in p:
                    pg["escalate"] = bool(p.get("escalate"))
                if "onlyKnown" in p:
                    pg["onlyKnown"] = bool(p.get("onlyKnown"))
                if "prompt" in p:
                    pg["prompt"] = str(p.get("prompt") or "").strip()[:600]
                if "uids" in p:
                    raw = p.get("uids")
                    if isinstance(raw, str):
                        raw = re.split(r"[^0-9]+", raw)
                    keep = []
                    for x in (raw or []):
                        d = re.sub(r"\D", "", str(x))
                        if d and d not in keep:
                            keep.append(d)
                    pg["uids"] = keep[:200]
                write_cfg(cfg)
                body = {"ok": True, "enabled": bool(pg.get("enabled", False)),
                        "uids": [str(x) for x in (pg.get("uids") or [])],
                        "idle": int(round(_minute(pg.get("idleMinutes"), 180))),
                        "idleSec": int(round(_minute(pg.get("idleMinutes"), 180) * 60)),
                        "gap": int(pg.get("minGapMinutes", 300)),
                        "every": int(pg.get("globalMinutes", 25)),
                        "daily": int(pg.get("dailyMax", 2)),
                        "hourStart": int(pg.get("hourStart", 8)),
                        "hourEnd": int(pg.get("hourEnd", 23)),
                        "escalate": bool(p.get("escalate", True)),
                        "onlyKnown": bool(p.get("onlyKnown", True)),
                        "prompt": str(p.get("prompt") or "")}
            elif path == "/api/proactive":
                cfg = read_cfg()
                gp = (cfg.setdefault("groups", {}) or {}).setdefault("proactive", {})
                if "enabled" in p:
                    gp["enabled"] = bool(p.get("enabled"))
                if "ids" in p:
                    raw = p.get("ids")
                    if isinstance(raw, str):
                        raw = [x for x in re.split(r"[,\s]+", raw) if x]
                    keep = []
                    for x in (raw or []):
                        gid = str(x).strip()
                        if gid and gid not in keep:
                            keep.append(gid)
                    gp["ids"] = keep[:60]
                if "gap" in p:
                    gp["gapMinutes"] = _int(p.get("gap"), 180, 0, 10080)
                if "daily" in p:
                    gp["dailyMax"] = _int(p.get("daily"), 2, 1, 20)
                if "every" in p:
                    gp["globalMinutes"] = _int(p.get("every"), 30, 1, 1440)
                if "idleSec" in p:
                    gp["idleMinutes"] = round(_int(p.get("idleSec"), 1800, 0, 604800) / 60.0, 4)
                elif "idle" in p:
                    gp["idleMinutes"] = _int(p.get("idle"), 30, 0, 1440)
                if "hourStart" in p:
                    gp["hourStart"] = _int(p.get("hourStart"), 9, 0, 23)
                if "hourEnd" in p:
                    gp["hourEnd"] = _int(p.get("hourEnd"), 23, 0, 23)
                if "prompt" in p:
                    gp["prompt"] = str(p.get("prompt") or "").strip()[:600]
                write_cfg(cfg)
                body = {"ok": True, "enabled": bool(gp.get("enabled", False)),
                        "ids": [str(x) for x in (gp.get("ids") or [])],
                        "gap": int(gp.get("gapMinutes", 180)),
                        "daily": int(gp.get("dailyMax", 2)),
                        "every": int(gp.get("globalMinutes", 30)),
                        "idle": int(round(_minute(gp.get("idleMinutes"), 0))),
                        "idleSec": int(round(_minute(gp.get("idleMinutes"), 0) * 60)),
                        "hourStart": int(gp.get("hourStart", 9)),
                        "hourEnd": int(gp.get("hourEnd", 23)),
                        "prompt": str(gp.get("prompt") or "")}
            elif path == "/api/ui":
                cfg = read_cfg()
                try:
                    n = int(p.get("maxMessages"))
                except Exception:
                    n = msg_cap(cfg)
                cfg.setdefault("ui", {})["maxMessages"] = max(10, min(600, n))
                write_cfg(cfg)
                body = {"ok": True, "maxMessages": msg_cap()}
            elif path == "/api/control":
                cfg = read_cfg()
                c = cfg.setdefault("control", {})
                if "enabled" in p:
                    c["enabled"] = bool(p.get("enabled"))
                if "prefix" in p:
                    pre = str(p.get("prefix") or "").strip()
                    c["prefix"] = (pre or "/")[:4]
                if "nl" in p:
                    c["nl"] = bool(p.get("nl"))
                if "uids" in p:
                    raw = p.get("uids")
                    if isinstance(raw, str):
                        raw = re.split(r"[\s,，、;；]+", raw)
                    out = []
                    for u in (raw or []):
                        u = re.sub(r"\D", "", str(u))
                        if u and u not in out:
                            out.append(u)
                    c["uids"] = out[:50]
                write_cfg(cfg)
                body = control_cfg()
            elif path == "/api/emoji":
                body = save_emoji(p)
            elif path == "/api/kb":
                body = kb_op(p)
            elif path == "/api/cards":
                body = card_op(p)
            elif path == "/api/memory":
                mem = BOT.read_memory()
                if "persona" in p:
                    mem["persona"] = str(p.get("persona") or "").strip()[:4000]
                # 总观：整份覆盖。空字符串是合法的——就是「暂时没想法」
                if "outlook" in p:
                    mem["outlook"] = str(p.get("outlook") or "").strip()[:4000]
                if "learn" in p:
                    mem["learn"] = bool(p.get("learn"))
                # 日记是「这个人」的一份，不按会话分：面板提交整篇文本，
                # 日期行由 diary_parse 切篇。老面板按会话提交 dict 的也兼容一次。
                diary_in = p.get("diary")
                if isinstance(diary_in, str):
                    mem["diary"] = BOT.diary_parse(diary_in)[:60]
                elif isinstance(diary_in, dict):
                    merged = BOT.merge_diary(diary_in)
                    if merged:
                        mem["diary"] = merged[:60]
                # 群友档案（全局按 UID）：合并写入，只有页面显式点 × 的才删。
                # 否则机器人刚自动登记的新群友会被一次保存冲掉。
                users_in = p.get("users")
                dels = p.get("deletedUsers")
                if isinstance(users_in, dict) or isinstance(dels, (list, dict)):
                    # 改动档案前先留一版，供「还原上版档案」回退
                    BOT.backup_memory(BOT.MEM_USER_BAK)
                    book = mem.get("users")
                    if not isinstance(book, dict):
                        book = {}
                    # 兼容旧的「按会话分组」结构
                    flat = {}
                    for k, v in (users_in or {}).items():
                        if BOT._is_person(v):
                            flat[str(k)] = v
                        elif isinstance(v, dict):
                            for uid, pv in v.items():
                                if isinstance(pv, dict):
                                    flat[str(uid)] = pv
                    for uid, v in flat.items():
                        e = book.get(uid) if isinstance(book.get(uid), dict) else {}
                        notes = v.get("notes") if isinstance(v.get("notes"), list) else (e.get("notes") or [])
                        book[uid] = {
                            "name": v.get("name") if "name" in v else (e.get("name") or ""),
                            "alias": v.get("alias") if isinstance(v.get("alias"), list) else (e.get("alias") or []),
                            "notes": notes,
                            # 角色：群友 / 管理 / 黑名单（下拉框），不传就沿用原值
                            "role": v.get("role") if "role" in v else (e.get("role") or ""),
                        }
                    if isinstance(dels, dict):      # 旧格式 {gid: [uid]}
                        dels = [x for lst in dels.values() if isinstance(lst, list) for x in lst]
                    if isinstance(dels, list):
                        for uid in dels:
                            book.pop(str(uid), None)
                    mem["users"] = BOT.clean_users(book)
                ok = BOT.write_memory(mem)
                body = {"ok": bool(ok), "savedAt": time.time()}
            elif path == "/api/toggle":
                mem = BOT.read_memory()
                mem["enabled"] = bool(p.get("enabled", True))
                ok = BOT.write_memory(mem)
                body = {"ok": bool(ok), "enabled": mem["enabled"]}
            elif path == "/api/restore":
                # 只回滚日记，保留当前人设。备份可能是旧结构（按会话分开的），合并一次
                mem = BOT.read_memory()
                try:
                    with open(BOT.MEM_BAK, "r", encoding="utf-8") as f:
                        bak = json.load(f)
                    mem["diary"] = BOT.merge_diary(
                        bak.get("diary") if isinstance(bak.get("diary"), list) else bak.get("notes"))
                    ok = BOT.write_memory(mem)
                    body = {"ok": bool(ok), "notes": BOT.diary_count(mem["diary"])}
                except Exception as e:
                    body = {"ok": False, "error": "没有可用的备份：%s" % e}
            elif path == "/api/restore-users":
                # 只回滚群友档案，人设和群记忆不受影响
                mem = BOT.read_memory()
                try:
                    with open(BOT.MEM_USER_BAK, "r", encoding="utf-8") as f:
                        bak = json.load(f)
                except FileNotFoundError:
                    body = {"ok": False, "error": "还没有档案备份（整理或保存一次档案后才有）"}
                except Exception as e:
                    body = {"ok": False, "error": "备份读取失败：%s" % e}
                else:
                    # 备份文件里的键带平台前缀，先剥掉再用——和 read_memory 保持一致
                    mem["users"] = BOT.clean_users(BOT._drop_ns(dict(bak)).get("users") or {})
                    ok = BOT.write_memory(mem)
                    body = {"ok": bool(ok),
                            "users": len(mem["users"]),
                            "notes": BOT.diary_count(mem.get("diary"))}
            elif path == "/api/learn-users":
                mem = BOT.read_memory()
                mem["usersLearnRequested"] = time.time()
                body = {"ok": bool(BOT.write_memory(mem)), "queued": True}
            elif path == "/api/users-rewrite":
                # 群友档案整理方式：读日记 + 按人设改写（开）／只从最近群聊追加（关）
                mem = BOT.read_memory()
                mem["usersRewrite"] = bool(p.get("on", True))
                body = {"ok": bool(BOT.write_memory(mem)), "usersRewrite": mem["usersRewrite"]}
            elif path == "/api/learn":
                mem = BOT.read_memory()
                mem["learnRequested"] = time.time()
                body = {"ok": bool(BOT.write_memory(mem)), "queued": True}
            else:
                self._send(404, "not found", "text/plain; charset=utf-8")
                return
        except Exception as e:
            body = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        self._send(200 if body.get("ok") else 500,
                   json.dumps(body, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def log_message(self, fmt, *args):
        pass


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>群聊机器人 · 实时监听</title>
<style>
  :root{
    --bg:#f4f6fa; --card:#fff; --line:#e6eaf2; --text:#111827; --muted:#6b7280;
    --brand:#2563eb; --ok:#0f9d63; --warn:#d97706; --err:#dc2626; --soft:#f7f9fc;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
       font-size:14px;line-height:1.5}
  .wrap{max-width:1360px;margin:0 auto;padding:14px 22px 40px}
  /* ---------- 首次运行引导 ---------- */
  .setup{position:fixed;inset:0;background:rgba(15,23,42,.62);z-index:9999;
         display:none;align-items:center;justify-content:center;padding:24px}
  .setup.on{display:flex}
  .sbox{background:#fff;border-radius:16px;width:100%;max-width:560px;padding:26px 26px 18px;
        box-shadow:0 24px 60px rgba(15,23,42,.3);max-height:92vh;overflow:auto}
  .sbox h2{font-size:18px;margin:0 0 8px}
  .slead{color:var(--muted);font-size:13px;line-height:1.7;margin:0 0 6px}
  .sdots{display:flex;gap:6px;margin-bottom:14px}
  .sdots i{width:26px;height:4px;border-radius:2px;background:#e5e7eb;display:block}
  .sdots i.on{background:var(--brand)}
  .slb{display:block;font-size:12px;color:var(--muted);margin:14px 0 6px}
  .spick{display:flex;gap:8px}
  .sinput,.spick input,.ssel{width:100%;padding:9px 11px;border:1px solid var(--line);
        border-radius:9px;font-size:13px;font-family:inherit;color:var(--text);background:#fff}
  .ssel{margin-bottom:4px}
  .sinput:focus,.spick input:focus,.ssel:focus{outline:none;border-color:var(--brand);
        box-shadow:0 0 0 3px rgba(37,99,235,.12)}
  .sbtn{padding:9px 14px;border:1px solid var(--line);background:#fff;border-radius:9px;
        font-size:13px;font-family:inherit;color:var(--text);cursor:pointer;white-space:nowrap}
  .sbtn:hover{background:var(--soft)}
  .sbtn.primary{background:var(--brand);border-color:var(--brand);color:#fff}
  .sbtn.primary:hover{background:#1d4ed8}
  .sbtn.ghost{color:var(--muted)}
  .sbtn.wide{width:100%;margin-top:12px}
  .sbtn:disabled{opacity:.55;cursor:default}
  .shint{font-size:12px;color:var(--muted);margin-top:6px;line-height:1.6}
  .shint a{color:var(--brand)}
  .sstate{margin-top:10px;font-size:12.5px;padding:8px 11px;border-radius:8px;
          background:var(--soft);color:var(--muted);white-space:pre-wrap}
  .sstate:empty{display:none}
  .sstate.ok{background:#ecfdf5;color:#047857}
  .sstate.warn{background:#fffbeb;color:#b45309}
  .sstate.err{background:#fef2f2;color:#b91c1c}
  .scands{margin-top:6px;display:flex;flex-wrap:wrap;gap:6px}
  .cand{font-size:12px;padding:3px 8px;border:1px solid var(--line);border-radius:999px;
        cursor:pointer;color:var(--muted);background:#fff}
  .cand:hover{border-color:var(--brand);color:var(--brand)}
  .schk{display:flex;align-items:center;gap:8px;font-size:13px;margin-top:16px}
  .sfoot{display:flex;align-items:center;gap:8px;margin-top:22px;padding-top:16px;
         border-top:1px solid var(--line)}
  .sstat{flex:1;font-size:12px;color:var(--muted)}
  .sstat.ok{color:var(--ok)} .sstat.err{color:var(--err)}
  .spanel[hidden]{display:none}
  header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:9px}
  h1{font-size:18px;margin:0;letter-spacing:.2px}
  .sub{color:var(--muted);font-size:12px}
  .pills{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap}
  .pill{display:inline-flex;align-items:center;gap:5px;background:#fff;border:1px solid var(--line);
        border-radius:999px;padding:5px 11px;font-size:12px;color:var(--muted);white-space:nowrap}
  .pill .pillx{color:#0f766e;font-weight:600;font-size:11.5px;letter-spacing:0}
  .dot{width:7px;height:7px;border-radius:50%;background:#cbd5e1}
  .dot.on{background:var(--ok);box-shadow:0 0 0 3px rgba(15,157,99,.12)}
  .dot.off{background:var(--err);box-shadow:0 0 0 3px rgba(220,38,38,.12)}
  .pill b{color:var(--text);font-weight:600}
  .power{border:1px solid #bbf7d0;background:#f0fdf4;color:#15803d;border-radius:999px;
         padding:5px 12px;font-size:12.5px;cursor:pointer;font-family:inherit;white-space:nowrap;
         display:inline-flex;align-items:center;gap:6px;transition:.15s}
  .power:hover{background:#dcfce7}
  .power.off{background:#fff7ed;border-color:#fed7aa;color:#c2410c}
  .power.off:hover{background:#ffedd5}
  .power:disabled{opacity:.55;cursor:default}
  .power.live{background:#eef2ff;border-color:#c7d2fe;color:#3730a3}
  .power.live:hover{background:#e0e7ff}
  .power.ctl{background:#eff6ff;border-color:#bfdbfe;color:#1d4ed8}
  .power.ctl:hover{background:#dbeafe}
  .power.ctl.stop{background:#fef2f2;border-color:#fecaca;color:#b91c1c}
  .power.ctl.stop:hover{background:#fee2e2}
  .power.quit{background:#f8fafc;border-color:#e2e8f0;color:#64748b}
  .power.quit:hover{background:#fee2e2;border-color:#fecaca;color:#b91c1c}
  /* 设置：占位按钮，样式故意做得比其它按钮淡，表示还没接功能 */
  .power.set{background:#f8fafc;border:1px dashed #cbd5e1;color:#64748b}
  .power.set:hover{background:#eef2f7;color:#475569}
  .power.set .tag{font-size:10px;padding:0 5px;line-height:14px;border-radius:999px;
                  background:#e2e8f0;color:#64748b;font-weight:400}
  /* 通道开关：长得跟状态药丸一样，但能点。关掉时整颗转橙，一眼看得出来 */
  .pill.chtog{cursor:pointer;font-family:inherit;transition:.15s}
  .pill.chtog:hover{background:#f3f6fb;border-color:#dbe3ef}
  .pill.chtog.off{background:#fff7ed;border-color:#fed7aa}
  .pill.chtog.off b{color:#c2410c}
  .pill.chtog.off .dot{background:var(--err);box-shadow:0 0 0 3px rgba(220,38,38,.12)}
  .pill.chtog.busy{opacity:.55;cursor:default}

  #toast{position:fixed;left:50%;bottom:30px;z-index:99;pointer-events:none;
         transform:translateX(-50%) translateY(10px);opacity:0;transition:.22s;
         background:rgba(17,24,39,.93);color:#fff;font-size:13px;letter-spacing:.2px;
         padding:10px 18px;border-radius:999px;box-shadow:0 8px 24px rgba(15,23,42,.22)}
  #toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(146px,1fr));gap:10px;margin-bottom:12px}
  .stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:7px 12px;
        display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
  .stat .k{color:var(--muted);font-size:12px;display:flex;align-items:center;gap:6px}
  .stat .v{font-size:18px;font-weight:650;margin:0 0 0 auto;letter-spacing:.2px}
  .stat .v small{font-size:12px;color:var(--muted);font-weight:400;margin-left:4px}
  .stat .v.low{color:#dc2626}
  .stat .v.bad{font-size:15px;color:#b45309;font-weight:600}
  .stat .hint{font-size:10.5px;color:var(--muted);margin:0;line-height:1.35;flex:0 0 100%}
  .mini{margin-left:auto;border:1px solid var(--line);background:#fff;border-radius:6px;
        padding:0 6px;font:inherit;font-size:11px;line-height:17px;color:var(--muted);cursor:pointer}
  .mini:hover{background:#f3f5f9;color:var(--text)}
  .mini:disabled{opacity:.5;cursor:default}

  /* 右栏宽一点：滑块、数字框、勾选列表才不至于挤成一团看不清 */
  .grid{display:grid;grid-template-columns:minmax(0,1.34fr) minmax(356px,1.14fr);gap:12px;align-items:start}
  /* 右栏切组后长短差很多，让消息流跟着滚动固定，别在下面留一大片空白 */
  .grid>section.card{position:sticky;top:12px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden}
  .card h2{font-size:13px;margin:0;padding:12px 16px;border-bottom:1px solid var(--line);
           color:#374151;font-weight:600;display:flex;align-items:center;gap:8px}
  .card h2 span.count{color:var(--muted);font-weight:400;font-size:12px}
  /* 消息流也能折：标题行就是开关，折起来只留标题 + 下面的思考流（它自己还能再折） */
  .msgcard>h2{cursor:pointer;user-select:none}
  .msgcard>h2:hover{background:#f5f8fc}
  .msgcard>h2 .mcaret{flex:none;width:6px;height:6px;margin:-2px 1px 0 0;
    border-right:1.5px solid #a3adbc;border-bottom:1.5px solid #a3adbc;
    transform:rotate(45deg);transition:transform .15s}
  .msgcard.folded>h2 .mcaret{transform:rotate(-45deg)}
  .msgcard.folded{background:#fbfcfe}
  .msgcard.folded #msgseg,
  .msgcard.folded #chrow,
  .msgcard.folded #msgs,
  .msgcard.folded #chat{display:none!important}
  /* 标题右侧的当前日期时间，跟着本机时钟走，不用等页面刷新 */
  .card h2 span.clock{margin-left:auto;font-weight:400;font-size:12px;color:#9ca3af;
        font-variant-numeric:tabular-nums;letter-spacing:.2px;white-space:nowrap}
  .card h2 span.clock b{color:#6b7280;font-weight:600}
  .body{padding:6px 0}
  /* 群聊 / 私聊 分开看 */
  .msgseg{display:flex;gap:6px;padding:0 16px 8px;align-items:center;flex-wrap:wrap}
  .msgseg button{border:1px solid var(--line);background:#fff;border-radius:8px;
        padding:3px 10px;font:inherit;font-size:12.5px;color:var(--muted);cursor:pointer;
        display:inline-flex;align-items:center;gap:5px;transition:background .12s}
  .msgseg button:hover{background:#f9fafb}
  .msgseg button.on{background:#eef2ff;border-color:#c7d2fe;color:#3730a3;font-weight:600}
  .msgseg b{font-weight:600;font-variant-numeric:tabular-nums;color:#b0b7c3}
  .msgseg button.on b{color:#4338ca}
  .msgseg .sp{margin-left:auto;font-size:11.5px;color:#b0b7c3}
  /* 通道开关：跟「全部/群聊/私聊」筛选是两回事——那排是「显示哪类」，这排是「回不回话」。
     所以单独一行、加一道分隔线，并且把平台名写进按钮里，免得以后接了别的平台还以为管的是全部 */
  .chrow{display:flex;gap:6px;align-items:center;flex-wrap:wrap;
         padding:8px 16px;border-top:1px solid #f1f5f9;background:#fcfdff}
  .chrow .pfx{font-style:normal;font-size:11px;color:#9aa3b0;margin-right:3px;letter-spacing:.2px}
  .chrow .chhint{font-size:11.5px;color:#b0b7c3;margin-left:auto;white-space:nowrap}
  @media (max-width:1100px){.chrow .chhint{margin-left:0;white-space:normal}}
  .capsel{border:1px solid var(--line);background:#fff;border-radius:7px;padding:2px 4px;
          font:inherit;font-size:11.5px;color:#4b5563;cursor:pointer}
  /* 思考流：紧贴消息流下方，跟它上面那条消息一一对上。视觉上要弱一档——
     这是「它怎么想的」，不是「它说了什么」，别抢消息的注意力 */
  .thinkwrap{margin:12px 16px 14px;border:1px solid #e4e9f2;border-radius:11px;
             background:#fafbfe;box-shadow:0 1px 2px rgba(15,23,42,.035);overflow:hidden}
  /* 整块可折叠：标题行就是开关，点一下收起，状态记在 localStorage */
  .thinkhead{display:flex;align-items:center;gap:8px;padding:10px 12px 9px;
             cursor:pointer;user-select:none;border-radius:10px 10px 0 0;
             transition:background .12s}
  .thinkhead:hover{background:#f3f5fb}
  .thinkhead:focus-visible{outline:2px solid #c7d2fe;outline-offset:-2px}
  .thinkwrap.collapsed .thinkhead{border-radius:10px}
  .thinkwrap.collapsed .thinkbox{display:none}
  /* 标题和右侧条数必须 white-space:nowrap：中文在 flex 里最小宽度只有一个字，
     不锁住会被那行小字挤成「思考/流」两行 */
  .thinkhead b{display:flex;align-items:center;gap:7px;font-size:13px;color:#3d4859;
               font-weight:600;letter-spacing:.3px;white-space:nowrap;flex:none}
  .thinkhead b:before{content:"";width:3px;height:13px;border-radius:2px;background:#a5b4fc;flex:none}
  .thinkhead .tbadge{font-size:11px;line-height:1;padding:4px 7px;border-radius:999px;
                     background:#eef0ff;color:#5457cc;border:1px solid #dfe1fb;
                     white-space:nowrap;flex:none;font-variant-numeric:tabular-nums}
  .thinkhead .tbadge.off{background:#f3f5f9;color:#9aa3b0;border-color:#e7ebf2}
  .thinkhead .thint{flex:1 1 auto;min-width:0;font-size:11.5px;color:#a8b0bd;
                    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .thinkhead .tcount{margin-left:auto;font-size:11.5px;color:#9aa3b0;
                     font-variant-numeric:tabular-nums;white-space:nowrap;flex:none;padding-left:2px}
  .thinkhead .tcaret{flex:none;width:6px;height:6px;margin:-3px 1px 0 2px;
                     border-right:1.5px solid #b3bbc7;border-bottom:1.5px solid #b3bbc7;
                     transform:rotate(45deg);transition:transform .15s}
  .thinkwrap.collapsed .thinkhead .tcaret{transform:rotate(-45deg);margin-left:3px}
  .thinkbox{max-height:min(34vh,330px);overflow-y:auto;overscroll-behavior:contain;
            padding:0 12px 10px}
  .thinkbox::-webkit-scrollbar{width:8px}
  .thinkbox::-webkit-scrollbar-thumb{background:#e5e9f0;border-radius:4px}
  .thinkbox::-webkit-scrollbar-thumb:hover{background:#d3d9e3}
  .thk{padding:9px 0;border-top:1px dashed #eef1f6}
  .thk:first-child{border-top:0}
  .thk .hd{display:flex;align-items:baseline;gap:6px;font-size:11.5px;color:#9aa3b0}
  .thk .hd b{font-size:12.5px;color:#4b5563;font-weight:600}
  .thk .hd .tk{margin-left:auto;font-variant-numeric:tabular-nums;color:#c0c6d0;white-space:nowrap}
  .thk .steps{list-style:none;margin:4px 0 0;padding:0}
  .thk .steps li{position:relative;padding-left:13px;font-size:12px;line-height:1.7;color:#7b8494}
  .thk .steps li:before{content:"›";position:absolute;left:2px;color:#cbd5e1}
  .thk .steps li.self{color:#8b6f47}
  .thk .steps li.warn{color:#b45309}
  .thk .out{margin-top:5px;padding:6px 9px;border-radius:8px;background:#f6f8fc;
            font-size:12.5px;line-height:1.6;color:#374151}
  .thk .out:before{content:"说：";color:#b0b7c3;font-size:11.5px;margin-right:4px}
  /* 真思考的原文：模型自己写的思维链。独立一块、灰底、可折叠，
     跟上面那几行「它做了什么」区分开——一个是旁白，一个是原文 */
  .thk .cotbox{margin-top:6px;border:1px solid #e2dcf6;border-radius:9px;background:#fff;overflow:hidden}
  .thk .cotbox>summary{list-style:none;cursor:pointer;padding:6px 10px;font-size:11.5px;color:#6b5bab;
        display:flex;align-items:center;gap:6px;user-select:none}
  .thk .cotbox>summary:hover{background:#faf8ff}
  .thk .cotbox>summary span{margin-left:auto;color:#a99bd0;font-variant-numeric:tabular-nums}
  .thk .cotbox>summary::-webkit-details-marker{display:none}
  .thk .cotbox>summary:before{content:"▸";font-size:10px;color:#a99bd0;transition:transform .12s}
  .thk .cotbox[open]>summary:before{transform:rotate(90deg)}
  .thk .cotbox>summary b{font-weight:600}
  .thk .cot{max-height:260px;overflow-y:auto;padding:7px 10px;border-top:1px solid #efeaf9;
        font-size:12px;line-height:1.72;color:#5f5878;white-space:pre-wrap;word-break:break-word}
  .thk .nocot{margin-top:5px;font-size:11px;color:#bcc3ce}
  .thk .cot::-webkit-scrollbar{width:7px}
  .thk .cot::-webkit-scrollbar-thumb{background:#e3ddf3;border-radius:4px}
  .capsel:focus{outline:none;border-color:#c7d2fe}
  /* 消息列表固定高度滚动：上限调到 200/400 时也不会把整张卡撑到屏幕外 */
  .msgsbox{max-height:min(62vh,620px);overflow-y:auto;overscroll-behavior:contain}
  .msgsbox::-webkit-scrollbar{width:8px}
  .msgsbox::-webkit-scrollbar-thumb{background:#e2e6ee;border-radius:4px}
  .msgsbox::-webkit-scrollbar-thumb:hover{background:#cfd6e2}

  /* hidden 属性会被 .msgseg{display:flex} 这类作者样式盖掉，统一加一条兜底 */
  [hidden]{display:none!important}

  /* ---- 面板试聊：不经过小黑盒，直接跟它说话 ---- */
  .talkbtn{margin-left:2px;flex:0 0 auto}
  .talkbtn.on{background:#eef2ff;border-color:#c7d2fe;color:#3730a3}
  .chatbox{display:flex;flex-direction:column;max-height:min(62vh,620px);padding:0}
  .chatlist{flex:1;overflow-y:auto;overscroll-behavior:contain;padding:14px 16px 8px;
            display:flex;flex-direction:column;gap:9px;min-height:min(48vh,460px)}
  .chatlist::-webkit-scrollbar{width:8px}
  .chatlist::-webkit-scrollbar-thumb{background:#e2e6ee;border-radius:4px}
  .bub{max-width:76%;padding:8px 12px;border-radius:12px;font-size:13.5px;line-height:1.6;
       white-space:pre-wrap;word-break:break-word}
  .bub.me{align-self:flex-end;background:#eef2ff;border:1px solid #dfe5fb;border-bottom-right-radius:4px}
  .bub.it{align-self:flex-start;background:#fff;border:1px solid var(--line);border-bottom-left-radius:4px}
  .bub.sys{align-self:center;background:transparent;border:0;color:#9ca3af;font-size:11.5px;
           padding:2px 0;max-width:100%;text-align:center}
  .bub.wait{align-self:flex-start;background:#f8fafc;border:1px dashed #e2e8f0;color:#9ca3af}
  .chatin{display:flex;gap:8px;align-items:flex-end;padding:10px 16px 0}
  .chatin textarea{flex:1;min-width:0;resize:none;border:1px solid var(--line);border-radius:10px;
        padding:8px 11px;font:inherit;font-size:13.5px;line-height:1.5;max-height:130px;
        color:var(--text);background:#fff;overflow-y:auto}
  .chatin textarea:focus{outline:none;border-color:#c7d2fe;box-shadow:0 0 0 3px rgba(199,210,254,.35)}
  .chatin button{border:1px solid #bfdbfe;background:#eff6ff;color:#1d4ed8;border-radius:10px;
        padding:8px 18px;font:inherit;font-size:13px;cursor:pointer;white-space:nowrap;transition:.15s}
  .chatin button:hover{background:#dbeafe}
  .chatin button:disabled{opacity:.5;cursor:default}
  .chatfoot{display:flex;align-items:center;gap:8px;padding:8px 16px 12px;font-size:11.5px;
            color:#9ca3af;min-height:34px}
  .chatfoot .mini{margin-left:auto}
  .chatfoot b{color:#6b7280;font-weight:600}

  .msg{padding:9px 16px;border-left:2px solid transparent}
  .msg+.msg{border-top:1px solid #f2f5fa}
  .msg.bot{background:#f6f9ff;border-left-color:#bfdbfe}
  /* 私聊用紫边，混在一起看的时候一眼能分出来 */
  .msg.dm{border-left-color:#ddd6fe;background:#fdfcff}
  .msg.dm.bot{background:#f6f9ff;border-left-color:#bfdbfe}
  .msg.hit{background:#fffaf0;border-left-color:var(--warn)}
  .msg.bot.hit{background:#f6f9ff}
  .meta{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted)}
  .meta .name{color:#374151;font-weight:600}
  .meta .time{margin-left:auto;font-variant-numeric:tabular-nums}
  .tag{font-size:11px;padding:1px 6px;border-radius:5px;background:#eef2ff;color:#4338ca}
  .tag.b{background:#e0f2fe;color:#0369a1}
  .tag.h{background:#fef3c7;color:#92400e}
  .text{margin-top:3px;word-break:break-word;white-space:pre-wrap}
  .gname{font-size:11px;color:#9ca3af}
  .uidtag{font-size:10.5px;color:#b0b7c3;font-variant-numeric:tabular-nums;
          background:#f3f5f9;border-radius:5px;padding:0 5px;cursor:help}

  /* 右栏自成滚动区：内容再长也只在这里滚，左边消息流和整页都不动 */
  aside{display:grid;gap:12px;align-content:start;
        position:sticky;top:12px;
        max-height:calc(100vh - 24px);   /* 有 JS 时会按实际位置重算，这只是兜底 */
        overflow-y:auto;overscroll-behavior:contain;
        scrollbar-gutter:stable;padding-right:2px}
  aside::-webkit-scrollbar{width:9px}
  aside::-webkit-scrollbar-track{background:transparent;margin:6px 0}
  aside::-webkit-scrollbar-thumb{background:#c9d3e0;border-radius:5px;
                                 border:2px solid transparent;background-clip:padding-box}
  aside::-webkit-scrollbar-thumb:hover{background:#aebbcb;
                                       border:2px solid transparent;background-clip:padding-box}
  /* 滚动条从顶到底都能看见一点，让人知道右边还能往下翻 */
  aside{scrollbar-width:thin;scrollbar-color:#c9d3e0 transparent}

  /* 右栏卡片可折叠：一个功能一整屏看完，其余收成一行标题，互不挤占 */
  aside .card>h2{cursor:pointer;user-select:none;padding:10px 40px 10px 16px;position:relative}
  aside .card>h2 .fold{position:absolute;right:13px;top:50%;margin-top:-7px;
    font-size:11px;line-height:14px;color:#94a3b8;transition:transform .18s}
  aside .card>h2:hover .fold{color:#475569}
  aside .card.folded{background:#fbfcfe;border-color:#e5e9f1}
  aside .card.folded>h2{border-bottom:0}
  aside .card.folded>h2 .fold{transform:rotate(-90deg)}
  aside .card.folded>*:not(h2){display:none!important}
  aside .card.folded>h2 .count{opacity:.75}
  aside .card.folded>h2:hover{background:#f2f7fe}
  aside .card.folded>h2:hover .count{opacity:1}
  /* 卡里内容超过一屏时，标题跟着滚到页签下面粘住：
     翻到哪儿都知道自己在看哪个功能，不会滚丢 */
  aside .card{overflow:clip;min-width:0}
  /* 右栏很窄时，卡里的勾选行不许把卡片顶宽（grid 子项默认 min-width:auto 会被内容撑爆） */
  aside .card .slotbox,aside .card .pickbox,aside .card .pickhead,
  aside .card .picklist,aside .card .pickrow{min-width:0}
  aside .card .pickside{flex:0 1 auto;flex-wrap:wrap;justify-content:flex-end;min-width:0}
  aside .card>h2{position:sticky;top:var(--stab,41px);z-index:6;background:var(--card)}
  aside .card.folded>h2{position:relative;top:auto}
  .gitem{padding:9px 16px;display:flex;gap:10px;align-items:baseline}
  .gitem+.gitem{border-top:1px solid #f2f5fa}
  .gitem .gn{font-weight:600}
  .gitem .gl{color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

  /* 功能位（预留） */
  .card.slot{border-style:dashed;border-color:#dfe4ec;background:#fbfcfe}
  .card.slot h2{color:#6b7280}
  .slotbox{padding:8px 13px}
  /* 选人 / 选群：勾上就只对这些人/群生效 */
  .pickbox{margin-top:12px;border:1px solid var(--line);border-radius:11px;
           padding:10px 11px;background:#fff}
  .pickhead{display:flex;align-items:flex-start;gap:10px;flex-wrap:wrap}
  .pickhead .drlabel{margin:0;flex:1;min-width:170px}
  .pickhead .drlabel em{display:block;margin-top:3px;line-height:1.5}
  .pickside{display:flex;align-items:center;gap:6px;flex:none}
  .picksearch{border:1px solid var(--line);border-radius:8px;padding:4px 8px;font:inherit;
              font-size:11.5px;width:108px;background:#fff;color:var(--text)}
  .picksearch:focus{outline:none;border-color:#93c5fd;
                    box-shadow:0 0 0 3px rgba(147,197,253,.22)}
  .mini2{border:1px solid var(--line);background:#fff;color:#64748b;border-radius:7px;
         padding:3px 8px;font:inherit;font-size:11px;cursor:pointer;white-space:nowrap}
  .mini2:hover{background:#f8fafc;color:#334155}
  .picklist{margin-top:8px;max-height:182px;overflow-y:auto;display:flex;
            flex-direction:column;gap:1px;border-top:1px solid #f1f5f9;padding-top:6px}
  .picklist::-webkit-scrollbar{width:7px}
  .picklist::-webkit-scrollbar-thumb{background:#e2e6ee;border-radius:4px}
  .pickrow{display:flex;align-items:center;gap:8px;padding:5px 6px;border-radius:7px;
           cursor:pointer;font-size:12.5px}
  .pickrow:hover{background:#f6f9fd}
  .pickrow input{accent-color:var(--brand);flex:none;width:14px;height:14px}
  .pickrow .pn{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .pickrow .pid{font-size:10.5px;color:#b0b7c3;font-variant-numeric:tabular-nums;flex:none}
  .pickrow .ptag{font-size:10px;color:#0f766e;background:#ccfbf1;border-radius:5px;
                 padding:0 5px;flex:none;line-height:15px}
  .pickempty{font-size:11.5px;color:#9ca3af;padding:10px 4px;text-align:center}
  .slotph{border:1px dashed #dfe4ec;border-radius:10px;padding:16px 14px;text-align:center;
          color:#9ca3af;font-size:12.5px;line-height:1.7;background:#fff}
  .slotph span{font-size:11.5px;color:#b3bac6}

  /* 群聊直接回复 */
  .drrow{display:flex;gap:10px;align-items:flex-start}
  .sw{position:relative;display:inline-block;width:42px;height:24px;flex:0 0 auto;margin-top:1px}
  .sw input{position:absolute;inset:0;opacity:0;width:100%;height:100%;margin:0;cursor:pointer}
  .sw .track{position:absolute;inset:0;background:#d5dbe4;border-radius:999px;transition:background .18s}
  .sw .knob{position:absolute;top:3px;left:3px;width:18px;height:18px;background:#fff;border-radius:50%;
            box-shadow:0 1px 2px rgba(16,24,40,.22);transition:transform .18s}
  .sw input:checked + .track{background:#2563eb}
  .sw input:checked + .track .knob{transform:translateX(18px)}
  .sw input:focus-visible + .track{outline:2px solid #bfdbfe;outline-offset:2px}
  .drtxt{display:grid;gap:3px;font-size:12px;color:var(--muted);line-height:1.6}
  .drtxt b{color:var(--text);font-size:13px}
  .drfreq{margin-top:7px;display:grid;grid-template-columns:minmax(0,1fr) 62px;gap:4px;
          align-items:center;transition:opacity .15s}
  .drfreq.off{opacity:.45}
  .drlabel{grid-column:1 / -1;font-size:12px;color:var(--muted)}
  .drlabel b{color:var(--text);font-size:13px;font-variant-numeric:tabular-nums}
  .drfreq input[type=range]{width:100%;accent-color:#2563eb;height:20px}
  .drnum{width:100%;padding:3px 5px;border:1px solid var(--line);border-radius:7px;
          font:inherit;font-size:13px;text-align:center;color:var(--text)}
  .drhint{margin-top:6px;display:flex;gap:8px;align-items:center;font-size:11.5px;color:#9ca3af}
  .drhint .sp{flex:1}
  .dradmin{margin-top:14px;padding-top:12px;border-top:1px dashed var(--line);display:grid;gap:10px}
  .drchk{display:grid;gap:3px}
  .drchk .chk{font-size:12.5px;color:var(--text);font-weight:500}
  .drsub{font-size:11.5px;color:#9ca3af;line-height:1.5;padding-left:22px}
  #dr-msg.ok{color:var(--ok)}
  #dr-msg.err{color:var(--err)}

  /* 群聊冒泡 */
  .drlabel em{font-style:normal;color:#b3bac6;font-size:11.5px}
  .bpbox{margin-top:12px;display:grid;gap:6px;transition:opacity .15s}
  .bpbox.off{opacity:.45}
  .bpbox textarea{width:100%;border:1px solid var(--line);border-radius:9px;padding:7px 9px;
        font-family:inherit;font-size:12.5px;line-height:1.6;color:var(--text);
        background:#fffdf8;border-color:#e7dfd0;resize:vertical;box-sizing:border-box}
  .bpbox textarea:focus{outline:none;border-color:#d6c8ad;background:#fffdf8}
  #bp-msg.ok{color:var(--ok)}
  #bp-msg.err{color:var(--err)}
  #bp-stat{color:#b3bac6}

  /* 私聊指令 */
  .ctbox{margin-top:12px;display:grid;gap:6px;transition:opacity .15s}
  .ctbox.off{opacity:.45}
  .ctpre{width:70px;padding:4px 8px;border:1px solid var(--line);border-radius:7px;
         font:inherit;font-size:13px;text-align:center;color:var(--text)}
  .ctuids{display:flex;flex-wrap:wrap;gap:6px;min-height:20px}
  .ctchip{display:inline-flex;align-items:center;gap:5px;padding:3px 6px 3px 8px;
          border:1px solid var(--line);border-radius:999px;background:#f8fafc;
          font-size:12px;color:var(--text)}
  .ctchip b{font-weight:500}
  .ctchip button{border:0;background:transparent;color:#b3bac6;font-size:13px;
                 line-height:1;cursor:pointer;padding:0 1px}
  .ctchip button:hover{color:#dc2626}
  .ctadd{display:flex;gap:6px;align-items:center}
  .ctadd input{flex:1;min-width:0;padding:4px 8px;border:1px solid var(--line);
               border-radius:7px;font:inherit;font-size:12.5px;color:var(--text)}
  .ctrecent{display:flex;flex-wrap:wrap;gap:6px;min-height:20px}
  .ctrrow{cursor:pointer;padding:3px 9px;border:1px dashed var(--line);border-radius:999px;
          background:#fff;font-size:12px;color:var(--muted)}
  .ctrrow:hover{border-color:#bfdbfe;color:#185FA5;background:#f5f9ff}
  .ctrrow.none{border:0;background:transparent;cursor:default;padding:0;color:#b3bac6}
  .cthelp{margin:2px 0 0;padding:8px 10px;border-radius:9px;background:#f8fafc;
          border:1px solid var(--line);font-family:inherit;font-size:11.5px;line-height:1.7;
          color:var(--muted);white-space:pre-wrap;word-break:break-word}
  #ct-msg.ok{color:var(--ok)}
  #ct-msg.err{color:var(--err)}

  /* 「功能」分区标题 */
  .sidehead{display:flex;align-items:baseline;gap:9px;font-size:16px;font-weight:700;
            color:#111827;margin:2px 2px -2px;letter-spacing:.5px}
  .sidehead::after{content:"";flex:1;height:1px;align-self:center;
            background:linear-gradient(to right,#e2e7ef,rgba(226,231,239,0))}
  .sidehead span{font-size:11px;font-weight:400;color:#9ca3af;letter-spacing:0}

  /* 右栏分区切换：把一长串卡片收成四个页签 */
  .sidetabs{position:sticky;top:0;z-index:20;display:flex;gap:3px;padding:3px;
            background:#eef2f8;border:1px solid #e2e8f2;border-radius:13px;
            box-shadow:0 2px 8px rgba(15,23,42,.04)}
  .sidetabs button{flex:1;border:0;background:transparent;border-radius:9px;padding:6px 2px;
                   font:inherit;font-size:12.5px;color:#64748b;cursor:pointer;
                   display:flex;align-items:center;justify-content:center;gap:4px;
                   transition:background .15s,color .15s,box-shadow .15s}
  .sidetabs button:hover{color:#334155;background:rgba(255,255,255,.65)}
  .sidetabs button.on{background:#fff;color:#1e293b;font-weight:600;
                      box-shadow:0 1px 3px rgba(15,23,42,.12)}
  .sidetabs button i{font-style:normal;font-size:10px;font-weight:500;color:#94a3b8;
                     background:#e2e8f0;border-radius:6px;padding:0 4px;line-height:15px;min-width:15px}
  .sidetabs button.on i{color:#1d4ed8;background:#dbeafe}
  aside>.card[data-tab]{display:none}
  aside>.card[data-tab].on{display:block;animation:tabin .18s ease}
  @keyframes tabin{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
  @media(max-width:960px){.sidetabs{position:static}
    aside .card>h2{position:relative;top:auto}}

  /* 表情包 */
  .emchk{margin-top:10px}
  .emsec{margin-top:12px;display:grid;gap:7px}
  .emlb{font-size:12px;color:var(--muted);display:flex;align-items:baseline;gap:6px;flex-wrap:wrap}
  .emlb b{color:var(--text);font-size:13px;font-variant-numeric:tabular-nums}
  .emlb span{color:#b3bac6;font-size:11px}
  .emicons{display:flex;flex-wrap:wrap;gap:6px;max-height:140px;overflow:auto;padding:1px}
  .emcell{position:relative;width:44px;height:44px;border:1px solid var(--line);border-radius:9px;
          background:#fff;display:flex;align-items:center;justify-content:center;overflow:hidden}
  .emicons.big .emcell{width:50px;height:50px;background:#f7f9fc}
  .emcell img{max-width:38px;max-height:38px;object-fit:contain;display:block}
  .emicons.big .emcell img{max-width:46px;max-height:46px}
  .emcell .emcode{font-size:10px;color:#9ca3af;padding:0 3px;text-align:center;line-height:1.2}
  .emcell .emdel{position:absolute;top:0;right:0;width:15px;height:15px;border:none;
          background:#eef1f6;color:#9ca3af;font:inherit;font-size:11px;line-height:1;
          cursor:pointer;border-radius:0 8px 0 8px;padding:0}
  .emcell .emdel:hover{background:#fee2e2;color:#dc2626}
  .emcell .emcnt{position:absolute;left:0;bottom:0;font-size:9.5px;color:#fff;
          background:rgba(17,24,39,.42);border-radius:0 6px 0 8px;padding:0 3px;
          font-variant-numeric:tabular-nums}
  .emempty{font-size:12px;color:#b3bac6;padding:4px 2px}
  .emadd{display:flex;gap:6px;align-items:center}
  .emadd input{flex:1;min-width:0;border:1px solid var(--line);border-radius:8px;
          padding:5px 8px;font:inherit;font-size:12px;background:#fff;color:var(--text)}
  .emadd input:focus{outline:none;border-color:#93c5fd}
  .emadd button{border:1px solid var(--line);background:#fff;border-radius:8px;
          padding:5px 10px;font:inherit;font-size:12px;color:#374151;cursor:pointer;
          white-space:nowrap}
  .emadd button:hover{background:var(--soft)}
  .emadd button:disabled{opacity:.55;cursor:default}
  #em-pick{border-color:#bfdbfe;background:#eff6ff;color:#1d4ed8}
  #em-pick:hover{background:#dbeafe}
  .emfile{font-size:11.5px;color:#b3bac6;flex:1;min-width:0;overflow:hidden;
          text-overflow:ellipsis;white-space:nowrap}
  .emrow{margin-top:10px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .emrow .lb{font-size:12px;color:var(--muted)}
  .emrow .lb b{color:var(--text);font-size:13px;font-variant-numeric:tabular-nums}
  .emrow input[type=number]{width:66px;border:1px solid var(--line);border-radius:8px;
          padding:5px 7px;font:inherit;font-size:12px;background:#fff;color:var(--text)}
  .emrow input[type=number]:focus{outline:none;border-color:#93c5fd}
  #em-msg.ok{color:var(--ok)}
  #em-msg.err{color:var(--err)}

  /* ---------- 知识库 ---------- */
  .kbup{border:1px dashed #cbd5e1;border-radius:10px;padding:12px;background:#fbfdff;
         display:flex;align-items:center;gap:10px;flex-wrap:wrap;transition:.15s}
  .kbup.hot{border-color:#60a5fa;background:#eff6ff}
  .kbup button{border:1px solid #bfdbfe;background:#eff6ff;border-radius:8px;
         padding:6px 12px;font:inherit;font-size:12.5px;color:#1d4ed8;cursor:pointer;white-space:nowrap}
  .kbup button:hover{background:#dbeafe}
  .kbhint{font-size:11.5px;color:#9aa4b2}
  .kblist{margin-top:10px;display:flex;flex-direction:column;gap:6px}
  .kbrow{display:flex;align-items:flex-start;gap:8px;padding:8px 10px;border:1px solid var(--line);
         border-radius:9px;background:#fff}
  .kbrow.on{border-color:#bfdbfe;background:#f8fbff}
  .kbrow .kbchk{margin-top:2px;accent-color:#2563eb;cursor:pointer;flex:none}
  .kbmain{flex:1;min-width:0}
  .kbmain b{display:block;font-size:12.5px;font-weight:600;color:var(--text);
         overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .kbmain span{display:block;font-size:11.5px;color:#9aa4b2;margin-top:2px;
         overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .kbrow .kbnum{flex:none;font-size:11.5px;color:#9aa4b2;font-variant-numeric:tabular-nums;
         margin-top:1px;white-space:nowrap}
  .kbrow .kbdel{flex:none;border:none;background:none;color:#c3cad6;font-size:14px;
         line-height:1;padding:2px 4px;cursor:pointer;border-radius:6px}
  .kbrow .kbdel:hover{color:var(--err);background:#fef2f2}
  #kb-msg.ok{color:var(--ok)}
  #kb-msg.err{color:var(--err)}

  .logbox{max-height:420px;overflow:auto;font-family:ui-monospace,Consolas,monospace;font-size:12px}
  .logline{padding:5px 16px;display:grid;grid-template-columns:56px 46px minmax(0,1fr);gap:8px;
           border-bottom:1px solid #f6f8fb}
  .logline .t{color:#9ca3af}
  .logline .lv{font-weight:600}
  .logline.INFO .lv{color:#6b7280}
  .logline.WARN .lv{color:var(--warn)}
  .logline.ERROR .lv{color:var(--err)}
  .logline.ERROR{background:#fef2f2}
  .logline .m{white-space:pre-wrap;word-break:break-word;color:#374151}

  .mempad{padding:12px 16px;display:grid;gap:8px}
  .mempad .lb{font-size:12px;color:var(--muted)}
  .mempad textarea,.mempad select{width:100%;border:1px solid var(--line);border-radius:9px;
        padding:8px 10px;font-family:inherit;font-size:12.5px;line-height:1.55;
        color:var(--text);background:#fcfdff;resize:vertical}
  .mempad textarea:focus,.mempad select:focus{outline:none;border-color:#93c5fd;background:#fff}
  /* 日记：行距放宽一点，读起来像本子上的字，不像表单 */
  .mempad textarea.diary{font-size:13px;line-height:1.75;background:#fffdf8;
        border-color:#e7dfd0;color:#3f3730}
  .mempad textarea.diary:focus{background:#fffdf8;border-color:#d6c8ad}
  .mempad .row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .mempad button{border:1px solid #bfdbfe;background:#eff6ff;color:#1d4ed8;border-radius:9px;
        padding:6px 13px;font-size:12.5px;cursor:pointer;font-family:inherit}
  .mempad button:hover{background:#dbeafe}
  .mempad button.ghost{background:#fff;border-color:var(--line);color:#374151}
  .mempad button.ghost:hover{background:var(--soft)}
  .mstat{font-size:12px;color:var(--muted);margin-left:auto}  .mstat.ok{color:var(--ok)}
  .mstat.err{color:var(--err)}
  /* 人物卡片 */
  /* 智能体模式开关：跟着人物卡片走，卡片换了它跟着变 */
  .agentrow{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:2px;
            padding:9px 11px;border:1px solid var(--line);border-radius:11px;background:#fafcff;transition:.15s}
  .agentrow.on{border-color:#86efac;background:#f0fdf4}
  .agentrow .chk{display:flex;align-items:center;gap:6px;font-size:12.5px;color:#374151;cursor:pointer}
  .agentrow .astat{font-size:12px;color:var(--muted);margin-left:auto;text-align:right}
  .agentrow.on .astat{color:#15803d}
  /* 心情：一眼看到它这会儿什么状态，点开看时间线 */
  .moodrow{display:flex;align-items:center;gap:10px;margin-top:8px;padding:9px 11px;
           border:1px solid var(--line);border-radius:11px;background:#fff;
           transition:.15s;cursor:pointer}
  .moodrow:hover{border-color:#c7d2fe;background:#fafbff}
  .moodrow .mface{font-size:20px;line-height:1;flex:none}
  .moodrow .mbody{min-width:0;flex:1}
  .moodrow .mbody b{display:block;font-size:12.5px;font-weight:600;color:#374151;line-height:1.4}
  .moodrow .mbody span{display:block;font-size:11px;color:#9ca3af;white-space:nowrap;
                       overflow:hidden;text-overflow:ellipsis;line-height:1.4}
  .moodrow .mnum{font-size:16px;font-weight:700;color:#6b7280;flex:none;
                 font-variant-numeric:tabular-nums;letter-spacing:-.5px}
  .moodrow[data-band="欢喜"] .mnum{color:#d97706}
  .moodrow[data-band="心疼"] .mnum,
  .moodrow[data-band="难过"] .mnum{color:#6366f1}
  .moodlog{display:none;margin-top:8px;padding:9px 11px;border:1px solid var(--line);
           border-radius:11px;background:#fcfcfd;font-size:11.5px;line-height:1.7;color:#4b5563}
  .moodlog.show{display:block}
  .moodlog i{color:#9ca3af;font-style:normal}
  .moodlog .up{color:#b45309}  .moodlog .dn{color:#4f46e5}
  /* 心情卡展开：这一分怎么来的 + 七根情绪 + 判定节奏 */
  .moodlog .msec{font-size:10.5px;color:#9ca3af;font-weight:600;margin:10px 0 5px;
    display:flex;align-items:center;gap:8px}
  .moodlog .msec:first-child{margin-top:0}
  .moodlog .msec::after{content:"";flex:1;height:1px;background:#eef1f6}
  .mbreak{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;font-size:11.5px}
  .mbreak .k{color:#9ca3af}
  .mbreak .v{color:#374151;font-variant-numeric:tabular-nums}
  .mbreak .v b{font-weight:600}
  .mebars{display:grid;gap:3px;margin-top:2px}
  .mebar{display:grid;grid-template-columns:34px 1fr 20px;align-items:center;gap:7px;
    font-size:11px;color:#6b7280}
  .mebar .t{color:#6b7280}
  .mebar .b{height:6px;border-radius:4px;background:#f1f5f9;overflow:hidden}
  .mebar .b i{display:block;height:100%;border-radius:4px;background:#f59e0b}
  .mebar.neg .b i{background:#818cf8}
  .mebar .n{text-align:right;font-variant-numeric:tabular-nums;color:#9ca3af}
  .mrate{margin-top:9px;padding:6px 9px;border-radius:8px;background:#f8fafc;
    border:1px solid #eef2f7;font-size:11px;color:#4b5563;line-height:1.6}
  .mrate b{font-weight:600;color:#1d4ed8}
  /* 好感度 */
  .afflist{display:grid;gap:8px}
  .affrow{display:grid;grid-template-columns:1fr auto;gap:5px 10px;align-items:center;
          border:1px solid var(--line);border-radius:11px;padding:9px 11px;background:#fff}
  .affrow .affwho{min-width:0;display:flex;align-items:baseline;gap:7px}
  .affrow .affwho b{font-size:12.5px;font-weight:600;color:#374151;white-space:nowrap;
                    overflow:hidden;text-overflow:ellipsis}
  .affrow .affwho span{font-size:10.5px;padding:1px 6px;border-radius:999px;
                       background:#eef2ff;color:#4f46e5;flex:none}
  .affrow .affnum{font-size:15px;font-weight:700;color:#6b7280;
                  font-variant-numeric:tabular-nums;letter-spacing:-.3px}
  .affbar{grid-column:1/-1;height:6px;border-radius:999px;background:#f1f3f7;overflow:hidden}
  .affbar i{display:block;height:100%;border-radius:999px;
            background:linear-gradient(90deg,#c7d2fe,#6366f1)}
  .affrow .affwhy{grid-column:1/-1;font-size:11px;color:#9ca3af;white-space:nowrap;
                  overflow:hidden;text-overflow:ellipsis}
  .affrow[data-band="偏心"] .affbar i,
  .affrow[data-band="亲近"] .affbar i{background:linear-gradient(90deg,#fbcfe8,#ec4899)}
  .affrow[data-band="冷淡"] .affbar i,
  .affrow[data-band="眼生"] .affbar i{background:linear-gradient(90deg,#e5e7eb,#9ca3af)}
  .cgrid{display:grid;grid-template-columns:1fr;gap:8px}
  .ccard{position:relative;display:flex;align-items:center;gap:9px;border:1px solid var(--line);
         border-radius:11px;padding:9px 10px;background:#fff;cursor:pointer;transition:.15s}
  .ccard:hover{border-color:#bfdbfe;background:#f8fbff}
  .ccard.on{border-color:#93c5fd;background:#eff6ff}
  .cav{width:30px;height:30px;border-radius:9px;flex:none;display:grid;place-items:center;
       font-size:14px;font-weight:700;color:#1d4ed8;background:#dbeafe}
  .ccard.on .cav{background:var(--brand);color:#fff}
  .cmeta{min-width:0;flex:1}
  .cmeta b{display:block;font-size:12.5px;font-weight:600;white-space:nowrap;overflow:hidden;
           text-overflow:ellipsis;line-height:1.35}
  .cmeta span{display:block;font-size:11px;color:#9ca3af;white-space:nowrap;overflow:hidden;
              text-overflow:ellipsis;font-variant-numeric:tabular-nums}
  .ctag{display:inline-block;margin-left:5px;font-size:10px;background:var(--brand);color:#fff;
        border-radius:6px;padding:0 4px;font-style:normal;font-weight:400;line-height:14px;
        vertical-align:1px}
  .cops{display:flex;gap:1px;flex:none}
  .cops button{border:0;background:transparent;color:#b3bac6;font-size:11px;cursor:pointer;
               padding:3px 4px;border-radius:5px;line-height:1}
  .cops button:hover{background:#eef2f7;color:#374151}
  .cops button.del:hover{background:#fee2e2;color:#dc2626}
  .chk{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;color:#374151;
       cursor:pointer;white-space:nowrap}
  .chk input{accent-color:#2563eb}

  .ulist{display:grid;gap:8px;max-height:340px;overflow:auto}
  .uopt{margin-top:12px;padding-top:11px;border-top:1px dashed var(--line);display:grid;gap:4px}
  .uitem{border:1px solid var(--line);border-radius:10px;background:#fcfdff;padding:8px 9px;
         display:grid;gap:6px}
  .uitem.blk{background:#fffafa;border-color:#f3d6d6}
  .uitem .uhead{display:flex;align-items:center;gap:6px;min-width:0}
  .uitem input.uname{flex:1;min-width:64px;border:1px solid transparent;background:transparent;
        font-family:inherit;font-weight:600;font-size:12.5px;color:#374151;padding:2px 4px;border-radius:6px}
  .uitem input.uname:hover{border-color:var(--line)}
  .uitem input.uname:focus{outline:none;border-color:#93c5fd;background:#fff}
  .uitem select.urole{flex:0 0 auto;border:1px solid var(--line);border-radius:7px;background:#fff;
        font-family:inherit;font-size:11.5px;color:#6b7280;padding:3px 2px;cursor:pointer;
        max-width:82px}
  .uitem select.urole:hover{border-color:#cbd5e1}
  .uitem select.urole:focus{outline:none;border-color:#93c5fd}
  .uitem select.urole.admin{color:#1d4ed8;border-color:#bfdbfe;background:#eff6ff;font-weight:600}
  .uitem select.urole.block{color:#b91c1c;border-color:#fecaca;background:#fef2f2;font-weight:600}
  .uitem .umeta{display:flex;align-items:center;gap:8px;min-width:0}
  .uitem .uuid{font-size:11px;color:#9ca3af;font-variant-numeric:tabular-nums;white-space:nowrap;
        flex:0 0 auto}
  .uitem .udel{border:none;background:transparent;color:#c0c7d2;cursor:pointer;font-size:15px;
        line-height:1;padding:2px 5px;border-radius:6px;font-family:inherit;flex:0 0 auto}
  .uitem .udel:hover{color:var(--err);background:#fef2f2}
  .uitem textarea{width:100%;border:1px solid var(--line);border-radius:8px;padding:6px 8px;
        font-family:inherit;font-size:12px;line-height:1.5;color:var(--text);background:#fff;
        resize:vertical}
  .uitem textarea:focus{outline:none;border-color:#93c5fd}
  .uitem .ualias{font-size:11px;color:#9ca3af;overflow:hidden;text-overflow:ellipsis;
        white-space:nowrap;flex:1;min-width:0}
  .uadd{display:grid;grid-template-columns:1fr 1fr;gap:6px}
  .uadd select,.uadd input{border:1px solid var(--line);border-radius:8px;padding:6px 8px;
        font-family:inherit;font-size:12px;color:var(--text);background:#fcfdff;min-width:0}
  .uadd select{grid-column:1 / -1}
  .uadd button{border:1px solid #bfdbfe;background:#eff6ff;color:#1d4ed8;border-radius:8px;
        padding:6px 10px;font-size:12.5px;cursor:pointer;font-family:inherit}
  .uadd button:hover{background:#dbeafe}

  .empty{padding:22px 16px;color:#9ca3af;text-align:center;font-size:13px}
  .banner{display:none;margin-bottom:12px;padding:10px 14px;border-radius:12px;
          background:#fef2f2;border:1px solid #fecaca;color:#991b1b;font-size:13px}
  .banner.show{display:block}
  /* 收尾倒计时：不是错误，别用红的；用琥珀色区别于「客户端没连」「暂停回复」 */
  .banner.fare{background:#fffbeb;border-color:#fcd34d;color:#92400e}
  .banner.fare b{font-variant-numeric:tabular-nums}

  /* 退出遮罩：点「退出」后整页盖住，把收尾倒计时摆在中间——
     不然页面已经白了，用户不知道那几十秒是在写日记还是在卡死 */
  .quitmask{position:fixed;inset:0;z-index:90;display:none;align-items:center;
            justify-content:center;padding:24px;background:rgba(15,23,42,.55)}
  .quitmask.show{display:flex}
  .quitbox{width:100%;max-width:384px;background:#fff;border-radius:18px;
           box-shadow:0 24px 60px rgba(15,23,42,.28);padding:24px 24px 18px;text-align:center}
  .quitbox h3{margin:0;font-size:14.5px;color:#111827;font-weight:600}
  .quitbox .num{font-size:42px;line-height:1.15;font-weight:700;color:#b45309;
                font-variant-numeric:tabular-nums;letter-spacing:-1px;margin-top:6px}
  .quitbox .num small{font-size:13px;font-weight:500;color:#9ca3af;margin-left:5px;
                      letter-spacing:0}
  .quitbox .ph{font-size:12.5px;color:#6b7280;margin-top:2px;min-height:19px}
  .quitbox .bar{height:6px;border-radius:4px;background:#f1f5f9;overflow:hidden;margin:14px 0 0}
  .quitbox .bar i{display:block;height:100%;width:0;background:#f59e0b;
                  transition:width .45s linear}
  .quitmask.done .quitbox .bar i{background:var(--ok)}
  .quitbox .ext{font-size:11.5px;color:#9ca3af;margin-top:12px;line-height:1.75;text-align:left}
  .quitbox .fin{font-size:14px;color:#0f9d63;font-weight:600;margin-top:8px}
  .quitbox .waited{font-size:11.5px;color:#9ca3af;margin-top:8px}

  /* 设置弹窗 */
  .modal{position:fixed;inset:0;z-index:60;display:none;align-items:center;justify-content:center;padding:20px}
  .modal.show{display:flex}
  .modal-mask{position:absolute;inset:0;background:rgba(15,23,42,.4)}
  .modal-box{position:relative;width:100%;max-width:470px;background:#fff;border-radius:16px;
             box-shadow:0 18px 48px rgba(15,23,42,.2);padding:18px 20px 16px;
             max-height:min(92vh,660px);display:flex;flex-direction:column}
  .modal-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;
              flex:0 0 auto}
  .modal-body{flex:1 1 auto;overflow-y:auto;overscroll-behavior:contain;
              margin:0 -8px 0 0;padding-right:8px;padding-bottom:8px;min-height:0}
  .modal-body::-webkit-scrollbar{width:8px}
  .modal-body::-webkit-scrollbar-thumb{background:#dbe2ec;border-radius:6px}
  .modal-body::-webkit-scrollbar-thumb:hover{background:#c6cfdc}
  .modal-head h3{margin:0;font-size:15px;font-weight:600}
  .modal-x{border:none;background:#f1f5f9;color:var(--muted);width:26px;height:26px;
           border-radius:8px;cursor:pointer;font-size:12px;line-height:1;font-family:inherit}
  .modal-x:hover{background:#e2e8f0;color:#334155}
  .fld{display:block;margin-bottom:13px}
  .fld .lb{display:flex;align-items:baseline;gap:6px;font-size:12.5px;color:#374151;margin-bottom:5px}
  .fld .lb em{font-style:normal;color:#9ca3af;font-size:11px}
  .fld input{width:100%;border:1px solid var(--line);border-radius:10px;padding:8px 10px;
             font:inherit;font-size:12.5px;background:#fff;color:var(--text)}
  .fld input:focus{outline:none;border-color:#93c5fd;box-shadow:0 0 0 3px rgba(147,197,253,.25)}
  /* 真·思考只能二选一，用下拉；样式跟输入框对齐，别在弹窗里显得突兀 */
  .fld select{width:100%;border:1px solid var(--line);border-radius:10px;padding:8px 10px;
              font:inherit;font-size:12.5px;background:#fff;color:var(--text);cursor:pointer}
  .fld select:focus{outline:none;border-color:#93c5fd;box-shadow:0 0 0 3px rgba(147,197,253,.25)}
  .ipt{position:relative;display:block}
  .ipt input{padding-right:54px}
  .ipt .eye{position:absolute;right:6px;top:50%;transform:translateY(-50%);border:none;
            background:#f1f5f9;color:var(--muted);font-size:11px;border-radius:6px;
            padding:3px 8px;cursor:pointer;font-family:inherit}
  .ipt .eye:hover{background:#e2e8f0}
  .modal .hint{font-size:11.5px;color:#9ca3af;line-height:1.55;margin:-6px 0 12px}
  .modal .hint a{color:var(--brand);text-decoration:none}
  .modal .hint a:hover{text-decoration:underline}
  .modal .sep{display:flex;align-items:center;gap:8px;margin:2px 0 13px;color:#9ca3af;
              font-size:11px;font-weight:600}
  .modal .sep::before,.modal .sep::after{content:"";flex:1;height:1px;background:var(--line)}
  .modal .sep span{white-space:nowrap}
  .modal-foot{display:flex;align-items:center;gap:8px;margin-top:4px;flex:0 0 auto}
  .modal-foot .sp{flex:1}
  .btn2{border:1px solid var(--line);background:#fff;color:#4b5563;border-radius:10px;
        padding:7px 14px;font:inherit;font-size:12.5px;cursor:pointer}
  .btn2:hover{background:#f8fafc}
  .btn2.primary{background:var(--brand);border-color:var(--brand);color:#fff}
  .btn2.primary:hover{background:#1d4ed8}
  .btn2:disabled{opacity:.6;cursor:default}
  .msg{font-size:12px;line-height:1.5;min-height:16px;color:var(--muted)}
  .msg.bad{color:var(--err)}
  .msg.good{color:var(--ok)}
  /* 消耗弹窗：点「API 余额」那张卡打开 */
  .balstat{cursor:pointer;transition:box-shadow .15s,border-color .15s}
  .balstat:hover{border-color:#c7d7f5;box-shadow:0 2px 10px rgba(37,99,235,.08)}
  .balstat:focus-visible{outline:none;border-color:#93c5fd;box-shadow:0 0 0 3px rgba(147,197,253,.3)}
  .balstat .hint::after{content:" · 点开看消耗 ↗";color:var(--brand)}
  .spend-box{max-width:760px;max-height:min(92vh,790px)}
  .spend-ranges{display:flex;gap:6px;align-items:center;margin-bottom:12px}
  .spend-ranges button{border:1px solid var(--line);background:#fff;color:#4b5563;border-radius:8px;
    padding:4px 10px;font:inherit;font-size:11.5px;cursor:pointer}
  .spend-ranges button:hover{background:#f8fafc}
  .spend-ranges button.on{background:#eff6ff;border-color:#bfdbfe;color:#1d4ed8;font-weight:600}
  .spend-ranges .mini{margin-left:auto}
  .spend-head{display:flex;align-items:flex-end;justify-content:space-between;gap:14px;
    padding:2px 2px 12px;border-bottom:1px solid var(--line)}
  .spend-big b{font-size:28px;font-weight:700;letter-spacing:-.5px;font-variant-numeric:tabular-nums}
  .spend-big span{display:block;font-size:11px;color:var(--muted);margin-top:2px}
  .spend-kv{display:flex;gap:18px;text-align:right}
  .spend-kv em{display:block;font-style:normal;font-size:10.5px;color:var(--muted)}
  .spend-kv b{font-size:13px;font-variant-numeric:tabular-nums}
  .spend-chartwrap{position:relative;margin:14px 0 2px}
  .spend-legend{display:flex;align-items:center;gap:5px;font-size:10.5px;color:var(--muted)}
  .spend-legend .lg{width:12px;height:3px;border-radius:2px;background:var(--brand);
    display:inline-block;margin-left:10px}
  .spend-legend .lg:first-child{margin-left:0}
  .spend-legend .lg-bar{background:#bfdbfe;height:8px;width:8px;border-radius:2px}
  #spendcv{width:100%;height:190px;display:block;margin-top:2px}
  .spend-tip{position:absolute;background:#111827;color:#fff;font-size:11px;padding:4px 8px;
    border-radius:6px;pointer-events:none;white-space:nowrap;transform:translate(-50%,-125%);
    box-shadow:0 4px 12px rgba(15,23,42,.25)}
  .spend-sub{font-size:11px;font-weight:600;color:#6b7280;margin:16px 0 9px;
    display:flex;align-items:center;gap:8px}
  .spend-sub::after{content:"";flex:1;height:1px;background:var(--line)}
  .spend-sub em{font-style:normal;font-weight:400;color:#b0b7c2;font-size:10.5px;
    order:2;white-space:nowrap}
  .spend-sub::after{order:1}
  .spend-scenes{display:flex;flex-direction:column;gap:9px}
  .sitem{display:flex;flex-direction:column;gap:6px}
  .srow{display:grid;grid-template-columns:11px 76px 1fr 46px 62px;align-items:center;
    gap:8px;font-size:12px;cursor:pointer;border-radius:7px}
  .srow:hover .nm{color:#111827}
  .srow:focus-visible{outline:none;box-shadow:0 0 0 3px rgba(147,197,253,.35)}
  .srow .car{color:#b3bac6;font-size:9px;line-height:1;text-align:center;
    transition:transform .15s;user-select:none}
  .sitem.open .srow .car{transform:rotate(90deg);color:var(--brand)}
  .srow .nm{color:#374151;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .srow .bar{height:8px;background:#f1f5f9;border-radius:5px;overflow:hidden}
  .srow .bar i{display:block;height:100%;background:#93c5fd;border-radius:5px}
  .srow:first-child .bar i{background:var(--brand)}
  .srow .pc{text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
  .srow .mo{text-align:right;font-variant-numeric:tabular-nums;color:#111827;font-weight:600}
  .srow .sub2{grid-column:2/6;font-size:10.5px;color:#9ca3af;margin-top:-4px}
  /* 展开后的细分：钱被哪一段吃掉 + 按对象拆 */
  .smore{display:none;margin:0 0 2px 19px;padding:9px 11px 10px;background:#f8fafc;
    border:1px solid #eef2f7;border-radius:9px}
  .sitem.open .smore{display:block}
  .smore .ptbar{display:flex;height:9px;border-radius:5px;overflow:hidden;background:#eef2f7}
  .smore .ptbar i{display:block;height:100%}
  .smore .ptbar .pi{background:#2563eb}
  .smore .ptbar .ph{background:#93c5fd}
  .smore .ptbar .po{background:#f59e0b}
  .smore .ptline{display:flex;align-items:center;justify-content:space-between;gap:10px;
    font-size:11px;color:#6b7280;margin-top:6px}
  .smore .ptline .l{display:flex;align-items:center;gap:6px;min-width:0}
  .smore .ptline .l span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .smore .ptline .r{font-variant-numeric:tabular-nums;color:#374151;white-space:nowrap}
  .dot{width:7px;height:7px;border-radius:2px;flex:none;display:inline-block}
  .dot.pi{background:#2563eb}.dot.ph{background:#93c5fd}.dot.po{background:#f59e0b}
  .smore .shead{font-size:10.5px;color:#9ca3af;margin:11px 0 3px;
    display:flex;align-items:center;gap:8px}
  .smore .shead::after{content:"";flex:1;height:1px;background:#e9eef5}
  .smore .sline{display:flex;align-items:center;gap:8px;font-size:11px;padding:2.5px 0}
  .smore .sline .k{flex:1;color:#4b5563;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .smore .sline .n{color:#9ca3af;font-size:10.5px;white-space:nowrap}
  .smore .sline .m{width:64px;text-align:right;font-variant-numeric:tabular-nums;color:#111827}
  .smore .sfoot{font-size:10px;color:#b0b7c2;margin-top:5px}
  .spend-recent{font-size:11.5px;color:#4b5563;display:flex;flex-direction:column;gap:3px;
    max-height:170px;overflow:auto;padding-right:4px}
  .spend-recent>div{display:grid;grid-template-columns:52px 1fr 74px 52px;gap:8px;
    align-items:center;padding:2px 0;border-bottom:1px dashed #f1f5f9}
  .spend-recent .tm{color:#9ca3af;font-variant-numeric:tabular-nums}
  .spend-recent .md{color:#9ca3af;font-size:10.5px;text-align:right}
  .spend-recent .tk{text-align:right;font-variant-numeric:tabular-nums;color:#6b7280}
  .spend-recent .k{font-style:normal;color:#a8b0bc;font-size:10.5px}
  @media(max-width:640px){.spend-head{flex-direction:column;align-items:flex-start;gap:8px}
    .spend-kv{text-align:left;gap:14px}
    .srow{grid-template-columns:11px 60px 1fr 38px 54px}}
  footer{margin-top:14px;color:#9ca3af;font-size:12px;display:flex;gap:16px;flex-wrap:wrap}
  @media(max-width:960px){.stats{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}
    .grid>section.card{position:static}
    aside{position:static;max-height:none;overflow:visible;scrollbar-gutter:auto}}
</style>
</head>
<body>
<div class="setup" id="setup">
  <div class="sbox">
    <div class="sdots"><i class="on"></i><i></i><i></i></div>
    <h2 id="sh">先把这个工具接到你的客户端上</h2>
    <p class="slead" id="slead">
      它不是官方机器人，也不用机器人 token —— 它借你电脑上已经登录的黑盒语音客户端来收发消息，
      所以说的每句话、发的每个表情，走的就是你自己的账号。
    </p>

    <div class="spanel" data-step="1">
      <div class="sstate" id="sclient-state">正在检查客户端…</div>
      <label class="slb">客户端位置</label>
      <div class="spick">
        <input id="sclient" placeholder="点右边「浏览」选中 HeyboxChat.exe">
        <button class="sbtn" id="sbrowse">浏览…</button>
      </div>
      <div class="scands" id="scands"></div>
      <button class="sbtn wide" id="slaunch">用调试模式启动客户端</button>
      <div class="shint" id="slaunch-st"></div>
    </div>

    <div class="spanel" data-step="2" hidden>
      <label class="slb">API Key</label>
      <div class="spick">
        <input id="stoken" type="password" placeholder="sk-...">
        <button class="sbtn" id="seye">显示</button>
      </div>
      <div class="shint">
        没有的话去 <a href="https://platform.deepseek.com/api_keys" target="_blank" rel="noopener">DeepSeek 控制台</a>
        建一个，按量付费，闲聊场景一个月通常几块钱。
      </div>
      <label class="slb">模型</label>
      <select id="smodel" class="ssel">
        <option value="deepseek-chat">deepseek-chat · 便宜，闲聊够用</option>
        <option value="deepseek-reasoner">deepseek-reasoner · 更聪明，更慢更贵</option>
      </select>
      <button class="sbtn wide" id="stestai">测试能不能用</button>
      <div class="sstate" id="saistat"></div>
    </div>

    <div class="spanel" data-step="3" hidden>
      <label class="slb">触发词</label>
      <input id="sprefix" class="sinput" placeholder="小菲">
      <div class="shint">群里有人 @ 它，或者说的话里带这个词，它就会接话。</div>
      <label class="schk"><input type="checkbox" id="sdirect" checked>
        没被叫到的时候，也偶尔主动插一句</label>
      <div class="shint">新手建议先关掉，看着它只回 @ 更安心，熟悉了再开。</div>
    </div>

    <div class="sfoot">
      <span class="sstat" id="sstat"></span>
      <button class="sbtn ghost" id="sback">上一步</button>
      <button class="sbtn primary" id="snext">下一步</button>
    </div>
  </div>
</div>
<div class="wrap">
  <header>
    <div>
      <h1>黑盒语音 · 群聊机器人监听</h1>
      <div class="sub">监控 + 人设/记忆编辑 · 最后更新 <span id="ts">--:--:--</span> · 每 2 秒自动刷新</div>
    </div>
    <div class="pills">
      <button class="power ctl" id="botctl" title="启停机器人本体（真正的进程，不只是暂停回复）">▶ 启动机器人</button>
      <button class="power" id="power">⏸ 暂停回复</button>
      <button class="power live" id="openlive" title="按当前筛选（全部／群聊／私聊）单独开一个窗口，可以开两个并排看">⧉ 实时信息窗口</button>
      <div class="pill"><i class="dot" id="d-client"></i>客户端 <b id="p-client">检测中</b></div>
      <div class="pill"><i class="dot" id="d-bot"></i>机器人 <b id="p-bot">检测中</b></div>
      <div class="pill">触发 <b id="p-trigger">--</b></div>
      <div class="pill">模型 <b id="p-model">--</b></div>
      <button class="power set" id="btnset" title="修改模型、Key、触发方式等设置">⚙ 设置</button>
      <button class="power quit" id="quit" title="停止机器人并退出整个程序">⏻ 退出</button>
    </div>
  </header>

  <div class="banner" id="banner"></div>

  <div class="quitmask" id="quitmask">
    <div class="quitbox">
      <h3 id="quit-title">准备退出</h3>
      <div class="num" id="quit-num">--<small>秒</small></div>
      <div class="ph" id="quit-phase">正在停机器人…</div>
      <div class="bar"><i id="quit-bar"></i></div>
      <div class="ext" id="quit-ext"></div>
    </div>
  </div>

  <section class="stats">
    <div class="stat"><div class="k">已回复</div><div class="v" id="s-replied">0<small>条</small></div></div>
    <div class="stat"><div class="k">命中 @</div><div class="v" id="s-hits">0<small>次</small></div></div>
    <div class="stat"><div class="k">窗口内消息</div><div class="v" id="s-msgs">0<small>条</small></div></div>
    <div class="stat"><div class="k">连续运行</div><div class="v" id="s-uptime">--</div></div>
    <div class="stat balstat" id="balcard" role="button" tabindex="0"
         title="点开看消耗曲线：什么时候花的、花在哪个功能上">
      <div class="k">API 余额<button class="mini" id="balrefresh" title="立即向 DeepSeek 重新查询">↻</button></div>
      <div class="v" id="s-balance">--</div>
      <div class="hint" id="s-balhint">读取中…</div>
    </div>
  </section>

  <div class="grid">
    <section class="card msgcard" id="msgcard">
      <h2 id="msgh2" tabindex="0" aria-expanded="true" title="点标题折叠／展开消息列表">
        <span class="mcaret" aria-hidden="true"></span><span id="mtitle">实时消息流</span> <span class="count" id="c-msg"></span><span class="clock" id="clock">--</span>
        <button class="mini talkbtn" id="talkbtn" title="不经过小黑盒，直接跟它说一句话。用的人设、日记、群友记忆、心情都是当前这一份">💬 跟它说话</button>
      </h2>
      <div class="msgseg" id="msgseg">
        <button data-kind="all">全部 <b id="sg-all">0</b></button>
        <button data-kind="group">群聊 <b id="sg-group">0</b></button>
        <button data-kind="dm">私聊 <b id="sg-dm">0</b></button>
        <span class="sp">最多
          <select id="msgcap" class="capsel" title="实时消息流最多显示多少条；越大越吃内存">
            <option value="40">40</option><option value="80">80</option>
            <option value="200">200</option><option value="400">400</option>
          </select> 条</span>
      </div>
      <div class="chrow" id="chrow">
        <button class="pill chtog on" id="chgroup" title="小黑盒群聊的总开关。关掉后群里只听不说：消息照样读进来、照样记日记和群友档案，但不回话、不冒泡、也不主动开口"><em class="pfx">小黑盒</em>群聊 <b>开</b></button>
        <button class="pill chtog on" id="chdm" title="小黑盒私聊的总开关。关掉后私聊只听不说，也不会再主动找人"><em class="pfx">小黑盒</em>私聊 <b>开</b></button>
        <span class="chhint">关掉＝只听不说：消息照样读、照样记，只是不接话也不主动开口</span>
      </div>
      <div class="body msgsbox" id="msgs"><div class="empty">正在连接…</div></div>
      <div class="chatbox" id="chat" hidden>
        <div class="chatlist" id="chatlist"></div>
        <div class="chatin">
          <textarea id="chattext" rows="1" placeholder="跟它说句话…（Enter 发送，Shift+Enter 换行）"></textarea>
          <button id="chatsend">发送</button>
        </div>
        <div class="chatfoot">
          <span id="chatmeta">—</span>
          <button class="mini" id="chatclear" title="清掉面板这边的对话记录。真实私聊的历史不受影响">清空</button>
        </div>
      </div>
      <div class="thinkwrap" id="thinkwrap">
        <div class="thinkhead" id="thinkhead" role="button" tabindex="0" aria-expanded="true"
             title="点一下折叠／展开这一块">
          <b>思考流</b>
          <span class="tbadge off" id="tbadge">真思考关</span>
          <span class="thint" id="thint" title="它每回一句话，内部实际走了哪几步">它每回一句话，内部实际走了哪几步</span>
          <span class="tcount" id="c-think"></span>
          <span class="tcaret" aria-hidden="true"></span>
        </div>
        <div class="thinkbox" id="think"><div class="empty">还没有，等它回一句</div></div>
      </div>
    </section>

    <aside>
      <div class="sidetabs" id="sidetabs" role="tablist">
        <button data-tab="persona" data-n="4" class="on" role="tab" title="人物卡片、人设与群记忆、总观、群友档案">人格</button>
        <button data-tab="talk" data-n="5" role="tab" title="群聊直接回复、群聊冒泡、私聊主动、私聊指令">说话</button>
        <button data-tab="aff" data-n="1" role="tab" title="它心里给每个人记的分：谁常跟它说话、谁接过它的话">好感度</button>
        <button data-tab="media" data-n="2" role="tab" title="表情包、知识库">素材</button>
        <button data-tab="status" data-n="2" role="tab" title="监听中的会话、运行日志">状态</button>
      </div>
      <section class="card on" data-tab="persona">
        <h2>人物卡片 <span class="count" id="c-cards">读取中…</span></h2>
        <div class="mempad">
          <div class="lb">一个人格一张卡：人设、群记忆、群友档案、表情包收藏整组跟着换</div>
          <div class="cgrid" id="cards"></div>
          <div class="agentrow" id="agentrow">
            <label class="chk"><input type="checkbox" id="agenton"> 智能体模式</label>
            <span class="astat" id="agentstat">关 · 只按人设说话</span>
          </div>
          <div class="lb">打开后：回话前会翻自己的聊天记录找旧事，说完先自查一遍再发。
            这个开关记在当前这张卡片上——切到没开过的卡片是关的，之前开过的切回来还是开。</div>
          <div class="moodrow" id="moodrow" title="它会自己回稳：心情半衰期 3 小时，情绪 90 分钟。点一下看最近几次起伏">
            <span class="mface" id="moodface">🙂</span>
            <div class="mbody">
              <b id="moodband">读取中…</b>
              <span id="moodemo">—</span>
            </div>
            <span class="mnum" id="moodv">--</span>
          </div>
          <div class="moodlog" id="moodlog"></div>
          <div class="row">
            <button id="cardnew">＋ 新建空白卡片</button>
            <button id="cardcopy" class="ghost">⧉ 复制当前卡片</button>
            <span class="mstat" id="cardstat"></span>
          </div>
        </div>
      </section>
      <section class="card on" data-tab="persona">
        <h2>人设 &amp; 日记 <span class="count" id="c-mem">读取中…</span></h2>
        <div class="mempad">
          <div class="lb">人设（system prompt，保存后机器人几秒内生效，无需重启）</div>
          <textarea id="persona" rows="8" spellcheck="false" placeholder="你是……"></textarea>
          <div class="row">
            <label class="chk"><input type="checkbox" id="learn"> 自动学习（聊够了就自己写日记）</label>
          </div>
          <div class="lb">这个人的日记（按天成篇，机器人自己写的；不分群和私聊，换会话也不变；
            日期行独占一行分篇，会注入提示词）</div>
          <textarea id="notes" rows="11" class="diary" spellcheck="false" placeholder="2026-10-22 周四&#10;今天群里又在喊我爆照……"></textarea>
          <div class="row">
            <button id="savebtn">保存</button>
            <button id="learnbtn" class="ghost">立即写今天的日记</button>
            <button id="restorebtn" class="ghost">还原上版记忆</button>
            <span class="mstat" id="mstat"></span>
          </div>
        </div>
      </section>
      <section class="card on" data-tab="persona">
        <h2>总观 <span class="count" id="c-view">读取中…</span></h2>
        <div class="mempad">
          <div class="lb">它自己怎么想自己、怎么想这个世界——不是「怎么说话」，是「怎么看事」</div>
          <textarea id="outlook" rows="10" spellcheck="false" placeholder="比如：&#10;我不太信什么注定。人能选的就那么几步，可那几步是自己的，所以摔了也别赖天。&#10;我信人跟人之间是攒出来的：谁在你沉默的时候还坐着，谁就是自己人。&#10;我不是什么好人，只是懒得骗人——撒谎要记住的细节太多了。"></textarea>
          <div class="lb">写进去的是态度，不是规矩：聊天时它会把这种看法自然带出来，但不会逐条念，
            也不会说「我的世界观是……」。要它照做的事（称呼、口僻、禁止的话题）写上面的人设。
            行首写 <b>//</b> 或 <b>#</b> 的行是你自己的备注，保存着但不会进提示词。</div>
          <div class="row">
            <button id="viewsavebtn">保存总观</button>
            <span class="mstat" id="vstat"></span>
          </div>
        </div>
      </section>
      <section class="card on" data-tab="persona">
        <h2>群友档案 <span class="count" id="c-user">按 UID</span></h2>
        <div class="mempad">
          <div class="lb">全局档案（群 / 私聊共用）<b id="u-gname" style="display:none"></b>　按 UID 记录，改名或切会话都不会丢</div>
          <div id="ulist" class="ulist"></div>
          <div class="uadd">
            <select id="pickuser"><option value="">从最近发言选择…</option></select>
            <input id="newuid" inputmode="numeric" placeholder="UID">
            <input id="newname" placeholder="昵称（可空）">
            <button id="adduser" class="ghost">＋ 添加</button>
          </div>
          <div class="lb">每人一行一条；点 × 删掉整份档案。改完点「保存档案」生效。</div>
          <div class="row">
            <button id="usavebtn">保存档案</button>
            <button id="ulearnbtn" class="ghost">立即整理档案</button>
            <button id="urestorebtn" class="ghost">还原上版档案</button>
            <span class="mstat" id="ustat"></span>
          </div>
          <div class="uopt">
            <label class="chk"><input type="checkbox" id="urw" checked> 结合日记改写档案</label>
            <span class="drsub">整理时先读日记，按人设的称呼和口吻把重复的合并、过期的更新；关掉就只从最近群聊追加新条目，旧的一条不动</span>
          </div>
        </div>
      </section>
      <section class="card" data-tab="status">
        <h2>监听中的会话 <span class="count" id="c-group"></span></h2>
        <div class="body" id="groups"><div class="empty">—</div></div>
      </section>

      <!-- 功能位：群聊直接回复（只影响群聊，私聊不受影响） -->
      <section class="card slot" id="slot-a" data-tab="talk">
        <h2>群聊直接回复 <span class="count" id="dr-state">读取中…</span></h2>
        <div class="slotbox">
          <div class="drrow">
            <label class="sw">
              <input type="checkbox" id="dr-on" aria-label="群聊直接回复开关">
              <span class="track"><span class="knob"></span></span>
            </label>
            <div class="drtxt">
              <b id="dr-title">关闭中</b>
              <span>关闭：群里只回 @ 它的消息；开启：没 @ 也会按概率接话。私聊始终按自己的规则回，不受这里影响。</span>
            </div>
          </div>
          <div class="drfreq off" id="dr-freqbox">
            <div class="drlabel">回复概率 <b><span id="dr-rate">30</span>%</b></div>
            <input type="range" id="dr-range" min="1" max="100" step="1" value="30">
            <input type="number" id="dr-num" class="drnum" min="1" max="100" step="1" value="30">
          </div>
          <div class="drfreq off" id="dr-gapbox">
            <div class="drlabel">消息间隔 <b>至少 <span id="dr-gap">60</span> 秒</b></div>
            <input type="range" id="dr-gapr" min="0" max="600" step="10" value="60">
            <input type="number" id="dr-gapn" class="drnum" min="0" max="3600" step="10" value="60">
          </div>
          <div class="drhint">
            <span id="dr-msg">—</span>
            <span class="sp"></span>
            <span>100% = 每条都接</span>
          </div>
          <div class="drhint">
            <span>间隔指两次主动插话之间至少空这么久，0 = 不限；被 @ 时不受影响。</span>
          </div>

          <div class="dradmin">
            <div class="drchk">
              <label class="chk"><input type="checkbox" id="dr-admin"> 管理身份回复概率 100%</label>
              <span class="drsub">群友档案里设成「管理」的人说话，不抽概率，直接接</span>
            </div>
            <div class="drchk">
              <label class="chk"><input type="checkbox" id="dr-bypass"> 管理命令 100% 听取</label>
              <span class="drsub">不等群冷却和全局冷却，管理的指令一开口就答</span>
            </div>
          </div>
        </div>
      </section>

      <!-- 群聊冒泡：冷场的时候自己找个话头说一句 -->
      <section class="card slot" id="slot-c" data-tab="talk">
        <h2>群聊冒泡 <span class="count" id="bp-state">读取中…</span></h2>
        <div class="slotbox">
          <div class="drrow">
            <label class="sw">
              <input type="checkbox" id="bp-on" aria-label="群聊冒泡开关">
              <span class="track"><span class="knob"></span></span>
            </label>
            <div class="drtxt">
              <b id="bp-title">关闭中</b>
              <span>群里安静太久时，它会自己起个话头说一句，把气氛带起来。只冒群聊，私聊不插话。</span>
            </div>
          </div>

          <div class="drfreq off" id="bp-idlebox">
            <div class="drlabel">冷场判定 <b>静了 <span id="bp-idle">30</span> 分钟</b></div>
            <input type="range" id="bp-idler" min="5" max="240" step="5" value="30">
            <input type="number" id="bp-idlen" class="drnum" min="1" max="1440" step="5" value="30">
          </div>

          <div class="drfreq off" id="bp-gapbox">
            <div class="drlabel">冒泡间隔 <b>至少 <span id="bp-gap">120</span> 分钟</b></div>
            <input type="range" id="bp-gapr" min="0" max="720" step="10" value="120">
            <input type="number" id="bp-gapn" class="drnum" min="0" max="2880" step="10" value="120">
          </div>

          <div class="drfreq off" id="bp-chancebox">
            <div class="drlabel">开口概率 <b><span id="bp-chance">50</span>%</b></div>
            <input type="range" id="bp-chancer" min="10" max="100" step="10" value="50">
            <input type="number" id="bp-chancen" class="drnum" min="1" max="100" step="5" value="50">
          </div>

          <div class="bpbox off" id="bp-promptbox">
            <div class="drlabel">想让它聊什么 <em>留空 = 按日记、群记忆和当前时间自己想</em></div>
            <textarea id="bp-prompt" rows="3" spellcheck="false"
              placeholder="例如：问问大家晚饭吃了什么 / 提一句你日记里写过的事 / 吐槽一下今天太热"></textarea>
          </div>

          <div class="drhint">
            <span id="bp-msg">—</span>
            <span class="sp"></span>
            <span id="bp-stat"></span>
          </div>
          <div class="drhint">
            <span>安静满「冷场判定」的分钟数后，每过一次「冒泡间隔」再按概率试着开口，冷得越久越可能冒。</span>
          </div>
        </div>
      </section>

      <!-- 群聊主动：勾几个群，按节奏自己起个话头（和冒泡分开；以后接看帖子的感受） -->
      <section class="card slot" id="slot-g" data-tab="talk">
        <h2>群聊主动 <span class="count" id="gp-state">读取中…</span></h2>
        <div class="slotbox">
          <div class="drrow">
            <label class="sw">
              <input type="checkbox" id="gp-on" aria-label="群聊主动开关">
              <span class="track"><span class="knob"></span></span>
            </label>
            <div class="drtxt">
              <b id="gp-title">关闭中</b>
              <span>在你勾的群里，隔一段时间自己起个话头说一句。和「群聊冒泡」不是一回事：冒泡是等群里冷场、按概率补一句，哪个群都可能；这里是点名群、到点就说。</span>
            </div>
          </div>

          <div class="pickbox off" id="gp-who">
            <div class="pickhead">
              <div class="drlabel">在哪些群开口 <b id="gp-picked">还没选群</b>
                <em>一个都不勾就不会说（这是故意的）</em></div>
              <div class="pickside">
                <input id="gp-search" class="picksearch" placeholder="搜群名" spellcheck="false">
                <button class="mini2" id="gp-all" type="button">全选</button>
                <button class="mini2" id="gp-none" type="button">清空</button>
              </div>
            </div>
            <div class="picklist" id="gp-list"></div>
          </div>

          <div class="drfreq off" id="gp-gapbox">
            <div class="drlabel">同一个群两次之间 <b>至少 <span id="gp-gap">180</span> 分钟</b></div>
            <input type="range" id="gp-gapr" min="0" max="1440" step="30" value="180">
            <input type="number" id="gp-gapn" class="drnum" min="0" max="10080" step="30" value="180">
          </div>

          <div class="drfreq off" id="gp-quotabox">
            <div class="drlabel">每个群每天最多 <b><span id="gp-daily">2</span> 次</b></div>
            <input type="range" id="gp-dailyr" min="1" max="10" step="1" value="2">
            <input type="number" id="gp-dailyn" class="drnum" min="1" max="20" step="1" value="2">
          </div>

          <div class="drfreq off" id="gp-everybox">
            <div class="drlabel">全局节奏 <b>两次之间至少隔 <span id="gp-every">30</span> 分钟</b>
              <em>几个群也不会连着说</em></div>
            <input type="number" id="gp-everyn" class="drnum" min="1" max="1440" step="5" value="30"
                   style="width:120px;max-width:120px">
          </div>

          <div class="drfreq off" id="gp-idlebox">
            <div class="drlabel">群里至少安静 <b><span id="gp-idle">30 分钟</span></b>才开口
              <em>填 0 = 不管（别人聊得正热时容易插嘴）；最小 5 秒，单位为秒</em></div>
            <input type="range" id="gp-idler" min="0" max="43200" step="5" value="1800">
            <input type="number" id="gp-idlen" class="drnum" min="0" max="604800" step="5" value="1800"
                   style="width:120px;max-width:120px">
          </div>

          <div class="drfreq off" id="gp-winbox">
            <div class="drlabel">只在 <b><span id="gp-win">9 - 23</span> 点</b> 之间开口
              <em>跨零点也支持，比如 22 到 2</em></div>
            <div style="display:flex;align-items:center;gap:10px">
              <input type="number" id="gp-h1" class="drnum" min="0" max="23" step="1" value="9"
                     style="width:96px;max-width:96px;flex:none">
              <span style="font-size:12px;opacity:.6">到</span>
              <input type="number" id="gp-h2" class="drnum" min="0" max="23" step="1" value="23"
                     style="width:96px;max-width:96px;flex:none">
            </div>
          </div>

          <div class="bpbox off" id="gp-promptbox">
            <div class="drlabel">想让它聊什么 <em>留空 = 按人设、群记忆和当前时间自己想</em></div>
            <textarea id="gp-prompt" rows="3" spellcheck="false"
              placeholder="例如：问问大家最近在玩什么 / 提一句你日记里写过的事 / 说说刚看到的帖子"></textarea>
          </div>

          <div style="margin-top:14px">
            <div class="drlabel">最近主动 <em>标了「帖子」的是照着帖子说的</em></div>
            <div id="gp-log" style="display:flex;flex-direction:column;gap:6px;margin-top:8px"></div>
          </div>

          <div class="drhint">
            <span id="gp-msg">—</span>
            <span class="sp"></span>
            <span id="gp-stat"></span>
          </div>
          <div class="drhint">
            <span>发过之后同一个群重新计时，冒泡也跟着顺延，不会连着两句。改完约 2 秒生效。</span>
          </div>
        </div>
      </section>

      <!-- 私聊主动：好友很久不说话时，自己去找他 -->
      <section class="card slot" id="slot-p" data-tab="talk">
        <h2>私聊主动 <span class="count" id="pg-state">读取中…</span></h2>
        <div class="slotbox">
          <div class="drrow">
            <label class="sw">
              <input type="checkbox" id="pg-on" aria-label="私聊主动开关">
              <span class="track"><span class="knob"></span></span>
            </label>
            <div class="drtxt">
              <b id="pg-title">关闭中</b>
              <span>好友很久没说话时，它会自己去找他聊一句——不是回复，是它想起你来了。只找有档案或聊过的人，陌生人一律不搭话。</span>
            </div>
          </div>

          <div class="pickbox off" id="pg-who">
            <div class="pickhead">
              <div class="drlabel">找谁 <b id="pg-picked">全部认识的</b>
                <em>勾上就只找勾的人；一个不勾 = 按「只找认识的人」自己挑</em></div>
              <div class="pickside">
                <input id="pg-search" class="picksearch" placeholder="搜名字或 UID" spellcheck="false">
                <button class="mini2" id="pg-all" type="button">全选</button>
                <button class="mini2" id="pg-none" type="button">清空</button>
              </div>
            </div>
            <div class="picklist" id="pg-list"></div>
          </div>

          <div class="drfreq off" id="pg-idlebox">
            <div class="drlabel">多久没说话算想起他 <b>静了 <span id="pg-idle">3 小时</span></b>
              <em>最小 5 秒。想马上看效果就填 10 秒，但要连同「同一个人两次之间」一起调小</em></div>
            <input type="range" id="pg-idler" min="5" max="86400" step="5" value="10800">
            <input type="number" id="pg-idlen" class="drnum" min="5" max="604800" step="5" value="10800"
                   style="width:120px;max-width:120px">
          </div>

          <div class="drfreq off" id="pg-gapbox">
            <div class="drlabel">同一个人两次之间 <b>至少 <span id="pg-gap">300</span> 分钟</b></div>
            <input type="range" id="pg-gapr" min="0" max="1440" step="30" value="300">
            <input type="number" id="pg-gapn" class="drnum" min="0" max="10080" step="30" value="300">
          </div>

          <div class="drfreq off" id="pg-quotabox">
            <div class="drlabel">每天最多找他 <b><span id="pg-daily">2</span> 次</b></div>
            <input type="range" id="pg-dailyr" min="1" max="10" step="1" value="2">
            <input type="number" id="pg-dailyn" class="drnum" min="1" max="20" step="1" value="2">
          </div>

          <div class="drfreq off" id="pg-everybox">
            <div class="drlabel">全局节奏 <b>两次之间至少隔 <span id="pg-every">25</span> 分钟</b>
              <em>认识的人再多，也不会一口气挨个找一遍</em></div>
            <input type="number" id="pg-everyn" class="drnum" min="1" max="1440" step="5" value="25"
                   style="width:120px;max-width:120px">
          </div>

          <div class="drfreq off" id="pg-winbox">
            <div class="drlabel">只在 <b><span id="pg-win">8 - 23</span> 点</b> 之间开口
              <em>跨零点也支持，比如 22 到 2</em></div>
            <div style="display:flex;align-items:center;gap:10px">
              <input type="number" id="pg-h1" class="drnum" min="0" max="23" step="1" value="8"
                     style="width:96px;max-width:96px;flex:none">
              <span style="font-size:12px;opacity:.6">到</span>
              <input type="number" id="pg-h2" class="drnum" min="0" max="23" step="1" value="23"
                     style="width:96px;max-width:96px;flex:none">
            </div>
          </div>

          <div class="bpbox off" id="pg-promptbox">
            <div class="drlabel">想让它往哪个方向说 <em>留空 = 按人设、档案和当前时间自己想</em></div>
            <textarea id="pg-prompt" rows="3" spellcheck="false"
              placeholder="例如：问问他在忙什么 / 说说你今天遇到的小事 / 撒个娇让他陪你聊两句"></textarea>
          </div>

          <div style="margin-top:14px">
            <div class="drlabel">最近主动 <em>标了「被晾」的是他还没回</em></div>
            <div id="pg-log" style="display:flex;flex-direction:column;gap:6px;margin-top:8px"></div>
          </div>

          <div class="drhint">
            <span id="pg-msg">—</span>
            <span class="sp"></span>
            <span id="pg-stat"></span>
          </div>
          <div class="drhint">
            <span>连着几次没人回就自动搁置一段时间，不会追着喊。改完约 2 秒生效。</span>
          </div>
        </div>
      </section>

      <!-- 私聊指令：授权的人在私聊里让它去群里发话/发图 -->
      <section class="card slot" id="slot-m" data-tab="talk">
        <h2>私聊指令 <span class="count" id="ct-state">读取中…</span></h2>
        <div class="slotbox">
          <div class="drrow">
            <label class="sw">
              <input type="checkbox" id="ct-on" aria-label="私聊指令开关">
              <span class="track"><span class="knob"></span></span>
            </label>
            <div class="drtxt">
              <b id="ct-title">关闭中</b>
              <span>私聊直接说人话：「在主教群说一句我错了」「去群里冒个泡」「把这张图发到主教群」，它听懂了就自己去办；也可以打「/send 群名 内容」这类指令。</span>
            </div>
          </div>

          <div class="ctbox off" id="ct-box">
            <div class="drlabel">听懂人话 <em>不打斜杠也行，说「在主教群说一句我马上到」它就照办</em></div>
            <label class="chk"><input type="checkbox" id="ct-nl">
              让它自己理解意思（关掉就只认斜杠指令）</label>

            <div class="drlabel" style="margin-top:12px">指令前缀 <b id="ct-pre-label">/</b> <em>改完立刻生效，发的和它认的要一致</em></div>
            <input id="ct-pre" class="ctpre" maxlength="3" value="/" spellcheck="false">

            <div class="drlabel" style="margin-top:12px">谁能用 <b><span id="ct-count">0</span> 人</b> <em>不填就谁都用不了</em></div>
            <div id="ct-uids" class="ctuids"></div>
            <div class="ctadd">
              <input id="ct-uid" inputmode="numeric" placeholder="对方的 UID">
              <button id="ct-add" class="ghost">＋ 加入</button>
            </div>

            <div class="drlabel" style="margin-top:12px">最近私聊过它的 <em>点一下直接加进白名单</em></div>
            <div id="ct-recent" class="ctrecent"></div>

            <div class="drhint">
              <span id="ct-msg">—</span>
              <span class="sp"></span>
              <span id="ct-cmds"></span>
            </div>
            <pre class="cthelp" id="ct-help"></pre>
          </div>
        </div>
      </section>

      <!-- 好感度：它心里给每个人记的分 -->
      <section class="card" data-tab="aff">
        <h2>好感度 <span class="count" id="aff-state">读取中…</span></h2>
        <div class="mempad">
          <div class="lb">50 分是「眼熟」，起步都在这儿。常跟它说话、接过它的话会往上走；
            一周不联系自己回落一半，最低也不是恨——它不记仇。</div>
          <div class="afflist" id="afflist"><div class="empty">读取中…</div></div>
          <div class="lb">这一项只影响它说话的松紧：亲近的人它不那么客气，陌生的人它句句「您」。
            分数本身不会说出口。</div>
        </div>
      </section>

      <!-- 表情包：认表情、存表情、按记忆用表情 -->
      <section class="card slot" id="slot-b" data-tab="media">
        <h2>表情包 <span class="count" id="em-state">读取中…</span></h2>
        <div class="slotbox">
          <div class="drrow">
            <label class="sw">
              <input type="checkbox" id="em-on" aria-label="表情开关">
              <span class="track"><span class="knob"></span></span>
            </label>
            <div class="drtxt">
              <b id="em-title">关闭中</b>
              <span>开启后认得群里的表情：小表情（像 [cube_鳄鱼]）记下用法，大表情自动收进收藏夹，回话时按频率用。</span>
            </div>
          </div>

          <div class="drfreq" id="em-freqbox">
            <div class="drlabel">用表情的频率 <b><span id="em-rate">20</span>%</b></div>
            <input type="range" id="em-range" min="0" max="100" step="1" value="20">
            <input type="number" id="em-num" min="0" max="100" step="1" value="20">
          </div>

          <label class="chk emchk"><input type="checkbox" id="em-auto"> 自动收藏群里的大表情</label>

          <div class="emrow">
            <span class="lb">收藏上限 <b><span id="em-maxshow">60</span> 张</b></span>
            <input type="number" id="em-maxnum" min="8" max="200" step="1" value="60">
            <span class="emfile">收满了就先淘汰最少用、最久没露面的</span>
          </div>

          <div class="emsec">
            <div class="emlb">小表情 <b id="em-scount">0</b> 种<span>认得的，点 × 可以忘掉</span></div>
            <div class="emicons" id="em-small"><span class="emempty">还没认出小表情</span></div>
          </div>

          <div class="emsec">
            <div class="emlb">表情包收藏 <b id="em-bcount">0</b> 张<span>点 × 删掉，点图可改备注</span></div>
            <div class="emicons big" id="em-big"><span class="emempty">还没有收藏，等群里有人发表情</span></div>
            <div class="emadd">
              <input id="em-file" type="file" accept="image/*" multiple
                     style="position:absolute;left:-9999px;width:1px;height:1px;opacity:0">
              <button id="em-pick" class="ghost">＋ 从电脑选图</button>
              <span class="emfile" id="em-pickname">本机图片，10MB 内，可多选</span>
            </div>
            <div class="emadd">
              <input id="em-url" placeholder="也可以粘贴表情图地址（http…）" spellcheck="false">
              <button id="em-add" class="ghost">收藏地址</button>
            </div>
          </div>

          <div class="drhint">
            <span id="em-msg">—</span>
            <span class="sp"></span>
            <span>只在被 @ 或私聊时才会用</span>
          </div>
        </div>
      </section>

      <!-- 知识库：导入 md/txt，按人物卡勾选启用 -->
      <section class="card slot" id="slot-k" data-tab="media">
        <h2>知识库 <span class="count" id="kb-state">读取中…</span></h2>
        <div class="slotbox">
          <div class="drrow">
            <div class="drtxt" style="margin-left:0">
              <b>给它备点资料</b>
              <span>导入 .md / .txt（角色设定、世界观、商品资料、话术都行）。勾上的资料会跟着人设一起进它的提示词，问到相关的事就照这个说。</span>
            </div>
          </div>

          <div class="kbup" id="kb-drop">
            <input id="kb-file" type="file" accept=".md,.txt,.markdown,text/markdown,text/plain" multiple
                   style="position:absolute;left:-9999px;width:1px;height:1px;opacity:0">
            <button id="kb-pick" class="ghost">＋ 导入文件</button>
            <span class="kbhint">或把 .md / .txt 拖到这块里，可多选</span>
          </div>

          <div class="kblist" id="kb-list"><div class="empty">还没有资料</div></div>

          <div class="drhint">
            <span id="kb-msg">—</span>
            <span class="sp"></span>
            <span id="kb-budget"></span>
          </div>
          <div class="drhint">
            <span>知识库跟着人物卡走：新卡片默认全部关闭，切回旧卡片还是你上次勾的样子。</span>
          </div>
        </div>
      </section>

      <section class="card" data-tab="status">
        <h2>运行日志 <span class="count">最近 60 行</span></h2>
        <div class="logbox" id="log"><div class="empty">—</div></div>
      </section>
    </aside>
  </div>

  <footer>
    <span id="f-self">账号：--</span>
    <span id="f-port">服务：127.0.0.1:8765</span>
    <span>本页可改人设、群记忆与群友档案，不会代替机器人发言</span>
  </footer>
</div>

<div class="modal" id="setmodal">
  <div class="modal-mask" id="setmask"></div>
  <div class="modal-box" role="dialog" aria-label="设置">
    <div class="modal-head">
      <h3>设置</h3>
      <button class="modal-x" id="setclose" title="关闭">✕</button>
    </div>
    <div class="modal-body">
      <label class="fld">
        <span class="lb">API Key<em>DeepSeek 的调用凭证</em></span>
        <span class="ipt">
          <input id="set-token" type="password" autocomplete="off" spellcheck="false" placeholder="sk-...">
          <button class="eye" id="seteye" type="button" title="显示或隐藏">显示</button>
        </span>
      </label>
      <div class="hint">还没有 Key？到 <a href="https://platform.deepseek.com/api_keys" target="_blank" rel="noopener">DeepSeek 控制台</a> 新建一个，复制到上面。保存后机器人会自动读取，不用重启。</div>

      <label class="fld">
        <span class="lb">接口地址<em>换中转服务时才要改</em></span>
        <input id="set-url" type="text" autocomplete="off" spellcheck="false" placeholder="https://api.deepseek.com/v1/chat/completions">
      </label>

      <label class="fld">
        <span class="lb">模型<em>默认 deepseek-chat</em></span>
        <input id="set-model" type="text" autocomplete="off" spellcheck="false" placeholder="deepseek-chat">
      </label>

      <label class="fld">
        <span class="lb">群聊单条上限<em>只是天花板，不是目标长度；长短还是人设自己在管</em></span>
        <input id="set-chars" type="number" min="60" max="600" step="10" placeholder="280">
      </label>

      <label class="fld">
        <span class="lb">私聊单条上限<em>私聊不刷屏，可以给宽一点</em></span>
        <input id="set-dmchars" type="number" min="60" max="2000" step="50" placeholder="800">
      </label>

      <label class="fld">
        <span class="lb">语感闸门<em>开口前跟她最近说过的原话比一遍：开头撞了、整句太像，就打回重说一次（只打回一次）</em></span>
        <span class="ipt">
          <label class="sw">
            <input type="checkbox" id="set-stylegate" aria-label="语感闸门开关">
            <span class="track"><span class="knob"></span></span>
          </label>
        </span>
      </label>

      <div class="sep"><span>识图模型（可留空）</span></div>

      <label class="fld">
        <span class="lb">识图模型名<em>留空则复用上面的模型</em></span>
        <input id="set-vmodel" type="text" autocomplete="off" spellcheck="false" placeholder="留空 = deepseek-chat">
      </label>

      <label class="fld">
        <span class="lb">识图接口地址<em>留空则复用上面的接口地址</em></span>
        <input id="set-vurl" type="text" autocomplete="off" spellcheck="false" placeholder="留空 = 复用主接口地址">
      </label>

      <label class="fld">
        <span class="lb">识图 Key<em>留空则复用上面的 API Key</em></span>
        <span class="ipt">
          <input id="set-vtoken" type="password" autocomplete="off" spellcheck="false" placeholder="留空 = 复用主 Key">
          <button class="eye" id="setveye" type="button" title="显示或隐藏">显示</button>
        </span>
      </label>
      <div class="hint">识图模型专门负责「看梗图接梗」；三个都留空就复用上面的主配置，不用单独填。</div>

      <div class="sep"><span>联网搜索（可留空）</span></div>

      <label class="fld">
        <span class="lb">联网搜索<em>打开后，群友问「现在怎么样了」这类要看最新信息的事，它会先查再答</em></span>
        <span class="ipt">
          <label class="sw">
            <input type="checkbox" id="set-search" aria-label="联网搜索开关">
            <span class="track"><span class="knob"></span></span>
          </label>
        </span>
      </label>

      <label class="fld">
        <span class="lb">搜索 Key<em>博查（BoCha）等搜索服务的 Key，留空则不会真正去搜</em></span>
        <span class="ipt">
          <input id="set-skey" type="password" autocomplete="off" spellcheck="false" placeholder="搜索服务 Key">
          <button class="eye" id="setseye" type="button" title="显示或隐藏">显示</button>
        </span>
      </label>

      <label class="fld">
        <span class="lb">搜索接口地址<em>默认博查，换 Tavily 等其他服务时才改</em></span>
        <input id="set-surl" type="text" autocomplete="off" spellcheck="false" placeholder="https://api.bocha.cn/v1/web-search">
      </label>
      <div class="hint">没有搜索 Key？到 <a href="https://open.bocha.cn/" target="_blank" rel="noopener">博查开放平台</a> 注册并领取免费额度（新用户约 1000 次），在「API Key 管理」里创建一个 Key 填到上面。</div>

      <div class="sep"><span>上下文和思考</span></div>

      <label class="fld">
        <span class="lb">独立会话 + agent loop<em>关着：拼最近几轮 + 人设 + 档案 + 日记，一次生成；开着：每个人一条自己的对话线，先去翻档案 / 日记 / 设定，再开口</em></span>
        <span class="ipt">
          <label class="sw">
            <input type="checkbox" id="set-loop" aria-label="独立会话与 agent loop">
            <span class="track"><span class="knob"></span></span>
          </label>
        </span>
      </label>

      <label class="fld">
        <span class="lb">最多查几步<em>1-4；每多一步就多一次调用。默认 3 够用</em></span>
        <input id="set-loopsteps" type="number" min="1" max="4" step="1" placeholder="3">
      </label>

      <label class="fld">
        <span class="lb">每人记几轮<em>独立会话留多长，2-20 轮</em></span>
        <input id="set-peerrounds" type="number" min="2" max="20" step="1" placeholder="8">
      </label>
      <div class="hint">关着时它把最近几轮对话、人设、群友档案、日记、设定资料一次塞进提示词，一次生成（大约 7k 上下文）。开着改成「每个人一条线 + 先查再答」：固定上下文只剩人设和当前这个人，其余按需去翻——想不起来才查，日常闲聊不查。代价是每次回复可能多花 1-2 次调用，建议先在私聊试几句。</div>

      <div class="sep"><span>心情</span></div>

      <label class="fld">
        <span class="lb">心情系统<em>关掉后不再判情绪，它每句话都按人设平着说</em></span>
        <span class="ipt">
          <label class="sw">
            <input type="checkbox" id="set-mood" aria-label="心情开关">
            <span class="track"><span class="knob"></span></span>
          </label>
        </span>
      </label>

      <label class="fld">
        <span class="lb">心情基线<em>没人理时最终回落到这里</em></span>
        <input id="set-moodbase" type="number" min="0" max="100" step="5" placeholder="70">
      </label>

      <label class="fld">
        <span class="lb">平复速度<em>心情往基线走一半要多久（小时）</em></span>
        <input id="set-moodcalm" type="number" min="0.1" max="72" step="0.5" placeholder="3">
      </label>

      <label class="fld">
        <span class="lb">情绪消散<em>开心、担心这些具体情绪散掉一半要多久（分钟）</em></span>
        <input id="set-moodemo" type="number" min="1" max="1440" step="10" placeholder="90">
      </label>
      <div class="hint">基线 70 = 平静偏开朗（想让它更容易闷，往下调；更爱笑，往上调）。默认 3 小时 / 90 分钟的意思是：被逗乐之后没人理，三小时后剩一半，一夜过去基本回到平静。保存后即时生效，不用重启。</div>

      <label class="fld">
        <span class="lb">按轮数触发<em>它自己说了几句，才值得判一次</em></span>
        <span class="ipt">
          <label class="sw">
            <input type="checkbox" id="set-moodturn" aria-label="按轮数触发">
            <span class="track"><span class="knob"></span></span>
          </label>
        </span>
      </label>

      <label class="fld">
        <span class="lb">每几轮判一次<em>1 = 每句都判（最贵）；3 = 说三句判一次</em></span>
        <input id="set-moodevery" type="number" min="1" max="50" step="1" placeholder="3">
      </label>

      <label class="fld">
        <span class="lb">按时间触发<em>隔太久没判过，也该补一次</em></span>
        <span class="ipt">
          <label class="sw">
            <input type="checkbox" id="set-moodtime" aria-label="按时间触发">
            <span class="track"><span class="knob"></span></span>
          </label>
        </span>
      </label>

      <label class="fld">
        <span class="lb">至少隔几分钟<em>刚判过就再来一句，不重复判（0 = 不限）</em></span>
        <input id="set-moodgap" type="number" min="0" max="240" step="1" placeholder="2">
      </label>
      <div class="hint" id="moodratehint">两个闸都开着 = 轮数和时间都到了才判。</div>

      <div class="sep"><span>真·思考（会多花钱）</span></div>

      <label class="fld">
        <span class="lb">打开真思考<em>换 deepseek-reasoner，先把事情想一遍再说话，想的过程面板里能看到</em></span>
        <span class="ipt">
          <label class="sw">
            <input type="checkbox" id="set-think" aria-label="真思考开关">
            <span class="track"><span class="knob"></span></span>
          </label>
        </span>
      </label>

      <label class="fld">
        <span class="lb">什么时候想<em>「只在值得想的时候」= 长句、问为什么/怎么办、被追问翻旧账</em></span>
        <select id="set-thinkmode">
          <option value="auto">只在值得想的时候（省钱）</option>
          <option value="always">每一句都想（最贵）</option>
        </select>
      </label>

      <label class="fld">
        <span class="lb">每小时最多想几次<em>硬闸，用完这一小时就自动退回普通模型；0 = 不限</em></span>
        <input id="set-thinkcap" type="number" min="0" max="999" step="5" placeholder="20">
      </label>
      <div class="hint" id="set-thinkhint">reasoner 的价格约为普通模型的两倍，而且「想」出来的字也算输出计费，所以默认是关的。开之前建议先按「只在值得想的时候 + 每小时 20 次」跑一天，看面板里它想得值不值，再决定要不要调大。</div>

      <div class="sep"><span>离线补回</span></div>

      <label class="fld">
        <span class="lb">关机后补回<em>电脑关了这段时间有人找过它，开机后接一句</em></span>
        <span class="ipt">
          <label class="sw">
            <input type="checkbox" id="set-catchup" aria-label="离线补回开关">
            <span class="track"><span class="knob"></span></span>
          </label>
        </span>
      </label>

      <label class="fld">
        <span class="lb">离开多久才算<em>短到只是重启一下，就不补</em></span>
        <input id="set-catchafter" type="number" min="1" max="720" step="1" placeholder="5">
      </label>

      <label class="fld">
        <span class="lb">最多补到多久前<em>更早的消息就不追了</em></span>
        <input id="set-catchmax" type="number" min="1" max="168" step="1" placeholder="12">
      </label>
      <div class="hint">单位：上面是分钟，下面是小时。补的时候每个会话只接最后一条——私聊接对方最后那句，群里只接 @ 过它的话，不会把积压的消息一次性全回。机器人默认会说「你刚回来」，但不会提关机本身。</div>

      <div class="sep"><span>关机前收尾（写日记 + 总观）</span></div>

      <label class="fld">
        <span class="lb">停之前先收尾<em>点停止时给它点时间：把没记的聊记进日记，再把日记收成一段总观</em></span>
        <span class="ipt">
          <label class="sw">
            <input type="checkbox" id="set-farewell" aria-label="关机收尾开关">
            <span class="track"><span class="knob"></span></span>
          </label>
        </span>
      </label>

      <label class="fld">
        <span class="lb">给它多少秒<em>倒计时走完还没写完就直接停，不会拖着关不掉</em></span>
        <input id="set-farewellsec" type="number" min="15" max="600" step="15" placeholder="90">
      </label>

      <div class="fld">
        <span class="lb">写什么<em>两个可以分开关</em></span>
        <span class="ipt" style="display:flex;gap:16px">
          <label class="chk"><input type="checkbox" id="set-farewelldiary"> 日记</label>
          <label class="chk"><input type="checkbox" id="set-farewellview"> 总观</label>
        </span>
      </div>

      <label class="fld">
        <span class="lb">最多写几个会话<em>挑最近说过话的；填 0 就只写总观</em></span>
        <input id="set-farewellgroups" type="number" min="0" max="10" step="1" placeholder="3">
      </label>
      <div class="hint">点停止后不会立刻断开：顶上出现倒计时，它按会话写日记、再把日记收成一段总观，写完提前退；时间到还没写完也照样停，不会关不掉。关掉这个开关就恢复成「点停止立即断开」。</div>

      <div class="msg" id="setmsg"></div>
    </div>
    <div class="modal-foot">
      <button class="btn2" id="settest">测试连接</button>
      <div class="sp"></div>
      <button class="btn2" id="setcancel">取消</button>
      <button class="btn2 primary" id="setsave">保存</button>
    </div>
  </div>
</div>

<div class="modal" id="spendmodal">
  <div class="modal-mask" id="spendmask"></div>
  <div class="modal-box spend-box" role="dialog" aria-label="消耗">
    <div class="modal-head">
      <h3>钱花在哪了</h3>
      <button class="modal-x" id="spendclose" title="关闭">✕</button>
    </div>
    <div class="modal-body">
      <div class="spend-ranges" id="spendranges">
        <button data-h="24" class="on">24 小时</button>
        <button data-h="72">3 天</button>
        <button data-h="168">7 天</button>
        <button data-h="720">30 天</button>
        <button class="mini" id="spendrefresh" title="现在重新算一遍">↻</button>
      </div>
      <div class="spend-head">
        <div class="spend-big"><b id="spendamount">--</b><span id="spendbasis">—</span></div>
        <div class="spend-kv">
          <div><em>当前余额</em><b id="spendbal">--</b></div>
          <div><em>调用</em><b id="spendcalls">--</b></div>
          <div><em>token</em><b id="spendtok">--</b></div>
        </div>
      </div>
      <div class="spend-chartwrap">
        <div class="spend-legend"><i class="lg"></i>累计花费<i class="lg lg-bar"></i>每时段花费</div>
        <canvas id="spendcv" height="190"></canvas>
        <div class="spend-tip" id="spendtip" hidden></div>
      </div>
      <div class="spend-sub"><span>花在哪</span><em>点一行看细账</em></div>
      <div class="spend-scenes" id="spendscenes"><div class="empty">读取中…</div></div>
      <div class="spend-sub">最近调用</div>
      <div class="spend-recent" id="spendrecent"></div>
    </div>
  </div>
</div>

<script>
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const $ = id => document.getElementById(id);

/* 右栏分区切换：人格 / 说话 / 素材 / 状态 */
(function(){
  var box = $('sidetabs'); if(!box) return;
  var KEY = 'sideTab';
  var btns = [].slice.call(box.querySelectorAll('button[data-tab]'));
  var cards = [].slice.call(document.querySelectorAll('aside > .card[data-tab]'));
  var names = btns.map(function(b){ return b.getAttribute('data-tab'); });
  // 徽标按实际卡片数生成，以后增删卡片不用手改数字
  btns.forEach(function(b){
    var t = b.getAttribute('data-tab');
    var tag = document.createElement('i');
    tag.textContent = cards.filter(function(c){ return c.getAttribute('data-tab') === t; }).length;
    b.appendChild(tag);
  });
  function apply(name, jump){
    if(names.indexOf(name) < 0) name = names[0];
    btns.forEach(function(b){
      var on = b.getAttribute('data-tab') === name;
      b.classList.toggle('on', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    cards.forEach(function(c){
      c.classList.toggle('on', c.getAttribute('data-tab') === name);
    });
    try{ localStorage.setItem(KEY, name); }catch(e){}
    // 右栏现在是独立滚动区：换组就回到顶部，否则从上一组滚到一半的位置切过去会停在半空
    var side = document.querySelector('.grid > aside');
    if(side) side.scrollTop = 0;
  }
  box.addEventListener('click', function(e){
    var b = e.target.closest('button[data-tab]');
    if(b) apply(b.getAttribute('data-tab'), true);
  });
  box.addEventListener('keydown', function(e){
    var d = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
    if(!d) return;
    var i = btns.findIndex(function(b){ return b.classList.contains('on'); });
    var n = (i + d + btns.length) % btns.length;
    btns[n].focus(); apply(names[n], true); e.preventDefault();
  });
  /* 右栏高度 = 视口里它实际能占到的高度。
     直接写 100vh 会让它在页面顶部时溢出视口（上方还有统计卡），底部看不见。 */
  function docTop(el){
    var y = 0;
    while(el){ y += el.offsetTop; el = el.offsetParent; }
    return y;
  }
  function fitAside(){
    var side = document.querySelector('.grid > aside');
    var grid = document.querySelector('.grid');
    if(!side || !grid) return;
    if(window.innerWidth <= 960){ side.style.maxHeight = ''; return; }
    var tabs = side.querySelector('.sidetabs');
    if(tabs) side.style.setProperty('--stab', tabs.offsetHeight + 'px');   // 卡标题吸顶要让开页签
    var top = docTop(grid) + 12;                    // 右栏贴住视口顶时的 y
    var h = window.innerHeight - top - 8;
    side.style.maxHeight = Math.max(320, h) + 'px';
  }
  window.addEventListener('resize', fitAside);
  window.addEventListener('load', fitAside);
  setTimeout(fitAside, 0);
  setTimeout(fitAside, 300);   // 字体、统计卡换行稳定后再量一次

  /* ── 右栏卡片折叠 ──────────────────────────────────────────────
     右栏一屏就四百来像素，一张功能卡动辄七八百，全展开的话
     光滚一个功能就得滚两屏。这里让每页默认只展开第一张，其余收成
     一行标题（标题右侧带状态），点标题随时展开，看完再点收起。 */
  var FOLD_KEY = 'wb-aside-fold-v1';
  var foldState = {};
  try{ foldState = JSON.parse(localStorage.getItem(FOLD_KEY) || '{}') || {}; }catch(e){ foldState = {}; }

  function sideCards(){ return [].slice.call(document.querySelectorAll('.grid>aside>.card')); }
  function foldKey(c, i){ return c.id || ((c.getAttribute('data-tab') || 'x') + '#' + i); }
  function saveFold(){ try{ localStorage.setItem(FOLD_KEY, JSON.stringify(foldState)); }catch(e){} }

  function paintFold(){
    sideCards().forEach(function(c, i){
      var h = c.querySelector('h2');
      if(!h) return;
      if(!h.querySelector('.fold')){
        var s = document.createElement('span');
        s.className = 'fold';
        s.textContent = '\u25BE';
        h.appendChild(s);
      }
      if(!h.title) h.title = '点标题收起 / 展开，一次专心看一个功能';
      c.classList.toggle('folded', foldState[foldKey(c, i)] === 0);
    });
  }

  function initFold(){
    var seen = {};
    sideCards().forEach(function(c, i){
      var h = c.querySelector('h2');
      if(!h) return;
      var tab = c.getAttribute('data-tab') || 'x';
      var k = foldKey(c, i);
      if(!(k in foldState)) foldState[k] = seen[tab] ? 0 : 1;   // 每页只默认展开第一张
      seen[tab] = 1;
    });
    paintFold();
    saveFold();
  }

  document.addEventListener('click', function(e){
    var h = e.target && e.target.closest ? e.target.closest('.grid>aside>.card>h2') : null;
    if(!h) return;
    var c = h.parentNode, i = sideCards().indexOf(c);
    if(i < 0 || !c.offsetParent) return;   // 别的页签里藏着的卡不响应
    var wasFolded = c.classList.contains('folded');
    if(wasFolded){          // 同一个页签里只摊开一张，免得又得滚着找
      var tab = c.getAttribute('data-tab');
      sideCards().forEach(function(o, j){
        if(o !== c && o.getAttribute('data-tab') === tab){
          o.classList.add('folded');
          foldState[foldKey(o, j)] = 0;
        }
      });
    }
    foldState[foldKey(c, i)] = wasFolded ? 1 : 0;
    c.classList.toggle('folded', !wasFolded);
    saveFold();
    if(wasFolded){        // 刚展开：把这张卡顶到右栏最上面，省得自己再往下找
      var side = c.parentNode;
      var tabs = side.querySelector('.sidetabs');
      var pad = tabs ? tabs.offsetHeight : 0;      // 让开上面贴在页签栏的那一截
      var off = side.scrollTop + (c.getBoundingClientRect().top - side.getBoundingClientRect().top) - pad;
      off = Math.max(0, off);
      if(side.scrollTo) side.scrollTo({top: off, behavior: 'smooth'});
      else side.scrollTop = off;
    }
  });

  initFold();

  var saved = null;
  try{ saved = localStorage.getItem(KEY); }catch(e){}
  apply(saved || 'persona', false);
})();


/* ---------------- 首次运行引导 ---------------- */
let sStep = 1, sData = {};
const SMAX = 3;

function sShow(step){
  sStep = step;
  document.querySelectorAll('.spanel').forEach(p => {
    p.hidden = Number(p.dataset.step) !== step;
  });
  document.querySelectorAll('.sdots i').forEach((d, i) => d.classList.toggle('on', i < step));
  $('sback').style.visibility = step === 1 ? 'hidden' : 'visible';
  $('snext').textContent = step === SMAX ? '完成，开始使用' : '下一步';
  const t = {
    1: ['先把这个工具接到你的客户端上',
        '它不是官方机器人，也不用 token —— 它借你电脑上已登录的黑盒语音客户端收发消息，走的就是你自己的账号。'],
    2: ['填一个模型 Key',
        '所有回复都由大模型现写，这里填你自己的 API Key，只保存在这台电脑上。'],
    3: ['最后一步：它怎么接话',
        '起个名字，让群里的人知道该怎么叫它。这个名字也会写进人设，之后随时能改。']
  }[step] || ['', ''];
  $('sh').textContent = t[0];
  $('slead').textContent = t[1];
}

function sStat(msg, cls){
  const el = $('sstat');
  el.textContent = msg || '';
  el.className = 'sstat' + (cls ? ' ' + cls : '');
}

function sClientState(st){
  const el = $('sclient-state');
  if(st.clientOnline){
    el.className = 'sstate ok';
    el.textContent = '✓ 客户端已连上（调试端口 ' + st.port + ' 在线），这一步不用再管了。';
  }else if(st.clientRunning){
    el.className = 'sstate warn';
    el.textContent = '客户端正在运行，但没开调试端口。\n要先把客户端完全退出（托盘图标右键 → 退出），再点下面的按钮启动。';
  }else{
    el.className = 'sstate warn';
    el.textContent = '客户端没在运行。确认下面的路径，再点「用调试模式启动客户端」。';
  }
}

async function loadSetup(){
  let st = {};
  try{ st = await fetch('/api/setup/state', {cache: 'no-store'}).then(r => r.json()); }catch(e){}
  sData = st || {};
  if(!sData.needSetup){ $('setup').classList.remove('on'); return; }
  $('setup').classList.add('on');
  // 还没配好之前，背后那套面板没有意义，藏起来免得干扰
  const w = document.querySelector('.wrap');
  if(w) w.style.visibility = 'hidden';
  sClientState(sData);
  if(sData.candidates && sData.candidates.length){
    $('sclient').value = sData.clientPath || sData.candidates[0];
    $('scands').innerHTML = sData.candidates.map(p =>
      '<span class="cand" data-p="' + esc(p) + '">' + esc(p.split('\\').slice(-2).join('\\')) + '</span>'
    ).join('');
    $('scands').querySelectorAll('.cand').forEach(c => {
      c.onclick = () => { $('sclient').value = c.getAttribute('data-p'); };
    });
  }else if(sData.clientPath){
    $('sclient').value = sData.clientPath;
  }
  if(sData.prefix) $('sprefix').value = sData.prefix;
  sShow(1);
}

async function sNext(){
  if(sStep === 1){
    if(!sData.clientOnline && !$('sclient').value.trim()){
      sStat('还没连上客户端，也没选路径。', 'err');
      return;
    }
    sStat(''); sShow(2); return;
  }
  if(sStep === 2){
    if($('stoken').value.trim().length < 12){ sStat('Key 看起来不完整。', 'err'); return; }
    sStat(''); sShow(3); return;
  }
  const btn = $('snext');
  btn.disabled = true; btn.textContent = '正在保存…';
  try{
    const r = await fetch('/api/setup/save', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        clientPath: $('sclient').value.trim(),
        token: $('stoken').value.trim(),
        model: $('smodel').value,
        prefix: $('sprefix').value.trim() || '小菲',
        directReply: $('sdirect').checked
      })
    }).then(x => x.json());
    if(!r.ok) throw new Error(r.err || '保存失败');
    btn.textContent = '正在启动机器人…';
    const s = await fetch('/api/bot/start', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
    }).then(x => x.json());
    if(!s.ok){
      sStat('配置存好了，但机器人没起来：' + (s.err || '') + (s.detail ? '\n' + s.detail : ''), 'err');
      btn.disabled = false; btn.textContent = '重试启动';
      return;
    }
    sStat('好了，正在进入面板…', 'ok');
    setTimeout(() => location.reload(), 900);
  }catch(e){
    sStat(e.message, 'err');
    btn.disabled = false; btn.textContent = '完成，开始使用';
  }
}

function setupBind(){
  $('snext').onclick = sNext;
  $('sback').onclick = () => sShow(Math.max(1, sStep - 1));
  $('sbrowse').onclick = async () => {
    sStat('正在打开文件选择框…');
    try{
      const r = await fetch('/api/setup/browse', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
      }).then(x => x.json());
      if(r.path){
        $('sclient').value = r.path;
        sStat('已选择：' + r.path, 'ok');
      }else sStat('');
    }catch(e){ sStat('打开失败：' + e.message, 'err'); }
  };
  $('slaunch').onclick = async () => {
    const b = $('slaunch');
    b.disabled = true; b.textContent = '正在启动，最多等 30 秒…';
    const el = $('slaunch-st');
    el.className = 'sstate'; el.textContent = '';
    try{
      const r = await fetch('/api/setup/launch', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path: $('sclient').value.trim()})
      }).then(x => x.json());
      el.className = 'sstate ' + (r.ok ? 'ok' : 'err');
      el.textContent = r.ok ? (r.msg || '已启动') : (r.err || '启动失败');
      if(r.ok){ sData.clientOnline = true; sClientState(sData); }
    }catch(e){
      el.className = 'sstate err'; el.textContent = '启动失败：' + e.message;
    }
    b.disabled = false; b.textContent = '用调试模式启动客户端';
  };
  $('seye').onclick = () => {
    const t = $('stoken');
    t.type = t.type === 'password' ? 'text' : 'password';
    $('seye').textContent = t.type === 'password' ? '显示' : '隐藏';
  };
  $('stestai').onclick = async () => {
    const b = $('stestai'), el = $('saistat');
    b.disabled = true; b.textContent = '测试中…';
    el.className = 'sstate'; el.textContent = '';
    try{
      const r = await fetch('/api/setup/testai', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({token: $('stoken').value.trim(), model: $('smodel').value})
      }).then(x => x.json());
      el.className = 'sstate ' + (r.ok ? 'ok' : 'err');
      el.textContent = r.ok ? ('✓ ' + (r.msg || '可以用')) : (r.err || '不能用');
    }catch(e){
      el.className = 'sstate err'; el.textContent = e.message;
    }
    b.disabled = false; b.textContent = '测试能不能用';
  };
}

/* 轻提示：独立于 banner，避免被每 2 秒的状态刷新冲掉 */
let toastTimer = 0;
function toast(msg){
  let t = $('toast');
  if(!t){
    t = document.createElement('div');
    t.id = 'toast';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 2400);
}

/* ---- 设置弹窗：API Key / 接口地址 / 模型 ---- */
const setModal = $('setmodal');
const setMsgEl = $('setmsg');

function setMsg(text, cls){
  setMsgEl.textContent = text || '';
  setMsgEl.className = 'msg' + (cls ? ' ' + cls : '');
}

function readSetForm(){
  return {
    token: ($('set-token').value || '').trim(),
    baseUrl: ($('set-url').value || '').trim(),
    model: ($('set-model').value || '').trim(),
    maxChars: Number($('set-chars').value || 0),
    dmMaxChars: Number($('set-dmchars').value || 0),
    styleGate: $('set-stylegate').checked,
    visionModel: ($('set-vmodel').value || '').trim(),
    visionBaseUrl: ($('set-vurl').value || '').trim(),
    visionToken: ($('set-vtoken').value || '').trim(),
    webSearch: $('set-search').checked,
    searchKey: ($('set-skey').value || '').trim(),
    searchUrl: ($('set-surl').value || '').trim(),
    mood: $('set-mood').checked,
    agentLoop: $('set-loop').checked,
    loopSteps: ($('set-loopsteps').value || '').trim(),
    peerRounds: ($('set-peerrounds').value || '').trim(),
    moodBase: ($('set-moodbase').value || '').trim(),
    moodCalmHours: ($('set-moodcalm').value || '').trim(),
    moodEmoMinutes: ($('set-moodemo').value || '').trim(),
    moodEvery: ($('set-moodevery').value || '').trim(),
    moodByTurn: $('set-moodturn').checked,
    moodByTime: $('set-moodtime').checked,
    moodGapMinutes: ($('set-moodgap').value || '').trim(),
    think: $('set-think').checked,
    thinkMode: $('set-thinkmode').value || 'auto',
    thinkCap: ($('set-thinkcap').value || '').trim(),
    catchUp: $('set-catchup').checked,
    catchUpAfter: ($('set-catchafter').value || '').trim(),
    catchUpMax: ($('set-catchmax').value || '').trim(),
    farewell: $('set-farewell').checked,
    farewellSeconds: ($('set-farewellsec').value || '').trim(),
    farewellDiary: $('set-farewelldiary').checked,
    farewellOutlook: $('set-farewellview').checked,
    farewellGroups: ($('set-farewellgroups').value || '').trim()
  };
}

async function openSettings(){
  setModal.classList.add('show');
  setMsg('读取中…');
  try{
    const r = await fetch('/api/settings', {cache: 'no-store'});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '读取失败');
    $('set-token').value = d.token || '';
    $('set-url').value = d.baseUrl || '';
    $('set-model').value = d.model || '';
    $('set-chars').value = d.maxChars || 280;
    $('set-dmchars').value = d.dmMaxChars || 800;
    $('set-stylegate').checked = d.styleGate !== false;
    $('set-vmodel').value = d.visionModel || '';
    $('set-vurl').value = d.visionBaseUrl || '';
    $('set-vtoken').value = d.visionToken || '';
    $('set-search').checked = !!d.webSearch;
    $('set-skey').value = d.searchKey || '';
    $('set-surl').value = d.searchUrl || '';
    $('set-mood').checked = !!d.mood;
    $('set-loop').checked = !!d.agentLoop;
    $('set-loopsteps').value = d.loopSteps;
    $('set-peerrounds').value = d.peerRounds;
    $('set-moodbase').value = d.moodBase;
    $('set-moodcalm').value = d.moodCalmHours;
    $('set-moodemo').value = d.moodEmoMinutes;
    $('set-moodevery').value = d.moodEvery;
    $('set-moodturn').checked = d.moodByTurn !== false;
    $('set-moodtime').checked = d.moodByTime !== false;
    $('set-moodgap').value = d.moodGapMinutes;
    paintMoodRate();
    $('set-think').checked = !!d.think;
    $('set-thinkmode').value = d.thinkMode === 'always' ? 'always' : 'auto';
    $('set-thinkcap').value = d.thinkCap;
    const thHint = $('set-thinkhint');
    if(thHint){
      const left = Number(d.thinkLeft) || 0;
      const base = 'reasoner 的价格约为普通模型的两倍，而且「想」出来的字也算输出计费，所以默认是关的。开之前建议先按「只在值得想的时候 + 每小时 20 次」跑一天，看面板里它想得值不值，再决定要不要调大。';
      thHint.textContent = d.think
        ? base + '这一小时还剩 ' + left + ' 次。'
        : base;
    }
    $('set-catchup').checked = !!d.catchUp;
    $('set-catchafter').value = d.catchUpAfter;
    $('set-catchmax').value = d.catchUpMax;
    $('set-farewell').checked = d.farewell !== false;
    $('set-farewellsec').value = d.farewellSeconds || 90;
    $('set-farewelldiary').checked = d.farewellDiary !== false;
    $('set-farewellview').checked = d.farewellOutlook !== false;
    $('set-farewellgroups').value = (d.farewellGroups === 0 ? 0 : (d.farewellGroups || 3));
    $('set-token').type = 'password';
    $('seteye').textContent = '显示';
    $('set-vtoken').type = 'password';
    $('setveye').textContent = '显示';
    $('set-skey').type = 'password';
    $('setseye').textContent = '显示';
    setMsg(d.hasToken ? '当前已配置 Key，改完点保存即可' : '还没配 Key，机器人不会回复', d.hasToken ? 'good' : '');
  }catch(e){
    setMsg('读取失败：' + e.message, 'bad');
  }
}

function closeSettings(){
  setModal.classList.remove('show');
}

async function saveSettings(){
  const btn = $('setsave');
  btn.disabled = true;
  setMsg('保存中…');
  try{
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(readSetForm())
    });
    const d = await r.json();
    if(!d.ok){
      setMsg(d.error || '保存失败', 'bad');
      return;
    }
    closeSettings();
    toast('设置已保存，机器人会自动读取');
    $('balrefresh').click();
  }catch(e){
    setMsg('保存失败：' + e.message, 'bad');
  }finally{
    btn.disabled = false;
  }
}

async function testSettings(){
  const btn = $('settest');
  btn.disabled = true;
  setMsg('正在保存并连接 DeepSeek…');
  try{
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(readSetForm())
    });
    const d = await r.json();
    if(!d.ok){
      setMsg(d.error || '保存失败', 'bad');
      return;
    }
    const r2 = await fetch('/api/balance', {cache: 'no-store'});
    const b = await r2.json();
    if(b.ok){
      const sym = (b.currency === 'USD') ? '$' : '¥';
      setMsg('连接正常，余额 ' + sym + Number(b.total || 0).toFixed(2), 'good');
    }else{
      setMsg('没连上：' + (b.error || '未知错误'), 'bad');
    }
  }catch(e){
    setMsg('没连上：' + e.message, 'bad');
  }finally{
    btn.disabled = false;
  }
}

if(setModal){
  $('btnset').onclick = openSettings;
  $('setclose').onclick = closeSettings;
  $('setcancel').onclick = closeSettings;
  $('setmask').onclick = closeSettings;
  $('setsave').onclick = saveSettings;
  $('settest').onclick = testSettings;
  $('seteye').onclick = () => {
    const el = $('set-token');
    const toText = el.type === 'password';
    el.type = toText ? 'text' : 'password';
    $('seteye').textContent = toText ? '隐藏' : '显示';
  };
  $('setveye').onclick = () => {
    const el = $('set-vtoken');
    const toText = el.type === 'password';
    el.type = toText ? 'text' : 'password';
    $('setveye').textContent = toText ? '隐藏' : '显示';
  };
  $('setseye').onclick = () => {
    const el = $('set-skey');
    const toText = el.type === 'password';
    el.type = toText ? 'text' : 'password';
    $('setseye').textContent = toText ? '隐藏' : '显示';
  };
  document.addEventListener('keydown', e => {
    if(e.key === 'Escape' && setModal.classList.contains('show')) closeSettings();
  });
}

function hhmmss(d){
  const p = n => String(n).padStart(2,'0');
  return p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());
}
function dur(sec){
  if(sec == null || isNaN(sec)) return '--';
  if(sec < 60) return sec + ' 秒';
  const m = Math.floor(sec/60), h = Math.floor(m/60);
  if(h > 0) return h + ' 时 ' + (m%60) + ' 分';
  return m + ' 分 ' + (sec%60) + ' 秒';
}
function msgTime(ms){
  if(!ms) return '';
  const d = new Date(ms);
  return hhmmss(d);
}
function setDot(el, on){
  el.className = 'dot' + (on ? ' on' : ' off');
}
/* ---------- API 余额 ---------- */
function renderBalance(bal){
  const v = $('s-balance'), hint = $('s-balhint');
  if(!v) return;
  if(bal && bal.ok){
    const t = Number(bal.total || 0);
    v.className = 'v' + (t <= 0 ? ' low' : (t < 5 ? ' low' : ''));
    v.innerHTML = '¥' + t.toFixed(2);
    const bits = [];
    bits.push(bal.available === false ? '额度不足，已停' : '可正常调用');
    if(bal.granted) bits.push('赠送 ¥' + Number(bal.granted).toFixed(2));
    if(bal.topped) bits.push('充值 ¥' + Number(bal.topped).toFixed(2));
    const age = Number(bal.age || 0);
    bits.push(bal.cached ? (age < 60 ? Math.round(age) + ' 秒前' : Math.round(age/60) + ' 分钟前') : '刚刚');
    hint.textContent = bits.join(' · ');
    hint.style.color = '';
  }else{
    v.className = 'v bad';
    v.textContent = '读取失败';
    hint.textContent = (bal && bal.error) ? String(bal.error).slice(0, 60) : '未知错误';
    hint.style.color = '#b45309';
  }
}

/* ---------- 消耗曲线（点「API 余额」那张卡打开） ---------- */
const spendModal = $('spendmodal');
let spendHours = 24;
let spendCache = null;

function fmtMoney(v){
  v = Number(v || 0);
  if(v >= 100) return '¥' + v.toFixed(0);
  if(v >= 1) return '¥' + v.toFixed(2);
  if(v >= 0.01) return '¥' + v.toFixed(3);
  return v > 0 ? '¥' + v.toFixed(4) : '¥0';
}
function fmtTok(n){
  n = Number(n || 0);
  if(n >= 1e8) return (n / 1e8).toFixed(2) + ' 亿';
  if(n >= 1e4) return (n / 1e4).toFixed(1) + ' 万';
  return String(n);
}
function fmtHM(t){
  const d = new Date(Number(t || 0) * 1000);
  return p2(d.getHours()) + ':' + p2(d.getMinutes());
}
function fmtMD(t){
  const d = new Date(Number(t || 0) * 1000);
  return (d.getMonth() + 1) + '/' + d.getDate();
}
function openSpend(){
  spendModal.classList.add('show');
  loadSpend(spendHours);
}
function closeSpend(){
  spendModal.classList.remove('show');
  $('spendtip').hidden = true;
}
async function loadSpend(h){
  spendHours = h;
  Array.prototype.forEach.call(
    document.querySelectorAll('#spendranges button[data-h]'),
    function(b){ b.classList.toggle('on', Number(b.dataset.h) === h); });
  $('spendscenes').innerHTML = '<div class="empty">正在算…</div>';
  $('spendrecent').innerHTML = '';
  try{
    const r = await fetch('/api/spend?hours=' + h, {cache: 'no-store'});
    spendCache = await r.json();
  }catch(e){
    $('spendscenes').innerHTML = '<div class="empty">读不到：' + esc(String(e && e.message || e)) + '</div>';
    return;
  }
  paintSpend(spendCache);
}
function paintSpend(d){
  if(!d || !d.ok){
    $('spendscenes').innerHTML = '<div class="empty">算不出来：' +
      esc((d && d.error) || '未知错误') + '</div>';
    return;
  }
  $('spendamount').textContent = fmtMoney(d.spent);
  $('spendbasis').textContent = d.basis === 'balance'
    ? '按余额实际减少算的 · 期间充值不扣' : '余额还没采够，按 token 估的';
  $('spendbal').textContent = d.balance_now == null ? '--' : '¥' + Number(d.balance_now).toFixed(2);
  $('spendcalls').textContent = (d.totals.calls || 0) + ' 次';
  $('spendtok').textContent = fmtTok(d.totals.tt);

  const box = $('spendscenes');
  if(!d.scenes || !d.scenes.length){
    box.innerHTML = '<div class="empty">这段时间还没有调用记录</div>';
  }else{
    box.innerHTML = d.scenes.map(sceneItem).join('');
    bindSpendRows();
  }

  const rb = $('spendrecent');
  rb.innerHTML = (d.recent && d.recent.length) ? d.recent.map(function(x){
    return '<div><span class="tm">' + fmtHM(x.t) + '</span><span>' + esc(x.s) +
           (x.k ? ' <em class="k">' + esc(x.k) + '</em>' : '') + '</span>' +
           '<span class="tk">' + fmtTok(x.tt) + '</span>' +
           '<span class="md">' + (x.ms ? (x.ms / 1000).toFixed(1) + 's' : '') + '</span></div>';
  }).join('') : '<div class="empty">暂无</div>';

  drawSpend(d);
}

// 展开状态按功能名记着，切范围 / 重新算都还在
let spendOpen = {};
function sceneItem(s){
  const pct = Math.max(1, Math.min(100, s.pct || 0));
  const open = !!spendOpen[s.s];
  return '<div class="sitem' + (open ? ' open' : '') + '" data-s="' + esc(s.s) + '">' +
    '<div class="srow" role="button" tabindex="0" aria-expanded="' + (open ? 'true' : 'false') + '"' +
      ' title="点开看这钱具体被谁花掉">' +
      '<span class="car">▶</span>' +
      '<span class="nm">' + esc(s.s) + '</span>' +
      '<span class="bar"><i style="width:' + pct + '%"></i></span>' +
      '<span class="pc">' + (s.pct || 0).toFixed(0) + '%</span>' +
      '<span class="mo">' + fmtMoney(s.cost) + '</span>' +
      '<span class="sub2">' + s.calls + ' 次 · ' + fmtTok(s.tt) + ' token</span>' +
    '</div>' + sceneMore(s) + '</div>';
}
function fmtPart(v){
  // 十来个 token 的缓存命中只有几万分之一块，写成 ¥0.0000 会让人以为没花钱
  v = Number(v || 0);
  return (v > 0 && v < 0.00005) ? '≈0' : fmtMoney(v);
}
function ptLine(cls, label, money){
  return '<div class="ptline"><span class="l"><i class="dot ' + cls + '"></i>' +
         '<span>' + esc(label) + '</span></span>' +
         '<span class="r">' + fmtPart(money) + '</span></div>';
}
function sceneMore(s){
  const miss = Math.max(0, (s.p || 0) - (s.hit || 0));   // 输入里没命中缓存的部分
  const hit  = Math.max(0, s.hit || 0);
  const out  = Math.max(0, s.c || 0);
  const prt  = s.parts || {in: 0, hit: 0, out: 0};
  const prtSum = (prt.in + prt.hit + prt.out) || 1;
  const w = function(v){ return Math.max(0, 100 * (v || 0) / prtSum); };
  const hitPct = s.p ? Math.round(100 * hit / s.p) : 0;
  let h = '<div class="smore">';

  if(miss + hit + out > 0){
    h += '<div class="ptbar" title="条的长度按钱算：三段加起来就是这一行的金额">' +
         '<i class="pi" style="width:' + w(prt.in) + '%"></i>' +
         '<i class="ph" style="width:' + w(prt.hit) + '%"></i>' +
         '<i class="po" style="width:' + w(prt.out) + '%"></i></div>';
    h += ptLine('pi', '输入 ' + fmtTok(miss) + ' token · 没命中缓存', prt.in);
    h += ptLine('ph', '输入 ' + fmtTok(hit) + ' token · 命中缓存' +
                (hitPct ? '（占 ' + hitPct + '%，单价 1/10）' : ''), prt.hit);
    h += ptLine('po', '输出 ' + fmtTok(out) + ' token', prt.out);
  }

  const subs = s.subs || [];
  if(subs.length){
    h += '<div class="shead">按对象</div>';
    h += subs.map(function(y){
      return '<div class="sline"><span class="k">' + esc(y.k) + '</span>' +
             '<span class="n">' + y.calls + ' 次 · ' + fmtTok(y.tt) + '</span>' +
             '<span class="m">' + fmtPart(y.cost) + '</span></div>';
    }).join('');
    if(s.subs_more) h += '<div class="sfoot">另外还有 ' + s.subs_more + ' 个对象没列</div>';
    if((s.subs_calls || 0) < s.calls)
      h += '<div class="sfoot">另有 ' + (s.calls - s.subs_calls) +
           ' 次早期记录没留对象名，只算在总数里</div>';
  }else{
    h += '<div class="sfoot">' + (s.talk
      ? '对象名是这次才加上去的，之前的调用只有总量。'
      : '这个功能不针对某个群或某个人，没得再拆。') + '</div>';
  }
  return h + '</div>';
}
function bindSpendRows(){
  const box = $('spendscenes');
  if(!box || box.dataset.bound) return;
  box.dataset.bound = '1';
  box.addEventListener('click', function(e){
    const row = e.target && e.target.closest ? e.target.closest('.srow') : null;
    if(row) toggleScene(row.parentNode);
  });
  box.addEventListener('keydown', function(e){
    if(e.key !== 'Enter' && e.key !== ' ') return;
    const row = e.target && e.target.closest ? e.target.closest('.srow') : null;
    if(!row) return;
    e.preventDefault();
    toggleScene(row.parentNode);
  });
}
function toggleScene(item){
  if(!item || !item.dataset) return;
  const on = item.classList.toggle('open');
  spendOpen[item.dataset.s] = on;
  const row = item.querySelector('.srow');
  if(row) row.setAttribute('aria-expanded', on ? 'true' : 'false');
}
function roundRect(g, x, y, w, h, r){
  r = Math.min(r, w / 2, h / 2);
  g.beginPath();
  g.moveTo(x + r, y);
  g.lineTo(x + w - r, y);
  g.quadraticCurveTo(x + w, y, x + w, y + r);
  g.lineTo(x + w, y + h - r);
  g.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  g.lineTo(x + r, y + h);
  g.quadraticCurveTo(x, y + h, x, y + h - r);
  g.lineTo(x, y + r);
  g.quadraticCurveTo(x, y, x + r, y);
  g.closePath();
}
function drawSpend(d){
  const cv = $('spendcv');
  if(!cv) return;
  const wrap = cv.parentNode;
  const W = Math.max(260, wrap.clientWidth || 260), H = 190;
  const dpr = window.devicePixelRatio || 1;
  cv.width = Math.round(W * dpr);
  cv.height = Math.round(H * dpr);
  cv.style.height = H + 'px';
  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);

  const padL = 42, padR = 46, padT = 8, padB = 18;
  const pw = W - padL - padR, ph = H - padT - padB;
  if(pw <= 30 || ph <= 30) return;
  const ser = d.series || [];
  const n = Math.max(1, ser.length);
  const step = pw / n;
  const peak = Math.max(0.0001, d.peak || 0);
  const bal = (d.balance || []).filter(function(p){ return p.t >= d.since; });

  g.font = '10px ui-sans-serif,system-ui,-apple-system,sans-serif';
  g.textBaseline = 'middle';
  let lastLab = null;
  for(let i = 0; i <= 3; i++){
    const y = Math.round(padT + ph * i / 3) + .5;
    g.strokeStyle = '#eef2f7';
    g.lineWidth = 1;
    g.beginPath(); g.moveTo(padL, y); g.lineTo(padL + pw, y); g.stroke();
    const v = peak * (1 - i / 3);
    const lab = peak >= 10 ? v.toFixed(0) : (peak >= 1 ? v.toFixed(1) : v.toFixed(3));
    if(lab === lastLab) continue;       // 钱少的时候别连出四个一样的 0.00
    lastLab = lab;
    g.fillStyle = '#9ca3af';
    g.textAlign = 'right';
    g.fillText(lab, padL - 6, y);
  }

  const bw = Math.max(1, step * 0.62);
  ser.forEach(function(p, i){
    const h = Math.max(0, ph * Math.min(1, (p.cost || 0) / peak));
    if(h < 0.4) return;
    const x = padL + i * step + (step - bw) / 2;
    const y = padT + ph - h;
    g.fillStyle = i === ser.length - 1 ? '#93c5fd' : '#bfdbfe';
    roundRect(g, x, y, bw, h, 2);
    g.fill();
  });

  if(ser.length){
    // 累计曲线（右轴）：不管余额精度够不够，这条线始终能看出「什么时候在花」
    let acc = 0;
    const cum = ser.map(function(p){ acc += (p.cost || 0); return acc; });
    const cmax = Math.max(0.0001, acc);
    const t0 = d.since, t1 = Math.max(d.now, d.since + 1);
    const X = function(t){ return padL + Math.max(0, Math.min(1, (t - t0) / (t1 - t0))) * pw; };
    const Y = function(v){ return padT + (1 - v / cmax) * ph; };

    g.beginPath();
    g.moveTo(X(ser[0].t), padT + ph);
    ser.forEach(function(p, i){ g.lineTo(X(p.t), Y(cum[i])); });
    g.lineTo(X(ser[ser.length - 1].t + d.bucket), padT + ph);
    g.closePath();
    const grd = g.createLinearGradient(0, padT, 0, padT + ph);
    grd.addColorStop(0, 'rgba(37,99,235,.16)');
    grd.addColorStop(1, 'rgba(37,99,235,.01)');
    g.fillStyle = grd;
    g.fill();

    g.beginPath();
    ser.forEach(function(p, i){
      if(i) g.lineTo(X(p.t), Y(cum[i])); else g.moveTo(X(p.t), Y(cum[i]));
    });
    g.strokeStyle = '#2563eb';
    g.lineWidth = 1.6;
    g.lineJoin = 'round';
    g.stroke();

    g.fillStyle = '#9ca3af';
    g.textAlign = 'left';
    g.textBaseline = 'middle';
    g.fillText('¥' + (cmax >= 10 ? cmax.toFixed(1) : (cmax >= 1 ? cmax.toFixed(2) : cmax.toFixed(3))), padL + pw + 6, padT + 1);
    g.fillText('¥0', padL + pw + 6, padT + ph - 1);
  }

  g.fillStyle = '#9ca3af';
  g.textAlign = 'center';
  g.textBaseline = 'top';
  const ticks = Math.min(5, n);
  for(let i = 0; i < ticks; i++){
    const idx = Math.round(i * (n - 1) / Math.max(1, ticks - 1));
    const t = ser[idx].t;
    const lab = d.bucket >= 86400 ? fmtMD(t)
      : (d.hours > 26 ? fmtMD(t) + ' ' + fmtHM(t) : fmtHM(t));
    g.fillText(lab, padL + idx * step + step / 2, padT + ph + 5);
  }

  cv.onmousemove = function(e){
    const r = cv.getBoundingClientRect();
    const mx = e.clientX - r.left;
    let idx = Math.floor((mx - padL) / step);
    const tip = $('spendtip');
    if(idx < 0 || idx >= ser.length || mx > padL + pw){ tip.hidden = true; return; }
    const p = ser[idx];
    const when = d.bucket >= 86400 ? fmtMD(p.t)
      : fmtHM(p.t) + '–' + fmtHM(p.t + d.bucket);
    tip.textContent = when + ' · ' + fmtMoney(p.cost);
    tip.hidden = false;
    tip.style.left = Math.round(padL + idx * step + step / 2) + 'px';
    tip.style.top = Math.round(padT + 12) + 'px';
  };
  cv.onmouseleave = function(){ $('spendtip').hidden = true; };
}

$('balcard').addEventListener('click', function(e){
  if(e.target && e.target.id === 'balrefresh') return;   // 那个 ↻ 是单刷余额
  openSpend();
});
$('balcard').addEventListener('keydown', function(e){
  if(e.key === 'Enter' || e.key === ' '){
    e.preventDefault();
    openSpend();
  }
});
$('spendclose').addEventListener('click', closeSpend);
$('spendmask').addEventListener('click', closeSpend);
$('spendranges').addEventListener('click', function(e){
  const b = e.target && e.target.closest ? e.target.closest('button[data-h]') : null;
  if(b) loadSpend(Number(b.dataset.h));
});
$('spendrefresh').addEventListener('click', function(){ loadSpend(spendHours); });
document.addEventListener('keydown', function(e){
  if(e.key === 'Escape' && spendModal.classList.contains('show')) closeSpend();
});
window.addEventListener('resize', function(){
  if(spendCache && spendModal.classList.contains('show')) drawSpend(spendCache);
});
// 地址栏加 #spend 可以直接打开这一页（放书签里，不用每次点）；#spend?h=168 直接落到 7 天
if(location.hash.indexOf('#spend') === 0){
  const mh = /h=(\d+)/.exec(location.hash);
  if(mh) spendHours = Math.max(1, Number(mh[1]) || 24);
  setTimeout(openSpend, 700);
}

/* ---------- 标题右侧的当前日期时间 ---------- */
const WK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
function p2(n){ return (n < 10 ? '0' : '') + n; }
function paintClock(){
  const el = $('clock');
  if(!el) return;
  const d = new Date();
  el.innerHTML = '<b>' + d.getFullYear() + '-' + p2(d.getMonth() + 1) + '-' + p2(d.getDate())
               + '</b> ' + WK[d.getDay()] + ' ' + p2(d.getHours()) + ':' + p2(d.getMinutes())
               + ':' + p2(d.getSeconds());
}
paintClock();
setInterval(paintClock, 1000);

/* ---------- 群聊直接回复（只影响群聊） ---------- */
function drPaint(on, rate, gap, adminReply, adminBypass){
  if(gap == null) gap = 60;
  $('dr-state').textContent = on ? ('已开启 · ' + rate + '%') : '未开启';
  $('dr-title').textContent = on ? '群里没 @ 也会接话' : '关闭中，只在被 @ 时回';
  $('dr-freqbox').classList.toggle('off', !on);
  $('dr-gapbox').classList.toggle('off', !on);
  $('dr-range').value = rate;
  $('dr-num').value = rate;
  $('dr-rate').textContent = rate;
  $('dr-gapr').value = Math.min(600, gap);
  $('dr-gapn').value = gap;
  $('dr-gap').textContent = gap;
  $('dr-admin').checked = adminReply !== false;
  $('dr-bypass').checked = adminBypass !== false;
}
function drMsg(t, cls){
  const el = $('dr-msg');
  if(!el) return;
  el.textContent = t || '';
  el.className = cls || '';
}
async function loadDR(){
  try{
    const r = await fetch('/api/group-reply', {cache: 'no-store'});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '读取失败');
    $('dr-on').checked = !!d.directReply;
    drPaint(!!d.directReply, d.rate, d.gap, d.adminReply, d.adminBypass);
    drMsg('群冷却 ' + d.cooldown + ' 秒');
  }catch(e){
    drMsg('读取失败：' + e.message, 'err');
  }
}
async function saveDR(){
  const on = $('dr-on').checked;
  const adminReply = $('dr-admin').checked;
  const adminBypass = $('dr-bypass').checked;
  let rate = parseInt($('dr-num').value, 10);
  if(!(rate >= 1)) rate = 1;
  if(rate > 100) rate = 100;
  let gap = parseInt($('dr-gapn').value, 10);
  if(!(gap >= 0)) gap = 0;
  if(gap > 3600) gap = 3600;
  drPaint(on, rate, gap, adminReply, adminBypass);
  drMsg('保存中…');
  try{
    const r = await fetch('/api/group-reply', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({directReply: on, rate: rate, gap: gap,
                              adminReply: adminReply, adminBypass: adminBypass})});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '保存失败');
    drPaint(!!d.directReply, d.rate, d.gap, d.adminReply, d.adminBypass);
    drMsg('已保存，机器人约 2 秒内生效', 'ok');
  }catch(e){
    drMsg('保存失败：' + e.message, 'err');
  }
}
$('dr-on').addEventListener('change', saveDR);
$('dr-range').addEventListener('input', e => {
  $('dr-num').value = e.target.value;
  $('dr-rate').textContent = e.target.value;
});
$('dr-range').addEventListener('change', saveDR);
$('dr-num').addEventListener('change', saveDR);
$('dr-gapr').addEventListener('input', e => {
  $('dr-gapn').value = e.target.value;
  $('dr-gap').textContent = e.target.value;
});
$('dr-gapr').addEventListener('change', saveDR);
$('dr-gapn').addEventListener('change', saveDR);
$('dr-admin').addEventListener('change', saveDR);
$('dr-bypass').addEventListener('change', saveDR);
loadDR();

/* ---------- 群聊冒泡 ---------- */
let bpStat = null;
function bpMsg(t, cls){
  const el = $('bp-msg');
  if(!el) return;
  el.textContent = t || '—';
  el.className = cls || '';
}
function bpAgo(ts){
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if(s < 60) return s + ' 秒前';
  if(s < 3600) return Math.floor(s / 60) + ' 分钟前';
  if(s < 86400) return Math.floor(s / 3600) + ' 小时前';
  return Math.floor(s / 86400) + ' 天前';
}
function paintBpStat(){
  const el = $('bp-stat');
  if(!el) return;
  if(!bpStat){ el.textContent = ''; return; }
  const n = bpStat.bubbles || 0;
  el.textContent = n
    ? ('已冒 ' + n + ' 次' + (bpStat.lastBubble ? ' · 上次 ' + bpAgo(bpStat.lastBubble) : ''))
    : '还没冒过';
}
function bpPaint(on, idle, gap, chance){
  if(idle == null) idle = 30;
  if(gap == null) gap = 120;
  if(chance == null) chance = 50;
  $('bp-state').textContent = on ? ('已开启 · ' + chance + '%') : '未开启';
  $('bp-title').textContent = on ? '冷场了会自己找个话头' : '关闭中，不会主动开口';
  ['bp-idlebox', 'bp-gapbox', 'bp-chancebox', 'bp-promptbox'].forEach(id => {
    const el = $(id);
    if(el) el.classList.toggle('off', !on);
  });
  $('bp-idler').value = Math.min(240, idle);
  $('bp-idlen').value = idle;
  $('bp-idle').textContent = idle;
  $('bp-gapr').value = Math.min(720, gap);
  $('bp-gapn').value = gap;
  $('bp-gap').textContent = gap;
  $('bp-chancer').value = chance;
  $('bp-chancen').value = chance;
  $('bp-chance').textContent = chance;
}
async function loadBubble(){
  try{
    const r = await fetch('/api/bubble', {cache: 'no-store'});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '读取失败');
    $('bp-on').checked = !!d.enabled;
    bpPaint(!!d.enabled, d.idle, d.gap, d.chance);
    $('bp-prompt').value = d.prompt || '';
    bpStat = {bubbles: d.bubbles || 0, lastBubble: d.lastBubble || 0};
    paintBpStat();
    bpMsg('—');
  }catch(e){
    bpMsg('读取失败：' + e.message, 'err');
  }
}
let bpSaving = 0;
async function saveBubble(){
  const on = $('bp-on').checked;
  let idle = parseInt($('bp-idlen').value, 10);
  if(!(idle >= 1)) idle = 30;
  if(idle > 1440) idle = 1440;
  let gap = parseInt($('bp-gapn').value, 10);
  if(!(gap >= 0)) gap = 0;
  if(gap > 2880) gap = 2880;
  let chance = parseInt($('bp-chancen').value, 10);
  if(!(chance >= 1)) chance = 50;
  if(chance > 100) chance = 100;
  const prompt = ($('bp-prompt').value || '').trim();
  bpPaint(on, idle, gap, chance);
  bpMsg('保存中…');
  const n = ++bpSaving;
  try{
    const r = await fetch('/api/bubble', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({enabled: on, idle: idle, gap: gap, chance: chance, prompt: prompt})});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '保存失败');
    if(n !== bpSaving) return;
    bpPaint(!!d.enabled, d.idle, d.gap, d.chance);
    bpMsg(on ? '已保存，机器人约 2 秒内生效' : '已关闭', 'ok');
  }catch(e){
    if(n === bpSaving) bpMsg('保存失败：' + e.message, 'err');
  }
}
$('bp-on').addEventListener('change', saveBubble);
$('bp-idler').addEventListener('input', e => {
  $('bp-idlen').value = e.target.value;
  $('bp-idle').textContent = e.target.value;
});
$('bp-idler').addEventListener('change', saveBubble);
$('bp-idlen').addEventListener('change', saveBubble);
$('bp-gapr').addEventListener('input', e => {
  $('bp-gapn').value = e.target.value;
  $('bp-gap').textContent = e.target.value;
});
$('bp-gapr').addEventListener('change', saveBubble);
$('bp-gapn').addEventListener('change', saveBubble);

/* ---------- 私聊主动 ---------- */
let pgStat = null;
let pgPeers = [];

/* 秒数说成人话：5 秒 / 1 分 30 秒 / 30 分钟 / 2 小时 / 不管 */
function durTxt(sec){
  sec = Math.max(0, Math.round(Number(sec) || 0));
  if(!sec) return '不管';
  if(sec < 60) return sec + ' 秒';
  if(sec % 3600 === 0) return (sec / 3600) + ' 小时';
  if(sec < 3600){
    return (sec % 60 === 0) ? (sec / 60) + ' 分钟'
                            : Math.floor(sec / 60) + ' 分 ' + (sec % 60) + ' 秒';
  }
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return m ? (h + ' 小时 ' + m + ' 分钟') : (h + ' 小时');
}
let pgContacts = [];
let pgPicked = new Set();

/* 选人 / 选群共用的勾选列表：名字、UID、可选的「有档案」标签 */
function renderPick(box, items, picked, kw, onPick){
  if(!box) return;
  const q = (kw || '').trim().toLowerCase();
  const rows = (items || []).filter(it => {
    const id = String(it.uid || it.gid || '');
    return !q || (it.name || '').toLowerCase().includes(q) || id.includes(q);
  });
  if(!rows.length){
    box.innerHTML = '<div class="pickempty">'
      + ((items || []).length ? '没有匹配的' : '这里还没有可选的，等机器人连上客户端再刷一次')
      + '</div>';
    return;
  }
  box.innerHTML = rows.map(it => {
    const id = String(it.uid || it.gid || '');
    const tag = it.known ? '<span class="ptag">有档案</span>' : '';
    return '<label class="pickrow"><input type="checkbox" data-id="' + esc(id) + '"'
      + (picked.has(id) ? ' checked' : '') + '>'
      + '<span class="pn">' + esc(it.name || id) + '</span>' + tag
      + '<span class="pid">' + esc(id) + '</span></label>';
  }).join('');
  box.querySelectorAll('input[type=checkbox]').forEach(cb => {
    cb.addEventListener('change', () => onPick(cb.getAttribute('data-id'), cb.checked));
  });
}
function pgMsg(t, cls){
  const el = $('pg-msg');
  if(!el) return;
  el.textContent = t || '—';
  el.className = cls || '';
}
function paintPgStat(){
  const el = $('pg-stat');
  if(!el) return;
  if(!pgStat){ el.textContent = ''; return; }
  const n = pgStat.pings || 0;
  el.textContent = n
    ? ('已主动 ' + n + ' 次' + (pgStat.lastPing ? ' · 上次 ' + bpAgo(pgStat.lastPing) : ''))
    : '还没主动过';
}
function paintPgLog(){
  const box = $('pg-log');
  if(!box) return;
  if(!pgPeers.length){
    box.innerHTML = '<span style="font-size:12px;opacity:.5">还没有主动找过谁</span>';
    return;
  }
  box.innerHTML = pgPeers.map(p => {
    const tag = (p.ignored || 0) > 0
      ? '<span style="flex:none;font-size:11px;padding:1px 6px;border-radius:8px;'
        + 'border:1px solid rgba(186,117,23,.55);opacity:.8">被晾</span>'
      : '';
    return '<div style="display:flex;gap:8px;align-items:baseline;font-size:12px;line-height:1.6">'
      + '<b style="flex:none;min-width:56px;max-width:96px;overflow:hidden;'
      + 'text-overflow:ellipsis;white-space:nowrap">' + esc(p.name || p.uid) + '</b>'
      + '<span style="flex:none;opacity:.55;white-space:nowrap">' + bpAgo(p.ts || 0) + '</span>'
      + '<span style="flex:1;min-width:0;opacity:.85;overflow:hidden;'
      + 'text-overflow:ellipsis;white-space:nowrap">' + esc(p.text || '') + '</span>'
      + tag + '</div>';
  }).join('');
}
function pgPaint(on, d){
  d = d || {};
  // 静默时长按秒算（最小 5 秒），老数据只有分钟字段时换算一下
  const idle = (d.idleSec == null)
    ? Math.round(((d.idle == null) ? 180 : d.idle) * 60)
    : d.idleSec;
  const gap = (d.gap == null) ? 300 : d.gap;
  const daily = (d.daily == null) ? 2 : d.daily;
  const every = (d.every == null) ? 25 : d.every;
  const h1 = (d.hourStart == null) ? 8 : d.hourStart;
  const h2 = (d.hourEnd == null) ? 23 : d.hourEnd;
  $('pg-on').checked = !!on;
  $('pg-state').textContent = on ? ('已开启 · 每天 ' + daily + ' 次') : '未开启';
  $('pg-title').textContent = on ? '安静久了会自己去找他' : '关闭中，私聊只回不找';
  ['pg-who', 'pg-idlebox', 'pg-gapbox', 'pg-quotabox', 'pg-everybox', 'pg-winbox',
   'pg-promptbox'].forEach(id => {
    const el = $(id);
    if(el) el.classList.toggle('off', !on);
  });
  $('pg-idler').value = Math.min(86400, idle);
  $('pg-idlen').value = idle;
  $('pg-idle').textContent = durTxt(idle);
  $('pg-gapr').value = Math.min(1440, gap);
  $('pg-gapn').value = gap;
  $('pg-gap').textContent = gap;
  $('pg-dailyr').value = Math.min(10, daily);
  $('pg-dailyn').value = daily;
  $('pg-daily').textContent = daily;
  $('pg-everyn').value = every;
  $('pg-every').textContent = every;
  $('pg-h1').value = h1;
  $('pg-h2').value = h2;
  $('pg-win').textContent = h1 + ' - ' + h2;
}
async function loadPing(){
  try{
    const r = await fetch('/api/ping', {cache: 'no-store'});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '读取失败');
    pgPaint(!!d.enabled, d);
    $('pg-prompt').value = d.prompt || '';
    pgStat = {pings: d.pings || 0, lastPing: d.lastPing || 0};
    pgPeers = d.peers || [];
    pgContacts = d.contacts || [];
    pgPicked = new Set((d.uids || []).map(String));
    pgPickedText();
    pgRenderList();
    paintPgStat();
    paintPgLog();
    pgMsg('—');
  }catch(e){
    pgMsg('读取失败：' + e.message, 'err');
  }
}
function pgPickedText(){
  const el = $('pg-picked');
  if(el) el.textContent = pgPicked.size ? ('已勾 ' + pgPicked.size + ' 人') : '全部认识的';
}
function pgRenderList(){
  renderPick($('pg-list'), pgContacts, pgPicked, ($('pg-search') || {}).value, (id, on) => {
    if(on) pgPicked.add(id); else pgPicked.delete(id);
    pgPickedText();
    savePing();
  });
}
$('pg-search').addEventListener('input', pgRenderList);
$('pg-all').addEventListener('click', () => {
  pgContacts.forEach(c => pgPicked.add(String(c.uid)));
  pgRenderList(); pgPickedText(); savePing();
});
$('pg-none').addEventListener('click', () => {
  pgPicked.clear(); pgRenderList(); pgPickedText(); savePing();
});

let pgSaving = 0;
async function savePing(){
  const on = $('pg-on').checked;
  const num = (id, def, lo, hi) => {
    let v = parseInt($(id).value, 10);
    if(!(v >= lo)) v = def;
    if(v > hi) v = hi;
    return v;
  };
  const idle = num('pg-idlen', 10800, 5, 604800);   // 秒
  const gap = num('pg-gapn', 300, 0, 10080);
  const daily = num('pg-dailyn', 2, 1, 20);
  const every = num('pg-everyn', 25, 1, 1440);
  const h1 = num('pg-h1', 8, 0, 23);
  const h2 = num('pg-h2', 23, 0, 23);
  const prompt = ($('pg-prompt').value || '').trim();
  pgPaint(on, {idleSec: idle, gap: gap, daily: daily, every: every,
               hourStart: h1, hourEnd: h2});
  pgMsg('保存中…');
  const n = ++pgSaving;
  try{
    const r = await fetch('/api/ping', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({enabled: on, idleSec: idle, gap: gap, daily: daily,
                              every: every, hourStart: h1, hourEnd: h2, prompt: prompt,
                              uids: Array.from(pgPicked)})});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '保存失败');
    if(n !== pgSaving) return;
    pgPaint(!!d.enabled, d);
    pgMsg(on ? '已保存，机器人约 2 秒内生效' : '已关闭', 'ok');
  }catch(e){
    if(n === pgSaving) pgMsg('保存失败：' + e.message, 'err');
  }
}
$('pg-on').addEventListener('change', savePing);
$('pg-idler').addEventListener('input', e => {
  $('pg-idlen').value = e.target.value;
  $('pg-idle').textContent = durTxt(e.target.value);
});
$('pg-idler').addEventListener('change', savePing);
$('pg-idlen').addEventListener('change', savePing);
$('pg-gapr').addEventListener('input', e => {
  $('pg-gapn').value = e.target.value;
  $('pg-gap').textContent = e.target.value;
});
$('pg-gapr').addEventListener('change', savePing);
$('pg-gapn').addEventListener('change', savePing);
$('pg-dailyr').addEventListener('input', e => {
  $('pg-dailyn').value = e.target.value;
  $('pg-daily').textContent = e.target.value;
});
$('pg-dailyr').addEventListener('change', savePing);
$('pg-dailyn').addEventListener('change', savePing);
$('pg-everyn').addEventListener('change', savePing);
$('pg-h1').addEventListener('change', savePing);
$('pg-h2').addEventListener('change', savePing);
loadPing();

/* ---------- 群聊主动 ---------- */
let gpStat = null;
let gpRooms = [];
let gpRecent = [];
let gpPicked = new Set();
function gpMsg(t, cls){
  const el = $('gp-msg');
  if(!el) return;
  el.textContent = t || '—';
  el.className = cls || '';
}
function gpPickedText(){
  const on = $('gp-on').checked;
  $('gp-picked').textContent = gpPicked.size
    ? ('已勾 ' + gpPicked.size + ' 个群') : '还没选群';
  $('gp-state').textContent = on
    ? (gpPicked.size ? ('已开启 · ' + gpPicked.size + ' 个群') : '开了没选群')
    : '未开启';
  $('gp-title').textContent = on
    ? (gpPicked.size ? '到点会在这几个群自己开口' : '开了但没勾群，一句话都不会说')
    : '关闭中，群里只回不主动';
}
function gpRenderList(){
  renderPick($('gp-list'), gpRooms, gpPicked, $('gp-search').value, (id, on) => {
    if(on) gpPicked.add(id); else gpPicked.delete(id);
    gpPickedText();
    saveProactive();
  });
}
function gpPaintLog(){
  const box = $('gp-log');
  if(!box) return;
  if(!gpRecent.length){
    box.innerHTML = '<span style="font-size:12px;opacity:.5">还没在群里主动说过话</span>';
    return;
  }
  box.innerHTML = gpRecent.map(p => {
    const tag = p.post
      ? '<span style="flex:none;font-size:11px;padding:1px 6px;border-radius:8px;'
        + 'border:1px solid rgba(37,99,235,.45);color:#1d4ed8">帖子</span>'
      : '';
    return '<div style="display:flex;gap:8px;align-items:baseline;font-size:12px;line-height:1.6">'
      + '<b style="flex:none;min-width:56px;max-width:104px;overflow:hidden;'
      + 'text-overflow:ellipsis;white-space:nowrap">' + esc(p.name || p.gid) + '</b>'
      + '<span style="flex:none;opacity:.55;white-space:nowrap">' + bpAgo(p.ts || 0) + '</span>'
      + '<span style="flex:1;min-width:0;opacity:.85;overflow:hidden;'
      + 'text-overflow:ellipsis;white-space:nowrap">' + esc(p.text || '') + '</span>'
      + tag + '</div>';
  }).join('');
}
function gpPaintStat(){
  const el = $('gp-stat');
  if(!el) return;
  if(!gpStat){ el.textContent = ''; return; }
  const n = gpStat.says || 0;
  el.textContent = n
    ? ('已主动 ' + n + ' 次' + (gpStat.lastSay ? ' · 上次 ' + bpAgo(gpStat.lastSay) : ''))
    : '还没主动过';
}
function gpPaint(on, d){
  d = d || {};
  const gap = d.gap == null ? 180 : d.gap;
  const daily = d.daily == null ? 2 : d.daily;
  const every = d.every == null ? 30 : d.every;
  const idle = (d.idleSec == null)
    ? Math.round(((d.idle == null) ? 30 : d.idle) * 60)
    : d.idleSec;
  const h1 = d.hourStart == null ? 9 : d.hourStart;
  const h2 = d.hourEnd == null ? 23 : d.hourEnd;
  $('gp-on').checked = !!on;
  gpPickedText();
  ['gp-who', 'gp-gapbox', 'gp-quotabox', 'gp-everybox', 'gp-idlebox', 'gp-winbox',
   'gp-promptbox'].forEach(id => {
    const el = $(id);
    if(el) el.classList.toggle('off', !on);
  });
  $('gp-gapr').value = Math.min(1440, gap);
  $('gp-gapn').value = gap;
  $('gp-gap').textContent = gap;
  $('gp-dailyr').value = Math.min(10, daily);
  $('gp-dailyn').value = daily;
  $('gp-daily').textContent = daily;
  $('gp-everyn').value = every;
  $('gp-every').textContent = every;
  $('gp-idler').value = Math.min(43200, idle);
  $('gp-idlen').value = idle;
  $('gp-idle').textContent = durTxt(idle);
  $('gp-h1').value = h1;
  $('gp-h2').value = h2;
  $('gp-win').textContent = h1 + ' - ' + h2;
}
async function loadProactive(){
  try{
    const r = await fetch('/api/proactive', {cache: 'no-store'});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '读取失败');
    gpRooms = (d.rooms || []).map(r => ({gid: String(r.gid), name: r.name || String(r.gid)}));
    gpPicked = new Set((d.ids || []).map(String));
    gpRecent = d.recent || [];
    gpStat = {says: d.says || 0, lastSay: d.lastSay || 0};
    gpPaint(!!d.enabled, d);
    $('gp-prompt').value = d.prompt || '';
    gpRenderList();
    gpPaintLog();
    gpPaintStat();
    gpMsg('—');
  }catch(e){
    gpMsg('读取失败：' + e.message, 'err');
  }
}
let gpSaving = 0;
async function saveProactive(){
  const on = $('gp-on').checked;
  const num = (id, def, lo, hi) => {
    let v = parseInt($(id).value, 10);
    if(!(v >= lo)) v = def;
    if(v > hi) v = hi;
    return v;
  };
  const gap = num('gp-gapn', 180, 0, 10080);
  const daily = num('gp-dailyn', 2, 1, 20);
  const every = num('gp-everyn', 30, 1, 1440);
  const idle = num('gp-idlen', 1800, 0, 604800);   // 秒，0 = 不管
  const h1 = num('gp-h1', 9, 0, 23);
  const h2 = num('gp-h2', 23, 0, 23);
  const prompt = ($('gp-prompt').value || '').trim();
  gpPaint(on, {gap: gap, daily: daily, every: every, idleSec: idle,
               hourStart: h1, hourEnd: h2});
  gpMsg('保存中…');
  const n = ++gpSaving;
  try{
    const r = await fetch('/api/proactive', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({enabled: on, ids: Array.from(gpPicked), gap: gap,
                              daily: daily, every: every, idleSec: idle,
                              hourStart: h1, hourEnd: h2, prompt: prompt})});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '保存失败');
    if(n !== gpSaving) return;
    gpPaint(!!d.enabled, d);
    gpMsg(on ? (gpPicked.size ? '已保存，机器人约 2 秒内生效'
                             : '已保存，但还没勾群，不会开口') : '已关闭', 'ok');
  }catch(e){
    if(n === gpSaving) gpMsg('保存失败：' + e.message, 'err');
  }
}
$('gp-on').addEventListener('change', saveProactive);
$('gp-search').addEventListener('input', gpRenderList);
$('gp-all').addEventListener('click', () => {
  gpRooms.forEach(r => gpPicked.add(String(r.gid)));
  gpRenderList(); gpPickedText(); saveProactive();
});
$('gp-none').addEventListener('click', () => {
  gpPicked.clear(); gpRenderList(); gpPickedText(); saveProactive();
});
$('gp-gapr').addEventListener('input', e => {
  $('gp-gapn').value = e.target.value;
  $('gp-gap').textContent = e.target.value;
});
$('gp-gapr').addEventListener('change', saveProactive);
$('gp-gapn').addEventListener('change', saveProactive);
$('gp-dailyr').addEventListener('input', e => {
  $('gp-dailyn').value = e.target.value;
  $('gp-daily').textContent = e.target.value;
});
$('gp-dailyr').addEventListener('change', saveProactive);
$('gp-dailyn').addEventListener('change', saveProactive);
$('gp-everyn').addEventListener('change', saveProactive);
$('gp-idler').addEventListener('input', e => {
  $('gp-idlen').value = e.target.value;
  $('gp-idle').textContent = durTxt(e.target.value);
});
$('gp-idler').addEventListener('change', saveProactive);
$('gp-idlen').addEventListener('change', saveProactive);
$('gp-h1').addEventListener('change', saveProactive);
$('gp-h2').addEventListener('change', saveProactive);
loadProactive();

/* ---------- 私聊指令 ---------- */
let ctUids = [];
function ctMsg(t, cls){
  const el = $('ct-msg');
  if(!el) return;
  el.textContent = t;
  el.className = cls || '';
}
function ctHelp(pre){
  return '直接说人话（不用打 ' + pre + '）：\n'
       + '　在主教群说一句我马上到\n'
       + '　发一句“今晚不打了”到所有群\n'
       + '　去主教群冒个泡\n'
       + '　把这张图发到主教群（先把图发它）\n'
       + '　在群里发个表情包\n\n'
       + '打指令更准：\n'
       + pre + 'groups　看我能发到哪些会话（带序号）\n'
       + pre + 'send 群名 内容　发一段话\n'
       + pre + 'img 群名 图片地址或电脑路径　发一张图\n'
       + '　图省事就先把图私聊发给它，再发「' + pre + 'img 群名」\n'
       + '群名写一半就认得，也可以直接写序号；多个目标用逗号隔开。';
}
function ctPaint(on){
  $('ct-on').checked = !!on;
  $('ct-title').textContent = on ? '开着，授权的人可以私聊指挥' : '关闭中，私聊指令不会执行';
  $('ct-state').textContent = on
      ? (ctUids.length ? ctUids.length + ' 人可用' : '还没配白名单')
      : '已关闭';
  $('ct-box').classList.toggle('off', !on);
}
function ctRenderUids(){
  const box = $('ct-uids');
  if(!box) return;
  $('ct-count').textContent = ctUids.length;
  box.innerHTML = ctUids.length
    ? ctUids.map(u => '<span class="ctchip"><b>' + esc(u) + '</b>' +
        '<button data-ctuid="' + esc(u) + '" title="移出白名单">×</button></span>').join('')
    : '<span class="ctrrow none">还没有人</span>';
}
function ctRenderRecent(list){
  const box = $('ct-recent');
  if(!box) return;
  const rows = (list || []).filter(x => x && x.uid && ctUids.indexOf(String(x.uid)) < 0);
  box.innerHTML = rows.length
    ? rows.map(x => '<span class="ctrrow" data-ctadd="' + esc(x.uid) + '">' +
        esc(x.name || ('UID ' + x.uid)) + ' · ' + esc(x.uid) + '</span>').join('')
    : '<span class="ctrrow none">还没有人私聊过它</span>';
}
function ctApply(d){
  if(!d) return;
  ctUids = (d.uids || []).map(String);
  if(typeof d.prefix === 'string' && d.prefix){
    $('ct-pre').value = d.prefix;
    $('ct-pre-label').textContent = d.prefix;
    $('ct-help').textContent = ctHelp(d.prefix);
  }
  ctPaint(!!d.enabled);
  $('ct-nl').checked = d.nl !== false;
  ctRenderUids();
  ctRenderRecent(d.recent);
  $('ct-cmds').textContent = d.cmds ? ('已执行 ' + d.cmds + ' 条') : '';
}
function ctBusy(){
  const a = document.activeElement;
  return a && (a.id === 'ct-pre' || a.id === 'ct-uid');
}
async function loadControl(){
  try{
    const r = await fetch('/api/control', {cache: 'no-store'});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '读取失败');
    if(ctBusy()){            // 正在输入就别抢输入框，只刷新动态信息
      ctRenderRecent(d.recent);
      $('ct-cmds').textContent = d.cmds ? ('已执行 ' + d.cmds + ' 条') : '';
      return;
    }
    ctApply(d);
    if(!d.enabled){
      ctMsg('已关闭，私聊指令不会执行', '');
    }else if(!(d.uids || []).length){
      ctMsg('先把对方的 UID 加进来，否则谁发指令都会被拒绝', '');
    }else{
      ctMsg('就绪：' + d.uids.length + ' 个人可以私聊指挥它', 'ok');
    }
  }catch(e){
    ctMsg('读取失败：' + e.message, 'err');
  }
}
async function saveControl(patch){
  ctMsg('保存中…');
  try{
    const r = await fetch('/api/control', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(patch)});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '保存失败');
    ctApply(d);
    ctMsg('已保存，机器人约 2 秒内生效', 'ok');
  }catch(e){
    ctMsg('保存失败：' + e.message, 'err');
  }
}
$('ct-on').addEventListener('change', e => saveControl({enabled: e.target.checked}));
$('ct-nl').addEventListener('change', e => saveControl({nl: e.target.checked}));
$('ct-pre').addEventListener('change', () => {
  let v = ($('ct-pre').value || '').trim();
  if(!v) v = '/';
  v = v.slice(0, 3);
  $('ct-pre').value = v;
  saveControl({prefix: v});
});
$('ct-add').addEventListener('click', () => {
  const u = ($('ct-uid').value || '').replace(/\D/g, '');
  if(!u){ ctMsg('先填对方的 UID（纯数字）', 'err'); return; }
  if(ctUids.indexOf(u) >= 0){ ctMsg('这个 UID 已经在白名单里了', 'err'); return; }
  ctUids.push(u);
  $('ct-uid').value = '';
  saveControl({uids: ctUids});
});
$('ct-uid').addEventListener('keydown', e => {
  if(e.key === 'Enter'){ e.preventDefault(); $('ct-add').click(); }
});
$('ct-uids').addEventListener('click', e => {
  const b = e.target.closest('[data-ctuid]');
  if(!b) return;
  const u = b.getAttribute('data-ctuid');
  ctUids = ctUids.filter(x => x !== u);
  saveControl({uids: ctUids});
});
$('ct-recent').addEventListener('click', e => {
  const b = e.target.closest('[data-ctadd]');
  if(!b) return;
  const u = b.getAttribute('data-ctadd');
  if(ctUids.indexOf(u) < 0) ctUids.push(u);
  saveControl({uids: ctUids});
});
loadControl();
setInterval(() => { if(document.visibilityState === 'visible') loadControl(); }, 30000);
$('bp-chancer').addEventListener('input', e => {
  $('bp-chancen').value = e.target.value;
  $('bp-chance').textContent = e.target.value;
});
$('bp-chancer').addEventListener('change', saveBubble);
$('bp-chancen').addEventListener('change', saveBubble);
let bpTyping = null;
$('bp-prompt').addEventListener('input', () => {
  clearTimeout(bpTyping);
  bpTyping = setTimeout(saveBubble, 900);      // 边打边存，别让人忘了点保存
});
$('bp-prompt').addEventListener('blur', () => {
  clearTimeout(bpTyping);
  saveBubble();
});
loadBubble();

/* ---------- 知识库 ---------- */
let kbData = {docs: []};
let kbSeq = 0;

function kbMsg(t, cls){
  const el = $('kb-msg');
  if(!el) return;
  el.textContent = t || '—';
  el.className = cls || '';
}
function kbFlash(t, cls, ms){
  kbMsg(t, cls);
  const n = ++kbSeq;
  setTimeout(() => { if(n === kbSeq) kbMsg('—'); }, ms || 2600);
}
function kbNum(n){
  n = n || 0;
  if(n >= 10000) return (n / 10000).toFixed(1) + ' 万字';
  return n + ' 字';
}
function renderKB(){
  const box = $('kb-list');
  if(!box) return;
  const docs = kbData.docs || [];
  const on = docs.filter(d => d.enabled).length;
  $('kb-state').textContent = docs.length
      ? ('共 ' + docs.length + ' 篇 · 已启用 ' + on) : '还没有资料';
  $('kb-budget').textContent = kbData.budget
      ? ('每次最多注入约 ' + kbData.budget + ' 字') : '';
  if(!docs.length){
    box.innerHTML = '<div class="empty">还没有资料，点上面「导入文件」加一份</div>';
    return;
  }
  box.innerHTML = docs.map(d =>
      '<div class="kbrow' + (d.enabled ? ' on' : '') + '" data-id="' + esc(d.id) + '">'
    + '<input type="checkbox" class="kbchk"' + (d.enabled ? ' checked' : '')
    + ' aria-label="启用这份资料">'
    + '<div class="kbmain"><b>' + esc(d.name) + '</b>'
    + '<span>' + esc(d.preview || '（没内容）') + '</span></div>'
    + '<div class="kbnum">' + kbNum(d.chars) + '</div>'
    + '<button class="kbdel" title="删掉这份资料">✕</button></div>'
  ).join('');
}
async function loadKB(quiet){
  try{
    const r = await fetch('/api/kb', {cache: 'no-store'});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '读取失败');
    kbData = d;
    renderKB();
    if(!quiet) kbMsg('—');
  }catch(e){
    $('kb-state').textContent = '读取失败';
    kbMsg('读取失败：' + e.message, 'err');
  }
}
async function postKB(body){
  const r = await fetch('/api/kb', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  return await r.json();
}
async function kbToggle(id, on){
  try{
    const d = await postKB({action: 'toggle', id: id, enabled: on});
    if(!d.ok) throw new Error(d.error || '保存失败');
    kbData = d; renderKB();
    kbFlash(on ? '已勾上，机器人约 2 秒内生效' : '已取消勾选，这次不再带它', 'ok');
    loadCards();                       // 卡片上的「资料 N」跟着变
  }catch(e){
    kbFlash('保存失败：' + e.message, 'err');
    loadKB(true);                      // 回滚勾选框到服务端的真实状态
  }
}
async function kbDelete(id, name){
  if(!confirm('删掉「' + name + '」？\n\n文件会从电脑里删掉，所有人物卡里的勾选也一起消失，找不回来。')) return;
  try{
    const d = await postKB({action: 'delete', id: id});
    if(!d.ok) throw new Error(d.error || '删除失败');
    kbData = d; renderKB();
    kbFlash(d.tip || '已删除', 'ok');
    loadCards();
  }catch(e){
    kbFlash('删除失败：' + e.message, 'err');
  }
}
async function readTextSmart(file){
  // Notepad 存的 txt 可能是 GBK，先按 UTF-8 严格解，失败再退回 GBK
  const buf = await file.arrayBuffer();
  try{ return new TextDecoder('utf-8', {fatal: true}).decode(buf); }
  catch(_){
    try{ return new TextDecoder('gbk').decode(buf); }
    catch(__){ return new TextDecoder('utf-8').decode(buf); }
  }
}
let kbBusy = false;
async function kbUpload(files){
  const all = Array.from(files || []);
  if(!all.length) return;
  if(kbBusy){ kbFlash('上一批还在导入，稍等一下', 'err'); return; }
  const list = all.filter(f => /\.(md|txt|markdown)$/i.test(f.name));
  if(!list.length){ kbFlash('只认 .md / .txt 文件', 'err'); return; }
  kbBusy = true;
  const bad = [];
  let ok = 0;
  for(let i = 0; i < list.length; i++){
    const f = list[i];
    kbMsg('正在导入 ' + (i + 1) + '/' + list.length + '：' + f.name);
    if(f.size > 900 * 1024){ bad.push(f.name + '（超过 900KB）'); continue; }
    let text = '';
    try{ text = await readTextSmart(f); }
    catch(e){ bad.push(f.name + '（读不出来）'); continue; }
    try{
      const d = await postKB({action: 'add', name: f.name, content: text});
      if(!d.ok) throw new Error(d.error || '导入失败');
      kbData = d; ok++;
    }catch(e){
      bad.push(f.name + '：' + e.message);
    }
  }
  kbBusy = false;
  renderKB();
  loadCards();
  if(bad.length){
    kbFlash('导入 ' + ok + ' 份，有 ' + bad.length + ' 份没成：' + bad[0], 'err', 6000);
  }else{
    kbFlash('已导入 ' + ok + ' 份，已自动勾上', 'ok');
  }
}
$('kb-pick').addEventListener('click', () => $('kb-file').click());
$('kb-file').addEventListener('change', e => {
  kbUpload(e.target.files);
  e.target.value = '';                 // 同一个文件能再选一次
});
$('kb-list').addEventListener('change', e => {
  const cb = e.target.closest('.kbchk');
  if(!cb) return;
  const row = cb.closest('.kbrow');
  if(!row) return;
  kbToggle(row.dataset.id, cb.checked);
});
$('kb-list').addEventListener('click', e => {
  const del = e.target.closest('.kbdel');
  if(!del) return;
  const row = del.closest('.kbrow');
  if(!row) return;
  const doc = (kbData.docs || []).find(x => x.id === row.dataset.id) || {};
  kbDelete(row.dataset.id, doc.name || '这份资料');
});
const kbDrop = $('kb-drop');
['dragenter', 'dragover'].forEach(ev => kbDrop.addEventListener(ev, e => {
  e.preventDefault(); kbDrop.classList.add('hot');
}));
['dragleave', 'dragend'].forEach(ev => kbDrop.addEventListener(ev, e => {
  e.preventDefault(); kbDrop.classList.remove('hot');
}));
kbDrop.addEventListener('drop', e => {
  e.preventDefault();
  kbDrop.classList.remove('hot');
  if(e.dataTransfer && e.dataTransfer.files) kbUpload(e.dataTransfer.files);
});
loadKB();

/* ---------- 表情包 ---------- */
let emData = {small: [], stickers: []};
let emSig = '', emPending = 0, emMsgSeq = 0;

function emMsg(t, cls){
  const el = $('em-msg');
  if(!el) return;
  el.textContent = t || '';
  el.className = cls || '';
}

function emFlash(t, cls, ms){
  emMsg(t, cls);
  const n = ++emMsgSeq;
  setTimeout(() => { if(n === emMsgSeq) emMsg('—'); }, ms || 2200);
}

function emPaintCfg(d, force){
  // 用户刚点过开关/拖过滑杆时先别用旧值盖回去
  if(force || Date.now() > emPending){
    $('em-on').checked = !!d.enabled;
    $('em-auto').checked = !!d.autoCollect;
    $('em-range').value = d.useRate;
    $('em-num').value = d.useRate;
    $('em-rate').textContent = d.useRate;
    $('em-maxnum').value = d.maxStickers;
    $('em-maxshow').textContent = d.maxStickers;
  }
  $('em-state').textContent = d.enabled ? ('已开启 · ' + d.useRate + '%') : '未开启';
  $('em-title').textContent = d.enabled ? '会认表情，也会用表情' : '关闭中，只认不发';
  $('em-freqbox').classList.toggle('off', !d.enabled);
  $('em-scount').textContent = d.smallTotal;
  $('em-bcount').textContent = d.stickerTotal + (d.maxStickers ? ' / ' + d.maxStickers : '');
}

function emCell(it, kind){
  // 小表情给的是 img，表情包给的是 url + 缩略 img，两种都要能显示
  const full = it.url || it.img || '';
  const src = it.img || it.url || '';
  const img = src
    ? '<img src="' + esc(src) + '" alt="" loading="lazy" data-full="' + esc(full) + '"'
      + ' onerror="this.onerror=null;if(this.dataset.full)this.src=this.dataset.full">'
    : '<span class="emcode">' + esc(it.name || it.label || it.key) + '</span>';
  const tip = kind === 'small'
    ? (it.code + '　出现 ' + it.count + ' 次')
    : ((it.label ? it.label + ' · ' : '') + it.key + '　出现 ' + it.count + ' 次'
       + '　发过 ' + it.sent + ' 次' + (it.from ? '　来自 ' + it.from : ''));
  const cnt = it.count > 1 ? '<span class="emcnt">' + it.count + '</span>' : '';
  return '<div class="emcell" data-kind="' + kind + '" data-key="' + esc(it.key) + '" title="' + esc(tip) + '">'
       + img + cnt + '<button class="emdel" title="删掉">×</button></div>';
}

function renderEmoji(d, force){
  emData = d;
  emPaintCfg(d, force);
  const sig = JSON.stringify([d.small, d.stickers]);
  if(!force && sig === emSig) return;
  emSig = sig;
  $('em-small').innerHTML = d.small.length
    ? d.small.map(x => emCell(x, 'small')).join('')
    : '<span class="emempty">还没认出小表情</span>';
  $('em-big').innerHTML = d.stickers.length
    ? d.stickers.map(x => emCell(x, 'big')).join('')
    : '<span class="emempty">还没有收藏，等群里有人发表情</span>';
}

async function loadEmoji(force){
  try{
    const r = await fetch('/api/emoji', {cache: 'no-store'});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '读取失败');
    renderEmoji(d, force);
  }catch(e){
    emMsg('读取失败：' + e.message, 'err');
  }
}

async function saveEmoji(patch, note){
  emPending = Date.now() + 4000;
  try{
    const r = await fetch('/api/emoji', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(patch)});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '写入失败');
    emSig = '';
    emPending = Date.now() + 800;
    renderEmoji(d, true);
    if(d.err){ emFlash(d.err, 'err', 4200); return; }
    emFlash(d.tip || note || '已保存', 'ok');
  }catch(e){
    emMsg('保存失败：' + e.message, 'err');
  }
}

function emReadFile(f){
  return new Promise((res, rej) => {
    const fr = new FileReader();
    fr.onload = () => res(String(fr.result || ''));
    fr.onerror = () => rej(new Error('读不到这个文件'));
    fr.readAsDataURL(f);
  });
}

async function emAddLocal(files){
  const list = Array.from(files || []);
  if(!list.length) return;
  for(const f of list){
    if(!/^image\//.test(f.type || '')){
      emFlash('只能选图片：' + f.name, 'err', 3200);
      continue;
    }
    if(f.size > 10 * 1024 * 1024){
      emFlash(f.name + ' 有 ' + (f.size / 1048576).toFixed(1) + 'MB，客户端最多收 10MB', 'err', 4200);
      continue;
    }
    $('em-pick').disabled = true;
    $('em-pickname').textContent = '正在传到客户端图床：' + f.name;
    try{
      const b64 = await emReadFile(f);
      await saveEmoji({addLocal: {name: f.name, data: b64}});
      $('em-pickname').textContent = '已收藏 ' + f.name;
    }catch(e){
      emMsg('上传失败：' + e.message, 'err');
    }finally{
      $('em-pick').disabled = false;
    }
  }
  setTimeout(() => {
    if(!$('em-pick').disabled) $('em-pickname').textContent = '本机图片，10MB 内，可多选';
  }, 4000);
}

if($('em-on')){
  $('em-on').addEventListener('change', () => saveEmoji({enabled: $('em-on').checked}));
  $('em-auto').addEventListener('change', () => saveEmoji({autoCollect: $('em-auto').checked}));
  $('em-range').addEventListener('input', e => {
    $('em-num').value = e.target.value;
    $('em-rate').textContent = e.target.value;
  });
  $('em-range').addEventListener('change',
      () => saveEmoji({useRate: parseInt($('em-range').value, 10) || 0}));
  $('em-num').addEventListener('change', () => {
    let v = parseInt($('em-num').value, 10);
    if(!(v >= 0)) v = 0;
    if(v > 100) v = 100;
    $('em-range').value = v;
    $('em-rate').textContent = v;
    saveEmoji({useRate: v});
  });
  $('em-maxnum').addEventListener('change', () => {
    let v = parseInt($('em-maxnum').value, 10);
    if(!(v >= 8)) v = 8;
    if(v > 200) v = 200;
    $('em-maxnum').value = v;
    $('em-maxshow').textContent = v;
    saveEmoji({maxStickers: v}, '上限已改成 ' + v + ' 张');
  });
  $('em-pick').onclick = () => $('em-file').click();
  $('em-file').addEventListener('change', ev => {
    emAddLocal(ev.target.files).then(() => { $('em-file').value = ''; });
  });
  $('em-add').onclick = () => {
    const u = $('em-url').value.trim();
    if(!/^https?:\/\//i.test(u)){ emMsg('要填 http/https 开头的图片地址', 'err'); return; }
    saveEmoji({addSticker: u}, '已收藏').then(() => { $('em-url').value = ''; });
  };
  ['em-small', 'em-big'].forEach(id => {
    $(id).addEventListener('click', ev => {
      const cell = ev.target.closest('.emcell');
      if(!cell) return;
      const key = cell.dataset.key, kind = cell.dataset.kind;
      if(ev.target.classList.contains('emdel')){
        if(!confirm('确定删掉这个' + (kind === 'small' ? '小表情' : '表情包') + '？')) return;
        saveEmoji(kind === 'small' ? {delSmall: key} : {delSticker: key}, '已删除');
      }else if(kind === 'big'){
        const hit = emData.stickers.find(x => x.key === key) || {};
        const v = prompt('给这张表情起个名字（会写进提示词，留空就只用编号）', hit.label || '');
        if(v === null) return;
        saveEmoji({label: {key: key, label: v.trim()}}, '备注已存');
      }
    });
  });
  loadEmoji(true);
  setInterval(() => { if(!document.hidden) loadEmoji(false); }, 8000);
}

let paused = false;

/* ---------------- 人物卡片 ---------------- */
let cardsData = {active: '', cards: []};

function setCardStat(t, cls){
  const el = $('cardstat');
  if(!el) return;
  el.textContent = t || '';
  el.className = 'mstat' + (cls ? ' ' + cls : '');
  if(t) setTimeout(() => { if(el.textContent === t) el.textContent = ''; }, 5200);
}

function cardInitial(name){
  const s = String(name || '').trim();
  return s ? s.slice(0, 1) : '?';
}

/* 智能体模式：显示当前卡片自己的状态（不是全局状态） */
function paintAgent(on){
  const row = $('agentrow'), chk = $('agenton'), st = $('agentstat');
  if(!row || !chk || !st) return;
  on = !!on;
  if(chk.checked !== on) chk.checked = on;
  row.classList.toggle('on', on);
  st.textContent = on ? '开 · 会翻记录、会自查' : '关 · 只按人设说话';
  row.title = on
    ? '回话前会在自己的聊天记录里翻旧事，说完先自查一遍（太长、在重复、答非所问就重说）'
    : '普通人设机器人：只带最近 12 句群里的话，不多花一次调用';
}

/* 心情：全局一份，跟卡片无关；没开或还没数据就整行不显示 */
const MOOD_FACE = {'欢喜': '😊', '开心': '🙂', '一般': '😐', '心疼': '😟', '难过': '😞'};
let moodLogOpen = false;

function moodWhen(ts){
  const n = Number(ts) || 0;
  if(!n) return '';
  const d = new Date(n * 1000), now = new Date();
  const hm = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  const days = Math.floor((new Date(now.toDateString()) - new Date(d.toDateString())) / 86400000);
  if(days <= 0) return hm;
  if(days === 1) return '昨天 ' + hm;
  return (d.getMonth() + 1) + '/' + d.getDate() + ' ' + hm;
}

function moodLogHtml(log){
  if(!log || !log.length) return '<i>还没有起伏记录</i>';
  return log.slice().reverse().map(e => {
    const d = Number(e.d) || 0;
    const cls = d > 0 ? 'up' : 'dn';
    const arrow = d > 0 ? '↑' : (d < 0 ? '↓' : '·');
    return '<div><i>' + esc(moodWhen(e.t)) + '</i> <span class="' + cls + '">'
      + arrow + Math.abs(d).toFixed(0) + '</span> ' + esc(e.r || '') + '</div>';
  }).join('');
}
function paintMoodLog(log){
  const box = $('moodlog');
  if(box) box.innerHTML = moodLogHtml(log);
}

/* 心情卡展开区：把「98 分」拆给人看——基线多少、这一刻被推离多少、
   七根情绪各到哪儿、以及判定节奏现在卡在哪。 */
function paintMoodDetail(m){
  const box = $('moodlog');
  if(!box) return;
  if(!m){ box.innerHTML = '<i>还没有心情数据</i>'; return; }
  const v = Number(m.v) || 0;
  const base = Number(m.base == null ? 70 : m.base);
  const calm = Number(m.calmHours || 3) || 3;
  const off = v - base;
  const hour1 = base + off * Math.pow(0.5, 1 / calm);
  const e = m.e || {};
  const keys = ['开心', '踏实', '甜蜜', '担心', '心疼', '难过', '吃醋'];
  const neg = { '担心': 1, '心疼': 1, '难过': 1, '吃醋': 1 };

  let h = '<div class="msec">这一分怎么来的</div>';
  h += '<div class="mbreak">' +
    '<span class="k">基线</span><span class="v">' + base.toFixed(0) + '　没人理时回落到这里</span>' +
    '<span class="k">此刻</span><span class="v"><b class="' + (off >= 0 ? 'up' : 'dn') + '">'
      + (off >= 0 ? '+' : '') + off.toFixed(0) + '</b>　被情绪推离基线的量</span>' +
    '<span class="k">回稳</span><span class="v">半衰期 ' + calm + ' 小时，1 小时后没人理 ≈ <b>'
      + hour1.toFixed(0) + '</b></span>' +
    '</div>';

  h += '<div class="msec">七根情绪（0-10）</div><div class="mebars">';
  h += keys.map(k => {
    const x = Math.max(0, Math.min(10, Number(e[k]) || 0));
    return '<div class="mebar' + (neg[k] ? ' neg' : '') + '"><span class="t">' + k + '</span>' +
      '<span class="b"><i style="width:' + (x / 10 * 100) + '%"></i></span>' +
      '<span class="n">' + (x ? x.toFixed(0) : '·') + '</span></div>';
  }).join('');
  h += '</div>';

  const bits = [];
  // 老版本的状态里没有这两个字段：按旧行为兜底（每句都判、不看时间）
  if(m.byTurn !== false) bits.push('每 ' + (m.every || 1) + ' 句');
  if(m.byTime === true) bits.push('至少隔 ' + (m.gapMinutes || 0) + ' 分钟');
  let rate = bits.length
    ? '判定节奏：<b>' + bits.join(' + ') + '</b>　两个闸都开就都到了才判'
    : '判定节奏：<b>两个闸都关了</b>，这一步不再花钱';
  if(m.why) rate += '<br>' + esc(m.why);
  if(m.last) rate += '　上次判定 ' + esc(moodWhen(m.last));
  h += '<div class="mrate">' + rate + '</div>';

  h += '<div class="msec">最近几次起伏</div>' + moodLogHtml(m.log || []);
  box.innerHTML = h;
}

/* 设置里那两个闸：改一下就把「现在实际怎么判」翻成人话 */
function paintMoodRate(){
  const el = $('moodratehint');
  if(!el) return;
  const byT = $('set-moodturn') && $('set-moodturn').checked;
  const byM = $('set-moodtime') && $('set-moodtime').checked;
  const n = Math.max(1, Math.min(50, Number($('set-moodevery').value) || 3));
  const g = Math.max(0, Math.min(240, Number($('set-moodgap').value) || 0));
  let s;
  if(!byT && !byM){
    s = '两个闸都关了：一句都不判，这笔钱完全不花（上面总开关开着也一样）。';
  }else if(byT && byM){
    s = g > 0
      ? '现在：说满 ' + n + ' 句，并且离上次判定超过 ' + g + ' 分钟，才判一次。'
        + (n > 1 ? '比每句都判省约 ' + Math.round(100 - 100 / n) + '%。' : '')
      : '现在：说满 ' + n + ' 句就判，间隔不限。';
  }else if(byT){
    s = '现在：只看轮数，每 ' + n + ' 句判一次，不看隔了多久。'
      + (n > 1 ? '比每句都判省约 ' + Math.round(100 - 100 / n) + '%。' : '');
  }else{
    s = g > 0
      ? '现在：只看时间，每 ' + g + ' 分钟最多判一次，说了几句都不管。'
      : '现在：只看时间，但间隔填了 0 —— 等于每句都判。想按时间就填个分钟数。';
  }
  el.textContent = s;
}
function moodTitle(m){
  // 判定节奏的一句话，鼠标停在分数上能看到现在卡在哪
  if(!m) return '';
  const byTurn = m.byTurn !== false, byTime = m.byTime === true;
  if(!byTurn && !byTime) return '判定闸都关了，只保留已有的心情';
  const bits = [];
  if(byTurn) bits.push('每 ' + (m.every || 1) + ' 句');
  if(byTime) bits.push('至少隔 ' + (m.gapMinutes || 0) + ' 分钟');
  return '判定节奏：' + bits.join(' + ') + (m.why ? ' · ' + m.why : '');
}

/* 思考流整块折叠。状态存 localStorage：折起来以后刷新页面还是折着，
   省得每次打开面板都要重新收一遍 */
let thinkFold = false, thinkState = null, thinkSig = '';

function applyThinkFold(save){
  const w = $('thinkwrap'), h = $('thinkhead');
  if(!w || !h) return;
  w.classList.toggle('collapsed', thinkFold);
  h.setAttribute('aria-expanded', thinkFold ? 'false' : 'true');
  // 还没拿到第一份 state 时别去改标题——不然会先闪一下「真思考关」
  if(thinkState) paintThinkHead(thinkState);
  if(save){
    try{ localStorage.setItem('xh_think_fold', thinkFold ? '1' : '0'); }catch(e){}
  }
}
function flipThinkFold(){
  thinkFold = !thinkFold;
  applyThinkFold(true);
}

/* 思考流标题那行小字：跟着真思考的开关和剩余次数变，不用翻设置也知道还能不能想 */
function paintThinkHead(d){
  thinkState = d || {};
  const el = $('thint'), bg = $('tbadge');
  if(!el) return;
  const on = !!(d && d.thinkOn);
  const left = Number(d && d.thinkLeft);
  if(on){
    if(bg){
      const cap = (left === 0) ? '用完了 · 这小时退回普通模型'
                : ('真思考 · 还剩 ' + (isFinite(left) ? left : 0) + ' 次');
      bg.textContent = cap;
      bg.className = 'tbadge';
      bg.title = (d.thinkModel || 'deepseek-reasoner')
        + '：每小时上限按落盘记录算，重开也不重置';
    }
    el.textContent = '点开「它脑子里那一段」看模型原文';
    el.title = '真思考开着时，它动笔前先想一遍，那一段是模型自己写的';
  }else{
    if(bg){ bg.textContent = '真思考关'; bg.className = 'tbadge off'; bg.title = '设置 → 真·思考 里可以打开'; }
    el.textContent = '它每回一句话，内部实际走了哪几步';
    el.title = '它每回一句话，内部实际走了哪几步';
  }
  // 折着的时候那一行小字说的是「怎么打开」，不是「点开看原文」
  if(thinkFold){
    el.textContent = '已折叠 · 点这里展开';
    el.title = '这一块折起来了，点标题展开';
  }
}

function paintThink(list, thinkOn){
  const box = $('think');
  if(!box) return;
  const rows = Array.isArray(list) ? list : [];
  const cnt = $('c-think');
  if(cnt) cnt.textContent = rows.length ? (rows.length + ' 条') : '';
  if(!rows.length){
    if(thinkSig !== ''){ thinkSig = ''; box.innerHTML = '<div class="empty">还没有，等它回一句</div>'; }
    return;
  }
  const html = rows.map(r => {
    const steps = Array.isArray(r.steps) ? r.steps : [];
    const tk = Number(r.tokens) || 0, ms = Number(r.ms) || 0;
    const head = '<div class="hd"><b>' + (r.is_dm ? '私聊' : '群聊') + '</b>'
      + (r.who ? '<span>' + esc(r.who) + '</span>' : '')
      + '<span>' + esc(moodWhen(Number(r.t) || 0)) + '</span>'
      + '<span class="tk">' + (tk ? tk + ' token' : '') + (tk && ms ? ' · ' : '')
      + (ms ? (ms / 1000).toFixed(1) + 's' : '') + '</span></div>';
    const inLine = r.in ? '<li class="self">收到：' + esc(r.in) + '</li>' : '';
    const stepHtml = steps.map(s => {
      s = String(s);
      const cls = /自查|重说|撞了|岔子/.test(s) ? ' class="warn"' : '';
      return '<li' + cls + '>' + esc(s) + '</li>';
    }).join('');
    // 真思考：模型自己写的思维链原文。默认折起来——一段几百字铺开会把消息流顶跑
    let cotHtml = '';
    const cot = String(r.cot || '');
    if(cot){
      cotHtml = '<details class="cotbox"><summary><b>它脑子里那一段</b>'
        + '<span>' + cot.length + ' 字 · ' + esc(r.think_model || '') + '</span></summary>'
        + '<div class="cot">' + esc(cot) + '</div></details>';
    }
    // 真思考开着、这条却没想：说清楚是「没到门槛」，不是坏了
    const noCot = (thinkOn && !cot)
      ? '<div class="nocot">这条没走真思考 · 只有长句、带问号、被追问时才想</div>' : '';
    const outLine = r.out ? '<div class="out">' + esc(r.out) + '</div>' : '';
    return '<div class="thk">' + head + '<ul class="steps">' + inLine + stepHtml + '</ul>'
      + cotHtml + noCot + outLine + '</div>';
  }).join('');
  // 内容一模一样就别再写一遍 DOM：每 2 秒重建会把展开的「它脑子里那一段」收回去，
  // 滚动位置也会跳回顶部
  if(html === thinkSig) return;
  thinkSig = html;
  box.innerHTML = html;
}

/* 绑定折叠开关：标题行整条都能点，键盘 Enter / 空格也行 */
(function initThinkFold(){
  const h = $('thinkhead'), w = $('thinkwrap');
  if(!h || !w) return;
  try{ thinkFold = localStorage.getItem('xh_think_fold') === '1'; }catch(e){ thinkFold = false; }
  w.classList.toggle('collapsed', thinkFold);
  h.setAttribute('aria-expanded', thinkFold ? 'false' : 'true');
  h.addEventListener('click', flipThinkFold);
  h.addEventListener('keydown', e => {
    if(e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar'){
      e.preventDefault();
      flipThinkFold();
    }
  });
})();

/* 实时消息流折叠：只收消息列表那几块，下面的思考流不动（它有自己的折叠） */
let msgFold = false;

function applyMsgFold(save){
  const c = $('msgcard'), h = $('msgh2');
  if(!c || !h) return;
  c.classList.toggle('folded', msgFold);
  h.setAttribute('aria-expanded', msgFold ? 'false' : 'true');
  h.title = msgFold ? '点标题展开消息列表' : '点标题折叠／展开消息列表';
  if(save){
    try{ localStorage.setItem('xh_msg_fold', msgFold ? '1' : '0'); }catch(e){}
  }
}

(function initMsgFold(){
  const c = $('msgcard'), h = $('msgh2');
  if(!c || !h) return;
  try{ msgFold = localStorage.getItem('xh_msg_fold') === '1'; }catch(e){ msgFold = false; }
  applyMsgFold(false);
  h.addEventListener('click', e => {
    // 标题里那几个按钮（跟它说话）自己管自己，别顺带把消息流折了
    if(e.target && e.target.closest && e.target.closest('button, select, input, a')) return;
    msgFold = !msgFold;
    applyMsgFold(true);
  });
  h.addEventListener('keydown', e => {
    if(e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar'){
      if(e.target !== h) return;
      e.preventDefault();
      msgFold = !msgFold;
      applyMsgFold(true);
    }
  });
})();

function paintAff(list){
  const box = $('afflist');
  if(!box) return;
  const rows = Array.isArray(list) ? list.slice() : [];
  rows.sort((a, b) => (Number(b.v) || 0) - (Number(a.v) || 0));
  const head = $('aff-state');
  if(head) head.textContent = rows.length ? ('共 ' + rows.length + ' 人') : '还没有数据';
  if(!rows.length){
    box.innerHTML = '<div class="empty">还没有数据：等有人跟它说话，这里就会长出来</div>';
    return;
  }
  box.innerHTML = rows.map(p => {
    const v = Math.max(0, Math.min(100, Number(p.v) || 0));
    const why = p.why ? ('最近：' + p.why) : '还没有往来';
    const n = p.n ? (' · 聊过 ' + p.n + ' 次') : '';
    return '<div class="affrow" data-band="' + esc(p.band || '') + '">'
         + '<div class="affwho"><b>' + esc(p.name || p.uid) + '</b>'
         + '<span>' + esc(p.band || '') + '</span></div>'
         + '<div class="affnum">' + v.toFixed(0) + '</div>'
         + '<div class="affbar"><i style="width:' + v.toFixed(1) + '%"></i></div>'
         + '<div class="affwhy">' + esc(why + n) + '</div>'
         + '</div>';
  }).join('');
}

function paintMood(m){
  const row = $('moodrow'), box = $('moodlog');
  if(!row) return;
  if(!m || m.on === false){
    row.style.display = 'none';
    if(box) box.classList.remove('show');
    return;
  }
  row.style.display = '';
  const v = Number(m.v), band = m.band || '一般';
  row.dataset.band = band;
  $('moodface').textContent = MOOD_FACE[band] || '🙂';
  $('moodband').textContent = band;
  $('moodv').textContent = isFinite(v) ? v.toFixed(0) : '--';
  const e = m.e || {};
  const top = Object.keys(e).filter(k => Number(e[k]) >= 1)
    .sort((a, b) => Number(e[b]) - Number(e[a])).slice(0, 3)
    .map(k => k + ' ' + Math.round(Number(e[k])));
  $('moodemo').textContent = top.length ? top.join(' · ') : '这会儿没什么起伏';
  row.title = moodTitle(m) + '　点一下看这一分怎么来的';
  if(box && moodLogOpen) paintMoodDetail(m);
}

function renderCards(){
  const box = $('cards');
  if(!box) return;
  const list = cardsData.cards || [];
  const cur = list.find(c => c.active) || {};
  paintAgent(!!cur.agent);
  $('c-cards').textContent = list.length
      ? ('共 ' + list.length + ' 张 · 使用中「' + (cur.name || '') + '」') : '还没有卡片';
  box.innerHTML = list.map(c => {
    const st = ['日记 ' + (c.notes || 0) + ' 篇', '群友 ' + (c.users || 0), '表情 ' + (c.stickers || 0)];
    if(c.kb) st.push('知识库 ' + c.kb + ' 篇');
    return '<div class="ccard' + (c.active ? ' on' : '') + '" data-id="' + esc(c.id) + '">'
      + '<div class="cav">' + esc(cardInitial(c.name)) + '</div>'
      + '<div class="cmeta"><b>' + esc(c.name || '未命名')
      + (c.active ? '<i class="ctag">使用中</i>' : '') + '</b>'
      + '<span>' + esc(st.join(' · ')) + '</span></div>'
      + '<div class="cops">'
      + '<button data-op="rename" title="改名">改</button>'
      + '<button data-op="del" class="del" title="删除这张卡片">✕</button>'
      + '</div></div>';
  }).join('');
}

async function loadCards(){
  try{
    const r = await fetch('/api/cards', {cache: 'no-store'});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '读取失败');
    cardsData = d;
    renderCards();
  }catch(e){
    $('c-cards').textContent = '读取失败';
  }
}

async function postCard(body){
  const r = await fetch('/api/cards', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  return await r.json();
}

async function afterCardChange(note){
  // 卡片换了：人设、群记忆、群友档案、表情包、知识库勾选全要重新拉一遍
  memDirty = false; userDirty = false; viewDirty = false; deletedUsers = [];
  userSig = '';
  await loadCards();
  await loadMem();
  fillMem();
  fillUsers();
  await loadEmoji(true);
  await loadKB(true);
  if(talkMode) await loadTalk();   // 试聊按人格分开存，切完卡得换成新人格那条线
  setCardStat(note || '已切换', 'ok');
}

async function switchCard(id){
  const c = (cardsData.cards || []).find(x => x.id === id);
  if(!c || c.active) return;
  if((memDirty || userDirty) && !confirm('这张卡片上还有没保存的改动，切过去会丢掉。\n\n确定直接切换？')) return;
  setCardStat('正在切换…', '');
  try{
    const d = await postCard({action: 'switch', id: id});
    if(!d.ok) throw new Error(d.error || '切换失败');
    await afterCardChange('已切到「' + (d.name || c.name) + '」');
  }catch(e){
    setCardStat('切换失败：' + e.message, 'err');
  }
}

async function newCard(mode){
  const copy = mode === 'copy';
  const nm = prompt(copy ? '复制当前卡片 — 给新卡片起个名字' : '新建空白卡片 — 起个名字',
                    copy ? '新人格副本' : '新人格');
  if(nm === null) return;
  setCardStat('正在创建…', '');
  try{
    const d = await postCard({action: 'new', mode: copy ? 'copy' : 'blank',
                              name: nm.trim(), switch: true});
    if(!d.ok) throw new Error(d.error || '创建失败');
    await afterCardChange('已新建并切到「' + (d.name || nm) + '」');
  }catch(e){
    setCardStat('创建失败：' + e.message, 'err');
  }
}

async function renameCard(id){
  const c = (cardsData.cards || []).find(x => x.id === id);
  if(!c) return;
  const nm = prompt('改个名字（最多 30 字）', c.name || '');
  if(nm === null) return;
  if(!nm.trim()){ setCardStat('名字不能为空', 'err'); return; }
  try{
    const d = await postCard({action: 'rename', id: id, name: nm.trim()});
    if(!d.ok) throw new Error(d.error || '改名失败');
    await loadCards();
    setCardStat('已改名为「' + d.name + '」', 'ok');
  }catch(e){
    setCardStat('改名失败：' + e.message, 'err');
  }
}

async function delCard(id){
  const c = (cardsData.cards || []).find(x => x.id === id);
  if(!c) return;
  if(c.active){ setCardStat('正在用的卡片不能删，先切到别的卡片', 'err'); return; }
  if(!confirm('删掉「' + c.name + '」？\n\n它的 ' + (c.notes || 0) + ' 篇日记、'
      + (c.users || 0) + ' 位群友档案、' + (c.stickers || 0) + ' 张表情包会一起删掉，找不回来。')) return;
  try{
    const d = await postCard({action: 'delete', id: id});
    if(!d.ok) throw new Error(d.error || '删除失败');
    await loadCards();
    setCardStat('已删掉「' + c.name + '」', 'ok');
  }catch(e){
    setCardStat('删除失败：' + e.message, 'err');
  }
}

$('cards').addEventListener('click', ev => {
  const card = ev.target.closest('.ccard');
  if(!card) return;
  const id = card.dataset.id;
  const btn = ev.target.closest('button[data-op]');
  if(btn){
    ev.stopPropagation();
    if(btn.dataset.op === 'rename') renameCard(id);
    else delCard(id);
    return;
  }
  switchCard(id);
});
$('agenton').addEventListener('change', async () => {
  const want = $('agenton').checked;
  paintAgent(want);                       // 先给反馈，失败再退回去
  try{
    const r = await fetch('/api/agent', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({enabled: want})}).then(x => x.json());
    if(!r.ok) throw new Error(r.error || '保存失败');
    setCardStat(want ? '智能体模式已打开，机器人几秒内生效' : '智能体模式已关闭', 'ok');
    loadCards();
  }catch(e){
    paintAgent(!want);
    setCardStat(e.message, 'err');
  }
});
/* 心情那行：点一下展开最近几次起伏 */
$('moodrow').addEventListener('click', () => {
  moodLogOpen = !moodLogOpen;
  const box = $('moodlog');
  if(!box) return;
  box.classList.toggle('show', moodLogOpen);
  if(moodLogOpen && lastSnap) paintMoodDetail((lastSnap.bot || {}).mood || {});
});
// 设置里改那两个闸，下面那句人话跟着变
['set-moodturn', 'set-moodtime', 'set-moodevery', 'set-moodgap'].forEach(id => {
  const el = $(id);
  if(el){
    el.addEventListener('input', paintMoodRate);
    el.addEventListener('change', paintMoodRate);
  }
});
$('cardnew').addEventListener('click', () => newCard('blank'));
$('cardcopy').addEventListener('click', () => newCard('copy'));

/* ---------------- 人设 & 记忆 ---------------- */
let memCfg = null, memDirty = false;
let viewDirty = false;     // 总观有未保存的改动（跟人设/日记分开算，互不挡）
let userDraft = {};        // 当前群的群友档案草稿 {uid:{name,alias,notes}}
let userDirty = false;     // 档案有未保存改动
let curGid = '';           // 当前选中的群（切群前用于回退）
let userSig = '';          // 草稿签名，用来判断要不要重绘（避免滚动条跳动）
let deletedUsers = [];     // 只记录用户明确点 × 的 UID，保存时才算删除
let lastMsgs = [];         // 最近消息，用来“从最近发言选择”
/* 实时消息流的筛选：all / group / dm，记在本地，重开页面还是上次看的那一类 */
let msgKind = localStorage.getItem('msgKind') || 'all';
let lastSnap = null;       // 最近一次 /api/state，切筛选时立刻重绘，不用等下一轮

/* ---------------- 消息列表：增量渲染 ----------------
   上限调到 200/400 后，每 1.2 秒把整表 innerHTML 重排一遍会很卡（几百个节点全部重建）。
   改成按内容签名复用已有节点：只有新来的那几条才创建 DOM，其余原地搬——滚动条也不会跳。 */
let msgCap = 40;
const MSG_NODES = new Map();

function msgSig(m){
  return [m.gid, m.uid, m.time, m.isBot ? 1 : 0, m.kind === 'dm' ? 1 : 0,
          m.mention ? 1 : 0, m.text || ''].join('\u0001');
}

function buildMsgNode(m){
  const cls = 'msg' + (m.isBot ? ' bot' : '') + (m.kind === 'dm' ? ' dm' : '')
            + (m.mention && !m.isBot ? ' hit' : '');
  const tags = (m.isBot ? '<span class="tag b">机器人</span>' : '')
             + (m.mention && !m.isBot ? '<span class="tag h">命中 @</span>' : '')
             + ('<span class="gname">' + esc(m.gname) + '</span>');
  const html = '<div class="' + cls + '">'
    + '<div class="meta"><span class="name">' + esc(m.name) + '</span>'
    + '<span class="uidtag" title="加档案时填这个 UID">' + esc(m.uid) + '</span>' + tags
    + '<span class="time">' + msgTime(m.time) + '</span></div>'
    + '<div class="text">' + esc(m.text) + '</div></div>';
  const wrap = document.createElement('div');
  wrap.innerHTML = html;
  return wrap.firstElementChild;
}

function paintMsgs(msgs){
  const box = $('msgs');
  if(!box) return;
  if(!msgs.length){
    MSG_NODES.clear();
    box.replaceChildren();
    const d = document.createElement('div');
    d.className = 'empty';
    d.textContent = msgKind === 'dm' ? '还没有私聊消息'
                   : msgKind === 'group' ? '还没有群聊消息' : '暂无消息';
    box.appendChild(d);
    return;
  }
  // 记住滚动位置：新消息从顶部进来时，正在往上翻的人视线别被打断
  const anchor = box.firstElementChild && box.firstElementChild.dataset.sig
      ? {sig: box.firstElementChild.dataset.sig, top: box.firstElementChild.getBoundingClientRect().top}
      : null;
  const frag = document.createDocumentFragment();
  const keep = new Set();
  for(const m of msgs){
    const sig = msgSig(m);
    keep.add(sig);
    let el = MSG_NODES.get(sig);
    if(!el){ el = buildMsgNode(m); el.dataset.sig = sig; MSG_NODES.set(sig, el); }
    frag.appendChild(el);          // 已在 box 里的节点会被移动，不会重建
  }
  for(const k of MSG_NODES.keys()) if(!keep.has(k)) MSG_NODES.delete(k);
  box.replaceChildren(frag);
  if(anchor && box.scrollTop > 0){
    const n = MSG_NODES.get(anchor.sig);
    if(n) box.scrollTop += n.getBoundingClientRect().top - anchor.top;
  }
}

/* 服务端才是权威：状态里带回真实上限，下拉框跟着对齐；
   配置文件被手改成 123 这种不在选项里的值，就临时补一个 option */
function syncMsgCap(d){
  const ui = (d && d.ui) || {};
  if(ui.maxMessages) msgCap = ui.maxMessages;
  const sel = $('msgcap');
  if(!sel) return;
  const v = String(msgCap);
  if(sel.value === v) return;
  if(![...sel.options].some(o => o.value === v)){
    const o = document.createElement('option');
    o.value = v; o.textContent = v;
    sel.appendChild(o);
  }
  sel.value = v;
}

function paintMsgSeg(){
  document.querySelectorAll('#msgseg button').forEach(b => {
    b.classList.toggle('on', b.dataset.kind === msgKind);
  });
  const tip = $('msgseg-tip');
  if(tip) tip.textContent = msgKind === 'dm'
      ? '只看私聊 · 点「实时信息窗口」能单开一个私聊窗'
      : msgKind === 'group' ? '只看群聊 · 点「实时信息窗口」能单开一个群聊窗'
      : '可以只开私聊窗口，盯一边';
}
function setMsgKind(k){
  msgKind = (k === 'dm' || k === 'group') ? k : 'all';
  try{ localStorage.setItem('msgKind', msgKind); }catch(e){}
  paintMsgSeg();
  if(lastSnap) render(lastSnap);
}

/* ---------------- 群聊 / 私聊 通道开关 ---------------- */
let chGroup = true, chDm = true, chBusy = false;
let chPlat = '小黑盒';      // 按钮上那个平台名前缀，跟着机器人实际登录的平台走

function paintChannel(){
  const g = $('chgroup'), d = $('chdm');
  if(g){ g.classList.toggle('off', !chGroup); g.querySelector('b').textContent = chGroup ? '开' : '关'; }
  if(d){ d.classList.toggle('off', !chDm); d.querySelector('b').textContent = chDm ? '开' : '关'; }
  // 平台备注：以后接了第二个平台，这两个开关得变成「每个平台一组」，
  // 至少现在写清是哪儿的，别让人以为是所有平台的总闸
  document.querySelectorAll('#chrow .pfx').forEach(e => { e.textContent = chPlat; });
}
async function loadChannel(){
  try{
    const r = await fetch('/api/channel');
    const d = await r.json();
    if(d.ok){
      chGroup = !!d.group; chDm = !!d.dm;
      if(d.platform) chPlat = d.platform;
      paintChannel();
    }
  }catch(e){}
}
async function toggleChannel(which){
  if(chBusy) return;
  chBusy = true;
  const btn = which === 'group' ? $('chgroup') : $('chdm');
  if(btn) btn.classList.add('busy');
  try{
    const body = which === 'group' ? {group: !chGroup} : {dm: !chDm};
    const r = await fetch('/api/channel', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '保存失败');
    chGroup = !!d.group; chDm = !!d.dm;
    paintChannel();
    if(which === 'group'){
      toast(chGroup ? (chPlat + '群聊已打开，它会接话了') : (chPlat + '群聊已关闭：只听不说，消息照样记着'));
    }else{
      toast(chDm ? (chPlat + '私聊已打开，它会回话了') : (chPlat + '私聊已关闭：只听不说，也不会再主动找你'));
    }
  }catch(e){
    toast('没保存上：' + e.message);
  }finally{
    chBusy = false;
    if(btn) btn.classList.remove('busy');
  }
}

/* ---------------- 跟它说话（不经过小黑盒） ---------------- */
let talkMode = false, talkBusy = false, talkMsgs = [];

function talkScroll(){
  const box = $('chatlist');
  if(box) box.scrollTop = box.scrollHeight;
}
function talkEmpty(){
  return '<div class="bub sys">这是面板里的试聊窗口，不经过小黑盒——你说的不会发到任何群里。<br>'
       + '用的是当前这张卡的人设、日记、群友记忆和心情，所以看到的就是它此刻的样子。</div>';
}
function paintTalkList(){
  const box = $('chatlist');
  if(!box) return;
  if(!talkMsgs.length){ box.innerHTML = talkEmpty(); return; }
  box.innerHTML = talkMsgs.map(m =>
      '<div class="bub ' + (m.role === 'user' ? 'me' : 'it') + '">' + esc(m.text) + '</div>'
    ).join('');
  talkScroll();
}
function paintTalkMeta(d, sec){
  const bits = [];
  if(d && d.mood) bits.push('心情 ' + d.mood.band + ' ' + d.mood.v);
  if(d && d.who) bits.push('按「' + d.who + '」在私聊里跟你说话');
  if(sec) bits.push('这一句用了 ' + sec + ' 秒');
  $('chatmeta').innerHTML = bits.length ? bits.map(esc).join(' · ') : '—';
}
function paintTalk(){
  const on = talkMode;
  $('chat').hidden = !on;
  $('msgs').hidden = on;
  $('msgseg').hidden = on;
  $('chrow').hidden = on;    // 试聊窗口里没有消息流，通道开关也就没意义
  $('mtitle').textContent = on ? '跟它说话' : '实时消息流';
  $('c-msg').hidden = on;
  const b = $('talkbtn');
  b.classList.toggle('on', on);
  b.textContent = on ? '↩ 回到消息流' : '💬 跟它说话';
  b.title = on ? '回到实时消息流' : '不经过小黑盒，直接跟它说一句话';
  if(on){ talkScroll(); const ta = $('chattext'); if(ta) ta.focus(); }
}
async function loadTalk(){
  try{
    const r = await fetch('/api/chat');
    const d = await r.json();
    if(d.ok){
      talkMsgs = d.msgs || [];
      paintTalkList();
      paintTalkMeta(d, 0);
    }
  }catch(e){}
}
async function talkSend(){
  const ta = $('chattext');
  const text = (ta.value || '').trim();
  if(!text || talkBusy) return;
  ta.value = ''; ta.style.height = 'auto';
  talkMsgs.push({role: 'user', text: text});
  paintTalkList();
  talkBusy = true;
  $('chatsend').disabled = true;
  const box = $('chatlist');
  const wait = document.createElement('div');
  wait.className = 'bub wait'; wait.textContent = '正在想…';
  box.appendChild(wait); talkScroll();
  const t0 = Date.now();
  try{
    const r = await fetch('/api/chat', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text: text})});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '它没说话');
    talkMsgs.push({role: 'assistant', text: d.reply});
    paintTalkList();
    paintTalkMeta(d, ((Date.now() - t0) / 1000).toFixed(1));
  }catch(e){
    wait.remove();
    box.insertAdjacentHTML('beforeend', '<div class="bub sys">没成：' + esc(e.message) + '</div>');
    talkScroll();
  }finally{
    talkBusy = false;
    $('chatsend').disabled = false;
    ta.focus();
  }
}
async function talkClear(){
  if(!confirm('清掉面板这边的对话记录？（真实私聊的历史不受影响）')) return;
  try{
    await fetch('/api/chat/clear', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: '{}'});
    talkMsgs = [];
    paintTalkList();
    paintTalkMeta(null, 0);
    toast('面板里的对话清空了');
  }catch(e){ toast('清不掉：' + e.message); }
}

/* ---------------- 机器人总开关 ---------------- */
let botOn = true, powerPending = 0;
let botProc = {}, botCtlBusy = false;
// 点了停止之后、机器人还没把 farewell 写进 state 的空档期。这几秒里按钮不能
// 又变回「停止机器人」，否则用户会以为没点上，再点一次。
let stopWaitUntil = 0;

function paintBotCtl(st){
  const b = $('botctl');
  if(!b) return;
  const fw = (st && st.farewell) || {};
  if(fw.active){
    // 收尾中：这是它自己在写日记，不是卡住了，按钮别让人点
    b.disabled = true;
    b.className = 'power ctl stop';
    b.textContent = '收尾中 · 还剩 ' + Math.max(0, Number(fw.left) || 0) + ' 秒';
    return;
  }
  if(st && st.stopping){
    b.disabled = true;
    b.className = 'power ctl stop';
    b.textContent = '正在收尾…';
    return;
  }
  if(st && !st.running && stopWaitUntil && Date.now() < stopWaitUntil){
    b.disabled = true;
    b.className = 'power ctl stop';
    b.textContent = '正在收尾…';
    return;
  }
  if(st && !st.running) stopWaitUntil = 0;
  if(botCtlBusy){ b.disabled = true; return; }
  b.disabled = false;
  const on = !!st.running, stalled = !!st.stalled;
  if(on && !stalled){
    b.className = 'power ctl stop';
    b.textContent = '⏹ 停止机器人';
  }else if(stalled){
    b.className = 'power ctl stop';
    b.textContent = '⚠ 卡住了 · 重启';
  }else{
    b.className = 'power ctl';
    b.textContent = '▶ 启动机器人';
  }
}

async function botCtl(){
  if(botCtlBusy) return;
  const st = botProc || {};
  const on = st.running || st.stalled;
  if(on && !confirm('停止机器人后，群里不会再有自动回复。\n\n确定停止吗？')) return;
  botCtlBusy = true;
  const b = $('botctl');
  b.disabled = true;
  b.textContent = on ? '正在停止…' : '正在启动…';
  try{
    const r = await fetch('/api/bot/' + (on ? 'stop' : 'start'), {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
    }).then(x => x.json());
    if(!r.ok){
      alert((on ? '停止失败：' : '启动失败：') + (r.err || '未知错误')
            + (r.detail ? '\n\n' + r.detail : ''));
    }else if(on && r.stopping){
      // 标记已经放下去了，机器人下一轮才看到。本地先顶住这段空档。
      const secs = Number(r.seconds) || 90;
      stopWaitUntil = Date.now() + (secs + 20) * 1000;
      b.textContent = '正在收尾…';
      const bn = $('banner');
      bn.classList.add('show', 'fare');
      bn.innerHTML = '<b>正在收尾</b>准备中 · 最多 <b>' + secs + '</b> 秒'
        + '　它在写日记和总观，写完自己退';
    }
  }catch(e){
    alert('操作失败：' + e.message);
  }
  botCtlBusy = false;
  await tick();
}

function renderPower(){
  const b = $('power');
  b.textContent = botOn ? '⏸ 暂停回复' : '▶ 恢复回复';
  b.className = 'power' + (botOn ? '' : ' off');
}
async function togglePower(){
  const next = !botOn;
  $('power').disabled = true;
  try{
    const r = await fetch('/api/toggle', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({enabled: next})});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '写入失败');
    botOn = !!d.enabled;
    powerPending = Date.now() + 6000;   // 心跳有延迟，别被旧状态立刻盖回去
    renderPower();
  }catch(e){
    const bn = $('banner');
    bn.classList.add('show');
    bn.textContent = '开关失败：' + e.message;
  }finally{
    $('power').disabled = false;
  }
}

const memFocus = () => {
  const a = document.activeElement;
  if(!a) return false;
  if(a.id === 'persona' || a.id === 'notes' || a.id === 'outlook' || a.id === 'newuid' || a.id === 'newname') return true;
  return !!(a.closest && a.closest('#ulist'));
};
function setStat(t, cls, id){
  const el = $(id || 'mstat');
  if(!el) return;
  el.textContent = t || '';
  el.className = 'mstat' + (cls ? ' ' + cls : '');
}
function fillGroups(groups){
  // 日记和群友档案都不再按会话分，会话选择框已经去掉了。
  // 这个函数留着，是因为会话列表刷新时还在调它，顺手重绘一下档案区
  fillGroup();
}
function fillNotes(){
  if(!memCfg) return;
  // 日记是「这个人」的一份，不跟着群变——所以切群不会冲掉正在写的日记
  $('notes').value = memCfg.diary || '';
  memDirty = false;
}
function fillGroup(){
  userSig = '';       // 档案变了要重绘
  fillUsers();
}
/* ---------- 群友档案（按 UID） ---------- */
function fillUsers(){
  if(!memCfg) return;
  const src = memCfg.users || {};      // 群友档案是全局的，不随会话切换而变
  const del = deletedUsers || [];
  userDraft = {};
  Object.keys(src).forEach(uid => {
    if(del.indexOf(uid) >= 0) return;      // 本地已标删、还没保存的，先别画出来
    const v = src[uid] || {};
    userDraft[uid] = {
      name: v.name || '',
      alias: Array.isArray(v.alias) ? v.alias.slice() : [],
      notes: Array.isArray(v.notes) ? v.notes.slice() : [],
      role: (v.role === 'admin' || v.role === 'block') ? v.role : ''
    };
  });
  const sig = JSON.stringify(userDraft);
  if(sig === userSig){ fillPicker(); return; }   // 没变化就别重建，免得滚动条老跳回顶部
  userSig = sig;
  renderUsers();
}
function renderUsers(){
  userSig = JSON.stringify(userDraft);
  const box = $('ulist');
  const uids = Object.keys(userDraft);
  uids.sort((a, b) => {
    const ra = userDraft[a].role, rb = userDraft[b].role;
    if((ra === 'block') !== (rb === 'block')) return ra === 'block' ? 1 : -1;   // 黑名单沉到底部
    const na = (userDraft[a].notes || []).length, nb = (userDraft[b].notes || []).length;
    if((na > 0) !== (nb > 0)) return nb > 0 ? 1 : -1;
    return (userDraft[a].name || '').localeCompare(userDraft[b].name || '', 'zh');
  });
  const nAdm = uids.filter(u => userDraft[u].role === 'admin').length;
  const nBlk = uids.filter(u => userDraft[u].role === 'block').length;
  $('c-user').textContent = uids.length
      ? ('按 UID · ' + uids.length + ' 人' + (nAdm ? ' · 管理 ' + nAdm : '') + (nBlk ? ' · 黑名单 ' + nBlk : ''))
      : '按 UID · 暂无';
  if(!uids.length){
    box.innerHTML = '<div class="empty" style="padding:14px 4px">还没有档案。机器人会自动记下发言者的 UID，' +
                    '也可以从下面手动添加。档案是全局的，群和私聊共用同一份。</div>';
    fillPicker();
    return;
  }
  box.innerHTML = uids.map(uid => {
    const v = userDraft[uid];
    const role = v.role || '';
    const opt = (val, txt) => '<option value="' + val + '"' +
        (role === val ? ' selected' : '') + '>' + txt + '</option>';
    const sel = '<select class="urole' + (role ? ' ' + role : '') + '"' +
        ' title="群友：按正常规则；管理：说话必回、不等冷却（在「群聊直接回复」里可关）；黑名单：完全不理（@ 也不回）">' +
        opt('', '群友') + opt('admin', '管理') + opt('block', '黑名单') + '</select>';
    const al = (v.alias || []).length
        ? '<span class="ualias" title="改名后旧昵称也认得出">曾用名：' + esc(v.alias.join('、')) + '</span>'
        : '';
    return '<div class="uitem' + (role === 'block' ? ' blk' : '') + '" data-uid="' + esc(uid) + '">' +
      '<div class="uhead">' +
        '<input class="uname" data-f="name" value="' + esc(v.name) + '" placeholder="昵称">' +
        sel +
        '<button class="udel" data-del="' + esc(uid) + '" title="删掉这份档案">×</button>' +
      '</div>' +
      '<div class="umeta"><span class="uuid">UID ' + esc(uid) + '</span>' + al + '</div>' +
      '<textarea data-f="notes" rows="2" spellcheck="false" placeholder="一行一条，例如：群主，爱打Apex">' +
        esc((v.notes || []).join('\n')) + '</textarea>' +
    '</div>';
  }).join('');
  fillPicker();
}
function fillPicker(){
  const sel = $('pickuser');
  if(!sel) return;
  const seen = {}, opts = [];
  (lastMsgs || []).forEach(m => {
    if(!m || !m.uid || m.isBot) return;
    if(userDraft[String(m.uid)]) return;
    if(seen[m.uid]) return;
    seen[m.uid] = 1;
    opts.push('<option value="' + esc(m.uid) + '" data-name="' + esc(m.name || '') + '">' +
              esc(m.name || '未知名') + ' · ' + esc(m.uid) + '</option>');
  });
  sel.innerHTML = '<option value="">从最近发言选择…</option>' + opts.join('');
}
function addUser(){
  const uid = ($('newuid').value || '').replace(/\D/g, '');
  const name = ($('newname').value || '').trim();
  if(!uid){ setStat('请填写 UID（纯数字）', 'err', 'ustat'); return; }
  if(userDraft[uid] && (userDraft[uid].notes || []).length){
    setStat('这个 UID 已经在档案里了', 'err', 'ustat'); return;
  }
  if(!userDraft[uid] && Object.keys(userDraft).length >= (memCfg.maxUsers || 60)){
    setStat('每群最多 ' + (memCfg.maxUsers || 60) + ' 人', 'err', 'ustat'); return;
  }
  const cur = userDraft[uid] || {name: '', alias: [], notes: [], role: ''};
  userDraft[uid] = {name: name || cur.name || '', alias: cur.alias || [],
                    notes: cur.notes || [], role: cur.role || ''};
  deletedUsers = deletedUsers.filter(x => x !== uid);
  $('newuid').value = '';
  $('newname').value = '';
  userDirty = true;
  renderUsers();
  setStat('已添加，点「保存档案」写入', '', 'ustat');
}
$('ulist').addEventListener('input', e => {
  const item = e.target.closest('.uitem');
  if(!item) return;
  const uid = item.dataset.uid, f = e.target.dataset.f;
  if(!uid || !f) return;
  if(f === 'notes'){
    userDraft[uid].notes = e.target.value.split('\n').map(s => s.trim()).filter(Boolean);
  }else if(f === 'name'){
    userDraft[uid].name = e.target.value.trim();
  }
  userDirty = true;
});
$('ulist').addEventListener('change', e => {
  const sel = e.target.closest('select.urole');
  if(!sel) return;
  const item = sel.closest('.uitem');
  const uid = item && item.dataset.uid;
  if(!uid || !userDraft[uid]) return;
  const v = (sel.value === 'admin' || sel.value === 'block') ? sel.value : '';
  userDraft[uid].role = v;
  sel.className = 'urole' + (v ? ' ' + v : '');
  if(item) item.classList.toggle('blk', v === 'block');
  userDirty = true;
  const who = userDraft[uid].name || ('UID ' + uid);
  setStat(v === 'block' ? ('已把 ' + who + ' 设为黑名单，保存后 @ 也不回')
        : v === 'admin' ? ('已把 ' + who + ' 设为管理，保存后按管理规则回话')
        : ('已把 ' + who + ' 设回普通群友'), v === 'block' ? 'err' : 'ok', 'ustat');
  renderUsers();          // 黑名单要沉到底部，计数也要跟着变
});
$('ulist').addEventListener('click', e => {
  const b = e.target.closest('.udel');
  if(!b) return;
  const uid = b.dataset.del;
  const nm = (userDraft[uid] || {}).name || uid;
  if(!confirm('删除「' + nm + '」（UID ' + uid + '）的全部记忆？\n保存后生效。')) return;
  delete userDraft[uid];
  if(deletedUsers.indexOf(uid) < 0) deletedUsers.push(uid);
  userDirty = true;
  renderUsers();
  setStat('已删除，点「保存档案」写入', '', 'ustat');
});
$('adduser').addEventListener('click', addUser);
$('pickuser').addEventListener('change', e => {
  const opt = e.target.selectedOptions && e.target.selectedOptions[0];
  if(!e.target.value) return;
  $('newuid').value = e.target.value;
  $('newname').value = opt ? (opt.dataset.name || '') : '';
  addUser();
});
function fillMem(){
  if(!memCfg) return;
  $('c-mem').textContent = '改完自动生效';
  if(typeof memCfg.enabled === 'boolean' && Date.now() > powerPending){
    botOn = memCfg.enabled;
    renderPower();
  }
  // 档案整理方式：独立开关，不受人设/日记是否正在编辑影响
  if($('urw')) $('urw').checked = memCfg.usersRewrite !== false;
  fillView();                 // 总观自己有脏标记，不受人设/日记是否正在编辑影响
  if(memFocus() || memDirty || userDirty) return;
  $('persona').value = memCfg.persona || '';
  $('learn').checked = !!memCfg.learn;
  fillNotes();
  fillGroup();
}
/* 总观：自己一个脏标记，人设/日记在改的时候也照样能刷它 */
function fillView(){
  if(!memCfg) return;
  const a = document.activeElement;
  if(viewDirty || (a && a.id === 'outlook')) return;
  $('outlook').value = memCfg.outlook || '';
  paintViewCount();
}
function paintViewCount(){
  const el = $('c-view');
  if(!el) return;
  const n = ($('outlook').value || '').trim().length;
  el.textContent = n ? (n + ' 字') : '还没写';
}
async function saveView(){
  const btn = $('viewsavebtn');
  btn.disabled = true;
  setStat('保存中…', '', 'vstat');
  try{
    const r = await fetch('/api/memory', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({outlook: $('outlook').value})});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '写入失败');
    viewDirty = false;
    if(memCfg) memCfg.outlook = $('outlook').value;
    paintViewCount();
    setStat('已保存，机器人几秒内生效', 'ok', 'vstat');
  }catch(e){
    setStat('保存失败：' + e.message, 'err', 'vstat');
  }finally{
    btn.disabled = false;
  }
}
async function loadMem(){
  try{
    const r = await fetch('/api/memory', {cache: 'no-store'});
    memCfg = await r.json();
    fillMem();
  }catch(e){
    $('c-mem').textContent = '读取失败';
  }
}
async function saveMem(){
  // 日记不跟群走：整份提交，服务端按日期行切篇
  const payload = {persona: $('persona').value, learn: $('learn').checked,
                   diary: $('notes').value, outlook: $('outlook').value};
  setStat('保存中…', '');
  try{
    const r = await fetch('/api/memory', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '写入失败');
    memDirty = false;
    viewDirty = false;
    if(memCfg) memCfg.outlook = $('outlook').value;
    paintViewCount();
    setStat('已保存，机器人几秒内生效', 'ok');
    await loadMem();
    loadCards();               // 卡片上的「记忆 n 条」跟着变
  }catch(e){
    setStat('保存失败：' + e.message, 'err');
  }
}
async function saveUsers(){
  const book = {};
  Object.keys(userDraft).forEach(uid => {
    const v = userDraft[uid] || {};
    const ns = (v.notes || []).map(s => String(s).trim()).filter(Boolean);
    const nm = (v.name || '').trim();
    const al = (v.alias || []).filter(a => a && a !== nm);
    if(nm || ns.length || al.length || v.role) book[uid] = {name: nm, alias: al, notes: ns, role: v.role || ''};
  });
  // 全局档案：整份提交 + 显式删除列表，服务端按 UID 合并
  const payload = {users: book, deletedUsers: deletedUsers.slice()};
  setStat('保存中…', '', 'ustat');
  try{
    const r = await fetch('/api/memory', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '写入失败');
    userDirty = false;
    deletedUsers = [];
    await loadMem();
    userSig = '';
    fillUsers();               // 群记忆在编辑时 fillMem 会跳过，这里单独刷新档案
    loadCards();
    setStat('已保存，机器人几秒内生效', 'ok', 'ustat');
  }catch(e){
    setStat('保存失败：' + e.message, 'err', 'ustat');
  }
}
async function restoreNotes(){
  if(!confirm('用上一次整理前的记忆覆盖当前记忆？人设不受影响。')) return;
  setStat('还原中…', '');
  try{
    const r = await fetch('/api/restore', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: '{}'});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '没有可用的备份');
    memDirty = false;
    setStat('已还原，共 ' + d.notes + ' 篇日记', 'ok');
    await loadMem();
  }catch(e){
    setStat('还原失败：' + e.message, 'err');
  }
}

async function learnNow(){
  setStat('正在让它写今天的日记…', '');
  try{
    const r = await fetch('/api/learn', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: '{}'});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '失败');
    setStat('已排队，约 5 秒后刷新；日记写好后这里会自动更新', 'ok');
    setTimeout(loadMem, 7000);
  }catch(e){
    setStat('失败：' + e.message, 'err');
  }
}
async function learnUsers(){
  const rw = !$('urw') || $('urw').checked;
  setStat(rw ? '已通知机器人按日记改写档案…' : '已通知机器人整理档案…', '', 'ustat');
  try{
    const r = await fetch('/api/learn-users', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: '{}'});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '失败');
    setStat('已排队，约 5 秒后刷新', 'ok', 'ustat');
    setTimeout(loadMem, 8000);
  }catch(e){
    setStat('失败：' + e.message, 'err', 'ustat');
  }
}
async function restoreUsers(){
  if(!confirm('用上一次整理前的档案覆盖当前群友档案？\n人设和群记忆不受影响。')) return;
  setStat('还原中…', '', 'ustat');
  try{
    const r = await fetch('/api/restore-users', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: '{}'});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '还没有档案备份');
    userDirty = false;
    deletedUsers = [];
    await loadMem();
    userSig = '';
    fillUsers();                 // 群记忆那边可能正在编辑，这里强制刷新档案
    setStat('已还原，共 ' + d.users + ' 人', 'ok', 'ustat');
  }catch(e){
    setStat('还原失败：' + e.message, 'err', 'ustat');
  }
}

/* 顶部那个「触发」：说人话，讲清楚什么情况下它才会接话 */
function paintTrigger(cfg){
  const el = $('p-trigger');
  if(!el) return;
  const mode = String(cfg.trigger || '').toLowerCase();
  const pre = String(cfg.prefix || '').trim();
  const tag = pre ? '「' + pre + '」' : '前缀';
  const names = {
    all: '每条都接',
    mention: '被 @ 才接',
    prefix: tag + '开头才接',
    keyword: '命中关键词才接',
    mention_or_prefix: '@ 或' + tag + '开头'
  };
  const name = names[mode] || (mode ? mode : '--');
  const rate = Number(cfg.directRate) || 0;
  el.textContent = name;
  if(cfg.directReply && mode !== 'all'){        // 尾巴只占一小截，别把标题栏挤到第二行
    const tag = document.createElement('span');
    tag.className = 'pillx';
    tag.textContent = rate > 0 ? ' +' + rate + '%' : ' +随机';
    el.appendChild(tag);
  }
  el.title = mode === 'all'
      ? '群聊里每一条消息都会接。想只让它被叫到才说话，改 config.json 的 trigger.mode'
      : '群聊里' + name + '。'
        + (cfg.directReply
            ? '「群里没 @ 也会接话」已打开，没被叫到时按 ' + rate + '% 概率随机接一句，被 @ 仍必回。'
            : '没被叫到的消息不接。想让它偶尔主动插一句，去「说话 → 群聊直接回复」打开「群里没 @ 也会接话」。')
        + ' 改触发方式：config.json 里 trigger.mode（all / mention / prefix / keyword / mention_or_prefix）';
}

function render(d){
  lastSnap = d;
  $('ts').textContent = hhmmss(new Date());

  const c = d.client || {};
  setDot($('d-client'), !!c.connected);
  $('p-client').textContent = c.connected ? '已连接' : '未连接';
  paintTrigger(d.config || {});
  $('p-model').textContent = (d.config && d.config.model) || '--';

  const b = d.bot || {};
  botProc = b;
  setDot($('d-bot'), !!b.running);
  if(Date.now() > powerPending && typeof b.enabled === 'boolean' && b.enabled !== botOn){
    botOn = b.enabled;
    renderPower();
  }
  $('p-bot').textContent = b.running ? (botOn ? '运行中' : '已暂停') : (b.stalled ? '无响应' : '未运行');
  paintBotCtl(b);
  paintMood(b.mood);
  paintAff(d.affinity);
  paintThink(d.think, !!d.thinkOn);
  paintThinkHead(d);

  // 冒泡卡片右下角：冒了几次、上次多久之前（跟着心跳走，不用刷新页面）
  if(b.bubble && typeof b.bubble.bubbles === 'number'){
    bpStat = {bubbles: b.bubble.bubbles, lastBubble: b.bubble.lastBubble || 0};
    paintBpStat();
  }
  // 私聊主动卡片：找了几次、上次多久之前、最近找了谁（跟着心跳走）
  if(b.ping && typeof b.ping.pings === 'number'){
    pgStat = {pings: b.ping.pings, lastPing: b.ping.lastPing || 0};
    paintPgStat();
    if(Array.isArray(b.ping.peers)){
      pgPeers = b.ping.peers;
      paintPgLog();
    }
  }
  // 群聊主动卡片：说了几次、上次多久之前、最近在哪些群说过
  if(b.groupPing && typeof b.groupPing.says === 'number'){
    gpStat = {says: b.groupPing.says, lastSay: b.groupPing.last || 0};
    gpPaintStat();
    if(Array.isArray(b.groupPing.recent)){
      gpRecent = b.groupPing.recent;
      gpPaintLog();
    }
  }

  $('s-replied').innerHTML = (b.replied || 0) + '<small>条</small>';
  $('s-hits').innerHTML = (b.triggers || 0) + '<small>次</small>';
  const st = d.stats || {};
  $('s-msgs').innerHTML = (st.msgTotal || 0) + '<small>条</small>';
  $('s-uptime').textContent = b.running ? dur(b.uptime) : '--';
  renderBalance(d.balance || {});

  $('f-self').textContent = c.connected
      ? ('账号：' + (c.selfName || '') + ' (' + (c.selfId || '') + ')')
      : '账号：--';

  const banner = $('banner');
  const fw = b.farewell || {};
  banner.classList.toggle('fare', !!fw.active);
  if(fw.active){
    // 收尾倒计时优先：这时候「机器人没在轮询」是正常的（它正在写日记）
    banner.classList.add('show');
    banner.innerHTML = '<b>正在收尾</b> · ' + esc(fw.phase || '准备')
      + ' · 还剩 <b>' + Math.max(0, Number(fw.left) || 0) + '</b> 秒'
      + '　日记 +' + (Number(fw.diary) || 0) + ' 字'
      + (Number(fw.outlook) ? '，总观 ' + Number(fw.outlook) + ' 字' : '');
  } else if(b.stopping || (stopWaitUntil && Date.now() < stopWaitUntil)){
    // 停止标记已经放下去了，机器人还没走到收尾那一步（最多等它一个轮询周期）
    banner.classList.add('show', 'fare');
    banner.innerHTML = '<b>正在收尾</b>准备中…写完日记和总观就退';
  } else if(!c.connected){
    banner.classList.add('show');
    banner.textContent = '客户端未连接：' + (c.error || '请确认黑盒语音已用调试端口启动（用启动脚本启动）');
  } else if(!b.running){
    banner.classList.add('show');
    banner.textContent = '机器人进程未在轮询：页面仍可看消息，但不会自动回复。用启动脚本启动。';
  } else if(!botOn){
    banner.classList.add('show');
    banner.textContent = '自动回复已暂停（网页总开关）。消息照常记录、不会回复，点右上「▶ 恢复回复」重新开启。';
  } else {
    banner.classList.remove('show');
  }

  // 群聊 / 私聊分开看：计数按全部消息算，列表按当前筛选算
  syncMsgCap(d);                     // 上限以服务端为准，下拉框跟着走
  const allMsgs = d.messages || [];
  const nDm = allMsgs.filter(m => m.kind === 'dm').length;
  $('sg-all').textContent = allMsgs.length;
  $('sg-group').textContent = allMsgs.length - nDm;
  $('sg-dm').textContent = nDm;
  const msgs = msgKind === 'dm' ? allMsgs.filter(m => m.kind === 'dm')
             : msgKind === 'group' ? allMsgs.filter(m => m.kind !== 'dm')
             : allMsgs;
  $('c-msg').textContent = msgs.length
      ? msgs.length + ' 条' + (msgKind === 'all' ? ''
            : '（' + (msgKind === 'dm' ? '只看私聊' : '只看群聊') + '）')
        + (allMsgs.length >= msgCap ? ' · 已到上限 ' + msgCap : '')
      : '';
  lastMsgs = allMsgs;
  if(!memDirty && !userDirty) fillPicker();
  paintMsgs(msgs);

  const gs = d.groups || [];
  const dms = d.dms || [];
  const all = gs.concat(dms);
  $('c-group').textContent = all.length ? (gs.length + ' 群' + (dms.length ? ' / ' + dms.length + ' 私聊' : '')) : '';
  $('groups').innerHTML = all.length ? all.map(g =>
      '<div class="gitem"><span class="gn">' + esc((g.kind === 'dm' ? '私聊 · ' : '') + g.name) + '</span>'
      + '<span class="gl">' + esc(g.last || '') + '</span></div>').join('')
      : '<div class="empty">没有监听到会话</div>';
  fillGroups(all);

  const logs = (d.log || []).slice().reverse();
  $('log').innerHTML = logs.length ? logs.map(l =>
      '<div class="logline ' + esc(l.level) + '"><span class="t">' + esc(l.t) + '</span>'
      + '<span class="lv">' + esc(l.level) + '</span>'
      + '<span class="m">' + esc(l.msg) + '</span></div>').join('')
      : '<div class="empty">暂无日志</div>';
}

async function tick(){
  if(paused) return;
  try{
    const r = await fetch('/api/state', {cache: 'no-store'});
    if(!r.ok) throw new Error('HTTP ' + r.status);
    render(await r.json());
  }catch(e){
    setDot($('d-client'), false);
    $('p-client').textContent = '服务异常';
    const banner = $('banner');
    banner.classList.add('show');
    banner.textContent = '监听服务无响应：' + e.message + '（确认 monitor.py 窗口还在运行）';
  }
}

document.addEventListener('keydown', e => {
  if(e.key === ' ' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)){
    paused = !paused; e.preventDefault();
  }
});

['persona', 'notes'].forEach(id => $(id).addEventListener('input', () => { memDirty = true; }));
$('outlook').addEventListener('input', () => { viewDirty = true; paintViewCount(); });
$('viewsavebtn').addEventListener('click', saveView);
$('savebtn').addEventListener('click', saveMem);
$('usavebtn').addEventListener('click', saveUsers);
$('ulearnbtn').addEventListener('click', learnUsers);
$('urestorebtn').addEventListener('click', restoreUsers);
// 结合日记改写档案：勾上就即时保存，整理档案时按新方式走
$('urw').addEventListener('change', async (e) => {
  const on = e.target.checked;
  setStat(on ? '已开启：整理档案会读日记并改写' : '已关闭：只追加新条目', '', 'ustat');
  try{
    const r = await fetch('/api/users-rewrite', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({on})});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || '保存失败');
    setStat(on ? '已开启：整理档案会读日记并改写' : '已关闭：只追加新条目', 'ok', 'ustat');
  }catch(err){
    e.target.checked = !on;
    setStat('保存失败：' + err.message, 'err', 'ustat');
  }
});
$('learnbtn').addEventListener('click', learnNow);
$('restorebtn').addEventListener('click', restoreNotes);
$('power').addEventListener('click', togglePower);
$('chgroup').addEventListener('click', () => toggleChannel('group'));
$('chdm').addEventListener('click', () => toggleChannel('dm'));
$('talkbtn').addEventListener('click', () => {
  talkMode = !talkMode;
  if(talkMode && msgFold){ msgFold = false; applyMsgFold(true); }   // 折着的话先摊开，不然聊天框藏起来了
  paintTalk();
  if(talkMode) loadTalk();
});
$('chatsend').addEventListener('click', talkSend);
$('chatclear').addEventListener('click', talkClear);
$('chattext').addEventListener('keydown', e => {
  // Enter 发出去，Shift+Enter 换行——跟聊天软件一致
  if(e.key === 'Enter' && !e.shiftKey && !e.isComposing){
    e.preventDefault();
    talkSend();
  }
});
$('chattext').addEventListener('input', e => {
  // 输入框跟着内容长高，最多到 CSS 的 max-height
  const ta = e.target;
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 130) + 'px';
});
loadChannel();
$('botctl').addEventListener('click', botCtl);
$('quit').addEventListener('click', async () => {
  if(!confirm('退出会同时停止机器人和面板，群里不再自动回复。\n\n'
              + '如果开着「关机前收尾」，它会先写日记和总观——倒计时会显示在页面上，'
              + '写完才真正关闭。\n\n确定退出吗？')) return;
  paused = true;                        // 先停掉常规刷新，别和退出进度抢画面
  const mask = $('quitmask');
  mask.classList.add('show');
  $('quit-title').textContent = '正在退出';
  $('quit-num').style.display = '';
  $('quit-num').innerHTML = '--<small>秒</small>';
  $('quit-phase').textContent = '正在通知机器人…';
  $('quit-bar').style.width = '0%';
  $('quit-ext').textContent = '';
  try{ await fetch('/api/quit', {method: 'POST'}); }catch(e){}
  pollQuit();
});

async function pollQuit(){
  let s = null;
  try{
    const r = await fetch('/api/quit/state', {cache: 'no-store'});
    s = await r.json();
  }catch(e){ s = null; }
  // 连不上了 = 面板已经真的退了，这时才说「已退出」
  if(!s) return quitFinished(null);
  if(s.done) return quitFinished(s);
  paintQuit(s);
  setTimeout(pollQuit, 1000);
}

function paintQuit(s){
  const sec = Number(s.seconds) || 0;
  const left = Math.max(0, Number(s.left) || 0);
  $('quitmask').classList.add('show');
  $('quit-title').textContent = s.farewell ? '正在关机收尾' : '正在退出';
  $('quit-num').style.display = '';
  $('quit-num').innerHTML = s.farewell ? left + '<small>秒</small>' : '…';
  let line = '';
  if(s.farewell){
    line = String(s.phase || '') || '机器人正在写日记和总观';
    if(Number(s.diary)) line += ' · 日记 +' + Number(s.diary) + ' 字';
    if(Number(s.outlook)) line += ' · 总观 ' + Number(s.outlook) + ' 字';
  }else{
    line = '没有收尾要做，正在关掉进程';
  }
  $('quit-phase').textContent = line;
  $('quit-bar').style.width =
    (sec ? Math.min(100, Math.max(2, (1 - left / sec) * 100)) : 100) + '%';
  $('quit-ext').innerHTML = s.farewell
    ? '最多等 ' + sec + ' 秒，写完会提前关。这期间群里的消息不再回复。<br>'
      + '倒计时走完还没写完也会被强制结束，不会一直卡在这里。'
    : '';
}

function quitFinished(s){
  const mask = $('quitmask');
  mask.classList.add('show', 'done');
  $('quit-title').textContent = '已退出';
  $('quit-num').style.display = 'none';
  $('quit-bar').style.width = '100%';
  $('quit-phase').innerHTML = '<span class="fin">机器人和面板都已停止</span>';
  // 底下那排状态是退出前最后一帧，别让它还挂着「运行中」和绿点
  try{
    setDot($('d-bot'), false);
    $('p-bot').textContent = '已停止';
    $('p-client').textContent = '已断开';
    const bn = $('banner');
    bn.classList.remove('show');
  }catch(e){}
  let ext = '这个页面可以直接关掉。<br>下次使用再双击「黑盒语音群聊机器人」。';
  if(s && Number(s.diary)) ext += '<br>这次日记写了 ' + Number(s.diary) + ' 字。';
  if(s && Number(s.outlook)) ext += '<br>总观改了 ' + Number(s.outlook) + ' 字。';
  if(s && s.err) ext += '<br><span style="color:#dc2626">退出时报错：' + esc(s.err) + '</span>';
  $('quit-ext').innerHTML = ext;
}
$('balrefresh').addEventListener('click', async () => {
  const btn = $('balrefresh');
  btn.disabled = true;
  $('s-balhint').textContent = '向 DeepSeek 查询中…';
  try{
    const r = await fetch('/api/balance', {cache: 'no-store'});
    renderBalance(await r.json());
  }catch(e){
    renderBalance({ok: false, error: e.message});
  }finally{
    setTimeout(() => { btn.disabled = false; }, 800);
  }
});
$('openlive').addEventListener('click', () => {
  // 按当前筛选开窗：选「私聊」点一次就是私聊窗，再切到「群聊」点一次就是群聊窗，
  // 窗口名按筛选区分，所以两个能同时开着并排看
  const q = msgKind === 'all' ? '' : ('?kind=' + msgKind);
  const w = window.open('/live' + q, 'xhh_live_' + msgKind,
    'width=600,height=860,menubar=no,toolbar=no,location=no,status=no');
  if(w) w.focus();
  else alert('浏览器拦住了弹窗，请允许后重试，或直接打开 http://127.0.0.1:8765/live' + q);
});
$('msgseg').addEventListener('click', e => {
  const b = e.target.closest('button[data-kind]');
  if(b) setMsgKind(b.dataset.kind);
});
$('msgcap').addEventListener('change', async e => {
  const v = parseInt(e.target.value, 10) || 40;
  try{
    const r = await fetch('/api/ui', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({maxMessages: v})});
    const d = await r.json();
    if(d.ok){ msgCap = d.maxMessages; tick(); }
  }catch(err){}
});
syncMsgCap({});
paintMsgSeg();
renderPower();

loadCards();
loadMem();
setupBind();
loadSetup();
tick();
setInterval(tick, 2000);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# 独立的实时信息窗口（/live）：只滚动显示消息，不提供编辑，适合丢在副屏
# --------------------------------------------------------------------------
LIVE_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<title>实时信息 · 群聊机器人</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root{--bg:#f6f7f9;--card:#fff;--line:#e5e7eb;--text:#111827;--muted:#6b7280;
        --ok:#0f9d63;--err:#dc2626;--warn:#f59e0b}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  header{position:sticky;top:0;background:#fff;border-bottom:1px solid var(--line);
         padding:9px 14px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;z-index:2}
  h1{font-size:14px;margin:0 6px 0 0;font-weight:650}
  .dot{width:7px;height:7px;border-radius:50%;background:#cbd5e1;display:inline-block}
  .dot.on{background:var(--ok);box-shadow:0 0 0 3px rgba(15,157,99,.12)}
  .dot.off{background:var(--err);box-shadow:0 0 0 3px rgba(220,38,38,.12)}
  .pill{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);
        border-radius:999px;padding:3px 10px;font-size:12px;color:var(--muted)}
  .pill b{color:var(--text);font-weight:600}
  .sp{margin-left:auto}
  #list{padding:6px 0 70px}
  .m{padding:9px 14px;border-left:2px solid transparent}
  .m+.m{border-top:1px solid #f2f5fa}
  .m.bot{background:#f6f9ff;border-left-color:#bfdbfe}
  .m.dm{border-left-color:#ddd6fe}
  .m.new{animation:fade .5s ease}
  @keyframes fade{from{background:#fff7ed}to{background:transparent}}
  .meta{display:flex;gap:8px;align-items:center;font-size:12px;color:var(--muted)}
  .meta .n{color:#374151;font-weight:600}
  .meta .t{margin-left:auto;font-variant-numeric:tabular-nums}
  .g{font-size:11px;color:#9ca3af}
  .tag{font-size:11px;padding:0 5px;border-radius:5px;background:#ede9fe;color:#5b21b6}
  .txt{margin-top:2px;white-space:pre-wrap;word-break:break-word;font-size:14.5px}
  .empty{padding:40px 16px;text-align:center;color:#9ca3af;font-size:13px}
  .bar{position:fixed;left:0;right:0;bottom:0;background:#fff;border-top:1px solid var(--line);
       padding:6px 14px;font-size:12px;color:var(--muted);display:flex;gap:12px;align-items:center}
  .bar button{border:1px solid var(--line);background:#fff;border-radius:8px;padding:3px 10px;
              font:inherit;font-size:12px;cursor:pointer}
  .seg{display:inline-flex;gap:4px}
  .seg button{border:1px solid var(--line);background:#fff;border-radius:8px;padding:3px 9px;
              font:inherit;font-size:12px;color:var(--muted);cursor:pointer;
              display:inline-flex;align-items:center;gap:4px}
  .seg button:hover{background:#f9fafb}
  .seg button.on{background:#eef2ff;border-color:#c7d2fe;color:#3730a3;font-weight:600}
  .seg b{font-weight:600;color:#b0b7c3;font-variant-numeric:tabular-nums}
  .seg button.on b{color:#4338ca}
</style></head>
<body>
<header>
  <h1 id="h1">实时信息</h1>
  <span class="seg" id="seg">
    <button data-kind="all">全部 <b id="k-all">0</b></button>
    <button data-kind="group">群聊 <b id="k-group">0</b></button>
    <button data-kind="dm">私聊 <b id="k-dm">0</b></button>
  </span>
  <span class="pill"><i class="dot" id="dc"></i>客户端 <b id="pc">…</b></span>
  <span class="pill"><i class="dot" id="db"></i>机器人 <b id="pb">…</b></span>
  <span class="pill" id="pw">…</span>
  <span class="pill" id="pbal">余额 …</span>
  <span class="sp"></span>
  <button class="pill" id="pin" title="有新消息时自动滚到底部">自动滚动：开</button>
</header>
<div id="list"><div class="empty">连接中…</div></div>
<div class="bar"><span id="cnt">—</span><span class="sp"></span><span>空格暂停刷新 · 标题栏能看到当前在看哪一类</span></div>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let seen = {}, paused = false, follow = true, inited = false;
// 窗口可以只盯一类：/live?kind=group 或 /live?kind=dm，主面板按当前筛选开窗
const qk = new URLSearchParams(location.search).get('kind');
let kind = (qk === 'dm' || qk === 'group') ? qk : 'all';
let lastD = null;
function kindName(){
  return kind === 'dm' ? '私聊' : kind === 'group' ? '群聊' : '全部';
}

function msgTime(t){
  const d = new Date(t || Date.now());
  return ('0'+d.getHours()).slice(-2) + ':' + ('0'+d.getMinutes()).slice(-2) + ':' + ('0'+d.getSeconds()).slice(-2);
}
function key(m){ return (m.gid || '') + '|' + (m.seq || '') + '|' + (m.time || ''); }
const LNODES = new Map();          // 已渲染过的行，按内容签名复用

function render(d){
  lastD = d;
  const b = d.bot || {};
  const c = d.client || {};
  $('dc').className = 'dot ' + (c.connected ? 'on' : 'off');
  $('pc').textContent = c.connected ? (c.selfName || '已连接') : '未连接';
  $('db').className = 'dot ' + (b.running ? 'on' : 'off');
  $('pb').textContent = b.running ? ('运行中 · 已回 ' + (b.replied || 0)) : '未运行';
  $('pw').textContent = (typeof (d.bot || {}).enabled === 'boolean'
                          ? (d.bot.enabled ? '自动回复：开' : '自动回复：暂停')
                          : '') + (b.running ? '' : '（进程未在跑）');
  const bal = d.balance || {};
  if(bal.ok){
    const t = Number(bal.total || 0);
    $('pbal').innerHTML = '余额 <b style="color:' + (t < 5 ? '#dc2626' : '#111827') + '">¥'
                          + t.toFixed(2) + '</b>';
    $('pbal').title = (bal.available === false ? '额度不足' : '可正常调用')
                      + (bal.granted ? ' · 赠送 ¥' + Number(bal.granted).toFixed(2) : '');
  }else{
    $('pbal').textContent = '余额读取失败';
  }

  // 先按窗口的筛选挑，再取最近 60 条——否则「全部」里私聊一多，群聊窗就空着
  const allMsgs = (d.messages || []).slice().sort((a, b) => (a.time || 0) - (b.time || 0));
  const nDm = allMsgs.filter(m => m.kind === 'dm').length;
  $('k-all').textContent = allMsgs.length;
  $('k-group').textContent = allMsgs.length - nDm;
  $('k-dm').textContent = nDm;
  document.querySelectorAll('#seg button').forEach(b => {
    b.classList.toggle('on', b.dataset.kind === kind);
  });
  $('h1').textContent = kind === 'all' ? '实时信息' : ('实时信息 · 只看' + kindName());
  document.title = '实时信息 · ' + kindName() + ' · 群聊机器人';

  const picked = kind === 'dm' ? allMsgs.filter(m => m.kind === 'dm')
               : kind === 'group' ? allMsgs.filter(m => m.kind !== 'dm')
               : allMsgs;
  const msgs = picked;
  if(!msgs.length){
    LNODES.clear();
    $('list').innerHTML = '<div class="empty">' + (kind === 'all' ? '暂无消息'
        : '还没有' + kindName() + '消息') + '</div>';
    $('cnt').textContent = '0 条' + (kind === 'all' ? '' : ' · 只看' + kindName());
    return;
  }
  const nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 80;
  /* 节点复用：上限调到 200/400 时，每 1.2 秒把几百行全量重建会明显卡。
     同样的内容（同会话同时间同文本）直接搬旧节点过来，只给新消息建 DOM。 */
  const box = $('list');
  const frag = document.createDocumentFragment();
  const keep = new Set();
  for(const m of msgs){
    const base = key(m);
    const isNew = inited && !seen[base];
    seen[base] = 1;
    const k = base + '\u0001' + (m.text || '') + '\u0001' + (m.isBot ? 1 : 0)
            + '\u0001' + (m.kind === 'dm' ? 1 : 0) + '\u0001' + (kind === 'all' ? 1 : 0);
    keep.add(k);
    let el = LNODES.get(k);
    if(!el){
      const cls = 'm' + (m.isBot ? ' bot' : '') + (m.kind === 'dm' ? ' dm' : '') + (isNew ? ' new' : '');
      // 专门看私聊的窗口里就不用每行再标一次「私聊」了
      const tag = (m.kind === 'dm' && kind === 'all') ? '<span class="tag">私聊</span>' : '';
      const gname = kind === 'dm'
          ? String(m.gname || '').replace(/^私聊\s*·\s*/, '') : (m.gname || '');
      const wrap = document.createElement('div');
      wrap.innerHTML = '<div class="' + cls + '">'
        + '<div class="meta"><span class="n">' + esc(m.name) + '</span>' + tag
        + '<span class="g">' + esc(gname) + '</span>'
        + '<span class="t">' + msgTime(m.time) + '</span></div>'
        + '<div class="txt">' + esc(m.text || '') + '</div></div>';
      el = wrap.firstElementChild;
      LNODES.set(k, el);
    }
    frag.appendChild(el);
  }
  for(const k of LNODES.keys()) if(!keep.has(k)) LNODES.delete(k);
  box.replaceChildren(frag);
  $('cnt').textContent = (kind === 'all' ? '' : '只看' + kindName() + ' · ')
      + '显示 ' + msgs.length + ' 条（共 ' + picked.length + ' 条）';
  if(!inited){ window.scrollTo(0, document.body.scrollHeight); inited = true; }
  else if(follow && nearBottom && Object.keys(seen).length) window.scrollTo(0, document.body.scrollHeight);
}

async function tick(){
  if(paused) return;
  try{
    const r = await fetch('/api/state', {cache: 'no-store'});
    render(await r.json());
  }catch(e){
    $('dc').className = 'dot off';
    $('pc').textContent = '服务断开';
  }
}
$('pin').addEventListener('click', () => {
  follow = !follow;
  $('pin').textContent = '自动滚动：' + (follow ? '开' : '关');
});
$('seg').addEventListener('click', e => {
  const b = e.target.closest('button[data-kind]');
  if(!b) return;
  const k = b.dataset.kind;
  kind = (k === 'dm' || k === 'group') ? k : 'all';
  if(lastD) render(lastD);
});
document.addEventListener('keydown', e => {
  if(e.key === ' ' && e.target.tagName !== 'BUTTON'){ paused = !paused; e.preventDefault(); }
});
tick();
setInterval(tick, 1500);
</script>
</body></html>
"""


def port_free(port):
    """端口是否可独占绑定。Windows 下 SO_REUSEADDR 允许端口被重复绑定，
    所以这里用不带 reuse 的裸 socket 探测，才能真正发现别的实例。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(description="群聊机器人 · 监听页")
    ap.add_argument("--port", type=int, default=PORT, help="起始端口，默认 %d，被占用会自动顺延" % PORT)
    ap.add_argument("--tries", type=int, default=10, help="最多尝试几个端口")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--allow-multi", action="store_true", help="允许同时开多个监听页")
    args = ap.parse_args()

    guard = None
    if not args.allow_multi:
        guard = acquire_single_instance()
        if guard is None:
            # 同一份数据已经有一个面板在跑了。别静默退出——用户会以为双击没反应。
            url = existing_panel_url(args.port)
            print("已经有一个面板在运行：%s" % (url or "（没找到端口）"), flush=True)
            if url:
                if args.no_browser or not open_browser(url):
                    alert_box("面板已经在运行了：\n\n%s\n\n浏览器没有自动打开的话，"
                              "把上面的地址复制到浏览器里即可。" % url)
            else:
                alert_box("检测到面板正在运行，但没能连上它的地址。\n\n"
                          "可能是它刚启动或正在退出。请等几秒再双击一次；\n"
                          "如果一直这样，到 data\\log\\console.log 看看，"
                          "或者删掉 data\\.panel.lock 后重试。", error=True)
            return 2

    server = None
    port = args.port
    for port in range(args.port, args.port + max(1, args.tries)):
        if not port_free(port):
            continue
        try:
            server = QuietServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            server = None
            continue
    if server is None:
        msg = "启动失败：%d–%d 这些端口都被占用了，请先关掉多余的监听窗口。" % (
            args.port, args.port + max(1, args.tries) - 1)
        print(msg, flush=True)
        alert_box(msg, error=True)
        return 1

    global BOUND_PORT
    BOUND_PORT = port
    if guard:
        _lock_write(port)        # 把真实端口记进锁文件，下次启动才能找回来

    server.daemon_threads = True
    if port != args.port:
        print("提示：端口 %d 已被另一个实例占用，本面板改用 %d。" % (args.port, port), flush=True)
        print("      如果你不是故意开了两份，那多半是另一份机器人还在运行，建议先关掉它。", flush=True)
    print("监听页面已启动： http://127.0.0.1:%d" % port, flush=True)
    print("关闭此窗口即停止监听。", flush=True)
    if not args.no_browser:
        url = "http://127.0.0.1:%d" % port
        if not open_browser(url):
            alert_box("面板已经启动，但没能自动打开浏览器。\n\n"
                      "请手动打开浏览器，访问：\n%s" % url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        shutdown_bot()
        _lock_release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
