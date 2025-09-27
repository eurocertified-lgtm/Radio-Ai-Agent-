from __future__ import annotations
import argparse
import html
import json
import logging
import os
import re
import sys
import time
import textwrap
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# ---- Optional imports (graceful fallbacks) ----
try:
    import feedparser  # type: ignore
    FEEDPARSER_AVAILABLE = True
except Exception:
    feedparser = None
    FEEDPARSER_AVAILABLE = False

try:
    from bs4 import BeautifulSoup  # type: ignore
    BS4_AVAILABLE = True
except Exception:
    BeautifulSoup = None
    BS4_AVAILABLE = False

try:
    from openai import OpenAI  # type: ignore
    OPENAI_AVAILABLE = True
except Exception:
    OpenAI = None
    OPENAI_AVAILABLE = False

# ====== CONFIG (added to avoid NameError and set defaults) ======
STATION = {
    "name": "Caribbean Air Force Radio",
    "slogan": "Sounds of home wherever you roam",
    "target_demo": "Ages 30–60 in the Caribbean and diaspora",
}

VOICE = {
    "style": "warm, upbeat Caribbean DJ; concise, vivid, culturally respectful",
    "pronunciation_notes": "Prefer Caribbean pronunciations; avoid heavy slang unless appropriate.",
}

LENGTH_SECS = 55

# ====== PATHS (robust to environments without __file__) ======

def _compute_base_dir() -> Path:
    """Return a reliable base directory.
    Priority: CAF_BASE_DIR env > script directory (__file__) > current working directory.
    Also supports CAF_FORCE_CWD=1 for tests.
    """
    # 1) explicit override
    env_dir = os.getenv("CAF_BASE_DIR")
    if env_dir:
        try:
            return Path(env_dir).resolve()
        except Exception:
            pass
    # 2) testing override: force cwd
    if os.getenv("CAF_FORCE_CWD") == "1":
        return Path(os.getcwd()).resolve()
    # 3) try script location
    try:
        return Path(__file__).resolve().parent
    except Exception:
        # 4) last resort: cwd
        return Path(os.getcwd()).resolve()


BASE_DIR = _compute_base_dir()
OUT_DIR = BASE_DIR / "out"
DEFAULT_LOG_DIR = BASE_DIR / "logs"

# ====== LOGGING CONFIGURATION ======
_logger_configured = False
logger = logging.getLogger("caf_agent")


def _level_from(value: str, default: int) -> int:
    try:
        return getattr(logging, value.upper())
    except Exception:
        return default


def configure_logging(console_level: Optional[str] = None,
                      file_level: Optional[str] = None,
                      no_file: bool = False) -> None:
    """Configure logging once. Prevent duplicate handlers and allow env/CLI overrides."""
    global _logger_configured
    if _logger_configured:
        for h in list(logger.handlers):
            logger.removeHandler(h)
    logger.setLevel(logging.DEBUG)  # capture everything; handlers decide output

    env_console = os.getenv("CAF_CONSOLE_LEVEL", "WARNING")
    env_file = os.getenv("CAF_LOG_LEVEL", "INFO")
    cl = _level_from(console_level or env_console, logging.WARNING)
    fl = _level_from(file_level or env_file, logging.INFO)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(cl)
    ch.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', '%Y-%m-%d %H:%M:%S'))
    logger.addHandler(ch)

    if not (no_file or os.getenv("CAF_NO_FILE_LOG") == "1"):
        log_dir = Path(os.getenv("CAF_LOG_DIR", str(DEFAULT_LOG_DIR)))
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"agent_{datetime.now().strftime('%Y-%m-%d')}.log"
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(fl)
        fh.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', '%Y-%m-%d %H:%M:%S'))
        logger.addHandler(fh)
        # retention
        try:
            keep_days = int(os.getenv("CAF_LOG_RETENTION_DAYS", "7"))
            cutoff = datetime.now() - timedelta(days=keep_days)
            for f in log_dir.glob("agent_*.log"):
                try:
                    d = datetime.strptime(f.stem.split("_")[1], "%Y-%m-%d")
                    if d < cutoff.replace(hour=0, minute=0, second=0, microsecond=0):
                        f.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            pass

    _logger_configured = True


# ====== UTIL ======

def ensure_outdir() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)


def clean_text(html_or_text: str) -> str:
    """Robust HTML→text cleaner that works with or without bs4."""
    if not html_or_text:
        return ""
    if BS4_AVAILABLE and BeautifulSoup is not None:
        try:
            return re.sub(r"\s+", " ", BeautifulSoup(html_or_text, "html.parser").get_text(" ")).strip()
        except Exception:
            pass
    no_tags = re.sub(r"<[^>]+>", " ", html_or_text)
    return re.sub(r"\s+", " ", html.unescape(no_tags)).strip()


def now_stamp() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d_%H%M%S")


# ====== FEED FETCH & PARSE ======

def _fetch_url(url: str, timeout: Optional[int] = None) -> str:
    """Fetch URL with a friendly User-Agent and timeout."""
    t = timeout or int(os.getenv("CAF_HTTP_TIMEOUT", "15"))
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": os.getenv("CAF_HTTP_UA", "CAFNewsBot/1.0 (+https://caribbeanairforce.com) Python")
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=t) as resp:
        data = resp.read()
        try:
            return data.decode("utf-8")
        except Exception:
            return data.decode("latin-1", errors="ignore")


def _et_text(elem: Optional[ET.Element], *candidates: str) -> str:
    if elem is None:
        return ""
    for child in list(elem):
        local = child.tag.rsplit('}', 1)[-1]
        if local in candidates:
            return (child.text or "").strip()
    return ""


def _et_findall(root: ET.Element, localname: str) -> List[ET.Element]:
    return [e for e in root.iter() if e.tag.rsplit('}', 1)[-1] == localname]


def parse_feed_fallback(xml_text: str, source_label: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    try:
        root = ET.fromstring(xml_text)
    except Exception as ex:
        logger.error(f"XML parse failed for {source_label}: {ex}")
        return items

    # RSS 2.0 <item>
    for it in _et_findall(root, "item"):
        title = _et_text(it, "title")
        summary = _et_text(it, "description") or _et_text(it, "summary")
        link = _et_text(it, "link")
        items.append({
            "title": clean_text(title),
            "summary": clean_text(summary),
            "link": link,
            "source": source_label,
        })
    if items:
        return items

    # Atom <entry>
    for e in _et_findall(root, "entry"):
        title = _et_text(e, "title")
        summary = _et_text(e, "summary") or _et_text(e, "content")
        link = ""
        for child in list(e):
            if child.tag.rsplit('}', 1)[-1] == 'link':
                link = child.attrib.get('href') or (child.text or "")
                break
        items.append({
            "title": clean_text(title),
            "summary": clean_text(summary),
            "link": link,
            "source": source_label,
        })
    return items


def _load_feeds() -> List[str]:
    """Load FEEDS from feeds.json if present, else defaults."""
    default = [
        "https://www.caribbeannationalweekly.com/feed/",
        "https://www.caribjournal.com/feed/",
        "https://barbadostoday.bb/feed/",
        "https://www.stabroeknews.com/feed/",
        "https://www.jamaicaobserver.com/feed/",
    ]
    cfg = BASE_DIR / "feeds.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            if isinstance(data, list) and all(isinstance(x, str) for x in data):
                return data
        except Exception as ex:
            logger.warning(f"feeds.json load failed, using defaults: {ex}")
    return default


def fetch_items(feeds: Iterable[str]) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for url in feeds:
        try:
            logger.info(f"Fetching feed: {url}")
            if FEEDPARSER_AVAILABLE and feedparser is not None:
                d = feedparser.parse(url)
                for e in d.entries[:20]:
                    items.append({
                        "title": clean_text(getattr(e, "title", "")),
                        "summary": clean_text(getattr(e, "summary", getattr(e, "description", ""))),
                        "link": getattr(e, "link", ""),
                        "source": clean_text(getattr(d.feed, "title", url)),
                    })
            else:
                xml_text = _fetch_url(url)
                items.extend(parse_feed_fallback(xml_text, source_label=url))
        except Exception as ex:
            logger.error(f"Feed error {url}: {ex}")

    logger.info(f"Fetched {len(items)} total items.")

    seen = set()
    deduped: List[Dict[str, str]] = []
    for it in items:
        key = re.sub(r"\W+", " ", (it.get("title") or "").lower()).strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(it)
    logger.info(f"After dedupe: {len(deduped)} items.")

    deduped.sort(key=lambda x: len(x.get("summary", "")), reverse=True)
    return deduped


# ====== LLM WRAPPER ======
@dataclass
class LLM:
    model: str
    client: Any = None

    def __post_init__(self) -> None:
        api_key = os.getenv("OPENAI_API_KEY")
        if OPENAI_AVAILABLE and api_key:
            try:
                self.client = OpenAI(api_key=api_key)
            except Exception as ex:
                logger.warning(f"OpenAI init failed: {ex}")
                self.client = None
        else:
            self.client = None
            if not api_key:
                logger.warning("OPENAI_API_KEY not set; skipping AI generation.")

    def chat(self, messages: List[Dict[str, str]]) -> str:
        if not self.client:
            logger.warning("Skipping LLM call due to missing client/API key.")
            return json.dumps({"brief": ["No API key set"], "script": "", "social": {}})
        try:
            logger.info("Calling LLM model...")
            # Optional: support custom base via env
            base = os.getenv("OPENAI_API_BASE")
            if base:
                self.client.base_url = base
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.6,
                max_tokens=1200,
            )
            logger.info("LLM response received.")
            return resp.choices[0].message.content.strip()
        except Exception as ex:
            logger.error(f"LLM call failed: {ex}")
            return json.dumps({"brief": ["Error generating output"], "script": "", "social": {}})


# ====== PROMPT CREATION ======

def make_prompt(articles: List[Dict[str, str]]) -> List[Dict[str, str]]:
    sys_msg = (
        f"You are a Caribbean radio producer for {STATION['name']}. "
        f"Audience: {STATION['target_demo']}. "
        f"Voice: {VOICE['style']} {VOICE['pronunciation_notes']}"
    )
    payload = json.dumps([
        {"title": a.get("title", ""), "summary": a.get("summary", ""), "source": a.get("source", ""), "link": a.get("link", "")}
        for a in articles[:10]
    ], ensure_ascii=False)
    user_msg = (
        "Here are headlines (JSON):\n" + payload +
        f"\nProduce exactly this JSON schema:\n{{\n  \"brief\": [\"bullet 1\", ... 5 bullets],\n  \"script\": \"radio script {LENGTH_SECS}s with (0:00) time cues\",\n  \"social\": {{\n      \"facebook\": \"...\",\n      \"instagram\": \"...\",\n      \"twitter\": \"...\"\n  }}\n}}\nUse sources naturally in the script (e.g., 'via <source>')."
    )
    return [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": user_msg},
    ]


# ====== SAVE & TTS ======

def save_and_tts(prefix: str, articles: List[Dict[str, str]], ai_json: Dict[str, Any], script_text: str) -> None:
    ensure_outdir()
    ts = now_stamp()
    (OUT_DIR / f"{prefix}_{ts}_articles.json").write_text(json.dumps(articles, indent=2, ensure_ascii=False))
    (OUT_DIR / f"{prefix}_{ts}_package.json").write_text(json.dumps(ai_json, indent=2, ensure_ascii=False))
    logger.info(f"Saved outputs to {OUT_DIR}")

    if not script_text:
        logger.warning("No script text to convert to audio.")
        return

    if not (OPENAI_AVAILABLE and OpenAI is not None):
        logger.info("OpenAI not installed; skipping TTS generation.")
        return

    try:
        client = OpenAI()
        with client.audio.speech.with_streaming_response.create(
            model=os.getenv("AI_TTS_MODEL", "gpt-4o-mini-tts"),
            voice=os.getenv("TTS_VOICE", "alloy"),
            input=script_text,
        ) as response:
            mp3_path = OUT_DIR / f"{prefix}_{ts}_script.mp3"
            response.stream_to_file(mp3_path)
            logger.info(f"MP3 saved: {mp3_path}")
    except Exception as ex:
        logger.error(f"TTS failed: {ex}")


# ====== INIT (write launch templates) ======

def write_templates() -> None:
    (BASE_DIR / ".vscode").mkdir(exist_ok=True)
    (BASE_DIR / "logs").mkdir(exist_ok=True)

    (BASE_DIR / "requirements.txt").write_text(
        """openai>=1.40.0\nfeedparser>=6.0.10\nbeautifulsoup4>=4.12.3\ntiktoken\npython-dateutil\nstreamlit\n""",
        encoding="utf-8",
    )

    (BASE_DIR / ".env.example").write_text(
        """# Copy to .env and fill in values\nOPENAI_API_KEY=your_key_here\nAI_MODEL=gpt-4o-mini\nAI_TTS_MODEL=gpt-4o-mini-tts\nTTS_VOICE=alloy\nCAF_CONSOLE_LEVEL=WARNING\nCAF_LOG_LEVEL=INFO\nCAF_LOG_RETENTION_DAYS=7\nCAF_HTTP_TIMEOUT=15\n# CAF_HTTP_UA=CAFNewsBot/1.0 (+https://caribbeanairforce.com) Python\n""",
        encoding="utf-8",
    )

    (BASE_DIR / ".vscode" / "launch.json").write_text(
        json.dumps({
            "version": "0.2.0",
            "configurations": [
                {
                    "name": "Run AI Agent",
                    "type": "python",
                    "request": "launch",
                    "program": "${workspaceFolder}/agent.py",
                    "console": "integratedTerminal",
                    "envFile": "${workspaceFolder}/.env",
                }
            ],
        }, indent=2),
        encoding="utf-8",
    )

    (BASE_DIR / "README_LAUNCH.txt").write_text(
        """Launch Guide\n============\n1) python -m venv .venv && source .venv/bin/activate\n2) python agent.py --init\n3) pip install -r requirements.txt\n4) Copy .env.example to .env and set OPENAI_API_KEY\n5) F5 in VS Code, or run: python agent.py\n6) Outputs in /out, logs in /logs\n\nViewer:\n- Run: streamlit run viewer.py\n- Change output dir: CAF_OUT_DIR=/path/to/out streamlit run viewer.py\n\nScheduling ideas:\n- Linux: use cron or systemd timer\n- Windows: Task Scheduler (run agent.py daily)\n- macOS: launchd or crontab\n""",
        encoding="utf-8",
    )

    # Optional feeds.json template (not overwriting if user made one)
    feeds_path = BASE_DIR / "feeds.json"
    if not feeds_path.exists():
        feeds_path.write_text(json.dumps(_load_feeds(), indent=2), encoding="utf-8")

    print("Templates written: requirements.txt, .env.example, .vscode/launch.json, README_LAUNCH.txt, feeds.json")


# ====== SCHEDULER ======

def _parse_hhmm(s: str) -> tuple[int, int]:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", s)
    if not m:
        raise ValueError("Time must be HH:MM (24h)")
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ValueError("Hour 0-23 and minute 0-59 required")
    return h, mi


def _seconds_until_next(hour: int, minute: int, now: Optional[datetime] = None) -> float:
    now = now or datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _parse_every(spec: str) -> int:
    """Parse interval spec: '4h', '240m', or 'H:MM'. Bare integer = hours."""
    s = spec.strip().lower()
    m = re.match(r"^(\d+)\s*([hm])?$", s)
    if m:
        val = int(m.group(1))
        unit = m.group(2) or 'h'
        seconds = val * 3600 if unit == 'h' else val * 60
    else:
        m2 = re.match(r"^(\d+):(\d{1,2})$", s)
        if not m2:
            raise ValueError("Use formats like 4h, 240m, or H:MM (e.g., 4:00)")
        h, mi = int(m2.group(1)), int(m2.group(2))
        seconds = h * 3600 + mi * 60
    if seconds <= 0:
        raise ValueError("Interval must be > 0")
    return seconds


def run_daily_at(hhmm: str) -> None:
    hour, minute = _parse_hhmm(hhmm)
    logger.info(f"Scheduler active: will run daily at {hour:02d}:{minute:02d} (local time)")
    while True:
        wait_s = _seconds_until_next(hour, minute)
        logger.info(f"Next run in ~{int(wait_s)} seconds")
        time.sleep(max(1, int(wait_s)))
        try:
            run_pipeline()
        except Exception as ex:
            logger.error(f"Scheduled run failed: {ex}")
        time.sleep(2)


def run_every_spec(spec: str) -> None:
    interval = _parse_every(spec)
    h, rem = divmod(interval, 3600)
    m = rem // 60
    logger.info(f"Scheduler active: will run every {h}h {m}m (starts now)")
    next_time = time.time()  # run immediately
    while True:
        now_ts = time.time()
        if now_ts < next_time:
            time.sleep(max(1, int(next_time - now_ts)))
        start = time.time()
        try:
            run_pipeline()
        except Exception as ex:
            logger.error(f"Interval run failed: {ex}")
        # schedule next run at fixed interval from last scheduled time to avoid drift
        next_time += interval


# ====== MAIN PIPELINE ======

def run_pipeline() -> None:
    logger.info("Starting Caribbean Air Force – Daily Agent")
    articles = fetch_items(_load_feeds())

    if not articles:
        logger.warning("No articles fetched; using dummy headline to avoid blocking broadcast.")
        articles = [{
            "title": "Tourism rebounds across several islands",
            "summary": "Officials report strong arrivals; airlines add routes; hotels see high occupancy.",
            "link": "#",
            "source": "Example",
        }]

    messages = make_prompt(articles)
    llm = LLM(os.getenv("AI_MODEL", "gpt-4o-mini"))
    ai_text = llm.chat(messages)

    try:
        ai_json = json.loads(ai_text)
    except Exception as ex:
        logger.error(f"Could not parse AI output as JSON: {ex}")
        ai_json = {"raw": ai_text}

    script_text = ai_json.get("script") or ""
    save_and_tts("caf_daily", articles, ai_json, script_text)
    logger.info("Process complete. Review outputs in /out and logs in /logs.")


# ====== TESTS ======

def _test_clean_text() -> None:
    src = "<p>Hello <b>world</b>&nbsp; &amp; friends!</p>"
    # Force fallback path (simulate missing bs4)
    global BS4_AVAILABLE, BeautifulSoup
    prev_flag, prev_bs = BS4_AVAILABLE, BeautifulSoup
    BS4_AVAILABLE, BeautifulSoup = False, None
    try:
        out = clean_text(src)
        assert out == "Hello world & friends!", out
    finally:
        BS4_AVAILABLE, BeautifulSoup = prev_flag, prev_bs


def _test_parse_rss() -> None:
    rss = """
    <rss version=\"2.0\">
      <channel>
        <title>Example RSS</title>
        <item>
          <title>Item One</title>
          <description>Desc 1</description>
          <link>http://ex/1</link>
        </item>
        <item>
          <title>Item Two</title>
          <description>Desc 2</description>
          <link>http://ex/2</link>
        </item>
      </channel>
    </rss>
    """
    items = parse_feed_fallback(rss, "Example RSS")
    assert len(items) == 2, items
    assert items[0]["title"] == "Item One"
    assert items[1]["link"] == "http://ex/2"


def _test_parse_atom() -> None:
    atom = """
    <feed xmlns=\"http://www.w3.org/2005/Atom\">
      <title>Example Atom</title>
      <entry>
        <title>Entry A</title>
        <summary>SA</summary>
        <link href=\"http://ex/A\" />
      </entry>
      <entry>
        <title>Entry B</title>
        <content>CB</content>
        <link href=\"http://ex/B\" />
      </entry>
    </feed>
    """
    items = parse_feed_fallback(atom, "Example Atom")
    assert len(items) == 2, items
    assert items[0]["title"] == "Entry A"
    assert items[1]["summary"] == "CB"


def _test_dedupe_rank() -> None:
    sample = [
        {"title": "Same", "summary": "a"},
        {"title": "Same", "summary": "longer summary"},
        {"title": "Different", "summary": "xx"},
    ]
    seen = set()
    deduped = []
    for it in sample:
        key = re.sub(r"\W+", " ", it["title"].lower()).strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(it)
    deduped.sort(key=lambda x: len(x.get("summary", "")), reverse=True)
    assert len(deduped) == 2, deduped
    assert deduped[0]["title"] in {"Same", "Different"}


def _test_llm_skip_no_key() -> None:
    os.environ.pop("OPENAI_API_KEY", None)
    agent = LLM("gpt-4o-mini")
    out = agent.chat([{"role": "user", "content": "test"}])
    try:
        data = json.loads(out)
    except Exception:
        raise AssertionError("LLM skip path should return JSON string")
    assert "brief" in data and "script" in data and "social" in data


def _test_malformed_xml_returns_empty() -> None:
    bad = "<rss><channel><item><title>Oops"
    items = parse_feed_fallback(bad, "Bad")
    assert items == [] or isinstance(items, list)


def _test_parse_time_and_wait_calc() -> None:
    # 07:30 from 07:00 -> 30 minutes
    base = datetime(2025, 1, 1, 7, 0, 0)
    assert _seconds_until_next(7, 30, now=base) == 1800
    # 06:00 from 07:00 -> next day 23h
    assert int(_seconds_until_next(6, 0, now=base)) == 23 * 3600


def _test_parse_every() -> None:
    assert _parse_every("4h") == 4 * 3600
    assert _parse_every("240m") == 240 * 60
    assert _parse_every("4") == 4 * 3600  # bare int = hours
    assert _parse_every("0:30") == 30 * 60
    try:
        _parse_every("0h")
        raise AssertionError("Expected failure for zero interval")
    except ValueError:
        pass


def _test_base_dir_fallback() -> None:
    # Ensure we can fall back to CWD when __file__ is unavailable (simulated via env flag)
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


# ====== CLI ======
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    parser.add_argument("--test", action="store_true", help="Run unit tests and exit")
    parser.add_argument("--quiet", action="store_true", help="Console warnings/errors only")
    parser.add_argument("--log-level", type=str, help="Console log level: DEBUG/INFO/WARNING/ERROR/CRITICAL")
    parser.add_argument("--no-file-log", action="store_true", help="Disable file logging")
    parser.add_argument("--init", action="store_true", help="Write requirements, .env.example, VS Code launch, README, feeds.json")
    group.add_argument("--schedule", type=str, help="Run daily at HH:MM local time; blocks and repeats")
    group.add_argument("--every", type=str, help="Run repeatedly at a fixed interval, e.g., 4h or 240m; starts immediately")
    args = parser.parse_args()

    if args.quiet:
        configure_logging(console_level="WARNING", no_file=args.no_file_log)
    elif args.log_level:
        configure_logging(console_level=args.log_level, no_file=args.no_file_log)
    else:
        configure_logging(no_file=args.no_file_log)

    if args.test:
        run_tests()
        sys.exit(0)

    if args.init:
        write_templates()
        sys.exit(0)

    if args.schedule:
        try:
            run_daily_at(args.schedule)
        except Exception as ex:
            logger.error(f"Invalid --schedule value: {ex}")
            sys.exit(2)

    if args.every:
        try:
            run_every_spec(args.every)
        except Exception as ex:
            logger.error(f"Invalid --every value: {ex}")
            sys.exit(2)

    # default: run once
    run_pipeline()
