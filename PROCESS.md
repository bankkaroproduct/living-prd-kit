# How we ship — the Living PRD, in plain words

**Build a working version of the feature. Get it checked twice. Then tech builds against it.**

That's the whole process. Instead of writing a document that describes the feature, the PM (or designer) builds a version you can click — with AI doing the heavy lifting — and that working version, plus a small folder of notes, is the PRD. Seven steps, two checkpoints, three signatures.

## The seven steps

**1. Pitch it** — *PM, half a day.* Write down the problem, what success looks like, and what you're **not** doing. Decide how real the draft needs to be: a **clickable picture** (look-and-feel work — carries a lighter folder, since there's nothing faked to document), a **working fake** (new flows, made-up data and APIs), or a **copy of the real product** (changes to existing behaviour, built on the shared fork). Data in the draft: **made-up data — always fine · cleaned staging data — only via the approved scrubbing pipeline · raw customer data — never.** If the feature touches **money, KYC, personal data, complex integrations, or another team** — the full process is compulsory. Anything else, skip the ceremony and just build.

**2. Sketch the flow** — *PM + Designer, a day.* List the screens, what the user can do on each, and what can go wrong. Designer leads when it's a visual project. This list becomes your to-do for the draft.

**3. Show a working draft early** — *+ Tech lead + QA, a 30-minute review.* One journey working end to end — fakes are fine. Tech and QA see it **now**, not at the end. They can push back on exactly three things: **can't be built, not safe, can't be tested** — not taste, not scope. Together you write the checklist of scenarios the finished version must handle. This meeting replaces the PRD review meeting.

**4. Finish it** — *PM/Designer + AI, the main build.* Every screen, every error message, every validation, every edge case on the checklist — working. Every tracking event actually fires (a small panel on screen shows them as you click). Anything faked stays clearly labelled. AI reviews continuously for what you've missed.

**5. Lock it** — *PM + Tech lead + QA sign.* First, the **stranger test**: an AI that had no part in building it gets your folder and must answer any "what happens if…?" question — wrong PAN, payment fails mid-way, user comes back after 30 days. If it can't answer from the folder alone, you're not done. That test, and the automated checks, are entry requirements — **the decision itself is human**: three signatures, given as real GitHub approvals — **you** ("this is what I want"), **tech lead** ("we can build this"), **QA** ("we can test this"). The lock gets a version number and a fingerprint, so what was signed can never quietly change — any real change needs a re-sign, not a Slack message.

**6. Tech builds the real one** — *Engineering.* For each part, tech decides: **keep** your code, **rebuild** it properly, or treat it as **reference only**. Fakes always get rebuilt as real integrations — a fake is a promise of how the real one must behave, and the promise is written down. Your draft stays the answer key: same input, same expected behaviour.

**7. Match check, then ship** — *QA + PM.* Before launch, QA compares what was built against what was signed — scenario by scenario — and confirms the analytics really fire. Differences get fixed or explicitly approved. Nothing drifts silently.

## The one honesty rule

Every part of the draft is marked **REAL** (talks to actual systems — trust it), **MOCKED** (a stand-in with its behaviour written down — trust the behaviour, not the numbers), or **STATIC** (a picture that computes nothing — trust the layout, nothing else). Tech never has to guess which is which.

One safety rule that never bends: staging data gets personal details scrubbed **before** it touches a draft. An Aadhaar number in a prototype is treated as a production incident.

## What's in the folder

One command creates the whole folder — `bin/new-prd.sh <feature-name>` — and one command checks it at any point: `bin/validate.py <folder>` (it also runs automatically on every push).

| Note | What it holds |
|---|---|
| Cover sheet | What this is, how real each part is, who signed, exact versions locked |
| How it behaves | Each screen: fields, rules, exact error messages, what can go wrong |
| What we track | Every analytics event and when it fires |
| What's faked | Each stand-in, and how the real integration must behave |
| API truth | The exact API specs the draft was built against, pinned — for APIs, these outrank everything |
| Decisions & notes | Open questions (with a default so nothing blocks), your context |
| Proof | Test runs, the stranger-test transcript, screenshots |

## Where's the tool?

**Today: this folder is the tool, version zero** — plus three commands: one that creates a bundle, one that checks it, one that locks it. Automated checks run on every push.

**First: one worked example.** We retrofit an already-shipped feature into a bundle so every PM has a real reference to copy from, not just blank templates.

**Next: two pilot features** run through the seven steps — one visual project, one integration-heavy project that trips the "compulsory" rule. We measure what matters: prep effort, handoff time, clarification round-trips, rework, and states discovered late. The pilots tell us which steps actually hurt.

**Then: the app** — guided intake, draft viewer with scenario switching, review inbox for tech and QA, the three-signature lock, version pinning to GitHub and Figma. The PM team builds it as its own first living PRD (pilot #3); engineering takes it over at the lock step. It deliberately does **not** try to be an IDE, a Jira replacement, or a deployment platform.

## For engineers and AI agents

The strict version — machine-readable cover sheet (`prd.manifest.yaml`), gate IDs G0–G6, the `REAL / MOCKED / STATIC` labels as schema enums, the freeze-release format, checklists — is `LIVING_PRD_STANDARD.md` in this kit. It exists so tech's AI tooling can consume a PRD without a meeting. **Humans never need to read it.**
