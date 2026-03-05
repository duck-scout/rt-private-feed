import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
import hashlib
import re
import os

BASE = "https://www.rt.com"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# Add more shows here later
SHOWS = {
    "america-first": "America First",
    "east-meets-west-with-olga-and-tara": "East Meets West",
    "crosstalk": "CrossTalk"
}

def fetch(url):
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    return r.text

def get_episode_links(slug):
    url = f"{BASE}/shows/{slug}/"
    html = fetch(url)
    soup = BeautifulSoup(html, "html.parser")

    links = []

    for a in soup.find_all("a", href=True):
        href = a["href"]

        # Must be internal relative link
        if not href.startswith(f"/shows/{slug}/"):
            continue

        # Must look like episode URL with numeric ID
        if not re.search(r"/\d+-", href):
            continue

        full_url = BASE + href
        links.append(full_url)

    return list(set(links))[:15]

def extract_episode_data(url):
    html = fetch(url)

    # Title
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("title")
    title = title_tag.get_text().strip() if title_tag else "Episode"

    # Description
    desc_tag = soup.find("meta", {"name": "description"})
    description = desc_tag["content"] if desc_tag else ""

    # Publish date
    pub_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")

    # MP3
    mp3_match = re.search(r'https://mf\.b37mrtl\.ru/files/.*?\.mp3', html)
    mp3 = mp3_match.group(0) if mp3_match else None

    # Artwork
    art_tag = soup.find("meta", property="og:image")
    artwork = art_tag["content"] if art_tag else None

    return {
        "title": title,
        "description": description,
        "pubDate": pub_date,
        "mp3": mp3,
        "artwork": artwork,
        "guid": hashlib.md5(url.encode()).hexdigest()
    }

def build_rss(show_slug, show_name):
    episodes = get_episode_links(show_slug)
    items = ""

    for ep_url in episodes:
        data = extract_episode_data(ep_url)
        if not data["mp3"]:
            continue

        art_xml = f'<itunes:image href="{data["artwork"]}"/>' if data["artwork"] else ""

        items += f"""
        <item>
            <title>{data['title']}</title>
            <description><![CDATA[{data['description']}]]></description>
            <pubDate>{data['pubDate']}</pubDate>
            <guid>{data['guid']}</guid>
            <enclosure url="{data['mp3']}" type="audio/mpeg"/>
            {art_xml}
        </item>
        """

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{show_name} - Private Feed</title>
    <link>{BASE}/shows/{show_slug}/</link>
    <description>Auto-generated private feed for {show_name}</description>
    <language>en-us</language>
    {items}
  </channel>
</rss>
"""

    with open(f"feed-{show_slug}.xml", "w", encoding="utf-8") as f:
        f.write(rss)

def main():
    for slug, name in SHOWS.items():
        build_rss(slug, name)

if __name__ == "__main__":
    main()
