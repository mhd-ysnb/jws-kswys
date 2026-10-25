# -*- coding: utf-8 -*-
"""面板窗口版 —— 双击就是一个 app，不是“打开浏览器输网址”。

面板本体（monitor.py）一行没改：它还是那个 8765 的网页，这里只给它一张脸。
「像不像 app」取决于三件事，都在这儿做：

    托盘常驻   关窗口＝缩到托盘，程序还在，机器人照跑
    单实例     再双击只把已有窗口叫到前面（靠本机一个小端口握手），不开第二份
    记得位置   关之前把 x/y/宽/高 写进 data/app_window.json，下次原样回来

跑： pythonw desktop.py      （双击 启动面板(窗口版).bat 也是这个）
依赖：pywebview（走系统 WebView2）、pystray、pillow

注意：根目录的 app.py 是打包 exe 用的统一入口，别和这个混了。
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

import webview
import pystray
from PIL import Image, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.abspath(__file__))
# 面板的锁文件位置跟它自己的 DATA_DIR 保持一致：源码跑在项目根，打包后才在 data\
FROZEN = getattr(sys, "frozen", False)
DATA = os.path.join(BASE, "data") if FROZEN else BASE
LOCK = os.path.join(DATA, ".panel.lock")
WCFG = os.path.join(DATA, "app_window.json")
CTRL_PORT = 8799                 # 单实例握手口，只连本机
PYEXE = sys.executable
NO_WINDOW = 0x08000000           # CREATE_NO_WINDOW：拉面板时别闪黑框

_child = None                    # 只关我们自己拉起来的面板，别人开着的别动
_win = None
_icon = None
_allow_close = False
_ctrl_srv = None
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------- 小工具
def tcp_ok(port, timeout=0.4):
    try:
        s = socket.create_connection(("127.0.0.1", int(port)), timeout)
        s.close()
        return True
    except OSError:
        return False


def panel_port():
    """面板实际监听的端口。锁文件里写的才准——8765 被占时会顺延到 8766/8767"""
    try:
        with open(LOCK, encoding="utf-8") as f:
            p = int(json.load(f).get("port") or 0)
        return p if p and tcp_ok(p) else 0
    except Exception:
        return 0


def http_json(method, path, body=None, timeout=20):
    port = panel_port()
    if not port:
        return {"ok": False, "error": "面板没在跑"}
    data = json.dumps(body if body is not None else {}).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                 data=data if method == "POST" else None,
                                 method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def ensure_panel():
    """已经有一份在跑就接上去；没有就自己拉一份（不开浏览器、不闪黑框）"""
    global _child
    p = panel_port()
    if p:
        return p
    _child = subprocess.Popen(
        [PYEXE, os.path.join(BASE, "monitor.py"), "--no-browser"],
        cwd=BASE, creationflags=NO_WINDOW,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    for _ in range(75):                 # 最多等 30 秒
        time.sleep(0.4)
        p = panel_port()
        if p:
            return p
        if _child.poll() is not None:
            return 0
    return 0


def load_geo():
    try:
        with open(WCFG, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_geo():
    if not _win:
        return
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(WCFG, "w", encoding="utf-8") as f:
            json.dump({"x": _win.x, "y": _win.y,
                       "w": _win.width, "h": _win.height}, f)
    except Exception:
        pass


def show_window(*_):
    if not _win:
        return
    try:
        _win.show()
        _win.restore()
    except Exception:
        pass


def make_dpi_aware():
    """必须最先做。不做的话进程被系统当成 96 DPI（Windows 会替我们虚拟化坐标），
    而 WebView2 子进程是按真实缩放（比如 150%）渲染的——页面就会以 1.5 倍画进一个
    只有 1/1.5 大的窗口里，右边被硬切掉（真实的坑，2026-10-26 02:20 踩的）。"""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)      # 每显示器 DPI 感知
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def dpi_scale():
    """系统缩放。优先用 GetScaleFactorForDevice —— pythonnet/WinForms 在 import 时
    可能已经替我们设过 DPI 模式，这时 GetDeviceCaps 会回 96 骗人（实测过），
    而设备缩放总是真实的。"""
    try:
        import ctypes
        pct = ctypes.windll.shcore.GetScaleFactorForDevice(0)
        if pct:
            return max(1.0, pct / 100.0)
    except Exception:
        pass
    try:
        import ctypes
        u = ctypes.windll.user32
        g = ctypes.windll.gdi32
        hdc = u.GetDC(0)
        dpi = g.GetDeviceCaps(hdc, 88)                      # LOGPIXELSX
        u.ReleaseDC(0, hdc)
        return max(1.0, dpi / 96.0)
    except Exception:
        return 1.0


def screen_workarea():
    """屏幕可用区（扣掉任务栏），物理像素。取不到就给个保守值。"""
    try:
        import ctypes
        import ctypes.wintypes as wt
        r = wt.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    except Exception:
        pass
    return (0, 0, 1280, 800)


def size_for(geo):
    """窗口尺寸和位置，**全是 DIP**（pywebview/WinForms 认这个，1 DIP = 缩放 × 物理像素）。

    坑：屏幕工作区拿回来是物理像素，几何文件里存的是 DIP，
    不换算直接比，就会出现「x=1070 宽 1700」在 2560 的屏上跑到外面去。
    """
    sx, sy, sw, sh = screen_workarea()
    sc = dpi_scale()
    sw, sh = int(sw / sc), int(sh / sc)          # 工作区换成 DIP
    sx, sy = int(sx / sc), int(sy / sc)
    w = int(geo.get("w") or 1700)
    h = int(geo.get("h") or 920)
    w = max(900, min(w, sw - 20))
    h = max(620, min(h, sh - 20))
    x, y = geo.get("x"), geo.get("y")
    if x is None or not (sx - 20 <= x <= sx + sw - 200):
        x = sx + (sw - w) // 2
    if y is None or not (sy - 20 <= y <= sy + sh - 200):
        y = sy + (sh - h) // 2
    x = max(sx, min(int(x), sx + sw - w))        # 别让右/下边露到屏幕外
    y = max(sy, min(int(y), sy + sh - h))
    return int(w), int(h), int(x), int(y)


def _hwnd_by_title(title):
    try:
        import ctypes
        import ctypes.wintypes as wt
        u = ctypes.windll.user32
        E = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        hit = []

        def cb(h, l):
            n = u.GetWindowTextLengthW(h)
            if n:
                b = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(h, b, n + 1)
                if b.value == title:
                    hit.append(h)
            return True
        u.EnumWindows(E(cb), 0)
        return hit[0] if hit else 0
    except Exception:
        return 0


def fix_position(window):
    """建完之后按真实窗口矩形纠一次位置。

    为什么不在建之前算：这台机器上「进程的 DPI 缩放」取不准（pythonnet 先替我们
    设过模式，GetDeviceCaps 回 96），算出来 1700 DIP 的窗口实际是 2550 物理像素，
    结果右半边跑到屏幕外。所以改成建完再量、再挪——物理像素是骗不了人的。
    """
    try:
        import ctypes
        import ctypes.wintypes as wt
        u = ctypes.windll.user32
        title = "黑盒语音面板"
        h = _hwnd_by_title(title)
        if not h:
            return False
        r = wt.RECT()
        u.GetWindowRect(h, ctypes.byref(r))
        pw, ph = r.right - r.left, r.bottom - r.top
        ww = int(window.width or pw) or pw
        sc = (pw / float(ww)) if ww else 1.0          # 物理 / DIP，实测出来的
        sx, sy, sw, sh = screen_workarea()            # 物理像素
        avail_w, avail_h = sw - 20, sh - 60
        if pw > avail_w or ph > avail_h:
            nw, nh = int(avail_w / sc), int(avail_h / sc)
            window.resize(nw, nh)
            window.move(int(sx / sc) + 5, int(sy / sc) + 5)
            print("屏装不下 %dx%d 物理，改成 %dx%d DIP" % (pw, ph, nw, nh), flush=True)
            return True
        x = min(max(r.left, sx), sx + sw - pw)
        y = min(max(r.top, sy), sy + sh - 60 - ph)
        if abs(x - r.left) > 2 or abs(y - r.top) > 2:
            window.move(int(x / sc), int(y / sc))
            print("挪回屏内：物理 (%d,%d)" % (x, y), flush=True)
        return True
    except Exception as e:
        print("fix_position 失败：%s: %s" % (type(e).__name__, e), flush=True)
        return False


def fit_width(window):
    """兜底：真装不下就整体缩一点，宁可字小一号，也别让人看不见右边的页签。

    挂在 documentElement 上、每次先清空再设，避免重复触发时缩放叠乘
    （body 上连设两次 zoom=1.5 会变成 2.25，页面直接塌成单栏——踩过）。
    """
    try:
        js = ("(function(){var el=document.documentElement;"
              "el.style.zoom='';"
              "var vw=el.clientWidth, sw=el.scrollWidth;"
              "if(sw>vw+4){el.style.zoom=(vw/sw).toFixed(3);}"
              "return [vw, sw, el.style.zoom||'1'];})()")
        r = window.evaluate_js(js) or [0, 0, "1"]
        print("视口 %s · 页面 %s · 缩放 %s" % tuple(r), flush=True)
    except Exception as e:
        print("fit_width 失败：%s: %s" % (type(e).__name__, e), flush=True)


# ---------------------------------------------------------------- 托盘
def make_icon():
    """现画一个图标：蓝底 + 「菲」。不依赖图片文件，改色只改这一行。"""
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((2, 2, 61, 61), fill=(37, 99, 235, 255))
    try:
        f = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 36)
        d.text((32, 33), "菲", font=f, fill=(255, 255, 255, 255), anchor="mm")
    except Exception:
        d.ellipse((23, 23, 41, 41), fill=(255, 255, 255, 255))
    return im


def tray_start_bot(icon=None, item=None):
    http_json("POST", "/api/bot/start", {})


def tray_stop_bot(icon=None, item=None):
    http_json("POST", "/api/bot/stop", {})


def tray_pause(icon=None, item=None):
    http_json("POST", "/api/toggle", {"enabled": False})


def tray_resume(icon=None, item=None):
    http_json("POST", "/api/toggle", {"enabled": True})


def tray_browser(icon=None, item=None):
    port = panel_port()
    if port:
        webbrowser.open("http://127.0.0.1:%d" % port)


def tray_quit(icon=None, item=None):
    quit_app()


def build_menu():
    return pystray.Menu(
        pystray.MenuItem("打开面板", show_window, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("启动机器人", tray_start_bot),
        pystray.MenuItem("停机器人（先写日记）", tray_stop_bot),
        pystray.MenuItem("暂停回复", tray_pause),
        pystray.MenuItem("恢复回复", tray_resume),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("在浏览器里打开", tray_browser),
        pystray.MenuItem("退出", tray_quit),
    )


def start_tray():
    global _icon
    _icon = pystray.Icon("heihe", make_icon(), "黑盒语音面板", build_menu())
    threading.Thread(target=_icon.run, daemon=True).start()


# ---------------------------------------------------------------- 单实例
def _ctrl_loop(srv):
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        try:
            data = conn.recv(64)
        except Exception:
            data = b""
        try:
            conn.close()
        except Exception:
            pass
        if b"show" in data:
            show_window()


def become_primary():
    """抢单实例口。抢不到＝已经有一份在跑：叫它一声，然后自己退。"""
    global _ctrl_srv
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", CTRL_PORT))
        s.listen(4)
    except OSError:
        try:
            c = socket.create_connection(("127.0.0.1", CTRL_PORT), 2)
            c.sendall(b"show\n")
            c.close()
        except Exception:
            pass
        return False
    _ctrl_srv = s
    threading.Thread(target=_ctrl_loop, args=(s,), daemon=True).start()
    return True


# ---------------------------------------------------------------- 关闭
def on_closing():
    """点 × 只是缩到托盘。真要退出走托盘菜单的「退出」。"""
    if _allow_close:
        return True
    save_geo()
    try:
        _win.hide()
    except Exception:
        pass
    if _icon and not getattr(_icon, "_told", False):
        _icon._told = True
        try:
            _icon.notify("还在后台跑，右键图标能叫回来", "缩到托盘了")
        except Exception:
            pass
    return False


def quit_app():
    global _allow_close
    _allow_close = True
    save_geo()
    try:
        if _icon:
            _icon.stop()
    except Exception:
        pass
    # 自己拉起来的面板才归我们关：先请它收尾（会写日记），等不到再强关
    if _child is not None and _child.poll() is None and panel_port():
        try:
            if http_json("GET", "/api/bot/status", timeout=6).get("running"):
                http_json("POST", "/api/bot/stop", {}, timeout=10)
        except Exception:
            pass
        http_json("POST", "/api/quit", {}, timeout=8)
        for _ in range(24):             # 最多等 12 秒
            if _child.poll() is not None:
                break
            time.sleep(0.5)
        if _child.poll() is None:
            try:
                subprocess.run(["taskkill", "/PID", str(_child.pid), "/T", "/F"],
                               creationflags=NO_WINDOW, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            except Exception:
                pass
    try:
        if _win:
            _win.destroy()
    except Exception:
        pass


def _fix_stdout():
    """用 pythonw 起的时候 sys.stdout 是 None，任何 print 都会抛异常。
    接到 data 里的一个日志文件上，出问题还能翻。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        os.makedirs(DATA, exist_ok=True)
        f = open(os.path.join(DATA, "窗口日志.txt"), "a", encoding="utf-8", errors="replace")
    except Exception:
        f = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


def main():
    _fix_stdout()
    make_dpi_aware()                    # 必须在建窗口之前
    if not become_primary():
        return 0                        # 已经有一份，窗口交给它去叫
    port = ensure_panel()
    if not port:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, "面板没起来。\n\n看看 data\\log\\console.log 里最后几行。",
                "黑盒语音面板", 0x10)
        except Exception:
            print("面板没起来")
        return 1

    global _win
    geo = load_geo()
    w, h, x, y = size_for(geo)
    sc = dpi_scale()
    _win = webview.create_window(
        "黑盒语音面板",
        "http://127.0.0.1:%d" % port,
        width=w, height=h, x=x, y=y,
        min_size=(int(900 * sc), int(620 * sc)),
        text_select=True,
    )
    _win.events.closing += on_closing

    def _loaded():
        # 窗口刚 loaded 时标题/句柄可能还没就绪，隔一会儿再纠一次位置
        def _later():
            for _ in range(12):
                time.sleep(0.5)
                if fix_position(_win):
                    break
        threading.Thread(target=_later, daemon=True).start()
        fit_width(_win)

    _win.events.loaded += _loaded
    start_tray()
    # private_mode=False：面板有些偏好存在 localStorage，别每次开都当新的
    webview.start(private_mode=False)
    try:
        if _icon:
            _icon.stop()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
