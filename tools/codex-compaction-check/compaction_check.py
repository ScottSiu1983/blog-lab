#!/usr/bin/env python3
"""Check a Codex rollout (session log) for context compactions and whether the task message survived them.

Usage:
  compaction_check.py ROLLOUT.jsonl [--task-file TASK.txt]
  compaction_check.py --latest --cwd DIR [--task-file TASK.txt] [--sessions-dir DIR]

Prints one JSON object:
  rollout                          path of the rollout that was inspected
  compactions                      number of entries with "type": "compacted"
  task_retained                    after the last compaction, does a user message in the rebuilt history
                                   (payload.replacement_history) still contain the task text? null when there was
                                   no compaction or no task was given
  task_in_retained_context_only    true when the task text is in payload.retained_context.user_messages but not in
                                   the rebuilt history, false when it is in the history or in neither, null when
                                   there was no compaction or no task was given

The task text is matched on its first 200 characters with whitespace normalized. Standard library only.
"""
import argparse
import json
import os
import sys
from pathlib import Path

VERSION = '0.1.0'
NEEDLE_CHARS = 200


def _norm(text):
    return ' '.join(text.split())


def _message_texts(items):
    """Texts of the user messages in a replacement_history list."""
    for item in items or []:
        if not isinstance(item, dict) or item.get('type') != 'message' or item.get('role') != 'user':
            continue
        content = item.get('content') or []
        if isinstance(content, list):
            yield ' '.join(c.get('text', '') for c in content if isinstance(c, dict))
        else:
            yield str(content)


def _retained_texts(payload):
    ctx = payload.get('retained_context')
    msgs = ctx.get('user_messages') if isinstance(ctx, dict) else None
    for m in msgs or []:
        if isinstance(m, dict) and isinstance(m.get('text'), str):
            yield m['text']


def check(rollout, task_text=None):
    """Inspect a rollout file. Returns the report dict described in the module docstring."""
    out = {'rollout': str(rollout), 'compactions': 0, 'task_retained': None, 'task_in_retained_context_only': None}
    last = None
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
                last = ev.get('payload') if isinstance(ev.get('payload'), dict) else {}
    needle = _norm(task_text)[:NEEDLE_CHARS] if task_text else ''
    if last is None or not needle:
        return out
    in_history = any(needle in _norm(t) for t in _message_texts(last.get('replacement_history')))
    in_retained = any(needle in _norm(t) for t in _retained_texts(last))
    out['task_retained'] = in_history
    out['task_in_retained_context_only'] = in_retained and not in_history
    return out


def find_latest(cwd, sessions_dir=None):
    """Newest rollout whose session_meta cwd equals realpath(cwd), or None."""
    root = Path(sessions_dir) if sessions_dir else Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'sessions'
    want = os.path.realpath(cwd)
    files = []
    for p in root.glob('**/rollout-*.jsonl'):
        try:
            files.append((p.stat().st_mtime, p))
        except OSError:
            continue
    for _, p in sorted(files, reverse=True):
        try:
            with open(p, encoding='utf-8') as f:
                meta = json.loads(f.readline())
        except (OSError, ValueError):
            continue
        pay = meta.get('payload') if isinstance(meta, dict) else None
        if meta.get('type') == 'session_meta' and isinstance(pay, dict) and pay.get('cwd') \
                and os.path.realpath(pay['cwd']) == want:
            return p
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description='Count compactions in a Codex rollout and check the task survived.')
    ap.add_argument('rollout', nargs='?', help='path of a rollout-*.jsonl file')
    ap.add_argument('--task-file', help='file with the original task text')
    ap.add_argument('--latest', action='store_true', help='use the newest rollout recorded for --cwd')
    ap.add_argument('--cwd', help='working directory of the session (with --latest)')
    ap.add_argument('--sessions-dir', help='sessions directory (default: $CODEX_HOME/sessions or ~/.codex/sessions)')
    ap.add_argument('--version', action='version', version=f'codex-compaction-check {VERSION}')
    a = ap.parse_args(argv)
    if bool(a.rollout) == bool(a.latest):
        ap.error('give either a rollout path or --latest --cwd DIR')
    task = None
    try:
        if a.task_file:
            task = Path(a.task_file).read_text(encoding='utf-8')
        if a.latest:
            if not a.cwd:
                ap.error('--latest needs --cwd DIR')
            rollout = find_latest(a.cwd, a.sessions_dir)
            if rollout is None:
                print(f'error: no rollout found for cwd {os.path.realpath(a.cwd)}', file=sys.stderr)
                return 2
        else:
            rollout = a.rollout
        print(json.dumps(check(rollout, task), indent=1))
    except OSError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
