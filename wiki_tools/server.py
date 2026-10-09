"""wiki-tools: the wiki (wiki_tools/wiki.py) as an OpenAPI tool server, so Open WebUI's model can search it.

Open WebUI reads /openapi.json and turns every operation into a tool the model can call:
    search_wiki(query, tags, category, date_from, date_to, limit)   -> "[[note]] | date | source | title" lines
    read_note(name)                                                   -> the page (first 6,000 characters)
    list_tags(contains)                                               -> "#tag (count)" list
GET /system_prompt returns the instructions scripts/webui.sh gives the model with the tools.

    WIKI_DIR=/path/to/notes python -m wiki_tools.server     (port 8000; WIKI_DESCRIPTION = what the notes are)
The wiki is read once at start: restart the pod after the folder changes (bash scripts/webui.sh does).
"""
from __future__ import annotations

import os

from aiohttp import web

from wiki_tools.wiki import list_tags, load_wiki, read_note, search_wiki, system_prompt

WIKI_DIR = os.environ.get("WIKI_DIR", "/wiki")
DESCRIPTION = os.environ.get("WIKI_DESCRIPTION", "notes")
PORT = int(os.environ.get("PORT", "8000"))


def body(props: dict, required: list[str] | None = None) -> dict:
    return {"required": True, "content": {"application/json": {"schema": {"type": "object", "properties": props, "required": required or []}}}}


TEXT = {"200": {"description": "plain text for the model", "content": {"text/plain": {"schema": {"type": "string"}}}}}
SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "wiki-tools", "version": "1.0", "description": "Search and read the pages of a local Markdown wiki."},
    "paths": {
        "/search_wiki": {"post": {
            "operationId": "search_wiki",
            "description": "Keyword search over the wiki notes (titles, tags, tickers, companies, sources, note text). Optional filters: "
                           "tags (all must match), category folder, date range (YYYY-MM-DD). Returns '[[note-name]] | date | source | title' "
                           "lines, best match first. These are only names and titles: open notes with read_note to get the facts.",
            "requestBody": body({
                "query": {"type": "string", "description": "Keywords, e.g. a company name, a ticker or a topic"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags to filter on, without #"},
                "category": {"type": "string", "description": "Category folder"},
                "date_from": {"type": "string", "description": "Earliest note date YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "Latest note date YYYY-MM-DD"},
                "limit": {"type": "integer", "description": "Max results (default 8, max 15)"},
            }),
            "responses": TEXT}},
        "/read_note": {"post": {
            "operationId": "read_note",
            "description": "Read one wiki page by its [[name]]: a note, a hub page (company, source, tag or month timeline: they list every "
                           "note on the subject with dates), a category index (<category>/_index) or index.",
            "requestBody": body({"name": {"type": "string", "description": "Page name as in [[...]], without .md"}}, ["name"]),
            "responses": TEXT}},
        "/list_tags": {"post": {
            "operationId": "list_tags",
            "description": "List wiki tags with note counts, optionally only tags containing a substring.",
            "requestBody": body({"contains": {"type": "string", "description": "Substring, e.g. 'ai' or 'rating/'"}}),
            "responses": TEXT}},
    },
}


async def args(request: web.Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def main() -> None:
    wiki = load_wiki(WIKI_DIR)
    print(f"wiki-tools: {len(wiki.notes)} notes, {len(wiki.files)} pages, {len(wiki.tags)} tags from {WIKI_DIR} on :{PORT}", flush=True)

    async def search(request: web.Request) -> web.Response:
        a = await args(request)
        return web.Response(text=search_wiki(wiki, a.get("query") or "", a.get("tags"), a.get("category"), a.get("date_from"),
                                             a.get("date_to"), a.get("limit") or 8))

    async def read(request: web.Request) -> web.Response:
        return web.Response(text=read_note(wiki, (await args(request)).get("name") or ""))

    async def tags(request: web.Request) -> web.Response:
        return web.Response(text=list_tags(wiki, (await args(request)).get("contains") or ""))

    app = web.Application()
    app.router.add_get("/openapi.json", lambda r: web.json_response(SPEC))
    app.router.add_post("/search_wiki", search)
    app.router.add_post("/read_note", read)
    app.router.add_post("/list_tags", tags)
    app.router.add_get("/system_prompt", lambda r: web.Response(text=system_prompt(wiki, DESCRIPTION)))
    app.router.add_get("/health", lambda r: web.json_response({"ok": True, "notes": len(wiki.notes)}))
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None, print=None)


if __name__ == "__main__":
    main()
