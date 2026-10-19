#!/usr/bin/env bash
#
# wq-ap-watchdog.sh -- keep the access point and the sensor server alive on a
# Pi nobody can walk up to.
#
# Run every minute by wq-ap-watchdog.timer. Three checks, cheapest first:
#
#   1. Is the AP connection active and does wlan0 have its address?
#      If not, bring it back up.
#   2. Has that failed repeatedly? Reload the Wi-Fi driver, which clears the
#      firmware wedge that brcmfmac occasionally gets into.
#   3. Is the sensor server answering on its port? If not, restart it.
#
# Installation is handled by wq-ap-watchdog.timer -- see its header.

set -uo pipefail

CONN="${WQ_AP_CONN:-wq-ap}"
IFACE="${WQ_AP_IFACE:-wlan0}"
EXPECT_IP="${WQ_AP_IP:-192.168.4.1}"
PORT="${WQ_PORT:-8000}"
SERVICE="${WQ_SERVICE:-wq-server}"
STATE="/var/lib/wq-ap-watchdog"
FAILFILE="$STATE/consecutive-failures"

mkdir -p "$STATE"
fails=$(cat "$FAILFILE" 2>/dev/null || echo 0)

log() { logger -t wq-ap-watchdog -- "$*"; echo "$*"; }

ap_healthy() {
  nmcli -t -f NAME connection show --active 2>/dev/null | grep -qx "$CONN" \
    && ip -4 addr show "$IFACE" 2>/dev/null | grep -q "inet ${EXPECT_IP}"
}

# ---- 1. the access point ---------------------------------------------------
if ap_healthy; then
  [[ "$fails" -gt 0 ]] && log "AP recovered after $fails failed check(s)"
  echo 0 > "$FAILFILE"
else
  fails=$((fails + 1))
  echo "$fails" > "$FAILFILE"
  log "AP '$CONN' is not healthy (failure $fails) -- bringing it up"
  rfkill unblock wifi 2>/dev/null
  nmcli connection up "$CONN" >/dev/null 2>&1

  # ---- 2. still broken after three tries: reset the radio itself -----------
  if [[ "$fails" -ge 3 ]] && ! ap_healthy; then
    log "reloading the Wi-Fi driver"
    modprobe -r brcmfmac_wcc 2>/dev/null
    modprobe -r brcmfmac 2>/dev/null
    sleep 2
    modprobe brcmfmac 2>/dev/null
    sleep 5
    systemctl restart NetworkManager
    sleep 5
    nmcli connection up "$CONN" >/dev/null 2>&1
    echo 0 > "$FAILFILE"
  fi
fi

# ---- 3. the sensor server --------------------------------------------------
# Only meaningful if you installed wq-server.service. Readings matter more than
# the network: a Pi serving nothing is as useless as a Pi nobody can reach.
if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE}.service"; then
  if ! ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
    log "nothing listening on port $PORT -- restarting $SERVICE"
    systemctl restart "$SERVICE"
  fi
fi

exit 0
