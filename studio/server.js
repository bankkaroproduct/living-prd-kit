// PRD Studio — self-hosted HTTP server and MySQL API.
"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");
const { isDeepStrictEqual } = require("node:util");
const { domainToASCII } = require("node:url");
const express = require("express");
const mysql = require("mysql2/promise");

const CURRENT_SCHEMA_VERSION = 1;
const MAX_PROJECT_BYTES = 1024 * 1024;
const PROJECT_ID_RE = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;
const SCENARIO_ID_RE = /^S[1-9][0-9]{0,5}$/;
const TIERS = new Set(["T1", "T2", "T3"]);
const STATUSES = new Set(["framing", "building", "frozen", "reconciled"]);
const TRIGGERS = new Set(["payments-kyc-pii", "regulated-data", "complex-state-machine", "irreversible-actions", "cross-team"]);
const OBJECTION_TYPES = new Set(["cant-build", "not-safe", "cant-test"]);
const FIDELITY_LABELS = new Set(["REAL", "MOCKED", "STATIC"]);

function configError(message) {
  const error = new Error(message);
  error.code = "CONFIG_ERROR";
  return error;
}

function normalizeBasePath(value) {
  const raw = String(value || "").trim();
  if (!raw || raw === "/") return "";
  if (!raw.startsWith("/") || raw.endsWith("/") || raw.includes("?") || raw.includes("#") || raw.includes("//")) {
    throw configError("BASE_PATH must be empty or a single absolute URL path without a trailing slash");
  }
  if (!/^\/[A-Za-z0-9._~/-]+$/.test(raw) || raw.includes("/../") || raw.endsWith("/..")) {
    throw configError("BASE_PATH contains unsupported characters or traversal segments");
  }
  return raw;
}

function positiveInteger(value, fallback, name, maximum) {
  const parsed = value === undefined || value === "" ? fallback : Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > maximum) {
    throw configError(`${name} must be an integer between 1 and ${maximum}`);
  }
  return parsed;
}

function isLoopbackHost(host) {
  return host === "127.0.0.1" || host === "::1" || host === "localhost";
}

function isDnsHostname(host) {
  const ascii = domainToASCII(String(host || ""));
  if (!ascii || ascii.length > 253 || ascii.endsWith(".") || !/[a-z]/i.test(ascii)) return false;
  return ascii.split(".").every((label) => label.length >= 1 && label.length <= 63 && /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/i.test(label));
}

function loadConfig(env = process.env) {
  const authMode = String(env.AUTH_MODE || "token").trim();
  if (authMode !== "token" && authMode !== "trusted_proxy") {
    throw configError("AUTH_MODE must be token or trusted_proxy");
  }

  const teamToken = String(env.TEAM_TOKEN || "");
  if (authMode === "token" && teamToken.length < 32) {
    throw configError("TEAM_TOKEN must contain at least 32 characters when AUTH_MODE=token");
  }

  const socketPath = String(env.SOCKET_PATH || "").trim();
  if (socketPath && (!path.isAbsolute(socketPath) || Buffer.byteLength(socketPath) > 100)) {
    throw configError("SOCKET_PATH must be an absolute Unix socket path of at most 100 bytes");
  }

  const dbSslMode = String(env.DB_SSL_MODE || "disabled").trim();
  if (!["disabled", "required", "verify_identity"].includes(dbSslMode)) {
    throw configError("DB_SSL_MODE must be disabled, required, or verify_identity");
  }
  if (dbSslMode === "verify_identity" && !String(env.DB_SSL_CA_FILE || "").trim()) {
    throw configError("DB_SSL_CA_FILE is required when DB_SSL_MODE=verify_identity");
  }

  const host = String(env.HOST || "127.0.0.1").trim();
  if (!socketPath && !host) throw configError("HOST is required when SOCKET_PATH is not set");
  if (authMode === "trusted_proxy" && !socketPath && !isLoopbackHost(host)) {
    throw configError("AUTH_MODE=trusted_proxy requires SOCKET_PATH or a loopback HOST");
  }
  const releaseId = String(env.RELEASE_ID || "development").trim();
  if (!/^[A-Za-z0-9._:@/+\-]{1,128}$/.test(releaseId)) {
    throw configError("RELEASE_ID must be a non-secret build identifier of at most 128 characters");
  }
  const releaseCommit = String(env.RELEASE_COMMIT || "").trim();
  const releaseTree = String(env.RELEASE_TREE || "").trim();
  const releaseArtifactSha256 = String(env.RELEASE_ARTIFACT_SHA256 || "").trim();
  const identityValues = [
    ["RELEASE_COMMIT", releaseCommit, /^[0-9a-f]{40}$/],
    ["RELEASE_TREE", releaseTree, /^[0-9a-f]{40}$/],
    ["RELEASE_ARTIFACT_SHA256", releaseArtifactSha256, /^[0-9a-f]{64}$/],
  ];
  for (const [name, value, pattern] of identityValues) {
    if (value && !pattern.test(value)) throw configError(`${name} has an invalid digest format`);
    if (env.NODE_ENV === "production" && !value) throw configError(`${name} is required in production`);
  }
  const writeFenceFile = String(env.WRITE_FENCE_FILE || "").trim();
  if (writeFenceFile && !path.isAbsolute(writeFenceFile)) {
    throw configError("WRITE_FENCE_FILE must be an absolute path");
  }

  const config = {
    authMode,
    teamToken,
    basePath: normalizeBasePath(env.BASE_PATH),
    socketPath,
    host,
    port: positiveInteger(env.PORT, 8080, "PORT", 65535),
    releaseId,
    releaseIdentity: {
      commit: releaseCommit,
      tree: releaseTree,
      artifactSha256: releaseArtifactSha256,
    },
    writeFenceFile,
    db: {
      host: String(env.DB_HOST || "127.0.0.1").trim(),
      socketPath: String(env.DB_SOCKET_PATH || "").trim(),
      port: positiveInteger(env.DB_PORT, 3306, "DB_PORT", 65535),
      user: String(env.DB_USER || "").trim(),
      password: String(env.DB_PASSWORD || ""),
      passwordFile: String(env.DB_PASSWORD_FILE || "").trim(),
      database: String(env.DB_NAME || "prd_studio").trim(),
      sslMode: dbSslMode,
      sslCaFile: String(env.DB_SSL_CA_FILE || "").trim(),
    },
  };
  validateDatabaseConfig(config);
  return config;
}

function validateDatabaseConfig(config) {
  if (config.db.socketPath && !path.isAbsolute(config.db.socketPath)) {
    throw configError("DB_SOCKET_PATH must be absolute");
  }
  if (config.db.passwordFile && !path.isAbsolute(config.db.passwordFile)) {
    throw configError("DB_PASSWORD_FILE must be absolute");
  }
  if (!["disabled", "required", "verify_identity"].includes(config.db.sslMode)) {
    throw configError("DB_SSL_MODE is invalid");
  }
  if (config.db.sslMode === "verify_identity" && !config.db.sslCaFile) {
    throw configError("DB_SSL_CA_FILE is required for verified database TLS");
  }
  if (config.db.sslMode === "verify_identity" && !path.isAbsolute(config.db.sslCaFile)) {
    throw configError("DB_SSL_CA_FILE must be absolute for verified database TLS");
  }
  if (!config.db.socketPath && config.db.sslMode === "verify_identity" &&
      (net.isIP(config.db.host) !== 0 || !isDnsHostname(config.db.host))) {
    throw configError("DB_SSL_MODE=verify_identity requires a DNS DB_HOST; IP literals are not supported");
  }
  if (!config.db.socketPath && !isLoopbackHost(config.db.host) && config.db.sslMode !== "verify_identity") {
    throw configError("a remote database requires DB_SSL_MODE=verify_identity");
  }
  if (!config.db.host || !config.db.user || (!config.db.password && !config.db.passwordFile) || !config.db.database) {
    throw configError("DB_HOST, DB_USER, DB_NAME, and one of DB_PASSWORD or DB_PASSWORD_FILE must be set");
  }
}

function buildPoolOptions(config) {
  validateDatabaseConfig(config);
  let password = config.db.password;
  if (config.db.passwordFile) {
    password = fs.readFileSync(config.db.passwordFile, "utf8").replace(/\r?\n$/, "");
    if (!password || /[\r\n]/.test(password)) throw configError("DB_PASSWORD_FILE must contain one non-empty line");
  }
  let ssl;
  if (config.db.sslMode === "required") ssl = { rejectUnauthorized: false };
  if (config.db.sslMode === "verify_identity") {
    ssl = { ca: fs.readFileSync(config.db.sslCaFile), rejectUnauthorized: true, verifyIdentity: true };
  }
  return {
    host: config.db.socketPath ? undefined : config.db.host,
    socketPath: config.db.socketPath || undefined,
    port: config.db.port,
    user: config.db.user,
    password,
    database: config.db.database,
    ssl,
    waitForConnections: true,
    connectionLimit: 10,
    queueLimit: 20,
    enableKeepAlive: true,
    keepAliveInitialDelay: 0,
    connectTimeout: 5000,
    decimalNumbers: true,
  };
}

function createPoolFromConfig(config) {
  return mysql.createPool(buildPoolOptions(config));
}

function safeCode(error) {
  const code = error && typeof error.code === "string" ? error.code : "UNEXPECTED";
  return /^[A-Z0-9_]{1,64}$/.test(code) ? code : "UNEXPECTED";
}

function createLogger(output = console) {
  function write(method, level, event, fields = {}) {
    const record = { level, event };
    if (fields.code) record.code = safeCode({ code: fields.code });
    if (fields.signal && /^(SIGTERM|SIGINT)$/.test(fields.signal)) record.signal = fields.signal;
    output[method](JSON.stringify(record));
  }
  return {
    info(event, fields) { write("log", "info", event, fields); },
    error(event, fields) { write("error", "error", event, fields); },
  };
}

function constantTimeEqual(candidate, expected) {
  const left = crypto.createHash("sha256").update(String(candidate), "utf8").digest();
  const right = crypto.createHash("sha256").update(String(expected), "utf8").digest();
  return crypto.timingSafeEqual(left, right);
}

function authentication(config) {
  const failedAttempts = new Map();
  const failureWindowMs = 60_000;
  const failureLimit = 10;

  return function requireAuthentication(req, res, next) {
    let authenticated = false;
    if (config.authMode === "trusted_proxy") {
      authenticated = req.get("x-prd-authenticated") === "1";
    } else {
      const key = String(req.socket.remoteAddress || "unknown");
      const now = Date.now();
      let state = failedAttempts.get(key);
      if (state && state.lockedUntil > now) {
        res.set({ "Cache-Control": "no-store", "Retry-After": String(Math.ceil((state.lockedUntil - now) / 1000)) });
        return res.status(429).json({ error: "too many authentication failures; retry later" });
      }
      if (state && now - state.startedAt >= failureWindowMs) {
        failedAttempts.delete(key);
        state = undefined;
      }
      const header = req.get("authorization") || "";
      const match = /^Bearer ([^\s]+)$/i.exec(header);
      authenticated = Boolean(match && constantTimeEqual(match[1], config.teamToken));
      if (authenticated) {
        failedAttempts.delete(key);
      } else {
        const nextState = state || { startedAt: now, failures: 0, lockedUntil: 0 };
        nextState.failures += 1;
        if (nextState.failures >= failureLimit) nextState.lockedUntil = now + failureWindowMs;
        failedAttempts.set(key, nextState);
        if (failedAttempts.size > 1000) {
          for (const [storedKey, stored] of failedAttempts) {
            if (now - stored.startedAt >= failureWindowMs && stored.lockedUntil <= now) failedAttempts.delete(storedKey);
          }
          while (failedAttempts.size > 1000) failedAttempts.delete(failedAttempts.keys().next().value);
        }
        if (nextState.lockedUntil > now) {
          res.set({ "Cache-Control": "no-store", "Retry-After": "60" });
          return res.status(429).json({ error: "too many authentication failures; retry later" });
        }
      }
    }
    if (!authenticated) {
      res.set("Cache-Control", "no-store");
      return res.status(401).json({ error: "authentication required" });
    }
    return next();
  };
}

function isPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function validBoundedString(value, maximum, allowEmpty = true) {
  return typeof value === "string" && Buffer.byteLength(value, "utf8") <= maximum && (allowEmpty || value.trim().length > 0);
}

function completedGateMarker(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00.000Z`);
  return Number.isFinite(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === value;
}

function validGateMarker(value) {
  return value === "" || completedGateMarker(value);
}

function validateJsonTree(root) {
  let nodes = 0;
  function walk(value, depth) {
    nodes += 1;
    if (nodes > 20_000) return false;
    if (depth > 12) return false;
    if (value === null || typeof value === "boolean") return true;
    if (typeof value === "number") return Number.isFinite(value);
    if (typeof value === "string") return Buffer.byteLength(value, "utf8") <= 100_000;
    if (Array.isArray(value)) return value.length <= 250 && value.every((entry) => walk(entry, depth + 1));
    if (!isPlainObject(value)) return false;
    const keys = Object.keys(value);
    if (keys.length > 100) return false;
    return keys.every((key) =>
      key.length <= 80 && key !== "__proto__" && key !== "prototype" && key !== "constructor" && walk(value[key], depth + 1)
    );
  }
  return walk(root, 0);
}

function boundedStrings(object, fields) {
  for (const [name, maximum, allowEmpty = true] of fields) {
    if (!validBoundedString(object[name], maximum, allowEmpty)) return name;
  }
  return null;
}

function booleanFields(object, names) {
  return names.every((name) => typeof object[name] === "boolean");
}

function validateProjectData(data) {
  if (!isPlainObject(data) || !isPlainObject(data.meta)) return "data.meta must be an object";
  let encoded;
  try {
    encoded = JSON.stringify(data);
  } catch (_) {
    return "data must be valid JSON";
  }
  if (Buffer.byteLength(encoded, "utf8") > MAX_PROJECT_BYTES) return "data exceeds the 1 MiB limit";
  if (!validateJsonTree(data)) return "data exceeds structural limits";

  const meta = data.meta;
  const fields = [
    ["title", 255, true],
    ["owner", 120, true],
    ["tech", 120, true],
    ["qa", 120, true],
    ["designer", 120, true],
  ];
  for (const [name, maximum, allowEmpty] of fields) {
    if (!validBoundedString(meta[name], maximum, allowEmpty)) return `data.meta.${name} is invalid`;
  }
  if (meta.slug !== undefined && (!validBoundedString(meta.slug, 64, false) || !PROJECT_ID_RE.test(meta.slug))) {
    return "data.meta.slug is invalid";
  }
  if (meta.tier !== null && meta.tier !== undefined && !TIERS.has(meta.tier)) return "data.meta.tier is invalid";
  if (!STATUSES.has(meta.status)) return "data.meta.status is invalid";

  const metaTextError = boundedStrings(meta, [
    ["data", 32], ["pipeline", 255], ["dumpdate", 32], ["entry", 2048], ["run", 2048],
    ["created", 64], ["g0", 64], ["g1", 64], ["g2", 64], ["g3", 64], ["g4", 64], ["g6", 64],
  ]);
  if (metaTextError) return `data.meta.${metaTextError} is invalid`;
  if (![meta.g0, meta.g1, meta.g2, meta.g3, meta.g4, meta.g6].every(validGateMarker)) {
    return "data.meta gate marker is invalid";
  }
  if (meta.data === "raw") return "raw customer data is prohibited";
  if (!["synthetic", "staging-dump"].includes(meta.data)) return "data.meta.data is invalid";
  if (!Array.isArray(meta.triggers) || meta.triggers.length > TRIGGERS.size || !meta.triggers.every((value) => TRIGGERS.has(value))) {
    return "data.meta.triggers is invalid";
  }

  if (!isPlainObject(data.spec) || !isPlainObject(data.review)) return "data.spec and data.review must be objects";
  const spec = data.spec;
  for (const [name, maximum] of [["screens", 100], ["edges", 200], ["events", 200], ["fidelity", 100], ["scenarios", 200]]) {
    if (!Array.isArray(spec[name]) || spec[name].length > maximum) return `data.spec.${name} is invalid`;
  }

  for (const screen of spec.screens) {
    if (!isPlainObject(screen) || boundedStrings(screen, [["name", 255], ["purpose", 2000]]) || !Array.isArray(screen.fields) || screen.fields.length > 100) {
      return "data.spec.screens is invalid";
    }
    for (const field of screen.fields) {
      if (!isPlainObject(field) || boundedStrings(field, [["f", 255], ["fmt", 2000], ["err", 2000]]) || typeof field.req !== "boolean") {
        return "data.spec.screens fields are invalid";
      }
    }
  }
  for (const edge of spec.edges) {
    if (!isPlainObject(edge) || boundedStrings(edge, [["sid", 16, false], ["caseTxt", 2000], ["expected", 4000]]) || !SCENARIO_ID_RE.test(edge.sid)) {
      return "data.spec.edges is invalid";
    }
  }
  for (const event of spec.events) {
    if (!isPlainObject(event) || boundedStrings(event, [["name", 255], ["trigger", 2000], ["props", 2000]]) || typeof event.fires !== "boolean") {
      return "data.spec.events is invalid";
    }
  }
  for (const fidelity of spec.fidelity) {
    if (!isPlainObject(fidelity) || boundedStrings(fidelity, [["path", 512], ["label", 16], ["note", 2000]]) || !FIDELITY_LABELS.has(fidelity.label)) {
      return "data.spec.fidelity is invalid";
    }
  }
  const scenarioIds = new Set();
  for (const scenario of spec.scenarios) {
    if (!isPlainObject(scenario) || boundedStrings(scenario, [["id", 16, false], ["name", 2000], ["trigger", 4000], ["ndreason", 4000]]) ||
        !SCENARIO_ID_RE.test(scenario.id) || scenarioIds.has(scenario.id) ||
        !booleanFields(scenario, ["demonstrable", "nd"])) {
      return "data.spec.scenarios is invalid";
    }
    scenarioIds.add(scenario.id);
  }

  const review = data.review;
  if (!Array.isArray(review.objections) || review.objections.length > 100) return "data.review.objections is invalid";
  for (const objection of review.objections) {
    if (!isPlainObject(objection) || boundedStrings(objection, [["type", 32, false], ["note", 4000], ["by", 120]]) ||
        !OBJECTION_TYPES.has(objection.type) || typeof objection.resolved !== "boolean") {
      return "data.review.objections is invalid";
    }
  }
  if (boundedStrings(review, [["coverageAgreed", 64], ["coverageBy", 255]])) return "data.review coverage is invalid";
  if (!isPlainObject(review.techChecks) || !booleanFields(review.techChecks, ["feasibility", "integrations", "dataBoundaries", "reuse"])) {
    return "data.review.techChecks is invalid";
  }
  if (!isPlainObject(review.qaChecks) || !booleanFields(review.qaChecks, ["states", "validations", "errorsRetries", "testability"])) {
    return "data.review.qaChecks is invalid";
  }
  if (!isPlainObject(review.dodManual) || !booleanFields(review.dodManual, ["secrets", "runzero"])) return "data.review.dodManual is invalid";
  if (!isPlainObject(review.stranger) || boundedStrings(review.stranger, [["ran", 64], ["report", 100_000]]) ||
      !Number.isSafeInteger(review.stranger.defects) || review.stranger.defects < -1 || review.stranger.defects > 10_000) {
    return "data.review.stranger is invalid";
  }
  const hasStrangerRun = review.stranger.ran !== "";
  if ((!hasStrangerRun && (review.stranger.defects !== -1 || review.stranger.report !== "")) ||
      (hasStrangerRun && (!completedGateMarker(review.stranger.ran) || review.stranger.defects < 0 ||
        !review.stranger.report.trim()))) {
    return "data.review.stranger is invalid";
  }
  if (!isPlainObject(review.signatures) || boundedStrings(review.signatures, [["pm", 255], ["tech", 255], ["qa", 255]])) {
    return "data.review.signatures is invalid";
  }
  if ([review.signatures.pm, review.signatures.tech, review.signatures.qa]
      .some((signature) => signature !== "" && !signature.trim())) {
    return "data.review.signatures is invalid";
  }
  if (review.digest !== undefined && !validBoundedString(review.digest, 71)) {
    return "data.review.digest is invalid";
  }
  if (!Number.isSafeInteger(review.releaseNum) || review.releaseNum < 1 || review.releaseNum > 10_000) return "data.review.releaseNum is invalid";
  if (!isPlainObject(review.reconcile) || typeof review.eventAudit !== "boolean" || typeof review.mocksVerified !== "boolean") {
    return "data.review reconciliation is invalid";
  }
  const allowedReconciliationKeys = new Set();
  for (const scenarioId of scenarioIds) {
    allowedReconciliationKeys.add(scenarioId);
    allowedReconciliationKeys.add(`${scenarioId}_note`);
    const state = review.reconcile[scenarioId];
    if (state !== undefined && state !== "pass" && state !== "deviation") return "data.review.reconcile is invalid";
    const note = review.reconcile[`${scenarioId}_note`];
    if (note !== undefined && !validBoundedString(note, 4000)) return "data.review.reconcile is invalid";
    if ((state === "deviation" && (!note || !note.trim())) || (state !== "deviation" && note !== undefined)) {
      return "data.review.reconcile is invalid";
    }
  }
  if (Object.keys(review.reconcile).some((key) => !allowedReconciliationKeys.has(key))) {
    return "data.review.reconcile is invalid";
  }

  const isFrozen = completedGateMarker(meta.g4);
  const digest = String(review.digest || "");
  const postFreezeStateIsEmpty = meta.g6 === "" && emptyObject(review.reconcile) &&
    review.eventAudit === false && review.mocksVerified === false;
  if (!isFrozen) {
    if (!["framing", "building"].includes(meta.status) || digest || !postFreezeStateIsEmpty) {
      return "data freeze and reconciliation state is invalid";
    }
  } else {
    if (!/^sha256:[0-9a-f]{64}$/.test(digest) ||
        (meta.status === "frozen" && meta.g6 !== "") ||
        (meta.status === "reconciled" && (meta.g6 === "" || !reconciliationComplete(data))) ||
        !["frozen", "reconciled"].includes(meta.status)) {
      return "data freeze and reconciliation state is invalid";
    }
  }
  return null;
}

function canonicalJson(value) {
  if (value === null || typeof value === "boolean" || typeof value === "number" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
}

function computeFreezeDigest(data) {
  const snapshot = frozenCore(data);
  snapshot.review.digest = "";
  return `sha256:${crypto.createHash("sha256").update(canonicalJson(snapshot), "utf8").digest("hex")}`;
}

function validateFrozenSnapshotIntegrity(data) {
  if (data.meta.g4 === "") return null;
  if (!completedGateMarker(data.meta.g4)) return "the stored frozen snapshot fingerprint is invalid";
  const digest = String(data.review.digest || "");
  return /^sha256:[0-9a-f]{64}$/.test(digest) &&
    constantTimeEqual(digest, computeFreezeDigest(data))
    ? null
    : "the stored frozen snapshot fingerprint is invalid";
}

function frozenCore(data) {
  const snapshot = JSON.parse(JSON.stringify(data));
  snapshot.meta.g6 = "";
  if (snapshot.meta.status === "frozen" || snapshot.meta.status === "reconciled") {
    snapshot.meta.status = "frozen";
  }
  snapshot.review.reconcile = {};
  snapshot.review.eventAudit = false;
  snapshot.review.mocksVerified = false;
  return snapshot;
}

function emptyObject(value) {
  return isPlainObject(value) && Object.keys(value).length === 0;
}

function freezePrerequisitesComplete(data) {
  const { meta, spec, review } = data;
  const scenarios = spec.scenarios.filter((scenario) => scenario.name.trim());
  const dataRuleClean = meta.data === "synthetic" ||
    (meta.data === "staging-dump" && meta.pipeline.trim() && meta.dumpdate.trim());
  return Boolean(
    review.coverageAgreed.trim() &&
    scenarios.length && scenarios.every((scenario) =>
      scenario.demonstrable || (scenario.nd && scenario.ndreason.trim())) &&
    spec.events.length && spec.events.every((event) => event.fires) &&
    spec.fidelity.length && spec.fidelity.every((item) => item.path.trim()) &&
    meta.entry.trim() && meta.run.trim() && dataRuleClean &&
    review.objections.every((objection) => objection.resolved) &&
    review.dodManual.secrets && review.dodManual.runzero &&
    completedGateMarker(review.stranger.ran) && review.stranger.defects === 0 &&
    review.stranger.report.trim() &&
    review.signatures.pm && review.signatures.tech && review.signatures.qa
  );
}

function reconciliationComplete(data) {
  const scenarios = data.spec.scenarios.filter((scenario) => scenario.name.trim());
  return Boolean(
    completedGateMarker(data.meta.g4) && scenarios.length &&
    scenarios.every((scenario) => {
      const state = data.review.reconcile[scenario.id];
      return state === "pass" ||
        (state === "deviation" && Boolean(data.review.reconcile[`${scenario.id}_note`]?.trim()));
    }) &&
    data.review.eventAudit && data.review.mocksVerified
  );
}

function signaturesEmpty(data) {
  const signatures = data.review.signatures;
  return signatures.pm === "" && signatures.tech === "" && signatures.qa === "";
}

function strangerEvidenceEmpty(data) {
  const stranger = data.review.stranger;
  const manual = data.review.dodManual;
  return stranger.ran === "" && stranger.defects === -1 && stranger.report === "" &&
    manual.secrets === false && manual.runzero === false && signaturesEmpty(data);
}

function transitionClone(data) {
  return JSON.parse(JSON.stringify(data));
}

function signatureCore(data) {
  const snapshot = transitionClone(data);
  snapshot.meta.g4 = "";
  snapshot.meta.g6 = "";
  snapshot.review.signatures = { pm: "", tech: "", qa: "" };
  snapshot.review.digest = "";
  snapshot.review.reconcile = {};
  snapshot.review.eventAudit = false;
  snapshot.review.mocksVerified = false;
  return snapshot;
}

function strangerCore(data) {
  const snapshot = signatureCore(data);
  snapshot.review.techChecks = { feasibility: false, integrations: false, dataBoundaries: false, reuse: false };
  snapshot.review.qaChecks = { states: false, validations: false, errorsRetries: false, testability: false };
  snapshot.review.objections = [];
  snapshot.review.dodManual = { secrets: false, runzero: false };
  snapshot.review.stranger = { ran: "", defects: -1, report: "" };
  return snapshot;
}

function freezeTransitionCore(data) {
  const snapshot = transitionClone(data);
  snapshot.meta.g4 = "";
  snapshot.meta.g6 = "";
  snapshot.meta.status = "";
  snapshot.review.digest = "";
  snapshot.review.reconcile = {};
  snapshot.review.eventAudit = false;
  snapshot.review.mocksVerified = false;
  return snapshot;
}

function validateFreezeTransition(currentData, nextData) {
  if (!validGateMarker(nextData.meta.g4) || !validGateMarker(nextData.meta.g6)) {
    return "the freeze gate marker is invalid";
  }
  const wasFrozen = Boolean(currentData && completedGateMarker(currentData.meta.g4));
  const isFrozen = completedGateMarker(nextData.meta.g4);
  const digest = String(nextData.review.digest || "");
  const postFreezeStateIsEmpty = nextData.meta.g6 === "" && emptyObject(nextData.review.reconcile) &&
    nextData.review.eventAudit === false && nextData.review.mocksVerified === false;

  if (!currentData) {
    if (isFrozen || !strangerEvidenceEmpty(nextData)) {
      return "new projects must start without freeze evidence or signatures";
    }
    return null;
  }
  if (!wasFrozen && !isFrozen) {
    if (!isDeepStrictEqual(strangerCore(currentData), strangerCore(nextData)) &&
        !strangerEvidenceEmpty(nextData)) {
      return "reviewed content changed without clearing freeze evidence and signatures";
    }
    if (!isDeepStrictEqual(signatureCore(currentData), signatureCore(nextData)) &&
        !signaturesEmpty(nextData)) {
      return "review evidence changed without clearing signatures";
    }
    return null;
  }
  if (!wasFrozen && isFrozen) {
    if (currentData.meta.status !== "building" || nextData.meta.status !== "frozen" ||
        !isDeepStrictEqual(freezeTransitionCore(currentData), freezeTransitionCore(nextData)) ||
        !postFreezeStateIsEmpty ||
        !freezePrerequisitesComplete(nextData) ||
        !/^sha256:[0-9a-f]{64}$/.test(digest) || !constantTimeEqual(digest, computeFreezeDigest(nextData))) {
      return "the frozen snapshot fingerprint is invalid";
    }
    return null;
  }

  if (isFrozen && isDeepStrictEqual(frozenCore(currentData), frozenCore(nextData))) {
    if (!['frozen', 'reconciled'].includes(nextData.meta.status) ||
        (nextData.meta.status === "frozen" && nextData.meta.g6 !== "") ||
        (nextData.meta.status === "reconciled" &&
          (nextData.meta.g6 === "" || !reconciliationComplete(nextData)))) {
      return "the post-freeze reconciliation state is invalid";
    }
    return null;
  }

  const signatures = nextData.review.signatures;
  const stranger = nextData.review.stranger;
  const manual = nextData.review.dodManual;
  const expectedRelease = Number(currentData.review.releaseNum) + 1;
  if (isFrozen || nextData.meta.status !== "building" || nextData.meta.g4 !== "" ||
      !postFreezeStateIsEmpty || digest || signatures.pm || signatures.tech || signatures.qa ||
      stranger.ran || stranger.defects !== -1 || stranger.report ||
      manual.secrets || manual.runzero ||
      !Number.isSafeInteger(expectedRelease) || expectedRelease > 10_000 ||
      nextData.review.releaseNum !== expectedRelease) {
    return "editing frozen content requires a clean, incremented release with new evidence and signatures";
  }
  return null;
}

function validateIdentifier(value) {
  return typeof value === "string" && value.length <= 64 && PROJECT_ID_RE.test(value);
}

function dateOnly(value) {
  if (value instanceof Date && Number.isFinite(value.valueOf())) return value.toISOString().slice(0, 10);
  const stringValue = String(value || "");
  return /^\d{4}-\d{2}-\d{2}/.test(stringValue) ? stringValue.slice(0, 10) : "";
}

function toMeta(row) {
  return {
    title: row.title,
    owner: row.owner,
    tier: row.tier,
    status: row.status,
    created: dateOnly(row.created_at),
  };
}

function parseStoredData(value) {
  return typeof value === "string" ? JSON.parse(value) : value;
}

function checkWriteFence(filePath, lstat = fs.lstatSync) {
  if (!filePath) return { allowed: true };
  try {
    const stat = lstat(filePath);
    if (!stat.isFile()) return { allowed: false, code: "WRITE_FENCE_INVALID" };
    return { allowed: false, code: "WRITE_FENCE_ACTIVE" };
  } catch (error) {
    if (error && error.code === "ENOENT") return { allowed: true };
    return { allowed: false, code: safeCode(error) };
  }
}

async function acquireConnection(pool, timeoutMs = 2000) {
  let timedOut = false;
  let timer;
  const pending = Promise.resolve().then(() => pool.getConnection());
  const guarded = pending.then((connection) => {
    if (timedOut) {
      connection.release();
      return null;
    }
    return connection;
  });
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      timedOut = true;
      const error = new Error("database connection acquisition timed out");
      error.code = "POOL_ACQUIRE_TIMEOUT";
      reject(error);
    }, timeoutMs);
  });
  try {
    return await Promise.race([guarded, timeout]);
  } finally {
    clearTimeout(timer);
  }
}

async function poolQuery(pool, statement, params = []) {
  const connection = await acquireConnection(pool);
  try {
    // mysql2 query() interpolates values on the client and is unsafe when the
    // server enables NO_BACKSLASH_ESCAPES. Any statement carrying values must
    // use the binary prepared-statement protocol instead.
    return params.length
      ? await connection.execute(statement, params)
      : await connection.query(statement);
  } finally {
    connection.release();
  }
}

function secureHeaders(req, res, next) {
  const nonce = crypto.randomBytes(18).toString("base64");
  res.locals.cspNonce = nonce;
  res.set({
    "Content-Security-Policy": `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'`,
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
  });
  next();
}

async function readSchemaVersion(pool) {
  const [rows] = await poolQuery(pool, {
    sql: "SELECT version FROM schema_versions ORDER BY version DESC LIMIT 1",
    timeout: 1500,
  });
  return rows.length === 1 ? Number(rows[0].version) : null;
}

async function assertDatabaseReady(pool) {
  const version = await readSchemaVersion(pool);
  if (version !== CURRENT_SCHEMA_VERSION) {
    const error = new Error("database schema version is not supported");
    error.code = "SCHEMA_NOT_READY";
    throw error;
  }
}

function createApp({ pool, config, logger = createLogger() }) {
  if (!pool || !config) throw new TypeError("pool and config are required");
  const app = express();
  const indexTemplate = fs.readFileSync(path.join(__dirname, "public", "index.html"), "utf8");
  const requireAuthentication = authentication(config);

  app.disable("x-powered-by");
  app.set("trust proxy", false);
  app.use(secureHeaders);
  app.use((_req, res, next) => {
    res.set("X-PRD-Studio-Release", config.releaseId || "development");
    next();
  });

  const health = (_req, res) => {
    res.set("Cache-Control", "no-store");
    return res.status(200).json({ status: "ok", release: config.releaseId || "development" });
  };
  const ready = async (_req, res) => {
    try {
      const version = await readSchemaVersion(pool);
      res.set("Cache-Control", "no-store");
      if (version !== CURRENT_SCHEMA_VERSION) {
        return res.status(503).json({ status: "not_ready" });
      }
      return res.status(200).json({
        status: "ready",
        release: config.releaseId || "development",
        identity: config.releaseIdentity || { commit: "", tree: "", artifactSha256: "" },
      });
    } catch (error) {
      logger.error("readiness_failed", { code: safeCode(error) });
      res.set("Cache-Control", "no-store");
      return res.status(503).json({ status: "not_ready" });
    }
  };

  app.get("/healthz", health);
  app.get("/readyz", ready);

  if (config.basePath) {
    app.get("/", (_req, res) => res.redirect(302, `${config.basePath}/`));
  }

  const router = express.Router();
  if (config.basePath) {
    router.get("/healthz", health);
    router.get("/readyz", ready);
  }

  // In trusted-proxy mode even the document requires the proxy-injected marker.
  // The proxy must overwrite, never append or pass through, this request header.
  if (config.authMode === "trusted_proxy") router.use(requireAuthentication);

  const api = express.Router();
  if (config.authMode === "token") api.use(requireAuthentication);
  api.use((req, res, next) => {
    res.set("Cache-Control", "no-store");
    next();
  });
  api.use((req, res, next) => {
    if (!config.writeFenceFile || (req.method !== "POST" && req.method !== "PUT")) return next();
    const fence = checkWriteFence(config.writeFenceFile);
    if (fence.allowed) return next();
    if (fence.code !== "WRITE_FENCE_ACTIVE") logger.error("write_fence_check_failed", { code: fence.code });
    return res.status(503).json({ error: "writes temporarily unavailable; retry later" });
  });
  api.use(express.json({ limit: `${MAX_PROJECT_BYTES + 16 * 1024}b`, strict: true }));

  api.get("/projects", async (_req, res) => {
    try {
      const [rows] = await poolQuery(pool,
        { sql: "SELECT id, title, owner, tier, status, created_at FROM projects ORDER BY updated_at DESC", timeout: 5000 }
      );
      for (const row of rows) {
        if (!validateIdentifier(row.id) || !validBoundedString(row.title, 255, true) ||
            !validBoundedString(row.owner, 120) || (row.tier !== null && !TIERS.has(row.tier)) || !STATUSES.has(row.status)) {
          const integrityError = new Error("invalid stored project metadata");
          integrityError.code = "INVALID_STORED_DATA";
          throw integrityError;
        }
      }
      return res.json(rows.map((row) => ({ id: row.id, meta: toMeta(row) })));
    } catch (error) {
      logger.error("project_list_failed", { code: safeCode(error) });
      return res.status(500).json({ error: "list failed" });
    }
  });

  api.get("/projects/:id", async (req, res) => {
    if (!validateIdentifier(req.params.id)) return res.status(400).json({ error: "invalid project id" });
    try {
      const [rows] = await poolQuery(pool,
        { sql: "SELECT data, row_version FROM projects WHERE id = ?", timeout: 5000 },
        [req.params.id]
      );
      if (!rows.length) return res.status(404).json({ error: "not found" });
      const data = parseStoredData(rows[0].data);
      if (validateProjectData(data) || validateFrozenSnapshotIntegrity(data)) {
        const integrityError = new Error("invalid stored project data");
        integrityError.code = "INVALID_STORED_DATA";
        throw integrityError;
      }
      return res.json({ id: req.params.id, version: rows[0].row_version, data });
    } catch (error) {
      logger.error("project_read_failed", { code: safeCode(error) });
      return res.status(500).json({ error: "read failed" });
    }
  });

  api.post("/projects", async (req, res) => {
    const body = isPlainObject(req.body) ? req.body : {};
    const { id, slug, data } = body;
    if (!validateIdentifier(id) || !validateIdentifier(slug)) {
      return res.status(400).json({ error: "id and slug must be lowercase URL-safe identifiers of at most 64 characters" });
    }
    const validationError = validateProjectData(data);
    if (validationError) return res.status(400).json({ error: validationError });
    const freezeError = validateFreezeTransition(null, data);
    if (freezeError) return res.status(400).json({ error: freezeError });
    const meta = data.meta;
    try {
      await poolQuery(pool,
        { sql: `INSERT INTO projects (id, slug, title, owner, tech_lead, qa, designer, tier, status, data, row_version)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)`, timeout: 5000 },
        [id, slug, meta.title, meta.owner, meta.tech, meta.qa, meta.designer,
          meta.tier || null, meta.status, JSON.stringify(data)]
      );
      return res.status(201).json({ ok: true, version: 1 });
    } catch (error) {
      if (error && error.code === "ER_DUP_ENTRY") return res.status(409).json({ error: "id already exists" });
      logger.error("project_create_failed", { code: safeCode(error) });
      return res.status(500).json({ error: "create failed" });
    }
  });

  api.put("/projects/:id", async (req, res) => {
    if (!validateIdentifier(req.params.id)) return res.status(400).json({ error: "invalid project id" });
    const body = isPlainObject(req.body) ? req.body : {};
    const { version, data } = body;
    if (!Number.isSafeInteger(version) || version < 1) {
      return res.status(400).json({ error: "version must be a positive integer" });
    }
    const validationError = validateProjectData(data);
    if (validationError) return res.status(400).json({ error: validationError });

    let connection;
    try {
      connection = await acquireConnection(pool);
      await connection.beginTransaction();
      const [rows] = await connection.execute(
        { sql: "SELECT row_version, data FROM projects WHERE id = ? FOR UPDATE", timeout: 5000 },
        [req.params.id]
      );
      if (!rows.length) {
        await connection.rollback();
        return res.status(404).json({ error: "not found" });
      }
      if (Number(rows[0].row_version) !== version) {
        await connection.rollback();
        const currentData = parseStoredData(rows[0].data);
        if (validateProjectData(currentData) || validateFrozenSnapshotIntegrity(currentData)) {
          const integrityError = new Error("invalid stored project data");
          integrityError.code = "INVALID_STORED_DATA";
          throw integrityError;
        }
        return res.status(409).json({
          error: "stale version",
          version: rows[0].row_version,
          data: currentData,
        });
      }
      const currentData = parseStoredData(rows[0].data);
      if (validateProjectData(currentData) || validateFrozenSnapshotIntegrity(currentData)) {
        const integrityError = new Error("invalid stored project data");
        integrityError.code = "INVALID_STORED_DATA";
        throw integrityError;
      }
      const freezeError = validateFreezeTransition(currentData, data);
      if (freezeError) {
        await connection.rollback();
        return res.status(409).json({ error: freezeError });
      }
      const meta = data.meta;
      await connection.execute(
        { sql: `UPDATE projects SET title=?, owner=?, tech_lead=?, qa=?, designer=?, tier=?, status=?,
         data=?, row_version=row_version+1 WHERE id=?`, timeout: 5000 },
        [meta.title, meta.owner, meta.tech, meta.qa, meta.designer, meta.tier || null,
          meta.status, JSON.stringify(data), req.params.id]
      );
      await connection.commit();
      return res.json({ ok: true, version: version + 1 });
    } catch (error) {
      if (connection) {
        try { await connection.rollback(); } catch (_) { /* preserve the first failure */ }
      }
      logger.error("project_save_failed", { code: safeCode(error) });
      return res.status(500).json({ error: "save failed" });
    } finally {
      if (connection) connection.release();
    }
  });

  api.use((_req, res) => res.status(404).json({ error: "not found" }));
  router.use("/api", api);

  router.get("/", (_req, res) => {
    const runtimeConfig = JSON.stringify({ basePath: config.basePath, authMode: config.authMode })
      .replace(/</g, "\\u003c")
      .replace(/>/g, "\\u003e")
      .replace(/&/g, "\\u0026");
    const html = indexTemplate
      .replaceAll("__CSP_NONCE__", res.locals.cspNonce)
      .replace("__PRD_STUDIO_RUNTIME_CONFIG__", runtimeConfig);
    res.set({ "Cache-Control": "no-store", "Content-Type": "text/html; charset=utf-8" });
    return res.send(html);
  });

  router.use((_req, res) => res.status(404).json({ error: "not found" }));
  app.use(config.basePath || "/", router);

  app.use((error, _req, res, _next) => {
    if (error && error.type === "entity.too.large") return res.status(413).json({ error: "request body too large" });
    if (error instanceof SyntaxError && error.status === 400) return res.status(400).json({ error: "invalid JSON" });
    logger.error("request_failed", { code: safeCode(error) });
    return res.status(500).json({ error: "request failed" });
  });
  return app;
}

function installGracefulShutdown(server, pool, logger, timeoutMs = 10_000) {
  let shuttingDown = false;
  const shutdown = (signal) => {
    if (shuttingDown) return;
    shuttingDown = true;
    logger.info("shutdown_started", { signal });
    const forceTimer = setTimeout(() => {
      logger.error("shutdown_timed_out", { code: "SHUTDOWN_TIMEOUT" });
      server.closeAllConnections?.();
      process.exitCode = 1;
    }, timeoutMs);
    forceTimer.unref?.();
    server.close(async (serverError) => {
      try {
        await pool.end();
      } catch (poolError) {
        logger.error("database_close_failed", { code: safeCode(poolError) });
        process.exitCode = 1;
      }
      clearTimeout(forceTimer);
      if (serverError) {
        logger.error("server_close_failed", { code: safeCode(serverError) });
        process.exitCode = 1;
      } else {
        logger.info("shutdown_complete");
      }
    });
  };
  process.once("SIGTERM", shutdown);
  process.once("SIGINT", shutdown);
  return shutdown;
}

async function handleServerFailure(server, pool, logger, error, setExitCode = (code) => { process.exitCode = code; }) {
  logger.error("server_failed", { code: safeCode(error) });
  setExitCode(1);
  try {
    server.closeAllConnections?.();
    if (server.listening) {
      await new Promise((resolve) => server.close((closeError) => {
        if (closeError) logger.error("server_close_failed", { code: safeCode(closeError) });
        resolve();
      }));
    }
  } catch (closeError) {
    logger.error("server_close_failed", { code: safeCode(closeError) });
  }
  try {
    await pool.end();
  } catch (poolError) {
    logger.error("database_close_failed", { code: safeCode(poolError) });
  }
}

function installServerFailureHandler(server, pool, logger, setExitCode) {
  return new Promise((resolve) => {
    server.once("error", (error) => {
      void handleServerFailure(server, pool, logger, error, setExitCode).then(resolve);
    });
  });
}

async function main() {
  require("dotenv").config({ quiet: true });
  const logger = createLogger();
  let config;
  let pool;
  try {
    config = loadConfig(process.env);
    pool = createPoolFromConfig(config);
    await assertDatabaseReady(pool);
  } catch (error) {
    logger.error("startup_configuration_failed", { code: safeCode(error) });
    if (pool) {
      try { await pool.end(); } catch (_) { /* startup already failed */ }
    }
    process.exitCode = 1;
    return;
  }
  const app = createApp({ pool, config, logger });
  const server = config.socketPath
    ? app.listen(config.socketPath)
    : app.listen(config.port, config.host);
  server.requestTimeout = 15_000;
  server.headersTimeout = 10_000;
  server.keepAliveTimeout = 5_000;
  server.on("listening", () => logger.info("server_listening"));
  installServerFailureHandler(server, pool, logger);
  installGracefulShutdown(server, pool, logger);
}

module.exports = {
  CURRENT_SCHEMA_VERSION,
  MAX_PROJECT_BYTES,
  acquireConnection,
  assertDatabaseReady,
  buildPoolOptions,
  checkWriteFence,
  constantTimeEqual,
  computeFreezeDigest,
  createApp,
  createLogger,
  createPoolFromConfig,
  handleServerFailure,
  installGracefulShutdown,
  installServerFailureHandler,
  loadConfig,
  normalizeBasePath,
  safeCode,
  validateIdentifier,
  validateFreezeTransition,
  validateProjectData,
};

if (require.main === module) main();
