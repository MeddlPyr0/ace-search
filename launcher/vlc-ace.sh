#!/bin/sh
# VLC mit Qualitaetsoptionen NUR fuer AceStream (globale vlcrc bleibt unveraendert).
# Wird vom acestream-launcher als "player" aufgerufen (letztes Argument = M3U/URL) und von ace-play.
#
# Optionen (Belege: vlc -H --advanced):
#  --network-caching=3000   Puffer fuer HTTP-Quelle in ms (Default 1000). Faengt Engine/P2P-Schwankungen ab.
#  --http-reconnect         bei unerwartet geschlossener Verbindung automatisch neu verbinden.
#  --avcodec-hw=none        Software-Decode. Grund: Mit VA-API verwirft VLC 3.0.24 bei 50-fps-Deinterlacing
#                           ~die Haelfte der Bilder ("main filter warning: dropping pictures"); SW-Decode
#                           liefert volle 50 Halbbilder/s. Auf einer modernen Desktop-CPU: 1080i50 H.264 = ca. 12 % eines Kerns.
#  --deinterlace=-1         automatisch: nur Bilder, die als interlaced markiert sind (1080i/576i), werden
#                           deinterlaced; progressive Streams (720p50, 1080p) bleiben unberuehrt.
#  --deinterlace-mode=yadif  ruhig (kein Zeilenwackeln wie bei linear/bob), aber nur 25 B/s. Standard-Player ist mpv+bwdif (50 B/s), siehe player-ace.sh.
#                           (linear/bob: 50 B/s, aber statische Overlays springen pro Halbbild -> Rattern; yadif2x stuerzt ab.)
#  --play-and-exit          VLC beendet sich am Stream-Ende (der Launcher raeumt dann auf).
#
# Umschalter per Umgebungsvariable:
#   ACE_TITLE=<Name>      Titel statt Stream-URL (setzt die App "AceStream Suche"); leer = keine Aenderung
#                         (--meta-title = Input-Metadaten/Playlist, --video-title = Fenstertitel; je EIN Argument)
#   ACE_HW=1              VA-API-Hardwaredecode erlauben (--avcodec-hw=any); Deinterlacing dann nur 25 B/s
#   ACE_DEINT=<modus>     anderer Modus: linear/bob (50 B/s, wackelt), yadif (Default), phosphor, blend, x, ...
#   ACE_CACHE=<ms>        anderes Netzwerk-Caching (z. B. 5000 bei Aussetzern, 1500 fuer weniger Verzoegerung)
hw=none
[ "${ACE_HW:-0}" = "1" ] && hw=any
if [ -n "${ACE_TITLE:-}" ]; then set -- "--meta-title=$ACE_TITLE" "--video-title=$ACE_TITLE" "$@"; fi
exec vlc \
  --network-caching="${ACE_CACHE:-3000}" \
  --http-reconnect \
  --avcodec-hw="$hw" \
  --deinterlace=-1 \
  --deinterlace-mode="${ACE_DEINT:-yadif}" \
  --play-and-exit \
  "$@"
