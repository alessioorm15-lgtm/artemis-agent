"""Cloud phone link: reopen the phone's ADB port 5555 through Android wireless debugging.

The cloud container reaches the phone only through Tailscale (userspace networking), so every
phone port is exposed locally with a socat bridge. Port 5555 closes on every phone reboot; this
router pairs/connects to the wireless-debugging port (random, read from the phone screen) and runs
`adb tcpip 5555`, after which the permanent 127.0.0.1:6555 bridge from entrypoint.sh works again.
"""

import asyncio
import os
import secrets
import subprocess

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

router = APIRouter(tags=["phone"])

ADB = os.getenv("ARTEMIS_ADB_PATH") or "adb"
PHONE_IP = os.getenv("PHONE_TS_IP", "100.116.147.90")
MAIN_SERIAL = f"127.0.0.1:{os.getenv('LOCAL_ADB_PORT', '6555')}"
PAIR_LOCAL_PORT = 6556
DEBUG_LOCAL_PORT = 6557

_bridges: dict[int, subprocess.Popen] = {}
_lock = asyncio.Lock()


class PhoneLinkRequest(BaseModel):
    """Ports shown in Settings > Developer options > Wireless debugging."""

    token: str
    port: int = Field(ge=1, le=65535, description="Port next to 'IP address & port'")
    pair_port: int | None = Field(default=None, ge=1, le=65535, description="Pairing port (first time only)")
    code: str | None = Field(default=None, description="6-digit pairing code (first time only)")


def _check_token(token: str) -> None:
    expected = os.getenv("PHONE_LINK_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="PHONE_LINK_TOKEN is not set on the server.")
    if not secrets.compare_digest(expected, token):
        raise HTTPException(status_code=403, detail="Wrong token.")


def _close_bridge(local_port: int) -> None:
    proc = _bridges.pop(local_port, None)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


async def _open_bridge(local_port: int, remote_port: int) -> None:
    _close_bridge(local_port)
    _bridges[local_port] = subprocess.Popen(
        [
            "socat",
            f"TCP-LISTEN:{local_port},bind=127.0.0.1,fork,reuseaddr",
            f"EXEC:tailscale nc {PHONE_IP} {remote_port}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await asyncio.sleep(1)


async def _adb(*args: str, timeout: float = 20) -> str:
    proc = await asyncio.create_subprocess_exec(
        ADB, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return "timeout"
    return out.decode(errors="replace").strip()


async def _wait_state(serial: str, seconds: int) -> str:
    state = ""
    for _ in range(seconds):
        state = await _adb("-s", serial, "get-state", timeout=5)
        if state == "device":
            break
        await asyncio.sleep(1)
    return state


@router.post("/api/phone/link")
async def link_phone(req: PhoneLinkRequest):
    """Pair (optional), connect to wireless debugging, reopen port 5555, reattach the main bridge."""
    _check_token(req.token)
    steps: list[str] = []
    async with _lock:
        if req.pair_port and req.code:
            await _open_bridge(PAIR_LOCAL_PORT, req.pair_port)
            out = await _adb("pair", f"127.0.0.1:{PAIR_LOCAL_PORT}", req.code.strip(), timeout=30)
            _close_bridge(PAIR_LOCAL_PORT)
            steps.append(f"pair: {out}")
            if "Successfully paired" not in out:
                return {"ok": False, "steps": steps}

        debug_serial = f"127.0.0.1:{DEBUG_LOCAL_PORT}"
        await _open_bridge(DEBUG_LOCAL_PORT, req.port)
        steps.append(f"connect: {await _adb('connect', debug_serial)}")
        state = await _wait_state(debug_serial, 10)
        steps.append(f"state: {state}")
        if state != "device":
            await _adb("disconnect", debug_serial)
            _close_bridge(DEBUG_LOCAL_PORT)
            return {"ok": False, "steps": steps}

        steps.append(f"tcpip: {await _adb('-s', debug_serial, 'tcpip', '5555')}")
        await _adb("disconnect", debug_serial)
        _close_bridge(DEBUG_LOCAL_PORT)

        # Reattach right away instead of waiting for the 30 s loop in entrypoint.sh
        state = ""
        for _ in range(10):
            await asyncio.sleep(2)
            await _adb("disconnect", MAIN_SERIAL)
            await _adb("connect", MAIN_SERIAL)
            state = await _wait_state(MAIN_SERIAL, 3)
            if state == "device":
                break
        steps.append(f"{MAIN_SERIAL}: {state}")
        return {"ok": state == "device", "steps": steps, "devices": await _adb("devices", "-l")}


_PAGE = """<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Collega telefono</title>
<style>body{font-family:system-ui,sans-serif;max-width:420px;margin:24px auto;padding:0 16px}
label{display:block;margin-top:12px}input{width:100%;padding:8px;font-size:16px;box-sizing:border-box}
button{margin-top:16px;width:100%;padding:12px;font-size:16px}pre{white-space:pre-wrap;background:#f2f2f2;padding:8px}
small{color:#666}</style></head><body>
<h2>Collega telefono ad ARTEMIS</h2>
<small>Opzioni sviluppatore &rarr; Debug wireless. Associazione solo la prima volta.</small>
<label>Token<input id="token" type="password"></label>
<label>Porta (accanto a "Indirizzo IP e porta")<input id="port" inputmode="numeric"></label>
<label>Porta associazione <small>(solo prima volta)</small><input id="pair_port" inputmode="numeric"></label>
<label>Codice associazione <small>(solo prima volta)</small><input id="code" inputmode="numeric"></label>
<button id="go">Collega</button><pre id="out"></pre>
<script>
const $ = id => document.getElementById(id);
try { $("token").value = localStorage.getItem("phoneToken") || ""; } catch (e) {}
$("go").onclick = async () => {
  try { localStorage.setItem("phoneToken", $("token").value); } catch (e) {}
  const body = { token: $("token").value, port: Number($("port").value) };
  if ($("pair_port").value) { body.pair_port = Number($("pair_port").value); body.code = $("code").value; }
  $("out").textContent = "Attendi...";
  try {
    const r = await fetch("/api/phone/link", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json();
    $("out").textContent = (j.ok ? "COLLEGATO\\n" : "NON collegato\\n") + JSON.stringify(j, null, 2);
  } catch (e) { $("out").textContent = "Errore: " + e; }
};
</script></body></html>"""


@router.get("/phone", response_class=HTMLResponse)
async def phone_page() -> str:
    """Small form to run the link from the phone's browser."""
    return _PAGE
