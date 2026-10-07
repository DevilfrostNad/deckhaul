"""Steam Web API calls.

fetch_details needs no key and sends only workshop item ids: it spots items
removed from the Workshop and updates Steam has not downloaded yet.

search needs the user's own Web API key and sends the mod name: it looks for
a Workshop copy of a mod installed from a website.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Dict, List

URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
QUERY_URL = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
RANKED_BY_TEXT_SEARCH = 12


def fetch_details(ids: List[int], timeout: float = 15.0) -> Dict[int, dict]:
    out: Dict[int, dict] = {}
    for start in range(0, len(ids), 100):
        chunk = ids[start : start + 100]
        form = {"itemcount": str(len(chunk))}
        for i, wid in enumerate(chunk):
            form[f"publishedfileids[{i}]"] = str(wid)
        req = urllib.request.Request(
            URL, data=urllib.parse.urlencode(form).encode(), method="POST",
            headers={"User-Agent": "DeckHaul/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        for item in data.get("response", {}).get("publishedfiledetails", []):
            try:
                out[int(item.get("publishedfileid"))] = item
            except (TypeError, ValueError):
                continue
    return out


def search(key: str, app_id: int, text: str, count: int = 8, timeout: float = 15.0) -> List[dict]:
    params = {
        "key": key, "appid": str(app_id), "search_text": text, "numperpage": str(count),
        "query_type": str(RANKED_BY_TEXT_SEARCH), "return_details": "true",
        "return_short_description": "true", "return_previews": "true", "page": "1",
    }
    req = urllib.request.Request(QUERY_URL + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": "DeckHaul"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    out = []
    for item in data.get("response", {}).get("publishedfiledetails", []) or []:
        try:
            wid = int(item.get("publishedfileid"))
        except (TypeError, ValueError):
            continue
        out.append({
            "id": wid,
            "title": item.get("title") or "",
            "description": (item.get("short_description") or item.get("file_description") or "")[:300],
            "updated": int(item.get("time_updated") or 0),
            "subscriptions": int(item.get("subscriptions") or 0),
            "preview": item.get("preview_url") or "",
        })
    return out
