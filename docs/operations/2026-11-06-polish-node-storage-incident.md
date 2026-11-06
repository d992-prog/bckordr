# Polish VPN node: storage stall and loss of reachability

Date: 2026-11-06. Moscow time is UTC+03:00.

## Assessment

The Poland node showed severe guest disk stalls and CPU steal before becoming
unreachable from the controller. A storage/hypervisor or other external
infrastructure failure is strongly supported by the observations. The exact
provider root cause is **not proven**; guest filesystem/storage faults have not
been excluded. The observations do not support VPN client capacity exhaustion.

No services were restarted, configurations changed, clients created, credentials
exported, or fixes deployed. Further network probes were stopped.

## Four-node comparison

Direct HTTPS downloads used Cloudflare's speed endpoint. Each successful probe
downloaded 5,000,000 bytes. Rates include connection/TLS startup and are not
measurements through a user's VPN client.

| Node | Probe completion, UTC / Moscow | Direct HTTPS download, two probes | Resource evidence |
| --- | --- | --- | --- |
| Poland (`polish-server`) | Initial probe timed out; health observation completed 13:26:27 / 16:26:27 | No valid throughput result | One vCPU; load 3.06 / 5.93 / 4.56; about 1.54 GiB available RAM; root disk 24.9% used. Later storage stalls detailed below. |
| Netherlands | 13:25:10 / 16:25:10 | 85.0 / 72.6 Mbit/s | CPU busy 4.92%, steal 1.64%; about 1.50 GiB available RAM; root disk 22.0% used. |
| Finland | 13:25:10 / 16:25:10 | 79.4 / 41.8 Mbit/s | CPU busy 1.99%, steal 0%; about 1.49 GiB available RAM; root disk 18.2% used. |
| Germany | 13:25:12 / 16:25:12 | 78.4 / 10.9 Mbit/s | CPU busy 1.67%, steal 0%; about 1.50 GiB available RAM; root disk 29.8% used. Second request spent 3.20 seconds reaching TLS completion. |

The Netherlands, Finland, and Germany samples showed no new NIC errors/drops,
OOM events in the checked 24-hour window, or configured `tc` rate classes.
Their `x-ui` services were active with zero automatic restarts reported by
systemd. Xray ran as a child of `x-ui`; an inactive standalone `xray.service`
was not evidence that the running VPN process had stopped.

Germany had 52 SYN retransmissions and 68 TCP timeouts during a three-second
sample. These aggregate counters cannot be attributed to the user's VPN
connection. They warrant caution about treating Germany as a validated fallback.

## Poland incident evidence

| Observation time, UTC / Moscow | Evidence |
| --- | --- |
| 13:26:27 / 16:26:27, health probe completion | Controller-to-node ping: 4/4 replies, 0% loss, RTT min/average/max 41.662 / 43.393 / 45.009 ms. `x-ui` active; sufficient memory and disk space; no `tc` rate classes. Node uptime about 19.4 hours; `x-ui` start time was 2026-11-05 18:04:21 UTC. |
| 13:27:52 / 16:27:52, five-second sample completion | CPU: 72.92% iowait, 15.93% steal, 0% idle. Load: 2.75 / 5.08 / 4.37. IO pressure `some avg10=100.00%`, `full avg10=76.69%`, `full avg60=70.80%`. Two process leaders were in D state. Xray used about 0.4% CPU; `x-ui` about 1.39%. No new NIC errors/drops or TCP retransmissions in this sample. |
| 13:29:03 / 16:29:03, 5.069-second sample completion | `vda` and its root partition `vda2` had 98 outstanding IO requests and **zero completed reads or writes** during the sample. Both `x-ui` and `systemd-journald` were in D state at `wait_transaction_locked`. Root filesystem: ext4. IO pressure `full avg60=76.43%`. No substantial application disk writer was observed in the short counter sample. |
| After the preceding sample, before the final reachability check | A later diagnostic command timed out after 30 seconds. A subsequent minimal `/proc` counter probe could no longer establish SSH. No usable later CPU or disk snapshot was obtained. |
| By 13:32:44 / 16:32:44, final check time recorded after probe completion | Controller-to-node ping: 0/3 replies, 100% loss. TCP connection to the SSH port timed out after 4.003 seconds. This establishes loss of reachability from the controller, not independently from every network. |

Disk throughput and average completion latency cannot be calculated from the
stalled sample: no requests completed. Zero delta in a busy-time counter during
this condition must not be interpreted as an idle or healthy disk.

## Scope and limits

- Confirmed successful diagnostic download payload: 30,000,000 bytes across the
  Netherlands, Finland, and Germany. The timed-out initial Poland probe could
  have downloaded up to 10,000,000 additional bytes; its actual transferred
  payload is unknown. The maximum planned download payload was therefore
  40,000,000 bytes total, excluding protocol overhead and ordinary SSH traffic.
- No additional response-body downloads were started after the initial batch.
  A later zero-body HTTPS HEAD attempt yielded no usable timing result.
- The probes were short, bounded observations, not sustained capacity tests.
  Fast direct node egress does not establish good end-to-end VPN performance.
  The user's device, ISP path, and actual VPN tunnel were not measured.
- Poland's kernel/provider root cause remains unknown. Reading service logs
  timed out; no complete kernel-log result or provider console evidence was
  obtained. Short IO samples cannot exclude an earlier workload that created
  the backlog.
- No secret values, customer addresses, database row identifiers, or raw private
  logs are included in this report.

## Safe next action

Use the Netherlands or Finland as a temporary client-side alternative, while
recognizing that only their direct egress was tested. Ask the hosting provider
to inspect the Poland VM's console, hypervisor, virtual disk, and storage backend.
The measured storage stall is not addressed by changing VPN application code or
restarting `x-ui`; a restart can itself block on the same storage. Any recovery
action should follow provider/console evidence and separate authorization.

### Ready-to-send hosting support request

> Please investigate a severe storage/VM availability incident on our Poland VPS
> on 6 November 2026. At 13:27:52 UTC (16:27:52 Moscow), a five-second guest sample
> showed 72.92% CPU iowait and 15.93% CPU steal. IO pressure reported 100% `some`
> and 76.69% `full` over the last 10 seconds. At 13:29:03 UTC, the virtual disk
> `vda` and root partition `vda2` had 98 outstanding requests with zero completed
> reads or writes over 5.069 seconds. The ext4 guest had `x-ui` and
> `systemd-journald` blocked in D state at `wait_transaction_locked`. Available
> RAM and disk space were sufficient. By 13:32:44 UTC, our controller received
> no replies to three pings and a TCP connection to SSH timed out. Earlier, around
> 13:26 UTC, ping averaged 43.393 ms with no loss. We made no service, firewall,
> sysctl, or application changes during the investigation. Please check the
> hypervisor, storage backend, virtual-disk queue, and VM console, and advise
> whether this coincides with provider maintenance or an infrastructure incident.
> These guest observations strongly suggest storage/infrastructure trouble, but
> we cannot establish the exact provider root cause without your evidence.
