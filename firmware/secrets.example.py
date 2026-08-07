"""Copy this file to secrets.py and fill it in.

secrets.py is gitignored and must never be committed.
"""

WIFI_SSID = "your-wifi-ssid"
WIFI_PASSWORD = "your-wifi-password"

# Shared secret required on POST /toggle, the only state-changing endpoint.
# Generate one with:  python3 -c "import secrets; print(secrets.token_urlsafe(32))"
API_TOKEN = "replace-me-with-a-long-random-string"

# Optional: pin the device to a static LAN address so the client tools and
# your reverse proxy do not chase DHCP leases. Leave as None to use DHCP.
# Format: (ip, netmask, gateway, dns)
STATIC_IP = None
# STATIC_IP = ("192.168.1.50", "255.255.255.0", "192.168.1.1", "192.168.1.1")
