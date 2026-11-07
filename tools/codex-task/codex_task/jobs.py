"""Background jobs: submit, worker, status, wait, result, cancel, list.

A job lives in <jobs root>/<job_id>/ with request.json, state.json (atomic writes), result.json (when finished),
worker.log and the per-attempt Codex logs. The worker is a detached process (own session), so the caller may exit.
"""
import datetime as dt
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import config, core

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1
ACTIVE = ('queued', 'running')
FINAL = ('complete', 'incomplete', 'failed', 'cancelled')
MAX_WAIT_SEC = 600  # per-call cap for the MCP wait tool only; the CLI waits as long as asked


def jobs_root():
    return config.load()['jobs_dir']


def now():
    return dt.datetime.now().astimezone().isoformat(timespec='seconds')


def job_dir(job_id):
    if not job_id or '/' in job_id or job_id.startswith('.'):
        raise ValueError(f'invalid job id: {job_id!r}')
    return jobs_root() / job_id


def _atomic_write(path, obj):
    path = Path(path)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def read_state(job_id, reconcile=True):
    d = job_dir(job_id)
    if not (d / 'state.json').is_file():
        raise KeyError(f'unknown job: {job_id}')
    st = _read(d / 'state.json')
    if reconcile and st['status'] in ACTIVE and st.get('pid') and not _alive(st['pid']):
        st = update_state(job_id, lambda s: s if s['status'] in FINAL else
                          dict(s, status='failed', reason='worker process died without a result'))
    return st


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie child of this process still answers kill(0); reap it if it is ours.
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


def update_state(job_id, fn):
    """Read-modify-write state.json atomically. fn(state) returns the new state."""
    d = job_dir(job_id)
    with open(d / '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        st = fn(_read(d / 'state.json'))
        st['updated_at'] = now()
        _atomic_write(d / 'state.json', st)
    return st


def submit(task, *, report, sandbox, cwd=None, schema=None, model=None, effort=None, add_dirs=(), network=False,
           max_resumes=2, timeout_min=45, config=(), label=None, log=None, start_worker=True):
    """Create a job and start its detached worker. Raises ValueError for unsafe or invalid requests."""
    if not task or not task.strip():
        raise ValueError('task text is empty')
    if not report:
        raise ValueError('report is required')
    if schema and not Path(schema).is_file():
        raise ValueError(f'schema file not found: {schema}')
    job_id = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    d = job_dir(job_id)
    cwd = str(cwd) if cwd else str(d / 'work')
    add_dirs = [str(x) for x in add_dirs]
    core.validate_request(cwd, add_dirs, sandbox, config)
    d.mkdir(parents=True)
    req = {'schema_version': SCHEMA_VERSION, 'task': task, 'cwd': cwd, 'report': report, 'sandbox': sandbox,
           'schema': str(schema) if schema else None, 'model': model, 'effort': effort, 'add_dirs': add_dirs,
           'network': bool(network), 'max_resumes': int(max_resumes), 'timeout_min': int(timeout_min),
           'config': list(config), 'label': label, 'log': str(log) if log else str(d / 'codex-run.log')}
    _atomic_write(d / 'request.json', req)
    rp = Path(report)
    state = {'schema_version': SCHEMA_VERSION, 'job_id': job_id, 'label': label, 'status': 'queued',
             'created_at': now(), 'updated_at': now(), 'pid': None, 'pgid': None, 'codex_pgid': None,
             'thread': None, 'attempts': [], 'reason': None, 'cwd': cwd,
             'report': str(rp if rp.is_absolute() else Path(cwd) / rp), 'model': model, 'effort': effort,
             'sandbox': sandbox, 'applied_overrides': core.model_overrides(model, config)}
    _atomic_write(d / 'state.json', state)
    if start_worker:
        with open(d / 'worker.log', 'ab') as wl:
            proc = subprocess.Popen([sys.executable, str(ROOT / 'codex-task'), '_worker', job_id],
                                    stdin=subprocess.DEVNULL, stdout=wl, stderr=wl, start_new_session=True,
                                    cwd=str(ROOT), env=dict(os.environ))
        update_state(job_id, lambda s: dict(s, pid=proc.pid, pgid=proc.pid) if s['status'] == 'queued' else s)
    return read_state(job_id, reconcile=False)


def run_worker(job_id):
    """Entry of the detached worker process. Returns the process exit code."""
    d = job_dir(job_id)
    req = _read(d / 'request.json')
    if read_state(job_id, reconcile=False)['status'] == 'cancelled':
        return 0
    update_state(job_id, lambda s: dict(s, status='running', pid=os.getpid(), pgid=os.getpgid(0)))

    def on_start(proc):
        update_state(job_id, lambda s: dict(s, codex_pgid=proc.pid))

    def progress(thread, attempts):
        update_state(job_id, lambda s: dict(s, thread=thread, attempts=list(attempts)))

    try:
        res = core.run_codex_task(req['task'], req['cwd'], req['log'], sandbox=req['sandbox'], report=req['report'],
                                  schema=req['schema'], add_dirs=req['add_dirs'], model=req['model'],
                                  effort=req['effort'], network=req['network'], timeout=req['timeout_min'] * 60,
                                  max_resumes=req['max_resumes'], extra_config=req['config'],
                                  on_start=on_start, progress=progress)
        status, reason = ('complete' if res['ok'] else 'incomplete'), res['reason']
    except Exception as exc:  # the worker must always leave a final state
        res = {'ok': False, 'thread': None, 'reason': f'{type(exc).__name__}: {exc}', 'attempts': [],
               'applied_overrides': core.model_overrides(req['model'], req['config'])}
        status, reason = 'failed', res['reason']
    result = {'schema_version': SCHEMA_VERSION, 'job_id': job_id, 'status': status, **res,
              'report_path': str(Path(req['cwd']) / req['report']) if not Path(req['report']).is_absolute() else req['report'],
              'last_message_path': str(Path(req['cwd']) / '.codex-last-message.txt'), 'cwd': req['cwd']}

    def final(s):
        if s['status'] == 'cancelled':  # a cancel wins over a late finish
            return s
        _atomic_write(d / 'result.json', result)
        return dict(s, status=status, reason=reason, thread=res['thread'], attempts=res['attempts'], codex_pgid=None)
    update_state(job_id, final)
    return 0


def result(job_id):
    """Final result JSON, or (None, state) shape: returns (result dict or None, state)."""
    st = read_state(job_id)
    rp = job_dir(job_id) / 'result.json'
    if st['status'] in FINAL and rp.is_file():
        return _read(rp), st
    if st['status'] in FINAL:  # failed or cancelled without a result file
        return {'schema_version': SCHEMA_VERSION, 'job_id': job_id, 'status': st['status'], 'ok': False,
                'thread': st.get('thread'), 'reason': st.get('reason'), 'attempts': st.get('attempts', []),
                'applied_overrides': st.get('applied_overrides', []),
                'report_path': st['report'], 'last_message_path': str(Path(st['cwd']) / '.codex-last-message.txt'),
                'cwd': st['cwd']}, st
    return None, st


def wait(job_id, timeout_sec=None, poll=1.0):
    """Block until the job is final or timeout. Returns the state (status may still be active)."""
    end = None if timeout_sec is None else time.monotonic() + timeout_sec
    while True:
        st = read_state(job_id)
        if st['status'] in FINAL:
            return st
        if end is not None and time.monotonic() >= end:
            return st
        time.sleep(poll if end is None else max(0.05, min(poll, end - time.monotonic())))


def _killpg(pgid, sig):
    try:
        os.killpg(pgid, sig)
        return True
    except OSError:
        return False


def cancel(job_id):
    st = read_state(job_id)
    if st['status'] in FINAL:
        return st
    # Mark first so a finishing worker cannot overwrite the cancel.
    st = update_state(job_id, lambda s: dict(s, status='cancelled', reason='cancelled by caller')
                      if s['status'] in ACTIVE else s)
    groups = [g for g in (st.get('codex_pgid'), st.get('pgid')) if g]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for g in groups:
            _killpg(g, sig)
        if sig == signal.SIGTERM:
            end = time.monotonic() + 3
            while time.monotonic() < end and any(_group_alive(g) for g in groups):
                time.sleep(0.1)
    return read_state(job_id, reconcile=False)


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except OSError:
        return False


def list_jobs(active_only=False):
    root = jobs_root()
    out = []
    if root.is_dir():
        for d in sorted(root.iterdir(), reverse=True):
            if (d / 'state.json').is_file():
                try:
                    st = read_state(d.name)
                except (KeyError, ValueError, OSError):
                    continue
                if not active_only or st['status'] in ACTIVE:
                    out.append({k: st.get(k) for k in ('job_id', 'label', 'status', 'created_at', 'updated_at', 'thread', 'cwd')})
    return out
