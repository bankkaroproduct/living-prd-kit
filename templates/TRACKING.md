# TRACKING — <feature>

> **Every event here fires visibly in the prototype** (event collector — `patterns/event-collector.md`), and every event the prototype fires is here. 1:1, both directions — the cold-session test checks this.

| Owner | Status | Naming convention |
|---|---|---|
| <pm> | Draft | <e.g. snake_case, `<surface>_<object>_<action>`> |

## Events

| Event | Trigger (exact) | Properties | Surface(s) | Fires in prototype? | Notes |
|---|---|---|---|---|---|
| `plan_configured` | user taps Continue on Screen 1 with valid config | `si`, `deductible`, `mix_code`, `indicative_premium` | web, app | yes | |

**Property rules:** type + allowed values per property; no free-text where an enum exists; user identifiers = <join key>, never raw PII.

## Funnel

<The ordered event chain that defines the funnel, and the drop-off points that matter. One line per step.>

## Non-events (explicitly not tracked)

<Things a reader might assume are tracked but aren't, and why — prevents the "where's the event for X" thread.>
