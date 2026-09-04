"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { describe, test } = require("node:test");
const {
  EXPECTED_CHECKS,
  EXPECTED_COLUMNS,
  EXPECTED_CONSTRAINTS,
  EXPECTED_INDEXES,
  applySchema,
  assertSupportedMysql,
  normalizeCheckClause,
  verifySchema,
} = require("../scripts/apply-schema");

const RAW_CHECKS = {
  chk_projects_id: "((regexp_like(`id`,_utf8mb4'^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$',_utf8mb4'c')))",
  chk_projects_row_version: "((`row_version` >= 1))",
  chk_projects_slug: "regexp_like(`slug`, _utf8mb4'^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$', _utf8mb4'c')",
  chk_projects_status: "(`status` in (_utf8mb4'framing',_utf8mb4'building',_utf8mb4'frozen',_utf8mb4'reconciled'))",
  chk_projects_tier: "((`tier` is null) or (`tier` in (_utf8mb4'T1',_utf8mb4'T2',_utf8mb4'T3')))",
};

function metadataConnection(drift = {}) {
  const connection = {
    async dispatch(statement, params = []) {
      const sql = typeof statement === "string" ? statement : statement.sql;
      if (sql.includes("information_schema.TABLES") && !sql.includes("TABLE_CONSTRAINTS")) {
        return [[
          { TABLE_NAME: "projects", ENGINE: "InnoDB", TABLE_COLLATION: "utf8mb4_unicode_ci" },
          { TABLE_NAME: "schema_versions", ENGINE: "InnoDB", TABLE_COLLATION: "utf8mb4_unicode_ci" },
        ], []];
      }
      if (sql.includes("information_schema.COLUMNS")) {
        const table = params[0];
        const rows = EXPECTED_COLUMNS[table].map((column) => ({
          COLUMN_NAME: column[0],
          COLUMN_TYPE: column[1],
          IS_NULLABLE: column[2],
          CHARACTER_SET_NAME: column[3],
          COLLATION_NAME: column[4],
          COLUMN_DEFAULT: column[5] === "current_timestamp" ? "CURRENT_TIMESTAMP()" : column[5],
          EXTRA: column[6].toUpperCase().replace(/CURRENT_TIMESTAMP/g, "CURRENT_TIMESTAMP()"),
        }));
        if (drift.columnDefault && table === "projects") rows.find((row) => row.COLUMN_NAME === "title").COLUMN_DEFAULT = "drift";
        if (drift.extra && table === "projects") rows.find((row) => row.COLUMN_NAME === "updated_at").EXTRA = "DEFAULT_GENERATED";
        return [rows, []];
      }
      if (sql.includes("information_schema.STATISTICS")) {
        return [EXPECTED_INDEXES.map((index) => ({
          TABLE_NAME: index[0], INDEX_NAME: index[1], NON_UNIQUE: index[2], SEQ_IN_INDEX: index[3], COLUMN_NAME: index[4],
        })), []];
      }
      if (sql.includes("JOIN information_schema.CHECK_CONSTRAINTS")) {
        const rows = EXPECTED_CHECKS.map((check) => ({
          TABLE_NAME: check[0],
          CONSTRAINT_NAME: check[1],
          ENFORCED: check[2],
          CHECK_CLAUSE: RAW_CHECKS[check[1]],
        }));
        if (drift.checkClause) rows.find((row) => row.CONSTRAINT_NAME === "chk_projects_row_version").CHECK_CLAUSE = "row_version >= 0";
        if (drift.enforced) rows.find((row) => row.CONSTRAINT_NAME === "chk_projects_status").ENFORCED = "NO";
        return [rows, []];
      }
      if (sql.includes("information_schema.TABLE_CONSTRAINTS")) {
        return [EXPECTED_CONSTRAINTS.map((constraint) => ({
          TABLE_NAME: constraint[0], CONSTRAINT_NAME: constraint[1], CONSTRAINT_TYPE: constraint[2],
        })), []];
      }
      throw new Error(`unexpected schema verification query: ${sql}`);
    },
    async query(statement, params = []) {
      if (params.length) throw new Error("parameterized metadata reads must use execute()");
      return this.dispatch(statement, params);
    },
    async execute(statement, params = []) {
      return this.dispatch(statement, params);
    },
  };
  return connection;
}

describe("schema verifier", () => {
  test("accepts normalized MySQL 8.0/8.4 metadata representations", async () => {
    await verifySchema(metadataConnection());
    assert.equal(normalizeCheckClause(RAW_CHECKS.chk_projects_tier),
      normalizeCheckClause("tier IS NULL OR tier IN ('T1', 'T2', 'T3')"));
  });

  test("preserves semantically significant boolean grouping", () => {
    assert.notEqual(normalizeCheckClause("(a OR b) AND c"), normalizeCheckClause("a OR (b AND c)"));
  });

  test("uses prepared statements for every migration value", async () => {
    const schemaSql = fs.readFileSync(path.join(__dirname, "..", "schema.sql"), "utf8");
    const checksum = crypto.createHash("sha256").update(schemaSql, "utf8").digest("hex");
    const connection = metadataConnection();
    const baseDispatch = connection.dispatch.bind(connection);
    connection.dispatch = async (statement, params = []) => {
      const sql = typeof statement === "string" ? statement : statement.sql;
      if (sql.startsWith("SELECT VERSION()")) return [[{ version: "8.4.6" }], []];
      if (sql.startsWith("SELECT GET_LOCK")) return [[{ acquired: 1 }], []];
      if (sql.startsWith("SELECT version, name, checksum")) {
        return [[{ version: 1, name: "initial", checksum }], []];
      }
      if (sql.startsWith("SELECT RELEASE_LOCK")) return [[{ released: 1 }], []];
      return baseDispatch(statement, params);
    };
    const executions = [];
    const baseExecute = connection.execute.bind(connection);
    connection.execute = async (statement, params = []) => {
      executions.push({ sql: typeof statement === "string" ? statement : statement.sql, params });
      return baseExecute(statement, params);
    };
    connection.release = () => {};
    const pool = { getConnection: async () => connection };

    assert.deepEqual(await applySchema({ pool, schemaSql }), { applied: false, version: 1 });
    assert.ok(executions.some((entry) => entry.sql.startsWith("SELECT GET_LOCK") && entry.params.length === 2));
    assert.ok(executions.some((entry) => entry.sql.includes("TABLE_NAME = ?") && entry.params.length === 1));
    assert.ok(executions.some((entry) => entry.sql.startsWith("SELECT RELEASE_LOCK") && entry.params.length === 1));
  });

  for (const [name, drift, code] of [
    ["column default drift", { columnDefault: true }, "SCHEMA_COLUMN_MISMATCH"],
    ["column extra drift", { extra: true }, "SCHEMA_COLUMN_MISMATCH"],
    ["check body drift", { checkClause: true }, "SCHEMA_CHECK_MISMATCH"],
    ["unenforced check drift", { enforced: true }, "SCHEMA_CHECK_MISMATCH"],
  ]) {
    test(`rejects ${name}`, async () => {
      await assert.rejects(verifySchema(metadataConnection(drift)), (error) => error.code === code);
    });
  }

  test("accepts supported MySQL versions and rejects pre-check or MariaDB versions", () => {
    assert.doesNotThrow(() => assertSupportedMysql("8.0.16"));
    assert.doesNotThrow(() => assertSupportedMysql("8.4.6"));
    assert.throws(() => assertSupportedMysql("8.0.15"), (error) => error.code === "UNSUPPORTED_DATABASE");
    assert.throws(() => assertSupportedMysql("10.11.0-MariaDB"), (error) => error.code === "UNSUPPORTED_DATABASE");
  });
});
