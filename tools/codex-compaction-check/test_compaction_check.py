"""Tests for compaction_check using synthetic rollouts."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compaction_check as cc  # noqa: E402

TASK = 'Read the four files and write report.md.   Last line must be the codeword.\nSecond line'
TASK_FLAT = 'Read the four files and write report.md. Last line must be the codeword. Second line'


def msg(role, text):
    return {'type': 'message', 'role': role, 'content': [{'type': 'input_text', 'text': text}]}


def compacted(history, retained=()):
    payload = {'replacement_history': history,
               'retained_context': {'user_messages': [{'order': i, 'text': t} for i, t in enumerate(retained)]}}
    return {'type': 'compacted', 'payload': payload}


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name).resolve()

    def rollout(self, events, name='rollout-1.jsonl'):
        p = self.dir / name
        p.write_text('\n'.join(json.dumps(e) for e in events) + '\n', encoding='utf-8')
        return p

    def test_no_compaction(self):
        r = cc.check(self.rollout([{'type': 'event_msg'}]), TASK)
        self.assertEqual((r['compactions'], r['task_retained'], r['task_in_retained_context_only']), (0, None, None))

    def test_task_kept_in_history(self):
        p = self.rollout([compacted([msg('developer', 'rules'), msg('user', TASK_FLAT)], [TASK_FLAT])])
        r = cc.check(p, TASK)
        self.assertEqual((r['compactions'], r['task_retained'], r['task_in_retained_context_only']), (1, True, False))

    def test_task_only_in_retained_context(self):
        p = self.rollout([compacted([msg('developer', 'rules')], [TASK_FLAT])])
        r = cc.check(p, TASK)
        self.assertEqual((r['task_retained'], r['task_in_retained_context_only']), (False, True))

    def test_task_lost_everywhere(self):
        p = self.rollout([compacted([msg('user', 'something else')], ['other'])])
        r = cc.check(p, TASK)
        self.assertEqual((r['task_retained'], r['task_in_retained_context_only']), (False, False))

    def test_only_last_compaction_counts(self):
        p = self.rollout([compacted([msg('user', TASK_FLAT)]), compacted([msg('developer', 'rules')])])
        r = cc.check(p, TASK)
        self.assertEqual((r['compactions'], r['task_retained']), (2, False))

    def test_without_task_only_counts(self):
        r = cc.check(self.rollout([compacted([])]))
        self.assertEqual((r['compactions'], r['task_retained'], r['task_in_retained_context_only']), (1, None, None))

    def test_latest_by_cwd(self):
        work = self.dir / 'work'
        work.mkdir()
        sessions = self.dir / 'sessions' / '2026' / '11' / '07'
        sessions.mkdir(parents=True)
        meta = {'type': 'session_meta', 'payload': {'cwd': str(work)}}
        other = {'type': 'session_meta', 'payload': {'cwd': str(self.dir / 'elsewhere')}}
        (sessions / 'rollout-a.jsonl').write_text(json.dumps(meta) + '\n', encoding='utf-8')
        mine = sessions / 'rollout-b.jsonl'
        mine.write_text(json.dumps(meta) + '\n' + json.dumps(compacted([])) + '\n', encoding='utf-8')
        (sessions / 'rollout-c.jsonl').write_text(json.dumps(other) + '\n', encoding='utf-8')
        import os
        os.utime(sessions / 'rollout-a.jsonl', (1, 1))
        found = cc.find_latest(work, self.dir / 'sessions')
        self.assertEqual(found, mine)
        self.assertIsNone(cc.find_latest(self.dir / 'nope', self.dir / 'sessions'))

    def test_cli_output(self):
        task_file = self.dir / 'task.txt'
        task_file.write_text(TASK, encoding='utf-8')
        p = self.rollout([compacted([msg('user', TASK_FLAT)])])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cc.main([str(p), '--task-file', str(task_file)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out.getvalue())['task_retained'])


if __name__ == '__main__':
    unittest.main()
