#!/bin/sh
# Platzhalter fuer das "engine"-Kommando des acestream-launcher.
# Der Launcher startet NIE selbst eine Engine (das waere als normaler Desktop-User, ohne VPN-Routing/Killswitch).
# Stattdessen: klare Meldung und sofortiger Abbruch des Launchers. Engine startet nur ueber 'acestream-start'.
#
# Warum kill auf den Launcher: Der Launcher wartet nach dem Start des Engine-Kommandos erst
# 'timeout' Sekunden (30) auf die API, bevor er das Ende des Kommandos bemerkt. Ohne kill gaebe es
# 30 s Totzeit ohne Meldung. Der Launcher hat zu diesem Zeitpunkt noch keinen Player/Stream.
# Ausserdem faengt der Launcher stdout/stderr dieses Skripts ab (PIPE) und hat kein
# Kontroll-Terminal (setsid); darum Desktop-Benachrichtigung + Direktausgabe aufs Launcher-Terminal.
msg="AceStream-Engine laeuft nicht (127.0.0.1:6878). Erst 'acestream-start' ausfuehren, dann den Link erneut oeffnen."
notify-send -u critical -a "AceStream" "AceStream: Engine fehlt" "$msg" >/dev/null 2>&1
tty="$(readlink "/proc/$PPID/fd/0" 2>/dev/null)"
if [ -c "$tty" ] && [ -w "$tty" ]; then
  printf '\n%s\n' "$msg" > "$tty" 2>/dev/null
fi
if tr '\0' ' ' < "/proc/$PPID/cmdline" 2>/dev/null | grep -q 'acestream.launcher'; then
  kill -TERM "$PPID" 2>/dev/null
fi
exit 1
