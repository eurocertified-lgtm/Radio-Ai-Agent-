"""Radio AI Agent: fetches news, summarises headlines and prints a radio-ready script.

The implementation favours graceful degradation: it works without optional
dependencies (``feedparser``, ``beautifulsoup4`` and the official OpenAI
package) and is safe to execute in offline environments.  When API access or
network connectivity is missing the agent still produces placeholder copy so a
radio show is never blocked.

Run ``python agent.py --help`` for the list of supported options.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

# ---- Optional imports (we keep running without them) ----
try:  # pragma: no cover - import guard
    import feedparser  # type: ignore

    FEEDPARSER_AVAILABLE = True
except Exception:  # pragma: no cover - import guard
    feedparser = None
    FEEDPARSER_AVAILABLE = False

try:  # pragma: no cover - import guard
    from bs4 import BeautifulSoup  # type: ignore

    BS4_AVAILABLE = True
except Exception:  # pragma: no cover - import guard
    BeautifulSoup = None
    BS4_AVAILABLE = False

try:  # pragma: no cover - import guard
    from openai import OpenAI  # type: ignore

    OPENAI_AVAILABLE = True
except Exception:  # pragma: no cover - import guard
    OpenAI = None
    OPENAI_AVAILABLE = False


# ---- Station specific defaults ----
STATION = {
    "name": "Caribbean Air Force Radio",
    "slogan": "Sounds of home wherever you roam",
    "target_demo": "Ages 30–60 in the Caribbean and diaspora",
}

VOICE = {
    "style": "Warm, upbeat Caribbean DJ; concise, vivid, culturally respectful",
    "pronunciation_notes": "Prefer Caribbean pronunciations; avoid heavy slang unless appropriate.",
}

DEFAULT_FEEDS = [
    "https://www.caribbeannews.net/rss/",
    "https://jamaica-gleaner.com/feed/rss.xml",
    "https://www.caribjournal.com/feed/",
]

LENGTH_SECS = 55


def _compute_base_dir() -> Path:
    """Return a robust base directory for storing generated files."""

    env_dir = os.getenv("CAF_BASE_DIR")
    if env_dir:
        try:
            return Path(env_dir).resolve()
        except Exception:
            pass

    if os.getenv("CAF_FORCE_CWD") == "1":
        return Path(os.getcwd()).resolve()

    try:
        return Path(__file__).resolve().parent
    except Exception:
        return Path(os.getcwd()).resolve()


BASE_DIR = _compute_base_dir()


def _default_feed_file() -> Path:
    return BASE_DIR / "feeds.json"


def configure_logging(
    *,
    console_level: str | int = "WARNING",
    log_level: str | int = "INFO",
    no_file: bool = False,
) -> None:
    """Configure structured logging.

    The console remains quiet by default.  Optional file logging writes to the
    ``logs`` directory inside the base directory.
    """

    logging.captureWarnings(True)
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
    )
    logger.addHandler(console_handler)

    if no_file:
        return

    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_dir / "agent.log", encoding="utf-8")
    file_handler.setLevel(log_level)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
    )
    logger.addHandler(file_handler)


logger = logging.getLogger("radio.agent")


@dataclass
class NewsItem:
    title: str
    summary: str
    link: str
    source: str
    published: Optional[datetime] = None

    def to_prompt_block(self) -> str:
        published = (
            self.published.astimezone(timezone.utc).isoformat()
            if self.published
            else "Unknown"
        )
        return textwrap.dedent(
            f"""
            Title: {self.title}
            Source: {self.source}
            Published: {published}
            Summary: {self.summary}
            Link: {self.link}
            """
        ).strip()


def _load_feed_urls() -> List[str]:
    feed_file = _default_feed_file()
    if feed_file.exists():
        try:
            data = json.loads(feed_file.read_text(encoding="utf-8"))
            feeds = [str(url).strip() for url in data if url]
            return [url for url in feeds if url]
        except Exception as exc:
            logger.warning("Failed to read %s: %s", feed_file, exc)
    return DEFAULT_FEEDS


def _request_url(url: str, timeout: float = 10.0) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "RadioAgent/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
        charset = response.headers.get_content_charset("utf-8")
        data = response.read()
        return data.decode(charset, errors="replace")


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        pass
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(value)
    except Exception:
        return None


def clean_text(text: str) -> str:
    text = html.unescape(text)
    if BS4_AVAILABLE and text:
        soup = BeautifulSoup(text, "html.parser")
        text = soup.get_text(" ")
    else:
        text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_feed_fallback(xml_text: str, source: str) -> List[NewsItem]:
    items: List[NewsItem] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("Failed to parse XML for %s: %s", source, exc)
        return items

    entries = root.findall(".//item") + root.findall(".//{*}entry")

    def _first_text(elem: ET.Element, names: Sequence[str]) -> str:
        for name in names:
            text = elem.findtext(name)
            if text:
                return text
        return ""

    def _first_link(elem: ET.Element) -> str:
        text = _first_text(elem, ["link", "{*}link"])
        if text:
            return text
        for link in elem.findall("{*}link"):
            href = link.get("href")
            if href:
                return href
        for link in elem.findall("link"):
            href = link.get("href")
            if href:
                return href
        return ""

    for elem in entries:
        title = _first_text(elem, ["title", "{*}title"]) or "Untitled"
        summary = _first_text(
            elem,
            ["description", "{*}description", "summary", "{*}summary", "content", "{*}content"],
        )
        link = _first_link(elem)
        published_text = _first_text(
            elem,
            ["pubDate", "{*}pubDate", "published", "{*}published", "updated", "{*}updated"],
        )
        items.append(
            NewsItem(
                title=clean_text(title),
                summary=clean_text(summary),
                link=link.strip(),
                source=source,
                published=_parse_datetime(published_text),
            )
        )
    return items


def parse_feed(url: str) -> List[NewsItem]:
    source = urllib.parse.urlparse(url).netloc if "//" in url else url

    if FEEDPARSER_AVAILABLE:
        try:
            parsed = feedparser.parse(url)
            items: List[NewsItem] = []
            for entry in parsed.entries[:20]:
                summary = clean_text(
                    getattr(entry, "summary", "")
                    or getattr(entry, "description", "")
                )
                published = None
                published_parsed = getattr(entry, "published", None)
                if not published_parsed:
                    published_parsed = getattr(entry, "updated", None)
                published = _parse_datetime(published_parsed)
                items.append(
                    NewsItem(
                        title=clean_text(getattr(entry, "title", "Untitled")),
                        summary=summary,
                        link=getattr(entry, "link", ""),
                        source=source,
                        published=published,
                    )
                )
            if items:
                return items
        except Exception as exc:
            logger.warning("feedparser failed for %s: %s", url, exc)

    try:
        xml_text = _request_url(url)
    except urllib.error.URLError as exc:
        logger.warning("Failed to download %s: %s", url, exc)
        return []
    return parse_feed_fallback(xml_text, source)


def dedupe_rank(items: Sequence[NewsItem], limit: int = 8) -> List[NewsItem]:
    seen: Dict[str, NewsItem] = {}
    for item in items:
        key = re.sub(r"\W+", " ", item.title.lower()).strip()
        if not key or key in seen:
            continue
        seen[key] = item

    deduped = list(seen.values())
    deduped.sort(key=lambda it: len(it.summary), reverse=True)
    return deduped[:limit]


class LLM:
    """Wrapper around the OpenAI client with graceful fallback."""

    def __init__(self, model: str = "gpt-4o-mini") -> None:
        self.model = model
        self._client = None
        if OPENAI_AVAILABLE and os.getenv("OPENAI_API_KEY"):
            try:
                self._client = OpenAI()
            except Exception as exc:  # pragma: no cover - network error
                logger.warning("Failed to initialise OpenAI client: %s", exc)
                self._client = None

    def chat(self, messages: List[Dict[str, Any]]) -> str:
        if not self._client:
            logger.info("OPENAI client unavailable – returning placeholder script")
            return json.dumps(
                {
                    "brief": [
                        "No API key available – share two live headlines and tease more updates.",
                        "Invite listeners to stay tuned while the newsroom reconnects.",
                    ],
                    "script": "Good day CAF family! We're experiencing a tech hiccup, so keep the vibes flowing while we reconnect with the news desk.",
                    "social": "Stay locked to Caribbean Air Force Radio – full headlines coming up soon!",
                }
            )

        try:
            completion = self._client.chat.completions.create(  # type: ignore[call-arg]
                model=self.model,
                messages=messages,
                temperature=0.5,
                max_tokens=600,
            )
        except Exception as exc:  # pragma: no cover - network error
            logger.warning("OpenAI chat failed: %s", exc)
            return json.dumps(
                {
                    "brief": [
                        "LLM request failed; deliver the latest headlines live.",
                        "Remind listeners that full stories are coming shortly.",
                    ],
                    "script": "Family, our newsroom link dropped for a moment. I'll bring you fresh updates shortly!",
                    "social": "Hold tight CAF Radio listeners – updates landing soon.",
                }
            )

        text = completion.choices[0].message["content"]  # type: ignore[index]
        if not isinstance(text, str):
            raise RuntimeError("Unexpected completion payload")
        return text


def generate_prompt(items: Sequence[NewsItem]) -> str:
    bulletins = "\n\n".join(item.to_prompt_block() for item in items)
    guidance = textwrap.dedent(
        f"""
        You are the news director for {STATION['name']}, slogan "{STATION['slogan']}".
        Audience: {STATION['target_demo']}.
        Voice guidelines: {VOICE['style']} ({VOICE['pronunciation_notes']}).
        Produce:
        1. A 3-bullet news brief (max 30 words each).
        2. A {LENGTH_SECS}-second radio script with intro, three stories, and sign-off.
        3. A single social media tease (max 150 characters).
        Respond strictly as JSON with keys: brief (list of strings), script (string), social (string).
        News items:
        {bulletins}
        """
    ).strip()
    return guidance


def run_pipeline() -> Dict[str, Any]:
    feeds = _load_feed_urls()
    logger.info("Fetching %d feeds", len(feeds))
    all_items: List[NewsItem] = []
    for url in feeds:
        all_items.extend(parse_feed(url))

    if not all_items:
        logger.warning("No news items retrieved – using placeholder entry")
        all_items = [
            NewsItem(
                title="Technical difficulties",
                summary="Network access unavailable, using canned backup script.",
                link="",
                source="System",
            )
        ]

    items = dedupe_rank(all_items)
    prompt = generate_prompt(items)
    llm = LLM()
    response = llm.chat([
        {"role": "system", "content": "You write broadcast scripts."},
        {"role": "user", "content": prompt},
    ])

    try:
        data = json.loads(response)
    except json.JSONDecodeError:
        logger.warning("LLM response not JSON. Wrapping into fallback structure")
        data = {"brief": [], "script": response, "social": ""}

    return {
        "items": items,
        "output": data,
        "prompt": prompt,
    }


def render_output(result: Dict[str, Any]) -> None:
    print("\n=== CAF Radio – Daily Brief ===\n")
    output = result["output"]
    brief = output.get("brief") or []
    if isinstance(brief, list):
        print("Top Stories:")
        for bullet in brief:
            print(f" • {bullet}")
    print("\nOn-Air Script:\n")
    print(textwrap.fill(output.get("script", ""), width=90))
    print("\nSocial Media Tease:\n")
    print(output.get("social", ""))


def _parse_every(spec: str) -> float:
    spec = spec.strip().lower()
    if not spec:
        raise ValueError("Empty interval spec")
    if ":" in spec:
        hours, minutes = spec.split(":", 1)
        value = int(hours) * 3600 + int(minutes) * 60
        if value <= 0:
            raise ValueError("Interval must be positive")
        return float(value)
    if spec.endswith("h"):
        value = float(spec[:-1]) * 3600
        if value <= 0:
            raise ValueError("Interval must be positive")
        return value
    if spec.endswith("m"):
        value = float(spec[:-1]) * 60
        if value <= 0:
            raise ValueError("Interval must be positive")
        return value
    if spec.isdigit():
        value = float(spec) * 3600
        if value <= 0:
            raise ValueError("Interval must be positive")
        return value
    raise ValueError(f"Unsupported interval: {spec}")


def _seconds_until_next(hour: int, minute: int, *, now: Optional[datetime] = None) -> float:
    if now is None:
        now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def run_daily_at(time_string: str) -> None:
    hour, minute = [int(part) for part in time_string.split(":", 1)]
    while True:
        delay = _seconds_until_next(hour, minute)
        logger.info("Sleeping %.0f seconds until next broadcast", delay)
        time.sleep(max(0, delay))
        result = run_pipeline()
        render_output(result)


def run_every_spec(spec: str) -> None:
    interval = _parse_every(spec)
    if interval <= 0:
        raise ValueError("Interval must be greater than zero")
    while True:
        start = time.time()
        result = run_pipeline()
        render_output(result)
        elapsed = time.time() - start
        sleep_for = max(0.0, interval - elapsed)
        logger.info("Sleeping %.0f seconds before next cycle", sleep_for)
        time.sleep(sleep_for)


def write_templates() -> None:
    requirements = textwrap.dedent(
        """
        feedparser>=6.0
        beautifulsoup4>=4.12
        openai>=1.0
        python-dotenv>=1.0
        """
    ).strip()
    (BASE_DIR / "requirements.txt").write_text(requirements + "\n", encoding="utf-8")

    env_text = textwrap.dedent(
        """
        # Copy to .env and add your API key
        OPENAI_API_KEY=your_api_key_here
        """
    ).strip()
    (BASE_DIR / ".env.example").write_text(env_text + "\n", encoding="utf-8")

    feeds = json.dumps(DEFAULT_FEEDS, indent=2)
    _default_feed_file().write_text(feeds + "\n", encoding="utf-8")

    launch = {
        "version": "0.2.0",
        "configurations": [
            {
                "name": "Run radio agent",
                "type": "python",
                "request": "launch",
                "program": "${workspaceFolder}/agent.py",
                "args": ["--log-level", "INFO"],
            }
        ],
    }
    launch_path = BASE_DIR / ".vscode" / "launch.json"
    launch_path.parent.mkdir(parents=True, exist_ok=True)
    launch_path.write_text(json.dumps(launch, indent=2) + "\n", encoding="utf-8")

    readme_launch = textwrap.dedent(
        """
        # Launching the Radio Agent

        1. Create a virtual environment and install dependencies from requirements.txt.
        2. Copy .env.example to .env and supply your OpenAI API key.
        3. Run `python agent.py --log-level INFO` for a single bulletin.
        4. Use `python agent.py --schedule 07:00` to schedule daily broadcasts or
           `python agent.py --every 4h` for recurring cycles.
        """
    ).strip()
    (BASE_DIR / "README_LAUNCH.txt").write_text(readme_launch + "\n", encoding="utf-8")

    logger.info("Template files written to %s", BASE_DIR)


# ---- Tests ----


def _test_clean_text() -> None:
    assert clean_text("<p>Hello <b>world</b></p>") == "Hello world"


def _test_parse_rss() -> None:
    rss = textwrap.dedent(
        """
        <rss><channel>
        <item><title>A</title><description>First</description><link>http://a</link><pubDate>2024-01-01T00:00:00</pubDate></item>
        </channel></rss>
        """
    ).strip()
    items = parse_feed_fallback(rss, "rss")
    assert len(items) == 1 and items[0].title == "A"


def _test_parse_atom() -> None:
    atom = textwrap.dedent(
        """
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <title>Story</title>
            <summary>Summary</summary>
            <link href="http://example.com" />
            <updated>2024-01-01T00:00:00</updated>
          </entry>
        </feed>
        """
    ).strip()
    items = parse_feed_fallback(atom, "atom")
    assert len(items) == 1 and items[0].link == "http://example.com"


def _test_dedupe_rank() -> None:
    sample = [
        NewsItem("Same Title", "Short", "", "A"),
        NewsItem("Same Title!", "Longer summary", "", "B"),
        NewsItem("Different", "xx", "", "C"),
    ]
    deduped = dedupe_rank(sample)
    assert len(deduped) == 2
    assert any(item.title == "Same Title" for item in deduped)


def _test_llm_skip_no_key() -> None:
    os.environ.pop("OPENAI_API_KEY", None)
    agent = LLM()
    out = agent.chat([{ "role": "user", "content": "hi" }])
    data = json.loads(out)
    assert "brief" in data and "script" in data and "social" in data


def _test_malformed_xml_returns_empty() -> None:
    items = parse_feed_fallback("<rss><channel><item><title>Oops", "bad")
    assert isinstance(items, list)


def _test_parse_time_and_wait_calc() -> None:
    base = datetime(2025, 1, 1, 7, 0, 0)
    assert _seconds_until_next(7, 30, now=base) == 1800
    assert int(_seconds_until_next(6, 0, now=base)) == 23 * 3600


def _test_parse_every() -> None:
    assert _parse_every("4h") == 4 * 3600
    assert _parse_every("240m") == 240 * 60
    assert _parse_every("4") == 4 * 3600
    assert _parse_every("0:30") == 30 * 60
    try:
        _parse_every("0h")
    except ValueError:
        pass
    else:
        raise AssertionError("Expected ValueError for zero interval")


def _test_base_dir_fallback() -> None:
    old = os.getenv("CAF_FORCE_CWD")
    try:
        os.environ["CAF_FORCE_CWD"] = "1"
        cwd = Path(os.getcwd()).resolve()
        assert _compute_base_dir() == cwd
    finally:
        if old is None:
            os.environ.pop("CAF_FORCE_CWD", None)
        else:
            os.environ["CAF_FORCE_CWD"] = old


def run_tests() -> None:
    configure_logging(console_level="CRITICAL", no_file=True)
    _test_clean_text()
    _test_parse_rss()
    _test_parse_atom()
    _test_dedupe_rank()
    _test_llm_skip_no_key()
    _test_malformed_xml_returns_empty()
    _test_parse_time_and_wait_calc()
    _test_parse_every()
    _test_base_dir_fallback()
    print("All tests passed ✔")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Caribbean Air Force Radio Agent")
    group = parser.add_mutually_exclusive_group()
    parser.add_argument("--test", action="store_true", help="Run built-in tests and exit")
    parser.add_argument("--quiet", action="store_true", help="Only warnings/errors on console")
    parser.add_argument("--log-level", type=str, help="Console log level")
    parser.add_argument("--no-file-log", action="store_true", help="Disable file logging")
    parser.add_argument("--init", action="store_true", help="Write launch templates")
    group.add_argument("--schedule", type=str, help="Run daily at HH:MM")
    group.add_argument("--every", type=str, help="Run repeatedly at interval (e.g. 4h, 30m, 0:30)")
    args = parser.parse_args(argv)

    if args.quiet:
        configure_logging(console_level="WARNING", no_file=args.no_file_log)
    elif args.log_level:
        configure_logging(console_level=args.log_level, no_file=args.no_file_log)
    else:
        configure_logging(no_file=args.no_file_log)

    if args.test:
        run_tests()
        return 0

    if args.init:
        write_templates()
        return 0

    if args.schedule:
        run_daily_at(args.schedule)
        return 0

    if args.every:
        try:
            run_every_spec(args.every)
        except ValueError as exc:
            logger.error("Invalid --every value: %s", exc)
            return 2
        return 0

    result = run_pipeline()
    render_output(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())

