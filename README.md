# nexus-settings-tracker

Version-control for [Subspace Nexus](https://subspacenexus.com) arena settings.

The Nexus site exposes each user-made arena's full configuration as a single
flattened blob, e.g. <https://subspacenexus.com/api/config/87>. That blob is
hard to track over time — small tweaks to a single knob can dramatically change
gameplay, but there's no history.

This tool periodically fetches the blob for a configurable set of arenas, splits
it into smaller topic-grouped `.conf` files, filters out private sections (like
`[Staff]`), and commits the result to git with an **auto-generated message that
describes exactly what changed**. A GitHub Actions workflow runs it on a
~15-minute schedule so the repo updates itself.

## Layout

```
arenas/
  svs/                     # one directory per arena, named after the arena
    ship-settings.conf     # Warbird, Javelin, Spider, Leviathan, Terrier,
                           #   Weasel, Lancaster, Shark
    weapons.conf           # Bullet, Bomb, Mine, Shrapnel, Burst, Rocket, ...
    prizes.conf            # Prize, PrizeWeight, DPrizeWeight, Periodic
    modules.conf           # Modules + module-specific config sections
    network.conf           # Lag, Latency, Net, Security, Message
    arena.conf             # General, Misc, Door, Radar, Team, Toggle, ...
```

The split `.conf` files **are** the tracked state. On each run the previous
state is reconstructed by parsing them back, so commits only appear when a
setting genuinely changes — nothing volatile (no timestamps) is ever written,
which keeps the scheduled job from producing noise commits.

## Configuration — `tracker.config.json`

| Key | Meaning |
| --- | --- |
| `base_url` | API base; arena id is appended (`/api/config/87`). |
| `output_dir` | Root directory for arena subdirectories (default `arenas`). |
| `arenas` | List of `{ "id": <int>, "name": <string\|null> }`. `null` name → auto-detected from the config's `;ArenaName` header. |
| `exclude_sections` | Section names to drop entirely (default `["Staff"]`). |
| `exclude_keys` | `{ "<Section>": ["Key", ...] }`; use `"*"` to drop a key from every section. |
| `file_groups` | `{ "<filename>.conf": ["Section", ...] }` — which sections go in which file. |
| `default_file` | Catch-all for sections not named in any group (so new upstream settings are never dropped). |

To monitor more arenas, add entries to `arenas`. To regroup files, edit
`file_groups`. To hide more private knobs, add to `exclude_sections` /
`exclude_keys`.

## Usage (local)

```bash
python track.py --dry-run     # fetch + show what would change, write nothing
python track.py               # write the split files (no commit)
python track.py --commit      # write + git commit per changed arena
python track.py --arena 87    # restrict to one arena
```

No third-party dependencies — Python 3.8+ standard library only.

## Automation

`.github/workflows/track.yml` runs `python track.py --commit` every ~15 minutes
and pushes. It uses the built-in `GITHUB_TOKEN` (no secrets to configure) and
needs `contents: write` permission, which is already set in the workflow.

> Scheduled GitHub workflows are best-effort: runs can be delayed under load and
> are automatically disabled after 60 days with no repository activity. Pushes
> from the bot count as activity.

## First-time GitHub setup

```bash
git init                       # if not already a repo
git add -A && git commit -m "Initial commit"
gh repo create nexus-settings-tracker --public --source . --push
# Settings → Actions → General → Workflow permissions → Read and write
```

Then trigger the first run from the Actions tab (**Run workflow**), or just wait
for the schedule.
