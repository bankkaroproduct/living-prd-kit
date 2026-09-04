# Deployment design and recovery contract

The package is an independent Python-standard-library supervisor plus a
single-process remote worker. The supervisor delegates authorization, approval
consumption, monotonic slot state, the first-mutation marker, failure markers,
rollback activation, and terminal receipts to the canonical external
`deployment_slot.py`; it does not reimplement those controls.

The worker is transmitted as exact runner source over one SSH session. It opens
the existing account-wide flock once and retains its descriptor through the
slot. Every child is launched without a shell in a new process group with a
deadline and bounded output. SSH configuration, forwarding, agents, X11, and
local commands are disabled; the pinned identity and known-hosts files are
re-hashed before both SSH and SCP.

The active nginx parent files are admitted by digest and unique anchors, then
snapshotted privately and atomically patched after the mutation marker. Expanded
`nginx -T` output must prove exactly one managed route, socket, trusted marker,
and rate-limit zone. Rollback restores the parent bytes before validation and
reload.

The write fence is durable at
`/var/lib/prd-studio/deployment-control/write-fence`, beneath root-controlled
directories that the application can traverse but cannot modify. It survives a
reboot. The application process has its private service group and a separate
socket group; nginx receives only the socket group. Effective-access probes,
not mode inference alone, prove that nginx cannot read application credentials
and the application cannot read proxy/TLS authentication material.

The reviewed design requires success to use a write-fenced prepare and a durable
global-commit-in-doubt marker. The canonical controller result must be the
coordinator decision. Finalization must be idempotent at every write/unlink/fsync
prefix and may remove recovery records and the fence only for an exact terminal
`deployed_verified` result after revalidating the full live gate set. If the
canonical result is absent, recovery must select rollback and never guess.

The current source does **not** yet satisfy that recovery design for every early
provisioning and finalization crash point. Therefore the packaged `execute`
path, supervisor execute/reconcile methods, and remote-worker entry/dispatcher
are hard-disabled before input or mutation. The remaining supervisor/worker code
is an auditable implementation draft, not a certified release runner. A future
change must add the durable per-mutation journal, boot/worker-death resolver,
controller fault reconciliation, idempotent finalizer, bounded transport
recovery, and independent fault certifications before removing every guard.

Canonical absence cannot represent user data. It is appropriate only before the
first team write. Day-2 release design must replace this overlay with encrypted
state snapshots, bounded drain, forward/backward schema compatibility, and a
tested restore path.
