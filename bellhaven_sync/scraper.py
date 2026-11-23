"""Scrape every Bellhaven community from the public website.

The directory has a few traps that this crawler is written around:
  * it is paginated, and asking for a page past the end just repeats the last page,
    so we follow the "Next" link and never guess page numbers;
  * a community can be live on the site without being in the directory yet (new
    acquisitions are announced on the homepage first), so we also collect community
    links from the homepage and About page;
  * the site states its own counts, which we use as a free self-check.
"""
from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from . import config

DETAIL_PATH = re.compile(r"^/communities/[a-z0-9][a-z0-9-]*$")
CITY_STATE_ZIP = re.compile(r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})(?:-\d{4})?$")
MAX_DIRECTORY_PAGES = 100


@dataclass
class Location:
    slug: str
    url: str
    name: str
    street: str
    city: str
    state: str
    zip: str
    care_offerings: list
    administrator: str = ""
    phone: str = ""
    found_on: list = field(default_factory=list)  # e.g. ["directory page 2"] or ["homepage"]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScrapeResult:
    locations: list
    site_notes: list      # sentences from the homepage / About page (ownership context)
    warnings: list
    stats: dict


class ScrapeError(RuntimeError):
    pass


def http_fetch(url: str) -> str:
    """GET with a couple of retries. Raises ScrapeError if the page cannot be read."""
    last_error = None
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=config.HTTP_TIMEOUT, headers={"User-Agent": config.USER_AGENT})
            if resp.status_code == 200:
                return resp.content.decode("utf-8", errors="replace")
            last_error = "HTTP {}".format(resp.status_code)
            if resp.status_code < 500:
                break
        except requests.RequestException as exc:
            last_error = str(exc)
        time.sleep(1.5 * (attempt + 1))
    raise ScrapeError("Could not fetch {}: {}".format(url, last_error))


def _detail_links(soup: BeautifulSoup) -> list:
    links = []
    for a in soup.find_all("a", href=True):
        path = urlparse(a["href"]).path
        if DETAIL_PATH.match(path) and path not in links:
            links.append(path)
    return links


def _sentences(soup: BeautifulSoup) -> list:
    container = soup.body or soup
    out = []
    for node in container.find_all(["p", "div"], recursive=True):
        if node.find(["p", "div"]) or node.find_parent(["header", "footer", "nav"]):
            continue
        text = " ".join(node.get_text(" ", strip=True).split())
        out.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 20)
    return out


def parse_detail(html: str, slug: str, url: str) -> Location:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if not h1:
        raise ScrapeError("No <h1> on {}".format(url))
    fields = {}
    for dt in soup.select("dl.detail dt"):
        dd = dt.find_next_sibling("dd")
        if dd is not None:
            fields[dt.get_text(strip=True).lower()] = dd

    address = fields.get("address")
    if address is None:
        raise ScrapeError("No address block on {}".format(url))
    lines = [ln.strip() for ln in address.get_text("\n", strip=True).split("\n") if ln.strip()]
    match = CITY_STATE_ZIP.match(lines[-1]) if len(lines) >= 2 else None
    if not match:
        raise ScrapeError("Unrecognised address on {}: {!r}".format(url, lines))

    care = fields.get("care offerings")
    offerings = [b.get_text(strip=True) for b in care.select(".badge")] if care is not None else []
    if care is not None and not offerings:
        offerings = [t.strip() for t in care.get_text(",", strip=True).split(",") if t.strip()]

    def text_of(key):
        return fields[key].get_text(" ", strip=True) if key in fields else ""

    return Location(
        slug=slug, url=url, name=" ".join(h1.get_text(" ", strip=True).split()),
        street=", ".join(lines[:-1]), city=match.group("city").strip(), state=match.group("state"),
        zip=match.group("zip"), care_offerings=offerings,
        administrator=text_of("administrator"), phone=text_of("phone"),
    )


def scrape(fetch=http_fetch, base_url: str = None) -> ScrapeResult:
    base_url = (base_url or config.BASE_URL).rstrip("/")
    warnings, site_notes = [], []
    found = {}  # detail path -> list of places we saw the link

    def note(path, where):
        found.setdefault(path, [])
        if where not in found[path]:
            found[path].append(where)

    # 1) Homepage and About page: ownership context + any communities linked from there.
    claimed_total = None
    for path, label in (("/", "homepage"), ("/about", "about page")):
        try:
            soup = BeautifulSoup(fetch(base_url + path), "html.parser")
        except ScrapeError as exc:
            warnings.append(str(exc))
            continue
        for link in _detail_links(soup):
            note(link, label)
        sentences = _sentences(soup)
        site_notes.extend(sentences)
        for s in sentences:
            m = re.search(r"serve\s+(\d+)\s+communities", s)
            if m:
                claimed_total = int(m.group(1))

    # 2) Directory: follow "Next" links only, and stop on any page we have already seen.
    directory_claim, directory_slugs, pages = None, [], 0
    next_url, seen_urls = base_url + "/communities", set()
    while next_url and next_url not in seen_urls and pages < MAX_DIRECTORY_PAGES:
        seen_urls.add(next_url)
        soup = BeautifulSoup(fetch(next_url), "html.parser")
        pages += 1
        m = re.search(r"(\d+)\s+communities listed", soup.get_text(" ", strip=True))
        if m:
            directory_claim = int(m.group(1))
        for link in _detail_links(soup):
            note(link, "directory page {}".format(pages))
            if link not in directory_slugs:
                directory_slugs.append(link)
        nxt = None
        for a in soup.select(".pager a[href]") or soup.find_all("a", href=True):
            if "next" in a.get_text(" ", strip=True).lower() or "next" in (a.get("rel") or []):
                nxt = urljoin(next_url, a["href"])
        next_url = nxt

    # 3) Detail pages.
    locations = []
    ordered = directory_slugs + [p for p in found if p not in directory_slugs]
    for path in ordered:
        where = found[path]
        url = base_url + path
        try:
            loc = parse_detail(fetch(url), path.rsplit("/", 1)[-1], url)
        except ScrapeError as exc:
            warnings.append("Skipped {}: {}".format(path, exc))
            continue
        loc.found_on = where
        locations.append(loc)

    # 4) Self-checks against what the site says about itself.
    if directory_claim is not None and directory_claim != len(directory_slugs):
        warnings.append("Directory says {} communities but {} were found in it.".format(
            directory_claim, len(directory_slugs)))
    if claimed_total is not None and claimed_total != len(locations):
        warnings.append("Homepage says {} communities but {} were scraped.".format(claimed_total, len(locations)))
    off_directory = [loc.name for loc in locations if not any(w.startswith("directory") for w in loc.found_on)]

    return ScrapeResult(
        locations=locations, site_notes=site_notes, warnings=warnings,
        stats={
            "locations": len(locations), "directory_pages": pages,
            "in_directory": len(directory_slugs), "directory_claim": directory_claim,
            "homepage_claim": claimed_total, "linked_outside_directory": off_directory,
        },
    )
