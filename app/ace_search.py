#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""AceStream Suche - kompakte lokale Suche/Favoriten-App fuer die EIGENE AceStream-Engine.

* Sucht ausschliesslich ueber die Such-API der lokalen Engine (127.0.0.1:6878), optional mit Kategorie-Filter
  (API-Parameter `category`) und EPG-Zeile ("Jetzt: ..." aus `show_epg=1`).
* Spielt ueber die bestehende Kette ab (player-ace.sh -> mpv):
    - Typ "infohash" (Suchtreffer): Standard ist `getstream?infohash=<hash>&pid=<uuid4>&format=json`
      (wie python-acestream/acestream-launcher). Die App gibt dem Player die `playback_url` und fragt alle 3 s
      die `stat_url` fuer die Live-Anzeige (Peers/Speed). Schlaegt das fehl oder ist "Live-Status" aus:
      alter Pfad (der Player holt .../ace/getstream?infohash=...&pid=... selbst, Redirect).
    - Typ "content_id" (manuell eingegeben / acestream://): ace-play <ID> (keine Stats).
  Der Name des Streams geht als ACE_TITLE in die Umgebung des Players (mpv-ace.sh/vlc-ace.sh -> Fenstertitel).
* Die App ruft NIE die `command_url` / `method=stop` auf (das beendet den Content fuer alle Clients);
  beim Beenden der App bzw. des Players passiert engineseitig nichts.
* Enthaelt KEINE vorbefuellte Sender-/Anbieterliste fuer AceStream. Favoriten und Favoriten-Kategorien legt allein
  der Nutzer an. Eigene Stichwort-Regeln pro Kategorie-Chip (settings.json "cat_keywords", Defaults nur generische
  Woerter) ergaenzen die API-Treffer um Kanaele, deren NAME ein Stichwort enthaelt (Badge "Regel"). Strenger
  Kategorie-Filter (Standard an, "strict_categories") zaehlt Kanaele nur unter ihrer Hauptkategorie; Ausschlusswoerter
  je Chip ("cat_exclude", Standard leer); Chips: Hauptreihe + "Mehr ▾" fuer seltene Kategorien.
* Tab "OER": offizielle, frei empfangbare Livestreams der oeffentlich-rechtlichen Sender (Zapp-API des
  MediathekView-Projekts, Cache oer-cache.json, eingebaute Fallback-Liste). Abgespielt wird direkt mit mpv-ace.sh,
  ohne AceStream. Das sind die einzigen HTTP-Ziele ausser der Engine.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# --------------------------------------------------------------------------
# Konfiguration / Pfade (Umgebungsvariablen nur fuer Tests/Mocks)
# --------------------------------------------------------------------------
HOME = Path.home()
DATA_DIR = Path(os.environ.get("ACE_SEARCH_DATA") or (HOME / ".local/share/ace-search"))
FAV_FILE = DATA_DIR / "favorites.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
HISTORY_FILE = DATA_DIR / "history.json"
PLAY_LOG = DATA_DIR / "last-play.log"
OER_CACHE = DATA_DIR / "oer-cache.json"

ENGINE_URL = (os.environ.get("ACE_SEARCH_ENGINE") or "http://127.0.0.1:6878").rstrip("/")
PLAY_CMD = os.environ.get("ACE_SEARCH_PLAY_CMD") or str(HOME / ".config/acestream-launcher/ace-play")
PLAYER = os.environ.get("ACE_SEARCH_PLAYER") or str(HOME / ".config/acestream-launcher/player-ace.sh")
# Offizielle Livestreams laufen direkt in mpv (NICHT ueber player-ace.sh/VLC: VLC liegt im VPN-Split-Tunnel,
# geoblockte Streams brauchen die deutsche IP, mpv geht darueber)
MPV_WRAPPER = os.environ.get("ACE_SEARCH_MPV") or str(HOME / ".config/acestream-launcher/mpv-ace.sh")
OER_API = os.environ.get("ACE_SEARCH_OER_API") or "https://api.zapp.mediathekview.de/v1/channelInfoList"
START_CMD = os.environ.get("ACE_SEARCH_START_CMD") or "kitty -e acestream-start"
STOP_CMD = os.environ.get("ACE_SEARCH_STOP_CMD") or "kitty -e acestream-stop"

HEX40 = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{40}(?![0-9a-fA-F])")
DEFAULT_INTERVAL_MIN = 5
MAX_RESULTS = 100
PAGE_SIZE = 50
MAX_FAVS_STATUS = 25
STATUS_POLL_S = 15
HISTORY_MAX = 15
RECENT_MAX = 10
MAX_CAT_NAME = 40
KIND_INFOHASH, KIND_CONTENT, KIND_URL = "infohash", "content_id", "url"
OER_MAX_AGE_S = 24 * 3600   # Senderliste hoechstens 1x pro 24 h neu laden
RULE_MAX_KEYWORDS = 6       # Stichwoerter pro Suche (Engine nicht fluten)
RULE_MAX_PAGES = 2          # Seiten pro Stichwort
MAX_KEYWORDS_PER_CAT = 20


def _env_float(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return float(default)


PLAY_OK_S = _env_float("ACE_SEARCH_OK_S", 3)       # laeuft der Player so lange -> "Laeuft"
STAT_POLL_S = _env_float("ACE_SEARCH_STAT_S", 3)   # Abstand der stat_url-Abfragen

# Such-API-Kategorien (belegt: docs.acestream.net/developers/knowledge-base/list-of-categories/ plus "kids",
# das die Engine 3.2.11 beim Test akzeptiert). Anzeige deutsch; unbekannte Werte erscheinen roh.
CATEGORY_LABELS = {
    "informational": "Nachrichten/Info", "sport": "Sport", "movies": "Filme", "documentaries": "Doku",
    "entertaining": "Unterhaltung", "kids": "Kinder", "music": "Musik", "regional": "Regional",
    "educational": "Bildung", "religion": "Religion", "ethnic": "Ethno", "fashion": "Mode",
    "teleshop": "Teleshop", "cyber_games": "E-Sport/Gaming", "tv": "TV", "other": "Sonstiges",
    "amateur": "Amateur", "webcam": "Webcam", "erotic_18_plus": "Erotik 18+", "other_18_plus": "18+ Sonstige",
}
# Chips: Hauptreihe (haeufig, mit Treffern) und "Mehr ▾" (selten). Die Analyse echter Engine-Daten (Engine 3.2.11) zeigte
# u. a. cyber_games leer und religion/teleshop/fashion nur mit 1-2 Kanaelen. Ohne 18+/Amateur/Webcam (API kennt sie).
CHIPS_MAIN = ["sport", "informational", "movies", "documentaries", "entertaining", "kids", "music"]
CHIPS_MORE = ["regional", "educational", "religion", "ethnic", "fashion", "teleshop", "cyber_games"]
CATEGORY_CHIPS = CHIPS_MAIN + CHIPS_MORE
CHIP_LABELS = {"informational": "Nachrichten"}  # kuerzere Beschriftung auf dem Chip (Zeilen-Tags: CATEGORY_LABELS)
CHIP_TIPS = {"cyber_games": "Kanäle, die das AceStream-Verzeichnis als Gaming/E-Sport einordnet (cyber_games)"}
UNCATEGORIZED = "Ohne Kategorie"
STRICT_TIP = ("Streng: Ein Kanal erscheint nur unter seiner Hauptkategorie (statt unter jeder seiner Kategorien). "
              "Aus: wie die API, Kanäle mit mehreren Kategorien erscheinen mehrfach.")


# --------------------------------------------------------------------------
# Hilfsfunktionen (ohne GUI)
# --------------------------------------------------------------------------
def parse_content_id(text):
    """40-stellige Hex-ID aus Roh-ID oder acestream://-Link (sonst None)."""
    if not text:
        return None
    t = text.strip().lower()
    if t.startswith("acestream://"):
        t = t[len("acestream://"):]
    m = re.match(r"[0-9a-f]{40}(?![0-9a-f])", t)
    if not m:
        return None
    rest = t[40:]
    if rest and rest[0] not in "/?#& \t":
        return None
    return m.group(0)


def parse_item_id(text):
    """Content-ID (40 Hex) / acestream://-Link ODER Kennung eines offiziellen Livestreams 'oer:<kanal-id>'."""
    t = (text or "").strip().lower()
    if OER_ID_RE.fullmatch(t):
        return t
    return parse_content_id(text)


def _http_json(url, timeout=6):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def engine_version(timeout=3):
    """Version der Engine oder None, wenn sie nicht laeuft (get_version)."""
    try:
        d = _http_json(ENGINE_URL + "/webui/api/service?method=get_version&format=json", timeout)
        return (d.get("result") or {}).get("version")
    except Exception:
        return None


def _get_token():
    d = _http_json(ENGINE_URL + "/server/api?api_version=3&method=get_api_access_token")
    tok = (d.get("result") or {}).get("token")
    if not tok:
        raise RuntimeError("Kein API-Token von der Engine erhalten")
    return tok


def _clean_list(v):
    """Liste kleingeschrieben, ohne Leerstrings/Nicht-Strings."""
    return [str(c).strip().lower() for c in (v or []) if isinstance(c, str) and c.strip()]


def cat_label(c):
    """Deutsche Anzeige einer API-Kategorie; unbekannte Werte roh."""
    return CATEGORY_LABELS.get(c, c)


DE_LANGS = {"deu", "ger", "de"}
EN_LANGS = {"eng", "en"}
DE_COUNTRIES = {"de", "at", "ch"}
LANG_2 = {"deu": "DE", "ger": "DE", "eng": "EN", "fra": "FR", "fre": "FR", "spa": "ES", "ita": "IT",
          "por": "PT", "nld": "NL", "dut": "NL", "pol": "PL", "rus": "RU", "tur": "TR", "ara": "AR",
          "zho": "ZH", "chi": "ZH", "jpn": "JA", "kor": "KO", "ces": "CS", "cze": "CS", "swe": "SV",
          "dan": "DA", "nor": "NO", "fin": "FI", "ell": "EL", "gre": "EL", "hun": "HU", "ron": "RO",
          "rum": "RO", "ukr": "UK", "hrv": "HR", "srp": "SR", "bul": "BG", "hin": "HI", "fas": "FA",
          "per": "FA", "heb": "HE", "slk": "SK", "slo": "SK", "slv": "SL"}
GROUP_NAMES = {0: "Deutsch", 1: "Englisch", 2: "Weitere", 3: "Deaktiviert"}


def lang_group(item):
    """0 = deutschsprachig, 1 = englisch, 2 = alle anderen.

    Deutsch: languages enthaelt deu/ger/de. Nur wenn languages fehlt/leer ist, entscheidet countries
    in {de, at, ch} (Fallback). Ein CH-Kanal mit languages=["fra"] ist also NICHT deutsch.
    """
    langs = set(_clean_list(item.get("languages")))
    if langs:
        if langs & DE_LANGS:
            return 0
        if langs & EN_LANGS:
            return 1
        return 2
    if set(_clean_list(item.get("countries"))) & DE_COUNTRIES:
        return 0
    return 2


def lang_badge(item):
    """Kurzes Badge (DE/AT/CH/EN/FR/...) oder None. Deutsch: AT/CH, wenn das Land so lautet, sonst DE."""
    langs = _clean_list(item.get("languages"))
    countries = _clean_list(item.get("countries"))
    if lang_group(item) == 0:
        for c in countries:
            if c in ("at", "ch"):
                return c.upper()
        return "DE"
    if langs:
        return LANG_2.get(langs[0], langs[0].upper()[:3])
    if countries:
        return countries[0].upper()[:3]
    return None


def sort_key(x):
    """Deaktivierte ganz ans Ende (nicht abspielbar), sonst Sprachgruppe, Verfuegbarkeit absteigend, Name."""
    return (x["disabled"], lang_group(x), -_avail_key(x["availability"]), x["name"].lower())


def sort_results(items):
    return sorted(items, key=sort_key)


def result_group(x):
    """Gruppenschluessel fuer die Trenner (3 = deaktiviert)."""
    return 3 if x["disabled"] else lang_group(x)


def group_counts(items):
    """{Gruppenschluessel: Anzahl} fuer die Header ("Deutsch · 7")."""
    out = {}
    for x in items:
        g = result_group(x)
        out[g] = out.get(g, 0) + 1
    return out


def _clean_epg(epg):
    """EPG-Liste der Such-API ([{start, stop, name, description}], Zeiten als UNIX-Strings) -> schlanke Liste."""
    out = []
    for e in epg if isinstance(epg, list) else []:
        if not isinstance(e, dict):
            continue
        try:
            start, stop = int(float(e.get("start"))), int(float(e.get("stop")))
        except (TypeError, ValueError):
            continue
        name = " ".join(str(e.get("name") or "").split())
        if name and stop > start:
            out.append({"name": name, "start": start, "stop": stop})
    return out


def epg_now(epg, now=None):
    """Laufende Sendung (name, stop) oder None."""
    now = time.time() if now is None else now
    for e in epg or []:
        if e["start"] <= now < e["stop"]:
            return e["name"], e["stop"]
    return None


def epg_line(epg, now=None, maxlen=60):
    """"Jetzt: <Sendung> · bis 21:45" (gekuerzt) oder None."""
    cur = epg_now(epg, now)
    if not cur:
        return None
    name, stop = cur
    if len(name) > maxlen:
        name = name[:maxlen - 1].rstrip() + "…"
    return "Jetzt: %s · bis %s" % (name, time.strftime("%H:%M", time.localtime(stop)))


def _norm_item(it, group_name=None, epg=None):
    ih = (it.get("infohash") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", ih):
        return None
    cats = [c for c in (it.get("categories") or []) if c]
    return {
        "id": ih,
        "type": KIND_INFOHASH,
        "name": (it.get("name") or group_name or ih[:8]).strip(),
        "availability": it.get("availability"),
        "bitrate": it.get("bitrate"),
        "disabled": bool(it.get("disabled")),
        "categories": cats,
        "countries": _clean_list(it.get("countries")),
        "languages": _clean_list(it.get("languages")),
        "epg": _clean_epg(epg),
    }


def _avail_key(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return -1.0


CAT_MAX_RESULTS = 400  # Obergrenze bei aktivem Kategorie-Chip (max. 8 Seiten a 50 Kanaele)


def has_category(item, category):
    """Traegt das Item die Kategorie? (Gross-/Kleinschreibung egal, Leerstrings ignoriert, Mehrfach-Kategorien ok)"""
    want = str(category or "").strip().lower()
    return bool(want) and want in [str(c).strip().lower() for c in (item.get("categories") or []) if str(c).strip()]


def search_engine(query, limit=None, max_pages=None, category=None, info=None, token=None):
    """Suche ueber die Such-API der lokalen Engine. Gibt flache, sortierte Trefferliste zurueck.

    Endpunkt: GET /server/api?api_version=3&method=search&token=T&query=Q[&category=C]&page=N&page_size=S
    (page ist 0-basiert; Engine 3.2.x liefert nach Kanaelen gruppiert: results[].items[], dazu je Gruppe
    optional `epg`). `category` ist ein API-Parameter (docs.acestream.net/developers/search/); mit leerem
    query listet die Engine die Kanaele der Kategorie. `token`: vorhandenen API-Token weiterverwenden
    (sonst wird ein frischer geholt).
    """
    if limit is None:
        limit = CAT_MAX_RESULTS if category else MAX_RESULTS
    if max_pages is None:
        max_pages = -(-CAT_MAX_RESULTS // PAGE_SIZE) if category else 3
    token = token or _get_token()
    out, seen = [], set()
    page = 0
    total = 0
    groups_loaded = 0
    while page < max_pages:  # alle Seiten holen, erst nach dem Sortieren kuerzen (Sprachprioritaet)
        params = {"api_version": 3, "method": "search", "token": token, "query": query,
                  "page": page, "page_size": PAGE_SIZE, "group_by_channels": 1, "show_epg": 1}
        if category:
            params["category"] = category
        d = _http_json(ENGINE_URL + "/server/api?" + urllib.parse.urlencode(params), timeout=20)
        res = d.get("result") or {}
        rows = res.get("results") or []
        groups_loaded += len(rows)
        for r in rows:
            items = r.get("items")
            if items is None:  # flaches Format (ohne Gruppen)
                items, gname = [r], None
            else:
                gname = r.get("name")
            for it in items:
                n = _norm_item(it, gname, r.get("epg"))
                # Clientseitiger Filter: bei aktivem Chip nur Items, die die Kategorie wirklich tragen
                # (die API filtert auf Gruppenebene; eine Gruppe kann Items anderer Kategorien enthalten)
                if n and category and not has_category(n, category):
                    continue
                if n and n["id"] not in seen:
                    seen.add(n["id"])
                    out.append(n)
        total = res.get("total") or 0
        page += 1
        if not rows or page * PAGE_SIZE >= total:
            break
        time.sleep(0.25)
    # Deaktivierte ans Ende, dann Deutsch > Englisch > Rest, je Gruppe Verfuegbarkeit absteigend, Name
    out = sort_results(out)
    if info is not None:
        info.update(total=total, groups=groups_loaded, cut=max(0, len(out) - limit))
    return out[:limit]


# --------------------------------------------------------------------------
# Eigene Stichwort-Regeln pro Kategorie (neutrales Nutzerwerkzeug, Defaults nur generische Woerter)
# --------------------------------------------------------------------------
DEFAULT_CAT_KEYWORDS = {
    "informational": ["news", "nachrichten"],
    "sport": ["sport"],
    "movies": ["cinema", "kino", "film", "movie"],
    "documentaries": ["doku", "documentary"],
    "kids": ["kids", "kinder"],
    "music": ["music", "musik"],
}


def default_keywords():
    """Frische Kopie der Standard-Stichwoerter fuer ALLE Chip-Kategorien ([] wo es keine Vorgabe gibt)."""
    return {c: list(DEFAULT_CAT_KEYWORDS.get(c, [])) for c in CATEGORY_CHIPS}


def parse_keywords(text):
    """"Sport, live; Doku" -> ["sport", "live", "doku"] (klein, ohne Duplikate/Leerstrings, min. 2 Zeichen)."""
    out = []
    for w in re.split(r"[,;\n]", text or ""):
        w = " ".join(w.split()).lower()[:40]
        if len(w) >= 2 and w not in out:
            out.append(w)
    return out[:MAX_KEYWORDS_PER_CAT]


def load_cat_keywords(settings):
    """Stichwoerter je Chip aus settings["cat_keywords"]; fehlt der Schluessel (alte settings.json) oder eine
    Kategorie darin, gelten die Standardwerte. Eine vom Nutzer geleerte Kategorie bleibt leer."""
    out = default_keywords()
    raw = settings.get("cat_keywords") if isinstance(settings, dict) else None
    if isinstance(raw, dict):
        for c in CATEGORY_CHIPS:
            v = raw.get(c)
            if isinstance(v, list):
                out[c] = parse_keywords(",".join(str(w) for w in v))
            elif isinstance(v, str):
                out[c] = parse_keywords(v)
    return out


def load_cat_exclude(settings):
    """Ausschlusswoerter je Chip aus settings["cat_exclude"]; fehlt der Schluessel (alte settings.json), ist alles leer."""
    out = {c: [] for c in CATEGORY_CHIPS}
    raw = settings.get("cat_exclude") if isinstance(settings, dict) else None
    if isinstance(raw, dict):
        for c in CATEGORY_CHIPS:
            v = raw.get(c)
            if isinstance(v, list):
                out[c] = parse_keywords(",".join(str(w) for w in v))
            elif isinstance(v, str):
                out[c] = parse_keywords(v)
    return out


def load_strict(settings):
    """settings["strict_categories"]: Standard AN (fehlt der Schluessel oder ist er kein bool)."""
    v = settings.get("strict_categories") if isinstance(settings, dict) else None
    return v if isinstance(v, bool) else True


def _word_pos(name, word, whole=False):
    """Position von `word` (klein) am Wortanfang im Namen, sonst -1. whole: auch am Wortende (ganzes Wort)."""
    if not word:
        return -1
    m = re.search(r"(?<!\w)" + re.escape(word) + (r"(?!\w)" if whole else ""), (name or "").lower())
    return m.start() if m else -1


def name_has_word(name, word):
    """Steht `word` (klein) am Wortanfang im Namen? ("sport" passt auf "Sport 1", nicht auf "Motorsport".)"""
    return _word_pos(name, word) >= 0


def name_matches_query(name, query):
    """Alle Woerter des Suchbegriffs muessen (als Teilstring, klein) im Namen vorkommen; leer = passt."""
    n = (name or "").lower()
    return all(t in n for t in (query or "").lower().split())


# --------------------------------------------------------------------------
# Strenger Kategorie-Filter, Ausschlusswoerter, Aufloesung doppelter Regeltreffer
# Grundlage ist eine Analyse echter Verzeichnisdaten (Engine 3.2.11, ~500 Kanaele in Filme/Doku/Unterhaltung/
# Nachrichten/Sport): ein Viertel der Kanaele traegt mehrere Genre-Kategorien (z. B. Filme+Bildung+Doku), die
# Reihenfolge der Liste ist NICHT stabil ("erste Kategorie = Hauptkategorie" haette fast alle Doku-Kanaele verloren).
# "Filme" ist die Sammelkategorie (Mehrheit aller Kanaele, tritt mit fast jeder anderen Kategorie zusammen auf).
# Darum: Hauptkategorie = einzige Genre-Kategorie, sonst die durch Stichwoerter im Namen gestuetzte, sonst die
# spezifischste nach MAIN_RANK (seltene/spezifische Kategorien vor allgemeinen, "movies" zuletzt).
# --------------------------------------------------------------------------
WEAK_CATS = {"tv", "regional", "other"}  # Zusatz-Etiketten (Verbreitung/Region), kein Genre
MAIN_RANK = ["religion", "teleshop", "fashion", "kids", "music", "sport", "documentaries", "informational",
             "educational", "entertaining", "movies"]
MAX_ALSO = 3  # "auch: …" in der Zeile zeigt hoechstens so viele weitere Kategorien


def _rank(c):
    return MAIN_RANK.index(c) if c in MAIN_RANK else MAIN_RANK.index("entertaining") + 0.5


def item_cats(item):
    """Kategorien des Items: klein, ohne Leerstrings/Dubletten, Reihenfolge der API."""
    out = []
    for c in (item.get("categories") or []):
        c = str(c).strip().lower()
        if c and c not in out:
            out.append(c)
    return out


def genre_cats(item):
    return [c for c in item_cats(item) if c not in WEAK_CATS]


def name_excluded(name, words):
    """Enthaelt der Name eines der Ausschlusswoerter (am Wortanfang)?"""
    return any(name_has_word(name, w) for w in (words or []))


def main_category(item, keywords=None, exclude=None):
    """Hauptkategorie (Genre) eines Items oder None, wenn es keine Genre-Kategorie traegt.
    Eine Genre-Kategorie -> diese. Mehrere -> die durch Stichwort-Treffer im Namen gestuetzte (bei Gleichstand die
    spezifischere nach MAIN_RANK), sonst die spezifischste. Per Ausschlusswort ausgeschlossene Kategorien scheiden aus."""
    name = item.get("name") or ""
    g = [c for c in genre_cats(item) if not name_excluded(name, (exclude or {}).get(c))]
    if not g:
        return None
    if len(g) == 1:
        return g[0]
    sup = [c for c in g if any(name_has_word(name, w) for w in (keywords or {}).get(c, []))]
    return min(sup or g, key=_rank)


def counts_for_category(item, cat, strict=True, keywords=None, exclude=None):
    """Zaehlt ein API-Treffer (Item traegt `cat`) unter dem Chip? Ausschlusswoerter immer; strict: nur wenn `cat`
    die Hauptkategorie ist. Zusatz-Etiketten (regional/tv/other) sind keine Genres und bleiben ungefiltert."""
    if not has_category(item, cat) or name_excluded(item.get("name"), (exclude or {}).get(cat)):
        return False
    if strict and cat not in WEAK_CATS:
        return main_category(item, keywords, exclude) == cat
    return True


def rule_winner(item, keywords, exclude=None):
    """Chip, unter dem ein per Stichwort getroffener Kanal erscheint (nie doppelt): die API-Hauptkategorie, wenn sie
    unter den Treffern ist; sonst der staerkste Namenstreffer (ganzes Wort vor Wortanfang, dann weiter vorn im
    Namen, dann spezifischere Kategorie). None, wenn kein Stichwort passt."""
    name = item.get("name") or ""
    cands = {}
    for c, ws in (keywords or {}).items():
        if name_excluded(name, (exclude or {}).get(c)):
            continue
        keys = [(0 if _word_pos(name, w, True) >= 0 else 1, _word_pos(name, w)) for w in ws if _word_pos(name, w) >= 0]
        if keys:
            cands[c] = min(keys)
    if not cands:
        return None
    gm = main_category(item, keywords, exclude)
    return gm if gm in cands else min(cands, key=lambda c: (cands[c], _rank(c)))


def rule_applies(item, cat, keywords, exclude=None, strict=True):
    """Darf der Namenstreffer unter `cat` erscheinen? strict: nur Kanaele ohne eigene Genre-Kategorie der API
    (die API-Einordnung geht vor), und immer nur unter dem staerksten Chip (rule_winner)."""
    if strict and main_category(item, keywords, exclude) is not None:
        return False
    return rule_winner(item, keywords, exclude) == cat


def filter_category_results(items, cat, strict=True, keywords=None, exclude=None):
    """Wendet strict/Ausschluss auf Kategorie-Treffer an: (behalten, Anzahl ausgeblendet). Regel-Treffer bleiben."""
    kept = [x for x in items if x.get("rule_match") or counts_for_category(x, cat, strict, keywords, exclude)]
    return kept, len(items) - len(kept)


def also_categories(item, ref=None, keywords=None):
    """Weitere Kategorien des Items (ohne 'tv' und die Referenz) als Anzeigenamen fuer "auch: …".
    ref: aktiver Chip, sonst die Hauptkategorie."""
    cs = [c for c in item_cats(item) if c != "tv"]
    if ref is None:
        ref = main_category(item, keywords) or (cs[0] if cs else None)
    return [cat_label(c) for c in cs if c != ref][:MAX_ALSO]


def empty_hint(cat, query="", hidden=0, strict=True):
    """Erklaerender Text, wenn ein Chip (mit/ohne Suchbegriff) nichts liefert."""
    if not cat:
        return "Keine Treffer für „%s“." % query
    lab = cat_label(cat)
    if query:
        base = "Keine Treffer für „%s“ in %s." % (query, lab)
    elif cat == "cyber_games":
        base = ("Hier ist gerade nichts: Das AceStream-Verzeichnis führt unter E-Sport/Gaming (cyber_games) "
                "aktuell keine Kanäle. Sportübertragungen stehen unter „Sport“.")
    else:
        base = "Das AceStream-Verzeichnis führt unter %s gerade keine Kanäle." % lab
    if hidden:
        base += (" %d Kanäle mit anderer Hauptkategorie sind ausgeblendet – „Streng“ aus zeigt sie." % hidden
                 if strict else " %d Kanäle sind per Ausschlusswort ausgeblendet." % hidden)
    elif not query and cat != "cyber_games":
        base += " Eigene Stichwörter (Menü → Kategorie-Regeln) können Lücken füllen."
    return base


def search_with_rules(query, category, keywords, info=None, strict=False, all_keywords=None, exclude=None):
    """Kategorie-Suche PLUS Stichwort-Regeln in einem Durchlauf (ein Worker, ein Ergebnis).

    1. API-Suche mit category=C (wie bisher, search_engine).
    2. Je Stichwort (max. RULE_MAX_KEYWORDS) eine Suche OHNE category mit query=<stichwort>, max. RULE_MAX_PAGES
       Seiten; uebernommen werden nur Treffer, deren NAME das Stichwort am Wortanfang enthaelt und die zusaetzlich
       zum eingegebenen Suchbegriff passen (Namensfilter). Nicht-Treffer der Kategorie-Suche werden so nie
       verdraengt: Dubletten (Infohash) behalten den Kategorie-Treffer.
    3. Merge, Dedupe, bisherige Sortierung. Nur per Regel hinzugekommene Treffer tragen rule_match=True.
    strict: strenger Filter (nur Hauptkategorie; Regeltreffer nur fuer Kanaele ohne Genre-Kategorie der API).
    all_keywords: Stichwoerter ALLER Chips (Aufloesung doppelter Treffer, rule_winner); sonst nur `category`.
    exclude: Ausschlusswoerter je Chip (gelten fuer API- wie Regeltreffer).
    info (optional): total/groups/cut der Kategorie-Suche plus rules_active, cat_n, rule_n, kw_used, kw_extra, rule_err,
    hidden (durch strict/Ausschluss ausgeblendete API-Treffer).
    """
    kws = [k for k in (keywords or []) if k][:]
    used = kws[:RULE_MAX_KEYWORDS]
    allkw = dict(all_keywords) if all_keywords else {}
    allkw[category] = kws
    token = _get_token()
    cat_info = {}
    raw = search_engine(query, category=category, info=cat_info, token=token)
    seen = {x["id"] for x in raw}
    cat_res, hidden = filter_category_results(raw, category, strict, allkw, exclude)
    extra, rule_err = [], ""
    for kw in used:
        try:
            res = search_engine(kw, limit=10 ** 6, max_pages=RULE_MAX_PAGES, token=token)
        except Exception as e:  # Regel-Suche ist nur Zugabe: die Kategorie-Treffer bleiben
            rule_err = str(getattr(e, "reason", e))
            break
        for x in res:
            if x["id"] in seen or not name_has_word(x["name"], kw) or not name_matches_query(x["name"], query):
                continue
            if not rule_applies(x, category, allkw, exclude, strict):
                continue
            seen.add(x["id"])
            x["rule_match"] = True
            extra.append(x)
        time.sleep(0.25)
    merged = sort_results(cat_res + extra)
    cut = max(0, len(merged) - CAT_MAX_RESULTS)
    merged = merged[:CAT_MAX_RESULTS]
    if info is not None:
        info.update(cat_info)
        info.update(rules_active=bool(used), cat_n=len(cat_res), rule_n=sum(1 for x in merged if x.get("rule_match")),
                    kw_used=len(used), kw_extra=max(0, len(kws) - len(used)), rule_err=rule_err,
                    cut=max(cat_info.get("cut", 0), cut), hidden=hidden)
    return merged


# --------------------------------------------------------------------------
# Oeffentlich-rechtliche Livestreams (offizielle Sender-CDNs, direkt in mpv, ohne AceStream)
# Quelle: Zapp-API des MediathekView-Projekts, GET <OER_API> -> {"<kanal-id>": {"name", "streamUrl"}, ...}.
# Reihenfolge/Gruppen unten; unbekannte neue Kanal-IDs der Quelle landen in "Weitere".
# --------------------------------------------------------------------------
OER_GROUPS = {"national": "Bundesweit", "dritte": "Dritte Programme", "international": "International",
              "weitere": "Weitere"}
OER_ORDER = [  # (kanal-id, gruppe) in Anzeigereihenfolge
    ("das_erste", "national"), ("zdf", "national"), ("dreisat", "national"), ("arte", "national"),
    ("phoenix", "national"), ("tagesschau24", "national"), ("zdf_info", "national"), ("zdf_neo", "national"),
    ("one", "national"), ("kika", "national"), ("ard_alpha", "national"),
    ("dw_english", "international"),
    ("br_sued", "dritte"), ("br_nord", "dritte"), ("hr", "dritte"), ("mdr_sachsen", "dritte"),
    ("mdr_sachsen_anhalt", "dritte"), ("mdr_thueringen", "dritte"), ("ndr_hh", "dritte"), ("ndr_mv", "dritte"),
    ("ndr_nds", "dritte"), ("ndr_sh", "dritte"), ("rbb_berlin", "dritte"), ("rbb_brandenburg", "dritte"),
    ("rb", "dritte"), ("sr", "dritte"), ("swr_bw", "dritte"), ("swr_rp", "dritte"), ("wdr", "dritte"),
]
OER_SKIP = {"parlamentsfernsehen_1", "parlamentsfernsehen_2"}  # kein Vollprogramm; Stream war bei der Pruefung 404
OER_NAME_FIX = {"NRD Niedersachsen": "NDR Niedersachsen"}      # Tippfehler in der Quelle
OER_KEY_RE = re.compile(r"[a-z0-9_]{1,40}")
OER_ID_RE = re.compile(r"oer:[a-z0-9_]{1,40}")
# Zusaetzliche offizielle Streams, die die Quelle nicht fuehrt (nur wenn sie dort fehlen).
# DW English: Livestream-Seite der Deutschen Welle (dw.com/de/live-tv/channel-english).
OER_EXTRA = [("dw_english", "DW (English)",
              "https://dwamdstream102.akamaized.net/hls/live/2015525/dwstream102/master.m3u8")]
# Eingebaute Fallback-Liste (offizielle HLS-Master-Playlists, Stand 2026-10-28 per HTTP-GET verifiziert: 200 + #EXTM3U)
OER_FALLBACK = [
    ("das_erste", "Das Erste", "https://daserste-live.ard-mcdn.de/daserste/live/hls/de/master.m3u8"),
    ("zdf", "ZDF", "https://zdf-hls-15.akamaized.net/hls/live/2016498/de/high/master.m3u8"),
    ("dreisat", "3sat", "https://zdf-hls-18.akamaized.net/hls/live/2016501/dach/high/master.m3u8"),
    ("arte", "ARTE", "https://artesimulcast.akamaized.net/hls/live/2030993/artelive_de/index.m3u8"),
    ("phoenix", "phoenix", "https://zdf-hls-19.akamaized.net/hls/live/2016502/de/high/master.m3u8"),
    ("tagesschau24", "tagesschau24",
     "https://tagesschau.akamaized.net/hls/live/2020117/tagesschau/tagesschau_3/master.m3u8"),
    ("zdf_info", "ZDFinfo", "https://zdf-hls-17.akamaized.net/hls/live/2016500/de/high/master.m3u8"),
    ("zdf_neo", "ZDFneo", "https://zdf-hls-16.akamaized.net/hls/live/2016499/de/high/master.m3u8"),
    ("one", "ONE", "https://mcdn-one.ard.de/ardone/hls/master.m3u8"),
    ("kika", "KiKA", "https://kikageohls.akamaized.net/hls/live/2022693/livetvkika_de/master.m3u8"),
    ("ard_alpha", "ARD-alpha", "https://mcdn.br.de/br/fs/ard_alpha/hls/de/master.m3u8"),
    ("br_sued", "BR Süd", "https://mcdn.br.de/br/fs/bfs_sued/hls/de/master.m3u8"),
    ("br_nord", "BR Nord", "https://mcdn.br.de/br/fs/bfs_nord/hls/de/master.m3u8"),
    ("hr", "HR", "https://hr-live.ard-mcdn.de/hr/live/hls/de/master.m3u8"),
    ("mdr_sachsen", "MDR Sachsen", "https://mdrtvsnhls.akamaized.net/hls/live/2016928/mdrtvsn/master.m3u8"),
    ("mdr_sachsen_anhalt", "MDR Sachsen-Anhalt",
     "https://mdrtvsahls.akamaized.net/hls/live/2016879/mdrtvsa/master.m3u8"),
    ("mdr_thueringen", "MDR Thüringen", "https://mdrtvthhls.akamaized.net/hls/live/2016880/mdrtvth/master.m3u8"),
    ("ndr_hh", "NDR Hamburg", "https://mcdn.ndr.de/ndr/hls/ndr_fs/ndr_hh/master.m3u8"),
    ("ndr_mv", "NDR Mecklenburg-Vorpommern", "https://mcdn.ndr.de/ndr/hls/ndr_fs/ndr_mv/master.m3u8"),
    ("ndr_nds", "NDR Niedersachsen", "https://mcdn.ndr.de/ndr/hls/ndr_fs/ndr_nds/master.m3u8"),
    ("ndr_sh", "NDR Schleswig-Holstein", "https://mcdn.ndr.de/ndr/hls/ndr_fs/ndr_sh/master.m3u8"),
    ("rbb_berlin", "rbb Fernsehen Berlin",
     "https://rbb-hls-berlin.akamaized.net/hls/live/2017824/rbb_berlin/index.m3u8"),
    ("rbb_brandenburg", "rbb Fernsehen Brandenburg",
     "https://rbb-hls-brandenburg.akamaized.net/hls/live/2017825/rbb_brandenburg/index.m3u8"),
    ("rb", "Radio Bremen", "https://rbhlslive.akamaized.net/hls/live/2020435/rbfs/master.m3u8"),
    ("sr", "SR", "https://srfs.akamaized.net/hls/live/689649/srfsgeo/master.m3u8"),
    ("swr_bw", "SWR Baden-Württemberg", "https://swrbwd-hls.akamaized.net/hls/live/2018672/swrbwd/master.m3u8"),
    ("swr_rp", "SWR Rheinland-Pfalz", "https://swrrpd-hls.akamaized.net/hls/live/2018676/swrrpd/master.m3u8"),
    ("wdr", "WDR", "https://wdr-live.ard-mcdn.de/wdr/live/hls/de/master.m3u8"),
] + OER_EXTRA
_OER_RANK = {k: (i, g) for i, (k, g) in enumerate(OER_ORDER)}


def _oer_channel(key, name, url):
    """Ein Kanal-Dict {"id": "oer:<key>", "name", "url", "group", "type"} oder None bei ungueltigen Daten."""
    key = str(key or "").strip().lower()
    name = " ".join(str(name or "").replace("­", "").split())
    if not OER_KEY_RE.fullmatch(key) or key in OER_SKIP or not name or not isinstance(url, str):
        return None
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        return None
    return {"id": "oer:" + key, "type": KIND_URL, "name": OER_NAME_FIX.get(name, name), "url": url,
            "group": _OER_RANK.get(key, (0, "weitere"))[1]}


def _oer_sorted(chs):
    return sorted(chs, key=lambda c: (_OER_RANK.get(c["id"][4:], (10 ** 6, ""))[0], c["name"].lower()))


def parse_oer(data):
    """Antwort der Zapp-API -> sortierte Kanalliste (inkl. OER_EXTRA, falls die Quelle sie nicht fuehrt).
    Ungueltige Eintraege werden uebersprungen; weniger als 5 gueltige Kanaele gelten als Fehler (ValueError)."""
    if not isinstance(data, dict):
        raise ValueError("unerwartetes Format (kein JSON-Objekt)")
    chs = []
    for key, v in data.items():
        if isinstance(v, dict):
            c = _oer_channel(key, v.get("name"), v.get("streamUrl"))
            if c:
                chs.append(c)
    if len(chs) < 5:
        raise ValueError("zu wenige gültige Sender in der Antwort (%d)" % len(chs))
    have = {c["id"] for c in chs}
    for key, name, url in OER_EXTRA:
        if "oer:" + key not in have:
            chs.append(_oer_channel(key, name, url))
    return _oer_sorted(chs)


def oer_fallback():
    return _oer_sorted([c for c in (_oer_channel(k, n, u) for k, n, u in OER_FALLBACK) if c])


def oer_load_cache():
    """(Kanalliste, Abrufzeit) aus oer-cache.json oder (None, 0)."""
    d = load_json(OER_CACHE, None)
    if not isinstance(d, dict) or not isinstance(d.get("channels"), list):
        return None, 0.0
    chs = []
    for c in d["channels"]:
        if isinstance(c, dict) and str(c.get("id", "")).startswith("oer:"):
            cc = _oer_channel(str(c["id"])[4:], c.get("name"), c.get("url"))
            if cc:
                chs.append(cc)
    try:
        ts = float(d.get("fetched") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    return (_oer_sorted(chs), ts) if chs else (None, 0.0)


OER_STATE = {"channels": None, "source": "fallback", "fetched": 0.0, "error": ""}


def oer_load():
    """Ohne Netz: Cache (egal wie alt), sonst eingebaute Liste. Setzt OER_STATE."""
    chs, ts = oer_load_cache()
    if chs:
        OER_STATE.update(channels=chs, source="cache", fetched=ts)
    else:
        OER_STATE.update(channels=oer_fallback(), source="fallback", fetched=0.0)
    return OER_STATE["channels"]


def oer_channels():
    return OER_STATE["channels"] if OER_STATE["channels"] is not None else oer_load()


def oer_update(force=False, now=None):
    """Blockierend (Netz): laedt die Liste neu, wenn Cache fehlt/aelter als OER_MAX_AGE_S (oder force).
    Bei Fehler bleibt der letzte Cache bzw. die Fallback-Liste (OER_STATE["error"] nennt den Grund).
    Gibt True zurueck, wenn frisch aus der Quelle geladen wurde."""
    now = time.time() if now is None else now
    oer_channels()
    if not force and OER_STATE["source"] in ("cache", "live") and now - OER_STATE["fetched"] < OER_MAX_AGE_S:
        return False
    try:
        req = urllib.request.Request(OER_API, headers={"Accept": "application/json", "User-Agent": "ace-search"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read(2_000_000).decode("utf-8", "replace"))
        chs = parse_oer(data)
    except Exception as e:
        OER_STATE["error"] = ("HTTP %d" % e.code if isinstance(e, urllib.error.HTTPError)
                              else str(getattr(e, "reason", e)))[:120]
        return False
    try:
        save_json(OER_CACHE, {"version": 1, "fetched": now, "source": OER_API, "channels": chs})
    except Exception:
        pass
    OER_STATE.update(channels=chs, source="live", fetched=now, error="")
    return True


def oer_resolve(oid):
    """Stream-URL zu 'oer:<kanal-id>' ueber die AKTUELLE Liste (URL-Aenderungen der Quelle brechen Favoriten nicht)."""
    for c in oer_channels():
        if c["id"] == oid:
            return c["url"]
    return None


def oer_source_text():
    src, ts = OER_STATE["source"], OER_STATE["fetched"]
    if src == "live":
        t = "Quelle: Zapp-API (MediathekView) · geladen " + fmt_when(ts)
    elif src == "cache":
        t = "Quelle: Zwischenspeicher vom " + fmt_when(ts)
    else:
        t = "Quelle: eingebaute Liste"
    if OER_STATE["error"]:
        t += " · Quelle nicht erreichbar (%s)" % OER_STATE["error"]
    return t


def avail_level(v):
    """Ampel: 'hi' (>=0.8), 'mid' (>=0.3), 'lo' (sonst), None (unbekannt)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f >= 0.8:
        return "hi"
    if f >= 0.3:
        return "mid"
    return "lo"


def fmt_bitrate(v):
    try:
        b = float(v)
    except (TypeError, ValueError):
        return None
    if b <= 0:
        return None
    if b >= 1_000_000:
        return "%.1f Mbit/s" % (b / 1_000_000)
    return "%d kbit/s" % round(b / 1000)


def fmt_speed(kbs):
    """speed_down der stat_url (KB/s) -> "850 KB/s" bzw. "1,1 MB/s"."""
    try:
        v = float(kbs)
    except (TypeError, ValueError):
        return None
    if v >= 1000:
        return ("%.1f MB/s" % (v / 1024)).replace(".", ",")
    return "%d KB/s" % round(v)


def fmt_live(name, st):
    """Statusleiste: "▶ <Name> · Peers 12 · ↓ 1,1 MB/s" (st = Antwort der stat_url oder None)."""
    parts = ["▶ " + name]
    if isinstance(st, dict):
        if st.get("status") == "prebuf":
            parts.append("puffert")
        try:
            parts.append("Peers %d" % int(st["peers"]))
        except (KeyError, TypeError, ValueError):
            pass
        sp = fmt_speed(st.get("speed_down")) if st.get("speed_down") is not None else None
        if sp:
            parts.append("↓ " + sp)
    return " · ".join(parts)


def fmt_when(ts, now=None):
    """Zeitpunkt fuer den Verlauf: "heute 21:45", "gestern 21:45", sonst "27.10. 21:45"."""
    now = time.time() if now is None else now
    try:
        t = time.localtime(float(ts))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    today = time.localtime(now)
    yest = time.localtime(now - 86400)
    hm = time.strftime("%H:%M", t)
    if t[:3] == today[:3]:
        return "heute " + hm
    if t[:3] == yest[:3]:
        return "gestern " + hm
    return time.strftime("%d.%m. ", t) + hm


def link_for(rec):
    """Kopier-Text: url (oer:) -> Stream-URL; content_id -> acestream://<id>; Infohash -> "infohash:<h>" (acestream://<infohash> wird von
    anderen Tools wie eine Content-ID behandelt und liefe ins Leere)."""
    if rec.get("type") == KIND_INFOHASH:
        return "infohash:" + rec["id"]
    if rec.get("type") == KIND_URL:  # offizieller Livestream: die aktuelle Stream-URL
        return oer_resolve(rec["id"]) or rec["id"]
    return "acestream://" + rec["id"]


def _engine_local(url):
    """Pfad+Query einer von der Engine genannten URL auf ENGINE_URL umgesetzt (HTTP-Ziel ist immer nur die
    eigene Engine, egal welchen Host die Antwort nennt). Nicht-http(s) -> None."""
    if not url or not isinstance(url, str):
        return None
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.path:
        return None
    return ENGINE_URL + p.path + ("?" + p.query if p.query else "")


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def load_json_safe(path, default):
    """Wie load_json; eine vorhandene, aber unlesbare Datei wird vorher als *.corrupt-<zeit> gesichert,
    damit der naechste Speichervorgang sie nicht ueberschreibt."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception:
        try:
            shutil.copy2(path, str(path) + ".corrupt-%d" % int(time.time()))
        except Exception:
            pass
        return default


def save_json(path, data):
    os.makedirs(path.parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _norm_kind(kind, api_name="", cid=""):
    """Gueltiger Typ; ohne Angabe: mit api_name (aus der Suche) -> infohash, sonst content_id.
    'oer:<id>' ist immer Typ url (offizieller Livestream); Typ url ohne oer-Kennung gibt es nicht."""
    if str(cid).startswith("oer:"):
        return KIND_URL
    if kind in (KIND_INFOHASH, KIND_CONTENT):
        return kind
    return KIND_INFOHASH if api_name else KIND_CONTENT


class Favorites:
    """Favoriten + eigene Favoriten-Kategorien.

    Datei favorites.json (Format 2): {"version": 2, "categories": [{"id", "name"}, ...],
    "favorites": [{"name", "id", "api_name", "type", "category"}, ...]}. Die Reihenfolge von `categories`
    ist die Anzeigereihenfolge. category = Kategorie-ID oder "" (Ohne Kategorie).
    Migration: Format 1 (reine Liste bzw. {"favorites": [...]} ohne Kategorien) wird verlustfrei geladen
    (category ""), Format 2 erst beim naechsten Speichern geschrieben. Fehlt `type`, gilt: mit api_name
    (aus der Suche) -> infohash, sonst content_id. Unbekannte Zusatzfelder bleiben erhalten.
    """

    def __init__(self, path=None):
        self.path = path or FAV_FILE
        self.items = []
        self.categories = []
        self.load()

    def load(self):
        d = load_json_safe(self.path, [])
        if isinstance(d, dict):
            items, cats = d.get("favorites", []), d.get("categories", [])
        else:
            items, cats = d, []
        self.categories, seen = [], set()
        for c in cats if isinstance(cats, list) else []:
            if isinstance(c, str):
                c = {"name": c}
            if not isinstance(c, dict):
                continue
            name = " ".join(str(c.get("name") or "").split())[:MAX_CAT_NAME]
            cid = str(c.get("id") or "")
            if not name or cid in seen:
                continue
            if not cid:
                cid = uuid.uuid4().hex[:8]
            seen.add(cid)
            self.categories.append({"id": cid, "name": name})
        self.items = []
        for e in items if isinstance(items, list) else []:
            cid = parse_item_id(str(e.get("id", ""))) if isinstance(e, dict) else None
            if cid:
                rec = dict(e)
                api_name = str(e.get("api_name") or "")
                rec.update({"name": str(e.get("name") or cid[:8]), "id": cid, "api_name": api_name,
                            "type": _norm_kind(e.get("type"), api_name, cid),
                            "category": e.get("category") if e.get("category") in seen else ""})
                self.items.append(rec)

    def save(self):
        save_json(self.path, {"version": 2, "categories": self.categories, "favorites": self.items})

    # --- Favoriten ---
    def has(self, cid):
        return any(f["id"] == cid for f in self.items)

    def get(self, cid):
        return next((f for f in self.items if f["id"] == cid), None)

    def add(self, cid, name=None, api_name="", kind=None, category=""):
        cid = parse_item_id(cid)
        if not cid:
            raise ValueError("Keine gültige 40-stellige Content-ID bzw. acestream://-Link")
        if self.has(cid):
            return False
        self.items.append({"name": (name or "").strip() or ("Stream " + cid[:8]),
                           "id": cid, "api_name": api_name or "",
                           "type": _norm_kind(kind, api_name, cid),
                           "category": category if self.has_category(category) else ""})
        self.save()
        return True

    def remove(self, cid):
        self.items = [f for f in self.items if f["id"] != cid]
        self.save()

    def rename(self, cid, name):
        for f in self.items:
            if f["id"] == cid and name.strip():
                f["name"] = name.strip()
        self.save()

    def set_category(self, cid, cat):
        f = self.get(cid)
        if f is not None:
            f["category"] = cat if self.has_category(cat) else ""
            self.save()

    # --- Kategorien ---
    def has_category(self, cat):
        return bool(cat) and any(c["id"] == cat for c in self.categories)

    def category_name(self, cat):
        return next((c["name"] for c in self.categories if c["id"] == cat), UNCATEGORIZED)

    def add_category(self, name):
        name = " ".join((name or "").split())[:MAX_CAT_NAME]
        if not name:
            raise ValueError("Der Name der Kategorie darf nicht leer sein.")
        if any(c["name"].lower() == name.lower() for c in self.categories) or name.lower() == UNCATEGORIZED.lower():
            raise ValueError("Die Kategorie „%s“ gibt es schon." % name)
        cid = uuid.uuid4().hex[:8]
        self.categories.append({"id": cid, "name": name})
        self.save()
        return cid

    def rename_category(self, cat, name):
        name = " ".join((name or "").split())[:MAX_CAT_NAME]
        if not name:
            raise ValueError("Der Name der Kategorie darf nicht leer sein.")
        if any(c["id"] != cat and c["name"].lower() == name.lower() for c in self.categories) \
                or name.lower() == UNCATEGORIZED.lower():
            raise ValueError("Die Kategorie „%s“ gibt es schon." % name)
        for c in self.categories:
            if c["id"] == cat:
                c["name"] = name
        self.save()

    def delete_category(self, cat):
        """Loescht nur die Kategorie; ihre Favoriten wandern nach "Ohne Kategorie" (nichts geht verloren)."""
        n = 0
        for f in self.items:
            if f["category"] == cat:
                f["category"] = ""
                n += 1
        self.categories = [c for c in self.categories if c["id"] != cat]
        self.save()
        return n

    def move_category(self, cat, delta):
        ids = [c["id"] for c in self.categories]
        if cat not in ids:
            return False
        i = ids.index(cat)
        j = i + delta
        if not 0 <= j < len(ids):
            return False
        self.categories[i], self.categories[j] = self.categories[j], self.categories[i]
        self.save()
        return True

    def grouped(self):
        """[(Kategorie-Dict | None, [Favoriten])] in Anzeigereihenfolge; None = Ohne Kategorie (zuletzt)."""
        out = [(c, [f for f in self.items if f["category"] == c["id"]]) for c in self.categories]
        out.append((None, [f for f in self.items if not f["category"]]))
        return out


class History:
    """Zuletzt gestartete Streams (max. HISTORY_MAX), neueste zuerst: [{name, id, type, ts}] in history.json."""

    def __init__(self, path=None):
        self.path = path or HISTORY_FILE
        self.items = []
        d = load_json_safe(self.path, [])
        for e in d if isinstance(d, list) else []:
            cid = parse_item_id(str(e.get("id", ""))) if isinstance(e, dict) else None
            if cid and cid not in [x["id"] for x in self.items]:
                try:
                    ts = float(e.get("ts") or 0)
                except (TypeError, ValueError):
                    ts = 0.0
                self.items.append({"name": str(e.get("name") or cid[:8]), "id": cid,
                                   "type": _norm_kind(e.get("type"), "", cid), "ts": ts})
        del self.items[HISTORY_MAX:]

    def add(self, cid, name, kind, ts=None):
        self.items = [x for x in self.items if x["id"] != cid]
        self.items.insert(0, {"name": name or cid[:8], "id": cid, "type": _norm_kind(kind, "", cid),
                              "ts": time.time() if ts is None else ts})
        del self.items[HISTORY_MAX:]
        self.save()

    def clear(self):
        self.items = []
        self.save()

    def save(self):
        save_json(self.path, self.items)


class PlayError(Exception):
    """Abspielen nicht gestartet. code=1: Engine laeuft nicht."""

    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


class Playback:
    """Ein gestarteter Player: proc, Name/Typ, optional stat_url (Live-Status) und Hinweis (Fallback)."""

    def __init__(self, proc, cid, name, kind, stat_url=None, note=""):
        self.proc, self.cid, self.name, self.kind = proc, cid, name, kind
        self.stat_url, self.note = stat_url, note
        self.cancel = False


def play_url(infohash):
    """Alter Pfad: der Player holt diese URL selbst (Engine antwortet mit Redirect)."""
    return "%s/ace/getstream?infohash=%s&pid=%s" % (ENGINE_URL, infohash, uuid.uuid4())


def start_stream_json(infohash):
    """Standardweg (python-acestream/acestream-launcher): getstream mit format=json.

    Gibt {"playback_url", "stat_url"} zurueck (beide auf ENGINE_URL umgesetzt). Die `command_url` (stop!) wird
    bewusst verworfen und nie aufgerufen. Blockiert (bis 30 s) -> nicht im GUI-Thread aufrufen.
    """
    qs = urllib.parse.urlencode({"infohash": infohash, "pid": str(uuid.uuid4()), "format": "json"})
    d = _http_json(ENGINE_URL + "/ace/getstream?" + qs, timeout=30)
    if d.get("error"):
        raise RuntimeError(str(d["error"])[:120])
    r = d.get("response") or d.get("result") or {}
    pb = _engine_local(r.get("playback_url"))
    if not pb:
        raise RuntimeError("keine playback_url in der Antwort")
    return {"playback_url": pb, "stat_url": _engine_local(r.get("stat_url"))}


def poll_stat(stat_url, timeout=4):
    """Eine stat_url-Abfrage -> dict (peers, speed_down, ...) oder None."""
    d = _http_json(stat_url, timeout=timeout)
    r = d.get("response") or d.get("result")
    return r if isinstance(r, dict) else None


def play(cid, kind=KIND_CONTENT, name="", live=True):
    """Startet das Abspielen als eigenen, von dieser App entkoppelten Prozess (eigene Session) -> Playback.

    content_id: ace-play <ID> (prueft die Engine selbst).
    url (oer:<kanal-id>): offizieller Livestream; die URL kommt aus der aktuellen Senderliste und geht direkt an
                mpv-ace.sh (MPV_WRAPPER; nie VLC, nie die Engine). Kein Engine-Check, keine Live-Stats.
    infohash:   Engine pruefen (get_version, blockiert bis 3 s -> nicht im GUI-Thread aufrufen), dann
                - live: getstream?...&format=json -> player-ace.sh <playback_url>, Stats ueber stat_url;
                  bei Fehler Fallback auf den alten Pfad (Hinweis in Playback.note)
                - sonst: player-ace.sh <getstream?infohash=...&pid=...> (Redirect). Der Infohash ist KEINE
                  Content-ID; id=<infohash> liefert von der Engine HTTP 500.
    ACE_TITLE=<Name> wird in die Umgebung des Players gesetzt (Fenstertitel in mpv-ace.sh/vlc-ace.sh).
    Ausgaben gehen in eine Logdatei statt in eine Pipe, damit der Player weiterlaeuft,
    auch wenn diese App geschlossen wird.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    header, stat_url, note = "", None, ""
    if kind == KIND_INFOHASH:
        if not re.fullmatch(r"[0-9a-f]{40}", cid or ""):
            raise PlayError("Ungültiger Infohash", code=2)
        if not engine_version():
            msg = "AceStream-Engine läuft nicht (%s)." % ENGINE_URL
            PLAY_LOG.write_text("FEHLER: " + msg + "\n", encoding="utf-8")
            raise PlayError(msg, code=1)
        target = None
        if live:
            try:
                info = start_stream_json(cid)
                target, stat_url = info["playback_url"], info["stat_url"]
            except Exception as e:
                note = "Live-Status nicht verfügbar (%s) – Standardstart." % getattr(e, "reason", e)
        if target is None:
            target = play_url(cid)
        argv = shlex.split(PLAYER) + [target]
        header = "Starte Player: " + target + "\n"
        if stat_url:
            header += "Live-Status: " + stat_url + "\n"
        if note:
            header += "HINWEIS: " + note + "\n"
    elif kind == KIND_URL:
        url = oer_resolve(cid)
        if not url:
            raise PlayError("Sender nicht (mehr) in der Liste: %s" % cid, code=3)
        argv = shlex.split(MPV_WRAPPER) + [url]
        header = "Starte mpv (offizieller Livestream): " + url + "\n"
    else:
        argv = shlex.split(PLAY_CMD) + [cid]
    env = dict(os.environ)
    title = " ".join((name or "").split())
    if title:
        env["ACE_TITLE"] = title
    else:
        env.pop("ACE_TITLE", None)
    log = open(PLAY_LOG, "wb")
    try:
        log.write(header.encode())
        log.flush()
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True, close_fds=True, env=env)
    finally:
        log.close()
    return Playback(p, cid, title or cid[:8], kind, stat_url, note)


def play_log_text():
    try:
        return PLAY_LOG.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def play_log_tail(n=300):
    try:
        t = PLAY_LOG.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        for line in reversed(t):
            if "FEHLER" in line:
                return line[:n]
        return t[-1][:n] if t else ""
    except Exception:
        return ""


def spawn_detached(cmd):
    return subprocess.Popen(shlex.split(cmd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


# --------------------------------------------------------------------------
# GUI (GTK4 + libadwaita)
# --------------------------------------------------------------------------
def run_gui(selftest_hook=None):
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, GLib, Gtk, Pango

    CSS = b"""
    window.ace-win, window.ace-win .ace-root { background: #121316; }
    .ace-root { color: #e3e2e6; }
    headerbar { background: #1a1b1f; box-shadow: none; min-height: 40px; }
    .ace-search { margin: 8px 10px 4px 10px; }
    .ace-chips { margin: 0 10px 2px 10px; }
    button.chip { border-radius: 99px; padding: 0 11px; min-height: 24px; font-size: 0.84em; background: alpha(#e3e2e6, 0.08); color: #c4c6d0; box-shadow: none; }
    button.chip:hover { background: alpha(#e3e2e6, 0.15); }
    button.chip:checked { background: alpha(#8ab4f8, 0.28); color: #8ab4f8; font-weight: 600; }
    menubutton.chip > button { border-radius: 99px; padding: 0 11px; min-height: 24px; font-size: 0.84em; background: alpha(#e3e2e6, 0.08); color: #c4c6d0; box-shadow: none; }
    menubutton.chip > button:hover { background: alpha(#e3e2e6, 0.15); }
    menubutton.chip-on > button { background: alpha(#8ab4f8, 0.28); color: #8ab4f8; font-weight: 600; }
    .ace-engine { margin: 2px 10px 6px 10px; }
    .badge { border-radius: 99px; padding: 1px 10px; font-size: 0.82em; font-weight: 600; }
    .badge-on  { background: alpha(#69f0ae, 0.16); color: #69f0ae; }
    .badge-off { background: alpha(#ff8a80, 0.16); color: #ff8a80; }
    .badge-wait{ background: alpha(#b0bec5, 0.14); color: #b0bec5; }
    list.ace-list { background: transparent; }
    list.ace-list > row { background: #1d1e23; border-radius: 12px; margin: 2px 8px; padding: 0; }
    list.ace-list > row:hover { background: #24262c; }
    list.ace-list > row:selected, list.ace-list > row:focus-visible { background: #2b2f3a; outline: 2px solid #8ab4f8; outline-offset: -2px; }
    list.ace-list > row.cat-head { background: alpha(#8ab4f8, 0.09); margin-top: 8px; }
    list.ace-list > row.cat-head:hover { background: alpha(#8ab4f8, 0.15); }
    .cat-name { font-weight: 700; }
    .cat-count { color: #a9acb5; font-size: 0.85em; }
    .row-title { font-weight: 600; }
    .row-sub { font-size: 0.82em; color: #a9acb5; }
    .row-epg { font-size: 0.82em; color: #8ab4f8; }
    .tag { background: alpha(#8ab4f8, 0.15); color: #8ab4f8; border-radius: 6px; padding: 0 6px; font-size: 0.75em; }
    .tag-rule { background: alpha(#ffd54f, 0.16); color: #ffd54f; }
    .star-on { color: #ffd54f; }
    button.play-btn { background: alpha(#69f0ae, 0.18); color: #69f0ae; border-radius: 99px; min-width: 30px; min-height: 30px; padding: 0; }
    button.play-btn:hover { background: alpha(#69f0ae, 0.32); }
    .lang { background: alpha(#e3e2e6, 0.12); color: #e3e2e6; border-radius: 6px; padding: 0 6px; font-size: 0.75em; font-weight: 700; }
    .lang-de { background: alpha(#ffd54f, 0.22); color: #ffd54f; }
    .lang-en { background: alpha(#8ab4f8, 0.22); color: #8ab4f8; }
    .grp { color: #a9acb5; font-size: 0.78em; font-weight: 700; margin: 8px 14px 2px 14px; }
    .hint { color: #a9acb5; font-size: 0.85em; }
    .bar { margin: 2px 10px 0 10px; }
    .live { color: #69f0ae; font-size: 0.88em; font-weight: 600; margin: 2px 12px 0 12px; }
    .notice { background: alpha(#ffd54f, 0.14); color: #ffd54f; border-radius: 8px; padding: 4px 10px; margin: 2px 10px; font-size: 0.88em; }
    .notice-err { background: alpha(#ff8a80, 0.16); color: #ff8a80; }
    scrolledwindow { background: transparent; }
    """
    LEVEL_COLOR = {"hi": "#69f0ae", "mid": "#ffd54f", "lo": "#ff8a80"}

    def clear_box(box):
        child = box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

    class Row(Gtk.ListBoxRow):
        """Zeile fuer kind = "result" (Suche), "fav" (Favorit), "hist" (Verlauf) oder "oer" (offizieller Livestream)."""

        def __init__(self, rec, kind, win):
            super().__init__()
            self.rec, self.kind, self.win = rec, kind, win
            self.key = (kind, rec["id"])
            self.set_activatable(True)
            box = Gtk.Box(spacing=6, margin_start=10, margin_end=6, margin_top=6, margin_bottom=6)
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
            self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
            self.title.add_css_class("row-title")
            self.sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, use_markup=True)
            self.sub.add_css_class("row-sub")
            self.epg = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, visible=False)
            self.epg.add_css_class("row-epg")
            col.append(self.title)
            col.append(self.sub)
            col.append(self.epg)
            box.append(col)
            self.tags = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
            box.append(self.tags)
            self.play_btn = Gtk.Button(icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER,
                                       tooltip_text="Abspielen (Enter / Doppelklick)")
            self.play_btn.add_css_class("play-btn")
            self.play_btn.connect("clicked", lambda *_: win.play_rec(self.rec))
            box.append(self.play_btn)
            self.star = None
            if kind in ("result", "hist", "oer"):
                self.star = Gtk.Button(valign=Gtk.Align.CENTER, tooltip_text="Zu Favoriten (f)")
                self.star.add_css_class("flat")
                self.star.connect("clicked", lambda *_: win.toggle_fav(self.rec, self.star))
                box.append(self.star)
            self.menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER,
                                           tooltip_text="Mehr (Rechtsklick)")
            self.menu_btn.add_css_class("flat")
            self.pop = Gtk.Popover()
            self.pop.connect("show", lambda *_: self._build_menu())
            self.menu_btn.set_popover(self.pop)
            box.append(self.menu_btn)
            rc = Gtk.GestureClick(button=3)
            rc.connect("pressed", lambda *_: self.menu_btn.popup())
            self.add_controller(rc)
            self.set_child(box)
            self._build_menu()
            self.refresh()

        def _build_menu(self):
            vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                         margin_start=6, margin_end=6)
            w, r = self.win, self.rec
            items = []
            if self.kind == "fav":
                items.append(("Kategorie ändern … (m)", lambda: w.assign_category(r, self.menu_btn)))
                items.append(("Umbenennen (F2)", lambda: w.rename_fav(r)))
            else:
                items.append(("Kategorie ändern …" if w.favs.has(r["id"]) else "In Favoriten / Kategorie …",
                              lambda: w.assign_category(r, self.menu_btn)))
            items.append(("Link kopieren (Strg+C)", lambda: w.copy_link(r)))
            if self.kind == "fav":
                items.append(("Entfernen (Entf)", lambda: w.delete_fav(r)))
            clear_box(vb)
            for label, fn in items:
                b = Gtk.Button(label=label)
                b.add_css_class("flat")
                b.get_child().set_xalign(0)
                b.connect("clicked", lambda _b, f=fn: (self.pop.popdown(), GLib.idle_add(lambda: (f(), False)[1])))
                vb.append(b)
            self.pop.set_child(vb)

        def refresh(self, info=None):
            """info: bei Favoriten Treffer-Dict aus der Namenssuche, 'missing' oder None."""
            r = self.rec
            self.title.set_text(r["name"])
            self.title.set_tooltip_text("%s\n%s: %s" % (r["name"], r.get("type", KIND_CONTENT), r["id"]))
            is_url = r.get("type") == KIND_URL
            if self.kind == "oer":  # kompakt: nur Name, ▶ und ★
                src = None
                self.sub.set_visible(False)
            elif is_url and self.kind in ("fav", "hist"):
                src = None
                self.sub.set_markup(GLib.markup_escape_text(
                    fmt_when(r.get("ts")) if self.kind == "hist" else "Offizieller Livestream · direkt, ohne AceStream"))
            elif self.kind == "hist":
                src = None
                self.sub.set_markup(GLib.markup_escape_text(fmt_when(r.get("ts"))))
            else:
                src = r if self.kind == "result" else info
                parts = []
                if self.kind == "fav" and src is None:
                    parts.append("Verfügbarkeit …" if self.win.fav_status_busy else "Verfügbarkeit –")
                elif self.kind == "fav" and src == "missing":
                    parts.append("nicht in der Suche gefunden")
                else:
                    lvl = avail_level(src.get("availability"))
                    if lvl:
                        parts.append('Verfügbarkeit <span foreground="%s">●</span>' % LEVEL_COLOR[lvl])
                    else:
                        parts.append("Verfügbarkeit –")
                    br = fmt_bitrate(src.get("bitrate"))
                    if br:
                        parts.append(br)
                    if src.get("countries"):
                        parts.append(", ".join(c.upper() for c in src["countries"][:3]))
                    if src.get("disabled"):
                        parts.append('<span foreground="#ff8a80">deaktiviert</span>')
                if self.kind == "result" and isinstance(src, dict):
                    also = also_categories(src, self.win.category, self.win.cat_keywords)
                    if also:
                        parts.append('<span alpha="65%%">auch: %s</span>' % GLib.markup_escape_text(", ".join(also)))
                self.sub.set_markup("  ·  ".join(parts))
            line = epg_line(src.get("epg")) if isinstance(src, dict) else None
            self.epg.set_visible(bool(line))
            self.epg.set_text(line or "")
            clear_box(self.tags)
            cats = (src.get("categories") or []) if isinstance(src, dict) else []
            cats = [c for c in cats if c != "tv"] + [c for c in cats if c == "tv"]
            badge = lang_badge(src) if isinstance(src, dict) else None
            if badge:
                lb_ = Gtk.Label(label=badge)
                lb_.add_css_class("lang")
                if badge in ("DE", "AT", "CH"):
                    lb_.add_css_class("lang-de")
                elif badge == "EN":
                    lb_.add_css_class("lang-en")
                self.tags.append(lb_)
            if isinstance(src, dict) and src.get("rule_match"):
                rb = Gtk.Label(label="Regel")
                rb.add_css_class("tag")
                rb.add_css_class("tag-rule")
                rb.set_tooltip_text("Per Stichwort-Regel gefunden (Kategorie-Regeln …), nicht über die API-Kategorie")
                self.tags.append(rb)
            for c in cats[:2]:
                t = Gtk.Label(label=cat_label(c))
                t.add_css_class("tag")
                self.tags.append(t)
            if is_url and self.kind == "fav":
                ob = Gtk.Label(label="ÖR")
                ob.add_css_class("lang")
                self.tags.append(ob)
            if self.star is not None:
                fav = self.win.favs.has(r["id"])
                self.star.set_icon_name("starred-symbolic" if fav else "non-starred-symbolic")
                if fav:
                    self.star.add_css_class("star-on")
                else:
                    self.star.remove_css_class("star-on")

    class CatRow(Gtk.ListBoxRow):
        """Einklappbarer Kategorie-Header im Favoriten-Tab. rec = {"id", "name"}; id "" = Ohne Kategorie."""

        def __init__(self, rec, count, collapsed, win):
            super().__init__()
            self.rec, self.kind, self.win = rec, "cat", win
            self.key = ("cat", rec["id"])
            self.add_css_class("cat-head")
            self.set_activatable(True)
            box = Gtk.Box(spacing=4, margin_start=6, margin_end=6, margin_top=2, margin_bottom=2)
            self.toggle = Gtk.Button(icon_name="pan-end-symbolic" if collapsed else "pan-down-symbolic",
                                     valign=Gtk.Align.CENTER, tooltip_text="Ein-/ausklappen (Doppelklick, Enter)")
            self.toggle.add_css_class("flat")
            self.toggle.connect("clicked", lambda *_: win.toggle_cat(rec["id"]))
            box.append(self.toggle)
            name = Gtk.Label(label=rec["name"], xalign=0, ellipsize=Pango.EllipsizeMode.END, hexpand=True)
            name.add_css_class("cat-name")
            box.append(name)
            cnt = Gtk.Label(label=str(count))
            cnt.add_css_class("cat-count")
            box.append(cnt)
            if rec["id"]:
                for icon, tip, fn in (
                        ("go-up-symbolic", "Nach oben (Strg+Hoch)", lambda: win.move_cat(rec["id"], -1)),
                        ("go-down-symbolic", "Nach unten (Strg+Runter)", lambda: win.move_cat(rec["id"], 1)),
                        ("document-edit-symbolic", "Umbenennen (F2)", lambda: win.rename_cat(rec["id"])),
                        ("user-trash-symbolic", "Kategorie löschen (Entf) – Favoriten bleiben",
                         lambda: win.delete_cat(rec["id"]))):
                    b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
                    b.add_css_class("flat")
                    b.connect("clicked", lambda _b, f=fn: f())
                    box.append(b)
            self.set_child(box)

    class Win(Adw.ApplicationWindow):
        MORE_LABEL = "Mehr ▾"

        def __init__(self, app):
            super().__init__(application=app, title="AceStream Suche")
            self.set_default_size(420, 640)
            self.add_css_class("ace-win")
            self.favs = Favorites()
            self.history = History()
            self.settings = load_json_safe(SETTINGS_FILE, {})
            if not isinstance(self.settings, dict):
                self.settings = {}
            try:
                self.interval_min = max(0, int(self.settings.get("interval_min", DEFAULT_INTERVAL_MIN)))
            except (TypeError, ValueError):
                self.interval_min = DEFAULT_INTERVAL_MIN
            self.live_enabled = bool(self.settings.get("live_status", True))
            rc = self.settings.get("recent")
            self.recent = [s for s in rc if isinstance(s, str) and s.strip()][:RECENT_MAX] if isinstance(rc, list) else []
            co = self.settings.get("collapsed")
            self.collapsed = set(s for s in co if isinstance(s, str)) if isinstance(co, list) else set()
            self.cat_keywords = load_cat_keywords(self.settings)
            self.cat_exclude = load_cat_exclude(self.settings)
            self.strict = load_strict(self.settings)
            self.oer_busy = False
            self.last_query = ""
            self.category = None
            self.results = []
            self.group_n = {}
            self.search_gen = 0
            self.debounce_id = 0
            self.auto_id = 0
            self.fav_status = {}
            self.fav_status_busy = False
            self.engine_ok = None
            self.engine_ver = None
            self.notice_id = 0
            self.play_busy = False
            self.current = None
            self.recent_idx = -1
            self.recent_draft = ""
            self._cycling = False
            self._chip_lock = False

            root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            root.add_css_class("ace-root")
            self.set_content(root)

            hb = Adw.HeaderBar()
            self.stack = Adw.ViewStack()
            hb.set_title_widget(Adw.ViewSwitcher(policy=Adw.ViewSwitcherPolicy.WIDE, stack=self.stack))
            self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic",
                                          tooltip_text="Aktualisieren (Strg+R / F5)")
            self.refresh_btn.connect("clicked", lambda *_: self.refresh_all(manual=True))
            hb.pack_start(self.refresh_btn)
            hb.pack_end(self._build_menu())
            root.append(hb)

            self.entry = Gtk.SearchEntry(placeholder_text="Suche in der lokalen Engine …  (Strg+F, Strg+L leert)",
                                         search_delay=0)
            self.entry.add_css_class("ace-search")
            self.entry.connect("changed", self.on_entry_changed)
            self.entry.connect("activate", lambda *_: self.on_entry_enter())
            self.entry.connect("stop-search", lambda *_: self.entry.set_text(""))
            ek = Gtk.EventControllerKey()
            ek.connect("key-pressed", self.on_entry_key)
            self.entry.add_controller(ek)
            root.append(self.entry)

            # Kategorie-Chips (nur API-Werte); Alt+Links/Rechts wechselt. Hauptreihe + "Mehr ▾" (seltene Kategorien),
            # rechts daneben der Schalter "Streng" (strenger Filter: nur Hauptkategorie).
            self.chips = {}
            chip_box = Gtk.Box(spacing=4)
            more_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                               margin_start=6, margin_end=6)
            first = None
            for key, label in [(None, "Alle")] + [(k, CHIP_LABELS.get(k, cat_label(k))) for k in CATEGORY_CHIPS]:
                b = Gtk.ToggleButton(label=label, focusable=False, focus_on_click=False)
                if key in CHIPS_MORE:
                    b.add_css_class("flat")
                    b.get_child().set_xalign(0)
                else:
                    b.add_css_class("chip")
                if first is None:
                    first = b
                    b.set_active(True)
                else:
                    b.set_group(first)
                b.set_tooltip_text(CHIP_TIPS.get(key) or (("Kategorie der Such-API: %s" % cat_label(key)) if key else None))
                b.connect("toggled", self.on_chip_toggled, key)
                (more_box if key in CHIPS_MORE else chip_box).append(b)
                self.chips[key] = b
            self.more_pop = Gtk.Popover(child=more_box)
            self.more_btn = Gtk.MenuButton(label=self.MORE_LABEL, popover=self.more_pop, focusable=False,
                                           focus_on_click=False, tooltip_text="Seltenere Kategorien")
            self.more_btn.add_css_class("chip")
            chip_box.append(self.more_btn)
            self.chip_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
                                                  vscrollbar_policy=Gtk.PolicyType.NEVER, hexpand=True)
            self.chip_scroll.add_css_class("ace-chips")
            self.chip_scroll.set_child(chip_box)
            self.chip_scroll.set_tooltip_text("Kategorie der Such-API (Alt+Links/Rechts)")
            self.strict_btn = Gtk.ToggleButton(label="Streng", active=self.strict, focusable=False,
                                               focus_on_click=False, valign=Gtk.Align.CENTER)
            self.strict_btn.add_css_class("chip")
            self.strict_btn.connect("toggled", self.on_strict_toggled)
            self.strict_btn.set_tooltip_text(STRICT_TIP)
            chip_row = Gtk.Box(spacing=4, margin_end=10)
            chip_row.append(self.chip_scroll)
            chip_row.append(self.strict_btn)
            root.append(chip_row)

            erow = Gtk.Box(spacing=8)
            erow.add_css_class("ace-engine")
            self.badge = Gtk.Label(label="Engine: prüfe …")
            self.badge.add_css_class("badge")
            self.badge.add_css_class("badge-wait")
            erow.append(self.badge)
            erow.append(Gtk.Box(hexpand=True))
            self.btn_start = Gtk.Button(label="Engine starten", visible=False)
            self.btn_start.add_css_class("suggested-action")
            self.btn_start.connect("clicked", lambda *_: self.engine_start())
            self.btn_stop = Gtk.Button(label="Stoppen", visible=False)
            self.btn_stop.add_css_class("destructive-action")
            self.btn_stop.connect("clicked", lambda *_: self.engine_stop_confirm())
            erow.append(self.btn_start)
            erow.append(self.btn_stop)
            root.append(erow)

            self.notice = Gtk.Label(xalign=0, wrap=True, visible=False)
            self.notice.add_css_class("notice")
            root.append(self.notice)

            # Tab Suche
            self.res_list = self._make_list()
            self.res_list.set_header_func(self._group_header)
            self.res_hint = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
            self.res_hint.add_css_class("hint")
            self.res_hint.set_can_target(False)
            self.res_hint_default = "Suchbegriff eingeben, Enter drücken –\noder oben eine Kategorie wählen."
            self.res_hint.set_text(self.res_hint_default)
            res_page = Gtk.Overlay()
            res_page.set_child(self._scroll(self.res_list))
            res_page.add_overlay(self.res_hint)

            # Tab Favoriten (nach eigenen Kategorien gruppiert)
            self.fav_list = self._make_list()
            self.fav_hint = Gtk.Label(
                label="Noch keine Favoriten.\n★ an einem Suchergebnis oder „+“ (Strg+N)\nmit Content-ID / acestream://-Link.",
                wrap=True, justify=Gtk.Justification.CENTER)
            self.fav_hint.add_css_class("hint")
            self.fav_hint.set_can_target(False)
            fav_ov = Gtk.Overlay()
            fav_ov.set_child(self._scroll(self.fav_list))
            fav_ov.add_overlay(self.fav_hint)
            add_btn = Gtk.Button(icon_name="list-add-symbolic", halign=Gtk.Align.END, valign=Gtk.Align.END,
                                 margin_end=14, margin_bottom=14, tooltip_text="Favorit hinzufügen (Strg+N)")
            add_btn.add_css_class("circular")
            add_btn.add_css_class("suggested-action")
            add_btn.connect("clicked", lambda *_: self.add_fav_dialog())
            fav_ov.add_overlay(add_btn)
            fav_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            bar = Gtk.Box(spacing=6)
            bar.add_css_class("bar")
            lab = Gtk.Label(label="Eigene Kategorien: Ordner anlegen, Favoriten zuordnen (m)", xalign=0,
                            hexpand=True, ellipsize=Pango.EllipsizeMode.END)
            lab.add_css_class("hint")
            bar.append(lab)
            nb = Gtk.Button(child=Adw.ButtonContent(icon_name="folder-new-symbolic", label="Kategorie"),
                            tooltip_text="Neue Kategorie (Strg+Umschalt+N)")
            nb.add_css_class("flat")
            nb.connect("clicked", lambda *_: self.new_category_dialog())
            bar.append(nb)
            fav_page.append(bar)
            fav_ov.set_vexpand(True)
            fav_page.append(fav_ov)

            # Tab Verlauf
            self.hist_list = self._make_list()
            self.hist_hint = Gtk.Label(label="Noch nichts gestartet.\nDie letzten 15 Streams erscheinen hier.",
                                       wrap=True, justify=Gtk.Justification.CENTER)
            self.hist_hint.add_css_class("hint")
            self.hist_hint.set_can_target(False)
            hist_ov = Gtk.Overlay()
            hist_ov.set_child(self._scroll(self.hist_list))
            hist_ov.add_overlay(self.hist_hint)
            hist_ov.set_vexpand(True)
            hist_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            hbar = Gtk.Box(spacing=6)
            hbar.add_css_class("bar")
            hl = Gtk.Label(label="Zuletzt gestartet (max. %d)" % HISTORY_MAX, xalign=0, hexpand=True)
            hl.add_css_class("hint")
            hbar.append(hl)
            self.hist_clear_btn = Gtk.Button(child=Adw.ButtonContent(icon_name="edit-clear-all-symbolic",
                                                                     label="Verlauf leeren"))
            self.hist_clear_btn.add_css_class("flat")
            self.hist_clear_btn.connect("clicked", lambda *_: self.clear_history())
            hbar.append(self.hist_clear_btn)
            hist_page.append(hbar)
            hist_page.append(hist_ov)

            # Tab OER: offizielle Livestreams der oeffentlich-rechtlichen Sender (direkt in mpv, ohne AceStream)
            self.oer_list = self._make_list()
            self.oer_list.set_header_func(self._oer_header)
            oer_ov = Gtk.Overlay()
            oer_ov.set_child(self._scroll(self.oer_list))
            oer_ov.set_vexpand(True)
            oer_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            obar = Gtk.Box(spacing=6)
            obar.add_css_class("bar")
            ol = Gtk.Label(label="Offizielle Livestreams · direkt, ohne AceStream/VPN", xalign=0, hexpand=True,
                           ellipsize=Pango.EllipsizeMode.END)
            ol.add_css_class("hint")
            obar.append(ol)
            self.oer_refresh_btn = Gtk.Button(child=Adw.ButtonContent(icon_name="view-refresh-symbolic",
                                                                      label="Senderliste"),
                                              tooltip_text="Senderliste jetzt neu laden (Strg+R)")
            self.oer_refresh_btn.add_css_class("flat")
            self.oer_refresh_btn.connect("clicked", lambda *_: self.refresh_oer(force=True))
            obar.append(self.oer_refresh_btn)
            oer_page.append(obar)
            self.oer_src = Gtk.Label(xalign=0, margin_start=12, margin_end=12, ellipsize=Pango.EllipsizeMode.END)
            self.oer_src.add_css_class("hint")
            oer_page.append(self.oer_src)
            oer_page.append(oer_ov)

            self.stack.add_titled(res_page, "results", "Suche").set_icon_name("system-search-symbolic")
            self.stack.add_titled(fav_page, "favs", "Favoriten").set_icon_name("starred-symbolic")
            self.stack.add_titled(hist_page, "hist", "Verlauf").set_icon_name("document-open-recent-symbolic")
            self.stack.add_titled(oer_page, "oer", "ÖR").set_icon_name("video-display-symbolic")
            self.stack.set_vexpand(True)
            self.stack.connect("notify::visible-child-name", lambda *_: self.on_page_changed())
            root.append(self.stack)

            self.live = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, visible=False)
            self.live.add_css_class("live")
            root.append(self.live)
            self.status = Gtk.Label(xalign=0, margin_start=12, margin_end=12, margin_bottom=6,
                                    ellipsize=Pango.EllipsizeMode.END)
            self.status.add_css_class("hint")
            root.append(self.status)

            kc = Gtk.EventControllerKey()
            kc.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            kc.connect("key-pressed", self.on_window_key)
            self.add_controller(kc)

            self.rebuild_favs()
            self.rebuild_hist()
            oer_load()  # sofort Cache bzw. eingebaute Liste; die Aktualisierung (max. 1x/24 h) laeuft im Hintergrund
            self.rebuild_oer()
            self.refresh_oer()
            self.entry.grab_focus()
            GLib.timeout_add_seconds(STATUS_POLL_S, self._poll_engine)
            self.check_engine()
            self._schedule_auto()

        # ---------- Aufbau ----------
        def _scroll(self, child):
            sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
            sc.set_child(child)
            return sc

        def _make_list(self):
            # Einzelklick markiert nur; Doppelklick bzw. Enter aktiviert (= abspielen). Mit dem Standard
            # (activate-on-single-click=True) wuerde ein Doppelklick den Player zweimal starten.
            lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, activate_on_single_click=False)
            lb.add_css_class("ace-list")
            lb.connect("row-activated", self.on_row_activated)
            kc = Gtk.EventControllerKey()
            kc.connect("key-pressed", self.on_list_key, lb)
            lb.add_controller(kc)
            return lb

        def _group_header(self, row, before):
            """Trenner Deutsch / Englisch / Weitere / Deaktiviert mit Anzahl ("Deutsch · 7") in der Suchliste."""
            g = result_group(row.rec)
            gb = result_group(before.rec) if before is not None else None
            if g != gb:
                lab = Gtk.Label(label="%s · %d" % (GROUP_NAMES[g], self.group_n.get(g, 0)), xalign=0)
                lab.add_css_class("grp")
                row.set_header(lab)
            else:
                row.set_header(None)

        def _oer_header(self, row, before):
            g = row.rec.get("group")
            if before is None or before.rec.get("group") != g:
                n = sum(1 for c in oer_channels() if c.get("group") == g)
                lab = Gtk.Label(label="%s · %d" % (OER_GROUPS.get(g, g), n), xalign=0)
                lab.add_css_class("grp")
                row.set_header(lab)
            else:
                row.set_header(None)

        def _build_menu(self):
            pop = Gtk.Popover()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10, margin_bottom=10,
                          margin_start=10, margin_end=10)
            row = Gtk.Box(spacing=8)
            row.append(Gtk.Label(label="Auto-Aktualisierung (Min.)", xalign=0, hexpand=True))
            self.spin = Gtk.SpinButton.new_with_range(0, 120, 1)
            self.spin.set_value(self.interval_min)
            self.spin.connect("value-changed", self.on_interval_changed)
            row.append(self.spin)
            box.append(row)
            hint = Gtk.Label(label="0 = aus. Lädt die letzte Suche neu.", xalign=0)
            hint.add_css_class("hint")
            box.append(hint)
            lrow = Gtk.Box(spacing=8)
            lrow.append(Gtk.Label(label="Live-Status (Peers, Speed)", xalign=0, hexpand=True))
            self.live_switch = Gtk.Switch(active=self.live_enabled, valign=Gtk.Align.CENTER)
            self.live_switch.connect("notify::active", self.on_live_toggled)
            lrow.append(self.live_switch)
            box.append(lrow)
            lh = Gtk.Label(label="An: Start über getstream?format=json. Aus: alter Redirect-Pfad.", xalign=0,
                           wrap=True)
            lh.add_css_class("hint")
            box.append(lh)
            srow = Gtk.Box(spacing=8)
            srow.append(Gtk.Label(label="Strenger Kategorie-Filter", xalign=0, hexpand=True))
            self.strict_switch = Gtk.Switch(active=self.strict, valign=Gtk.Align.CENTER)
            self.strict_switch.connect("notify::active", lambda sw, _p: self.set_strict(sw.get_active()))
            srow.append(self.strict_switch)
            box.append(srow)
            sh = Gtk.Label(label=STRICT_TIP, xalign=0, wrap=True)
            sh.add_css_class("hint")
            box.append(sh)
            ab = Gtk.Button(label="Favorit hinzufügen …")
            ab.connect("clicked", lambda *_: (pop.popdown(), self.add_fav_dialog()))
            box.append(ab)
            cb = Gtk.Button(label="Neue Favoriten-Kategorie …")
            cb.connect("clicked", lambda *_: (pop.popdown(), self.new_category_dialog()))
            box.append(cb)
            rb = Gtk.Button(label="Kategorie-Regeln …")
            rb.set_tooltip_text("Eigene Stichwörter je Kategorie-Chip")
            rb.connect("clicked", lambda *_: (pop.popdown(), self.open_rules_dialog()))
            box.append(rb)
            pop.set_child(box)
            return Gtk.MenuButton(icon_name="open-menu-symbolic", popover=pop, tooltip_text="Einstellungen")

        def on_live_toggled(self, sw, _p):
            self.live_enabled = sw.get_active()
            self.settings["live_status"] = self.live_enabled
            self._save_settings()

        def _save_settings(self):
            try:
                save_json(SETTINGS_FILE, self.settings)
            except Exception:
                pass

        # ---------- Tastatur ----------
        def on_window_key(self, ctrl, keyval, keycode, state):
            ctl = bool(state & Gdk.ModifierType.CONTROL_MASK)
            shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
            alt = bool(state & Gdk.ModifierType.ALT_MASK)
            if ctl and keyval == Gdk.KEY_f:
                self.stack.set_visible_child_name("results")
                self.entry.grab_focus()
                return True
            if ctl and keyval == Gdk.KEY_l:
                self.clear_search()
                return True
            if (ctl and keyval == Gdk.KEY_r) or keyval == Gdk.KEY_F5:
                self.refresh_all(manual=True)
                return True
            if ctl and shift and keyval in (Gdk.KEY_n, Gdk.KEY_N):
                self.new_category_dialog()
                return True
            if ctl and keyval == Gdk.KEY_n:
                self.add_fav_dialog()
                return True
            if ctl and keyval in (Gdk.KEY_1, Gdk.KEY_2, Gdk.KEY_3, Gdk.KEY_4):
                self.stack.set_visible_child_name({Gdk.KEY_1: "results", Gdk.KEY_2: "favs", Gdk.KEY_3: "hist",
                                                   Gdk.KEY_4: "oer"}[keyval])
                return True
            if alt and keyval in (Gdk.KEY_Left, Gdk.KEY_Right) and self.stack.get_visible_child_name() == "results":
                self.cycle_category(-1 if keyval == Gdk.KEY_Left else 1)
                return True
            if ctl and keyval == Gdk.KEY_q:
                self.close()
                return True
            return False

        def on_entry_key(self, ctrl, keyval, keycode, state):
            """Hoch/Runter im Suchfeld blaettert durch die letzten Suchbegriffe; Runter ohne Verlauf -> Liste."""
            if keyval == Gdk.KEY_Up and self.recent:
                if self.recent_idx + 1 < len(self.recent):
                    if self.recent_idx < 0:
                        self.recent_draft = self.entry.get_text()
                    self.recent_idx += 1
                    self._set_entry(self.recent[self.recent_idx])
                return True
            if keyval == Gdk.KEY_Down:
                if self.recent_idx >= 0:
                    self.recent_idx -= 1
                    self._set_entry(self.recent[self.recent_idx] if self.recent_idx >= 0 else self.recent_draft)
                else:
                    self.focus_first_row()
                return True
            return False

        def _set_entry(self, text):
            self._cycling = True
            try:
                self.entry.set_text(text)
                self.entry.set_position(-1)
            finally:
                self._cycling = False

        def current_list(self):
            return {"results": self.res_list, "favs": self.fav_list, "hist": self.hist_list,
                    "oer": self.oer_list}[self.stack.get_visible_child_name()]

        def focus_first_row(self):
            lb = self.current_list()
            row = lb.get_row_at_index(0)
            if row:
                lb.select_row(row)
                row.grab_focus()

        def on_list_key(self, ctrl, keyval, keycode, state, lb):
            row = lb.get_selected_row()
            ctl = bool(state & Gdk.ModifierType.CONTROL_MASK)
            if keyval == Gdk.KEY_Escape:
                self.entry.grab_focus()
                return True
            if row is None:
                return False
            if keyval == Gdk.KEY_Up and row.get_index() == 0 and not ctl:
                self.entry.grab_focus()
                return True
            if ctl and keyval == Gdk.KEY_c and row.kind != "cat":
                self.copy_link(row.rec)
                return True
            if keyval == Gdk.KEY_f and row.kind in ("result", "hist", "oer"):
                self.toggle_fav(row.rec, row.star)
                return True
            if row.kind == "fav":
                if keyval == Gdk.KEY_Delete:
                    self.delete_fav(row.rec)
                    return True
                if keyval == Gdk.KEY_F2:
                    self.rename_fav(row.rec)
                    return True
                if keyval == Gdk.KEY_m:
                    self.assign_category(row.rec, row.menu_btn)
                    return True
            if row.kind == "cat":
                cid = row.rec["id"]
                if keyval in (Gdk.KEY_Left, Gdk.KEY_Right):
                    if (keyval == Gdk.KEY_Left) != (cid in self.collapsed):
                        self.toggle_cat(cid)
                    return True
                if cid:
                    if keyval == Gdk.KEY_Delete:
                        self.delete_cat(cid)
                        return True
                    if keyval == Gdk.KEY_F2:
                        self.rename_cat(cid)
                        return True
                    if ctl and keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
                        self.move_cat(cid, -1 if keyval == Gdk.KEY_Up else 1)
                        return True
            return False

        def on_page_changed(self):
            name = self.stack.get_visible_child_name()
            if name == "favs":
                self.refresh_fav_status()
            elif name == "hist":
                self.rebuild_hist()
            elif name == "oer":
                self.refresh_oer()  # nur wenn die Liste aelter als 24 h ist
            else:
                self.entry.grab_focus()

        # ---------- Anzeige ----------
        def say(self, text):
            self.status.set_text(text)

        def show_notice(self, text, err=False, seconds=12):
            self.notice.set_text(text)
            if err:
                self.notice.add_css_class("notice-err")
            else:
                self.notice.remove_css_class("notice-err")
            self.notice.set_visible(True)
            if self.notice_id:
                GLib.source_remove(self.notice_id)
                self.notice_id = 0
            if seconds:
                self.notice_id = GLib.timeout_add_seconds(seconds, self._hide_notice)

        def _hide_notice(self):
            self.notice.set_visible(False)
            self.notice_id = 0
            return False

        @staticmethod
        def _rows(lb):
            i = 0
            while True:
                r = lb.get_row_at_index(i)
                if r is None:
                    return
                yield r
                i += 1

        def _clear(self, lb):
            for r in list(self._rows(lb)):
                lb.remove(r)

        def _reselect(self, lb, keep, focus=False):
            if keep is None:
                return
            for r in self._rows(lb):
                if r.key == keep:
                    lb.select_row(r)
                    if focus:
                        r.grab_focus()
                    return

        def refresh_stars(self):
            for lb in (self.res_list, self.hist_list, self.oer_list):
                for r in self._rows(lb):
                    r.refresh()

        def rebuild_results(self, keep=None):
            self._clear(self.res_list)
            self.group_n = group_counts(self.results)
            for rec in self.results:
                self.res_list.append(Row(rec, "result", self))
            self.res_hint.set_visible(not self.results)
            self._reselect(self.res_list, keep)

        def rebuild_favs(self, keep=None, focus=False):
            if keep is None:
                sel = self.fav_list.get_selected_row()
                keep = sel.key if sel else None
            self._clear(self.fav_list)
            has_cats = bool(self.favs.categories)
            for cat, items in self.favs.grouped():
                if has_cats:
                    if cat is None and not items:
                        continue
                    rec = cat or {"id": "", "name": UNCATEGORIZED}
                    folded = rec["id"] in self.collapsed
                    self.fav_list.append(CatRow(rec, len(items), folded, self))
                    if folded:
                        continue
                for f in items:
                    row = Row(f, "fav", self)
                    row.refresh(self.fav_status.get(f["id"]))
                    self.fav_list.append(row)
            self.fav_hint.set_visible(not self.favs.items and not self.favs.categories)
            self._reselect(self.fav_list, keep, focus)

        def rebuild_hist(self):
            self._clear(self.hist_list)
            for h in self.history.items:
                self.hist_list.append(Row(h, "hist", self))
            self.hist_hint.set_visible(not self.history.items)
            self.hist_clear_btn.set_sensitive(bool(self.history.items))

        def rebuild_oer(self):
            sel = self.oer_list.get_selected_row()
            keep = sel.key if sel is not None else None
            self._clear(self.oer_list)
            for c in oer_channels():
                self.oer_list.append(Row(c, "oer", self))
            self.oer_src.set_text(oer_source_text())
            self._reselect(self.oer_list, keep)

        def refresh_oer(self, force=False):
            """Senderliste im Hintergrund aktualisieren (Standard: nur wenn aelter als 24 h; force: immer)."""
            if self.oer_busy:
                return
            self.oer_busy = True
            if force:
                self.oer_src.set_text("Senderliste wird geladen …")

            def work():
                try:
                    changed = oer_update(force=force)
                except Exception:
                    changed = False
                GLib.idle_add(self._oer_done, changed, force)

            threading.Thread(target=work, daemon=True).start()

        def _oer_done(self, changed, force):
            self.oer_busy = False
            if changed or force:
                self.rebuild_oer()
            else:
                self.oer_src.set_text(oer_source_text())
            if force:
                if changed:
                    self.say("Senderliste aktualisiert (%d Sender)." % len(oer_channels()))
                else:
                    self.say("Senderliste nicht aktualisiert: %s" % (OER_STATE["error"] or "unverändert"))
            return False

        # ---------- Suche ----------
        def on_entry_changed(self, entry):
            if self.debounce_id:
                GLib.source_remove(self.debounce_id)
                self.debounce_id = 0
            if self._cycling:
                return
            self.recent_idx = -1
            text = entry.get_text().strip()
            if len(text) >= 2 or (not text and self.category):
                self.debounce_id = GLib.timeout_add(600, self._debounced, text)

        def _debounced(self, text):
            self.debounce_id = 0
            if self.entry.get_text().strip() == text:
                self.start_search(text)
            return False

        def on_entry_enter(self):
            q = self.entry.get_text().strip()
            self.recent_idx = -1
            self.remember_query(q)
            self.start_search(q)

        def remember_query(self, q):
            """Letzte RECENT_MAX Suchbegriffe (neueste zuerst, ohne Duplikate)."""
            q = " ".join((q or "").split())
            if len(q) < 2:
                return
            self.recent = [q] + [s for s in self.recent if s.lower() != q.lower()]
            del self.recent[RECENT_MAX:]
            self.settings["recent"] = self.recent
            self._save_settings()

        def on_chip_toggled(self, btn, key):
            if self._chip_lock or not btn.get_active():
                return
            self.category = key
            self._sync_more_btn()
            self._reveal_chip(btn)
            self.start_search(self.entry.get_text())

        def _sync_more_btn(self):
            """"Mehr ▾" zeigt die gewaehlte seltene Kategorie ("E-Sport/Gaming ▾") und ist dann hervorgehoben."""
            more = self.category in CHIPS_MORE
            self.more_btn.set_label((cat_label(self.category) + " ▾") if more else self.MORE_LABEL)
            if more:
                self.more_btn.add_css_class("chip-on")
            else:
                self.more_btn.remove_css_class("chip-on")
            try:
                self.more_pop.popdown()
            except Exception:
                pass

        def on_strict_toggled(self, btn):
            self.set_strict(btn.get_active())

        def set_strict(self, on):
            """Strenger Filter an/aus (Schalter neben den Chips und Menue), speichert und startet die Suche neu."""
            on = bool(on)
            if on == self.strict and self.strict_btn.get_active() == on and self.strict_switch.get_active() == on:
                return
            self.strict = on
            for w in (self.strict_btn, self.strict_switch):
                if w.get_active() != on:
                    w.set_active(on)  # loest set_strict erneut aus, bricht oben ab (Zustand schon gleich)
            self.settings["strict_categories"] = on
            self._save_settings()
            if self.category:
                self.start_search(self.entry.get_text())

        def set_category(self, key, search=True):
            if key not in self.chips:
                key = None
            self.category = key
            self._chip_lock = True
            try:
                self.chips[key].set_active(True)
            finally:
                self._chip_lock = False
            self._sync_more_btn()
            self._reveal_chip(self.chips[key])
            if search:
                self.start_search(self.entry.get_text())

        def cycle_category(self, step):
            keys = list(self.chips)
            i = max(0, min(len(keys) - 1, keys.index(self.category) + step))
            self.set_category(keys[i])

        def _reveal_chip(self, btn):
            if self.category in CHIPS_MORE:  # steckt im Mehr-Menue: dessen Knopf sichtbar machen
                btn = self.more_btn
            try:
                ok, rect = btn.compute_bounds(btn.get_parent())
                adj = self.chip_scroll.get_hadjustment()
                if ok:
                    x, w = rect.get_x(), rect.get_width()
                    if x < adj.get_value():
                        adj.set_value(max(0, x - 8))
                    elif x + w > adj.get_value() + adj.get_page_size():
                        adj.set_value(x + w - adj.get_page_size() + 8)
            except Exception:
                pass

        def clear_search(self):
            """Strg+L: Suchfeld, Kategorie und Ergebnisse leeren."""
            self.stack.set_visible_child_name("results")
            if self.debounce_id:
                GLib.source_remove(self.debounce_id)
                self.debounce_id = 0
            self.search_gen += 1
            self.last_query = ""
            self.recent_idx = -1
            self._set_entry("")
            self.set_category(None, search=False)
            self.results = []
            self.rebuild_results()
            self.res_hint.set_text(self.res_hint_default)
            self.say("")
            self.entry.grab_focus()

        def start_search(self, query, background=False):
            query = (query or "").strip()
            cat = self.category
            if self.debounce_id:
                GLib.source_remove(self.debounce_id)
                self.debounce_id = 0
            if not query and not cat:  # nichts zu suchen: Liste leeren
                if self.results:
                    self.search_gen += 1
                    self.results = []
                    self.rebuild_results()
                    self.res_hint.set_text(self.res_hint_default)
                return
            if len(query) < 2 and not cat:
                self.search_gen += 1  # laufende Anfragen sind damit veraltet
                return
            self.last_query = query
            self.search_gen += 1
            gen = self.search_gen
            if not background:
                self.say("Suche …")
            self.refresh_btn.set_sensitive(False)
            kws = list(self.cat_keywords.get(cat) or []) if cat else []
            strict = self.strict
            allkw = {c: list(v) for c, v in self.cat_keywords.items()}
            excl = {c: list(v) for c, v in self.cat_exclude.items()}

            def work():
                try:
                    info = {}
                    if kws:  # Kategorie-Suche + Stichwort-Regeln in EINEM Durchlauf -> EIN _search_done
                        res = search_with_rules(query, cat, kws, info, strict=strict, all_keywords=allkw, exclude=excl)
                    else:
                        res = search_engine(query, category=cat, info=info)
                        if cat:
                            res, info["hidden"] = filter_category_results(res, cat, strict, allkw, excl)
                    GLib.idle_add(self._search_done, gen, query, cat, res, None, info)
                except Exception as e:
                    GLib.idle_add(self._search_done, gen, query, cat, None, e, None)

            threading.Thread(target=work, daemon=True).start()

        def _search_done(self, gen, query, cat, res, err, info=None):
            # Veraltete Antwort (neuere Anfrage gestartet, anderer Chip oder anderer Suchbegriff): verwerfen
            if gen != self.search_gen or cat != self.category or query != self.last_query:
                return False
            self.refresh_btn.set_sensitive(True)
            if err is not None:
                self.say("Suche fehlgeschlagen.")
                self.show_notice("Suche fehlgeschlagen: %s – läuft die Engine?" % self._errtxt(err), err=True)
                self.check_engine()
                return False
            sel = self.res_list.get_selected_row()
            if cat:  # Sicherheitsnetz: nie Treffer ohne die gewaehlte Kategorie anzeigen (ausser Regel-Treffer)
                res = [x for x in res if has_category(x, cat) or x.get("rule_match")]
            self.results = res
            self.rebuild_results(sel.key if sel is not None else None)
            what = ("„%s“" % query if query else "") + (" in " if query and cat else "") + (cat_label(cat) if cat else "")
            hidden = (info or {}).get("hidden", 0)
            if not res:
                self.res_hint.set_text(empty_hint(cat, query, hidden, self.strict))
            part = ""
            if info and info.get("total"):
                part = " (%d von %d Kanälen%s)" % (info.get("groups", 0), info["total"],
                                                   ", gekürzt" if info.get("cut") else "")
            if info and info.get("rules_active"):
                n_rule = sum(1 for x in res if x.get("rule_match"))
                head = "%d Treffer (%d per Kategorie, %d per Regel)" % (len(res), len(res) - n_rule, n_rule)
                if info.get("kw_extra"):
                    self.show_notice("Es sind %d Stichwörter definiert; pro Suche werden nur die ersten %d genutzt."
                                     % (info["kw_used"] + info["kw_extra"], info["kw_used"]), seconds=10)
                if info.get("rule_err"):
                    self.show_notice("Regel-Suche unvollständig: %s" % info["rule_err"], err=True, seconds=10)
            else:
                head = "%d Treffer" % len(res)
            hid = (" · %d ausgeblendet (%s)" % (hidden, "Streng/Ausschluss" if self.strict else "Ausschluss")) if hidden else ""
            self.say("%s%s für %s%s · %s" % (head, part, what, hid, time.strftime("%H:%M:%S")))
            return False

        @staticmethod
        def _errtxt(e):
            if isinstance(e, urllib.error.URLError):
                return str(e.reason)
            return str(e)

        def refresh_all(self, manual=False):
            if manual and self.stack.get_visible_child_name() == "oer":
                self.refresh_oer(force=True)
                return
            if self.last_query or self.category:
                self.start_search(self.entry.get_text() if manual else self.last_query, background=not manual)
            elif manual:
                self.say("Nichts zu aktualisieren – erst suchen.")
            self.refresh_fav_status()
            self.check_engine()

        # ---------- Auto-Refresh ----------
        def _schedule_auto(self):
            if self.auto_id:
                GLib.source_remove(self.auto_id)
                self.auto_id = 0
            if self.interval_min > 0:
                self.auto_id = GLib.timeout_add_seconds(self.interval_min * 60, self._auto_tick)

        def _auto_tick(self):
            if self.engine_ok:
                self.refresh_all(manual=False)
            return True

        def on_interval_changed(self, spin):
            self.interval_min = int(spin.get_value())
            self.settings["interval_min"] = self.interval_min
            self._save_settings()
            self._schedule_auto()

        # ---------- Abspielen ----------
        def on_row_activated(self, lb, row):
            if row.kind == "cat":
                self.toggle_cat(row.rec["id"])
            else:
                self.play_rec(row.rec)

        def play_rec(self, rec):
            if rec in self.results and self.last_query:
                self.remember_query(self.last_query)
            self.play_id(rec["id"], rec["name"], rec.get("type") or KIND_CONTENT)

        def play_id(self, cid, name="", kind=KIND_CONTENT):
            """Startet im Hintergrund-Thread (Engine-Check/getstream blockieren) und meldet in der Statuszeile:
            "Starte ..." -> "Laeuft im Player" (Prozess lebt nach PLAY_OK_S noch) bzw. Fehlermeldung.
            Danach laeuft bei Typ infohash (Live-Status an) die stat_url-Abfrage bis der Player beendet ist."""
            if self.play_busy:
                self.say("Start läuft bereits …")
                return
            self.play_busy = True
            label = name or cid[:8]
            self.say("Starte „%s“ …" % label)
            live = self.live_enabled

            def work():
                try:
                    pb = play(cid, kind, label, live=live)
                except PlayError as e:
                    GLib.idle_add(self._play_failed, label, str(e), e.code)
                    return
                except Exception as e:
                    GLib.idle_add(self._play_failed, label, "Abspielen nicht möglich: %s" % e, None)
                    return
                GLib.idle_add(self._play_started, pb)
                try:
                    code = pb.proc.wait(timeout=PLAY_OK_S)
                except subprocess.TimeoutExpired:
                    GLib.idle_add(self._play_running, pb)
                    GLib.idle_add(self._play_exited, pb, pb.proc.wait(), False)
                    return
                GLib.idle_add(self._play_exited, pb, code, True)

            threading.Thread(target=work, daemon=True).start()

        def _play_started(self, pb):
            if self.current is not None:
                self.current.cancel = True  # alte Stats-Abfrage beenden
            self.current = pb
            self.live.set_text(fmt_live(pb.name, None))
            self.live.set_visible(True)
            if pb.note:
                self.show_notice(pb.note, seconds=15)
            if pb.stat_url:
                threading.Thread(target=self._stat_loop, args=(pb,), daemon=True).start()
            return False

        def _stat_loop(self, pb):
            """Fragt alle STAT_POLL_S die stat_url ab, solange der Player-Prozess lebt. Ruft sonst NICHTS auf
            (insbesondere nie die command_url)."""
            while not pb.cancel and pb.proc.poll() is None:
                try:
                    st = poll_stat(pb.stat_url)
                    if st:
                        GLib.idle_add(self._live_update, pb, st)
                except Exception:
                    pass
                try:
                    pb.proc.wait(timeout=STAT_POLL_S)
                except subprocess.TimeoutExpired:
                    pass

        def _live_update(self, pb, st):
            if pb is self.current and not pb.cancel and pb.proc.poll() is None:
                self.live.set_text(fmt_live(pb.name, st))
            return False

        def _play_running(self, pb):
            self.play_busy = False
            self.history.add(pb.cid, pb.name, pb.kind)
            self.rebuild_hist()
            self.say("Läuft im Player: „%s“" % pb.name)
            return False

        def _play_failed(self, label, msg, code):
            self.play_busy = False
            self.say("Start fehlgeschlagen: „%s“" % label)
            self.show_notice(msg, err=True, seconds=20)
            if code == 1:
                self.check_engine()
            return False

        @staticmethod
        def _play_error_text(code):
            """Klartext zu einem fehlgeschlagenen Start, aus last-play.log. Kein zweiter Versuch."""
            log = play_log_text()
            if re.search(r"HTTP error 5\d\d|HTTP Error 5\d\d", log):
                m = re.search(r"HTTP [Ee]rror (5\d\d)", log)
                return "Stream nicht verfügbar (Engine %s)" % (m.group(1) if m else "5xx")
            if re.search(r"HTTP [Ee]rror 4\d\d", log):
                return "Stream nicht verfügbar (%s)" % re.search(r"HTTP [Ee]rror (4\d\d)", log).group(1)
            if code == 1 and "FEHLER" in log:
                return "Engine läuft nicht – erst „Engine starten“. %s" % play_log_tail()
            if code == 2 and "FEHLER" in log:
                return "Ungültige Content-ID. %s" % play_log_tail()
            return "Player beendet mit Code %s. %s" % (code, play_log_tail())

        def _play_exited(self, pb, code, early):
            label = pb.name
            if self.current is pb:
                self.live.set_visible(False)
                self.current = None
            pb.cancel = True
            if early:
                self.play_busy = False
            if code == 0:
                if early:  # kurz gestartet und gleich wieder beendet, aber kein Fehler
                    self.history.add(pb.cid, pb.name, pb.kind)
                    self.rebuild_hist()
                self.say("Player beendet.")
                return False
            if early:
                self.say("Start fehlgeschlagen: „%s“" % label)
                self.show_notice(self._play_error_text(code), err=True, seconds=20)
                if code == 1:
                    self.check_engine()
            else:
                self.say("Player beendet (Code %s)." % code)
            return False

        # ---------- Favoriten ----------
        def add_fav(self, rec, cat=""):
            kind = rec.get("type") or KIND_INFOHASH
            self.favs.add(rec["id"], rec["name"], api_name=rec["name"] if kind == KIND_INFOHASH else "",
                          kind=kind, category=cat)
            if rec.get("type") == KIND_INFOHASH and "availability" in rec:
                self.fav_status[rec["id"]] = dict(rec)
            where = ("Kategorie „%s“" % self.favs.category_name(cat)) if cat else UNCATEGORIZED
            self.say("Favorit gespeichert: %s (%s)" % (rec["name"], where))
            self.rebuild_favs()
            self.refresh_stars()

        def toggle_fav(self, rec, anchor=None):
            """★: ist es Favorit, wird er entfernt. Sonst: gibt es eigene Kategorien, fragt ein kleines Popover
            "In Kategorie …" (Standard "Ohne Kategorie", Enter bestaetigt); ohne Kategorien wird direkt gespeichert."""
            if self.favs.has(rec["id"]):
                self.favs.remove(rec["id"])
                self.say("Aus Favoriten entfernt: %s" % rec["name"])
                self.rebuild_favs()
                self.refresh_stars()
            elif self.favs.categories and anchor is not None:
                self.pick_category(anchor, "", lambda cid: self.add_fav(rec, cid))
            else:
                self.add_fav(rec)

        def assign_category(self, rec, anchor):
            cur = self.favs.get(rec["id"])

            def done(cid):
                if self.favs.has(rec["id"]):
                    self.favs.set_category(rec["id"], cid)
                    self.say("„%s“ → %s" % (rec["name"], self.favs.category_name(cid)))
                    self.rebuild_favs()
                else:
                    self.add_fav(rec, cid)

            self.pick_category(anchor, cur["category"] if cur else "", done)

        def pick_category(self, anchor, current, on_pick, heading="In Kategorie …"):
            """Kleines Popover am Anker: Ohne Kategorie, eigene Kategorien, "Neue Kategorie …"."""
            pop = Gtk.Popover()
            pop.set_parent(anchor)
            vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                         margin_start=6, margin_end=6)
            h = Gtk.Label(label=heading, xalign=0, margin_start=6)
            h.add_css_class("hint")
            vb.append(h)
            picked = []

            def finish(*_):
                if pop.get_parent() is not None:
                    pop.unparent()
                if picked:
                    picked.pop(0)()
                return False

            def choose(fn):
                picked.append(fn)
                pop.popdown()
                GLib.idle_add(finish)

            first = cur_btn = None
            for cid, name in [("", UNCATEGORIZED)] + [(c["id"], c["name"]) for c in self.favs.categories]:
                b = Gtk.Button(label=("✓ " if cid == (current or "") else "") + name)
                b.add_css_class("flat")
                b.get_child().set_xalign(0)
                b.connect("clicked", lambda _b, c=cid: choose(lambda: on_pick(c)))
                vb.append(b)
                first = first or b
                if cid == (current or ""):
                    cur_btn = b
            first = cur_btn or first
            nb = Gtk.Button(label="＋ Neue Kategorie …")
            nb.add_css_class("flat")
            nb.get_child().set_xalign(0)
            nb.connect("clicked", lambda _b: choose(lambda: self.new_category_dialog(then=on_pick)))
            vb.append(nb)
            sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                    propagate_natural_width=True, max_content_height=280)
            sc.set_child(vb)
            pop.set_child(sc)
            pop.connect("closed", lambda *_: GLib.idle_add(finish))
            pop.popup()
            if first is not None:
                GLib.idle_add(lambda: (first.grab_focus(), False)[1])
            return pop

        def delete_fav(self, rec):
            idx = next((r.get_index() for r in self._rows(self.fav_list)
                        if r.kind == "fav" and r.rec["id"] == rec["id"]), None)
            self.favs.remove(rec["id"])
            self.rebuild_favs()
            self.refresh_stars()
            if idx is not None:
                nxt = self.fav_list.get_row_at_index(idx) or self.fav_list.get_row_at_index(idx - 1)
                if nxt:
                    self.fav_list.select_row(nxt)
                    nxt.grab_focus()

        def copy_link(self, rec):
            text = link_for(rec)
            try:
                cb = self.get_clipboard()
                try:
                    cb.set_text(text)
                except AttributeError:
                    cb.set(text)
                self.say("Kopiert: %s" % text)
            except Exception as e:
                self.show_notice("Kopieren nicht möglich: %s" % e, err=True)

        def clear_history(self):
            self.history.clear()
            self.rebuild_hist()
            self.say("Verlauf geleert.")

        # ----- Kategorien -----
        def toggle_cat(self, cid):
            if cid in self.collapsed:
                self.collapsed.discard(cid)
            else:
                self.collapsed.add(cid)
            self.settings["collapsed"] = sorted(self.collapsed)
            self._save_settings()
            self.rebuild_favs(keep=("cat", cid), focus=True)

        def move_cat(self, cid, delta):
            if self.favs.move_category(cid, delta):
                self.rebuild_favs(keep=("cat", cid), focus=True)

        def new_category_dialog(self, then=None):
            def ok(vals):
                try:
                    cid = self.favs.add_category(vals[0])
                except ValueError as e:
                    self.show_notice(str(e), err=True)
                    return
                self.say("Kategorie angelegt: %s" % self.favs.category_name(cid))
                self.rebuild_favs(keep=("cat", cid))
                if then:
                    then(cid)

            self._dialog("Neue Kategorie", "Eigene Ordner für Favoriten, z. B. nach Thema oder Anlass.",
                         [("Name der Kategorie", "")], "Anlegen", ok)

        def rename_cat(self, cid):
            def ok(vals):
                try:
                    self.favs.rename_category(cid, vals[0])
                except ValueError as e:
                    self.show_notice(str(e), err=True)
                    return
                self.rebuild_favs(keep=("cat", cid), focus=True)

            self._dialog("Kategorie umbenennen", "", [("Name", self.favs.category_name(cid))], "Umbenennen", ok)

        def delete_cat(self, cid):
            name = self.favs.category_name(cid)
            n = self.favs.delete_category(cid)
            self.collapsed.discard(cid)
            self.say("Kategorie „%s“ gelöscht – %d Favorit(en) jetzt ohne Kategorie." % (name, n))
            self.rebuild_favs()

        # ----- Kategorie-Regeln (Stichwoerter) -----
        def build_rules_dialog(self):
            """Fenster 'Kategorie-Regeln': pro Chip-Kategorie ein Feld mit kommagetrennten Stichwoertern und ein
            zweites Feld 'ausschließen' (Ausschlusswoerter). Gibt (Fenster, {kategorie: Gtk.Entry}, speichern_fn,
            zuruecksetzen_fn) zurueck (noch nicht angezeigt); die Ausschluss-Felder stehen in dlg.excl_entries."""
            dlg = Adw.Window(transient_for=self, modal=True, title="Kategorie-Regeln")
            dlg.set_default_size(420, 620)
            outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            outer.add_css_class("ace-root")
            outer.append(Adw.HeaderBar())
            info = Gtk.Label(wrap=True, xalign=0, margin_start=14, margin_end=14, margin_top=6, margin_bottom=6,
                             label="Ist ein Kategorie-Chip aktiv, erscheinen zusätzlich zu den API-Treffern auch "
                                   "Kanäle, deren Name ein Stichwort (am Wortanfang) enthält – mit dem Badge "
                                   "„Regel“. Das schließt Lücken, wenn das Verzeichnis Kanäle nicht oder falsch "
                                   "einordnet. Pro Suche werden höchstens %d Stichwörter genutzt. Kanäle, deren Name "
                                   "ein Wort im Feld „ausschließen“ enthält, fallen unter diesem Chip weg (egal ob "
                                   "API- oder Regeltreffer). Ein Kanal erscheint per Regel nie unter zwei Chips."
                                   % RULE_MAX_KEYWORDS)
            info.add_css_class("hint")
            outer.append(info)
            grid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=14, margin_end=14,
                           margin_bottom=8)
            entries, excl_entries = {}, {}
            for c in CATEGORY_CHIPS:
                lab = Gtk.Label(label=cat_label(c), xalign=0)
                lab.add_css_class("cat-name")
                e = Gtk.Entry(placeholder_text="z. B. sport, live", text=", ".join(self.cat_keywords.get(c, [])))
                x = Gtk.Entry(placeholder_text="ausschließen: z. B. shop, replay",
                              text=", ".join(self.cat_exclude.get(c, [])))
                entries[c], excl_entries[c] = e, x
                grid.append(lab)
                grid.append(e)
                grid.append(x)
            dlg.excl_entries = excl_entries
            sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
            sc.set_child(grid)
            outer.append(sc)

            def reset(*_):
                for c, e in entries.items():
                    e.set_text(", ".join(DEFAULT_CAT_KEYWORDS.get(c, [])))
                for x in excl_entries.values():
                    x.set_text("")

            def save(*_):
                self.save_cat_keywords({c: parse_keywords(e.get_text()) for c, e in entries.items()},
                                       {c: parse_keywords(x.get_text()) for c, x in excl_entries.items()})
                dlg.close()

            brow = Gtk.Box(spacing=8, margin_start=14, margin_end=14, margin_bottom=12, margin_top=4)
            rb = Gtk.Button(label="Auf Standard zurücksetzen")
            rb.connect("clicked", reset)
            brow.append(rb)
            brow.append(Gtk.Box(hexpand=True))
            cb = Gtk.Button(label="Abbrechen")
            cb.connect("clicked", lambda *_: dlg.close())
            brow.append(cb)
            sb = Gtk.Button(label="Speichern")
            sb.add_css_class("suggested-action")
            sb.connect("clicked", save)
            brow.append(sb)
            outer.append(brow)
            dlg.set_content(outer)
            return dlg, entries, save, reset

        def open_rules_dialog(self):
            dlg = self.build_rules_dialog()[0]
            dlg.present()

        def save_cat_keywords(self, kw, excl=None):
            """Speichert Stichwoerter und Ausschlusswoerter (settings.json "cat_keywords"/"cat_exclude") und startet
            eine laufende Kategorie-Suche neu. excl=None laesst die Ausschlusswoerter unveraendert."""
            self.cat_keywords = {c: list(kw.get(c) or []) for c in CATEGORY_CHIPS}
            self.settings["cat_keywords"] = self.cat_keywords
            if excl is not None:
                self.cat_exclude = {c: list(excl.get(c) or []) for c in CATEGORY_CHIPS}
            self.settings["cat_exclude"] = self.cat_exclude
            self._save_settings()
            self.say("Kategorie-Regeln gespeichert.")
            if self.category:
                self.start_search(self.entry.get_text())

        def _dialog(self, heading, body, fields, ok_label, on_ok):
            Dlg = getattr(Adw, "AlertDialog", None)
            dlg = Dlg(heading=heading, body=body) if Dlg else Adw.MessageDialog(
                transient_for=self, heading=heading, body=body)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            entries = []
            for ph, text in fields:
                e = Gtk.Entry(placeholder_text=ph, text=text, activates_default=True)
                box.append(e)
                entries.append(e)
            dlg.set_extra_child(box)
            dlg.add_response("cancel", "Abbrechen")
            dlg.add_response("ok", ok_label)
            dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
            dlg.set_default_response("ok")
            dlg.set_close_response("cancel")
            dlg.connect("response", lambda d, r: on_ok([e.get_text() for e in entries]) if r == "ok" else None)
            if Dlg:
                dlg.present(self)
            else:
                dlg.present()
            entries[0].grab_focus()

        def add_fav_dialog(self):
            def ok(vals):
                link, name = vals
                try:
                    added = self.favs.add(link, name, api_name="", kind=KIND_CONTENT)
                except ValueError as e:
                    self.show_notice(str(e), err=True)
                    return
                self.say("Favorit hinzugefügt." if added else "Schon in den Favoriten.")
                self.stack.set_visible_child_name("favs")
                self.rebuild_favs()
                self.refresh_fav_status()

            self._dialog("Favorit hinzufügen", "Content-ID (40 Hex) oder acestream://-Link, optional mit Name.",
                         [("Content-ID oder acestream://…", ""), ("Name (optional)", "")], "Hinzufügen", ok)

        def rename_fav(self, rec):
            def ok(vals):
                self.favs.rename(rec["id"], vals[0])
                self.rebuild_favs()

            self._dialog("Favorit umbenennen", rec["id"], [("Name", rec["name"])], "Umbenennen", ok)

        def refresh_fav_status(self):
            """Verfuegbarkeit der Favoriten ueber eine Namenssuche (kein getstream)."""
            if self.fav_status_busy or not self.favs.items or not self.engine_ok:
                return
            self.fav_status_busy = True
            favs = list(self.favs.items)[:MAX_FAVS_STATUS]

            def work():
                out, cache = {}, {}
                for f in favs:
                    if f.get("type") == KIND_URL:  # offizielle Livestreams: keine Engine-Suche
                        continue
                    q = (f.get("api_name") or f["name"]).strip()
                    if len(q) < 2 or (not f.get("api_name") and q.startswith("Stream ")):
                        continue
                    try:
                        if q not in cache:
                            cache[q] = search_engine(q, limit=100, max_pages=2)
                            time.sleep(0.3)
                        hit = next((r for r in cache[q] if r["id"] == f["id"]), None)
                        out[f["id"]] = hit if hit else "missing"
                    except Exception:
                        break
                GLib.idle_add(self._fav_status_done, out)

            threading.Thread(target=work, daemon=True).start()

        def _fav_status_done(self, out):
            self.fav_status_busy = False
            self.fav_status.update(out)
            self.rebuild_favs()
            return False

        # ---------- Engine ----------
        def _poll_engine(self):
            self.check_engine()
            return True

        def check_engine(self):
            def work():
                GLib.idle_add(self._engine_checked, engine_version())
            threading.Thread(target=work, daemon=True).start()

        def _engine_checked(self, v):
            was = self.engine_ok
            self.engine_ok = bool(v)
            self.engine_ver = v
            for c in ("badge-on", "badge-off", "badge-wait"):
                self.badge.remove_css_class(c)
            if v:
                self.badge.set_text("● Engine läuft · v%s" % v)
                self.badge.add_css_class("badge-on")
            else:
                self.badge.set_text("● Engine läuft nicht")
                self.badge.add_css_class("badge-off")
            self.btn_start.set_visible(not v)
            self.btn_stop.set_visible(bool(v))
            if v and was is False and self.favs.items:
                self.refresh_fav_status()
            return False

        def engine_start(self):
            try:
                spawn_detached(START_CMD)
                self.show_notice("Terminal geöffnet – dort sudo-Passwort eingeben und das Terminal offen lassen.",
                                 seconds=20)
            except Exception as e:
                self.show_notice("Start nicht möglich: %s" % e, err=True)

        def engine_stop_confirm(self):
            Dlg = getattr(Adw, "AlertDialog", None)
            body = "Beendet die AceStream-Engine – laufende Streams brechen ab."
            dlg = Dlg(heading="Engine stoppen?", body=body) if Dlg else Adw.MessageDialog(
                transient_for=self, heading="Engine stoppen?", body=body)
            dlg.add_response("cancel", "Abbrechen")
            dlg.add_response("stop", "Stoppen")
            dlg.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.set_default_response("cancel")
            dlg.set_close_response("cancel")

            def resp(d, r):
                if r == "stop":
                    try:
                        spawn_detached(STOP_CMD)
                        self.show_notice("Terminal geöffnet – dort ggf. sudo-Passwort eingeben.", seconds=15)
                    except Exception as e:
                        self.show_notice("Stoppen nicht möglich: %s" % e, err=True)
                    GLib.timeout_add_seconds(4, lambda: (self.check_engine(), False)[1])

            dlg.connect("response", resp)
            if Dlg:
                dlg.present(self)
            else:
                dlg.present()

    class App(Adw.Application):
        def __init__(self):
            super().__init__(application_id="local.AceSearch.Selftest" if selftest_hook else "local.AceSearch")
            self.win = None

        def do_startup(self):
            Adw.Application.do_startup(self)
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            prov = Gtk.CssProvider()
            prov.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        def do_activate(self):
            if not self.win:
                self.win = Win(self)
            if selftest_hook:
                GLib.idle_add(selftest_hook, self)  # Fenster wird im Selbsttest NICHT angezeigt
            else:
                self.win.present()

    return App


def main(argv):
    if "--selftest" in argv:
        return selftest()
    App = run_gui()
    return App().run([argv[0]])


# --------------------------------------------------------------------------
# Selbsttest (ohne sichtbares Fenster, ohne echte Wiedergabe, ohne echte Engine)
# Der Test startet seine eigene Mock-Engine (HTTP-Server auf einem freien Port, 127.0.0.1) und eigene Mock-Player
# in einem Temp-Verzeichnis; ACE_SEARCH_* aus der Umgebung werden dabei ueberschrieben. Er fasst weder die echte
# Engine noch ~/.local/share/ace-search an. Die Mock-Engine zaehlt Aufrufe von command_url/method=stop (Soll: 0).
# --------------------------------------------------------------------------
def selftest():
    import http.server
    import traceback
    global ENGINE_URL, DATA_DIR, FAV_FILE, SETTINGS_FILE, HISTORY_FILE, PLAY_LOG, PLAYER, PLAY_CMD
    global PLAY_OK_S, STAT_POLL_S, OER_CACHE, OER_API, MPV_WRAPPER

    ok = True
    DUMMY = "a" * 40
    HASH = "b" * 40
    HASH2 = "c" * 40
    HASH3 = "d" * 40
    here = Path(__file__).resolve().parent

    def check(name, cond, info=""):
        nonlocal ok
        ok &= bool(cond)
        print(("PASS " if cond else "FAIL ") + name + ((" - " + str(info)) if info != "" else ""), flush=True)

    # ---------------- Testumgebung ----------------
    tmp = Path(tempfile.mkdtemp(prefix="ace-selftest-"))
    DATA_DIR = tmp / "data"
    DATA_DIR.mkdir()
    FAV_FILE, SETTINGS_FILE = DATA_DIR / "favorites.json", DATA_DIR / "settings.json"
    HISTORY_FILE, PLAY_LOG = DATA_DIR / "history.json", DATA_DIR / "last-play.log"
    OER_CACHE = DATA_DIR / "oer-cache.json"
    os.environ["ACE_SEARCH_DATA"] = str(DATA_DIR)  # erbt der Mock-Player
    PLAY_OK_S, STAT_POLL_S = 0.3, 0.3
    mock_player, mock_aceplay, mock_mpv = tmp / "mock-player.sh", tmp / "mock-aceplay.sh", tmp / "mock-mpv.sh"
    body = ('#!/bin/sh\nd="$ACE_SEARCH_DATA"\nprintf \'%s\\n\' "$PFX$*" >> "$d/mock.out"\n'
            'printf \'%s\' "$ACE_TITLE" > "$d/mock.title"\n'
            'case "$MOCK_MODE" in http500) echo "HTTP error 500 Internal Server Error"; exit 1;; esac\n'
            'sleep "${MOCK_SLEEP:-1.5}"\n')
    mock_player.write_text(body)
    mock_aceplay.write_text(body.replace('"$PFX$*"', '"ace-play $*"'))
    mock_mpv.write_text(body.replace('"$PFX$*"', '"mpv-ace $*"'))
    for p in (mock_player, mock_aceplay, mock_mpv):
        p.chmod(0o755)
    PLAYER, PLAY_CMD, MPV_WRAPPER = str(mock_player), str(mock_aceplay), str(mock_mpv)
    out_file, title_file = DATA_DIR / "mock.out", DATA_DIR / "mock.title"

    # ---------------- Mock-Engine ----------------
    now = time.time()

    def mk_item(name, langs, countries, cats, avail, disabled=False):
        import hashlib
        d = {"name": name, "infohash": hashlib.sha1(name.encode()).hexdigest(), "bitrate": 800000,
             "categories": cats, "availability": avail, "availability_updated_at": int(now), "status": 2,
             "disabled": disabled, "countries": countries}
        if langs:
            d["languages"] = langs
        return d

    live_epg = [{"start": str(int(now - 600)), "stop": str(int(now + 1800)), "name": "Testspiel Live",
                 "description": "x"}]
    old_epg = [{"start": str(int(now - 7200)), "stop": str(int(now - 3600)), "name": "Alt"}]
    GROUPS = [
        {"name": "Testkanal Sport DE", "items": [mk_item("Testkanal Sport DE", ["deu"], ["de"], ["sport"], 1)],
         "epg": live_epg},
        {"name": "Testkanal Sport EN", "items": [mk_item("Testkanal Sport EN", ["eng"], ["gb"], ["sport"], 0.9),
                                                 mk_item("Testkanal Sport EN HD", ["eng"], ["gb"], ["sport"], 0.8)]},
        {"name": "Testkanal News DE", "items": [mk_item("Testkanal News DE", ["deu"], ["de"], ["informational", "tv"], 0.7)]},
        {"name": "Testkanal News EN", "items": [mk_item("Testkanal News EN", ["eng"], ["us"], ["informational"], 0.95)]},
        {"name": "Testkanal Film FR", "items": [mk_item("Testkanal Film FR", ["fra"], ["fr"], ["movies"], 0.6)],
         "epg": old_epg},
        {"name": "Testkanal Kinder", "items": [mk_item("Testkanal Kinder", ["deu"], ["at"], ["kids"], 0.8)]},
        {"name": "Testkanal Doku", "items": [mk_item("Testkanal Doku", [], ["ch"], ["documentaries", "exotic_x"], 0.5)]},
        {"name": "Testkanal Aus", "items": [mk_item("Testkanal Aus", ["deu"], ["de"], ["sport"], 0, disabled=True)]},
        # Regel-Tests: Name enthaelt ein generisches Stichwort, die API ordnet sie aber nicht (richtig) ein
        {"name": "Regeltest Sport Extra", "items": [mk_item("Regeltest Sport Extra", ["deu"], ["de"], [], 0.5)]},
        {"name": "Regeltest Xsportx", "items": [mk_item("Regeltest Xsportx", ["deu"], ["de"], [], 0.5)]},
        {"name": "Regeltest Kino Allerlei", "items": [mk_item("Regeltest Kino Allerlei", ["deu"], ["de"],
                                                              ["entertaining"], 0.4)]},
    ]
    ZAPP_MOCK = {
        "das_erste": {"streamUrl": "https://mock.invalid/erste/master.m3u8", "name": "Das Erste"},
        "zdf": {"streamUrl": "https://mock.invalid/zdf/master.m3u8", "name": "ZDF"},
        "arte": {"streamUrl": "https://mock.invalid/arte/index.m3u8", "name": "ARTE"},
        "wdr": {"streamUrl": "https://mock.invalid/wdr/master.m3u8", "name": "WDR"},
        "ndr_nds": {"streamUrl": "https://mock.invalid/ndr/master.m3u8", "name": "NRD Niedersachsen"},
        "neu_kanal": {"streamUrl": "https://mock.invalid/neu/master.m3u8", "name": "Neuer\u00adSender"},
        "parlamentsfernsehen_1": {"streamUrl": "https://mock.invalid/p1/playlist.m3u8", "name": "Parlament 1"},
        "kaputt": {"name": "ohne URL"},
        "Bad Key": {"streamUrl": "https://mock.invalid/x.m3u8", "name": "Ungueltiger Schluessel"},
        "ftp_kanal": {"streamUrl": "ftp://mock.invalid/x", "name": "FTP"},
    }

    class MockState:
        hits = []
        extra = []         # zusaetzliche Gruppen fuer einzelne Testbloecke (sonst leer)
        json_fail = False
        zapp_hits = []
        zapp_mode = "ok"   # ok | fail (HTTP 503) | bad (kein Kanal-Objekt)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, code=200):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/zapp/"):  # Mock der Zapp-API (zaehlt NICHT als Engine-Aufruf)
                MockState.zapp_hits.append(self.path)
                if MockState.zapp_mode == "fail":
                    return self._send({"error": "mock"}, 503)
                return self._send(ZAPP_MOCK if MockState.zapp_mode == "ok" else ["kein", "objekt"])
            MockState.hits.append(self.path)
            u = urllib.parse.urlsplit(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query, keep_blank_values=True).items()}
            port = self.server.server_address[1]
            if u.path == "/webui/api/service":
                return self._send({"result": {"version": "3.2.11-mock"}, "error": None})
            if u.path == "/server/api":
                if q.get("method") == "get_api_access_token":
                    return self._send({"result": {"token": "T0K"}, "error": None})
                if q.get("method") == "search":
                    if q.get("token") != "T0K":
                        return self._send({"result": {"total": 0, "results": []}, "error": None})
                    groups = []
                    for g in GROUPS + MockState.extra:
                        its = [i for i in g["items"] if (not q.get("category") or q["category"] in i["categories"])
                               and q.get("query", "").lower() in i["name"].lower()]
                        if its:
                            gg = {"name": g["name"], "items": its}
                            if g.get("epg") and q.get("show_epg") == "1":
                                gg["epg"] = g["epg"]
                            groups.append(gg)
                    ps, pg = int(q.get("page_size", 10)), int(q.get("page", 0))
                    return self._send({"result": {"total": len(groups), "time": 0.01,
                                                  "results": groups[pg * ps:(pg + 1) * ps]}, "error": None})
            if u.path == "/ace/getstream":
                if q.get("format") == "json":
                    if MockState.json_fail:
                        return self._send({"response": None, "error": "mock: Fehler"}, 500)
                    base = "/ace/r/SID/" + q.get("infohash", "")[:6]
                    return self._send({"response": {
                        "playback_url": "http://127.0.0.1:%d%s" % (port, base),
                        # fremder Host: die App muss auf ENGINE_URL umsetzen (sonst bleibt stat leer)
                        "stat_url": "http://203.0.113.9:1/ace/stat/SID/x",
                        "command_url": "http://127.0.0.1:%d/ace/cmd/SID/x" % port,
                        "is_live": 1, "is_encrypted": 0}, "error": None})
                self.send_response(302)
                self.send_header("Location", "/ace/r/plain")
                self.end_headers()
                return
            if u.path.startswith("/ace/stat/"):
                return self._send({"response": {"peers": 12, "speed_down": 1100, "speed_up": 5, "status": "dl",
                                                "downloaded": 1234}, "error": None})
            self._send({"response": None, "error": "mock: unbekannt"}, 404)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ENGINE_URL = "http://127.0.0.1:%d" % srv.server_address[1]
    OER_API = ENGINE_URL + "/zapp/v1/channelInfoList"

    def hits(sub):
        return [h for h in MockState.hits if sub in h]

    def json_starts():
        """getstream-Aufrufe mit format=json (nicht zu verwechseln mit dem get_version-Check)."""
        return [h for h in MockState.hits if h.startswith("/ace/getstream") and "format=json" in h]

    # ---------------- reine Funktionen ----------------
    check("Mock-Engine/Testdaten isoliert", str(DATA_DIR).startswith(tempfile.gettempdir())
          and ENGINE_URL.startswith("http://127.0.0.1:"), (ENGINE_URL, DATA_DIR))
    check("parse_content_id Roh-ID", parse_content_id(DUMMY.upper()) == DUMMY)
    check("parse_content_id acestream://", parse_content_id("acestream://" + DUMMY + "?x=1") == DUMMY)
    check("parse_content_id ungueltig", parse_content_id("hallo") is None and parse_content_id("a" * 39) is None
          and parse_content_id("a" * 41) is None)
    check("Ampel", [avail_level(x) for x in (1, 0.8, 0.5, 0.3, 0.1, None)] == ["hi", "hi", "mid", "mid", "lo", None])

    def mk(name, langs, countries, avail=1, disabled=False):
        return _norm_item({"name": name, "infohash": "%040x" % (abs(hash(name)) % (16 ** 40)),
                           "languages": langs, "countries": countries, "availability": avail,
                           "disabled": disabled})

    syn = [mk("F-fra", ["fra"], ["fr"]), mk("E-eng", ["EN"], ["us"]), mk("L-leer", [], []),
           mk("CH-fra", ["fra"], ["ch"]), mk("AT-ohne", [], ["AT"]), mk("D-deu", ["DEU", ""], ["de"]),
           mk("D-ger-tot", ["ger"], ["de"], disabled=True), mk("D-schwach", ["deu"], ["de"], avail=0.2),
           mk("D-stark", ["deu"], ["ch"], avail=1), mk("E-schwach", ["eng"], [], avail=0.1),
           mk("X-leerstr", [""], [""])]
    order = [x["name"] for x in sort_results(syn)]
    expect = ["AT-ohne", "D-deu", "D-stark", "D-schwach", "E-eng", "E-schwach",
              "CH-fra", "F-fra", "L-leer", "X-leerstr", "D-ger-tot"]
    check("Sortierung Deutsch > Englisch > Rest, Deaktivierte zuletzt", order == expect, order)
    byname = {x["name"]: x for x in syn}
    check("Gruppen: CH+fra nicht deutsch, AT ohne Sprache deutsch, leere Strings ignoriert",
          [lang_group(byname[n]) for n in ("CH-fra", "AT-ohne", "D-deu", "E-eng", "L-leer", "X-leerstr")] == [2, 0, 0, 1, 2, 2])
    check("Badges", [lang_badge(byname[n]) for n in ("AT-ohne", "D-deu", "D-stark", "E-eng", "CH-fra", "F-fra", "L-leer")]
          == ["AT", "DE", "CH", "EN", "FR", "FR", None])
    gc = group_counts(syn)
    check("Gruppenzaehler fuer Header", gc == {0: 4, 1: 2, 2: 4, 3: 1}, gc)

    # Kategorien-Anzeige
    check("Kategorie-Labels deutsch, unbekannt roh",
          [cat_label(c) for c in ("informational", "sport", "movies", "kids", "music", "documentaries",
                                  "entertaining", "regional", "exotic_x")]
          == ["Nachrichten/Info", "Sport", "Filme", "Kinder", "Musik", "Doku", "Unterhaltung", "Regional", "exotic_x"])
    check("Chips nur aus API-Werten", all(c in CATEGORY_LABELS for c in CATEGORY_CHIPS)
          and len(set(CATEGORY_CHIPS)) == len(CATEGORY_CHIPS))

    # EPG
    raw = [{"start": "100", "stop": "200", "name": "A   B"}, {"start": "x", "stop": "1", "name": "kaputt"},
           {"start": "300", "stop": "250", "name": "rueckwaerts"}, "kein dict", {"start": "200", "stop": "400", "name": ""}]
    ce = _clean_epg(raw)
    check("EPG bereinigt (Leerzeichen, ungueltige Eintraege raus)", ce == [{"name": "A B", "start": 100, "stop": 200}], ce)
    check("epg_now: laufend / danach / davor", epg_now(ce, 150) == ("A B", 200) and epg_now(ce, 250) is None
          and epg_now(ce, 50) is None and epg_now(None) is None)
    el = epg_line(ce, 150)
    check("epg_line 'Jetzt: <Sendung> · bis HH:MM'", re.fullmatch(r"Jetzt: A B · bis \d\d:\d\d", el or "") is not None, el)
    long_line = epg_line([{"name": "N" * 100, "start": 0, "stop": 10}], 5)
    check("epg_line kuerzt lange Namen", long_line is not None and len(long_line) < 90 and "…" in long_line, long_line)
    # Live-Anzeige / Formate
    check("fmt_live mit Stats", fmt_live("X", {"peers": 12, "speed_down": 1100}) == "▶ X · Peers 12 · ↓ 1,1 MB/s",
          fmt_live("X", {"peers": 12, "speed_down": 1100}))
    check("fmt_live ohne Stats / kaputte Stats", fmt_live("X", None) == "▶ X" and fmt_live("X", {"peers": "?"}) == "▶ X")
    check("fmt_speed", [fmt_speed(v) for v in (850, 1100, None, "x")] == ["850 KB/s", "1,1 MB/s", None, None])
    t0 = time.time()
    check("fmt_when heute/gestern/aelter", fmt_when(t0, t0).startswith("heute ") and fmt_when(t0 - 86400, t0).startswith("gestern ")
          and re.fullmatch(r"\d\d\.\d\d\. \d\d:\d\d", fmt_when(t0 - 5 * 86400, t0)) is not None)
    check("Kopier-Text: Infohash als 'infohash:<h>', Content-ID als acestream://",
          link_for({"id": HASH, "type": "infohash"}) == "infohash:" + HASH
          and link_for({"id": DUMMY, "type": "content_id"}) == "acestream://" + DUMMY)
    check("Engine-URLs werden auf ENGINE_URL umgesetzt (nie fremder Host)",
          _engine_local("http://10.9.9.9:1/ace/x?a=1&b=2") == ENGINE_URL + "/ace/x?a=1&b=2"
          and _engine_local("file:///etc/passwd") is None and _engine_local(None) is None)

    # Favoriten: Rueckwaertskompatibilitaet + Migration
    tmpf = tmp / "old" / "favorites.json"
    tmpf.parent.mkdir()
    old = [{"name": "A", "id": "1" * 40, "api_name": "A", "note": "bleibt"}, {"name": "B", "id": "2" * 40, "api_name": ""},
           {"name": "C", "id": "3" * 40}, "kaputt", {"id": "xyz"},
           {"name": "D", "id": "4" * 40, "type": "content_id", "api_name": "D"}]
    json.dump(old, open(tmpf, "w"))
    fl = Favorites(tmpf)
    check("alte favorites.json (Liste ohne Typ/Kategorie) laedt",
          [f["type"] for f in fl.items] == ["infohash", "content_id", "content_id", "content_id"]
          and all(f["category"] == "" for f in fl.items) and fl.categories == [], [f["type"] for f in fl.items])
    check("Laden schreibt nichts (Datei bleibt Format 1)", json.load(open(tmpf)) == old)
    check("Zusatzfelder bleiben erhalten", fl.items[0].get("note") == "bleibt")
    fl.add("5" * 40, "N", api_name="N", kind="infohash")
    check("Typ wird gespeichert", Favorites(tmpf).items[-1]["type"] == "infohash")
    d = json.load(open(tmpf))
    check("nach dem Speichern Format 2 mit allen alten Eintraegen (kein Datenverlust)",
          isinstance(d, dict) and d["version"] == 2 and [f["id"] for f in d["favorites"]] == ["1" * 40, "2" * 40, "3" * 40, "4" * 40, "5" * 40]
          and d["categories"] == [] and d["favorites"][0].get("note") == "bleibt")
    ca = fl.add_category("  Abends   Sport ")
    cb = fl.add_category("Kinder")
    check("Kategorie anlegen (Name bereinigt, IDs)", fl.categories[0]["name"] == "Abends Sport" and ca != cb and len(fl.categories) == 2)
    for bad in ("", "  ", "abends sport", UNCATEGORIZED):
        try:
            fl.add_category(bad)
            check("Kategorie '%s' wird abgelehnt" % bad, False)
        except ValueError:
            check("Kategorie '%s' wird abgelehnt" % bad, True)
    fl.set_category("1" * 40, ca)
    fl.set_category("2" * 40, cb)
    fl.set_category("3" * 40, "gibt-es-nicht")
    fl2 = Favorites(tmpf)
    check("Zuordnung gespeichert/nachladbar, ungueltige Kategorie -> ohne",
          [f["category"] for f in fl2.items] == [ca, cb, "", "", ""] and [c["name"] for c in fl2.categories] == ["Abends Sport", "Kinder"])
    g = fl2.grouped()
    check("grouped: Kategorien in Reihenfolge, Ohne Kategorie zuletzt",
          [(c["name"] if c else None, len(it)) for c, it in g] == [("Abends Sport", 1), ("Kinder", 1), (None, 3)],
          [(c["name"] if c else None, len(it)) for c, it in g])
    check("Kategorie nach oben/unten", fl2.move_category(cb, -1) and [c["id"] for c in Favorites(tmpf).categories] == [cb, ca]
          and not fl2.move_category(cb, -1) and not fl2.move_category("zzz", 1))
    fl2.rename_category(ca, "Abend")
    check("Kategorie umbenennen", Favorites(tmpf).category_name(ca) == "Abend")
    try:
        fl2.rename_category(ca, "kinder")
        check("Umbenennen auf vorhandenen Namen abgelehnt", False)
    except ValueError:
        check("Umbenennen auf vorhandenen Namen abgelehnt", True)
    n = fl2.delete_category(cb)
    fl3 = Favorites(tmpf)
    check("Kategorie loeschen: Favoriten bleiben (ohne Kategorie)", n == 1 and len(fl3.items) == 5
          and fl3.get("2" * 40)["category"] == "" and [c["id"] for c in fl3.categories] == [ca])
    # Format 1b: {"favorites": [...]} ohne Kategorien + Eintrag mit unbekannter Kategorie
    json.dump({"favorites": [{"name": "Z", "id": "6" * 40, "type": "infohash", "category": "weg"}]}, open(tmpf, "w"))
    f4 = Favorites(tmpf)
    check("Dict-Format ohne Kategorien laedt, unbekannte Kategorie -> ohne", len(f4.items) == 1 and f4.items[0]["category"] == "")
    open(tmpf, "w").write("{kaputt")
    f5 = Favorites(tmpf)
    check("kaputte Datei: leer geladen, Kopie *.corrupt-* gesichert",
          f5.items == [] and any(p.name.startswith("favorites.json.corrupt-") for p in tmpf.parent.iterdir()))

    # Verlauf
    hf = tmp / "old" / "history.json"
    h = History(hf)
    for i in range(17):
        h.add("%040x" % (i + 1), "Stream %d" % i, "infohash", ts=1000 + i)
    check("Verlauf: max. 15, neueste zuerst", len(h.items) == 15 and h.items[0]["name"] == "Stream 16"
          and h.items[-1]["name"] == "Stream 2", [x["name"] for x in h.items][:3])
    h.add("%040x" % 5, "Stream 4 neu", "content_id", ts=5000)
    check("Verlauf: erneuter Start rueckt nach oben, keine Duplikate",
          h.items[0]["name"] == "Stream 4 neu" and len(h.items) == 15 and len({x["id"] for x in h.items}) == 15)
    h2 = History(hf)
    check("Verlauf nachladbar (name/id/type/ts)", h2.items == h.items and set(h2.items[0]) == {"name", "id", "type", "ts"})
    h2.clear()
    check("Verlauf leeren", History(hf).items == [] and json.load(open(hf)) == [])
    json.dump([{"id": "x"}, "y", {"id": DUMMY, "name": "Ok", "ts": "kaputt"}], open(hf, "w"))
    check("Verlauf toleriert kaputte Eintraege", [x["id"] for x in History(hf).items] == [DUMMY])

    # Wrapper-Trockenlauf: ACE_TITLE kommt als EIN Argument an
    patch_dir = here.parent / "launcher"  # Repo-Layout: app/ neben launcher/
    if not patch_dir.is_dir():
        patch_dir = here / "launcher"
    for wrapper, fake, opts in (("mpv-ace.sh", "mpv", ["--force-media-title="]),
                                ("vlc-ace.sh", "vlc", ["--meta-title=", "--video-title="])):
        if not (patch_dir / wrapper).exists():
            print("INFO %s nicht gefunden (launcher/ fehlt in dieser Installation), Trockenlauf uebersprungen" % wrapper)
            continue
        fb = tmp / ("fakebin-" + fake)
        fb.mkdir()
        (fb / fake).write_text('#!/bin/sh\nfor a in "$@"; do printf \'%s\\n\' "$a"; done > "$FAKE_OUT"\n')
        (fb / fake).chmod(0o755)
        title = 'Test Sport News & "Co" $HOME `x` ä'
        for with_title in (True, False):
            env = dict(os.environ, PATH=str(fb) + os.pathsep + os.environ["PATH"], FAKE_OUT=str(tmp / "fake.out"))
            env.pop("ACE_TITLE", None)
            if with_title:
                env["ACE_TITLE"] = title
            subprocess.run(["sh", str(patch_dir / wrapper), "http://127.0.0.1:1/x"], env=env, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            args = (tmp / "fake.out").read_text().splitlines()
            if with_title:
                check("%s: ACE_TITLE als EIN Argument je Option (%s)" % (wrapper, " ".join(opts)),
                      all(o + title in args for o in opts) and args[-1] == "http://127.0.0.1:1/x", [a for a in args if title in a])
            else:
                check("%s: ohne ACE_TITLE unveraendert (kein Titel-Argument)" % wrapper,
                      not any(a.startswith(tuple(opts)) for a in args) and args[-1] == "http://127.0.0.1:1/x")


    # ---------------- Stichwort-Regeln: reine Funktionen ----------------
    check("parse_keywords: klein, ohne Duplikate/Kurzes/Leeres, Komma/Semikolon/Zeilenumbruch",
          parse_keywords(" Sport, live ;SPORT,\n a, ,Doku  Neu") == ["sport", "live", "doku neu"], parse_keywords(" Sport, live ;SPORT,\n a, ,Doku  Neu"))
    check("Defaults: alle Chips vorhanden, nur generische Woerter",
          set(default_keywords()) == set(CATEGORY_CHIPS) and default_keywords()["sport"] == ["sport"]
          and default_keywords()["informational"] == ["news", "nachrichten"]
          and default_keywords()["movies"] == ["cinema", "kino", "film", "movie"]
          and default_keywords()["kids"] == ["kids", "kinder"] and default_keywords()["music"] == ["music", "musik"]
          and default_keywords()["documentaries"] == ["doku", "documentary"] and default_keywords()["religion"] == [])
    check("settings.json ohne cat_keywords (alt) -> Standardwerte; Teil-Eintrag -> Rest Standard; geleert bleibt leer",
          load_cat_keywords({"interval_min": 5}) == default_keywords()
          and load_cat_keywords({"cat_keywords": {"sport": ["Live", "x"], "kids": []}})["sport"] == ["live"]
          and load_cat_keywords({"cat_keywords": {"sport": ["Live"], "kids": []}})["kids"] == []
          and load_cat_keywords({"cat_keywords": {"sport": ["Live"]}})["music"] == ["music", "musik"]
          and load_cat_keywords({"cat_keywords": "kaputt"}) == default_keywords()
          and load_cat_keywords({"cat_keywords": {"sport": "a, bb"}})["sport"] == ["bb"])
    check("name_has_word: Wortanfang, case-insensitiv",
          name_has_word("Sport 1 HD", "sport") and name_has_word("Das große SPORT-Studio", "sport")
          and name_has_word("Kanal Sportschau", "sport") and not name_has_word("Motorsport TV", "sport")
          and not name_has_word("", "sport") and not name_has_word("Sport", ""))
    check("name_matches_query: alle Woerter als Teilstring",
          name_matches_query("Regeltest Sport Extra", "sport extra") and name_matches_query("X", "")
          and not name_matches_query("Regeltest Sport", "extra"))

    # ---------------- Strenger Filter, Ausschluss, Doppel-Regeln, Chip-Layout: reine Funktionen ----------------
    def itm(name, cats):
        return {"name": name, "categories": cats}

    _kw = default_keywords()
    check("Hauptkategorie: eine Genre-Kategorie zaehlt voll (tv/regional/Leerstrings/Dubletten ignoriert)",
          main_category(itm("X", ["tv", "movies", "", "movies"])) == "movies"
          and main_category(itm("X", ["regional", "tv", " "])) is None and main_category(itm("X", [])) is None
          and genre_cats(itm("X", ["TV", "Sport ", "regional"])) == ["sport"])
    check("Hauptkategorie bei mehreren: spezifischste nach MAIN_RANK, unabhaengig von der API-Reihenfolge",
          main_category(itm("Abc", ["movies", "documentaries"])) == "documentaries"
          and main_category(itm("Abc", ["documentaries", "movies"])) == "documentaries"
          and main_category(itm("Abc", ["movies", "educational"])) == "educational"
          and main_category(itm("Abc", ["entertaining", "movies"])) == "entertaining"
          and main_category(itm("Abc", ["regional", "movies", "sport"])) == "sport"
          and main_category(itm("Abc", ["movies", "exotic_x"])) == "exotic_x")
    check("Hauptkategorie: Stichwort-Treffer im Namen stuetzt eine Kategorie (vor MAIN_RANK)",
          main_category(itm("Kino Magazin", ["documentaries", "movies"]), _kw) == "movies"
          and main_category(itm("Magazin", ["documentaries", "movies"]), _kw) == "documentaries"
          and main_category(itm("Kino Doku", ["documentaries", "movies"]), _kw) == "documentaries")
    check("Strenger Filter: Item mit Filme+Doku zaehlt nur unter Doku; nicht-streng unter beiden",
          counts_for_category(itm("Abc", ["movies", "documentaries"]), "documentaries", True)
          and not counts_for_category(itm("Abc", ["movies", "documentaries"]), "movies", True)
          and counts_for_category(itm("Abc", ["movies", "documentaries"]), "movies", False)
          and counts_for_category(itm("Abc", ["tv", "movies"]), "movies", True)
          and not counts_for_category(itm("Abc", ["sport"]), "movies", False))
    check("Strenger Filter laesst Zusatz-Etiketten (regional) unberuehrt",
          counts_for_category(itm("Abc", ["regional", "movies"]), "regional", True))
    _ex = {"movies": ["shop"], "sport": []}
    check("Ausschlusswoerter: Wortanfang, wirken auch ohne strict, nehmen die Kategorie aus der Hauptkategorie-Wahl",
          name_excluded("Teleshop Film", ["shop"]) is False and name_excluded("Shop Film", ["shop"])
          and not counts_for_category(itm("Shop Film", ["movies"]), "movies", False, None, _ex)
          and counts_for_category(itm("Film Plus", ["movies"]), "movies", False, None, _ex)
          and main_category(itm("Shop Abc", ["movies", "educational"]), None, {"educational": ["shop"]}) == "movies"
          and main_category(itm("Shop Abc", ["movies"]), None, {"movies": ["shop"]}) is None)
    check("Doppel-Regel: staerkerer Treffer gewinnt (ganzes Wort > Wortanfang, dann weiter vorn, dann Rang)",
          rule_winner(itm("Doku Film Nacht", []), _kw) == "documentaries"
          and rule_winner(itm("Film Doku Nacht", []), _kw) == "movies"
          and rule_winner(itm("Kinofilm Doku", []), _kw) == "documentaries"
          and rule_winner(itm("Abc", []), _kw) is None)
    check("Doppel-Regel: API-Hauptkategorie vor Regel; Ausschlusswort nimmt den Chip aus der Wahl",
          rule_winner(itm("Doku Film", ["movies"]), _kw) == "movies"
          and rule_winner(itm("Doku Film", []), _kw, {"documentaries": ["doku"]}) == "movies")
    check("rule_applies: streng nur ohne eigene Genre-Kategorie (regional zaehlt nicht), nicht-streng wie frueher",
          rule_applies(itm("Sport Extra", []), "sport", _kw, None, True)
          and rule_applies(itm("Sport Extra", ["regional"]), "sport", _kw, None, True)
          and not rule_applies(itm("Kino Allerlei", ["entertaining"]), "movies", _kw, None, True)
          and rule_applies(itm("Kino Allerlei", ["entertaining"]), "movies", _kw, None, False)
          and not rule_applies(itm("Doku Film", []), "movies", _kw, None, False))
    _kept, _hid = filter_category_results([itm("A1", ["movies", "sport"]), dict(itm("A2", ["kids"]), rule_match=True),
                                           itm("A3", ["movies"])], "movies", True)
    check("filter_category_results: Regeltreffer bleiben, Rest streng, Anzahl ausgeblendet",
          [x["name"] for x in _kept] == ["A2", "A3"] and _hid == 1, (_kept, _hid))
    check("also_categories: ohne tv und Referenz, hoechstens 3, Anzeigenamen",
          also_categories(itm("Abc", ["tv", "movies", "documentaries", "entertaining"]), "movies")
          == ["Doku", "Unterhaltung"]
          and also_categories(itm("Abc", ["movies", "documentaries", "sport", "kids", "music"]), "movies") == ["Doku", "Sport", "Kinder"]
          and also_categories(itm("Abc", ["movies", "documentaries"])) == ["Filme"] and also_categories(itm("Abc", ["tv"])) == []
          and also_categories(itm("Abc", ["sport"]), "sport") == [])
    check("Einstellungen abwaertskompatibel: ohne strict_categories AN, ohne cat_exclude leer, Muell ignoriert",
          load_strict({}) is True and load_strict({"strict_categories": False}) is False
          and load_strict({"strict_categories": "nein"}) is True and load_strict({"strict_categories": True}) is True
          and load_cat_exclude({}) == {c: [] for c in CATEGORY_CHIPS}
          and load_cat_exclude({"cat_exclude": {"movies": ["Shop", "x"], "sport": "a, bb"}})["movies"] == ["shop"]
          and load_cat_exclude({"cat_exclude": {"movies": ["Shop"]}})["sport"] == []
          and load_cat_exclude({"cat_exclude": "kaputt"}) == {c: [] for c in CATEGORY_CHIPS})
    check("Standard-Ausschlusswoerter: keine (die Analyse zeigte keine wortbasierten Fehltreffer)",
          not any(load_cat_exclude({}).values()))
    check("Chip-Layout: Hauptreihe kompakt, seltene hinter Mehr, Games umbenannt mit Erklaer-Tooltip",
          CHIPS_MAIN == ["sport", "informational", "movies", "documentaries", "entertaining", "kids", "music"]
          and "cyber_games" in CHIPS_MORE and set(CHIPS_MORE).isdisjoint(CHIPS_MAIN)
          and CATEGORY_CHIPS == CHIPS_MAIN + CHIPS_MORE and cat_label("cyber_games") == "E-Sport/Gaming"
          and CHIP_TIPS["cyber_games"] == "Kanäle, die das AceStream-Verzeichnis als Gaming/E-Sport einordnet (cyber_games)")
    check("Leer-Hinweise: Games erklaert, Chip mit Ausblendung nennt Streng, mit Suchbegriff knapp",
          "cyber_games" in empty_hint("cyber_games") and "Sport" in empty_hint("cyber_games")
          and "Streng" in empty_hint("movies", "", 5, True) and "5" in empty_hint("movies", "", 5, True)
          and "Ausschlusswort" in empty_hint("movies", "", 2, False)
          and empty_hint("movies", "abc") == "Keine Treffer für „abc“ in Filme." and "abc" in empty_hint(None, "abc")
          and "Stichwörter" in empty_hint("religion"))

    # ---------------- Oeffentlich-Rechtliche: Parser, Cache, Fallback ----------------
    pc = parse_oer(ZAPP_MOCK)
    check("parse_oer: gueltige Sender, sortiert, Tippfehler korrigiert, weicher Bindestrich raus, DW-Zusatz",
          [c["name"] for c in pc] == ["Das Erste", "ZDF", "ARTE", "DW (English)", "NDR Niedersachsen", "WDR", "NeuerSender"],
          [c["name"] for c in pc])
    check("parse_oer: ids oer:<key>, Typ url, Gruppen; unbekannte Kennung -> weitere; Parlamentsfernsehen/ungueltige weg",
          [c["id"] for c in pc] == ["oer:das_erste", "oer:zdf", "oer:arte", "oer:dw_english", "oer:ndr_nds", "oer:wdr", "oer:neu_kanal"]
          and all(c["type"] == "url" for c in pc) and [c["group"] for c in pc]
          == ["national", "national", "national", "international", "dritte", "dritte", "weitere"], [c["group"] for c in pc])
    bad_ok = True
    for bad in (["x"], {"a": {"name": "A", "streamUrl": "https://x/1.m3u8"}}, {}, None):
        try:
            parse_oer(bad)
            bad_ok = False
        except ValueError:
            pass
    check("parse_oer: kein Objekt / zu wenige Sender -> ValueError", bad_ok)
    fb = oer_fallback()
    check("Fallback-Liste: >= 25 Sender, eindeutige IDs, nur https-HLS, enthaelt Das Erste/ZDF/arte/phoenix/WDR/DW",
          len(fb) >= 25 and len({c["id"] for c in fb}) == len(fb)
          and all(c["url"].startswith("https://") and ".m3u8" in c["url"] for c in fb)
          and {"oer:das_erste", "oer:zdf", "oer:arte", "oer:phoenix", "oer:wdr", "oer:dw_english"} <= {c["id"] for c in fb}, len(fb))
    check("Fallback-Liste: Dritte, ONE, KiKA, tagesschau24, ZDFinfo, ZDFneo, 3sat, ARD-alpha vorhanden",
          {"oer:br_sued", "oer:hr", "oer:mdr_sachsen", "oer:ndr_nds", "oer:rbb_berlin", "oer:sr", "oer:swr_bw", "oer:wdr",
           "oer:one", "oer:kika", "oer:tagesschau24", "oer:zdf_info", "oer:zdf_neo", "oer:dreisat", "oer:ard_alpha"}
          <= {c["id"] for c in fb})
    # Negativtest: keine Privat-/Bezahlsender in Default-Strukturen und eingebauter Fallback-Liste
    _brands = ["dazn", "sky", "rtl", "prosieben", "pro7", "sat.1", "sat1", "vox", "kabel", "sport1", "eurosport",
               "netflix", "amazon", "disney", "magenta", "joyn", "tele5", "nitro", "n-tv", "ntv", "bild", "welt",
               "premiere", "espn", "bein", "canal"]
    _scan = [w for ws in DEFAULT_CAT_KEYWORDS.values() for w in ws] + list(CATEGORY_LABELS.values()) \
        + [str(x) for t in OER_FALLBACK + OER_EXTRA for x in (t[0], t[1])] + list(OER_NAME_FIX.values())
    _hits = [(b, w) for b in _brands for w in _scan if re.search(r"(?<![a-z0-9])" + re.escape(b) + r"(?![a-z0-9])", w.lower())]
    check("Negativtest: keine Privat-/Bezahlsender in Default-Stichwoertern, Labels und Fallback-Liste", not _hits, _hits)
    check("Fallback-Liste: nur offizielle Sender-/CDN-Hosts (kein Dritthoster)",
          all(re.search(r"(ard|zdf|akamaized|br\.de|ndr\.de|hr-live|dw)", urllib.parse.urlsplit(c["url"]).netloc) for c in fb),
          [urllib.parse.urlsplit(c["url"]).netloc for c in fb if not re.search(r"(ard|zdf|akamaized|br\.de|ndr\.de|hr-live|dw)", urllib.parse.urlsplit(c["url"]).netloc)])
    # Cache / Aktualisierung (alles gegen die Mock-Zapp-API)
    check("OER_API zeigt im Test auf den Mock (kein echtes Netz)", OER_API.startswith("http://127.0.0.1:") and OER_CACHE.parent == DATA_DIR)
    OER_STATE.update(channels=None, source="fallback", fetched=0.0, error="")
    oer_load()
    check("ohne Cache: eingebaute Fallback-Liste", OER_STATE["source"] == "fallback" and len(oer_channels()) == len(fb))
    check("oer_update: laedt von der Quelle, schreibt oer-cache.json",
          oer_update() is True and OER_STATE["source"] == "live" and len(MockState.zapp_hits) == 1 and OER_CACHE.exists()
          and [c["name"] for c in oer_channels()][0] == "Das Erste", MockState.zapp_hits)
    cj = json.load(open(OER_CACHE))
    check("oer-cache.json: version, fetched, source, channels[{id,type,name,url,group}]",
          cj["version"] == 1 and cj["fetched"] > 0 and cj["source"] == OER_API and len(cj["channels"]) == 7
          and set(cj["channels"][0]) == {"id", "type", "name", "url", "group"})
    check("frischer Cache (<24 h): kein weiterer Abruf", oer_update() is False and len(MockState.zapp_hits) == 1)
    OER_STATE.update(channels=None, source="fallback", fetched=0.0)
    oer_load()
    check("Neustart: Liste kommt aus dem Cache (source=cache)", OER_STATE["source"] == "cache" and len(oer_channels()) == 7
          and oer_update() is False and len(MockState.zapp_hits) == 1)
    MockState.zapp_mode = "fail"
    check("Cache aelter als 24 h + Quelle down: letzter Cache bleibt, Fehler gemerkt",
          oer_update(now=time.time() + OER_MAX_AGE_S + 60) is False and len(MockState.zapp_hits) == 2
          and OER_STATE["source"] == "cache" and len(oer_channels()) == 7 and "503" in OER_STATE["error"]
          and "nicht erreichbar" in oer_source_text(), (OER_STATE["error"], oer_source_text()))
    MockState.zapp_mode = "bad"
    check("Quelle liefert Muell: Cache bleibt", oer_update(force=True) is False and len(oer_channels()) == 7
          and OER_STATE["source"] == "cache")
    OER_CACHE.write_text("{kaputt")
    OER_STATE.update(channels=None)
    oer_load()
    check("kaputter Cache -> eingebaute Liste", OER_STATE["source"] == "fallback" and len(oer_channels()) == len(fb))
    check("Quelle down ohne Cache: Fallback bleibt", oer_update(force=True) is False and OER_STATE["source"] == "fallback")
    MockState.zapp_mode = "ok"
    check("force=True laedt trotz Cache neu", oer_update(force=True) is True and OER_STATE["source"] == "live"
          and "Zapp-API" in oer_source_text())
    # Aufloesung oer:<id> ueber die AKTUELLE Liste
    check("oer_resolve: Kennung -> URL der aktuellen Liste, unbekannt -> None",
          oer_resolve("oer:zdf") == "https://mock.invalid/zdf/master.m3u8" and oer_resolve("oer:gibtesnicht") is None)
    _ch = [dict(c) for c in oer_channels()]
    _old = oer_resolve("oer:zdf")
    for c in _ch:
        if c["id"] == "oer:zdf":
            c["url"] = "https://mock.invalid/zdf-neu/master.m3u8"
    OER_STATE["channels"] = _ch
    check("URL-Aenderung der Quelle: gleiche Kennung loest auf die neue URL auf", oer_resolve("oer:zdf") != _old
          and oer_resolve("oer:zdf").endswith("zdf-neu/master.m3u8"))
    oer_update(force=True)
    # Favoriten/Verlauf mit Typ url
    ff = Favorites(tmp / "old" / "fav-oer.json")
    ff.add("oer:zdf", "ZDF", kind="url")
    ff.add("oer:ZDF", "doppelt")
    ff.add(DUMMY, "Ace", api_name="Ace", kind="infohash")
    ff2 = Favorites(tmp / "old" / "fav-oer.json")
    check("Favorit oer:<id>: Typ url, id bleibt stabil, ohne Duplikat, neben infohash",
          [(f["id"], f["type"]) for f in ff2.items] == [("oer:zdf", "url"), (DUMMY, "infohash")], [(f["id"], f["type"]) for f in ff2.items])
    try:
        ff.add("oer:zu viel!", "x")
        check("ungueltige oer-Kennung abgelehnt", False)
    except ValueError:
        check("ungueltige oer-Kennung abgelehnt", True)
    hh = History(tmp / "old" / "hist-oer.json")
    hh.add("oer:wdr", "WDR", "url", ts=1000)
    hh.add(DUMMY, "Ace", "content_id", ts=1001)
    check("Verlauf kennt Typ url (nachladbar)", [(x["id"], x["type"]) for x in History(tmp / "old" / "hist-oer.json").items]
          == [(DUMMY, "content_id"), ("oer:wdr", "url")])
    check("Kopier-Text fuer Typ url = aktuelle Stream-URL", link_for({"id": "oer:wdr", "type": "url"}) == oer_resolve("oer:wdr")
          and link_for({"id": "oer:wdr", "type": "url"}).startswith("https://"))
    try:
        play("oer:gibtesnicht", KIND_URL, "X")
        check("play: unbekannter Sender -> PlayError", False)
    except PlayError as e:
        check("play: unbekannter Sender -> PlayError (kein Prozess)", e.code == 3 and "nicht (mehr) in der Liste" in str(e), str(e))

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gdk, GLib, Gtk

    def pump(seconds, cond=None):
        """Laeuft die GLib-Hauptschleife, bis cond() wahr wird oder die Zeit um ist."""
        ctx = GLib.MainContext.default()
        end = time.time() + seconds
        while time.time() < end:
            while ctx.pending():
                ctx.iteration(False)
            if cond and cond():
                return True
            time.sleep(0.01)
        return bool(cond and cond())

    def lines(f):
        return f.read_text().splitlines() if f.exists() else []

    def run_gui_tests(app):
        win = app.win
        check("Fenster gebaut (nicht praesentiert)", win is not None and not win.get_visible())
        check("Favoriten/Kategorien/Verlauf anfangs leer, keine Dateien",
              win.favs.items == [] and win.favs.categories == [] and win.history.items == []
              and not FAV_FILE.exists() and not HISTORY_FILE.exists())
        check("Engine-Check lief (Mock)", pump(5, lambda: win.engine_ok), win.badge.get_text())
        check("Chips: Alle + API-Kategorien", list(win.chips) == [None] + CATEGORY_CHIPS and win.chips[None].get_active())
        check("Fenster laedt Standard-Stichwoerter (settings.json ohne cat_keywords)", win.cat_keywords == default_keywords()
              and "cat_keywords" not in load_json(SETTINGS_FILE, {}))
        check("Tabs: Suche, Favoriten, Verlauf, OER", [win.stack.get_pages().get_item(i).get_name() for i in range(4)]
              == ["results", "favs", "hist", "oer"])
        check("Fenster: strenger Filter standardmaessig AN, ohne Ausschlusswoerter, settings.json unberuehrt",
              win.strict is True and win.strict_btn.get_active() and win.strict_switch.get_active()
              and win.cat_exclude == {c: [] for c in CATEGORY_CHIPS}
              and "strict_categories" not in load_json(SETTINGS_FILE, {}) and "cat_exclude" not in load_json(SETTINGS_FILE, {}))
        _cb = win.chips[None].get_parent()
        _lab = []
        _w = _cb.get_first_child()
        while _w is not None:
            _lab.append(_w.get_label())
            _w = _w.get_next_sibling()
        check("Hauptreihe: Alle, Sport, Nachrichten, Filme, Doku, Unterhaltung, Kinder, Musik, Mehr ▾",
              _lab == ["Alle", "Sport", "Nachrichten", "Filme", "Doku", "Unterhaltung", "Kinder", "Musik", "Mehr ▾"], _lab)
        check("Seltene Chips liegen im Mehr-Menue (eigener Container), alle bleiben in win.chips",
              all(win.chips[c].get_parent() is win.chips["religion"].get_parent() for c in CHIPS_MORE)
              and all(win.chips[c].get_parent() is _cb for c in CHIPS_MAIN) and win.chips["religion"].get_parent() is not _cb
              and win.more_btn.get_label() == "Mehr ▾")
        check("Games-Chip: Name E-Sport/Gaming und Erklaer-Tooltip",
              win.chips["cyber_games"].get_label() == "E-Sport/Gaming"
              and win.chips["cyber_games"].get_tooltip_text() == "Kanäle, die das AceStream-Verzeichnis als Gaming/E-Sport einordnet (cyber_games)")
        win.cat_keywords = {c: [] for c in CATEGORY_CHIPS}  # alte Tests laufen ohne Regeln; die Regeln testet ein eigener Block

        # --- Suche, Gruppen-Header mit Anzahl ---
        win.start_search("testkanal")
        check("Suche liefert 9 Treffer (Gruppen flach, dedupliziert)", pump(5, lambda: len(win.results) == 9), len(win.results))
        s = [h for h in hits("method=search") if "query=testkanal" in h][-1]
        check("Suche sendet show_epg=1 und keinen category-Parameter", "show_epg=1" in s and "category=" not in s, s)
        check("Treffer tragen Typ infohash und sind nach Sprachgruppe sortiert",
              all(r["type"] == "infohash" for r in win.results)
              and [result_group(r) for r in win.results] == sorted(result_group(r) for r in win.results))
        rows = list(win._rows(win.res_list))
        headers = [r.get_header().get_label() for r in rows if r.get_header()]
        check("Header mit Anzahl pro Gruppe", headers == ["Deutsch · 4", "Englisch · 3", "Weitere · 1", "Deaktiviert · 1"], headers)
        by = {r.rec["name"]: r for r in rows}
        check("EPG-Zeile 'Jetzt: <Sendung> · bis HH:MM' bei laufender Sendung",
              by["Testkanal Sport DE"].epg.get_visible()
              and re.fullmatch(r"Jetzt: Testspiel Live · bis \d\d:\d\d", by["Testkanal Sport DE"].epg.get_text()) is not None,
              by["Testkanal Sport DE"].epg.get_text())
        check("keine EPG-Zeile bei abgelaufener/fehlender Sendung",
              not by["Testkanal Film FR"].epg.get_visible() and not by["Testkanal News DE"].epg.get_visible())
        tags = []
        c = by["Testkanal Doku"].tags.get_first_child()
        while c:
            tags.append(c.get_label())
            c = c.get_next_sibling()
        check("Kategorie-Tags deutsch, unbekannte roh", "Doku" in tags and "exotic_x" in tags, tags)
        c = by["Testkanal News DE"].tags.get_first_child()
        tags = []
        while c:
            tags.append(c.get_label())
            c = c.get_next_sibling()
        check("'tv' steht hinter den Inhalts-Kategorien", tags[1:] == ["Nachrichten/Info", "TV"], tags)

        # --- Kategorie-Suche (API-Parameter category) ---
        win._set_entry("")
        win.set_category("sport")
        check("Kategorie + leere Suche listet die Kategorie (4 Sport-Treffer)",
              pump(5, lambda: len(win.results) == 4 and all("sport" in r["categories"] for r in win.results)),
              [r["name"] for r in win.results])
        s = hits("category=sport")
        check("API-Aufruf mit category=sport und leerem query", s and "query=&" in s[-1] and "show_epg=1" in s[-1], s[-1:] )
        check("Chip 'Sport' aktiv, Status nennt Kategorie", win.chips["sport"].get_active() and "Sport" in win.status.get_text(),
              win.status.get_text())
        win._set_entry("news")
        win.set_category("informational")
        check("Kategorie + Suchbegriff kombiniert",
              pump(5, lambda: len(win.results) == 2 and all("informational" in r["categories"] for r in win.results)),
              [r["name"] for r in win.results])
        check("... als query=news&category=informational", any("query=news" in h and "category=informational" in h
                                                              for h in hits("method=search")))
        win.set_category("music")
        check("Kategorie ohne Treffer: Hinweis", pump(5, lambda: win.results == []) and "Keine Treffer" in win.res_hint.get_text()
              and win.res_hint.get_visible(), win.res_hint.get_text())
        # Chip-Reihenfolge seit dem Kategorie-Umbau: Alle, Sport, Nachrichten, Filme, ... (Sport vor Nachrichten)
        win.set_category("informational")
        win.cycle_category(-1)
        win.cycle_category(-1)
        check("Alt+Links wechselt die Chips (Nachrichten -> Sport -> Alle)", win.category is None and win.chips[None].get_active()
              and pump(5, lambda: len(win.results) == 2), win.category)
        win.cycle_category(1)
        check("Alt+Rechts: weiter zu 'Sport' (erster Chip der neuen Reihenfolge)", win.category == "sport" and win.chips["sport"].get_active())
        # Strg+L
        win.clear_search()
        check("Strg+L leert Suchfeld, Kategorie und Ergebnisse", win.entry.get_text() == "" and win.category is None
              and win.chips[None].get_active() and win.results == [] and list(win._rows(win.res_list)) == [])
        pump(1)
        check("... und ein spaeter Treffer der alten Suche ueberschreibt das nicht", win.results == [])
        _ih = lambda n: "%040x" % n
        def _mk(n, nm, cats):
            return {"name": nm, "infohash": _ih(n), "categories": cats, "languages": ["de"], "countries": ["de"]}
        _pages = {0: [{"name": "G1", "items": [_mk(1, "A", ["Sport"]), _mk(2, "B", ["kinder", "regional"])]},
                      {"name": "G2", "items": [_mk(3, "C", ["tv", "sport"]), _mk(4, "D", [" ", ""])]}],
                  1: [{"name": "G3", "items": [_mk(5, "E", ["SPORT "])]}], 2: []}
        _orig = (globals()["_http_json"], globals()["_get_token"])
        globals()["_get_token"] = lambda: "T"
        globals()["_http_json"] = lambda url, timeout=20: {"result": {
            "total": 120, "results": _pages.get(int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["page"][0]), [])}}
        try:
            _info = {}
            _r = search_engine("", category="sport", info=_info)
        finally:
            globals()["_http_json"], globals()["_get_token"] = _orig
        check("Clientfilter: gemischte Gruppe -> nur Items mit Kategorie sport (inkl. Mehrfach-Kategorie, Gross/Klein)",
              sorted(x["name"] for x in _r) == ["A", "C", "E"], [x["name"] for x in _r])
        check("Clientfilter: Info total/groups/cut", _info == {"total": 120, "groups": 3, "cut": 0}, _info)
        check("has_category: Leerstrings/None ignoriert",
              not has_category({"categories": ["", " "]}, "sport") and not has_category({"categories": ["sport"]}, "")
              and has_category({"categories": ["tv", "Sport"]}, " SPORT"))
        # Race: alte Antwort (anderer Chip/Query/Generation) trifft nach der neuen ein
        win.set_category("sport")
        pump(3, lambda: len(win.results) == 4)
        _good = list(win.results)
        _fake = [dict(_good[0], name="ALTER-TREFFER", categories=["kinder"])]
        win._search_done(win.search_gen - 1, "", "sport", _fake, None, {})
        check("Race: Antwort alter Generation wird verworfen", win.results == _good)
        win._search_done(win.search_gen, "", None, _fake, None, {})
        check("Race: Antwort ohne aktuellen Chip (cat=None) wird verworfen", win.results == _good)
        win._search_done(win.search_gen, "andere", "sport", _fake, None, {})
        check("Race: Antwort mit anderem Suchbegriff wird verworfen", win.results == _good)
        win._search_done(win.search_gen, "", "sport", _fake, None, {})
        check("Fremde Kategorie in einer Antwort wird als Sicherheitsnetz gefiltert", win.results == [])
        win.set_category("sport")
        pump(3, lambda: len(win.results) == 4)
        win._search_done(win.search_gen, "", "sport", _good, None, {"total": 150, "groups": 100, "cut": 3})
        check("Status zeigt 'N von total' und 'gekuerzt'", "100 von 150 Kanälen, gekürzt" in win.status.get_text(), win.status.get_text())
        win.clear_search()
        pump(1)

        # --- Stichwort-Regeln (Merge in einem Worker-Durchlauf) ---
        win.cat_keywords = default_keywords()
        win._set_entry("")
        n0 = len(hits("method=search"))
        win.set_category("sport")
        check("Regel-Merge: 4 Kategorie-Treffer + 1 Regel-Treffer (keine API-Kategorie), kein Duplikat",
              pump(5, lambda: len(win.results) == 5), [r["name"] for r in win.results])
        ids_ = [r["id"] for r in win.results]
        rule_ = [r for r in win.results if r.get("rule_match")]
        check("... genau der Regel-Treffer traegt rule_match, kein Duplikat, Wortanfang-Regel ('Xsportx' fehlt)",
              len(set(ids_)) == 5 and [r["name"] for r in rule_] == ["Regeltest Sport Extra"]
              and "Regeltest Xsportx" not in [r["name"] for r in win.results], [r["name"] for r in win.results])
        rrow = {r.rec["name"]: r for r in win._rows(win.res_list)}

        def tag_texts(row):
            out_, c_ = [], row.tags.get_first_child()
            while c_:
                out_.append(c_.get_label())
                c_ = c_.get_next_sibling()
            return out_

        check("... Badge 'Regel' nur am Regel-Treffer", "Regel" in tag_texts(rrow["Regeltest Sport Extra"])
              and all("Regel" not in tag_texts(r) for n_, r in rrow.items() if n_ != "Regeltest Sport Extra"))
        check("Statuszeile: 'N Treffer (x per Kategorie, y per Regel)'",
              win.status.get_text().startswith("5 Treffer (4 per Kategorie, 1 per Regel)") and "für Sport" in win.status.get_text(), win.status.get_text())
        new_hits = hits("method=search")[n0:]
        check("Suchaufrufe: 1x category=sport (leer) + 1x query=sport ohne category (1 Seite je Suche)",
              len([h for h in new_hits if "category=sport" in h]) == 1
              and len([h for h in new_hits if "query=sport" in h and "category=" not in h]) == 1, new_hits)
        win._set_entry("extra")
        win.set_category("sport")
        check("Suchbegriff wirkt als Namensfilter auch auf Regel-Treffer (nur 'Extra')",
              pump(5, lambda: [r["name"] for r in win.results] == ["Regeltest Sport Extra"]) and win.results[0].get("rule_match")
              and win.status.get_text().startswith("1 Treffer (0 per Kategorie, 1 per Regel)"), (win.status.get_text(), [r["name"] for r in win.results]))
        win._set_entry("")
        # Der Standard ist jetzt "Streng" (Regeltreffer nur fuer Kanaele ohne eigene Genre-Kategorie der API; der
        # Kino-Kanal traegt "entertaining"). Diese alten Regel-Tests pruefen das bisherige Verhalten -> Streng aus.
        win.strict = False
        win.set_category("movies")
        check("Filme: Kategorie-Treffer + Regel-Treffer ('Kino'), 'Film' aus Regel dedupliziert",
              pump(5, lambda: sorted(r["name"] for r in win.results) == ["Regeltest Kino Allerlei", "Testkanal Film FR"])
              and [r["name"] for r in win.results if r.get("rule_match")] == ["Regeltest Kino Allerlei"],
              [(r["name"], r.get("rule_match")) for r in win.results])
        win.cat_keywords["sport"] = []
        win.set_category("sport")
        check("Ohne Stichwoerter: wie bisher (4 Treffer, kein Badge, alter Status)",
              pump(5, lambda: len(win.results) == 4) and not any(r.get("rule_match") for r in win.results)
              and win.status.get_text().startswith("4 Treffer") and "per Regel" not in win.status.get_text(), win.status.get_text())
        win.cat_keywords["music"] = ["k%d" % i for i in range(1, 9)]
        n0 = len(hits("method=search"))
        win.set_category("music")
        pump(5, lambda: win.notice.get_visible() and "Stichwörter" in win.notice.get_text())
        nh = hits("method=search")[n0:]
        qs = {re.search(r"query=([^&]*)", h).group(1) for h in nh if "category=" not in h}
        check("Hoechstens 6 Stichwoerter pro Suche + Hinweis bei mehr", qs == {"k%d" % i for i in range(1, 7)}
              and "8" in win.notice.get_text() and "6" in win.notice.get_text(), (sorted(qs), win.notice.get_text()))
        win.cat_keywords["music"] = []
        # Race mit Regeln: Chip-Wechsel waehrend der Merge-Suche -> nur das neue Ergebnis zaehlt, genau ein _search_done
        win.cat_keywords = default_keywords()
        calls = []
        orig_done = win._search_done
        win._search_done = lambda *a, **k: (calls.append(a[0]), orig_done(*a, **k))[1]
        win.set_category("sport")
        win.set_category("movies")
        pump(3, lambda: len(win.results) == 2 and len(calls) >= 2)
        pump(1)
        check("Race: schnell Sport -> Filme: Ergebnis gehoert zu Filme, nichts von Sport",
              sorted(r["name"] for r in win.results) == ["Regeltest Kino Allerlei", "Testkanal Film FR"], [r["name"] for r in win.results])
        check("... jede Worker-Antwort ruft genau einmal _search_done (je Suche 1 Aufruf)", len(calls) == 2 and calls[0] != calls[1], calls)
        win._search_done = orig_done
        win.strict = True
        _good = list(win.results)
        _stale = [dict(_good[0], name="ALT", rule_match=True)]
        win._search_done(win.search_gen - 1, "", "movies", _stale, None, {})
        check("Race: Regel-Antwort alter Generation wird verworfen", win.results == _good)
        win._search_done(win.search_gen, "", "sport", _stale, None, {})
        check("Race: Regel-Antwort mit anderem Chip wird verworfen", win.results == _good)
        win._search_done(win.search_gen, "", "movies", _stale + [dict(_good[0], name="OHNE", rule_match=False, categories=["kids"])], None, {})
        check("Sicherheitsnetz: rule_match bleibt, Fremd-Kategorie ohne Regel fliegt raus",
              [r["name"] for r in win.results] == ["ALT"])
        # Dialog (ohne Anzeige)
        win.clear_search()
        pump(0.5)
        win.cat_keywords = default_keywords()
        dlg, ents, save_fn, reset_fn = win.build_rules_dialog()
        check("Dialog: ein Feld je Chip-Kategorie, generische Platzhalter, Standardwerte vorbelegt",
              list(ents) == CATEGORY_CHIPS and all(e.get_placeholder_text() == "z. B. sport, live" for e in ents.values())
              and ents["sport"].get_text() == "sport" and ents["movies"].get_text() == "cinema, kino, film, movie"
              and ents["religion"].get_text() == "")
        ents["sport"].set_text("sport, Live , live")
        ents["religion"].set_text("gottesdienst")
        save_fn()
        sj = load_json(SETTINGS_FILE, {})
        check("Dialog Speichern: cat_keywords in settings.json (bereinigt), im Fenster aktiv",
              sj.get("cat_keywords", {}).get("sport") == ["sport", "live"] and sj["cat_keywords"]["religion"] == ["gottesdienst"]
              and win.cat_keywords["sport"] == ["sport", "live"] and set(sj["cat_keywords"]) == set(CATEGORY_CHIPS), sj.get("cat_keywords"))
        check("... nach Neustart gelesen (load_cat_keywords)", load_cat_keywords(sj) == win.cat_keywords)
        dlg2, ents2, save2, reset2 = win.build_rules_dialog()
        check("Dialog zeigt gespeicherte Werte", ents2["sport"].get_text() == "sport, live" and ents2["religion"].get_text() == "gottesdienst")
        reset2()
        check("Zuruecksetzen fuellt die Standardwerte ein (religion leer), gespeichert wird erst mit Speichern",
              ents2["sport"].get_text() == "sport" and ents2["religion"].get_text() == "" and win.cat_keywords["sport"] == ["sport", "live"])
        save2()
        check("Standard gespeichert", win.cat_keywords == default_keywords() and load_cat_keywords(load_json(SETTINGS_FILE, {})) == default_keywords())
        # --- Strenger Filter, Ausschluss, Doppel-Regeln, Mehr-Menue, Leer-Hinweis (Mock-Zusatzgruppen) ---
        MockState.extra = [
            {"name": "Strenger Mix", "items": [mk_item("Strenger Mix", ["deu"], ["de"], ["movies", "documentaries"], 0.5)]},
            {"name": "Strenger Film", "items": [mk_item("Strenger Film", ["deu"], ["de"], ["tv", "movies"], 0.5)]},
            {"name": "Strenger Pur", "items": [mk_item("Strenger Pur", ["deu"], ["de"], ["documentaries"], 0.5)]},
            {"name": "Strenger Shop Film", "items": [mk_item("Strenger Shop Film", ["deu"], ["de"], ["movies"], 0.5)]},
            {"name": "Strenger Doku Film Regel", "items": [mk_item("Strenger Doku Film Regel", ["deu"], ["de"], [], 0.5)]},
        ]
        win.cat_keywords = default_keywords()
        win.cat_exclude = {c: [] for c in CATEGORY_CHIPS}
        win.strict = True
        win._set_entry("strenger")

        def names_():
            return sorted(r["name"] for r in win.results)

        win.set_category("movies")
        check("Streng: Filme zeigt nur Kanaele mit Filme als Hauptkategorie (Mix=Doku, Doppel-Regel=Doku fehlen)",
              pump(5, lambda: names_() == ["Strenger Film", "Strenger Shop Film"]) and not any(r.get("rule_match") for r in win.results),
              (names_(), win.status.get_text()))
        check("... Statuszeile nennt die ausgeblendeten", pump(2, lambda: "1 ausgeblendet (Streng/Ausschluss)" in win.status.get_text()),
              win.status.get_text())
        win.set_category("documentaries")
        check("Streng: Doku = Mix (Hauptkategorie), Pur und der Regeltreffer, der beide Regeln traf (staerkerer Treffer)",
              pump(5, lambda: names_() == ["Strenger Doku Film Regel", "Strenger Mix", "Strenger Pur"])
              and [r["name"] for r in win.results if r.get("rule_match")] == ["Strenger Doku Film Regel"], (names_(), win.status.get_text()))
        rws = {r.rec["name"]: r for r in win._rows(win.res_list)}
        check("Zeile 'auch: …' bei weiteren Kategorien (Mix: Filme), nicht bei Kanaelen ohne",
              "auch: Filme" in rws["Strenger Mix"].sub.get_text() and "auch:" not in rws["Strenger Pur"].sub.get_text(),
              (rws["Strenger Mix"].sub.get_text(), rws["Strenger Pur"].sub.get_text()))
        win.strict_btn.set_active(False)
        check("Schalter 'Streng' aus: Fenster, Menue-Schalter und settings.json folgen",
              win.strict is False and not win.strict_switch.get_active() and load_json(SETTINGS_FILE, {}).get("strict_categories") is False)
        check("Nicht streng: Doku zeigt Mix und Pur (API) und den Regeltreffer; Doppel-Regel gilt weiter",
              pump(5, lambda: names_() == ["Strenger Doku Film Regel", "Strenger Mix", "Strenger Pur"]), names_())
        win.set_category("movies")
        check("Nicht streng: Filme zeigt auch Mix (alle API-Kategorien), nicht den Doku-Regeltreffer",
              pump(5, lambda: names_() == ["Strenger Film", "Strenger Mix", "Strenger Shop Film"])
              and "ausgeblendet" not in win.status.get_text(), (names_(), win.status.get_text()))
        win.strict_switch.set_active(True)
        check("Menue-Schalter an: Streng wieder an (Chip-Schalter folgt), Suche laeuft neu",
              win.strict is True and win.strict_btn.get_active() and load_json(SETTINGS_FILE, {}).get("strict_categories") is True
              and pump(5, lambda: names_() == ["Strenger Film", "Strenger Shop Film"]), names_())
        win.cat_exclude["movies"] = ["shop"]
        win.set_category("movies")
        check("Ausschlusswort 'shop' nimmt Kanal unter Filme heraus",
              pump(5, lambda: names_() == ["Strenger Film"]) and "2 ausgeblendet" in win.status.get_text(), (names_(), win.status.get_text()))
        win.cat_exclude["movies"] = ["strenger"]
        win.set_category("movies")
        check("Leer-Hinweis mit Ausblendung: erklaert Streng, Liste leer, Hinweis sichtbar",
              pump(5, lambda: win.results == [] and "Streng" in win.res_hint.get_text() and win.res_hint.get_visible()),
              win.res_hint.get_text())
        win.cat_exclude["movies"] = []
        win._set_entry("")
        win.set_category("cyber_games")
        check("E-Sport/Gaming (leer): Leer-Hinweis erklaert den Chip, Mehr-Knopf zeigt ihn an",
              pump(5, lambda: win.results == [] and "cyber_games" in win.res_hint.get_text() and win.res_hint.get_visible())
              and win.more_btn.get_label() == "E-Sport/Gaming ▾" and win.chips["cyber_games"].get_active(), win.res_hint.get_text())
        win.set_category("religion")
        check("Anderer leerer Chip: allgemeiner Hinweis statt nackter Liste",
              pump(5, lambda: "Religion" in win.res_hint.get_text() and win.res_hint.get_visible()
                   and "Stichwörter" in win.res_hint.get_text()), win.res_hint.get_text())
        win.set_category("music")
        check("Zurueck in die Hauptreihe: Mehr-Knopf wieder 'Mehr ▾'", win.more_btn.get_label() == "Mehr ▾")
        win.set_category(None)
        MockState.extra = []
        dlgx, entsx, savex, resetx = win.build_rules_dialog()
        check("Regel-Dialog: zweites Feld 'ausschließen' je Chip (leer, eigener Platzhalter)",
              list(dlgx.excl_entries) == CATEGORY_CHIPS and all(e.get_text() == "" and e.get_placeholder_text().startswith("ausschließen")
                                                                for e in dlgx.excl_entries.values()))
        dlgx.excl_entries["movies"].set_text("Shop, replay , shop")
        savex()
        sjx = load_json(SETTINGS_FILE, {})
        check("Dialog Speichern: cat_exclude in settings.json (bereinigt) und im Fenster; Stichwoerter bleiben",
              sjx.get("cat_exclude", {}).get("movies") == ["shop", "replay"] and win.cat_exclude["movies"] == ["shop", "replay"]
              and set(sjx["cat_exclude"]) == set(CATEGORY_CHIPS) and load_cat_exclude(sjx) == win.cat_exclude
              and win.cat_keywords == default_keywords() and sjx.get("strict_categories") is True, sjx.get("cat_exclude"))
        dlgy, entsy, savey, resety = win.build_rules_dialog()
        check("Dialog zeigt gespeicherte Ausschlusswoerter; Zuruecksetzen leert sie",
              dlgy.excl_entries["movies"].get_text() == "shop, replay"
              and (resety(), dlgy.excl_entries["movies"].get_text())[1] == "")
        savey()
        check("Standard gespeichert: Ausschlusswoerter wieder leer", win.cat_exclude == {c: [] for c in CATEGORY_CHIPS}
              and load_cat_exclude(load_json(SETTINGS_FILE, {})) == win.cat_exclude)
        win.cat_keywords = {c: [] for c in CATEGORY_CHIPS}

        # --- letzte Suchbegriffe ---
        for i in range(12):
            win.remember_query("Begriff %02d" % i)
        win.remember_query("begriff 05")  # Duplikat (ohne Gross/Klein) rueckt nach vorn
        win.remember_query("x")  # zu kurz
        check("Suchverlauf: 10 Eintraege, neueste zuerst, ohne Duplikate",
              len(win.recent) == 10 and win.recent[0] == "begriff 05" and win.recent[1] == "Begriff 11"
              and len({s.lower() for s in win.recent}) == 10, win.recent)
        check("... in settings.json gespeichert", load_json(SETTINGS_FILE, {}).get("recent") == win.recent)
        win._set_entry("entwurf")
        k = lambda key: win.on_entry_key(None, key, 0, 0)
        k(Gdk.KEY_Up)
        t1 = win.entry.get_text()
        k(Gdk.KEY_Up)
        t2 = win.entry.get_text()
        k(Gdk.KEY_Down)
        t3 = win.entry.get_text()
        k(Gdk.KEY_Down)
        check("Pfeil hoch/runter blaettert durch Suchbegriffe und stellt den Entwurf wieder her",
              (t1, t2, t3, win.entry.get_text()) == ("begriff 05", "Begriff 11", "begriff 05", "entwurf"),
              (t1, t2, t3, win.entry.get_text()))
        pump(0.9)
        check("Blaettern loest keine Suche aus", not hits("query=begriff") and not hits("query=Begriff"))
        win._set_entry("")

        # --- Favoriten mit eigenen Kategorien ---
        win.start_search("testkanal")
        pump(5, lambda: len(win.results) == 9)
        R = {r["name"]: r for r in win.results}
        win.toggle_fav(R["Testkanal Sport DE"], None)
        check("Stern ohne Kategorien: direkt gespeichert (Ohne Kategorie, Typ infohash)",
              win.favs.has(R["Testkanal Sport DE"]["id"]) and win.favs.items[0]["category"] == ""
              and win.favs.items[0]["type"] == "infohash" and FAV_FILE.exists())
        check("Ohne Kategorien: flache Liste ohne Header", [r.kind for r in win._rows(win.fav_list)] == ["fav"])
        ca = win.favs.add_category("Abends")
        cb = win.favs.add_category("Wochenende")
        win.rebuild_favs()
        check("Mit Kategorien: Header je Kategorie + 'Ohne Kategorie'",
              [(r.kind, r.rec["name"] if r.kind == "cat" else "") for r in win._rows(win.fav_list)]
              == [("cat", "Abends"), ("cat", "Wochenende"), ("cat", UNCATEGORIZED), ("fav", "")][:3] + [("fav", "")],
              [(r.kind, r.rec["name"] if r.kind == "cat" else "") for r in win._rows(win.fav_list)])
        win.add_fav(R["Testkanal News DE"], ca)
        win.add_fav(R["Testkanal Kinder"], ca)
        win.add_fav(R["Testkanal News EN"], cb)
        kinds = [(r.kind, r.rec["name"]) for r in win._rows(win.fav_list)]
        check("Favoriten stehen unter ihrer Kategorie",
              kinds == [("cat", "Abends"), ("fav", "Testkanal News DE"), ("fav", "Testkanal Kinder"),
                        ("cat", "Wochenende"), ("fav", "Testkanal News EN"), ("cat", UNCATEGORIZED),
                        ("fav", "Testkanal Sport DE")], kinds)
        check("category pro Eintrag in favorites.json",
              {f["name"]: f["category"] for f in Favorites().items}
              == {"Testkanal Sport DE": "", "Testkanal News DE": ca, "Testkanal Kinder": ca, "Testkanal News EN": cb})
        # Doppelklick/Enter auf Header klappt ein/aus
        head = next(r for r in win._rows(win.fav_list) if r.kind == "cat" and r.rec["id"] == ca)
        head.activate()
        names = [r.rec["name"] for r in win._rows(win.fav_list) if r.kind == "fav"]
        check("Doppelklick/Enter auf Header klappt ein (Favoriten der Kategorie verborgen, Zaehler bleibt)",
              "Testkanal News DE" not in names and "Testkanal News EN" in names and ca in win.collapsed, names)
        check("... Zustand in settings.json", load_json(SETTINGS_FILE, {}).get("collapsed") == [ca])
        next(r for r in win._rows(win.fav_list) if r.kind == "cat" and r.rec["id"] == ca).activate()
        check("... und wieder aus", "Testkanal News DE" in [r.rec["name"] for r in win._rows(win.fav_list)] and not win.collapsed)
        # Reihenfolge
        win.move_cat(cb, -1)
        check("Kategorie hoch/runter", [r.rec["name"] for r in win._rows(win.fav_list) if r.kind == "cat"][:2] == ["Wochenende", "Abends"]
              and [c["id"] for c in Favorites().categories] == [cb, ca])
        # Zuordnung ueber Popover "In Kategorie ..."
        row = next(r for r in win._rows(win.fav_list) if r.kind == "fav" and r.rec["name"] == "Testkanal Sport DE")
        got = []
        pop = win.pick_category(row.menu_btn, "", got.append)
        vb = pop.get_child().get_child().get_child()
        labels = []
        b = vb.get_first_child()
        btns = {}
        while b:
            if isinstance(b, Gtk.Button):
                labels.append(b.get_label())
                btns[b.get_label().lstrip("✓ ")] = b
            b = b.get_next_sibling()
        check("Popover bietet Ohne Kategorie, alle eigenen Kategorien und 'Neue Kategorie'",
              labels == ["✓ " + UNCATEGORIZED, "Wochenende", "Abends", "＋ Neue Kategorie …"], labels)
        btns["Abends"].emit("clicked")
        check("Popover-Auswahl liefert die Kategorie", pump(2, lambda: got == [ca]), got)
        win.favs.set_category(row.rec["id"], got[0])
        win.rebuild_favs()
        check("Zuordnung nachtraeglich geaendert (Sport DE -> Abends)", Favorites().get(row.rec["id"])["category"] == ca)
        # Stern bei vorhandenen Kategorien oeffnet das Popover; Auswahl speichert direkt in die Kategorie
        res_row = next(r for r in win._rows(win.res_list) if r.rec["name"] == "Testkanal Doku")
        pop = win.toggle_fav(res_row.rec, res_row.star)
        pop = None
        check("Stern bei vorhandenen Kategorien: noch nicht gespeichert (wartet auf Auswahl)", not win.favs.has(res_row.rec["id"]))
        pump(0.3)
        # (Popover-Auswahl wird oben getestet; hier direkt zuordnen wie _assign)
        win.add_fav(res_row.rec, cb)
        check("Stern-Favorit landet in gewaehlter Kategorie", Favorites().get(res_row.rec["id"])["category"] == cb)
        for r in win._rows(win.res_list):
            r.refresh()
        check("Stern-Icons aktualisiert", res_row.star.get_icon_name() == "starred-symbolic")
        # Kategorie loeschen
        n_before = len(win.favs.items)
        win.delete_cat(ca)
        check("Kategorie loeschen: Favoriten bleiben, wandern nach Ohne Kategorie",
              len(win.favs.items) == n_before and all(f["category"] in ("", cb) for f in win.favs.items)
              and ca not in [c["id"] for c in Favorites().categories])
        # Umbenennen eines Favoriten / Loeschen
        win.favs.rename(HASH, "x")  # unbekannte ID: keine Wirkung, kein Fehler
        win.delete_fav(R["Testkanal Sport DE"])
        check("Favorit loeschen", not win.favs.has(R["Testkanal Sport DE"]["id"]))
        check("Einzelklick aktiviert nicht (Doppelklick/Enter)", not win.res_list.get_activate_on_single_click()
              and not win.fav_list.get_activate_on_single_click() and not win.hist_list.get_activate_on_single_click())

        # --- Link kopieren ---
        win.copy_link(R["Testkanal Sport DE"])
        check("Link kopieren: Infohash als infohash:<h>", win.status.get_text() == "Kopiert: infohash:" + R["Testkanal Sport DE"]["id"],
              win.status.get_text())
        win.copy_link({"id": DUMMY, "type": "content_id"})
        check("Link kopieren: Content-ID als acestream://", win.status.get_text() == "Kopiert: acestream://" + DUMMY)

        # --- Abspielen: format=json, Stats, Titel ---
        os.environ["MOCK_MODE"] = "ok"
        os.environ["MOCK_SLEEP"] = "2.5"

        def idle_player():
            return win.current is None and not win.play_busy

        name = 'Test "Q" & Co ünï'
        n_json = len(json_starts())
        win.play_id(HASH, name, "infohash")
        check("Live-Status erscheint: ▶ Name · Peers 12 · ↓ 1,1 MB/s",
              pump(6, lambda: win.live.get_visible() and "Peers 12" in win.live.get_text()), win.live.get_text())
        check("Anzeige exakt", win.live.get_text() == "▶ %s · Peers 12 · ↓ 1,1 MB/s" % name, win.live.get_text())
        check("getstream?format=json genau einmal, mit infohash und pid",
              len(json_starts()) == n_json + 1 and ("infohash=" + HASH) in json_starts()[-1]
              and "pid=" in json_starts()[-1], json_starts()[-1:])
        out = lines(out_file)
        check("Player bekommt die playback_url (nicht die getstream-URL)",
              len(out) == 1 and re.fullmatch(re.escape(ENGINE_URL) + r"/ace/r/SID/bbbbbb", out[0]) is not None, out)
        check("ACE_TITLE = Name (Leerzeichen, Anfuehrungszeichen, &, Umlaute)", title_file.read_text() == name, title_file.read_text())
        pump(PLAY_OK_S + 0.5, lambda: win.status.get_text().startswith("Läuft im Player"))
        check("Statuszeile: Laeuft im Player", win.status.get_text().startswith("Läuft im Player"), win.status.get_text())
        check("stat_url mit fremdem Host (203.0.113.9) wurde auf die Engine umgesetzt; Abfrage wiederholt sich",
              pump(3, lambda: len(hits("/ace/stat/")) >= 3), len(hits("/ace/stat/")))
        check("Log nennt Player-URL und stat_url", play_log_text().startswith("Starte Player: " + ENGINE_URL + "/ace/r/")
              and "Live-Status: " + ENGINE_URL + "/ace/stat/" in play_log_text())
        check("Stats-Abfrage endet mit dem Player: Leiste weg, 'Player beendet'",
              pump(6, lambda: win.current is None and not win.live.get_visible()) and win.status.get_text() == "Player beendet.",
              win.status.get_text())
        n_stat = len(hits("/ace/stat/"))
        pump(1.0)
        check("... danach keine weiteren stat-Abfragen", len(hits("/ace/stat/")) == n_stat, (n_stat, len(hits("/ace/stat/"))))
        check("Verlauf: Start eingetragen (Name, ID, Typ, Zeit), Datei history.json",
              win.history.items and win.history.items[0]["id"] == HASH and win.history.items[0]["name"] == name
              and win.history.items[0]["type"] == "infohash" and win.history.items[0]["ts"] > 0
              and History().items == win.history.items)
        hrows = list(win._rows(win.hist_list))
        check("Verlauf-Tab zeigt die Zeile mit Zeit, ▶ und ★", len(hrows) == 1 and hrows[0].sub.get_label().startswith("heute ")
              and hrows[0].star is not None and hrows[0].play_btn is not None, [r.sub.get_label() for r in hrows])
        check("Verlauf-Zeile: ★ speichert als Favorit", (win.toggle_fav(hrows[0].rec, None), win.favs.has(HASH))[1])

        # --- Fallback: format=json liefert Fehler ---
        MockState.json_fail = True
        out_file.unlink(missing_ok=True)
        n_stat = len(hits("/ace/stat/"))
        win.play_id(HASH2, "Fallback-Test", "infohash")
        check("json-Fehler -> Fallback auf alten Pfad (getstream?infohash=..&pid=<uuid4>, ohne format)",
              pump(5, lambda: len(lines(out_file)) == 1)
              and re.fullmatch(re.escape(ENGINE_URL) + r"/ace/getstream\?infohash=" + HASH2 +
                               r"&pid=[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", lines(out_file)[0]) is not None,
              lines(out_file))
        pump(0.5)
        check("... mit Hinweis, Leiste nur mit Namen, ohne Stats-Abfrage",
              "Live-Status nicht verfügbar" in win.notice.get_text() and win.live.get_text() == "▶ Fallback-Test"
              and len(hits("/ace/stat/")) == n_stat and "HINWEIS" in play_log_text(), (win.notice.get_text(), win.live.get_text()))
        check("... ACE_TITLE auch im Fallback", title_file.read_text() == "Fallback-Test")
        pump(6, idle_player)
        MockState.json_fail = False

        # --- Schalter 'Live-Status' aus ---
        out_file.unlink(missing_ok=True)
        n_json = len(json_starts())
        win.live_switch.set_active(False)
        check("Schalter Live-Status aus (in settings.json)", not win.live_enabled and load_json(SETTINGS_FILE, {}).get("live_status") is False)
        win.play_id(HASH3, "Ohne Live", "infohash")
        pump(5, lambda: len(lines(out_file)) == 1)
        check("Live-Status aus: alter Pfad, kein format=json", "/ace/getstream?infohash=" + HASH3 in lines(out_file)[0]
              and len(json_starts()) == n_json, lines(out_file))
        pump(6, idle_player)
        win.live_switch.set_active(True)
        check("Schalter wieder an", win.live_enabled and load_json(SETTINGS_FILE, {}).get("live_status") is True)

        # --- content_id: ace-play, keine Stats ---
        out_file.unlink(missing_ok=True)
        n_stat = len(hits("/ace/stat/"))
        win.play_id(DUMMY, "Manuell Test", "content_id")
        pump(5, lambda: len(lines(out_file)) == 1)
        pump(PLAY_OK_S + 0.4)
        check("content_id -> ace-play-Mock mit genau <id>, ACE_TITLE gesetzt",
              lines(out_file) == ["ace-play " + DUMMY] and title_file.read_text() == "Manuell Test", lines(out_file))
        check("content_id: Leiste nur mit Namen, keine Stats-Abfrage", win.live.get_visible() and win.live.get_text() == "▶ Manuell Test"
              and len(hits("/ace/stat/")) == n_stat, win.live.get_text())
        pump(6, idle_player)

        # --- Doppelstart, Zeilenaktivierung, Fehlerfall ---
        os.environ["MOCK_SLEEP"] = "1"
        out_file.unlink(missing_ok=True)
        row = next(win._rows(win.res_list))
        row.activate()
        win.play_id(row.rec["id"], "doppelt", "infohash")  # waehrend des Starts: ignoriert
        pump(2.5, idle_player)
        check("row.activate() startet genau einen Player; Doppel-Start verhindert",
              len(lines(out_file)) == 1 and "/ace/r/SID/" in lines(out_file)[0], lines(out_file))
        out_file.unlink(missing_ok=True)
        hist_before = [dict(x) for x in win.history.items]
        os.environ["MOCK_MODE"] = "http500"
        win.play_id(HASH, "Fehler", "infohash")
        pump(5, lambda: win.notice.get_visible() and win.notice.get_text().startswith("Stream nicht"))
        check("500: Meldung 'Stream nicht verfuegbar (Engine 500)', kein zweiter Versuch",
              win.notice.get_text() == "Stream nicht verfügbar (Engine 500)" and len(lines(out_file)) == 1, win.notice.get_text())
        check("... Fehlstart landet nicht im Verlauf, Leiste verschwindet", pump(2, lambda: not win.live.get_visible())
              and win.history.items == hist_before)
        os.environ["MOCK_MODE"] = "ok"
        win.clear_history()
        check("Verlauf leeren (Knopf-Funktion)", win.history.items == [] and History().items == []
              and list(win._rows(win.hist_list)) == [] and win.hist_hint.get_visible())


        # --- Oeffentlich-Rechtliche: Tab, Liste, Favoriten, Abspielen ueber mpv-ace.sh ---
        ev0 = len(hits("/webui/api/service"))
        n_eng = len(MockState.hits)
        win.on_window_key(None, Gdk.KEY_4, 0, Gdk.ModifierType.CONTROL_MASK)
        check("Strg+4 oeffnet den Tab OER", win.stack.get_visible_child_name() == "oer")
        pump(3, lambda: not win.oer_busy)
        win.refresh_oer(force=True)
        check("Senderliste laedt von der Quelle (Mock-Zapp), Label nennt Quelle",
              pump(5, lambda: not win.oer_busy and OER_STATE["source"] == "live") and win.oer_src.get_text().startswith("Quelle: Zapp-API"),
              win.oer_src.get_text())
        orows = list(win._rows(win.oer_list))
        check("Liste: alle Sender der Quelle (+ DW), in Reihenfolge, kompakt (▶ und ★, keine Zusatzzeile)",
              [r.rec["name"] for r in orows] == ["Das Erste", "ZDF", "ARTE", "DW (English)", "NDR Niedersachsen", "WDR", "NeuerSender"]
              and all(r.kind == "oer" and r.star is not None and r.play_btn is not None and not r.sub.get_visible() for r in orows),
              [r.rec["name"] for r in orows])
        heads = [r.get_header().get_label() for r in orows if r.get_header()]
        check("Gruppen-Header mit Anzahl", heads == ["Bundesweit · 3", "International · 1", "Dritte Programme · 2", "Weitere · 1"], heads)
        check("Tab zeigt die Netz-Quelle, kein Engine-Aufruf durch das Laden", len(MockState.hits) == n_eng, len(MockState.hits) - n_eng)
        # abspielen
        os.environ["MOCK_MODE"] = "ok"
        os.environ["MOCK_SLEEP"] = "1"
        out_file.unlink(missing_ok=True)
        title_file.unlink(missing_ok=True)
        n_json = len(json_starts())
        hist_n = len(win.history.items)
        orows[0].activate()
        check("Enter/Doppelklick: mpv-ace.sh (Mock) mit Stream-URL, ACE_TITLE = Sendername",
              pump(5, lambda: len(lines(out_file)) == 1) and lines(out_file) == ["mpv-ace https://mock.invalid/erste/master.m3u8"]
              and pump(2, lambda: title_file.exists()) and title_file.read_text() == "Das Erste", lines(out_file))
        pump(PLAY_OK_S + 0.5, lambda: win.status.get_text().startswith("Läuft im Player"))
        check("... Leiste nur mit Namen, kein Engine-Check, kein getstream, keine Stats",
              win.live.get_text() == "▶ Das Erste" and len(MockState.hits) == n_eng and len(json_starts()) == n_json
              and len(hits("/webui/api/service")) == ev0, win.live.get_text())
        check("... Log nennt mpv-Start mit der URL", play_log_text().startswith("Starte mpv (offizieller Livestream): https://mock.invalid/erste/master.m3u8"),
              play_log_text()[:100])
        check("Verlauf: oer:<id> mit Typ url", len(win.history.items) == hist_n + 1 and win.history.items[0]["id"] == "oer:das_erste"
              and win.history.items[0]["type"] == "url" and History().items[0]["id"] == "oer:das_erste")
        pump(6, idle_player)
        hr_ = [r for r in win._rows(win.hist_list)][0]
        check("Verlauf-Zeile (Typ url): Zeit + ▶ + ★", hr_.sub.get_label().startswith("heute ") and hr_.star is not None)
        # Favorit
        zdf_row = next(r for r in win._rows(win.oer_list) if r.rec["id"] == "oer:zdf")
        win.toggle_fav(zdf_row.rec, None)
        fz = win.favs.get("oer:zdf")
        check("★ am Sender: Favorit mit id oer:zdf, Typ url, ohne api_name", fz and fz["type"] == "url" and fz["api_name"] == ""
              and Favorites().get("oer:zdf")["type"] == "url")
        frow = next(r for r in win._rows(win.fav_list) if r.rec["id"] == "oer:zdf")
        check("Favoriten-Zeile: 'Offizieller Livestream', Tag OER, Stern gefuellt",
              "Offizieller Livestream" in frow.sub.get_label() and "ÖR" in tag_texts(frow) and zdf_row.star.get_icon_name() == "starred-symbolic")
        n_q = len(hits("method=search"))
        win.refresh_fav_status()
        pump(1.5)
        check("Favoriten-Verfuegbarkeit ueberspringt Typ url (keine Engine-Suche fuer ZDF)",
              not any("query=ZDF" in h for h in hits("method=search")[n_q:]))
        # Aufloesung: Quelle aendert URL -> Favorit spielt die neue
        _ch = [dict(c) for c in oer_channels()]
        for c in _ch:
            if c["id"] == "oer:zdf":
                c["url"] = "https://mock.invalid/zdf-v2/master.m3u8"
        OER_STATE["channels"] = _ch
        out_file.unlink(missing_ok=True)
        frow.activate()
        check("Favorit oer:zdf wird ueber die AKTUELLE Liste aufgeloest (neue URL)",
              pump(5, lambda: len(lines(out_file)) == 1) and lines(out_file) == ["mpv-ace https://mock.invalid/zdf-v2/master.m3u8"]
              and title_file.read_text() == "ZDF", lines(out_file))
        pump(PLAY_OK_S + 0.5)
        pump(6, idle_player)
        win.copy_link(zdf_row.rec)
        check("Link kopieren (Typ url) = aktuelle Stream-URL", win.status.get_text() == "Kopiert: https://mock.invalid/zdf-v2/master.m3u8",
              win.status.get_text())
        # Sender verschwunden
        out_file.unlink(missing_ok=True)
        win.play_id("oer:gibtesnicht", "Weg", "url")
        check("Unbekannter Sender: Fehlermeldung, kein Prozess", pump(3, lambda: win.notice.get_visible() and "nicht (mehr) in der Liste" in win.notice.get_text())
              and not out_file.exists() and not win.play_busy, win.notice.get_text())
        # Quelle faellt aus: Liste bleibt, Meldung im Label
        MockState.zapp_mode = "fail"
        win.refresh_oer(force=True)
        check("Quelle nicht erreichbar: Liste bleibt, Label nennt den Grund",
              pump(5, lambda: not win.oer_busy and "nicht erreichbar" in win.oer_src.get_text())
              and len(list(win._rows(win.oer_list))) == 7, win.oer_src.get_text())
        MockState.zapp_mode = "ok"
        check("Strg+R im OER-Tab laedt die Senderliste neu (nicht die Suche)",
              (win.refresh_all(manual=True), pump(5, lambda: OER_STATE["source"] == "live" and not win.oer_busy))[1])
        win.on_window_key(None, Gdk.KEY_1, 0, Gdk.ModifierType.CONTROL_MASK)
        check("Strg+1 zurueck zur Suche", win.stack.get_visible_child_name() == "results")
        check("Zapp-Aufrufe liefen nur gegen den Mock (OER_API)", len(MockState.zapp_hits) >= 3
              and all(h.startswith("/zapp/v1/channelInfoList") for h in MockState.zapp_hits), MockState.zapp_hits[:3])

        # --- harte Grenzen ---
        bad = [h for h in MockState.hits if "/ace/cmd" in h or "method=stop" in h or "command" in h]
        check("command_url / method=stop wurde NIE aufgerufen (Mock-Zaehler = 0)", len(bad) == 0, bad)
        plain = [h for h in hits("/ace/getstream") if "format=json" not in h]
        check("Die App selbst holt nie ein getstream ohne format=json (Redirect holt nur der Player)", len(plain) == 0, plain)
        check("Alle HTTP-Ziele sind Engine-Pfade (nur ENGINE_URL)",
              all(h.startswith(("/webui/api/service", "/server/api", "/ace/getstream", "/ace/stat/")) for h in MockState.hits),
              sorted({h.split("?")[0] for h in MockState.hits}))

    def hook(app):
        nonlocal ok
        try:
            run_gui_tests(app)
        except Exception:
            ok = False
            traceback.print_exc()
            print("FAIL Ausnahme im GUI-Test", flush=True)
        finally:
            app.quit()
        return False

    App = run_gui(selftest_hook=hook)
    App().run([sys.argv[0]])
    srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    print("ERGEBNIS:", "OK" if ok else "FEHLER")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
