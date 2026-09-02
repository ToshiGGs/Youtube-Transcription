from __future__ import annotations

from datetime import UTC, date, datetime

from media_transcription_bot.models import PodcastEpisode
from media_transcription_bot.podcast import EpisodeTarget, _episode_score, parse_rss

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Research Desk</title>
    <item>
      <title>Market Structure Update</title>
      <guid>episode-1</guid>
      <link>https://publisher.example/episodes/market-structure</link>
      <pubDate>Fri, 21 Aug 2026 12:00:00 +0000</pubDate>
      <itunes:duration>01:02:03</itunes:duration>
      <enclosure url="https://cdn.example/episode.mp3"
                 type="audio/mpeg" length="123456" />
    </item>
  </channel>
</rss>
"""


def test_parse_rss_preserves_episode_metadata():
    feed = parse_rss(RSS, "https://publisher.example/feed.xml")
    assert feed.title == "Research Desk"
    assert len(feed.episodes) == 1
    episode = feed.episodes[0]
    assert episode.title == "Market Structure Update"
    assert episode.duration_seconds == 3723
    assert episode.enclosure_type == "audio/mpeg"
    assert episode.enclosure_length == 123456


def test_episode_scoring_prefers_matching_title_date_and_duration():
    episode = PodcastEpisode(
        title="Market Structure Update",
        feed_url="https://publisher.example/feed.xml",
        enclosure_url="https://cdn.example/episode.mp3",
        published_at=datetime(2026, 8, 21, tzinfo=UTC),
        duration_seconds=3723,
    )
    target = EpisodeTarget(
        title="Market Structure Update",
        release_date=date(2026, 8, 21),
        duration_seconds=3700,
    )
    assert _episode_score(target, episode) == 1.0
