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
    # In-memory state = ephemeral node: removed from the tailnet when the container stops,
    # so restarts no longer pile up offline "artemis-cloud-N" machines.
    tailscaled --state=mem: --tun=userspace-networking --socks5-server=localhost:1055 --outbound-http-proxy-listen=localhost:1055 &
    sleep 3
    # A Tailscale failure must not kill the web server (set -e would crash-loop the container).
    if tailscale up --authkey="$TS_AUTHKEY" --hostname="artemis-cloud"; then
        echo "Tailscale connected successfully!"
    else
        echo "WARNING: tailscale up failed, continuing without the phone link"
    fi

    # 3. Bridge local port 5555 to phone Tailscale IP
    PHONE_IP="${PHONE_TS_IP:-100.116.147.90}"
    echo "Forwarding 127.0.0.1:5555 -> $PHONE_IP:5555 via Tailscale..."
    socat TCP-LISTEN:5555,fork,reuseaddr EXEC:"tailscale nc $PHONE_IP 5555" &
    sleep 2

    # 4. Connect ADB, then keep reconnecting: the phone may be offline at boot or drop later
    echo "Connecting ADB..."
    adb connect 127.0.0.1:5555 || true
    adb devices || true
    (
        while true; do
            sleep 30
            if ! adb devices | grep -q "^127.0.0.1:5555[[:space:]]*device"; then
                adb disconnect 127.0.0.1:5555 >/dev/null 2>&1 || true
                adb connect 127.0.0.1:5555 >/dev/null 2>&1 || true
            fi
        done
    ) &
fi

# 5. Keep the free Render instance awake on its own (backup for the external cron):
# Render sleeps after 15 min without inbound traffic; a request through the public URL counts.
if [ -n "$RENDER_EXTERNAL_URL" ]; then
    (
        sleep 60
        while true; do
            curl -fsS -m 30 "$RENDER_EXTERNAL_URL/api/status" >/dev/null 2>&1 || echo "keepalive ping failed"
            sleep 600
        done
    ) &
fi

# 6. Start web server
PORT="${PORT:-10000}"
echo "Starting Artemis Admin Console on port $PORT..."
exec uvicorn apps.admin_console.server:app --host 0.0.0.0 --port "$PORT"
