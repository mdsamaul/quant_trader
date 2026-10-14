"""
quotex_vision.py - Real-time Quotex Chart & Screen Vision Analyzer

Features:
- Locates the Quotex trading browser window via Win32 API.
- Captures the window buffer using PrintWindow / GDI memory DC.
- Analyzes the chart region for candlestick colors (bullish green vs bearish red).
- Measures candle body and wick ratios visually.
- Pushes visual candle telemetry to the QuantTrader trading engine.
"""

import ctypes
import logging
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("QuotexVision")
IS_WIN = sys.platform.startswith("win")

if IS_WIN:
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32


def find_quotex_window() -> Optional[Tuple[int, str, Tuple[int, int, int, int]]]:
    """Finds visible Quotex browser window handle and rect."""
    if not IS_WIN:
        return None

    found = []

    def enum_cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            buf = ctypes.create_unicode_buffer(512)
            if user32.GetWindowTextW(hwnd, buf, 512) > 0:
                title = buf.value
                tl = title.lower()
                if "quotex" in tl and "quanttrader" not in tl and "localhost" not in tl:
                    r = wintypes.RECT()
                    if user32.GetWindowRect(hwnd, ctypes.byref(r)):
                        found.append((hwnd, title, (r.left, r.top, r.right, r.bottom)))
        return True

    from quotex_clicker import WNDENUMPROC
    cb = WNDENUMPROC(enum_cb)
    user32.EnumWindows(cb, 0)

    if not found:
        return None
    # Pick largest window
    return max(found, key=lambda w: (w[2][2] - w[2][0]) * (w[2][3] - w[2][1]))


def capture_window_gdi(hwnd: int) -> Optional[np.ndarray]:
    """Captures window image buffer via Win32 GDI PrintWindow (works even in background/overlap)."""
    if not IS_WIN or not hwnd:
        return None

    try:
        r = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(r)):
            return None

        width = r.right - r.left
        height = r.bottom - r.top
        if width <= 100 or height <= 100:
            return None

        hdc_wnd = user32.GetDC(hwnd)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_wnd)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_wnd, width, height)
        gdi32.SelectObject(hdc_mem, hbmp)

        # PrintWindow with PW_RENDERFULLCONTENT (2)
        PW_RENDERFULLCONTENT = 2
        success = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
        if not success:
            success = user32.PrintWindow(hwnd, hdc_mem, 0)

        if not success:
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(hwnd, hdc_wnd)
            return None

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ('biSize', wintypes.DWORD),
                ('biWidth', wintypes.LONG),
                ('biHeight', wintypes.LONG),
                ('biPlanes', wintypes.WORD),
                ('biBitCount', wintypes.WORD),
                ('biCompression', wintypes.DWORD),
                ('biSizeImage', wintypes.DWORD),
                ('biXPelsPerMeter', wintypes.LONG),
                ('biYPelsPerMeter', wintypes.LONG),
                ('biClrUsed', wintypes.DWORD),
                ('biClrImportant', wintypes.DWORD)
            ]

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = width
        bmi.biHeight = -height  # top-down
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0

        buffer = ctypes.create_string_buffer(width * height * 4)
        gdi32.GetDIBits(hdc_mem, hbmp, 0, height, buffer, ctypes.byref(bmi), 0)

        # Clean up GDI handles
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_wnd)

        arr = np.frombuffer(buffer, dtype=np.uint8).reshape((height, width, 4))
        # Convert BGRA to RGB
        rgb = arr[:, :, [2, 1, 0]]
        return rgb
    except Exception as e:
        logger.warning(f"capture_window_gdi error: {e}")
        return None


def analyze_chart_colors(rgb: np.ndarray) -> Dict[str, Any]:
    """
    Analyzes Quotex chart area for bullish/bearish candle distribution and momentum.
    Green = (RGB ~ [0-50, 160-255, 100-220])
    Red   = (RGB ~ [200-255, 30-90, 70-130])
    """
    import cv2

    H, W = rgb.shape[:2]
    # Chart area is usually the left 75% and middle 75% height of Quotex
    chart_crop = rgb[int(H * 0.15):int(H * 0.85), int(W * 0.05):int(W * 0.78)]

    hsv = cv2.cvtColor(chart_crop, cv2.COLOR_RGB2HSV)
    # Green candle mask
    g_mask = cv2.inRange(hsv, (40, 100, 80), (85, 255, 255))
    # Red candle mask
    r_mask = cv2.bitwise_or(cv2.inRange(hsv, (0, 100, 80), (10, 255, 255)),
                            cv2.inRange(hsv, (165, 100, 80), (180, 255, 255)))

    green_pixels = int(np.sum(g_mask > 0))
    red_pixels = int(np.sum(r_mask > 0))
    total_candle_px = green_pixels + red_pixels

    if total_candle_px == 0:
        return {"action": "WAIT", "confidence": 0.0, "reason": "No clear candles detected on chart"}

    # Rightmost 20% of the chart = recent candles / momentum
    crop_w = chart_crop.shape[1]
    recent_g = int(np.sum(g_mask[:, int(crop_w * 0.8):] > 0))
    recent_r = int(np.sum(r_mask[:, int(crop_w * 0.8):] > 0))
    recent_tot = recent_g + recent_r

    if recent_tot > 50:
        bull_ratio = recent_g / recent_tot
        bear_ratio = recent_r / recent_tot
    else:
        bull_ratio = green_pixels / total_candle_px
        bear_ratio = red_pixels / total_candle_px

    if bull_ratio >= 0.65:
        return {
            "action": "CALL",
            "confidence": round(float(bull_ratio), 2),
            "green_px": green_pixels,
            "red_px": red_pixels,
            "reasons": [f"Visual Bullish Momentum (Green candle dominance {bull_ratio*100:.1f}%)"]
        }
    elif bear_ratio >= 0.65:
        return {
            "action": "PUT",
            "confidence": round(float(bear_ratio), 2),
            "green_px": green_pixels,
            "red_px": red_pixels,
            "reasons": [f"Visual Bearish Momentum (Red candle dominance {bear_ratio*100:.1f}%)"]
        }
    else:
        return {
            "action": "WAIT",
            "confidence": round(float(max(bull_ratio, bear_ratio)), 2),
            "green_px": green_pixels,
            "red_px": red_pixels,
            "reasons": ["Ranging / indecision on visual chart"]
        }


class QuotexVisionTracker:
    """Background screen vision tracker that analyzes Quotex window periodically."""

    def __init__(self, callback=None):
        self.callback = callback
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.last_status: Dict[str, Any] = {"status": "idle", "window": None}
        self.interval = 1.0

    def start(self, interval: float = 1.0):
        if self.running:
            return
        self.interval = interval
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        logger.info("QuotexVisionTracker started.")

    def stop(self):
        self.running = False
        self.last_status["status"] = "stopped"
        logger.info("QuotexVisionTracker stopped.")

    def _run(self):
        while self.running:
            try:
                win_info = find_quotex_window()
                if not win_info:
                    self.last_status = {
                        "status": "searching",
                        "error": "Quotex browser window not detected. Keep Quotex open.",
                        "timestamp": time.time()
                    }
                    time.sleep(2.0)
                    continue

                hwnd, title, rect = win_info
                img = capture_window_gdi(hwnd)
                if img is None:
                    self.last_status = {
                        "status": "capture_failed",
                        "window": title,
                        "error": "Could not capture window graphics buffer.",
                        "timestamp": time.time()
                    }
                    time.sleep(1.5)
                    continue

                analysis = analyze_chart_colors(img)
                self.last_status = {
                    "status": "tracking",
                    "window": title,
                    "analysis": analysis,
                    "resolution": f"{img.shape[1]}x{img.shape[0]}",
                    "timestamp": time.time()
                }

                if self.callback:
                    self.callback(analysis)

            except Exception as e:
                logger.error(f"Vision tracker error: {e}")
                self.last_status = {"status": "error", "error": str(e), "timestamp": time.time()}

            time.sleep(self.interval)


_vision_tracker: Optional[QuotexVisionTracker] = None


def get_vision_tracker() -> QuotexVisionTracker:
    global _vision_tracker
    if _vision_tracker is None:
        _vision_tracker = QuotexVisionTracker()
    return _vision_tracker
