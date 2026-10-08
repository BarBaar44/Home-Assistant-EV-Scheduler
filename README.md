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

> **Status, October 2026.** The trip logic is the **EV Trip Planner** integration (`custom_components/ev_trip_planner`, installable from HACS), running live on my system since 7 October 2026. It replaced an earlier set of pyscript apps; those are in the git history if you want them.

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
   │ (invite email)                          │ ev_trip_planner.* services
   ▼                                         ▼
 car@ mailbox ──► Invite Calendar ◄──── EV Trip Planner (integration)
                   │ IMAP/SMTP, iMIP,        search, validate, reachability,
                   │ RSVP, .ics store        create / move / cancel events
                   ▼                         │
              calendar.tesla ───────────────►│ geocode (Nominatim), route (Waze,
                                             │ predicted traffic), weather Wh/km,
                                             │ clustering, SOC floor, accept
                                             │ invites, alerts
                                             ▼
                              sensor.ev_trip_planner_plan
                                   │
                                   ▼
                  automation "evcc publish trip plan"  ──► car charge limit
                                   │ rest_command
                                   ▼
                 evcc: solar, dynamic prices, load balancing ──► charger ──► car
```

The plan sensor in the middle is the whole interface to charging. Everything above them answers "how much, by when". Everything below is evcc deciding how.

## Repository layout

```
custom_components/ev_trip_planner/   the integration (config flow, sensors, services)
tests/                               its tests (pytest-homeassistant-custom-component)
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
| [Invite Calendar](https://github.com/BarBaar44/invite-calendar) | HACS integration, 1.2.2 or later | required |
| A mailbox with IMAP and SMTP | self hosted mailcow | any provider that allows IMAP and SMTP login |
| [EV Trip Card](https://github.com/BarBaar44/EV-Trip-Card) | HACS dashboard card, 0.4.0 or later | optional, but it is the trip form |
| A weather entity | OpenWeatherMap | any `weather.*` entity |
| Car integration | Tesla Fleet | the trip side only needs SOC; the charge limit automation needs a writable limit |
| [evcc](https://evcc.io) | HA add-on | optional; without it you get the plan sensor and can drive any charger yourself |
| Charger | Peblar (Modbus TCP) | anything evcc supports |
| Grid meter | DSMR P1 | anything evcc can read |
| Dynamic tariff | Frank Energie via evcc template | optional |

Nominatim (OpenStreetMap) and Waze need no API key. Please respect [Nominatim's usage policy](https://operations.osmfoundation.org/policies/nominatim/): set a real contact email in the integration's setup form. Results are cached so normal use stays far below the limits.

## Setup

### 1. Mailbox and Invite Calendar

Create a mailbox for the car, e.g. `car@yourdomain`, then install [Invite Calendar](https://github.com/BarBaar44/invite-calendar) from HACS and add an entry for that mailbox.

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=BarBaar44&repository=invite-calendar&category=integration)
[![Add integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=invite_calendar)

 Name the entry so the entity is `calendar.tesla` (or pick yours in step 2). Set its **accept policy to Manual**: EV Trip Planner accepts an invite only once its location is found. Turn on the missing location reply if you like.

Outgoing invites are sent **from** the car's mailbox, which is also the ORGANIZER of trips booked on the card. That keeps SPF/DKIM/DMARC aligned, so invites to Gmail do not land in spam.

### 2. EV Trip Planner

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=BarBaar44&repository=Home-Assistant-EV-Scheduler&category=integration)
[![Add integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ev_trip_planner)

1. **Open in HACS** (or HACS > three dots > Custom repositories > `https://github.com/BarBaar44/Home-Assistant-EV-Scheduler`, type **Integration**). Install EV Trip Planner and restart.
2. **Add integration** (or Settings > Devices & services > Add integration > **EV Trip Planner**): pick the trip calendar, your email as OpenStreetMap contact, usable battery capacity, the car's battery level sensor, a consumption sensor (optional, see step 3) and a fallback notify service.
3. On the new entry, **Add household member** for everyone who books trips: their Home Assistant user, the email that gets the trip invites, and their phone's notify service.
4. Options: battery floor and its ready hour, safety buffer, prep time and the rest.

It creates four entities with fixed ids: `sensor.ev_trip_planner_trips`, `_search`, `_status` and `_plan`. The plan sensor is what charging reads: state the SOC, attributes `kind` (trip, floor, idle), `deadline`, `uid`, `place`, `km` and `notify_service`. Caches live in Home Assistant storage and the entities survive restarts.

> Never use `notify.notify` as a notify service. It broadcasts to every device. The integration refuses it.

### 3. Efficiency sensor

Add `homeassistant/weather_efficiency_sensor.yaml` to your config, replacing `weather.openweathermap` with your weather entity, and create the seven `input_number` helpers listed at its bottom. Check the result is `sensor.tesla_adjusted_efficiency_wh_km`. Set `tesla_base_efficiency_wh_km` to your car's figure (153 Wh/km is the Model 3 LR RWD EPA number).

### 4. Dashboard card

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=BarBaar44&repository=EV-Trip-Card&category=plugin)

Install [EV Trip Card](https://github.com/BarBaar44/EV-Trip-Card) from HACS (custom repository, type Dashboard) and add:

```yaml
type: custom:ev-trip-card
```

Set the car entity options if yours are not named like mine (see the card's README).

### 5. evcc (optional)

[Open the app store in your Home Assistant](https://my.home-assistant.io/redirect/supervisor_apps/)

1. Install evcc (the HA add-on is easiest; add the repository `https://github.com/evcc-io/hassio-addon` in the store first) and adapt `evcc.yaml`: meters, vehicle, charger and tariffs.
2. Add `homeassistant/templates.yaml` (charge status and signed grid currents) and `homeassistant/rest_command.yaml`, then restart HA.
3. Create the helper `input_boolean.evcc_car_limit_raised` (Toggle).
4. Import `homeassistant/automation_evcc_publish_trip_plan.yaml` as an automation and replace the `<car>`, `<evcc_vehicle>` and `<your_phone>` placeholders. Then Run actions once.
5. In evcc, set a smart cost limit on the loadpoint so it also charges in cheap grid hours, not only from solar, and set its limit to 80%.

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

**Checking the numbers:** `sensor.ev_trip_planner_plan` shows the SOC, deadline, place and kilometres of the plan evcc gets. When the floor is above what the next trip needs, the plan shows `kind: floor` instead; the trip is covered either way.

## Design notes and gotchas

These cost real debugging time. The code comments explain each in more detail.

* **Blocking calls freeze Home Assistant.** HTTP runs in executor threads.
* **A naive datetime is not UTC.** The card sends local wall time; treating it as UTC shifted every trip by the UTC offset.
* **Ambiguous places are a choice, not a guess.** Unattended invites pick the match nearest home; the card shows the list.
* **Success must mean the trip can happen.** An arrive by trip with less time left than the drive is refused, not booked in green with a plan nobody can meet.
* **A floor plan is not a trip.** The charge limit automation lowers a hand set limit only when a booked trip says it can, so the always present floor plan cannot reset it early.
* **Test doubles from observed output.** The offline tests once agreed with the code instead of with the real integration, and the trip list came up empty on the first live run.

## Limitations

* The efficiency model is simple: linear cold penalty, no wind direction, elevation or HVAC. Calibrate it against your own driving; the sensor records history for that.
* Usable battery capacity is a config value, not read from the car.
* Repeating trips (every Monday, say) can only come from calendar invites. The card books one trip at a time.
* Trips in a cluster are budgeted together, ignoring any charging in between. Conservative on purpose.
* The charge limit is raised when a trip is booked, not shortly before it.
* Built and tested on one setup: Tesla Model 3, Peblar, Dutch dynamic tariff, mailcow. Expect to adapt entity names.

## Licence

MIT, see [LICENSE](LICENSE).
