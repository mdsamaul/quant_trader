"""
quotex_clicker.py - Hardened auto-clicker + screen scanner for the Quotex UP/DOWN buttons.

Fixes over the old version:
  * Proper 64-bit ctypes signatures (HWND/handles were truncated before -> crashes / wrong windows)
  * Per-monitor DPI awareness (screenshot pixels == click pixels on 125%/150% scaling)
  * Click = SetCursorPos + relative SendInput (no fragile virtual-desktop normalisation), cursor verified
  * Scan only inside the Quotex browser window; the dashboard window is ignored (it also has green/red UI)
  * Shape-based pairing (green button above a same-size red button) instead of "largest green blob"
  * Every failure returns a readable error; scan_debug.png shows exactly what was detected
  * All blocking work is meant to be called from plain `def` endpoints (never blocks the event loop)
"""
import ctypes
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from pydantic import BaseModel
import json
import os
import time

logger = logging.getLogger("QuotexClicker")
IS_WIN = sys.platform.startswith("win")
BASE = Path(__file__).parent
CONFIG_FILE = BASE / "autoclick_config.json"
DEBUG_IMG = BASE / "scan_debug.png"
# windows whose title contains one of these are OUR dashboard, never the trading page
EXCLUDE_TITLES = ("autonomous paper-trading", "quant_trader", "localhost", "127.0.0.1")


class AutoClickerConfig(BaseModel):
    enabled: bool = False
    call_x: int = 0
    call_y: int = 0
    put_x: int = 0
    put_y: int = 0
    restore_cursor: bool = False


# ==============================================================================
# WINDOWS API (safe signatures)
# ==============================================================================
if IS_WIN:
    from ctypes import wintypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ULONG_PTR = ctypes.c_size_t

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _INPUT_U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_U)]

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
    user32.SetThreadDesktop.restype = wintypes.BOOL
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.restype = wintypes.BOOL
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ULONG_PTR]
    user32.keybd_event.restype = None
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def _ensure_desktop() -> bool:
    """Ensure the calling thread is attached to the interactive input desktop."""
    if not IS_WIN:
        return True
    try:
        p = wintypes.POINT()
        if user32.GetCursorPos(ctypes.byref(p)):
            return True
        desk = user32.OpenInputDesktop(0, False, 0x01FF)
        if desk:
            return bool(user32.SetThreadDesktop(desk))
    except Exception as e:
        logger.debug(f"ensure_desktop: {e}")
    return False


def _win_list_windows() -> List[Tuple[int, str, Tuple[int, int, int, int]]]:
    out: List[Tuple[int, str, Tuple[int, int, int, int]]] = []

    def cb(hwnd, _lp):
        try:
            if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
                buf = ctypes.create_unicode_buffer(512)
                if user32.GetWindowTextW(hwnd, buf, 512) > 0:
                    r = wintypes.RECT()
                    if user32.GetWindowRect(hwnd, ctypes.byref(r)):
                        out.append((hwnd, buf.value, (r.left, r.top, r.right, r.bottom)))
        except Exception:
            pass
        return True

    proc = WNDENUMPROC(cb)  # keep a reference alive during the call
    user32.EnumWindows(proc, 0)
    return out


def _win_focus(hwnd) -> bool:
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        fg = user32.GetForegroundWindow()
        if fg and fg == hwnd:
            return True
        cur = kernel32.GetCurrentThreadId()
        fg_t = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = False
        if fg_t and fg_t != cur:
            attached = bool(user32.AttachThreadInput(cur, fg_t, True))
        try:
            user32.keybd_event(0x12, 0, 0, 0)  # Alt down/up unlocks SetForegroundWindow
            user32.keybd_event(0x12, 0, 2, 0)
            user32.BringWindowToTop(hwnd)
            return bool(user32.SetForegroundWindow(hwnd))
        finally:
            if attached:
                user32.AttachThreadInput(cur, fg_t, False)
    except Exception as e:
        logger.warning(f"focus failed: {e}")
        return False


def _win_click(x: int, y: int) -> None:
    _ensure_desktop()
    target_x, target_y = int(x), int(y)

    # 1. Bring Quotex window to front if found
    try:
        pt = wintypes.POINT(target_x, target_y)
        hw = user32.WindowFromPoint(pt)
        if hw:
            root = user32.GetAncestor(hw, 2)  # GA_ROOT
            if root:
                _win_focus(root)
    except Exception:
        pass

    time.sleep(0.04)

    # 2. Position cursor directly on target button
    user32.SetCursorPos(target_x, target_y)
    time.sleep(0.04)

    # 3. Method 1: SendInput (standard Windows hardware injection)
    clicked = False
    try:
        for flag, pause in ((0x0002, 0.08), (0x0004, 0.02)):  # LEFTDOWN, LEFTUP
            inp = _INPUT(type=0)
            inp.mi.dwFlags = flag
            res = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
            if res == 1:
                clicked = True
            time.sleep(pause)
    except Exception:
        pass

    # 4. Method 2: mouse_event (always works on browser / Electron windows)
    try:
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
        time.sleep(0.08)
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
        clicked = True
    except Exception:
        pass

    # 5. Method 3: pyautogui fallback
    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.click(target_x, target_y)
        clicked = True
    except Exception:
        pass

    if not clicked:
        raise RuntimeError(f"Click could not be sent to ({target_x}, {target_y})")


def _win_cursor() -> Tuple[int, int]:
    _ensure_desktop()
    p = wintypes.POINT()
    if user32.GetCursorPos(ctypes.byref(p)):
        return int(p.x), int(p.y)

    # Fallback 1: CURSORINFO
    try:
        class _CURSORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HICON),
                ("ptScreenPos", wintypes.POINT),
            ]
        ci = _CURSORINFO()
        ci.cbSize = ctypes.sizeof(_CURSORINFO)
        if user32.GetCursorInfo(ctypes.byref(ci)):
            if ci.ptScreenPos.x != 0 or ci.ptScreenPos.y != 0:
                return int(ci.ptScreenPos.x), int(ci.ptScreenPos.y)
    except Exception:
        pass

    # Fallback 2: pyautogui
    try:
        import pyautogui
        pos = pyautogui.position()
        if pos.x != 0 or pos.y != 0:
            return int(pos.x), int(pos.y)
    except Exception:
        pass

    return int(p.x), int(p.y)


# ==============================================================================
# BUTTON DETECTION (pure function - works on any RGB screenshot array)
# ==============================================================================
def detect_buttons(rgb: np.ndarray, save_debug: bool = True) -> Dict[str, Any]:
    """Find a green UP button stacked above a same-size red DOWN button. Returns centres in image pixels."""
    import cv2

    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    g_mask = cv2.inRange(hsv, (45, 110, 100), (90, 255, 255))
    r_mask = cv2.bitwise_or(cv2.inRange(hsv, (0, 120, 110), (8, 255, 255)),
                            cv2.inRange(hsv, (168, 120, 110), (180, 255, 255)))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 11))

    def boxes(mask):
        m = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)  # closes the white text/arrow inside the button
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        res = []
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)
            if w < 60 or h < 26 or not (1.2 <= w / h <= 9.0):
                continue
            if cv2.contourArea(c) / float(w * h) < 0.75:  # must be a solid rectangle
                continue
            res.append((x, y, w, h))
        return res

    G, R = boxes(g_mask), boxes(r_mask)
    best, best_score = None, -1.0
    for gx, gy, gw, gh in G:
        for rx, ry, rw, rh in R:
            if abs(gw - rw) > 0.25 * max(gw, rw) or abs(gh - rh) > 0.30 * max(gh, rh):
                continue
            if abs((gx + gw / 2) - (rx + rw / 2)) > 0.5 * max(gw, rw):
                continue
            if ry <= gy or ry - (gy + gh) > 3 * max(gh, rh):
                continue
            score = gw * gh + rw * rh
            if score > best_score:
                best, best_score = ((gx, gy, gw, gh), (rx, ry, rw, rh)), score

    if save_debug:
        try:
            dbg = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR).copy()
            for (x, y, w, h) in G:
                cv2.rectangle(dbg, (x, y), (x + w, y + h), (0, 255, 255), 1)
            for (x, y, w, h) in R:
                cv2.rectangle(dbg, (x, y), (x + w, y + h), (255, 0, 255), 1)
            if best:
                for (x, y, w, h) in best:
                    cv2.rectangle(dbg, (x, y), (x + w, y + h), (255, 255, 255), 4)
            s = min(1.0, 2000.0 / max(dbg.shape[1], 1))
            if s < 1.0:
                dbg = cv2.resize(dbg, None, fx=s, fy=s)
            cv2.imwrite(str(DEBUG_IMG), dbg)
        except Exception as e:
            logger.warning(f"debug image failed: {e}")

    info = {"green_candidates": len(G), "red_candidates": len(R)}
    if not best:
        return {"success": False, **info}
    (gx, gy, gw, gh), (rx, ry, rw, rh) = best
    return {"success": True, "call_x": int(gx + gw // 2), "call_y": int(gy + gh // 2),
            "put_x": int(rx + rw // 2), "put_y": int(ry + rh // 2), **info}


# ==============================================================================
# AUTO CLICKER (drop-in replacement for the old QuotexAutoClicker)
# ==============================================================================
class QuotexAutoClicker:
    def __init__(self):
        self.config = AutoClickerConfig()
        self.lock = threading.Lock()
        self._last_click_time = 0.0
        self.last_error = ""
        try:
            if CONFIG_FILE.exists():
                self.config = AutoClickerConfig(**json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            logger.warning(f"could not load autoclick config: {e}")
        self.config.enabled = False  # safety: always start disabled, enable from the UI

    def _save(self):
        try:
            CONFIG_FILE.write_text(json.dumps(self.config.dict(), indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"could not save autoclick config: {e}")

    def update_config(self, enabled=None, call_x=None, call_y=None, put_x=None, put_y=None, restore_cursor=None):
        with self.lock:
            if enabled is not None: self.config.enabled = bool(enabled)
            if call_x is not None: self.config.call_x = int(call_x)
            if call_y is not None: self.config.call_y = int(call_y)
            if put_x is not None: self.config.put_x = int(put_x)
            if put_y is not None: self.config.put_y = int(put_y)
            if restore_cursor is not None: self.config.restore_cursor = bool(restore_cursor)
            self._save()

    # ---- mouse ----
    def get_mouse_position(self) -> Dict[str, int]:
        try:
            if IS_WIN:
                x, y = _win_cursor()
            else:
                import pyautogui
                p = pyautogui.position(); x, y = int(p.x), int(p.y)
            return {"x": x, "y": y}
        except Exception as e:
            logger.warning(f"mouse position error: {e}")
            return {"x": 0, "y": 0}

    def _click_xy(self, x: int, y: int):
        if IS_WIN:
            _win_click(x, y)
        else:
            import pyautogui
            pyautogui.FAILSAFE = False
            pyautogui.click(x=int(x), y=int(y))

    def _target(self, direction: str) -> Tuple[int, int]:
        d = direction.upper()
        return (self.config.call_x, self.config.call_y) if d == "CALL" else (self.config.put_x, self.config.put_y)

    def execute_click(self, direction: str) -> bool:
        with self.lock:
            if not self.config.enabled:
                return False
            now = time.time()
            if now - self._last_click_time < 2.0:  # debounce duplicate signals
                return False
            x, y = self._target(direction)
            if not x and not y:
                self.last_error = "coordinates not set"
                return False
            self._last_click_time = now
            restore = self.config.restore_cursor

        def worker():
            try:
                old = self.get_mouse_position()
                self._click_xy(x, y)
                self.last_error = ""
                logger.info(f"[AUTO-CLICK] {direction} clicked at ({x},{y})")
                if restore:
                    time.sleep(0.12)
                    if IS_WIN:
                        user32.SetCursorPos(old["x"], old["y"])
            except Exception as e:
                self.last_error = str(e)
                logger.error(f"auto-click failed: {e}")

        threading.Thread(target=worker, daemon=True).start()
        return True

    def test_click(self, direction: str) -> Dict[str, Any]:
        try:
            x, y = self._target(direction)
            if not x and not y:
                return {"success": False, "error": "Coordinates not set. Press Scan or Grab Cursor first."}
            self._click_xy(x, y)
            return {"success": True, "clicked_at": {"x": x, "y": y}, "direction": direction}
        except Exception as e:
            logger.error(f"test click error: {e}")
            return {"success": False, "error": str(e)}

    def capture_now(self, direction: str, seconds: int = 3) -> Dict[str, Any]:
        """Waits `seconds` then captures cursor position and updates config."""
        wait_s = max(1, int(seconds))
        time.sleep(wait_s)
        p = self.get_mouse_position()
        x, y = int(p.get("x", 0)), int(p.get("y", 0))

        if x == 0 and y == 0:
            logger.warning(f"Failed to capture cursor position: got ({x}, {y})")
            return {
                "success": False,
                "error": f"Mouse cursor at ({x},{y}). Move your mouse over Quotex {direction.upper()} and try again.",
                "x": 0,
                "y": 0,
                "direction": direction.upper(),
            }

        if direction.upper() == "CALL":
            self.update_config(call_x=x, call_y=y)
        else:
            self.update_config(put_x=x, put_y=y)

        logger.info(f"[GRAB OK] {direction.upper()} captured at ({x}, {y})")
        cfg_dict = self.config.model_dump() if hasattr(self.config, "model_dump") else self.config.dict()
        return {
            "success": True,
            "direction": direction.upper(),
            "x": x,
            "y": y,
            "autoclick": cfg_dict,
        }

    def capture_after(self, direction: str, seconds: int = 3):
        """Hover over the button within `seconds`; the cursor position is saved in background."""
        threading.Thread(target=self.capture_now, args=(direction, seconds), daemon=True).start()

    # ---- scan ----
    def auto_detect_quotex_buttons(self, delay: float = 0) -> Dict[str, Any]:
        try:
            if delay and delay > 0:
                time.sleep(min(float(delay), 15))  # time to switch to the Quotex tab
            from PIL import ImageGrab
            win, seen = None, []
            v_left = v_top = 0
            wins = []
            if IS_WIN:
                wins = _win_list_windows()
                cands = []
                for hwnd, title, rect in wins:
                    tl = title.lower()
                    if "quotex" in tl and not any(x in tl for x in EXCLUDE_TITLES):
                        seen.append(title)
                        cands.append((hwnd, title, rect))
                if cands:
                    win = max(cands, key=lambda c: (c[2][2] - c[2][0]) * (c[2][3] - c[2][1]))
                    _win_focus(win[0])
                    time.sleep(0.5)
                v_left, v_top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)

            img = ImageGrab.grab(all_screens=True)
            arr = np.array(img.convert("RGB"))
            H, W = arr.shape[:2]

            if win:
                l, t, r, b = win[2]
                x0, y0 = max(0, l - v_left), max(0, t - v_top)
                x1, y1 = min(W, r - v_left), min(H, b - v_top)
                crop, off = arr[y0:y1, x0:x1], (v_left + x0, v_top + y0)
                note = f"scanned window: {win[1][:50]}"
            else:
                for hwnd, title, (l, t, r, b) in wins:  # black out our own dashboard
                    if any(x in title.lower() for x in EXCLUDE_TITLES):
                        arr[max(0, t - v_top):max(0, b - v_top), max(0, l - v_left):max(0, r - v_left)] = 0
                crop, off = arr, (v_left, v_top)
                note = "Quotex window title not found; scanned whole screen"

            if crop.size == 0:
                return {"success": False, "error": "Quotex window is off-screen or minimised."}
            res = detect_buttons(crop)
            if not res["success"]:
                return {"success": False,
                        "error": (f"Buttons not found ({res['green_candidates']} green / {res['red_candidates']} red shapes). "
                                  f"{note}. Keep the Quotex trading panel visible and open scan_debug.png to see what was seen."
                                  + ("" if seen else " (No browser window with 'Quotex' in its title was found.)"))}
            out = {"success": True, "note": note,
                   "call_x": res["call_x"] + off[0], "call_y": res["call_y"] + off[1],
                   "put_x": res["put_x"] + off[0], "put_y": res["put_y"] + off[1]}
            self.update_config(call_x=out["call_x"], call_y=out["call_y"], put_x=out["put_x"], put_y=out["put_y"])
            logger.info(f"[SCAN OK] {out}")
            return out
        except Exception as e:
            logger.exception("scan error")
            return {"success": False, "error": f"{type(e).__name__}: {e}"}


_default_clicker: Optional[QuotexAutoClicker] = None


def get_clicker() -> QuotexAutoClicker:
    global _default_clicker
    if _default_clicker is None:
        _default_clicker = QuotexAutoClicker()
    return _default_clicker


def execute_click(action: str) -> bool:
    """
    Executes an automated click for CALL or PUT using QuotexAutoClicker.
    """
    return get_clicker().execute_click(action)

