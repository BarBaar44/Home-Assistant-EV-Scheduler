/*
 * EV Trip Card
 * One Lovelace card for the EV trip planner: charging plan, form banner,
 * destination search, schedule, move, cancel.
 *
 * Talks only to the EV trip planner contract, version 1 (CONTRACT.md):
 * four sensors and its services, implemented by the EV Trip Planner
 * integration (domain ev_trip_planner). Ships inside that integration,
 * which serves this file and loads it on every page; no Lovelace resource
 * is needed. The retired pyscript backend was removed in 0.3.0.
 *
 * The typed text, the picked destination and the date live in the card's
 * own DOM and go straight to the services, so the phone problem where a
 * dashboard button never receives freshly typed text does not apply.
 *
 * Times: the backend reads naive wall time in Home Assistant's time zone,
 * so every time shown or entered here is in that zone, not the browser's.
 *
 * https://github.com/BarBaar44/Home-Assistant-EV-Scheduler  (MIT)
 */

const VERSION = "0.4.1";

const DEFAULTS = {
  title: "Trips and charging",
  backend: "integration",
  // Car side, read only.
  soc_entity: "sensor.calimero_battery_level",
  charge_limit_entity: "number.calimero_charge_limit",
  charging_entity: "sensor.calimero_charging",
  limit_raised_entity: "input_boolean.evcc_car_limit_raised",
  // Contract v1.
  plan_entity: "sensor.ev_trip_planner_plan",
  trips_entity: "sensor.ev_trip_planner_trips",
  search_entity: "sensor.ev_trip_planner_search",
  status_entity: "sensor.ev_trip_planner_status",
  show_status: true,
  show_manage: true,
  show_arrive_by: true,
  // Picture of the car, e.g. /local/calimero.png. A plain outline otherwise.
  image: null,
  // A whole card in the car's place, e.g. { type: "custom:tesla-view-card", ... }.
  // Wins over `image`.
  car_card: null,
};

// sensor.<car>_charging states (Tesla Fleet) in words.
const CHARGING_TEXT = {
  charging: "Charging",
  complete: "Charged",
  stopped: "Plugged in, not charging",
  starting: "Starting to charge",
  no_power: "Plugged in, no power",
  disconnected: "Not plugged in",
};

// Contract service name -> [domain, service] per backend.
const BACKENDS = {
  integration: (name) => ["ev_trip_planner", name],
};

const BANNER_LEVELS = ["info", "success", "warning", "error"];

function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function hasValue(stateObj) {
  return stateObj && !["unknown", "unavailable", ""].includes(stateObj.state);
}

class EvTripCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._built = false;
    this._selected = 0; // index into the search results
    this._resultsKey = null;
    this._tripsKey = null;
    this._statusKey = null;
    this._row = null; // { uid, mode: "move" | "cancel" } open in the trip list
    this._busy = false;
    this._awaitReset = 0; // token of the schedule call waiting for a reset
    this._arriveBy = false;
    this._carCard = null;
    // A banner shown here because set_status itself failed:
    // { level, message, updated } where updated is the status sensor's at
    // that moment. It stays until the shared status changes or is dismissed.
    this._local = null;
  }

  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    const merged = { ...DEFAULTS, ...(config || {}) };
    if (merged.car_card && (typeof merged.car_card !== "object" || !merged.car_card.type)) {
      throw new Error("car_card needs a card config with a type");
    }
    if (merged.backend === "pyscript") {
      throw new Error(
        "backend: pyscript was removed in 0.3.0; install the EV Trip Planner integration and drop the backend line",
      );
    }
    if (!BACKENDS[merged.backend]) {
      throw new Error(`backend must be one of: ${Object.keys(BACKENDS).join(", ")}`);
    }
    this._config = merged;
    if (this._built) {
      this._built = false;
      this._resultsKey = this._tripsKey = this._statusKey = null;
      this._build();
      if (this._hass) this._update();
    }
  }

  getCardSize() {
    return 8;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) this._build();
    if (this._carCard) this._carCard.hass = hass;
    this._update();
  }

  // Host another card (Tesla View, a picture card) in the car slot, the way
  // stack cards do, so this card carries no dependency on it.
  async _mountCarCard(slot, cfg) {
    let el;
    try {
      if (window.loadCardHelpers) {
        el = (await window.loadCardHelpers()).createCardElement(cfg);
      } else {
        el = document.createElement(String(cfg.type).replace(/^custom:/, ""));
        el.setConfig(cfg);
      }
    } catch (err) {
      slot.innerHTML = CAR_SVG;
      console.warn("ev-trip-card: car_card failed", err);
      return;
    }
    if (this._config.car_card !== cfg) return; // reconfigured meanwhile
    if (this._hass) el.hass = this._hass;
    slot.replaceChildren(el);
    this._carCard = el;
  }

  // ------------------------------------------------------------------
  // Time, in Home Assistant's time zone
  // ------------------------------------------------------------------
  get _tz() {
    return (this._hass && this._hass.config && this._hass.config.time_zone) || undefined;
  }

  // Date -> "YYYY-MM-DDTHH:MM" wall time in HA's zone (datetime-local format).
  _wall(date) {
    const parts = {};
    new Intl.DateTimeFormat("en-CA", {
      timeZone: this._tz,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(date)
      .forEach((p) => (parts[p.type] = p.value));
    return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
  }

  _nextWholeHour() {
    return this._wall(new Date(Date.now() + 3600 * 1000)).slice(0, 14) + "00";
  }

  // Wall time strings of equal format compare correctly as strings.
  _isFuture(wallValue) {
    return Boolean(wallValue) && wallValue > this._wall(new Date());
  }

  _fmt(date) {
    const lang = (this._hass && this._hass.locale && this._hass.locale.language) || undefined;
    try {
      return date.toLocaleString(lang, {
        timeZone: this._tz,
        weekday: "short",
        day: "numeric",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch (e) {
      return date.toString();
    }
  }

  // "today 17:58", "tomorrow 07:00", or "Fri 9 Oct, 07:00".
  _fmtRelative(date) {
    const lang = (this._hass && this._hass.locale && this._hass.locale.language) || undefined;
    const day = this._wall(date).slice(0, 10);
    const today = this._wall(new Date()).slice(0, 10);
    const tomorrow = this._wall(new Date(Date.now() + 86400 * 1000)).slice(0, 10);
    let time;
    try {
      time = date.toLocaleTimeString(lang, { timeZone: this._tz, hour: "2-digit", minute: "2-digit" });
    } catch (e) {
      return this._fmt(date);
    }
    if (day === today) return `today ${time}`;
    if (day === tomorrow) return `tomorrow ${time}`;
    return this._fmt(date);
  }

  // ------------------------------------------------------------------
  // Static skeleton, built once so inputs keep focus and typed text
  // ------------------------------------------------------------------
  _build() {
    const c = this._config;
    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <ha-card>
        <div class="header">${esc(c.title)}</div>

        <div class="status" id="status" ${c.show_status ? "" : "hidden"}>
          <div class="status-text" id="status-text"></div>
          <div class="car ${c.car_card ? "hosted" : ""}" id="car">${
            c.car_card ? "" : c.image ? `<img src="${esc(c.image)}" alt="" />` : CAR_SVG
          }</div>
        </div>

        <div class="banner" id="banner" hidden>
          <span class="banner-text" id="banner-text"></span>
          <button class="link" id="dismiss" type="button">Dismiss</button>
        </div>

        <div class="section">
          <div class="label">Plan a trip</div>
          <form id="search-form" class="row" autocomplete="off">
            <input id="query" type="search" enterkeyhint="search"
                   placeholder="Address, place or business with town" />
            <button id="search-btn" type="submit">Search</button>
          </form>

          <div id="results" class="results" hidden></div>

          <div id="details" hidden>
            <div class="seg" id="mode" ${c.show_arrive_by ? "" : "hidden"}>
              <button type="button" data-mode="leave" class="on">Leave at</button>
              <button type="button" data-mode="arrive">Arrive by</button>
            </div>
            <div class="row">
              <input id="when" type="datetime-local" aria-label="Date and time" />
            </div>
            <label class="check">
              <input id="oneway" type="checkbox" /> One way
            </label>
            <div class="row end">
              <button class="link" id="clear" type="button">Clear</button>
              <button class="primary" id="schedule" type="button">Schedule trip</button>
            </div>
          </div>
        </div>

        <div class="section" id="manage" hidden>
          <div class="label">Upcoming trips</div>
          <div id="trips"></div>
        </div>
      </ha-card>
    `;

    const $ = (id) => this.shadowRoot.getElementById(id);
    this._el = {
      status: $("status-text"),
      banner: $("banner"),
      bannerText: $("banner-text"),
      query: $("query"),
      results: $("results"),
      details: $("details"),
      mode: $("mode"),
      when: $("when"),
      oneway: $("oneway"),
      manage: $("manage"),
      trips: $("trips"),
    };
    this._el.when.value = this._nextWholeHour();
    this._carCard = null;
    if (c.car_card) this._mountCarCard($("car"), c.car_card);

    $("search-form").addEventListener("submit", (ev) => {
      ev.preventDefault();
      this._search();
    });
    $("dismiss").addEventListener("click", () => {
      if (this._local) {
        this._local = null;
        this._renderBanner(this._hass.states[this._config.status_entity]);
        return;
      }
      this._call("clear_status", {});
    });
    $("clear").addEventListener("click", () => this._clear());
    $("schedule").addEventListener("click", () => this._schedule());

    this._el.results.addEventListener("change", (ev) => {
      if (ev.target.name === "dest") this._selected = Number(ev.target.value);
    });

    this._el.mode.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-mode]");
      if (btn) this._setArriveBy(btn.dataset.mode === "arrive");
    });

    this._el.trips.addEventListener("click", (ev) => this._tripAction(ev));

    this._built = true;
  }

  _setArriveBy(on) {
    this._arriveBy = on;
    this._el.mode
      .querySelectorAll("button")
      .forEach((b) => b.classList.toggle("on", (b.dataset.mode === "arrive") === on));
  }

  // ------------------------------------------------------------------
  // Per-update rendering of the dynamic parts only
  // ------------------------------------------------------------------
  _update() {
    const c = this._config;
    const s = this._hass.states;
    if (c.show_status) this._renderStatus(s);
    this._renderBanner(s[c.status_entity]);
    this._renderResults(s[c.search_entity]);
    if (c.show_manage) this._renderTrips(s[c.trips_entity]);
  }

  _renderStatus(s) {
    const c = this._config;
    const soc = s[c.soc_entity];
    const limit = s[c.charge_limit_entity];
    const charging = s[c.charging_entity];
    const raised = s[c.limit_raised_entity];
    const plan = s[c.plan_entity];

    const socNum = hasValue(soc) ? Math.round(Number(soc.state)) : null;
    const limitNum = hasValue(limit) ? Math.round(Number(limit.state)) : null;
    const chargingText = hasValue(charging)
      ? CHARGING_TEXT[charging.state] || charging.state
      : "";
    const raisedOn = raised && raised.state === "on";

    // The plan: a headline (what to do) and a line under it (why, when).
    let target = null;
    let head = "Plan not known yet";
    let sub = "";
    let tone = "muted"; // muted | ok | todo
    if (hasValue(plan)) {
      const a = plan.attributes;
      const when = a.deadline ? new Date(a.deadline) : null;
      const kind = when ? a.kind : "idle";
      if (kind === "trip" || kind === "floor") {
        target = Math.min(100, Math.ceil(Number(plan.state)));
        const ready = socNum != null && socNum >= target;
        // The planner clamps a late deadline to now: say so plainly.
        const by = when.getTime() <= Date.now() + 60 * 1000 ? "now" : `by ${this._fmtRelative(when)}`;
        const km = a.km != null ? `${Math.round(a.km)} km` : "";
        if (kind === "trip") {
          const place = a.place || "the next trip";
          head = ready ? `Ready for ${place}` : `Charge to ${target}% ${by}`;
          sub = ready
            ? [`needs ${target}%`, km].filter(Boolean).join(" · ")
            : [`for ${place}`, km].filter(Boolean).join(" · ");
        } else {
          head = ready ? `Above the ${target}% floor` : `Charge to ${target}% ${by}`;
          sub = ready ? "no trip needs more" : "battery floor, no trip needs more";
        }
        tone = ready ? "ok" : "todo";
      } else {
        head = "No trips planned";
      }
    }

    const key = JSON.stringify([socNum, limitNum, chargingText, raisedOn, target, head, sub]);
    if (key === this._statusKey) return;
    this._statusKey = key;

    const pct = (n) => Math.max(0, Math.min(100, n));
    const bar =
      socNum == null
        ? ""
        : `
      <div class="bar" role="img"
           aria-label="Battery ${socNum}%${limitNum != null ? `, limit ${limitNum}%` : ""}">
        <div class="bar-fill ${tone}" style="width:${pct(socNum)}%"></div>
        ${limitNum != null ? `<div class="bar-over" style="left:${pct(limitNum)}%"></div>` : ""}
        ${target != null ? `<div class="bar-target" style="left:${pct(target)}%"></div>` : ""}
      </div>`;

    const legend = [
      target != null ? `<span><i class="key-target"></i>target ${target}%</span>` : "",
      limitNum != null
        ? `<span><i class="key-limit"></i>limit ${limitNum}%${
            raisedOn ? ` <span class="badge">raised</span>` : ""
          }</span>`
        : "",
    ]
      .filter(Boolean)
      .join("");

    this._el.status.innerHTML = `
      <div class="soc">
        <span class="soc-value">${socNum != null ? `${socNum}%` : "?"}</span>
        <span class="soc-state">${esc(chargingText)}</span>
      </div>
      ${bar}
      <div class="legend">${legend}</div>
      <div class="plan ${tone}">
        <div class="plan-head">${esc(head)}</div>
        ${sub ? `<div class="plan-sub">${esc(sub)}</div>` : ""}
      </div>
    `;
  }

  _statusUpdated(stateObj) {
    return stateObj ? `${stateObj.state}|${stateObj.attributes.updated || ""}` : "";
  }

  _renderBanner(stateObj) {
    if (this._local && this._local.updated !== this._statusUpdated(stateObj)) {
      this._local = null; // the shared status moved on
    }
    const level = this._local ? this._local.level : stateObj ? stateObj.state : "ok";
    const message = this._local
      ? this._local.message
      : (stateObj && stateObj.attributes.message) || "";
    const show = BANNER_LEVELS.includes(level);
    this._el.banner.hidden = !show;
    if (!show) return;
    this._el.banner.className = `banner ${level}`;
    this._el.bannerText.textContent = message;
  }

  _results() {
    const st = this._hass.states[this._config.search_entity];
    if (!st || st.state !== "results") return [];
    return Array.isArray(st.attributes.results) ? st.attributes.results : [];
  }

  _renderResults(stateObj) {
    const results = this._results();
    const searchState = stateObj ? stateObj.state : "idle";

    // The backend clears the search after a trip is accepted. That is the
    // signal to clear this card's own fields too.
    if (this._awaitReset && searchState === "idle") {
      this._awaitReset = 0;
      this._resetForm();
    }

    const key = JSON.stringify([searchState, results]);
    if (key === this._resultsKey) return;
    this._resultsKey = key;

    if (this._selected >= results.length) this._selected = 0;
    const has = results.length > 0;
    this._el.results.hidden = !has;
    this._el.details.hidden = !has;
    this._el.results.innerHTML = results
      .map((r, i) => {
        const km = r.km != null ? `${Math.round(r.km)} km` : "";
        const warn = r.warning ? `<span class="warn">⚠ ${esc(r.warning)}</span>` : "";
        return `
        <label class="result">
          <input type="radio" name="dest" value="${i}" ${i === this._selected ? "checked" : ""} />
          <span class="result-main">
            <span class="result-place">${esc(r.place)}</span>
            <span class="result-meta">${esc(km)} ${warn}</span>
          </span>
        </label>`;
      })
      .join("");
  }

  _trips() {
    const st = this._hass.states[this._config.trips_entity];
    const trips = st && st.attributes.trips;
    return Array.isArray(trips) ? trips : [];
  }

  _renderTrips() {
    const trips = this._trips();
    if (this._row && !trips.some((t) => t.uid === this._row.uid)) this._row = null;
    const key = JSON.stringify([trips, this._row]);
    if (key === this._tripsKey) return;
    this._tripsKey = key;

    this._el.manage.hidden = trips.length === 0;
    this._el.trips.innerHTML = trips
      .map((t) => {
        const start = new Date(t.start);
        const mode = this._row && this._row.uid === t.uid ? this._row.mode : null;
        const tags = [
          t.time_is === "arrival" ? "arrive by" : "leave",
          t.one_way ? "one way" : "",
        ]
          .filter(Boolean)
          .map((x) => `<span class="tag">${esc(x)}</span>`)
          .join("");
        let extra = "";
        if (mode === "move") {
          extra = `
            <div class="row sub">
              <input type="datetime-local" class="move-when" value="${esc(this._wall(start))}"
                     aria-label="New date and time" />
              <button class="primary" data-act="move-save" type="button">Save</button>
            </div>`;
        } else if (mode === "cancel") {
          extra = `
            <div class="row sub confirm">
              <span>Cancel this trip? The invite is withdrawn by email.</span>
              <button class="link" data-act="close" type="button">Keep</button>
              <button class="primary danger" data-act="cancel-yes" type="button">Cancel trip</button>
            </div>`;
        }
        return `
        <div class="trip" data-uid="${esc(t.uid)}">
          <div class="trip-main">
            <div class="trip-place">${esc(t.place)}</div>
            <div class="trip-when">${esc(this._fmt(start))} ${tags}</div>
          </div>
          <div class="trip-actions">
            <button class="link" data-act="${mode === "move" ? "close" : "move"}" type="button">
              ${mode === "move" ? "Close" : "Move"}</button>
            <button class="link danger" data-act="cancel" type="button">Cancel</button>
          </div>
          ${extra}
        </div>`;
      })
      .join("");
  }

  // ------------------------------------------------------------------
  // Actions
  // ------------------------------------------------------------------
  async _call(name, data) {
    const [domain, service] = BACKENDS[this._config.backend](name);
    try {
      await this._hass.callService(domain, service, data);
      return true;
    } catch (err) {
      this._localBanner("error", `${domain}.${service} failed: ${err.message || err}`);
      return false;
    }
  }

  // For mistakes caught in the card itself: the shared banner, so every
  // open dashboard shows it. Shown locally if even that call fails.
  _localBanner(level, message) {
    const [domain, service] = BACKENDS[this._config.backend]("set_status");
    this._hass.callService(domain, service, { level, message }).catch(() => {
      const stateObj = this._hass.states[this._config.status_entity];
      this._local = { level, message, updated: this._statusUpdated(stateObj) };
      this._renderBanner(stateObj);
    });
  }

  _setBusy(busy) {
    this._busy = busy;
    this.shadowRoot
      .querySelectorAll("button.primary, #search-btn")
      .forEach((b) => (b.disabled = busy));
  }

  async _search() {
    if (this._busy) return;
    const query = this._el.query.value.trim();
    if (query.length < 3) {
      this._localBanner("warning", "Type at least three characters, then search.");
      return;
    }
    this._el.query.blur(); // closes the phone keyboard
    this._selected = 0;
    this._resultsKey = null;
    this._setBusy(true);
    await this._call("search", { query });
    this._setBusy(false);
  }

  async _schedule() {
    if (this._busy) return;
    const pick = this._results()[this._selected];
    if (!pick || !pick.geo) {
      this._localBanner("warning", "Search for a destination and pick one from the list first.");
      return;
    }
    const when = this._el.when.value;
    if (!this._isFuture(when)) {
      this._localBanner("warning", "Pick a date and time in the future.");
      return;
    }

    const data = {
      start: `${when}:00`,
      place: pick.place,
      location: pick.location,
      geo: pick.geo,
      user_id: this._hass.user.id,
      one_way: this._el.oneway.checked,
      arrive_by: this._config.show_arrive_by && this._arriveBy,
    };

    // A rejected trip leaves the search in place, so the form stays filled
    // for a correction. An accepted one clears the search, which clears the
    // form in _renderResults. Stop waiting after a while.
    const token = Date.now();
    this._awaitReset = token;
    this._setBusy(true);
    const ok = await this._call("schedule", data);
    this._setBusy(false);
    if (!ok && this._awaitReset === token) this._awaitReset = 0;
    setTimeout(() => {
      if (this._awaitReset === token) this._awaitReset = 0;
    }, 30000);
  }

  async _clear() {
    this._resetForm();
    await this._call("clear_search", {});
  }

  _resetForm() {
    this._el.query.value = "";
    this._el.when.value = this._nextWholeHour();
    this._el.oneway.checked = false;
    this._setArriveBy(false);
    this._selected = 0;
  }

  _openRow(uid, mode) {
    this._row = mode ? { uid, mode } : null;
    this._tripsKey = null;
    this._renderTrips();
  }

  async _tripAction(ev) {
    const btn = ev.target.closest("button[data-act]");
    if (!btn || this._busy) return;
    const row = btn.closest(".trip");
    const uid = row.dataset.uid;
    const act = btn.dataset.act;

    if (act === "move" || act === "cancel") return this._openRow(uid, act);
    if (act === "close") return this._openRow(uid, null);

    if (act === "cancel-yes") {
      this._setBusy(true);
      await this._call("cancel", { uid });
      this._setBusy(false);
      this._openRow(uid, null);
      return;
    }

    if (act === "move-save") {
      const value = row.querySelector(".move-when").value;
      if (!this._isFuture(value)) {
        this._localBanner("warning", "Pick a new date and time in the future.");
        return;
      }
      this._setBusy(true);
      const ok = await this._call("move", { uid, start: `${value}:00` });
      this._setBusy(false);
      if (ok) this._openRow(uid, null);
    }
  }
}

// Plain side outline of a saloon, used when no `image` is set.
const CAR_SVG = `
<svg viewBox="0 0 240 90" fill="none" stroke="currentColor" stroke-width="2.5"
     stroke-linejoin="round" stroke-linecap="round" aria-hidden="true">
  <path d="M12 62 C12 52 18 47 30 45 L68 40 C84 26 104 18 132 18 C160 18 182 28 200 40
           L218 44 C228 46 232 52 232 60 L232 64 C232 67 230 68 227 68 L208 68
           M172 68 L74 68 M38 68 L16 68 C13 68 12 66 12 62 Z" />
  <path d="M80 40 C94 29 110 24 130 24 L132 40 Z M140 24 C158 25 174 31 188 40 L140 40 Z"
        opacity="0.6" />
  <circle cx="56" cy="68" r="15" />
  <circle cx="190" cy="68" r="15" />
  <circle cx="56" cy="68" r="6" opacity="0.6" />
  <circle cx="190" cy="68" r="6" opacity="0.6" />
</svg>`;

const STYLE = `
  :host { display: block; }
  ha-card { padding: 16px; }
  [hidden] { display: none !important; }
  .header {
    font-size: 1.25rem; font-weight: 500; margin-bottom: 12px;
    color: var(--primary-text-color);
  }
  .section { margin-top: 16px; }
  .label {
    font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--secondary-text-color); margin-bottom: 8px;
  }
  /* Battery row full width; plan lower left, car lower right. */
  .status {
    display: grid; grid-template-columns: minmax(0, 1fr) 38%;
    column-gap: 16px; align-items: end;
  }
  .status-text { display: contents; }
  .soc, .bar, .legend { grid-column: 1 / -1; }
  .plan { grid-column: 1; align-self: center; }
  .car { grid-column: 2; margin-top: 10px; color: var(--secondary-text-color); }
  .car img, .car svg {
    display: block; width: 100%; max-height: 120px; object-fit: contain;
  }
  /* A hosted card blends in: no frame, no background of its own. */
  .car.hosted {
    align-self: center;
    --ha-card-background: transparent;
    --ha-card-box-shadow: none;
    --ha-card-border-width: 0;
  }

  .soc { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
  .soc-value {
    font-size: 2.4rem; font-weight: 500; line-height: 1;
    color: var(--primary-text-color);
  }
  .soc-state { font-size: 0.95rem; color: var(--secondary-text-color); }

  .bar {
    position: relative; height: 10px; margin-top: 12px; border-radius: 5px;
    background: var(--divider-color); overflow: hidden;
  }
  .bar-fill { position: absolute; inset: 0 auto 0 0; border-radius: 5px; }
  .bar-fill.todo, .bar-fill.muted { background: var(--primary-color); }
  .bar-fill.ok { background: var(--success-color, #43a047); }
  /* Above the car's charge limit: hatched, the car will not charge there. */
  .bar-over {
    position: absolute; top: 0; bottom: 0; right: 0;
    background: repeating-linear-gradient(135deg,
      transparent 0 3px, var(--card-background-color, #1c1c1c) 3px 6px);
    opacity: 0.6;
  }
  .bar-target {
    position: absolute; top: -2px; bottom: -2px; width: 3px; margin-left: -1px;
    background: var(--primary-text-color);
  }
  .legend {
    display: flex; gap: 14px; flex-wrap: wrap; margin-top: 6px;
    font-size: 0.8rem; color: var(--secondary-text-color);
  }
  .legend i {
    display: inline-block; vertical-align: middle; margin-right: 5px;
  }
  .key-target { width: 3px; height: 10px; background: var(--primary-text-color); }
  .key-limit {
    width: 10px; height: 10px; border-radius: 2px; background: var(--divider-color);
  }

  .plan {
    margin-top: 14px; padding-left: 10px;
    border-left: 3px solid var(--plan-color);
    --plan-color: var(--divider-color);
  }
  .plan.todo { --plan-color: var(--primary-color); }
  .plan.ok { --plan-color: var(--success-color, #43a047); }
  .plan-head { font-size: 1.1rem; font-weight: 500; color: var(--primary-text-color); }
  .plan.muted .plan-head { font-weight: 400; color: var(--secondary-text-color); }
  .plan-sub { font-size: 0.9rem; color: var(--secondary-text-color); margin-top: 2px; }

  .badge {
    display: inline-block; margin-left: 4px; padding: 0 6px; border-radius: 8px;
    font-size: 0.75rem; background: var(--warning-color, #ff9800); color: #fff;
  }

  @media (max-width: 340px) {
    .car { display: none; }
    .plan { grid-column: 1 / -1; }
  }

  .banner {
    display: flex; align-items: center; gap: 8px; margin-top: 16px;
    padding: 10px 12px; border-radius: 8px; font-size: 0.95rem;
    border-left: 4px solid var(--banner-color);
    background: color-mix(in srgb, var(--banner-color) 12%, transparent);
    color: var(--primary-text-color);
  }
  .banner-text { flex: 1; }
  .banner.info { --banner-color: var(--info-color, #039be5); }
  .banner.success { --banner-color: var(--success-color, #43a047); }
  .banner.warning { --banner-color: var(--warning-color, #ffa600); }
  .banner.error { --banner-color: var(--error-color, #db4437); }

  .row { display: flex; gap: 8px; align-items: center; margin-top: 8px; }
  .row.end { justify-content: flex-end; }
  input[type="search"], input[type="datetime-local"] {
    flex: 1; min-width: 0; box-sizing: border-box; padding: 10px 12px;
    font: inherit; font-size: 16px; /* 16px stops iOS zooming in */
    color: var(--primary-text-color);
    background: var(--input-fill-color, var(--secondary-background-color));
    border: 1px solid var(--divider-color); border-radius: 8px;
  }
  input:focus { outline: 2px solid var(--primary-color); outline-offset: -1px; }
  #search-form { margin-top: 0; }

  button {
    font: inherit; cursor: pointer; border-radius: 8px; padding: 10px 14px;
    border: 1px solid var(--divider-color);
    background: var(--secondary-background-color); color: var(--primary-text-color);
  }
  button:disabled { opacity: 0.5; cursor: default; }
  button.primary {
    background: var(--primary-color); color: var(--text-primary-color, #fff);
    border-color: var(--primary-color);
  }
  button.primary.danger {
    background: var(--error-color, #db4437); border-color: var(--error-color, #db4437);
  }
  button.link {
    background: none; border: none; padding: 6px 8px; color: var(--primary-color);
  }
  button.link.danger { color: var(--error-color, #db4437); }

  .results {
    margin-top: 8px; border: 1px solid var(--divider-color); border-radius: 8px;
    overflow: hidden;
  }
  .result {
    display: flex; gap: 10px; align-items: center; padding: 10px 12px;
    border-top: 1px solid var(--divider-color); cursor: pointer;
    color: var(--primary-text-color);
  }
  .result:first-child { border-top: none; }
  .result input { margin: 0; accent-color: var(--primary-color); }
  .result-main { display: flex; flex-direction: column; min-width: 0; }
  .result-meta { font-size: 0.85rem; color: var(--secondary-text-color); }
  .warn { color: var(--warning-color, #ffa600); }

  .seg {
    display: inline-flex; margin-top: 12px; border: 1px solid var(--divider-color);
    border-radius: 8px; overflow: hidden;
  }
  .seg button { border: none; border-radius: 0; background: none; }
  .seg button.on { background: var(--primary-color); color: var(--text-primary-color, #fff); }

  .check {
    display: flex; gap: 8px; align-items: center; margin-top: 10px;
    color: var(--primary-text-color);
  }
  .check input { accent-color: var(--primary-color); }

  .trip {
    display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px;
    padding: 8px 0; border-top: 1px solid var(--divider-color);
  }
  .trip:first-child { border-top: none; }
  .trip-main { flex: 1; min-width: 0; }
  .trip-place {
    color: var(--primary-text-color); overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap;
  }
  .trip-when { font-size: 0.85rem; color: var(--secondary-text-color); }
  .tag {
    display: inline-block; margin-left: 4px; padding: 0 6px; border-radius: 8px;
    font-size: 0.75rem; border: 1px solid var(--divider-color);
  }
  .trip .sub { flex-basis: 100%; margin-top: 4px; }
  .confirm { flex-wrap: wrap; color: var(--primary-text-color); font-size: 0.95rem; }
  .confirm span { flex: 1 1 100%; }
`;

// The integration loads this file on every page. A copy still installed
// from the old HACS Dashboard repository would be loaded too; defining the
// element twice throws, so the first one wins and the second says so.
if (customElements.get("ev-trip-card")) {
  console.warn(
    `ev-trip-card ${VERSION}: another copy is already loaded. Remove the old ` +
      "HACS Dashboard install and its Lovelace resource; the EV Trip Planner " +
      "integration serves the card now.",
  );
} else {
  customElements.define("ev-trip-card", EvTripCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "ev-trip-card",
    name: "EV Trip Card",
    description: "Plan, move and cancel EV trips and see the charging plan.",
    documentationURL: "https://github.com/BarBaar44/Home-Assistant-EV-Scheduler",
  });
  console.info(`%c EV-TRIP-CARD %c ${VERSION} `, "background:#03a9f4;color:#fff", "");
}
