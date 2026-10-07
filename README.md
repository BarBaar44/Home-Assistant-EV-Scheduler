# Home Assistant EV Scheduler

Calendar driven EV charging for Home Assistant.

Invite your car to a meeting, or book a trip from a dashboard card, and the car is charged enough to get there and back, in time, as cheaply as possible.

```
"Dentist, Tuesday 14:00, Kerkstraat 12 Haarlem"
        │
        ▼
  58 km round trip × 161 Wh/km (cold, windy) = 9.3 kWh
  9.3 kWh / 79 kWh = 12% + 10% buffer = 22% needed
  leave at 13:02 (14:00 minus 43 min drive minus 15 min buffer)
        │
        ▼
  evcc: "22% by 13:02", charged from solar or the cheapest hours before then
```

> **Status, October 2026.** The trip logic runs as two [pyscript](https://github.com/custom-components/pyscript) apps (this repo, `pyscript/`). A proper Home Assistant integration, installable from HACS, is being built in this repo to replace them. It will implement the same [EV trip planner contract](https://github.com/BarBaar44/EV-Trip-Card/blob/main/CONTRACT.md), so the dashboard card keeps working unchanged.

## Why

Smart charging tools are good at *how* to charge: solar surplus, cheap hours, load balancing. They don't know *when you need the car and for what*. The usual fix is setting a departure time and target by hand in an app, which you forget exactly on the day it matters.

Your calendar already knows. This project reads it, works out the energy each trip really needs, and hands evcc a single plan: this SOC, by this time. evcc does the rest.

## What it does

* **Calendar invites as trip input.** A dedicated mailbox (say `car@yourdomain`) receives normal calendar invites from any client: Outlook, Gmail, Nextcloud, Evolution. The mailbox side is handled by [Invite Calendar](https://github.com/BarBaar44/invite-calendar), a separate integration.
* **Auto accept.** The car accepts an invite once its location can be found on the map, so it shows as attending in the organizer's calendar. No location? Invite Calendar replies asking for one.
* **Dashboard card.** [EV Trip Card](https://github.com/BarBaar44/EV-Trip-Card) books, moves and cancels trips and shows the charging plan. Destination search shows a pick list (useful for "Lidl Amsterdam", which has a dozen branches) and pins the chosen coordinates to the trip. The person who booked it gets a real calendar invite.
* **Real energy estimate.** Waze routing for distance and drive time, a weather adjusted Wh/km figure (temperature, wind, rain), round trip or one way, and a safety buffer. Trips close together are budgeted as one.
* **Leave at or arrive by.** An invite's start time is when you need to *arrive*, so the drive time is subtracted. On the card you choose: leave at, or arrive by. An arrive by trip you can no longer make in time is refused instead of booked.
* **SOC floor.** Optionally keep the battery above, say, 50% while plugged in, so the car is never empty at home even with no trips planned.
* **Charge limit management.** If a trip needs more than 80%, the car's own limit is raised for that trip and restored afterwards. A limit set to 100% by hand and forgotten is brought back to 80% once it is no longer needed. Never touched mid charge.
* **Notifications to the right person.** Failures (address not found, trip needs a charging stop) go to whoever booked the trip, not every phone in the house.

## Architecture

```
 Any calendar app                          EV Trip Card (dashboard)
   │ (invite email)                          │ pyscript.ev_trip_* services
   ▼                                         ▼
 car@ mailbox ──► Invite Calendar ◄──── trip_scheduler.py
                   │ IMAP/SMTP, iMIP,        search, validate, reachability,
                   │ RSVP, .ics store        create / move / cancel events
                   ▼
              calendar.tesla ──────────► ev_trip_energy.py
                                          geocode (Nominatim), route (Waze),
                                          weather Wh/km, clustering, SOC floor,
                                          accept invites, alerts
                                             │
           sensor.ev_trip_planner_plan ◄─────┤  (for the card)
           input_number.next_trip_required_soc ◄─┤
           input_datetime.next_trip_deadline   ◄─┘
                                   │
                                   ▼
                  automation "evcc publish trip plan"  ──► car charge limit
                                   │ rest_command
                                   ▼
                 evcc: solar, dynamic prices, load balancing ──► charger ──► car
```

The two helpers in the middle are the whole interface to charging. Everything above them answers "how much, by when". Everything below is evcc deciding how.

## Repository layout

```
pyscript/
  apps/
    trip_scheduler.py          the card's backend: search, schedule, move, cancel, status
    ev_trip_energy.py          geocode, route, energy, SOC floor, accept, publishes the plan
  modules/
    routing.py                 Waze (via curl_cffi) and straight line distance
    geocode.py                 Nominatim with a shared cache, destination search
    json_store.py              small JSON maps and caches
    atomic_io.py               atomic file I/O (pyscript blocks bare open())
  config.example.yaml          app configuration
  tesla_household.example.json who can book trips, and where to notify them
homeassistant/
  weather_efficiency_sensor.yaml   Wh/km template sensor plus its 7 helpers
  templates.yaml                   evcc charge status and signed grid currents
  rest_command.yaml                calls to the evcc API
  automation_evcc_publish_trip_plan.yaml
evcc.yaml                          example evcc config
```

## What you need

| Part | Used here | Swappable? |
|---|---|---|
| Home Assistant | with HACS | required |
| [pyscript](https://github.com/custom-components/pyscript) | HACS integration | required (until the integration replaces it) |
| [Invite Calendar](https://github.com/BarBaar44/invite-calendar) | HACS integration, 1.2.2 or later | required |
| A mailbox with IMAP and SMTP | self hosted mailcow | any provider that allows IMAP and SMTP login |
| [EV Trip Card](https://github.com/BarBaar44/EV-Trip-Card) | HACS dashboard card, 0.2.0 or later | optional, but it is the trip form |
| A weather entity | OpenWeatherMap | any `weather.*` entity |
| Car integration | Tesla Fleet | the trip side only needs SOC; the charge limit automation needs a writable limit |
| [evcc](https://evcc.io) | HA add-on | optional; without it you get the two helpers and can drive any charger yourself |
| Charger | Peblar (Modbus TCP) | anything evcc supports |
| Grid meter | DSMR P1 | anything evcc can read |
| Dynamic tariff | Frank Energie via evcc template | optional |

Nominatim (OpenStreetMap) and Waze need no API key. Please respect [Nominatim's usage policy](https://operations.osmfoundation.org/policies/nominatim/): set a real contact in `nominatim_user_agent`. Results are cached so normal use stays far below the limits.

## Setup

### 1. Mailbox and Invite Calendar

Create a mailbox for the car, e.g. `car@yourdomain`, then install Invite Calendar from HACS and add an entry for that mailbox. Name the entry so the entity is `calendar.tesla` (or change `calendar_entity` below). Set its **accept policy to Manual**: `ev_trip_energy` accepts an invite only once its location is found. Turn on the missing location reply if you like.

Outgoing invites are sent **from** the car's mailbox, which is also the ORGANIZER of trips booked on the card. That keeps SPF/DKIM/DMARC aligned, so invites to Gmail do not land in spam.

### 2. pyscript

1. Install pyscript from HACS, then **also** add it under Settings > Devices & Services. HACS alone is not enough.
2. Enable "Allow all imports".
3. Create `/config/pyscript/requirements.txt`:
   ```
   requests
   pywaze
   curl_cffi
   ```
   `curl_cffi` is needed because Waze refuses plain HTTP clients, and Home Assistant pins a pywaze version without the fix.
4. Copy `pyscript/modules/*` to `/config/pyscript/modules/` **first**, then `pyscript/apps/*` to `/config/pyscript/apps/`. The modules **must** be in `modules/`, not `apps/`: pyscript only allows imports between files from there.
5. Copy `config.example.yaml` to `/config/pyscript/config.yaml`, fill it in, and add `pyscript: !include pyscript/config.yaml` to `configuration.yaml`.
6. Copy `tesla_household.example.json` to `/config/pyscript/tesla_household.json`. Keys are Home Assistant user IDs (Settings > People > Users, click the user, the ID is in the URL). Notify services are named `notify.mobile_app_<device>`: check the exact name in Developer Tools > Actions.

> Never use `notify.notify` as a notify service. It broadcasts to every device. The code refuses it and falls back to a persistent notification.

### 3. Helpers

Create these in Settings > Devices & Services > Helpers. They are what the evcc automation reads.

| Helper | Type | Notes |
|---|---|---|
| `input_number.next_trip_required_soc` | Number | 0 to 100, step 0.1 |
| `input_datetime.next_trip_deadline` | Date **and time** | a date only helper silently drops the time |
| `input_text.next_trip_notify_service` | Text | max length 255; optional but recommended |
| `input_boolean.evcc_car_limit_raised` | Toggle | for the evcc automation |

For the efficiency sensor: the seven `input_number` helpers listed at the bottom of `homeassistant/weather_efficiency_sensor.yaml`.

The apps create the four `sensor.ev_trip_planner_*` entities the card reads by themselves.

### 4. Efficiency sensor

Add `homeassistant/weather_efficiency_sensor.yaml` to your config, replacing `weather.openweathermap` with your weather entity. Check the result is `sensor.tesla_adjusted_efficiency_wh_km`. Set `tesla_base_efficiency_wh_km` to your car's figure (153 Wh/km is the Model 3 LR RWD EPA number).

### 5. Dashboard card

Install [EV Trip Card](https://github.com/BarBaar44/EV-Trip-Card) from HACS (custom repository, type Dashboard) and add:

```yaml
type: custom:ev-trip-card
```

Set the car entity options if yours are not named like mine (see the card's README).

### 6. evcc (optional)

1. Install evcc (the HA add-on is easiest) and adapt `evcc.yaml`: meters, vehicle, charger and tariffs.
2. Add `homeassistant/templates.yaml` (charge status and signed grid currents) and `homeassistant/rest_command.yaml`, then restart HA.
3. Import `homeassistant/automation_evcc_publish_trip_plan.yaml` as an automation and replace the `<car>` and `<your_phone>` placeholders.
4. In evcc, set a smart cost limit on the loadpoint so it also charges in cheap grid hours, not only from solar.

evcc needs a sponsor token for some chargers (Peblar included). Turn off auto update for the evcc add-on and update deliberately: releases can rename modes.

### Tesla specific

To change the charge limit, Home Assistant needs a **virtual key** paired with the car (Tesla Vehicle Command Protocol). Reading works without one, so an integration that shows all data can still be unable to send commands. The public key must be served at `https://<your-domain>/.well-known/appspecific/com.tesla.3p.public-key.pem` and must be the public half of the key HA signs with. Check before pairing:

```bash
openssl ec -in /config/tesla_fleet.key -pubout 2>/dev/null | sha256sum
curl -s https://<your-domain>/.well-known/appspecific/com.tesla.3p.public-key.pem | sha256sum
```

The hashes must match. Then pair from a phone near the car: `https://tesla.com/_ak/<your-domain>?vin=<VIN>`.

## Using it

**From your calendar:** invite `car@yourdomain` to any event with a location. After the next mailbox poll it appears in `calendar.tesla`, the car accepts once the address is found, and the card shows the plan. Moving or cancelling the event in your calendar flows through the same way. Recurring events work.

**From the card:** search a destination, pick the right result, choose leave at or arrive by, set the time, Schedule. You get an invite by email with the trip attached.

**Checking the numbers:** every run logs a line like

```
Next trip cluster (1 event(s), 1 pinned, 0 estimated, first: 'Trip to Kerkstraat 12, Haarlem'):
58.4 km, 9.4 kWh at 161 Wh/km, target SOC 21.9% by 2026-10-06 13:02
(event starts 14:00 as arrival, 43 min drive + 15 min buffer)
```

## Design notes and gotchas

These cost real debugging time. The code comments explain each in more detail.

* **pyscript is not quite Python.** Each file has its own globals, so shared code must live in `modules/`. Inside a `@pyscript_executor` function, calling another function defined in a pyscript file returns an unrun coroutine, so executor functions are self contained. Generator expressions are not supported; use list comprehensions. `with` blocks are avoided.
* **Blocking calls freeze Home Assistant.** HTTP and file I/O run in executor threads.
* **A naive datetime is not UTC.** The card sends local wall time; treating it as UTC shifted every trip by the UTC offset.
* **Ambiguous places are a choice, not a guess.** Unattended invites pick the match nearest home; the card shows the list.
* **Success must mean the trip can happen.** An arrive by trip with less time left than the drive is refused, not booked in green with a plan nobody can meet.
* **Test doubles from observed output.** The offline tests once agreed with the code instead of with the real integration, and the trip list came up empty on the first live run.

## Limitations

* The efficiency model is simple: linear cold penalty, no wind direction, elevation or HVAC. Calibrate it against your own driving; the sensor records history for that.
* Drive times use current traffic, not the predicted traffic at departure. Planned for the integration.
* Usable battery capacity is a config value, not read from the car.
* Repeating trips (every Monday, say) can only come from calendar invites. The card books one trip at a time.
* Trips in a cluster are budgeted together, ignoring any charging in between. Conservative on purpose.
* The charge limit is raised when a trip is booked, not shortly before it.
* Built and tested on one setup: Tesla Model 3, Peblar, Dutch dynamic tariff, mailcow. Expect to adapt entity names.

## Licence

MIT, see [LICENSE](LICENSE).
