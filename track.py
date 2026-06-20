#!/usr/bin/env python3
"""Subspace Nexus arena settings tracker.

Fetches the flattened arena configuration blob from subspacenexus.com
(e.g. https://subspacenexus.com/api/config/87), splits it into smaller,
human-friendly ``.conf`` files grouped by topic, filters out private
sections (such as ``[Staff]``), and optionally commits the result to git
with an auto-generated message summarising the diff against the previous
state.

The committed ``.conf`` files ARE the version-controlled state: on the next
run the previous state is reconstructed by parsing them back, so the diff
reflects real setting changes and nothing volatile (no timestamps) is ever
written. That keeps the 15-minute scheduled job from producing noise commits.

Pure standard library -- no third-party dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections import OrderedDict

CONFIG_DEFAULT = "tracker.config.json"
HTTP_TIMEOUT = 30
USER_AGENT = "nexus-settings-tracker/1.0 (+https://github.com/)"

# Lines like ";ArenaId: 87" / ";ArenaName: svs" in the generated header.
_HEADER_RE = re.compile(r"^;\s*Arena(Id|Name)\s*:\s*(.*)$", re.IGNORECASE)


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
def fetch(url: str) -> str:
    """Fetch the raw config text from a URL."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# Parsing / rendering
#
# A parsed config is an OrderedDict mapping section name -> OrderedDict of
# key -> value, where value is either a ``str`` (scalar) or a ``list[str]``
# (a multi-line, backslash-continued value such as the module list).
# --------------------------------------------------------------------------
def parse_config(text: str):
    """Parse INI-ish config text into (meta, sections).

    ``meta`` carries the arena id/name extracted from the header comments.
    """
    meta = {"id": None, "name": None}
    sections: "OrderedDict[str, OrderedDict[str, object]]" = OrderedDict()
    current = None

    physical = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i, n = 0, len(physical)
    while i < n:
        # Gather a logical line, honouring trailing-backslash continuation.
        line = physical[i]
        cont = [line]
        while line.rstrip().endswith("\\") and i + 1 < n:
            i += 1
            line = physical[i]
            cont.append(line)
        i += 1

        head = cont[0].strip()
        if not head:
            continue
        if head.startswith(";") or head.startswith("#"):
            m = _HEADER_RE.match(head)
            if m:
                meta["id" if m.group(1).lower() == "id" else "name"] = m.group(2).strip()
            continue
        if head.startswith("[") and head.endswith("]"):
            current = head[1:-1].strip()
            sections.setdefault(current, OrderedDict())
            continue
        if "=" not in cont[0]:
            continue  # stray line; ignore

        key, _, first_val = cont[0].partition("=")
        key = key.strip()
        if len(cont) == 1:
            value: object = first_val.strip()
        else:
            items = []
            for part in [first_val] + cont[1:]:
                part = part.rstrip()
                if part.endswith("\\"):
                    part = part[:-1]
                part = part.strip()
                if part:
                    items.append(part)
            value = items
        if current is None:
            current = "_root"
            sections.setdefault(current, OrderedDict())
        sections[current][key] = value
    return meta, sections


def _render_value(value: object) -> str:
    """Render a value as the text that follows ``key=``."""
    if isinstance(value, list):
        return " \\\n" + "\n".join(" " + item + " \\" for item in value)
    return str(value)


def render_section(name: str, entries: "OrderedDict[str, object]") -> str:
    """Render one section, keys sorted for stable, diff-friendly output."""
    lines = ["[" + name + "]"]
    for key in sorted(entries.keys(), key=str.lower):
        lines.append(key + "=" + _render_value(entries[key]))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Filtering / grouping into files
# --------------------------------------------------------------------------
def _norm(value: object):
    """Comparable, hashable form of a value."""
    return tuple(value) if isinstance(value, list) else value


def filter_sections(sections, cfg):
    """Drop excluded sections and keys (case-insensitive section match)."""
    excluded = {s.lower() for s in cfg.get("exclude_sections", [])}
    excl_keys = cfg.get("exclude_keys", {}) or {}
    global_excl = {k.lower() for k in excl_keys.get("*", [])}

    out = OrderedDict()
    for sec, entries in sections.items():
        if sec.lower() in excluded:
            continue
        sec_excl = {k.lower() for k in excl_keys.get(sec, [])} | global_excl
        kept = OrderedDict(
            (k, v) for k, v in entries.items() if k.lower() not in sec_excl
        )
        out[sec] = kept
    return out


def _reverse_group_map(cfg):
    """section name (lower) -> filename, from the configured file_groups."""
    rev = {}
    for filename, secs in cfg.get("file_groups", {}).items():
        for sec in secs:
            rev[sec.lower()] = filename
    return rev


def build_files(sections, cfg):
    """Group sections into {filename: rendered_text}.

    Sections not listed in any group land in ``default_file`` so new settings
    introduced upstream are never silently dropped.
    """
    rev = _reverse_group_map(cfg)
    default_file = cfg.get("default_file", "other.conf")
    banner_tmpl = (
        "; {filename} -- managed by nexus-settings-tracker. Do not edit by hand;\n"
        "; changes are overwritten on the next sync. Source: {source}\n"
    )

    # Preserve a deterministic section order: the order declared in file_groups,
    # then any leftover sections alphabetically.
    declared_order = [s for secs in cfg.get("file_groups", {}).values() for s in secs]
    order_index = {s.lower(): i for i, s in enumerate(declared_order)}

    grouped: "OrderedDict[str, list]" = OrderedDict()
    for sec, entries in sections.items():
        filename = rev.get(sec.lower(), default_file)
        grouped.setdefault(filename, []).append(sec)

    files = {}
    for filename, secs in grouped.items():
        secs_sorted = sorted(
            secs, key=lambda s: (order_index.get(s.lower(), 10_000), s.lower())
        )
        banner = banner_tmpl.format(filename=filename, source=cfg.get("base_url", ""))
        body = "\n\n".join(render_section(s, sections[s]) for s in secs_sorted)
        files[filename] = banner + "\n" + body + "\n"
    return files


# --------------------------------------------------------------------------
# Diffing
# --------------------------------------------------------------------------
def read_existing_sections(arena_dir):
    """Reconstruct the previous state by parsing the managed .conf files."""
    sections = OrderedDict()
    if not os.path.isdir(arena_dir):
        return sections
    for fn in sorted(os.listdir(arena_dir)):
        if not fn.endswith(".conf"):
            continue
        with open(os.path.join(arena_dir, fn), encoding="utf-8") as f:
            _, secs = parse_config(f.read())
        for sec, entries in secs.items():
            sections[sec] = entries
    return sections


def diff_sections(old, new):
    """Structured key-level diff between two parsed configs."""
    changes = OrderedDict()
    seen = set()
    ordered = list(new.keys()) + [s for s in old.keys() if s not in new]
    for sec in ordered:
        if sec in seen:
            continue
        seen.add(sec)
        o = old.get(sec, OrderedDict())
        nw = new.get(sec, OrderedDict())
        added = [k for k in nw if k not in o]
        removed = [k for k in o if k not in nw]
        changed = [k for k in nw if k in o and _norm(o[k]) != _norm(nw[k])]
        if added or removed or changed:
            changes[sec] = {
                "added": added,
                "removed": removed,
                "changed": changed,
                "old": o,
                "new": nw,
            }
    return changes


def _short(value: object, width: int = 60) -> str:
    if isinstance(value, list):
        text = "[" + ", ".join(value) + "]"
    else:
        text = str(value)
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def build_commit_message(name, aid, changes, is_initial, new_sections):
    """Return (subject, body) for a commit."""
    if is_initial:
        n_settings = sum(len(s) for s in new_sections.values())
        subject = "{} ({}): initial import — {} sections, {} settings".format(
            name, aid, len(new_sections), n_settings
        )
        return subject, ""

    total = sum(
        len(c["added"]) + len(c["removed"]) + len(c["changed"]) for c in changes.values()
    )
    secs = list(changes.keys())
    shown = ", ".join(secs[:3]) + ("…" if len(secs) > 3 else "")
    plural = "change" if total == 1 else "changes"
    subject = "{} ({}): {} {} in {}".format(name, aid, total, plural, shown)

    body_lines = []
    for sec, c in changes.items():
        body_lines.append("[{}]".format(sec))
        for k in c["changed"]:
            body_lines.append(
                "  ~ {}: {} → {}".format(k, _short(c["old"][k]), _short(c["new"][k]))
            )
        for k in c["added"]:
            body_lines.append("  + {} = {}".format(k, _short(c["new"][k])))
        for k in c["removed"]:
            body_lines.append("  - {} (was {})".format(k, _short(c["old"][k])))
        body_lines.append("")
    return subject, "\n".join(body_lines).rstrip() + "\n"


# --------------------------------------------------------------------------
# Filesystem / git
# --------------------------------------------------------------------------
def sanitize(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._")
    return safe or "arena"


def write_files(arena_dir, files):
    """Write the grouped files; remove managed .conf files no longer produced."""
    os.makedirs(arena_dir, exist_ok=True)
    for existing in os.listdir(arena_dir):
        if existing.endswith(".conf") and existing not in files:
            os.remove(os.path.join(arena_dir, existing))
    for filename, content in files.items():
        with open(os.path.join(arena_dir, filename), "w", encoding="utf-8") as f:
            f.write(content)


def git_commit(arena_dir, subject, body):
    """Stage and commit only this arena's directory. Returns True if committed."""
    subprocess.run(["git", "add", "-A", "--", arena_dir], check=True)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--", arena_dir]
    ).returncode
    if staged == 0:
        return False  # nothing staged
    args = ["git", "commit", "-m", subject]
    if body.strip():
        args += ["-m", body]
    args += ["--", arena_dir]
    subprocess.run(args, check=True)
    return True


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def process_arena(cfg, arena, do_commit, dry_run):
    aid = arena["id"]
    url = cfg["base_url"].rstrip("/") + "/" + str(aid)
    text = fetch(url)
    meta, sections = parse_config(text)

    name = arena.get("name") or meta.get("name") or ("arena-" + str(aid))
    arena_dir = os.path.join(cfg.get("output_dir", "arenas"), sanitize(name))

    new_sections = filter_sections(sections, cfg)
    old_sections = read_existing_sections(arena_dir)
    is_initial = not old_sections
    changes = diff_sections(old_sections, new_sections)

    label = "{} ({})".format(name, aid)
    if not is_initial and not changes:
        print("  {}: no changes".format(label))
        return False

    subject, body = build_commit_message(name, aid, changes, is_initial, new_sections)

    if dry_run:
        print("  [dry-run] would write {} -> {}".format(label, arena_dir))
        print("  " + subject)
        if body:
            print("\n".join("    " + ln for ln in body.splitlines()))
        return True

    write_files(arena_dir, build_files(new_sections, cfg))
    print("  " + subject)
    if do_commit:
        if git_commit(arena_dir, subject, body):
            print("    committed.")
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="Track Subspace Nexus arena settings.")
    ap.add_argument("--config", default=CONFIG_DEFAULT, help="path to tracker config")
    ap.add_argument("--arena", type=int, help="only process this arena id")
    ap.add_argument("--commit", action="store_true", help="git commit per changed arena")
    ap.add_argument("--dry-run", action="store_true", help="show changes, write nothing")
    args = ap.parse_args(argv)

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)

    arenas = cfg.get("arenas", [])
    if args.arena is not None:
        arenas = [a for a in arenas if a["id"] == args.arena]
        if not arenas:
            arenas = [{"id": args.arena, "name": None}]

    failures = []
    changed_any = False
    for arena in arenas:
        try:
            print("Arena {}:".format(arena["id"]))
            if process_arena(cfg, arena, args.commit, args.dry_run):
                changed_any = True
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            print("  ERROR fetching/writing arena {}: {}".format(arena["id"], exc),
                  file=sys.stderr)
            failures.append(arena["id"])

    if failures:
        print("Completed with failures for arenas: {}".format(failures), file=sys.stderr)
    if not changed_any and not failures:
        print("No changes detected.")
    # Transient fetch failures shouldn't fail the scheduled job loudly.
    return 0


if __name__ == "__main__":
    sys.exit(main())
