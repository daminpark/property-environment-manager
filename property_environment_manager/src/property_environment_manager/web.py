"""Read-only combined dashboard for Home Assistant ingress."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlsplit

LOGGER = logging.getLogger(__name__)


class DashboardServer:
    """Small stdlib HTTP server for operational status and diagnostics."""

    def __init__(self, *, host: str, port: int, payload_provider: Any) -> None:
        self.host = host
        self.port = port
        self.payload_provider = payload_provider
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        LOGGER.info("Dashboard listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            line = request.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            parts = line.split()
            path = urlsplit(parts[1] if len(parts) > 1 else "/").path
            payload = self.payload_provider.dashboard_payload()
            if path.endswith("/api/status") or path == "/api/status":
                await self._send_json(writer, payload)
            elif path.endswith("/api/ventilation/status"):
                await self._send_json(
                    writer, payload.get("controllers", {}).get("ventilation", {})
                )
            elif path.endswith("/api/trv/status"):
                await self._send_json(
                    writer, payload.get("controllers", {}).get("trv", {})
                )
            elif path.endswith("/health") or path == "/health":
                await self._send_text(writer, "ok", content_type="text/plain")
            else:
                await self._send_text(
                    writer, HTML, content_type="text/html; charset=utf-8"
                )
        except Exception:
            LOGGER.exception("Dashboard request failed")
            if not writer.is_closing():
                await self._send_text(
                    writer,
                    "internal server error",
                    status="500 Internal Server Error",
                )
        finally:
            writer.close()
            await writer.wait_closed()

    async def _send_json(
        self, writer: asyncio.StreamWriter, payload: dict[str, Any]
    ) -> None:
        body = json.dumps(payload, sort_keys=True).encode()
        await self._send(writer, body, status="200 OK", content_type="application/json")

    async def _send_text(
        self,
        writer: asyncio.StreamWriter,
        text: str,
        *,
        status: str = "200 OK",
        content_type: str,
    ) -> None:
        await self._send(
            writer, text.encode(), status=status, content_type=content_type
        )

    async def _send(
        self,
        writer: asyncio.StreamWriter,
        body: bytes,
        *,
        status: str,
        content_type: str,
    ) -> None:
        writer.write(
            (
                f"HTTP/1.1 {status}\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Cache-Control: no-store\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            + body
        )
        await writer.drain()


HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Property Environment Manager</title>
<style>
:root { color-scheme: dark; --bg:#101214; --panel:#181b1f; --line:#30363d; --text:#eceff1; --muted:#a7adb3; --good:#7bd88f; --warn:#e5bd5b; --bad:#f07178; --accent:#76c7c0; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
main { max-width:1240px; margin:0 auto; padding:24px; }
header { display:flex; align-items:flex-end; justify-content:space-between; gap:16px; border-bottom:1px solid var(--line); padding-bottom:18px; margin-bottom:18px; }
h1 { margin:0; font-size:24px; letter-spacing:0; }
h2 { margin:22px 0 10px; font-size:18px; }
.sub { color:var(--muted); margin-top:6px; }
.status { display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
.pill { border:1px solid var(--line); border-radius:999px; padding:6px 10px; color:var(--muted); background:#15181b; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(245px,1fr)); gap:12px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; min-height:178px; }
.card h3 { margin:0 0 10px; font-size:17px; }
.metric { display:grid; grid-template-columns:1fr auto; gap:8px; padding:4px 0; border-bottom:1px solid rgba(255,255,255,.05); }
.metric:last-child { border-bottom:0; }
.key { color:var(--muted); }
.value { text-align:right; }
.good { color:var(--good); }
.warn { color:var(--warn); }
.bad { color:var(--bad); }
.accent { color:var(--accent); }
.events { margin-top:14px; border:1px solid var(--line); border-radius:8px; overflow:hidden; }
.event { display:grid; grid-template-columns:170px 70px 120px 1fr; gap:12px; padding:10px 12px; border-top:1px solid var(--line); background:#15181b; }
.event:first-child { border-top:0; }
.empty { padding:16px; color:var(--muted); }
.reason { color:var(--muted); overflow-wrap:anywhere; margin-top:8px; }
@media (max-width:720px) { main { padding:14px; } header { display:block; } .status { justify-content:flex-start; margin-top:12px; } .event { grid-template-columns:1fr; gap:2px; } }
</style>
</head>
<body>
<main>
<header><div><h1>Property Environment Manager</h1><div class="sub" id="subtitle">Loading observer state...</div></div><div class="status" id="status"></div></header>
<section id="content"></section>
</main>
<script>
const fmt = (v, suffix='') => v === null || v === undefined ? 'unknown' : `${v}${suffix}`;
const bool = (v) => v ? 'yes' : 'no';
const pill = (text, tone='') => `<span class="pill ${tone}">${text}</span>`;
const metric = (k, v, tone='') => `<div class="metric"><span class="key">${k}</span><span class="value ${tone}">${v}</span></div>`;
function renderVentilation(data) {
  if (!data || !data.zones) return '';
  const mismatches = data.zones.filter(z => z.fan_state_mismatch).length;
  const stale = data.zones.filter(z => z.sensor_stale).length;
  return `<h2>Ventilation · ${data.house_code}</h2><div class="grid">${data.zones.map(z => {
    const tone = z.sensor_stale || z.fan_state_mismatch ? 'bad' : z.should_run ? 'warn' : 'good';
    return `<article class="card"><h3>${z.zone_id.toUpperCase()} <span class="${tone}">${z.mode}</span></h3>${[
      metric('would run', bool(z.should_run), z.should_run ? 'warn' : 'good'),
      metric('actual fan', z.fan_on ? 'on' : 'off'),
      metric('abs humidity', fmt(z.absolute_humidity, ' g/m3')),
      metric('delta', fmt(z.delta_absolute_humidity, ' g/m3')),
      metric('rate', fmt(z.rate_gm3_per_min, ' g/m3/min')),
      metric('stale', bool(z.sensor_stale), z.sensor_stale ? 'bad' : 'good')
    ].join('')}<div class="reason">${z.reason}</div></article>`;
  }).join('')}</div>${renderSummaries(data.daily_summaries, 'ventilation')}`;
}
function renderTrv(data) {
  if (!data || !data.zones) return '';
  return `<h2>TRV · ${data.house_code}</h2><div class="grid">${data.zones.map(z => {
    const alert = z.window_open_risk || z.heating_ineffective || z.sensor_stale || z.suggested_action !== 'none';
    const tone = alert ? (z.window_open_risk || z.heating_ineffective || z.sensor_stale ? 'bad' : 'warn') : 'good';
    return `<article class="card"><h3>${z.zone_id.toUpperCase()} <span class="${tone}">${z.mode}</span></h3>${[
      metric('action', z.suggested_action || 'none', z.suggested_action !== 'none' ? 'warn' : 'good'),
      metric('target', fmt(z.target_temperature_c, '°C')),
      metric('suggested target', fmt(z.suggested_target_temperature_c, '°C')),
      metric('room temp', fmt(z.room_temperature_c, '°C')),
      metric('HVAC', z.hvac_mode || 'unknown'),
      metric('calendar', z.calendar_policy_state || 'unknown'),
      metric('stale', bool(z.sensor_stale), z.sensor_stale ? 'bad' : 'good')
    ].join('')}<div class="reason">${z.reason}</div></article>`;
  }).join('')}</div>${renderSummaries(data.daily_summaries, 'trv')}`;
}
function renderSummaries(items, kind) {
  const summaries = (items || []).slice(0, 12);
  if (!summaries.length) return `<div class="events"><div class="empty">No ${kind} daily summaries recorded yet.</div></div>`;
  return `<div class="events">${summaries.map(s => {
    const m = s.metrics || {};
    const worse = (m.would_be_worse_than_current_system || 0) + (m.dangerous_miss_candidates || 0);
    const tone = worse || m.hard_safety_blockers ? 'bad' : m.would_improve_current_system ? 'warn' : 'good';
    return `<div class="event"><span>${s.day}</span><span>${s.zone_id.toUpperCase()}</span><span class="${tone}">daily</span><span class="reason">better ${m.would_improve_current_system || 0} · worse ${worse} · blockers ${m.hard_safety_blockers || 0}</span></div>`;
  }).join('')}</div>`;
}
async function load() {
  const res = await fetch('api/status', {cache:'no-store'});
  const data = await res.json();
  const controllers = data.controllers || {};
  const bits = [];
  if (controllers.ventilation) bits.push(`vent ${controllers.ventilation.active_control ? 'active' : 'observer'}`);
  if (controllers.trv) bits.push(`trv ${controllers.trv.active_control ? 'active' : 'observer'}`);
  document.getElementById('subtitle').textContent = bits.length ? bits.join(' · ') : 'no controllers enabled';
  document.getElementById('status').innerHTML = [
    pill(controllers.ventilation ? 'ventilation on' : 'ventilation off', controllers.ventilation ? 'accent' : 'warn'),
    pill(controllers.trv ? 'trv on' : 'trv off', controllers.trv ? 'accent' : 'warn'),
    controllers.trv ? pill(`calendar ${controllers.trv.active_calendar_policy ? 'active' : 'observed'}`, controllers.trv.active_calendar_policy ? 'warn' : 'good') : ''
  ].join('');
  document.getElementById('content').innerHTML = renderVentilation(controllers.ventilation) + renderTrv(controllers.trv);
}
load(); setInterval(load, 30000);
</script>
</body>
</html>'''
