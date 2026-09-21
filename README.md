# zwave-controller

> **Warning**: This project was nearly 100% vibecoded. The UniFi API key
> required to toggle firewall policies is extremely permissive — it grants
> full network admin access. Use at your own risk.

Bridges a Ring Alarm Keypad (1st Gen, paired via `zwave-js-server`) to a UniFi
zone-based firewall policy. A correct PIN + **Disarm** pauses the policy; the
**Arm Away** / **Arm Home** buttons resume it.

Also sends [ntfy](https://ntfy.sh) push notifications for door-open, motion,
water-leak and sensor health events from every other node on the controller —
see [Notifications](#notifications). Door and motion pushes can be muted while
a phone is on the WiFi — see [Presence](#presence).

## Local development

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```
uv venv --python 3.12
uv sync
```

Populate a local `.env` (not committed), then:

```
set -a; source .env; set +a
uv run python -m zwave_controller
```

Tests (no hardware or network needed):

```
uv sync --group dev
uv run pytest
```

## Finding your UniFi firewall policy UUID

```
curl -sk -H "X-API-KEY: $UNIFI_API_KEY" \
  https://<unifi-host>/proxy/network/v2/api/site/default/firewall-policies \
  | jq '.[] | {_id, name, enabled}'
```

Paste the `_id` into `UNIFI_POLICY_ID`. An API key is created in the UniFi OS
Control Plane → Admins → Create API Key.

## Notifications

Optional, and off unless `NTFY_URL` is set. Every node on the controller is
watched — no node IDs to configure, so a newly paired sensor starts alerting
after a restart.

| Event | Push | Priority |
|---|---|---|
| Door/window **opened** | yes, unless someone is home ([Presence](#presence)) | high |
| Door/window **closed** (back to idle) | no (logged) | — |
| Motion detected | yes, unless `NOTIFY_MOTION=false` or someone is home | default |
| Tamper — cover removed, product moved | yes, even when home | urgent |
| Water leak detected | yes, even when home | urgent |
| Water leak cleared (sensor dry again) | no (logged) | — |
| Battery low (`isLow`, or level ≤ `NOTIFY_BATTERY_THRESHOLD`) | yes, even when home | default |
| Node stopped responding / recovered | yes, even when home | high / low |
| Welcome home — phone back on WiFi after being away ([Presence](#presence)) | yes | low |

Every push names the device in the title and, when the node has a *location*
set in zwave-js (next to its name), prefixes the body with it:
`Leak: Washer` / `Basement: Water detected.` Nodes without a location get the
bare message.

**Every event is logged to stdout at INFO whether or not it is pushed.** A
push that goes out logs `notified: <title>`; one that doesn't logs
`event: <title> [reason]`, where the reason is `muted, someone is home`,
`suppressed, 40s into 300s motion cooldown`, `NOTIFY_MOTION off`, or
`not notifying` for door-closed/idle and leak-cleared. The journal is
therefore a complete
record of what the sensors saw, and the phone only hears the interesting
subset.

"Door opened" covers two different spellings, because devices disagree:
Access Control *Door state* (event 22) and Home Security *Sensor status*
(intrusion, events 1–2). The Ring contact sensor here reports **only** the
latter — it exposes no Access Control variable at all — so mapping just the
door-state events would have left it permanently silent.

**Repeat suppression.** After an alert, further events in the same category
from the same sensor are dropped for a per-category window. Every event is
still logged, and categories are independent — a stream of motion never masks
a tamper alert, and a flapping sensor produces one offline/online pair per
window rather than a storm.

| Category | Window | Why |
|---|---|---|
| Door, tamper, leak | `NOTIFY_DOOR_COOLDOWN_SECONDS` (15) | Short on purpose. A door only fires on the open transition, so it cannot flood — and a long window would mean someone entering minutes after you did goes unreported. Just long enough to absorb a chattering reed switch. A leak detector that re-reports while still wet *should* keep nagging. |
| Motion | `NOTIFY_COOLDOWN_SECONDS` (300) | The sensor that actually floods. |
| Offline / online | `NOTIFY_COOLDOWN_SECONDS` (300) | Damps a node flapping at the edge of range. |
| Low battery | 24 h, plus edge detection | Re-reported on every wake-up. |

Low battery adds edge detection on top (only a not-low → low transition
fires) and is capped at one push per node per day, since it is re-reported on
every wake-up. Restarting the service re-arms the edge and can repeat one
low-battery push; that beats missing one.

Sensors report door and motion state as a Notification CC *value* on most
devices and as a `notification` *event* on others; both paths are handled, and
the cooldown collapses a device that fires both into a single push.

To see what your own devices actually expose (useful if a sensor stays quiet),
run with `LOG_LEVEL=DEBUG` — every unmatched notification is logged with its
type and event number.

Alerts are titled with the node's name from zwave-js — set friendly names in
zwave-js-ui, or they read `Contact Sensor v2`.

### Setup

> **The ntfy.sh topic name is the only credential.** Anyone who guesses it can
> read your door-open alerts and publish fake ones. Use a long random topic, or
> self-host with `NTFY_TOKEN`.

```
python3 -c 'import secrets; print("https://ntfy.sh/zw-" + secrets.token_urlsafe(24))'
```

Subscribe to that topic in the ntfy mobile app, confirm the channel works
before wiring it up, then put it in `NTFY_URL`:

```
curl -d "test" -H "Title: hello" https://ntfy.sh/zw-<your-topic>
```

Delivery is best-effort: a failed push is logged as a warning and dropped.
Alerting must never be able to block the firewall toggle.

### Presence

Optional, and off unless `PRESENCE_MACS` is set (it also needs `NTFY_URL`).
The UniFi client list is polled every `PRESENCE_POLL_SECONDS` (30); while any
listed MAC is associated with the WiFi, **door and motion** pushes are muted.
Leak, tamper, battery and offline/online alerts fire regardless — being home
is no reason not to hear about a burst pipe or a dead sensor.

| | |
|---|---|
| `PRESENCE_MACS` | comma-separated, e.g. `3e:90:21:a9:71:a2,aa-bb-cc-dd-ee-ff` (case and separator don't matter) |
| `PRESENCE_POLL_SECONDS` | 30 (minimum 5) |
| `PRESENCE_AWAY_GRACE_SECONDS` | 300 |

The state machine is deliberately lopsided. One sighting flips to *home*
immediately, but *away* needs the phone unseen for the whole grace period,
because an iPhone drops off WiFi for a minute or two whenever it sleeps. A
failed poll (UniFi unreachable, bad payload) never counts as "nobody home" on
its own, but it doesn't stop the away timer either: an outage longer than the
grace period ends with alerts **un**muted, which is the safe direction. The
first failure logs a warning; the rest of the outage is DEBUG.

Muted events are still logged (`event: Front Door opened [muted, someone is
home]`), so `journalctl` remains a record of when the door moved. A muted
event does not start a cooldown window, so a door opened one second after the
phone leaves still pushes.

**Welcome home.** When the phone comes back after a confirmed absence you get
one low-priority push (`Welcome home — 3e:90:… is back on the WiFi`) so you
know alerts are muted. "Confirmed" means the service actually saw you gone: a
successful poll without the phone, or the grace period expiring. A restart
while you are home does not greet you, and a brief WiFi drop inside the grace
period does not either, because you never counted as away.

If the poller task ever dies the service exits non-zero and systemd restarts
it — a poller frozen at "home" would otherwise mute door alerts forever while
looking perfectly healthy.

Find the MAC in the UniFi client list rather than in iOS Settings, since the
two differ when *Private Wi-Fi Address* is on:

```
curl -sk -H "X-API-KEY: $UNIFI_API_KEY" \
  https://<unifi-host>/proxy/network/api/s/default/stat/sta \
  | jq '.data[] | {mac, name, hostname}'
```

A MAC whose first octet has the `2` bit set (`3e:`, `da:`, …) is a private
address. iOS keeps it stable per network as long as *Private Wi-Fi Address*
is set to **Fixed** for that SSID; on **Rotating** (iOS 18+) it changes and
presence quietly reads "away" from then on. Fail-safe, but useless.

## Deploying to the Ubuntu VM (Podman Quadlet)

CI (`.github/workflows/build-and-push.yaml`) publishes
`ghcr.io/sethdandridge/zwave-controller:latest` on every push to `main`.

1. **Let Podman pull from the private package.** Create a fine-grained PAT
   with `read:packages`, then on the VM:
   ```
   sudo podman login ghcr.io -u sethdandridge --authfile /etc/containers/auth.json
   ```

2. **Drop in the secrets and Quadlet unit.** On the VM:
   ```
   sudo mkdir -p /etc/zwave-controller
   sudo install -m 600 secrets.env.example /etc/zwave-controller/secrets.env
   sudo vim /etc/zwave-controller/secrets.env     # fill in real values
   sudo install -m 644 zwave-controller.container /etc/containers/systemd/
   ```

3. **Start the service.** The Quadlet generator turns the `.container` file
   into a `.service` — use that name:
   ```
   sudo systemctl daemon-reload
   sudo systemctl start zwave-controller.service
   sudo systemctl status zwave-controller.service
   sudo journalctl -u zwave-controller.service -f
   ```

## Verification

- `journalctl` should show `connected to Home ...` and `subscribed to notifications on node 2`.
- At the keypad, enter `DISARM_PIN` + **Disarm**: logs show `disarm accepted`; the UniFi UI shows the policy now disabled.
- With `PRESENCE_MACS` set and the phone on WiFi: startup logs `presence: home (3e:90:… on WiFi)`; opening the door produces no push, only an INFO `muted door alert` line; pulling a sensor cover still pushes a tamper alert.
- Put the phone in airplane mode; within `PRESENCE_AWAY_GRACE_SECONDS` the log shows `presence: away`, and the next door open pushes.
- Turn WiFi back on: within a poll interval the log shows `presence: home` and a low-priority **Welcome home** push arrives.
- Press **Arm Away**: logs show `arm_away accepted`; policy re-enabled.
- Wrong PIN + **Disarm**: logs show `disarm rejected: bad PIN`; no state change.
- `sudo podman kill zwave-controller` — systemd restarts the unit within 5 s.
- Startup logs a `watching node N: <name>` line per node — confirm the door and
  motion sensors are listed.
- Open the door: a push arrives within a second or two. Close it: no push, only
  an `event: Front Door closed [not notifying]` line. Open it again
  immediately: suppressed, with an `event: … [suppressed, 2s into 15s door
  cooldown]` line.
- Walk past the motion sensor: one push, then quiet for the cooldown.
- Restart `zwave-js-server`: the controller logs the dropped connection and
  exits non-zero rather than sitting on a dead socket, so systemd restarts it.

## Notes

- The Ring 1st-gen keypad sleeps 1–5 s after activity. LED writes fired
  immediately inside the notification callback generally land; later writes
  may not apply until the next button press. The firewall toggle is the
  load-bearing behavior and is independent of LED delivery.
- The keypad must be included with S0 or S2 — unencrypted would leak PINs.
- `zwave-js-server` dropping the websocket used to leave the service parked on
  a dead socket forever — alive from systemd's point of view, but deaf. The
  listener is now watched and a disconnect exits non-zero so the unit restarts.
