"""Build a podcast RSS feed of Gordon B. Hinckley General Conference talks.

Audio discovery uses the Church's own JSON content API (the same one the
website's audio player calls), instead of regex-scraping the raw HTML:

    /study/api/v3/language-pages/type/content?lang=eng&uri=<talk-uri>

The API response contains meta.audio[].mediaUrl (a direct media2.ldscdn.org
MP3) plus title, and a content body with the transcript HTML.
"""

import hashlib
import html
import json
import re
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://www.churchofjesuschrist.org"
ARCHIVE_URL = (
    "https://www.churchofjesuschrist.org/study/general-conference/"
    "speakers/gordon-b-hinckley?lang=eng"
)
API_URL = BASE + "/study/api/v3/language-pages/type/content"

PAGES_URL = "https://duck-scout.github.io/rt-private-feed"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; HinckleyPodcastArchive/1.0; "
        "+https://github.com/duck-scout/rt-private-feed)"
    )
}

OUTPUT_FILE = "feed-gordon-b-hinckley.xml"

# Set to an integer (e.g. 4000) to truncate transcripts in episode
# descriptions and keep the feed XML small. None = full transcript.
MAX_DESCRIPTION_CHARS = None

# Politeness delay between requests, in seconds.
SLEEP = 0.25

session = requests.Session()
session.headers.update(HEADERS)


def fetch_text(url, timeout=30, retries=2):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            return response.text
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(1.0 + attempt)
    raise last_exc


def fetch_json(url, params=None, timeout=30, retries=2):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(1.0 + attempt)
    raise last_exc


def clean_text(value):
    if not value:
        return ""
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def get_archive_links():
    """Scrape the Hinckley speaker archive for talk-page URIs.

    Returns a list of (uri, fallback_title) where uri looks like
    /general-conference/2001/10/the-times-in-which-we-live
    """
    print("Fetching Hinckley speaker archive...")
    page = fetch_text(ARCHIVE_URL)
    soup = BeautifulSoup(page, "html.parser")

    pattern = re.compile(r"^/study(/general-conference/\d{4}/(?:0[1-9]|1[0-2])/[^/]+)$")

    links = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0]
        match = pattern.match(href)
        if not match:
            continue

        uri = match.group(1)

        # Skip session/contents pages, keep actual talk pages.
        if uri.endswith("-session") or uri.endswith("/"):
            continue

        title = clean_text(a.get_text(" ", strip=True))
        links[uri] = title

    result = sorted(links.items())
    print(f"Found {len(result)} possible Hinckley talk pages.")
    return result


def find_audio_urls(obj):
    """Recursively walk API JSON and collect every mp3/m4a URL in it."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            if re.search(r"\.(?:mp3|m4a)(?:\?|$)", node, re.IGNORECASE):
                url = node
                if url.startswith("//"):
                    url = "https:" + url
                if url.startswith("http") and url not in found:
                    found.append(url)

    walk(obj)

    # Prefer Church-owned media domains.
    preferred = [
        u for u in found
        if any(
            d in urlparse(u).netloc.lower()
            for d in ("ldscdn.org", "churchofjesuschrist.org", "churchofjesuschrist.net")
        )
    ]
    return preferred or found


def find_first_string(obj, key_names):
    """Recursively find the first non-empty string value under any of the
    given key names in nested API JSON."""

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in key_names and isinstance(value, str) and value.strip():
                    return value
                result = walk(value)
                if result:
                    return result
        elif isinstance(node, list):
            for value in node:
                result = walk(value)
                if result:
                    return result
        return None

    return walk(obj)


def extract_transcript_html(api_data, uri):
    """Get the transcript HTML, preferring the API body, falling back to
    scraping the public page."""
    body = find_first_string(api_data, {"body", "content", "html"})

    if body and "<" in body:
        transcript_html = body
    else:
        # Fallback: scrape the public page.
        page_html = fetch_text(f"{BASE}/study{uri}?lang=eng")
        soup = BeautifulSoup(page_html, "html.parser")
        article = soup.find("article") or soup.find("main")
        if article:
            transcript_html = article.decode_contents()
        else:
            meta = soup.find("meta", attrs={"name": "description"})
            desc = meta.get("content", "") if meta else ""
            transcript_html = f"<p>{html.escape(desc)}</p>"

    # Strip scripts/nav junk and resolve relative links.
    transcript_soup = BeautifulSoup(transcript_html, "html.parser")
    for tag in transcript_soup.find_all(["script", "style", "nav", "form", "button", "video", "audio", "iframe"]):
        tag.decompose()
    for a in transcript_soup.find_all("a", href=True):
        a["href"] = urljoin(BASE, a["href"])
    for img in transcript_soup.find_all("img", src=True):
        img["src"] = urljoin(BASE, img["src"])

    return str(transcript_soup).strip()


def extract_metadata(uri, fallback_title):
    print(f"Processing: {uri}")

    api_data = fetch_json(API_URL, params={"lang": "eng", "uri": uri})

    # Title
    title = None
    meta = api_data.get("meta") if isinstance(api_data, dict) else None
    if isinstance(meta, dict):
        title = meta.get("title")
    if not title:
        title = find_first_string(api_data, {"title"})
    title = clean_text(title) or fallback_title or "Gordon B. Hinckley"

    # Audio
    audio_urls = find_audio_urls(api_data)
    audio_url = audio_urls[0] if audio_urls else None
    if not audio_url:
        print(f"  WARNING: No audio URL in API response for {uri}")
        # Diagnostic: show what keys the API actually returned so a future
        # structure change is easy to debug from the Actions log.
        if isinstance(api_data, dict):
            print(f"  API top-level keys: {sorted(api_data.keys())}")
            if isinstance(meta, dict):
                print(f"  meta keys: {sorted(meta.keys())}")

    # Date from URI (YYYY/MM). tzinfo=utc fixes the usegmt error.
    match = re.search(r"/general-conference/(\d{4})/(0[1-9]|1[0-2])/", uri)
    if match:
        pub_dt = datetime(int(match.group(1)), int(match.group(2)), 1, tzinfo=timezone.utc)
    else:
        pub_dt = datetime(1970, 1, 1, tzinfo=timezone.utc)

    conference = (
        f"{'April' if pub_dt.month == 4 else 'October'} "
        f"{pub_dt.year} General Conference"
    )

    # Transcript
    transcript_html = extract_transcript_html(api_data, uri)
    if MAX_DESCRIPTION_CHARS:
        text_only = clean_text(BeautifulSoup(transcript_html, "html.parser").get_text(" ", strip=True))
        if len(text_only) > MAX_DESCRIPTION_CHARS:
            transcript_html = f"<p>{html.escape(text_only[:MAX_DESCRIPTION_CHARS])}…</p>"

    canonical_url = f"{BASE}/study{uri}?lang=eng"

    return {
        "title": title,
        "url": canonical_url,
        "conference": conference,
        "pub_dt": pub_dt,
        "pub_date": format_datetime(pub_dt, usegmt=True),
        "description": transcript_html,
        "audio": audio_url,
        "guid": hashlib.sha256(canonical_url.encode("utf-8")).hexdigest(),
    }


def probe_enclosure(url):
    """HEAD the audio URL to validate it and get its byte length."""
    try:
        response = session.head(url, timeout=30, allow_redirects=True)
        if response.status_code == 200:
            length = response.headers.get("Content-Length")
            ctype = response.headers.get("Content-Type", "audio/mpeg")
            return int(length) if length and length.isdigit() else 0, ctype
        print(f"  WARNING: enclosure HEAD returned {response.status_code}: {url}")
    except Exception as exc:  # noqa: BLE001
        print(f"  WARNING: enclosure HEAD failed ({exc}): {url}")
    return 0, "audio/mpeg"


def xml_escape(value):
    return html.escape(str(value), quote=True)


def make_item(data):
    description = (
        f"<p><strong>{xml_escape(data['conference'])}</strong></p>"
        f"<p>Gordon B. Hinckley</p>"
        f"{data['description']}"
        f'<p><a href="{xml_escape(data["url"])}">'
        f"Original talk on ChurchofJesusChrist.org</a></p>"
    )

    return f"""
    <item>
      <title>{xml_escape(data["title"])} ({xml_escape(data["conference"])})</title>
      <itunes:author>Gordon B. Hinckley</itunes:author>
      <description><![CDATA[{description}]]></description>
      <pubDate>{data["pub_date"]}</pubDate>
      <guid isPermaLink="false">{data["guid"]}</guid>
      <link>{xml_escape(data["url"])}</link>
      <enclosure url="{xml_escape(data["audio"])}" length="{data["length"]}" type="{xml_escape(data["mime"])}"/>
      <itunes:episodeType>full</itunes:episodeType>
    </item>
    """


def build_feed(episodes):
    items = "\n".join(make_item(ep) for ep in episodes)

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">

  <channel>
    <title>Gordon B. Hinckley — General Conference Talks</title>
    <link>{xml_escape(ARCHIVE_URL)}</link>
    <description><![CDATA[
      General Conference talks by President Gordon B. Hinckley,
      compiled from the official archive of The Church of Jesus Christ
      of Latter-day Saints. Includes the full transcript and links to
      the original Church-hosted audio.
    ]]></description>
    <language>en-us</language>
    <itunes:author>Gordon B. Hinckley</itunes:author>
    <itunes:owner>
      <itunes:name>Gordon B. Hinckley Archive</itunes:name>
    </itunes:owner>
    <itunes:explicit>false</itunes:explicit>
    <itunes:type>episodic</itunes:type>
    <itunes:image href="{xml_escape(PAGES_URL + '/art-gordon-b-hinckley.jpg')}"/>

    {items}

  </channel>
</rss>
"""

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(rss)


def main():
    links = get_archive_links()

    episodes = []
    missing_audio = []

    for uri, title in links:
        try:
            data = extract_metadata(uri, title)
            if not data["audio"]:
                missing_audio.append((title, uri))
                continue
            episodes.append(data)
            time.sleep(SLEEP)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR processing {uri}: {exc}")

    # Deduplicate and sort chronologically, oldest first.
    episodes = list({ep["guid"]: ep for ep in episodes}.values())
    episodes.sort(key=lambda x: x["pub_dt"])

    print(f"\nSuccessfully collected {len(episodes)} audio talks.")
    if missing_audio:
        print(f"Skipped {len(missing_audio)} pages with no audio:")
        for title, uri in missing_audio[:20]:
            print(f"  - {title}: {uri}")

    if not episodes:
        raise RuntimeError(
            "No Hinckley audio episodes were found. "
            "The Church's content API structure may have changed — "
            "check the 'API top-level keys' diagnostics above."
        )

    print("\nFirst five audio URLs:")
    for episode in episodes[:5]:
        print(f"  {episode['title']}: {episode['audio']}")

    # Validate enclosures + get byte lengths (best effort).
    print("\nProbing enclosure URLs...")
    for episode in episodes:
        episode["length"], episode["mime"] = probe_enclosure(episode["audio"])
        time.sleep(SLEEP / 2)

    dead = [ep for ep in episodes if ep["length"] == 0]
    if dead:
        print(f"\nNOTE: {len(dead)} enclosures did not return a Content-Length "
              "(they may still play — test one in a browser).")

    build_feed(episodes)
    print(f"\nCreated {OUTPUT_FILE} with {len(episodes)} episodes.")


if __name__ == "__main__":
    main()
