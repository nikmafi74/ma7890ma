import os, asyncio, logging, feedparser, re, httpx, html, hashlib, time, sys
from urllib.parse import quote, urlparse
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineQueryResultArticle, InputTextMessageContent, InputFile,
)
from telegram.ext import Application, CommandHandler, InlineQueryHandler
from telegram.constants import ParseMode
from database import Database
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import uvicorn

load_dotenv()

BASE_PATH = os.path.dirname(os.path.abspath(__file__))


def _env_value(key, fallback):
    value = os.getenv(key)
    if value is None:
        return fallback
    value = value.strip()
    # An empty variable (easy to create in a Railway UI) is "unset", not "0".
    return value if value else fallback


def _env_int(key, fallback, minimum=None):
    """Parse an integer env var without crashing boot on a typo/empty value."""
    try:
        value = int(_env_value(key, str(fallback)))
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning("%s=%r is not a valid integer; using %s", key, os.getenv(key), fallback)
        value = fallback
    return max(minimum, value) if minimum is not None else value


def _env_float(key, fallback, minimum=None):
    try:
        value = float(_env_value(key, str(fallback)))
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning("%s=%r is not a valid number; using %s", key, os.getenv(key), fallback)
        value = fallback
    return max(minimum, value) if minimum is not None else value


TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not TOKEN:
    # Fail loudly and cleanly: continuing would crash deep inside telegram polling
    # with a stack trace that hides the actual missing-variable problem.
    logging.getLogger(__name__).critical(
        "TELEGRAM_BOT_TOKEN is not set! Get one from @BotFather and set it as an environment variable."
    )
    sys.exit(1)
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "").strip()
X_API_BASE_URL = _env_value("X_API_BASE_URL", "https://api.x.com/2").rstrip("/")
X_USER_ID_CACHE_TTL = _env_int("X_USER_ID_CACHE_TTL", 86400, minimum=60)
CHECK_INTERVAL = _env_int("CHECK_INTERVAL", 300, minimum=30)
CONCURRENT_LIMIT = _env_int("CONCURRENT_LIMIT", 3, minimum=1)
HTTP_MAX_CONNECTIONS = max(50, _env_int("HTTP_MAX_CONNECTIONS", 50, minimum=50))
# Each tracked account fans out to several RSS mirrors; cap requests globally so
# concurrent accounts cannot exhaust httpx's connection pool and raise PoolTimeout.
RSS_FETCH_CONCURRENCY = _env_int("RSS_FETCH_CONCURRENCY", 12, minimum=1)
# Bound every account's mirror sweep and only wait briefly to merge other fast replies.
RSS_FETCH_TIMEOUT = _env_float("RSS_FETCH_TIMEOUT", 12, minimum=1.0)
RSS_MERGE_GRACE = _env_float("RSS_MERGE_GRACE", 0.25, minimum=0.0)
# Circuit breaker for feed mirrors: most public mirrors are permanently dead
# (DNS gone, HTTP 403/410/451). Every tracked account used to hit every dead
# mirror on every check. A mirror is benched after N CONSECUTIVE hard failures
# — N must be large enough (≫ accounts probed in one round) that a mirror
# which serves most accounts is not benched on a bad streak for a few of them
# — for a cooldown, then re-probed in case it came back.
MIRROR_BREAKER_SAMPLE = _env_int("MIRROR_BREAKER_SAMPLE", 12, minimum=2)
MIRROR_BREAKER_COOLDOWN = _env_int("MIRROR_BREAKER_COOLDOWN", 1800, minimum=60)
# HTTP 429 means "alive but throttled", NOT dead. Back such a mirror off briefly
# without feeding the death counter, so a mirror we merely rate-limited ourselves
# is never benched for half an hour and our own bursts stop making it worse.
MIRROR_RATE_LIMIT_COOLDOWN = _env_int("MIRROR_RATE_LIMIT_COOLDOWN", 120, minimum=10)
mirror_health = {}  # host -> {"fails": int, "tripped_until": monotonic ts}
# Sentinel returned by _try_feed_instance on HTTP 429 so fetch_feed can tell
# "throttled" apart from "dead".
_RATE_LIMITED = object()

PLACEHOLDER_API_KEYS = {"key", "your_key", "your_api_key", "your_gemini_api_key", "your_requesty_or_gemini_key"}

def _clean_api_key(value):
    value = (value or "").strip()
    return "" if value.lower() in PLACEHOLDER_API_KEYS else value

GEMINI_API_KEY = _clean_api_key(os.getenv("GEMINI_API_KEY"))
OPENAI_API_KEY = _clean_api_key(os.getenv("OPENAI_API_KEY"))
REQUESTY_API_KEY = _clean_api_key(os.getenv("REQUESTY_API_KEY")) or GEMINI_API_KEY or OPENAI_API_KEY

# Never send an API key to a host the user did not explicitly configure.
if os.getenv("REQUESTY_BASE_URL"):
    REQUESTY_BASE_URL = os.getenv("REQUESTY_BASE_URL", "").strip().rstrip("/")
elif GEMINI_API_KEY and REQUESTY_API_KEY == GEMINI_API_KEY:
    REQUESTY_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
elif OPENAI_API_KEY and REQUESTY_API_KEY == OPENAI_API_KEY:
    REQUESTY_BASE_URL = "https://api.openai.com/v1"
elif REQUESTY_API_KEY:
    # A bare REQUESTY_API_KEY without a base URL has no known safe default.
    REQUESTY_BASE_URL = ""
else:
    REQUESTY_BASE_URL = ""

if os.getenv("REQUESTY_MODEL"):
    REQUESTY_MODEL = os.getenv("REQUESTY_MODEL", "").strip()
elif GEMINI_API_KEY and REQUESTY_API_KEY == GEMINI_API_KEY:
    REQUESTY_MODEL = "gemini-2.0-flash"
elif OPENAI_API_KEY and REQUESTY_API_KEY == OPENAI_API_KEY:
    REQUESTY_MODEL = "gpt-4o-mini"
elif REQUESTY_API_KEY:
    REQUESTY_MODEL = ""
else:
    REQUESTY_MODEL = ""

if REQUESTY_API_KEY and not (REQUESTY_BASE_URL and REQUESTY_MODEL):
    logging.getLogger(__name__).warning(
        "REQUESTY_API_KEY set but REQUESTY_BASE_URL/REQUESTY_MODEL incomplete; "
        "AI translation disabled — set both or use GEMINI_API_KEY/OPENAI_API_KEY."
    )
    REQUESTY_API_KEY = ""
TRANSLATE_FA = _env_value("TRANSLATE_FA", "true").lower() in ("1", "true", "yes")
TRANSLATION_TIMEOUT = _env_float("TRANSLATION_TIMEOUT", 20, minimum=1.0)
TRANSLATION_DELIVERY_TIMEOUT = _env_float("TRANSLATION_DELIVERY_TIMEOUT", 15, minimum=1.0)
TRANSLATION_RETRIES = _env_int("TRANSLATION_RETRIES", 2, minimum=1)
TRANSLATION_CONCURRENT_LIMIT = _env_int("TRANSLATION_CONCURRENT_LIMIT", 1, minimum=1)
TRANSLATION_BACKFILL_LIMIT = _env_int("TRANSLATION_BACKFILL_LIMIT", 15, minimum=1)
MAX_TWEETS_PER_CHECK = _env_int("MAX_TWEETS_PER_CHECK", 10, minimum=1)
MYMEMORY_SOURCE_LANG = os.getenv("MYMEMORY_SOURCE_LANG", "").strip() or "en"
MYMEMORY_EMAIL = os.getenv("MYMEMORY_EMAIL", "").strip()

# Set LOG_LEVEL=DEBUG to see why each RSS entry was kept or skipped by the retweet filter.
LOG_LEVEL = (os.getenv("LOG_LEVEL", "INFO") or "INFO").strip().upper()
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)


class _TelegramTokenRedactionFilter(logging.Filter):
    """Keep HTTP client logs useful without exposing the bot token in request URLs."""

    _url_pattern = re.compile(r"(?i)(https?://api\.telegram\.org/bot)[^\s/\"']+")

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = self._url_pattern.sub(r"\1[REDACTED]", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


class _ConflictHintFilter(logging.Filter):
    """"terminated by other getUpdates" errors sound cryptic; append the exact cure."""

    _hint = (
        " | FIX: another bot instance is polling this token — stop the duplicate "
        "(a previous Railway deployment still draining, a second Railway service/replica, "
        "or a local `python twitter_telegram_bot.py`); the error stops once that instance quits."
    )

    def filter(self, record):
        try:
            haystack = record.getMessage()
        except Exception:
            haystack = ""
        # python-telegram logs the polling failure as a short message ("Exception
        # happened while polling for updates.") with the Conflict in exc_info's
        # traceback, so the hint must look there too.
        if record.exc_info and record.exc_info[1] is not None:
            haystack += "\n" + str(record.exc_info[1])
        if "terminated by other getUpdates" in haystack and self._hint not in record.getMessage():
            record.msg = record.getMessage() + self._hint
            record.args = ()
        return True


_token_redaction_filter = _TelegramTokenRedactionFilter()
_conflict_hint_filter = _ConflictHintFilter()
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_token_redaction_filter)
    _handler.addFilter(_conflict_hint_filter)

# httpx/httpcore log every request and protocol event at INFO/DEBUG; python-
# telegram's DEBUG logs can also include full message text and chat IDs. Keep
# those third-party logs quiet while preserving this module's own DEBUG diagnostics.
# The token filter is defense in depth for any Telegram URL that reaches a handler.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)

logger = logging.getLogger(__name__)

db = Database()
if not db.enabled:
    logger.warning("Running without database - /add, /list, dashboard disabled")
if not X_BEARER_TOKEN:
    logger.warning("X_BEARER_TOKEN is not set; tweet checks will rely on public RSSHub/RSS mirrors, which may be unavailable.")
else:
    logger.info("Official X API source is configured; public RSS is a fallback.")

templates = Jinja2Templates(directory=os.path.join(BASE_PATH, "templates"))

http: httpx.AsyncClient = None
bot_app_ref = None
translations_cache = {}
x_user_id_cache = {}
translation_sem = asyncio.Semaphore(max(1, TRANSLATION_CONCURRENT_LIMIT))
rss_fetch_sem = asyncio.Semaphore(RSS_FETCH_CONCURRENCY)

RSSHUB_BASE_URL = (os.getenv("RSSHUB_BASE_URL") or "https://rsshub.app").strip().rstrip("/")
# Shared secret for a private RSSHub instance (RSSHub side sets ACCESS_KEY to the same
# value). Every RSSHub request then carries ?key=... so strangers cannot use the instance.
RSSHUB_ACCESS_KEY = os.getenv("RSSHUB_ACCESS_KEY", "").strip()
PUBLIC_RSSHUB_BASE_URL = "https://rsshub.app"


def build_rsshub_user_feed_url(base_url=None, access_key=None):
    """Build the RSSHub user-timeline feed URL template.

    A dedicated RSSHub only needs an X account cookie (TWITTER_AUTH_TOKEN on the
    RSSHub side), never an X API Bearer Token. When the instance is protected
    with RSSHub's ACCESS_KEY, the key travels as a percent-encoded `key` query
    parameter, so the template stays safe for `.format(username=...)`.
    """
    base = (RSSHUB_BASE_URL if base_url is None else base_url or "").strip().rstrip("/")
    key = (RSSHUB_ACCESS_KEY if access_key is None else access_key or "").strip()
    url = f"{base}/twitter/user/{{username}}"
    if key:
        url += f"?key={quote(key, safe='')}"
    return url


RSS_SOURCES = [
    build_rsshub_user_feed_url(),
    "https://nitter.net/{username}/rss",
    "https://xcancel.com/{username}/rss",
    "https://nitter.perennialte.ch/{username}/rss",
    "https://nitter.privacydev.net/{username}/rss",
    "https://lightbrd.com/{username}/rss",
    "https://nitter.poast.org/{username}/rss",
    "https://nitter.cz/{username}/rss",
    "https://nitter.netbub.com/{username}/rss",
]

if RSSHUB_BASE_URL != PUBLIC_RSSHUB_BASE_URL:
    logger.info(
        "Dedicated RSSHub instance configured: %s%s",
        RSSHUB_BASE_URL,
        " (requests signed with RSSHUB_ACCESS_KEY)" if RSSHUB_ACCESS_KEY else "",
    )
else:
    logger.info(
        "No dedicated RSSHub configured; using the public instance plus RSS mirrors. "
        "Set RSSHUB_BASE_URL (and optionally RSSHUB_ACCESS_KEY) for reliable feeds."
    )

RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Helpers ───────────────────────────────────────────────────────────────

_USERNAME_RESERVED = {"i", "status", "statuses", "home", "explore", "search", "intent", "share"}

def clean_username(raw):
    raw = (raw or "").strip().lower()
    raw = raw.replace("https://", "").replace("http://", "")
    for d in ["x.com/", "twitter.com/", "nitter.net/", "xcancel.com/", "uni-sonia.com/"]:
        raw = raw.replace(d, "")
    return raw.lstrip("@").split("?")[0].split("/")[0].strip()

def is_valid_twitter(u):
    return bool(re.match(r"^[a-z0-9_]{1,15}$", u))

def extract_id(entry):
    for key in ["id", "guid", "link"]:
        val = str(entry.get(key, ""))
        m = re.search(r"status(?:es)?/(\d+)", val)
        if m: return m.group(1)
        m2 = re.search(r"(\d{17,})", val)
        if m2: return m2.group(1)
    val = str(entry.get("id", ""))
    m3 = re.search(r"(\d+)$", val)
    if m3 and len(m3.group(1)) >= 10:
        return m3.group(1)
    return None

def extract_tweet_id_from_link(link):
    value = str(link or "")
    match = re.search(r"status(?:es)?/(\d+)", value)
    if match:
        return match.group(1)
    match = re.search(r"(\d{17,})", value)
    return match.group(1) if match else None

def is_newer_tweet_id(tweet_id, last_id):
    if not last_id:
        return True
    try:
        return int(tweet_id) > int(last_id)
    except (TypeError, ValueError):
        return str(tweet_id) > str(last_id)

def clean_tweet_text(text):
    """Turn Nitter/RSS HTML-ish text into plain text before sending/translating."""
    if not text:
        return ""
    text = html.unescape(str(text))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def extract_tweet_text(entry):
    for key in ("title", "summary", "description"):
        text = clean_tweet_text(entry.get(key, ""))
        # RSS descriptions can be media-only HTML. Prefer the first field that has real text.
        if re.search(r"[\w\u0600-\u06FF]", text, flags=re.UNICODE):
            return text
    return ""

def _handle_or_empty(candidate):
    """Normalise a candidate handle, returning '' for anything that is not a valid one."""
    username = clean_username(candidate)
    if not username or username in _USERNAME_RESERVED:
        return ""
    return username if is_valid_twitter(username) else ""

def _username_from_text(value):
    """Read a handle out of a feed field such as `<author>` or `<dc:creator>`.

    Returns `(username, explicit)`. `explicit` is True only when the feed actually stated a
    handle: an `@name`, an RSS `email (name)` author (the local part is the handle, the
    domain is just the instance), or a profile/status URL.

    A lone token without `@` is never explicit: Nitter mirrors and RSS bridges routinely put
    the *display name* in these fields, and names like "RippleX" or "NASA" are
    indistinguishable from a handle. Treating such a guess as a handle made every tweet of
    those accounts look like a retweet of somebody else and get silently dropped.
    """
    value = html.unescape(str(value or "")).strip()
    if not value:
        return "", False
    # <author>nasa@nitter.net (NASA)</author>: the local part is the handle, the domain is
    # only the instance — so read addresses before looking for @mentions, otherwise the
    # mirror's hostname ("nitter", "xcancel", …) turns into a fake author.
    email_match = re.search(r"(?<![A-Za-z0-9_])([A-Za-z0-9_]{1,15})@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value)
    if email_match:
        return _handle_or_empty(email_match.group(1)), True
    mention_match = re.search(r"(?<![\w.@])@([A-Za-z0-9_]{1,15})", value)
    if mention_match:
        return _handle_or_empty(mention_match.group(1)), True
    # href-style references: https://nitter.net/nasa or .../nasa/status/123456
    for pattern in (
        r"(?:https?://[^/]+)?/([A-Za-z0-9_]{1,15})/status(?:es)?/\d+",
        r"(?:https?://[^/]+)?/([A-Za-z0-9_]{1,15})/?$",
    ):
        ref = re.search(pattern, value, flags=re.I)
        if not ref:
            continue
        username = _handle_or_empty(ref.group(1))
        if username:
            return username, True
    # Display names with spaces ("Elon Musk") carry no handle at all; a single word is kept
    # only as a weak hint, which can confirm the expected account but never accuse another.
    if re.search(r"\s", value):
        return "", False
    return _handle_or_empty(value), False

def _author_fields(entry):
    """All author-ish values a feed parser may hand us, in order of trustworthiness."""
    candidates = [entry.get("author"), entry.get("dc_creator"), entry.get("creator")]
    author_detail = entry.get("author_detail") or {}
    if isinstance(author_detail, dict):
        candidates.extend([author_detail.get("name"), author_detail.get("href"), author_detail.get("email")])
    for author in entry.get("authors", []) or []:
        if isinstance(author, dict):
            candidates.extend([author.get("name"), author.get("href"), author.get("email")])
        else:
            candidates.append(author)
    return candidates

def extract_author_username(entry):
    """Author handle of an entry as `(username, explicit)`.

    An explicit `@handle`/URL always wins over a bare display-name guess.
    """
    weak = ("", False)
    for value in _author_fields(entry):
        username, explicit = _username_from_text(value)
        if not username:
            continue
        if explicit:
            return username, True
        if not weak[0]:
            weak = (username, False)
    return weak

def extract_link_username(entry):
    for key in ("link", "id", "guid"):
        value = str(entry.get(key, "") or "")
        match = re.search(r"(?:https?://[^/]+)?/([^/?#]+)/status(?:es)?/\d+", value, flags=re.I)
        if not match:
            continue
        username = _handle_or_empty(match.group(1))
        if username:
            return username
    return None

# Quoted tweets, link cards, polls and articles are appended to the item body after an <hr>
# (Nitter additionally wraps quotes in a <blockquote>); a retweet banner inside one of those
# blocks belongs to the quoted tweet, not to the entry being filtered.
_EMBEDDED_BLOCK_RE = re.compile(r'<(?:blockquote|hr)\b|class=["\'][^"\']*\bquote\b', re.I)

def _own_item_text(entry):
    """Title + body of an entry, cut before the first embedded (someone else's) block."""
    raw = "\n".join(str(entry.get(key, "") or "") for key in ("title", "description", "summary"))
    match = _EMBEDDED_BLOCK_RE.search(raw)
    return raw if match is None else raw[: match.start()]

def retweet_reason(entry, username=None):
    """Reason an RSS entry looks like a retweet instead of a tweet by `username`, else None.

    Returning the reason, not just a bool, keeps the filter debuggable: feed mirrors disagree
    constantly, and "why was this tweet dropped" is the only question that matters once an
    account stops showing up in Telegram.
    """
    own_raw = _own_item_text(entry)
    own_lower = own_raw.lower()

    # 1. HTML markers from Nitter / RSS bridges
    if "retweet-header" in own_lower or "retweet_header" in own_lower:
        return "retweet-header markup"
    if re.search(r'class=["\'][^"\']*\bretweet\b[^"\']*["\']', own_raw, re.I):
        return "retweet css class"

    # 2. RSS tags / categories
    for tag_field in (entry.get("tags", []) or [], entry.get("categories", []) or []):
        for tag in tag_field:
            term = (tag.get("term") or tag.get("label")) if isinstance(tag, dict) else str(tag)
            if term and str(term).strip().lower() in {"rt", "retweet", "repost"}:
                return "retweet tag"

    # 3. Retweet banners. Bridges always render them on their own line, before the tweet
    # text ("RT by @nasa: ...", "nasa retweeted"), so anchor every pattern to a line start —
    # a post that merely talks about retweets ("I was retweeted by @nasa") is not one.
    clean_all = clean_tweet_text(own_raw).lower()
    banner_patterns = [
        r"^\s*rt\s+(?:by\s+)?@?[a-z0-9_]{1,15}\b",
        r"^\s*rt\s*[:\-]",
        r"^\s*[\[(]\s*rt\s*[\])]",
        r"^\s*(?:retweet|repost)(?:ed)?(?:\s+by)?\b",
        r"^\s*@?[a-z0-9_]{1,15}\s+(?:retweeted|reposted)\b",
        r"^\s*(?:@?[a-z0-9_]{1,15}\s+)?(?:retweeted|reposted)\s+by\s+@",
        r"^\s*(?:بازتوییت|ریتوییت)\b",
    ]
    if any(re.search(pat, clean_all, re.I | re.M) for pat in banner_patterns):
        return "retweet banner text"

    # 4. Ownership: whose tweet is this entry, really?
    expected = _handle_or_empty(username) if username else ""
    if not expected:
        return None
    link_username = extract_link_username(entry)
    if link_username:
        # A timeline feed links every item to that item's own status page, and on a retweet
        # Nitter links (and credits) the original author instead. So a link pointing at a
        # foreign handle is a repost, while a link pointing back at `expected` proves the
        # entry belongs to the subscribed account no matter what the display name says.
        if link_username != expected:
            return f"status link is @{link_username}"
        return None
    author_username, author_explicit = extract_author_username(entry)
    # Without a usable link, only an explicit @handle can accuse the entry; a display-name
    # guess such as "RippleX" is not evidence (see _username_from_text).
    if author_explicit and author_username != expected:
        return f"author @{author_username}"
    return None

def is_retweet(entry, username=None):
    """Detect RSS entries that are retweets/reposts rather than tweets by `username`."""
    return retweet_reason(entry, username) is not None

def extract_image_url(entry):
    """Best tweet image URL, or None.

    Avatar/profile pictures are skipped — Nitter embeds them in every entry and
    using one as the "tweet photo" looks broken in Telegram.
    """
    candidates = []

    def _ok(url):
        u = str(url or "").lower()
        return u.startswith("http") and "profile_images" not in u

    # media_content is the most reliable structured source
    for media in entry.get("media_content", []) or []:
        url = media.get("url") if isinstance(media, dict) else None
        if url and _ok(url) and ("pbs.twimg.com" in url or "media" in url):
            candidates.append(url)
    desc = entry.get("description", "") or entry.get("summary", "")
    for match in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', desc, re.I):
        raw = match.group(1)
        if "profile_images" in raw or "profile_images%2F" in raw:
            continue
        if "/pic/media%2F" in raw:
            media_id = raw.split("%2F")[-1].split("?")[0]
            candidates.append(f"https://pbs.twimg.com/media/{media_id}?format=jpg&name=large")
        elif _ok(raw) and "media" in raw.lower():
            candidates.append(raw)
    return candidates[0] if candidates else None

def _trim_translation(result):
    result = clean_tweet_text(result)
    result = re.sub(r"^(ترجمه(?:\s*فارسی)?|translation)\s*[:：-]\s*", "", result, flags=re.I)
    return result.strip(' "“”\n\r\t')

def _message_content_to_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content or "")

def _has_text_to_translate(text):
    # Avoid wasting API quota on empty/media-only tweets, links, emoji-only posts, etc.
    return bool(re.search(r"[A-Za-z\u0600-\u06FF\u0400-\u04FF\u4E00-\u9FFF]", text or "", flags=re.UNICODE))

@asynccontextmanager
async def _translation_client():
    global http
    if http is not None:
        yield http
        return
    async with httpx.AsyncClient(headers=RSS_HEADERS, timeout=TRANSLATION_TIMEOUT, follow_redirects=True) as client:
        yield client

async def _translate_with_requesty(text):
    if not REQUESTY_API_KEY:
        return ""
    base = REQUESTY_BASE_URL if re.search(r"/v\d", REQUESTY_BASE_URL) else f"{REQUESTY_BASE_URL}/v1"
    payload = {
        "model": REQUESTY_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a reliable translation engine. Translate the user's tweet to natural, "
                    "colloquial Persian. Keep crypto symbols, tickers, hashtags, usernames, URLs, "
                    "and product names in English. Return only the Persian translation; no notes."
                ),
            },
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
    }
    async with _translation_client() as client:
        resp = await client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {REQUESTY_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=TRANSLATION_TIMEOUT,
        )
    if resp.status_code != 200:
        logger.warning("AI translate status %s: %s", resp.status_code, resp.text[:250])
        return ""
    data = resp.json()
    result = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return _trim_translation(_message_content_to_text(result))

async def _translate_with_google_endpoint(text):
    # Fast unofficial endpoint used only as a fallback when the AI provider is absent/down.
    params = {"client": "gtx", "sl": "auto", "tl": "fa", "dt": "t"}
    async with _translation_client() as client:
        try:
            resp = await client.post(
                "https://translate.googleapis.com/translate_a/single",
                params=params,
                data={"q": text},
                timeout=TRANSLATION_TIMEOUT,
            )
        except Exception:
            resp = await client.get(
                "https://translate.googleapis.com/translate_a/single",
                params={**params, "q": text},
                timeout=TRANSLATION_TIMEOUT,
            )
    if resp.status_code != 200:
        logger.warning("Google endpoint translate status %s: %s", resp.status_code, resp.text[:200])
        return ""
    data = resp.json()
    if not data or not isinstance(data, list) or not data[0]:
        return ""
    parts = []
    for part in data[0]:
        if part and isinstance(part, list) and len(part) > 0 and part[0]:
            parts.append(str(part[0]))
    return _trim_translation("".join(parts))

def _chunk_text(text, max_chars=450):
    parts = re.split(r"(?<=[.!?؟؛])\s+|\n+", text)
    chunks, current = [], ""
    for part in [p.strip() for p in parts if p.strip()]:
        if len(part) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(part[i:i + max_chars] for i in range(0, len(part), max_chars))
            continue
        candidate = f"{current} {part}".strip()
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = part
    if current:
        chunks.append(current)
    return chunks or [text[:max_chars]]

async def _translate_with_mymemory(text):
    # Non-Google fallback for hosts where Google is rate-limited/blocked.
    translated_parts = []
    source_lang = MYMEMORY_SOURCE_LANG if MYMEMORY_SOURCE_LANG else "en"
    async with _translation_client() as client:
        for chunk in _chunk_text(text):
            params = {"q": chunk, "langpair": f"{source_lang}|fa"}
            if MYMEMORY_EMAIL:
                params["de"] = MYMEMORY_EMAIL
            resp = await client.get(
                "https://api.mymemory.translated.net/get",
                params=params,
                timeout=TRANSLATION_TIMEOUT,
            )
            if resp.status_code != 200:
                logger.warning("MyMemory translate status %s: %s", resp.status_code, resp.text[:200])
                return ""
            data = resp.json()
            if data.get("responseStatus") not in (None, 200):
                logger.warning("MyMemory translate response %s: %s", data.get("responseStatus"), data.get("responseDetails"))
                return ""
            part = data.get("responseData", {}).get("translatedText", "")
            if not part or "MYMEMORY WARNING" in str(part).upper():
                return ""
            translated_parts.append(part)
            await asyncio.sleep(0.2)
    return _trim_translation("\n".join(translated_parts))

def _translate_with_deep_translator_sync(text):
    from deep_translator import GoogleTranslator
    chunks = _chunk_text(text, max_chars=4000)
    results = []
    translator = GoogleTranslator(source="auto", target="fa")
    for chunk in chunks:
        res = translator.translate(chunk)
        if res:
            results.append(res)
    return _trim_translation("\n".join(results))

async def _translate_with_deep_translator(text):
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_translate_with_deep_translator_sync, text),
            timeout=TRANSLATION_TIMEOUT,
        )
    except Exception as e:
        logger.warning("deep-translator Google failed: %s", e)
        return ""

def _translate_with_deep_mymemory_sync(text):
    from deep_translator import MyMemoryTranslator
    chunks = _chunk_text(text, max_chars=450)
    results = []
    translator = MyMemoryTranslator(source="auto", target="fa-IR")
    for chunk in chunks:
        res = translator.translate(chunk)
        if res and "MYMEMORY WARNING" not in str(res).upper():
            results.append(res)
        else:
            return ""
    return _trim_translation("\n".join(results))

async def _translate_with_deep_mymemory(text):
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_translate_with_deep_mymemory_sync, text),
            timeout=TRANSLATION_TIMEOUT,
        )
    except Exception as e:
        logger.warning("deep-translator MyMemory failed: %s", e)
        return ""

async def translate_text(text):
    text = clean_tweet_text(text)
    if not TRANSLATE_FA or not text or not _has_text_to_translate(text):
        return ""

    # Keep enough context for long posts/quotes but stay below provider limits.
    source_text = text[:3000]
    cache_key = hashlib.md5(source_text.encode()).hexdigest()

    # Never return or retain empty cached translation
    if cache_key in translations_cache:
        cached_val = translations_cache[cache_key]
        if cached_val and cached_val.strip():
            return cached_val
        translations_cache.pop(cache_key, None)

    translators = [_translate_with_requesty] if REQUESTY_API_KEY else []
    translators.extend([
        _translate_with_google_endpoint,
        _translate_with_deep_translator,
        _translate_with_mymemory,
        _translate_with_deep_mymemory,
    ])

    retries_per_translator = max(1, TRANSLATION_RETRIES)

    async with translation_sem:
        for translator in translators:
            for attempt in range(1, retries_per_translator + 1):
                try:
                    result = await translator(source_text)
                except Exception as e:
                    logger.warning("%s failed on attempt %s: %s", translator.__name__, attempt, e)
                    result = ""
                if result and result.strip():
                    cleaned = result.strip()
                    if len(translations_cache) > 500:
                        translations_cache.clear()
                    translations_cache[cache_key] = cleaned
                    return cleaned
                if attempt < retries_per_translator:
                    await asyncio.sleep(0.5 * attempt)

    # Important: do not cache failures / empty results.
    # A temporary provider outage should not make this tweet permanently untranslated.
    logger.warning("Translation unavailable after all fallbacks for text: %s", source_text[:120])
    return ""


async def _try_feed_instance(url, target_username):
    """Fetch and parse one RSS source. Returns valid entries or None."""
    try:
        async with rss_fetch_sem:
            resp = await http.get(url, timeout=12, follow_redirects=True)
        if resp.status_code == 429:
            logger.debug("Feed %s -> %s returned HTTP 429 (rate limited)", url, resp.url)
            return _RATE_LIMITED
        if resp.status_code != 200:
            logger.debug("Feed %s -> %s returned HTTP %s", url, resp.url, resp.status_code)
            return None
        if "uni-sonia" in str(resp.url):
            logger.debug("Feed %s -> %s redirected to a challenge page", url, resp.url)
            return None
        feed = await asyncio.to_thread(feedparser.parse, resp.text)
        valid = []
        skipped_retweets = 0
        usable_feed = False
        for entry in feed.entries:
            tid = extract_id(entry)
            if not tid:
                continue
            usable_feed = True
            reason = retweet_reason(entry, target_username)
            if reason:
                skipped_retweets += 1
                logger.debug("Skip @%s %s: %s", target_username, tid, reason)
                continue
            valid.append(entry)
        if skipped_retweets:
            logger.info("Skipped %s retweets/reposts for @%s", skipped_retweets, target_username)
        if not usable_feed:
            logger.debug("Feed %s -> %s returned HTTP 200 but no identifiable tweet entries", url, resp.url)
        # Keep an all-reposts feed distinguishable from a failed/empty endpoint. The
        # caller still checks the other mirrors and can merge originals from them.
        return valid if usable_feed else None
    except Exception as e:
        # Some httpx timeout/connect exceptions stringify to an empty string; include
        # their type and repr so Railway logs explain why a mirror was unusable.
        logger.debug("Feed %s failed (%s): %r", url, type(e).__name__, e)
        return None


async def probe_rsshub_instance():
    """Best-effort /healthz check of the configured RSSHub instance.

    The bot keeps working either way — public mirrors stay as fallbacks — but a
    dedicated instance that is down or misconfigured becomes visible right after
    boot instead of after a silent streak of failed feed checks.
    """
    if not RSSHUB_BASE_URL or http is None:
        return
    try:
        resp = await http.get(f"{RSSHUB_BASE_URL}/healthz", timeout=5)
    except Exception as e:
        logger.warning(
            "RSSHub %s did not answer /healthz (%s: %r); feed checks will still try it "
            "and fall back to public mirrors.",
            RSSHUB_BASE_URL,
            type(e).__name__,
            e,
        )
        return
    if resp.status_code == 200:
        logger.info("RSSHub %s is up (HTTP 200 on /healthz).", RSSHUB_BASE_URL)
    else:
        logger.warning(
            "RSSHub %s answered HTTP %s on /healthz; feeds may fail or be rate-limited.",
            RSSHUB_BASE_URL,
            resp.status_code,
        )


def _x_api_entries_from_payload(username, payload):
    """Convert an X API v2 timeline response to the dict shape used by RSS entries."""
    includes = payload.get("includes") or {}
    media_by_key = {
        str(media.get("media_key")): media
        for media in includes.get("media", []) or []
        if isinstance(media, dict) and media.get("media_key")
    }
    entries = []
    for tweet in payload.get("data", []) or []:
        if not isinstance(tweet, dict):
            continue
        tweet_id = str(tweet.get("id") or "").strip()
        if not tweet_id:
            continue
        media_content = []
        for media_key in (tweet.get("attachments") or {}).get("media_keys", []) or []:
            media = media_by_key.get(str(media_key), {})
            image_url = media.get("url") or media.get("preview_image_url")
            if image_url:
                media_content.append({"url": image_url})
        text = str(tweet.get("text") or "")
        link = f"https://x.com/{username}/status/{tweet_id}"
        entries.append({
            "id": tweet_id,
            "guid": tweet_id,
            "link": link,
            "title": text,
            "summary": text,
            "description": text,
            "author": f"@{username}",
            "media_content": media_content,
            "published": tweet.get("created_at", ""),
        })
    return entries


async def _fetch_x_api_feed(username):
    """Fetch a user's recent original posts from the official X API.

    Returns a list (possibly empty) on a successful response, or None when the
    API is unavailable/unauthorized so callers can try the RSS fallback.
    """
    if not X_BEARER_TOKEN:
        return None
    if http is None:
        logger.warning("X API client is not initialized; cannot fetch @%s", username)
        return None

    async def get_json(path, params=None):
        try:
            response = await http.get(
                f"{X_API_BASE_URL}{path}",
                params=params,
                headers={"Authorization": f"Bearer {X_BEARER_TOKEN}"},
                timeout=15,
            )
        except Exception as e:
            logger.warning("X API request failed for @%s (%s): %r", username, type(e).__name__, e)
            return None
        if response.status_code != 200:
            logger.warning("X API request for @%s returned HTTP %s", username, response.status_code)
            return None
        try:
            return response.json()
        except ValueError as e:
            logger.warning("X API returned invalid JSON for @%s: %r", username, e)
            return None

    normalized = clean_username(username)
    cached_user = x_user_id_cache.get(normalized)
    now = time.monotonic()
    if cached_user and now - cached_user[1] < X_USER_ID_CACHE_TTL:
        user_id = cached_user[0]
    else:
        user_payload = await get_json(f"/users/by/username/{normalized}")
        user_data = user_payload.get("data") if isinstance(user_payload, dict) else None
        user_id = str(user_data.get("id") or "") if isinstance(user_data, dict) else ""
        if not user_id:
            logger.warning("X API could not resolve user @%s; trying RSS fallback", normalized)
            return None
        if len(x_user_id_cache) > 1000:
            x_user_id_cache.clear()
        x_user_id_cache[normalized] = (user_id, now)

    params = {
        "max_results": 100,
        "exclude": "retweets,replies",
        "tweet.fields": "created_at,attachments,entities,referenced_tweets",
        "expansions": "attachments.media_keys",
        "media.fields": "media_key,type,url,preview_image_url",
    }
    timeline = await get_json(f"/users/{user_id}/tweets", params=params)
    if not isinstance(timeline, dict):
        logger.warning("X API timeline response was not an object for @%s", normalized)
        return None
    entries = _x_api_entries_from_payload(normalized, timeline)
    logger.debug("X API returned %s recent posts for @%s", len(entries), normalized)
    return entries


def _tweet_id_sort_key(tweet_id):
    """Sort X snowflake IDs numerically and keep any nonnumeric IDs deterministic."""
    value = str(tweet_id or "")
    return (0, int(value)) if value.isdigit() else (1, value)


def _tweet_id_newest_first_key(tweet_id):
    """Ordering key that puts the greatest numeric X status ID first."""
    value = str(tweet_id or "")
    return (0, -int(value)) if value.isdigit() else (1, value)


def _mirror_host(source):
    return urlparse(source).netloc


def _mirror_is_exempt(source):
    """The dedicated RSSHub instance never enters the breaker: a brief outage
    (free-tier slowdown, deploy) must not bench it for half an hour while the
    bot silently runs on dead public mirrors. The public rsshub.app default is
    NOT exempt — it is just as dead/alive as any other mirror."""
    host = _mirror_host(source)
    return bool(host) and host == _mirror_host(RSSHUB_BASE_URL) and RSSHUB_BASE_URL != PUBLIC_RSSHUB_BASE_URL


def _mirror_mark_success(source):
    mirror_health[_mirror_host(source)] = {"fails": 0, "tripped_until": 0.0}


def _mirror_mark_failure(source):
    host = _mirror_host(source)
    if not host or _mirror_is_exempt(source):
        return
    record = mirror_health.setdefault(host, {"fails": 0, "tripped_until": 0.0})
    record["fails"] += 1
    if record["fails"] >= MIRROR_BREAKER_SAMPLE:
        record["tripped_until"] = time.monotonic() + MIRROR_BREAKER_COOLDOWN
        logger.info(
            "Mirror %s benched for %ss after %s consecutive failures",
            host,
            MIRROR_BREAKER_COOLDOWN,
            record["fails"],
        )


def _mirror_mark_rate_limited(source):
    """HTTP 429: the mirror is alive but asking us to back off.

    The cooldown is short and the death counter is left untouched, so a mirror
    we merely throttled ourselves is never benched as dead for half an hour.
    """
    host = _mirror_host(source)
    if not host or _mirror_is_exempt(source):
        return
    record = mirror_health.setdefault(host, {"fails": 0, "tripped_until": 0.0})
    until = time.monotonic() + MIRROR_RATE_LIMIT_COOLDOWN
    if until > record["tripped_until"]:
        record["tripped_until"] = until
    logger.debug(
        "Mirror %s rate-limited (HTTP 429); backing off %ss (death counter stays at %s)",
        host,
        MIRROR_RATE_LIMIT_COOLDOWN,
        record["fails"],
    )


def _mirror_stats():
    """(total sources, currently-skipped sources) for /status and /healthz.

    Counts every mirror in cooldown, whether benched as dead or merely
    rate-limited — both mean "not being probed right now".
    """
    now = time.monotonic()
    total = len(RSS_SOURCES)
    unavailable = sum(
        1
        for source in RSS_SOURCES
        if (record := mirror_health.get(_mirror_host(source)))
        and now < record["tripped_until"]
    )
    return total, unavailable


async def fetch_feed(username):
    """Race RSS mirrors, keep repost-only feeds from winning, and cap waiting time."""
    target_username = clean_username(username)
    if X_BEARER_TOKEN:
        api_entries = await _fetch_x_api_feed(target_username)
        if api_entries is not None:
            return sorted(
                api_entries,
                key=lambda entry: _tweet_id_newest_first_key(extract_id(entry)),
            )
        logger.warning("Falling back to public RSS mirrors for @%s", target_username)

    all_sources = list(RSS_SOURCES)
    now_mono = time.monotonic()
    # tripped_until alone governs: it stays at 0 until either the death counter
    # reaches the sample or a rate limit arms a (short) cooldown, and expires for
    # both. The old `fails < SAMPLE` clause was redundant — and it would have
    # cancelled a rate-limit cooldown for any mirror not yet benched.
    sources = [
        src
        for src in all_sources
        if (record := mirror_health.get(_mirror_host(src))) is None
        or now_mono >= record["tripped_until"]
    ]
    if not sources and all_sources:
        # The breaker must never blind the bot completely: when every mirror is
        # benched, still probe the one whose cooldown expires soonest.
        soonest_host = min(mirror_health, key=lambda host: mirror_health[host]["tripped_until"])
        sources = [src for src in all_sources if _mirror_host(src) == soonest_host][:1]
        logger.info("All mirrors are in reset cooldown; probing the soonest: %s", soonest_host)
    skipped = len(all_sources) - len(sources)
    if skipped:
        logger.debug(
            "Circuit breaker skipped %s dead mirror(s); probing %s of %s.",
            skipped,
            len(sources),
            len(all_sources),
        )
    task_sources = {
        asyncio.create_task(
            _try_feed_instance(src.format(username=target_username), target_username)
        ): src
        for src in sources
    }
    pending = set(task_sources)
    feed_results = []
    usable_mirrors = 0
    first_post_deadline = None
    loop = asyncio.get_running_loop()
    overall_deadline = loop.time() + RSS_FETCH_TIMEOUT
    try:
        # Probe mirrors concurrently, but do not wait for every dead/slow host once an
        # original-post feed has responded. A brief grace period merges other fast feeds.
        while pending:
            now = loop.time()
            remaining = overall_deadline - now
            if first_post_deadline is not None:
                remaining = min(remaining, first_post_deadline - now)
            if remaining <= 0:
                break

            done, pending = await asyncio.wait(
                pending,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                break

            for task in done:
                source = task_sources[task]
                try:
                    result = task.result()
                except Exception as exc:
                    logger.debug(
                        "Feed task for %s failed (%s): %r",
                        source,
                        type(exc).__name__,
                        exc,
                    )
                    _mirror_mark_failure(source)
                    continue
                if result is _RATE_LIMITED:
                    _mirror_mark_rate_limited(source)
                    continue
                if result is None:
                    _mirror_mark_failure(source)
                    continue
                _mirror_mark_success(source)
                usable_mirrors += 1
                if result:
                    feed_results.append(result)
                    if first_post_deadline is None:
                        first_post_deadline = min(
                            overall_deadline,
                            loop.time() + RSS_MERGE_GRACE,
                        )

        merged = {}
        for result in feed_results:
            for entry in result:
                tweet_id = extract_id(entry)
                if tweet_id:
                    merged.setdefault(tweet_id, entry)

        if not merged and pending:
            logger.warning(
                "RSS fetch for @%s hit its %.1fs deadline (%s/%s mirrors returned; "
                "none produced original posts).",
                target_username,
                RSS_FETCH_TIMEOUT,
                usable_mirrors,
                len(sources),
            )
        elif not merged and not usable_mirrors:
            logger.warning(
                "No usable RSS feed for @%s: all %s mirrors failed or returned empty data.",
                target_username,
                len(sources),
            )
        elif not merged:
            logger.info(
                "RSS feeds for @%s were reachable, but no original posts were found; "
                "reposts are filtered.",
                target_username,
            )
        else:
            logger.debug(
                "Collected %s unique original posts from %s completed RSS mirrors for @%s",
                len(merged),
                len(feed_results),
                target_username,
            )

        return sorted(
            merged.values(),
            key=lambda entry: _tweet_id_newest_first_key(extract_id(entry)),
        )
    finally:
        for task in task_sources:
            if not task.done():
                task.cancel()
        if task_sources:
            await asyncio.gather(*task_sources, return_exceptions=True)


# ── Bot Commands ──────────────────────────────────────────────────────────

WELCOME = (
    "👋 <b>سلام!</b>\n\n"
    "ربات فالوور توییتر/X\n"
    "بدون نیاز به API پولی — با RSS کار میکنه.\n\n"
    "📌 <b>دستورات:</b>\n"
    "/add username  — اضافه کردن\n"
    "/del username  — حذف کردن\n"
    "/list  — لیست اکانت‌ها\n"
    "/test  — تست سریع\n"
    "/search متن  — جستجو در توییت‌ها\n\n"
    "💡 از کیبورد پایین هم میتونی استفاده کنی.\n"
    "🔍 <b>Inline Mode:</b> از هر چتی بنویس <code>@رباتت متن</code>"
)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("➕ اضافه کردن"), KeyboardButton("📋 لیست اکانت‌ها")],
        [KeyboardButton("🔍 جستجو"), KeyboardButton("🌐 داشبورد")],
        [KeyboardButton("📊 وضعیت"), KeyboardButton("ℹ️ راهنما")],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="نام کاربری توییتر رو بنویس...",
)

RAILWAY_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN")
DASHBOARD_URL = os.getenv("DASHBOARD_URL") or (f"https://{RAILWAY_DOMAIN}" if RAILWAY_DOMAIN else "http://localhost:8080")


async def cmd_start(update, context):
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.HTML, reply_markup=MAIN_KEYBOARD)


async def cmd_quick_add(update, context):
    text = update.message.text.strip()
    username = clean_username(text)
    if is_valid_twitter(username):
        if db.is_subscribed(update.effective_chat.id, username):
            await update.message.reply_text(f"⏭ @{username} قبلاً اضافه شده.")
        else:
            db.add_subscription(update.effective_chat.id, username, "")
            await update.message.reply_text(f"✅ @{username} اضافه شد!")
    else:
        await update.message.reply_text("⚠️ یوزرنیم معتبر نیست. فقط حروف انگلیسی، اعداد و _ مجازه.")


async def cmd_quick_list(update, context):
    chat_id = str(update.effective_chat.id)
    users = db.get_subs_for_chat(chat_id)
    if users:
        lines = [f"• @{u}" for u in sorted(set(users))]
        await update.message.reply_text(f"📋 لیست شما ({len(lines)}):\n\n" + "\n".join(lines))
    else:
        await update.message.reply_text("📋 لیست شما خالیه.")


async def cmd_quick_search(update, context):
    await update.message.reply_text("🔍 بنویس چی میخوای جستجو کنی:")


async def cmd_quick_dashboard(update, context):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🌐 باز کردن داشبورد", url=DASHBOARD_URL)]])
    await update.message.reply_text("🌐 داشبورد لایو توییترها:", reply_markup=kb)


async def handle_text(update, context):
    text = update.message.text.strip()
    if not text:
        return
    if text.startswith("/"):
        return
    if text == "➕ اضافه کردن":
        await update.message.reply_text("📝 یوزرنیم توییتر رو بنویس (مثلاً ElonMusk):")
        return
    if text == "📋 لیست اکانت‌ها":
        await cmd_quick_list(update, context)
        return
    if text == "🔍 جستجو":
        await update.message.reply_text("🔍 متن جستجو رو بنویس:")
        return
    if text == "🌐 داشبورد":
        await cmd_quick_dashboard(update, context)
        return
    if text == "🔄 بروزرسانی":
        await update.message.reply_text("🔄 بروزرسانی شد! توییت‌های جدید بررسی میشن.")
        return
    if text == "📊 وضعیت":
        await cmd_status(update, context)
        return
    if text == "ℹ️ راهنما":
        await cmd_start(update, context)
        return
    if is_valid_twitter(clean_username(text)):
        await cmd_quick_add(update, context)
    else:
        await cmd_search(update, context)


async def cmd_status(update, context):
    if not db.enabled:
        await update.message.reply_text("❌ دیتابیس متصل نیست. لطفاً PostgreSQL رو اضافه کن.")
        return
    chat_id = str(update.effective_chat.id)
    users = db.get_subs_for_chat(chat_id)
    tracked = db.get_all_tracked()
    tweet_count = db._run("SELECT COUNT(*) FROM tweets_content", fetch="one", default=[0])[0] if db.enabled else 0
    missing_translation_count = db.count_missing_translations() if db.enabled else 0
    ai_status = "✅ AI + Google/MyMemory fallback" if REQUESTY_API_KEY else "⚠️ Google/MyMemory fallback"
    db_status = "✅ PostgreSQL" if db.enabled else "❌ غیرفعال"
    if RSSHUB_BASE_URL != PUBLIC_RSSHUB_BASE_URL:
        feed_source = f"✅ RSSHub اختصاصی{' 🔒' if RSSHUB_ACCESS_KEY else ''}"
    else:
        feed_source = "⚠️ عمومی (RSSHub/آینه‌ها)"

    lines = [
        "📊 <b>وضعیت ربات</b>",
        "",
        f"🤖 <b>ترجمه:</b> {ai_status}",
        f"💾 <b>دیتابیس:</b> {db_status}",
        f"📡 <b>منبع فید:</b> {feed_source}",
        f"📝 <b>توییت‌های ذخیره شده:</b> {tweet_count}",
        f"🦁 <b>بدون ترجمه:</b> {missing_translation_count}",
        f"👤 <b>اکانت‌های شما:</b> {len(set(users))}",
        f"🌐 <b>اکانت‌های کل:</b> {len(tracked)}",
        f"⏱ <b>بررسی هر:</b> {CHECK_INTERVAL} ثانیه",
    ]

    if REQUESTY_API_KEY:
        lines.append(f"🧠 <b>مدل:</b> {REQUESTY_MODEL}")

    if check_state.get("last_finished"):
        lines.append(f"✅ <b>آخرین بررسی:</b> {check_state['last_finished'][:16].replace('T', ' ')} UTC ({check_state['last_duration']}s، {check_state['errors']} خطا)")

    total_mirrors, benched_mirrors = _mirror_stats()
    lines.append(f"🔁 <b>آینه‌های فید:</b> {total_mirrors - benched_mirrors} سالم از {total_mirrors}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_add(update, context):
    if not db.enabled:
        await update.message.reply_text("❌ دیتابیس متصل نیست. اول PostgreSQL رو اضافه کن.")
        return
    raw = " ".join(context.args)
    if not raw.strip():
        await update.message.reply_text("⚠️ استفاده: <code>/add username1 username2</code>", parse_mode=ParseMode.HTML)
        return
    users = list(set([clean_username(u) for u in re.split(r"[,\s]+", raw) if u]))
    added, skipped = [], []
    for u in users:
        if not is_valid_twitter(u):
            skipped.append(f"@{u}")
            continue
        if db.is_subscribed(update.effective_chat.id, u):
            skipped.append(f"@{u}")
            continue
        db.add_subscription(update.effective_chat.id, u, "")
        added.append(f"@{u}")
    msg = ""
    if added:
        msg += f"✅ اضافه شد: {', '.join(added)}\n"
    if skipped:
        msg += f"⏭ رد شد: {', '.join(skipped)}\n"
    if not msg:
        msg = "هیچی اضافه نشد."
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_del(update, context):
    if not context.args:
        await update.message.reply_text("⚠️ استفاده: <code>/del username</code>", parse_mode=ParseMode.HTML)
        return
    for arg in context.args:
        db.remove_subscription(update.effective_chat.id, clean_username(arg))
    await update.message.reply_text("✅ حذف شد.")

async def cmd_list(update, context):
    if not db.enabled:
        await update.message.reply_text("❌ دیتابیس متصل نیست.")
        return
    chat_id = str(update.effective_chat.id)
    users = db.get_subs_for_chat(chat_id)
    if users:
        lines = [f"• @{u}" for u in sorted(set(users))]
        await update.message.reply_text(f"📋 لیست شما ({len(lines)}):\n\n" + "\n".join(lines))
    else:
        await update.message.reply_text("📋 لیست شما خالیه.")

async def cmd_test(update, context):
    username = clean_username(context.args[0]) if context.args else "ElonMusk"
    await update.message.reply_text(f"🔍 تست @{username}...")
    entries = await fetch_feed(username)
    if entries:
        content = await build_content(username, entries[0])
        if content:
            await deliver(content, [str(update.effective_chat.id)], context.application.bot, force=True)
    else:
        await update.message.reply_text("❌ خطا در دریافت فید.")


async def cmd_search(update, context):
    if not db.enabled:
        await update.message.reply_text("❌ دیتابیس متصل نیست.")
        return
    if context.args:
        query = " ".join(context.args)
    else:
        text = update.message.text.strip()
        if text in ["🔍 جستجو", "/search"]:
            await update.message.reply_text("🔍 متن جستجو رو بنویس:")
            return
        query = text
    if not query:
        await update.message.reply_text("🔍 استفاده: /search متن جستجو")
        return
    chat_id = str(update.effective_chat.id)
    rows = db.search_tweets(query, chat_id, limit=5)
    if not rows:
        await update.message.reply_text("🔍 نتیجه‌ای پیدا نشد.")
        return
    for r in rows:
        msg = f"🐦 @{r['username']}:\n{r['title'][:200]}"
        if r.get("translation"):
            msg += f"\n\n🦁 {r['translation'][:200]}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 View", url=r["tweet_link"])]] if r.get("tweet_link") else [])
        await update.message.reply_text(msg, reply_markup=kb)


async def handle_inline_query(update, context):
    query = update.inline_query.query.strip()
    if not query or len(query) < 2:
        return
    rows = db.search_tweets(query, limit=5)
    results = []
    for i, r in enumerate(rows):
        # Raw tweet text is user content — escape it, otherwise stray < > & break
        # HTML parse mode and Telegram rejects the whole inline answer.
        safe_title = html.escape(str(r["title"] or ""))[:300]
        safe_trans = html.escape(str(r.get("translation") or ""))[:300]
        text = f"🐦 @{html.escape(r['username'])}\n\n{safe_title}"
        if safe_trans:
            text += f"\n\n🦁 {safe_trans}"
        results.append(
            InlineQueryResultArticle(
                id=str(i),
                title=f"@{r['username']}: {str(r['title'] or '')[:50]}...",
                description=str(r["title"] or "")[:100],
                input_message_content=InputTextMessageContent(text, parse_mode=ParseMode.HTML),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔗 View on X", url=r["tweet_link"])]]) if r.get("tweet_link") else None,
            )
        )
    await update.inline_query.answer(results, cache_time=300, is_persistent=True)


# ── Tweet Engine ──────────────────────────────────────────────────────────

TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_TEXT_LIMIT = 4096

def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2

def escape_and_truncate(text: str, budget: int) -> str:
    """Escapes text for HTML and truncates with ellipsis so utf16_len <= budget."""
    if budget <= 0:
        return ""
    escaped = html.escape(text.strip())
    if utf16_len(escaped) <= budget:
        return escaped
    if budget <= 1:
        return "…"
    cut = text.strip()
    while cut and (utf16_len(html.escape(cut) + "…") > budget):
        overflow = utf16_len(html.escape(cut) + "…") - budget
        step = max(1, overflow // 4)
        cut = cut[:-step].strip()
    return (html.escape(cut) + "…") if cut else "…"

async def build_content(username, entry):
    tid = extract_id(entry)
    if not tid or is_retweet(entry, username):
        return None
    title = extract_tweet_text(entry)
    try:
        translation = await asyncio.wait_for(
            translate_text(title),
            timeout=TRANSLATION_DELIVERY_TIMEOUT,
        )
    except asyncio.TimeoutError:
        # Deliver the original post even when a translation provider stalls; a
        # saved untranslated post can be repaired later by the backfill job.
        logger.warning(
            "Translation exceeded %.1fs for @%s; continuing without translation",
            TRANSLATION_DELIVERY_TIMEOUT,
            username,
        )
        translation = ""
    img_url = extract_image_url(entry)
    link = f"https://x.com/i/status/{tid}"
    return {
        "tid": tid,
        "username": username,
        "title": title,
        "translation": translation,
        "img_url": img_url,
        "link": link,
    }


def build_message(c, max_len=TELEGRAM_TEXT_LIMIT):
    username = str(c.get("username", "") or "")
    title = str(c.get("title", "") or "").strip()
    translation = str(c.get("translation", "") or "").strip()

    header = f"🔔 <b>NEW UPDATE | @{html.escape(username).upper()}</b>"
    has_title = bool(title)
    has_trans = bool(translation)

    if not has_title and not has_trans:
        return header

    orig_prefix = "\n📝 <b>Original:</b>\n<blockquote expandable>"
    orig_suffix = "</blockquote>"
    trans_prefix = f"\n{'━'*10}\n🦁 <b>ترجمه فارسی:</b>\n<blockquote expandable><i>"
    trans_suffix = "</i></blockquote>"

    if has_title and has_trans:
        fixed_len = (
            utf16_len(header)
            + utf16_len(orig_prefix)
            + utf16_len(orig_suffix)
            + utf16_len(trans_prefix)
            + utf16_len(trans_suffix)
        )
        budget = max(0, max_len - fixed_len)
        orig_full = html.escape(title)
        trans_full = html.escape(translation)

        if utf16_len(orig_full) + utf16_len(trans_full) <= budget:
            body_orig = orig_full
            body_trans = trans_full
        else:
            half = budget // 2
            orig_len = utf16_len(orig_full)
            trans_len = utf16_len(trans_full)
            if orig_len < half:
                orig_budget = orig_len
                trans_budget = budget - orig_budget
            elif trans_len < half:
                trans_budget = trans_len
                orig_budget = budget - trans_budget
            else:
                orig_budget = half
                trans_budget = budget - half
            body_orig = escape_and_truncate(title, orig_budget)
            body_trans = escape_and_truncate(translation, trans_budget)
        return f"{header}{orig_prefix}{body_orig}{orig_suffix}{trans_prefix}{body_trans}{trans_suffix}"

    elif has_title:
        fixed_len = utf16_len(header) + utf16_len(orig_prefix) + utf16_len(orig_suffix)
        budget = max(0, max_len - fixed_len)
        body_orig = escape_and_truncate(title, budget)
        return f"{header}{orig_prefix}{body_orig}{orig_suffix}"

    else:
        fixed_len = utf16_len(header) + utf16_len(trans_prefix) + utf16_len(trans_suffix)
        budget = max(0, max_len - fixed_len)
        body_trans = escape_and_truncate(translation, budget)
        return f"{header}{trans_prefix}{body_trans}{trans_suffix}"


async def _download_image(url):
    """Download tweet media so Telegram doesn't have to fetch pbs.twimg.com itself
    (Telegram's servers are often blocked/rate-limited for that host, which used to
    silently turn photo tweets into plain text)."""
    if not url or not str(url).lower().startswith(("http://", "https://")):
        return None
    try:
        resp = await http.get(url, timeout=20, follow_redirects=True)
        if resp.status_code != 200 or not resp.content:
            return None
        ctype = (resp.headers.get("content-type") or "").lower()
        if ctype and not ctype.startswith(("image/", "application/octet-stream")):
            return None
        if len(resp.content) > 20 * 1024 * 1024:
            return None
        return resp.content
    except Exception as e:
        logger.debug("Image download failed (%s): %s", url, e)
    return None


async def deliver(content, chat_ids, bot, force=False):
    if not content:
        return False
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 View on X", url=content["link"])]])
    saved = False
    all_delivered = True

    has_photo = bool(content.get("img_url"))
    caption = build_message(content, max_len=TELEGRAM_CAPTION_LIMIT)
    full_text = build_message(content, max_len=TELEGRAM_TEXT_LIMIT)
    photo_bytes = await _download_image(content["img_url"]) if has_photo else None

    for i, cid in enumerate(chat_ids):
        # Small stagger avoids Telegram flood limits when broadcasting to many chats.
        if i and i % 20 == 0:
            await asyncio.sleep(1)
        if not force and db.is_duplicate(cid, content["tid"]):
            continue
        sent_ok = False
        if has_photo:
            try:
                if photo_bytes:
                    await bot.send_photo(
                        chat_id=cid,
                        photo=InputFile(photo_bytes, filename=f"{content['tid']}.jpg"),
                        caption=caption,
                        reply_markup=kb,
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await bot.send_photo(
                        chat_id=cid,
                        photo=content["img_url"],
                        caption=caption,
                        reply_markup=kb,
                        parse_mode=ParseMode.HTML,
                    )
                sent_ok = True
            except Exception as e:
                logger.warning("Photo send failed to %s: %s, falling back to text message", cid, e)
                try:
                    await bot.send_message(
                        chat_id=cid,
                        text=full_text,
                        reply_markup=kb,
                        parse_mode=ParseMode.HTML,
                    )
                    sent_ok = True
                except Exception as e2:
                    logger.error("Fallback text send failed to %s: %s", cid, e2)
        else:
            try:
                await bot.send_message(
                    chat_id=cid,
                    text=full_text,
                    reply_markup=kb,
                    parse_mode=ParseMode.HTML,
                )
                sent_ok = True
            except Exception as e:
                logger.error("Send failed to %s: %s", cid, e)

        if sent_ok:
            db.mark_sent(cid, content["tid"])
            saved = True
        else:
            all_delivered = False

    if saved:
        c = content
        db.save_tweet_content(c["username"], c["title"], c["translation"], c["img_url"], c["link"])
    return all_delivered


async def process_user(username, last_id, bot, sem):
    async with sem:
        entries = await fetch_feed(username)
        await asyncio.sleep(1)
    if not entries:
        return

    # Merge may return overlapping items from several mirrors. Deduplicate again here
    # so callers/tests and the X API path receive the same safe processing behavior.
    all_ids = {}
    for entry in entries:
        tweet_id = extract_id(entry)
        if tweet_id:
            all_ids.setdefault(tweet_id, entry)
    if not all_ids:
        return

    ordered = sorted(
        all_ids.items(),
        key=lambda item: _tweet_id_newest_first_key(item[0]),
    )
    if not last_id:
        newest_id = ordered[0][0]
        db.update_last_id(username, newest_id)
        logger.info("Baseline @%s: %s", username, newest_id)
        return

    # Process the oldest unseen entries first. If we cap a backlog, advancing the
    # cursor to the newest first would permanently skip the older unprocessed posts.
    fresh = sorted(
        ((tweet_id, entry) for tweet_id, entry in ordered if is_newer_tweet_id(tweet_id, last_id)),
        key=lambda item: _tweet_id_sort_key(item[0]),
    )
    if not fresh:
        return

    subs = db.get_subs_for_user(username)
    if not subs:
        return

    logger.info("@%s: %s new tweets", username, len(fresh))
    batch_limit = max(1, MAX_TWEETS_PER_CHECK)
    if len(fresh) > batch_limit:
        logger.warning(
            "@%s has %s unseen tweets; processing the oldest %s this cycle and "
            "leaving the rest for the next check",
            username,
            len(fresh),
            batch_limit,
        )

    for tweet_id, entry in fresh[:batch_limit]:
        content = await build_content(username, entry)
        if not content:
            logger.error(
                "Could not build @%s post %s; keeping the cursor before it for retry",
                username,
                tweet_id,
            )
            break
        if not await deliver(content, subs, bot):
            logger.error(
                "Could not deliver @%s post %s to every subscriber; keeping it for retry",
                username,
                tweet_id,
            )
            break
        db.update_last_id(username, tweet_id)

# Runtime observability for /status and /healthz: when did the last feed check
# run, how long did it take, and how many accounts errored. Updated by check_updates.
check_state = {
    "running": False,
    "last_started": None,   # UTC ISO timestamp
    "last_finished": None,  # UTC ISO timestamp
    "last_duration": None,  # seconds
    "accounts": 0,
    "errors": 0,
}


async def check_updates(context):
    # PTB's run_repeating schedules the next run regardless of whether the
    # previous one finished: a slow check (dead feed, stalled translation) would
    # overlap with the next one and duplicate Telegram sends. Skip instead.
    if check_state["running"]:
        logger.warning("Previous feed check is still running; skipping this tick.")
        return
    check_state["running"] = True
    started_at = time.monotonic()
    try:
        tracked = db.get_all_tracked() or []
        check_state["accounts"] = len(tracked)
        check_state["last_started"] = datetime.now(timezone.utc).isoformat()
        if not tracked:
            logger.info("Update check finished: no tracked accounts in the database.")
            return

        logger.info("Checking feeds for %s tracked accounts.", len(tracked))
        bot = context.application.bot
        sem = asyncio.Semaphore(CONCURRENT_LIMIT)
        tasks = [process_user(u, li, bot, sem) for u, li in tracked]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        failures = 0
        for (username, _), result in zip(tracked, results):
            if isinstance(result, Exception):
                failures += 1
                logger.error(
                    "Error processing @%s: %s",
                    username,
                    result,
                    exc_info=(type(result), result, result.__traceback__),
                )

        logger.info(
            "Feed check finished in %.1f seconds (%s accounts, %s errors).",
            time.monotonic() - started_at,
            len(tracked),
            failures,
        )
        check_state["errors"] = failures
    except Exception:
        logger.exception("Feed check crashed")
        raise
    finally:
        # report the most recently *completed* run to /healthz and /status;
        # last_started stays as the start time of this run.
        duration = time.monotonic() - started_at
        check_state["last_finished"] = datetime.now(timezone.utc).isoformat()
        check_state["last_duration"] = round(duration, 1)
        check_state["running"] = False


async def run_translation_backfill(context):
    if not db.enabled or not TRANSLATE_FA:
        return
    rows = db.get_tweets_missing_translation(TRANSLATION_BACKFILL_LIMIT)
    if not rows:
        return
    logger.info("Backfilling translations for %s saved tweets (DB/dashboard only)", len(rows))
    for row in rows:
        translation = await translate_text(row["title"])
        if translation and translation.strip():
            db.update_tweet_translation(row["id"], translation.strip())
        await asyncio.sleep(0.4)


async def run_cleanup(context):
    db.cleanup()


# ── FastAPI + Telegram Lifecycle ──────────────────────────────────────────

@asynccontextmanager
async def lifespan(fastapi_app):
    global http, bot_app_ref
    http = httpx.AsyncClient(
        headers=RSS_HEADERS,
        timeout=10,
        follow_redirects=True,
        limits=httpx.Limits(
            max_connections=HTTP_MAX_CONNECTIONS,
            max_keepalive_connections=min(20, HTTP_MAX_CONNECTIONS),
        ),
    )
    logger.info(
        "HTTP pool max_connections=%s; RSS fetch concurrency=%s; per-account RSS deadline=%.1fs",
        HTTP_MAX_CONNECTIONS,
        RSS_FETCH_CONCURRENCY,
        RSS_FETCH_TIMEOUT,
    )
    # Surface a broken dedicated RSSHub (wrong URL, missing access key, sleeping
    # free-tier dyno, ...) immediately instead of via cryptic feed failures.
    await probe_rsshub_instance()

    from telegram.ext import MessageHandler, filters

    bot_commands = [
        ("start", "شروع و راهنما"),
        ("add", "اضافه کردن اکانت"),
        ("del", "حذف اکانت"),
        ("list", "لیست اکانت‌ها"),
        ("test", "تست سریع"),
        ("search", "جستجو در توییت‌ها"),
        ("status", "وضعیت ربات"),
    ]

    async def post_init(application):
        await application.bot.set_my_commands(bot_commands)

    bot_app_ref = Application.builder().token(TOKEN).post_init(post_init).build()

    bot_app_ref.add_handler(CommandHandler("start", cmd_start))
    bot_app_ref.add_handler(CommandHandler("help", cmd_start))
    bot_app_ref.add_handler(CommandHandler("add", cmd_add))
    bot_app_ref.add_handler(CommandHandler("del", cmd_del))
    bot_app_ref.add_handler(CommandHandler("remove", cmd_del))
    bot_app_ref.add_handler(CommandHandler("list", cmd_list))
    bot_app_ref.add_handler(CommandHandler("test", cmd_test))
    bot_app_ref.add_handler(CommandHandler("search", cmd_search))
    bot_app_ref.add_handler(CommandHandler("status", cmd_status))
    bot_app_ref.add_handler(InlineQueryHandler(handle_inline_query))
    bot_app_ref.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    async def handle_error(update, context):
        from telegram.error import Conflict
        if isinstance(context.error, Conflict):
            return
        logger.error(f"Unhandled exception: {context.error}")

    bot_app_ref.add_error_handler(handle_error)

    if db.enabled:
        bot_app_ref.job_queue.run_repeating(check_updates, interval=CHECK_INTERVAL, first=10)
        bot_app_ref.job_queue.run_repeating(
            run_translation_backfill,
            interval=max(CHECK_INTERVAL * 2, 600),
            first=120,
        )
        bot_app_ref.job_queue.run_repeating(run_cleanup, interval=86400, first=300)

    await bot_app_ref.initialize()
    await bot_app_ref.start()
    await bot_app_ref.updater.start_polling(drop_pending_updates=True)
    yield
    await bot_app_ref.updater.stop()
    await bot_app_ref.stop()
    await bot_app_ref.shutdown()
    await http.aclose()

app = FastAPI(lifespan=lifespan)


def _health_payload():
    """Fine for uptime monitors; 503 only when a hard dependency is unusable."""
    ok = bool(TOKEN) and (db.enabled or not (os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL")))
    return {
        "ok": ok,
        "database": "ok" if db.enabled else "disabled",
        "feed_source": "dedicated-rsshub" if RSSHUB_BASE_URL != PUBLIC_RSSHUB_BASE_URL else "public-mirrors",
        "x_api": bool(X_BEARER_TOKEN),
        "check_interval": CHECK_INTERVAL,
        "mirrors": {"total": _mirror_stats()[0], "benched": _mirror_stats()[1]},
        "last_check": {k: check_state[k] for k in ("running", "last_started", "last_finished", "last_duration", "accounts", "errors")},
    }


@app.get("/healthz")
async def healthz():
    """Uptime/health probe for Railway or any external monitor."""
    payload = _health_payload()
    status = 200 if payload["ok"] else 503
    return JSONResponse(payload, status_code=status)


def _tweet_to_dict(t):
    return {
        "username": t.get("username") or "",
        "title": t.get("title") or "",
        "translation": t.get("translation") or "",
        "img_url": t.get("img_url") or "",
        "tweet_link": t.get("tweet_link") or "",
        "created_at": t["created_at"].isoformat() if t.get("created_at") else None,
    }


@app.get("/api/tweets")
async def api_tweets():
    """JSON feed for the dashboard's live (no-reload) refresh."""
    limit = 30
    tweets = db.get_latest_tweets(limit) if db.enabled else []
    return {"tweets": [_tweet_to_dict(t) for t in tweets], "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    tweets = db.get_latest_tweets(30) if db.enabled else []
    return templates.TemplateResponse(request=request, name="index.html", context={"tweets": tweets})

@app.get("/manifest.json")
async def get_manifest():
    return FileResponse(os.path.join(BASE_PATH, "manifest.json"))

@app.get("/sw.js")
async def get_sw():
    return FileResponse(os.path.join(BASE_PATH, "sw.js"))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
