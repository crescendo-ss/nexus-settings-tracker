#!/usr/bin/env python3
"""Static changelog site generator for nexus-settings-tracker.

Reads the repository's git history (the version-controlled state produced by
``track.py``) and renders a static HTML site that lists arena setting changes
over time:

  * ``index.html``        -- a single timeline across all tracked arenas.
  * ``<arena>.html``      -- one timeline per arena.

The data source is ``git log``: every change commit already carries a
structured subject (``<name> (<id>): N changes in ...``) and body (the
``~`` changed / ``+`` added / ``-`` removed lines) emitted by
``track.py``'s ``build_commit_message``. This script just parses that back
out and renders it.

IMPORTANT: the generated site is intentionally volatile (it reflects commit
dates and the latest history) and must NEVER be committed into the tracked
tree -- doing so would produce a noise commit on every scheduled run, which
is exactly what track.py's "no volatile content" invariant exists to avoid.
It is built fresh in CI and deployed straight to GitHub Pages as an
artifact. The default output dir (``_site``) is git-ignored.

Pure standard library -- no third-party dependencies.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import subprocess
import sys
from collections import OrderedDict
from datetime import datetime, timezone

# Record/field separators for the git log format. These bytes never appear in
# commit subjects or bodies, so parsing stays unambiguous.
_RS = "\x1e"
_FS = "\x1f"

# "svs (87): 3 changes in Warbird, Bomb" -> name, id, summary. Tooling commits
# (e.g. "Initial commit: ...") lack the " (<digits>): " shape and are skipped.
_SUBJECT_RE = re.compile(r"^(?P<name>.+?) \((?P<id>\d+)\): (?P<summary>.+)$")

DEFAULT_OUTPUT = "_site"


# --------------------------------------------------------------------------
# Reading git history
# --------------------------------------------------------------------------
def read_commits():
    """Return arena change commits, newest first, as a list of dicts."""
    fmt = _FS.join(["%H", "%aI", "%s", "%b"]) + _RS
    out = subprocess.run(
        ["git", "log", "--no-merges", "--format=" + fmt],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout

    commits = []
    for record in out.split(_RS):
        record = record.lstrip("\n")
        if not record.strip():
            continue
        parts = record.split(_FS)
        if len(parts) < 4:
            continue
        chash, date_iso, subject, body = parts[0], parts[1], parts[2], parts[3]
        m = _SUBJECT_RE.match(subject.strip())
        if not m:
            continue  # not an arena change commit
        commits.append(
            {
                "hash": chash.strip(),
                "date_iso": date_iso.strip(),
                "name": m.group("name"),
                "id": m.group("id"),
                "summary": m.group("summary"),
                "sections": parse_body(body),
            }
        )
    return commits


def parse_body(body):
    """Parse a commit body into [{"name": sec, "lines": [(kind, text)]}].

    ``kind`` is one of "changed" / "added" / "removed". Lines that don't match
    a known marker are ignored, so a future change to the body format degrades
    gracefully rather than rendering garbage.
    """
    sections = []
    current = None
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = {"name": line[1:-1], "lines": []}
            sections.append(current)
            continue
        if current is None:
            continue
        if line.startswith("~"):
            current["lines"].append(("changed", line[1:].strip()))
        elif line.startswith("+"):
            current["lines"].append(("added", line[1:].strip()))
        elif line.startswith("-"):
            current["lines"].append(("removed", line[1:].strip()))
    return sections


def group_by_arena(commits):
    """Group commits by arena id (stable across renames), newest name wins."""
    arenas = OrderedDict()
    for c in commits:
        a = arenas.get(c["id"])
        if a is None:
            a = {"id": c["id"], "name": c["name"], "commits": []}
            arenas[c["id"]] = a
        a["commits"].append(c)
    # commits are already newest-first; the first one seen carries the latest name.
    return arenas


# --------------------------------------------------------------------------
# Repo / link helpers
# --------------------------------------------------------------------------
def detect_repo(explicit):
    """Return "owner/name" for building commit links, or None if unknown."""
    if explicit:
        return explicit
    env = os.environ.get("GITHUB_REPOSITORY")
    if env:
        return env
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return None
    m = re.search(r"github\.com[:/]+([^/]+)/(.+?)(?:\.git)?$", url)
    return m.group(1) + "/" + m.group(2) if m else None


def commit_url(repo, chash):
    return "https://github.com/{}/commit/{}".format(repo, chash) if repo else None


def slug_for(name, id_, used):
    """Stable, unique, filesystem-safe page slug for an arena."""
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._").lower() or "arena"
    slug = base
    if slug in used and used[slug] != id_:
        slug = "{}-{}".format(base, id_)
    used[slug] = id_
    return slug


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def _e(text):
    return html.escape(str(text))


def render_diff(sections):
    if not sections:
        return ""
    blocks = []
    for sec in sections:
        rows = ['<div class="section">[{}]</div>'.format(_e(sec["name"]))]
        for kind, text in sec["lines"]:
            sign = {"changed": "~", "added": "+", "removed": "−"}[kind]
            rows.append(
                '<div class="line {}"><span class="sign">{}</span>{}</div>'.format(
                    kind, sign, _e(text)
                )
            )
        blocks.append('<div class="secblock">' + "".join(rows) + "</div>")
    return '<div class="diff">' + "".join(blocks) + "</div>"


def render_commit(c, repo, show_arena, arena_href=None):
    disp_date = c["date_iso"][:16].replace("T", " ")
    short = c["hash"][:7]
    url = commit_url(repo, c["hash"])
    hash_html = (
        '<a class="hash" href="{}" rel="noopener">{}</a>'.format(_e(url), short)
        if url
        else '<span class="hash">{}</span>'.format(short)
    )
    arena_html = ""
    if show_arena:
        label = "{} <span class=\"aid\">#{}</span>".format(_e(c["name"]), _e(c["id"]))
        arena_html = (
            '<a class="arena" href="{}">{}</a>'.format(_e(arena_href), label)
            if arena_href
            else '<span class="arena">{}</span>'.format(label)
        )
    return (
        '<article class="commit">'
        '<header class="chead">'
        '<time datetime="{iso}">{date}</time>'
        "{arena}{hash}"
        "</header>"
        '<div class="summary">{summary}</div>'
        "{diff}"
        "</article>"
    ).format(
        iso=_e(c["date_iso"]),
        date=_e(disp_date),
        arena=arena_html,
        hash=hash_html,
        summary=_e(c["summary"]),
        diff=render_diff(c["sections"]),
    )


def render_timeline(commits, repo, show_arena, arena_hrefs=None):
    """Render commits grouped under day headers (newest first)."""
    if not commits:
        return '<p class="empty">No changes recorded yet.</p>'
    out = []
    current_day = None
    for c in commits:
        day = c["date_iso"][:10]
        if day != current_day:
            out.append('<h2 class="day">{}</h2>'.format(_e(day)))
            current_day = day
        href = arena_hrefs.get(c["id"]) if (show_arena and arena_hrefs) else None
        out.append(render_commit(c, repo, show_arena, href))
    return "\n".join(out)


CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
 background:#0d1117;color:#c9d1d9}
a{color:#58a6ff;text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:860px;margin:0 auto;padding:32px 20px 64px}
.site-head{border-bottom:1px solid #21262d;padding-bottom:16px;margin-bottom:24px}
.site-head h1{margin:0 0 4px;font-size:22px}
.site-head .sub{color:#8b949e;font-size:13px}
nav.arenas{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
nav.arenas a{background:#161b22;border:1px solid #21262d;border-radius:999px;padding:4px 12px;font-size:13px;color:#c9d1d9}
nav.arenas a:hover{border-color:#58a6ff;text-decoration:none}
nav.arenas a .count{color:#8b949e;margin-left:6px}
.back{display:inline-block;margin-bottom:16px;font-size:13px}
h2.day{font-size:13px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.04em;
 margin:28px 0 12px;padding-bottom:6px;border-bottom:1px solid #21262d}
.commit{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:14px 16px;margin:10px 0}
.chead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:13px;color:#8b949e}
.chead time{font-variant-numeric:tabular-nums}
.chead .arena{font-weight:600;color:#c9d1d9}
.chead .aid{color:#8b949e;font-weight:400}
.chead .hash{margin-left:auto;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#8b949e}
.summary{margin:6px 0 0;font-size:14px}
.diff{margin-top:10px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;
 background:#0d1117;border:1px solid #21262d;border-radius:6px;overflow-x:auto}
.secblock{padding:6px 0}
.secblock+.secblock{border-top:1px solid #21262d}
.diff .section{padding:2px 12px;color:#8b949e;font-weight:600}
.diff .line{padding:1px 12px;white-space:pre-wrap;word-break:break-word}
.diff .sign{display:inline-block;width:1.2em;color:#6e7681}
.diff .changed{color:#d29922}
.diff .added{color:#3fb950}
.diff .removed{color:#f85149}
.empty{color:#8b949e}
footer{margin-top:40px;padding-top:16px;border-top:1px solid #21262d;color:#6e7681;font-size:12px}
"""

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<div class="wrap">
{header}
{body}
<footer>{footer}</footer>
</div>
</body>
</html>
"""


def build_footer(repo, built):
    repo_link = (
        '<a href="https://github.com/{r}" rel="noopener">{r}</a>'.format(r=_e(repo))
        if repo
        else "git history"
    )
    return "Generated from {} by nexus-settings-tracker &middot; last built {} UTC".format(
        repo_link, _e(built)
    )


def build_site(output_dir, repo):
    commits = read_commits()
    arenas = group_by_arena(commits)
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    footer = build_footer(repo, built)

    # Assign a stable page slug/href per arena id.
    used = {}
    hrefs = {}
    for aid, a in arenas.items():
        hrefs[aid] = slug_for(a["name"], aid, used) + ".html"

    os.makedirs(output_dir, exist_ok=True)

    # Index: cross-arena timeline + arena nav.
    nav = ""
    if arenas:
        chips = [
            '<a href="{href}">{name} <span class="count">{n}</span></a>'.format(
                href=_e(hrefs[aid]), name=_e(a["name"]), n=len(a["commits"])
            )
            for aid, a in arenas.items()
        ]
        nav = '<nav class="arenas">' + "".join(chips) + "</nav>"
    index_header = (
        '<header class="site-head">'
        "<h1>Arena settings changelog</h1>"
        '<div class="sub">{n} change{s} across {na} arena{nas}</div>'
        "{nav}"
        "</header>"
    ).format(
        n=len(commits),
        s="" if len(commits) == 1 else "s",
        na=len(arenas),
        nas="" if len(arenas) == 1 else "s",
        nav=nav,
    )
    index_body = render_timeline(commits, repo, show_arena=True, arena_hrefs=hrefs)
    _write(
        os.path.join(output_dir, "index.html"),
        PAGE.format(
            title="Arena settings changelog",
            css=CSS,
            header=index_header,
            body=index_body,
            footer=footer,
        ),
    )

    # Per-arena pages.
    for aid, a in arenas.items():
        header = (
            '<a class="back" href="index.html">&larr; all arenas</a>'
            '<header class="site-head">'
            '<h1>{name} <span class="aid">#{id}</span></h1>'
            '<div class="sub">{n} change{s}</div>'
            "</header>"
        ).format(
            name=_e(a["name"]),
            id=_e(aid),
            n=len(a["commits"]),
            s="" if len(a["commits"]) == 1 else "s",
        )
        body = render_timeline(a["commits"], repo, show_arena=False)
        _write(
            os.path.join(output_dir, hrefs[aid]),
            PAGE.format(
                title="{} (#{}) changelog".format(a["name"], aid),
                css=CSS,
                header=header,
                body=body,
                footer=footer,
            ),
        )

    return len(commits), len(arenas)


def _write(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the static changelog site.")
    ap.add_argument(
        "--output", default=DEFAULT_OUTPUT, help="output directory (default: _site)"
    )
    ap.add_argument(
        "--repo", help='"owner/name" for commit links (default: auto-detect)'
    )
    args = ap.parse_args(argv)

    repo = detect_repo(args.repo)
    n_commits, n_arenas = build_site(args.output, repo)
    print(
        "Wrote {} ({} changes, {} arenas) -> {}".format(
            "index.html + {} arena page(s)".format(n_arenas),
            n_commits,
            n_arenas,
            args.output,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
