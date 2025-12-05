# Radio AI Agent

The **Caribbean Air Force Radio Agent** is a ready-to-air newsroom helper.  It
collects stories from Caribbean-focused RSS feeds, deduplicates and ranks them,
then asks an OpenAI model to deliver a broadcast-ready script, social media
tease, and quick brief.  When network access or an API key is missing the agent
falls back to safe placeholder copy so the mic never goes cold.

## Features

- Resilient news ingestion using ``feedparser`` when available and a pure-stdlib
  XML fallback when not.
- Built-in logging that is quiet on the console by default but can write
  rotating file logs inside ``logs/``.
- Scheduler helpers: run once, every N hours/minutes (``--every``) or daily at a
  specific local time (``--schedule``).
- ``--init`` generator that writes ``requirements.txt``, ``.env.example``,
  ``feeds.json`` and VS Code launch settings.
- Offline-friendly: without API access the agent produces placeholder copy and
  keeps the show moving.
- Lightweight tests runnable via ``python agent.py --test`` (no network required).

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python agent.py --init      # write helper files (optional)
pip install -r requirements.txt
cp .env.example .env        # add your OPENAI_API_KEY
python agent.py --log-level INFO
```

To schedule a morning bulletin:

```bash
python agent.py --schedule 07:00 --log-level INFO
```

To repeat the broadcast every four hours:

```bash
python agent.py --every 4h --log-level INFO
```

## Configuration

- ``feeds.json`` (created by ``--init``) overrides the default RSS list.
- Set ``CAF_BASE_DIR`` to relocate output directories.
- ``--quiet`` limits console output to warnings and above; ``--no-file-log``
  disables writing logs to disk.

## Testing

Run the internal smoke tests:

```bash
python agent.py --test
```

## Real-world smoke run

Even without outbound network access the agent stays on-air.  Running the
default pipeline in this sandboxed environment yields logged warnings for each
RSS endpoint and then prints the placeholder bulletin, demonstrating the
"never-silent" fallback:

```bash
$ python agent.py
2025-09-27 23:14:03,032 WARNING radio.agent | Failed to download https://www.caribbeannews.net/rss/: <urlopen error Tunnel connection failed: 403 Forbidden>
2025-09-27 23:14:03,037 WARNING radio.agent | Failed to download https://jamaica-gleaner.com/feed/rss.xml: <urlopen error Tunnel connection failed: 403 Forbidden>
2025-09-27 23:14:03,041 WARNING radio.agent | Failed to download https://www.caribjournal.com/feed/: <urlopen error Tunnel connection failed: 403 Forbidden>

=== CAF Radio – Daily Brief ===

Top Stories:
 • No API key available – share two live headlines and tease more updates.
 • Invite listeners to stay tuned while the newsroom reconnects.

On-Air Script:

Good day CAF family! We're experiencing a tech hiccup, so keep the vibes flowing while we reconnect with the news desk.

Social Media Tease:

Stay locked to Caribbean Air Force Radio – full headlines coming up soon!
```

## Troubleshooting

- No API key?  The agent still prints a placeholder script and brief.
- No network?  The feed fetcher logs warnings and falls back to a backup
  bulletin.
- Want to inspect the prompt?  The returned data structure from ``run_pipeline``
  (used in ``render_output``) includes the prompt and deduplicated items.

## License

MIT – use it on-air, in the lab, or as inspiration for your own newsroom
automation projects.

