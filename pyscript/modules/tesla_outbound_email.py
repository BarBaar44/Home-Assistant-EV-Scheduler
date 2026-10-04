"""
tesla_outbound_email — shared pyscript module
-------------------------------------------------
Builds and sends the three kinds of outbound email tesla_calendar.py needs:
  - send_trip_invite_email:    iMIP REQUEST/CANCEL for a manually-scheduled
                               trip, so the organizer can manage it in their
                               own calendar app.
  - send_accept_reply:         iMIP REPLY (PARTSTAT=ACCEPTED) for an inbound
                               invitation, so tesla@ actually shows as having
                               accepted in the sender's calendar. This is the
                               "auto accept invitations" requirement — the
                               mailbox pipeline used to mirror invites into
                               .ics but never RSVP'd, so tesla@ sat on
                               "no response" forever.
  - send_missing_location_reply: a plain threaded courtesy reply asking an
                               inbound invite's organizer to add a LOCATION.

Must live at /config/pyscript/modules/tesla_outbound_email.py. Import
from an app with:
    import tesla_outbound_email as outbound_email

INLINING, AND WHY. All three entry points below are self-contained: the
.ics-building, subject/body text, MIME assembly, and SMTP send/retry logic
all live directly inside each @pyscript_executor function's own body,
rather than being factored into shared helper functions called from
inside them.

This is deliberate, not an accident of style. pyscript's interpreter
wraps EVERY function it parses in a file — decorated or not — as its own
internal async object. Its interpreter normally awaits those
automatically when one script-defined function calls another. But code
running inside a @pyscript_executor thread is still governed by that same
interpreter (it's just off the event loop, not "real" compiled Python) —
so a call from inside an executor function to a plain sibling function
(e.g. a shared _build_calendar_message() helper) does NOT get
auto-awaited. It silently returns an unrun coroutine instead of the
function's actual result:

    TypeError: cannot unpack non-iterable coroutine object

An earlier version of this module split the .ics-building, message-
assembly and SMTP-send logic into shared helpers (_calendar_bytes(),
_build_calendar_message(), _smtp_send(), _decoded_subject()) called from
all three entry points. That crashed the first time send_accept_reply()
actually ran, at the _build_calendar_message() call. It's the same
underlying gap as the "generator expression not implemented" issue found
in tesla_trip_energy.py's alert_failure() — pyscript's AST interpreter
implementing only a subset of real Python semantics — just hit from a
different angle.

The safe pattern inside a @pyscript_executor function: call only real
external library/stdlib functions (smtplib, email.mime.*, icalendar,
math, etc. — none of these are parsed by pyscript, so none of them are
wrapped). Never call another function defined in a pyscript-parsed file
from inside one. tesla_file_io.py's independent, self-contained functions
already followed this by accident; tesla_trip_energy.py's geocode() had
the same latent bug (a call to a sibling _haversine_km()) and has been
fixed the same way — see that file.

SENDER IDENTITY. RFC 6047 requires the message sender to be the
ORGANIZER for METHOD:REQUEST and METHOD:CANCEL, and the ATTENDEE for
METHOD:REPLY. Callers therefore pass `from_addr` separately from
`smtp_user`; when they differ we send From: <from_addr> with
Sender: <smtp_user> ("on behalf of"). NOTE: mailcow/Postfix enforces
sender == login by default. Either add the organizer address to the
authenticating mailbox's "Allow to send as" list, or disable the sender
check for that mailbox — otherwise the message is rejected at submission
and the send returns None/False (surfaced by the caller as a notification
rather than failing silently).

MESSAGE STRUCTURE.

REQUEST/CANCEL (trip invites, read on a phone):

    multipart/mixed
    ├── text/plain                   (the human-readable summary)
    └── application/ics attachment   (for clients that take a file)

REPLY (accept sent to an inbound invite's organizer, read by their
calendar server):

    multipart/mixed
    └── multipart/alternative
        ├── text/plain
        └── text/calendar; method=REPLY

WHY THE TWO DIFFER — this was changed deliberately and reverting it will
make trip invites unreadable on a phone. An inline `text/calendar`
alternative is what makes a desktop client (Evolution) render Accept /
Decline buttons instead of a paperclip, and invites were originally built
that way for exactly that reason. But a client with no calendar handler
picks that part anyway — it is the last and therefore "richest"
alternative — and, having no way to render it, dumps the raw VCALENDAR
text into the message body. Gmail on an IMAP (non-Google-hosted) account
does precisely this: BEGIN:VCALENDAR, PRODID, escaped SUMMARY and all,
below the real text.

Gmail renders invite cards only for Google-hosted accounts, so no MIME
arrangement can produce Accept/Decline there — the inline part bought
nothing on the phone and cost a wall of raw text. Trip invites are read
on the phone, so they are now plain text plus an attachment.

Losing Accept/Decline on the desktop costs almost nothing here: the
recipient is an ATTENDEE on a trip this system organizes, and their RSVP
comes back as METHOD:REPLY, which the inbound path ignores. The button
was decorative.

REPLY keeps its inline part because there is no attachment fallback in a
REPLY and the reader is a calendar server, not a person.

`with smtplib.SMTP(...) as server:` is deliberately NOT used here, even
though this is genuinely running in a background thread — same reasoning
as tesla_file_io.py avoiding `with` for file I/O: pyscript's interpreter
still governs this code, and `with`-block semantics have already been
observed to behave unlike real Python elsewhere in this project. Explicit
connect/try/finally/quit() instead.
"""
import logging
import smtplib
from email import encoders
from email.header import decode_header, make_header
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.utils import make_msgid, formataddr, formatdate
from icalendar import Calendar, Event, vCalAddress, vText

_logger = logging.getLogger(__name__)

PRODID = "-//Home Assistant//Tesla Calendar//EN"


@pyscript_executor
def send_trip_invite_email(
    smtp_host, smtp_port, smtp_user, smtp_pass, from_name,
    from_addr, to_addr, event, method, event_start, location,
    in_reply_to=None,
):
    """Send an outbound iMIP invite/update/cancel for a manually
    scheduled trip, so the organizer can manage it in their own calendar
    app.

    `from_addr` should be the ORGANIZER address (see SENDER IDENTITY in
    the module docstring). The caller is responsible for having already
    set DTSTAMP and, for updates/cancellations, a bumped SEQUENCE on the
    event — see tesla_ics_store.touch_dtstamp() / bump_sequence().

    Returns the new Message-ID on success, or None on failure.
    """
    # --- build the .ics bytes ---
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("method", method)
    cal.add("calscale", "GREGORIAN")
    cal.add_component(event)
    try:
        cal.add_missing_timezones()
    except Exception:
        pass
    ics_bytes = cal.to_ical()

    if method == "CANCEL":
        subject = f"Cancelled: trip to {location}"
        body = (
            f"The trip to \"{location}\" ({event_start}) has been "
            f"cancelled from Home Assistant.\n\n"
            f"Your calendar app should remove it automatically.\n\n"
            f"— Tesla Calendar (automated)"
        )
    elif in_reply_to:
        subject = f"Updated: trip to {location}"
        body = (
            f"The trip to \"{location}\" has been rescheduled to "
            f"{event_start}.\n\n"
            f"Your calendar app should recognise this as an update to the "
            f"existing event rather than a new one.\n\n"
            f"— Tesla Calendar (automated)"
        )
    else:
        subject = f"Trip scheduled: {location}"
        body = (
            f"A trip to \"{location}\" has been scheduled in Home "
            f"Assistant for {event_start}.\n\n"
            f"Add it to your own calendar to reschedule or cancel it from "
            f"there — the car's charging schedule stays in sync either "
            f"way.\n\n"
            f"— Tesla Calendar (automated)"
        )

    # --- build the message ---
    outer = MIMEMultipart("mixed")
    outer["Subject"] = subject
    outer["From"] = formataddr((from_name, from_addr))
    if from_addr.lower() != smtp_user.lower():
        outer["Sender"] = smtp_user
    outer["To"] = to_addr
    outer["Reply-To"] = from_addr
    outer["Date"] = formatdate(localtime=True)
    msg_id = make_msgid()
    outer["Message-ID"] = msg_id
    if in_reply_to:
        outer["In-Reply-To"] = in_reply_to
        outer["References"] = in_reply_to

    # Plain text only — no inline text/calendar alternative. See the module
    # docstring: an inline calendar part makes clients that cannot render
    # it (Gmail on IMAP) print the raw VCALENDAR into the body, and it
    # buys no Accept/Decline on the phone in return.
    outer.attach(MIMEText(body, "plain", "utf-8"))

    attachment = MIMEBase("application", "ics", name="invite.ics")
    attachment.set_payload(ics_bytes)
    encoders.encode_base64(attachment)
    attachment.add_header(
        "Content-Disposition", "attachment", filename="invite.ics"
    )
    outer.attach(attachment)

    # --- send, with one retry on a transient failure ---
    last_error = None
    sent = False
    for attempt in range(2):
        server = None
        try:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=20)
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(outer)
            sent = True
        except smtplib.SMTPRecipientsRefused as e:
            _logger.warning(f"Recipient refused, not retrying: {e}")
            break
        except smtplib.SMTPSenderRefused as e:
            _logger.warning(
                f"Sender {outer.get('From')} refused by {smtp_host} — the "
                f"authenticating mailbox is probably not permitted to send "
                f"as that address: {e}"
            )
            break
        except Exception as e:
            last_error = e
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass
        if sent:
            break

    if not sent:
        if last_error is not None:
            _logger.warning(f"SMTP send failed after 2 attempts: {last_error}")
        return None

    _logger.info(f"Sent {method} invite email to {to_addr} for '{location}'")
    return msg_id


@pyscript_executor
def send_accept_reply(
    smtp_host, smtp_port, smtp_user, smtp_pass, from_name,
    attendee_addr, to_addr, source_event, original_msg=None,
):
    """RSVP "accepted" to an inbound invitation on behalf of the car.

    Builds a minimal REPLY VEVENT per RFC 5546: the same UID and
    SEQUENCE as the request, DTSTAMP, the original ORGANIZER, and exactly
    ONE ATTENDEE line — ours — carrying PARTSTAT=ACCEPTED. Deliberately
    does NOT echo the full event back; a REPLY that carries the whole
    event body invites clients to treat it as an update.

    Returns True on success.
    """
    import homeassistant.util.dt as dt_util

    reply_event = Event()
    reply_event.add("uid", str(source_event.get("UID")))
    reply_event.add("dtstamp", dt_util.utcnow())
    reply_event.add("sequence", int(source_event.get("SEQUENCE", 0)))

    organizer = source_event.get("ORGANIZER")
    if organizer is not None:
        reply_event.add("organizer", organizer)

    dtstart = source_event.get("DTSTART")
    if dtstart is not None:
        reply_event.add("dtstart", dtstart.dt)

    summary = source_event.get("SUMMARY")
    if summary is not None:
        reply_event.add("summary", str(summary))

    attendee = vCalAddress(f"MAILTO:{attendee_addr}")
    attendee.params["CN"] = vText("Tesla")
    attendee.params["PARTSTAT"] = vText("ACCEPTED")
    attendee.params["ROLE"] = vText("REQ-PARTICIPANT")
    reply_event.add("attendee", attendee, encode=0)

    # --- build the .ics bytes ---
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("method", "REPLY")
    cal.add("calscale", "GREGORIAN")
    cal.add_component(reply_event)
    try:
        cal.add_missing_timezones()
    except Exception:
        pass
    ics_bytes = cal.to_ical()

    event_summary = str(source_event.get("SUMMARY", "your invitation"))
    subject = f"Accepted: {event_summary}"
    body = (
        f"The car's calendar has accepted \"{event_summary}\".\n\n"
        f"— Tesla Calendar (automated)"
    )
    in_reply_to = original_msg.get("Message-ID") if original_msg else None

    # --- build the message (no file attachment for a REPLY) ---
    outer = MIMEMultipart("mixed")
    outer["Subject"] = subject
    outer["From"] = formataddr((from_name, attendee_addr))
    if attendee_addr.lower() != smtp_user.lower():
        outer["Sender"] = smtp_user
    outer["To"] = to_addr
    outer["Reply-To"] = attendee_addr
    outer["Date"] = formatdate(localtime=True)
    outer["Message-ID"] = make_msgid()
    if in_reply_to:
        outer["In-Reply-To"] = in_reply_to
        outer["References"] = in_reply_to

    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(body, "plain", "utf-8"))
    cal_part = MIMEText(ics_bytes.decode("utf-8"), "calendar", "utf-8")
    cal_part.set_param("method", "REPLY")
    cal_part.set_param("component", "VEVENT")
    alternative.attach(cal_part)
    outer.attach(alternative)

    # --- send, with one retry on a transient failure ---
    last_error = None
    sent = False
    for attempt in range(2):
        server = None
        try:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=20)
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(outer)
            sent = True
        except smtplib.SMTPRecipientsRefused as e:
            _logger.warning(f"Recipient refused, not retrying: {e}")
            break
        except smtplib.SMTPSenderRefused as e:
            _logger.warning(
                f"Sender {outer.get('From')} refused by {smtp_host} — the "
                f"authenticating mailbox is probably not permitted to send "
                f"as that address: {e}"
            )
            break
        except Exception as e:
            last_error = e
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass
        if sent:
            break

    if not sent:
        if last_error is not None:
            _logger.warning(f"SMTP send failed after 2 attempts: {last_error}")
        return False

    _logger.info(f"Sent ACCEPTED reply to {to_addr} for '{event_summary}'")
    return True


@pyscript_executor
def send_missing_location_reply(
    smtp_host, smtp_port, smtp_user, smtp_pass, from_name,
    to_addr, original_msg, event_summary, event_start,
):
    """Send a threaded reply asking an inbound invite's organizer to add
    a location. Plain email, not a formal iTip REPLY.

    Returns True on success.
    """
    # --- RFC 2047-decode the original Subject ---
    # A non-ASCII subject arrives encoded, so replying with the raw value
    # produces things like "Re: =?UTF-8?Q?Vergadering=5Fwoensdag?=".
    raw_subject = original_msg.get("Subject")
    if not raw_subject:
        subject = "your event"
    else:
        try:
            subject = str(make_header(decode_header(raw_subject)))
        except Exception:
            subject = raw_subject
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    body = (
        f"Hi,\n\n"
        f"Thanks for the invite to \"{event_summary}\" on {event_start}.\n\n"
        f"This event doesn't have a location set, so the car charging "
        f"automation can't calculate how much charge will be needed for "
        f"the trip. Could you add a location to the event and resend it?\n\n"
        f"— Tesla Calendar (automated)"
    )

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, smtp_user))
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    # RFC 3834 — mark as machine-generated so the far end's own
    # autoresponders don't ping-pong with us.
    msg["Auto-Submitted"] = "auto-replied"

    orig_msg_id = original_msg.get("Message-ID")
    if orig_msg_id:
        msg["In-Reply-To"] = orig_msg_id
        orig_refs = original_msg.get("References", "")
        msg["References"] = f"{orig_refs} {orig_msg_id}".strip()

    # --- send, with one retry on a transient failure ---
    last_error = None
    sent = False
    for attempt in range(2):
        server = None
        try:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=20)
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
            sent = True
        except smtplib.SMTPRecipientsRefused as e:
            _logger.warning(f"Recipient refused, not retrying: {e}")
            break
        except smtplib.SMTPSenderRefused as e:
            _logger.warning(
                f"Sender {msg.get('From')} refused by {smtp_host} — the "
                f"authenticating mailbox is probably not permitted to send "
                f"as that address: {e}"
            )
            break
        except Exception as e:
            last_error = e
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass
        if sent:
            break

    if not sent:
        if last_error is not None:
            _logger.warning(f"SMTP send failed after 2 attempts: {last_error}")
        return False

    _logger.info(f"Sent missing-location reply to {to_addr} for '{event_summary}'")
    return True
