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

    # 3. Bridge a local port to the phone's Tailscale IP.
    # Not 5555: adb scans localhost 5555-5585 for emulators and would list the phone twice
    # (127.0.0.1:5555 + emulator-5554), so two tasks could grab the same phone.
    PHONE_IP="${PHONE_TS_IP:-100.116.147.90}"
    LOCAL_ADB="127.0.0.1:${LOCAL_ADB_PORT:-6555}"
    echo "Forwarding $LOCAL_ADB -> $PHONE_IP:5555 via Tailscale..."
    socat TCP-LISTEN:"${LOCAL_ADB##*:}",bind=127.0.0.1,fork,reuseaddr EXEC:"tailscale nc $PHONE_IP 5555" &
    sleep 2

    # 4. Connect ADB, then keep reconnecting: the phone may be offline at boot or drop later
    echo "Connecting ADB..."
    adb connect "$LOCAL_ADB" || true
    adb devices || true
    (
        while true; do
            sleep 30
            if ! adb devices | grep -q "^$LOCAL_ADB[[:space:]]*device"; then
                adb disconnect "$LOCAL_ADB" >/dev/null 2>&1 || true
                adb connect "$LOCAL_ADB" >/dev/null 2>&1 || true
            fi
        done
    ) &

    # 4b. Port 5555 closes on every phone reboot: reopen it via wireless debugging, no PC needed
    python -m apps.admin_console.routers.phone &
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
