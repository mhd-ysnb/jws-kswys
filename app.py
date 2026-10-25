# -*- coding: utf-8 -*-
"""
黑盒语音群聊机器人 —— 统一入口

打包成 exe 后只有一个可执行文件，靠参数决定跑哪一半：

    黑盒语音群聊机器人.exe              启动面板（默认，双击就是这个）
    黑盒语音群聊机器人.exe --bot        只跑机器人本体
    黑盒语音群聊机器人.exe --bot --check 自检

源码运行时仍可直接 python bot.py / python monitor.py，行为不变。
"""
import os
import sys


def _redirect_console():
    """打包成无控制台程序后 sys.stdout 是 None，任何 print 都会抛异常。
    这里把它接到 data/log/console.log，出问题时还能翻到启动阶段的输出。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    here = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                                           else __file__))
    log_dir = os.path.join(here, "data", "log") if getattr(sys, "frozen", False) \
        else os.path.join(here, "log")
    try:
        os.makedirs(log_dir, exist_ok=True)
        f = open(os.path.join(log_dir, "console.log"), "a", encoding="utf-8", errors="replace")
    except OSError:
        f = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


def main():
    _redirect_console()
    argv = sys.argv[1:]
    if "--bot" in argv:
        # 把 --bot 摘掉，剩下的参数原样交给机器人（--check / --groups …）
        sys.argv = [sys.argv[0]] + [a for a in argv if a != "--bot"]
        import bot
        return bot.main()
    import monitor
    return monitor.main()


if __name__ == "__main__":
    sys.exit(main())
