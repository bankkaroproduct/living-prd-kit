# MOCKS — <feature>

> A mock is a **promise about the real integration**. Tech builds the real thing to these contracts, so a wrong mock is a spec bug, not a prototype shortcut. Every mock site in code is annotated `// MOCK — contract in MOCKS.md`. **Mocks never ship.**

| Owner | Status |
|---|---|
| <pm> | Draft |

## Inventory

| ID | Service mocked | Why mocked | Lives at (file/route) | Visible how |
|---|---|---|---|---|
| M1 | <e.g. staging SMS gateway> | too expensive to integrate for a prototype | `mocks/sms.js` | mock banner + collector log |

## Contract per mock

### M1 — <service>

**What the real integration must satisfy:**

| Aspect | Contract |
|---|---|
| Call | `<METHOD> <endpoint>` — request shape (fields, types) |
| Success response | shape + the fields we actually consume |
| Failure modes simulated | which errors the mock can produce, and how to trigger each in the prototype |
| Failure modes NOT simulated | known real-world failures the mock ignores — tech must handle these anyway |
| Latency behaviour | <e.g. mock adds 800ms; real p95 is ~2s — timeouts must assume real> |
| Side effects in real system | <what actually happens — SMS sent, money moved — that the mock only logs> |
| Source of contract | <partner API doc link / observed staging behaviour / assumption ⚠️ (→ DECISIONS.md)> |

**Trigger matrix** — how to make the mock produce each behaviour while clicking the prototype:

| Behaviour | How to trigger |
|---|---|
| success | <e.g. any valid input> |
| <error X> | <e.g. phone ending 0000> |
