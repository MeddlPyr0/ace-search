# ace-search

A compact GTK4 / libadwaita search app for your **own local AceStream engine** (`127.0.0.1:6878`), plus a tab with the
**official public-broadcaster (German ARD/ZDF/... "ÖR") livestreams**. Search with category filters, favorites with
your own categories, history, live stream status (peers / speed) and engine status. Playback goes through a small,
swappable chain of wrapper scripts (`mpv` by default, `VLC` optional).

The repository also contains an optional, hardened **example setup** for running the engine: a dedicated system user,
VPN-only policy routing, an nftables kill switch, an input lock for the engine ports, and a launcher guard so that a
click on an `acestream://` link can never start an unprotected engine. See [docs/SETUP.md](docs/SETUP.md).

> The UI and the script messages are in German. The code is plain Python 3 with PyGObject; no extra pip packages.

## Disclaimer

- This tool **only talks to the search function of the AceStream engine running on your own machine**. It contains
  **no content, no channel lists and no links to streams** (favorites and categories start empty and are maintained
  by you alone).
- **You are solely responsible** for only using content you have the rights to watch. Check the laws that apply to you.
- The "OER" tab lists official, freely accessible livestreams of public broadcasters, loaded from the community
  [Zapp API](https://api.zapp.mediathekview.de/) of the MediathekView project (with a built-in fallback list of the
  broadcasters' own CDN URLs). Geo-restrictions of the broadcasters apply.
- This project is **not affiliated with** AceStream, the MediathekView project, any VPN provider or any broadcaster.
- The security example is provided as-is, without any warranty. Verify the leak tests yourself (docs/SETUP.md).
- Parts of this project were built with AI assistance.

## Features

- **Search** via the engine's search API, debounced auto-search, availability traffic light, bitrate, countries,
  language-aware sorting (German, English, others, disabled), optional "now playing" line when the engine provides EPG data.
- **Category chips** (all, sport, news, movies, documentaries, entertainment, kids, music, "more") with a *strict
  filter* (a channel counts only under its main category) and **your own keyword rules / exclusion words** per category.
- **Favorites** with your own categories (collapsible sections), manual add by content ID or `acestream://` link,
  rename, delete, copy link.
- **History** of the last 15 started streams, search history of the last 10 terms.
- **Playback** via `player-ace.sh` -> `mpv` (deinterlacing with `bwdif`, smooth frame pacing) or `VLC`; stream name shown
  as window title; live status (peers, download speed) while playing.
- **Engine control**: status badge, start/stop buttons that open `kitty -e acestream-start` / `acestream-stop`.
- **OER tab** (Ctrl+4): official livestreams, played directly with mpv (no AceStream, no VPN involved).
- The app never calls the engine's `command_url` / `method=stop` (it would end the session for all clients).
- Keyboard-driven (Ctrl+F, Ctrl+1..4, Ctrl+R/F5, `f`, `m`, F2, Del, Alt+Left/Right, ...).

More details (data files, API notes, environment variables) are documented in the docstrings of
[`app/ace_search.py`](app/ace_search.py); the self-test (below) doubles as an executable specification.

## Requirements

Tested on Arch-based systems (CachyOS) with Wayland.

- Python 3, GTK4, libadwaita, PyGObject (`python-gobject`, `gtk4`, `libadwaita`)
- `mpv` (default player); optional `vlc`
- `acestream-engine` (AUR) and optionally `acestream-launcher` (AUR) for `acestream://` links in the browser
- optional `kitty` (used by the "start/stop engine" buttons; change with `ACE_SEARCH_START_CMD` / `ACE_SEARCH_STOP_CMD`)
- for the secure engine setup: `nftables`, `iproute2`, `curl`, `sudo`, an OpenVPN tunnel (see docs/SETUP.md)

## Install

```sh
git clone https://github.com/MeddlPyr0/ace-search.git
cd ace-search
./app/install.sh                  # app + launcher entry in the application menu
./app/install.sh --with-launcher  # additionally the player wrappers / launcher guard (see below)
```

- The app goes to `~/.local/share/ace-search/`; the `.desktop` entry is written with your real `$HOME` path.
- `--with-launcher` copies `launcher/*` to `~/.config/acestream-launcher/` and turns `config.example` into `config`
  with your home directory filled in (the launcher does not expand `~`).
- Existing, different files are **not** overwritten silently: they get a `.bak-<date>` backup first
  (`--force` skips the backup). Identical files are left alone.
- Run directly without installing: `python3 app/ace_search.py` (wrappers are expected in
  `~/.config/acestream-launcher/`, or set `ACE_SEARCH_PLAYER`, `ACE_SEARCH_PLAY_CMD`, `ACE_SEARCH_MPV`).

### Root scripts (not installed automatically)

`acestream-start` and `acestream-stop` change routing and firewall rules, so you install them yourself, after
reading them and after creating the dedicated user:

```sh
sudo useradd -r -m -s /usr/bin/nologin acestreamvpn
sudo install -m 755 -o root -g root scripts/acestream-start scripts/acestream-stop /usr/local/bin/
```

Then start the engine in a terminal and keep it open: `acestream-start`. Details, assumptions (OpenVPN `tun*`
interface, `sudo` rights for the engine user, PIA as the example VPN) and leak tests: [docs/SETUP.md](docs/SETUP.md).

## Security concept (short version)

| Layer | What it does |
|---|---|
| Dedicated system user | The engine runs as its own user (`nologin`), so firewall / routing rules can target exactly it. |
| Policy routing by UID | `ip rule uidrange <uid>-<uid>` sends everything from that user into a table whose only route is the VPN tunnel. |
| nftables kill switch | `meta skuid <uid>`: only `lo` and the VPN interface are allowed on output, everything else is dropped. |
| Input lock | The engine ports (6878, 62062, 8621) accept connections from localhost only. |
| Leak test on start | `acestream-start` compares the engine user's public IP with the host's own and aborts when they match. |
| Launcher guard | `acestream-launcher` is configured to run `engine-guard.sh` instead of an engine: no engine running -> notification and abort, never an unprotected engine. |
| Fail-closed | If the VPN is down or the route is stale, the engine simply has no connectivity. |

The example uses OpenVPN (`tun*`) and Private Internet Access (`piactl`) for the connection check; any VPN that gives
you a `tun*` interface can be used by adapting step 1 of `acestream-start`. WireGuard interfaces are not detected.

## Self-test

```sh
python3 app/ace_search.py --selftest
```

Runs headless against a built-in mock engine, a mock Zapp API and fake players in a temp directory (238 checks).
It never touches your real engine or your data directory and never calls `command_url` / `method=stop`.

## Repository layout

```
app/        ace_search.py, ace-search.desktop (template), install.sh
scripts/    acestream-start, acestream-stop       (root, installed manually)
launcher/   config.example, engine-guard.sh, player-ace.sh, mpv-ace.sh, vlc-ace.sh, ace-play
docs/       SETUP.md  (architecture, setup, leak tests)
```

## Kurzfassung (Deutsch)

**ace-search** ist eine kompakte GTK4/libadwaita-App zum Suchen in der **eigenen, lokalen AceStream-Engine**
(`127.0.0.1:6878`), mit Favoriten, Verlauf, Live-Status und einem Tab mit den **offiziellen Livestreams der
öffentlich-rechtlichen Sender**. Dazu gibt es ein optionales Beispiel-Setup (eigener System-User, Routing nur über das
VPN, nftables-Killswitch, Eingangssperre, Launcher-Schutz).

- Installation: `./app/install.sh` (App) bzw. `./app/install.sh --with-launcher` (Player-Wrapper und Launcher-Schutz);
  vorhandene Dateien werden mit `.bak-<Datum>` gesichert. Die Root-Skripte (`scripts/`) installierst du bewusst selbst,
  siehe oben und [docs/SETUP.md](docs/SETUP.md) (Englisch).
- Selbsttest: `python3 app/ace_search.py --selftest`.
- **Hinweis:** Das Tool enthält keine Inhalte, Senderlisten oder Links zu Streams, sondern durchsucht nur die lokal
  laufende Engine. Du bist selbst dafür verantwortlich, nur Inhalte zu nutzen, für die du die Rechte hast. Die ÖR-Livestreams
  sind offizielle, frei zugängliche Angebote der Sender (es gelten deren Geo-Beschränkungen). Keine Verbindung zu
  AceStream, den Sendern oder VPN-Anbietern.

## License

[MIT](LICENSE), Copyright (c) 2026 MeddlPyr0.
