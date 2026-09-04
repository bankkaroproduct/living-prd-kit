-- PRD Studio schema version 1. MySQL 8.0+ only.
-- Applied and checksummed by scripts/apply-schema.js; do not run ad hoc.

CREATE TABLE schema_versions (
  version       INT UNSIGNED NOT NULL PRIMARY KEY,
  name          VARCHAR(120) NOT NULL,
  checksum      CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  applied_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE projects (
  id           VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
  slug         VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  title        VARCHAR(255) NOT NULL DEFAULT '',
  owner        VARCHAR(120) NOT NULL DEFAULT '',
  tech_lead    VARCHAR(120) NOT NULL DEFAULT '',
  qa           VARCHAR(120) NOT NULL DEFAULT '',
  designer     VARCHAR(120) NOT NULL DEFAULT '',
  tier         VARCHAR(8) NULL,
  status       VARCHAR(24) NOT NULL DEFAULT 'framing',
  data         JSON NOT NULL,
  row_version  INT UNSIGNED NOT NULL DEFAULT 1,
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  CONSTRAINT chk_projects_id CHECK (REGEXP_LIKE(id, '^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$', 'c')),
  CONSTRAINT chk_projects_slug CHECK (REGEXP_LIKE(slug, '^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$', 'c')),
  CONSTRAINT chk_projects_tier CHECK (tier IS NULL OR tier IN ('T1', 'T2', 'T3')),
  CONSTRAINT chk_projects_status CHECK (status IN ('framing', 'building', 'frozen', 'reconciled')),
  CONSTRAINT chk_projects_row_version CHECK (row_version >= 1),
  INDEX idx_projects_status (status),
  INDEX idx_projects_updated (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
