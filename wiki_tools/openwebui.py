"""Write the Open WebUI settings that connect the wiki tools, one file per env var (scripts/webui.sh).

    python3 -m wiki_tools.openwebui <out dir> [wiki dir] [description]

With a wiki dir: TOOL_SERVER_CONNECTIONS registers http://wiki-tools:8000 as tool server "wiki" (readable by
every user), DEFAULT_MODEL_METADATA turns it on for every model and turns off Open WebUI's built-in tools
(knowledge files, chats, memory, web search: the wiki is the knowledge here), and DEFAULT_MODEL_PARAMS sets
native tool calling and the system prompt that explains the wiki. Without one: empty settings.
Standard library only: runs on the host.
"""
from __future__ import annotations

import json
import os
import sys

from wiki_tools.wiki import load_wiki, system_prompt


def settings(wiki_dir: str = "", description: str = "notes") -> dict[str, str]:
    if not wiki_dir:
        return {"TOOL_SERVER_CONNECTIONS": "[]", "DEFAULT_MODEL_METADATA": "{}", "DEFAULT_MODEL_PARAMS": "{}"}
    server = {
        "type": "openapi", "url": "http://wiki-tools:8000", "path": "/openapi.json", "auth_type": "none", "key": "",
        "config": {"enable": True, "access_grants": [{"principal_type": "user", "principal_id": "*", "permission": "read"}]},
        "info": {"id": "wiki", "name": "Wiki", "description": "Search and read the notes of the local wiki"},
    }
    return {
        "TOOL_SERVER_CONNECTIONS": json.dumps([server]),
        "DEFAULT_MODEL_METADATA": json.dumps({"toolIds": ["server:wiki"], "capabilities": {"builtin_tools": False}}),
        "DEFAULT_MODEL_PARAMS": json.dumps({"function_calling": "native", "system": system_prompt(load_wiki(wiki_dir), description)}),
    }


def main() -> None:
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    for name, value in settings(*sys.argv[2:4]).items():
        with open(os.path.join(out, name), "w", encoding="utf-8") as fh:
            fh.write(value)


if __name__ == "__main__":
    main()
