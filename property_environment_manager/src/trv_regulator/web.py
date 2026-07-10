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

    async def _send_text(
        self,
        writer: asyncio.StreamWriter,
        text: str,
        *,
        status: str = "200 OK",
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        await self._send(writer, text.encode(), status=status, content_type=content_type)

    async def _send(self, writer: asyncio.StreamWriter, body: bytes, *, status: str, content_type: str) -> None:
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

    def _html(self) -> str:
        return HTML.replace("__TITLE__", html.escape(self.title))


HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>__TITLE__</title>
<style>
:root { color-scheme: dark; --bg:#111414; --panel:#191d1d; --panel2:#151818; --line:#303737; --text:#efe9df; --muted:#a9a196; --good:#85ce83; --warn:#deb55f; --bad:#ef766d; --cool:#83bdd6; --violet:#b5a4e8; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.45 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
main { max-width:1260px; margin:0 auto; padding:24px; }
header { display:flex; align-items:flex-end; justify-content:space-between; gap:16px; border-bottom:1px solid var(--line); padding-bottom:18px; margin-bottom:18px; }
h1 { margin:0; font-size:25px; letter-spacing:0; font-weight:760; }
.sub { color:var(--muted); margin-top:6px; }
.status { display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
.pill { border:1px solid var(--line); border-radius:999px; padding:6px 10px; color:var(--muted); background:var(--panel2); white-space:nowrap; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:12px; align-items:stretch; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; min-height:236px; }
.card h2 { margin:0 0 10px; font-size:18px; letter-spacing:0; display:flex; justify-content:space-between; gap:12px; }
.badge { font-size:12px; font-weight:700; align-self:center; overflow-wrap:anywhere; text-align:right; }
.metric { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:8px; padding:4px 0; border-bottom:1px solid rgba(255,255,255,.055); }
.metric:last-child { border-bottom:0; }
.key { color:var(--muted); min-width:0; }
.value { text-align:right; min-width:0; overflow-wrap:anywhere; }
.reason { margin-top:10px; color:var(--muted); overflow-wrap:anywhere; }
.good { color:var(--good); } .warn { color:var(--warn); } .bad { color:var(--bad); } .cool { color:var(--cool); } .violet { color:var(--violet); }
.events { margin-top:18px; border:1px solid var(--line); border-radius:8px; overflow:hidden; background:var(--panel2); }
.event { display:grid; grid-template-columns:170px 70px 170px 1fr; gap:12px; padding:10px 12px; border-top:1px solid var(--line); }
.event:first-child { border-top:0; }
.empty { padding:16px; color:var(--muted); }
@media (max-width:760px) { main { padding:14px; } header { display:block; } .status { justify-content:flex-start; margin-top:12px; } .event { grid-template-columns:1fr; gap:2px; } .card { min-height:0; } }
</style>
</head>
<body>
<main>
<header>
  <div><h1>__TITLE__</h1><div class="sub" id="subtitle">Loading observer state...</div></div>
  <div class="status" id="status"></div>
</header>
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
const fmt = (v, suffix='') => v === null || v === undefined ? 'unknown' : `${Number.isFinite(v) ? Math.round(v * 1000) / 1000 : v}${suffix}`;
const bool = (v) => v ? 'yes' : 'no';
const tone = (z) => {
  if (!z.climate_available || z.sensor_stale || z.window_open_risk || z.heating_ineffective || z.mode === 'drying_severe') return 'bad';
  if (z.mode === 'drying_elevated' || z.mode === 'drying_watch' || z.mode === 'heating_observed') return 'warn';
  return 'good';
};
const metric = (k, v, c='') => `<div class="metric"><span class="key">${escapeHtml(k)}</span><span class="value ${c}">${escapeHtml(v)}</span></div>`;
async function load() {
  const res = await fetch('api/status', {cache:'no-store'});
  const data = await res.json();
  const alerts = data.zones.filter(z => tone(z) === 'bad').length;
  const suggestions = data.zones.filter(z => z.suggested_action && z.suggested_action !== 'none').length;
  const calendarState = !data.calendar_policy_enabled ? 'off' : data.active_calendar_policy ? 'active' : 'observed';
  document.getElementById('subtitle').textContent = `${data.house_code} · ${data.active_control ? 'ACTIVE CONTROL' : 'observer only'} · updated ${data.last_run_at || 'never'}`;
  document.getElementById('status').innerHTML = `<span class="pill">${escapeHtml(data.zones.length)} zones</span><span class="pill ${data.boiler_on ? 'warn' : 'good'}">boiler ${data.boiler_on ? 'on' : 'off'}</span><span class="pill ${data.boiler_policy_action === 'none' ? 'good' : 'warn'}">${escapeHtml(data.boiler_policy_action || 'boiler policy unknown')}</span><span class="pill ${calendarState === 'observed' ? 'cool' : 'warn'}">calendar ${escapeHtml(calendarState)}</span><span class="pill warn">${escapeHtml(suggestions)} suggestions</span><span class="pill ${alerts ? 'bad' : 'good'}">${escapeHtml(alerts)} alerts</span>`;
  document.getElementById('zones').innerHTML = data.zones.map(z => `<article class="card"><h2><span>${escapeHtml(data.house_code)} ${escapeHtml(String(z.zone_id ?? '').toUpperCase())}</span><span class="badge ${tone(z)}">${escapeHtml(z.mode)}</span></h2>${[
    metric('action', z.suggested_action || 'none', z.suggested_action === 'none' ? 'good' : 'warn'),
    metric('observed target', fmt(z.target_temperature_c, '°C')),
    metric('suggested target', fmt(z.suggested_target_temperature_c, '°C'), z.suggested_action === 'would_raise_drying_target' ? 'warn' : ''),
    metric('calendar policy', z.calendar_policy_state || 'unknown'),
    metric('calendar target', fmt(z.calendar_policy_target_temperature_c, '°C')),
    metric('calendar source', z.calendar_policy_entity_id || 'none'),
    metric('room temp', fmt(z.room_temperature_c, '°C')),
    metric('TRV temp', fmt(z.trv_current_temperature_c, '°C')),
    metric('HVAC mode', z.hvac_mode || 'unknown'),
    metric('child lock', z.child_lock_on === null || z.child_lock_on === undefined ? 'unknown' : bool(z.child_lock_on), z.child_lock_on === false ? 'bad' : ''),
    metric('temp rate', fmt(z.room_temperature_rate_c_per_hour, '°C/h')),
    metric('heating response', fmt(z.heating_response_c, '°C')),
    metric('humidity', fmt(z.absolute_humidity_gm3, ' g/m3')),
    metric('humidity rate', fmt(z.absolute_humidity_rate_gm3_per_min, ' g/m3/min')),
    metric('climate entity', bool(z.climate_available), z.climate_available ? 'good' : 'bad'),
    metric('stale sensor', bool(z.sensor_stale), z.sensor_stale ? 'bad' : 'good')
  ].join('')}<div class="reason">${escapeHtml(z.reason)}</div></article>`).join('');
  const summaries = (data.daily_summaries || []).slice(0, 30);
  document.getElementById('summaries').innerHTML = summaries.length ? summaries.map(s => { const m = s.metrics || {}; const tone = m.would_be_worse_than_current_system ? 'bad' : m.would_improve_current_system ? 'warn' : (m.hard_safety_blockers || 0) ? 'bad' : 'good'; return `<div class="event"><span>${escapeHtml(s.day)}</span><span>${escapeHtml(String(s.zone_id ?? '').toUpperCase())}</span><span class="${tone}">daily</span><span class="reason">better ${escapeHtml(m.would_improve_current_system || 0)} · worse ${escapeHtml(m.would_be_worse_than_current_system || 0)} · HA policy matches ${escapeHtml(m.would_match_current_ha_policy || 0)} · blockers ${escapeHtml(m.hard_safety_blockers || 0)} · drying severe ${escapeHtml(m.drying_severe_observations || 0)} · avg response ${escapeHtml(fmt(m.heating_response_avg_c, '°C'))}</span></div>`; }).join('') : '<div class="empty">No daily observer summary recorded yet.</div>';
  const events = (data.recent_events || []).slice(0, 30);
  document.getElementById('events').innerHTML = events.length ? events.map(e => `<div class="event"><span>${escapeHtml(e.ts)}</span><span>${escapeHtml(String(e.zone_id ?? '').toUpperCase())}</span><span class="cool">${escapeHtml(e.kind)}</span><span class="reason">${escapeHtml(e.payload.reason || e.payload.suggested_action || '')}</span></div>`).join('') : '<div class="empty">No observer events recorded yet.</div>';
}
load(); setInterval(load, 30000);
</script>
</body>
</html>"""
