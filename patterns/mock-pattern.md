# Mock pattern — expensive services

For dependencies too expensive to rebuild or integrate in a prototype (SMS gateway, payment, KYC, insurer APIs). A mock here is a **promise about the real integration** — it must be visible, loggable, triggerable, and contracted.

## The four properties of an acceptable mock

| Property | Meaning | How |
|---|---|---|
| **Visible** | Nobody can mistake it for the real thing | Mock banner in the UI when a mocked path runs ("MOCK: SMS — no message sent"); `// MOCK — contract in MOCKS.md` at every code site |
| **Logged** | Every call it receives is inspectable | Log through the event collector as `mock_<service>_called` with the request payload |
| **Triggerable** | Every failure mode it simulates can be produced on demand while clicking | Magic inputs (phone ending `0000` → delivery failure) documented in MOCKS.md's trigger matrix |
| **Contracted** | The real integration is fully specified | MOCKS.md entry: request/response shapes, failure modes simulated AND not simulated, latency, real-world side effects |

## Shape (T2 standalone / T3 fork)

```js
// mocks/sms.js — MOCK, never ships. Contract: MOCKS.md#M1
export async function sendSms({ phone, template, params }) {
  track('mock_sms_called', { phone: mask(phone), template });
  await delay(800);                      // stated in contract; real p95 ~2s
  if (phone.endsWith('0000')) return { status: 'FAILED', code: 'DND' };   // trigger matrix
  return { status: 'SENT', messageId: `mock-${Date.now()}` };
}
```

Wire it behind the same interface the real client will use (`services/sms.js` exports either impl by env flag). Tech's job at build: implement the interface against the real service per the contract; delete `mocks/`.

## T1 single-file mocks

Same rules, smaller: a `MOCKED` badge on the affected UI, `track('mock_*_called', …)`, and the MOCKS.md entry. Hardcoded responses are fine — the contract still isn't optional, because the contract is what tech builds from.

## Never

- A mock that silently succeeds where the real service can fail — every real failure mode is either simulated or listed as NOT simulated in the contract.
- A mock reachable from any shipped build.
- A mock whose contract lives only in code comments — code is not the spec; MOCKS.md is.
