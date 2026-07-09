# Property Environment Manager Add-on

This Home Assistant add-on runs ventilation and TRV regulation in one process
with separate active-control gates.

The default configuration is observer-only. Review the dashboard and SQLite
diagnostics before enabling any write path.

Write gates are independent for humidity ventilation, TRV policy, calendar
policy, and boiler control. The first ventilation cutover is humidity-only;
presence, button, scheduled air-out, and drying-room routines remain in Home
Assistant until the add-on explicitly models them.
