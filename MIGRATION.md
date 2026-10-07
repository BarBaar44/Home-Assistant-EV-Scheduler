# From the pyscript apps to the EV Trip Planner integration

The integration (`custom_components/ev_trip_planner`) does what
`trip_scheduler.py` and `ev_trip_energy.py` do, and serves the same
[EV trip planner contract](https://github.com/BarBaar44/EV-Trip-Card/blob/main/CONTRACT.md),
so [EV Trip Card](https://github.com/BarBaar44/EV-Trip-Card) works with
either. Do the switch on a quiet moment: no trip in the next day.

## What changes

| pyscript | integration |
|---|---|
| `pyscript.ev_trip_*` services | `ev_trip_planner.*` services (same names without the prefix) |
| `sensor.ev_trip_planner_*` made with `state.set()`, gone after a restart | real entities with the same entity ids |
| `config.yaml` app settings | the integration's setup form and options |
| `tesla_household.json` | **Household member** entries under the integration |
| `input_number.next_trip_required_soc` | state of `sensor.ev_trip_planner_plan` |
| `input_datetime.next_trip_deadline` (2099 when idle) | `deadline` attribute of the plan sensor (none when idle) |
| `input_text.next_trip_notify_service` | `notify_service` attribute of the plan sensor |
| geocode, route and alert JSON files | Home Assistant storage (`.storage/ev_trip_planner.<entry>`) |
| Waze on current traffic | Waze on predicted traffic at the departure time |

Same as before: clustering, departure vs arrival (`TIME_IS=ARRIVAL` wins
over own), `GEO=` pins, the straight line fallback when Waze fails, the SOC
floor, accepting an invite once its location is found, one alert per
occurrence and failure, never a broadcast notify, and refusing an arrive by
trip you can no longer make.

## Steps

1. **Install** EV Trip Planner from HACS (custom repository
   `https://github.com/BarBaar44/Home-Assistant-EV-Scheduler`, type
   Integration). Restart Home Assistant.
2. **Stop the pyscript apps**: remove `trip_scheduler` and `ev_trip_energy`
   from `/config/pyscript/config.yaml`, save, and move the two app files out
   of `apps/`. Both running would accept invites twice and fight over the
   `sensor.ev_trip_planner_*` entity ids. Restart, so the pyscript made
   sensors are gone.
3. **Add the integration**: Settings > Devices & services > Add integration >
   EV Trip Planner. Trip calendar `calendar.tesla`, your email as the
   OpenStreetMap contact, 79 kWh, `sensor.calimero_battery_level`,
   `sensor.tesla_adjusted_efficiency_wh_km`, fallback
   `notify.mobile_app_pixel_7`.
4. **Add household members**: on the integration's entry, Add household
   member, once per person (HA user, email, notify service). These replace
   `tesla_household.json`.
5. **Options**: set the battery floor (50) and anything else you had
   changed from the defaults.
6. **Check**: the four `sensor.ev_trip_planner_*` entities exist without a
   `_2` suffix (if they have one, a pyscript sensor was still there: remove
   the new entities, restart, reload the integration). The plan sensor shows
   the next trip.
7. **Card**: add `backend: integration` to the card config.
8. **evcc automation**: replace the trip inputs (below), then Run actions once.

## evcc publish trip plan

Replace the `trip` trigger:

```yaml
  - trigger: state
    id: trip
    entity_id: sensor.ev_trip_planner_plan
    not_from: [unavailable, unknown]
    not_to: [unavailable, unknown]
```

A state trigger without `to` also fires on attribute changes, so a moved
deadline at the same SOC still publishes.

Replace these variables; everything after them stays as it is:

```yaml
      plan_entity: sensor.ev_trip_planner_plan
      soc_raw: "{{ states(plan_entity) | float(0) }}"
      soc: "{{ [soc_raw | round(0, 'ceil') | int, 100] | min }}"
      deadline_ts: "{{ as_timestamp(state_attr(plan_entity, 'deadline'), 0) }}"
      now_ts: "{{ as_timestamp(now()) }}"
      idle: "{{ state_attr(plan_entity, 'kind') in [None, 'idle'] or soc_raw <= 0 }}"
      plan_ts: "{{ [deadline_ts, now_ts + 900] | max }}"
      plan_time: "{{ plan_ts | timestamp_custom('%Y-%m-%dT%H:%M:%SZ', false) }}"
      raise_needed: "{{ not idle and soc > normal_limit }}"
      notify_raw: "{{ state_attr(plan_entity, 'notify_service') or '' }}"
```

After that `input_number.next_trip_required_soc`,
`input_datetime.next_trip_deadline` and `input_text.next_trip_notify_service`
are unused and can be deleted.

## Rolling back

Delete the integration entry, put the two app files back and their blocks
in `config.yaml`, set the card back to `backend: pyscript`, and restore the
automation's trip inputs. The calendar itself is Invite Calendar's and is
not touched by either.
