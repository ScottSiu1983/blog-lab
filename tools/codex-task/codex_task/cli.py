"""codex-task command line. Entrances: submit/status/wait/result/cancel/list/run, and `mcp` (stdio MCP server)."""
import argparse
import json
import sys
from pathlib import Path

from . import VERSION, config, jobs

EPILOG = (
    'JSON on stdout. submit: {"schema_version": 1, "job_id", "status", "state_path", "job_dir"}.\n'
    'status/wait: the job state. result/run: {"schema_version": 1, "job_id", "status", "ok", "thread", "reason",\n'
    '"applied_overrides", "attempts": [{"log", "compactions", "task_retained", "reason"}], "report_path", "last_message_path", "cwd"}.\n'
    'Exit codes: run: 0 complete, 1 incomplete/failed/cancelled, 2 usage, setup or safety error (message on stderr).\n'
    'wait/result: 0 complete, 1 incomplete/failed/cancelled, 3 still running, 2 error. Other commands: 0 or 2.\n'
    'Sandbox is read-only or workspace-write only; --cwd and --add-dir must resolve under one of the allowed_roots in\n'
    'the config file ($CODEX_TASK_CONFIG, default ~/.config/codex-task/config.json). The caller chooses the model;\n'
    'this tool never picks one.')


def _dump(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def _submit_args(p):
    p.add_argument('--cwd', help='directory Codex works in (default: <job dir>/work)')
    p.add_argument('--task-file', required=True, help='file with the task text, or - to read it from stdin')
    p.add_argument('--report', required=True, help='report file name, relative to --cwd')
    p.add_argument('--sandbox', required=True, help='read-only or workspace-write')
    p.add_argument('--schema', help='JSON schema file for the final message (its top-level required keys are checked)')
    p.add_argument('--model', help='model name, exactly as the user asked; omit for the Codex default')
    p.add_argument('--effort', help='reasoning effort, e.g. low, medium, high')
    p.add_argument('--add-dir', action='append', default=[], help='extra writable directory (repeatable)')
    p.add_argument('--network', action='store_true', help='allow network access in workspace-write')
    p.add_argument('--max-resumes', type=int, default=2)
    p.add_argument('--timeout-min', type=int, default=45, help='per attempt')
    p.add_argument('--config', action='append', default=[], help='extra key=value passed to codex as -c')
    p.add_argument('--label', help='free-text label stored in the job state')
    p.add_argument('--log', help='log file path for the first attempt (default: <job dir>/codex-run.log)')


def _do_submit(a):
    task = sys.stdin.read() if a.task_file == '-' else Path(a.task_file).read_text(encoding='utf-8')
    return jobs.submit(task, report=a.report, sandbox=a.sandbox, cwd=a.cwd, schema=a.schema, model=a.model,
                       effort=a.effort, add_dirs=a.add_dir, network=a.network, max_resumes=a.max_resumes,
                       timeout_min=a.timeout_min, config=a.config, label=a.label, log=a.log)


def _exit_for(status):
    return 0 if status == 'complete' else 3 if status in jobs.ACTIVE else 1


def build_parser():
    ap = argparse.ArgumentParser(prog='codex-task', formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG,
                                 description='Run long Codex tasks as background jobs that survive context compaction.')
    ap.add_argument('--version', action='version', version=f'codex-task {VERSION}')
    sub = ap.add_subparsers(dest='cmd', required=True)
    _submit_args(sub.add_parser('submit', help='start a background job and print its id'))
    _submit_args(sub.add_parser('run', help='submit and wait; prints the final result'))
    sub.add_parser('status', help='print a job state').add_argument('job')
    w = sub.add_parser('wait', help='wait for a job; exit 3 if still running at timeout')
    w.add_argument('job')
    w.add_argument('--timeout-sec', type=int)
    sub.add_parser('result', help='print the final result of a job').add_argument('job')
    sub.add_parser('cancel', help='kill a job process group and mark it cancelled').add_argument('job')
    sub.add_parser('list', help='list jobs, newest first').add_argument('--active', action='store_true')
    sub.add_parser('mcp', help='run a stdio MCP server exposing the codex_task_* tools')
    sub.add_parser('_worker', help=argparse.SUPPRESS).add_argument('job')
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        config.load()  # refuse to run without a usable configuration
        if a.cmd == '_worker':
            return jobs.run_worker(a.job)
        if a.cmd == 'mcp':
            from . import mcp_server
            return mcp_server.serve()
        if a.cmd == 'submit':
            st = _do_submit(a)
            _dump({'schema_version': 1, 'job_id': st['job_id'], 'status': st['status'],
                   'state_path': str(jobs.job_dir(st['job_id']) / 'state.json'),
                   'job_dir': str(jobs.job_dir(st['job_id']))})
            return 0
        if a.cmd == 'run':
            st = _do_submit(a)
            jobs.wait(st['job_id'])
            res, st = jobs.result(st['job_id'])
            _dump({'schema_version': 1, 'job_id': st['job_id'], **{k: res[k] for k in
                   ('status', 'ok', 'thread', 'reason', 'applied_overrides', 'attempts', 'report_path', 'last_message_path', 'cwd')}})
            return 0 if res['ok'] else 1
        if a.cmd == 'status':
            _dump(jobs.read_state(a.job))
            return 0
        if a.cmd == 'wait':
            st = jobs.wait(a.job, a.timeout_sec)
            _dump(st)
            return _exit_for(st['status'])
        if a.cmd == 'result':
            res, st = jobs.result(a.job)
            _dump(res if res else st)
            return _exit_for(st['status'])
        if a.cmd == 'cancel':
            _dump(jobs.cancel(a.job))
            return 0
        if a.cmd == 'list':
            _dump({'schema_version': 1, 'jobs': jobs.list_jobs(a.active)})
            return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f'codex-task setup error: {exc.args[0] if isinstance(exc, KeyError) else exc}', file=sys.stderr)
        return 2
    return 2
