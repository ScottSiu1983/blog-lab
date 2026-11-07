# codex-task

Version v0.1.0.

Run long Codex tasks as background jobs that survive context compaction. Any caller can use it: an agent, a script, a cron job. There are two entrances that share one core: the `codex-task` command line and a stdio MCP server (`codex-task mcp`). Python 3 standard library only; no `mcp` package needed.

## Why this exists

- Some models (we saw it with `gpt-6-astra`) drop the user's task message when they auto-compact the context inside `codex exec`. Afterwards only the global rules are left and the run ends early with a generic acknowledgement.
- Codex cannot start `codex exec` from inside its own sandbox (it cannot write to `~/.codex`). The MCP server runs outside the caller's sandbox, so Codex itself (or any other agent) can submit jobs through MCP.

## Configuration

The tool refuses to run without a configuration file. Path: `$CODEX_TASK_CONFIG`, else `~/.config/codex-task/config.json`.

```json
{
  "allowed_roots": ["~/work"],
  "codex_bin": "/usr/local/bin/codex",
  "jobs_dir": "~/.local/state/codex-task/jobs"
}
```

| Key | Required | Meaning |
| --- | --- | --- |
| `allowed_roots` | yes | Non-empty list of absolute directories (a leading `~` is expanded). `--cwd` and every `--add-dir` must resolve under one of them. |
| `codex_bin` | no | Path of the `codex` executable. Default: `codex` found on `PATH`. |
| `jobs_dir` | no | Where job directories live. Default: `~/.local/state/codex-task/jobs`. The environment variable `CODEX_TASK_JOBS` overrides it (used by the tests). |

If the file is missing, `allowed_roots` is missing or empty, or no `codex` executable can be found, the CLI exits with code 2 and prints on stderr which file to create and an example. An MCP tool call returns `isError: true` with text starting with `error:`.

## Two layers of protection

1. File layer. The working directory gets `TASK.md` (the task text) and a marked section in `AGENTS.md` (added once, never duplicated, existing content untouched). The section tells the agent to read `TASK.md`, `progress.md` and `findings.md` before acting, to append one line to `progress.md` for every finished item, to not redo items already recorded there after compaction or resume, to append findings to `findings.md`, and that the task is not complete until the report file exists. The section has start and end markers; an older version of it is replaced in place. `codex exec resume` re-reads `AGENTS.md`, so these files must live in the working directory.
2. Orchestration layer. After each run the tool checks completion: the report file exists and is not empty; with `--schema`, the last message (the `-o` file) is JSON and has the schema's top-level `required` keys. If the task is incomplete and a thread id is known, it continues with `codex exec resume` (the original task text is sent again), at most `--max-resumes` times (default 2). `resume` accepts no `-s`, `-C` or `--add-dir`, so the sandbox is passed as `-c sandbox_mode=...`, extra writable directories as `-c sandbox_workspace_write.writable_roots=[...]` and network access as `-c sandbox_workspace_write.network_access=true`.

Every attempt also gets a compaction report.

## Compaction report

Each entry of `attempts` carries:

| Field | Meaning |
| --- | --- |
| `compactions` | Number of `"type":"compacted"` entries in the thread's rollout. |
| `task_retained` | After the last compaction, whether a user message in the rebuilt history (`replacement_history`) still contains the first ~200 characters of the task (whitespace normalized). `null` when there was no compaction or no rollout was found. |
| `reason` | Why the attempt did not finish, or `null`. |
| `log` | Path of the attempt's log file. |

The standalone version of this check is in [`../codex-compaction-check`](../codex-compaction-check).

## Model policy and per-model parameters

The caller passes the model and reasoning effort it was asked for (`--model`, `--effort`); with no model, the Codex default applies. This tool never picks a model by task length or difficulty.

`MODEL_OVERRIDES` in `codex_task/core.py` is a small table of extra `-c key=value` parameters. When the caller's `--model` matches a key exactly, the parameters are placed before the caller's own `--config` values, on the first run and on every resume. If the caller sets the same key, the caller wins and the table entry is skipped. The entries actually applied are recorded in `state.json` and in `applied_overrides` of the result.

| Model | Parameter | Why |
| --- | --- | --- |
| `gpt-6-astra` | `model_context_window=872000` | This model drops the task message when it compacts in `exec`. A larger window avoids compaction. Remove the entry when the model is fixed. |

With the larger window the usable size is about 828,400 tokens. A task that needs more than roughly 800K tokens should be split into several jobs and combined by the caller.

## Compaction loop guard

While an attempt runs, every 60 seconds (`GUARD_INTERVAL`) the tool finds the attempt's rollout (by the thread id from the `thread.started` event, or else by working directory and start time: the newest rollout modified since the start whose `session_meta.cwd` equals the working directory's realpath), counts `"type":"compacted"` lines and takes a progress fingerprint of the working directory: mtime and size of `progress.md`, `findings.md` and any other file modified since the start (excluding `TASK.md`, `AGENTS.md`, Codex output files and logs). A fingerprint is recorded for every newly observed compaction. If 3 compactions in a row (`COMPACTION_LOOP_THRESHOLD`) leave the fingerprint unchanged, the process group is killed, no resume is attempted, the job ends `incomplete` and the reason starts with `compaction loop:` followed by the compaction count.

## Command line

Run it as `/path/to/codex-task/codex-task` (symlinks work) or put it on your `PATH`.

| Command | What it does |
| --- | --- |
| `submit` | Create a job, start a background worker, print `job_id` at once. Options: `--cwd` (default `<job dir>/work`), `--task-file` (`-` reads stdin), `--report` (relative to `--cwd`), `--sandbox`, `--schema`, `--model`, `--effort`, `--add-dir` (repeatable), `--network`, `--max-resumes`, `--timeout-min` (per attempt), `--config key=value` (repeatable), `--label`, `--log` |
| `status JOB` | Print the job state |
| `wait JOB [--timeout-sec N]` | Wait for the job to finish |
| `result JOB` | Print the final result |
| `cancel JOB` | Kill the worker and Codex process groups and mark the job `cancelled` |
| `list [--active]` | List jobs, newest first |
| `run` | `submit` plus `wait`, same options as `submit`, prints the final result |
| `mcp` | Start the stdio MCP server |
| `--version` | Print the version |

After `submit` the caller may exit; the job keeps running (the worker has its own session and its output goes to the job directory).

## JSON conventions

- `submit`: `{"schema_version": 1, "job_id", "status", "state_path", "job_dir"}`
- `status`, `wait`: the job state with fields `schema_version, job_id, label, status, created_at, updated_at, pid, pgid, codex_pgid, thread, attempts, reason, cwd, report, model, effort, sandbox, applied_overrides`
- `result`, `run`: `{"schema_version": 1, "job_id", "status", "ok", "thread", "reason", "applied_overrides", "attempts": [{"log", "compactions", "task_retained", "reason"}], "report_path", "last_message_path", "cwd"}`. While the job is still running, `result` prints the state instead.
- `list`: `{"schema_version": 1, "jobs": [{job_id, label, status, created_at, updated_at, thread, cwd}]}`

## Exit codes

- `run`: 0 complete; 1 not complete (incomplete, failed, cancelled); 2 usage, setup, configuration or safety error (message on stderr).
- `wait`, `result`: 0 complete; 1 incomplete, failed or cancelled; 3 still running at the timeout; 2 error (for example unknown job).
- `submit`, `status`, `cancel`, `list`: 0, or 2 on error.

## Job states

`queued` (created, worker not started), `running`, `complete`, `incomplete` (all resumes used and the completion check still fails), `failed` (worker error or the process vanished), `cancelled`. The last four are final. After a cancel, a late worker never overwrites `cancelled`.

## MCP

`codex-task mcp` speaks newline-delimited JSON-RPC 2.0 over stdio and implements `initialize`, `notifications/initialized`, `ping`, `tools/list` and `tools/call`. Register it with your client; this project changes no configuration for you.

```
claude mcp add --scope user codex-task -- /path/to/codex-task mcp
codex mcp add codex-task -- /path/to/codex-task mcp
```

Tools:

- `codex_task_submit`: `task`, `report`, `sandbox` required; optional `cwd`, `schema`, `model`, `effort`, `add_dirs`, `network`, `max_resumes`, `timeout_min`, `label`
- `codex_task_status`, `codex_task_result`, `codex_task_cancel`: argument `job_id`
- `codex_task_wait`: `job_id`, `timeout_sec` (default 50, at most 600 per call). MCP clients often have tool timeouts, so if the job is not finished the current state is returned; call again.
- `codex_task_list`: optional `active_only`

A result is one text block holding the JSON above. Errors (unknown job, safety violation, missing argument, bad configuration) come back with `isError: true` and text starting with `error:`.

### Calling it from Codex

With `approval_policy = "never"`, Codex rejects MCP tool calls that are not pre-approved. Approve the read-only tools in `~/.codex/config.toml`:

```toml
[mcp_servers.codex-task.tools.codex_task_status]
approval_mode = "approve"

[mcp_servers.codex-task.tools.codex_task_wait]
approval_mode = "approve"

[mcp_servers.codex-task.tools.codex_task_result]
approval_mode = "approve"

[mcp_servers.codex-task.tools.codex_task_list]
approval_mode = "approve"
```

Leave `codex_task_submit` and `codex_task_cancel` on the default (approval required). A Codex session running in a read-only sandbox should not be able to use a submit call to start a writable job for itself without a human or a stronger policy saying yes.

## Safety limits

Enforced in the core, identical for both entrances:

- The sandbox is `read-only` or `workspace-write` only. `danger-full-access` is refused, and so is any `--config` that sets `sandbox_mode` or `approval_policy` or contains `danger` or `bypass`. The approval policy is fixed to `never`.
- `--cwd` and every `--add-dir` are resolved with realpath and must lie under one of the configured `allowed_roots` (symlink escapes are caught). A violating request creates no job.

## Where jobs live

`<jobs_dir>/<job_id>/` holds `request.json`, `state.json` (written atomically: temp file plus rename), `result.json`, `worker.log` and one log per attempt (`codex-run.log`, `codex-run.resume1.log`, ...). Without `--cwd`, Codex works in `<job dir>/work`.

## Tests

```
python3 -m unittest discover -s tests
```

The tests use a temporary directory, a temporary config file (via `CODEX_TASK_CONFIG`) and a fake `codex` script; they never call a real model.
