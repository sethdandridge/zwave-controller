# zwave-controller

> **Warning**: This project was nearly 100% vibecoded. The UniFi API key
> required to toggle firewall policies is extremely permissive — it grants
> full network admin access. Use at your own risk.

Bridges a Ring Alarm Keypad (1st Gen, paired via `zwave-js-server`) to a UniFi
zone-based firewall policy. A correct PIN + **Disarm** pauses the policy; the
**Arm Away** / **Arm Home** buttons resume it.

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

## Finding your UniFi firewall policy UUID

```
curl -sk -H "X-API-KEY: $UNIFI_API_KEY" \
  https://<unifi-host>/proxy/network/v2/api/site/default/firewall-policies \
  | jq '.[] | {_id, name, enabled}'
```

Paste the `_id` into `UNIFI_POLICY_ID`. An API key is created in the UniFi OS
Control Plane → Admins → Create API Key.

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

## Notes

- The Ring 1st-gen keypad sleeps 1–5 s after activity. LED writes fired
  immediately inside the notification callback generally land; later writes
  may not apply until the next button press. The firewall toggle is the
  load-bearing behavior and is independent of LED delivery.
- The keypad must be included with S0 or S2 — unencrypted would leak PINs.
