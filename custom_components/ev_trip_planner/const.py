"""Constants for EV Trip Planner."""

from __future__ import annotations

import logging
from typing import Final

DOMAIN: Final = "ev_trip_planner"
LOGGER = logging.getLogger(__package__)

# EV trip planner contract (CONTRACT.md in BarBaar44/EV-Trip-Card).
CONTRACT_VERSION: Final = 1

# Invite Calendar, the integration this one plans on top of.
IC_DOMAIN: Final = "invite_calendar"
IC_UPDATED_EVENT: Final = "invite_calendar_updated"

# Config entry data
CONF_CALENDAR: Final = "calendar_entity"
CONF_CONTACT: Final = "nominatim_contact"
CONF_BATTERY_KWH: Final = "usable_battery_kwh"
CONF_SOC_SENSOR: Final = "soc_sensor"
CONF_EFFICIENCY_SENSOR: Final = "efficiency_sensor"
CONF_DEFAULT_NOTIFY: Final = "default_notify_service"

# Options (defaults in options.py)
OPT_SAFETY_BUFFER: Final = "safety_buffer_pct"
OPT_FALLBACK_WH_KM: Final = "fallback_efficiency_wh_km"
OPT_LOOKAHEAD_DAYS: Final = "lookahead_days"
OPT_PREP_BUFFER: Final = "prep_buffer_min"
OPT_CLUSTER_HOURS: Final = "trip_cluster_hours"
OPT_ALL_DAY_HOUR: Final = "all_day_departure_hour"
OPT_FLOOR_SOC: Final = "floor_soc"
OPT_FLOOR_READY_HOUR: Final = "floor_ready_hour"
OPT_TRIP_DURATION: Final = "trip_duration_min"
OPT_ACCEPT_INVITES: Final = "accept_invites"

# Household member subentries
SUBENTRY_MEMBER: Final = "member"
CONF_USER_ID: Final = "user_id"
CONF_EMAIL: Final = "email"
CONF_NOTIFY: Final = "notify_service"

# Services (contract v1 names)
SERVICE_SEARCH: Final = "search"
SERVICE_CLEAR_SEARCH: Final = "clear_search"
SERVICE_SCHEDULE: Final = "schedule"
SERVICE_MOVE: Final = "move"
SERVICE_CANCEL: Final = "cancel"
SERVICE_SET_STATUS: Final = "set_status"
SERVICE_CLEAR_STATUS: Final = "clear_status"
SERVICE_REFRESH: Final = "refresh"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"

# Status levels (ha-alert's values plus "ok")
STATUS_LEVELS: Final = ("ok", "info", "success", "warning", "error")
# Status sources this integration owns; "form" is the card's and is only
# cleared by clear_status.
STATUS_SOURCES: Final = (
    "schedule",
    "cancel",
    "reschedule",
    "email",
    "selection",
    "search",
)

# Search
MAX_SEARCH_RESULTS: Final = 6
TRIP_LIST_DAYS: Final = 180

# Routing and reachability
WAZE_ROUTING_URL: Final = (
    "https://routing-livemap-row.waze.com/RoutingManager/routingRequest"
)
FALLBACK_DETOUR_FACTOR: Final = 1.3
FALLBACK_SPEED_KMH: Final = 70.0
NEAR_TRIP_HOURS: Final = 6.0
ROUTE_CACHE_HOURS: Final = 6.0
ROUTE_PREDICT_MAX_HOURS: Final = 72.0
REACH_FAST_KMH: Final = 120.0
REACH_SLOW_KMH: Final = 30.0

# Geocoding
NOMINATIM_URL: Final = "https://nominatim.openstreetmap.org/search"
GEOCODE_CACHE_DAYS: Final = 30
GEOCODE_FAILED_TTL: Final = 3600

# Alerts
ALERT_KEEP_PAST_SECONDS: Final = 86400

# Update interval of the plan, minutes (time passing changes deadlines).
UPDATE_MINUTES: Final = 5

# Description markers on trip events (shared with the pyscript apps)
MARK_ONE_WAY: Final = "TRIP_TYPE=ONE_WAY"
MARK_ROUND_TRIP: Final = "TRIP_TYPE=ROUND_TRIP"
MARK_DEPARTURE: Final = "TIME_IS=DEPARTURE"
MARK_ARRIVAL: Final = "TIME_IS=ARRIVAL"

BROADCAST_NOTIFY: Final = ("notify.notify", "notify", "")
