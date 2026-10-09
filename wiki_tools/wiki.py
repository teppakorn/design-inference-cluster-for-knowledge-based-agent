"""A folder of Markdown notes as a searchable wiki: keyword / tag / date search, read a page, list tags.

No embeddings and no vector store: the model greps, filters by frontmatter and follows [[wiki-links]]
into notes and hub pages. Standard library only (scripts/webui.sh also runs it on the host).

Folder layout it understands:
  <category>/<note>.md       one note per file. YAML-style frontmatter between --- lines is read for
                             title, date, category, tags, tickers, companies, broker (all optional);
                             otherwise the title is the first "# " line and the date a leading
                             YYYY-MM-DD in the file name
  _hubs/<kind>/<page>.md     hub pages (company, broker, tag, timeline ...): readable, not searched
  <category>/_index.md       category index; index.md = entry page
  _meta/catalog.json         optional: the note list with the same fields (skips parsing frontmatter)
  _meta/tags.json            optional: {"tags": {tag: count}, "categories": {folder: label}}
"""
from __future__ import annotations

import json
import os
import posixpath
import re
from dataclasses import dataclass, field

STOP = set(("a an and are as at be by did do does for from has have how in is it its of on or over said say says than that the "
            "their them they this to was were what when where which who why will with about after before between during into most "
            "latest recent research report reports wiki note notes").split())
MAX_PAGE = 6000  # characters of a page returned by read_note


def tokens(s: str) -> list[str]:
    out = []
    for t in re.split(r"[^a-z0-9.]+", (s or "").lower()):
        t = t.strip(".")
        if len(t) > 1 and t not in STOP:
            out.append(t)
    return out


def _count(hay: str, needle: str) -> int:
    return hay.count(needle) if needle else 0


def _frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    meta = {}
    for line in text[3:end].splitlines():
        key, sep, val = line.partition(":")
        if not sep or not key.strip() or key.startswith(" "):
            continue
        val = val.strip()
        if val[:1] in '["{':
            try:
                val = json.loads(val)
            except ValueError:
                val = val.strip('"')
        meta[key.strip()] = val
    return meta


@dataclass
class Note:
    name: str
    path: str
    title: str
    date: str
    category: str
    broker: str | None = None
    tags: list[str] = field(default_factory=list)
    tickers: list[str] = field(default_factory=list)
    companies: list[str] = field(default_factory=list)
    title_lc: str = ""
    meta_lc: str = ""
    body_lc: str = ""


@dataclass
class Wiki:
    dir: str
    notes: list[Note]
    tags: dict[str, int]
    categories: dict[str, str]
    files: dict[str, str]  # page name -> relative path
    date_range: tuple[str, str]


def _as_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)] if v else []


def load_wiki(wiki_dir: str) -> Wiki:
    files: dict[str, str] = {}
    note_paths: list[str] = []
    for root, dirs, names in os.walk(wiki_dir):
        dirs.sort()
        for fn in sorted(names):
            if not fn.endswith(".md"):
                continue
            rel = os.path.relpath(os.path.join(root, fn), wiki_dir).replace(os.sep, "/")
            files[rel[:-3]] = rel  # "ai-semiconductors/_index"
            if not fn.startswith("_"):
                files.setdefault(fn[:-3], rel)  # bare page name
                if not any(part.startswith("_") for part in rel.split("/")[:-1]) and rel != "index.md":
                    note_paths.append(rel)

    catalog_path = os.path.join(wiki_dir, "_meta", "catalog.json")
    if os.path.exists(catalog_path):
        with open(catalog_path, encoding="utf-8") as fh:
            entries = json.load(fh)
    else:
        entries = []
        for rel in note_paths:
            with open(os.path.join(wiki_dir, rel), encoding="utf-8") as fh:
                text = fh.read()
            fm = _frontmatter(text)
            stem = posixpath.basename(rel)[:-3]
            heading = next((ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), stem)
            m = re.match(r"\d{4}-\d{2}-\d{2}", stem)
            entries.append({"name": stem, "path": rel, "title": fm.get("title") or heading,
                            "date": str(fm.get("date") or (m.group(0) if m else "")),
                            "category": fm.get("category") or (rel.split("/")[0] if "/" in rel else ""),
                            "broker": fm.get("broker") or fm.get("source_name"), "tags": _as_list(fm.get("tags")),
                            "tickers": _as_list(fm.get("tickers")), "companies": _as_list(fm.get("companies"))})

    notes = []
    for e in entries:
        with open(os.path.join(wiki_dir, e["path"]), encoding="utf-8") as fh:
            text = fh.read()
        parts = text.split("\n## Report\n", 1)
        body = parts[1] if len(parts) > 1 else text
        n = Note(name=e["name"], path=e["path"], title=e.get("title") or e["name"], date=e.get("date") or "",
                 category=e.get("category") or "", broker=e.get("broker"), tags=_as_list(e.get("tags")),
                 tickers=_as_list(e.get("tickers")), companies=_as_list(e.get("companies")))
        n.title_lc = n.title.lower()
        n.meta_lc = " ".join(x for x in [n.broker or "", n.category, *n.tags, *n.tickers, *n.companies]).lower()
        n.body_lc = body.lower()
        notes.append(n)

    tags_path = os.path.join(wiki_dir, "_meta", "tags.json")
    if os.path.exists(tags_path):
        with open(tags_path, encoding="utf-8") as fh:
            reg = json.load(fh)
        tags, categories = reg.get("tags") or {}, reg.get("categories") or {}
    else:
        tags = {}
        for n in notes:
            for t in n.tags:
                tags[t] = tags.get(t, 0) + 1
        tags = dict(sorted(tags.items(), key=lambda kv: (-kv[1], kv[0])))
        categories = {c: c for c in sorted({n.category for n in notes if n.category})}
    dates = sorted(n.date for n in notes if n.date)
    return Wiki(wiki_dir, notes, tags, categories, files, (dates[0], dates[-1]) if dates else ("", ""))


def _resolve_tag(wiki: Wiki, raw) -> str | None:
    t = re.sub(r"\s+", "-", re.sub(r"^#", "", str(raw).strip()).lower())
    if t in wiki.tags:
        return t
    return next((x for x in wiki.tags if x.endswith("/" + t)), None) or next((x for x in wiki.tags if t in x), None)


def search_wiki(wiki: Wiki, query: str = "", tags=None, category: str | None = None, date_from: str | None = None,
                date_to: str | None = None, limit=8) -> str:
    terms = tokens(query)
    asked = tags if isinstance(tags, list) else ([tags] if tags else [])
    resolved = [(t, _resolve_tag(wiki, t)) for t in asked]
    want = [r for _, r in resolved if r]
    unknown = [t for t, r in resolved if not r]
    if category and category not in wiki.categories:
        category = None  # unknown folder: ignore
    try:
        lim = int(float(limit)) or 8
    except (TypeError, ValueError):
        lim = 8
    lim = max(1, min(15, lim))
    hits = []
    for n in wiki.notes:
        if category and n.category != category:
            continue
        if date_from and n.date < date_from:
            continue
        if date_to and n.date > date_to:
            continue
        if want and not all(t in n.tags for t in want):
            continue
        score = sum(3 * _count(n.title_lc, t) + 2 * _count(n.meta_lc, t) + min(5, _count(n.body_lc, t)) for t in terms)
        if terms and score == 0:
            continue
        hits.append((score, n))
    hits.sort(key=lambda h: h[1].date, reverse=True)  # newest first on ties ...
    hits.sort(key=lambda h: h[0], reverse=True)       # ... best score first (stable sort)
    # Index lines only: the model has to open a page to ground its answer, as with a real knowledge base
    lines = [f"[[{n.name}]] | {n.date} | {n.broker or '-'} | {n.title}" for _, n in hits[:lim]]
    head = (f"{len(hits)} match(es){' with tags ' + ' '.join('#' + t for t in want) if want else ''}; "
            f"showing {len(lines)}, best first (newest first on ties).")
    if unknown:
        head += f" Ignored unknown tag(s): {', '.join(map(str, unknown))} (see list_tags)."
    return "\n".join([head, *lines])


def list_tags(wiki: Wiki, contains: str = "") -> str:
    c = str(contains or "").lower()
    rows = [f"#{t} ({n})" for t, n in wiki.tags.items() if not c or c in t][:80]
    return ", ".join(rows) if rows else f'no tag contains "{contains}"'


def read_note(wiki: Wiki, name: str) -> str:
    key = re.sub(r"^\[\[|\]\]$", "", str(name or "").strip()).split("|")[0]
    key = re.sub(r"\.md$", "", key).lstrip("/")
    rel = wiki.files.get(key) or wiki.files.get(posixpath.basename(key))
    if not rel:
        toks = tokens(key)
        guesses = [k for k in wiki.files if any(t in k for t in toks)][:6]
        return f'note "{name}" not found.' + (" Did you mean: " + ", ".join(f"[[{g}]]" for g in guesses) if guesses else "")
    with open(os.path.join(wiki.dir, rel), encoding="utf-8") as fh:
        text = fh.read()
    if len(text) > MAX_PAGE:
        text = text[:MAX_PAGE] + f"\n…[truncated {len(text) - MAX_PAGE} chars]"
    return text


def system_prompt(wiki: Wiki, description: str = "notes") -> str:
    """The instructions the chat model gets with the tools: what is in the wiki and how to use it."""
    def has(page: str) -> bool:
        return page in wiki.files

    cats = "\n".join(f"- {k}: {v} ({sum(1 for n in wiki.notes if n.category == k)} notes)" + (f" -> [[{k}/_index]]" if has(f"{k}/_index") else "")
                     for k, v in wiki.categories.items())
    hubs_dir = os.path.join(wiki.dir, "_hubs")
    hub_kinds = []
    if os.path.isdir(hubs_dir):
        for kind in sorted(os.listdir(hubs_dir)):
            pages = sorted(f[:-3] for f in os.listdir(os.path.join(hubs_dir, kind)) if f.endswith(".md")) if os.path.isdir(os.path.join(hubs_dir, kind)) else []
            if pages:
                hub_kinds.append(f"{kind} ({len(pages)} pages, e.g. [[{pages[0]}]])")
    brokers = sorted({n.broker for n in wiki.notes if n.broker})
    plain = " ".join(f"#{t}({c})" for t, c in wiki.tags.items() if "/" not in t)
    spaces: dict[str, list[str]] = {}
    for t in wiki.tags:
        if "/" in t:
            ns, _, val = t.partition("/")
            spaces.setdefault(ns, []).append(val)
    namespaced = ", ".join(f"{ns}/<{'|'.join(v[:6])}{'|...' if len(v) > 6 else ''}>" for ns, v in spaces.items())
    lines = [
        f"You are a research assistant. You answer questions ONLY from a local knowledge wiki of {len(wiki.notes)} {description}"
        + (f", dated {wiki.date_range[0]} to {wiki.date_range[1]}." if wiki.date_range[0] else "."),
        "",
        "Rules:",
        "1. Search results only list page names and titles. Facts, numbers and reasoning are only inside the pages, so you MUST open pages "
        "with read_note before answering - at least 2 pages, up to 5. Never answer from titles alone and never invent numbers.",
        "2. Dates matter. Give the date (YYYY-MM-DD) for every claim. If notes disagree, prefer the most recent one and say which is newer.",
        "3. Workflow: search_wiki (keywords and/or tags, category, date range) -> read_note on the most relevant notes and hub pages -> answer."
        + (" Hub pages collect every note on one subject with dates, so read them for timelines." if hub_kinds else ""),
        "4. Cite pages as [[page-name]]. Keep answers concise. If the wiki has nothing relevant, say so.",
        "",
        "Wiki layout:",
    ]
    if has("index"):
        lines.append("- [[index]]: entry page.")
    lines.append("- One folder per category; one note per file.")
    if hub_kinds:
        lines.append("- Hub pages: " + "; ".join(hub_kinds) + ".")
    lines += ["", "Categories:", cats]
    if brokers:
        lines += ["", "Sources: " + ", ".join(brokers)]
    if plain:
        lines += ["", "Tags (count): " + plain]
    if namespaced:
        lines.append("Namespaced tags: " + namespaced + ".")
    return "\n".join(lines)
