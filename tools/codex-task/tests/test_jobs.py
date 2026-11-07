"""Tests for jobs, the CLI and the MCP server (fake codex; the detached worker is run in a thread)."""
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import types
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codex_task import cli, core, jobs, mcp_server  # noqa: E402
import test_core  # noqa: E402

GOOD = 'echo \'{"type":"thread.started","thread_id":"TH"}\'\necho ok > report.md\n'


class ThreadPopen:
    """Stands in for the detached worker: runs run_worker in a thread of the test process."""
    def __init__(self, cmd, **kw):
        self.job = cmd[-1]
        self.pid = os.getpid()
        self.t = threading.Thread(target=jobs.run_worker, args=(self.job,))
        self.t.start()


class Base(test_core.Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(jobs, 'subprocess', types.SimpleNamespace(Popen=ThreadPopen, DEVNULL=subprocess.DEVNULL))
        p.start()
        self.addCleanup(p.stop)

    def codex(self, body):
        self.set_codex(body)

    def cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, 'stdin', io.StringIO(stdin or '')):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class JobTests(Base):
    def test_submit_wait_result_list(self):
        self.codex(GOOD)
        st = jobs.submit('do it', report='report.md', sandbox='workspace-write', label='L')
        self.assertEqual(st['label'], 'L')
        final = jobs.wait(st['job_id'], 30, poll=0.05)
        self.assertEqual(final['status'], 'complete')
        for key in ('schema_version', 'job_id', 'label', 'status', 'created_at', 'updated_at', 'pid', 'pgid', 'thread',
                    'attempts', 'reason', 'cwd', 'report', 'model', 'effort', 'sandbox'):
            self.assertIn(key, final)
        self.assertEqual(final['thread'], 'TH')
        d = jobs.job_dir(st['job_id'])
        self.assertTrue((d / 'request.json').exists())
        self.assertEqual(final['cwd'], str(d / 'work'))
        res, _ = jobs.result(st['job_id'])
        self.assertTrue(res['ok'])
        self.assertTrue(res['report_path'].endswith('work/report.md'))
        self.assertTrue(res['last_message_path'].endswith('.codex-last-message.txt'))
        self.assertEqual([j['job_id'] for j in jobs.list_jobs()], [st['job_id']])
        self.assertEqual(jobs.list_jobs(active_only=True), [])

    def test_applied_overrides_in_state_and_result(self):
        self.codex(GOOD)
        st = jobs.submit('do it', report='report.md', sandbox='workspace-write', model='gpt-6-astra',
                         config=['model_context_window=100'])
        self.assertEqual(st['applied_overrides'], [])  # caller key wins
        st = jobs.submit('do it', report='report.md', sandbox='workspace-write', model='gpt-6-astra')
        final = jobs.wait(st['job_id'], 30, poll=0.05)
        self.assertEqual(final['applied_overrides'], ['model_context_window=872000'])
        res, _ = jobs.result(st['job_id'])
        self.assertEqual(res['applied_overrides'], ['model_context_window=872000'])
        code, out, _ = self.cli('result', st['job_id'])
        self.assertEqual(json.loads(out)['applied_overrides'], ['model_context_window=872000'])

    def test_incomplete_status(self):
        self.codex('exit 1\n')
        st = jobs.submit('do it', report='report.md', sandbox='read-only', max_resumes=0)
        self.assertEqual(jobs.wait(st['job_id'], 30, poll=0.05)['status'], 'incomplete')

    def test_wait_timeout_returns_active_state(self):
        st = jobs.submit('x', report='r.md', sandbox='read-only', start_worker=False)
        self.assertEqual(jobs.wait(st['job_id'], 0.2, poll=0.05)['status'], 'queued')

    def test_dead_worker_is_failed(self):
        p = subprocess.Popen(['true'])
        p.wait()
        st = jobs.submit('x', report='r.md', sandbox='read-only', start_worker=False)
        jobs.update_state(st['job_id'], lambda s: dict(s, status='running', pid=p.pid))
        got = jobs.read_state(st['job_id'])
        self.assertEqual(got['status'], 'failed')
        res, _ = jobs.result(st['job_id'])
        self.assertFalse(res['ok'])

    def test_cancel_kills_group_and_sticks(self):
        st = jobs.submit('x', report='r.md', sandbox='read-only', start_worker=False)
        proc = subprocess.Popen(['sleep', '60'], start_new_session=True)
        self.addCleanup(lambda: (proc.poll() is None) and proc.kill())
        jobs.update_state(st['job_id'], lambda s: dict(s, status='running', pid=proc.pid, pgid=proc.pid, codex_pgid=proc.pid))
        out = jobs.cancel(st['job_id'])
        self.assertEqual(out['status'], 'cancelled')
        self.assertIsNotNone(proc.wait(timeout=10))
        # a late worker finish must not overwrite the cancel
        self.assertEqual(jobs.read_state(st['job_id'])['status'], 'cancelled')
        self.assertEqual(jobs.cancel(st['job_id'])['status'], 'cancelled')

    def test_unsafe_requests_create_no_job(self):
        for kw in ({'sandbox': 'danger-full-access'}, {'sandbox': 'read-only', 'cwd': '/etc'},
                   {'sandbox': 'read-only', 'add_dirs': ['/etc']}):
            with self.assertRaises(ValueError):
                jobs.submit('x', report='r.md', **kw)
        self.assertEqual(jobs.list_jobs(), [])


class CliTests(Base):
    def test_run_stdin_contract(self):
        self.codex(GOOD)
        w = self.dir / 'w'
        code, out, _ = self.cli('run', '--cwd', str(w), '--task-file', '-', '--report', 'report.md',
                                '--sandbox', 'workspace-write', stdin='task from stdin')
        data = json.loads(out)
        self.assertEqual((code, data['schema_version'], data['ok']), (0, 1, True))
        self.assertEqual((w / 'TASK.md').read_text(), 'task from stdin')

    def test_run_incomplete_exits_1(self):
        self.codex('exit 1\n')
        code, out, _ = self.cli('run', '--cwd', str(self.dir / 'w'), '--task-file', '-', '--report', 'r.md',
                                '--sandbox', 'read-only', '--max-resumes', '0', stdin='t')
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)['ok'])

    def test_setup_and_safety_errors_exit_2(self):
        for argv in (['--cwd', str(self.dir), '--task-file', str(self.dir / 'none'), '--report', 'r', '--sandbox', 'read-only'],
                     ['--cwd', str(self.dir), '--task-file', '-', '--report', 'r', '--sandbox', 'danger-full-access'],
                     ['--cwd', '/etc', '--task-file', '-', '--report', 'r', '--sandbox', 'read-only'],
                     ['--cwd', str(self.dir), '--add-dir', '/etc', '--task-file', '-', '--report', 'r', '--sandbox', 'read-only']):
            for sub in ('run', 'submit'):
                code, _, err = self.cli(sub, *argv, stdin='t')
                self.assertEqual(code, 2, (sub, argv, err))
                self.assertIn('setup error', err)

    def test_submit_status_wait_result_list_cancel(self):
        self.codex(GOOD)
        code, out, _ = self.cli('submit', '--task-file', '-', '--report', 'report.md', '--sandbox', 'workspace-write',
                                '--label', 'lbl', stdin='t')
        sub = json.loads(out)
        self.assertEqual(code, 0)
        self.assertTrue(Path(sub['state_path']).is_file())
        code, out, _ = self.cli('wait', sub['job_id'], '--timeout-sec', '30')
        self.assertEqual((code, json.loads(out)['status']), (0, 'complete'))
        self.assertEqual(json.loads(self.cli('status', sub['job_id'])[1])['label'], 'lbl')
        code, out, _ = self.cli('result', sub['job_id'])
        self.assertEqual((code, json.loads(out)['ok']), (0, True))
        self.assertEqual(len(json.loads(self.cli('list')[1])['jobs']), 1)
        self.assertEqual(json.loads(self.cli('list', '--active')[1])['jobs'], [])
        self.assertEqual(self.cli('status', 'nope')[0], 2)

    def test_wait_exit_codes(self):
        st = jobs.submit('x', report='r.md', sandbox='read-only', start_worker=False)
        code, _, _ = self.cli('wait', st['job_id'], '--timeout-sec', '1')
        self.assertEqual(code, 3)
        jobs.update_state(st['job_id'], lambda s: dict(s, status='incomplete'))
        self.assertEqual(self.cli('wait', st['job_id'])[0], 1)
        self.assertEqual(self.cli('result', st['job_id'])[0], 1)


class McpTests(Base):
    def session(self, *msgs):
        out = io.StringIO()
        mcp_server.serve(io.StringIO('\n'.join(json.dumps(m) for m in msgs) + '\n'), out)
        return [json.loads(x) for x in out.getvalue().splitlines()]

    def call(self, name, args, mid=2):
        r = self.session({'jsonrpc': '2.0', 'id': mid, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}})[0]
        return r['result']['isError'], json.loads(r['result']['content'][0]['text'].removeprefix('error: ')) \
            if not r['result']['isError'] else r['result']['content'][0]['text']

    def test_protocol(self):
        rs = self.session({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}},
                          {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                          {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'},
                          {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'},
                          {'jsonrpc': '2.0', 'id': 4, 'method': 'nope'})
        self.assertEqual([r['id'] for r in rs], [1, 2, 3, 4])  # the notification got no reply
        self.assertEqual(rs[0]['result']['protocolVersion'], '2025-03-26')
        names = {t['name'] for t in rs[2]['result']['tools']}
        self.assertEqual(names, {'codex_task_submit', 'codex_task_status', 'codex_task_wait', 'codex_task_result',
                                 'codex_task_cancel', 'codex_task_list'})
        for t in rs[2]['result']['tools']:
            self.assertEqual(t['inputSchema']['type'], 'object')
        self.assertEqual(rs[3]['error']['code'], -32601)

    def test_tools_flow(self):
        self.codex(GOOD)
        err, sub = self.call('codex_task_submit', {'task': 't', 'report': 'report.md', 'sandbox': 'workspace-write'})
        self.assertFalse(err)
        err, st = self.call('codex_task_wait', {'job_id': sub['job_id'], 'timeout_sec': 30})
        self.assertEqual(st['status'], 'complete')
        self.assertEqual(self.call('codex_task_status', {'job_id': sub['job_id']})[1]['status'], 'complete')
        self.assertTrue(self.call('codex_task_result', {'job_id': sub['job_id']})[1]['ok'])
        self.assertEqual(len(self.call('codex_task_list', {})[1]['jobs']), 1)
        self.assertEqual(self.call('codex_task_cancel', {'job_id': sub['job_id']})[1]['status'], 'complete')

    def test_errors(self):
        for name, args in (('codex_task_submit', {'task': 't', 'report': 'r', 'sandbox': 'danger-full-access'}),
                           ('codex_task_submit', {'task': 't', 'report': 'r', 'sandbox': 'read-only', 'cwd': '/etc'}),
                           ('codex_task_submit', {'task': 't', 'report': 'r', 'sandbox': 'read-only', 'add_dirs': ['/etc']}),
                           ('codex_task_submit', {'task': 't'}),
                           ('codex_task_status', {'job_id': 'nope'}), ('codex_task_status', {})):
            err, text = self.call(name, args)
            self.assertTrue(err, (name, args))
        r = self.session({'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call', 'params': {'name': 'zzz'}})[0]
        self.assertEqual(r['error']['code'], -32602)

    def test_wait_is_capped(self):
        with mock.patch.object(jobs, 'wait', lambda j, t: {'t': t}) as _:
            self.assertEqual(mcp_server.call_tool('codex_task_wait', {'job_id': 'x', 'timeout_sec': 9999}), {'t': 600})


class ConfigRefusalTests(Base):
    def test_cli_exits_2_with_instructions(self):
        self.cfg_path.unlink()
        for argv in (['list'], ['submit', '--task-file', '-', '--report', 'r', '--sandbox', 'read-only']):
            code, out, err = self.cli(*argv, stdin='t')
            self.assertEqual(code, 2, argv)
            self.assertIn(str(self.cfg_path), err)
            self.assertIn('allowed_roots', err)
            self.assertEqual(out, '')

    def test_cli_exits_2_for_empty_roots_and_missing_codex(self):
        self.write_config(str(self.dir / 'codex'), allowed_roots=[])
        self.assertEqual(self.cli('list')[0], 2)
        self.write_config(str(self.dir / 'no-such-codex'))
        self.assertEqual(self.cli('list')[0], 2)

    def test_mcp_tool_calls_return_error_text(self):
        self.cfg_path.unlink()
        out = io.StringIO()
        msgs = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                 'params': {'name': 'codex_task_list', 'arguments': {}}}]
        mcp_server.serve(io.StringIO('\n'.join(json.dumps(m) for m in msgs) + '\n'), out)
        r = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertNotIn('error', r[0])  # the server itself starts
        self.assertTrue(r[1]['result']['isError'])
        self.assertTrue(r[1]['result']['content'][0]['text'].startswith('error: '))
        self.assertIn('allowed_roots', r[1]['result']['content'][0]['text'])


if __name__ == '__main__':
    unittest.main()
