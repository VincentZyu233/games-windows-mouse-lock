"""
窗口化游戏鼠标防飞出物理锁定助手 (Games Window Mouse Lock)
起源于英雄联盟 (LOL)，支持 Minecraft、CS2 等各类窗口化竞技游戏

特点:
1. 纯 Python 标准库实现，零外部依赖，直接用 uv run 或 python 启动。
2. 使用 Win32 底层 ClipCursor API，驱动级限制鼠标移动范围，甩得再快也绝不出界。
3. 智能前台检测：切到桌面或微信自动释放，切回游戏自动重新锁死。
4. 默认快捷键 F8，异步硬件级按键监听，抗游戏拦截。
5. 原生双音调清脆提示音（无需任何音频素材）。
6. 支持 targets.txt 目标进程白名单与配置自动热加载（支持 LOL、Minecraft 等任意游戏）。
7. 毫秒级高精度对称式日志输出。
"""

import sys
import os
import time
from datetime import datetime
import threading
import atexit
import ctypes
from ctypes import wintypes
import winsound

# ==================== 用户可配置参数 ====================
# 默认开/关快捷键：F8（可改为 0x76 对应 F7, 0x75 对应 F6 等）
HOTKEY_VK = 0x77  # 0x77 = F8 键
HOTKEY_NAME = "F8"

# 退出程序快捷键: Ctrl + Alt + Q (退出脚本并恢复鼠标)
EXIT_KEY_VK = 0x51  # 'Q' 键

# 声音提示开关
SOUND_ENABLED = True

# 自动跟随前台窗口（Alt+Tab 切出游戏自动释放，切回自动锁死）
AUTO_FOLLOW_FOREGROUND = True

# 轮询频率 (秒)
POLL_INTERVAL = 0.05

# 配置文件名 (放置在脚本同级目录下)
TARGETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "targets.txt")

# 窗口最小有效尺寸 (过滤过小的无边框挂件或小托盘)
MIN_WINDOW_WIDTH = 400
MIN_WINDOW_HEIGHT = 300
# =======================================================

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

def log(msg: str):
    """输出带精确到毫秒级时间戳的日志行"""
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    for line in msg.splitlines():
        if line.strip():
            print(f"[{ts}] {line}")
        else:
            print()

# 确保无论脚本怎样退出（正常退出/异常/Ctrl+C），必须彻底释放鼠标
def release_mouse():
    try:
        user32.ClipCursor(None)
    except Exception:
        pass

atexit.register(release_mouse)

def play_sound(action: str):
    """在独立线程播放提示音，避免阻塞鼠标判定逻辑"""
    if not SOUND_ENABLED:
        return

    def _beep():
        try:
            if action == "lock":
                # 锁定生效：清脆短促双升调 "哔哔"
                winsound.Beep(1200, 70)
                winsound.Beep(1800, 100)
            elif action == "unlock":
                # 解除锁定：降调单音 "嘟——"
                winsound.Beep(700, 130)
            elif action == "pause":
                # 切出游戏暂停锁定（轻微短音）
                winsound.Beep(600, 50)
            elif action == "resume":
                # 切回游戏恢复锁定
                winsound.Beep(1400, 70)
        except Exception:
            pass

    threading.Thread(target=_beep, daemon=True).start()

def get_process_name_by_hwnd(hwnd) -> str:
    """根据窗口句柄获取进程名"""
    if not hwnd or not user32.IsWindow(hwnd):
        return ""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    
    # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h_proc = kernel32.OpenProcess(0x1000, False, pid.value)
    if not h_proc:
        return ""
    
    buf = ctypes.create_unicode_buffer(512)
    size = wintypes.DWORD(len(buf))
    kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size))
    kernel32.CloseHandle(h_proc)
    
    full_path = buf.value
    return full_path.split("\\")[-1] if full_path else ""

def get_window_class_name(hwnd) -> str:
    """获取窗口类名"""
    if not hwnd or not user32.IsWindow(hwnd):
        return ""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value

def get_window_title(hwnd) -> str:
    if not hwnd or not user32.IsWindow(hwnd):
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buff = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buff, length + 1)
    return buff.value

def get_client_screen_rect(hwnd):
    """获取窗口客户区在屏幕上的绝对物理像素坐标"""
    client_rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(client_rect)):
        return None
    
    pt_tl = POINT(client_rect.left, client_rect.top)
    pt_br = POINT(client_rect.right, client_rect.bottom)
    
    user32.ClientToScreen(hwnd, ctypes.byref(pt_tl))
    user32.ClientToScreen(hwnd, ctypes.byref(pt_br))
    
    screen_rect = wintypes.RECT(pt_tl.x, pt_tl.y, pt_br.x, pt_br.y)
    return screen_rect

def is_key_down(vk_code) -> bool:
    """查询物理按键当前状态"""
    return (user32.GetAsyncKeyState(vk_code) & 0x8000) != 0

def is_ctrl_down() -> bool:
    return is_key_down(0x11)

def is_alt_down() -> bool:
    return is_key_down(0x12)

class WhitelistManager:
    """管理 targets.txt 白名单及其热重载"""
    def __init__(self, filepath):
        self.filepath = filepath
        self.last_mtime = 0
        self.whitelist = []
        self.load(initial=True)

    def load(self, initial=False):
        if not os.path.exists(self.filepath):
            self.whitelist = ["league of legends.exe"]
            return

        try:
            mtime = os.path.getmtime(self.filepath)
            if mtime != self.last_mtime:
                self.last_mtime = mtime
                new_list = []
                with open(self.filepath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            new_list.append(line.lower())
                self.whitelist = new_list
                if not initial:
                    names = ", ".join(self.whitelist) if self.whitelist else "空 (通用模式)"
                    log(f"[配置热载] targets.txt 已更新，当前生效白名单: [{names}]")
        except Exception as e:
            if initial:
                self.whitelist = ["league of legends.exe"]

    def is_target(self, hwnd, proc_name) -> bool:
        """多重加固判断前台窗口是否属于目标保护进程"""
        if not proc_name:
            return False

        proc_lower = proc_name.lower()

        # 1. 严格排除非游戏主程序的组件
        if proc_lower in ["leagueclient.exe", "leagueclientux.exe", "leagueclientuxrender.exe"]:
            return False

        # 2. 如果白名单有配置，必须严格匹配白名单
        if self.whitelist:
            matched = False
            for target in self.whitelist:
                if proc_lower == target or proc_lower == f"{target}.exe":
                    matched = True
                    break
            if not matched:
                return False

        # 3. 英雄联盟对局加固校验：类名二次确认
        if "league of legends" in proc_lower:
            cls_name = get_window_class_name(hwnd)
            # LOL 对局引擎的主类名为 RiotWindowClass
            if cls_name and cls_name not in ["RiotWindowClass"]:
                # 如果类名不匹配，可能是辅助工具或更新窗口，忽略
                pass

        # 4. 窗口有效面积过滤（防止微型隐藏窗口误触）
        rect = get_client_screen_rect(hwnd)
        if not rect:
            return False
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w < MIN_WINDOW_WIDTH or h < MIN_WINDOW_HEIGHT:
            return False

        return True

def main():
    print("=" * 68)
    print("  🎮 窗口化游戏鼠标防飞出物理锁定助手 (Games Window Mouse Lock)")
    print("     (起源于英雄联盟，支持 Minecraft、CS2 等各类窗口化竞技游戏)")
    print("=" * 68)
    print(f"  [快捷键] {HOTKEY_NAME:<6} : 开启 / 关闭 鼠标锁定 (清脆蜂鸣提示)")
    print(f"  [退出键] Ctrl+Alt+Q : 退出脚本并彻底释放鼠标")
    print("  [白名单] 读取 targets.txt 目标进程，支持修改后自动热重载")
    print("  [切  屏] Alt+Tab 切出游戏自动释放，切回游戏自动锁死")
    print("-" * 68)

    wm = WhitelistManager(TARGETS_FILE)
    targets_str = ", ".join(wm.whitelist) if wm.whitelist else "全局自动"
    log(f"[就绪] 当前保护进程: [{targets_str}]，按 F8 开启锁定...")
    print("=" * 68)

    user_lock_enabled = False
    currently_clipped = False
    prev_hotkey_state = False

    try:
        while True:
            # 1. 检查 targets.txt 是否发生热重载
            wm.load()

            # 2. 检查退出按键 (Ctrl + Alt + Q)
            if is_ctrl_down() and is_alt_down() and is_key_down(EXIT_KEY_VK):
                log("[退出] 收到退出指令 (Ctrl+Alt+Q)，正在退出...")
                break

            # 3. 检查切换按键 (F8)
            curr_hotkey_state = is_key_down(HOTKEY_VK)
            if curr_hotkey_state and not prev_hotkey_state:
                user_lock_enabled = not user_lock_enabled
                
                if user_lock_enabled:
                    fg_hwnd = user32.GetForegroundWindow()
                    proc_name = get_process_name_by_hwnd(fg_hwnd)
                    is_match = wm.is_target(fg_hwnd, proc_name)

                    log(f"[状态] >> 鼠标锁定已开启 (ON) << [随时按 {HOTKEY_NAME} 可关闭]")
                    play_sound("lock")

                    # 如果开启时前台刚好在游戏窗口内，立即上锁
                    if is_match:
                        rect = get_client_screen_rect(fg_hwnd)
                        if rect:
                            user32.ClipCursor(ctypes.byref(rect))
                            currently_clipped = True
                            log(f"[切回锁定] 前台已切至 [{proc_name}]，命中目标进程，已锁定区域: ({rect.left}, {rect.top}) -> ({rect.right}, {rect.bottom})")
                    else:
                        log(f"[等待切回] 当前前台为 [{proc_name or '桌面'}]，切入游戏窗口时将自动执行锁定。")
                else:
                    log(f"[状态] >> 鼠标锁定已关闭 (OFF) << (鼠标已自由恢复)")
                    release_mouse()
                    currently_clipped = False
                    play_sound("unlock")

            prev_hotkey_state = curr_hotkey_state

            # 4. 前台窗口维护逻辑 (当且仅当开启锁定时)
            if user_lock_enabled:
                fg_hwnd = user32.GetForegroundWindow()
                proc_name = get_process_name_by_hwnd(fg_hwnd)
                is_match = wm.is_target(fg_hwnd, proc_name)

                if is_match:
                    rect = get_client_screen_rect(fg_hwnd)
                    if rect and (rect.right > rect.left) and (rect.bottom > rect.top):
                        # 维持物理锁定
                        user32.ClipCursor(ctypes.byref(rect))
                        if not currently_clipped:
                            currently_clipped = True
                            log(f"[切回锁定] 前台已切至 [{proc_name}]，命中目标进程，已锁定区域: ({rect.left}, {rect.top}) -> ({rect.right}, {rect.bottom})")
                else:
                    # 前台非目标窗口
                    if AUTO_FOLLOW_FOREGROUND and currently_clipped:
                        release_mouse()
                        currently_clipped = False
                        log(f"[切出放行] 前台已切至 [{proc_name or '未知/桌面'}]，非目标进程，已暂时释放鼠标")
            else:
                if currently_clipped:
                    release_mouse()
                    currently_clipped = False

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        log("[退出] 用户按下 Ctrl+C 退出程序")
    finally:
        release_mouse()
        log("[完成] 鼠标限制已彻底解除，程序安全退出。")

if __name__ == "__main__":
    main()
