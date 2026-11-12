"""
英雄联盟 (League of Legends) 窗口化鼠标防飞出锁定工具
Mouse Lock for League of Legends (Windowed / Borderless)

特点:
1. 纯 Python 标准库实现，零外部依赖，直接用 uv run 或 python 启动。
2. 使用 Win32 底层 ClipCursor API，驱动级限制鼠标移动范围，甩得再快也绝不出界。
3. 智能前台检测：切到桌面或微信自动释放，切回游戏自动重新锁死。
4. 默认快捷键 F8（LOL 默认无冲突），异步硬件级按键监听，抗拦截。
5. 原生双音调清脆提示音（无需任何音频素材）。
6. 支持测试模式：不在对局时也可以用来测试锁定当前任意窗口。
7. 毫秒级高精度日志时间戳输出。
"""

import sys
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

# 目标模式:
# - "AUTO": 优先检测 LOL 对局窗口；若不在对局中，按 F8 会锁住当前活动窗口方便测试体验
# - "LOL_ONLY": 严格仅在 LOL 对局窗口中锁定
TARGET_MODE = "AUTO"
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
    if not hwnd:
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

def get_window_title(hwnd) -> str:
    if not hwnd:
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buff = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buff, length + 1)
    return buff.value

def find_lol_game_window():
    """查找英雄联盟对局窗口"""
    hwnd = user32.FindWindowW("RiotWindowClass", None)
    if hwnd and user32.IsWindowVisible(hwnd):
        return hwnd
    
    hwnd = user32.FindWindowW(None, "League of Legends (TM) Client")
    if hwnd and user32.IsWindowVisible(hwnd):
        return hwnd
        
    return None

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

def main():
    print("=" * 65)
    print("  英雄联盟 (LOL) 窗口化鼠标防飞出锁定助手")
    print("=" * 65)
    print(f"  [快捷键] {HOTKEY_NAME:<6} : 开启 / 关闭 鼠标锁定 (清脆蜂鸣提示)")
    print(f"  [退出键] Ctrl+Alt+Q : 退出脚本并彻底释放鼠标")
    print("  [特  性] 物理锁死在游戏窗口内，甩得再快也绝不出界")
    print("  [切  屏] Alt+Tab 切出游戏自动释放，切回游戏自动锁死")
    print("-" * 65)
    log("[就绪] 服务已在后台运行中，请进入游戏或切至测试窗口后按 F8 开启...")
    print("=" * 65)

    user_lock_enabled = False
    currently_clipped = False
    locked_hwnd = None
    prev_hotkey_state = False

    try:
        while True:
            # 1. 检查退出按键 (Ctrl + Alt + Q)
            if is_ctrl_down() and is_alt_down() and is_key_down(EXIT_KEY_VK):
                log("[退出] 收到退出指令 (Ctrl+Alt+Q)，正在退出...")
                break

            # 2. 检查切换按键 (F8)
            curr_hotkey_state = is_key_down(HOTKEY_VK)
            if curr_hotkey_state and not prev_hotkey_state:
                user_lock_enabled = not user_lock_enabled
                
                if user_lock_enabled:
                    # 获取当前前台窗口
                    fg_hwnd = user32.GetForegroundWindow()
                    proc_name = get_process_name_by_hwnd(fg_hwnd)
                    title = get_window_title(fg_hwnd)

                    lol_hwnd = find_lol_game_window()
                    if lol_hwnd:
                        locked_hwnd = lol_hwnd
                        target_desc = "英雄联盟对局窗口"
                    elif "League of Legends" in proc_name:
                        locked_hwnd = fg_hwnd
                        target_desc = "英雄联盟对局窗口"
                    elif TARGET_MODE == "AUTO":
                        locked_hwnd = fg_hwnd
                        target_desc = f"当前前台窗口 [{title or proc_name}]"
                    else:
                        locked_hwnd = None
                        target_desc = "等待英雄联盟进入对局..."

                    log(f"[状态] >> 鼠标锁定已开启 (ON) <<")
                    log(f"[目标] {target_desc}")
                    log(f"[提示] 随时按 [{HOTKEY_NAME}] 可关闭锁定。")
                    play_sound("lock")
                else:
                    log(f"[状态] >> 鼠标锁定已关闭 (OFF) << (鼠标已自由恢复)")
                    release_mouse()
                    currently_clipped = False
                    locked_hwnd = None
                    play_sound("unlock")

            prev_hotkey_state = curr_hotkey_state

            # 3. 锁定维护逻辑
            if user_lock_enabled:
                fg_hwnd = user32.GetForegroundWindow()

                # 如果尚未找到 LOL 对局窗口，持续尝试嗅探
                if not locked_hwnd or not user32.IsWindow(locked_hwnd):
                    lol_hwnd = find_lol_game_window()
                    if lol_hwnd:
                        locked_hwnd = lol_hwnd

                if locked_hwnd and user32.IsWindow(locked_hwnd):
                    # 判断当前用户是否在目标窗口内
                    if fg_hwnd == locked_hwnd:
                        rect = get_client_screen_rect(locked_hwnd)
                        if rect and (rect.right > rect.left) and (rect.bottom > rect.top):
                            user32.ClipCursor(ctypes.byref(rect))
                            if not currently_clipped:
                                log(f"[锁定生效] 已将光标物理锁在窗口区域: ({rect.left}, {rect.top}) -> ({rect.right}, {rect.bottom})")
                                currently_clipped = True
                    else:
                        # 切到其他程序了
                        if AUTO_FOLLOW_FOREGROUND and currently_clipped:
                            release_mouse()
                            currently_clipped = False
                            other_proc = get_process_name_by_hwnd(fg_hwnd)
                            log(f"[切出暂停] 前台已切至 [{other_proc or '桌面'}]，已暂时解除锁定")
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
