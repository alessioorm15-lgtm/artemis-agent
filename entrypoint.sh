#!/usr/bin/env bash
set -e

echo "=== Starting Artemis Mobile Cloud Agent ==="

# 1. Restore authorized ADB key if provided
mkdir -p /root/.android
if [ -n "$ADB_KEY_B64" ]; then
    echo "Restoring ADB RSA keys..."
    echo "$ADB_KEY_B64" | base64 -d > /root/.android/adbkey
    chmod 600 /root/.android/adbkey
    if [ -n "$ADB_PUB_B64" ]; then
        echo "$ADB_PUB_B64" | base64 -d > /root/.android/adbkey.pub
    fi
fi

# 2. Start Tailscale userspace daemon
if [ -n "$TS_AUTHKEY" ]; then
    echo "Starting Tailscale userspace networking..."
    mkdir -p /var/run/tailscale /var/lib/tailscale
    tailscaled --tun=userspace-networking --socks5-server=localhost:1055 --outbound-http-proxy-listen=localhost:1055 &
    sleep 3
    tailscale up --authkey="$TS_AUTHKEY" --hostname="artemis-cloud"
    echo "Tailscale connected successfully!"

    # 3. Bridge local port 5555 to phone Tailscale IP
    PHONE_IP="${PHONE_TS_IP:-100.116.147.90}"
    echo "Forwarding 127.0.0.1:5555 -> $PHONE_IP:5555 via Tailscale..."
    socat TCP-LISTEN:5555,fork EXEC:"tailscale nc $PHONE_IP 5555" &
    sleep 2

    # 4. Connect ADB
    echo "Connecting ADB..."
    adb connect 127.0.0.1:5555 || true
    adb devices
fi

# 5. Start web server
PORT="${PORT:-10000}"
echo "Starting Artemis Admin Console on port $PORT..."
exec uvicorn apps.admin_console.server:app --host 0.0.0.0 --port "$PORT"
