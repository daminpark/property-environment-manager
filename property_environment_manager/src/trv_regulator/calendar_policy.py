"""Calendar policy mirror for the existing Home Assistant heating automations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from trv_regulator.config import Settings

IGNORED_SUMMARY_MARKERS = ("blocked", "shut down")


@dataclass(frozen=True)
class ZoneCalendarPolicy:
    """Calendar-derived target and trigger diagnostics for one zone."""

    zone_id: str
    calendar_state: str
    baseline_target_temperature_c: float | None
    trigger_action: str
    trigger_target_temperature_c: float | None
    hvac_mode: str | None
    reason: str
    active_booking: bool
    calendar_entity_id: str | None = None
    event_summary: str | None = None
    transition_id: str | None = None
    suppressed_by_renovation: bool = False


@dataclass(frozen=True)
class CalendarWindow:
    """One usable calendar event converted into HA automation trigger moments."""

    entity_id: str
    zone_ids: tuple[str, ...]
    checkin_at: datetime
    checkout_at: datetime


def calendar_zone_map(house_code: str) -> dict[str, tuple[str, ...]]:
    """Return the room calendar to local TRV zone map used by heating.yaml."""

    return {
        f"calendar.{house_code}_1_calendar": ("1",),
        f"calendar.{house_code}_2_calendar": ("2",),
        f"calendar.{house_code}_3_calendar": ("3", "c"),
        f"calendar.{house_code}_4_calendar": ("4",),
        f"calendar.{house_code}_5_calendar": ("5",),
        f"calendar.{house_code}_6_calendar": ("6",),
        f"calendar.{house_code}_a_calendar": ("1", "2"),
        f"calendar.{house_code}_b_calendar": ("4", "5", "6"),
        f"calendar.{house_code}vbr_calendar": ("1", "2", "3", "4", "5", "6", "c"),
        "calendar.193195vbr_calendar": ("1", "2", "3", "4", "5", "6", "c"),
    }


def calendar_entities_for_house(house_code: str) -> list[str]:
    return sorted(calendar_zone_map(house_code))


class CalendarPolicyEvaluator:
    """Evaluate the existing calendar heating policy from fetched HA events."""

    def __init__(
        self,
        settings: Settings,
        *,
        events_by_entity: dict[str, list[dict[str, Any]]],
        now: datetime,
        renovation_mode_on: bool,
    ) -> None:
        self.settings = settings
        self.now = now.astimezone(UTC)
        self.renovation_mode_on = renovation_mode_on
        self._zone_map = calendar_zone_map(settings.house_code)
        self._windows = self._windows_from_events(events_by_entity)

    def policy_for_zone(self, zone_id: str) -> ZoneCalendarPolicy | None:
        if zone_id == "z":
            return None

        if zone_id in {"a", "b"}:
            return self._service_policy(
                zone_id,
                "service_bathroom_default",
                self.settings.service_bathroom_target_c,
                "bathroom service TRV default is 20C; HA reverts manual changes after 4h",
            )
        if zone_id == "k":
            return self._service_policy(
                zone_id,
                "service_kitchen_default",
                self.settings.service_kitchen_target_c,
                "kitchen service TRV default is 18C; HA reverts manual changes after 4h",
            )

        if self.renovation_mode_on:
            return ZoneCalendarPolicy(
                zone_id=zone_id,
                calendar_state="renovation_mode",
                baseline_target_temperature_c=None,
                trigger_action="none",
                trigger_target_temperature_c=None,
                hvac_mode=None,
                reason="renovation mode is on; HA suppresses calendar and limit policies",
                active_booking=False,
                suppressed_by_renovation=True,
            )

        matching = [
            window for window in self._windows if zone_id in window.zone_ids
        ]
        if not matching and zone_id not in {"0", "1", "2", "3", "4", "5", "6", "c"}:
            return None

        trigger = self._trigger_window(matching)
        if trigger is not None:
            window, kind = trigger
            target = (
                self.settings.calendar_checkin_target_c
                if kind == "checkin"
                else self.settings.calendar_checkout_target_c
            )
            action = (
                "would_set_calendar_checkin_target"
                if kind == "checkin"
                else "would_set_calendar_checkout_target"
            )
            return ZoneCalendarPolicy(
                zone_id=zone_id,
                calendar_state=f"calendar_{kind}_trigger",
                baseline_target_temperature_c=target,
                trigger_action=action,
                trigger_target_temperature_c=target,
                hvac_mode="heat",
                reason=(
                    f"{window.entity_id} {kind} trigger would set heat to "
                    f"{target:.1f}C for a calendar event"
                ),
                active_booking=kind == "checkin",
                calendar_entity_id=window.entity_id,
                transition_id=self._transition_id(window, kind),
            )

        active = self._active_window(matching)
        if active is not None:
            return ZoneCalendarPolicy(
                zone_id=zone_id,
                calendar_state="calendar_occupied",
                baseline_target_temperature_c=self.settings.calendar_checkin_target_c,
                trigger_action="would_set_calendar_checkin_target",
                trigger_target_temperature_c=self.settings.calendar_checkin_target_c,
                hvac_mode=None,
                reason=(
                    f"{active.entity_id} is active; guest target changes within "
                    f"{self.settings.guest_min_target_c:.0f}-"
                    f"{self.settings.guest_max_target_c:.0f}C remain allowed"
                ),
                active_booking=True,
                calendar_entity_id=active.entity_id,
                transition_id=self._transition_id(active, "checkin"),
            )

        latest_checkout = self._latest_checkout(matching)
        return ZoneCalendarPolicy(
            zone_id=zone_id,
            calendar_state="calendar_vacant",
            baseline_target_temperature_c=self.settings.calendar_checkout_target_c,
            trigger_action=(
                "would_set_calendar_checkout_target"
                if latest_checkout is not None
                else "none"
            ),
            trigger_target_temperature_c=(
                self.settings.calendar_checkout_target_c
                if latest_checkout is not None
                else None
            ),
            hvac_mode=None,
            reason="no active calendar booking; HA checkout triggers leave guest rooms at 14C",
            active_booking=False,
            calendar_entity_id=(
                latest_checkout.entity_id if latest_checkout is not None else None
            ),
            transition_id=(
                self._transition_id(latest_checkout, "checkout")
                if latest_checkout is not None
                else None
            ),
        )

    def _service_policy(
        self, zone_id: str, state: str, target: float, reason: str
    ) -> ZoneCalendarPolicy:
        if self.renovation_mode_on:
            return ZoneCalendarPolicy(
                zone_id=zone_id,
                calendar_state="renovation_mode",
                baseline_target_temperature_c=None,
                trigger_action="none",
                trigger_target_temperature_c=None,
                hvac_mode=None,
                reason="renovation mode is on; HA suppresses service default reverts",
                active_booking=False,
                suppressed_by_renovation=True,
            )
        return ZoneCalendarPolicy(
            zone_id=zone_id,
            calendar_state=state,
            baseline_target_temperature_c=target,
            trigger_action="none",
            trigger_target_temperature_c=None,
            hvac_mode=None,
            reason=reason,
            active_booking=False,
        )

    def _trigger_window(
        self, windows: list[CalendarWindow]
    ) -> tuple[CalendarWindow, str] | None:
        tolerance = timedelta(minutes=self.settings.calendar_trigger_tolerance_minutes)
        candidates: list[tuple[datetime, CalendarWindow, str]] = []
        for window in windows:
            for kind, trigger_at in (
                ("checkin", window.checkin_at),
                ("checkout", window.checkout_at),
            ):
                age = self.now - trigger_at
                if timedelta(0) <= age <= tolerance:
                    candidates.append((trigger_at, window, kind))
        if not candidates:
            return None
        # At a back-to-back boundary, preserving the arriving booking is safer
        # than applying the departing booking's checkout target.
        _, window, kind = max(
            candidates, key=lambda item: (item[0], item[2] == "checkin")
        )
        return window, kind

    def _active_window(self, windows: list[CalendarWindow]) -> CalendarWindow | None:
        active = [
            window for window in windows if window.checkin_at <= self.now < window.checkout_at
        ]
        if not active:
            return None
        return min(active, key=lambda window: window.checkout_at)

    def _latest_checkout(self, windows: list[CalendarWindow]) -> CalendarWindow | None:
        completed = [window for window in windows if window.checkout_at <= self.now]
        if not completed:
            return None
        return max(completed, key=lambda window: window.checkout_at)

    def _transition_id(self, window: CalendarWindow, kind: str) -> str:
        transition_at = window.checkin_at if kind == "checkin" else window.checkout_at
        material = f"{window.entity_id}|{kind}|{transition_at.isoformat()}".encode()
        digest = hashlib.sha256(material).hexdigest()[:16]
        return f"calendar-transition-{digest}"

    def _windows_from_events(
        self, events_by_entity: dict[str, list[dict[str, Any]]]
    ) -> list[CalendarWindow]:
        windows: list[CalendarWindow] = []
        tz = self._timezone()
        for entity_id, zone_ids in self._zone_map.items():
            for event in events_by_entity.get(entity_id, []):
                summary = str(
                    event.get("summary")
                    or event.get("message")
                    or event.get("title")
                    or ""
                )
                if self._ignore_summary(summary):
                    continue
                start = self._parse_event_time(
                    event.get("start"), tz, all_day_hour=14
                )
                end = self._parse_event_time(event.get("end"), tz, all_day_hour=11)
                if start is None or end is None or end <= start:
                    continue
                windows.append(
                    CalendarWindow(
                        entity_id=entity_id,
                        zone_ids=zone_ids,
                        checkin_at=start,
                        checkout_at=end,
                    )
                )
        return windows

    def _parse_event_time(
        self, value: Any, tz: ZoneInfo, *, all_day_hour: int
    ) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, dict):
            value = value.get("dateTime") or value.get("date")
            if value is None:
                return None
        text = str(value)
        try:
            if len(text) == 10:
                parsed_date = datetime.fromisoformat(text).date()
                return datetime.combine(
                    parsed_date, time(hour=all_day_hour), tzinfo=tz
                ).astimezone(UTC)
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(UTC)

    def _timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.settings.local_timezone)
        except ZoneInfoNotFoundError:
            return ZoneInfo("Europe/London")

    def _ignore_summary(self, summary: str) -> bool:
        lower = summary.lower()
        return any(marker in lower for marker in IGNORED_SUMMARY_MARKERS)
