"""EduBotics Servo-Station — the vendor's bench window (spec §5A, S16).

A thin tkinter shell over ``station_controller`` (the state machine) and
``station_core`` (the bus runs). ONE worker thread owns the serial port and
runs every job; the window only ever talks to it through two queues, polled
with ``after(50)``. Start on Windows with ``Servo-Station.cmd`` (python.org
Python 3.12 with Tcl/Tk, ``pyserial==3.5``); ``--data-dir`` moves the data.

The admin password typed into the sign-in dialog goes to the one sign-in
request and is dropped; the tokens live in the worker's ``CloudSession`` only.
This is the one station module that imports tkinter or pyserial.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import pathlib
import queue
import socket
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk

_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
for _path in (_REPO / 'robotis_ai_setup' / 'docker' / 'open_manipulator', _HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import feetech_bus as fb  # noqa: E402
import feetech_servo_spec as fss  # noqa: E402
import station_cloud as cloud  # noqa: E402
import station_controller as sc  # noqa: E402
import station_core as core  # noqa: E402
import station_records as records  # noqa: E402
import station_texts_de as T  # noqa: E402

log = logging.getLogger('servo_station')

ADAPTER_VID = 0x1A86      # Waveshare CH343P
ADAPTER_PID = 0x55D3
_STEP_ORDER = ('A', 'B', 'C', 'C2', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M')


def list_ports():
    from serial.tools import list_ports as lp
    return [(p.device, p.vid, p.pid) for p in lp.comports()]


def preselected_port(ports):
    adapters = [device for device, vid, pid in ports if vid == ADAPTER_VID and pid == ADAPTER_PID]
    return adapters[0] if len(adapters) == 1 else None


def tool_commit():
    try:
        out = subprocess.run(['git', '-C', str(_REPO), 'rev-parse', 'HEAD'], capture_output=True,
                             text=True, timeout=5)
        commit = out.stdout.strip()
        return commit if out.returncode == 0 and commit else 'unknown'
    except (OSError, subprocess.SubprocessError):
        return 'unknown'


class _Facts:
    def __init__(self, store):
        self.store = store

    def gauge_state(self, cap_label, cap_type):
        return self.store.gauge_state(cap_label, cap_type, datetime.date.today().isoformat())


class Worker:
    """Owns the serial port and the cloud session; runs one job at a time."""

    def __init__(self, store, spec, events):
        self.store = store
        self.spec = spec
        self.events = events
        self.jobs = queue.Queue()
        self.port = None
        self.session = None
        self.host = socket.gethostname()
        self.commit = tool_commit()
        self._last_note = None
        self.thread = threading.Thread(target=self._loop, name='servo-station-worker', daemon=True)
        self.thread.start()

    def _notify(self, kind, text='', **data):
        if kind == 'note':
            self._last_note = text
        self.events.put(('notify', kind, text, data))

    def _env(self):
        if self.port is None:
            raise core.StationError('NEED_PORT', T.NEED_PORT)
        port = self.port
        return core.Env(lambda baud: fb.FeetechBus(port, baudrate=baud, timeout_s=0.2),
                        notify=self._notify, port=port)

    def _loop(self):
        while True:
            action = self.jobs.get()
            if action is None:
                return
            self._last_note = None
            try:
                ok, message, result = True, *self._run(action)
            except core.StationError as e:
                ok, message, result = False, e.message_de, None
            except cloud.CloudError as e:
                ok, message, result = False, e.message_de, None
            except Exception:  # noqa: BLE001 — the window must outlive any bug
                log.exception('job %s failed', action.kind)
                ok, message, result = False, T.UNEXPECTED, None
            self.events.put(('finished', action, ok, message, result))

    def _upload(self):
        if self.session is None or not self.session.signed_in:
            return
        result = cloud.upload_pending(self.session, self.store)
        if result.uploaded:
            self._notify('note', T.CLOUD_UPLOADED.format(n=result.uploaded))
        for serial, code in result.rejected:
            self._notify('note', T.CLOUD_REJECTED_KEPT.format(serial=serial, code=code))
        if result.stopped is not None:
            self._notify('note', result.stopped.message_de)
        self.events.put(('cloud', None))

    def _run(self, action):
        """``(message, result)`` of one job; refusals raise."""
        kind, p = action.kind, action.params
        if kind == 'connect':
            try:
                fb.FeetechBus(p['port'], timeout_s=0.2).close()
            except OSError as e:
                raise core.refusal('PORT_OPEN_FAILED', port=p['port'], reason=str(e)[:120]) from None
            self.port = p['port']
            return T.CONNECTED.format(port=self.port), None
        if kind == 'sign_in':
            password = p.pop('password', '')      # out of the shared Action at once: memory only, once
            session = cloud.CloudSession(p['url'], p['anon_key'])
            try:
                session.sign_in(p['username'], password)
            finally:
                del password
            self.session = session
            config = self.store.load_config()
            config.update(supabase_url=p['url'].strip().rstrip('/'), supabase_anon_key=p['anon_key'].strip())
            self.store.save_config(config)
            self._upload()
            return T.CLOUD_SIGNED_IN.format(user=session.username), None
        if kind == 'upload':
            if self.session is None or not self.session.signed_in:
                return T.CLOUD_SIGNED_OUT, None
            self._upload()
            return self._last_note or T.CLOUD_UPLOADED.format(n=0), None
        if kind == 'p0_report':
            report, _snippet = core.p0_bericht(self.store)
            return T.P0_REPORT_SAVED.format(path=report), None
        env = self._env()
        if kind == 'reference':
            path, _dump = core.referenz_lesen(env, self.store)
            return T.REF_SAVED.format(path=path), str(path)
        if kind == 'direction':
            core.drehrichtung_messen(env, self.store, p['path'])
        elif kind == 'motion':
            core.fahrzeit_messen(env, self.store, p['path'])
        elif kind == 'incoming':
            core.wareneingang(env, self.store, serial=p['serial'], spec=self.spec)
        elif kind == 'check':
            core.pruefen(env, self.store, serial=p['serial'], spec=self.spec)
        elif kind == 'gauge':
            core.lehre_pruefen(env, self.store, serial=p['serial'], cap_label=p['cap_label'],
                               local_date=datetime.date.today().isoformat(), spec=self.spec)
        elif kind == 'calibrate':
            _record, path = core.kalibrieren(
                env, self.store, serial=p['serial'], joint_index=p['joint_index'], cap_label=p['cap_label'],
                operator=p['operator'], gauge_check_at=p['gauge_check_at'], warnings=p['warnings'],
                spec=self.spec, host=self.host, tool_commit=self.commit)
            try:
                self._upload()
            except cloud.CloudError as e:      # the local record is the commit point
                self._notify('note', e.message_de)
            return T.RECORD_SAVED.format(path=path), str(path)
        return self._last_note or T.READY, None


class StationApp:
    def __init__(self, root, store, spec):
        self.root = root
        self.store = store
        self.spec = spec
        self.controller = sc.Controller(spec, _Facts(store))
        self.events = queue.Queue()
        self.worker = Worker(store, spec, self.events)
        self.step_state = {}
        root.title(T.TITLE)
        root.protocol('WM_DELETE_WINDOW', self.close)
        self._build()
        self._refresh_ports()
        self._refresh_cloud()
        self._render()
        root.after(50, self._poll)

    # ── layout ───────────────────────────────────────────────────────────────
    def _build(self):
        pad = {'padx': 6, 'pady': 3}
        top = ttk.Frame(self.root)
        top.pack(fill='x', **pad)
        ttk.Label(top, text=T.LBL_PORT).pack(side='left')
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(top, textvariable=self.port_var, width=14, state='readonly')
        self.port_box.pack(side='left', **pad)
        ttk.Button(top, text=T.BTN_REFRESH, command=self._refresh_ports).pack(side='left')
        ttk.Button(top, text=T.BTN_CONNECT, command=self._connect).pack(side='left', **pad)
        self.cloud_label = ttk.Label(top, text=T.CLOUD_SIGNED_OUT)
        self.cloud_label.pack(side='right')
        ttk.Button(top, text=T.BTN_UPLOAD, command=lambda: self._dispatch(sc.Action('upload', {}))
                   ).pack(side='right', **pad)
        ttk.Button(top, text=T.BTN_SIGN_IN, command=self._sign_in_dialog).pack(side='right')

        modes = ttk.LabelFrame(self.root, text=T.LBL_MODE)
        modes.pack(fill='x', **pad)
        self.mode_var = tk.StringVar(value=self.controller.mode)
        self.mode_buttons = {}
        for mode in sc.MODES:
            button = ttk.Radiobutton(modes, text=sc.MODE_LABELS[mode], value=mode, variable=self.mode_var,
                                     command=lambda m=mode: self._set_mode(m))
            button.pack(side='left', **pad)
            self.mode_buttons[mode] = button

        settings = ttk.Frame(self.root)
        settings.pack(fill='x', **pad)
        ttk.Label(settings, text=T.LBL_BATCH).grid(row=0, column=0, sticky='w')
        self.batch_box = ttk.Combobox(settings, values=self.controller.batch_entries(), width=36, state='readonly')
        self.batch_box.grid(row=0, column=1, sticky='w', **pad)
        self.batch_box.bind('<<ComboboxSelected>>', lambda _e: self.controller.set_batch(self.batch_box.current()))
        ttk.Label(settings, text=T.LBL_OPERATOR).grid(row=1, column=0, sticky='w')
        self.operator_var = tk.StringVar()
        self.operator_var.trace_add('write', lambda *_a: self.controller.set_operator(self.operator_var.get()))
        ttk.Entry(settings, textvariable=self.operator_var, width=24).grid(row=1, column=1, sticky='w', **pad)
        ttk.Label(settings, text=T.LBL_CAP).grid(row=2, column=0, sticky='w')
        self.cap_label = ttk.Label(settings, text='–')
        self.cap_label.grid(row=2, column=1, sticky='w', **pad)

        self.reference_bar = ttk.Frame(self.root)
        for name, label in (('reference', T.BTN_REFERENCE), ('direction', T.BTN_DIRECTION),
                            ('motion', T.BTN_MOTION), ('p0_report', T.BTN_P0_REPORT)):
            ttk.Button(self.reference_bar, text=label,
                       command=lambda n=name: self._dispatch(self.controller.press(n))).pack(side='left', **pad)

        scan = ttk.Frame(self.root)
        scan.pack(fill='x', **pad)
        ttk.Label(scan, text=T.LBL_SCAN).pack(side='left')
        self.scan_entry = ttk.Entry(scan, width=28, font=('Segoe UI', 14))
        self.scan_entry.pack(side='left', **pad)
        self.scan_entry.bind('<Return>', self._on_return)
        self.swatch = tk.Canvas(scan, width=48, height=32, highlightthickness=1, highlightbackground='#444444')
        self.swatch.pack(side='left', **pad)

        self.status = tk.Label(self.root, text='', font=('Segoe UI', 16, 'bold'), wraplength=760,
                               justify='left', anchor='w')
        self.status.pack(fill='x', **pad)
        self.live = ttk.Label(self.root, text='')
        self.live.pack(fill='x', **pad)

        body = ttk.Frame(self.root)
        body.pack(fill='both', expand=True, **pad)
        steps = ttk.LabelFrame(body, text=T.LBL_STEPS)
        steps.pack(side='left', fill='y')
        self.steps = ttk.Treeview(steps, columns=('state',), height=len(_STEP_ORDER))
        self.steps.column('#0', width=200)
        self.steps.column('state', width=90)
        for step in _STEP_ORDER:
            self.steps.insert('', 'end', iid=step, text=f'{step}  {T.STEP_NAMES[step]}', values=('',))
        self.steps.pack(fill='y')
        self.log_text = tk.Text(body, height=14, width=70, state='disabled', wrap='word')
        self.log_text.pack(side='left', fill='both', expand=True, padx=6)
        self.pending_label = ttk.Label(self.root, text='')
        self.pending_label.pack(fill='x', **pad)
        self.scan_entry.focus_set()

    # ── input ────────────────────────────────────────────────────────────────
    def _refresh_ports(self):
        try:
            ports = list_ports()
        except Exception:  # noqa: BLE001 — no pyserial tools: no list
            ports = []
        self.port_box['values'] = [device for device, _vid, _pid in ports]
        chosen = preselected_port(ports) or self.store.load_config().get('last_port') or ''
        if chosen in self.port_box['values']:
            self.port_var.set(chosen)
        if not ports:
            self._log(T.PORT_NONE)

    def _connect(self):
        port = self.port_var.get().strip()
        if not port:
            self._log(T.PORT_NONE)
            return
        config = self.store.load_config()
        config['last_port'] = port
        self.store.save_config(config)
        self._dispatch(sc.Action('connect', {'port': port}))

    def _set_mode(self, mode):
        self.controller.set_mode(mode)
        self.mode_var.set(self.controller.mode)
        self._render()

    def _on_return(self, _event=None):
        text = self.scan_entry.get()
        self.scan_entry.delete(0, 'end')
        action = self.controller.scan(text) if text.strip() else self.controller.enter()
        self._dispatch(action)

    def _dispatch(self, action):
        if action is not None:
            self.controller.started(action)
            if action.kind in ('calibrate', 'incoming', 'check', 'gauge', 'reference', 'direction', 'motion'):
                self.step_state = {}
            self.worker.jobs.put(action)
        self._render()

    def _sign_in_dialog(self):
        config = self.store.load_config()
        dialog = tk.Toplevel(self.root)
        dialog.title(T.CLOUD_SIGN_IN_TITLE)
        dialog.transient(self.root)
        fields = {}
        for row, (key, label, value, secret) in enumerate((
                ('url', T.LBL_SUPABASE_URL, config.get('supabase_url') or '', False),
                ('anon_key', T.LBL_ANON_KEY, config.get('supabase_anon_key') or '', False),
                ('username', T.LBL_USERNAME, '', False),
                ('password', T.LBL_PASSWORD, '', True))):
            ttk.Label(dialog, text=label).grid(row=row, column=0, sticky='w', padx=6, pady=3)
            entry = ttk.Entry(dialog, width=48, show='*' if secret else '')
            entry.insert(0, value)
            entry.grid(row=row, column=1, padx=6, pady=3)
            fields[key] = entry

        def submit(_event=None):
            params = {key: entry.get() for key, entry in fields.items()}
            fields['password'].delete(0, 'end')
            dialog.destroy()
            self._dispatch(sc.Action('sign_in', params))

        buttons = ttk.Frame(dialog)
        buttons.grid(row=4, column=0, columnspan=2, pady=6)
        ttk.Button(buttons, text=T.BTN_OK, command=submit).pack(side='left', padx=6)
        ttk.Button(buttons, text=T.BTN_CANCEL, command=dialog.destroy).pack(side='left', padx=6)
        fields['password'].bind('<Return>', submit)
        (fields['username'] if fields['url'].get() else fields['url']).focus_set()

    # ── events from the worker ───────────────────────────────────────────────
    def _poll(self):
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def _handle(self, event):
        if event[0] == 'notify':
            _tag, kind, text, data = event
            if kind == 'step':
                for step, state in self.step_state.items():
                    if state == T.STEP_RUNNING:
                        self.step_state[step] = T.STEP_DONE
                self.step_state[data['step']] = T.STEP_RUNNING
            elif kind == 'live':
                self.live.configure(text=text)
            elif kind == 'prompt':
                self.status.configure(text=text)
                self._log(text)
            else:
                self._log(text)
            self._render_steps()
            return
        if event[0] == 'cloud':
            self._refresh_cloud()
            return
        _tag, action, ok, message, result = event
        for step, state in self.step_state.items():
            if state == T.STEP_RUNNING:
                self.step_state[step] = T.STEP_DONE if ok else T.STEP_FAILED
        if ok and action.kind == 'calibrate':
            self.step_state['M'] = T.STEP_RUNNING
        self.controller.finished(action, ok, message, result)
        self._log(self.controller.status if ok else message)
        self._refresh_cloud()
        self._render()

    # ── rendering ────────────────────────────────────────────────────────────
    def _render(self):
        enabled = self.controller.enabled_modes()
        for mode, button in self.mode_buttons.items():
            button.state(['!disabled'] if mode in enabled and not self.controller.busy else ['disabled'])
        if self.controller.mode == sc.MODE_REFERENCE:
            self.reference_bar.pack(fill='x', padx=6, pady=3, after=self.mode_buttons[sc.MODE_REFERENCE].master)
        else:
            self.reference_bar.pack_forget()
        self.cap_label.configure(text=self.controller.cap or '–')
        self.status.configure(text=self.controller.status)
        self.swatch.delete('all')
        if self.controller.mark_colour:
            self.swatch.create_rectangle(2, 2, 47, 31, fill=self.controller.mark_colour, outline='#444444')
        if self.controller.awaiting_mark is None and self.step_state.get('M') == T.STEP_RUNNING:
            self.step_state['M'] = T.STEP_DONE
        self._render_steps()

    def _render_steps(self):
        for step in _STEP_ORDER:
            self.steps.set(step, 'state', self.step_state.get(step, ''))

    def _refresh_cloud(self):
        session = self.worker.session
        signed_in = session is not None and session.signed_in
        self.cloud_label.configure(text=T.CLOUD_SIGNED_IN.format(user=session.username) if signed_in
                                   else T.CLOUD_SIGNED_OUT)
        lines = [T.CLOUD_PENDING.format(n=len(self.store.pending_records()))]
        rejected = self.store.rejected_records()
        if rejected:
            lines.append(T.REJECTED_LIST.format(list=', '.join(f'{s} ({c})' for s, c in rejected)))
        self.pending_label.configure(text=' · '.join(lines))

    def _log(self, text):
        if not text:
            return
        stamp = datetime.datetime.now().strftime('%H:%M:%S')
        self.log_text.configure(state='normal')
        self.log_text.insert('end', f'{stamp}  {text}\n')
        self.log_text.see('end')
        self.log_text.configure(state='disabled')

    def close(self):
        self.worker.jobs.put(None)
        self.root.destroy()


def _setup_logging(store):
    store.root.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(store.log_path, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    logger = logging.getLogger('servo_station')
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def main(argv=None):
    if sys.version_info < (3, 12):
        print(T.PYTHON_TOO_OLD)
        return 1
    try:
        import serial  # noqa: F401 — pyserial; FeetechBus opens the port with it
    except ImportError:
        print(T.PYSERIAL_MISSING)
        return 1
    parser = argparse.ArgumentParser(description=T.TITLE)
    parser.add_argument('--data-dir', default=None)
    args = parser.parse_args(argv)
    store = records.Store(records.default_data_dir(args.data_dir))
    _setup_logging(store)
    log.info('station start, data dir %s, spec %s (pinned=%s)', store.root, fss.V1.digest, fss.V1.pinned)
    root = tk.Tk()
    StationApp(root, store, fss.V1)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
