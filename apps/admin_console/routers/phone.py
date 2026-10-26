"""Cloud phone link: reopen the phone's ADB port 5555 through Android wireless debugging.

The cloud container reaches the phone only through Tailscale (userspace networking): the SOCKS5
proxy on localhost:1055 is used to probe phone ports, socat bridges expose them to adb. Port 5555
closes on every phone reboot. `autolink_loop` (started by entrypoint.sh) notices it, finds the
wireless-debugging port (random, in the Linux ephemeral range), connects with the server's
already-authorized ADB key and runs `adb tcpip 5555`, after which the permanent 127.0.0.1:6555
bridge works again. The user only has to turn wireless debugging on.

No token: every action only touches the server's own link to its own phone, and pairing needs the
code shown on the phone screen.
"""

import asyncio
import ipaddress
import os
import subprocess

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

router = APIRouter(tags=["phone"])

ADB = os.getenv("ARTEMIS_ADB_PATH") or "adb"
PHONE_IP = os.getenv("PHONE_TS_IP", "100.116.147.90")
MAIN_SERIAL = f"127.0.0.1:{os.getenv('LOCAL_ADB_PORT', '6555')}"
SOCKS = ("127.0.0.1", 1055)
EPHEMERAL_PORTS = range(32768, 61000)

# Separate local ports for the HTTP endpoint and the background loop (different processes)
API_PORTS = (6556, 6557)
LOOP_PORTS = (6558, 6559)

_bridges: dict[int, subprocess.Popen] = {}
_lock = asyncio.Lock()


class PhoneLinkRequest(BaseModel):
    """Ports shown in Settings > Developer options > Wireless debugging (all optional)."""

    port: int | None = Field(default=None, ge=1, le=65535, description="Port next to 'IP address & port'")
    pair_port: int | None = Field(default=None, ge=1, le=65535, description="Pairing port")
    code: str | None = Field(default=None, pattern=r"^\d{6}$", description="6-digit pairing code")


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


async def _probe(port: int, timeout: float = 4) -> bool | None:
    """TCP connect to the phone through Tailscale SOCKS5: True open, False refused, None no answer."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(*SOCKS), timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        writer.write(b"\x05\x01\x00")
        await asyncio.wait_for(reader.readexactly(2), timeout)
        writer.write(b"\x05\x01\x00\x01" + ipaddress.IPv4Address(PHONE_IP).packed + port.to_bytes(2, "big"))
        reply = await asyncio.wait_for(reader.readexactly(10), timeout)
        return reply[1] == 0
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
        return None
    finally:
        writer.close()


async def find_open_ports(concurrency: int = 500) -> list[int]:
    """Scan the ephemeral range where adbd puts the wireless-debugging port (~2 min)."""
    sem = asyncio.Semaphore(concurrency)

    async def one(port: int) -> int | None:
        async with sem:
            return port if await _probe(port, timeout=2) else None

    found = await asyncio.gather(*(one(p) for p in EPHEMERAL_PORTS))
    return [p for p in found if p]


async def reopen_5555(
    port: int, local_ports: tuple[int, int], pair_port: int | None = None, code: str | None = None
) -> tuple[bool, list[str]]:
    """Pair (optional), connect to wireless debugging on `port`, run `adb tcpip 5555`."""
    pair_local, debug_local = local_ports
    steps: list[str] = []
    if pair_port and code:
        await _open_bridge(pair_local, pair_port)
        out = await _adb("pair", f"127.0.0.1:{pair_local}", code, timeout=30)
        _close_bridge(pair_local)
        steps.append(f"pair: {out}")
        if "Successfully paired" not in out:
            return False, steps

    debug_serial = f"127.0.0.1:{debug_local}"
    await _open_bridge(debug_local, port)
    steps.append(f"connect {port}: {await _adb('connect', debug_serial)}")
    state = await _wait_state(debug_serial, 8)
    if state == "device":
        steps.append(f"tcpip: {await _adb('-s', debug_serial, 'tcpip', '5555')}")
    else:
        steps.append(f"state: {state}")
    await _adb("disconnect", debug_serial)
    _close_bridge(debug_local)
    return state == "device", steps


async def reattach_main(tries: int = 10) -> str:
    """Reconnect the permanent bridge right away instead of waiting for entrypoint's 30 s loop."""
    state = ""
    for _ in range(tries):
        await asyncio.sleep(2)
        await _adb("disconnect", MAIN_SERIAL)
        await _adb("connect", MAIN_SERIAL)
        state = await _wait_state(MAIN_SERIAL, 3)
        if state == "device":
            break
    return state


async def heal(local_ports: tuple[int, int], port: int | None = None, **pairing) -> dict:
    """Full repair: find the wireless-debugging port if not given, reopen 5555, reattach."""
    steps: list[str] = []
    if await _probe(5555) is None:
        return {"ok": False, "steps": ["phone not reachable over Tailscale"]}
    ports = [port] if port else [p for p in await find_open_ports() if p != 5555]
    steps.append(f"candidate ports: {ports}")
    for p in ports:
        ok, s = await reopen_5555(p, local_ports, **pairing)
        steps += s
        if ok:
            state = await reattach_main()
            steps.append(f"{MAIN_SERIAL}: {state}")
            return {"ok": state == "device", "steps": steps}
    return {"ok": False, "steps": steps}


async def autolink_loop() -> None:
    """Background guard: whenever the main bridge is down and 5555 is closed, heal it."""
    while True:
        await asyncio.sleep(30)
        if await _adb("-s", MAIN_SERIAL, "get-state", timeout=5) == "device":
            continue
        if await _probe(5555) is not False:  # open: entrypoint reconnects it; None: phone offline
            continue
        result = await heal(LOOP_PORTS)
        print(f"[phone-autolink] {result}", flush=True)
        if not result["ok"]:
            await asyncio.sleep(60)


@router.post("/api/phone/link")
async def link_phone(req: PhoneLinkRequest):
    """Run the repair now; ports and pairing code are optional (the port is found by scanning)."""
    pairing = {"pair_port": req.pair_port, "code": req.code} if req.pair_port and req.code else {}
    async with _lock:
        result = await heal(API_PORTS, req.port, **pairing)
        result["devices"] = await _adb("devices", "-l")
        return result


_PAGE = """<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Collega telefono</title>
<style>body{font-family:system-ui,sans-serif;max-width:420px;margin:24px auto;padding:0 16px}
label{display:block;margin-top:12px}input{width:100%;padding:8px;font-size:16px;box-sizing:border-box}
button{margin-top:16px;width:100%;padding:12px;font-size:16px}pre{white-space:pre-wrap;background:#f2f2f2;padding:8px}
small{color:#666}</style></head><body>
<h2>Collega telefono ad ARTEMIS</h2>
<small>Accendi Opzioni sviluppatore &rarr; Debug wireless (con un Wi-Fi qualsiasi) e premi Collega.
I campi servono solo se il telefono chiede l'associazione.</small>
<label>Porta associazione <small>(facoltativo)</small><input id="pair_port" inputmode="numeric"></label>
<label>Codice associazione <small>(facoltativo)</small><input id="code" inputmode="numeric"></label>
<button id="go">Collega</button><pre id="out"></pre>
<script>
const $ = id => document.getElementById(id);
$("go").onclick = async () => {
  const body = {};
  if ($("pair_port").value) { body.pair_port = Number($("pair_port").value); body.code = $("code").value.trim(); }
  $("out").textContent = "Attendi (fino a un minuto)...";
  try {
    const r = await fetch("/api/phone/link", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json();
    $("out").textContent = (j.ok ? "COLLEGATO\\n" : "NON collegato\\n") + JSON.stringify(j, null, 2);
  } catch (e) { $("out").textContent = "Errore: " + e; }
};
</script></body></html>"""


@router.get("/phone", response_class=HTMLResponse)
async def phone_page() -> str:
    """Small form to run the repair from the phone's browser."""
    return _PAGE


if __name__ == "__main__":
    asyncio.run(autolink_loop())
