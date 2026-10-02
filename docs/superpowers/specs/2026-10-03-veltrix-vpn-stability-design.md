# Veltrix VPN Stability Design

## Goal

Restore reliable Windows gaming connectivity without disrupting existing users,
and stop a single short monitoring failure from producing confusing Telegram
messages.

## Evidence

- The affected Hiddify profile uses the legacy plain VLESS listener on
  `64.188.64.159:8443`.
- Hiddify recorded simultaneous TCP timeouts and remote resets for that listener.
- 3x-UI/Xray did not restart, both VPS network interfaces report no local errors,
  and no VPN maintenance or client mutation ran at the incident time.
- The same node had one short SSH health-check transport failure on 2 October at
  18:24 Moscow time and recovered about four minutes later.
- The watchdog sent its automatic failure/recovery pair for that transient event.
- The second ready REALITY endpoint on `2.26.83.221:443` currently has lower
  latency from the affected Windows computer and no observed packet loss.

## Replacement profile

Issue one additional profile for the existing subscription on worker 2, using
the existing production provisioning path and the ready REALITY listener on TCP
443. Name it `Windows · Hiddify`. Keep the legacy `test1` profile active during
verification and rename it `Старый профиль · 8443` so the cabinet makes the
difference clear. Do not change the subscription, UUIDs of existing profiles,
payments, public-trial flags, or other customers.

Verify the new profile without printing or persisting its URI in diagnostics:

1. Confirm the access-key row becomes active on worker 2 and its saved URI is a
   REALITY profile on port 443.
2. Confirm the expected client exists on the selected 3x-UI inbound.
3. Run real HTTP, HTTPS and UDP-DNS traffic through an isolated local client.
4. Keep the old profile until the owner has used the new profile in Hiddify.

## Watchdog behavior

Reuse the existing JSON state file. Do not add a database table, queue or new
dependency.

- On the first observation of a new failing-code set, save it as pending with
  `notified=false` and send nothing.
- If the next watchdog run sees the same failing-code set, send one alert and
  mark it notified.
- If service health returns before an alert was sent, clear the pending failure
  silently.
- If health returns after an alert was sent, send one recovery message.
- Keep retrying a failed Telegram delivery on later runs without losing the
  pending state.
- A changed failing-code set restarts the confirmation window while no alert has
  been sent. During an already reported outage, update the stored codes without
  sending another alert; send one recovery when all checks pass again.

Use clear administrator-facing copy:

- Alert: `Veltrix VPN: мониторинг обнаружил устойчивую проблему.` Explain that
  connections may be unstable, point to the `Готовность` section, and retain the
  technical codes on a separate line for diagnosis.
- Recovery: `Veltrix VPN: работа сервиса восстановлена.` State that checks pass
  again and no action is required.

## Verification and rollout

Add focused state-sequence tests before changing production code, then run the
complete watchdog test module and Ruff. Run the broader backend suite before a
pull request. Merge only after CI succeeds. Deploy the merged revision to the
control server after a fresh backup and clean-tree check. The watchdog is a
one-shot timer and picks up the new code on its next run; VPN services do not
need a restart for the notification change.

Hysteria2/TUIC is intentionally out of scope. Add a gaming-specific UDP-native
protocol only if the new REALITY profile still drops during real play.
