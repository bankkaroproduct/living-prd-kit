"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const { once } = require("node:events");
const { test } = require("node:test");
const mysql = require("mysql2/promise");
const { createApp, createPoolFromConfig, loadConfig } = require("../server");

const enabled = process.env.MYSQL_INTEGRATION === "1";
const AUTHENTICATED = { "X-PRD-Authenticated": "1" };

function requiredEnvironment(name) {
  const value = String(process.env[name] || "");
  assert.ok(value, `${name} is required when MYSQL_INTEGRATION=1`);
  return value;
}

function project(title) {
  return {
    meta: {
      title, slug: "mysql-http-integration", owner: "CI", tech: "", qa: "", designer: "",
      tier: null, triggers: [], data: "synthetic", pipeline: "", dumpdate: "",
      entry: "", run: "", status: "framing", created: "2026-09-04", g0: "",
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

async function closeServer(server) {
  if (!server) return;
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
}

test("real MySQL serves the HTTP create/read/update/conflict flow with exact runtime grants", { skip: !enabled }, async () => {
  const database = requiredEnvironment("DB_NAME");
  const runtimeUser = requiredEnvironment("DB_USER");
  assert.match(database, /^[A-Za-z0-9_]+$/, "DB_NAME must be a simple MySQL identifier in integration CI");

  const runtimeEnvironment = {
    NODE_ENV: "test",
    AUTH_MODE: "trusted_proxy",
    TEAM_TOKEN: "",
    BASE_PATH: "/prd-studio",
    HOST: "127.0.0.1",
    PORT: "8080",
    DB_HOST: requiredEnvironment("DB_HOST"),
    DB_PORT: String(process.env.DB_PORT || "3306"),
    DB_USER: runtimeUser,
    DB_PASSWORD: requiredEnvironment("DB_PASSWORD"),
    DB_NAME: database,
    DB_SSL_MODE: "disabled",
    RELEASE_ID: "mysql-http-integration",
  };
  const cleanupPool = mysql.createPool({
    host: runtimeEnvironment.DB_HOST,
    port: Number(runtimeEnvironment.DB_PORT),
    user: requiredEnvironment("MYSQL_CLEANUP_USER"),
    password: requiredEnvironment("MYSQL_CLEANUP_PASSWORD"),
    database,
    connectionLimit: 1,
  });
  const config = loadConfig(runtimeEnvironment);
  assert.equal(config.db.user, runtimeUser);
  const pool = createPoolFromConfig(config);
  const id = `mysql-http-${crypto.randomBytes(6).toString("hex")}`;
  let server;

  try {
    const [grantRows] = await pool.query("SHOW GRANTS FOR CURRENT_USER");
    const grantPrefixes = grantRows.map((row) => String(Object.values(row)[0]).split(" TO ")[0]).sort();
    assert.deepEqual(grantPrefixes, [
      "GRANT SELECT ON `prd_studio_test`.`schema_versions`",
      "GRANT SELECT, INSERT, UPDATE ON `prd_studio_test`.`projects`",
      "GRANT USAGE ON *.*",
    ].map((value) => value.replace("prd_studio_test", database)).sort());

    const app = createApp({ pool, config });
    server = app.listen(0, "127.0.0.1");
    await once(server, "listening");
    const origin = `http://127.0.0.1:${server.address().port}`;
    const api = `${origin}/prd-studio/api/projects`;

    assert.equal((await fetch(`${origin}/prd-studio/`)).status, 401);
    const ready = await fetch(`${origin}/prd-studio/readyz`, { headers: AUTHENTICATED });
    assert.equal(ready.status, 200);
    assert.equal((await ready.json()).status, "ready");

    const original = project("Prepared customer's \\server\\share");
    const created = await fetch(api, {
      method: "POST",
      headers: { ...AUTHENTICATED, "Content-Type": "application/json" },
      body: JSON.stringify({ id, slug: "mysql-http-integration", data: original }),
    });
    assert.equal(created.status, 201);
    assert.deepEqual(await created.json(), { ok: true, version: 1 });

    const read = await fetch(`${api}/${id}`, { headers: AUTHENTICATED });
    assert.equal(read.status, 200);
    const stored = await read.json();
    assert.equal(stored.version, 1);
    assert.equal(stored.data.meta.title, original.meta.title);

    const changed = project("Updated through the real HTTP application");
    const updated = await fetch(`${api}/${id}`, {
      method: "PUT",
      headers: { ...AUTHENTICATED, "Content-Type": "application/json" },
      body: JSON.stringify({ version: 1, data: changed }),
    });
    assert.equal(updated.status, 200);
    assert.deepEqual(await updated.json(), { ok: true, version: 2 });

    const stale = await fetch(`${api}/${id}`, {
      method: "PUT",
      headers: { ...AUTHENTICATED, "Content-Type": "application/json" },
      body: JSON.stringify({ version: 1, data: original }),
    });
    assert.equal(stale.status, 409);
    const conflict = await stale.json();
    assert.equal(conflict.version, 2);
    assert.equal(conflict.data.meta.title, changed.meta.title);
  } finally {
    await closeServer(server);
    await pool.end();
    try {
      await cleanupPool.execute("DELETE FROM projects WHERE id = ?", [id]);
    } finally {
      await cleanupPool.end();
    }
  }
});
