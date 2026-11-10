# Codex compaction experiment

Companion code for the blog post [Claude Told Me Astra Blacked Out and Sol Won. 10M Tokens to Fill One Pothole](https://scottsiu.me/writing/codex-compaction-task-loss/) ([Chinese version](https://scottsiu.me/zh/writing/codex-compaction-task-loss/)).

## What it tests

`codex exec` compacts its context when the conversation grows past `model_auto_compact_token_limit`. We saw one model (`gpt-6-astra`) lose the user's task message at that point: the model woke up with only its global rules in view, answered with a generic acknowledgement and ended the run without doing the work. This experiment gives Codex a task that needs far more context than the threshold allows, so compaction is certain, and then checks whether the task still gets done.

The task (`prompt.txt`): read four source files in full with `cat`, one at a time, then write `report.md` with one line per file (name, `wc -l` line count, last function name). The last line of `report.md` must be the codeword `ORCHID-7731`.

## Files

| File | Purpose |
| --- | --- |
| `make_samples.py` | Generates four synthetic C++ files, `f1.cpp` to `f4.cpp`, deterministically: about 55,000 tokens in total (4 characters per token), `f4.cpp` about half of it, each ending with an easy-to-recognize function. No third-party code. |
| `prompt.txt` | The task given to Codex. |
| `run.sh` | Runs one experiment with `codex exec` and checks the outcome. |
| `results.md` | Results table of the original runs. |

## How to run

```
./run.sh MODEL EFFORT OUT_DIR
COMPACT_LIMIT=40000 COMPACT_PROMPT="Reply with exactly the single word: EMPTY" CONTEXT_WINDOW=872000 ./run.sh gpt-6-astra low ./out
```

`COMPACT_LIMIT` (default 40000), `COMPACT_PROMPT` (replaces the summarizing prompt, to cripple the summary on purpose) and `CONTEXT_WINDOW` are optional. `run.sh` creates the samples and the prompt in `OUT_DIR`, runs `codex exec --json` there (workspace-write sandbox) and writes `events.jsonl` (token usage per turn), `last.txt` and `run.log`.

Use a model name your Codex installation knows. Each run consumes a few hundred thousand tokens or more; a looping run can consume millions, so watch the first runs.

## How the outcome is decided

A run is complete when `report.md` exists and its last line is `ORCHID-7731`. `run.sh` prints this verdict. To see what compaction did, inspect the session log with `tools/codex-compaction-check`:

```
python3 ../../tools/codex-compaction-check/compaction_check.py --latest --cwd ./out --task-file ./out/prompt.txt
```

`compactions` is the number of compactions, `task_retained` says whether the task message is still in the rebuilt history, and `task_in_retained_context_only` is `true` when it was moved to a side field the model does not see.

## Results

See [results.md](results.md). The original runs read four real source files of about the same total size, not these synthetic ones, so the numbers will differ when you run this yourself.

## Tool versions

- `tools/codex-task` v0.1.0
- `tools/codex-compaction-check` v0.1.0
- Original runs: Codex 0.162.1 and 0.167.0 (see the table)
