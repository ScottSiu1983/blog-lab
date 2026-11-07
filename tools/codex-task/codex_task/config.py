"""Configuration of codex-task, read from a JSON file on every call (no caching).

Path: $CODEX_TASK_CONFIG, else ~/.config/codex-task/config.json. Keys:
  allowed_roots  required, non-empty list of absolute directories; cwd and add-dirs must resolve under one of them
  codex_bin      optional, path of the codex executable (default: `codex` found on PATH)
  jobs_dir       optional, where job directories live (default: ~/.local/state/codex-task/jobs)
$CODEX_TASK_JOBS overrides jobs_dir.
"""
import json
import os
import shutil
from pathlib import Path

ENV_CONFIG = 'CODEX_TASK_CONFIG'
ENV_JOBS = 'CODEX_TASK_JOBS'
EXAMPLE = '{\n  "allowed_roots": ["/home/you/work"]\n}'


class ConfigError(ValueError):
    """The configuration is missing or invalid. Callers report it as a setup error."""


def config_path():
    env = os.environ.get(ENV_CONFIG)
    return Path(env).expanduser() if env else Path.home() / '.config' / 'codex-task' / 'config.json'


def _fail(path, why):
    raise ConfigError(f'{why}. Create {path} (or point {ENV_CONFIG} at a file) with content like:\n{EXAMPLE}')


def load():
    """Return {"allowed_roots": [...], "codex_bin": str, "jobs_dir": Path}. Raise ConfigError when unusable."""
    path = config_path()
    if not path.is_file():
        _fail(path, f'configuration file not found: {path}')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        _fail(path, f'configuration file {path} is not readable JSON ({exc})')
    if not isinstance(data, dict):
        _fail(path, f'configuration file {path} must contain a JSON object')
    roots = data.get('allowed_roots')
    if not isinstance(roots, list) or not roots or not all(isinstance(r, str) and r for r in roots):
        _fail(path, f'"allowed_roots" in {path} must be a non-empty list of directory paths')
    roots = [os.path.expanduser(r) for r in roots]
    bad = [r for r in roots if not os.path.isabs(r)]
    if bad:
        _fail(path, f'"allowed_roots" entries must be absolute paths (got {bad[0]!r})')
    configured = data.get('codex_bin')
    if configured:
        codex = os.path.expanduser(str(configured))
        if not os.path.isfile(codex):
            _fail(path, f'"codex_bin" {codex} is not a file')
    else:
        codex = shutil.which('codex')
        if not codex:
            _fail(path, '"codex_bin" is not set and no `codex` executable was found on PATH')
    jobs = os.environ.get(ENV_JOBS) or data.get('jobs_dir')
    jobs = Path(os.path.expanduser(jobs)) if jobs else Path.home() / '.local' / 'state' / 'codex-task' / 'jobs'
    return {'allowed_roots': roots, 'codex_bin': codex, 'jobs_dir': jobs}
