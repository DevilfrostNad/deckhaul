"""Optional online check against the public Steam Web API (no key needed).

Only workshop item ids are sent. Used to spot items removed from the
Workshop and updates Steam has not downloaded yet.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Dict, List

URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"


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
