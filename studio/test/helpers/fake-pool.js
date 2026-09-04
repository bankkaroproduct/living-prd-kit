"use strict";

function sqlText(value) {
  return typeof value === "string" ? value : value.sql;
}

function databaseError(code = "ER_TEST_FAILURE") {
  const error = new Error("sensitive-password SELECT * FROM private_data");
  error.code = code;
  error.sql = "SELECT secret FROM private_data";
  return error;
}

class FakePool {
  constructor({ schemaVersion = 1 } = {}) {
    this.schemaVersion = schemaVersion;
    this.projects = new Map();
    this.failurePattern = null;
    this.getConnectionFailure = false;
    this.ended = false;
    this.executions = [];
  }

  failNext(pattern) {
    this.failurePattern = pattern;
  }

  maybeFail(sql) {
    if (this.failurePattern && sql.includes(this.failurePattern)) {
      this.failurePattern = null;
      throw databaseError();
    }
  }

  async dispatch(statement, params = []) {
    const sql = sqlText(statement);
    this.maybeFail(sql);
    if (sql.includes("FROM schema_versions")) {
      return [this.schemaVersion === null ? [] : [{ version: this.schemaVersion }], []];
    }
    if (sql.startsWith("SELECT id, title")) {
      const rows = [...this.projects.values()]
        .sort((a, b) => b.updated_at - a.updated_at)
        .map(({ id, title, owner, tier, status, created_at }) => ({ id, title, owner, tier, status, created_at }));
      return [rows, []];
    }
    if (sql.startsWith("SELECT data, row_version")) {
      const row = this.projects.get(params[0]);
      return [row ? [{ data: row.data, row_version: row.row_version }] : [], []];
    }
    if (sql.startsWith("INSERT INTO projects")) {
      const [id, slug, title, owner, tech_lead, qa, designer, tier, status, data] = params;
      if (this.projects.has(id)) throw databaseError("ER_DUP_ENTRY");
      const now = new Date();
      this.projects.set(id, { id, slug, title, owner, tech_lead, qa, designer, tier, status, data, row_version: 1, created_at: now, updated_at: now });
      return [{ affectedRows: 1 }, []];
    }
    throw databaseError("ER_UNEXPECTED_QUERY");
  }

  async query(statement, params = []) {
    if (params.length) throw databaseError("ER_CLIENT_INTERPOLATION_FORBIDDEN");
    return this.dispatch(statement, params);
  }

  async execute(statement, params = []) {
    this.executions.push({ sql: sqlText(statement), params: [...params] });
    return this.dispatch(statement, params);
  }

  async getConnection() {
    if (this.getConnectionFailure) throw databaseError("ER_CON_COUNT_ERROR");
    const pool = this;
    return {
      async beginTransaction() {},
      async rollback() {},
      async commit() {},
      release() {},
      async query(statement, params = []) {
        if (params.length) throw databaseError("ER_CLIENT_INTERPOLATION_FORBIDDEN");
        return pool.dispatch(statement, params);
      },
      async execute(statement, params = []) {
        const sql = sqlText(statement);
        pool.executions.push({ sql, params: [...params] });
        pool.maybeFail(sql);
        if (sql.startsWith("SELECT row_version")) {
          const row = pool.projects.get(params[0]);
          return [row ? [{ row_version: row.row_version, data: row.data }] : [], []];
        }
        if (sql.startsWith("UPDATE projects SET")) {
          const [title, owner, tech_lead, qa, designer, tier, status, data, id] = params;
          const row = pool.projects.get(id);
          if (!row) return [{ affectedRows: 0 }, []];
          Object.assign(row, { title, owner, tech_lead, qa, designer, tier, status, data, row_version: row.row_version + 1, updated_at: new Date() });
          return [{ affectedRows: 1 }, []];
        }
        return pool.dispatch(statement, params);
      },
    };
  }

  async end() {
    this.ended = true;
  }
}

module.exports = { FakePool, databaseError };
