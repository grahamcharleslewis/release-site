#!/usr/bin/env python3
"""Build the static site from content/index.md plus releases of upstream repos.

- Each upstream release is fetched and cached once (in the release-cache branch,
  checked out at ./cache by the workflow). Existing releases are never re-rendered.
- "What's new" summaries are written by people (or with an AI assistant, offline) and
  committed as content/summaries/<owner>__<repo>/<tag>.md. Releases without one show
  "Summary not available yet". `--list-missing` lists the releases that need one.
- Releases deleted upstream are removed from the cache.
- Planned releases come from content/upcoming.md and/or a roadmap repo
  (site.config.json "roadmap"): a folder with one markdown file per planned version. A snapshot is kept in the cache so
  upstream roadmap edits trigger a deploy and a failed fetch falls back to it.

Standard library only. GITHUB_TOKEN is used for the GitHub API.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX_MD = ROOT / "content" / "index.md"
SUMMARIES_DIR = ROOT / "content" / "summaries"
TEMPLATE = ROOT / "templates" / "page.html"
STATIC_DIR = ROOT / "static"

API = "https://api.github.com"
USER_AGENT = "release-site-builder"

UPCOMING_MD = ROOT / "content" / "upcoming.md"
UPCOMING_KEYS = {"title", "platform", "target", "status"}
ROADMAP_SNAPSHOT = "roadmap.json"   # stored in the cache dir

# {{releases:owner/repo}} = one repo's releases; {{releases:merged}} = all repos, one card per version.
MARKER_RE = re.compile(r"^[ \t]*\{\{\s*releases:\s*([\w.-]+/[\w.-]+|merged)\s*\}\}[ \t]*$", re.M)
# One ```upcoming fenced block, and a run of adjacent ones (rendered as one card list).
UPCOMING_BLOCK_RE = re.compile(r"^```upcoming[ \t]*\n(.*?)^```[ \t]*$", re.M | re.S)
UPCOMING_RUN_PATTERN = r"(?:^```upcoming[ \t]*\n.*?^```[ \t]*$\s*)+"
UPCOMING_RUN_RE = re.compile(UPCOMING_RUN_PATTERN, re.M | re.S)
# On the upcoming page: either a {{roadmap}} marker or a run of ```upcoming blocks.
UPCOMING_SLOT_RE = re.compile(
    r"(?P<roadmap>^[ \t]*\{\{\s*roadmap\s*\}\}[ \t]*$)|(?P<run>" + UPCOMING_RUN_PATTERN + ")", re.M | re.S
)
BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- HTTP helpers

def http(method, url, *, token=None, body=None, accept="application/vnd.github+json", timeout=60):
    headers = {"Accept": accept, "User-Agent": USER_AGENT}
    if url.startswith(API):
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers


def next_link(headers) -> str | None:
    m = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("Link", "") or "")
    return m.group(1) if m else None


def list_releases(repo: str, token: str | None) -> list[dict]:
    """All published releases (incl. prereleases), with body and body_html."""
    url = f"{API}/repos/{repo}/releases?per_page=100"
    out: list[dict] = []
    while url:
        raw, headers = http("GET", url, token=token, accept="application/vnd.github.full+json")
        out.extend(json.loads(raw))
        url = next_link(headers)
    return [r for r in out if not r.get("draft")]


def render_markdown(text: str, token: str | None) -> str:
    """Render markdown the way GitHub renders README files (wrapped lines stay one paragraph)."""
    body = {"text": text, "mode": "markdown"}
    raw, _ = http("POST", f"{API}/markdown", token=token, body=body)
    return raw.decode("utf-8")


# ---------------------------------------------------------------- roadmap repo

def fetch_roadmap(rcfg: dict, token: str | None) -> dict:
    """Fetch roadmap sources from the configured repo: `path` is a folder in which every
    .md file is one planned version (README.md and _*.md are skipped)."""
    repo, ref = rcfg["repo"], rcfg.get("ref", "main")
    path = rcfg.get("path", "releases").strip("/")
    url = f"{API}/repos/{repo}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref, safe='')}"
    raw, _ = http("GET", url, token=token)
    listing = json.loads(raw)
    if not isinstance(listing, list):
        raise ValueError(f"roadmap path '{path}' is a file; it must be a folder of one file per release")

    files = sorted(
        (f for f in listing
         if f["type"] == "file" and f["name"].lower().endswith(".md")
         and not f["name"].startswith("_") and f["name"].lower() != "readme.md"),
        key=lambda f: f["name"],
    )
    sources = []
    for f in files:
        raw, _ = http("GET", f["url"], token=token, accept="application/vnd.github.raw+json")
        sources.append([f["name"], raw.decode("utf-8")])
    return {"repo": repo, "ref": ref, "path": path, "sources": sources}


def sync_roadmap(rcfg: dict | None, cache_dir: Path, token: str | None) -> tuple[dict | None, bool]:
    """Return (roadmap, changed). Falls back to the cached snapshot if the fetch fails."""
    snap_path = cache_dir / ROADMAP_SNAPSHOT
    old = json.loads(snap_path.read_text(encoding="utf-8")) if snap_path.is_file() else None
    if not rcfg or rcfg.get("repo", "").startswith("OWNER/"):
        if rcfg:
            log("Roadmap repo in site.config.json is still a placeholder; skipping roadmap.")
        if old:
            snap_path.unlink()
            return None, True
        return None, False

    log(f"Fetching roadmap from {rcfg['repo']} ({rcfg.get('path', 'releases')})")
    try:
        new = fetch_roadmap(rcfg, token)
    except Exception as e:  # noqa: BLE001
        log(f"  ! could not fetch roadmap: {e}; using last good copy" if old else f"  ! could not fetch roadmap: {e}")
        return old, False

    if new == old:
        return new, False
    snap_path.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"  roadmap changed ({len(new['sources'])} file(s))")
    return new, True


def split_front_matter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---[ \t]*\n(.*?)^---[ \t]*(?:\n|$)", text, re.S | re.M)
    if not m:
        return {}, text
    meta = {}
    for key, value in re.findall(r"^\s*([A-Za-z]+)\s*:\s*(.*?)\s*$", m.group(1), re.M):
        meta[key.lower()] = value.strip().strip("\"'")
    return meta, text[m.end():]


SHARED_HEADING_RE = re.compile(r"^(what'?s planned|planned|both( platforms)?|all( platforms)?)$", re.I)
DETAILS_HEADING_RE = re.compile(r"^(more )?details$", re.I)


def parse_release_file(text: str, name: str, platforms: list[str] = ()) -> dict:
    """One-file-per-release format:
      front matter: title, platform ("Android, iPhone" or one), target, status
      bullets before any heading or under "## What's planned" = planned for every platform
      bullets under "## Android" / "## iPhone only" (a platform label) = that platform only
      "## Details" and anything else = markdown for the collapsed "More details" section."""
    meta, body = split_front_matter(text)
    by_label = {p.lower(): p for p in platforms}
    items: list[str] = []
    platform_items: dict[str, list[str]] = {}
    details: list[str] = []
    section: str | tuple = "shared"
    current: list[str] | None = None
    for line in body.splitlines():
        if h := re.match(r"^\s*#{1,6}\s+(.*?)\s*#*\s*$", line):
            label = h.group(1).strip().rstrip(":")
            platform = by_label.get(re.sub(r"\s+only$", "", label, flags=re.I).lower())
            if platform:
                section = ("platform", platform)
            elif SHARED_HEADING_RE.match(label):
                section = "shared"
            else:
                section = "details"
                if not DETAILS_HEADING_RE.match(label):
                    details.append(line)  # other headings belong to the details text
            current = None
            continue
        if section == "details":
            details.append(line)
        elif m := BULLET_RE.match(line):
            current = items if section == "shared" else platform_items.setdefault(section[1], [])
            current.append(m.group(1).strip())
        elif current and line.strip() and line[:1] in (" ", "\t"):
            current[-1] += " " + line.strip()  # wrapped bullet
        elif line.strip():
            details.append(line)  # stray text outside a list goes to details
            current = None
    if "title" not in meta:
        log(f"  ! roadmap {name}: no 'title' in front matter")
    return {
        **{k: v for k, v in meta.items() if k in UPCOMING_KEYS},
        "items": items,
        "platform_items": {p: platform_items[p] for p in platforms if p in platform_items},
        "details": "\n".join(details).strip(),
    }


def roadmap_sort_key(filename: str):
    """Files named by version (1.12.md, 1.12.1.md) sort by version, soonest first;
    any other names sort alphabetically after them."""
    stem = filename.rsplit(".", 1)[0]
    if re.match(r"^v?\d+(\.\d+)*", stem, re.I):
        return (0, version_sort_key(version_key(stem)))
    return (1, stem)


# ---------------------------------------------------------------- cache

def repo_key(repo: str) -> str:
    return repo.replace("/", "__")


def entry_path(cache_dir: Path, repo: str, release_id: int) -> Path:
    return cache_dir / repo_key(repo) / f"{release_id}.json"


def load_cache(cache_dir: Path, repo: str) -> dict[int, dict]:
    entries: dict[int, dict] = {}
    d = cache_dir / repo_key(repo)
    if d.is_dir():
        for p in d.glob("*.json"):
            try:
                entries[int(p.stem)] = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, json.JSONDecodeError) as e:
                log(f"  ! ignoring unreadable cache file {p}: {e}")
    return entries


def save_entry(cache_dir: Path, entry: dict) -> None:
    p = entry_path(cache_dir, entry["repo"], entry["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sync_repo(repo: str, releases: list[dict], cache_dir: Path) -> tuple[dict[int, dict], bool]:
    """Add releases not yet cached; drop cached releases deleted upstream."""
    cached = load_cache(cache_dir, repo)
    changed = False
    live_ids = {r["id"] for r in releases}

    for r in releases:
        if r["id"] in cached:
            continue  # already processed once — never re-render
        entry = {
            "id": r["id"],
            "repo": repo,
            "tag": r["tag_name"],
            "name": r.get("name") or r["tag_name"],
            "published_at": r.get("published_at") or r.get("created_at"),
            "prerelease": bool(r.get("prerelease")),
            "html_url": r["html_url"],
            "body": r.get("body") or "",
            "body_html": r.get("body_html") or "",
        }
        save_entry(cache_dir, entry)
        cached[r["id"]] = entry
        changed = True
        log(f"  + new release {repo} {entry['tag']}")

    for rid in [rid for rid in cached if rid not in live_ids]:
        entry_path(cache_dir, repo, rid).unlink(missing_ok=True)
        log(f"  - removed release {repo} {cached[rid].get('tag')} (deleted upstream)")
        del cached[rid]
        changed = True

    return cached, changed


# ---------------------------------------------------------------- HTML rendering

def esc(s) -> str:
    return html.escape(str(s), quote=True)


def inline(text: str) -> str:
    """Escape text, then allow `code` spans only."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", esc(text))


def fmt_date(iso: str | None) -> str:
    if not iso:
        return ""
    d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return f"{d.day} {d:%b %Y}"


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def summary_path(repo: str, tag: str) -> Path:
    return SUMMARIES_DIR / repo_key(repo) / f"{tag.replace('/', '_')}.md"


def load_summary(repo: str, tag: str) -> list[str] | None:
    """Items from content/summaries/<owner>__<repo>/<tag>.md: one per bullet or line;
    lines starting with # are ignored."""
    p = summary_path(repo, tag)
    if not p.is_file():
        return None
    items = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = re.sub(r"^\s*[-*+]\s+", "", line).strip()
        if line and not line.startswith("#"):
            items.append(line)
    return items or None


def slug(*parts: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", "-".join(parts).lower()).strip("-")


PENDING_HTML = '<p class="release-pending">Summary not available yet. See the original notes below.</p>'


def items_html(entry: dict) -> str:
    """The "What's new" bullet list from the release's summary file, or a pending note."""
    items = load_summary(entry["repo"], entry["tag"])
    if not items:
        return PENDING_HTML
    return "<ul>\n" + "\n".join(f"<li>{inline(i)}</li>" for i in items) + "\n</ul>"


def assets_html(live: dict | None, heading: str = "Downloads") -> str:
    # Assets come from the live listing so download links are always current.
    assets = (live or {}).get("assets") or []
    if not assets:
        return ""
    lis = "\n".join(
        f'<li><a href="{esc(a["browser_download_url"])}">{esc(a["name"])}</a> '
        f'<span class="asset-size">({fmt_size(a.get("size", 0))})</span></li>'
        for a in assets
    )
    return f'<div class="release-assets"><h4>{esc(heading)}</h4><ul>\n{lis}\n</ul></div>'


def original_html(entry: dict, summary: str = "Original release notes") -> str:
    if not entry.get("body_html", "").strip():
        return ""
    return "\n".join([
        '<details class="release-original">',
        f"<summary>{esc(summary)}</summary>",
        f'<div class="markdown-body">{entry["body_html"]}</div>',  # GitHub-sanitised HTML
        "</details>",
    ])


# ---- merged view: one card per version across platforms

def version_key(tag: str) -> str:
    """Pairing key: 'v1.10' and '1.10' match, as do '1.3' and '1.3.0'."""
    v = re.sub(r"^v", "", tag.strip(), flags=re.I).lower()
    m = re.match(r"^(\d+(?:\.\d+)*)(.*)$", v)
    if not m:
        return v
    nums = [int(n) for n in m.group(1).split(".")]
    while len(nums) > 1 and nums[-1] == 0:
        nums.pop()
    return ".".join(map(str, nums)) + m.group(2)


def version_sort_key(key: str):
    """Numeric version order; '1.2-beta' sorts before '1.2'; non-numeric tags last."""
    m = re.match(r"^(\d+(?:\.\d+)*)(.*)$", key)
    if not m:
        return ((), 0, key)
    nums = tuple(int(n) for n in m.group(1).split("."))
    nums += (0,) * (6 - len(nums))
    return (nums, 0 if m.group(2) else 1, m.group(2))


def platform_label(repo: str, repo_cfgs: dict) -> str:
    return repo_cfgs.get(repo, {}).get("platform") or repo.split("/")[-1]


def render_merged_card(version: str, members: list[tuple[str, dict, dict | None]]) -> str:
    """members: [(platform label, cache entry, live release)] in config order."""
    display = re.sub(r"^v", "", members[0][1]["tag"], flags=re.I)
    all_pre = all(e.get("prerelease") for _, e, _ in members)
    single = len(members) == 1
    out = [
        f'<article class="release release-merged{" is-prerelease" if all_pre else ""}" id="{slug("version", version)}">',
        '<header class="release-header">',
        f'<h3 class="release-title">Version {esc(display)}</h3>',
        '<p class="release-meta">',
    ]
    for label, e, _ in members:
        pre = '<span class="badge badge-prerelease">Pre-release</span>' if e.get("prerelease") else ""
        out.append(
            f'<a class="platform-chip{" is-prerelease" if e.get("prerelease") else ""}" href="{esc(e["html_url"])}" '
            f'title="{esc(label)} release {esc(e["tag"])} on GitHub">'
            f'<span class="platform-name">{esc(label)}</span> '
            f'<time datetime="{esc(e["published_at"] or "")}">{fmt_date(e["published_at"])}</time>{pre}</a>'
        )
    if single:
        out.append(f'<span class="badge badge-only">{esc(members[0][0])} only</span>')
    out += ["</p>", "</header>", '<div class="release-whats-new"><h4>What’s new</h4>']
    if single:
        out.append(items_html(members[0][1]))
    else:
        for label, e, _ in members:
            out.append(f'<div class="platform-notes"><h5>{esc(label)}</h5>\n{items_html(e)}\n</div>')
    out.append("</div>")
    for label, e, live in members:
        out.append(assets_html(live, "Downloads" if single else f"Downloads – {label}"))
    for label, e, _ in members:
        out.append(original_html(e, "Original release notes" if single else f"Original release notes – {label}"))
    out.append("</article>")
    return "\n".join(x for x in out if x)


def render_merged(data: dict, repo_cfgs: dict) -> str:
    groups: dict[str, list[tuple[str, dict, dict | None]]] = {}
    for repo in repo_cfgs:  # config order = chip order
        if repo not in data:
            continue
        entries, live = data[repo]
        for e in entries.values():
            groups.setdefault(version_key(e["tag"]), []).append(
                (platform_label(repo, repo_cfgs), e, live.get(e["id"]))
            )
    if not groups:
        return '<div class="releases"><p class="releases-empty">No releases yet.</p></div>'
    for key, members in groups.items():
        labels = [m[0] for m in members]
        if len(labels) != len(set(labels)):
            log(f"  ! version {key}: more than one release per platform ({', '.join(m[1]['tag'] for m in members)})")
    ordered = sorted(groups, key=version_sort_key, reverse=True)
    cards = "\n".join(render_merged_card(k, groups[k]) for k in ordered)
    return f'<div class="releases">\n{cards}\n</div>'


def render_card(entry: dict, live: dict | None) -> str:
    pre = entry.get("prerelease")
    out = [
        f'<article class="release{" is-prerelease" if pre else ""}" id="{slug(entry["repo"], entry["tag"])}">',
        '<header class="release-header">',
        f'<h3 class="release-title"><a href="{esc(entry["html_url"])}">{esc(entry["name"])}</a></h3>',
        '<p class="release-meta">',
        f'<span class="release-tag">{esc(entry["tag"])}</span>',
        f'<time datetime="{esc(entry["published_at"] or "")}">{fmt_date(entry["published_at"])}</time>',
    ]
    if pre:
        out.append('<span class="badge badge-prerelease">Pre-release</span>')
    out += ["</p>", "</header>"]
    items = items_html(entry)
    if items == PENDING_HTML:
        out.append(items)
    else:
        out.append(f'<div class="release-whats-new"><h4>What’s new</h4>{items}</div>')
    out += [assets_html(live), original_html(entry), "</article>"]
    return "\n".join(x for x in out if x)


def render_repo(entries: dict[int, dict], live: dict[int, dict]) -> str:
    if not entries:
        return '<div class="releases"><p class="releases-empty">No releases yet.</p></div>'
    ordered = sorted(entries.values(), key=lambda e: e["published_at"] or "", reverse=True)
    cards = "\n".join(render_card(e, live.get(e["id"])) for e in ordered)
    return f'<div class="releases">\n{cards}\n</div>'


# ---------------------------------------------------------------- upcoming releases

def parse_upcoming(block: str) -> dict:
    """Parse one ```upcoming fenced block: key: value lines, a bullet list,
    and optional free markdown after a '---' line."""
    parts = re.split(r"^---[ \t]*$", block, maxsplit=1, flags=re.M)
    meta: dict[str, str] = {}
    items: list[str] = []
    for line in parts[0].splitlines():
        if not line.strip():
            continue
        if m := re.match(r"^\s*[-*+]\s+(.*)$", line):
            items.append(m.group(1).strip())
        elif (m := re.match(r"^\s*([A-Za-z]+)\s*:\s*(.*)$", line)) and m.group(1).lower() in UPCOMING_KEYS:
            meta[m.group(1).lower()] = m.group(2).strip()
        elif items:
            items[-1] += " " + line.strip()  # wrapped bullet
        else:
            log(f"  ! upcoming.md: ignoring unrecognised line: {line.strip()}")
    if "title" not in meta:
        log("  ! upcoming.md: a release block has no 'title:' line")
    return {**meta, "items": items, "details": parts[1].strip() if len(parts) > 1 else ""}


def render_upcoming_card(u: dict, token, all_platforms: list[str] = ()) -> str:
    title = u.get("title") or "Untitled release"
    out = [
        f'<article class="release is-upcoming" id="{slug("upcoming", title)}">',
        '<header class="release-header">',
        f'<h3 class="release-title">{esc(title)}</h3>',
        '<p class="release-meta">',
    ]
    platforms = [p.strip() for p in u.get("platform", "").split(",") if p.strip()]
    for p in platforms:
        out.append(f'<span class="platform-chip"><span class="platform-name">{esc(p)}</span></span>')
    if len(platforms) == 1 and len(all_platforms) > 1:
        out.append(f'<span class="badge badge-only">{esc(platforms[0])} only</span>')
    if u.get("target"):
        out.append(f'<span class="release-target">Target: {esc(u["target"])}</span>')
    if u.get("status"):
        out.append(f'<span class="badge badge-status status-{slug(u["status"])}">{esc(u["status"])}</span>')
    out += ["</p>", "</header>"]

    platform_items = u.get("platform_items") or {}
    if u["items"] or platform_items:
        out.append('<div class="release-whats-new"><h4>What’s planned</h4>')
        if u["items"]:
            out.append("<ul>\n" + "\n".join(f"<li>{inline(i)}</li>" for i in u["items"]) + "\n</ul>")
        for p, its in platform_items.items():
            lis = "\n".join(f"<li>{inline(i)}</li>" for i in its)
            out.append(f'<div class="platform-notes"><h5>{esc(p)} only</h5>\n<ul>\n{lis}\n</ul>\n</div>')
        out.append("</div>")
    if u["details"]:
        out += [
            '<details class="release-original">',
            "<summary>More details</summary>",
            f'<div class="markdown-body">{render_markdown(u["details"], token)}</div>',
            "</details>",
        ]
    out.append("</article>")
    return "\n".join(out)


def render_upcoming_group(run: str, token) -> str:
    cards = [render_upcoming_card(parse_upcoming(b), token) for b in UPCOMING_BLOCK_RE.findall(run)]
    return '<div class="releases">\n' + "\n".join(cards) + "\n</div>"


def render_roadmap(roadmap: dict | None, token, platforms: list[str] = ()) -> str:
    if not roadmap or not roadmap["sources"]:
        return '<div class="releases"><p class="releases-empty">No planned releases yet.</p></div>'
    sources = sorted(roadmap["sources"], key=lambda src: roadmap_sort_key(src[0]))
    cards = [render_upcoming_card(parse_release_file(text, name, platforms), token, platforms)
             for name, text in sources]
    return '<div class="releases">\n' + "\n".join(cards) + "\n</div>"


# ---------------------------------------------------------------- pages

SLOT_RE = re.compile(r"(?:<p[^>]*>\s*)?RELEASESSLOT(\d+)X(?:\s*</p>)?")


def render_with_slots(markdown: str, pattern: re.Pattern, make_html, token) -> str:
    """Render markdown, replacing each `pattern` match with make_html(match).
    Matches become opaque tokens first so the markdown renderer can't mangle them."""
    slots: list[re.Match] = []

    def to_token(m):
        slots.append(m)
        return f"\n\nRELEASESSLOT{len(slots) - 1}X\n\n"

    markdown = re.sub(r"<!--.*?-->", "", markdown, flags=re.S)  # commented-out markers stay inert
    body = render_markdown(pattern.sub(to_token, markdown), token)
    return SLOT_RE.sub(lambda m: make_html(slots[int(m.group(1))]), body)


def write_page(out_dir: Path, filename: str, title: str, body: str, nav: list, footer_note: str) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    nav_html = "\n".join(
        f'<a href="{href}"{" aria-current=\"page\"" if href == filename else ""}>{esc(label)}</a>'
        for href, label in nav
    )
    page = (
        TEMPLATE.read_text(encoding="utf-8")
        .replace("{{title}}", esc(title))
        .replace("{{nav}}", nav_html)
        .replace("{{footer_note}}", footer_note)
        .replace("{{generated_at_iso}}", now.isoformat(timespec="seconds"))
        .replace("{{generated_at}}", f"{now.day} {now:%b %Y, %H:%M} UTC")
        .replace("{{content}}", body)
    )
    (out_dir / filename).write_text(page, encoding="utf-8")
    log(f"Wrote {out_dir / filename}")


def build_site(cfg, data, out_dir: Path, token, roadmap: dict | None = None) -> None:
    site_title = cfg.get("site_title", "")
    has_upcoming = UPCOMING_MD.is_file() or roadmap is not None
    nav = [("index.html", "Releases")] + ([("upcoming.html", "Upcoming")] if has_upcoming else [])

    if out_dir.exists():
        shutil.rmtree(out_dir)
    if STATIC_DIR.is_dir():
        shutil.copytree(STATIC_DIR, out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- index.html: index.md + past releases
    lookup = {repo.lower(): repo for repo in data}
    repo_cfgs = {r["repo"]: r for r in cfg["repos"]}
    platforms = [platform_label(r["repo"], repo_cfgs) for r in cfg["repos"]]
    used: set[str] = set()

    def repo_html(m):
        if m.group(1).lower() == "merged":
            used.update(data)
            return render_merged(data, repo_cfgs)
        repo = lookup.get(m.group(1).lower())
        if not repo:
            log(f"  ! marker for {m.group(1)} doesn't match any repo in site.config.json")
            return ""
        used.add(repo)
        return render_repo(*data[repo])

    body = render_with_slots(INDEX_MD.read_text(encoding="utf-8"), MARKER_RE, repo_html, token)
    for repo in data:
        if repo not in used:
            body += f'\n<section class="releases-section"><h2>{esc(repo)} releases</h2>\n{render_repo(*data[repo])}\n</section>'
    write_page(out_dir, "index.html", site_title, body, nav,
               "The original release notes are included with each release.")

    # --- upcoming.html: upcoming.md (local blocks) + {{roadmap}} from the roadmap repo
    if has_upcoming:
        markdown = (UPCOMING_MD.read_text(encoding="utf-8") if UPCOMING_MD.is_file()
                    else "# Upcoming releases\n\n{{roadmap}}\n")
        roadmap_used = False

        def upcoming_html(m):
            nonlocal roadmap_used
            if m.group("roadmap") is not None:
                roadmap_used = True
                return render_roadmap(roadmap, token, platforms)
            return render_upcoming_group(m.group("run"), token)

        body = render_with_slots(markdown, UPCOMING_SLOT_RE, upcoming_html, token)
        if roadmap is not None and not roadmap_used:
            body += "\n" + render_roadmap(roadmap, token, platforms)
        title = cfg.get("upcoming_title", "Upcoming releases")
        write_page(out_dir, "upcoming.html", f"{title} · {site_title}" if site_title else title, body, nav,
                   "Planned releases are subject to change.")


# ---------------------------------------------------------------- main

def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def missing_summaries(data, limit: int | None = None) -> list[dict]:
    """Releases with no summary file, newest first."""
    missing = [e for entries, _live in data.values() for e in entries.values()
               if not summary_path(e["repo"], e["tag"]).is_file()]
    missing.sort(key=lambda e: e["published_at"] or "", reverse=True)
    return missing[:limit] if limit else missing


def list_missing(data, limit: int | None = None) -> None:
    """Print releases with no summary file, newest first, with the file to create."""
    total = len(missing_summaries(data))
    missing = missing_summaries(data, limit)
    if not missing:
        print("Every release has a summary.")
        return
    shown = f", showing {len(missing)}" if len(missing) < total else ""
    print(f"{total} release(s) without a summary (newest first{shown}):\n")
    for e in missing:
        pre = "  [pre-release]" if e.get("prerelease") else ""
        print(f"{e['repo']} {e['tag']}  ({fmt_date(e['published_at'])}){pre}")
        print(f"  notes:  {e['html_url']}")
        print(f"  create: {summary_path(e['repo'], e['tag']).relative_to(ROOT)}\n")


def export_missing(data, repo_cfgs: dict, out_dir: Path, limit: int | None = None) -> int:
    """Write each unsummarised release's raw notes to out_dir/<owner>__<repo>/<tag>.md, with a
    header naming the summary file to create. Used by scripts/draft-summaries.sh."""
    missing = missing_summaries(data, limit)
    for e in missing:
        p = out_dir / repo_key(e["repo"]) / f"{e['tag'].replace('/', '_')}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            f"Repo: {e['repo']}\n"
            f"Platform: {platform_label(e['repo'], repo_cfgs)}\n"
            f"Release: {e['name']} ({e['tag']}){' - pre-release' if e.get('prerelease') else ''}\n"
            f"Published: {fmt_date(e['published_at'])}\n"
            f"Write the summary to: {summary_path(e['repo'], e['tag']).relative_to(ROOT)}\n"
            f"\n--- Release notes ---\n\n{e['body'].strip() or '(no release notes)'}\n",
            encoding="utf-8",
        )
    log(f"Exported notes for {len(missing)} release(s) to {out_dir}")
    return len(missing)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "site.config.json"))
    ap.add_argument("--cache-dir", default=str(ROOT / "cache"))
    ap.add_argument("--out", default=str(ROOT / "_site"))
    ap.add_argument("--list-missing", action="store_true",
                    help="list releases that have no summary file yet (newest first) and exit")
    ap.add_argument("--export-missing", metavar="DIR",
                    help="write the notes of releases without a summary into DIR (for drafting) and exit")
    ap.add_argument("--limit", type=int, help="with --list-missing/--export-missing: only the newest N")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    repo_cfgs = {r["repo"]: r for r in cfg["repos"]}
    placeholders = [r for r in repo_cfgs if r.startswith(("OWNER_A/", "OWNER_B/"))]
    if placeholders:
        log(f"site.config.json still has placeholder repos: {', '.join(placeholders)}")
        return 1

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        log("! No GITHUB_TOKEN set: using the unauthenticated GitHub API (low rate limit).")

    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    changed = False
    data: dict[str, tuple[dict[int, dict], dict[int, dict]]] = {}
    for repo in repo_cfgs:
        log(f"Fetching releases for {repo}")
        releases = list_releases(repo, token)  # raises on failure → no pruning on partial data
        entries, ch = sync_repo(repo, releases, cache_dir)
        changed |= ch
        data[repo] = (entries, {r["id"]: r for r in releases})

    if args.list_missing:
        list_missing(data, args.limit)
        return 0
    if args.export_missing:
        export_missing(data, repo_cfgs, Path(args.export_missing), args.limit)
        return 0

    roadmap, roadmap_changed = sync_roadmap(cfg.get("roadmap"), cache_dir, token)
    changed |= roadmap_changed

    build_site(cfg, data, Path(args.out), token, roadmap)
    set_output("changed", "true" if changed else "false")
    log(f"Cache changed: {changed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
