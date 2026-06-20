# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A version-control layer for [Subspace Nexus](https://subspacenexus.com) arena settings. The Nexus site serves each user-made arena's full configuration as one flattened INI-style blob (e.g. `https://subspacenexus.com/api/config/87`). This tool fetches that blob on a schedule, splits it into topic-grouped `.conf` files under `arenas/<arena-name>/`, drops private sections, and commits the result with an auto-generated diff message. A GitHub Actions cron (`*/15`) runs it and pushes.

Everything lives in a single pure-stdlib script, `track.py` (Python 3.8+, no dependencies). `tracker.config.json` is the only configuration.

## Commands

```bash
python3 track.py --dry-run      # fetch + report what would change, write nothing
python3 track.py                # write split files, no git
python3 track.py --commit       # write + one git commit per changed arena
python3 track.py --arena 87     # restrict to a single arena id
python3 track.py --config PATH  # alternate config file (default tracker.config.json)
```

There is no build step, no lint config, and no test suite. The de-facto correctness check is the round-trip invariant below: **run `python3 track.py` twice against the live endpoint — the second run must report `no changes`.** If it doesn't, rendering became non-deterministic or non-round-tripping (see below). To exercise the diff path without waiting for a real upstream change, edit a value in a committed `arenas/<name>/*.conf` (that file *is* the "previous" state) and run `--dry-run`.

## Architecture and the invariants that hold it together

Data flow per arena (`process_arena`): fetch text → `parse_config` → `filter_sections` → diff against previous → `build_files` → `write_files` → optional `git_commit`.

The split `.conf` files **are** the version-controlled state. There is no separate snapshot or database. On each run the previous state is reconstructed by parsing the on-disk `.conf` files back in (`read_existing_sections`), and the freshly fetched config is the new state. Three invariants make this safe, and breaking any one causes the 15-minute job to spew noise commits or lose data:

1. **No volatile content is ever written.** The upstream blob has no timestamp; do not add one (or any run-dependent value) to generated files. The file banner and structure must be a pure function of the config content. "Last updated" comes from git history, not file contents.

2. **Rendering is deterministic and round-trips.** `render_section` sorts keys case-insensitively; section order within a file follows the declared `file_groups` order then alphabetical. `parse_config(render(x))` must equal `x` at the value level, so previous-vs-new comparison sees only real changes. Multi-line backslash-continued values (e.g. `[Modules] AttachModules`) are parsed into `list[str]` and re-emitted in a canonical continuation format precisely to preserve this. If you touch parsing or rendering, re-verify the run-twice no-op.

3. **Filtering is applied identically to old and new.** `exclude_sections` / `exclude_keys` are removed from the fetched config; because excluded data is never written, the reconstructed "previous" state also lacks it, so comparison stays consistent. `[Staff]` is excluded by default.

Other structural notes:
- **Section→file mapping** comes from `file_groups` in the config; `build_files` builds the reverse map. Any section not named in a group falls through to `default_file` (`other.conf`) so new upstream sections are never silently dropped.
- **One commit per changed arena.** `build_commit_message` renders a subject + structured body (`~` changed, `+` added, `-` removed, grouped by section) from `diff_sections`. First run for an arena is an "initial import" commit.
- **Arena directory name** is the sanitized `;ArenaName` header value (overridable per-arena in config). Renaming an arena upstream would start a fresh directory — a known, accepted limitation.

## Changelog site (`build_site.py`)

`build_site.py` renders a static GitHub Pages changelog from `git log` — an
`index.html` timeline plus one page per arena — by parsing the structured
commit subjects/bodies that `build_commit_message` emits. It is pure stdlib and
independent of `track.py` (it reads history, not the live endpoint).

The site is **derived and intentionally volatile** (it shows commit dates and a
build timestamp), which is the exact opposite of the `.conf` files. The only
rule that matters: **never commit the generated site into the tracked tree.**
Doing so would produce a noise commit on every 15-minute run — precisely what
invariant #1 forbids. CI builds it into `_site/` (git-ignored) and publishes it
via `actions/deploy-pages` as an artifact, not a commit. The Pages job needs
`fetch-depth: 0` (full history) and `pages: write` / `id-token: write`.

## Git identity (do not "fix" the SSH flags)

This repo commits locally as `Crescendo <weasal.ss@gmail.com>` and pushes over the `crescendo-ss` GitHub account. `core.sshCommand` deliberately includes `-o IdentityAgent=none -F /dev/null` so the user's global `~/.ssh/config` (which forces a different `IdentityFile` for `github.com`) is bypassed — without those flags, pushes authenticate as the wrong account. This matches the sibling repo `../subspace-in-browser`.

The GitHub Actions workflow (`.github/workflows/track.yml`) is separate: it commits as `subspace-tracker[bot]` and pushes via the built-in `GITHUB_TOKEN`, not the SSH key. This bot identity for automated commits is intentional — keep it.
