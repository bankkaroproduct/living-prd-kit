#!/usr/bin/env node
"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { acquireConnection, constantTimeEqual, createPoolFromConfig, safeCode } = require("../server");

const SCHEMA_VERSION = 1;
const SCHEMA_NAME = "initial";
const LOCK_TIMEOUT_SECONDS = 10;

const EXPECTED_COLUMNS = {
  projects: [
    ["id", "varchar(64)", "NO", "ascii", "ascii_bin", null, ""],
    ["slug", "varchar(64)", "NO", "ascii", "ascii_bin", null, ""],
    ["title", "varchar(255)", "NO", "utf8mb4", "utf8mb4_unicode_ci", "", ""],
    ["owner", "varchar(120)", "NO", "utf8mb4", "utf8mb4_unicode_ci", "", ""],
    ["tech_lead", "varchar(120)", "NO", "utf8mb4", "utf8mb4_unicode_ci", "", ""],
    ["qa", "varchar(120)", "NO", "utf8mb4", "utf8mb4_unicode_ci", "", ""],
    ["designer", "varchar(120)", "NO", "utf8mb4", "utf8mb4_unicode_ci", "", ""],
    ["tier", "varchar(8)", "YES", "utf8mb4", "utf8mb4_unicode_ci", null, ""],
    ["status", "varchar(24)", "NO", "utf8mb4", "utf8mb4_unicode_ci", "framing", ""],
    ["data", "json", "NO", null, null, null, ""],
    ["row_version", "int unsigned", "NO", null, null, "1", ""],
    ["created_at", "datetime", "NO", null, null, "current_timestamp", "default_generated"],
    ["updated_at", "datetime", "NO", null, null, "current_timestamp", "default_generated on update current_timestamp"],
  ],
  schema_versions: [
    ["version", "int unsigned", "NO", null, null, null, ""],
    ["name", "varchar(120)", "NO", "utf8mb4", "utf8mb4_unicode_ci", null, ""],
    ["checksum", "char(64)", "NO", "ascii", "ascii_bin", null, ""],
    ["applied_at", "timestamp", "NO", null, null, "current_timestamp", "default_generated"],
  ],
};

const EXPECTED_INDEXES = [
  ["projects", "idx_projects_status", 1, 1, "status"],
  ["projects", "idx_projects_updated", 1, 1, "updated_at"],
  ["projects", "PRIMARY", 0, 1, "id"],
  ["schema_versions", "PRIMARY", 0, 1, "version"],
];

const EXPECTED_CONSTRAINTS = [
  ["projects", "chk_projects_id", "CHECK"],
  ["projects", "chk_projects_row_version", "CHECK"],
  ["projects", "chk_projects_slug", "CHECK"],
  ["projects", "chk_projects_status", "CHECK"],
  ["projects", "chk_projects_tier", "CHECK"],
  ["projects", "PRIMARY", "PRIMARY KEY"],
  ["schema_versions", "PRIMARY", "PRIMARY KEY"],
];

const EXPECTED_CHECKS = [
  ["projects", "chk_projects_id", "YES", normalizeCheckClause("REGEXP_LIKE(id, '^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$', 'c')")],
  ["projects", "chk_projects_row_version", "YES", normalizeCheckClause("row_version >= 1")],
  ["projects", "chk_projects_slug", "YES", normalizeCheckClause("REGEXP_LIKE(slug, '^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$', 'c')")],
  ["projects", "chk_projects_status", "YES", normalizeCheckClause("status IN ('framing', 'building', 'frozen', 'reconciled')")],
  ["projects", "chk_projects_tier", "YES", normalizeCheckClause("tier IS NULL OR tier IN ('T1', 'T2', 'T3')")],
];

function migrationConfig(env) {
  const port = Number(env.MIGRATION_DB_PORT || 3306);
  if (!Number.isSafeInteger(port) || port < 1 || port > 65535) {
    const error = new Error("MIGRATION_DB_PORT is invalid");
    error.code = "CONFIG_ERROR";
    throw error;
  }
  return {
    db: {
      host: String(env.MIGRATION_DB_HOST || "127.0.0.1").trim(),
      port,
      socketPath: String(env.MIGRATION_DB_SOCKET_PATH || "").trim(),
      user: String(env.MIGRATION_DB_USER || "").trim(),
      password: String(env.MIGRATION_DB_PASSWORD || ""),
      passwordFile: String(env.MIGRATION_DB_PASSWORD_FILE || "").trim(),
      database: String(env.MIGRATION_DB_NAME || "").trim(),
      sslMode: String(env.MIGRATION_DB_SSL_MODE || "disabled").trim(),
      sslCaFile: String(env.MIGRATION_DB_SSL_CA_FILE || "").trim(),
    },
  };
}

function log(level, event, code) {
  const record = { level, event };
  if (code) record.code = safeCode({ code });
  const line = JSON.stringify(record);
  if (level === "error") console.error(line);
  else console.log(line);
}

function splitStatements(sql) {
  const withoutComments = sql.replace(/^\s*--.*$/gm, "");
  return withoutComments.split(/;\s*(?:\r?\n|$)/).map((statement) => statement.trim()).filter(Boolean);
}

function assertEqual(actual, expected, code) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    const error = new Error("schema does not match the certified definition");
    error.code = code;
    throw error;
  }
}

function normalizeColumnDefault(value) {
  if (value === null || value === undefined) return null;
  const normalized = String(value).trim().toLowerCase();
  return normalized === "current_timestamp()" ? "current_timestamp" : normalized;
}

function normalizeExtra(value) {
  return String(value || "").trim().toLowerCase().replace(/current_timestamp\(\)/g, "current_timestamp").replace(/\s+/g, " ");
}

function normalizeCheckClause(value) {
  const input = String(value || "").trim().replace(/`/g, "").replace(/_[a-z0-9]+(?=\s*')/gi, "");
  const tokens = [];
  let index = 0;

  function mismatch() {
    const error = new Error("invalid check-clause representation");
    error.code = "SCHEMA_CHECK_MISMATCH";
    throw error;
  }

  while (index < input.length) {
    const character = input[index];
    if (/\s/.test(character)) {
      index += 1;
      continue;
    }
    if (character === "'") {
      index += 1;
      let literal = "";
      let closed = false;
      while (index < input.length) {
        if (input[index] === "'" && input[index + 1] === "'") {
          literal += "'";
          index += 2;
        } else if (input[index] === "'") {
          index += 1;
          closed = true;
          break;
        } else {
          literal += input[index];
          index += 1;
        }
      }
      if (!closed) mismatch();
      tokens.push({ type: "literal", value: literal });
      continue;
    }
    const word = /^[A-Za-z_][A-Za-z0-9_$]*/.exec(input.slice(index));
    if (word) {
      tokens.push({ type: "word", value: word[0].toLowerCase() });
      index += word[0].length;
      continue;
    }
    const number = /^\d+(?:\.\d+)?/.exec(input.slice(index));
    if (number) {
      tokens.push({ type: "number", value: number[0] });
      index += number[0].length;
      continue;
    }
    const operator = /^(?:>=|<=|<>|!=|=|>|<)/.exec(input.slice(index));
    if (operator) {
      tokens.push({ type: "operator", value: operator[0] });
      index += operator[0].length;
      continue;
    }
    if (character === "(" || character === ")" || character === ",") {
      tokens.push({ type: character, value: character });
      index += 1;
      continue;
    }
    mismatch();
  }

  let position = 0;
  const peek = () => tokens[position];
  const take = (type, value) => {
    const token = tokens[position];
    if (!token || token.type !== type || (value !== undefined && token.value !== value)) mismatch();
    position += 1;
    return token;
  };
  const isWord = (value) => peek() && peek().type === "word" && peek().value === value;

  function parsePrimary() {
    const token = peek();
    if (!token) mismatch();
    if (token.type === "(") {
      take("(");
      const expression = parseOr();
      take(")");
      return expression;
    }
    if (token.type === "literal" || token.type === "number") {
      position += 1;
      return [token.type, token.value];
    }
    if (token.type !== "word") mismatch();
    position += 1;
    const name = token.value;
    if (!peek() || peek().type !== "(") return ["identifier", name];
    take("(");
    const args = [];
    if (!peek() || peek().type !== ")") {
      args.push(parseOr());
      while (peek() && peek().type === ",") {
        take(",");
        args.push(parseOr());
      }
    }
    take(")");
    return ["call", name, args];
  }

  function parsePredicate() {
    const left = parsePrimary();
    if (isWord("is")) {
      take("word", "is");
      const negated = isWord("not");
      if (negated) take("word", "not");
      take("word", "null");
      return [negated ? "is-not-null" : "is-null", left];
    }
    if (isWord("in")) {
      take("word", "in");
      take("(");
      const values = [parseOr()];
      while (peek() && peek().type === ",") {
        take(",");
        values.push(parseOr());
      }
      take(")");
      return ["in", left, values];
    }
    if (peek() && peek().type === "operator") {
      const operator = take("operator").value;
      return ["compare", operator, left, parsePrimary()];
    }
    return left;
  }

  function parseAnd() {
    let expression = parsePredicate();
    while (isWord("and")) {
      take("word", "and");
      expression = ["and", expression, parsePredicate()];
    }
    return expression;
  }

  function parseOr() {
    let expression = parseAnd();
    while (isWord("or")) {
      take("word", "or");
      expression = ["or", expression, parseAnd()];
    }
    return expression;
  }

  const expression = parseOr();
  if (position !== tokens.length) mismatch();
  return JSON.stringify(expression);
}

async function listTables(connection) {
  const [rows] = await connection.query(
    `SELECT TABLE_NAME, ENGINE, TABLE_COLLATION
       FROM information_schema.TABLES
      WHERE TABLE_SCHEMA = DATABASE()
      ORDER BY TABLE_NAME`
  );
  return rows.map((row) => [row.TABLE_NAME, row.ENGINE, row.TABLE_COLLATION]);
}

async function verifySchema(connection) {
  assertEqual(await listTables(connection), [
    ["projects", "InnoDB", "utf8mb4_unicode_ci"],
    ["schema_versions", "InnoDB", "utf8mb4_unicode_ci"],
  ], "SCHEMA_TABLE_MISMATCH");

  for (const [tableName, expected] of Object.entries(EXPECTED_COLUMNS)) {
    const [rows] = await connection.execute(
      `SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, CHARACTER_SET_NAME, COLLATION_NAME, COLUMN_DEFAULT, EXTRA
         FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ?
        ORDER BY ORDINAL_POSITION`,
      [tableName]
    );
    const actual = rows.map((row) => [
      row.COLUMN_NAME,
      row.COLUMN_TYPE,
      row.IS_NULLABLE,
      row.CHARACTER_SET_NAME,
      row.COLLATION_NAME,
      normalizeColumnDefault(row.COLUMN_DEFAULT),
      normalizeExtra(row.EXTRA),
    ]);
    assertEqual(actual, expected, "SCHEMA_COLUMN_MISMATCH");
  }

  const [indexRows] = await connection.query(
    `SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME
       FROM information_schema.STATISTICS
      WHERE TABLE_SCHEMA = DATABASE()
      ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX`
  );
  assertEqual(indexRows.map((row) => [row.TABLE_NAME, row.INDEX_NAME, Number(row.NON_UNIQUE), Number(row.SEQ_IN_INDEX), row.COLUMN_NAME]),
    EXPECTED_INDEXES, "SCHEMA_INDEX_MISMATCH");

  const [constraintRows] = await connection.query(
    `SELECT TABLE_NAME, CONSTRAINT_NAME, CONSTRAINT_TYPE
       FROM information_schema.TABLE_CONSTRAINTS
      WHERE TABLE_SCHEMA = DATABASE()
      ORDER BY TABLE_NAME, CONSTRAINT_NAME`
  );
  assertEqual(constraintRows.map((row) => [row.TABLE_NAME, row.CONSTRAINT_NAME, row.CONSTRAINT_TYPE]),
    EXPECTED_CONSTRAINTS, "SCHEMA_CONSTRAINT_MISMATCH");

  const [checkRows] = await connection.query(
    `SELECT tc.TABLE_NAME, tc.CONSTRAINT_NAME, tc.ENFORCED, cc.CHECK_CLAUSE
       FROM information_schema.TABLE_CONSTRAINTS AS tc
       JOIN information_schema.CHECK_CONSTRAINTS AS cc
         ON cc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
        AND cc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
      WHERE tc.TABLE_SCHEMA = DATABASE() AND tc.CONSTRAINT_TYPE = 'CHECK'
      ORDER BY tc.TABLE_NAME, tc.CONSTRAINT_NAME`
  );
  assertEqual(checkRows.map((row) => [row.TABLE_NAME, row.CONSTRAINT_NAME, row.ENFORCED, normalizeCheckClause(row.CHECK_CLAUSE)]),
    EXPECTED_CHECKS, "SCHEMA_CHECK_MISMATCH");
}

function assertSupportedMysql(version) {
  const match = /^(\d+)\.(\d+)\.(\d+)/.exec(String(version || ""));
  const tuple = match ? match.slice(1).map(Number) : [];
  const supported = tuple.length === 3 && (tuple[0] > 8 || (tuple[0] === 8 && (tuple[1] > 0 || tuple[2] >= 16)));
  if (!supported || /mariadb/i.test(String(version || ""))) {
    const error = new Error("MySQL 8.0.16 or newer is required");
    error.code = "UNSUPPORTED_DATABASE";
    throw error;
  }
}

async function applySchema({ pool, schemaSql }) {
  const checksum = crypto.createHash("sha256").update(schemaSql, "utf8").digest("hex");
  const connection = await acquireConnection(pool, 5000);
  let locked = false;
  try {
    const [versionRows] = await connection.query({ sql: "SELECT VERSION() AS version", timeout: 2000 });
    assertSupportedMysql(versionRows[0] && versionRows[0].version);

    const [lockRows] = await connection.execute(
      { sql: "SELECT GET_LOCK(?, ?) AS acquired", timeout: (LOCK_TIMEOUT_SECONDS + 2) * 1000 },
      ["prd_studio_schema", LOCK_TIMEOUT_SECONDS]
    );
    if (!lockRows[0] || Number(lockRows[0].acquired) !== 1) {
      const error = new Error("schema lock unavailable");
      error.code = "SCHEMA_LOCK_UNAVAILABLE";
      throw error;
    }
    locked = true;

    const existingTables = await listTables(connection);
    if (existingTables.length) {
      await verifySchema(connection);
      const [versions] = await connection.query("SELECT version, name, checksum FROM schema_versions ORDER BY version");
      if (versions.length !== 1 || Number(versions[0].version) !== SCHEMA_VERSION || versions[0].name !== SCHEMA_NAME ||
          !constantTimeEqual(versions[0].checksum, checksum)) {
        const error = new Error("existing database is not the certified schema");
        error.code = "SCHEMA_VERSION_MISMATCH";
        throw error;
      }
      return { applied: false, version: SCHEMA_VERSION };
    }

    for (const statement of splitStatements(schemaSql)) {
      await connection.query({ sql: statement, timeout: 10_000 });
    }
    await verifySchema(connection);
    await connection.execute(
      "INSERT INTO schema_versions (version, name, checksum) VALUES (?, ?, ?)",
      [SCHEMA_VERSION, SCHEMA_NAME, checksum]
    );
    return { applied: true, version: SCHEMA_VERSION };
  } finally {
    if (locked) {
      try { await connection.execute("SELECT RELEASE_LOCK(?)", ["prd_studio_schema"]); } catch (_) { /* retain the first outcome */ }
    }
    connection.release();
  }
}

async function main() {
  require("dotenv").config({ quiet: true });
  let pool;
  try {
    pool = createPoolFromConfig(migrationConfig(process.env));
    const schemaSql = fs.readFileSync(path.join(__dirname, "..", "schema.sql"), "utf8");
    const result = await applySchema({ pool, schemaSql });
    log("info", result.applied ? "schema_applied" : "schema_already_current");
  } catch (error) {
    log("error", "schema_apply_failed", safeCode(error));
    process.exitCode = 1;
  } finally {
    if (pool) {
      try { await pool.end(); } catch (_) { process.exitCode = 1; }
    }
  }
}

module.exports = {
  EXPECTED_CHECKS,
  EXPECTED_COLUMNS,
  EXPECTED_CONSTRAINTS,
  EXPECTED_INDEXES,
  applySchema,
  assertSupportedMysql,
  migrationConfig,
  normalizeCheckClause,
  normalizeColumnDefault,
  normalizeExtra,
  splitStatements,
  verifySchema,
};

if (require.main === module) main();
