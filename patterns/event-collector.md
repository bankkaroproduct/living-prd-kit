# Event collector pattern

"Every tracking event fires visibly" must stay cheap at every tier — including a single-file T1 mock. This is the standard ~40-line collector: an on-screen panel + `window.__events`, no dependencies. Tech replaces `track()`'s body with the real analytics SDK; **event names and properties transfer verbatim from TRACKING.md.**

## Drop-in (T1/T2 single-file or any web prototype)

```html
<script>
/* Living PRD event collector — MOCK, never ships. Contract: TRACKING.md */
window.__events = [];
function track(name, props = {}) {
  const e = { name, props, ts: new Date().toISOString() };
  window.__events.push(e);
  console.log('[track]', name, props);
  const panel = document.getElementById('__evt') || (() => {
    const p = document.createElement('div');
    p.id = '__evt';
    p.style.cssText = 'position:fixed;bottom:0;right:0;width:340px;max-height:45vh;overflow:auto;' +
      'background:#111;color:#7fdc7f;font:11px/1.5 monospace;padding:8px;z-index:99999;' +
      'border-top-left-radius:8px;opacity:.94';
    p.innerHTML = '<b style="color:#fff">events (' +
      '<a href="#" style="color:#8cf" onclick="copyEvents();return false">copy</a>)</b><hr style="opacity:.2">';
    document.body.appendChild(p);
    return p;
  })();
  const row = document.createElement('div');
  row.textContent = `${e.ts.slice(11,19)} ${name} ${JSON.stringify(props)}`;
  panel.appendChild(row);
  panel.scrollTop = panel.scrollHeight;
}
function copyEvents() { navigator.clipboard.writeText(JSON.stringify(window.__events, null, 2)); }
</script>
```

Usage at every instrumentation point: `track('plan_configured', { si: '30L', deductible: '3L' })`.

## Rules

- Event names/properties come from `TRACKING.md` — the collector is the proof they fire, the doc is the contract.
- The panel ships in every prototype build; it's the first thing a reviewer and the cold session look at while clicking through.
- **Event audit** (at G4 freeze, rerun on the real build at G6 reconcile): click the full flow, `copy` the export, diff against `TRACKING.md` → `EVIDENCE/event-audit-<date>.md`. 1:1 both directions.
- T3 forks that already have a real analytics layer: point the SDK at a console/collector sink in the prototype env instead — same audit applies. Never fire prototype events into production analytics.
