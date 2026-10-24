# Fixture sources

Each fixture is a real devague file copied from a repository that is public on
GitHub (checked with `gh repo view`). Files are re-indented JSON; some are
trimmed to a few claims or tasks, keeping the schema shape real. The commit is
the last commit that touched the file in the local clone.

| Fixture | Source repo | Path | Commit | Notes |
| --- | --- | --- | --- | --- |
| `frame_v1.json` | `https://github.com/agentculture/agentfront` | `.devague/frames/agentfront-issue-31-is-resolved-the-teken-wrapper.json` | `64bb89341c8adc34bcc5caae100392246334e239` | verbatim (re-indented JSON) |
| `frame_v2.json` | `https://github.com/agentculture/league-of-agents-platform.git` | `.devague/frames/league-of-agents-matures-the-arena-grows-past-laun.json` | `bdae5c1a7ed7ee1fc5a80c10cc427ed4d7db8bcb` | verbatim (re-indented JSON) |
| `frame_v3.json` | `https://github.com/agentculture/lobes-cli` | `.devague/frames/lobes-capabilities-tells-you-where-a-lobe-is-serve.json` | `702d1a2f3ab15fe606c2dd48055f78777522634c` | trimmed to first 6 of 17 claims |
| `frame_v4.json` | `https://github.com/agentculture/devague` | `.devague/frames/issue-backlog-sweep.json` | `e5047a4875b34857b5312232723690483bb5acb7` | trimmed to first 6 of 36 claims |
| `frame_v5.json` | `https://github.com/agentculture/culture-nodes.git` | `.devague/frames/two-fixes-upkeep-dispatch-and-decidable-outcomes.json` | `1f15c704beb08611aa77521164778b43324b110d` | verbatim (re-indented JSON) |
| `frame_v6.json` | `https://github.com/agentculture/shabbos-goy.git` | `.devague/frames/readme-refresh.json` | `2bf429b213d442832c75c845cf217cdbdd6db6d4` | verbatim (re-indented JSON) |
| `frame_vunversioned.json` | `https://github.com/agentculture/devague` | `.devague/frames/devague-now-ships-a-documented-spec-contract-every.json` | `d09d1c2a618445b34e4d68990f1b16f89c7019ea` | verbatim (re-indented JSON) |
| `plan_v1.json` | `https://github.com/agentculture/agentfront` | `.devague/plans/agentfront-issue-31-is-resolved-the-teken-wrapper.json` | `64bb89341c8adc34bcc5caae100392246334e239` | verbatim (re-indented JSON) |
| `plan_v2.json` | `https://github.com/agentculture/lobes-cli` | `.devague/plans/lobes-gateway-relays-sse-streams-frame-by-frame-th.json` | `9e3c9dae8a98930c53d1b06c6b7dee817ebcd0fe` | verbatim (re-indented JSON) |
| `plan_v3.json` | `https://github.com/agentculture/lobes-cli` | `.devague/plans/lobes-capabilities-tells-you-where-a-lobe-is-serve.json` | `702d1a2f3ab15fe606c2dd48055f78777522634c` | trimmed to first 4 of 6 tasks |
| `plan_v4.json` | `https://github.com/agentculture/colleague` | `.devague/plans/qwen38-pin-rollover.json` | `82ea7339efe407aacb28e45d5bb81b9d2167eba7` | trimmed to first 4 of 5 tasks |
| `plan_v5.json` | `https://github.com/agentculture/climate-cli.git` | `.devague/plans/dashboard-data-efficiency.json` | `6aada4ded7db43593ac7428f5320dfcd50fd55b7` | verbatim (re-indented JSON) |
| `plan_vunversioned.json` | `https://github.com/agentculture/devague` | `.devague/plans/devague-0-6-0-ships-the-human-review-loop-devague.json` | `39470832e563f907d78ff16de6eb4642219950eb` | verbatim (re-indented JSON) |
| `frame_v6_rich.json` | `https://github.com/agentculture/climate-cli.git` | `.devague/frames/dashboard-data-efficiency.json` | `f7478cdc59c338baa5ddd8174b8596288427166c` | trimmed: claims 4 of 19; scope_entries 2 of 15; open_vagueness 2 of 3; lapses 1 of 1; obligations 2 of 5 |
| `plan_v5_rich.json` | `https://github.com/agentculture/shabbos-goy.git` | `.devague/plans/strict-window-close-boundary.json` | `2bf429b213d442832c75c845cf217cdbdd6db6d4` | trimmed: targets 8 of 18; tasks 3 of 4; risks 2 of 19; obligations 2 of 6 |

There is no public schema-6 plan: no plan at schema 6 exists in `~/git`, because
schema 6 only changed frames. The schema 7 case is synthesised in the tests.
