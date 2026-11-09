"""LogDeck-Helfer: strukturierte Ausgabe für die Mac-App LogDeck – Protokoll 1.

Kopie aus hamradio/logdeck (helper/logdeck.py). Einfach neben das Script legen und

    import logdeck as ld

Läuft das Script in LogDeck, gehen die Aufrufe als JSON-Zeilen an die App (Fortschritt, Tabellen,
Bilder, Fragen als Dialoge). Im Terminal tun sie nichts – außer den Fragen, die dann wie gewohnt mit
input() gestellt werden. Die normalen print()-Ausgaben des Scripts bleiben in beiden Fällen erhalten.

    ld.progress(3, 12, "Lese IMG_0412.heic")
    ld.step("Wavelog-Stationen abrufen", "ok", id="stations", detail="9 Stationen")
    ld.stat("Rufzeichen", 4)
    ld.table(["Rufzeichen", "Wavelog", "QRZ.com"], rows)      # rows: Listen oder dicts, "_state" je Zeile
    ld.image("/pfad/DL1ABC.jpg", caption="DL1ABC")
    ld.file("/pfad/ergebnis.csv", label="Seriendruck-CSV")
    ld.note("Kein Key für LA/DM6AX", level="warn")
    ld.done("Alles stimmt überein", state="ok")
    if ld.confirm("Station anlegen?", default=True): ...
    wahl = ld.choose("Welche Station?", ["DE-0034", "Neu"], default="Neu")
    name = ld.ask("Name der Station", default="POTA DE-0034")

Zustände (state): ok, warn, err, run, skip. Keine Abhängigkeiten außer der Standardbibliothek.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any, Iterable, Sequence

__all__ = ["active", "progress", "step", "note", "stat", "table", "image", "file", "done", "ask", "confirm", "choose"]

PROTOCOL = 1

_lock = threading.Lock()
_out = None
_in = None
_asked = 0


def _open() -> None:
    global _out, _in
    fd = os.environ.get("LOGDECK_EVENTS_FD")
    if not fd:
        return
    try:
        _out = os.fdopen(int(fd), "w", buffering=1, encoding="utf-8", closefd=False)
    except (OSError, ValueError):
        _out = None
        return
    rfd = os.environ.get("LOGDECK_REPLY_FD")
    if rfd:
        try:
            _in = os.fdopen(int(rfd), "r", encoding="utf-8", closefd=False)
        except (OSError, ValueError):
            _in = None
    _send({"t": "hello", "proto": PROTOCOL, "script": os.path.basename(sys.argv[0] or "")})


def _send(ev: dict) -> bool:
    global _out
    if _out is None:
        return False
    try:
        line = json.dumps(ev, ensure_ascii=False, default=str)
        with _lock:
            _out.write(line + "\n")
            _out.flush()
        return True
    except (OSError, ValueError):
        _out = None  # LogDeck ist weg: ab jetzt wie im Terminal
        return False


def active() -> bool:
    """True, wenn das Script in LogDeck läuft."""
    return _out is not None


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


# --- Anzeige (im Terminal ohne Wirkung) -----------------------------------------------------------

def progress(done: float, total: float | None = None, text: str = "") -> None:
    """Fortschritt; total=None für „unbestimmt“. Ersetzt den vorigen Fortschritt."""
    _send(_clean({"t": "progress", "done": done, "total": total, "text": text or None}))


def step(text: str, state: str = "run", id: str | None = None, detail: str | None = None) -> None:
    """Ein Arbeitsschritt. Gleiche id ersetzt den Schritt (z. B. erst „run“, dann „ok“)."""
    _send(_clean({"t": "step", "text": text, "state": state, "id": id, "detail": detail}))


def note(text: str, level: str = "info") -> None:
    """Hinweis: level info, warn oder err."""
    _send({"t": "note", "text": text, "level": level})


def stat(label: str, value: Any, state: str | None = None, unit: str | None = None) -> None:
    """Kennzahl (große Ziffern oben in der Ergebnis-Ansicht). Gleicher label ersetzt den Wert."""
    _send(_clean({"t": "stat", "label": label, "value": value, "state": state, "unit": unit}))


def table(columns: Sequence[Any], rows: Iterable[Any], id: str = "main", title: str | None = None) -> None:
    """Tabelle. columns: Namen oder dicts {key, label, align: left|right, mono: bool}.
    rows: Listen (in Spaltenreihenfolge) oder dicts; ein dict darf "_state" haben (ok, warn, err)."""
    cols = []
    for i, c in enumerate(columns):
        if isinstance(c, dict):
            cols.append({"key": str(c.get("key", i)), **{k: v for k, v in c.items() if k != "key"}})
        else:
            cols.append({"key": str(i), "label": str(c)})
    out_rows = []
    for r in rows:
        if isinstance(r, dict):
            out_rows.append(r)
        else:
            out_rows.append({cols[i]["key"]: v for i, v in enumerate(r) if i < len(cols)})
    _send(_clean({"t": "table", "id": id, "title": title, "columns": cols, "rows": out_rows}))


def image(path: str, caption: str = "", sub: str | None = None, id: str | None = None) -> None:
    """Bild für die Galerie (absoluter Pfad wird gebildet)."""
    _send(_clean({"t": "image", "path": os.path.abspath(path), "caption": caption, "sub": sub, "id": id}))


def file(path: str, label: str = "") -> None:
    """Ergebnisdatei oder -ordner zum Öffnen."""
    _send({"t": "file", "path": os.path.abspath(path), "label": label or os.path.basename(path)})


def done(text: str = "", state: str = "ok") -> None:
    """Abschluss mit Zusammenfassung."""
    _send({"t": "done", "text": text, "state": state})


# --- Fragen (im Terminal mit input()) -------------------------------------------------------------

def _ask(kind: str, text: str, default: Any = None, options: list | None = None) -> Any:
    global _asked
    if _out is None or _in is None:
        raise LookupError
    _asked += 1
    qid = f"q{_asked}"
    if not _send(_clean({"t": "ask", "id": qid, "kind": kind, "text": text, "default": default, "options": options})):
        raise LookupError
    while True:
        line = _in.readline()
        if not line:
            raise EOFError("LogDeck hat die Frage nicht beantwortet")
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("id") == qid:
            value = msg.get("value")
            # fürs Protokoll im Terminal
            print(f"{text} → {value}", flush=True)
            return value


def ask(text: str, default: str | None = None) -> str:
    """Freie Eingabe."""
    try:
        v = _ask("text", text, default)
        return "" if v is None else str(v)
    except LookupError:
        hint = f" [{default}]" if default else ""
        v = input(f"{text}{hint}: ").strip()
        return v or (default or "")


def confirm(text: str, default: bool | None = None) -> bool:
    """Ja/Nein."""
    try:
        return bool(_ask("confirm", text, default))
    except LookupError:
        hint = {True: "[J/n]", False: "[j/N]", None: "[j/n]"}[default]
        while True:
            v = input(f"{text} {hint} ").strip().lower()
            if not v and default is not None:
                return default
            if v in ("j", "ja", "y", "yes"):
                return True
            if v in ("n", "nein", "no"):
                return False


def choose(text: str, options: Sequence[Any], default: str | None = None) -> str:
    """Auswahl. options: Strings oder (wert, beschriftung)-Paare. Liefert den Wert."""
    opts = []
    for o in options:
        if isinstance(o, (tuple, list)):
            opts.append({"value": str(o[0]), "label": str(o[1])})
        else:
            opts.append({"value": str(o), "label": str(o)})
    try:
        return str(_ask("choice", text, default, opts))
    except LookupError:
        for i, o in enumerate(opts, 1):
            print(f"  [{i}] {o['label']}")
        while True:
            v = input(f"{text}{f' [{default}]' if default else ''}: ").strip()
            if not v and default is not None:
                return default
            if v.isdigit() and 1 <= int(v) <= len(opts):
                return opts[int(v) - 1]["value"]
            for o in opts:
                if v.lower() in (o["value"].lower(), o["label"].lower()):
                    return o["value"]


_open()
