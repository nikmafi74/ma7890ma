import asyncio
import html
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from twitter_telegram_bot import (
    TELEGRAM_CAPTION_LIMIT,
    TELEGRAM_TEXT_LIMIT,
    RSS_SOURCES,
    PUBLIC_RSSHUB_BASE_URL,
    _mirror_mark_failure,
    _mirror_mark_success,
    mirror_health,
    build_content,
    build_message,
    build_rsshub_user_feed_url,
    deliver,
    escape_and_truncate,
    extract_author_username,
    extract_id,
    extract_image_url,
    fetch_feed,
    _fetch_x_api_feed,
    _try_feed_instance,
    _x_api_entries_from_payload,
    is_retweet,
    probe_rsshub_instance,
    process_user,
    run_translation_backfill,
    translate_text,
    translations_cache,
    utf16_len,
)


class TestTweetBot(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        translations_cache.clear()
        mirror_health.clear()

    def test_utf16_len(self):
        self.assertEqual(utf16_len("hello"), 5)
        # Emojis like 🦁 and 🔔 are surrogate pairs in UTF-16 (2 code units each)
        self.assertEqual(utf16_len("🦁"), 2)
        self.assertEqual(utf16_len("🔔"), 2)

    def test_escape_and_truncate(self):
        text = "Hello <world> & friends" * 20
        budget = 50
        res = escape_and_truncate(text, budget)
        self.assertLessEqual(utf16_len(res), budget)
        self.assertTrue(res.endswith("…"))
        # Must be valid HTML escaped
        self.assertIn("&lt;world&gt;", res)
        self.assertIn("&amp;", res)

    def test_truncation_shortens_both_original_and_translation(self):
        """When caption is too long, BOTH original and translation must be shortened with ellipsis."""
        c = {
            "username": "user1",
            "title": "Alpha " * 150,
            "translation": "بتا " * 150,
            "img_url": "https://pbs.twimg.com/media/pic.jpg",
            "link": "https://x.com/i/status/999",
        }
        caption = build_message(c, max_len=TELEGRAM_CAPTION_LIMIT)
        self.assertLessEqual(utf16_len(caption), TELEGRAM_CAPTION_LIMIT)
        # Check that both sections have ellipsis indicating truncation
        self.assertIn("Alpha", caption)
        self.assertIn("بتا", caption)
        self.assertEqual(caption.count("…"), 2)
        # Check valid HTML tags are all properly closed
        self.assertEqual(caption.count("<b>"), caption.count("</b>"))
        self.assertEqual(caption.count("<blockquote expandable>"), caption.count("</blockquote>"))
        self.assertEqual(caption.count("<i>"), caption.count("</i>"))

    def test_quote_tweet_not_filtered(self):
        """Quote tweet where link matches username should not be flagged as retweet."""
        entry = {
            "title": "Check out this amazing launch https://x.com/nasa/status/999",
            "link": "https://nitter.cz/elonmusk/status/1111",
            "description": "Check out this amazing launch",
        }
        self.assertFalse(is_retweet(entry, "elonmusk"))

    def test_single_card_without_translation(self):
        """When translation is empty, card contains only original text without pending notice."""
        c = {
            "username": "vitalikbuterin",
            "title": "Ethereum upgrade is live",
            "translation": "",
            "img_url": None,
            "link": "https://x.com/i/status/456",
        }
        msg = build_message(c, max_len=TELEGRAM_TEXT_LIMIT)
        self.assertIn("NEW UPDATE | @VITALIKBUTERIN", msg)
        self.assertIn("Original:", msg)
        self.assertNotIn("ترجمه فارسی:", msg)
        self.assertNotIn("بعداً ارسال می‌کند", msg)

    async def test_deliver_sends_only_one_message_with_photo(self):
        """When tweet has photo, deliver sends exactly 1 send_photo call, never send_message."""
        bot = MagicMock()
        bot.send_photo = AsyncMock()
        bot.send_message = AsyncMock()

        c = {
            "tid": "12345",
            "username": "testuser",
            "title": "A" * 1500,  # Long text
            "translation": "ت" * 1500,
            "img_url": "https://pbs.twimg.com/media/pic.jpg",
            "link": "https://x.com/i/status/12345",
        }

        with patch("twitter_telegram_bot.db") as mock_db:
            mock_db.is_duplicate.return_value = False
            await deliver(c, ["1111"], bot, force=True)

        self.assertEqual(bot.send_photo.call_count, 1)
        self.assertEqual(bot.send_message.call_count, 0)
        # Verify caption passed to send_photo is <= 1024
        call_kwargs = bot.send_photo.call_args.kwargs
        self.assertLessEqual(utf16_len(call_kwargs["caption"]), 1024)

    async def test_deliver_falls_back_to_single_text_if_photo_fails(self):
        """If send_photo fails, fall back to exactly 1 send_message."""
        bot = MagicMock()
        bot.send_photo = AsyncMock(side_effect=Exception("Failed to download image"))
        bot.send_message = AsyncMock()

        c = {
            "tid": "12345",
            "username": "testuser",
            "title": "A tweet",
            "translation": "یک توییت",
            "img_url": "https://pbs.twimg.com/media/broken.jpg",
            "link": "https://x.com/i/status/12345",
        }

        with patch("twitter_telegram_bot.db") as mock_db:
            mock_db.is_duplicate.return_value = False
            await deliver(c, ["1111"], bot, force=True)

        self.assertEqual(bot.send_photo.call_count, 1)
        self.assertEqual(bot.send_message.call_count, 1)

    def test_is_retweet_filtering(self):
        """All variations of retweets/reposts must be filtered."""
        # 1. RT by @username
        self.assertTrue(is_retweet({"title": "RT by @elonmusk: NASA Artemis launch"}, "elonmusk"))
        # 2. RT by username (no @)
        self.assertTrue(is_retweet({"title": "RT by elonmusk: NASA Artemis launch"}, "elonmusk"))
        # 3. RT @nasa
        self.assertTrue(is_retweet({"title": "RT @nasa: Launching now"}, "elonmusk"))
        # 4. RT: @nasa
        self.assertTrue(is_retweet({"title": "RT: @nasa Launching now"}, "elonmusk"))
        # 5. [RT]
        self.assertTrue(is_retweet({"title": "[RT] Launching now"}, "elonmusk"))
        # 6. Retweeted by @...
        self.assertTrue(is_retweet({"title": "Retweeted by @elonmusk: Launching"}, "elonmusk"))
        # 7. Reposted by @...
        self.assertTrue(is_retweet({"title": "Reposted by @elonmusk: Launching"}, "elonmusk"))
        # 8. HTML retweet-header
        self.assertTrue(
            is_retweet(
                {"title": "Some tweet", "description": '<div class="retweet-header">Retweeted</div>'},
                "elonmusk",
            )
        )
        # 9. Tags
        self.assertTrue(is_retweet({"title": "Some tweet", "tags": [{"term": "retweet"}]}, "elonmusk"))
        # 10. Link username mismatch
        self.assertTrue(
            is_retweet(
                {"title": "Great news", "link": "https://nitter.cz/nasa/status/98765"},
                "elonmusk",
            )
        )
        # 11. Author mismatch
        self.assertTrue(is_retweet({"title": "Great news", "author": "@nasa"}, "elonmusk"))

        # Normal original tweet must NOT be filtered
        self.assertFalse(
            is_retweet(
                {"title": "Working on Falcon Heavy today", "link": "https://nitter.cz/elonmusk/status/123"},
                "elonmusk",
            )
        )
        self.assertFalse(
            is_retweet(
                {"title": "I will start at noon", "link": "https://nitter.cz/elonmusk/status/123"},
                "elonmusk",
            )
        )

    def test_display_name_is_not_mistaken_for_a_handle(self):
        """A display name such as "RippleX" must never make a real tweet look like a retweet."""
        # <dc:creator> holds the fullname on many mirrors: no spaces, looks exactly like a handle.
        self.assertEqual(extract_author_username({"author": "RippleX"}), ("ripplex", False))
        entry = {
            "title": "X Money support is live",
            "link": "https://nitter.cz/RippleSupport/status/1234",
            "author": "RippleX",
        }
        self.assertFalse(is_retweet(entry, "ripplesupport"))
        # Without a usable status link, a guessed handle is still not evidence of a repost.
        self.assertFalse(is_retweet({"title": "Hello", "author": "RippleX"}, "ripplesupport"))
        # Full names with spaces are not even reported as hints.
        self.assertEqual(extract_author_username({"author": "Ripple Support"}), ("", False))

    def test_author_email_is_not_the_mirror_domain(self):
        """`<author>nasa@nitter.net (NASA)</author>` means @nasa, not @nitter."""
        entry = {"title": "Perseverance update", "author": "nasa@nitter.net (NASA)"}
        self.assertEqual(extract_author_username(entry), ("nasa", True))
        self.assertFalse(is_retweet(entry, "nasa"))
        self.assertTrue(is_retweet(entry, "elonmusk"))

    def test_own_status_link_wins_over_conflicting_author_field(self):
        """If the entry links to the subscribed account's own status, that tweet is theirs."""
        entry = {
            "title": "Interesting thread",
            "link": "https://nitter.cz/elonmusk/status/7",
            "author": "Elon (@nasa)",
        }
        self.assertFalse(is_retweet(entry, "elonmusk"))

    def test_explicit_foreign_handle_still_filters(self):
        """Nitter credits a repost to the original author with an explicit @handle."""
        entry = {"title": "Great launch", "link": "https://nitter.cz/i/status/8", "author": "@nasa"}
        self.assertTrue(is_retweet(entry, "elonmusk"))

    def test_quoted_block_cannot_leak_retweet_markers(self):
        """Banners inside an embedded quote/card block belong to the quoted tweet, not to us."""
        entry = {
            "title": "This take aged badly",
            "link": "https://nitter.cz/elonmusk/status/9",
            "description": (
                "This take aged badly<hr/>\n<blockquote>\n"
                '<div class="retweet-header">nasa retweeted</div>\n<p>old</p>\n</blockquote>'
            ),
        }
        self.assertFalse(is_retweet(entry, "elonmusk"))

    def test_tweet_talking_about_retweets_is_kept(self):
        """"I was retweeted by @nasa" is the tweet's own text, not a feed banner."""
        entry = {
            "title": "I was retweeted by @nasa today",
            "description": "I was retweeted by @nasa today. RT by @nasa got 3k likes.",
            "link": "https://nitter.cz/elonmusk/status/10",
        }
        self.assertFalse(is_retweet(entry, "elonmusk"))
        # The same wording on the banner line (before the text) is still a repost.
        banner = {
            "title": "RT by @nasa: Great launch",
            "description": "RT by @nasa: Great launch",
            "link": "https://nitter.cz/nasa/status/11",
        }
        self.assertTrue(is_retweet(banner, "elonmusk"))

    def test_parsed_feed_entries(self):
        """End-to-end classification of the two Nitter RSS shapes we have to support."""
        import feedparser

        def parse(creator, link, title="Hello world"):
            xml = (
                '<?xml version="1.0"?><rss version="2.0" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>ch</title>'
                f"<item><title>{title}</title><guid isPermaLink=\"false\">123</guid>"
                f"<link>{link}</link><dc:creator>{creator}</dc:creator>"
                "<description><![CDATA[<p>Hello world</p>]]></description></item></channel></rss>"
            )
            return feedparser.parse(xml).entries[0]

        # A repost: Nitter links and credits it to the original author.
        repost = parse("@nasa", "https://nitter.cz/nasa/status/123", title="RT by @ripplesupport: Hello world")
        self.assertTrue(is_retweet(repost, "ripplesupport"))
        # An original tweet whose creator field is only the display name.
        own = parse("RippleX", "https://nitter.cz/ripplesupport/status/124")
        self.assertFalse(is_retweet(own, "ripplesupport"))
        # An explicit @creator that matches the subscription is never a repost.
        self.assertFalse(is_retweet(parse("@ripplesupport", "https://nitter.cz/ripplesupport/status/125"), "ripplesupport"))

    def _rss(self, items):
        return (
            '<?xml version="1.0"?><rss version="2.0" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>ch</title>'
            + items
            + "</channel></rss>"
        )

    def test_public_rsshub_is_in_rss_fallback_sources(self):
        self.assertTrue(any(source.endswith("/twitter/user/{username}") for source in RSS_SOURCES))

    def test_rsshub_user_feed_url_builder(self):
        """A dedicated RSSHub needs no X API token; an access key rides along as ?key=."""
        # Default: no access key, trailing slash tolerated.
        self.assertEqual(
            build_rsshub_user_feed_url("http://rsshub:1200/"),
            "http://rsshub:1200/twitter/user/{username}",
        )
        # Access key must be percent-encoded so .format() templates stay injectable.
        signed = build_rsshub_user_feed_url("https://rss.example.org", "top secret/&")
        self.assertEqual(signed, "https://rss.example.org/twitter/user/{username}?key=top%20secret%2F%26")
        self.assertEqual(signed.format(username="nasa"), "https://rss.example.org/twitter/user/nasa?key=top%20secret%2F%26")
        # Explicitly empty key behaves like no key.
        self.assertEqual(
            build_rsshub_user_feed_url("http://rsshub:1200", ""),
            "http://rsshub:1200/twitter/user/{username}",
        )

    async def test_probe_rsshub_instance_reports_health(self):
        ok = MagicMock(status_code=200)
        client = MagicMock()
        client.get = AsyncMock(return_value=ok)
        with patch("twitter_telegram_bot.http", client):
            await probe_rsshub_instance()  # must not raise
        self.assertEqual(client.get.await_count, 1)
        self.assertTrue(client.get.await_args.args[0].endswith("/healthz"))

    async def test_probe_rsshub_instance_tolerates_unreachable_instance(self):
        client = MagicMock()
        client.get = AsyncMock(side_effect=ConnectionError("no route to host"))
        with patch("twitter_telegram_bot.http", client):
            await probe_rsshub_instance()  # must not raise; mirrors stay fallbacks
        self.assertEqual(client.get.await_count, 1)

    async def test_rsshub_source_carries_access_key_when_configured(self):
        """Feed fetching must hit the signed URL of a protected private RSSHub."""
        template = build_rsshub_user_feed_url("http://rsshub:1200", "s3cret")
        feed = self._rss(
            "<item><title>new post</title>"
            "<link>https://x.com/user/status/2111000000000000001</link>"
            "<description>new post</description></item>"
        )
        client = MagicMock()
        client.get = AsyncMock(return_value=MagicMock(status_code=200, text=feed, url="http://rsshub:1200/twitter/user/user?key=s3cret"))
        with patch("twitter_telegram_bot.RSS_SOURCES", [template]), \
             patch("twitter_telegram_bot.X_BEARER_TOKEN", ""), \
             patch("twitter_telegram_bot.http", client):
            entries = await fetch_feed("user")
        self.assertEqual(client.get.await_args.args[0], "http://rsshub:1200/twitter/user/user?key=s3cret")
        self.assertEqual([extract_id(e) for e in entries], ["2111000000000000001"])

    async def test_rss_requests_obey_global_concurrency_limit(self):
        active = 0
        peak = 0

        async def slow_get(url, **_kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return MagicMock(status_code=503, url=url)

        client = MagicMock()
        client.get = AsyncMock(side_effect=slow_get)
        with patch("twitter_telegram_bot.http", client), \
             patch("twitter_telegram_bot.rss_fetch_sem", asyncio.Semaphore(1)):
            await asyncio.gather(*[
                _try_feed_instance(f"https://mirror-{i}.example/rss", "user")
                for i in range(4)
            ])

        self.assertEqual(peak, 1)

    async def test_fetch_feed_merges_unique_posts_from_all_mirrors(self):
        def item(tweet_id, title):
            return (
                f"<item><title>{title}</title>"
                f"<link>https://x.com/user/status/{tweet_id}</link>"
                f"<description>{title}</description></item>"
            )

        first_feed = self._rss(
            item("2111000000000000002", "newest")
            + item("2111000000000000001", "shared")
        )
        second_feed = self._rss(
            item("2111000000000000001", "shared duplicate")
            + item("2111000000000000000", "older")
        )
        responses = {
            "https://mirror-a.example/user/rss": MagicMock(
                status_code=200, text=first_feed, url="https://mirror-a.example/user/rss"
            ),
            "https://mirror-b.example/user/rss": MagicMock(
                status_code=200, text=second_feed, url="https://mirror-b.example/user/rss"
            ),
        }

        async def get(url, **_kwargs):
            await asyncio.sleep(0.02)
            return responses[url]

        client = MagicMock()
        client.get = AsyncMock(side_effect=get)
        with patch("twitter_telegram_bot.RSS_SOURCES", [
            "https://mirror-a.example/{username}/rss",
            "https://mirror-b.example/{username}/rss",
        ]), patch("twitter_telegram_bot.X_BEARER_TOKEN", ""), \
             patch("twitter_telegram_bot.http", client):
            entries = await fetch_feed("user")

        self.assertEqual(
            [extract_id(entry) for entry in entries],
            ["2111000000000000002", "2111000000000000001", "2111000000000000000"],
        )
        self.assertEqual(len(entries), 3)
        self.assertEqual(client.get.await_count, 2)

    async def test_fetch_feed_cancels_slow_mirrors_after_first_original_feed(self):
        slow_cancelled = asyncio.Event()
        rss = self._rss(
            "<item><title>new post</title>"
            "<link>https://x.com/user/status/2111000000000000003</link>"
            "<description>new post</description></item>"
        )
        fast_response = MagicMock(
            status_code=200,
            text=rss,
            url="https://fast.example/user/rss",
        )

        async def get(url, **_kwargs):
            if "slow.example" in url:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    slow_cancelled.set()
                    raise
            return fast_response

        client = MagicMock()
        client.get = AsyncMock(side_effect=get)
        with patch("twitter_telegram_bot.RSS_SOURCES", [
            "https://fast.example/{username}/rss",
            "https://slow.example/{username}/rss",
        ]), patch("twitter_telegram_bot.X_BEARER_TOKEN", ""), \
             patch("twitter_telegram_bot.RSS_MERGE_GRACE", 0), \
             patch("twitter_telegram_bot.http", client):
            entries = await fetch_feed("user")

        self.assertEqual([extract_id(entry) for entry in entries], ["2111000000000000003"])
        self.assertTrue(slow_cancelled.is_set())

    async def test_fetch_feed_cancels_all_mirrors_at_overall_deadline(self):
        cancelled = 0

        async def get(_url, **_kwargs):
            nonlocal cancelled
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled += 1
                raise

        client = MagicMock()
        client.get = AsyncMock(side_effect=get)
        with patch("twitter_telegram_bot.RSS_SOURCES", [
            "https://slow-a.example/{username}/rss",
            "https://slow-b.example/{username}/rss",
        ]), patch("twitter_telegram_bot.X_BEARER_TOKEN", ""), \
             patch("twitter_telegram_bot.RSS_FETCH_TIMEOUT", 0.02), \
             patch("twitter_telegram_bot.http", client):
            entries = await fetch_feed("user")

        self.assertEqual(entries, [])
        self.assertEqual(cancelled, 2)

    async def test_process_user_batches_oldest_unseen_posts_without_skipping(self):
        entries = [
            {
                "id": str(tweet_id),
                "link": f"https://x.com/user/status/{tweet_id}",
                "title": f"post {tweet_id}",
            }
            for tweet_id in range(1015, 1000, -1)
        ]
        mock_db = MagicMock()
        mock_db.get_subs_for_user.return_value = ["chat"]

        async def build(_username, entry):
            return {"tid": extract_id(entry)}

        mock_deliver = AsyncMock(return_value=True)
        with patch("twitter_telegram_bot.db", mock_db), \
             patch("twitter_telegram_bot.fetch_feed", new=AsyncMock(return_value=entries)), \
             patch("twitter_telegram_bot.build_content", new=AsyncMock(side_effect=build)), \
             patch("twitter_telegram_bot.deliver", new=mock_deliver), \
             patch("twitter_telegram_bot.MAX_TWEETS_PER_CHECK", 10), \
             patch("twitter_telegram_bot.asyncio.sleep", new=AsyncMock()):
            await process_user("user", "1000", MagicMock(), asyncio.Semaphore(1))

        sent_ids = [call.args[0]["tid"] for call in mock_deliver.await_args_list]
        self.assertEqual(sent_ids, [str(tweet_id) for tweet_id in range(1001, 1011)])
        self.assertEqual(mock_db.update_last_id.call_args.args, ("user", "1010"))

    async def test_process_user_keeps_cursor_when_delivery_fails(self):
        entry = {
            "id": "1001",
            "link": "https://x.com/user/status/1001",
            "title": "post 1001",
        }
        mock_db = MagicMock()
        mock_db.get_subs_for_user.return_value = ["chat"]
        mock_deliver = AsyncMock(return_value=False)
        with patch("twitter_telegram_bot.db", mock_db), \
             patch("twitter_telegram_bot.fetch_feed", new=AsyncMock(return_value=[entry])), \
             patch("twitter_telegram_bot.build_content", new=AsyncMock(return_value={"tid": "1001"})), \
             patch("twitter_telegram_bot.deliver", new=mock_deliver), \
             patch("twitter_telegram_bot.asyncio.sleep", new=AsyncMock()):
            await process_user("user", "1000", MagicMock(), asyncio.Semaphore(1))

        mock_db.update_last_id.assert_not_called()

    async def test_fetch_feed_drops_reposts_and_keeps_originals(self):
        """A feed with a repost and an own tweet returns only the own tweet."""
        items = (
            "<item><title>RT by @ripplesupport: NASA news</title>"
            "<link>https://nitter.cz/nasa/status/1</link><dc:creator>@nasa</dc:creator>"
            "<description>NASA news</description></item>"
            "<item><title>My own news</title>"
            "<link>https://nitter.cz/ripplesupport/status/2</link><dc:creator>RippleX</dc:creator>"
            "<description>My own news</description></item>"
        )
        resp = MagicMock(status_code=200, text=self._rss(items))
        resp.url = "https://nitter.cz/ripplesupport/rss"
        client = MagicMock()
        client.get = AsyncMock(return_value=resp)
        with patch("twitter_telegram_bot.X_BEARER_TOKEN", ""), patch("twitter_telegram_bot.http", client):
            entries = await fetch_feed("ripplesupport")
        self.assertEqual([str(e.get("title")) for e in entries], ["My own news"])
        # All mirrors are queried and duplicate tweet IDs are merged.
        self.assertEqual(client.get.await_count, len(RSS_SOURCES))

    async def test_fetch_feed_discards_reposts_from_all_mirrors(self):
        """Repost-only feeds produce no original entries after checking all mirrors."""
        items = (
            "<item><title>RT by @ripplesupport: Someone else</title>"
            "<link>https://nitter.cz/nasa/status/3</link><dc:creator>@nasa</dc:creator>"
            "<description>Someone else</description></item>"
        )
        resp = MagicMock(status_code=200, text=self._rss(items))
        resp.url = "https://nitter.cz/ripplesupport/rss"
        client = MagicMock()
        client.get = AsyncMock(return_value=resp)
        with patch("twitter_telegram_bot.X_BEARER_TOKEN", ""), patch("twitter_telegram_bot.http", client):
            entries = await fetch_feed("ripplesupport")
        self.assertEqual(entries, [])
        self.assertEqual(client.get.await_count, len(RSS_SOURCES))

    def test_x_api_payload_converts_text_ids_links_and_images(self):
        payload = {
            "data": [{
                "id": "2110376229941674267",
                "text": "A new wallet update",
                "created_at": "2026-10-15T07:20:00Z",
                "attachments": {"media_keys": ["3_123"]},
            }],
            "includes": {"media": [{
                "media_key": "3_123",
                "type": "photo",
                "url": "https://pbs.twimg.com/media/example.jpg",
            }]},
        }

        entry = _x_api_entries_from_payload("slushwallet", payload)[0]
        self.assertEqual(extract_id(entry), "2110376229941674267")
        self.assertEqual(entry["title"], "A new wallet update")
        self.assertEqual(entry["link"], "https://x.com/slushwallet/status/2110376229941674267")
        self.assertEqual(extract_image_url(entry), "https://pbs.twimg.com/media/example.jpg")
        self.assertFalse(is_retweet(entry, "slushwallet"))

    async def test_fetch_x_api_feed_uses_bearer_and_excludes_replies(self):
        user_response = MagicMock(status_code=200)
        user_response.json.return_value = {"data": {"id": "12345", "username": "slushwallet"}}
        timeline_response = MagicMock(status_code=200)
        timeline_response.json.return_value = {
            "data": [{"id": "2110376229941674267", "text": "A new wallet update"}],
        }
        client = MagicMock()
        client.get = AsyncMock(side_effect=[user_response, timeline_response])

        with patch("twitter_telegram_bot.X_BEARER_TOKEN", "test-bearer"), \
             patch("twitter_telegram_bot.http", client), \
             patch("twitter_telegram_bot.x_user_id_cache", {}):
            entries = await _fetch_x_api_feed("slushwallet")

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], "2110376229941674267")
        self.assertEqual(client.get.await_count, 2)
        self.assertEqual(client.get.await_args_list[0].kwargs["headers"]["Authorization"], "Bearer test-bearer")
        self.assertEqual(client.get.await_args_list[1].kwargs["params"]["exclude"], "retweets,replies")

    async def test_fetch_feed_prefers_official_x_api_when_configured(self):
        api_entries = [{"id": "123", "title": "Recent post"}]
        with patch("twitter_telegram_bot.X_BEARER_TOKEN", "test-bearer"), \
             patch("twitter_telegram_bot._fetch_x_api_feed", new=AsyncMock(return_value=api_entries)), \
             patch("twitter_telegram_bot.http") as client:
            entries = await fetch_feed("slushwallet")
        self.assertEqual(entries, api_entries)
        client.get.assert_not_called()

    async def test_build_content_sends_without_translation_if_provider_stalls(self):
        async def slow_translation(_text):
            await asyncio.sleep(0.1)
            return "translated"

        tweet_id = "2110376229941674267"
        entry = {
            "id": tweet_id,
            "link": f"https://x.com/slushwallet/status/{tweet_id}",
            "title": "A new wallet update",
        }
        with patch("twitter_telegram_bot.TRANSLATION_DELIVERY_TIMEOUT", 0.01), \
             patch("twitter_telegram_bot.translate_text", new=AsyncMock(side_effect=slow_translation)):
            content = await build_content("slushwallet", entry)

        self.assertIsNotNone(content)
        self.assertEqual(content["title"], "A new wallet update")
        self.assertEqual(content["translation"], "")

    async def test_empty_translation_never_cached(self):
        """Empty translation results must never be added to cache."""
        with patch("twitter_telegram_bot._translate_with_requesty", new=AsyncMock(return_value="")):
            with patch("twitter_telegram_bot._translate_with_google_endpoint", new=AsyncMock(return_value="")):
                with patch("twitter_telegram_bot._translate_with_deep_translator", new=AsyncMock(return_value="")):
                    with patch("twitter_telegram_bot._translate_with_mymemory", new=AsyncMock(return_value="")):
                        with patch("twitter_telegram_bot._translate_with_deep_mymemory", new=AsyncMock(return_value="")):
                            res = await translate_text("Some text that cannot be translated")
                            self.assertEqual(res, "")
                            self.assertEqual(len(translations_cache), 0)

    async def test_successful_translation_cached(self):
        """Valid translation is cached."""
        with patch("twitter_telegram_bot.REQUESTY_API_KEY", "test_key"):
            with patch("twitter_telegram_bot._translate_with_requesty", new=AsyncMock(return_value="ترجمه تستی")):
                res = await translate_text("A test tweet")
                self.assertEqual(res, "ترجمه تستی")
                self.assertGreater(len(translations_cache), 0)
                # Second call should return from cache
                res2 = await translate_text("A test tweet")
                self.assertEqual(res2, "ترجمه تستی")

    async def test_backfill_does_not_send_telegram_message(self):
        """run_translation_backfill only updates DB and sends NO Telegram messages."""
        bot = MagicMock()
        bot.send_message = AsyncMock()
        context = MagicMock()
        context.application.bot = bot

        fake_rows = [{"id": 42, "username": "elonmusk", "title": "Tweet to backfill", "tweet_link": "https://x.com/elonmusk/status/123"}]

        with patch("twitter_telegram_bot.db") as mock_db:
            mock_db.enabled = True
            mock_db.get_tweets_missing_translation.return_value = fake_rows
            with patch("twitter_telegram_bot.translate_text", new=AsyncMock(return_value="ترجمه بک‌فیل")):
                await run_translation_backfill(context)
                mock_db.update_tweet_translation.assert_called_once_with(42, "ترجمه بک‌فیل")

        # Bot must NEVER be called during backfill
        self.assertEqual(bot.send_message.call_count, 0)

    def _item(self, tweet_id, title="hello post"):
        return (
            f"<item><title>{title}</title>"
            f"<link>https://x.com/user/status/{tweet_id}</link>"
            f"<description>{title}</description></item>"
        )

    async def test_circuit_breaker_skips_dead_mirror_after_threshold(self):
        """A mirror failing N consecutive checks is benched and not queried anymore."""
        rss = self._rss(self._item("2111000000000000001"))

        calls = []

        async def get(url, **_kwargs):
            calls.append(url)
            if "breaker-a" in url:
                return MagicMock(status_code=503, url=url)
            await asyncio.sleep(0.01)
            return MagicMock(status_code=200, text=rss, url=url)

        sources = [
            "https://breaker-a.example/{username}/rss",
            "https://breaker-b.example/{username}/rss",
        ]
        client = MagicMock()
        client.get = AsyncMock(side_effect=get)
        with patch("twitter_telegram_bot.RSS_SOURCES", sources), \
             patch("twitter_telegram_bot.X_BEARER_TOKEN", ""), \
             patch("twitter_telegram_bot.MIRROR_BREAKER_SAMPLE", 2), \
             patch("twitter_telegram_bot.MIRROR_BREAKER_COOLDOWN", 10**9), \
             patch("twitter_telegram_bot.RSS_MERGE_GRACE", 0), \
             patch("twitter_telegram_bot.http", client):
            for _ in range(3):
                await fetch_feed("user")

        # Runs 1 and 2 hit both mirrors (cooldown arms on the 2nd failure); run 3
        # must skip breaker-a entirely.
        self.assertEqual(len([u for u in calls if "breaker-a" in u]), 2)
        self.assertEqual(len([u for u in calls if "breaker-b" in u]), 3)

    async def test_circuit_breaker_never_blinds_all_mirrors(self):
        """When every mirror is benched, the soonest one is still probed."""
        calls = []

        async def get(url, **_kwargs):
            calls.append(url)
            return MagicMock(status_code=403, url=url)

        sources = [
            "https://breaker-x.example/{username}/rss",
            "https://breaker-y.example/{username}/rss",
        ]
        client = MagicMock()
        client.get = AsyncMock(side_effect=get)
        with patch("twitter_telegram_bot.RSS_SOURCES", sources), \
             patch("twitter_telegram_bot.X_BEARER_TOKEN", ""), \
             patch("twitter_telegram_bot.MIRROR_BREAKER_SAMPLE", 1), \
             patch("twitter_telegram_bot.MIRROR_BREAKER_COOLDOWN", 10**9), \
             patch("twitter_telegram_bot.http", client):
            await fetch_feed("user")
            await fetch_feed("user")

        # Run 1 probes both mirrors; run 2 probes exactly one benched mirror
        # instead of going blind. Which mirror wins the tie depends on task
        # completion order (equal monotonic timestamps on fast CI), so the
        # assertion must not pick a specific host.
        self.assertEqual(len(calls), 3)
        second_run = set(calls[2:])
        self.assertEqual(len(second_run), 1)
        self.assertTrue(
            next(iter(second_run)).startswith(("https://breaker-x.example/", "https://breaker-y.example/"))
        )
        self.assertEqual(
            len([u for u in calls if "breaker-x" in u]) + len([u for u in calls if "breaker-y" in u]),
            3,
        )

    def test_mirror_breaker_resets_on_success(self):
        """SAMPLE hard failures bench a mirror; one success clears it again."""
        with patch("twitter_telegram_bot.MIRROR_BREAKER_SAMPLE", 2), \
             patch("twitter_telegram_bot.MIRROR_BREAKER_COOLDOWN", 10**9):
            host = "https://reset.example/{username}/rss"
            _mirror_mark_failure(host)
            # Below sample: the record tracks the failure streak but the mirror is NOT benched.
            below = mirror_health["reset.example"]
            self.assertEqual(below["fails"], 1)
            self.assertEqual(below["tripped_until"], 0.0)
            _mirror_mark_failure(host)
            record = mirror_health["reset.example"]
            self.assertEqual(record["fails"], 2)
            self.assertGreater(record["tripped_until"], 0)
            _mirror_mark_success(host)
            self.assertEqual(mirror_health["reset.example"], {"fails": 0, "tripped_until": 0.0})

    def test_dedicated_rsshub_is_exempt_from_breaker(self):
        """A configured dedicated RSSHub instance is never benched."""
        with patch("twitter_telegram_bot.RSSHUB_BASE_URL", "http://rsshub.internal:1200"), \
             patch("twitter_telegram_bot.PUBLIC_RSSHUB_BASE_URL", "https://rsshub.app"), \
             patch("twitter_telegram_bot.MIRROR_BREAKER_SAMPLE", 1), \
             patch("twitter_telegram_bot.MIRROR_BREAKER_COOLDOWN", 10**9):
            _mirror_mark_failure("http://rsshub.internal:1200/twitter/user/{username}")
            _mirror_mark_failure("https://public.example/rss")
            self.assertNotIn("rsshub.internal:1200", mirror_health)
            self.assertIn("public.example", mirror_health)


if __name__ == "__main__":
    unittest.main()
