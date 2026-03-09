#!/usr/bin/env python3
"""
Telegram Stream Player v10.0
- Multi-channel
- PDF viewing (PDF.js)
- Search across all media
- Watch history (persisted to ~/.tg_stream_data.json)
- Continue watching (saves position every 5s)
- Plyr.js player
- Resizable sidebar
"""

import asyncio
import os
import json
import time
import webbrowser
from pathlib import Path
from aiohttp import web
from telethon import TelegramClient


# ── Credentials ───────────────────────────────────────────────
api_id   = 3477714 # replace with your api_id from telegram.org
api_hash = '1264d2d7d397c4635147ee25ab5808d1' # replace with your api_hash from telegram.org
#CHANNEL_ID = -1002792665255   # replace with your private channel ID
# ──────────────────────────────────────────────────────────────

# Format: { 'name': 'Display Name', 'id': CHANNEL_ID, 'icon': 'emoji' }
CHANNELS = [
    { 'name': 'ABC Lectures',   'id': -1002792665255, 'icon': '📚' },
    { 'name': 'AXL Lectures',   'id': -1002260648725, 'icon': '🎬' },
   # { 'name': 'Channel Three', 'id': -1001122334455, 'icon': '🎵' },
]
# ──────────────────────────────────────────────────────────────

HTTP_HOST  = '127.0.0.1'
HTTP_PORT  = 8766
DATA_FILE  = Path.home() / '.tg_stream_data.json'

current_message  = None
tg_client        = None
media_cache      = {}   # channel_idx -> list of media items

# ── Persistent data ───────────────────────────────────────────
def load_data():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {'history': [], 'progress': {}}

def save_data(data):
    try:
        DATA_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"Warning: could not save data: {e}")

app_data = load_data()

def media_key(ch_idx, med_idx):
    return f"{ch_idx}_{med_idx}"

def record_history(ch_idx, med_idx, title, mime, size, dur):
    key = media_key(ch_idx, med_idx)
    entry = {
        'key':     key,
        'ch_idx':  ch_idx,
        'med_idx': med_idx,
        'title':   title,
        'mime':    mime,
        'size':    size,
        'dur':     dur,
        'ch_name': CHANNELS[ch_idx]['name'],
        'ch_icon': CHANNELS[ch_idx]['icon'],
        'ts':      int(time.time()),
    }
    # Remove duplicate
    app_data['history'] = [h for h in app_data['history'] if h['key'] != key]
    app_data['history'].insert(0, entry)
    app_data['history'] = app_data['history'][:100]   # keep last 100
    save_data(app_data)

def save_progress(key, position, duration):
    app_data['progress'][key] = {
        'position': position,
        'duration': duration,
        'ts':       int(time.time()),
    }
    save_data(app_data)

# ── Helpers ───────────────────────────────────────────────────
def get_ext(name, mime):
    ext = os.path.splitext(name)[-1] if name else ''
    if not ext:
        ext = {
            'video/mp4':        '.mp4',
            'video/x-matroska': '.mkv',
            'video/webm':       '.webm',
            'video/3gpp':       '.3gp',
            'video/quicktime':  '.mov',
            'audio/mpeg':       '.mp3',
            'audio/ogg':        '.ogg',
            'audio/mp4':        '.m4a',
            'application/pdf':  '.pdf',
        }.get(mime, '.mp4')
    return ext

def fmt_time(secs):
    secs = max(0, int(secs or 0))
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'

def fmt_size(size):
    if not size: return '?'
    mb = size / 1024 / 1024
    return f'{mb:.1f} MB'

def mime_icon(mime):
    if 'video'       in mime: return '🎬'
    if 'audio'       in mime: return '🎵'
    if 'pdf'         in mime: return '📄'
    if 'image'       in mime: return '🖼️'
    return '📁'

def mime_type_label(mime):
    if 'video' in mime: return 'video'
    if 'audio' in mime: return 'audio'
    if 'pdf'   in mime: return 'pdf'
    return 'other'

# ── HTTP Range Stream ─────────────────────────────────────────
async def handle_stream(request):
    global current_message, tg_client
    if current_message is None:
        return web.Response(status=404)

    msg      = current_message
    filesize = getattr(msg.file, 'size', 0) or 0
    mime     = getattr(msg.file, 'mime_type', '') or 'application/octet-stream'

    rng = request.headers.get('Range')
    if rng:
        parts = rng.replace('bytes=', '').split('-')
        start = int(parts[0]) if parts[0] else 0
        end   = int(parts[1]) if len(parts) > 1 and parts[1] else filesize - 1
    else:
        start, end = 0, filesize - 1

    end  = min(end, filesize - 1)
    size = end - start + 1

    resp = web.StreamResponse(
        status=206 if rng else 200,
        headers={
            'Content-Type':                mime,
            'Content-Length':              str(size),
            'Content-Range':               f'bytes {start}-{end}/{filesize}',
            'Accept-Ranges':               'bytes',
            'Access-Control-Allow-Origin': '*',
            'Cache-Control':               'no-cache',
        }
    )
    await resp.prepare(request)
    async for chunk in tg_client.iter_download(
        msg.media, offset=start, limit=size, chunk_size=512 * 1024
    ):
        try:
            await resp.write(chunk)
        except Exception:
            break
    return resp

# ── Channel fetch ─────────────────────────────────────────────
async def handle_channel(request):
    global tg_client, media_cache
    idx = int(request.match_info.get('idx', 0))
    if idx < 0 or idx >= len(CHANNELS):
        return web.json_response({'ok': False, 'error': 'Invalid channel'}, status=400)

    if idx in media_cache:
        return web.json_response({'ok': True, 'items': media_cache[idx], 'title': CHANNELS[idx]['name']})

    ch = CHANNELS[idx]
    try:
        entity = await tg_client.get_entity(ch['id'])
        items  = []
        async for message in tg_client.iter_messages(entity, limit=100):
            if message.media and message.file:
                caption  = (message.text or '').replace('\n', ' ')
                name     = getattr(message.file, 'name',      '') or ''
                mime     = getattr(message.file, 'mime_type', '') or 'unknown'
                size     = getattr(message.file, 'size',       0) or 0
                duration = getattr(message.file, 'duration',  None)
                desc     = (caption or name or mime)[:70]
                items.append({
                    'desc':  desc,
                    'size':  fmt_size(size),
                    'dur':   fmt_time(duration),
                    'mime':  mime,
                    'icon':  mime_icon(mime),
                    'mtype': mime_type_label(mime),
                    'name':  name,
                    'raw_size': size,
                    'raw_dur':  duration or 0,
                })
        media_cache[idx] = items
        return web.json_response({'ok': True, 'items': items, 'title': entity.title})
    except Exception as e:
        return web.json_response({'ok': False, 'error': str(e)}, status=500)

# ── Select media ──────────────────────────────────────────────
async def handle_select(request):
    global current_message, tg_client, media_cache
    data    = await request.json()
    ch_idx  = data.get('ch_idx', 0)
    med_idx = data.get('med_idx', 0)

    if ch_idx not in media_cache:
        return web.json_response({'ok': False, 'error': 'Channel not loaded'}, status=400)

    items = media_cache[ch_idx]
    if med_idx < 0 or med_idx >= len(items):
        return web.json_response({'ok': False}, status=400)

    ch     = CHANNELS[ch_idx]
    entity = await tg_client.get_entity(ch['id'])
    count  = 0
    async for message in tg_client.iter_messages(entity, limit=100):
        if message.media and message.file:
            if count == med_idx:
                current_message = message
                m   = items[med_idx]
                ext = get_ext(m['name'], m['mime'])
                key = media_key(ch_idx, med_idx)

                # Record history
                record_history(ch_idx, med_idx, m['desc'], m['mime'],
                               m['size'], m['dur'])

                # Get saved progress
                prog = app_data['progress'].get(key, {})

                return web.json_response({
                    'ok':       True,
                    'title':    m['desc'],
                    'size':     m['size'],
                    'dur':      m['dur'],
                    'mime':     m['mime'],
                    'mtype':    m['mtype'],
                    'url':      f'/stream{ext}?t={key}',
                    'key':      key,
                    'resume':   prog.get('position', 0),
                })
            count += 1

    return web.json_response({'ok': False}, status=404)

# ── Save progress ─────────────────────────────────────────────
async def handle_progress(request):
    data = await request.json()
    save_progress(data['key'], data['position'], data['duration'])
    return web.json_response({'ok': True})

# ── Search ────────────────────────────────────────────────────
async def handle_search(request):
    q = request.query.get('q', '').lower().strip()
    if not q:
        return web.json_response([])

    results = []
    for ch_idx, items in media_cache.items():
        ch = CHANNELS[ch_idx]
        for med_idx, m in enumerate(items):
            if q in m['desc'].lower() or q in m['mime'].lower():
                results.append({
                    'ch_idx':  ch_idx,
                    'med_idx': med_idx,
                    'ch_name': ch['name'],
                    'ch_icon': ch['icon'],
                    'desc':    m['desc'],
                    'size':    m['size'],
                    'dur':     m['dur'],
                    'mime':    m['mime'],
                    'icon':    m['icon'],
                    'mtype':   m['mtype'],
                })
    return web.json_response(results[:50])

# ── History ───────────────────────────────────────────────────
async def handle_history(request):
    history = []
    for h in app_data['history']:
        key  = h['key']
        prog = app_data['progress'].get(key, {})
        pos  = prog.get('position', 0)
        dur  = prog.get('duration', 0) or h.get('raw_dur', 0)
        pct  = round(pos / dur * 100) if dur > 0 else 0
        history.append({**h, 'progress_pct': pct, 'resume_pos': pos})
    return web.json_response(history)

async def handle_channels(request):
    return web.json_response([
        {'idx': i, 'name': c['name'], 'icon': c['icon']}
        for i, c in enumerate(CHANNELS)
    ])

# ── HTML ──────────────────────────────────────────────────────
async def handle_index(request):
    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>📡 Telegram Stream Player</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/plyr/3.7.8/plyr.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/plyr/3.7.8/plyr.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Inter:wght@300;400;600;700&display=swap');
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#0d0d18;--bg2:#13131f;--bg3:#1c1c2e;--bg4:#0a0a12;
  --border:#ffffff0d;--accent:#2563eb;--hl:#e94560;
  --cyan:#38bdf8;--green:#4ecca3;--yellow:#fbbf24;
  --purple:#a78bfa;--text:#f1f5f9;--dim:#475569;--dim2:#334155;
}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;overflow:hidden}

/* ═══ APP SHELL ═══════════════════════════════════════════ */
.app{display:flex;flex-direction:column;height:100vh}

/* ═══ TOP BAR ══════════════════════════════════════════════ */
.topbar{display:flex;align-items:center;height:48px;flex-shrink:0;background:var(--bg4);border-bottom:1px solid var(--border);z-index:30}
.topbar-brand{display:flex;align-items:center;gap:8px;padding:0 18px;height:100%;border-right:1px solid var(--border);flex-shrink:0}
.dot{width:8px;height:8px;border-radius:50%;background:var(--cyan);box-shadow:0 0 8px var(--cyan);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
.topbar-brand span{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;color:var(--cyan);letter-spacing:2px;text-transform:uppercase}
.topbar-title{flex:1;padding:0 20px;font-size:13px;font-weight:500;color:var(--text);opacity:.6;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.topbar-meta{display:flex;align-items:center;gap:6px;padding:0 18px;font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--dim);border-left:1px solid var(--border);height:100%;flex-shrink:0}
.meta-pill{background:var(--bg3);border:1px solid var(--border);border-radius:4px;padding:2px 8px;color:var(--yellow);font-weight:700}

/* ═══ BODY ══════════════════════════════════════════════════ */
.body{display:flex;flex:1;overflow:hidden}

/* ═══ LEFT PANEL ════════════════════════════════════════════ */
.left-panel{display:flex;flex-direction:column;width:300px;min-width:160px;max-width:55vw;background:var(--bg2);border-right:1px solid var(--border);flex-shrink:0;position:relative}

/* nav tabs */
.side-nav{display:flex;background:var(--bg4);border-bottom:1px solid var(--border);flex-shrink:0}
.side-tab{flex:1;padding:10px 6px;font-size:10px;font-weight:700;color:var(--dim);letter-spacing:1px;text-transform:uppercase;font-family:'JetBrains Mono',monospace;cursor:pointer;text-align:center;transition:all .15s;border-bottom:2px solid transparent}
.side-tab:hover{color:var(--text)}
.side-tab.active{color:var(--cyan);border-bottom-color:var(--cyan)}

/* panels */
.side-panel{display:none;flex-direction:column;flex:1;overflow:hidden}
.side-panel.active{display:flex}

/* search bar */
.search-wrap{padding:10px;border-bottom:1px solid var(--border);flex-shrink:0}
.search-input{width:100%;background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:7px 12px;color:var(--text);font-size:12px;font-family:'Inter',sans-serif;outline:none;transition:border .15s}
.search-input:focus{border-color:var(--accent)}
.search-input::placeholder{color:var(--dim)}

/* channel rail */
.channel-rail{flex-shrink:0;background:var(--bg4);border-bottom:1px solid var(--border);padding:8px 8px 6px}
.rail-label{font-family:'JetBrains Mono',monospace;font-size:9px;font-weight:700;color:var(--dim);letter-spacing:1.5px;text-transform:uppercase;padding:0 6px 6px}
.ch-tabs{display:flex;flex-direction:column;gap:1px}
.ch-tab{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:6px;cursor:pointer;transition:all .15s;border:1px solid transparent;position:relative}
.ch-tab:hover{background:var(--bg3)}
.ch-tab.active{background:linear-gradient(90deg,#2563eb18,#2563eb08);border-color:#2563eb33}
.ch-tab.active::before{content:'';position:absolute;left:0;top:20%;bottom:20%;width:3px;border-radius:0 2px 2px 0;background:var(--accent)}
.ch-tab .ch-icon{font-size:16px;flex-shrink:0}
.ch-tab .ch-name{flex:1;font-size:12px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ch-tab.active .ch-name{color:var(--cyan)}
.ch-tab .ch-count{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--dim);flex-shrink:0}
.ch-loading{width:6px;height:6px;border-radius:50%;border:1.5px solid var(--dim);border-top-color:var(--cyan);animation:spin .6s linear infinite;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}

/* media list */
.section-head{padding:9px 14px;font-size:10px;font-weight:700;color:var(--dim);letter-spacing:1.5px;text-transform:uppercase;font-family:'JetBrains Mono',monospace;border-bottom:1px solid var(--border);flex-shrink:0;display:flex;align-items:center;justify-content:space-between}
.count-badge{background:var(--accent);color:white;font-size:9px;padding:2px 7px;border-radius:10px;font-weight:700}
.scroll-list{flex:1;overflow-y:auto;padding:5px}
.scroll-list::-webkit-scrollbar{width:3px}
.scroll-list::-webkit-scrollbar-thumb{background:var(--dim2);border-radius:2px}

.media-item{display:flex;align-items:flex-start;gap:9px;padding:9px 10px;border-radius:6px;cursor:pointer;transition:background .12s;border:1px solid transparent;margin-bottom:1px;position:relative}
.media-item:hover{background:var(--bg3)}
.media-item.active{background:#2563eb18;border-color:#2563eb33}
.media-item .num{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--yellow);font-weight:700;min-width:20px;padding-top:1px}
.media-item .ico{font-size:14px;padding-top:1px}
.media-item .info{flex:1;overflow:hidden}
.media-item .ttl{font-size:12px;font-weight:600;color:var(--text);line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.media-item.active .ttl{color:var(--cyan)}
.media-item .meta{font-size:10px;color:var(--dim);margin-top:3px;font-family:'JetBrains Mono',monospace}
.media-item .ch-tag{font-size:9px;color:var(--purple);margin-top:2px;font-family:'JetBrains Mono',monospace}

/* progress bar on media item */
.item-progress{position:absolute;bottom:0;left:0;right:0;height:2px;background:var(--dim2);border-radius:0 0 6px 6px;overflow:hidden}
.item-progress-fill{height:100%;background:var(--cyan);border-radius:0 0 6px 6px;transition:width .3s}

/* history item */
.hist-item{display:flex;align-items:flex-start;gap:9px;padding:10px 10px;border-radius:6px;cursor:pointer;transition:background .12s;border:1px solid transparent;margin-bottom:2px;position:relative}
.hist-item:hover{background:var(--bg3)}
.hist-ts{font-family:'JetBrains Mono',monospace;font-size:9px;color:var(--dim);margin-top:3px}
.hist-ch{font-size:9px;color:var(--purple);margin-top:2px;font-family:'JetBrains Mono',monospace}

/* empty state */
.empty-state{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;padding:40px 20px;color:var(--dim);text-align:center}
.empty-state .e-icon{font-size:36px;opacity:.3}
.empty-state p{font-size:12px;opacity:.5}

/* resize handle */
.resize-handle{position:absolute;right:-4px;top:0;bottom:0;width:8px;cursor:col-resize;z-index:20;background:transparent;transition:background .2s}
.resize-handle:hover,.resize-handle.dragging{background:var(--accent);opacity:.4}

/* ═══ PLAYER AREA ══════════════════════════════════════════ */
.player-area{flex:1;display:flex;flex-direction:column;overflow:hidden;background:#000}
.video-wrap{flex:1;display:flex;align-items:center;justify-content:center;background:#000;overflow:hidden;position:relative}
.video-wrap video,.video-wrap audio{max-width:100%;max-height:100%}
.pdf-wrap{flex:1;width:100%;border:none;background:#fff}
.placeholder{display:flex;flex-direction:column;align-items:center;gap:16px;color:var(--dim);user-select:none}
.placeholder .big{font-size:72px;opacity:.15}
.placeholder p{font-size:14px;font-weight:600;opacity:.3}
.placeholder small{font-size:11px;opacity:.2;font-family:'JetBrains Mono',monospace}

/* ═══ PLYR ═════════════════════════════════════════════════ */
.plyr{width:100%;height:100%;--plyr-color-main:#2563eb;--plyr-range-thumb-background:#38bdf8;--plyr-video-background:#000;--plyr-font-family:'Inter',sans-serif}
.plyr--video{height:100%}
.plyr__video-wrapper{height:100%}

/* resume toast */
.toast{position:fixed;bottom:24px;right:24px;background:var(--bg3);border:1px solid var(--accent);border-radius:10px;padding:12px 18px;font-size:13px;color:var(--text);display:flex;align-items:center;gap:12px;z-index:999;box-shadow:0 8px 32px #00000088;animation:slideUp .3s ease;max-width:360px}
@keyframes slideUp{from{transform:translateY(20px);opacity:0}to{transform:translateY(0);opacity:1}}
.toast-btn{background:var(--accent);color:white;border:none;border-radius:6px;padding:5px 12px;font-size:12px;cursor:pointer;font-weight:600;white-space:nowrap}
.toast-close{background:none;border:none;color:var(--dim);cursor:pointer;font-size:16px;line-height:1;padding:0 2px}
</style>
</head>
<body>
<div class="app">

  <!-- Top bar -->
  <div class="topbar">
    <div class="topbar-brand"><div class="dot"></div><span>TG Stream</span></div>
    <div class="topbar-title" id="topTitle">Select a channel &amp; media to begin</div>
    <div class="topbar-meta">
      <span class="meta-pill" id="metaSize" style="display:none"></span>
      <span class="meta-pill" id="metaDur"  style="display:none"></span>
    </div>
  </div>

  <div class="body">

    <!-- Left panel -->
    <div class="left-panel" id="leftPanel">

      <!-- Side nav tabs -->
      <div class="side-nav">
        <div class="side-tab active" onclick="showSidePanel('channels')"  id="tab-channels">📺 Channels</div>
        <div class="side-tab"       onclick="showSidePanel('search')"    id="tab-search">🔍 Search</div>
        <div class="side-tab"       onclick="showSidePanel('history')"   id="tab-history">🕓 History</div>
      </div>

      <!-- ── CHANNELS PANEL ── -->
      <div class="side-panel active" id="panel-channels">
        <div class="channel-rail">
          <div class="rail-label">Channels</div>
          <div class="ch-tabs" id="chTabs"></div>
        </div>
        <div style="display:flex;flex-direction:column;flex:1;overflow:hidden">
          <div class="section-head">
            <span id="mediaSectionTitle">Media</span>
            <span class="count-badge" id="mediaCount" style="display:none"></span>
          </div>
          <div class="scroll-list" id="mediaList">
            <div class="empty-state"><div class="e-icon">📡</div><p>Select a channel above</p></div>
          </div>
        </div>
      </div>

      <!-- ── SEARCH PANEL ── -->
      <div class="side-panel" id="panel-search">
        <div class="search-wrap">
          <input class="search-input" id="searchInput" placeholder="Search across all loaded channels..." oninput="onSearch(this.value)">
        </div>
        <div class="section-head">
          <span id="searchTitle">Results</span>
          <span class="count-badge" id="searchCount" style="display:none"></span>
        </div>
        <div class="scroll-list" id="searchResults">
          <div class="empty-state"><div class="e-icon">🔍</div><p>Type to search media</p></div>
        </div>
      </div>

      <!-- ── HISTORY PANEL ── -->
      <div class="side-panel" id="panel-history">
        <div class="section-head">
          <span>Watch History</span>
          <span class="count-badge" id="histCount" style="display:none"></span>
        </div>
        <div class="scroll-list" id="historyList">
          <div class="empty-state"><div class="e-icon">🕓</div><p>Nothing watched yet</p></div>
        </div>
      </div>

      <div class="resize-handle" id="resizeHandle"></div>
    </div>

    <!-- Player area -->
    <div class="player-area" id="playerArea">
      <div class="video-wrap" id="videoWrap">
        <div class="placeholder" id="placeholder">
          <div class="big">📡</div>
          <p>Nothing playing</p>
          <small>Select media from the sidebar</small>
        </div>
      </div>
    </div>

  </div>
</div>

<script>
// ── State ──────────────────────────────────────────────────────
let player      = null;
let activeCh    = -1;
let activeMed   = -1;
let currentKey  = null;
let saveTimer   = null;
let loadingCh   = -1;
let searchTimer = null;

// ── Side panel switching ───────────────────────────────────────
function showSidePanel(name) {
  document.querySelectorAll('.side-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.side-tab').forEach(t => t.classList.remove('active'));
  document.getElementById(`panel-${name}`).classList.add('active');
  document.getElementById(`tab-${name}`).classList.add('active');
  if (name === 'history') loadHistory();
}

// ── Channel init ───────────────────────────────────────────────
async function initChannels() {
  const channels = await (await fetch('/channels')).json();
  const tabs = document.getElementById('chTabs');
  tabs.innerHTML = '';
  channels.forEach(ch => {
    const el = document.createElement('div');
    el.className = 'ch-tab'; el.id = `ch-${ch.idx}`;
    el.innerHTML = `<span class="ch-icon">${ch.icon}</span>
      <div class="ch-name">${ch.name}</div>
      <span class="ch-count" id="ch-count-${ch.idx}">—</span>`;
    el.onclick = () => loadChannel(ch.idx);
    tabs.appendChild(el);
  });
}

// ── Load channel ───────────────────────────────────────────────
async function loadChannel(idx) {
  if (loadingCh === idx) return;
  loadingCh = idx;
  document.querySelectorAll('.ch-tab').forEach(e => e.classList.remove('active'));
  document.getElementById(`ch-${idx}`)?.classList.add('active');
  const countEl = document.getElementById(`ch-count-${idx}`);
  if (countEl) countEl.outerHTML = `<div class="ch-loading" id="ch-count-${idx}"></div>`;

  document.getElementById('mediaSectionTitle').textContent = 'Loading...';
  document.getElementById('mediaCount').style.display = 'none';
  document.getElementById('mediaList').innerHTML = `<div class="empty-state"><div class="e-icon" style="display:inline-block;animation:spin 1s linear infinite">⏳</div><p>Fetching media...</p></div>`;
  activeCh = idx;

  const data = await (await fetch(`/channel/${idx}`)).json();
  loadingCh = -1;

  // Restore count element
  const old = document.getElementById(`ch-count-${idx}`);
  if (old) { old.outerHTML = `<span class="ch-count" id="ch-count-${idx}">${data.ok ? data.items.length : '!'}</span>`; }

  if (!data.ok) {
    document.getElementById('mediaList').innerHTML = `<div class="empty-state"><div class="e-icon">⚠️</div><p>${data.error||'Failed'}</p></div>`;
    return;
  }

  document.getElementById('mediaSectionTitle').textContent = data.title || 'Media';
  document.getElementById('mediaCount').textContent = data.items.length;
  document.getElementById('mediaCount').style.display = 'inline';

  renderMediaList(data.items, idx, document.getElementById('mediaList'), true);
}

// ── Render media list ──────────────────────────────────────────
function renderMediaList(items, chIdx, container, numbered) {
  container.innerHTML = '';
  if (!items.length) {
    container.innerHTML = `<div class="empty-state"><div class="e-icon">🈳</div><p>No media</p></div>`;
    return;
  }
  items.forEach((m, i) => {
    const key = `${chIdx}_${i}`;
    const prog = window._progCache?.[key] || 0;
    const el = document.createElement('div');
    el.className = 'media-item';
    el.id = `med-${chIdx}-${i}`;
    el.innerHTML = `
      ${numbered ? `<span class="num">${i+1}</span>` : ''}
      <span class="ico">${m.icon}</span>
      <div class="info">
        <div class="ttl">${m.desc}</div>
        <div class="meta">${m.size} · ${m.dur}</div>
        ${m.ch_tag ? `<div class="ch-tag">${m.ch_tag}</div>` : ''}
      </div>
      ${prog > 2 ? `<div class="item-progress"><div class="item-progress-fill" style="width:${prog}%"></div></div>` : ''}`;
    el.onclick = () => selectMedia(chIdx, i);
    container.appendChild(el);
  });
}

// ── Select & play ──────────────────────────────────────────────
async function selectMedia(chIdx, medIdx) {
  // Stop progress saving
  if (saveTimer) clearInterval(saveTimer);

  const res  = await fetch('/select', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ch_idx: chIdx, med_idx: medIdx}),
  });
  const data = await res.json();
  if (!data.ok) { alert(data.error || 'Failed to load media'); return; }

  // Highlight
  document.querySelectorAll('.media-item').forEach(e => e.classList.remove('active'));
  document.getElementById(`med-${chIdx}-${medIdx}`)?.classList.add('active');
  document.getElementById(`med-${chIdx}-${medIdx}`)?.scrollIntoView({block:'nearest'});
  activeCh = chIdx; activeMed = medIdx; currentKey = data.key;

  // Top bar
  document.getElementById('topTitle').textContent = data.title;
  const sEl = document.getElementById('metaSize'); sEl.textContent=data.size; sEl.style.display='inline';
  const dEl = document.getElementById('metaDur');  dEl.textContent=data.dur;  dEl.style.display='inline';

  document.getElementById('placeholder').style.display = 'none';

  // ── PDF ──────────────────────────────────────────────────────
  if (data.mtype === 'pdf') {
    if (player) { try{player.destroy();}catch(e){} player=null; }
    const wrap = document.getElementById('videoWrap');
    wrap.innerHTML = '';
    const iframe = document.createElement('iframe');
    iframe.className = 'pdf-wrap';
    iframe.src = `https://mozilla.github.io/pdf.js/web/viewer.html?file=${encodeURIComponent('http://127.0.0.1:8766'+data.url)}`;
    wrap.appendChild(iframe);
    return;
  }

  // ── Audio / Video ─────────────────────────────────────────────
  const wrap = document.getElementById('videoWrap');
  if (player) { try{player.destroy();}catch(e){} player=null; }
  const old = wrap.querySelector('video,audio,iframe');
  if (old) old.remove();

  const isAudio = data.mtype === 'audio';
  const mediaEl = document.createElement(isAudio ? 'audio' : 'video');
  mediaEl.src = data.url;
  mediaEl.crossOrigin = 'anonymous';
  wrap.appendChild(mediaEl);

  player = new Plyr(mediaEl, {
    controls: ['play-large','play','rewind','fast-forward','progress','current-time','duration','mute','volume','settings','fullscreen'],
    settings: ['speed'],
    speed: { selected:1, options:[0.25,0.5,0.75,1,1.25,1.5,1.75,2,2.5,3] },
    keyboard: {focused:true, global:true},
    tooltips: {controls:true, seek:true},
    invertTime: false,
  });

  // Resume position
  const resume = data.resume || 0;
  if (resume > 5) {
    player.once('ready', () => {
      showResumeToast(resume, () => { player.currentTime = resume; player.play(); });
      player.play();
    });
  } else {
    player.play();
  }

  // Save progress every 5s
  saveTimer = setInterval(() => {
    if (!player || !currentKey) return;
    const pos = player.currentTime || 0;
    const dur = player.duration  || 0;
    if (pos > 2 && dur > 0) {
      fetch('/progress', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({key: currentKey, position: pos, duration: dur}),
      });
      // Update progress bar in list
      const pct = Math.round(pos/dur*100);
      const itemEl = document.getElementById(`med-${activeCh}-${activeMed}`);
      if (itemEl) {
        let bar = itemEl.querySelector('.item-progress-fill');
        if (!bar) {
          const wrap2 = document.createElement('div');
          wrap2.className = 'item-progress';
          bar = document.createElement('div');
          bar.className = 'item-progress-fill';
          wrap2.appendChild(bar);
          itemEl.appendChild(wrap2);
        }
        bar.style.width = pct + '%';
      }
    }
  }, 5000);

  // Update history panel if open
  if (document.getElementById('panel-history').classList.contains('active')) {
    setTimeout(loadHistory, 500);
  }
}

// ── Resume toast ───────────────────────────────────────────────
function showResumeToast(pos, onResume) {
  const existing = document.querySelector('.toast');
  if (existing) existing.remove();
  const t = document.createElement('div');
  t.className = 'toast';
  t.innerHTML = `<span>▶ Resume from <b>${fmtTime(pos)}</b>?</span>
    <button class="toast-btn" id="resumeBtn">Resume</button>
    <button class="toast-close" id="toastClose">✕</button>`;
  document.body.appendChild(t);
  document.getElementById('resumeBtn').onclick = () => { onResume(); t.remove(); };
  document.getElementById('toastClose').onclick = () => t.remove();
  setTimeout(() => { if (t.parentNode) t.remove(); }, 100000000000);
}

// ── Search ─────────────────────────────────────────────────────
function onSearch(q) {
  clearTimeout(searchTimer);
  if (!q.trim()) {
    document.getElementById('searchResults').innerHTML = `<div class="empty-state"><div class="e-icon">🔍</div><p>Type to search media</p></div>`;
    document.getElementById('searchCount').style.display = 'none';
    return;
  }
  searchTimer = setTimeout(async () => {
    document.getElementById('searchResults').innerHTML = `<div class="empty-state"><div class="e-icon" style="animation:spin 1s linear infinite;display:inline-block">⏳</div><p>Searching...</p></div>`;
    const results = await (await fetch(`/search?q=${encodeURIComponent(q)}`)).json();
    document.getElementById('searchCount').textContent = results.length;
    document.getElementById('searchCount').style.display = results.length ? 'inline' : 'none';
    document.getElementById('searchTitle').textContent = results.length ? 'Results' : 'No results';
    const container = document.getElementById('searchResults');
    container.innerHTML = '';
    if (!results.length) {
      container.innerHTML = `<div class="empty-state"><div class="e-icon">😶</div><p>No results for "${q}"</p></div>`;
      return;
    }
    results.forEach(m => {
      const el = document.createElement('div');
      el.className = 'media-item';
      el.innerHTML = `
        <span class="ico">${m.icon}</span>
        <div class="info">
          <div class="ttl">${m.desc}</div>
          <div class="meta">${m.size} · ${m.dur}</div>
          <div class="ch-tag">${m.ch_icon} ${m.ch_name}</div>
        </div>`;
      el.onclick = () => {
        // Switch to channels panel and play
        showSidePanel('channels');
        selectMedia(m.ch_idx, m.med_idx);
      };
      container.appendChild(el);
    });
  }, 300);
}

// ── History ────────────────────────────────────────────────────
async function loadHistory() {
  const items = await (await fetch('/history')).json();
  const container = document.getElementById('historyList');
  document.getElementById('histCount').textContent = items.length;
  document.getElementById('histCount').style.display = items.length ? 'inline' : 'none';
  container.innerHTML = '';
  if (!items.length) {
    container.innerHTML = `<div class="empty-state"><div class="e-icon">🕓</div><p>Nothing watched yet</p></div>`;
    return;
  }
  items.forEach(h => {
    const el = document.createElement('div');
    el.className = 'hist-item';
    const ago = timeAgo(h.ts);
    el.innerHTML = `
      <span class="ico" style="font-size:16px;padding-top:2px">${h.ch_icon}</span>
      <div class="info" style="flex:1;overflow:hidden">
        <div class="ttl" style="font-size:12px;font-weight:600;color:var(--text);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${h.title}</div>
        <div class="hist-ch">${h.ch_name}</div>
        <div class="hist-ts">${ago}${h.progress_pct > 2 ? ` · <span style="color:var(--cyan)">${h.progress_pct}% watched</span>` : ''}</div>
        ${h.progress_pct > 2 && h.progress_pct < 98 ? `<div class="item-progress" style="position:relative;height:2px;margin-top:5px;background:var(--dim2);border-radius:2px"><div class="item-progress-fill" style="height:100%;background:var(--cyan);width:${h.progress_pct}%"></div></div>` : ''}
      </div>`;
    el.onclick = () => {
      showSidePanel('channels');
      selectMedia(h.ch_idx, h.med_idx);
    };
    container.appendChild(el);
  });
}

// ── Keyboard shortcuts ─────────────────────────────────────────
document.addEventListener('keydown', e => {
  if (!player || e.target.tagName === 'INPUT') return;
  const n = parseInt(e.key);
  if (n >= 1 && n <= 9) player.currentTime = player.duration * n / 10;
});

// ── Resize handle ──────────────────────────────────────────────
(function() {
  const handle = document.getElementById('resizeHandle');
  const panel  = document.getElementById('leftPanel');
  let drag=false, startX=0, startW=0;
  handle.addEventListener('mousedown', e => {
    drag=true; startX=e.clientX; startW=panel.offsetWidth;
    handle.classList.add('dragging');
    document.body.style.cursor='col-resize';
    document.body.style.userSelect='none';
    e.preventDefault();
  });
  document.addEventListener('mousemove', e => {
    if(!drag) return;
    panel.style.width = Math.max(160,Math.min(window.innerWidth*.55,startW+e.clientX-startX))+'px';
  });
  document.addEventListener('mouseup', () => {
    if(!drag) return; drag=false;
    handle.classList.remove('dragging');
    document.body.style.cursor=''; document.body.style.userSelect='';
  });
})();

// ── Helpers ────────────────────────────────────────────────────
function fmtTime(s) {
  s=Math.max(0,Math.floor(s||0));
  const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;
  return h?`${h}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`:`${m}:${String(sec).padStart(2,'0')}`;
}
function timeAgo(ts) {
  const d = Math.floor(Date.now()/1000 - ts);
  if (d < 60)   return 'just now';
  if (d < 3600) return `${Math.floor(d/60)}m ago`;
  if (d < 86400)return `${Math.floor(d/3600)}h ago`;
  return `${Math.floor(d/86400)}d ago`;
}

initChannels();
</script>
</body>
</html>"""
    return web.Response(text=html, content_type='text/html')

# ── Server ────────────────────────────────────────────────────
async def start_server():
    app = web.Application()
    app.router.add_get('/',              handle_index)
    app.router.add_get('/channels',      handle_channels)
    app.router.add_get('/channel/{idx}', handle_channel)
    app.router.add_post('/select',       handle_select)
    app.router.add_post('/progress',     handle_progress)
    app.router.add_get('/search',        handle_search)
    app.router.add_get('/history',       handle_history)
    for method in ('GET', 'HEAD'):
        app.router.add_route(method, '/stream',      handle_stream)
        app.router.add_route(method, '/stream{ext}', handle_stream)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, HTTP_HOST, HTTP_PORT).start()
    return runner

# ── Terminal ──────────────────────────────────────────────────
class C:
    RESET="\033[0m";BOLD="\033[1m";DIM="\033[2m"
    BRED="\033[91m";BGREEN="\033[92m";BYELLOW="\033[93m"
    BBLUE="\033[94m";BCYAN="\033[96m";BWHITE="\033[97m"

async def main():
    global tg_client
    print(f"""
{C.BBLUE}{C.BOLD}╔══════════════════════════════════════════════╗
║   📡  Telegram Stream Player  v10.0         ║
║   🔍  Search · 📄 PDF · 🕓 History          ║
╚══════════════════════════════════════════════╝{C.RESET}
""")
    print(f"  {C.BWHITE}Channels:{C.RESET}")
    for i, ch in enumerate(CHANNELS):
        print(f"  {C.BYELLOW}{i+1}.{C.RESET} {ch['icon']}  {C.BWHITE}{ch['name']}{C.RESET}")
    print(f"\n  {C.DIM}Data saved to: {DATA_FILE}{C.RESET}\n")

    async with TelegramClient('session', api_id, api_hash) as client:
        tg_client = client
        runner    = await start_server()
        url       = f"http://{HTTP_HOST}:{HTTP_PORT}/"
        print(f"  {C.BCYAN}🌐  {C.RESET}{C.BWHITE}{C.BOLD}{url}{C.RESET}")
        print(f"  {C.DIM}Ctrl+C to stop.{C.RESET}\n")
        webbrowser.open(url)
        try:
            while True: await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await runner.cleanup()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{C.DIM}Stopped.{C.RESET}\n")
