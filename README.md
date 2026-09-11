# zwave-controller

> **Warning**: This project was nearly 100% vibecoded. The UniFi API key
> required to toggle firewall policies is extremely permissive — it grants
> full network admin access. Use at your own risk.

Bridges a Ring Alarm Keypad (1st Gen, paired via `zwave-js-server`) to a UniFi
zone-based firewall policy. A correct PIN + **Disarm** pauses the policy; the
**Arm Away** / **Arm Home** buttons resume it.

Also sends [ntfy](https://ntfy.sh) push notifications for door-open, motion,
and sensor health events from every other node on the controller — see
[Notifications](#notifications).

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
| Door/window **opened** | yes | high |
| Door/window **closed** (back to idle) | no (DEBUG log only) | — |
| Motion detected | yes, unless `NOTIFY_MOTION=false` | default |
| Tamper — cover removed, product moved | yes | urgent |
| Battery low (`isLow`, or level ≤ `NOTIFY_BATTERY_THRESHOLD`) | yes | default |
| Node stopped responding / recovered | yes | high / low |

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
| Door, tamper | `NOTIFY_DOOR_COOLDOWN_SECONDS` (15) | Short on purpose. A door only fires on the open transition, so it cannot flood — and a long window would mean someone entering minutes after you did goes unreported. Just long enough to absorb a chattering reed switch. |
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
- Press **Arm Away**: logs show `arm_away accepted`; policy re-enabled.
- Wrong PIN + **Disarm**: logs show `disarm rejected: bad PIN`; no state change.
- `sudo podman kill zwave-controller` — systemd restarts the unit within 5 s.
- Startup logs a `watching node N: <name>` line per node — confirm the door and
  motion sensors are listed.
- Open the door: a push arrives within a second or two. Close it: no push, only
  a DEBUG line. Open it again immediately: suppressed, with a DEBUG line saying
  how far into the cooldown it was.
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
