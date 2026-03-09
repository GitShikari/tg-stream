#Telegram Stream Player v10

A self-hosted media player for streaming files directly from Telegram channels — right in your browser.

Built with Python, [Telethon](https://github.com/LonamiWebs/Telethon), and [Plyr.js](https://plyr.io/). Serves a local web UI at `http://127.0.0.1:8766`.

---

## Features

- **Multi-channel support** — browse and switch between multiple Telegram channels
- **HTTP range server** — smooth seeking and streaming for video/audio files
- **PDF viewer** — renders PDFs via a local [PDF.js](https://mozilla.github.io/pdf.js/) install (`~/.pdfjs`)
- **Search** — search across channel media
- **Watch history & progress** — resume where you left off; progress persisted to `~/.tg_stream_data.json`
- **Resume toast** — notified when resumable progress is available
- **Resizable sidebar** — adjustable layout for comfortable browsing

---

## Stack

| Layer | Technology |
|-------|------------|
| Backend | Python, [aiohttp](https://docs.aiohttp.org/) |
| Telegram client | [Telethon](https://github.com/LonamiWebs/Telethon) |
| Frontend player | [Plyr.js](https://plyr.io/) |
| PDF rendering | [PDF.js](https://mozilla.github.io/pdf.js/) (local, `~/.pdfjs`) |
| Data persistence | JSON (`~/.tg_stream_data.json`) |

---

## Requirements

- Python 3.8+
- A Telegram account with API credentials ([get them here](https://my.telegram.org/apps))
- PDF.js installed locally at `~/.pdfjs` (for PDF support)

Install Python dependencies:

```bash
pip install aiohttp telethon
```

---

## Setup

1. **Clone / download** `tg_stream.py`

2. **Configure your Telegram API credentials** — on first run you'll be prompted for your `api_id`, `api_hash`, and phone number. A session file is created locally.

3. **Install PDF.js** (optional, for PDF viewing):

```bash
# Download and extract PDF.js to ~/.pdfjs
curl -L https://github.com/mozilla/pdf.js/releases/latest/download/pdfjs-*-dist.zip -o pdfjs.zip
unzip pdfjs.zip -d ~/.pdfjs
```

---

## Usage

```bash
python tg_stream.py
```

Then open your browser at:

```
http://127.0.0.1:8766
```

### In the UI

- Use the **sidebar** to browse channels and their media files
- Click any file to start streaming
- **Search** filters media within the active channel
- A **resume toast** appears when saved progress is detected
- Drag the sidebar edge to **resize** it

---

## Data & Persistence

Watch history and playback progress are stored locally:

```
~/.tg_stream_data.json
```

This file is read on startup and written automatically as you watch. Delete it to reset all history.

---

## Notes

- Streams are served over HTTP with **range request** support, enabling seeking without downloading the full file first.
- No data leaves your machine — the app communicates directly with Telegram using your own credentials.
- PDF.js is loaded from a local path (`~/.pdfjs`) rather than a CDN, so it works fully offline.

---
