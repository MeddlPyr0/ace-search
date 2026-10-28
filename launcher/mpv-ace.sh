#!/bin/sh
# mpv fuer AceStream: ruhiges Bild + 50 B/s bei 1080i50-Sport.
# Wird vom acestream-launcher (letztes Argument = M3U/URL) bzw. ace-play ueber player-ace.sh aufgerufen.
# Belegt an einem 1080i50-TFF-H.264-Stream: VLC "linear"/"bob" lassen statische Overlays (Logo,
# Score-Bug, Bandenschrift) pro Halbbild um eine Zeile springen; bwdif (bewegungsadaptiv) nicht.
#
#  --vf=bwdif=mode=send_field:parity=auto:deint=interlaced
#       bewegungsadaptives Deinterlacing mit voller Halbbildrate (50 B/s). deint=interlaced: als progressiv
#       markierte Streams (720p50, 1080p) werden NICHT angefasst/verdoppelt. parity=auto: Stream meldet TFF.
#  --deinterlace=no     schaltet das "deinterlace=yes" aus ~/.config/mpv/mpv.conf ab (sonst doppelt).
#  --hwdec=no           Software-Decode (wie in einer typischen mpv.conf); 1080i50 H.264 kostet auf einer modernen Desktop-CPU ca. 25 % eines Kerns.
#  --video-sync=display-resample --interpolation --tscale=oversample
#       50 B/s auf einem Monitor mit nicht ganzzahligem Vielfachen ungleichmaessig (z. B. 144/50 = 2,88). Resample + oversample
#       gibt gleichmaessiges Frame-Pacing (kein Raten/Wackeln bei Schwenks). Aus: ACE_INTERP=0
#  --glsl-shaders=      leert den Schaerfe-Shader aus mpv.conf (der Stream ist von der Produktion schon
#       kantenueberhoeht). Wieder an: ACE_SHARPEN=1
#  --cache/--cache-pause-*  ~3 s Anlaufpuffer, danach Pause + Neupuffern bei Unterlauf (ACE_CACHE_S)
#  --stream-lavf-o=reconnect...  automatisch neu verbinden (wie VLC --http-reconnect)
#  --brightness/--gamma/--contrast/--saturation  Farbe nur fuer AceStream, unabhaengig von ~/.config/mpv/mpv.conf
#       (dort gesetzte gamma-/brightness-Werte wuerden sonst auch den Stream veraendern; fuer normale Videos bleiben sie).
#       Standard: brightness 0, gamma 2 (empirisch gewaehlt), contrast 0, saturation 0.
#       Live im mpv-Fenster: 1/2 Kontrast -/+, 3/4 Helligkeit -/+, 5/6 Gamma -/+, 7/8 Saettigung -/+ (je +-1).
#       Gefundenen Wert hier eintragen, z. B. ACE_SAT=5 oder ACE_BRIGHT=1 (ganze Zahlen, -100..100).
#
# Titel: ACE_TITLE=<Name> (setzt die App "AceStream Suche" fuer den Player-Start) -> mpv zeigt den Namen statt der
#        Stream-URL (--force-media-title, als EIN Argument, Sonderzeichen/Leerzeichen sind unkritisch).
#        Leer/nicht gesetzt (z. B. Start aus dem Browser ueber den Launcher): keine Aenderung.
# Umschalter: ACE_CACHE_S=<s> (Default 3), ACE_INTERP=0, ACE_SHARPEN=1, ACE_DEINT_MODE=send_frame (25 B/s),
#             ACE_BRIGHT=<n> (0), ACE_GAMMA=<n> (2), ACE_CONTRAST=<n> (0), ACE_SAT=<n> (0)
interp=yes; vsync=display-resample
if [ "${ACE_INTERP:-1}" = "0" ]; then interp=no; vsync=audio; fi
shaders="--glsl-shaders="
[ "${ACE_SHARPEN:-0}" = "1" ] && shaders="--glsl-shaders-append="
# Farbwerte: nur ganze Zahlen -100..100, sonst Standard (Tippfehler soll mpv nicht abbrechen)
cval() { case "$1" in ''|*[!0-9-]*|-|*?-*) echo "$2";; *) [ "$1" -ge -100 ] && [ "$1" -le 100 ] && echo "$1" || echo "$2";; esac; }
bright=$(cval "${ACE_BRIGHT:-0}" 0)
gam=$(cval "${ACE_GAMMA:-2}" 2)
contr=$(cval "${ACE_CONTRAST:-0}" 0)
sat=$(cval "${ACE_SAT:-0}" 0)
if [ -n "${ACE_TITLE:-}" ]; then set -- "--force-media-title=$ACE_TITLE" "$@"; fi
exec mpv \
  --brightness="$bright" \
  --gamma="$gam" \
  --contrast="$contr" \
  --saturation="$sat" \
  --vf="bwdif=mode=${ACE_DEINT_MODE:-send_field}:parity=auto:deint=interlaced" \
  --deinterlace=no \
  --hwdec=no \
  --vo=gpu-next \
  --video-sync="$vsync" \
  --interpolation="$interp" \
  --tscale=oversample \
  $shaders \
  --cache=yes \
  --demuxer-max-bytes=300M \
  --demuxer-max-back-bytes=100M \
  --cache-pause=yes \
  --cache-pause-initial=yes \
  --cache-pause-wait="${ACE_CACHE_S:-3}" \
  --network-timeout=60 \
  --stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=5 \
  "$@"
