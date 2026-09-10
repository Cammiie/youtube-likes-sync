"""Small local install/repair screen for the unpacked browser extension."""
from pathlib import Path
import os
import subprocess
import webbrowser

from .extension_host import PROJECT, register


def browser_path(name):
    relatives = {'Brave': 'BraveSoftware/Brave-Browser/Application/brave.exe',
                 'Edge': 'Microsoft/Edge/Application/msedge.exe',
                 'Chrome': 'Google/Chrome/Application/chrome.exe'}
    for variable in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        path = Path(os.environ.get(variable, 'C:/'))/relatives[name]
        if path.is_file():
            return path
    return None


def run_setup(root, *, allow_account_change=False):
    import tkinter as tk
    from tkinter import ttk
    register(root)
    window = tk.Tk()
    window.title('Connect YouTube Music')
    window.geometry('630x485')
    window.minsize(610, 465)
    frame = ttk.Frame(window, padding=26)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='Connect from your browser.', font=('Segoe UI', 23, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='No Google Cloud setup or copied login details.', font=('Segoe UI', 11)).pack(anchor='w', pady=(8, 22))
    ttk.Label(frame, text='1. Add the extension once', font=('Segoe UI', 12, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='Open your browser’s Extensions page. Turn on Developer mode,\nchoose Load unpacked, and select the folder below.', font=('Segoe UI', 10)).pack(anchor='w', pady=(6, 10))
    folder = str(PROJECT/'extension')
    value = tk.StringVar(value=folder)
    entry = ttk.Entry(frame, textvariable=value, state='readonly')
    entry.pack(fill='x')
    row = ttk.Frame(frame); row.pack(fill='x', pady=(10, 20))
    available = [name for name in ('Brave', 'Edge', 'Chrome') if browser_path(name)] or ['Edge']
    selected = tk.StringVar(value=available[0])
    ttk.Combobox(row, textvariable=selected, values=available, state='readonly', width=9).pack(side='left')
    def open_extensions():
        name = selected.get(); exe = browser_path(name)
        if exe:
            # Explicit user click opens an interactive settings tab.
            subprocess.Popen([str(exe), f'{name.lower()}://extensions/'])
    ttk.Button(row, text='Open Extensions', command=open_extensions).pack(side='left', padx=8)
    def copy_folder():
        window.clipboard_clear(); window.clipboard_append(folder); window.update()
    ttk.Button(row, text='Copy folder path', command=copy_folder).pack(side='left')
    ttk.Label(frame, text='2. Connect your Music account', font=('Segoe UI', 12, 'bold')).pack(anchor='w')
    text = 'Sign in to YouTube Music, open its Library, then click the extension\nand choose Connect YouTube Music. Pin it for easy access.'
    if allow_account_change:
        text += '\nSelect “Use this as a different sync account” to switch accounts.'
    ttk.Label(frame, text=text, font=('Segoe UI', 10)).pack(anchor='w', pady=(6, 12))
    def open_music():
        exe = browser_path(selected.get())
        if exe: subprocess.Popen([str(exe), 'https://music.youtube.com/library'])
        else: webbrowser.open('https://music.youtube.com/library')
    ttk.Button(frame, text='Open YouTube Music', command=open_music).pack(anchor='w')
    ttk.Label(frame, text='The local connector is ready. Existing downloads and history stay intact.',
              font=('Segoe UI', 10), wraplength=560).pack(anchor='w', pady=(20, 0))
    def reveal():
        window.lift(); window.attributes('-topmost', True)
        window.after(600, lambda: window.attributes('-topmost', False))
    window.after(100, reveal)
    window.mainloop()
    return {'status': 'extension_setup_ready'}
