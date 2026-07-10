"""Read-only operational dashboard."""

from __future__ import annotations

import asyncio
import html
import json
import logging
from typing import Any
from urllib.parse import urlsplit

LOGGER = logging.getLogger(__name__)


class DashboardServer:
    """Tiny stdlib HTTP server for Home Assistant ingress."""

    def __init__(self, *, host: str, port: int, payload_provider: Any, title: str) -> None:
        self.host = host
        self.port = port
        self.payload_provider = payload_provider
        self.title = title
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        LOGGER.info("Dashboard listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            line = request.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            parts = line.split()
            path = urlsplit(parts[1] if len(parts) > 1 else "/").path
            if path.endswith("/api/status") or path == "/api/status":
                await self._send_json(writer, self.payload_provider.dashboard_payload())
            elif path.endswith("/health") or path == "/health":
                await self._send_text(writer, "ok", content_type="text/plain")
            else:
                await self._send_text(writer, self._html(), content_type="text/html; charset=utf-8")
        except Exception:
            LOGGER.exception("Dashboard request failed")
            if not writer.is_closing():
                await self._send_text(writer, "internal server error", status="500 Internal Server Error")
        finally:
            writer.close()
            await writer.wait_closed()

    async def _send_json(self, writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode()
        await self._send(writer, body, status="200 OK", content_type="application/json")

    async def _send_text(self, writer: asyncio.StreamWriter, text: str, *, status: str = "200 OK", content_type: str = "text/html; charset=utf-8") -> None:
        await self._send(writer, text.encode(), status=status, content_type=content_type)

    async def _send(self, writer: asyncio.StreamWriter, body: bytes, *, status: str, content_type: str) -> None:
        writer.write((f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n").encode() + body)
        await writer.drain()

    def _html(self) -> str:
        return HTML.replace("__TITLE__", html.escape(self.title))


HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>__TITLE__</title>
<style>
:root { color-scheme: dark; --bg:#111314; --panel:#181b1d; --line:#30363a; --text:#ece7dc; --muted:#a7a094; --good:#84d28a; --warn:#e0b85c; --bad:#f0756d; --accent:#7cc7c4; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
main { max-width:1180px; margin:0 auto; padding:24px; }
header { display:flex; align-items:flex-end; justify-content:space-between; gap:16px; border-bottom:1px solid var(--line); padding-bottom:18px; margin-bottom:18px; }
h1 { margin:0; font-size:24px; letter-spacing:0; font-weight:700; }
.sub { color:var(--muted); margin-top:6px; }
.status { display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
.pill { border:1px solid var(--line); border-radius:999px; padding:6px 10px; color:var(--muted); background:#151718; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(245px,1fr)); gap:12px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; min-height:178px; }
.card h2 { margin:0 0 10px; font-size:18px; }
.metric { display:grid; grid-template-columns:1fr auto; gap:8px; padding:4px 0; border-bottom:1px solid rgba(255,255,255,.05); }
.metric:last-child { border-bottom:0; }
.key { color:var(--muted); } .value { text-align:right; }
.good { color:var(--good); } .warn { color:var(--warn); } .bad { color:var(--bad); } .accent { color:var(--accent); }
.events { margin-top:18px; border:1px solid var(--line); border-radius:8px; overflow:hidden; }
.event { display:grid; grid-template-columns:170px 70px 120px 1fr; gap:12px; padding:10px 12px; border-top:1px solid var(--line); background:#151718; }
.event:first-child { border-top:0; }
.empty { padding:16px; color:var(--muted); }
.reason { color:var(--muted); overflow-wrap:anywhere; }
@media (max-width:720px) { main { padding:14px; } header { display:block; } .status { justify-content:flex-start; margin-top:12px; } .event { grid-template-columns:1fr; gap:2px; } }
</style>
</head>
<body>
<main>
<header><div><h1>__TITLE__</h1><div class="sub" id="subtitle">Loading observer state...</div></div><div class="status" id="status"></div></header>
<section class="grid" id="zones"></section>
<section class="events" id="summaries"></section>
<section class="events" id="events"></section>
</main>
<script>
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, character => ({
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;'
})[character]);
const fmt = (v, suffix='') => v === null || v === undefined ? 'unknown' : `${v}${suffix}`;
const cls = (z) => z.fan_state_mismatch || z.sensor_stale ? 'bad' : z.should_run ? 'warn' : 'good';
async function load() {
  const res = await fetch('api/status', {cache:'no-store'});
  const data = await res.json();
  document.getElementById('subtitle').textContent = `${data.house_code} · ${data.active_control ? 'ACTIVE CONTROL' : 'observer only'} · updated ${data.last_run_at || 'never'}`;
  const active = data.zones.filter(z => z.should_run).length;
  const mismatches = data.zones.filter(z => z.fan_state_mismatch).length;
  document.getElementById('status').innerHTML = `<span class="pill">${escapeHtml(data.zones.length)} zones</span><span class="pill warn">${escapeHtml(active)} would run</span><span class="pill ${mismatches ? 'bad' : 'good'}">${escapeHtml(mismatches)} mismatches</span>`;
  document.getElementById('zones').innerHTML = data.zones.map(z => `<article class="card"><h2>${escapeHtml(data.house_code)} ${escapeHtml(String(z.zone_id ?? '').toUpperCase())} <span class="${cls(z)}">${escapeHtml(z.mode)}</span></h2><div class="metric"><span class="key">would run</span><span class="value ${z.should_run ? 'warn' : 'good'}">${z.should_run ? 'yes' : 'no'}</span></div><div class="metric"><span class="key">actual fan</span><span class="value">${z.fan_on ? 'on' : 'off'}</span></div><div class="metric"><span class="key">abs humidity</span><span class="value">${escapeHtml(fmt(z.absolute_humidity, ' g/m3'))}</span></div><div class="metric"><span class="key">delta</span><span class="value">${escapeHtml(fmt(z.delta_absolute_humidity, ' g/m3'))}</span></div><div class="metric"><span class="key">rate</span><span class="value">${escapeHtml(fmt(z.rate_gm3_per_min, ' g/m3/min'))}</span></div><div class="reason">${escapeHtml(z.reason)}</div></article>`).join('');
  const summaries = (data.daily_summaries || []).slice(0, 24);
  document.getElementById('summaries').innerHTML = summaries.length ? summaries.map(s => { const m = s.metrics || {}; const worse = (m.would_be_worse_than_current_system || 0) + (m.dangerous_miss_candidates || 0); const tone = worse ? 'bad' : m.would_improve_current_system ? 'warn' : 'good'; return `<div class="event"><span>${escapeHtml(s.day)}</span><span>${escapeHtml(String(s.zone_id ?? '').toUpperCase())}</span><span class="${tone}">daily</span><span class="reason">better ${escapeHtml(m.would_improve_current_system || 0)} · worse ${escapeHtml(worse)} · blockers ${escapeHtml(m.hard_safety_blockers || 0)} · low-delta risk ${escapeHtml(m.false_positive_candidates || 0)} · max delta ${escapeHtml(fmt(m.max_delta_absolute_humidity, ' g/m3'))} · max RH ${escapeHtml(fmt(m.max_relative_humidity, '%'))}</span></div>`; }).join('') : '<div class="empty">No daily observer summary recorded yet.</div>';
  const events = (data.recent_events || []).slice(0,20);
  document.getElementById('events').innerHTML = events.length ? events.map(e => `<div class="event"><span>${escapeHtml(e.ts)}</span><span>${escapeHtml(String(e.zone_id ?? '').toUpperCase())}</span><span class="accent">${escapeHtml(e.kind)}</span><span class="reason">${escapeHtml(e.payload.reason || e.payload.mode)}</span></div>`).join('') : '<div class="empty">No observer events recorded yet.</div>';
}
load(); setInterval(load, 30000);
</script>
</body>
</html>'''
