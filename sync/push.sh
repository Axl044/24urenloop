#!/usr/bin/env bash
# Pusht het publieke scorebord (index.html + scorebord.json) van de laptop naar de thuisserver,
# met rsync over ssh via Tailscale. Blijft draaien en probeert opnieuw als het netwerk wegvalt.
#
#   SYNC_TARGET=thuisserver: ./sync/push.sh
#
# SYNC_TARGET  rsync-bestemming, bv. "deploy@thuisserver:" (met rrsync) of "deploy@thuisserver:/srv/24urenloop-public/"
# SYNC_SRC     map met de export (standaard data/public/ naast dit script)
# SYNC_SECONDS pauze tussen twee pushes (standaard 15)
# SYNC_KEY     ssh-sleutel om mee te pushen (standaard de gewone ssh-instellingen)
set -u

here="$(cd "$(dirname "$0")/.." && pwd)"
src="${SYNC_SRC:-$here/data/public}"
target="${SYNC_TARGET:?zet SYNC_TARGET, bv. SYNC_TARGET=deploy@thuisserver:}"
every="${SYNC_SECONDS:-15}"
ssh_cmd="ssh -o BatchMode=yes -o ConnectTimeout=10"
[ -n "${SYNC_KEY:-}" ] && ssh_cmd="$ssh_cmd -o IdentitiesOnly=yes -i $SYNC_KEY"
failing=0

while true; do
  if [ -f "$src/scorebord.json" ]; then
    # Enkel de twee publieke bestanden; --delay-updates zodat bezoekers nooit een half bestand zien.
    if rsync -t --chmod=F644 --delay-updates --timeout=20 \
         -e "$ssh_cmd" \
         "$src/index.html" "$src/scorebord.json" "$target"; then
      [ "$failing" -eq 1 ] && echo "$(date +%T) sync hersteld"
      failing=0
    else
      [ "$failing" -eq 0 ] && echo "$(date +%T) sync mislukt, blijf proberen…" >&2
      failing=1
    fi
  else
    echo "$(date +%T) nog geen $src/scorebord.json (draait de server met PUBLIC_DIR?)" >&2
  fi
  sleep "$every"
done
