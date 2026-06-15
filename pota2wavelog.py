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

import urllib.request
import urllib.parse
import urllib.error

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
    }
    conf_path = Path.home() / ".wavelog.conf"
    if conf_path.exists():
        p = configparser.ConfigParser()
        p.read(conf_path)
        s = "wavelog"
        if p.has_section(s):
            cfg["url"]          = cfg["url"]          or p.get(s, "url",          fallback="")
            cfg["api_key"]      = cfg["api_key"]      or p.get(s, "api_key",      fallback="")
            cfg["callsign"]     = cfg["callsign"]     or p.get(s, "callsign",     fallback="")
            cfg["home_locator"] = cfg["home_locator"] or p.get(s, "home_locator", fallback="")
            cfg["qrz_api_key"]  = cfg["qrz_api_key"]  or p.get(s, "qrz_api_key",  fallback="")
    return cfg


def save_config(cfg: dict):
    conf_path = Path.home() / ".wavelog.conf"
    p = configparser.ConfigParser()
    if conf_path.exists():
        p.read(conf_path)
    if not p.has_section("wavelog"):
        p.add_section("wavelog")
    p.set("wavelog", "url",          cfg["url"])
    p.set("wavelog", "api_key",      cfg["api_key"])
    p.set("wavelog", "callsign",     cfg["callsign"])
    p.set("wavelog", "home_locator", cfg.get("home_locator", ""))
    p.set("wavelog", "qrz_api_key",  cfg.get("qrz_api_key",  ""))
    with open(conf_path, "w") as f:
        p.write(f)
    print(f"  ✓ Saved: {conf_path}")


def ensure_config(cfg: dict) -> dict:
    changed = False
    if not cfg["url"]:
        cfg["url"] = input("Wavelog URL (e.g. https://log.example.com): ").strip().rstrip("/")
        changed = True
    if not cfg["api_key"]:
        cfg["api_key"] = input("Wavelog API key: ").strip()
        changed = True
    if not cfg["callsign"]:
        cfg["callsign"] = input("Station callsign (e.g. DA6MAX): ").strip().upper()
        changed = True
    if not cfg["home_locator"]:
        hl = input("Your home QTH locator (e.g. JO40JG, Enter to skip): ").strip().upper()
        if hl:
            cfg["home_locator"] = hl
            changed = True
    if not cfg["qrz_api_key"]:
        qrz = input("QRZ.com logbook API key (Enter to skip): ").strip()
        if qrz:
            cfg["qrz_api_key"] = qrz
            changed = True
    if changed and input("Save to ~/.wavelog.conf? [y/N] ").strip().lower() == "y":
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


def http_post(url: str, payload, timeout: int = 10) -> tuple:
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
        flagged as a GPS failure. If a real GPS locator exists for the same
        activation in the same file, it is preferred automatically.

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
            # No other locator found → warn but continue
            print()
            print(f"  ⚠️  Home QTH locator detected: {home}")
            print(f"     All {home_count} QSOs were logged with home locator (no GPS fix).")
            print(f"     No alternative GPS locator found in this file.")
            print(f"     → Locator must be corrected manually (step 2/4).")

    if len(groups) == 1:
        g = list(groups.values())[0]
        return [{"refs": sorted(g["refs"]), "locator": g["locator"],
                 "date": g["date"], "callsign": g["callsign"],
                 "dxcc": g["dxcc"], "country": g["country"],
                 "cqz":  g["cqz"],  "ituz":    g["ituz"],
                 "qso_count": g["count"]}]

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
                answer = input(f"  │    Merge into one station? [y/n] ").strip().lower()
                if answer != "n":
                    union(loc_a, loc_b, winner)
                    print(f"  │    ✓ Merged → locator: {winner} ({max(count_a,count_b)} QSOs)")
                else:
                    print(f"  │    → Kept separate.")
            else:
                print(f"  │  → Different refs: possible N-fer")
                print(f"  │    [1] N-fer: one station with all refs")
                print(f"  │    [2] Separate stations")
                while True:
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
            }
        merged[root]["refs"].update(g["refs"])
        merged[root]["count"] += g["count"]
        merged[root]["locators_merged"].append(loc)
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
    print(f"  → Wavelog: creating station...")
    return http_post(url, [payload])

# ---------------------------------------------------------------------------
# Build station payload
# ---------------------------------------------------------------------------

def build_station_payload(cfg, pota_refs, park_data, locator, geo,
                           link_logbook=True, adif_meta=None) -> dict:
    refs_str   = " + ".join(pota_refs)
    park_name  = park_data.get("name", "")
    city       = geo.get("city", "")

    parts = [f"POTA {refs_str}"]
    if park_name: parts.append(park_name)
    parts.append(locator)
    if city:      parts.append(city)

    # Determine DXCC from callsign — more reliable than the ADIF DXCC field
    # (the ADIF field belongs to the contact, not to our own station).
    callsign = cfg.get("callsign", "").upper()
    # German callsigns: DA-DR prefix (including /P suffixes like LA/DA6MAX)
    base_call   = callsign.split("/")[0]  # LA/DA6MAX → DA6MAX
    call_prefix = base_call[:2]
    is_germany  = call_prefix in (
        "DA","DB","DC","DD","DE","DF","DG","DH","DI",
        "DJ","DK","DL","DM","DN","DO","DP","DQ","DR"
    )

    if is_germany:
        dxcc = dict(DXCC_DE)
    else:
        # Non-Germany: leave DXCC fields empty; Wavelog resolves from callsign
        # during import.
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
        # QRZ key: not supported by the create_station API
        # → shown as a reminder after creation
        "qrzrealtime":         "-1",
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
    print(f"  QRZ.com:  enter API key manually after creation ⚠️")
    print("━" * 62)
    print()
    while True:
        a = input("  [y] Create  [n] Cancel  [e] Name  [l] Locator  [c] City\n"
                  "  Choice: ").strip().lower()
        if a == "y":
            return payload
        elif a == "n":
            print("  Cancelled."); return None
        elif a == "e":
            val = input(f"  New name [{payload['station_profile_name']}]: ").strip()
            if val: payload["station_profile_name"] = val
        elif a == "l":
            val = input(f"  New locator [{payload['station_gridsquare']}]: ").strip().upper()
            if val: payload["station_gridsquare"] = val
        elif a == "c":
            val = input(f"  New city [{payload['station_city']}]: ").strip()
            if val: payload["station_city"] = val
        else:
            print("  Please enter y, n, e, l or c.")

# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_parks(cfg, pota_refs, manual_locator="", adif_locator="",
                  adif_meta=None, existing_stations=None) -> bool:
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

    # 2. Locator
    print(f"\n  [2/4] Locator...")
    if manual_locator:
        locator = manual_locator.upper()
        print(f"       ✓ Manual (--locator): {locator}")
    elif adif_locator and len(adif_locator) >= 6:
        locator = adif_locator.upper()
        print(f"       ✓ From ADIF: {locator}")
        override = input(f"       Accept [{locator}] or enter new (Enter to accept): ").strip().upper()
        if override: locator = override
    elif adif_locator and len(adif_locator) == 4:
        print(f"       ⚠  ADIF locator is only 4 characters: {adif_locator.upper()}")
        if pota_lat is not None:
            calc = latlon_to_maidenhead(pota_lat, pota_lon)
            print(f"       ℹ  Calculated from POTA GPS: {calc}")
            val = input(f"       Use [{calc}]: ").strip().upper()
            locator = val if val else calc
        else:
            val = input(f"       Enter manually [{adif_locator.upper()}]: ").strip().upper()
            locator = val if val else adif_locator.upper()
    elif pota_lat is not None:
        locator = latlon_to_maidenhead(pota_lat, pota_lon)
        print(f"       ✓ From POTA GPS: {locator}")
        override = input(f"       Accept [{locator}] or enter new (Enter to accept): ").strip().upper()
        if override: locator = override
    else:
        locator = input("       GPS not available. Enter locator manually: ").strip().upper()

    # 3. Reverse geocoding (locator → GPS → Nominatim)
    print(f"\n  [3/4] Location...")
    gc_lat, gc_lon = maidenhead_to_latlon(locator)
    if gc_lat is not None:
        geo = reverse_geocode(gc_lat, gc_lon)
        print(f"       ✓ {geo['city']}, {geo['state_name']} ({geo['state_code']})")
        override = input(f"       Accept [{geo['city']}] or enter new (Enter to accept): ").strip()
        if override: geo["city"] = override
    else:
        geo = {"city": "", "state_code": "", "state_name": "", "country": ""}
        geo["city"] = input("       Enter city manually: ").strip()

    # 4. Duplicate check
    print(f"\n  [4/4] Duplicate check...")
    if existing_stations is None:
        existing_stations = get_existing_stations(cfg)
    dupes = check_duplicate(existing_stations, pota_refs, cfg["callsign"],
                            new_locator=locator)
    existing_station_id = None
    if dupes:
        print(f"  ⚠️  Station already exists:")
        for d in dupes:
            print(f"     • [{d.get('station_id')}] {d.get('station_profile_name')}")
        answer = input("  Import QSOs into this station anyway? [y/n] ").strip().lower()
        if answer == "n":
            create_new = input("  Create a new station instead? [y/n] ").strip().lower()
            if create_new != "y":
                print("  Skipped."); return None
            print("  → Proceeding to create a new station.")
            # Fall through to payload building below
        else:
            # Use the ID of the first match (user confirmed)
            existing_station_id = dupes[0].get("station_id")
            print(f"  → Using station ID: {existing_station_id}")
    else:
        print("       ✓ No duplicates")

    # Station already known → return ID directly, skip creation
    if existing_station_id is not None:
        return existing_station_id

    # Build payload and confirm
    link    = input("\n  Link station to active logbook? [y/n] ").strip().lower()
    payload = build_station_payload(cfg, pota_refs, park_data, locator, geo,
                                    link_logbook=(link != "n"), adif_meta=adif_meta)
    confirmed = confirm_station(payload)
    if confirmed is None:
        return None

    print("\n  Creating station...")
    status, response = create_station(cfg, confirmed)
    if status == 201:
        # Look up station ID from station list (API does not return it directly)
        print(f"  ✅ Created: {confirmed['station_profile_name']}")
        if cfg.get("qrz_api_key"):
            print(f"  ℹ️  QRZ key must be entered manually:")
            print(f"     Wavelog → Edit station → QRZ.com → enter API key")
            print(f"     Set upload to 'Enabled' (not real-time)")
        updated = get_existing_stations(cfg)
        # Find the most recently created station matching the POTA ref
        for s in reversed(updated):
            name = s.get("station_profile_name", "")
            if any(ref in name for ref in pota_refs):
                sid = s.get("station_id")
                print(f"  → Station ID: {sid}")
                return sid
        print(f"  ⚠️  Could not determine station ID.")
        return None
    elif status == 200 and response.get("status") == "dupe":
        print(f"  ⚠️  Wavelog: already exists (internal check).")
        return None
    else:
        print(f"  ✗ Error (HTTP {status}): {response}")
        return None


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
                           station_locator: str = "") -> dict:
    """
    Imports QSOs from an ADIF file into a Wavelog station.

    pota_refs:        if given, only QSOs whose MY_POTA_REF contains at least
                      one of these refs are sent. This avoids locator-mismatch
                      errors when a single ADIF file contains multiple activations.
    station_locator:  if given, MY_GRIDSQUARE in each record is normalised to
                      this value before sending. This ensures GPS-drift and
                      home-fallback QSOs (which carry a different locator) are
                      accepted by Wavelog instead of being silently skipped.

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

    print(f"\n  → QSO import: {len(records)} QSOs → station ID {station_id}")

    url = f"{cfg['url']}/index.php/api/qso"
    imported = dupes = errors = 0

    for i, record in enumerate(records, 1):
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
              merge_distance_km=DEFAULT_MERGE_DISTANCE_KM):
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
        return

    print(f"\n  Detected activations: {len(activations)}")
    for i, act in enumerate(activations, 1):
        refs_str = " + ".join(act["refs"])
        nfer     = f"  ⚡ {len(act['refs'])}-fer" if len(act["refs"]) > 1 else ""
        merged   = (f"  [merged from: {', '.join(act['merged_from'])}]"
                    if act.get("merged_from") else "")
        print(f"\n  [{i}] {refs_str}{nfer}")
        print(f"       Locator: {act['locator']}  |  QSOs: {act['qso_count']}"
              f"  |  Date: {act['date']}{merged}")

    print()
    existing = get_existing_stations(cfg)
    print(f"  → {len(existing)} existing stations in Wavelog.")

    ok = skip = qsos_imported = qsos_dupes = 0
    for i, act in enumerate(activations, 1):
        refs_str = " + ".join(act["refs"])
        print(f"\n{'─' * 62}")
        print(f"  Activation {i}/{len(activations)}: {refs_str}")
        print(f"{'─' * 62}")

        # Callsign from ADIF vs. config
        if act.get("callsign") and act["callsign"] != cfg["callsign"]:
            print(f"  ℹ  Callsign in ADIF: {act['callsign']} | Config: {cfg['callsign']}")
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
        )
        if station_id is not None:
            ok += 1
            print(f"\n  [5/5] QSO import...")
            result = import_qsos_from_adif(cfg, station_id, adif_path,
                                            pota_refs=act["refs"],
                                            station_locator=act["locator"])
            print(f"  ✅ {result['imported']} QSOs imported"
                  f"  |  {result['dupes']} duplicates"
                  f"  |  {result['errors']} errors")
            qsos_imported += result["imported"]
            qsos_dupes    += result["dupes"]
        else:
            skip += 1

        if i < len(activations):
            if input(f"\n  Continue to next activation? [y/n] ").strip().lower() == "n":
                print("  Aborted."); break

    print()
    print("=" * 62)
    print(f"  Stations: {ok} processed, {skip} skipped")
    print(f"  QSOs:     {qsos_imported} imported, {qsos_dupes} duplicates")
    print("=" * 62)

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
        entry = input(
            "POTA ref(s) (e.g. 'DE-0034' or 'DE-0034 DE-1197' for N-fer)\n"
            "or 'q' to quit: "
        ).strip()
        if entry.lower() in ("q", "quit", "exit"):
            print("73 de DA6MAX"); break
        refs = [r.strip().upper().replace("POTA ","")
                for r in entry.replace(",", " ").split() if r.strip()]
        if not refs: continue
        process_parks(cfg, refs)
        if input("\nCreate another station? [y/n] ").strip().lower() == "n":
            print("73 de DA6MAX"); break

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
    p.add_argument("--version", action="version", version="pota2wavelog 2.0")

    args = p.parse_args()
    cfg  = ensure_config(load_config())

    if args.adif:
        adif_path = Path(args.adif)
        if not adif_path.exists():
            print(f"✗ File not found: {adif_path}"); sys.exit(1)
        adif_mode(cfg, str(adif_path),
                  manual_locator=args.locator.upper(),
                  merge_distance_km=args.merge_distance)
    elif args.refs:
        refs = [r.strip().upper().replace("POTA ","") for r in args.refs if r.strip()]
        process_parks(cfg, refs, manual_locator=args.locator.upper())
    else:
        interactive_mode(cfg)


if __name__ == "__main__":
    main()
