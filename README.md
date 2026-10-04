# Home Assistant EV scheduler

Calendar driven EV charging for Home Assistant. Send a calendar invite (with a location) to a dedicated mailbox, or schedule a trip from a dashboard card, and the car gets enough charge in time for it.

## How it works

* **`tesla_calendar.py`** polls an IMAP mailbox for iMIP invitations, mirrors them into an `.ics` file read by the Remote Calendar integration, auto accepts invites whose location geocodes, and offers manual trip scheduling (with a destination picker) that sends a proper invite back to the person who booked it.
* **`tesla_trip_energy.py`** reads upcoming events, geocodes them (Nominatim), routes them (Waze), applies a weather adjusted Wh/km figure, and publishes the required SOC and deadline to two helpers.
* An HA automation pushes those helpers to **evcc** as a vehicle charge plan. evcc handles solar surplus, dynamic pricing and the actual charger control.

## Layout

```
pyscript/apps/      tesla_calendar.py, tesla_trip_energy.py
pyscript/modules/   shared modules (file I/O, ICS store, JSON store, geocoding, outbound email)
evcc.yaml           example evcc config (HA meters, Tesla via HA, Peblar charger)
```

Copy `pyscript/` into `/config/pyscript/`. Configuration for both apps goes in `/config/pyscript/config.yaml`; see the docstring at the top of each app for the keys.

## Requirements

* Home Assistant with [pyscript](https://github.com/custom-components/pyscript) (HACS), "Allow all imports" enabled
* `icalendar`, `requests`, `pywaze` in `/config/pyscript/requirements.txt`
* A mailbox with IMAP and SMTP for the car
* evcc (optional, for the charging side)

## Status

Personal project, shared as is. Built for a Tesla on a Peblar charger with a Dutch dynamic tariff, so expect to adapt entity names.
