#!/usr/bin/env bash
# Installiert AceStream Suche (kein root noetig).
#
#   ./install.sh                  App nach ~/.local/share/ace-search/ und Starter ins App-Menue
#   ./install.sh --with-launcher  zusaetzlich die Launcher-Dateien nach ~/.config/acestream-launcher/
#                                 (config.example -> config mit eingesetztem Home-Pfad)
#   ./install.sh --force          vorhandene, abweichende Dateien ohne Backup ueberschreiben
#   ./install.sh --help
#
# Vorhandene, abweichende Dateien werden NICHT ungefragt ueberschrieben: Standard ist ein Backup
# mit Endung .bak-<Datum>, dann erst wird ersetzt. Identische Dateien bleiben unberuehrt.
# Die root-Skripte (scripts/acestream-start, scripts/acestream-stop) installiert dieses Skript NICHT.
# Siehe README.md und docs/SETUP.md.
set -eu

force=0
with_launcher=0
for arg in "$@"; do
  case "$arg" in
    --force) force=1 ;;
    --with-launcher) with_launcher=1 ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unbekannte Option: $arg (siehe --help)" >&2; exit 2 ;;
  esac
done

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(dirname "$here")"
stamp="$(date +%Y%m%d-%H%M%S)"
app_dir="$HOME/.local/share/ace-search"
apps_dir="$HOME/.local/share/applications"
launcher_dir="$HOME/.config/acestream-launcher"

# put <quelle> <ziel> <modus>: kopiert, sichert Abweichendes mit .bak-<Datum> (oder --force: ohne Backup)
put() {
  src="$1"; dst="$2"; mode="$3"
  if [ -e "$dst" ]; then
    if cmp -s "$src" "$dst"; then
      chmod "$mode" "$dst"
      echo "unveraendert: $dst"
      return 0
    fi
    if [ "$force" -eq 0 ]; then
      cp -p "$dst" "$dst.bak-$stamp"
      echo "Backup:       $dst.bak-$stamp"
    fi
  fi
  cp "$src" "$dst"
  chmod "$mode" "$dst"
  echo "installiert:  $dst"
}

mkdir -p "$app_dir" "$apps_dir"
put "$here/ace_search.py" "$app_dir/ace_search.py" 755

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
sed "s|@APP_DIR@|$app_dir|g" "$here/ace-search.desktop" > "$tmp"
put "$tmp" "$apps_dir/ace-search.desktop" 644
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$apps_dir" || true

if [ "$with_launcher" -eq 1 ]; then
  if [ ! -d "$repo/launcher" ]; then
    echo "FEHLER: $repo/launcher nicht gefunden (install.sh muss im Repo unter app/ liegen)." >&2
    exit 1
  fi
  mkdir -p "$launcher_dir"
  for f in engine-guard.sh player-ace.sh mpv-ace.sh vlc-ace.sh ace-play; do
    put "$repo/launcher/$f" "$launcher_dir/$f" 755
  done
  # Der Launcher expandiert "~" nicht: absoluten Home-Pfad in die config schreiben.
  sed "s|/home/YOUR_USER|$HOME|g" "$repo/launcher/config.example" > "$tmp"
  put "$tmp" "$launcher_dir/config" 644
  echo
  echo "Launcher-Dateien installiert. Die Engine-Skripte (acestream-start/stop) brauchen root:"
  echo "  sudo install -m 755 -o root -g root scripts/acestream-start scripts/acestream-stop /usr/local/bin/"
  echo "Vorher den System-User und die Firewall-Voraussetzungen einrichten (docs/SETUP.md)."
fi

echo
echo "Fertig. Starten: App-Menue 'AceStream Suche' oder  python3 $app_dir/ace_search.py"
