#!/usr/bin/env python3
"""Discord Rich Presence for the Foliate ebook reader.

How it works (no Foliate API exists, so three local sources are combined):
  * AT-SPI (accessibility bus): the title of Foliate's open windows -> which book is open
  * ~/.local/share/com.github.johnfactotum.Foliate/<book>.json: progress + EPUB CFI,
    rewritten by Foliate ~1s after every page turn
  * the .epub itself (path from library/uri-store.json): table of contents -> chapter name

Requires PyGObject + Atspi typelib (system packages, usually already installed with GTK).
Usage: ./foliate_rpc.py [--covers]
"""

import argparse
import json
import os
import posixpath
import re
import socket
import struct
import sys
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

DATA = Path.home() / ".local/share/com.github.johnfactotum.Foliate"
CACHE = Path.home() / ".cache/com.github.johnfactotum.Foliate"
APP_NAME = "com.github.johnfactotum.Foliate"
POLL = 2.0
DEFAULT_CLIENT_ID = "1556855470684119131"
FALLBACK_IMAGE = (
    "foliate"  # asset key uploaded under the app's Rich Presence > Art Assets
)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- Discord IPC


class Discord:
    def __init__(self, client_id):
        self.client_id = client_id
        self.sock = None

    def _send(self, op, payload):
        data = json.dumps(payload).encode()
        self.sock.sendall(struct.pack("<II", op, len(data)) + data)

    def _recv(self):
        op, n = struct.unpack("<II", self._read(8))
        return op, json.loads(self._read(n))

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Discord closed the connection")
            buf += chunk
        return buf

    def connect(self):
        runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        dirs = [
            runtime,
            f"{runtime}/app/com.discordapp.Discord",
            f"{runtime}/snap.discord",
        ]
        for d in dirs:
            for i in range(10):
                path = f"{d}/discord-ipc-{i}"
                if not os.path.exists(path):
                    continue
                s = socket.socket(socket.AF_UNIX)
                s.settimeout(5)
                try:
                    s.connect(path)
                except OSError:
                    s.close()
                    continue
                self.sock = s
                self._send(0, {"v": 1, "client_id": self.client_id})
                _, msg = self._recv()
                if msg.get("evt") != "READY":
                    self.close()
                    raise ConnectionError(f"Discord handshake rejected: {msg}")
                return
        raise ConnectionError("no Discord IPC socket found")

    def set_activity(self, activity):
        self._send(
            1,
            {
                "cmd": "SET_ACTIVITY",
                "nonce": str(uuid.uuid4()),
                "args": {"pid": os.getpid(), "activity": activity},
            },
        )
        _, msg = self._recv()
        if msg.get("evt") == "ERROR":
            raise RuntimeError(f"SET_ACTIVITY failed: {msg.get('data')}")

    def close(self):
        if self.sock:
            self.sock.close()
        self.sock = None


# ------------------------------------------------------------ Foliate state


def foliate_window_titles():
    """Titles of all Foliate windows as (title, is_active); None if Foliate isn't running."""
    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app and app.get_name() == APP_NAME:
            wins = []
            for j in range(app.get_child_count()):
                w = app.get_child_at_index(j)
                if w:
                    wins.append(
                        (
                            w.get_name(),
                            w.get_state_set().contains(Atspi.StateType.ACTIVE),
                        )
                    )
            return wins
    return None


def lang_str(v):
    """Metadata fields may be a string, a {lang: string} map, or a list/dict of contributors."""
    if isinstance(v, list):
        return ", ".join(filter(None, map(lang_str, v)))
    if isinstance(v, dict):
        v = v.get("name", next(iter(v.values()), ""))
        return lang_str(v) if not isinstance(v, str) else v
    return v or ""


_books = {}  # json path -> (mtime, data)


def load_books():
    """All books Foliate knows about: [(title, json_path, data)], freshly re-read on change."""
    out = []
    for p in DATA.glob("*.json"):
        try:
            mtime = p.stat().st_mtime
            if _books.get(p, (None,))[0] != mtime:
                _books[p] = (mtime, json.loads(p.read_text()))
            data = _books[p][1]
            if "metadata" in data:
                out.append((lang_str(data["metadata"].get("title")), p, data))
        except (OSError, ValueError):
            pass  # mid-write or unreadable; retry next poll
    return out


def find_open_book(books, windows):
    """Match window titles to books; prefer the focused window, then the most recently read."""
    by_title = {}
    for title, p, data in books:
        by_title.setdefault(title, []).append((p.stat().st_mtime, p, data))
    cands = []
    for name, active in windows:
        for mtime, p, data in by_title.get(name, []):
            cands.append((active, mtime, p, data))
    if not cands:
        return None
    _, _, p, data = max(cands, key=lambda c: (c[0], c[1]))
    return p, data


def book_path(identifier):
    try:
        uris = dict(json.loads((DATA / "library/uri-store.json").read_text())["uris"])
        return Path(uris[identifier]).expanduser()
    except (OSError, ValueError, KeyError):
        return None


# ---------------------------------------------------------- chapter lookup


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _resolve(base_dir, href):
    return posixpath.normpath(
        posixpath.join(base_dir, urllib.parse.unquote(href.split("#")[0]))
    )


def parse_epub_toc(path):
    """Return (spine_hrefs, [(title, spine_index)]) in TOC order, or None."""
    try:
        with zipfile.ZipFile(path) as z:
            opf_path = re.search(
                r'full-path="([^"]+)"', z.read("META-INF/container.xml").decode()
            ).group(1)
            opf_dir = posixpath.dirname(opf_path)
            opf = ET.fromstring(z.read(opf_path))
            manifest = {}
            for el in opf.iter():
                if _local(el.tag) == "item":
                    manifest[el.get("id")] = (
                        _resolve(opf_dir, el.get("href")),
                        el.get("properties", ""),
                    )
            spine, toc_id = [], None
            for el in opf.iter():
                if _local(el.tag) == "itemref" and el.get("idref") in manifest:
                    spine.append(manifest[el.get("idref")][0])
                elif _local(el.tag) == "spine":
                    toc_id = el.get("toc")
            index = {href: i for i, href in enumerate(spine)}

            entries = []  # (title, resolved href)
            nav = next(
                (h for h, props in manifest.values() if "nav" in props.split()), None
            )
            if nav:
                html = z.read(nav).decode("utf8", "replace")
                m = re.search(
                    r'<nav[^>]*epub:type="toc"[^>]*>(.*?)</nav>', html, re.DOTALL
                ) or re.search(r"<nav.*?>(.*?)</nav>", html, re.DOTALL)
                for href, label in re.findall(
                    r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
                    m.group(1) if m else "",
                    re.DOTALL,
                ):
                    entries.append(
                        (
                            re.sub(r"<[^>]+>|\s+", " ", label).strip(),
                            _resolve(posixpath.dirname(nav), href),
                        )
                    )
            elif toc_id in manifest:
                ncx_path = manifest[toc_id][0]
                for el in ET.fromstring(z.read(ncx_path)).iter():
                    if _local(el.tag) == "navPoint":
                        label = (
                            next(
                                (t.text for t in el.iter() if _local(t.tag) == "text"),
                                "",
                            )
                            or ""
                        )
                        src = next(
                            (c.get("src") for c in el if _local(c.tag) == "content"),
                            None,
                        )
                        if src:
                            entries.append(
                                (
                                    " ".join(label.split()),
                                    _resolve(posixpath.dirname(ncx_path), src),
                                )
                            )
            toc = [(t, index[h]) for t, h in entries if h in index and t]
            return toc or None
    except (OSError, KeyError, AttributeError, ET.ParseError, zipfile.BadZipFile):
        return None


_tocs = {}


def chapter_for(path, cfi):
    """Chapter title for a CFI like epubcfi(/6/116!/4...): spine step /6/N -> item N/2-1."""
    m = re.match(r"epubcfi\(/6/(\d+)", cfi or "")
    if not (path and m):
        return None
    if path not in _tocs:
        _tocs[path] = parse_epub_toc(path)
    toc = _tocs[path]
    if not toc:
        return None
    cur = int(m.group(1)) // 2 - 1
    best = None
    for title, idx in toc:  # last TOC entry at or before the current spine item
        if idx <= cur:
            best = title
    return best


# ------------------------------------------------------------------ covers

_cover_urls = {}


def upload_cover(identifier):
    """Upload the cover to litterbox.catbox.moe (temporary, 72h) so Discord can fetch it."""
    if identifier in _cover_urls:
        return _cover_urls[identifier]
    url = None
    png = CACHE / (urllib.parse.quote(identifier, safe="") + ".png")
    try:
        boundary = uuid.uuid4().hex
        body = b"".join(
            [
                f'--{boundary}\r\nContent-Disposition: form-data; name="reqtype"\r\n\r\nfileupload\r\n'.encode(),
                f'--{boundary}\r\nContent-Disposition: form-data; name="time"\r\n\r\n72h\r\n'.encode(),
                f'--{boundary}\r\nContent-Disposition: form-data; name="fileToUpload"; filename="cover.png"\r\n'
                f"Content-Type: image/png\r\n\r\n".encode(),
                png.read_bytes(),
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        req = urllib.request.Request(
            "https://litterbox.catbox.moe/resources/internals/api.php",
            body,
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        url = urllib.request.urlopen(req, timeout=20).read().decode().strip()
        if not url.startswith("https://"):
            log("cover upload failed:", url[:100])
            url = None
    except (
        OSError,
        ValueError,
    ) as e:  # cover is optional; never break presence over it
        log("cover upload failed:", e)
    _cover_urls[identifier] = url
    return url


# -------------------------------------------------------------------- main


def build_activity(data, path, started, covers):
    meta = data["metadata"]
    title, author = lang_str(meta.get("title")), lang_str(meta.get("author"))
    chapter = chapter_for(book_path(meta.get("identifier")), data.get("lastLocation"))
    cur, total = data.get("progress") or (0, 0)
    pct = f"{cur * 100 // total}%" if total else ""
    state = " · ".join(filter(None, [chapter, pct])) or (
        f"by {author}" if author else "Reading"
    )
    act = {
        "type": 0,
        "details": title[:128],
        "state": state[:128],
        "timestamps": {"start": started},
    }
    cover = covers and upload_cover(meta.get("identifier", ""))
    act["assets"] = {
        "large_image": cover or FALLBACK_IMAGE,
        "large_text": f"{title} — {author}"[:128] if author else title[:128],
    }
    return act


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--client-id",
        default=os.environ.get("FOLIATE_RPC_CLIENT_ID", DEFAULT_CLIENT_ID),
        help="Discord application ID (or env FOLIATE_RPC_CLIENT_ID)",
    )
    ap.add_argument(
        "--covers",
        action="store_true",
        help="upload book covers to a temporary public host (litterbox, 72h) to show them in Discord",
    )
    args = ap.parse_args()

    discord = Discord(args.client_id)
    current = None  # book json path being shown
    started = 0
    last = None  # last activity sent (None = nothing shown)
    log("watching Foliate...")
    while True:
        try:
            windows = foliate_window_titles()
            found = find_open_book(load_books(), windows) if windows else None
            activity = None
            if found:
                path, data = found
                if path != current:
                    current, started = path, int(time.time())
                activity = build_activity(data, path, started, args.covers)
            else:
                current = None

            if activity != last:
                if not discord.sock:
                    discord.connect()
                    log("connected to Discord")
                    last = None
                if activity:
                    discord.set_activity(activity)
                    log("presence:", activity["details"], "|", activity["state"])
                else:
                    discord.set_activity(None)
                    log("presence cleared")
                last = activity
        except (ConnectionError, OSError, RuntimeError) as e:
            log("discord:", e)
            discord.close()
            last = None
        except KeyboardInterrupt:
            break
        try:
            time.sleep(POLL)
        except KeyboardInterrupt:
            break
    try:
        if discord.sock:
            discord.set_activity(None)
    except (OSError, RuntimeError):
        pass  # Discord already gone; nothing left to clear
    discord.close()


if __name__ == "__main__":
    main()
