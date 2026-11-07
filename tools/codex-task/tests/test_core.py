"""Tests for codex_task.core: run_codex_task, compaction_report and the AGENTS.md section (fake codex, no real model)."""
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codex_task import config, core  # noqa: E402


def fake_codex(dirpath, body):
    p = Path(dirpath) / 'codex'
    p.write_text('#!/bin/sh\n' + body, encoding='utf-8')
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def write_rollout(root, thread, events):
    d = Path(root) / '2026/11/07'
    d.mkdir(parents=True, exist_ok=True)
    p = d / f'rollout-2026-11-07T00-00-00-{thread}.jsonl'
    p.write_text('\n'.join(json.dumps(e) for e in events) + '\n', encoding='utf-8')
    return p


def compacted(*messages):
    hist = [{'type': 'message', 'role': r, 'content': [{'type': 'input_text', 'text': t}]} for r, t in messages]
    return {'type': 'compacted', 'payload': {'replacement_history': hist}}


class Base(unittest.TestCase):
    """Each test gets a temporary directory, a temporary config file (via CODEX_TASK_CONFIG) and a fake HOME."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)
        self.home = self.dir / 'home'
        self.cfg_path = self.dir / 'config.json'
        for p in (mock.patch.object(core, 'env', lambda: dict(os.environ)),
                  mock.patch.dict(os.environ, {'CODEX_TASK_CONFIG': str(self.cfg_path),
                                               'CODEX_TASK_JOBS': str(self.dir / 'jobs')}),
                  mock.patch.object(Path, 'home', lambda: self.home)):
            p.start()
            self.addCleanup(p.stop)
        self.sessions = self.home / '.codex/sessions'
        self.write_config(fake_codex(self.dir, 'exit 0\n'))

    def write_config(self, codex_bin, **extra):
        cfg = {'allowed_roots': [str(self.dir)], 'codex_bin': codex_bin}
        cfg.update(extra)
        self.cfg_path.write_text(json.dumps(cfg), encoding='utf-8')

    def set_codex(self, body):
        """Install a fake codex with the given shell body and point the config at it."""
        self.write_config(fake_codex(self.dir, body))


class CompactionReportTests(Base):
    TASK = 'Find the bug in f1.cpp and write report.md.   Keep going.\nSecond line'

    def test_retained(self):
        write_rollout(self.sessions, 'T1', [{'type': 'x'}, compacted(('developer', 'rules'), ('user', 'Find the bug in f1.cpp and write report.md. Keep going. Second line'))])
        r = core.compaction_report('T1', self.TASK)
        self.assertEqual((r['compactions'], r['task_retained']), (1, True))

    def test_lost(self):
        write_rollout(self.sessions, 'T2', [compacted(('developer', 'rules'), ('user', 'something else'))])
        r = core.compaction_report('T2', self.TASK)
        self.assertEqual((r['compactions'], r['task_retained']), (1, False))

    def test_none_and_missing(self):
        write_rollout(self.sessions, 'T3', [{'type': 'event_msg'}])
        r = core.compaction_report('T3', self.TASK)
        self.assertEqual((r['compactions'], r['task_retained']), (0, None))
        r = core.compaction_report('nope', self.TASK)
        self.assertEqual((r['rollout'], r['compactions'], r['task_retained']), (None, 0, None))


class AgentsSectionTests(Base):
    def test_created_appended_and_idempotent(self):
        d = self.dir / 'w1'
        core.ensure_task_files(d, 'task', 'report.md')
        text = (d / 'AGENTS.md').read_text()
        self.assertEqual(text.count(core.AGENTS_MARKER), 1)
        self.assertIn('report.md', text)
        core.ensure_task_files(d, 'task2', 'report.md')
        self.assertEqual((d / 'AGENTS.md').read_text().count(core.AGENTS_MARKER), 1)
        self.assertEqual((d / 'TASK.md').read_text(), 'task2')
        d2 = self.dir / 'w2'
        d2.mkdir()
        (d2 / 'AGENTS.md').write_text('# mine\nkeep')
        core.ensure_task_files(d2, 't', 'r.md')
        core.ensure_task_files(d2, 't', 'r.md')
        t = (d2 / 'AGENTS.md').read_text()
        self.assertTrue(t.startswith('# mine\nkeep'))
        self.assertEqual(t.count(core.AGENTS_MARKER), 1)


class AgentsSectionUpgradeTests(Base):
    # A section written before the end marker and the progress.md rule existed.
    LEGACY = (core.AGENTS_MARKER + '\n## Current task (added automatically)\n\n'
              '- The task text is in `TASK.md`. Read `TASK.md` (and `findings.md` if present) before acting.\n'
              '- Append findings to `findings.md` as you go.\n- The task is not complete until `report.md` is written.\n')

    def test_new_section_mentions_progress(self):
        d = self.dir / 'n'
        core.ensure_task_files(d, 't', 'report.md')
        t = (d / 'AGENTS.md').read_text()
        self.assertIn('progress.md', t)
        self.assertIn('do not redo', t)

    def test_legacy_section_replaced_in_place(self):
        d = self.dir / 'u'
        d.mkdir()
        (d / 'AGENTS.md').write_text('# mine\nkeep\n\n' + self.LEGACY)
        core.ensure_task_files(d, 't', 'report.md')
        t = (d / 'AGENTS.md').read_text()
        self.assertTrue(t.startswith('# mine\nkeep\n\n'))
        self.assertEqual(t.count(core.AGENTS_MARKER), 1)
        self.assertIn('progress.md', t)
        self.assertNotIn('Read `TASK.md` (and `findings.md` if present) before acting', t)
        before = t
        core.ensure_task_files(d, 't', 'report.md')
        self.assertEqual((d / 'AGENTS.md').read_text(), before)

    def test_legacy_section_in_the_middle_keeps_following_content(self):
        d = self.dir / 'm'
        d.mkdir()
        (d / 'AGENTS.md').write_text('# top\n\n' + self.LEGACY + '\n# after\nstays\n')
        core.ensure_task_files(d, 't', 'report.md')
        t = (d / 'AGENTS.md').read_text()
        self.assertEqual(t.count(core.AGENTS_MARKER), 1)
        self.assertIn('progress.md', t)
        self.assertTrue(t.endswith('# after\nstays\n'))
        self.assertTrue(t.startswith('# top\n\n'))

    def test_current_section_with_changed_report_name_updates(self):
        d = self.dir / 'r'
        core.ensure_task_files(d, 't', 'a.md')
        core.ensure_task_files(d, 't', 'b.md')
        t = (d / 'AGENTS.md').read_text()
        self.assertEqual(t.count(core.AGENTS_MARKER), 1)
        self.assertIn('b.md', t)
        self.assertNotIn('a.md', t)


class ModelOverrideTests(Base):
    def test_table_lookup_and_caller_wins(self):
        with mock.patch.dict(core.MODEL_OVERRIDES, {'m1': ['model_context_window=1', 'x=2']}):
            self.assertEqual(core.model_overrides('m1'), ['model_context_window=1', 'x=2'])
            self.assertEqual(core.model_overrides('m1', ['x=9']), ['model_context_window=1'])
            self.assertEqual(core.model_overrides('other'), [])
            self.assertEqual(core.model_overrides(None), [])
        self.assertEqual(core.MODEL_OVERRIDES['gpt-6-astra'], ['model_context_window=872000'])

    def run_with(self, model, config):
        body = ('echo "CALL $@" >> "$ARGLOG"\necho \'{"type":"thread.started","thread_id":"TH"}\'\n'
                'if [ "$2" = resume ]; then echo done > report.md; exit 0; fi\nexit 1\n')
        w = self.dir / 'work'
        argl = self.dir / 'args.txt'
        self.set_codex(body)
        with mock.patch.dict(os.environ, {'ARGLOG': str(argl)}), \
                mock.patch.dict(core.MODEL_OVERRIDES, {'m1': ['model_context_window=1', 'x=2']}):
            res = core.run_codex_task('t', w, w / 'l.log', sandbox='read-only', report='report.md', model=model,
                                      extra_config=config)
        return res, ['CALL ' + c for c in argl.read_text().split('CALL ')[1:]]

    def test_applied_to_first_run_and_resume_before_caller_config(self):
        res, calls = self.run_with('m1', ['x=9'])
        self.assertEqual(res['applied_overrides'], ['model_context_window=1'])
        self.assertEqual(len(calls), 2)
        for c in calls:
            self.assertIn('-c model_context_window=1', c)
            self.assertNotIn('x=2', c)
            self.assertLess(c.index('model_context_window=1'), c.index('x=9'))

    def test_no_match_no_overrides(self):
        res, calls = self.run_with('zzz', [])
        self.assertEqual(res['applied_overrides'], [])
        self.assertNotIn('model_context_window', calls[0])


class CompactionLoopGuardTests(Base):
    def run_fake(self, body, **kw):
        w = self.dir / 'work'
        env = {'SESSIONS': str(self.sessions), 'ARGLOG': str(self.dir / 'args.txt')}
        self.set_codex(body)
        with mock.patch.dict(os.environ, env):
            return core.run_codex_task('do it', w, w / 'run.log', sandbox='workspace-write', report='report.md',
                                       guard_interval=0.3, timeout=60, **kw), w

    HEAD = ('echo "CALL $@" >> "$ARGLOG"\n'
            'D="$SESSIONS/2026/11/07"; mkdir -p "$D"; F="$D/rollout-2026-11-07T00-00-00-TH.jsonl"\n'
            'THREAD\n'
            'echo "{\\"type\\":\\"session_meta\\",\\"payload\\":{\\"cwd\\":\\"$(pwd -P)\\"}}" > "$F"\n'
            'comp() { echo \'{"type":"compacted","payload":{"replacement_history":[]}}\' >> "$F"; }\n')

    def test_loop_without_progress_is_stopped(self):
        body = self.HEAD.replace('THREAD', 'echo \'{"type":"thread.started","thread_id":"TH"}\'') + \
            'for i in 1 2 3 4 5 6; do comp; sleep 1; done\nsleep 30\n'
        res, w = self.run_fake(body)
        self.assertFalse(res['ok'])
        self.assertTrue(res['reason'].startswith('compaction loop:'), res['reason'])
        self.assertEqual(len(res['attempts']), 1)  # no resume
        self.assertEqual(len(self.argl_calls()), 1)
        self.assertIn(res['attempts'][0]['compactions'], (3, 4, 5, 6))
        self.assertTrue(res['attempts'][0]['reason'].startswith('compaction loop:'))

    def argl_calls(self):
        return (self.dir / 'args.txt').read_text().split('CALL ')[1:]

    def test_loop_found_by_cwd_when_stdout_has_no_thread(self):
        body = self.HEAD.replace('THREAD', ':') + 'for i in 1 2 3 4 5 6; do comp; sleep 1; done\nsleep 30\n'
        res, w = self.run_fake(body)
        self.assertFalse(res['ok'])
        self.assertTrue(res['reason'].startswith('compaction loop:'), res['reason'])
        self.assertEqual(len(self.argl_calls()), 1)

    def test_progress_between_compactions_is_not_stopped(self):
        body = self.HEAD.replace('THREAD', 'echo \'{"type":"thread.started","thread_id":"TH"}\'') + \
            'for i in 1 2 3 4 5; do comp; echo "item $i" >> progress.md; sleep 1; done\necho done > report.md\nexit 0\n'
        res, w = self.run_fake(body)
        self.assertTrue(res['ok'], res['reason'])
        self.assertEqual(res['attempts'][0]['compactions'], 5)

    def test_unrelated_cwd_rollout_ignored(self):
        body = self.HEAD.replace('THREAD', ':').replace('$(pwd -P)', '/somewhere/else') + \
            'for i in 1 2 3 4; do comp; sleep 1; done\necho done > report.md\nexit 0\n'
        res, w = self.run_fake(body)
        self.assertTrue(res['ok'], res['reason'])

    def test_fingerprint_ignores_task_files(self):
        w = self.dir / 'fp'
        w.mkdir()
        since = time.time() - 1
        a = core.progress_fingerprint(w, since)
        (w / 'TASK.md').write_text('x')
        (w / 'AGENTS.md').write_text('x')
        (w / '.codex-last-message.txt').write_text('x')
        self.assertEqual(core.progress_fingerprint(w, since), a)
        (w / 'progress.md').write_text('x')
        self.assertNotEqual(core.progress_fingerprint(w, since), a)

    def test_threshold_constant(self):
        self.assertEqual(core.COMPACTION_LOOP_THRESHOLD, 3)


class RunTaskTests(Base):
    FIRST_FAILS = ('echo "CALL $@" >> "$ARGLOG"\n'
                   'echo \'{"type":"thread.started","thread_id":"TH"}\'\n'
                   'if [ "$2" = resume ]; then echo done > report.md; echo \'{"ok":1}\' > .codex-last-message.txt; exit 0; fi\n'
                   'exit 1\n')

    def calls(self):
        return ['CALL ' + c for c in self.argl.read_text().split('CALL ')[1:]]

    def run_task(self, body, **kw):
        w = self.dir / 'work'
        self.argl = self.dir / 'args.txt'
        self.set_codex(body)
        with mock.patch.dict(os.environ, {'ARGLOG': str(self.argl)}):
            return core.run_codex_task('do it', w, w / 'run.log', sandbox='workspace-write', report='report.md',
                                           add_dirs=[str(self.dir / 'y')], network=True, model='m', effort='low', **kw), w

    def test_resume_completes_and_command_line(self):
        res, w = self.run_task(self.FIRST_FAILS)
        self.assertTrue(res['ok'])
        self.assertEqual(len(res['attempts']), 2)
        self.assertIsNotNone(res['attempts'][0]['reason'])
        self.assertIsNone(res['attempts'][1]['reason'])
        self.assertTrue((w / 'run.resume1.log').exists())
        lines = self.calls()
        first = lines[0].split()
        resume = lines[1]
        self.assertIn('-C', first)
        self.assertIn('exec resume TH', resume)
        self.assertIn('sandbox_mode="workspace-write"', resume)
        self.assertIn('sandbox_workspace_write.writable_roots=["' + str(self.dir / 'y') + '"]', resume)
        self.assertIn('sandbox_workspace_write.network_access=true', resume)
        for bad in (' -s ', ' -C ', '--add-dir'):
            self.assertNotIn(bad, resume)
        self.assertIn('The previous run did not finish', resume)

    def test_max_resumes_respected(self):
        res, _ = self.run_task('echo "CALL $@" >> "$ARGLOG"\necho \'{"type":"thread.started","thread_id":"TH"}\'\nexit 1\n', max_resumes=2)
        self.assertFalse(res['ok'])
        self.assertEqual(len(res['attempts']), 3)
        self.assertEqual(len(self.calls()), 3)
        self.argl.unlink()
        res, _ = self.run_task('echo "CALL $@" >> "$ARGLOG"\necho \'{"type":"thread.started","thread_id":"TH"}\'\nexit 1\n', max_resumes=0)
        self.assertEqual(len(res['attempts']), 1)

    def test_no_thread_means_no_resume(self):
        res, _ = self.run_task('echo "CALL $@" >> "$ARGLOG"\nexit 1\n')
        self.assertFalse(res['ok'])
        self.assertEqual(len(res['attempts']), 1)

    def test_safety_rejections(self):
        w = self.dir / 'work'
        for kw in ({'sandbox': 'danger-full-access'}, {'sandbox': 'workspace-write', 'add_dirs': ['/etc']},
                   {'sandbox': 'workspace-write', 'extra_config': ['sandbox_mode="danger-full-access"']},
                   {'sandbox': 'workspace-write', 'extra_config': ['approval_policy="never"']}):
            with self.assertRaises(ValueError):
                core.run_codex_task('t', w, w / 'l.log', report='r.md', **kw)
        with self.assertRaises(ValueError):
            core.run_codex_task('t', '/tmp/outside', '/tmp/outside/l.log', sandbox='read-only', report='r.md')
        link = self.dir / 'link'
        link.symlink_to('/etc')
        with self.assertRaises(ValueError):  # symlink escaping the root is resolved
            core.validate_request(link, (), 'read-only')
        core.validate_request(self.dir / 'ok', [self.dir], 'read-only')

    def test_check_and_schema(self):
        w = self.dir / 'work'
        w.mkdir()
        schema = w / 's.json'
        schema.write_text(json.dumps({'required': ['a', 'b']}))
        (w / 'report.md').write_text('x')
        (w / '.codex-last-message.txt').write_text('{"a":1}')
        self.assertIn('lacks required keys: b', core._incomplete_reason(w, 'report.md', schema, w / '.codex-last-message.txt', None))
        (w / '.codex-last-message.txt').write_text('{"a":1,"b":2}')
        self.assertIsNone(core._incomplete_reason(w, 'report.md', schema, w / '.codex-last-message.txt', None))
        self.assertEqual(core._incomplete_reason(w, 'report.md', None, None, lambda d: 'bad'), 'bad')


class ConfigTests(Base):
    def test_missing_file_refused_with_instructions(self):
        self.cfg_path.unlink()
        with self.assertRaises(config.ConfigError) as cm:
            config.load()
        msg = str(cm.exception)
        self.assertIn(str(self.cfg_path), msg)
        self.assertIn('allowed_roots', msg)

    def test_default_path_when_env_unset(self):
        env = {k: v for k, v in os.environ.items() if k != 'CODEX_TASK_CONFIG'}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config.config_path(), self.home / '.config/codex-task/config.json')
            with self.assertRaises(config.ConfigError):
                config.load()

    def test_missing_or_empty_allowed_roots_refused(self):
        for cfg in ({}, {'allowed_roots': []}, {'allowed_roots': 'x'}, {'allowed_roots': ['relative/dir']}):
            self.cfg_path.write_text(json.dumps(dict(cfg, codex_bin=fake_codex(self.dir, 'exit 0\n'))))
            with self.assertRaises(config.ConfigError, msg=cfg):
                config.load()

    def test_invalid_json_refused(self):
        self.cfg_path.write_text('{nope')
        with self.assertRaises(config.ConfigError):
            config.load()

    def test_codex_bin_not_found_refused(self):
        self.cfg_path.write_text(json.dumps({'allowed_roots': [str(self.dir)], 'codex_bin': str(self.dir / 'missing')}))
        with self.assertRaises(config.ConfigError):
            config.load()
        self.cfg_path.write_text(json.dumps({'allowed_roots': [str(self.dir)]}))
        with mock.patch.object(config.shutil, 'which', lambda name: None):
            with self.assertRaises(config.ConfigError):
                config.load()

    def test_codex_bin_defaults_to_path_lookup(self):
        self.cfg_path.write_text(json.dumps({'allowed_roots': [str(self.dir)]}))
        with mock.patch.object(config.shutil, 'which', lambda name: '/opt/bin/codex'):
            self.assertEqual(config.load()['codex_bin'], '/opt/bin/codex')

    def test_jobs_dir_default_config_and_env(self):
        self.write_config(str(self.dir / 'codex'), jobs_dir=str(self.dir / 'from-config'))
        env = {k: v for k, v in os.environ.items() if k != 'CODEX_TASK_JOBS'}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config.load()['jobs_dir'], self.dir / 'from-config')
            self.write_config(str(self.dir / 'codex'))
            self.assertEqual(config.load()['jobs_dir'], self.home / '.local/state/codex-task/jobs')
        self.assertEqual(config.load()['jobs_dir'], self.dir / 'jobs')  # CODEX_TASK_JOBS wins

    def test_run_refused_without_config(self):
        self.cfg_path.unlink()
        w = self.dir / 'work'
        with self.assertRaises(ValueError):
            core.run_codex_task('t', w, w / 'l.log', sandbox='read-only', report='r.md')

    def test_configured_roots_take_effect(self):
        other = self.dir / 'other-root'
        inside = self.dir / 'inside'
        other.mkdir()
        self.write_config(str(self.dir / 'codex'), allowed_roots=[str(inside), str(other)])
        core.validate_request(other / 'a', [inside], 'read-only')
        with self.assertRaises(ValueError) as cm:
            core.validate_request(self.dir / 'elsewhere', (), 'read-only')
        self.assertIn('configured allowed_roots', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
