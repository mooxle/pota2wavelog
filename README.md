# 📻 pota2wavelog

> Create Wavelog station locations for POTA activations and import QSOs — fully automated.

Active POTA activators quickly end up with dozens of station locations in Wavelog. This tool takes the manual work off your hands: it queries the POTA API for park data, determines the Maidenhead locator, fetches city and region via reverse geocoding, and creates the station location with a single confirmation step. In ADIF mode it also automatically detects all activations in the log file and imports the QSOs in the same step.

The approach follows the workflow described by [DB4SCW](https://db4scw.de) — **one station location per physical location**, for clean statistics and correct LoTW data for your QSO partners.

---

## 🚀 Quick Start

### 1. Dependencies

No external libraries required — Python 3.8+ (stdlib only) is all you need.

```bash
python3 --version   # 3.8 or newer
```

### 2. Configuration

On first run the script asks interactively for your credentials and optionally saves them to `~/.wavelog.conf`:

```
Wavelog URL (e.g. https://log.example.com): https://my-wavelog.net
Wavelog API key: abc123...
Station callsign (e.g. DA6MAX): DB4SCW
Your home QTH locator (e.g. JO40JG, Enter to skip): JN49OR
```

Alternatively use environment variables or edit `~/.wavelog.conf` directly (INI format):

```ini
[wavelog]
url          = https://my-wavelog.net
api_key      = abc123...
callsign     = DB4SCW
home_locator = JN49OR
qrz_api_key  = xyz789...
```

| Variable | Description |
|---|---|
| `WAVELOG_URL` | Base URL of your Wavelog instance |
| `WAVELOG_API_KEY` | Wavelog API key |
| `WAVELOG_CALLSIGN` | Your station callsign |
| `WAVELOG_HOME_LOCATOR` | Home QTH locator (for GPS fallback detection) |
| `WAVELOG_QRZ_API_KEY` | QRZ.com logbook API key (optional) |

### 3. Run

```bash
# Interactive mode — guided input
python3 pota2wavelog.py

# Direct CLI
python3 pota2wavelog.py DE-0034

# ADIF import with automatic activation detection
python3 pota2wavelog.py --adif activation.adi
```

---

## 🛠️ Modes

```
python3 pota2wavelog.py [POTA-REF ...] [options]
python3 pota2wavelog.py --adif FILE    [options]
```

| Mode | Invocation | What it does |
|---|---|---|
| **Interactive** | No arguments | Guided POTA ref entry, repeatable in a loop |
| **CLI** | `DE-0034` or `DE-0034 DE-1197` | Direct processing of one or more refs |
| **ADIF** | `--adif file.adi` | Auto-detect activations, create stations, import QSOs |

---

## 📡 CLI Mode

Pass a single activation or an N-fer directly on the command line. The script fetches park data, resolves the locator, and walks you through a single confirmation step.

### Arguments

| Argument | Description |
|---|---|
| `POTA-REF ...` | One or more POTA references (e.g. `DE-0034` or `DE-0034 DE-1197`) |
| `--locator LOCATOR` | Override locator manually — takes priority over ADIF and GPS |
| `--merge-distance KM` | Maximum distance for GPS drift detection in ADIF mode (default: 5 km) |
| `--version` | Show version and exit |

```bash
# Single park
python3 pota2wavelog.py DE-0034

# N-fer (two parks in one activation)
python3 pota2wavelog.py DE-0034 DE-1197

# With manual locator override
python3 pota2wavelog.py DE-0034 --locator JO40HJ
```

### Four-step workflow

Every run goes through the same four steps:

```
[1/4] Park data ...        → POTA API
[2/4] Locator ...          → --locator > ADIF > POTA GPS > manual input
[3/4] Location ...         → Reverse geocoding via Nominatim
[4/4] Duplicate check ...  → Compare against existing Wavelog stations
```

Afterwards a summary is shown, letting you adjust the name, locator, or city before the station is created.

### Confirmation step

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Planned station location:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Name:     POTA DE-0034 Taunus JO40HJ Bad Homburg
  Call:     DB4SCW
  Locator:  JO40HJ
  City:     Bad Homburg
  State:    HE
  POTA:     DE-0034
  DXCC:     FEDERAL REPUBLIC OF GERMANY (230)
  CQ/ITU:   14 / 28
  QRZ.com:  enter API key manually after creation ⚠️
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  [y] Create  [n] Cancel  [e] Name  [l] Locator  [c] City
  Choice:
```

---

## 🎯 ADIF Mode

ADIF mode is the centrepiece: it reads an `.adi` file, automatically detects all activations by POTA ref and GPS locator, creates the matching station location for each activation, and imports the QSOs in the same step.

```bash
# Full import in one go
python3 pota2wavelog.py --adif activation.adi

# With manually overridden locator
python3 pota2wavelog.py --adif activation.adi --locator JO40PL

# Adjust GPS drift tolerance (default: 5 km)
python3 pota2wavelog.py --adif activation.adi --merge-distance 3
```

### Works great with Polo

[Polo](https://polo.ham2k.com) is a popular POTA logging app for iOS and Android. Its **Full ADIF Export** (Settings → Export → Full ADIF) produces a file that pota2wavelog reads directly — park refs, GPS locators, and all QSO data are included out of the box. Simply export from Polo and pass the file to `--adif`.

### Activation detection

QSOs are grouped by locator and POTA ref. Two edge cases are handled automatically:

**GPS drift:** If two locators are within the merge distance (default 5 km) and share identical POTA refs, their QSOs are merged into a single activation — the locator with the most QSOs wins.

**Home QTH detection:** If Polo had no GPS fix and wrote your home locator instead, the script detects this automatically — provided `home_locator` is configured — and replaces it with the real GPS locator found elsewhere in the same file.

```
  ADIF import: activation.adi
  Merge distance: 5 km
  Home QTH:       JN49OR  (GPS fallback detection active)
  ══════════════════════════════════════════════════════════════

  Detected activations: 2

  [1] DE-0034
       Locator: JO40HJ  |  QSOs: 23  |  Date: 20250501

  [2] DE-1197 + DE-0028  ⚡ 2-fer
       Locator: JN49PU  |  QSOs: 15  |  Date: 20250501

  ──────────────────────────────────────────────────────────────
  Activation 1/2: DE-0034
  ──────────────────────────────────────────────────────────────

  [1/4] Park data ...
  [2/4] Locator ...
         ✓ From ADIF: JO40HJ
         Accept [JO40HJ] or enter new (Enter to accept):
  ...
  [5/5] QSO import...
  ✅ 23 QSOs imported  |  0 duplicates  |  0 errors

  Continue to next activation? [y/n]
```

### Import summary

```
  ══════════════════════════════════════════════════════════════
  Stations: 2 processed, 0 skipped
  QSOs:     38 imported, 0 duplicates
  ══════════════════════════════════════════════════════════════
```

---

## 📛 Station naming

Station locations are named following the scheme recommended by [DB4SCW](https://db4scw.de):

```
POTA DE-0034 Taunus JO40HJ Bad Homburg
POTA DE-0028 + DE-1197 Spessart JN49PU Wintersbach
```

This makes stations uniquely findable in dropdown lists and the Wavelog search even when you have a large number of locations. The name can be edited before creation.

---

## 🔧 How it works

### Locator resolution (priority order)

1. `--locator` flag — highest priority
2. `MY_GRIDSQUARE` from the ADIF file (≥ 6 characters used directly; 4-character locators trigger a prompt)
3. GPS coordinates from the POTA API → converted to Maidenhead
4. Manual input (fallback if everything else is unavailable)

### DXCC auto-fill

For German callsigns (prefixes DA–DR) DXCC entity, CQ zone, and ITU zone are set automatically. For all other callsigns Wavelog determines the values itself during import.

### Duplicate detection

Before creating a station the script compares the POTA refs and locator against all existing stations in Wavelog. If a match is found you can import directly into the existing station instead of creating a new one.

### QRZ.com note

The Wavelog API does not support passing a QRZ API key when creating a station via the API. The script displays a reminder — the key has to be entered once manually in the station setup dialog.

---

## 📡 Data sources & attribution

| Source | Used for | License |
|---|---|---|
| [POTA API](https://api.pota.app) | Park name, GPS coordinates | Public |
| [Nominatim / OSM](https://nominatim.openstreetmap.org) | Reverse geocoding (city, region) | [ODbL 1.0](https://opendatacommons.org/licenses/odbl/) |
| [Wavelog API](https://github.com/wavelog/wavelog) | Read stations, create stations, import QSOs | MIT |

Nominatim results contain OpenStreetMap data. This tool does not display that data publicly — it is used exclusively to populate the Wavelog station location fields.

---

## ⚠️ Background: why one station location per activation?

Wavelog stores location data not per QSO but centrally in the station location. That sounds like extra work — but it matters:

**For you:** Statistics like "activated locators" and "worked distances" show correct data. Award tracking works reliably.

**For your QSO partners:** LoTW confirmations carry your real locator. Someone who correctly logged you as `JO40HJ` will not have their data overwritten with your home locator `JN49OR` when your confirmation comes through.

This script implements exactly that workflow — and makes it fast enough that even 10 stations in a single day are no problem.

Further reading: [Station Locations in Wavelog and POTA](https://db4scw.de) by DB4SCW

---

## 🤖 Transparency

Idea and concept: **DA6MAX**. Code generated with the assistance of [Claude](https://claude.ai) by Anthropic.

---

73 de DA6MAX
