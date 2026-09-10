"""Window ownership and notification-area controls for our normal Edge app."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as W
import os
from pathlib import Path
import subprocess
import time

import psutil


def profile_processes(profile):
    expected = os.path.normcase(str(Path(profile).resolve()))
    found = []
    for process in psutil.process_iter(['name']):
        try:
            if process.info['name'].lower() != 'msedge.exe':
                continue
            args = process.cmdline()
            values = [a.split('=', 1)[1] for a in args if a.startswith('--user-data-dir=')]
            if any(os.path.normcase(str(Path(v).resolve())) == expected for v in values):
                if not any(a.startswith('--type=') for a in args):
                    found.append(process)
        except (psutil.Error, OSError):
            continue
    return found


class BrowserWindow:
    def __init__(self, profile):
        self.profile = profile
        self.process = None
        self.created = None
        self.desired = 'minimized'
        self.known_handles = set()
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.user.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
        self.user.IsWindowVisible.argtypes = [W.HWND]
        self.user.IsIconic.argtypes = [W.HWND]
        self.user.ShowWindowAsync.argtypes = [W.HWND, ctypes.c_int]
        self.user.SetForegroundWindow.argtypes = [W.HWND]
        self.user.GetForegroundWindow.restype = W.HWND
        self.user.GetWindowLongW.argtypes = [W.HWND, ctypes.c_int]
        self.user.GetWindowLongW.restype = W.LONG

    def alive(self):
        try:
            return self.process is not None and self.process.is_running() and self.process.create_time() == self.created
        except psutil.Error:
            return False

    def bind(self, process):
        self.process, self.created = process, process.create_time()

    def recover(self):
        matches = profile_processes(self.profile)
        if len(matches) == 1:
            self.bind(matches[0])
            return True
        if matches:
            raise RuntimeError('monochrome_profile_busy')
        return False

    def handles(self):
        if not self.alive():
            return []
        handles = []
        callback_type = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        def visit(hwnd, unused):
            pid = W.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            # Edge owns its top-level app window in the browser (root) process.
            # Include only real Chrome widget windows, not message-only helpers.
            name = ctypes.create_unicode_buffer(256)
            self.user.GetClassNameW(hwnd, name, len(name))
            if pid.value == self.process.pid and name.value.startswith('Chrome_WidgetWin_'):
                style = self.user.GetWindowLongW(hwnd,-16)
                extended = self.user.GetWindowLongW(hwnd,-20)
                # Never reveal Edge's hidden tool/utility windows. A background
                # app is kept iconic so a replacement host can discover it.
                if style & 0x00C00000 and not extended & 0x80 and (
                    hwnd in self.known_handles or self.user.IsWindowVisible(hwnd) or self.user.IsIconic(hwnd)):
                    handles.append(hwnd)
                    self.known_handles.add(hwnd)
            return True
        callback = callback_type(visit)
        self.user.EnumWindows.argtypes = [callback_type, W.LPARAM]
        self.user.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
        self.user.EnumWindows(callback, 0)
        return handles

    def apply(self, mode, *, user_requested=False):
        if mode not in ('tray', 'minimized', 'visible'):
            raise ValueError('invalid_window_mode')
        self.desired = mode
        handles = self.handles()
        for hwnd in handles:
            if mode == 'tray':
                if self.user.IsWindowVisible(hwnd):
                    if not self.user.IsIconic(hwnd):
                        self.user.ShowWindowAsync(hwnd, 7)
                    self.user.ShowWindowAsync(hwnd, 0)  # SW_HIDE
            elif mode == 'minimized':
                if not self.user.IsWindowVisible(hwnd) or not self.user.IsIconic(hwnd):
                    self.user.ShowWindowAsync(hwnd, 7)  # SW_SHOWMINNOACTIVE
            elif user_requested:
                self.user.ShowWindowAsync(hwnd, 9)
                self.user.SetForegroundWindow(hwnd)
        return bool(handles)

    def snapshot(self):
        handles = self.handles()
        return {'owned_windows':len(handles), 'visible_windows':sum(bool(self.user.IsWindowVisible(h)) for h in handles),
                'minimized_windows':sum(bool(self.user.IsIconic(h)) for h in handles),
                'foreground_owned':self.user.GetForegroundWindow() in handles, 'alive':self.alive()}

    def close(self):
        if not self.alive():
            return
        # PID + creation time guards against PID reuse. Never kill a personal browser.
        for child in reversed(self.process.children(recursive=True)):
            try:
                child.terminate()
            except psutil.Error:
                pass
        try:
            self.process.terminate()
            self.process.wait(10)
        except psutil.Error:
            pass


class TrayController:
    def __init__(self, action):
        import pystray
        from PIL import Image, ImageDraw
        # pystray restores the icon after TaskbarCreated. Add balloon-click
        # handling, which its pinned Windows backend does not implement.
        class Icon(pystray.Icon):
            def _on_notify(self, wparam, lparam):
                if lparam == 0x405:  # NIN_BALLOONUSERCLICK
                    self()
                else:
                    super()._on_notify(wparam, lparam)
        graphic = Image.new('RGBA', (64, 64))
        draw = ImageDraw.Draw(graphic)
        draw.ellipse((4, 4, 60, 60), fill='#4397ca')
        draw.line((36, 16, 36, 43), fill='white', width=6)
        draw.ellipse((20, 36, 38, 50), fill='white')
        def call(name):
            return lambda icon, item: action(name)
        self.icon = Icon('YouTubeLikesSync', graphic, 'YouTube Likes Sync', pystray.Menu(
            pystray.MenuItem('Open downloader', call('open'), default=True),
            pystray.MenuItem('Hide/minimize', call('hide')),
            pystray.MenuItem('Retry pending', call('retry')),
            pystray.MenuItem('Pause', call('pause')),
            pystray.MenuItem('Resume', call('resume')),
            pystray.MenuItem('Quit and pause', call('quit'))))
        self.ready = False

    def run(self):
        def setup(icon):
            icon.visible = True
            self.ready = True
        self.icon.run(setup)

    def update(self, text):
        title = ('YouTube Likes Sync · ' + text)[:127]
        if title != self.icon.title:
            self.icon.title = title

    def notify(self):
        if self.ready:
            self.icon.notify('Downloads are waiting for verification. Click to open the downloader.', 'YouTube Likes Sync')
            return True
        return False

    def stop(self):
        self.icon.stop()
