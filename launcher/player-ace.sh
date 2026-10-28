#!/bin/sh
# Umschalter fuer den Player des acestream-launcher (der Launcher teilt "player" per str.split() -> kein Quoting).
# Wahl: Umgebungsvariable ACE_PLAYER=mpv|vlc, sonst Datei ~/.config/acestream-launcher/player.choice (Inhalt: mpv oder vlc),
# sonst Standard mpv. Die Datei wirkt auch bei Start aus dem Browser (acestream://-Link), wo keine Umgebungsvariable ankommt.
here="$(dirname "$(readlink -f "$0")")"
choice="${ACE_PLAYER:-}"
[ -z "$choice" ] && [ -r "$here/player.choice" ] && choice="$(tr -d ' \n\r\t' < "$here/player.choice")"
case "$choice" in
  vlc) exec "$here/vlc-ace.sh" "$@" ;;
  *)   exec "$here/mpv-ace.sh" "$@" ;;
esac
