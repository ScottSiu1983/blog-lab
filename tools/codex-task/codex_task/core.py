"""Core of codex-task: run a long Codex task so that it survives context compaction.

Self-contained (standard library only). Two layers:
1. File layer: cwd/TASK.md holds the task text and cwd/AGENTS.md carries a marked section telling the agent to
   re-read TASK.md after compaction.
2. Orchestrator layer: after each run we check completion and, if incomplete, resume the same thread.

Model choice belongs to the caller. Nothing here picks a model, and it must never choose one by task length.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import config

SANDBOXES = ('read-only', 'workspace-write')
ROLLOUT_GLOB = '*/*/*/rollout-*{thread}.jsonl'
AGENTS_MARKER = '<!-- headless-codex-task: read TASK.md after compaction -->'
# Per-model call parameters applied when the caller's --model matches exactly. Caller --config keys win.
# gpt-6-astra: Codex catalog context_window is 272000 (max 872000); compaction in exec drops the task message,
# so widen the window to avoid compaction. Remove this entry once Astra's compaction is fixed.
MODEL_OVERRIDES = {'gpt-6-astra': ['model_context_window=872000']}
GUARD_INTERVAL = 60  # seconds between compaction-loop checks
COMPACTION_LOOP_THRESHOLD = 3  # compactions without progress in one attempt that stop it
AGENTS_END = '<!-- /headless-codex-task -->'
FINGERPRINT_SKIP = {'TASK.md', 'AGENTS.md', '.codex-last-message.txt'}
FINGERPRINT_SKIP_DIRS = {'.git', 'node_modules', '__pycache__'}
FORBIDDEN_CONFIG = re.compile(r'(^|\.)(sandbox_mode|approval_policy)\s*=|danger|bypass', re.I)


def env():
    return dict(os.environ)


# ---- safety limits (enforced for every entrance) ----

def _under(path, roots):
    real = os.path.realpath(path)
    for root in roots:
        r = os.path.realpath(root)
        if real == r or real.startswith(r.rstrip('/') + '/'):
            return True
    return False


def validate_request(cwd, add_dirs=(), sandbox=None, extra_config=()):
    """Raise ValueError when the request is outside the safety limits (or the configuration is unusable)."""
    allowed = config.load()['allowed_roots']
    if sandbox not in SANDBOXES:
        raise ValueError(f'sandbox must be one of {", ".join(SANDBOXES)} (got {sandbox!r}); '
                         'danger-full-access and bypass modes are not allowed')
    for label, p in [('cwd', cwd)] + [('add-dir', d) for d in add_dirs]:
        if not _under(p, allowed):
            raise ValueError(f'{label} {p} resolves outside the configured allowed_roots: {", ".join(allowed)}')
    for c in extra_config:
        if FORBIDDEN_CONFIG.search(str(c)):
            raise ValueError(f'config override not allowed: {c}')


def _config_key(c):
    return str(c).split('=', 1)[0].strip()


def model_overrides(model, config=()):
    """Table overrides for `model` whose key the caller did not set itself (caller --config wins)."""
    keys = {_config_key(c) for c in config}
    return [o for o in MODEL_OVERRIDES.get(model, []) if _config_key(o) not in keys] if model else []


# ---- process helpers ----

def kill_process_group(proc, grace=10):
    """SIGTERM the child's process group, then SIGKILL whatever is left after `grace` seconds."""
    import signal
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = proc.pid
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5)):
        try:
            os.killpg(pgid, sig)
        except OSError:
            return
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                os.killpg(pgid, 0)
            except OSError:
                return
            time.sleep(0.1)
    try:
        proc.wait(timeout=1)
    except Exception:
        pass


def _run_group(cmd, cwd, e, timeout, on_start, kill_grace=10, guard=None, guard_interval=None):
    """Run cmd in its own session. Returns (returncode or None, stdout, stderr, timed_out, guard_reason).

    guard(stdout_reader) is called every guard_interval seconds while the process runs; stdout_reader() returns the
    stdout written so far (read with pread, so the child's file offset is untouched). A non-empty string result
    stops the process group and is returned as guard_reason.
    """
    with tempfile.TemporaryFile('w+', encoding='utf-8') as out, tempfile.TemporaryFile('w+', encoding='utf-8') as err:
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=err, text=True, env=e,
                                start_new_session=True)
        if on_start:
            try:
                on_start(proc)
            except Exception:
                pass
        start = time.monotonic()
        timed_out = False
        guard_reason = None
        interval = GUARD_INTERVAL if guard_interval is None else guard_interval

        def reader():
            chunks, off = [], 0
            while True:
                b = os.pread(out.fileno(), 1 << 20, off)
                if not b:
                    break
                chunks.append(b)
                off += len(b)
            return b''.join(chunks).decode('utf-8', 'replace')

        next_guard = start + interval
        while proc.poll() is None:
            time.sleep(0.2)
            now = time.monotonic()
            if now - start > timeout:
                timed_out = True
                kill_process_group(proc, kill_grace)
                break
            if guard and now >= next_guard:
                next_guard = now + interval
                try:
                    guard_reason = guard(reader)
                except Exception:
                    guard_reason = None
                if guard_reason:
                    kill_process_group(proc, kill_grace)
                    break
        if not timed_out and not guard_reason:
            try:
                os.killpg(proc.pid, 9)  # sweep grandchildren the child left behind
            except OSError:
                pass
        proc.wait()
        out.seek(0)
        err.seek(0)
        return (None if timed_out else proc.returncode), out.read(), err.read(), timed_out, guard_reason


def _execute_codex(cmd, cwd, log, timeout, on_start=None, guard=None, guard_interval=None):
    """Run a prepared codex command line in its own process group and log it.

    Returns (ok, thread or None, log, guard_reason or None).
    """
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    code, stdout, stderr, timed_out, guard_reason = _run_group(cmd, cwd, env(), timeout, on_start, guard=guard,
                                                              guard_interval=guard_interval)
    if timed_out:
        log.write_text(stdout + '\n--- stderr ---\n' + stderr + f'\n--- timeout after {timeout // 60} minutes ---\n',
                       encoding='utf-8')
        return False, None, log, None
    log.write_text(stdout + ('\n--- stderr ---\n' + stderr if stderr else ''), encoding='utf-8')
    if guard_reason:
        with open(log, 'a', encoding='utf-8') as f:
            f.write(f'\n--- stopped by guard: {guard_reason} ---\n')
    thread = _thread_from_stdout(stdout)
    if not thread:
        m = re.search(r'session id:\s*(\S+)', stdout + stderr)
        thread = m.group(1) if m else None
    return code == 0, thread, log, guard_reason


def _thread_from_stdout(stdout):
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get('type') == 'thread.started' and ev.get('thread_id'):
            return ev['thread_id']
    return None


def _exec_cmd(prompt, cwd, *, sandbox, add_dirs, model, effort, network, search, schema, output_file, extra_config):
    cmd = [config.load()['codex_bin']] + (['--search'] if search else []) + ['exec', '-C', str(cwd), '-s', sandbox]
    if model:
        cmd += ['-m', model]
    if effort:
        cmd += ['-c', f'model_reasoning_effort="{effort}"']
    if network:
        cmd += ['-c', 'sandbox_workspace_write.network_access=true']
    cmd += ['-c', 'approval_policy="never"']
    for c in extra_config:
        cmd += ['-c', c]
    for d in add_dirs:
        cmd += ['--add-dir', str(d)]
    if schema:
        cmd += ['--output-schema', str(schema)]
    cmd += ['-o', str(output_file), '--json', '--skip-git-repo-check', prompt]
    return cmd


# ---- compaction inspection ----

def find_rollout(thread_id):
    """Newest rollout file of a Codex thread under ~/.codex/sessions, or None."""
    if not thread_id:
        return None
    root = Path.home() / '.codex' / 'sessions'
    hits = sorted(root.glob(ROLLOUT_GLOB.format(thread=thread_id)), key=lambda p: p.stat().st_mtime)
    return hits[-1] if hits else None


def _norm(text):
    return ' '.join(text.split())


def compaction_report(thread_id, task_text):
    """Inspect a thread's rollout for context compactions.

    Returns {"rollout": path or None, "compactions": int, "task_retained": True/False/None}. task_retained looks at
    the last compaction: True when a user message of its replacement_history contains the task's first ~200
    characters (whitespace normalized), False when none does, None when there was no compaction or no rollout.
    """
    rollout = find_rollout(thread_id)
    out = {'rollout': str(rollout) if rollout else None, 'compactions': 0, 'task_retained': None}
    if not rollout:
        return out
    last = None
    try:
        with open(rollout, encoding='utf-8') as f:
            for line in f:
                if '"compacted"' not in line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict) and ev.get('type') == 'compacted':
                    out['compactions'] += 1
                    last = ev.get('payload') or {}
    except OSError:
        return out
    if last is None:
        return out
    needle = _norm(task_text)[:200]
    retained = False
    for item in last.get('replacement_history') or []:
        if not isinstance(item, dict) or item.get('type') != 'message' or item.get('role') != 'user':
            continue
        content = item.get('content') or []
        text = ' '.join(c.get('text', '') for c in content if isinstance(c, dict)) if isinstance(content, list) else str(content)
        if needle and needle in _norm(text):
            retained = True
            break
    out['task_retained'] = retained
    return out


# ---- compaction loop guard ----

def _count_compacted(rollout):
    n = 0
    try:
        with open(rollout, encoding='utf-8') as f:
            for line in f:
                if '"compacted"' in line:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(ev, dict) and ev.get('type') == 'compacted':
                        n += 1
    except OSError:
        pass
    return n


def find_rollout_by_cwd(cwd, since):
    """Newest rollout modified at or after `since` (epoch seconds) whose session_meta cwd is `cwd`, or None."""
    root = Path.home() / '.codex' / 'sessions'
    want = os.path.realpath(cwd)
    hits = []
    for p in root.glob('*/*/*/rollout-*.jsonl'):
        try:
            if p.stat().st_mtime >= since:
                hits.append(p)
        except OSError:
            continue
    for p in sorted(hits, key=lambda q: q.stat().st_mtime, reverse=True):
        try:
            with open(p, encoding='utf-8') as f:
                meta = json.loads(f.readline())
        except (OSError, ValueError):
            continue
        pay = meta.get('payload') if isinstance(meta, dict) else None
        if isinstance(pay, dict) and meta.get('type') == 'session_meta' and pay.get('cwd') \
                and os.path.realpath(pay['cwd']) == want:
            return p
    return None


def progress_fingerprint(cwd, since, exclude=()):
    """Fingerprint of work-dir progress: (path, mtime_ns, size) of progress.md, findings.md and any file modified
    since `since` (epoch seconds), skipping TASK.md, AGENTS.md, Codex output files and `exclude` paths."""
    cwd = Path(cwd)
    skip = {os.path.realpath(x) for x in exclude}
    since_ns = int(since * 1e9)
    items = []
    for dirpath, dirnames, filenames in os.walk(cwd):
        dirnames[:] = [d for d in dirnames if d not in FINGERPRINT_SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            if name in FINGERPRINT_SKIP and Path(dirpath) == cwd:
                continue
            if os.path.realpath(p) in skip:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime_ns >= since_ns or (Path(dirpath) == cwd and name in ('progress.md', 'findings.md')):
                items.append((str(p.relative_to(cwd)), st.st_mtime_ns, st.st_size))
    return tuple(sorted(items))


def make_compaction_guard(cwd, since, thread=None, baseline=0, exclude=(), threshold=None):
    """Guard for _run_group: stop an attempt when `threshold` compactions pile up with no progress between them."""
    threshold = threshold or COMPACTION_LOOP_THRESHOLD
    state = {'seen': 0, 'fps': [], 'rollout': None}

    def guard(read_stdout):
        t = thread or _thread_from_stdout(read_stdout())
        rollout = state['rollout']
        if rollout is None or not Path(rollout).exists():
            rollout = find_rollout(t) if t else find_rollout_by_cwd(cwd, since)
            if rollout is None and t:
                rollout = find_rollout_by_cwd(cwd, since)
            state['rollout'] = rollout
        if rollout is None:
            return None
        count = _count_compacted(rollout) - baseline
        if count <= state['seen']:
            return None
        fp = progress_fingerprint(cwd, since, exclude)
        state['fps'] += [fp] * (count - state['seen'])
        state['seen'] = count
        fps = state['fps']
        if len(fps) >= threshold and all(f == fps[-1] for f in fps[-threshold:]):
            return (f'compaction loop: {count} compactions in this attempt, {threshold} in a row with no change '
                    'in progress.md, findings.md, the report or other work files')
        return None
    return guard


# ---- file layer ----

def _agents_section(report):
    return (f'{AGENTS_MARKER}\n'
            '## Current task (added automatically)\n\n'
            '- The task text is in `TASK.md` in the current directory. At any time, including after the context is compacted or the thread is resumed, read `TASK.md`, `progress.md` and `findings.md` (if present) before acting.\n'
            '- Whenever an item of the task is finished, immediately append one line to the end of `progress.md` (what was done, where the output is). After compaction or resume, do not redo items already recorded in `progress.md`.\n'
            '- Append findings to `findings.md` as you go.\n'
            f'- The task is not complete until the report file `{report}` is written.\n'
            f'{AGENTS_END}\n')


def ensure_task_files(cwd, task, report):
    """Write cwd/TASK.md and make sure cwd/AGENTS.md carries the marked section exactly once (current version).

    An older version of the section (marker line up to the end marker, or up to the next heading / end of file for
    sections written before the end marker existed) is replaced in place.
    """
    cwd = Path(cwd)
    cwd.mkdir(parents=True, exist_ok=True)
    (cwd / 'TASK.md').write_text(task, encoding='utf-8')
    agents = cwd / 'AGENTS.md'
    section = _agents_section(report)
    if not agents.exists():
        agents.write_text(section, encoding='utf-8')
        return
    text = agents.read_text(encoding='utf-8')
    start = text.find(AGENTS_MARKER)
    if start < 0:
        agents.write_text(text + ('' if text.endswith('\n') or not text else '\n') + '\n' + section, encoding='utf-8')
        return
    end_at = text.find(AGENTS_END, start)
    if end_at >= 0:
        end = end_at + len(AGENTS_END)
        end = end + 1 if text[end:end + 1] == '\n' else end
    else:  # legacy section: runs to the next heading that is not ours, else to the end of the file
        m = re.search(r'^#{1,6} (?!Current task \(added automatically\))', text[start + len(AGENTS_MARKER):], re.M)
        end = start + len(AGENTS_MARKER) + m.start() if m else len(text)
    new = text[:start] + section + text[end:]
    if new != text:
        agents.write_text(new, encoding='utf-8')


# ---- completion check and resume ----

def _incomplete_reason(cwd, report, schema, last_message, check):
    """None when the task is complete, else a short reason."""
    rp = Path(cwd) / report
    if not rp.is_file() or rp.stat().st_size == 0:
        return f'report file {report} missing or empty'
    if schema:
        try:
            required = json.loads(Path(schema).read_text(encoding='utf-8')).get('required') or []
        except (OSError, ValueError, AttributeError):
            required = []
        try:
            data = json.loads(Path(last_message).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return 'last message is missing or not valid JSON'
        if not isinstance(data, dict):
            return 'last message JSON is not an object'
        missing = [k for k in required if k not in data]
        if missing:
            return 'last message lacks required keys: ' + ', '.join(missing)
    if check:
        why = check(Path(cwd))
        if isinstance(why, str):
            return why
    return None


def _resume_cmd(thread, prompt, cwd, *, sandbox, schema, add_dirs, model, effort, network, search, output_file, extra_config):
    """`codex exec resume` accepts no -s, -C or --add-dir, so those become -c overrides (cwd is the subprocess cwd)."""
    cmd = [config.load()['codex_bin']] + (['--search'] if search else []) + ['exec', 'resume', thread]
    if model:
        cmd += ['-m', model]
    if effort:
        cmd += ['-c', f'model_reasoning_effort="{effort}"']
    cmd += ['-c', f'sandbox_mode="{sandbox}"']
    if network:
        cmd += ['-c', 'sandbox_workspace_write.network_access=true']
    if add_dirs:
        cmd += ['-c', 'sandbox_workspace_write.writable_roots=' + json.dumps([str(d) for d in add_dirs])]
    cmd += ['-c', 'approval_policy="never"']
    for c in extra_config:
        cmd += ['-c', c]
    if schema:
        cmd += ['--output-schema', str(schema)]
    cmd += ['-o', str(output_file), '--json', '--skip-git-repo-check', prompt]
    return cmd


def run_codex_task(task, cwd, log, *, sandbox, report, schema=None, add_dirs=(), model=None, effort=None,
                   network=False, search=False, timeout=45 * 60, max_resumes=2, check=None, extra_config=(),
                   on_start=None, progress=None, guard_interval=None):
    """Run a long Codex task so that it survives context compaction. Never raises for an unfinished task.

    Raises ValueError when the request violates the safety limits (see validate_request).
    on_start(proc) is called when each Codex process starts; progress(thread, attempts) after each attempt.
    Returns {"ok", "thread", "reason", "attempts": [{"log", "compactions", "task_retained", "reason"}],
    "applied_overrides"}. An attempt stopped by the compaction-loop guard is final: no resume, reason starts with
    "compaction loop:".
    """
    validate_request(cwd, add_dirs, sandbox, extra_config)
    applied = model_overrides(model, extra_config)
    extra_config = applied + list(extra_config)
    cwd = Path(cwd)
    ensure_task_files(cwd, task, report)
    last_message = cwd / '.codex-last-message.txt'
    log = Path(log)
    prompt = f'{task}\n\nWhen done, write the report to {report}.'
    attempts = []

    def attempt(cmd, alog_path, thread, baseline):
        since = time.time() - 1
        guard = make_compaction_guard(cwd, since, thread, baseline, exclude=[log, alog_path, last_message])
        return _execute_codex(cmd, cwd, alog_path, timeout, on_start, guard=guard, guard_interval=guard_interval)

    cmd = _exec_cmd(prompt, cwd, sandbox=sandbox, add_dirs=add_dirs, model=model, effort=effort, network=network,
                    search=search, schema=schema, output_file=last_message, extra_config=extra_config)
    ok, thread, alog, stopped = attempt(cmd, log, None, 0)
    reason = _incomplete_reason(cwd, report, schema, last_message, check)
    for n in range(max_resumes + 1):
        cr = compaction_report(thread, task)
        if stopped:
            reason = f'{stopped} (compactions: {cr["compactions"]})'
        attempts.append({'log': str(alog), 'compactions': cr['compactions'], 'task_retained': cr['task_retained'],
                         'reason': reason})
        if progress:
            try:
                progress(thread, attempts)
            except Exception:
                pass
        if reason is None or n == max_resumes or not thread or stopped:
            break
        rlog = log.with_name(f'{log.stem}.resume{n + 1}.log')
        rprompt = (f'The previous run did not finish (reason: {reason}). The original task follows; continue from the actual '
                   f'state of the working directory and write {report} when done:\n\n{task}')
        cmd = _resume_cmd(thread, rprompt, cwd, sandbox=sandbox, schema=schema, add_dirs=add_dirs, model=model,
                          effort=effort, network=network, search=search, output_file=last_message,
                          extra_config=extra_config)
        ok, t2, alog, stopped = attempt(cmd, rlog, thread, cr['compactions'])
        thread = t2 or thread
        reason = _incomplete_reason(cwd, report, schema, last_message, check)
    return {'ok': reason is None and not stopped, 'thread': thread, 'reason': reason, 'attempts': attempts,
            'applied_overrides': applied}
