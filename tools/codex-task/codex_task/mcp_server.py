"""Minimal MCP server over stdio (newline-delimited JSON-RPC 2.0), no third-party dependencies.

Handles initialize, notifications/initialized, ping, tools/list and tools/call. Tool failures (unknown job, safety
violation, bad arguments) come back as a tool result with isError true and a text message.
"""
import json
import sys

from . import VERSION, config, jobs

PROTOCOL = '2025-06-18'
JOB = {'type': 'object', 'properties': {'job_id': {'type': 'string'}}, 'required': ['job_id'], 'additionalProperties': False}
TOOLS = [
    {'name': 'codex_task_submit',
     'description': 'Start a long Codex task as a background job that survives context compaction and caller exit. '
                    'Returns job_id. The caller chooses the model; omit model for the default. Sandbox is read-only or '
                    'workspace-write; cwd and add_dirs must be under one of the configured allowed_roots.',
     'inputSchema': {'type': 'object', 'properties': {
         'task': {'type': 'string', 'description': 'Full task text'},
         'report': {'type': 'string', 'description': 'Report file name relative to cwd; the task is not complete until it exists'},
         'sandbox': {'type': 'string', 'enum': ['read-only', 'workspace-write']},
         'cwd': {'type': 'string', 'description': 'Working directory (default: <job dir>/work)'},
         'schema': {'type': 'string', 'description': 'Path of a JSON schema file for the final message'},
         'model': {'type': 'string'}, 'effort': {'type': 'string'},
         'add_dirs': {'type': 'array', 'items': {'type': 'string'}},
         'network': {'type': 'boolean'}, 'max_resumes': {'type': 'integer', 'minimum': 0, 'maximum': 5},
         'timeout_min': {'type': 'integer', 'minimum': 1, 'description': 'Per attempt, default 45'},
         'label': {'type': 'string'}},
         'required': ['task', 'report', 'sandbox'], 'additionalProperties': False}},
    {'name': 'codex_task_status', 'description': 'Current state of a job (status queued|running|complete|incomplete|failed|cancelled).',
     'inputSchema': JOB},
    {'name': 'codex_task_wait',
     'description': 'Wait for a job to finish, at most timeout_sec (default 50, max 600). Returns the state; '
                    'check status, call again if it is still running.',
     'inputSchema': {'type': 'object', 'properties': {'job_id': {'type': 'string'},
                     'timeout_sec': {'type': 'integer', 'minimum': 1, 'maximum': 600}},
                     'required': ['job_id'], 'additionalProperties': False}},
    {'name': 'codex_task_result', 'description': 'Final result of a finished job, with report and last message paths; '
                                                 'the state if it is still running.', 'inputSchema': JOB},
    {'name': 'codex_task_cancel', 'description': 'Kill a job process group and mark it cancelled.', 'inputSchema': JOB},
    {'name': 'codex_task_list',
     'description': 'List jobs, newest first.',
     'inputSchema': {'type': 'object', 'properties': {'active_only': {'type': 'boolean'}}, 'additionalProperties': False}},
]


def call_tool(name, args):
    """Return a JSON-serializable result; raise ValueError/KeyError for tool errors."""
    config.load()  # refuse to run without a usable configuration (ConfigError is a ValueError)
    if name == 'codex_task_submit':
        for k in ('task', 'report', 'sandbox'):
            if not args.get(k):
                raise ValueError(f'missing required argument: {k}')
        st = jobs.submit(args['task'], report=args['report'], sandbox=args['sandbox'], cwd=args.get('cwd'),
                         schema=args.get('schema'), model=args.get('model'), effort=args.get('effort'),
                         add_dirs=args.get('add_dirs') or [], network=bool(args.get('network')),
                         max_resumes=args.get('max_resumes', 2), timeout_min=args.get('timeout_min', 45),
                         label=args.get('label'))
        return {'schema_version': 1, 'job_id': st['job_id'], 'status': st['status'],
                'state_path': str(jobs.job_dir(st['job_id']) / 'state.json')}
    if name == 'codex_task_list':
        return {'schema_version': 1, 'jobs': jobs.list_jobs(bool(args.get('active_only')))}
    job_id = args.get('job_id')
    if not job_id:
        raise ValueError('missing required argument: job_id')
    if name == 'codex_task_status':
        return jobs.read_state(job_id)
    if name == 'codex_task_wait':
        t = min(int(args.get('timeout_sec', 50)), jobs.MAX_WAIT_SEC)
        return jobs.wait(job_id, t)
    if name == 'codex_task_result':
        res, st = jobs.result(job_id)
        return res if res else st
    if name == 'codex_task_cancel':
        return jobs.cancel(job_id)
    raise ValueError(f'unknown tool: {name}')


def handle(msg):
    """Return the response dict for a request, or None for a notification."""
    method, mid = msg.get('method'), msg.get('id')
    if mid is None:
        return None  # notifications (notifications/initialized, cancelled, ...) get no reply

    def ok(result):
        return {'jsonrpc': '2.0', 'id': mid, 'result': result}

    def err(code, text):
        return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': code, 'message': text}}
    params = msg.get('params') or {}
    if method == 'initialize':
        return ok({'protocolVersion': params.get('protocolVersion') or PROTOCOL, 'capabilities': {'tools': {}},
                   'serverInfo': {'name': 'codex-task', 'version': VERSION}})
    if method == 'ping':
        return ok({})
    if method == 'tools/list':
        return ok({'tools': TOOLS})
    if method == 'tools/call':
        name = params.get('name')
        if name not in {t['name'] for t in TOOLS}:
            return err(-32602, f'unknown tool: {name}')
        try:
            res = call_tool(name, params.get('arguments') or {})
            return ok({'content': [{'type': 'text', 'text': json.dumps(res, ensure_ascii=False, indent=1)}], 'isError': False})
        except (ValueError, KeyError, OSError, TypeError) as exc:
            text = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
            return ok({'content': [{'type': 'text', 'text': f'error: {text}'}], 'isError': True})
    return err(-32601, f'method not found: {method}')


def serve(stdin=None, stdout=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict):
                raise ValueError('not an object')
        except ValueError:
            resp = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}
        else:
            resp = handle(msg)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + '\n')
            stdout.flush()
    return 0
