#!/usr/bin/env bash
#
# join_wifi.sh -- put the Pi on an ordinary Wi-Fi network (your office "Guest"
# SSID), headlessly, and report everything you need to know before carrying it
# to the tank.
#
# Run it over the Ethernet cable while the Pi is still on your desk:
#
#   scp join_wifi.sh pi@raspberrypi.local:~
#   ssh pi@raspberrypi.local
#   chmod +x join_wifi.sh
#   sudo ./join_wifi.sh --ssid Guest --pass 'the-wifi-password'
#
# Options:
#   --ssid NAME     network to join                      (required)
#   --pass SECRET   WPA2 password; omit for an open network
#   --country CC    regulatory domain, 2 letters         (default IN)
#   --iface NAME    wireless interface                   (default wlan0)
#   --name CONN     NetworkManager connection name       (default wq-wifi)
#   --status        show what it is connected to now, change nothing
#   --forget        remove the saved network
#
# This is the opposite of setup_hotspot.sh: that one makes the Pi *be* a
# network, this one makes it *join* one. A single radio cannot do both, so
# running this removes an access point configured earlier.

set -euo pipefail

SSID=""
PASSWORD=""
COUNTRY="IN"
IFACE="wlan0"
CONN="wq-wifi"
MODE="join"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ssid)    SSID="$2";     shift 2 ;;
    --pass)    PASSWORD="$2"; shift 2 ;;
    --country) COUNTRY="$2";  shift 2 ;;
    --iface)   IFACE="$2";    shift 2 ;;
    --name)    CONN="$2";     shift 2 ;;
    --status)  MODE="status"; shift ;;
    --forget)  MODE="forget"; shift ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; }
die()  { printf '  \033[31merror\033[0m %s\n' "$*" >&2; exit 1; }

show_status() {
  say "Wireless"
  nmcli -f IN-USE,SSID,SIGNAL,BARS,FREQ,SECURITY device wifi list ifname "$IFACE" 2>/dev/null | head -12 || true
  say "This Pi"
  echo "  hostname   $(hostname)"
  echo "  addresses  $(hostname -I 2>/dev/null)"
  ip route | sed -n 's/^default/  gateway   /p'
  say "Connectivity"
  if ping -c1 -W2 "$(ip route | awk '/^default/{print $3; exit}')" >/dev/null 2>&1; then
    ok "the gateway answers"
  else
    warn "the gateway does not answer ping (some networks block it)"
  fi
  if ping -c1 -W3 1.1.1.1 >/dev/null 2>&1; then
    ok "the internet is reachable -- the MQTT path will work"
  else
    warn "no internet -- the MQTT path will NOT work from here"
  fi
  if getent hosts hivemq.cloud >/dev/null 2>&1 || getent hosts google.com >/dev/null 2>&1; then
    ok "DNS resolves"
  else
    warn "DNS does not resolve"
  fi
  say "Can anything reach this Pi?"
  echo "  From the PC, try:  ping $(hostname -I | awk '{print $1}')"
  echo "  Many guest networks block that on purpose. If it fails, use the MQTT"
  echo "  path -- see the office tank guide."
  say "Is the sensor server up?"
  if ss -ltn 2>/dev/null | grep -q ':8000 '; then ok "listening on 8000"; else warn "nothing on port 8000"; fi
}

case "$MODE" in
  status) show_status; exit 0 ;;
  forget)
    [[ $EUID -eq 0 ]] || die "run with sudo"
    nmcli connection delete "$CONN" 2>/dev/null && ok "removed '$CONN'" || warn "no '$CONN' saved"
    exit 0 ;;
esac

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ -n "$SSID" ]] || die "--ssid is required"
command -v nmcli >/dev/null || die "NetworkManager is not installed (needs Raspberry Pi OS Bookworm or newer)"

say "Preparing the radio"
rfkill unblock wifi 2>/dev/null || true
nmcli device status | grep -q "^$IFACE " || die "$IFACE does not exist -- check 'nmcli device status'"
if command -v raspi-config >/dev/null; then
  raspi-config nonint do_wifi_country "$COUNTRY" >/dev/null 2>&1 && ok "regulatory domain $COUNTRY"
fi

# An access point profile and a client profile fight over the same radio.
if nmcli -t -f NAME,TYPE connection show | grep -q '^wq-ap:'; then
  nmcli connection modify wq-ap connection.autoconnect no 2>/dev/null || true
  nmcli connection down wq-ap >/dev/null 2>&1 || true
  warn "disabled the 'wq-ap' hotspot -- one radio cannot do both"
fi

say "Joining $SSID"
nmcli connection delete "$CONN" >/dev/null 2>&1 || true
if [[ -n "$PASSWORD" ]]; then
  nmcli connection add type wifi ifname "$IFACE" con-name "$CONN" ssid "$SSID" >/dev/null
  nmcli connection modify "$CONN" \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$PASSWORD"
else
  nmcli connection add type wifi ifname "$IFACE" con-name "$CONN" ssid "$SSID" >/dev/null
  warn "no password given -- joining as an open network"
fi

nmcli connection modify "$CONN" \
  802-11-wireless.powersave 2 \
  ipv4.method auto \
  connection.autoconnect yes \
  connection.autoconnect-priority 50 \
  connection.autoconnect-retries 0
ok "power saving off, reconnects by itself, starts at boot"

chmod 600 "/etc/NetworkManager/system-connections/$CONN.nmconnection" 2>/dev/null || true

nmcli connection up "$CONN" >/dev/null || die "could not join. Wrong password, or out of range.
  Check what is in range:  nmcli device wifi list
  Check the log:           journalctl -u NetworkManager -n 40"
sleep 4

ADDR=$(ip -4 addr show "$IFACE" | sed -n 's/^ *inet \([0-9.]*\).*/\1/p' | head -1)
[[ -n "$ADDR" ]] || die "joined but got no address -- the network's DHCP did not answer"
ok "joined $SSID as $ADDR"

show_status

cat <<EOF

  Write this address down: $ADDR
  It comes from DHCP and will change, so do not rely on it. 'raspberrypi.local'
  usually follows the Pi, but plenty of guest networks block that too.

  Next, from the PC:   ping $ADDR
    it replies      -> the direct path works: python3 pc_dashboard.py --host $ADDR
    it times out    -> the network isolates its clients, which is normal for a
                       guest SSID. Use the MQTT path; see the office tank guide.

EOF
