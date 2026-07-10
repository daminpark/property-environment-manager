from __future__ import annotations

import pytest

from property_environment_manager.web import HTML as COMBINED_HTML
from trv_regulator.web import DashboardServer as TRVDashboardServer
from trv_regulator.web import HTML as TRV_HTML
from ventilation_manager.web import DashboardServer as VentilationDashboardServer
from ventilation_manager.web import HTML as VENTILATION_HTML


@pytest.mark.parametrize("template", [COMBINED_HTML, TRV_HTML, VENTILATION_HTML])
def test_dashboard_inner_html_values_are_escaped(template: str) -> None:
    assert "const escapeHtml" in template
    assert "${escapeHtml(data.house_code)}" in template
    for unsafe_interpolation in (
        "${z.zone_id.toUpperCase()}",
        "${z.mode}",
        "${z.reason}",
        "${s.day}",
        "${s.zone_id.toUpperCase()}",
        "${e.ts}",
        "${e.kind}",
        "${e.payload.reason",
    ):
        assert unsafe_interpolation not in template


@pytest.mark.parametrize(
    "server_type",
    [TRVDashboardServer, VentilationDashboardServer],
)
def test_standalone_dashboard_title_is_escaped(server_type: type) -> None:
    marker = '<img src=x onerror="alert(1)">'
    server = server_type(
        host="127.0.0.1",
        port=0,
        payload_provider=object(),
        title=marker,
    )

    rendered = server._html()

    assert marker not in rendered
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in rendered


def test_combined_dashboard_has_calendar_off_observed_active_states() -> None:
    assert "!controllers.trv.calendar_policy_enabled ? 'off'" in COMBINED_HTML
    assert "controllers.trv.active_calendar_policy ? 'active' : 'observed'" in COMBINED_HTML
