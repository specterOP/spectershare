<h1 align="center">spectershare</h1>

<p align="center">
  <b>A codeshare-style live text &amp; image pad for your local network.</b><br>
  Open a URL on any device on the same Wi-Fi, and whatever you type or paste
  shows up for everyone — instantly.
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/python-3.8%2B-3776ab?logo=python&logoColor=white">
  <img alt="Dependencies" src="https://img.shields.io/badge/dependencies-none-brightgreen">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-blue">
  <img alt="Platform" src="https://img.shields.io/badge/platform-win%20%7C%20mac%20%7C%20linux-lightgrey">
</p>

---

## Why

You've got two laptops, a phone, and a VM open, and you need to move a chunk of
text or a screenshot between them. Email is overkill, chat apps recompress your
images, and USB is from another era. Public pastebins send your data across the
internet just to hand it to the machine sitting next to you.

**spectershare** is one Python file that turns any machine on your LAN into a
live shared pad. No accounts, no cloud, no install. The text and images never
leave your network.

## Features

- **Live sync** — type on one device, watch it appear on the others as you go.
- **Paste or drop images** — screenshots and files show up for everyone in real
  time, full quality, no recompression.
- **Rooms** — every URL path is its own pad (`/r/main`, `/r/creds`, `/r/notes`).
  They spring into existence on first visit.
- **Zero dependencies** — pure Python standard library. If you have Python, you
  can run it.
- **Works everywhere** — the UI is a web page, so any phone, tablet, or laptop
  with a browser joins in. No app to install on the clients.
- **Optional persistence** — `--store` keeps your text across restarts.
- **Nice to look at** — a dark, monospace terminal-flavored UI with a live
  activity indicator so you can see the sync happening.

## Quick start

You need **Python 3.8 or newer**. Nothing else.

```bash
git clone https://github.com/SpecterOP/spectershare.git
cd spectershare
python spectershare.py --port 5555
```

The console prints two addresses:

```
  spectershare 1.0.0
  ----------------------------------------------
  on this machine   http://localhost:5555/r/main
  on your network   http://192.168.1.42:5555/r/main
  other rooms       add /r/<any-name> to the address
  images            paste, drop, or use Add image
  ----------------------------------------------
  Ctrl-C to stop
```

Open the **network** address on any other device on the same Wi-Fi, and start
typing. That's it.

> **Windows note:** if you get `PermissionError: [WinError 10013]` on startup,
> the port is inside a Windows-reserved range. Just pick another one:
> `python spectershare.py --port 5555`. Ports like 5555, 7000, or anything above
> 49152 are usually clear.

## Usage

| What you want | How |
|---|---|
| Share text | Just type in the pad. |
| Share an image | Paste (`Ctrl/Cmd+V`), drag a file onto the pad, or click **Add image**. |
| Switch rooms | Type a name in the `/r/` box in the header and press Enter. |
| Copy the room link | Click the address chip in the top-right. |
| Remove an image | Hover it and hit **✕** (removes it for everyone). |
| Save the text | **Download text** button, or run with `--store`. |

### Command-line options

```
python spectershare.py [options]

  --port PORT     port to listen on            (default 8080)
  --host HOST     bind address                 (default 0.0.0.0 = whole LAN)
  --store FILE    keep TEXT across restarts     (e.g. --store pads.json)
  --verbose       log every request
  --version       print version and exit
```

## How it works

Each browser holds one [Server-Sent Events](https://developer.mozilla.org/docs/Web/API/Server-sent_events)
connection to the server. Text edits POST back on a short debounce and fan out
to everyone else in the room; last write wins, which is the right trade for a
scratchpad.

Images take a different path on purpose: the raw bytes are uploaded once and
stored server-side, and only a tiny "image added" event rides the live stream.
Every client then pulls the actual image over a normal cached GET. That keeps
the sync stream lean, so a big screenshot never stalls someone's typing.

Everything runs in a single file on Python's built-in HTTP server — no
framework, no build step, no `pip install`.

## Security

**spectershare is built for trusted local networks.** By design it has **no
authentication** and binds to `0.0.0.0`, so anyone who can reach the port can
read and write any room they can guess. On your home Wi-Fi behind a router,
that's exactly what you want. On shared or public networks (a café, an office
LAN, a hotel), treat everything you put in it as visible to others on that
network.

Want it locked to just your machine? Bind to localhost and reach it over an SSH
tunnel instead of exposing it to the LAN:

```bash
python spectershare.py --host 127.0.0.1 --port 5555
```

Images are held **in memory only** — they are never written to `--store` and
they clear when the server restarts.

## Limits

- Max **25 MB** per image, **60 images** per room (oldest drops off). Both are
  constants at the top of the file (`MAX_IMAGE_BYTES`, `MAX_ROOM_IMAGES`).
- Text sync is last-write-wins, not a full collaborative merge — perfect for a
  shared pad, not meant to be Google Docs.

## Contributing

It's one file and it's meant to stay small and dependency-free. Issues and pull
requests are welcome — bug fixes, sensible options, and portability tweaks
especially. If you're proposing a big feature, open an issue first so we can
talk about whether it fits the "single file, zero deps" spirit.

## License

[MIT](LICENSE) © 2026 SpecterOP
