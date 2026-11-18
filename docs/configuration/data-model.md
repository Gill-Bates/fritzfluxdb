# Polling and data model

fritzFluxDB runs independent asynchronous handlers for FRITZ!Box data, Lua data, log/config data,
and the selected database writer. The writer buffers measurements and retries transient HTTP
failures with exponential backoff; the rules are in [InfluxDB](../storage/influxdb.md#retention-and-writes).

## Collection cadence

| Data family | Built-in cadence |
| --- | --- |
| TR-064 service data | 60 seconds |
| Lua service data and smart home data | 60 seconds |
| Active network hosts and connection details | 10 minutes |
| System statistics | 150 seconds |
| FRITZ!Box log/call data | 60 seconds |

`FRITZBOX_REQUEST_INTERVAL` is a lower bound for requests. It can slow a service down but never
overrides a longer service-specific interval.

## Measurement and table names

The default base name is `fritzbox`. Once the FRITZ!Box serial is available, the daemon uses
`fritzbox_<serial>` after replacing non-alphanumeric characters with underscores. This keeps data
from replaced hardware separated while preserving history for the same box.

The same logical name is an InfluxDB measurement. QuestDB stores it in the tables
`<name>_metrics` and `<name>_events`. The supplied dashboards use a
configurable measurement variable because the serial-specific name is discovered at runtime.

Home-automation series use the FRITZ!Box AIN as the `id` tag and keep the device name for display.
VPN series include the router-provided connection ID when available. If the router omits that ID,
the name and VPN type remain the available identifiers, so duplicate names cannot be distinguished.

## Metric units

`energy_consumption` is the FRITZ!Box Energy Monitor's current percentage (`actPerc`) for each
component, relative to its maximum operating load. It is not a power reading in watts; an unused
component can legitimately report `0%`.

`dsl_line_length` forwards the integer `lineLength` reported by the FRITZ!Box's DSL overview.
The daemon does not calculate or calibrate a cable distance from it.

`systemuptime` and `linkuptime` are reported in seconds. The dashboards display them as days,
hours, minutes, and seconds instead of accumulating hours beyond 24.

The daemon reads the active WAN connection status and uptime over TR-064 every 60 seconds,
using the service selected as described below. When a connected DSL session approaches 24 hours, it logs one
estimate of the possible renewal time. The router reports only the elapsed connection time, so this
estimate does not prove that the provider will disconnect it. A reported disconnected or disconnecting
status logs an outage, including `NewLastConnectionError` as the router's last connection error when
available; the next
`Connected` status logs recovery. If a drop and return happen between polls, a
decrease in uptime reports the restart afterward. A database write error alone does not establish
a WAN outage or identify who initiated a disconnection.

### WAN interruption causes

The active WAN service is read through TR-064
`Layer3Forwarding:GetDefaultConnectionService`, independently of the physical access type.
This also covers PPPoE over Ethernet or fiber. If that action is unavailable or the monitoring
user lacks its configuration permission, the daemon uses the access-type compatibility path,
preferring the native WAN IP service over IGD where available. WAN service selection is refreshed
every minute and on reconnect. An initially failed access-type query is retried every minute,
so a temporary startup failure does not permanently disable the watcher. Switching the selected
WAN service starts a fresh counter baseline.

A separate light poller (`FRITZBOX_WAN_WATCH_INTERVAL`, default 5 seconds) reads the WAN status,
the DSL sync state, and now and then the router uptime over TR-064. The FRITZ!Box stays reachable
on the LAN during an outage, so an interruption is classified while it happens. Only fixed codes
are logged. The log shows `cause not yet known` at first detection, then `cause: <cause> (<confidence>)`
when it is refined, and `ended after <n> s` with cause, confidence and evidence codes at the end.

| Cause | Confidence | Evidence |
| --- | --- | --- |
| `router_restart` | `confirmed` | The router uptime decreased. |
| `router_restart` | `probable` | The derived router start time moved forward, but the counter did not decrease. |
| `dsl_sync_loss` | `confirmed` | A poll during the interruption reported the DSL sync as lost. |
| `planned_disconnect` | `probable` | TR-064 `NewLastConnectionError` changed from a known different code to `ERROR_FORCED_DISCONNECT`, even if the WAN is already connected again. |
| `planned_disconnect` | `probable` | DSL sync stayed up, the WAN status was seen as down, and the disconnect fits the forced-disconnect window. Never `confirmed`. |
| `ppp_interruption` | `probable` | DSL sync stayed up and the WAN status was seen as down, with no window match. |
| `unknown` | `unknown` | Anything else: non-DSL link, DSL data unavailable, or an interruption seen only through a start-time shift. |

Interruptions between two polls are found by deriving a start time (observation time minus uptime)
for the WAN link and the router. A start time that moves forward by more than a small tolerance
(5 seconds plus clock drift over the time since the last good sample) means the counter restarted,
even if the uptime never visibly dropped. Clock steps on the host (NTP, suspend) are detected and
never reported as an interruption. A start-time shift alone is at most `probable`. A link uptime of
about 24 hours is no evidence by itself, because classification only runs for a detected interruption.

The [TR-064 WAN PPP Connection specification (v16)](../assets/TR-064_WAN_PPP_Connection_v16.pdf)
defines `NewLastConnectionError` in `GetInfo` and `GetStatusInfo`. The PDF is kept in the
repository as the reference for this behavior; its source is
<https://fritz.support/resources/TR-064_WAN_PPP_Connection.pdf>. A newly reported
`ERROR_FORCED_DISCONNECT` detects a forced disconnection independently of uptime, a sampled down
status, or a DSL sync reading. A code already present at daemon startup or left unchanged is
ignored. The code does not identify who initiated the termination or prove a scheduled daily
disconnect, so the classification stays `probable`; router restart and observed DSL sync loss
take precedence. If only the error transition is available, the duration is the observation bound
between polls. If the box clears the error before it is polled, this signal cannot identify the cause.

The forced-disconnect window is read from the FRITZ!Box (`X_AVM_DE_GetAutoDisconnectTimeSpan`,
DSL only) and refreshed every 15 minutes. If it is enabled, the disconnect must fall into the
configured hour of the box's local time (plus or minus 5 minutes) after a session of at least
12 hours. The local time offset is measured against the box, not taken from `FRITZBOX_TIMEZONE`.
Temporary window or clock-query failures retain the last usable window and retry after 60 seconds.
An unavailable or closed published session is reported through the watcher's bounded backoff.
If the box reports no usable window (disabled, action missing, error, or offset not measurable),
the daemon logs once that it falls back to the session length: a disconnect about 24 hours
(plus or minus 15 minutes) after the session began counts as the window. On non-DSL links only a
new explicit forced-disconnect code can indicate a planned disconnect. The router log text is not used as evidence, because message
texts change between FRITZ!OS versions.

When an interruption ends, three fields are written once (QuestDB table `<name>_metrics`,
timestamp of the closing observation): `wan_interruption_cause` and `wan_interruption_confidence`
(text), and `wan_interruption_duration` (seconds; an upper bound with poll resolution, omitted when the outage lies in a longer
observation gap and its length is unknown). The
interim state is only logged. InfluxDB receives the same fields without extra setup.

Durations use the monotonic clock. Completed events anchor both start and end to the closing
observation's wall clock, so a host clock correction cannot produce a negative duration or an
inverted interval. An earlier opening log keeps the timestamp observed at that time; the completed
event's start can therefore differ after a clock correction. A reliable link uptime can narrow
the end of the interval. Events detected between polls start at the last confirmed connected
sample, independently of whether its error code or uptime was readable. If no samples were
available for a longer period, the event keeps the last observation as its boundary and omits
the unknown duration. It does not turn the observation gap into an outage length.

A replacement TR-064 session rediscovers optional DSL and disconnect-window signals and refreshes
the cached window. It keeps the interruption tracker's history for the same WAN service, so a
reconnect during an outage does not discard that outage.

Limits:

- A connected WAN with unreachable internet is not detectable from uptime or status. There is no
  active reachability check.
- An interruption shorter than the poll interval is seen only as a start-time shift and usually
  stays `unknown` unless TR-064 reports a new forced-disconnect code. A DSL resync shorter than
  the interval may appear as `ppp_interruption`.
- A forced-disconnect error counts only when it is new. If the FRITZ!Box keeps reporting
  `ERROR_FORCED_DISCONNECT` after the disconnect (not checked on a live box), later disconnections
  are not treated as new and are classified by the remaining rules.
- The hour window relies on the clock of the FRITZ!Box; a wrong box clock can only yield `probable`.
- The AVM action names and the DSL sync behavior during a resync could not be tested on a live box.
  If an action is not available, the DSL state stays unknown and DSL interruptions are `unknown`.

## Startup preflight

Before the first measurement is polled, fritzfluxdb checks every counterpart. A rejected setting
stops the daemon with exit status `78`; a device that is only unreachable is retried and never
causes `78`.

| Counterpart | Checked by | Exit `78` | Retried (exit `1` on failure) |
| --- | --- | --- | --- |
| FRITZ!Box TR-064 | `DeviceInfo` query | HTTP 401 (wrong credentials); TR-064 switched off | Box unreachable |
| FRITZ!Box Lua (FRITZ!OS 7 and newer) | Login | Rejected credentials | Login block (`BlockTime`), unreachable |
| InfluxDB v1 / v2 | Empty write request (stores nothing) | HTTP 401/403; HTTP 404 (database, organization or bucket missing) | Unreachable |
| QuestDB | `SELECT build();` | HTTP 401/403; version older than 10 | Unreachable |

TR-064 is switched off when the FRITZ!Box offers no TR-064 description. Enable it under
**Home Network > Network > Network Settings > Allow access for applications**. The same 401/403/404
answers from the database during operation also stop the daemon with `78`.

At startup, the first rejected FRITZ!Box login or switched-off TR-064 access stops the daemon at
once with `78`. During operation, the daemon stops with `78` only after 3 consecutive rejected login
attempts (Lua login, TR-064 `DeviceInfo:GetInfo` probe, or switched-off TR-064 access); a new
login is attempted at most every 30 seconds. A successful attempt or a network error resets the
count, so a box that restarts and answers a login briefly with a rejection does not end the daemon.

A login block (`BlockTime`) neither counts nor resets the count; the next Lua login waits at least
for the block time, and the block is logged once per phase. At startup a block is temporary (exit
`1`, never `78`).

TR-064 checks the login again during operation only when a polling cycle has queried actions and
every one of them answered `401`. The session then counts as suspect and the next cycle runs a
counted login with the `DeviceInfo:GetInfo` probe, so a changed password ends with `78` after 3
rejected probes, about a minute later. If the probe is accepted, the `401` answers are missing
rights of the FRITZ!Box user for those actions: the services back off, a warning names the cause,
and the check is not repeated for 15 minutes. A `401` on only some actions never triggers a login
check. While the box is unreachable the probe is retried with a growing pause (60 seconds up to 5
minutes) and is not counted.

## Failure and shutdown behaviour

- A missing or invalid configuration, rejected credentials and a missing database target return
  exit status `78`; the watchdog parks instead of retrying (see [Watchdog](watchdog.md)).
- Transient backend failures are retried and logged with connection-loss/recovery messages.
- On `SIGINT`, `SIGTERM`, or `SIGHUP`, producers stop first (waiting at most 5 seconds for running
  FRITZ!Box requests), the measurement queue drains for up to 10 seconds, and the writer then closes.
- A full buffer drops the oldest measurements so the daemon remains bounded in memory.
