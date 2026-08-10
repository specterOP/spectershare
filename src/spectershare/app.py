#!/usr/bin/env python3
"""
spectershare - a codeshare-style live text + image pad for your local network.

Zero dependencies. Python 3.8+.

    python3 spectershare.py                 # serve on 0.0.0.0:8080
    python3 spectershare.py --port 5555     # pick a clear port (Windows: avoid reserved ranges)
    python3 spectershare.py --store pads.json   # keep TEXT across restarts

Open the printed LAN address on any device on the same network.
Everyone on the same room URL sees the same text live, and can paste or
drop images that appear for everyone in real time.

Images live in memory only (not written to --store); they clear on restart.
"""

__version__ = "1.0.0"

import argparse
import json
import os
import queue
import re
import socket
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

ROOM_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

MAX_IMAGE_BYTES = 25 * 1024 * 1024     # per image
MAX_ROOM_IMAGES = 60                    # oldest dropped past this
OK_IMAGE_MIME = ("image/png", "image/jpeg", "image/gif", "image/webp",
                 "image/bmp", "image/svg+xml", "image/avif")


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

class Hub:
    """Holds room text + images and fans updates out to every browser."""

    def __init__(self, store_path=None):
        self.lock = threading.Lock()
        self.rooms = {}          # room -> {"text": str, "rev": int, "at": float}
        self.images = {}         # room -> OrderedDict[id -> record]
        self.subs = {}           # room -> {client_id: Queue}
        self.store_path = store_path
        self._dirty = False
        if store_path and os.path.exists(store_path):
            try:
                with open(store_path, "r", encoding="utf-8") as fh:
                    self.rooms = json.load(fh)
            except Exception:
                self.rooms = {}

    # -- text --------------------------------------------------------------

    def _room(self, room):
        return self.rooms.setdefault(room, {"text": "", "rev": 0, "at": time.time()})

    def set_text(self, room, text, sender):
        with self.lock:
            r = self._room(room)
            r["text"] = text
            r["rev"] += 1
            r["at"] = time.time()
            self._dirty = True
            self._fanout(room, {"type": "text", "text": text, "rev": r["rev"], "from": sender})
            return r["rev"]

    # -- images ------------------------------------------------------------

    def _img_meta(self, rec):
        return {"id": rec["id"], "name": rec["name"], "mime": rec["mime"],
                "size": rec["size"], "at": rec["at"], "by": rec["by"]}

    def add_image(self, room, name, mime, data, sender):
        rid = os.urandom(9).hex()
        rec = {"id": rid, "name": name or "image", "mime": mime,
               "data": data, "size": len(data), "at": time.time(), "by": sender}
        with self.lock:
            bag = self.images.setdefault(room, OrderedDict())
            bag[rid] = rec
            while len(bag) > MAX_ROOM_IMAGES:
                old, orec = bag.popitem(last=False)
                self._fanout(room, {"type": "imgdel", "id": old})
            self._room(room)["at"] = time.time()
            meta = self._img_meta(rec)
            self._fanout(room, {"type": "image", **meta, "from": sender})
        return meta

    def get_image(self, room, rid):
        with self.lock:
            rec = self.images.get(room, {}).get(rid)
            if not rec:
                return None
            return rec["mime"], rec["data"]

    def remove_image(self, room, rid, sender):
        with self.lock:
            bag = self.images.get(room)
            if bag and rid in bag:
                del bag[rid]
                self._fanout(room, {"type": "imgdel", "id": rid, "from": sender})
                return True
        return False

    def image_metas(self, room):
        with self.lock:
            return [self._img_meta(r) for r in self.images.get(room, {}).values()]

    # -- rooms listing -----------------------------------------------------

    def room_list(self):
        with self.lock:
            out = []
            for name, r in self.rooms.items():
                out.append({
                    "room": name,
                    "chars": len(r["text"]),
                    "images": len(self.images.get(name, {})),
                    "peers": len(self.subs.get(name, {})),
                    "at": r["at"],
                })
            out.sort(key=lambda x: -x["at"])
            return out

    # -- subscribers -------------------------------------------------------

    def subscribe(self, room, client):
        q = queue.Queue(maxsize=512)
        with self.lock:
            self.subs.setdefault(room, {})[client] = q
            r = self._room(room)
            metas = [self._img_meta(x) for x in self.images.get(room, {}).values()]
        q.put({"type": "init", "text": r["text"], "rev": r["rev"], "room": room})
        q.put({"type": "imginit", "images": metas})
        self.broadcast_peers(room)
        return q

    def unsubscribe(self, room, client):
        with self.lock:
            peers = self.subs.get(room)
            if peers:
                peers.pop(client, None)
                if not peers:
                    self.subs.pop(room, None)
        self.broadcast_peers(room)

    def broadcast_peers(self, room):
        with self.lock:
            n = len(self.subs.get(room, {}))
            self._fanout(room, {"type": "peers", "count": n})

    def _fanout(self, room, payload):
        """Caller must hold the lock."""
        for q in list(self.subs.get(room, {}).values()):
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass

    # -- persistence (text only) ------------------------------------------

    def flush(self):
        if not self.store_path:
            return
        with self.lock:
            if not self._dirty:
                return
            data = json.dumps(self.rooms)
            self._dirty = False
        tmp = self.store_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(tmp, self.store_path)
        except Exception:
            pass


HUB = None  # set in main()


# --------------------------------------------------------------------------
# page
# --------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>__ROOM__ · spectershare</title>
<style>
  :root {
    --ink:      #0e1118;
    --panel:    #141924;
    --edge:     #222a3b;
    --fg:       #dfe4ef;
    --dim:      #6f7a95;
    --signal:   #f2a33c;
    --signal-2: #7fd4e8;
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
            "Liberation Mono", "Courier New", monospace;
  }

  * { box-sizing: border-box; }

  html, body {
    height: 100%;
    margin: 0;
    background: var(--ink);
    color: var(--fg);
    font-family: var(--mono);
    font-size: 14px;
    -webkit-text-size-adjust: 100%;
  }

  body { display: flex; flex-direction: column; overflow: hidden; }

  /* ---- signature: the link-activity strip ---------------------------- */

  #wire {
    position: relative;
    height: 3px;
    flex: 0 0 auto;
    background: var(--edge);
    overflow: hidden;
  }
  #wire::after {
    content: "";
    position: absolute;
    inset: 0;
    background: linear-gradient(90deg,
      transparent 0%, var(--signal) 45%, #fff 50%, var(--signal) 55%, transparent 100%);
    transform: translateX(-100%);
    opacity: 0;
  }
  #wire.out::after { animation: sweep-out .55s cubic-bezier(.3,0,.2,1); }
  #wire.in::after  { animation: sweep-in  .55s cubic-bezier(.3,0,.2,1); }
  #wire.in::after  { background: linear-gradient(90deg,
      transparent 0%, var(--signal-2) 45%, #fff 50%, var(--signal-2) 55%, transparent 100%); }

  @keyframes sweep-out {
    0%   { transform: translateX(-100%); opacity: 1; }
    100% { transform: translateX(100%);  opacity: 1; }
  }
  @keyframes sweep-in {
    0%   { transform: translateX(100%);  opacity: 1; }
    100% { transform: translateX(-100%); opacity: 1; }
  }

  /* ---- header --------------------------------------------------------- */

  header {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: 18px;
    padding: 12px 18px;
    background: var(--panel);
    border-bottom: 1px solid var(--edge);
    flex-wrap: wrap;
  }
  .mark { font-size: 11px; letter-spacing: .22em; text-transform: uppercase;
          color: var(--dim); white-space: nowrap; }
  .mark b { color: var(--fg); font-weight: 600; }

  .room { display: flex; align-items: center; gap: 6px; }
  .room label { color: var(--dim); }
  .room input {
    background: var(--ink); border: 1px solid var(--edge); border-radius: 3px;
    color: var(--signal); font: inherit; padding: 5px 9px; width: 150px;
  }
  .room input:focus { outline: 2px solid var(--signal); outline-offset: 1px; }

  .spacer { flex: 1 1 auto; }

  .stat { display: flex; align-items: center; gap: 7px; color: var(--dim); white-space: nowrap; }
  .dot {
    width: 7px; height: 7px; border-radius: 50%; background: var(--dim);
    box-shadow: 0 0 0 0 rgba(242,163,60,.5);
  }
  .dot.live { background: var(--signal); animation: breathe 2.4s ease-in-out infinite; }
  .dot.down { background: #d05b5b; }
  @keyframes breathe {
    0%,100% { box-shadow: 0 0 0 0 rgba(242,163,60,.45); }
    50%     { box-shadow: 0 0 0 5px rgba(242,163,60,0); }
  }
  .stat b { color: var(--fg); font-weight: 600; }

  .addr {
    color: var(--dim); border: 1px dashed var(--edge); border-radius: 3px;
    padding: 4px 9px; cursor: pointer; background: none; font: inherit;
  }
  .addr:hover { color: var(--signal); border-color: var(--signal); }

  /* ---- pad ------------------------------------------------------------ */

  main { flex: 1 1 auto; display: flex; min-height: 0; position: relative; }

  #pad {
    flex: 1 1 auto; width: 100%; resize: none; border: 0; outline: 0;
    background: var(--ink); color: var(--fg);
    font-family: var(--mono); font-size: 15px; line-height: 1.65;
    padding: 26px 22px; tab-size: 4; caret-color: var(--signal);
  }
  #pad::placeholder { color: #39425a; }
  #pad::selection { background: rgba(242,163,60,.28); }

  /* drop veil */
  #veil {
    position: absolute; inset: 0; display: none;
    align-items: center; justify-content: center;
    background: rgba(14,17,24,.82);
    border: 2px dashed var(--signal-2);
    color: var(--signal-2); font-size: 15px; letter-spacing: .06em;
    pointer-events: none; z-index: 5;
  }
  main.drag #veil { display: flex; }

  /* ---- image strip ---------------------------------------------------- */

  #strip {
    flex: 0 0 auto; display: none; gap: 10px;
    padding: 12px 16px; overflow-x: auto; overflow-y: hidden;
    background: var(--panel); border-top: 1px solid var(--edge);
    scrollbar-width: thin;
  }
  #strip.show { display: flex; }
  #strip::-webkit-scrollbar { height: 8px; }
  #strip::-webkit-scrollbar-thumb { background: var(--edge); border-radius: 4px; }

  figure.tile {
    position: relative; flex: 0 0 auto; margin: 0;
    width: 92px; height: 92px; border-radius: 5px; overflow: hidden;
    border: 1px solid var(--edge); background: #0a0d14;
    animation: pop .28s cubic-bezier(.2,.7,.3,1.3);
  }
  @keyframes pop { from { transform: scale(.8); opacity: 0; } to { transform: none; opacity: 1; } }
  figure.tile a { display: block; width: 100%; height: 100%; }
  figure.tile img { width: 100%; height: 100%; object-fit: cover; display: block; }

  figure.tile .bar {
    position: absolute; left: 0; right: 0; bottom: 0;
    display: flex; gap: 2px; padding: 3px;
    background: linear-gradient(transparent, rgba(0,0,0,.72));
    opacity: 0; transition: opacity .12s;
  }
  figure.tile:hover .bar, figure.tile:focus-within .bar { opacity: 1; }
  figure.tile .bar button, figure.tile .bar a.dl {
    flex: 1 1 auto; border: 0; border-radius: 3px; cursor: pointer;
    background: rgba(20,25,36,.9); color: var(--fg);
    font: 600 12px var(--mono); line-height: 20px; height: 20px;
    text-align: center; text-decoration: none;
  }
  figure.tile .bar button:hover { background: #d05b5b; color: #fff; }
  figure.tile .bar a.dl:hover { background: var(--signal); color: #1a1205; }

  figure.tile .tag {
    position: absolute; top: 0; left: 0; right: 0;
    font-size: 10px; color: var(--dim); padding: 3px 5px;
    background: linear-gradient(rgba(0,0,0,.6), transparent);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    pointer-events: none;
  }

  /* ---- footer --------------------------------------------------------- */

  footer {
    flex: 0 0 auto; display: flex; align-items: center; gap: 14px;
    padding: 9px 18px; background: var(--panel); border-top: 1px solid var(--edge);
    color: var(--dim); font-size: 12px; flex-wrap: wrap;
  }
  button.act {
    background: none; border: 1px solid var(--edge); border-radius: 3px;
    color: var(--fg); font: inherit; padding: 5px 12px; cursor: pointer;
  }
  button.act:hover  { border-color: var(--signal); color: var(--signal); }
  button.act:active { background: rgba(242,163,60,.12); }
  button.act:focus-visible { outline: 2px solid var(--signal); outline-offset: 1px; }

  /* ---- toast ---------------------------------------------------------- */

  #toast {
    position: fixed; left: 50%; bottom: 64px; transform: translateX(-50%) translateY(20px);
    background: var(--panel); border: 1px solid var(--edge); border-left: 3px solid var(--signal);
    color: var(--fg); padding: 10px 16px; border-radius: 4px; font-size: 13px;
    opacity: 0; pointer-events: none; transition: opacity .2s, transform .2s; z-index: 20;
    max-width: 80vw;
  }
  #toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }

  @media (max-width: 620px) {
    .mark, .addr { display: none; }
    header { gap: 12px; padding: 10px 12px; }
    #pad { padding: 16px 14px; font-size: 16px; }
    figure.tile { width: 76px; height: 76px; }
  }

  @media (prefers-reduced-motion: reduce) {
    #wire.out::after, #wire.in::after { animation-duration: .01ms; }
    .dot.live { animation: none; }
    figure.tile { animation: none; }
  }
</style>
</head>
<body>

<div id="wire"></div>

<header>
  <span class="mark"><b>spectershare</b> &nbsp;lan pad</span>

  <div class="room">
    <label for="roomin">/r/</label>
    <input id="roomin" value="__ROOM__" spellcheck="false" autocomplete="off"
           aria-label="Room name — press Enter to switch">
  </div>

  <span class="spacer"></span>

  <span class="stat"><i class="dot" id="dot"></i><span id="link">connecting</span></span>
  <span class="stat"><b id="peers">1</b>&nbsp;here</span>
  <button class="addr" id="addr" title="Copy this address">…</button>
</header>

<main id="main">
  <textarea id="pad" spellcheck="false" autocomplete="off" autocapitalize="off"
            placeholder="Type here. Paste or drop an image to share it — everyone on this room URL sees it live."></textarea>
  <div id="veil">drop image to share</div>
</main>

<div id="strip" aria-label="Shared images"></div>

<footer>
  <span><b id="chars">0</b> chars · <span id="lines">1</span> lines · <b id="imgn">0</b> images</span>
  <span id="sync">waiting for first sync</span>
  <span class="spacer"></span>
  <button class="act" id="addimg">Add image</button>
  <button class="act" id="copy">Copy text</button>
  <button class="act" id="save">Download text</button>
  <button class="act" id="clear">Clear text</button>
  <input id="file" type="file" accept="image/*" multiple hidden>
</footer>

<div id="toast" role="status" aria-live="polite"></div>

<script>
const ROOM = __ROOM_JSON__;
const CID  = Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);
const MAX  = __MAX_IMAGE_BYTES__;

const pad   = document.getElementById('pad');
const main  = document.getElementById('main');
const wire  = document.getElementById('wire');
const dot   = document.getElementById('dot');
const link  = document.getElementById('link');
const peers = document.getElementById('peers');
const chars = document.getElementById('chars');
const lines = document.getElementById('lines');
const imgn  = document.getElementById('imgn');
const sync  = document.getElementById('sync');
const addr  = document.getElementById('addr');
const roomin= document.getElementById('roomin');
const strip = document.getElementById('strip');
const fileIn= document.getElementById('file');

let rev = 0, pushing = false, pending = false, timer = null, lastSync = 0;
const seen = new Set();   // image ids currently shown

addr.textContent = location.origin + '/r/' + ROOM;

/* the wire ------------------------------------------------------------- */
function pulse(dir) {
  wire.classList.remove('in', 'out');
  void wire.offsetWidth;
  wire.classList.add(dir);
}

/* toast ---------------------------------------------------------------- */
let toastT;
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove('show'), 2600);
}

/* counters ------------------------------------------------------------- */
function counts() {
  chars.textContent = pad.value.length.toLocaleString();
  lines.textContent = (pad.value.match(/\n/g) || []).length + 1;
  imgn.textContent = seen.size;
}

setInterval(() => {
  if (!lastSync) return;
  const s = Math.round((Date.now() - lastSync) / 1000);
  sync.textContent = s < 2 ? 'synced just now'
        : s < 60 ? 'synced ' + s + 's ago'
        : 'synced ' + Math.round(s / 60) + 'm ago';
}, 1000);

/* images: rendering ---------------------------------------------------- */
function imgURL(id) {
  return '/api/img?room=' + encodeURIComponent(ROOM) + '&id=' + encodeURIComponent(id);
}

function addTile(m) {
  if (seen.has(m.id)) return;
  seen.add(m.id);

  const url = imgURL(m.id);
  const fig = document.createElement('figure');
  fig.className = 'tile';
  fig.dataset.id = m.id;

  const a = document.createElement('a');
  a.href = url; a.target = '_blank'; a.rel = 'noopener';
  const im = document.createElement('img');
  im.src = url; im.alt = m.name || 'shared image'; im.loading = 'lazy';
  a.appendChild(im);

  const tag = document.createElement('span');
  tag.className = 'tag';
  tag.textContent = fmtSize(m.size);

  const bar = document.createElement('div');
  bar.className = 'bar';
  const dl = document.createElement('a');
  dl.className = 'dl'; dl.href = url; dl.download = m.name || 'image';
  dl.textContent = '↓'; dl.title = 'Download';
  const rm = document.createElement('button');
  rm.textContent = '✕'; rm.title = 'Remove for everyone';
  rm.onclick = () => removeImage(m.id);
  bar.appendChild(dl); bar.appendChild(rm);

  fig.appendChild(a); fig.appendChild(tag); fig.appendChild(bar);
  strip.appendChild(fig);
  strip.classList.add('show');
  counts();
}

function dropTile(id) {
  const el = strip.querySelector('figure[data-id="' + CSS.escape(id) + '"]');
  if (el) el.remove();
  seen.delete(id);
  if (!seen.size) strip.classList.remove('show');
  counts();
}

function fmtSize(n) {
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(0) + ' KB';
  return (n / 1048576).toFixed(1) + ' MB';
}

/* images: sending ------------------------------------------------------ */
async function uploadImage(blob, name) {
  if (!blob) return;
  if (!blob.type.startsWith('image/')) { toast('That is not an image'); return; }
  if (blob.size > MAX) { toast('Image too large (max ' + fmtSize(MAX) + ')'); return; }
  try {
    const r = await fetch('/api/image?room=' + encodeURIComponent(ROOM) + '&client=' + CID, {
      method: 'POST',
      headers: { 'Content-Type': blob.type, 'X-Img-Name': encodeURIComponent(name || 'pasted.png') },
      body: blob
    });
    if (!r.ok) { toast('Upload failed (' + r.status + ')'); return; }
    lastSync = Date.now();
    pulse('out');
  } catch (_) { toast('Upload failed'); }
}

async function removeImage(id) {
  try {
    await fetch('/api/imgdel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ room: ROOM, id, client: CID })
    });
  } catch (_) {}
}

/* receive -------------------------------------------------------------- */
let es;
function connect() {
  es = new EventSource('/api/stream?room=' + encodeURIComponent(ROOM) + '&client=' + CID);

  es.onopen  = () => { dot.className = 'dot live'; link.textContent = 'live'; };
  es.onerror = () => { dot.className = 'dot down'; link.textContent = 'reconnecting'; };

  es.onmessage = (ev) => {
    const m = JSON.parse(ev.data);

    if (m.type === 'peers') { peers.textContent = m.count; return; }

    if (m.type === 'init') {
      rev = m.rev;
      if (m.text !== pad.value) applyRemote(m.text);
      lastSync = Date.now(); counts(); return;
    }

    if (m.type === 'text') {
      rev = m.rev; lastSync = Date.now();
      if (m.from === CID) return;
      applyRemote(m.text); counts(); pulse('in'); return;
    }

    if (m.type === 'imginit') {
      strip.innerHTML = ''; seen.clear(); strip.classList.remove('show');
      (m.images || []).forEach(addTile);
      counts(); return;
    }

    if (m.type === 'image') {
      const fresh = !seen.has(m.id);
      addTile(m);
      if (fresh && m.from !== CID) pulse('in');
      return;
    }

    if (m.type === 'imgdel') { dropTile(m.id); return; }
  };
}

function applyRemote(text) {
  const start = pad.selectionStart, end = pad.selectionEnd;
  const delta = text.length - pad.value.length;
  const atEnd = start >= pad.value.length;
  pad.value = text;
  if (document.activeElement === pad) {
    const s = atEnd ? text.length : Math.max(0, Math.min(text.length, start + (start > 0 ? delta : 0)));
    const e = atEnd ? text.length : Math.max(0, Math.min(text.length, end   + (end   > 0 ? delta : 0)));
    try { pad.setSelectionRange(s, e); } catch (_) {}
  }
}

/* send text ------------------------------------------------------------ */
async function push() {
  if (pushing) { pending = true; return; }
  pushing = true;
  try {
    const r = await fetch('/api/push', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ room: ROOM, client: CID, text: pad.value, rev })
    });
    const j = await r.json();
    rev = j.rev; lastSync = Date.now(); pulse('out');
  } catch (_) { link.textContent = 'send failed'; }
  pushing = false;
  if (pending) { pending = false; push(); }
}

pad.addEventListener('input', () => {
  counts(); clearTimeout(timer); timer = setTimeout(push, 140);
});

/* paste + drop --------------------------------------------------------- */
document.addEventListener('paste', (e) => {
  const items = (e.clipboardData && e.clipboardData.items) || [];
  let handled = false;
  for (const it of items) {
    if (it.kind === 'file' && it.type.startsWith('image/')) {
      const f = it.getAsFile();
      if (f) { uploadImage(f, f.name || 'pasted.png'); handled = true; }
    }
  }
  if (handled) e.preventDefault();
});

let dragDepth = 0;
['dragenter', 'dragover'].forEach(ev => main.addEventListener(ev, (e) => {
  if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files')) {
    e.preventDefault();
    if (ev === 'dragenter') dragDepth++;
    main.classList.add('drag');
  }
}));
main.addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; main.classList.remove('drag'); } });
main.addEventListener('drop', (e) => {
  e.preventDefault(); dragDepth = 0; main.classList.remove('drag');
  const files = (e.dataTransfer && e.dataTransfer.files) || [];
  for (const f of files) if (f.type.startsWith('image/')) uploadImage(f, f.name);
});

/* file picker ---------------------------------------------------------- */
document.getElementById('addimg').onclick = () => fileIn.click();
fileIn.onchange = () => {
  for (const f of fileIn.files) if (f.type.startsWith('image/')) uploadImage(f, f.name);
  fileIn.value = '';
};

/* actions -------------------------------------------------------------- */
function flash(btn, word) {
  const old = btn.textContent; btn.textContent = word;
  setTimeout(() => { btn.textContent = old; }, 1200);
}

document.getElementById('copy').onclick = async (e) => {
  try { await navigator.clipboard.writeText(pad.value); }
  catch (_) { pad.select(); document.execCommand('copy'); }
  flash(e.target, 'Copied');
};

document.getElementById('save').onclick = () => {
  const blob = new Blob([pad.value], { type: 'text/plain' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = ROOM + '.txt'; a.click();
  URL.revokeObjectURL(a.href);
};

document.getElementById('clear').onclick = () => {
  if (pad.value && !confirm('Clear the text for everyone in the room?')) return;
  pad.value = ''; counts(); push();
};

addr.onclick = async (e) => {
  try { await navigator.clipboard.writeText(addr.textContent); flash(e.target, 'Address copied'); }
  catch (_) {}
};

roomin.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return;
  const name = roomin.value.trim().replace(/[^A-Za-z0-9._-]/g, '-');
  if (name) location.href = '/r/' + encodeURIComponent(name);
});

window.addEventListener('beforeunload', () => { if (es) es.close(); });

counts();
connect();
pad.focus();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "spectershare"
    quiet = True

    def log_message(self, fmt, *args):
        if not Handler.quiet:
            BaseHTTPRequestHandler.log_message(self, fmt, *args)

    # -- helpers -----------------------------------------------------------

    def _send(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if not (extra and "Cache-Control" in extra):
            self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)

        if path == "/":
            self._send(302, b"", extra={"Location": "/r/main"})
            return

        if path == "/favicon.ico":
            self._send(204, b"")
            return

        if path.startswith("/r/"):
            room = unquote(path[3:]).strip("/")
            if not ROOM_RE.match(room):
                self._send(400, "Room names may use letters, numbers, dot, dash, underscore.")
                return
            page = (PAGE
                    .replace("__ROOM_JSON__", json.dumps(room))
                    .replace("__MAX_IMAGE_BYTES__", str(MAX_IMAGE_BYTES))
                    .replace("__ROOM__", room))
            self._send(200, page, "text/html; charset=utf-8")
            return

        if path == "/api/rooms":
            self._json(200, HUB.room_list())
            return

        if path == "/api/img":
            self._serve_image(qs)
            return

        if path == "/api/stream":
            self._stream(qs)
            return

        self._send(404, "not found")

    def do_POST(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)

        if path == "/api/push":
            self._push()
            return
        if path == "/api/image":
            self._upload_image(qs)
            return
        if path == "/api/imgdel":
            self._delete_image()
            return

        self._send(404, "not found")

    # -- text push ---------------------------------------------------------

    def _push(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            room = str(data["room"])
            if not ROOM_RE.match(room):
                raise ValueError
            text = str(data.get("text", ""))
            client = str(data.get("client", ""))
        except Exception:
            self._json(400, {"error": "bad request"})
            return
        rev = HUB.set_text(room, text, client)
        self._json(200, {"rev": rev})

    # -- image upload ------------------------------------------------------

    def _upload_image(self, qs):
        room = (qs.get("room") or [""])[0]
        client = (qs.get("client") or [""])[0]
        mime = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        name = unquote(self.headers.get("X-Img-Name", "image"))

        if not ROOM_RE.match(room):
            self._json(400, {"error": "bad room"})
            return
        if mime not in OK_IMAGE_MIME:
            self._json(415, {"error": "unsupported image type"})
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_IMAGE_BYTES:
            self._json(413, {"error": "image too large or empty"})
            return

        data = self._read_exact(n)
        if data is None:
            self._json(400, {"error": "short read"})
            return

        meta = HUB.add_image(room, name, mime, data, client)
        self._json(200, meta)

    def _read_exact(self, n):
        buf = bytearray()
        remaining = n
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                return None
            buf.extend(chunk)
            remaining -= len(chunk)
        return bytes(buf)

    def _delete_image(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            room = str(data["room"])
            rid = str(data["id"])
            client = str(data.get("client", ""))
            if not ROOM_RE.match(room):
                raise ValueError
        except Exception:
            self._json(400, {"error": "bad request"})
            return
        HUB.remove_image(room, rid, client)
        self._json(200, {"ok": True})

    # -- image fetch -------------------------------------------------------

    def _serve_image(self, qs):
        room = (qs.get("room") or [""])[0]
        rid = (qs.get("id") or [""])[0]
        if not ROOM_RE.match(room):
            self._send(400, "bad room")
            return
        got = HUB.get_image(room, rid)
        if not got:
            self._send(404, "no such image")
            return
        mime, data = got
        # bytes are immutable per id, so let the browser cache them
        self._send(200, data, mime, extra={"Cache-Control": "public, max-age=31536000, immutable"})

    # -- server-sent events -------------------------------------------------

    def _stream(self, qs):
        room = (qs.get("room") or ["main"])[0]
        client = (qs.get("client") or [""])[0] or os.urandom(4).hex()
        if not ROOM_RE.match(room):
            self._send(400, "bad room")
            return

        q = HUB.subscribe(room, client)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()

            self.wfile.write(b"retry: 1500\n\n")
            self.wfile.flush()

            while True:
                try:
                    msg = q.get(timeout=20)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                chunk = "data: " + json.dumps(msg) + "\n\n"
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            HUB.unsubscribe(room, client)
            self.close_connection = True


# --------------------------------------------------------------------------
# boot
# --------------------------------------------------------------------------

def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        s.close()


def autosave(hub, every=3.0):
    while True:
        time.sleep(every)
        hub.flush()


def main():
    global HUB
    ap = argparse.ArgumentParser(description="Live text + image pad for your local network.")
    ap.add_argument("--port", type=int, default=8080, help="port to listen on (default 8080)")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    ap.add_argument("--store", metavar="FILE", help="keep TEXT in FILE so it survives a restart")
    ap.add_argument("--verbose", action="store_true", help="log every request")
    ap.add_argument("--version", action="version", version="spectershare " + __version__)
    args = ap.parse_args()

    HUB = Hub(args.store)
    Handler.quiet = not args.verbose

    if args.store:
        threading.Thread(target=autosave, args=(HUB,), daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True

    ip = lan_ip()
    bar = "-" * 46
    print("\n  spectershare " + __version__)
    print("  " + bar)
    print("  on this machine   http://localhost:%d/r/main" % args.port)
    print("  on your network   http://%s:%d/r/main" % (ip, args.port))
    print("  other rooms       add /r/<any-name> to the address")
    print("  images            paste, drop, or use Add image")
    if args.store:
        print("  saving text to    %s" % args.store)
    print("  " + bar)
    print("  Ctrl-C to stop\n")

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopping…")
    finally:
        HUB.flush()
        srv.server_close()


if __name__ == "__main__":
    main()
