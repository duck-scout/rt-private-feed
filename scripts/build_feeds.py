import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
import hashlib
import re
import json
import os

BASE = "https://www.rt.com"
HEADERS = {"User-Agent": "Mozilla/5.0"}

SHOWS = {
    "america-first": "America First",
    "east-meets-west-with-olga-and-tara": "East Meets West",
    "crosstalk": "CrossTalk"
}

WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # optional

def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.text
    except:
        return None

def get_episode_links(slug):
    html = fetch(f"{BASE}/shows/{slug}/")
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    links = []

    for a in soup.find_all("a", href=True):
        href = a["href"]

        if not href.startswith(f"/shows/{slug}/"):
            continue
        if not re.search(r"/\d+-", href):
            continue

        links.append(BASE + href)

    return list(set(links))


def extract_episode_data(url):
    html = fetch(url)
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Title
    title_tag = soup.find("h1")
    title = title_tag.get_text(strip=True) if title_tag else "Episode"

    # Full description (better)
    article = soup.find("div", class_="article__text")
    if article:
        description = article.get_text(separator="\n").strip()
    else:
        desc_tag = soup.find("meta", {"name": "description"})
        description = desc_tag["content"] if desc_tag else ""

    # Publish date (best effort)
    time_tag = soup.find("time")
    if time_tag and time_tag.get("datetime"):
        dt = datetime.fromisoformat(time_tag["datetime"].replace("Z", "+00:00"))
        pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
    else:
        pub_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")

    # MP3
    mp3_match = re.search(r'https://mf\.b37mrtl\.ru/files/.*?\.mp3', html)
    mp3 = mp3_match.group(0) if mp3_match else None

    # Duration (sometimes embedded)
    duration_match = re.search(r'"duration":"(\d+)"', html)
    duration = duration_match.group(1) if duration_match else None

    # Artwork
    art_tag = soup.find("meta", property="og:image")
    artwork = art_tag["content"] if art_tag else None

    # Chapters (if timestamps exist)
    chapters = []
    for match in re.findall(r'(\d{1,2}:\d{2}(?::\d{2})?)\s+(.*)', description):
        time_str, title_text = match
        parts = list(map(int, time_str.split(":")))
        seconds = parts[-1] + (parts[-2] * 60) + (parts[-3] * 3600 if len(parts) == 3 else 0)

        chapters.append({
            "startTime": seconds,
            "title": title_text.strip()
        })

    guid = hashlib.md5(url.encode()).hexdigest()

    return {
        "title": title,
        "description": description,
        "pubDate": pub_date,
        "mp3": mp3,
        "artwork": artwork,
        "guid": guid,
        "duration": duration,
        "chapters": chapters
    }


def build_rss(slug, name):
    raw_links = get_episode_links(slug)

    episode_data = []
    for ep in raw_links:
        data = extract_episode_data(ep)
        if data and data["mp3"]:
            episode_data.append(data)

    # Deduplicate
    unique = {ep["guid"]: ep for ep in episode_data}
    episode_data = list(unique.values())

    # Sort newest first
    def parse_date(d):
        return datetime.strptime(d, "%a, %d %b %Y %H:%M:%S GMT")

    episode_data.sort(key=lambda x: parse_date(x["pubDate"]), reverse=True)

    # Limit to latest 10
    episode_data = episode_data[:10]

    items = ""

    for data in episode_data:
        art_xml = f'<itunes:image href="{data["artwork"]}"/>' if data["artwork"] else ""
        duration_xml = f"<itunes:duration>{data['duration']}</itunes:duration>" if data["duration"] else ""

        chapter_xml = ""
        if data["chapters"]:
            chapter_file = f"chapters-{data['guid']}.json"
            with open(chapter_file, "w") as f:
                json.dump({"version": "1.2.0", "chapters": data["chapters"]}, f)

            chapter_xml = f'<podcast:chapters url="https://duck-scout.github.io/rt-private-feed/{chapter_file}" type="application/json"/>'

        items += f"""
        <item>
            <title>{data['title']}</title>
            <description><![CDATA[{data['description']}]]></description>
            <pubDate>{data['pubDate']}</pubDate>
            <guid>{data['guid']}</guid>
            <enclosure url="{data['mp3']}" type="audio/mpeg"/>
            {duration_xml}
            {art_xml}
            {chapter_xml}
        </item>
        """

    art_url = f"https://duck-scout.github.io/rt-private-feed/art-{slug}.jpg"

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <title>{name} - Private Feed</title>
    <link>{BASE}/shows/{slug}/</link>
    <description>Auto-generated private feed for {name}</description>
    <language>en-us</language>
    <itunes:author>RT</itunes:author>
    <itunes:explicit>false</itunes:explicit>
    <itunes:type>episodic</itunes:type>
    <itunes:image href="{art_url}"/>
    {items}
  </channel>
</rss>
"""

    with open(f"feed-{slug}.xml", "w", encoding="utf-8") as f:
        f.write(rss)

    # Optional webhook notification
    if WEBHOOK_URL:
        try:
            requests.post(WEBHOOK_URL, json={"show": name, "episodes": len(episode_data)})
        except:
            pass


def main():
    for slug, name in SHOWS.items():
        build_rss(slug, name)


if __name__ == "__main__":
    main()
