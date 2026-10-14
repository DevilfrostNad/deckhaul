"""TruckyMods catalog, read politely from the public site.

TruckyMods has no public API. The Trucky team allowed DeckHaul to use the
site as long as downloads are done properly. So DeckHaul:

  * reads only public pages (search, categories, mod pages), with a
    descriptive User-Agent, at most one request every REQUEST_GAP seconds,
    and caches answers (mod pages for a day, lists for half an hour);
  * never downloads files itself. "Download" opens the official download
    page in the user's browser, where the site counts the download and shows
    the ads that pay creators. The file lands in Downloads, and DeckHaul's
    installer picks it up from there.
"""

from __future__ import annotations

import html as htmllib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from . import __version__

BASE = "https://truckymods.io"
USER_AGENT = f"DeckHaul/{__version__} (+https://github.com/DevilfrostNad/deckhaul)"
GAME_SLUGS = {"ets2": "euro-truck-simulator-2", "ats": "american-truck-simulator"}
REQUEST_GAP = 1.5
TTL_MOD = 24 * 3600
TTL_LIST = 30 * 60
MAX_PAGE = 3 << 20

_gap_lock = threading.Lock()
_last_request = [0.0]


class TruckyError(Exception):
    pass


# ----------------------------------------------------------------- fetching

def _fetch(url: str, timeout: float = 30.0) -> str:
    if not url.startswith(BASE + "/"):
        raise TruckyError("адрес не с TruckyMods")
    with _gap_lock:
        wait = REQUEST_GAP - (time.time() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(MAX_PAGE)
    except OSError as exc:
        raise TruckyError(f"TruckyMods не отвечает: {exc}")
    return data.decode("utf-8", "replace")


class Cache:
    def __init__(self, path: str):
        self.path = path
        self.data: Dict[str, dict] = {}
        try:
            with open(path, encoding="utf-8") as fh:
                self.data = json.load(fh)
        except (OSError, ValueError):
            pass

    def get(self, key: str, ttl: float):
        item = self.data.get(key)
        if item and time.time() - item.get("t", 0) < ttl:
            return item["v"]
        return None

    def put(self, key: str, value) -> None:
        self.data[key] = {"t": time.time(), "v": value}
        if len(self.data) > 2000:  # keep the file small
            for k in sorted(self.data, key=lambda k: self.data[k]["t"])[:500]:
                del self.data[k]
        tmp = f"{self.path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass


# ------------------------------------------------------------------- parsing

def _text(fragment: str) -> str:
    return htmllib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment))).strip()


def _int(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else 0


def mod_url_re(game_slug: str):
    return re.compile(r'href="?(%s/%s/([a-z0-9-]+)/([a-z0-9-]+))"?[\s>]' % (re.escape(BASE), re.escape(game_slug)))


def parse_list(page: str, game_slug: str) -> List[dict]:
    """Mod cards from a search, category or trending page (two layouts exist)."""
    rx = mod_url_re(game_slug)
    items: Dict[str, dict] = {}
    for chunk in re.split(r'<div class="card[ "]', page)[1:]:
        m = rx.search(chunk)
        if not m:
            continue
        url, category, slug = m.group(1), m.group(2), m.group(3)
        if url in items:
            continue
        title = ""
        t = re.search(r'title="([^"]+)"[^>]*href="?%s' % re.escape(url), chunk) or \
            re.search(r'href="?%s"? title="([^"]+)"' % re.escape(url), chunk)
        if t:
            title = htmllib.unescape(t.group(1))
        if not title:
            a = re.search(r'href="?%s"?[^>]*>([^<]+)</a>' % re.escape(url), chunk)
            title = _text(a.group(1)) if a else slug.replace("-", " ")
        img = re.search(r'data-src="([^"]+)"', chunk)
        author = re.search(r'href="?%s/user/\d+"?[^>]*>(.*?)</a>' % re.escape(BASE), chunk, re.S)
        dl = re.search(r'bi-download[^>]*></i>\s*([\d,.\s]+)<', chunk)
        rating = re.search(r'bi-star-fill[^>]*></i>\s*(?:<span[^>]*>)?\s*([\d.]+|N/A)', chunk)
        desc = re.search(r'<div class="mt-3">(.*?)</div>', chunk, re.S)
        items[url] = {
            "url": url, "category": category, "title": title.strip(),
            "image": img.group(1) if img else "",
            "author": _text(author.group(1)) if author else "",
            "downloads": _int(dl.group(1)) if dl else 0,
            "rating": float(rating.group(1)) if rating and rating.group(1) != "N/A" else None,
            "description": _text(desc.group(1))[:300] if desc else "",
        }
    return list(items.values())


def _field(page: str, label: str) -> str:
    m = re.search(r'fw-bolder mt-5">\s*%s\s*</div>\s*<div[^>]*>(.*?)</div>' % re.escape(label), page, re.S)
    return m.group(1) if m else ""


def _badges(fragment: str) -> List[str]:
    return [_text(b) for b in re.findall(r'<span class="badge[^"]*">(.*?)</span>', fragment, re.S) if _text(b)]


def parse_mod(page: str, url: str, game_slug: str) -> dict:
    """Details of one mod page."""
    title = ""
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        if d.get("@type") == "WebPage":
            title = d.get("name") or ""
            desc = d.get("description") or ""
            images = d.get("image") or []
            break
    else:
        desc, images = "", []
    if not title:
        og = re.search(r'<meta property="og:title" content="([^"]*)"', page)
        title = htmllib.unescape(og.group(1)) if og else url.rsplit("/", 1)[-1]
    for suffix in (" for Euro Truck Simulator 2", " for American Truck Simulator"):
        if title.endswith(suffix):
            title = title[: -len(suffix)]
    author = re.search(r'fw-bolder"\s+href="%s/user/\d+"\s*>\s*([^<]+)</a>' % re.escape(BASE), page)

    latest = re.search(r'<downloadbutton(?=[^>]*download/latest)([^>]*)>', page)
    attrs = latest.group(1) if latest else ""
    size = re.search(r':size="(\d+)"', attrs)
    project = re.search(r':project_id="(\d+)"', attrs)
    auth = re.search(r':auth="(true|false)"', attrs)

    builds = []
    sec = page.find('id="nav-builds"')
    if sec >= 0:
        part = page[sec:]
        end = part.find('<div class="tab-pane', 10)
        part = part[: end if end > 0 else len(part)]
        for item in part.split('<div class="timeline-item">')[1:]:
            ver = re.search(r'badge-light-primary[^"]*">([^<]+)<', item)
            date = re.search(r"<br\s*/?>\s*([\d-]{8,10})", item)
            bid = re.search(r':build_id="(\d+)"', item)
            durl = re.search(r":download_url=\"'([^']+)'\"", item)
            games = item.split("Supported Game Versions:", 1)
            gvs = _badges(games[1].split("</div>", 1)[0]) if len(games) > 1 else []
            changes = item.split("Changes:", 1)
            builds.append({
                "version": _text(ver.group(1)) if ver else "",
                "date": date.group(1) if date else "",
                "build_id": int(bid.group(1)) if bid else None,
                "download_url": durl.group(1) if durl else "",
                "game_versions": [g for g in gvs if re.match(r"^\d+\.\d+", g)],
                "changes": _text(changes[1])[:600] if len(changes) > 1 else "",
            })

    dlcs, required = [], []
    deps = page.find('id="nav-deps"')
    if deps >= 0:
        part = page[deps:]
        end = part.find('<div class="tab-pane', 10)
        part = part[: end if end > 0 else len(part)]
        for title_attr, app in re.findall(r'title="([^"]+)"\s+href="https://store\.steampowered\.com/app/(\d+)', part):
            dlcs.append({"name": htmllib.unescape(title_attr), "app_id": int(app)})
        rx = mod_url_re(game_slug)
        for m in rx.finditer(part):
            if m.group(1) != url and m.group(1) not in {r["url"] for r in required}:
                t = re.search(r'href="?%s"?[^>]*>(.*?)</a>' % re.escape(m.group(1)), part, re.S)
                required.append({"url": m.group(1), "title": _text(t.group(1)) if t else m.group(3)})

    compat = _badges(_field(page, "Compatibility"))
    return {
        "url": url, "title": title.strip(), "description": desc[:1500],
        "images": images[:6] if isinstance(images, list) else [images],
        "author": htmllib.unescape(author.group(1)).strip() if author else "",
        "version": " ".join(_badges(_field(page, "Current Version"))) or _text(_field(page, "Current Version")),
        "game_versions": [g for g in compat if re.match(r"^\d+\.\d+", g)],
        "updated": _text(_field(page, "Last Updated")),
        "downloads": _int(_text(_field(page, "Downloads"))),
        "size": int(size.group(1)) if size else 0,
        "project_id": int(project.group(1)) if project else None,
        "login_required": bool(auth and auth.group(1) == "true" and "download_permitted_to=\"all\"" not in attrs),
        "download_page": url + "/download/latest",
        "builds": builds, "dlcs": dlcs, "required_mods": required,
    }


def page_of(url: str) -> Optional[str]:
    """Mod page URL from any TruckyMods URL of that mod (page, download page)."""
    m = re.match(r"(%s/[a-z0-9-]+/[a-z0-9-]+/[a-z0-9-]+)(?:/|$|\?)" % re.escape(BASE), url or "")
    return m.group(1) if m else None


# --------------------------------------------------------------------- client

class Client:
    def __init__(self, cache_path: str, game: str = "ets2", fetch=None):
        self.cache = Cache(cache_path)
        self.slug = GAME_SLUGS.get(game, GAME_SLUGS["ets2"])
        self.fetch = fetch or _fetch

    def _get(self, url: str, ttl: float, parse):
        cached = self.cache.get(url, ttl)
        if cached is not None:
            return cached
        value = parse(self.fetch(url))
        self.cache.put(url, value)
        return value

    def search(self, query: str, page: int = 1) -> List[dict]:
        q = urllib.parse.urlencode({"query": query.strip()[:100], "page": page})
        return self._get(f"{BASE}/search?{q}", TTL_LIST, lambda h: parse_list(h, self.slug))

    def category(self, category: str, page: int = 1) -> List[dict]:
        if not re.fullmatch(r"[a-z0-9-]+", category or ""):
            raise TruckyError("неизвестная категория")
        return self._get(f"{BASE}/{self.slug}/{category}?page={int(page)}", TTL_LIST,
                         lambda h: parse_list(h, self.slug))

    def trending(self) -> List[dict]:
        return self._get(f"{BASE}/trending/weekly", TTL_LIST, lambda h: parse_list(h, self.slug))

    def mod(self, url: str, fresh: bool = False) -> dict:
        page = page_of(url)
        if not page or f"/{self.slug}/" not in page:
            raise TruckyError("это не страница мода TruckyMods для этой игры")
        ttl = 0 if fresh else TTL_MOD
        return self._get(page, ttl, lambda h: parse_mod(h, page, self.slug))


def best_build(details: dict, game_version: Optional[str]) -> Optional[dict]:
    """The newest build made for the user's game version, else the newest one."""
    builds = [b for b in details.get("builds", []) if b.get("download_url")]
    if not builds:
        return None
    if game_version:
        mm = ".".join(game_version.split(".")[:2])
        for b in builds:
            if any(g == mm or g.startswith(mm + ".") or game_version.startswith(g) for g in b["game_versions"]):
                return b
    return builds[0]


def version_key(v: str):
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:4]) if nums else None


def is_newer(remote: str, local: str) -> Optional[bool]:
    """True/False when both look like versions, None when they cannot be compared."""
    if not remote or not local:
        return None
    a, b = version_key(remote), version_key(local)
    if a is None or b is None:
        return None if remote.strip().lower() == local.strip().lower() else None
    return a > b
