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

## Why

Smart charging tools are good at *how* to charge: solar surplus, cheap hours, load balancing. They don't know *when you need the car and for what*. The usual fix is setting a departure time and target by hand in an app, which you forget exactly on the day it matters.

Your calendar already knows. This project reads it, works out the energy each trip really needs, and hands evcc a single plan: this SOC, by this time. evcc does the rest.

## What it does

* **Calendar invites as trip input.** A dedicated mailbox (say `car@yourdomain`) receives normal calendar invites from any client: Outlook, Gmail, Nextcloud, Evolution. Invites are parsed directly from the mail (iMIP), so it works with any sender and any calendar server.
* **Auto accept.** The car accepts an invite once its location can be found on the map, so it shows as attending in the organizer's calendar. No location? It replies asking for one.
* **Dashboard trip form.** Book, move or cancel a trip from Home Assistant. Destination search shows a pick list (useful for "Lidl Amsterdam", which has a dozen branches) and pins the chosen coordinates to the trip. The person who booked it gets a real calendar invite.
* **Real energy estimate.** Waze routing for distance and drive time, a weather adjusted Wh/km figure (temperature, wind, rain), round trip or one way, and a safety buffer. Trips close together are budgeted as one.
* **Correct deadline.** For an invite the start time is when you need to *arrive*, so the drive is subtracted. For a dashboard trip it is when you *leave*.
* **SOC floor.** Optionally keep the battery above, say, 50% while plugged in, so the car is never empty at home even with no trips planned.
* **Charge limit management.** If a trip needs more than 80%, the car's own limit is raised for that trip and restored afterwards. A limit set to 100% by hand and forgotten is brought back to 80% once it is no longer needed. Never touched mid charge.
* **Notifications to the right person.** Failures (address not found, trip needs a charging stop) go to whoever booked the trip, not every phone in the house.

## Architecture

```
 Any calendar app                       HA dashboard "Plan a trip"
   │ (invite email)                       │
   ▼                                      ▼
 IMAP mailbox ──► tesla_calendar.py ◄── pyscript services
                       │  parse iMIP, auto accept, send invites
                       ▼
                 /config/www/tesla.ics ──► Remote Calendar ──► calendar.tesla
                                                                   │
                                                                   ▼
                                                        tesla_trip_energy.py
                                    Nominatim (geocode) ◄──┤ Waze (route)
                                    weather Wh/km sensor ◄──┘
                                                                   │
                       input_number.next_trip_required_soc ◄───────┤
                       input_datetime.next_trip_deadline   ◄───────┘
                                   │
                                   ▼
                  automation "evcc publish trip plan"  ──► car charge limit
                                   │ rest_command
                                   ▼
                 evcc: solar, dynamic prices, load balancing ──► charger ──► car
```

The two helpers in the middle are the whole interface. Everything above them answers "how much, by when". Everything below is evcc deciding how.

## Repository layout

```
pyscript/
  apps/
    tesla_calendar.py          mailbox polling, invites, accept, manual trip services
    tesla_trip_energy.py       geocode, route, energy, SOC floor, publishes the helpers
  modules/
    tesla_file_io.py           atomic file I/O (pyscript blocks bare open())
    tesla_ics_store.py         .ics storage, recurring events, iMIP bookkeeping
    tesla_json_store.py        small JSON maps and caches
    tesla_geocode.py           Nominatim with a shared cache, destination search
    tesla_outbound_email.py    invite, accept and reply emails
  config.example.yaml          app configuration
  tesla_household.example.json who can book trips, and where to notify them
homeassistant/
  weather_efficiency_sensor.yaml   Wh/km template sensor plus its 7 helpers
  templates.yaml                   evcc charge status and signed grid currents
  rest_command.yaml                calls to the evcc API
  automation_evcc_publish_trip_plan.yaml
  trip_form_scripts.example.yaml   minimal dashboard form scripts (starting point)
evcc.yaml                          example evcc config
```

## What you need

| Part | Used here | Swappable? |
|---|---|---|
| Home Assistant | with HACS | required |
| [pyscript](https://github.com/custom-components/pyscript) | HACS integration | required |
| A mailbox with IMAP and SMTP | self hosted mailcow | any provider that allows IMAP and SMTP login |
| Remote Calendar integration | built in | required |
| A weather entity | OpenWeatherMap | any `weather.*` entity |
| Car integration | Tesla Fleet | the trip side only needs SOC; the charge limit automation needs a writable limit |
| [evcc](https://evcc.io) | HA add-on | optional; without it you get the two helpers and can drive any charger yourself |
| Charger | Peblar (Modbus TCP) | anything evcc supports |
| Grid meter | DSMR P1 | anything evcc can read |
| Dynamic tariff | Frank Energie via evcc template | optional |

Nominatim (OpenStreetMap) and Waze need no API key. Please respect [Nominatim's usage policy](https://operations.osmfoundation.org/policies/nominatim/): set a real contact in `nominatim_user_agent`. Results are cached so normal use stays far below the limits.

## Setup

### 1. Mailbox

Create a mailbox for the car, e.g. `car@yourdomain`. IMAP and SMTP must use the same login: outgoing mail is sent **from** this address, and it is also the calendar ORGANIZER on trips booked from the dashboard. That keeps SPF/DKIM/DMARC aligned, so invites to Gmail do not land in spam.

Put the password in `secrets.yaml` as `tesla_mailbox_password`.

### 2. pyscript

1. Install pyscript from HACS, then **also** add it under Settings > Devices & Services. HACS alone is not enough.
2. Enable "Allow all imports".
3. Create `/config/pyscript/requirements.txt`:
   ```
   icalendar
   requests
   pywaze
   ```
4. Copy `pyscript/apps/*` to `/config/pyscript/apps/` and `pyscript/modules/*` to `/config/pyscript/modules/`. The modules **must** be in `modules/`, not `apps/`: pyscript only allows imports between files from there.
5. Copy `config.example.yaml` to `/config/pyscript/config.yaml`, fill it in, and add `pyscript: !include pyscript/config.yaml` to `configuration.yaml`.
6. Copy `tesla_household.example.json` to `/config/pyscript/tesla_household.json`. Keys are Home Assistant user IDs (Settings > People > Users, click the user, the ID is in the URL). Notify services are named `notify.mobile_app_<device>`: check the exact name in Developer Tools > Actions.

> Never use `notify.notify` as a notify service. It broadcasts to every device. The code refuses it and falls back to a persistent notification.

### 3. Helpers

Create these in Settings > Devices & Services > Helpers.

**Required by the trip energy app:**

| Helper | Type | Notes |
|---|---|---|
| `input_number.next_trip_required_soc` | Number | 0 to 100, step 0.1 |
| `input_datetime.next_trip_deadline` | Date **and time** | a date only helper silently drops the time |
| `input_text.next_trip_notify_service` | Text | max length 255; optional but recommended |

**For the dashboard trip form:**

| Helper | Type | Notes |
|---|---|---|
| `input_text.manual_trip_location` | Text | the search box |
| `input_select.manual_trip_destination` | Dropdown | one option: `(search first)` |
| `input_datetime.manual_trip_datetime` | Date and time | departure time |
| `input_boolean.manual_trip_one_way` | Toggle | |
| `input_select.manual_trip_to_cancel` | Dropdown | one option: `(none)` |
| `input_datetime.manual_trip_reschedule_datetime` | Date and time | used by the example reschedule script |

Dropdowns must be created by hand with one placeholder option. pyscript can rewrite their options but cannot create them.

**For the evcc automation:** `input_boolean.evcc_car_limit_raised` (Toggle).

**For the efficiency sensor:** the seven `input_number` helpers listed at the bottom of `homeassistant/weather_efficiency_sensor.yaml`.

pyscript creates `sensor.tesla_trip_form_status`, `sensor.manual_trip_options` and `sensor.manual_trip_destination_results` itself.

### 4. Calendar

1. Restart pyscript (or HA). Within five minutes `/config/www/tesla.ics` appears.
2. Add the **Remote Calendar** integration pointing at `http://<your-ha>:8123/local/tesla.ics`. Name it so the entity is `calendar.tesla`.

### 5. Efficiency sensor

Add `homeassistant/weather_efficiency_sensor.yaml` to your config, replacing `weather.openweathermap` with your weather entity. Check the result is `sensor.tesla_adjusted_efficiency_wh_km`. Set `tesla_base_efficiency_wh_km` to your car's figure (153 Wh/km is the Model 3 LR RWD EPA number).

### 6. Dashboard form

`homeassistant/trip_form_scripts.example.yaml` has a minimal submit script, cancel and reschedule scripts, and the "search on Enter" automation. Put the helpers in an Entities card with buttons for the scripts. A markdown card can show the status banner:

```yaml
type: markdown
content: >-
  {% set s = states('sensor.tesla_trip_form_status') %}
  {% if s in ['info','success','warning','error'] %}
  <ha-alert alert-type="{{ s }}">{{ state_attr('sensor.tesla_trip_form_status','message') }}</ha-alert>
  {% endif %}
```

Search is triggered by pressing Enter (the arrow key on a phone keyboard), not by a button. On phones, a dashboard button prevents the text field from committing, so a "Search" button would search the previous text.

### 7. evcc (optional)

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

**From your calendar:** invite `car@yourdomain` to any event with a location. Within five minutes it appears in `calendar.tesla`, the car accepts, and the helpers show the SOC and deadline. Moving or cancelling the event in your calendar flows through the same way. Recurring events work.

**From the dashboard:** type a destination, press Enter, pick the right result, set the departure time, Schedule. You get an invite by email with the trip attached.

**Checking the numbers:** every run logs a line like

```
Next trip cluster (1 event(s), 1 pinned, first: 'Trip to Kerkstraat 12, Haarlem'):
58.4 km, 9.4 kWh at 161 Wh/km, target SOC 21.9% by 2026-10-06 13:02
(event starts 14:00 as arrival, 43 min drive + 15 min buffer)
```

Allow up to five minutes after booking: the trip app runs every five minutes.

## Design notes and gotchas

These cost real debugging time. The code comments explain each in more detail.

* **pyscript is not quite Python.** Each file has its own globals, so shared code must live in `modules/`. Inside a `@pyscript_executor` function, calling another function defined in a pyscript file returns an unrun coroutine, so executor functions are self contained. Generator expressions are not supported; use list comprehensions. `with` blocks are avoided.
* **Blocking calls freeze Home Assistant.** IMAP, SMTP, HTTP and file I/O all run in executor threads.
* **The Remote Calendar integration returns no event UIDs.** The trip app recovers them from `tesla.ics` by summary and start time.
* **Only REQUEST and CANCEL change the calendar.** An incoming REPLY carries a stripped down copy of the event and would otherwise overwrite it.
* **A naive datetime is not UTC.** `input_datetime` gives local wall time; treating it as UTC shifted every trip by the UTC offset.
* **Invites go out as plain text plus an `.ics` attachment.** An inline calendar part makes Gmail on IMAP print raw iCalendar text into the message body.
* **Writes are atomic.** A truncated `tesla.ics` would otherwise stop the pipeline for good, because processed mails are never fetched again.
* **Ambiguous places are a choice, not a guess.** Unattended invites pick the match nearest home; the dashboard shows the list.

## Limitations

* The efficiency model is simple: linear cold penalty, no wind direction, elevation or HVAC. Calibrate it against your own driving; the sensor records history for that.
* Usable battery capacity is a config value, not read from the car.
* Repeating trips (every Monday, say) can only come from calendar invites. The dashboard books one trip at a time, round trip by default or one way with the toggle.
* Trips in a cluster are budgeted together, ignoring any charging in between. Conservative on purpose.
* The charge limit is raised when a trip is booked, not shortly before it.
* Built and tested on one setup: Tesla Model 3, Peblar, Dutch dynamic tariff, mailcow. Expect to adapt entity names.

## Status

Personal project, shared as is. The calendar and trip side runs on my own system; the charger side is being installed. Recurring invites and the SOC floor are newer and less tested than the rest. Issues and ideas welcome, support not guaranteed.

## Licence

MIT, see [LICENSE](LICENSE).
