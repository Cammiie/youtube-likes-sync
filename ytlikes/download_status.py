"""Local status and controls; opening this window never starts audio or a browser."""
from .common import config, data_dir
from .state import State


def show():
    import tkinter as tk
    from tkinter import ttk
    root = data_dir()
    state = State(root)
    window = tk.Tk()
    window.title('YouTube Likes Sync — Downloads')
    window.geometry('640x420')
    window.minsize(480, 320)
    frame = ttk.Frame(window, padding=20)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='Your downloads', font=('Segoe UI', 20, 'bold')).pack(anchor='w')
    summary = ttk.Label(frame, wraplength=570)
    summary.pack(fill='x', pady=12)
    jobs = tk.Text(frame, wrap='word', font=('Segoe UI', 10), height=10)
    jobs.pack(fill='both', expand=True)
    actions = ttk.Frame(frame)
    actions.pack(fill='x', pady=(12, 0))

    def refresh():
        status = state.status()
        counts = status['counts']
        message = 'Paused' if status['paused'] else 'Checking every five minutes'
        if status['auth_required']:
            message = 'Reconnect from the YouTube Music extension'
        summary.configure(text=f"{message}. {counts.get('completed', 0)} completed; "
                          f"{counts.get('pending', 0)} pending.\nBrowserless FLAC downloads.\n{config(root)['output']}")
        jobs.configure(state='normal')
        jobs.delete('1.0', 'end')
        for job in status['jobs']:
            jobs.insert('end', f"{job['source'].get('title', 'Track')} — {job['state']}"
                        + (f" ({job['error'].replace('_', ' ')})" if job['error'] else '') + '\n')
        jobs.configure(state='disabled')

    def control(action):
        from .cli import main
        main([action, '--quiet'])
        refresh()

    for action in ('pause', 'resume', 'retry'):
        ttk.Button(actions, text='Retry pending' if action == 'retry' else action.title(),
                   command=lambda name=action: control(name)).pack(side='left', padx=(0, 8))
    ttk.Button(actions, text='Refresh', command=refresh).pack(side='right')
    refresh()
    try:
        window.mainloop()
    finally:
        state.close()
    return 0
