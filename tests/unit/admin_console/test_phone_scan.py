"""The wireless-debugging port scan must find open ports with a bounded number of live probes."""

import asyncio

from apps.admin_console.routers import phone


def test_find_open_ports_bounded(monkeypatch):
    live = 0
    peak = 0

    async def fake_probe(port, timeout=4):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0)
        live -= 1
        return port in (33000, 45633, 60999)

    monkeypatch.setattr(phone, "_probe", fake_probe)
    assert asyncio.run(phone.find_open_ports(concurrency=50)) == [33000, 45633, 60999]
    assert peak <= 50
