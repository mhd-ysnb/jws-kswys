# -*- coding: utf-8 -*-
"""
停止正在运行的机器人与监听页

比直接关窗口更彻底：关窗口有时只结束父进程，真正的 python 子进程会留在后台继续跑，
表现就是「窗口关掉了，机器人还在回消息」。

用法：python stop.py            结束本机所有 bot.py / monitor.py
      python stop.py --list     只看不杀
"""
import argparse
import os
import subprocess
import sys

QUERY_SCRIPT = (
    "Get-CimInstance Win32_Process | Where-Object { "
    "$_.Name -like 'python*' -and $_.CommandLine -match 'bot\\.py|monitor\\.py' "
    "} | ForEach-Object { "
    "$kind = if ($_.CommandLine -match 'monitor\\.py') { 'monitor' } else { 'bot' }; "
    "Write-Output ([string]$_.ProcessId + ' ' + $kind) }"
)


def find_targets():
    """返回 [(pid, 类型)]。用 PowerShell 查命令行走 subprocess 参数传递，
    不受 cmd 引号与中文编码影响。"""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", QUERY_SCRIPT],
                           capture_output=True, text=True, timeout=30,
                           errors="ignore")
    except Exception as e:
        print("查询进程失败：%s" % e)
        return []
    if r.returncode != 0 and not r.stdout.strip():
        print("查询进程失败：%s" % (r.stderr or "").strip()[:200])
        return []

    out = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit():
            out.append((int(parts[0]), parts[1]))
    return out


def kill(pid):
    try:
        r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="停止机器人与监听页")
    ap.add_argument("--list", action="store_true", help="只列出，不结束")
    a = ap.parse_args()

    targets = find_targets()
    if not targets:
        print("没有发现正在运行的机器人或监听页。")
        return 0

    if a.list:
        print("发现 %d 个进程：" % len(targets))
        for pid, kind in targets:
            print("  PID %-7d %s" % (pid, "监听页 monitor.py" if kind == "monitor" else "机器人 bot.py"))
        return 0

    print("找到 %d 个进程，开始结束……" % len(targets))
    ok = 0
    for pid, kind in targets:
        name = "监听页" if kind == "monitor" else "机器人"
        if kill(pid):
            ok += 1
            print("  已结束 PID %-7d %s" % (pid, name))
        else:
            print("  已跳过 PID %-7d %s（可能已经退出）" % (pid, name))

    left = find_targets()
    print()
    if left:
        print("仍有 %d 个进程没结束：" % len(left))
        for pid, kind in left:
            print("  PID %d %s" % (pid, kind))
        print("可以让它们再跑一次本脚本，或在任务管理器里结束 python.exe。")
        return 1
    print("已全部结束（共 %d 个）。" % ok)
    return 0


if __name__ == "__main__":
    sys.exit(main())
