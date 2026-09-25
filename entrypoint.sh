#!/bin/sh
set -e

# Als root gestartet (Normalfall): Benutzer-/Gruppen-ID anpassen, die
# Datenordner übernehmen und dann ohne root weiterlaufen.
# PUID/PGID wie bei Unraid üblich (99/100); ohne Angabe bleibt es bei 1000.
if [ "$(id -u)" = "0" ]; then
    if [ -n "$PGID" ] && [ "$PGID" != "$(id -g appuser)" ]; then
        groupmod -o -g "$PGID" appuser
    fi
    if [ -n "$PUID" ] && [ "$PUID" != "$(id -u appuser)" ]; then
        usermod -o -u "$PUID" appuser
    fi
    chown -R appuser:appuser /output /logs /config 2>/dev/null || true
    exec gosu appuser "$@"
fi

# Mit --user gestartet: so wie angegeben laufen, die Ordner müssen dann schon beschreibbar sein
exec "$@"
