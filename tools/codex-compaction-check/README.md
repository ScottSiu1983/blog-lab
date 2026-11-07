# codex-compaction-check

Version v0.1.0. One file, Python 3 standard library only.

Counts context compactions in a Codex session log (a "rollout" `.jsonl` under `~/.codex/sessions`) and tells you whether the task message survived the last one.

```
python3 compaction_check.py PATH/TO/rollout-....jsonl --task-file task.txt
python3 compaction_check.py --latest --cwd /path/to/workdir --task-file task.txt
```

`--latest --cwd DIR` picks the newest rollout whose `session_meta.cwd` equals the real path of `DIR`. Use `--sessions-dir` if your sessions are not in `$CODEX_HOME/sessions` (default `~/.codex/sessions`).

Output:

```json
{
 "rollout": "...",
 "compactions": 1,
 "task_retained": false,
 "task_in_retained_context_only": true
}
```

| Field | Meaning |
| --- | --- |
| `compactions` | Number of entries with `"type": "compacted"`. |
| `task_retained` | After the last compaction, does a user message in the rebuilt history (`replacement_history`) still contain the task text? `null` if there was no compaction or no `--task-file`. |
| `task_in_retained_context_only` | `true` when the task text is only in `retained_context.user_messages` (a side field the model does not see) and not in the history. `null` if there was no compaction or no task. |

The task is matched on its first 200 characters with whitespace normalized. Exit code 2 means the file or session was not found.

Test: `python3 -m unittest test_compaction_check` (synthetic rollouts only).
