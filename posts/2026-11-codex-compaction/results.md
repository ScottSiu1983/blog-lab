# Results

Raw numbers come from the `turn.completed` usage of each run's `--json` event stream: `input_tokens` and `cached_input_tokens`. Uncached input is the difference of the two. All runs used reasoning effort `low` and the same task (read four source files in full, write `report.md`, finish with the codeword `ORCHID-7731`).

Models: `gpt-6.1-sol` is the Codex default model at the time; the Sol runs were started without `-m`, and the session logs record `gpt-6.1-sol`. `gpt-6-astra` was passed with `-m gpt-6-astra`.

| group | Codex version | model | setup | compactions | backend errors | input tokens | uncached input | outcome |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | 0.162.1 | gpt-6.1-sol | threshold 40000; summary prompt replaced by "reply with one word" | 6 | 0 | 563,260 | 102,332 | complete |
| B | 0.162.1 | gpt-6.1-sol | as A, plus a thread goal | 4 | 0 | 603,636 | 83,444 | complete |
| D | 0.162.1 | gpt-6.1-sol | as A, plus a post-compaction re-injection hook in the working directory (never trusted, so it did not run) | 3 | 0 | 291,575 | 76,151 | complete |
| A2 | 0.162.1 | gpt-6-astra | as A | 1 | 3 | 226,655 | 51,423 | task lost |
| A3 | 0.162.1 | gpt-6-astra | compaction parameters all default, only the threshold lowered to 40000 | 1 | 3 | 226,799 | 51,439 | task lost |
| B2 | 0.162.1 | gpt-6-astra | as A, plus a thread goal | 1 | 4 | 251,713 | 51,777 | task lost |
| C2 | 0.162.1 | gpt-6-astra | as A, task also written to `AGENTS.md` and `TASK.md` in the working directory | 6 | 18 | 1,725,796 | 188,388 | complete |
| S1 | 0.167.0 | gpt-6-astra | `model_context_window=872000` (about 828,400 usable), no lowered threshold | 0 | n/a | 1,172,441 | 105,305 | complete |
| S3-1..4 | 0.167.0 | gpt-6-astra | default window, task split into four runs (one file each) | 0 each | n/a | 1,245,508 (sum of the four) | 233,668 (sum of the four) | all four correct |
| S2 | 0.167.0 | gpt-6-astra | threshold 40000 plus a progress checklist | 60 on the fourth file, stopped by hand | n/a | ~9.56M input tokens (from the run's own accounting before it was stopped) | n/a | compaction loop, stopped |
| A3n | 0.167.0 | gpt-6-astra | as A3, retest | 7 (first run plus two resumes) | n/a | 265,668 (first run only) | 53,444 | task lost again; finished only after two manual resumes |
| C2n | 0.167.0 | gpt-6-astra | as C2, retest | 5 | 2 | 1,709,668 | 153,060 | complete |

Notes:

- "Task lost" means that after compaction the model answered with a generic acknowledgement of its global rules and ended the run without the report, because the task message was no longer in the history it could see (see `tools/codex-compaction-check`, field `task_in_retained_context_only`).
- Compaction and error counts for the 0.162.1 groups are as recorded at the time of each run. A session log of a group that was resumed by hand later can show a higher total. Token usage is from the first run only; for A3n the two manual resumes are not included.
- n/a: not recorded for that group.
- The original experiment read four source files of about 55,000 tokens in total from another code base. The synthetic samples in this directory (`make_samples.py`) only reproduce the mechanism; the numbers will differ if you run them.
- Every request carries a fixed overhead of about 25,000 tokens (instructions and tool definitions). With a 40,000-token threshold, most of the window is that overhead, so the model compacts again and again and re-reads files it had already read. A low threshold therefore multiplies the input cost.
