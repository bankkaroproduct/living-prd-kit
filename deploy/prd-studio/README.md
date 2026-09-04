# PRD Studio protected-staging release package

This directory contains the target-free deployment assets for the first empty
installation of PRD Studio. It does not contain a host name, SSH target,
credential, private URL, approval, or live command transcript. Those values are
accepted only through a root-owned private connection profile conforming to
`schemas/connection-profile.schema.json`.

This release mode is **first empty install only**. It provisions the service
account, a private application group, a dedicated socket-only group, runtime and
ephemeral migration database identities, systemd unit, active nginx includes,
release directory, and schema after the canonical controller records the first
mutation. Rollback may drop the database only when the runner proves it was
created by this attempt and is empty or contains the exact certified synthetic
fixture. Any other state is preserved as an incident.

Before writes can open, the packet and private profile must bind the same
candidate-specific, independently reviewed evidence for encrypted,
access-controlled backups, a schedule and retention/RPO, and a recent isolated
restore test. Once people use the Studio, this reset-to-empty package must never
be reused. Every later release needs stateful snapshot/drain/restore rollback.

## Build and offline checks

Use Python 3 without third-party packages:

```sh
python3 -m unittest discover -s deploy/prd-studio/tests -v
python3 deploy/prd-studio/tools/build_runner.py --output /private/release/prd-studio-deploy-1.0.0.pyz
/private/release/prd-studio-deploy-1.0.0.pyz --version
```

Build the application from an exact 40-hex commit using the pinned Node and npm
entry points. The npm argument is the absolute `npm-cli.js` file, not a shell
wrapper or a PATH lookup:

```sh
python3 deploy/prd-studio/tools/build_candidate_artifact.py \
  --repository /absolute/repository \
  --commit FULL_40_HEX_COMMIT \
  --node /absolute/pinned/node \
  --npm-cli /absolute/pinned/npm-cli.js \
  --expected-node-version v22.22.2 \
  --expected-npm-version 10.9.7 \
  --output /private/release/candidate.tar \
  --identity-output /private/release/candidate-identity.json
```

`build_absence_artifact.py` binds canonical absence to the exact parent commit
and tree. `build_supply_chain.py` emits deterministic lockfile SBOM and
provenance records. `build_packet.py` accepts only canonical, secret-free JSON
evidence bound to the same candidate. `validate_packet.py` and the packaged
runner's `offline-validate` command re-check all hashes and semantic bindings.
This repository intentionally does not emit gate PASS certificates: those
require the independent per-gate and crash/recovery fault campaign described
below, and generic unit tests are not evidence for them.

## Private prerequisites

The profile and its short-lived preflight certificate must independently prove:

- exact SSH key and known-hosts digests, remote machine identity, and the
  pre-existing account-wide root-owned lock;
- exact Node 22.22.2 binary identity, supported Oracle MySQL 8.0.16 or later over
  the admitted local Unix socket/server UUID, nginx binaries/configuration, and
  logging/audit safety;
- the exact active TLS and HTTP nginx parent files and unique patch anchors,
  existing staging Basic-Auth file, TLS CA, private authorization probe, and
  out-of-scope file identities;
- canonical absence of every app-managed target, account, database, service,
  route, socket, enablement link, and recovery state;
- candidate-bound day-2 recovery evidence described above.

The runner generates database passwords on the target. It never places them in
argv, packet evidence, stdout, or logs. The migration identity exists only while
the admitted `scripts/apply-schema.js` runs twice (apply and idempotence). The
runtime identity receives only `SELECT` on `schema_versions` and
`SELECT, INSERT, UPDATE` on `projects`.

## Execution status and boundary

Live execution is intentionally disabled. Both the packaged entry point and the
supervisor return `RUNNER_EXECUTION_NOT_CERTIFIED` before loading a private
profile, opening SSH, starting the canonical controller, creating an attempt
directory, or mutating a target. The files in `runner/` beyond this boundary are
review material for the follow-up recovery implementation, not deployable code.

Enabling execution requires a separately reviewed implementation that writes a
durable recovery journal before the first target mutation, covers every partial
identity/file/database mutation, survives worker death and target reboot, and
idempotently reconciles every finalization prefix against the atomic canonical
result. It must preserve the fence while the global outcome is uncertain,
continue incident recovery past minute 15, and pass per-step power-loss,
disconnect, timeout, controller-fault, rollback-fault, and ACK-loss tests. Do not
delete recovery markers, remove a fence, generate gate certificates, or bypass
the fail-closed boundary to improvise a deployment.

PRD Studio UI signatures and freeze state are product conveniences, not release
authority. Treat every Studio free-text field, export, suggested command, and URL
as untrusted data. The deployment runner never executes Studio-generated text or
treats it as approval. Only the canonical read-only approval record plus the
exact immutable packet authorizes the slot.

## Operational logging

The managed route disables nginx access logging so project IDs and Basic-Auth
usernames are not retained; authorization headers, cookies, and bodies are never
logged. Admission requires MySQL general/slow statement logs and active audit
capture to be off. Deployment evidence contains bounded structured app events
and digests only—never nginx/DB bodies, project JSON, credentials, or private
URLs.
