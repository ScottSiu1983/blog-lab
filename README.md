# blog-lab

Companion code for Scott Siu's blog at [scottsiu.me](https://scottsiu.me): the scripts, tools and experiment setups behind the posts, so you can run them yourself.

## Posts

| Post | Code |
| --- | --- |
| 2026-11 · [Claude Told Me Astra Blacked Out and Sol Won](https://scottsiu.me/writing/codex-compaction-task-loss/) | [posts/2026-11-codex-compaction](posts/2026-11-codex-compaction) |

## Tools

| Tool | Version | What it does |
| --- | --- | --- |
| [tools/codex-task](tools/codex-task) | v0.1.0 | Runs long Codex tasks as background jobs (CLI and MCP server) that survive context compaction. |
| [tools/codex-compaction-check](tools/codex-compaction-check) | v0.1.0 | Counts compactions in a Codex session log and checks whether the task message survived. |

See also the separate companion repo [typeless-vnc-bridge](https://github.com/ScottSiu1983/typeless-vnc-bridge).

## License

MIT, see [LICENSE](LICENSE).
