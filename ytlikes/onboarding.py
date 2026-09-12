"""Small, cancellable Windows setup flow. Google owns the sign-in screen."""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import queue
import sqlite3
import threading
import time

from .common import SyncError, RunLock, atomic_write, config, dpapi
from .google_auth import GoogleSignIn, import_client, load_client


def suggested_output():
    database=Path.home()/'AppData/Local/CrammyPlayer/CrammyPlayer/crammyplayer.db'
    if database.is_file():
        try:
            with closing(sqlite3.connect('file:'+database.as_posix()+'?mode=ro',uri=True)) as connection:
                folders=connection.execute('SELECT path FROM directories WHERE subdirs=1').fetchall()
            if len(folders)==1 and Path(folders[0][0]).is_dir():
                return str(Path(folders[0][0])/'YouTube Likes')
        except sqlite3.Error:
            pass
    return str(Path.home()/'Music/YouTube Likes')


def installation_settings(root):
    settings=config(root)
    if not (root/'config.json').exists():
        settings.update(output=suggested_output(), download_engine='antra_tidal',browser_launch_allowed=False,
                        browser_window_mode='tray',browser_downloads_ready=True,auto_api_renewal=False,
                        api_migration_pending=False,allow_encrypted_lossless=False)
    return settings


MESSAGES={
    'google_client_required':'Google sign-in needs its one-time app setup. Import the Google client file below.',
    'google_client_invalid':'This Google client file could not be used. Import the correct client JSON and try again.',
    'google_desktop_client_required':'Choose a Google OAuth client of type Desktop app, then import its JSON file.',
    'google_access_denied':'Access was not granted. You can try Google sign-in again.',
    'google_permission_required':'Review and approve the YouTube permission shown by Google, then try again.',
    'google_offline_access_required':'Google did not grant background access. Try signing in again and approve access.',
    'google_signin_timeout':'Sign-in timed out. Click Sign in with Google to try again.',
    'google_browser_open_failed':'The browser could not open. Check your default browser and try again.',
    'google_network_error':'Could not reach Google. Check your connection and try again.',
    'google_rate_limited':'Google asked us to wait. Please try again later.',
    'google_signin_failed':'Google could not complete sign-in. Check the OAuth app setup, then try again.',
    'google_response_invalid':'Google returned an unexpected response. Please try again.',
    'different_youtube_account':'This sync is connected to a different YouTube account. Sign in to the original account; its library has been preserved.',
    'incomplete_likes_snapshot':'We could not read all your likes. Nothing was changed. Please try again.',
    'youtube_auth_required':'YouTube did not accept this connection. Check the Google app permissions and try again.',
    'youtube_response_changed':'Google sign-in returned, but YouTube Music could not read your library. Your existing connection is unchanged.',
    'youtube_music_oauth_rejected':'Google sign-in completed, but YouTube Music rejected this OAuth connection. Signing in again may not help. Your existing connection and downloads are unchanged.',
    'youtube_network_error':'Could not finish reading your likes. Check your connection and try again.',
    'already_running':'A sync is finishing. Please try connecting again in a moment.',
    'setup_cancelled':'Sign-in cancelled. Your existing connection is unchanged.',
    'output_folder_unavailable':'That folder could not be created. Choose a different folder and try again.',
}


def complete_setup(root, record, output, *, automatic=True,allow_account_change=False):
    from .cli import connect_account, scheduler
    from .state import State
    destination=Path(output).expanduser()
    if not destination.is_absolute():
        raise SyncError('output_folder_unavailable')
    try:
        destination.mkdir(parents=True,exist_ok=True)
    except OSError:
        raise SyncError('output_folder_unavailable') from None
    with RunLock(root):
        state=State(root)
        try:
            if record.get('kind')=='google_oauth':
                atomic_write(root/'google-pending.dpapi',dpapi(json.dumps({'created':time.time(),'record':record}).encode()))
            try:
                result=connect_account(state,root,record,allow_account_change=allow_account_change)
            except SyncError as error:
                if error.code == 'youtube_music_oauth_rejected':
                    (root/'google-pending.dpapi').unlink(missing_ok=True)
                raise
            (root/'google-pending.dpapi').unlink(missing_ok=True)
            settings=installation_settings(root)
            settings['output']=str(destination)
            atomic_write(root/'config.json',json.dumps(settings,indent=2).encode())
        finally:
            state.close()
    result['output']=str(destination)
    if automatic:
        try:
            scheduler('Install')
        except SyncError:
            result['scheduler_warning']=True
    return result


def run_setup(root, *, allow_account_change=False):
    import tkinter as tk
    from tkinter import ttk, filedialog
    import webbrowser
    window=tk.Tk()
    window.title('Connect YouTube Music')
    window.geometry('620x540')
    window.minsize(600,520)
    window.configure(background='#f7f8fa')
    style=ttk.Style(window)
    style.theme_use('vista' if 'vista' in style.theme_names() else 'clam')
    style.configure('Setup.TFrame',background='#f7f8fa')
    style.configure('Setup.TLabel',background='#f7f8fa',foreground='#242d38',font=('Segoe UI',10))
    style.configure('Title.TLabel',background='#f7f8fa',foreground='#142535',font=('Segoe UI',23,'bold'))
    style.configure('Setup.TButton',font=('Segoe UI',10),padding=(14,9))
    content=ttk.Frame(window,style='Setup.TFrame',padding=28)
    content.pack(fill='both',expand=True)
    content.columnconfigure(0,weight=1)
    ttk.Label(content,text='Your likes. Saved as FLAC.',style='Title.TLabel').grid(row=0,column=0,sticky='w')
    subtitle='Connect another account. Downloaded files and history stay intact.' if allow_account_change else 'Connect YouTube Music, then new likes will download here.'
    ttk.Label(content,text=subtitle,style='Setup.TLabel',wraplength=555).grid(row=1,column=0,sticky='w',pady=(8,22))
    ttk.Label(content,text='Download folder',style='Setup.TLabel').grid(row=2,column=0,sticky='w')
    folder_row=ttk.Frame(content,style='Setup.TFrame')
    folder_row.grid(row=3,column=0,sticky='ew',pady=(6,16))
    folder_row.columnconfigure(0,weight=1)
    output=tk.StringVar(value=installation_settings(root)['output'])
    entry=ttk.Entry(folder_row,textvariable=output,font=('Segoe UI',10))
    entry.grid(row=0,column=0,sticky='ew',ipady=6)
    def choose_folder():
        selected=filedialog.askdirectory(parent=window,title='Choose download folder',initialdir=output.get())
        if selected: output.set(selected)
    choose=ttk.Button(folder_row,text='Choose…',command=choose_folder)
    choose.grid(row=0,column=1,padx=(10,0))
    ttk.Label(content,text='Existing likes stay as they are. Only new likes download.\nLikes from your phone work too, using the same account.\n\nGoogle asks to “Manage your YouTube account” for Music access.\nThis app only reads your account and likes; it makes no changes.',
              style='Setup.TLabel',wraplength=555).grid(row=4,column=0,sticky='w')
    status=tk.StringVar(value='Sign in on Google’s page. Your password stays with Google.')
    status_label=ttk.Label(content,textvariable=status,wraplength=540,style='Setup.TLabel')
    status_label.grid(row=5,column=0,sticky='ew',pady=(22,10))
    progress=ttk.Progressbar(content,mode='indeterminate')
    progress.grid(row=6,column=0,sticky='ew',pady=(0,12))
    actions=ttk.Frame(content,style='Setup.TFrame')
    actions.grid(row=7,column=0,sticky='ew')
    events=queue.Queue()
    current={'flow':None,'busy':False,'closed':False,'commit':False,'candidate':None}
    guard=threading.Lock()
    result=[]

    def busy(value):
        current['busy']=value
        for control in (connect,entry,choose,import_button):
            control.configure(state='disabled' if value else 'normal')
        if value: progress.start(15)
        else: progress.stop()

    def start():
        try:
            client=load_client(root)
        except SyncError as error:
            status.set(MESSAGES.get(error.code,'Google sign-in is not configured yet.'))
            return
        destination=output.get().strip()
        busy(True)
        status.set('Opening Google sign-in…')
        flow=GoogleSignIn(client)
        current['flow']=flow
        def work():
            try:
                record=current['candidate'] or flow.run(lambda:events.put(('status','Approve access in your browser. This window will finish automatically.')))
                current['candidate']=record
                with guard:
                    if current['closed'] or flow.cancelled.is_set():
                        return
                    current['commit']=True
                events.put(('status','Reading your complete liked-songs library…'))
                connected=complete_setup(root,record,destination,allow_account_change=allow_account_change)
                events.put(('done',connected))
            except SyncError as error:
                events.put(('error',error.code))
            except Exception:
                events.put(('error','google_signin_failed'))
            finally:
                current['commit']=False
        threading.Thread(target=work,daemon=True).start()

    def import_settings():
        chosen=filedialog.askopenfilename(parent=window,title='Import Google Desktop app client',filetypes=[('Google client JSON','*.json')])
        if chosen:
            try:
                import_client(root,chosen)
                status.set('Google sign-in is ready. Click Sign in with Google to connect.')
            except SyncError as error:
                status.set(MESSAGES.get(error.code,'Could not import that file.'))

    def close():
        with guard:
            if current['commit']:
                status.set('Finishing the library check. Please wait a moment.')
                return
            current['closed']=True
            if current['flow']: current['flow'].cancel()
            current['candidate']=None
            (root/'google-pending.dpapi').unlink(missing_ok=True)
        window.destroy()

    connect=ttk.Button(actions,text='Sign in with Google',style='Setup.TButton',command=start)
    connect.pack(side='left')
    ttk.Button(actions,text='Close',command=close).pack(side='right',padx=(12,0))
    admin=ttk.Frame(content,style='Setup.TFrame')
    admin.grid(row=8,column=0,sticky='w',pady=(18,0))
    import_button=ttk.Button(admin,text='Import app configuration',command=import_settings)
    import_button.pack(side='left')
    ttk.Button(admin,text='Setup help',command=lambda:webbrowser.open('https://console.cloud.google.com/auth/clients')).pack(side='left',padx=(8,0))
    try:
        load_client(root)
    except SyncError:
        status.set(MESSAGES['google_client_required'])

    def poll():
        if current['closed']: return
        while not events.empty():
            kind,value=events.get_nowait()
            if kind=='status': status.set(value)
            elif kind=='error':
                busy(False)
                if value in ('youtube_response_changed','incomplete_likes_snapshot','youtube_network_error','already_running') and current['candidate']:
                    connect.configure(text='Retry library check')
                else:
                    current['candidate']=None
                    (root/'google-pending.dpapi').unlink(missing_ok=True)
                    connect.configure(text='Sign in with Google')
                status.set(MESSAGES.get(value,'Could not finish connecting. Your existing connection is unchanged. Please try again.'))
            else:
                busy(False)
                result.append(value)
                connect.configure(state='disabled',text='Connected')
                for control in (entry,choose,import_button):
                    control.configure(state='disabled')
                message=f"Connected. {value['current_likes']} existing likes checked. New likes will download every five minutes while you’re signed in to Windows."
                if value.get('scheduler_warning'):
                    message='Connected, but automatic checks could not be enabled. Run install.ps1 to finish setup.'
                status.set(message)
        window.after(100,poll)
    window.bind('<Escape>',lambda event:close())
    window.protocol('WM_DELETE_WINDOW',close)
    window.bind('<Configure>',lambda event:status_label.configure(wraplength=max(400,content.winfo_width()-56)) if event.widget==window else None)
    def reveal_setup():
        # Setup is explicitly opened by the user; do not leave it behind a browser.
        window.deiconify()
        window.lift()
        window.attributes('-topmost',True)
        window.after(600,lambda:window.attributes('-topmost',False))
        connect.focus_set()
    window.after(100,reveal_setup)
    window.after(100,poll)
    window.mainloop()
    if not result:
        raise SyncError('setup_cancelled')
    return result[-1]
