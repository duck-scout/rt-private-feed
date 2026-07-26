import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from email.utils import format_datetime
import hashlib
import html
import json
import os
import re
import time
from urllib.parse import urljoin, urlparse

BASE = "https://www.churchofjesuschrist.org"
ARCHIVE_URL = (
    "https://www.churchofjesuschrist.org/study/general-conference/"
    "speakers/gordon-b-hinckley?lang=eng"
)

PAGES_URL = "https://duck-scout.github.io/rt-private-feed"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; HinckleyPodcastArchive/1.0; "
        "+https://github.com/duck-scout/rt-private-feed)"
    )
}

OUTPUT_FILE = "feed-gordon-b-hinckley.xml"

session = requests.Session()
session.headers.update(HEADERS)


def fetch(url, timeout=30):
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return response.text


def clean_text(value):
    if not value:
        return ""
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def absolute_url(url):
    return urljoin(BASE, url)


def get_archive_links():
    print("Fetching Hinckley speaker archive...")
    page = fetch(ARCHIVE_URL)
    soup = BeautifulSoup(page, "html.parser")

    links = {}

    pattern = re.compile(
        r"^/study/general-conference/\d{4}/"
        r"(?:0[1-9]|1[0-2])/"
    )

    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0]

        if not pattern.match(href):
            continue

        # Ignore session/navigation pages.
        if href.endswith("-session") or href.endswith("/"):
            continue

        full = absolute_url(href)
        title = clean_text(a.get_text(" ", strip=True))

        if title and full:
            links[full] = title

    result = list(links.items())

    print(f"Found {len(result)} possible Hinckley talk pages.")

    return result


def extract_audio_urls(page_html):
    candidates = []

    # Direct MP3/M4A URLs.
    patterns = [
        r'https?://[^"\'>\s]+?\.(?:mp3|m4a)(?:\?[^"\'>\s]*)?',
        r'//[^"\'>\s]+?\.(?:mp3|m4a)(?:\?[^"\'>\s]*)?',
    ]

    for pattern in patterns:
        for match in re.findall(pattern, page_html, re.IGNORECASE):
            url = match

            if url.startswith("//"):
                url = "https:" + url

            url = html.unescape(url)
            url = url.replace("\\/", "/")

            if url not in candidates:
                candidates.append(url)

    # Look for escaped URLs inside JSON.
    decoded = page_html.replace("\\u002F", "/").replace("\\/", "/")

    for match in re.findall(
        r'https?://[^"\'>\s]+?\.(?:mp3|m4a)(?:\?[^"\'>\s]*)?',
        decoded,
        re.IGNORECASE,
    ):
        if match not in candidates:
            candidates.append(match)

    # Prefer Church-owned media domains.
    preferred = []

    for url in candidates:
        host = urlparse(url).netloc.lower()

        if (
            "ldscdn.org" in host
            or "churchofjesuschrist.org" in host
            or "churchofjesuschrist.net" in host
        ):
            preferred.append(url)

    if preferred:
        return preferred

    return candidates


def extract_duration(soup, page_html):
    # Current Church page displays durations such as "19:05".
    text = soup.get_text(" ", strip=True)

    # Search near the beginning first to avoid accidentally
    # finding scripture timestamps later in the transcript.
    beginning = text[:5000]

    matches = re.findall(
        r"(?<!\d)(\d{1,2}):([0-5]\d)(?!\d)",
        beginning
    )

    if matches:
        minutes, seconds = matches[0]
        return f"{int(minutes)}:{seconds}"

    # JSON-style duration values, if present.
    duration_match = re.search(
        r'"duration"\s*:\s*"?(?:(\d+):)?(\d{1,3})(?::(\d{2}))?"?',
        page_html,
        re.IGNORECASE,
    )

    if duration_match:
        groups = duration_match.groups()

        if groups[2] is not None:
            hours = int(groups[1])
            minutes = int(groups[0] or 0)
            seconds = int(groups[2])
            return f"{hours}:{minutes:02d}:{seconds:02d}"

    return None


def extract_metadata(url, fallback_title):
    print(f"Processing: {url}")

    page_html = fetch(url)
    soup = BeautifulSoup(page_html, "html.parser")

    # Title
    h1 = soup.find("h1")
    title = clean_text(h1.get_text(" ", strip=True)) if h1 else fallback_title

    if not title:
        title = fallback_title or "Gordon B. Hinckley"

    # Description / transcript.
    #
    # The Church's current pages expose the entire transcript in the HTML.
    # We keep it as HTML so the podcast client has useful formatting.
    transcript_container = None

    possible_classes = [
        "article-content",
        "article__body",
        "body-block",
        "study-content",
    ]

    for class_name in possible_classes:
        transcript_container = soup.find(
            class_=re.compile(re.escape(class_name), re.IGNORECASE)
        )
        if transcript_container:
            break

    if transcript_container:
        transcript_html = transcript_container.decode_contents()
    else:
        # Fallback: find the main article.
        article = soup.find("article")

        if article:
            transcript_html = article.decode_contents()
        else:
            # Final fallback to the meta description.
            meta = soup.find("meta", attrs={"name": "description"})
            desc = meta.get("content", "") if meta else ""
            transcript_html = f"<p>{html.escape(desc)}</p>"

    # Remove obvious navigation/script junk.
    transcript_soup = BeautifulSoup(transcript_html, "html.parser")

    for tag in transcript_soup.find_all(
        ["script", "style", "nav", "form", "button"]
    ):
        tag.decompose()

    transcript_html = str(transcript_soup).strip()

    # Date from URL. Church conference URLs are YYYY/MM.
    match = re.search(
        r"/general-conference/(\d{4})/(0[1-9]|1[0-2])/",
        url
    )

    if match:
        year = int(match.group(1))
        month = int(match.group(2))
        day = 1

        # General Conference dates are approximately:
        # April = first Saturday of April
        # October = first Saturday of October.
        # We use the first day of the month for feed sorting only,
        # then preserve the conference label in the description.
        pub_dt = datetime(year, month, day, tzinfo=timezone.utc)
    else:
        pub_dt = datetime(1970, 1, 1)

    # Conference label.
    conference = (
        f"{'April' if pub_dt.month == 4 else 'October'} "
        f"{pub_dt.year} General Conference"
    )

    # Duration.
    duration = extract_duration(soup, page_html)

    # Artwork.
    og_image = soup.find("meta", property="og:image")
    artwork = og_image.get("content") if og_image else None

    # Audio.
    audio_urls = extract_audio_urls(page_html)

    audio_url = audio_urls[0] if audio_urls else None

    if not audio_url:
        print("  WARNING: No audio URL found.")

    # GUID based on canonical Church URL.
    guid = hashlib.sha256(url.encode("utf-8")).hexdigest()

    # Plain text version for description fallback/search.
    transcript_text = clean_text(
        transcript_soup.get_text(" ", strip=True)
    )

    return {
        "title": title,
        "url": url,
        "conference": conference,
        "pub_dt": pub_dt,
        "pub_date": format_datetime(pub_dt, usegmt=True),
        "description": transcript_html,
        "transcript_text": transcript_text,
        "duration": duration,
        "audio": audio_url,
        "artwork": artwork,
        "guid": guid,
    }


def duration_to_seconds(duration):
    if not duration:
        return None

    parts = duration.split(":")

    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])

        if len(parts) == 3:
            return (
                int(parts[0]) * 3600
                + int(parts[1]) * 60
                + int(parts[2])
            )
    except ValueError:
        pass

    return None


def xml_escape(value):
    return html.escape(str(value), quote=True)


def make_item(data):
    duration_xml = ""

    if data["duration"]:
        duration_xml = (
            f"<itunes:duration>"
            f"{xml_escape(data['duration'])}"
            f"</itunes:duration>"
        )

    artwork_xml = ""

    if data["artwork"]:
        artwork_xml = (
            f'<itunes:image href="{xml_escape(data["artwork"])}"/>'
        )

    # Include a short metadata header before the transcript.
    description = (
        f"<p><strong>{xml_escape(data['conference'])}</strong></p>"
        f"<p>Gordon B. Hinckley</p>"
        f"{data['description']}"
        f'<p><a href="{xml_escape(data["url"])}">'
        f"Original talk on ChurchofJesusChrist.org</a></p>"
    )

    return f"""
    <item>
      <title>{xml_escape(data["title"])}</title>
      <itunes:author>Gordon B. Hinckley</itunes:author>
      <description><![CDATA[{description}]]></description>
      <pubDate>{data["pub_date"]}</pubDate>
      <guid isPermaLink="false">{data["guid"]}</guid>
      <link>{xml_escape(data["url"])}</link>
      <enclosure
        url="{xml_escape(data["audio"])}"
        type="audio/mpeg"
      />
      {duration_xml}
      {artwork_xml}
      <itunes:episodeType>full</itunes:episodeType>
    </item>
    """


def build_feed(episodes):
    # Use the first available Church artwork as the feed artwork.
    channel_art = None

    for episode in episodes:
        if episode.get("artwork"):
            channel_art = episode["artwork"]
            break

    if not channel_art:
        channel_art = (
            f"{PAGES_URL}/art-gordon-b-hinckley.jpg"
        )

    items = "\n".join(make_item(ep) for ep in episodes)

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">

  <channel>
    <title>Gordon B. Hinckley — General Conference Talks</title>

    <link>
      https://www.churchofjesuschrist.org/study/general-conference/
      speakers/gordon-b-hinckley?lang=eng
    </link>

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
    <itunes:image href="{xml_escape(channel_art)}"/>

    {items}

  </channel>
</rss>
"""

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(rss)


def main():
    links = get_archive_links()

    episodes = []

    for url, title in links:
        try:
            data = extract_metadata(url, title)

            if not data["audio"]:
                print(f"SKIPPING — no audio: {url}")
                continue

            episodes.append(data)

            # Be polite to the Church's servers.
            time.sleep(0.25)

        except Exception as exc:
            print(f"ERROR processing {url}: {exc}")

    # Deduplicate.
    unique = {}

    for episode in episodes:
        unique[episode["guid"]] = episode

    episodes = list(unique.values())

    # Sort chronologically, oldest first.
    episodes.sort(key=lambda x: x["pub_dt"])

    print(f"Successfully collected {len(episodes)} audio talks.")

    if not episodes:
        raise RuntimeError(
            "No Hinckley audio episodes were found. "
            "The Church's audio URL structure may have changed."
        )

    # Print useful diagnostics.
    print("\nFirst five audio URLs:")
    for episode in episodes[:5]:
        print(
            f"  {episode['title']}: "
            f"{episode['audio']}"
        )

    build_feed(episodes)

    print(f"\nCreated {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
