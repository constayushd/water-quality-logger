#!/usr/bin/env bash
#
# setup_hotspot.sh -- turn the Raspberry Pi into its own Wi-Fi access point,
# so the laptop can connect to it directly at a site with no network.
#
# Run this ONCE on the Pi, over the Ethernet cable you are using now (or with a
# screen attached). After it finishes, the Pi stops being a Wi-Fi client and
# starts being an access point -- at every boot, with no internet needed.
#
#   scp setup_hotspot.sh pi@raspberrypi.local:~
#   ssh pi@raspberrypi.local
#   chmod +x setup_hotspot.sh
#   sudo ./setup_hotspot.sh --ssid WQ-FIELD --pass 'choose-a-password' --country IN
#
# Options (all have defaults):
#   --ssid NAME       network name the laptop will see     (default WQ-FIELD)
#   --pass SECRET     WPA2 password, 8-63 characters       (default wq-monitor)
#   --country CC      regulatory domain, 2 letters         (default IN)
#   --ip ADDR/PREFIX  the Pi's address on its own network  (default 192.168.4.1/24)
#   --channel N       2.4 GHz channel, 1 / 6 / 11          (default 6)
#   --iface NAME      wireless interface                   (default wlan0)
#   --name CONN       NetworkManager connection name       (default wq-ap)
#   --status          show what is configured now, change nothing
#   --revert          delete the AP and go back to being a Wi-Fi client
#
# Needs Raspberry Pi OS Bookworm or newer (NetworkManager). On Bullseye or
# older, see the note at the bottom of this file.

set -euo pipefail

SSID="WQ-FIELD"
PASSWORD="wq-monitor"
COUNTRY="IN"
ADDRESS="192.168.4.1/24"
CHANNEL="6"
IFACE="wlan0"
CONN="wq-ap"
MODE="setup"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ssid)    SSID="$2";    shift 2 ;;
    --pass)    PASSWORD="$2"; shift 2 ;;
    --country) COUNTRY="$2"; shift 2 ;;
    --ip)      ADDRESS="$2"; shift 2 ;;
    --channel) CHANNEL="$2"; shift 2 ;;
    --iface)   IFACE="$2";   shift 2 ;;
    --name)    CONN="$2";    shift 2 ;;
    --status)  MODE="status"; shift ;;
    --revert)  MODE="revert"; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; }
die()  { printf '  \033[31merror\033[0m %s\n' "$*" >&2; exit 1; }

IP_ONLY="${ADDRESS%%/*}"

# --------------------------------------------------------------------------
show_status() {
  say "Interface"
  nmcli device status | awk -v i="$IFACE" 'NR==1 || $1==i'
  say "Connection"
  if nmcli -t -f NAME connection show | grep -qx "$CONN"; then
    nmcli -f connection.id,connection.autoconnect,802-11-wireless.mode,\
802-11-wireless.ssid,802-11-wireless.band,802-11-wireless.channel,\
802-11-wireless.powersave,ipv4.method,ipv4.addresses \
      connection show "$CONN" 2>/dev/null || nmcli connection show "$CONN" | head -30
  else
    warn "no connection named '$CONN' -- the hotspot is not set up yet"
  fi
  say "Address"
  ip -4 addr show "$IFACE" | sed -n 's/^ *inet /  /p' || true
  say "Regulatory domain"
  iw reg get 2>/dev/null | sed -n 's/^country/  country/p' | head -2 || warn "iw not installed"
  say "Clients connected"
  if [[ -r /var/lib/misc/dnsmasq.leases ]]; then
    awk '{printf "  %-16s %s\n", $3, $4}' /var/lib/misc/dnsmasq.leases
  fi
  ip neigh show dev "$IFACE" 2>/dev/null | awk '$3=="lladdr"{printf "  %-16s %s\n", $1, $5}' || true
  say "Sensor server"
  if ss -ltn 2>/dev/null | grep -q ':8000 '; then
    ok "listening on port 8000"
  else
    warn "nothing is listening on port 8000 -- is pi_server.py running?"
  fi
}

revert() {
  say "Removing the access point"
  nmcli connection delete "$CONN" 2>/dev/null && ok "deleted '$CONN'" || warn "no '$CONN' to delete"
  systemctl disable --now wq-ap-watchdog.timer 2>/dev/null && ok "watchdog stopped" || true
  nmcli device disconnect "$IFACE" 2>/dev/null || true
  nmcli device connect "$IFACE" 2>/dev/null || true
  ok "the Pi is a Wi-Fi client again (it will rejoin a saved network)"
}

# --------------------------------------------------------------------------
case "$MODE" in
  status) show_status; exit 0 ;;
  revert) [[ $EUID -eq 0 ]] || die "run with sudo"; revert; exit 0 ;;
esac

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ ${#PASSWORD} -ge 8 && ${#PASSWORD} -le 63 ]] || die "the password must be 8-63 characters"
command -v nmcli >/dev/null || die "NetworkManager is not installed -- see the note at the end of this file"

say "Checking the radio"
rfkill unblock wifi 2>/dev/null || true
if ! nmcli device status | grep -q "^$IFACE "; then
  die "$IFACE does not exist. Run 'nmcli device status' to see the real name."
fi
ok "$IFACE is present"

# A Pi with no country set refuses to transmit on most channels.
if command -v raspi-config >/dev/null; then
  raspi-config nonint do_wifi_country "$COUNTRY" && ok "regulatory domain set to $COUNTRY"
else
  iw reg set "$COUNTRY" 2>/dev/null && ok "regulatory domain set to $COUNTRY" \
    || warn "could not set the country -- the AP may refuse to start"
fi

say "Configuring the access point"
nmcli connection delete "$CONN" >/dev/null 2>&1 || true
nmcli connection add type wifi ifname "$IFACE" con-name "$CONN" ssid "$SSID" >/dev/null
ok "created '$CONN' with SSID $SSID"

nmcli connection modify "$CONN" \
  802-11-wireless.mode ap \
  802-11-wireless.band bg \
  802-11-wireless.channel "$CHANNEL" \
  802-11-wireless.hidden no \
  802-11-wireless.powersave 2 \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.proto rsn \
  wifi-sec.pairwise ccmp \
  wifi-sec.group ccmp \
  wifi-sec.psk "$PASSWORD" \
  ipv4.method shared \
  ipv4.addresses "$ADDRESS" \
  ipv6.method disabled \
  connection.autoconnect yes \
  connection.autoconnect-priority 100 \
  connection.autoconnect-retries 0
ok "WPA2 on channel $CHANNEL, Pi at $IP_ONLY, power saving off, starts at boot"

# The config file holds the Wi-Fi password.
chmod 600 "/etc/NetworkManager/system-connections/$CONN.nmconnection" 2>/dev/null || true

say "Starting it"
nmcli connection up "$CONN" >/dev/null || die "the AP failed to start -- run: journalctl -u NetworkManager -n 50"
sleep 3

if ip -4 addr show "$IFACE" | grep -q "inet ${IP_ONLY}"; then
  ok "$IFACE is up at $IP_ONLY"
else
  warn "$IFACE does not have $IP_ONLY yet -- check 'nmcli device status'"
fi

say "Done. From the laptop:"
cat <<EOF

  1. Join the Wi-Fi network   $SSID   (password: the one you set)
  2. The laptop gets an address from the Pi automatically, e.g. 192.168.4.x
  3. Then:

       ping $IP_ONLY
       python3 pc_dashboard.py --host $IP_ONLY

     or just open  http://$IP_ONLY:8000/  in a browser.

  The Pi keeps this network up at every boot. There is no internet through it
  -- it is a private link between the Pi and whoever connects.

  To check on it later:   sudo ./setup_hotspot.sh --status
  To undo it:             sudo ./setup_hotspot.sh --revert

EOF

# --------------------------------------------------------------------------
# Raspberry Pi OS Bullseye or older (no NetworkManager)
#
# Either upgrade to Bookworm (much simpler), or install the old stack:
#
#   sudo apt install -y hostapd dnsmasq
#   sudo systemctl unmask hostapd
#   # /etc/hostapd/hostapd.conf
#   #   interface=wlan0
#   #   ssid=WQ-FIELD
#   #   hw_mode=g
#   #   channel=6
#   #   wpa=2
#   #   wpa_passphrase=your-password
#   #   wpa_key_mgmt=WPA-PSK
#   #   rsn_pairwise=CCMP
#   #   country_code=IN
#   # /etc/dnsmasq.conf
#   #   interface=wlan0
#   #   dhcp-range=192.168.4.10,192.168.4.50,255.255.255.0,24h
#   # /etc/dhcpcd.conf
#   #   interface wlan0
#   #   static ip_address=192.168.4.1/24
#   #   nohook wpa_supplicant
#   sudo systemctl enable --now hostapd dnsmasq
