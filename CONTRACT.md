# EV trip planner contract, version 1

What the EV Trip Card reads and calls. Implemented by the EV Trip Planner
integration in this repository (services `ev_trip_planner.<name>`), which
also ships the card. The pyscript apps that first implemented it were
retired on 7 Oct 2026; card support for them went in card 0.3.0.

The entity ids below are fixed: the integration creates them whatever
language Home Assistant runs in, so a card config never depends on it.

Rules:

* Times **sent to** a service are naive local wall time in Home
  Assistant's time zone, `YYYY-MM-DDTHH:MM:SS`. Times **published** in
  attributes are ISO 8601 with an offset.
* Every service reports its outcome on the status sensor. A service only
  raises for bad input the card should never send (missing field).
* Trips are addressed by `uid` (Invite Calendar's UID), never by a label.
* Attributes are additive: a backend may add fields, the card ignores
  what it does not know. Removing or renaming a field is a new version.

## Entities

### `sensor.ev_trip_planner_trips`

Upcoming trips this calendar organizes (trips booked on the form), soonest
first. Inbound invites are not listed in version 1.

State: number of trips. Attributes:

```yaml
version: 1
trips:
  - uid: "c1f0…@bartbaars.nl"
    start: "2026-10-09T08:00:00+02:00"
    place: "Markt 87, Delft"        # short name, for display
    location: "Markt 87, 2611 GW Delft, Zuid-Holland, Nederland"
    one_way: false
    time_is: departure              # departure | arrival
```

`time_is: arrival` means `start` is when the person must be there; the
backend subtracts the drive. A trip without the marker counts as a
departure.

### `sensor.ev_trip_planner_search`

The last destination search.

State: `idle | results | empty | failed`. `empty` means no such place
(edit the query), `failed` means the lookup service did not answer (try
again). Attributes:

```yaml
version: 1
query: "lidl delft"
results:                            # best match first, Nominatim's order
  - place: "Papsouwselaan 119, Delft"
    location: "Lidl, 119, Papsouwselaan, Delft, …, Nederland"
    geo: "52.001234,4.371234"
    km: 46.2                        # straight line from zone.home, or null
    warning: null                   # e.g. "number 12, not 14"
```

Pass `place`, `location` and `geo` of the picked result to `schedule`
unchanged.

### `sensor.ev_trip_planner_status`

The form banner.

State: `ok | info | success | warning | error` (the last four are
`ha-alert` types). Attributes: `message`, `source`, `updated`. `ok` means
nothing to show. Messages with `source: form` were written by the card
and are only cleared by `clear_status`.

### `binary_sensor.ev_trip_planner_limit_raised`

`on` while the planner has raised the car's charge limit above normal for
a trip and the car confirmed it. Always `off` when the charging side is not
set up. Added in integration 0.3.0; the card shows it as "raised" next to
the limit.

### `sensor.ev_trip_planner_plan`

What the charging side gets: the one plan published to evcc.

State: required SOC in %, `0` when idle. Attributes:

```yaml
version: 1
kind: trip                          # trip | floor | idle
deadline: "2026-10-09T07:45:00+02:00"   # null when idle
uid: "c1f0…@bartbaars.nl"           # first trip of the cluster, null unless trip
place: "Markt 87, Delft"            # null unless trip
km: 120.4                           # whole cluster, null unless trip
notify_service: notify.mobile_app_x # who to tell about this plan
```

`floor` is the SOC floor (50 to 80 band) published instead of a trip.
Since integration 0.3.0 the integration itself publishes this plan to evcc
and keeps the car's charge limit, when its charging options are set. A
`floor` plan is not a booked trip:
charge limit logic must check `kind == trip` before treating it as one.

## Services

All fields are required unless marked optional.

| Name | Fields | Does |
|---|---|---|
| `search` | `query` | fills the search sensor; status `success`, `warning` (empty) or `error` (failed) |
| `clear_search` | none | search sensor back to `idle`, status `ok` |
| `schedule` | `start`, `place`, `location`, `geo`, `user_id`, `one_way` (optional, false), `arrive_by` (optional, false) | validates, creates the event, emails the member; on success clears the search |
| `move` | `uid`, `start` | moves the start, keeps the duration, mails the update |
| `cancel` | `uid` | mails the CANCEL first; if that fails nothing changes |
| `set_status` | `level`, `message` | card's own messages, source `form` |
| `clear_status` | none | status `ok` |
| `refresh` | none | recalculates the plan now (not used by the card) |

Every service also takes an optional `config_entry_id`, needed only with
more than one EV Trip Planner entry.

`user_id` is the HA user who books the trip (`hass.user.id`). The backend
maps it to the household member's email and notify service; an unknown
user is refused.
