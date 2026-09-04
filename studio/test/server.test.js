"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const net = require("node:net");
const tls = require("node:tls");
const { once } = require("node:events");
const { afterEach, describe, test } = require("node:test");
const {
  acquireConnection,
  buildPoolOptions,
  checkWriteFence,
  computeFreezeDigest,
  createApp,
  createLogger,
  installServerFailureHandler,
  loadConfig,
  validateFreezeTransition,
  validateProjectData,
} = require("../server");
const { migrationConfig } = require("../scripts/apply-schema");
const { FakePool } = require("./helpers/fake-pool");

const TOKEN = "test-token-that-is-at-least-32-characters-long";
const servers = [];

afterEach(async () => {
  await Promise.all(servers.splice(0).map((server) => new Promise((resolve) => server.close(resolve))));
});

function config(overrides = {}) {
  const environment = {
    AUTH_MODE: "token",
    TEAM_TOKEN: TOKEN,
    BASE_PATH: "/prd-studio",
    HOST: "127.0.0.1",
    PORT: "8080",
    DB_HOST: "127.0.0.1",
    DB_PORT: "3306",
    DB_USER: "test",
    DB_NAME: "prd_studio_test",
    RELEASE_ID: "test-release",
    ...overrides,
  };
  environment[["DB", "PASSWORD"].join("_")] = "x";
  return loadConfig(environment);
}

function project(title = "Checkout PRD") {
  return {
    meta: {
      title, slug: "checkout-prd", owner: "PM", tech: "Tech", qa: "QA", designer: "",
      tier: null, triggers: [], data: "synthetic", pipeline: "", dumpdate: "",
      entry: "", run: "", status: "framing", created: "2026-09-04", g0: "2026-09-04",
      g1: "", g2: "", g3: "", g4: "", g6: "",
    },
    spec: {
      screens: [], edges: [], events: [], fidelity: [],
      scenarios: [{ id: "S1", name: "", demonstrable: false, trigger: "", nd: false, ndreason: "" }],
    },
    review: {
      objections: [], coverageAgreed: "", coverageBy: "",
      techChecks: { feasibility: false, integrations: false, dataBoundaries: false, reuse: false },
      qaChecks: { states: false, validations: false, errorsRetries: false, testability: false },
      dodManual: { secrets: false, runzero: false },
      stranger: { ran: "", defects: -1, report: "" },
      signatures: { pm: "", tech: "", qa: "" }, releaseNum: 1,
      reconcile: {}, eventAudit: false, mocksVerified: false,
    },
  };
}

function captureLogger() {
  const lines = [];
  const output = { log: (line) => lines.push(line), error: (line) => lines.push(line) };
  return { logger: createLogger(output), lines };
}

async function launch({ pool = new FakePool(), appConfig = config(), logger = captureLogger().logger } = {}) {
  const app = createApp({ pool, config: appConfig, logger });
  const server = app.listen(0, "127.0.0.1");
  servers.push(server);
  await once(server, "listening");
  const address = server.address();
  return { origin: `http://127.0.0.1:${address.port}`, pool };
}

function authHeaders(extra = {}) {
  return { Authorization: `Bearer ${TOKEN}`, ...extra };
}

describe("configuration", () => {
  test("requires a long token in token mode", () => {
    assert.throws(() => config({ TEAM_TOKEN: "short" }), /TEAM_TOKEN/);
  });

  test("trusted proxy mode is restricted to a socket or loopback bind", () => {
    assert.throws(() => config({ AUTH_MODE: "trusted_proxy", TEAM_TOKEN: "", HOST: "0.0.0.0" }), /loopback/);
    assert.equal(config({ AUTH_MODE: "trusted_proxy", TEAM_TOKEN: "", HOST: "127.0.0.1" }).authMode, "trusted_proxy");
  });

  test("production requires exact release identities", () => {
    assert.throws(() => config({ NODE_ENV: "production" }), /RELEASE_COMMIT/);
    const production = config({
      NODE_ENV: "production",
      RELEASE_COMMIT: "a".repeat(40),
      RELEASE_TREE: "b".repeat(40),
      RELEASE_ARTIFACT_SHA256: "c".repeat(64),
    });
    assert.equal(production.releaseIdentity.tree, "b".repeat(40));
    assert.throws(() => config({ RELEASE_COMMIT: "not-a-sha" }), /RELEASE_COMMIT/);
  });

  test("rejects relative write-fence paths", () => {
    assert.throws(() => config({ WRITE_FENCE_FILE: "relative/fence" }), /WRITE_FENCE_FILE/);
  });

  test("write-fence inspection only permits ENOENT and rejects non-regular entries", () => {
    const missing = Object.assign(new Error("missing"), { code: "ENOENT" });
    const denied = Object.assign(new Error("denied"), { code: "EACCES" });
    assert.deepEqual(checkWriteFence("/fence", () => { throw missing; }), { allowed: true });
    assert.deepEqual(checkWriteFence("/fence", () => { throw denied; }), { allowed: false, code: "EACCES" });
    assert.deepEqual(checkWriteFence("/fence", () => ({ isFile: () => false })), { allowed: false, code: "WRITE_FENCE_INVALID" });
    assert.deepEqual(checkWriteFence("/fence", () => ({ isFile: () => true })), { allowed: false, code: "WRITE_FENCE_ACTIVE" });
  });

  test("bounds pool acquisition and releases a connection that arrives late", async () => {
    let released = false;
    const pool = {
      getConnection: () => new Promise((resolve) => setTimeout(() => resolve({ release: () => { released = true; } }), 25)),
    };
    await assert.rejects(acquireConnection(pool, 5), (error) => error.code === "POOL_ACQUIRE_TIMEOUT");
    await new Promise((resolve) => setTimeout(resolve, 35));
    assert.equal(released, true);
  });

  test("requires DNS hostname verification for remote runtime and migration TLS", (t) => {
    assert.throws(() => config({ DB_HOST: "10.20.30.40", DB_SSL_MODE: "disabled" }), /verify_identity/);
    assert.throws(() => config({ DB_HOST: "10.20.30.40", DB_SSL_MODE: "required" }), /verify_identity/);
    assert.throws(() => config({ DB_HOST: "10.20.30.40", DB_SSL_MODE: "verify_identity", DB_SSL_CA_FILE: "/tmp/test-ca.pem" }), /DNS DB_HOST/);
    assert.throws(() => config({ DB_HOST: "mysql.internal.example", DB_SSL_MODE: "verify_identity", DB_SSL_CA_FILE: "relative-ca.pem" }), /must be absolute/);

    const relativeMigrationEnvironment = {
      MIGRATION_DB_HOST: "mysql-migration.internal.example",
      MIGRATION_DB_USER: "migration-test",
      MIGRATION_DB_NAME: "prd_studio_test",
      MIGRATION_DB_SSL_MODE: "verify_identity",
      MIGRATION_DB_SSL_CA_FILE: "relative-ca.pem",
    };
    relativeMigrationEnvironment[["MIGRATION", "DB", "PASSWORD"].join("_")] = "x";
    assert.throws(() => buildPoolOptions(migrationConfig(relativeMigrationEnvironment)), /must be absolute/);

    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "prd-studio-ca-"));
    const caFile = path.join(directory, "ca.pem");
    fs.writeFileSync(caFile, "test-ca\n", { mode: 0o600 });
    t.after(() => fs.rmSync(directory, { recursive: true, force: true }));

    const runtimeOptions = buildPoolOptions(config({
      DB_HOST: "mysql.internal.example",
      DB_SSL_MODE: "verify_identity",
      DB_SSL_CA_FILE: caFile,
    }));
    assert.equal(runtimeOptions.host, "mysql.internal.example");
    assert.equal(runtimeOptions.ssl.verifyIdentity, true);
    assert.equal(runtimeOptions.ssl.rejectUnauthorized, true);
    assert.equal(runtimeOptions.ssl.ca.toString("utf8"), "test-ca\n");

    const migrationEnvironment = {
      MIGRATION_DB_HOST: "mysql-migration.internal.example",
      MIGRATION_DB_USER: "migration-test",
      MIGRATION_DB_NAME: "prd_studio_test",
      MIGRATION_DB_SSL_MODE: "verify_identity",
      MIGRATION_DB_SSL_CA_FILE: caFile,
    };
    migrationEnvironment[["MIGRATION", "DB", "PASSWORD"].join("_")] = "x";
    const migrationOptions = buildPoolOptions(migrationConfig(migrationEnvironment));
    assert.equal(migrationOptions.ssl.verifyIdentity, true);
    assert.equal(migrationOptions.ssl.rejectUnauthorized, true);

    const certificate = {
      subject: { CN: "mysql.internal.example" },
      subjectaltname: "DNS:mysql.internal.example",
    };
    assert.equal(tls.checkServerIdentity("mysql.internal.example", certificate), undefined);
    const identityError = tls.checkServerIdentity("wrong.internal.example", certificate);
    assert.equal(identityError.code, "ERR_TLS_CERT_ALTNAME_INVALID");
  });

  test("a listen failure closes the database pool and records a failed exit", async (t) => {
    const blocker = net.createServer();
    blocker.listen(0, "127.0.0.1");
    await once(blocker, "listening");
    t.after(() => new Promise((resolve) => blocker.close(resolve)));

    let poolEnded = 0;
    let exitCode = 0;
    const pool = { end: async () => { poolEnded += 1; } };
    const captured = captureLogger();
    const failed = net.createServer();
    const handled = installServerFailureHandler(failed, pool, captured.logger, (code) => { exitCode = code; });
    failed.listen(blocker.address().port, "127.0.0.1");
    await handled;

    assert.equal(exitCode, 1);
    assert.equal(poolEnded, 1);
    assert.match(captured.lines.join("\n"), /"event":"server_failed"/);
    assert.match(captured.lines.join("\n"), /"code":"EADDRINUSE"/);
  });
});

describe("HTTP and authentication", () => {
  test("serves health, readiness, strict CSP, and base-path runtime config", async () => {
    const { origin } = await launch();
    const health = await fetch(`${origin}/healthz`);
    assert.equal(health.status, 200);
    assert.equal((await health.json()).release, "test-release");

    const ready = await fetch(`${origin}/readyz`);
    assert.equal(ready.status, 200);
    assert.equal((await ready.json()).status, "ready");

    const index = await fetch(`${origin}/prd-studio/`);
    const html = await index.text();
    assert.equal(index.status, 200);
    assert.match(index.headers.get("content-security-policy"), /script-src 'nonce-[^']+'/);
    assert.doesNotMatch(index.headers.get("content-security-policy"), /script-src[^;]*unsafe-inline/);
    assert.equal(index.headers.get("x-content-type-options"), "nosniff");
    assert.match(html, /window\.__PRD_STUDIO_CONFIG__=\{"basePath":"\/prd-studio","authMode":"token"\}/);
    assert.match(html, /<input type="password" id="tokenIn"/);
    assert.match(html, /id="strDefects"/);
    assert.match(html, /id="strConfirm"/);
    assert.match(html, /id="recordStr"/);
    assert.doesNotMatch(html, /id="strReport"|type="file"/);
    assert.match(html, /querySelector\("#recordStr"\)\.disabled=frozen/);
    assert.match(html, /Save current edits before confirming lock evidence/);
    const pollSource = html.slice(html.indexOf("pollTimer=setInterval"), html.indexOf("},4000);"));
    assert.match(pollSource, /request=\{id:projId,project:P,version:rowVersion,editedAt:lastLocalEdit\}/);
    assert.match(pollSource, /api\("\/api\/projects\/"\+request\.id\)/);
    assert.equal((pollSource.match(/stillCurrent\(\)/g) || []).length, 2);
    assert.doesNotMatch(html, /fonts\.googleapis|localStorage|sessionStorage/);
    assert.doesNotMatch(html, /__CSP_NONCE__|__PRD_STUDIO_RUNTIME_CONFIG__/);
  });

  test("token auth fails closed and API responses are not cacheable", async () => {
    const { origin } = await launch();
    const missing = await fetch(`${origin}/prd-studio/api/projects`);
    assert.equal(missing.status, 401);
    assert.equal(missing.headers.get("cache-control"), "no-store");
    const wrong = await fetch(`${origin}/prd-studio/api/projects`, { headers: { Authorization: "Bearer wrong" } });
    assert.equal(wrong.status, 401);
    const allowed = await fetch(`${origin}/prd-studio/api/projects`, { headers: authHeaders() });
    assert.equal(allowed.status, 200);
    assert.equal(allowed.headers.get("cache-control"), "no-store");
  });

  test("bounds repeated token failures without logging the client or token", async () => {
    const captured = captureLogger();
    const { origin } = await launch({ logger: captured.logger });
    let response;
    for (let index = 0; index < 10; index += 1) {
      response = await fetch(`${origin}/prd-studio/api/projects`, { headers: { Authorization: `Bearer wrong-${index}` } });
    }
    assert.equal(response.status, 429);
    assert.equal(response.headers.get("retry-after"), "60");
    assert.doesNotMatch(captured.lines.join("\n"), /wrong-|127\.0\.0\.1|::1/);
  });

  test("conflicts require a user choice and never auto-retry an overwrite", () => {
    const source = fs.readFileSync(path.join(__dirname, "..", "public", "index.html"), "utf8");
    assert.match(source, /window\.confirm\("Someone else saved this PRD first/);
    assert.doesNotMatch(source, /return persist\(1\)|persist\(attempt\)/);
    assert.match(source, /if\(!projId\|\|!P\|\|dirty\|\|saveInFlight\|\|Date\.now\(\)-lastLocalEdit<2500\) return/);
    assert.match(source, /catch\(e\)\{ dirty=true;/);
  });

  test("trusted proxy mode requires the overwritten marker and no browser token", async () => {
    const appConfig = config({ AUTH_MODE: "trusted_proxy", TEAM_TOKEN: "" });
    const { origin } = await launch({ appConfig });
    assert.equal((await fetch(`${origin}/prd-studio/`)).status, 401);
    const index = await fetch(`${origin}/prd-studio/`, { headers: { "X-PRD-Authenticated": "1" } });
    assert.equal(index.status, 200);
    const html = await index.text();
    assert.match(html, /"authMode":"trusted_proxy"/);
    const list = await fetch(`${origin}/prd-studio/api/projects`, { headers: { "X-PRD-Authenticated": "1" } });
    assert.equal(list.status, 200);
  });
});

describe("project API", () => {
  test("creates, lists, reads, updates, and reports optimistic conflicts", async () => {
    const { origin } = await launch();
    const url = `${origin}/prd-studio/api/projects`;
    const data = project();
    const created = await fetch(url, {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ id: "checkout-prd-a1b2", slug: "checkout-prd", data }),
    });
    assert.equal(created.status, 201);
    assert.deepEqual(await created.json(), { ok: true, version: 1 });

    const list = await fetch(url, { headers: authHeaders() });
    assert.equal((await list.json())[0].meta.title, "Checkout PRD");

    const read = await fetch(`${url}/checkout-prd-a1b2`, { headers: authHeaders() });
    assert.equal((await read.json()).version, 1);

    const changed = project("Changed title");
    const updated = await fetch(`${url}/checkout-prd-a1b2`, {
      method: "PUT", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ version: 1, data: changed }),
    });
    assert.deepEqual(await updated.json(), { ok: true, version: 2 });

    const stale = await fetch(`${url}/checkout-prd-a1b2`, {
      method: "PUT", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ version: 1, data }),
    });
    const conflict = await stale.json();
    assert.equal(stale.status, 409);
    assert.equal(conflict.version, 2);
    assert.equal(conflict.data.meta.title, "Changed title");
  });

  test("binds a frozen snapshot, permits reconciliation only, and requires clean re-versioning", async () => {
    const { origin } = await launch();
    const url = `${origin}/prd-studio/api/projects`;
    const draft = project("Frozen snapshot");
    draft.meta.entry = "https://prototype.invalid/example";
    draft.meta.run = "Open the prototype in a browser";
    draft.meta.status = "building";
    draft.spec.scenarios[0] = {
      id: "S1", name: "Checkout completes", demonstrable: true,
      trigger: "Open checkout and submit", nd: false, ndreason: "",
    };
    draft.spec.events = [{ name: "checkout_completed", trigger: "submit", props: "order_id", fires: true }];
    draft.spec.fidelity = [{ path: "checkout", label: "REAL", note: "working path" }];
    draft.review.coverageAgreed = "2026-09-04";
    draft.review.coverageBy = "Tech + QA";
    const evidence = structuredClone(draft);
    evidence.review.dodManual = { secrets: true, runzero: true };
    evidence.review.stranger = { ran: "2026-09-04", defects: 0, report: "Fixed external-test attestation." };
    const signed = structuredClone(evidence);
    signed.review.signatures = { pm: "PM", tech: "Tech", qa: "QA" };
    const frozen = structuredClone(signed);
    frozen.meta.g4 = "2026-09-04";
    frozen.meta.status = "frozen";
    frozen.review.digest = computeFreezeDigest(frozen);

    const missingStrangerSummary = structuredClone(frozen);
    missingStrangerSummary.review.stranger.report = "";
    missingStrangerSummary.review.digest = computeFreezeDigest(missingStrangerSummary);
    assert.match(validateProjectData(missingStrangerSummary), /data\.review\.stranger/);

    const invalidStrangerDate = structuredClone(frozen);
    invalidStrangerDate.review.stranger.ran = "2026-02-30";
    invalidStrangerDate.review.digest = computeFreezeDigest(invalidStrangerDate);
    assert.match(validateProjectData(invalidStrangerDate), /data\.review\.stranger/);

    const pendingMarker = structuredClone(frozen);
    pendingMarker.meta.g4 = "pending";
    pendingMarker.review.digest = computeFreezeDigest(pendingMarker);
    assert.match(validateProjectData(pendingMarker), /gate marker is invalid/);
    assert.match(validateFreezeTransition(null, pendingMarker), /gate marker is invalid/);

    const impossibleDate = project("Impossible marker date");
    impossibleDate.meta.g0 = "2026-02-30";
    assert.match(validateProjectData(impossibleDate), /gate marker is invalid/);

    const whitespaceSignature = structuredClone(draft);
    whitespaceSignature.review.signatures.pm = " ";
    assert.match(validateProjectData(whitespaceSignature), /data\.review\.signatures/);

    assert.equal(validateFreezeTransition(null, draft), null);
    assert.match(validateFreezeTransition(null, evidence), /new projects must start/);

    const staleEvidence = structuredClone(evidence);
    staleEvidence.meta.title = "Changed after the stranger test";
    assert.match(validateFreezeTransition(evidence, staleEvidence), /reviewed content changed/);
    const clearedEvidence = structuredClone(staleEvidence);
    clearedEvidence.review.dodManual = { secrets: false, runzero: false };
    clearedEvidence.review.stranger = { ran: "", defects: -1, report: "" };
    clearedEvidence.review.signatures = { pm: "", tech: "", qa: "" };
    assert.equal(validateFreezeTransition(evidence, clearedEvidence), null);

    const workflowAfterSigning = structuredClone(signed);
    workflowAfterSigning.review.techChecks.feasibility = true;
    assert.match(validateFreezeTransition(signed, workflowAfterSigning), /review evidence changed/);
    workflowAfterSigning.review.signatures = { pm: "", tech: "", qa: "" };
    assert.equal(validateFreezeTransition(signed, workflowAfterSigning), null);

    const rerunAfterSigning = structuredClone(signed);
    rerunAfterSigning.review.stranger.report = "New fixed external-test attestation.";
    assert.match(validateFreezeTransition(signed, rerunAfterSigning), /review evidence changed/);
    const firstSignature = structuredClone(evidence);
    firstSignature.review.signatures.pm = "PM";
    assert.equal(validateFreezeTransition(evidence, firstSignature), null);
    const secondSignature = structuredClone(firstSignature);
    secondSignature.review.signatures.tech = "Tech";
    assert.equal(validateFreezeTransition(firstSignature, secondSignature), null);

    const changedWhileFreezing = structuredClone(frozen);
    changedWhileFreezing.meta.title = "Changed in the freeze request";
    changedWhileFreezing.review.digest = computeFreezeDigest(changedWhileFreezing);
    assert.match(validateFreezeTransition(signed, changedWhileFreezing), /fingerprint is invalid/);

    const invalidFrozenRerun = structuredClone(frozen);
    invalidFrozenRerun.meta.g4 = "";
    invalidFrozenRerun.meta.status = "building";
    invalidFrozenRerun.review.digest = "";
    invalidFrozenRerun.review.releaseNum = 2;
    invalidFrozenRerun.review.dodManual = { secrets: false, runzero: false };
    invalidFrozenRerun.review.signatures = { pm: "", tech: "", qa: "" };
    invalidFrozenRerun.review.reconcile = {};
    invalidFrozenRerun.review.eventAudit = false;
    invalidFrozenRerun.review.mocksVerified = false;
    invalidFrozenRerun.review.stranger = {
      ran: "2026-09-05", defects: 0, report: "Attempted in the unfreeze request.",
    };
    assert.match(validateFreezeTransition(frozen, invalidFrozenRerun), /clean, incremented release/);

    const shortcutFreeze = project("Shortcut freeze");
    shortcutFreeze.meta.g4 = "2026-09-04";
    shortcutFreeze.meta.status = "frozen";
    shortcutFreeze.review.dodManual = { secrets: true, runzero: true };
    shortcutFreeze.review.stranger = { ran: "2026-09-04", defects: 0, report: "No gaps." };
    shortcutFreeze.review.signatures = { pm: "PM", tech: "Tech", qa: "QA" };
    shortcutFreeze.review.digest = computeFreezeDigest(shortcutFreeze);
    assert.match(validateFreezeTransition(null, shortcutFreeze), /new projects must start/);

    const created = await fetch(url, {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ id: "frozen-snapshot-a1", slug: "frozen-snapshot", data: draft }),
    });
    assert.equal(created.status, 201);

    const evidenceResponse = await fetch(`${url}/frozen-snapshot-a1`, {
      method: "PUT", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ version: 1, data: evidence }),
    });
    assert.deepEqual(await evidenceResponse.json(), { ok: true, version: 2 });

    const rejectedStaleEvidence = await fetch(`${url}/frozen-snapshot-a1`, {
      method: "PUT", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ version: 2, data: staleEvidence }),
    });
    assert.equal(rejectedStaleEvidence.status, 409);
    const transitionConflict = await rejectedStaleEvidence.json();
    assert.match(transitionConflict.error, /reviewed content changed/);
    assert.equal(transitionConflict.version, undefined);
    assert.equal(transitionConflict.data, undefined);

    const signedResponse = await fetch(`${url}/frozen-snapshot-a1`, {
      method: "PUT", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ version: 2, data: signed }),
    });
    assert.deepEqual(await signedResponse.json(), { ok: true, version: 3 });

    const frozenResponse = await fetch(`${url}/frozen-snapshot-a1`, {
      method: "PUT", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ version: 3, data: frozen }),
    });
    assert.deepEqual(await frozenResponse.json(), { ok: true, version: 4 });

    const incompleteReconciliation = structuredClone(frozen);
    incompleteReconciliation.meta.g6 = "2026-09-05";
    incompleteReconciliation.meta.status = "reconciled";
    assert.match(validateProjectData(incompleteReconciliation), /freeze and reconciliation state/);
    const incompleteResponse = await fetch(`${url}/frozen-snapshot-a1`, {
      method: "PUT", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ version: 4, data: incompleteReconciliation }),
    });
    assert.equal(incompleteResponse.status, 400);

    const falseReconciliation = project("Never frozen");
    falseReconciliation.meta.status = "reconciled";
    falseReconciliation.meta.g6 = "2026-09-05";
    assert.match(validateProjectData(falseReconciliation), /freeze and reconciliation state/);

    const missingMockCheck = structuredClone(frozen);
    missingMockCheck.meta.g6 = "2026-09-05";
    missingMockCheck.meta.status = "reconciled";
    missingMockCheck.review.reconcile.S1 = "pass";
    missingMockCheck.review.eventAudit = true;
    assert.match(validateProjectData(missingMockCheck), /freeze and reconciliation state/);

    const emptyDeviation = structuredClone(frozen);
    emptyDeviation.review.reconcile = { S1: "deviation", S1_note: "" };
    assert.match(validateProjectData(emptyDeviation), /data\.review\.reconcile/);

    const reconciled = structuredClone(frozen);
    reconciled.meta.g6 = "2026-09-05";
    reconciled.meta.status = "reconciled";
    reconciled.review.reconcile.S1 = "pass";
    reconciled.review.eventAudit = true;
    reconciled.review.mocksVerified = true;
    const reconciledResponse = await fetch(`${url}/frozen-snapshot-a1`, {
      method: "PUT", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ version: 4, data: reconciled }),
    });
    assert.equal(reconciledResponse.status, 200);

    const staleFingerprint = structuredClone(reconciled);
    staleFingerprint.meta.title = "Changed without a new release";
    const rejected = await fetch(`${url}/frozen-snapshot-a1`, {
      method: "PUT", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ version: 5, data: staleFingerprint }),
    });
    assert.equal(rejected.status, 409);
    assert.match((await rejected.json()).error, /clean, incremented release/);

    const revised = structuredClone(staleFingerprint);
    revised.meta.g4 = "";
    revised.meta.g6 = "";
    revised.meta.status = "building";
    revised.review.digest = "";
    revised.review.releaseNum = 2;
    revised.review.signatures = { pm: "", tech: "", qa: "" };
    revised.review.stranger = { ran: "", defects: -1, report: "" };
    revised.review.dodManual = { secrets: false, runzero: false };
    revised.review.reconcile = {};
    revised.review.eventAudit = false;
    revised.review.mocksVerified = false;
    const accepted = await fetch(`${url}/frozen-snapshot-a1`, {
      method: "PUT", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ version: 5, data: revised }),
    });
    assert.equal(accepted.status, 200);
    assert.deepEqual(await accepted.json(), { ok: true, version: 6 });
  });

  test("keeps quote and backslash payloads as prepared-statement values", async () => {
    const pool = new FakePool();
    const { origin } = await launch({ pool });
    const url = `${origin}/prd-studio/api/projects`;
    const title = "Customer's \\server\\share — not SQL";
    const data = project(title);
    const created = await fetch(url, {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ id: "prepared-payload-a1", slug: "prepared-payload", data }),
    });
    assert.equal(created.status, 201);
    const stored = pool.projects.get("prepared-payload-a1");
    assert.equal(stored.title, title);
    const insert = pool.executions.find((entry) => entry.sql.startsWith("INSERT INTO projects"));
    assert.ok(insert);
    assert.equal(insert.params[2], title);
    assert.match(insert.sql, /VALUES \(\?, \?, \?,/);

    const read = await fetch(`${url}/prepared-payload-a1`, { headers: authHeaders() });
    assert.equal((await read.json()).data.meta.title, title);
  });

  test("validates identifiers, nested data, and metadata bounds", async () => {
    const { origin } = await launch();
    const url = `${origin}/prd-studio/api/projects`;
    const malicious = project();
    malicious.spec.scenarios[0].id = '<img src=x onerror="alert(1)">';
    malicious.review.releaseNum = "<script>alert(1)</script>";
    assert.match(validateProjectData(malicious), /scenarios|releaseNum/);
    const response = await fetch(url, {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ id: "../invalid", slug: "valid", data: malicious }),
    });
    assert.equal(response.status, 400);

    const oversized = project("x".repeat(256));
    assert.match(validateProjectData(oversized), /title/);

    const rawData = project();
    rawData.meta.data = "raw";
    assert.equal(validateProjectData(rawData), "raw customer data is prohibited");
    const rawResponse = await fetch(url, {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ id: "raw-data-a1b2", slug: "raw-data", data: rawData }),
    });
    assert.equal(rawResponse.status, 400);
    assert.deepEqual(await rawResponse.json(), { error: "raw customer data is prohibited" });

    const frontend = fs.readFileSync(path.join(__dirname, "..", "public", "index.html"), "utf8");
    assert.match(frontend, /k==="raw"\?"disabled aria-disabled/);
  });

  test("write fence blocks mutations and disappears without affecting reads", async (t) => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "prd-studio-fence-"));
    const fence = path.join(directory, "write.fence");
    fs.writeFileSync(fence, "fenced\n", { mode: 0o600 });
    t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
    const appConfig = config({ WRITE_FENCE_FILE: fence });
    const { origin } = await launch({ appConfig });
    const url = `${origin}/prd-studio/api/projects`;
    const blocked = await fetch(url, {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ id: "checkout-prd-a1b2", slug: "checkout-prd", data: project() }),
    });
    assert.equal(blocked.status, 503);
    assert.equal((await fetch(url, { headers: authHeaders() })).status, 200);
    fs.unlinkSync(fence);

    const target = path.join(directory, "target");
    fs.writeFileSync(target, "target\n", { mode: 0o600 });
    fs.symlinkSync(target, fence);
    const symlinkBlocked = await fetch(url, {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ id: "checkout-prd-a1b2", slug: "checkout-prd", data: project() }),
    });
    assert.equal(symlinkBlocked.status, 503);
    fs.unlinkSync(fence);

    fs.mkdirSync(fence);
    const directoryBlocked = await fetch(url, {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ id: "checkout-prd-a1b2", slug: "checkout-prd", data: project() }),
    });
    assert.equal(directoryBlocked.status, 503);
    fs.rmdirSync(fence);

    const allowed = await fetch(url, {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ id: "checkout-prd-a1b2", slug: "checkout-prd", data: project() }),
    });
    assert.equal(allowed.status, 201);
  });

  test("returns generic errors and never logs SQL, data, or secrets", async () => {
    const pool = new FakePool();
    pool.failNext("SELECT id, title");
    const captured = captureLogger();
    const { origin } = await launch({ pool, logger: captured.logger });
    const response = await fetch(`${origin}/prd-studio/api/projects`, { headers: authHeaders() });
    assert.equal(response.status, 500);
    assert.deepEqual(await response.json(), { error: "list failed" });
    const logs = captured.lines.join("\n");
    assert.doesNotMatch(logs, /sensitive-password|private_data|SELECT \*/);
    assert.match(logs, /ER_TEST_FAILURE/);
  });

  test("catches pool connection failures during save", async () => {
    const pool = new FakePool();
    pool.getConnectionFailure = true;
    const { origin } = await launch({ pool });
    const response = await fetch(`${origin}/prd-studio/api/projects/project-a`, {
      method: "PUT", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ version: 1, data: project() }),
    });
    assert.equal(response.status, 500);
    assert.deepEqual(await response.json(), { error: "save failed" });
  });
});

describe("readiness", () => {
  test("requires the exact supported schema and fails closed on DB errors", async () => {
    const future = new FakePool({ schemaVersion: 2 });
    const futureServer = await launch({ pool: future });
    assert.equal((await fetch(`${futureServer.origin}/readyz`)).status, 503);

    const failed = new FakePool({ schemaVersion: 1 });
    failed.failNext("FROM schema_versions");
    const failedServer = await launch({ pool: failed });
    assert.equal((await fetch(`${failedServer.origin}/readyz`)).status, 503);
  });
});
