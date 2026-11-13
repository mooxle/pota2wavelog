#!/usr/bin/env python3
"""
pota2wavelog.py  v2.0
------------------------------
Creates Wavelog station locations for POTA activations.

Usage:
  Interactive:        python pota2wavelog.py
  CLI (1 park):       python pota2wavelog.py DE-0034
  CLI (N-fer):        python pota2wavelog.py DE-0034 DE-1197
  CLI + locator:      python pota2wavelog.py DE-0034 --locator JO40HJ
  ADIF import:        python pota2wavelog.py --adif activation.adi
  ADIF + locator:     python pota2wavelog.py --adif activation.adi --locator JO40PL
  ADIF + merge dist.: python pota2wavelog.py --adif act.adi --merge-distance 5

ADIF mode creates the station AND imports all QSOs in one step.

Configuration (searched in this order):
  1. Environment variables: WAVELOG_URL, WAVELOG_API_KEY, WAVELOG_CALLSIGN
  2. ~/.wavelog.conf  (INI format, section [wavelog])

QRZ keys: qrz_api_key belongs to the configured callsign; other callsigns
(e.g. S5/DM6AX) need their own option qrz_api_key_s5_dm6ax.
"""

import argparse
import configparser
import json
import math
import os
import re
import sys
import uuid
from pathlib import Path

import http.client
import urllib.request
import urllib.parse
import urllib.error

import logdeck as ld  # LogDeck integration (dialogs, progress); no effect in a terminal


def _confirm(cli_prompt: str, text: str, default: bool = True) -> bool:
    """Yes/no: a dialog in LogDeck, the unchanged input() prompt in a terminal.
    default=True: anything but "n" means yes; default=False: only "y" means yes."""
    if ld.active():
        return ld.confirm(text, default=default)
    a = input(cli_prompt).strip().lower()
    return a != "n" if default else a == "y"


def _text(cli_prompt: str, text: str, default: str = "") -> str:
    """Free text: a dialog in LogDeck (pre-filled with default), input() in a terminal.
    Returns what was entered; in a terminal an empty answer stays empty (callers keep their default)."""
    if ld.active():
        v = ld.ask(text, default=default or None).strip()
        return "" if v == default else v
    return input(cli_prompt).strip()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DXCC_DE = {
    "station_dxcc": "230",
    "dxccname":     "FEDERAL REPUBLIC OF GERMANY",
    "dxccprefix":   "DL",
    "station_cq":   "14",
    "station_itu":  "28",
}

# CQ / ITU zones for entities that lie entirely in one zone, keyed by DXCC
# entity number (= POTA entityId). Convenience table only — entities not listed
# get empty zones. Germany is handled by DXCC_DE.
ENTITY_ZONES = {
    206: ("15", "28"),  # Austria
    239: ("15", "28"),  # Hungary
    248: ("15", "28"),  # Italy
    269: ("15", "28"),  # Poland
    287: ("14", "28"),  # Switzerland
    497: ("15", "28"),  # Croatia
    499: ("15", "28"),  # Slovenia
    503: ("15", "28"),  # Czech Republic
    504: ("15", "28"),  # Slovak Republic
}

# Maps both English and German state names (as returned by Nominatim) to
# their two-letter ISO 3166-2:DE codes.
STATE_CODE_MAP = {
    "Baden-Württemberg": "BW", "Bavaria": "BY", "Bayern": "BY",
    "Berlin": "BE", "Brandenburg": "BB", "Bremen": "HB", "Hamburg": "HH",
    "Hesse": "HE", "Hessen": "HE", "Mecklenburg-Vorpommern": "MV",
    "Lower Saxony": "NI", "Niedersachsen": "NI",
    "North Rhine-Westphalia": "NW", "Nordrhein-Westfalen": "NW",
    "Rhineland-Palatinate": "RP", "Rheinland-Pfalz": "RP",
    "Saarland": "SL", "Saxony": "SN", "Sachsen": "SN",
    "Saxony-Anhalt": "ST", "Sachsen-Anhalt": "ST",
    "Schleswig-Holstein": "SH", "Thuringia": "TH", "Thüringen": "TH",
}

# GPS drift threshold: locators within this distance are considered the
# same location and will be offered for merging.
# Override with --merge-distance (in km).
DEFAULT_MERGE_DISTANCE_KM = 5.0

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config() -> dict:
    cfg = {
        "url":          os.environ.get("WAVELOG_URL",          ""),
        "api_key":      os.environ.get("WAVELOG_API_KEY",      ""),
        "callsign":     os.environ.get("WAVELOG_CALLSIGN",     ""),
        "home_locator": os.environ.get("WAVELOG_HOME_LOCATOR", ""),
        "qrz_api_key":  os.environ.get("WAVELOG_QRZ_API_KEY",  ""),
        "qrz_keys":     {},   # per-callsign QRZ keys: {"S5/DM6AX": "..."}
        "rigs":         {},   # [rigs]     id = name, in file order
        "antennas":     {},   # [antennas] id = name, in file order
        "default_rig":  "",   # [setup] rig / antenna: preselected at import
        "default_antenna": "",
    }
    conf_path = Path.home() / ".wavelog.conf"
    if conf_path.exists():
        p = configparser.ConfigParser(interpolation=None)
        p.read(conf_path)
        for sect, key in (("rigs", "rigs"), ("antennas", "antennas")):
            if p.has_section(sect):
                cfg[key] = {k: v.strip() for k, v in p.items(sect) if v.strip()}
        cfg["default_rig"]     = p.get("setup", "rig",     fallback="").strip().lower()
        cfg["default_antenna"] = p.get("setup", "antenna", fallback="").strip().lower()
        s = "wavelog"
        if p.has_section(s):
            cfg["url"]          = cfg["url"]          or p.get(s, "url",          fallback="")
            cfg["api_key"]      = cfg["api_key"]      or p.get(s, "api_key",      fallback="")
            cfg["callsign"]     = cfg["callsign"]     or p.get(s, "callsign",     fallback="")
            cfg["home_locator"] = cfg["home_locator"] or p.get(s, "home_locator", fallback="")
            # Legacy: bare qrz_api_key = key of the main callsign (still honoured)
            cfg["qrz_api_key"]  = cfg["qrz_api_key"]  or p.get(s, "qrz_api_key",  fallback="")
            # Per-callsign keys: qrz_api_key_dm6ax, qrz_api_key_s5_dm6ax = ...
            # → "DM6AX", "S5/DM6AX" (same convention as wavelog-qrz-reconcile)
            for opt, val in p.items(s):
                if opt.startswith("qrz_api_key_") and val.strip():
                    call = opt[len("qrz_api_key_"):].upper().replace("_", "/", 1)
                    cfg["qrz_keys"][call] = val.strip()
    return cfg


def strip_portable(callsign: str) -> str:
    """DM6AX/P → DM6AX (portable suffixes share the base callsign's QRZ book)."""
    cs = callsign.upper().strip()
    if cs.endswith(("/P", "/M", "/QRP", "/A", "/MM")):
        cs = cs.rsplit("/", 1)[0]
    return cs


def qrz_option_name(callsign: str) -> str:
    """S5/DM6AX → qrz_api_key_s5_dm6ax (the ~/.wavelog.conf option name)."""
    return "qrz_api_key_" + strip_portable(callsign).lower().replace("/", "_")


def qrz_key_for(cfg: dict, callsign: str) -> str:
    """
    QRZ logbook key for this callsign. QRZ keeps one logbook per callsign, so a
    key must never be reused for a different one:
      1. the per-callsign key qrz_api_key_<call> wins
      2. legacy: a bare qrz_api_key applies only to the main (configured) callsign
      3. otherwise "" — the station is created without a key and without upload
    """
    cs   = strip_portable(callsign)
    keys = cfg.get("qrz_keys", {})
    if cs in keys:
        return keys[cs]
    main = strip_portable(cfg.get("main_callsign") or cfg.get("callsign", ""))
    return cfg.get("qrz_api_key", "") if cs == main else ""


def save_config(cfg: dict):
    conf_path = Path.home() / ".wavelog.conf"
    p = configparser.ConfigParser(interpolation=None)
    if conf_path.exists():
        p.read(conf_path)
    if not p.has_section("wavelog"):
        p.add_section("wavelog")
    p.set("wavelog", "url",          cfg["url"])
    p.set("wavelog", "api_key",      cfg["api_key"])
    p.set("wavelog", "callsign",     cfg["callsign"])
    p.set("wavelog", "home_locator", cfg.get("home_locator", ""))
    # QRZ keys are stored per callsign (qrz_api_key_<call>); the legacy bare
    # qrz_api_key is migrated to the main callsign's option.
    main = strip_portable(cfg["callsign"])
    key  = qrz_key_for(cfg, cfg["callsign"])
    if key:
        p.set("wavelog", qrz_option_name(main), key)
        p.remove_option("wavelog", "qrz_api_key")
    with open(conf_path, "w") as f:
        p.write(f)
    print(f"  ✓ Saved: {conf_path}")


def ensure_config(cfg: dict) -> dict:
    changed = False
    if not cfg["url"]:
        cfg["url"] = _text("Wavelog URL (e.g. https://log.example.com): ", "Wavelog URL (e.g. https://log.example.com)").rstrip("/")
        changed = True
    if not cfg["api_key"]:
        cfg["api_key"] = _text("Wavelog API key: ", "Wavelog API key")
        changed = True
    if not cfg["callsign"]:
        cfg["callsign"] = _text("Station callsign (e.g. DM6AX): ", "Station callsign (e.g. DM6AX)").upper()
        changed = True
    if not cfg["home_locator"]:
        hl = _text("Your home QTH locator (e.g. JO40JG, Enter to skip): ", "Your home QTH locator (e.g. JO40JG) – leave empty to skip").upper()
        if hl:
            cfg["home_locator"] = hl
            changed = True
    if not qrz_key_for(cfg, cfg["callsign"]):
        qrz = _text(f"QRZ.com logbook API key for {cfg['callsign']} (Enter to skip): ", f"QRZ.com logbook API key for {cfg['callsign']} – leave empty to skip")
        if qrz:
            cfg["qrz_keys"][strip_portable(cfg["callsign"])] = qrz
            changed = True
    if changed and _confirm("Save to ~/.wavelog.conf? [y/N] ", "Save these settings to ~/.wavelog.conf?", default=False):
        save_config(cfg)
    return cfg

# ---------------------------------------------------------------------------
# HTTP (stdlib only)
# ---------------------------------------------------------------------------

def http_get(url: str, timeout: int = 10):
    req = urllib.request.Request(
        url, headers={"User-Agent": "pota2wavelog/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def http_post(url: str, payload, timeout: int = 30) -> tuple:
    """
    POST JSON. Returns (http_status, body). If there is no usable answer at all
    (timeout, connection dropped, non-JSON reply) it returns (0, {"error": ...})
    instead of raising — the request may or may not have been processed, so
    callers must not assume failure means "nothing happened".
    """
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Accept":       "application/json",
        "User-Agent":   "pota2wavelog/2.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:    body = json.loads(e.read().decode())
        except: body = {}
        return e.code, body
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}

# ---------------------------------------------------------------------------
# Maidenhead locator
# ---------------------------------------------------------------------------

def latlon_to_maidenhead(lat: float, lon: float) -> str:
    """GPS coordinates → 6-character Maidenhead locator."""
    lon += 180; lat += 90
    fl  = int(lon / 20);  fa  = int(lat / 10)
    sl  = int((lon % 20) / 2); sa = int(lat % 10)
    ssl = int(((lon % 20) % 2) * 12)
    ssa = int((lat % 1) * 24)
    return (chr(ord('A')+fl) + chr(ord('A')+fa)
            + str(sl) + str(sa)
            + chr(ord('a')+ssl) + chr(ord('a')+ssa)).upper()


def maidenhead_to_latlon(loc: str) -> tuple:
    """Maidenhead → (lat, lon) centre point. Returns (None, None) on error."""
    loc = loc.upper()
    if len(loc) < 4:
        return None, None
    lon = (ord(loc[0])-ord('A'))*20 - 180 + int(loc[2])*2
    lat = (ord(loc[1])-ord('A'))*10 - 90  + int(loc[3])
    if len(loc) >= 6:
        lon += (ord(loc[4])-ord('A'))*(2/24) + (1/24)
        lat += (ord(loc[5])-ord('A'))*(1/24) + (0.5/24)
    else:
        lon += 1.0; lat += 0.5
    return round(lat, 6), round(lon, 6)


def locator_distance_km(loc_a: str, loc_b: str) -> float:
    """Haversine distance between two locators in km."""
    la = maidenhead_to_latlon(loc_a)
    lb = maidenhead_to_latlon(loc_b)
    if la[0] is None or lb[0] is None:
        return float("inf")
    R    = 6371.0
    dlat = math.radians(lb[0] - la[0])
    dlon = math.radians(lb[1] - la[1])
    x    = (math.sin(dlat/2)**2
            + math.cos(math.radians(la[0])) * math.cos(math.radians(lb[0]))
            * math.sin(dlon/2)**2)
    return R * 2 * math.asin(math.sqrt(min(x, 1.0)))

# ---------------------------------------------------------------------------
# Reverse geocoding
# ---------------------------------------------------------------------------

def reverse_geocode(lat: float, lon: float) -> dict:
    url = (f"https://nominatim.openstreetmap.org/reverse"
           f"?lat={lat}&lon={lon}&format=json&accept-language=en")
    print(f"  → Nominatim: ({lat:.4f}, {lon:.4f})")
    try:
        addr = http_get(url).get("address", {})
        city = (addr.get("village") or addr.get("town") or addr.get("city")
                or addr.get("municipality") or addr.get("county") or "")
        state = addr.get("state", "")
        return {"city": city, "state_code": STATE_CODE_MAP.get(state, ""),
                "state_name": state, "country": addr.get("country", "")}
    except Exception as e:
        print(f"  ✗ Geocoding error: {e}")
        return {"city": "", "state_code": "", "state_name": "", "country": ""}

# ---------------------------------------------------------------------------
# POTA API
# ---------------------------------------------------------------------------

def fetch_pota_park(ref: str) -> dict:
    url = f"https://api.pota.app/park/{urllib.parse.quote(ref)}"
    print(f"  → POTA API: {ref}")
    try:
        return http_get(url)
    except Exception as e:
        print(f"  ✗ POTA API error for {ref}: {e}")
        return {}

# ---------------------------------------------------------------------------
# ADIF parser
# ---------------------------------------------------------------------------

def parse_adif_field(tag: str, record: str) -> str:
    m = re.search(r'<' + re.escape(tag) + r':(\d+)(?::[^>]*)?>',
                  record, re.IGNORECASE)
    if not m:
        return ""
    return record[m.end() : m.end() + int(m.group(1))].strip()


def parse_adif_file(filepath: str) -> list:
    text = Path(filepath).read_text(encoding="utf-8", errors="replace")
    eoh  = re.search(r'<EOH>', text, re.IGNORECASE)
    if eoh:
        text = text[eoh.end():]
    qsos = []
    for record in re.split(r'<EOR>', text, flags=re.IGNORECASE):
        record = record.strip()
        if not record:
            continue
        fields = ["MY_POTA_REF", "MY_SIG_INFO", "MY_SIG", "MY_GRIDSQUARE",
                  "STATION_CALLSIGN", "OPERATOR", "QSO_DATE",
                  "DXCC", "COUNTRY", "CQZ", "ITUZ"]
        qso = {f: parse_adif_field(f, record) for f in fields}
        if not qso["MY_POTA_REF"] and qso["MY_SIG"].upper() == "POTA":
            qso["MY_POTA_REF"] = qso["MY_SIG_INFO"]
        if qso["MY_POTA_REF"]:
            qsos.append(qso)
    return qsos


def extract_activations_from_adif(filepath: str,
                                   merge_distance_km: float = DEFAULT_MERGE_DISTANCE_KM,
                                   home_locator: str = "") -> list:
    """
    Reads an ADIF file and returns a list of activation dicts.

    Locator conflict logic:
      Home locator detection:
        If a locator matches the home QTH but contains POTA QSOs, it is
        either a GPS failure or a real activation from home (park right at
        your doorstep). If a real GPS locator exists for the same activation
        in the same file, it is a GPS failure and the real locator is
        preferred automatically. Otherwise you are asked; a home activation is
        flagged ("home_activation") so process_parks lets you pick the station.

      Distance-based merge logic (applied after home cleanup):
        1. Distance ≤ merge_distance_km AND same POTA refs  → suggest GPS drift merge
        2. Distance ≤ merge_distance_km AND different refs  → ask about N-fer
        3. Distance > merge_distance_km                     → always keep separate
    """
    qsos = parse_adif_file(filepath)
    if not qsos:
        print("  ✗ No POTA QSOs found in ADIF file.")
        return []

    home = home_locator.upper().strip()

    # Step 1: Raw grouping by exact locator.
    # Each group gets an "is_home_fallback" flag when the locator matches
    # the home QTH (= no GPS fix during the POTA activation).
    groups = {}
    for qso in qsos:
        loc  = qso["MY_GRIDSQUARE"].upper()
        refs = [r.strip() for r in
                qso["MY_POTA_REF"].upper().replace(";", ",").split(",") if r.strip()]
        if loc not in groups:
            groups[loc] = {
                "refs":             set(),
                "locator":          loc,
                "date":             qso["QSO_DATE"],
                "callsign":         (qso["STATION_CALLSIGN"] or qso["OPERATOR"]).upper(),
                "dxcc":             qso["DXCC"],
                "country":          qso["COUNTRY"],
                "cqz":              qso["CQZ"],
                "ituz":             qso["ITUZ"],
                "count":            0,
                "is_home_fallback": bool(home and loc == home),
                "home_activation":  False,
            }
        groups[loc]["refs"].update(refs)
        groups[loc]["count"] += 1

    # Step 1b: Home fallback cleanup.
    # If groups exist that are NOT the home QTH but share the same refs,
    # the home group is a GPS failure → reassign its QSOs to the real locator.
    if home and home in groups and groups[home]["is_home_fallback"]:
        home_refs  = groups[home]["refs"]
        home_count = groups[home]["count"]
        # Find real locators with overlapping refs
        real_locs  = [
            loc for loc, g in groups.items()
            if loc != home and not g["is_home_fallback"]
            and g["refs"] & home_refs  # at least one ref in common
        ]
        if real_locs:
            # Choose the best real locator (most QSOs = most stable GPS)
            best = max(real_locs, key=lambda l: groups[l]["count"])
            print()
            print(f"  ⚠️  Home QTH locator detected: {home}")
            print(f"     {home_count} QSOs were logged with home locator (no GPS fix).")
            print(f"     Real activation locator from GPS: {best} ({groups[best]['count']} QSOs)")
            print(f"     → Home group automatically reassigned to {best}.")
            # Merge home group into the real locator
            groups[best]["refs"].update(groups[home]["refs"])
            groups[best]["count"] += groups[home]["count"]
            del groups[home]
        else:
            # No other locator in this file → either a real activation from home
            # (park right at the doorstep) or a GPS failure: ask.
            print()
            print(f"  ⚠️  Home QTH locator detected: {home}")
            print(f"     {home_count} QSOs were logged with the home locator.")
            if _confirm("     Was this an activation from your home QTH "
                        "(park right at your doorstep)? [Y/n] ",
                        f"Home QTH locator {home} found ({home_count} QSOs). Was this an activation "
                        f"from your home QTH (park right at your doorstep)?\n\nNo = GPS failure, "
                        f"the locator will be corrected manually.", default=True):
                # Deliberate home activation → not a GPS failure
                groups[home]["is_home_fallback"] = False
                groups[home]["home_activation"]  = True
                print(f"     → Home activation. You choose the station in step 4/4.")
            else:
                print(f"     → Treated as GPS failure — locator must be corrected manually (step 2/4).")

    if len(groups) == 1:
        g = list(groups.values())[0]
        return [{"refs": sorted(g["refs"]), "locator": g["locator"],
                 "date": g["date"], "callsign": g["callsign"],
                 "dxcc": g["dxcc"], "country": g["country"],
                 "cqz":  g["cqz"],  "ituz":    g["ituz"],
                 "qso_count":       g["count"],
                 "home_activation": g.get("home_activation", False)}]

    # Step 2: Check pairs and collect merge decisions.
    locators = list(groups.keys())
    parent   = {loc: loc for loc in locators}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    def union(x, y, winner):
        px, py = find(x), find(y)
        parent[px] = py
        # remember winner so the correct locator is selected
        groups[py]["_winner"] = winner

    print()
    header_shown = False

    for i, loc_a in enumerate(locators):
        for loc_b in locators[i+1:]:
            # Already in the same group?
            if find(loc_a) == find(loc_b):
                continue

            dist       = locator_distance_km(loc_a, loc_b)
            same_refs  = groups[loc_a]["refs"] == groups[loc_b]["refs"]
            count_a    = groups[loc_a]["count"]
            count_b    = groups[loc_b]["count"]
            winner     = loc_a if count_a >= count_b else loc_b

            if dist > merge_distance_km:
                # Clearly different locations → keep separate silently
                continue

            # ─── Nearby locators: interactive decision ───────────────────
            if not header_shown:
                print("  ┌─ Nearby locators detected ──────────────────────────────")
                header_shown = True

            refs_a = sorted(groups[loc_a]["refs"])
            refs_b = sorted(groups[loc_b]["refs"])
            print(f"  │")
            print(f"  │  {loc_a}  ({count_a} QSOs | refs: {', '.join(refs_a)})")
            print(f"  │  {loc_b}  ({count_b} QSOs | refs: {', '.join(refs_b)})")
            print(f"  │  Distance: {dist:.1f} km  (threshold: {merge_distance_km} km)")

            if same_refs:
                print(f"  │  → Same refs: likely GPS drift")
                if _confirm(f"  │    Merge into one station? [y/n] ",
                            f"{loc_a} ({count_a} QSOs) and {loc_b} ({count_b} QSOs) are {dist:.1f} km apart "
                            f"with the same refs ({', '.join(refs_a)}) – likely GPS drift.\n\n"
                            f"Merge into one station?", default=True):
                    union(loc_a, loc_b, winner)
                    print(f"  │    ✓ Merged → locator: {winner} ({max(count_a,count_b)} QSOs)")
                else:
                    print(f"  │    → Kept separate.")
            else:
                print(f"  │  → Different refs: possible N-fer")
                print(f"  │    [1] N-fer: one station with all refs")
                print(f"  │    [2] Separate stations")
                while True:
                    if ld.active():
                        choice = ld.choose(
                            f"{loc_a} ({', '.join(refs_a)}) and {loc_b} ({', '.join(refs_b)}) are "
                            f"{dist:.1f} km apart with different refs – possible N-fer.",
                            [("1", "N-fer: one station with all refs"), ("2", "Separate stations")], default="1")
                    else:
                        choice = input(f"  │  Choice [1/2]: ").strip()
                    if choice == "1":
                        union(loc_a, loc_b, winner)
                        all_refs = sorted(groups[loc_a]["refs"] | groups[loc_b]["refs"])
                        print(f"  │    ✓ N-fer → {', '.join(all_refs)} | locator: {winner}")
                        break
                    elif choice == "2":
                        print(f"  │    → Separate stations.")
                        break
                    else:
                        print(f"  │    Please enter 1 or 2.")

    if header_shown:
        print(f"  └─────────────────────────────────────────────────────────")
        print()

    # Step 3: Merge Union-Find groups.
    merged = {}
    for loc in locators:
        root = find(loc)
        g    = groups[loc]
        if root not in merged:
            merged[root] = {
                "refs":            set(),
                "locator":         groups[root].get("_winner", root),
                "date":            g["date"],
                "callsign":        g["callsign"],
                "dxcc":            g["dxcc"],
                "country":         g["country"],
                "cqz":             g["cqz"],
                "ituz":            g["ituz"],
                "count":           0,
                "locators_merged": [],
                "home_activation": False,
            }
        merged[root]["refs"].update(g["refs"])
        merged[root]["count"] += g["count"]
        merged[root]["locators_merged"].append(loc)
        # a merged activation counts as home activation if any member was one
        merged[root]["home_activation"] = (merged[root]["home_activation"]
                                           or g.get("home_activation", False))
        # propagate winner from child nodes if set
        if "_winner" in groups[root]:
            merged[root]["locator"] = groups[root]["_winner"]

    activations = []
    for root, g in merged.items():
        act = {
            "refs":      sorted(g["refs"]),
            "locator":   g["locator"],
            "date":      g["date"],
            "callsign":  g["callsign"],
            "dxcc":      g["dxcc"],
            "country":   g["country"],
            "cqz":       g["cqz"],
            "ituz":      g["ituz"],
            "qso_count": g["count"],
            "home_activation": g.get("home_activation", False),
        }
        if len(g["locators_merged"]) > 1:
            act["merged_from"] = sorted(g["locators_merged"])
        activations.append(act)

    return activations

# ---------------------------------------------------------------------------
# Wavelog API
# ---------------------------------------------------------------------------

def get_existing_stations(cfg: dict) -> list:
    url = f"{cfg['url']}/index.php/api/station_info/{cfg['api_key']}"
    print(f"  → Wavelog: fetching stations...")
    try:
        data = http_get(url)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"  ✗ Wavelog API error: {e}")
        return []


def check_duplicate(existing: list, pota_refs: list, callsign: str,
                    new_locator: str = "", merge_distance_km: float = DEFAULT_MERGE_DISTANCE_KM) -> list:
    refs_set = {r.upper() for r in pota_refs}
    dupes = []
    for s in existing:
        if not any(ref in s.get("station_profile_name", "").upper() for ref in refs_set):
            continue
        # Only flag as duplicate if same location (when locator is known)
        if new_locator:
            existing_loc = s.get("station_gridsquare", "")
            if existing_loc and locator_distance_km(new_locator, existing_loc) > merge_distance_km:
                continue  # Same ref, but different location → not a duplicate
        dupes.append(s)
    return dupes


def create_station(cfg: dict, payload: dict) -> tuple:
    url = f"{cfg['url']}/index.php/api/create_station/{cfg['api_key']}"
    # Send only fields that carry a value. Wavelog's create_station was seen to
    # hang (Cloudflare 502 after ~50 s, nothing created) for JSON bodies of
    # ~900 bytes and more, while 834 bytes went through in 0.1 s. The full field
    # set with a long park name reaches ~900; without the empty fields it is
    # ~630. Omitted fields fall back to Wavelog's defaults (verified).
    slim = {k: v for k, v in payload.items() if v not in ("", None)}
    size = len(json.dumps([slim]).encode())
    if size > 850:
        print(f"  ⚠️  Request body is {size} bytes — create_station may hang above ~900.")
    print(f"  → Wavelog: creating station...")
    return http_post(url, [slim], timeout=60)

# ---------------------------------------------------------------------------
# Build station payload
# ---------------------------------------------------------------------------

def dxcc_from_park(park_data: dict) -> dict:
    """
    DXCC fields for the station location, taken from the park itself: the POTA
    API's entityId is the DXCC entity number (SI-0001 → 499 Slovenia). This is
    where the station is operated, so it is right for any callsign — DM6AX in
    Slovenia as well as S5/DM6AX. Returns {} if the park has no entity.
    """
    try:
        entity = int(park_data.get("entityId"))
    except (TypeError, ValueError):
        return {}
    if entity == 230:
        return dict(DXCC_DE)
    cq, itu = ENTITY_ZONES.get(entity, ("", ""))
    return {
        "station_dxcc": str(entity),
        "dxccname":     (park_data.get("entityName") or "").upper(),
        "dxccprefix":   "",
        "station_cq":   cq,
        "station_itu":  itu,
    }


def build_station_payload(cfg, pota_refs, park_data, locator, geo,
                           link_logbook=True, adif_meta=None) -> dict:
    refs_str   = " + ".join(pota_refs)
    park_name  = park_data.get("name", "")
    city       = geo.get("city", "")

    parts = [f"POTA {refs_str}"]
    if park_name: parts.append(park_name)
    parts.append(locator)
    if city:      parts.append(city)

    # DXCC: primarily from the park's POTA entity (where we operate). The ADIF
    # DXCC field is no use here — it belongs to the contact, not to our station.
    dxcc = dxcc_from_park(park_data)

    callsign = cfg.get("callsign", "").upper()
    qrz_key  = qrz_key_for(cfg, callsign)
    # Fallback (no park data): decide by callsign. For DM6AX/P the first part
    # is the call itself; for prefix calls like S5/DM6AX it is the operating
    # prefix (S5) — so only German calls and German-prefixed calls count here.
    base_call   = callsign.split("/")[0]
    call_prefix = base_call[:2]
    is_germany  = call_prefix in (
        "DA","DB","DC","DD","DE","DF","DG","DH","DI",
        "DJ","DK","DL","DM","DN","DO","DP","DQ","DR"
    )

    if dxcc:
        pass
    elif is_germany:
        dxcc = dict(DXCC_DE)
    else:
        # Non-Germany and no park data: leave DXCC fields empty.
        dxcc = {
            "station_dxcc":  adif_meta.get("dxcc", "") if adif_meta else "",
            "dxccname":      adif_meta.get("country", "").upper() if adif_meta else "",
            "dxccprefix":    "",
            "station_cq":    adif_meta.get("cqz",  "") if adif_meta else "",
            "station_itu":   adif_meta.get("ituz", "") if adif_meta else "",
        }

    return {
        "station_profile_name": " - ".join(parts),
        "station_callsign":     cfg["callsign"],
        "station_gridsquare":   locator,
        "station_city":         city,
        "state":                geo.get("state_code", ""),
        "station_pota":         ",".join(pota_refs),
        "station_sota":         "",
        "station_wwff":         "",
        "station_iota":         "",
        "station_sig":          "",
        "station_sig_info":     "",
        "station_power":        "100",
        "station_active":       "0",
        "link_active_logbook":  "1" if link_logbook else "0",
        "station_uuid":         str(uuid.uuid4()),
        **dxcc,
        "eqslqthnickname":     "",
        "eqsl_default_qslmsg": "",
        "hrdlog_username":     "",
        "oqrs":                "0",
        "oqrs_text":           "",
        "oqrs_email":          "0",
        "webadifrealtime":     "0",
        "clublogrealtime":     "0",
        "clublogignore":       "0",
        "hrdlogrealtime":      "0",
        # QRZ key: supported by create_station since Wavelog >2.5.1 (field
        # "qrzapikey"). qrzrealtime: -1=Disabled, 0=Enabled, 1=Realtime.
        # Enable upload (non-realtime) only if there is a key for THIS callsign
        # (see qrz_key_for) — never switch on an upload with another call's key.
        "qrzrealtime":         "0" if qrz_key else "-1",
        "qrzapikey":           qrz_key,
        "county":              None,
        "station_cnty":        "",
    }

# ---------------------------------------------------------------------------
# Confirmation step
# ---------------------------------------------------------------------------

def confirm_station(payload: dict):
    print()
    print("━" * 62)
    print("  Planned station location:")
    print("━" * 62)
    print(f"  Name:     {payload['station_profile_name']}")
    print(f"  Call:     {payload['station_callsign']}")
    print(f"  Locator:  {payload['station_gridsquare']}")
    print(f"  City:     {payload['station_city']}")
    print(f"  State:    {payload['state']}")
    print(f"  POTA:     {payload['station_pota']}")
    print(f"  DXCC:     {payload['dxccname']} ({payload['station_dxcc']})")
    print(f"  CQ/ITU:   {payload['station_cq']} / {payload['station_itu']}")
    if payload.get("qrzapikey"):
        print(f"  QRZ.com:  key + upload will be enabled automatically (non-realtime)")
    else:
        opt = qrz_option_name(payload["station_callsign"])
        print(f"  QRZ.com:  no key for {payload['station_callsign']} — upload stays disabled")
        print(f"            (add {opt} to ~/.wavelog.conf)")
    print("━" * 62)
    print()
    while True:
        if ld.active():
            ld.table(["Field", "Value"], [
                ["Name", payload["station_profile_name"]], ["Call", payload["station_callsign"]],
                ["Locator", payload["station_gridsquare"]], ["City", payload["station_city"]],
                ["State", payload["state"]], ["POTA", payload["station_pota"]],
                ["DXCC", f"{payload['dxccname']} ({payload['station_dxcc']})"],
                ["CQ / ITU", f"{payload['station_cq']} / {payload['station_itu']}"],
                ["QRZ.com", "upload enabled (key found)" if payload.get("qrzapikey")
                 else f"no key for {payload['station_callsign']} – upload disabled"],
            ], id="planned", title="Planned station location")
            a = ld.choose("Create this station location in Wavelog?", [
                ("y", "Create station"), ("e", "Change name"), ("l", "Change locator"),
                ("c", "Change city"), ("n", "Cancel")], default="y")
        else:
            a = input("  [y] Create  [n] Cancel  [e] Name  [l] Locator  [c] City\n"
                      "  Choice: ").strip().lower()
        if a == "y":
            return payload
        elif a == "n":
            print("  Cancelled."); return None
        elif a == "e":
            val = _text(f"  New name [{payload['station_profile_name']}]: ", "Station name",
                        payload["station_profile_name"])
            if val: payload["station_profile_name"] = val
        elif a == "l":
            val = _text(f"  New locator [{payload['station_gridsquare']}]: ", "Locator",
                        payload["station_gridsquare"]).upper()
            if val: payload["station_gridsquare"] = val
        elif a == "c":
            val = _text(f"  New city [{payload['station_city']}]: ", "City", payload["station_city"])
            if val: payload["station_city"] = val
        else:
            print("  Please enter y, n, e, l or c.")

# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def _ask_home_station_choice(existing_stations: list, locator: str,
                             pota_refs: list) -> str:
    """
    Home activation (park right at the home QTH): ask which station gets the QSOs.

    Returns "new" (create a POTA station) or the station_id of an existing one:
      - home stations:  same locator, no POTA ref set in the profile
      - POTA stations already created for these refs at this locator
        (e.g. from an earlier run, so a rerun doesn't create a second one)
    """
    loc6 = locator.upper()[:6]
    home_stns = [
        s for s in existing_stations
        if not (s.get("station_pota") or "").strip()
        and (s.get("station_gridsquare") or "").upper()[:6] == loc6
    ]
    home_ids  = {str(s.get("station_id")) for s in home_stns}
    pota_stns = [s for s in check_duplicate(existing_stations, pota_refs, "",
                                            new_locator=locator)
                 if str(s.get("station_id")) not in home_ids]

    refs_str = ", ".join(pota_refs)
    options  = {"1": "new"}
    print()
    print(f"  ┌─ Home activation ─────────────────────────────────────────")
    print(f"  │  Locator {locator} is your home QTH.")
    print(f"  │  POTA refs: {refs_str}")
    print(f"  │")
    print(f"  │  [1] Create a new POTA station  ({refs_str} @ {locator})")
    for kind, stations in (("home station", home_stns), ("existing POTA station", pota_stns)):
        for s in stations:
            key = str(len(options) + 1)
            options[key] = str(s.get("station_id"))
            print(f"  │  [{key}] Import into {kind}: [{s.get('station_id')}] "
                  f"{s.get('station_profile_name')}")
    print(f"  └───────────────────────────────────────────────────────────")

    if ld.active():
        labels = {"1": f"Create a new POTA station ({refs_str} @ {locator})"}
        for kind, stations in (("home station", home_stns), ("existing POTA station", pota_stns)):
            for s in stations:
                key = next(k for k, v in options.items() if v == str(s.get("station_id")))
                labels[key] = f"Import into {kind}: [{s.get('station_id')}] {s.get('station_profile_name')}"
        choice = ld.choose(f"Home activation: locator {locator} is your home QTH ({refs_str}). "
                           f"Which station gets the QSOs?", [(k, labels[k]) for k in options], default="1")
        return options[choice]
    while True:
        choice = input(f"  Choice [{'/'.join(options)}]: ").strip()
        if choice in options:
            return options[choice]
        print(f"  Please enter one of: {', '.join(options)}")


def process_parks(cfg, pota_refs, manual_locator="", adif_locator="",
                  adif_meta=None, existing_stations=None,
                  home_activation=False) -> bool:
    refs_label = " + ".join(pota_refs)
    print()
    print(f"▶  POTA: {' + '.join(pota_refs)}")

    # 1. POTA API
    print(f"\n  [1/4] Park data...")
    park_data = {}
    pota_lat = pota_lon = None
    for ref in pota_refs:
        data = fetch_pota_park(ref)
        if data and not park_data:
            park_data = data
            try:
                pota_lat = float(data.get("latitude",  0))
                pota_lon = float(data.get("longitude", 0))
            except (TypeError, ValueError):
                pass
        if data:
            print(f"       ✓ {data.get('name','?')} ({data.get('locationName','')})")
        else:
            print(f"       ✗ No data for {ref}")
    ld.step(f"{refs_label} · park data", "ok" if park_data else "warn", id=f"{refs_label}:park",
            detail=(f"{park_data.get('name', '?')} ({park_data.get('locationName', '')})" if park_data
                    else "no data from the POTA API"))

    # 2. Locator
    print(f"\n  [2/4] Locator...")
    if manual_locator:
        locator = manual_locator.upper()
        print(f"       ✓ Manual (--locator): {locator}")
    elif adif_locator and len(adif_locator) >= 6:
        locator = adif_locator.upper()
        print(f"       ✓ From ADIF: {locator}")
        override = _text(f"       Accept [{locator}] or enter new (Enter to accept): ",
                         f"{refs_label}: locator from the ADIF file – accept or correct", locator).upper()
        if override: locator = override
    elif adif_locator and len(adif_locator) == 4:
        print(f"       ⚠  ADIF locator is only 4 characters: {adif_locator.upper()}")
        if pota_lat is not None:
            calc = latlon_to_maidenhead(pota_lat, pota_lon)
            print(f"       ℹ  Calculated from POTA GPS: {calc}")
            val = _text(f"       Use [{calc}]: ",
                        f"{refs_label}: the ADIF locator {adif_locator.upper()} has only 4 characters. "
                        f"Calculated from the POTA park position:", calc).upper()
            locator = val if val else calc
        else:
            val = _text(f"       Enter manually [{adif_locator.upper()}]: ",
                        f"{refs_label}: the ADIF locator has only 4 characters – enter the full locator",
                        adif_locator.upper()).upper()
            locator = val if val else adif_locator.upper()
    elif pota_lat is not None:
        locator = latlon_to_maidenhead(pota_lat, pota_lon)
        print(f"       ✓ From POTA GPS: {locator}")
        override = _text(f"       Accept [{locator}] or enter new (Enter to accept): ",
                         f"{refs_label}: locator from the POTA park position – accept or correct", locator).upper()
        if override: locator = override
    else:
        locator = _text("       GPS not available. Enter locator manually: ",
                        f"{refs_label}: no position available – enter the locator").upper()

    ld.step(f"{refs_label} · locator", "ok" if locator else "warn", id=f"{refs_label}:locator",
            detail=locator or "none")

    # 3. Reverse geocoding (locator → GPS → Nominatim)
    print(f"\n  [3/4] Location...")
    gc_lat, gc_lon = maidenhead_to_latlon(locator)
    if gc_lat is not None:
        geo = reverse_geocode(gc_lat, gc_lon)
        print(f"       ✓ {geo['city']}, {geo['state_name']} ({geo['state_code']})")
        override = _text(f"       Accept [{geo['city']}] or enter new (Enter to accept): ",
                         f"{refs_label}: city for locator {locator} – accept or correct", geo["city"])
        if override: geo["city"] = override
    else:
        geo = {"city": "", "state_code": "", "state_name": "", "country": ""}
        geo["city"] = _text("       Enter city manually: ", f"{refs_label}: enter the city")

    ld.step(f"{refs_label} · location", "ok", id=f"{refs_label}:geo",
            detail=", ".join(x for x in (geo.get("city"), geo.get("state_name")) if x) or "–")

    # 4. Station selection (home activation) or duplicate check
    if existing_stations is None:
        existing_stations = get_existing_stations(cfg)
    existing_station_id = None
    if home_activation:
        print(f"\n  [4/4] Station selection (home activation)...")
        choice = _ask_home_station_choice(existing_stations, locator, pota_refs)
        if choice != "new":
            print(f"  → Using station ID: {choice}")
            ld.step(f"{refs_label} · station", "ok", id=f"{refs_label}:station",
                    detail=f"existing station ID {choice}")
            return choice
        # "new": the user chose explicitly → no duplicate check, create below
        dupes = []
    else:
        print(f"\n  [4/4] Duplicate check...")
        dupes = check_duplicate(existing_stations, pota_refs, cfg["callsign"],
                                new_locator=locator)
    if dupes:
        print(f"  ⚠️  Station already exists:")
        for d in dupes:
            print(f"     • [{d.get('station_id')}] {d.get('station_profile_name')}")
        names = ", ".join(f"[{d.get('station_id')}] {d.get('station_profile_name')}" for d in dupes)
        if not _confirm("  Import QSOs into this station anyway? [y/n] ",
                        f"{refs_label}: station already exists: {names}.\n\n"
                        f"Use this station (import the QSOs into it)?", default=True):
            if not _confirm("  Create a new station instead? [y/n] ",
                            f"{refs_label}: create a new station instead? No = skip this activation.",
                            default=False):
                print("  Skipped.")
                ld.step(f"{refs_label} · station", "skip", id=f"{refs_label}:station", detail="skipped")
                return None
            print("  → Proceeding to create a new station.")
            # Fall through to payload building below
        else:
            # Use the ID of the first match (user confirmed)
            existing_station_id = dupes[0].get("station_id")
            print(f"  → Using station ID: {existing_station_id}")
            ld.step(f"{refs_label} · station", "ok", id=f"{refs_label}:station",
                    detail=f"existing station ID {existing_station_id}")
    elif not home_activation:
        print("       ✓ No duplicates")

    # Station already known → return ID directly, skip creation
    if existing_station_id is not None:
        return existing_station_id

    # Build payload and confirm
    link    = _confirm("\n  Link station to active logbook? [y/n] ",
                       f"{refs_label}: link the new station to the active logbook?", default=True)
    payload = build_station_payload(cfg, pota_refs, park_data, locator, geo,
                                    link_logbook=link, adif_meta=adif_meta)
    confirmed = confirm_station(payload)
    if confirmed is None:
        ld.step(f"{refs_label} · station", "skip", id=f"{refs_label}:station", detail="cancelled")
        return None
    ld.step(f"{refs_label} · station", "run", id=f"{refs_label}:station", detail="creating …")

    print("\n  Creating station...")
    status, response = create_station(cfg, confirmed)
    if status == 0 or status >= 500:
        # No usable answer (timeout, dropped connection, 5xx from Wavelog or a
        # proxy like Cloudflare): the server may still have created the
        # station, so look before reporting a failure.
        why = response.get("error") or response.get("title") or f"HTTP {status}"
        print(f"  ⚠️  No usable answer from Wavelog ({why})")
        print(f"     Response: {str(response)[:300]}")
        print(f"     Checking whether the station was created anyway...")
        match = next((s for s in get_existing_stations(cfg)
                      if s.get("station_profile_name") == confirmed["station_profile_name"]), None)
        if match:
            print(f"  ✅ Station exists after all → ID {match.get('station_id')}")
            ld.step(f"{refs_label} · station", "ok", id=f"{refs_label}:station",
                    detail=f"created (ID {match.get('station_id')})")
            return match.get("station_id")
        print(f"  ✗ Not created. Nothing was left behind, so a rerun is safe.")
        ld.step(f"{refs_label} · station", "err", id=f"{refs_label}:station", detail=f"not created ({why})")
        print(f"     If it fails the same way again, the problem is on the Wavelog server")
        print(f"     (check its PHP / web server log for this request).")
        return None
    if status == 201:
        # Look up station ID from station list (API does not return it directly)
        print(f"  ✅ Created: {confirmed['station_profile_name']}")
        if confirmed.get("qrzapikey"):
            print(f"  ℹ️  QRZ key set and upload enabled (non-realtime).")
        else:
            ld.note(f"No QRZ key for {confirmed['station_callsign']} – QRZ upload is disabled for this station.",
                    "warn")
            print(f"  ℹ️  No QRZ key for {confirmed['station_callsign']} — upload is disabled. "
                  f"Add {qrz_option_name(confirmed['station_callsign'])} to ~/.wavelog.conf "
                  f"and set key + upload in Wavelog (Edit station → QRZ.com).")
        updated = get_existing_stations(cfg)
        # Find the most recently created station matching the POTA ref
        for s in reversed(updated):
            name = s.get("station_profile_name", "")
            if any(ref in name for ref in pota_refs):
                sid = s.get("station_id")
                print(f"  → Station ID: {sid}")
                ld.step(f"{refs_label} · station", "ok", id=f"{refs_label}:station",
                        detail=f"created: {confirmed['station_profile_name']} (ID {sid})")
                return sid
        print(f"  ⚠️  Could not determine station ID.")
        return None
    elif status == 200 and response.get("status") == "dupe":
        print(f"  ⚠️  Wavelog: already exists (internal check).")
        ld.step(f"{refs_label} · station", "warn", id=f"{refs_label}:station", detail="already exists in Wavelog")
        return None
    else:
        print(f"  ✗ Error (HTTP {status}): {response}")
        ld.step(f"{refs_label} · station", "err", id=f"{refs_label}:station", detail=f"error HTTP {status}")
        return None


# ---------------------------------------------------------------------------
# Rig / antenna setup (written into the QSO comment)
# ---------------------------------------------------------------------------
#
# ~/.wavelog.conf:
#   [rigs]
#   ft891 = Yaesu FT-891
#   [antennas]
#   vdipole = Vertical Dipole - 4m high
#   [setup]            ; optional defaults
#   rig = ft891
#   antenna = vdipole
#
# The chosen names end up in each QSO's COMMENT as "[antenna] … [rig] …" —
# the tags wavelog-qsl-convert reads for the QSL card.

NONE_CHOICE = "-"   # option value for "leave this one out"


def resolve_setup_item(items: dict, wanted: str) -> str | None:
    """Config id or (case-insensitive) name → the display name; None if unknown."""
    w = wanted.strip().lower()
    for key, name in items.items():
        if w in (key.lower(), name.lower()):
            return name
    return None


def pick_setup_item(kind: str, items: dict, wanted: str = "", default: str = "") -> str:
    """
    Name of the rig/antenna to use, or "" for none.
      wanted:  --rig/--antenna value (id or name) — no question asked
      default: preselected config id (or name) in the question
    One configured entry is used without asking; several are asked for, in a
    LogDeck dialog or on the terminal (ld.choose falls back to input()).
    """
    if wanted:   # unknown names are taken literally (main() rejects them when a list exists)
        return resolve_setup_item(items, wanted) or wanted.strip()
    if not items:
        return ""
    if len(items) == 1:
        return next(iter(items.values()))
    options = [(name, name) for name in items.values()] + [(NONE_CHOICE, f"(no {kind})")]
    preset  = resolve_setup_item(items, default) if default else None
    choice  = ld.choose(f"Which {kind} did you use?", options, default=preset or None)
    return "" if choice == NONE_CHOICE else choice


def choose_setup(cfg: dict, rig: str = "", antenna: str = "",
                 previous: tuple | None = None) -> tuple:
    """(rig name, antenna name) for one activation. `previous` = last choice of this run."""
    rig_default = previous[0] if previous else cfg.get("default_rig", "")
    ant_default = previous[1] if previous else cfg.get("default_antenna", "")
    return (pick_setup_item("rig",     cfg.get("rigs", {}),     rig,     rig_default),
            pick_setup_item("antenna", cfg.get("antennas", {}), antenna, ant_default))


def _tag_text(text: str) -> str:
    """Tag values must not contain brackets (they delimit the tags)."""
    return re.sub(r"\s+", " ", text.replace("[", "(").replace("]", ")")).strip()


def comment_with_setup(comment: str, rig: str, antenna: str) -> str:
    """
    Existing comment text (before the first tag) + "[antenna] … [rig] …".
    Old [antenna]/[rig] tags are replaced, so re-importing never duplicates them.
    """
    remark = re.split(r"\[(?:antenna|rig)\]", comment, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    parts  = [remark] if remark else []
    if antenna: parts.append(f"[antenna] {_tag_text(antenna)}")
    if rig:     parts.append(f"[rig] {_tag_text(rig)}")
    return " ".join(parts)


def set_adif_comment(record: str, rig: str, antenna: str) -> str:
    """Write the rig/antenna tags into the COMMENT field of one raw ADIF record."""
    if not (rig or antenna):
        return record
    m = re.search(r"<COMMENT:(\d+)(?::[^>]*)?>", record, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        old, end = record[m.end():m.end() + n], m.end() + n
        # Some loggers count bytes instead of characters; if the field does not
        # end where the char count says, take the byte count.
        if record[end:end + 1].strip() not in ("", "<") and not old.isascii():
            old = record[m.end():].encode("utf-8")[:n].decode("utf-8", errors="ignore")
            end = m.end() + len(old)
        head, tail, sep = record[:m.start()], record[end:], ""
    else:
        old, sep = "", " "
        e = re.search(r"\s*<eor>\s*$", record, re.IGNORECASE)   # insert before <eor>
        head, tail = (record[:e.start()], record[e.start():]) if e else (record, "")
    new = comment_with_setup(old, rig, antenna)
    # Wavelog (PHP) cuts the field by bytes, so the length is the UTF-8 byte count
    return f"{head}{sep}<COMMENT:{len(new.encode('utf-8'))}>{new}{tail}"


# ---------------------------------------------------------------------------
# QSO import
# ---------------------------------------------------------------------------

def read_raw_qso_strings(filepath: str) -> list:
    """
    Reads all QSO records from an ADIF file as raw ADIF strings.
    Each string is a complete QSO record (without <EOR>).
    Wavelog expects the ADIF string without a trailing <EOR>.
    """
    text = Path(filepath).read_text(encoding="utf-8", errors="replace")
    eoh  = re.search(r'<EOH>', text, re.IGNORECASE)
    if eoh:
        text = text[eoh.end():]
    records = []
    for record in re.split(r'<EOR>', text, flags=re.IGNORECASE):
        record = record.strip()
        if record:
            records.append(record + " <eor>")  # Wavelog expects <eor> at the end
    return records


def import_qsos_from_adif(cfg: dict, station_id: str, adif_path: str,
                           pota_refs: list = None,
                           station_locator: str = "",
                           setup: tuple = ("", "")) -> dict:
    """
    Imports QSOs from an ADIF file into a Wavelog station.

    pota_refs:        if given, only QSOs whose MY_POTA_REF contains at least
                      one of these refs are sent. This avoids locator-mismatch
                      errors when a single ADIF file contains multiple activations.
    station_locator:  if given, MY_GRIDSQUARE in each record is normalised to
                      this value before sending. This ensures GPS-drift and
                      home-fallback QSOs (which carry a different locator) are
                      accepted by Wavelog instead of being silently skipped.

    setup:            (rig, antenna) names; written into each QSO's COMMENT as
                      "[antenna] … [rig] …" (read by wavelog-qsl-convert).

    Returns {"imported": N, "dupes": N, "errors": N}.
    """
    records = read_raw_qso_strings(adif_path)
    if not records:
        print("  ✗ No QSOs found in ADIF file.")
        return {"imported": 0, "dupes": 0, "errors": 0}

    # ── Filter to QSOs belonging to this activation ────────────────────────────────────────
    if pota_refs:
        refs_upper = {r.upper() for r in pota_refs}
        filtered = []
        for record in records:
            raw_ref = parse_adif_field("MY_POTA_REF", record)
            qso_refs = {r.strip().upper()
                        for r in raw_ref.replace(";", ",").split(",") if r.strip()}
            if qso_refs & refs_upper:
                filtered.append(record)
        skipped = len(records) - len(filtered)
        records  = filtered
        if skipped:
            print(f"       ({skipped} QSOs from other activations filtered out)")

    # ── Normalise locator for GPS-drift / home-fallback QSOs ─────────────────
    if station_locator:
        normalised = []
        for record in records:
            existing = parse_adif_field("MY_GRIDSQUARE", record)
            if existing.upper() != station_locator.upper():
                record = re.sub(
                    r'<MY_GRIDSQUARE:\d+(?::[^>]*)?>[ \w]+',
                    f'<MY_GRIDSQUARE:{len(station_locator)}>{station_locator}',
                    record, flags=re.IGNORECASE,
                )
            normalised.append(record)
        records = normalised

    if any(setup):
        records = [set_adif_comment(r, *setup) for r in records]
        print(f"       Comment: {comment_with_setup('', *setup)}")

    print(f"\n  → QSO import: {len(records)} QSOs → station ID {station_id}")

    url = f"{cfg['url']}/index.php/api/qso"
    imported = dupes = errors = 0
    label = " + ".join(pota_refs) if pota_refs else f"station {station_id}"

    for i, record in enumerate(records, 1):
        ld.progress(i - 1, len(records), f"QSO import {label}: {imported} imported, {dupes} duplicates")
        payload = {
            "key":                cfg["api_key"],
            "station_profile_id": str(station_id),
            "type":               "adif",
            "string":             record,
        }
        status, response = http_post(url, payload)

        if status in (200, 201) and response.get("status") == "created":
            imported += 1
        elif status == 200 and response.get("status") == "dupe":
            dupes += 1
        elif status == 400 and response.get("status") == "abort":
            # Wavelog returns 400 when a QSO is a duplicate → check messages
            messages = " ".join(str(m) for m in response.get("messages", []))
            if "Duplicate" in messages or "duplicate" in messages:
                dupes += 1
            else:
                errors += 1
                if errors <= 3:
                    print(f"     ✗ QSO {i} error (HTTP {status}): {response}")
        else:
            errors += 1
            if errors <= 3:
                print(f"     ✗ QSO {i} error (HTTP {status}): {response}")

        # Progress every 10 QSOs
        if i % 10 == 0 or i == len(records):
            print(f"     {i}/{len(records)}  ✓ {imported} imported"
                  f"  ⟳ {dupes} duplicates  ✗ {errors} errors", end="\r")

    print()  # newline after progress display
    ld.progress(len(records), len(records), f"QSO import {label} done")
    ld.step(f"{label} · QSO import", "err" if errors else "ok", id=f"{label}:import",
            detail=f"{imported} imported, {dupes} duplicates, {errors} errors")
    return {"imported": imported, "dupes": dupes, "errors": errors}


def _dxcc_from_locator(locator: str) -> dict:
    """
    Derives DXCC data from the Maidenhead locator.

    Some logging apps (e.g. Polo) do not write MY_DXCC. The DXCC/COUNTRY
    field in ADIF belongs to the contacted station, not to our own station,
    so we cannot use it here.

    For now this returns empty values for all non-German locators and lets
    Wavelog resolve the DXCC from the callsign during import. German
    callsigns are handled in build_station_payload via prefix detection.
    """
    if not locator or len(locator) < 4:
        return {"dxcc": "", "cqz": "", "ituz": "", "country": ""}
    return {"dxcc": "", "cqz": "", "ituz": "", "country": ""}

# ---------------------------------------------------------------------------
# ADIF mode
# ---------------------------------------------------------------------------

def adif_mode(cfg, adif_path, manual_locator="",
              merge_distance_km=DEFAULT_MERGE_DISTANCE_KM,
              rig="", antenna=""):
    home = cfg.get("home_locator", "").upper()
    print()
    print("=" * 62)
    print(f"  ADIF import: {Path(adif_path).name}")
    print(f"  Merge distance: {merge_distance_km} km")
    if home:
        print(f"  Home QTH:       {home}  (GPS fallback detection active)")
    print("=" * 62)

    activations = extract_activations_from_adif(adif_path, merge_distance_km,
                                                home_locator=home)
    if not activations:
        ld.done(f"No POTA activations found in {Path(adif_path).name}.", "err")
        return
    ld.step(f"Read {Path(adif_path).name}", "ok", id="adif", detail=f"{len(activations)} activation(s)")
    ld.table([
        {"key": "n", "label": "#", "mono": True},
        {"key": "refs", "label": "POTA", "mono": True},
        {"key": "loc", "label": "Locator", "mono": True},
        {"key": "qsos", "label": "QSOs", "align": "right"},
        {"key": "date", "label": "Date", "mono": True},
        {"key": "note", "label": ""},
    ], [{
        "n": i, "refs": " + ".join(a["refs"]), "loc": a["locator"], "qsos": a["qso_count"],
        "date": a["date"],
        "note": ", ".join(x for x in (
            f"{len(a['refs'])}-fer" if len(a["refs"]) > 1 else "",
            "home activation" if a.get("home_activation") else "",
            f"merged from {', '.join(a['merged_from'])}" if a.get("merged_from") else "") if x),
    } for i, a in enumerate(activations, 1)], id="activations", title="Detected activations")

    print(f"\n  Detected activations: {len(activations)}")
    for i, act in enumerate(activations, 1):
        refs_str = " + ".join(act["refs"])
        nfer     = f"  ⚡ {len(act['refs'])}-fer" if len(act["refs"]) > 1 else ""
        home_tag = "  🏠 home activation" if act.get("home_activation") else ""
        merged   = (f"  [merged from: {', '.join(act['merged_from'])}]"
                    if act.get("merged_from") else "")
        print(f"\n  [{i}] {refs_str}{nfer}{home_tag}")
        print(f"       Locator: {act['locator']}  |  QSOs: {act['qso_count']}"
              f"  |  Date: {act['date']}{merged}")

    print()
    existing = get_existing_stations(cfg)
    print(f"  → {len(existing)} existing stations in Wavelog.")

    ok = skip = qsos_imported = qsos_dupes = 0
    setup = None   # last (rig, antenna) of this run — preselected for the next activation
    for i, act in enumerate(activations, 1):
        refs_str = " + ".join(act["refs"])
        print(f"\n{'─' * 62}")
        print(f"  Activation {i}/{len(activations)}: {refs_str}")
        print(f"{'─' * 62}")

        # Callsign from ADIF vs. config
        if act.get("callsign") and act["callsign"] != cfg["callsign"]:
            print(f"  ℹ  Callsign in ADIF: {act['callsign']} | Config: {cfg['callsign']}")
            if ld.active():
                choice = ld.choose(f"{refs_str}: the ADIF file was logged as {act['callsign']}, "
                                   f"the configuration says {cfg['callsign']}. Which callsign?",
                                   [act["callsign"], cfg["callsign"]], default=cfg["callsign"])
                choice = "" if choice == cfg["callsign"] else choice
            else:
                choice = input(f"  Which one to use? [{cfg['callsign']}]: ").strip().upper()
            if choice:
                cfg = dict(cfg); cfg["callsign"] = choice

        # Derive DXCC from locator — NOT from QSO records
        # (DXCC/COUNTRY in ADIF belongs to the contact, not to our own station)
        loc = manual_locator or act["locator"]
        adif_meta = _dxcc_from_locator(loc)

        station_id = process_parks(
            cfg=cfg,
            pota_refs=act["refs"],
            adif_locator=loc,
            adif_meta=adif_meta,
            existing_stations=existing,
            home_activation=act.get("home_activation", False),
        )
        if station_id is not None:
            ok += 1
            setup = choose_setup(cfg, rig, antenna, previous=setup)
            print(f"\n  [5/5] QSO import...")
            result = import_qsos_from_adif(cfg, station_id, adif_path,
                                            pota_refs=act["refs"],
                                            station_locator=act["locator"],
                                            setup=setup)
            print(f"  ✅ {result['imported']} QSOs imported"
                  f"  |  {result['dupes']} duplicates"
                  f"  |  {result['errors']} errors")
            qsos_imported += result["imported"]
            qsos_dupes    += result["dupes"]
        else:
            skip += 1

        if i < len(activations):
            if not _confirm(f"\n  Continue to next activation? [y/n] ",
                            f"Continue with activation {i + 1}/{len(activations)}?", default=True):
                print("  Aborted."); break

    print()
    print("=" * 62)
    print(f"  Stations: {ok} processed, {skip} skipped")
    print(f"  QSOs:     {qsos_imported} imported, {qsos_dupes} duplicates")
    print("=" * 62)
    ld.stat("Stations", ok, "ok" if ok else None)
    ld.stat("Skipped", skip, "warn" if skip else None)
    ld.stat("QSOs imported", qsos_imported, "ok" if qsos_imported else None)
    ld.stat("Duplicates", qsos_dupes)
    ld.done(f"{ok} station(s) processed, {skip} skipped – {qsos_imported} QSOs imported, "
            f"{qsos_dupes} duplicates.", "ok" if ok and not skip else "warn")

# ---------------------------------------------------------------------------
# Interactive mode
# ---------------------------------------------------------------------------

def interactive_mode(cfg):
    print()
    print("=" * 62)
    print("  Create Wavelog POTA Station Location")
    print("=" * 62)
    while True:
        print()
        entry = _text(
            "POTA ref(s) (e.g. 'DE-0034' or 'DE-0034 DE-1197' for N-fer)\n"
            "or 'q' to quit: ",
            "POTA reference(s), e.g. DE-0034 or DE-0034 DE-1197 for an N-fer – 'q' to quit"
        ) or ("q" if ld.active() else "")
        if entry.lower() in ("q", "quit", "exit"):
            print(f"73 de {cfg['callsign']}"); break
        refs = [r.strip().upper().replace("POTA ","")
                for r in entry.replace(",", " ").split() if r.strip()]
        if not refs: continue
        process_parks(cfg, refs)
        if not _confirm("\nCreate another station? [y/n] ", "Create another station?", default=True):
            print(f"73 de {cfg['callsign']}"); break

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Creates Wavelog station locations for POTA activations.",
        epilog=(
            "Examples:\n"
            "  %(prog)s DE-0034\n"
            "  %(prog)s DE-0034 DE-1197                  # N-fer\n"
            "  %(prog)s DE-0034 --locator JO40HJ\n"
            "  %(prog)s --adif activation.adi\n"
            "  %(prog)s --adif act.adi --merge-distance 5\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("refs", nargs="*", metavar="POTA-REF",
                   help="POTA reference(s)")
    p.add_argument("--adif",  metavar="FILE",
                   help="ADIF file to import")
    p.add_argument("--locator", metavar="LOCATOR", default="",
                   help="Maidenhead locator override (takes priority over ADIF and GPS)")
    p.add_argument("--merge-distance", metavar="KM", type=float,
                   default=DEFAULT_MERGE_DISTANCE_KM,
                   help=f"Maximum distance in km for GPS drift detection "
                        f"(default: {DEFAULT_MERGE_DISTANCE_KM})")
    p.add_argument("--rig", metavar="ID|NAME", default="",
                   help="Rig from [rigs] in ~/.wavelog.conf for the QSO comment "
                        "(default: ask, preselecting [setup] rig)")
    p.add_argument("--antenna", metavar="ID|NAME", default="",
                   help="Antenna from [antennas] in ~/.wavelog.conf for the QSO comment")
    p.add_argument("--version", action="version", version="pota2wavelog 2.0")

    args = p.parse_args()
    cfg  = ensure_config(load_config())
    cfg["main_callsign"] = cfg["callsign"]   # the call the main qrz_api_key belongs to

    for flag, kind, wanted in (("--rig", "rigs", args.rig), ("--antenna", "antennas", args.antenna)):
        if wanted and cfg[kind] and resolve_setup_item(cfg[kind], wanted) is None:
            known = ", ".join(cfg[kind])
            print(f"✗ {flag} {wanted}: not in [{kind}] of ~/.wavelog.conf (known: {known})")
            sys.exit(1)

    if args.adif:
        adif_path = Path(args.adif)
        if not adif_path.exists():
            print(f"✗ File not found: {adif_path}"); sys.exit(1)
        adif_mode(cfg, str(adif_path),
                  manual_locator=args.locator.upper(),
                  merge_distance_km=args.merge_distance,
                  rig=args.rig, antenna=args.antenna)
    elif args.refs:
        refs = [r.strip().upper().replace("POTA ","") for r in args.refs if r.strip()]
        sid = process_parks(cfg, refs, manual_locator=args.locator.upper())
        ld.done(f"{' + '.join(refs)}: station ID {sid}." if sid is not None
                else f"{' + '.join(refs)}: no station created.", "ok" if sid is not None else "warn")
    else:
        interactive_mode(cfg)


if __name__ == "__main__":
    main()
