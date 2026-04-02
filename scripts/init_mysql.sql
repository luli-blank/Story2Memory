-- story2memory new structured schema (chapter-first + hierarchical memory)
CREATE DATABASE IF NOT EXISTS novel_cognition CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE novel_cognition;

CREATE TABLE IF NOT EXISTS books (
  book_id           VARCHAR(64) PRIMARY KEY,
  title             VARCHAR(255) NOT NULL,
  author            VARCHAR(255) NULL,
  language          VARCHAR(32) NULL,
  tags              JSON NULL,
  extra             JSON NULL,
  status            VARCHAR(32) NOT NULL DEFAULT 'active',
  created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ingest_jobs (
  job_id            VARCHAR(64) PRIMARY KEY,
  book_id           VARCHAR(64) NOT NULL,
  status            VARCHAR(32) NOT NULL,
  file_path         VARCHAR(1024) NOT NULL,
  meta              JSON NULL,
  error             TEXT NULL,
  created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_ingest_jobs_book_id (book_id),
  INDEX idx_ingest_jobs_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS chapters (
  chapter_id        VARCHAR(128) PRIMARY KEY,
  book_id           VARCHAR(64) NOT NULL,
  chapter_no        INT NOT NULL,
  title             VARCHAR(255) NOT NULL,
  raw_text          LONGTEXT NOT NULL,
  normalized_text   LONGTEXT NOT NULL,
  prev_summary      LONGTEXT NULL,
  l1_summary        LONGTEXT NOT NULL,
  story_arc_id      VARCHAR(128) NOT NULL,
  context_tags      JSON NULL,
  entities          JSON NULL,
  factions          JSON NULL,
  created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_chapters_book_no (book_id, chapter_no),
  INDEX idx_chapters_arc (book_id, story_arc_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS event_chunks (
  event_id           VARCHAR(128) PRIMARY KEY,
  book_id            VARCHAR(64) NOT NULL,
  story_arc_id       VARCHAR(128) NOT NULL,
  start_chapter_no   INT NOT NULL,
  end_chapter_no     INT NOT NULL,
  start_chapter_id   VARCHAR(128) NOT NULL,
  end_chapter_id     VARCHAR(128) NOT NULL,
  chapter_ids        JSON NULL,
  context_tags       JSON NULL,
  characters         JSON NULL,
  summary            LONGTEXT NOT NULL,
  created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_event_chunks_book_no (book_id, start_chapter_no),
  INDEX idx_event_chunks_arc (book_id, story_arc_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS global_arcs (
  arc_id             VARCHAR(128) PRIMARY KEY,
  book_id            VARCHAR(64) NOT NULL,
  arc_index          INT NOT NULL,
  title              VARCHAR(255) NOT NULL,
  summary            LONGTEXT NOT NULL,
  start_chapter_no   INT NOT NULL,
  end_chapter_no     INT NOT NULL,
  context_tags       JSON NULL,
  source_event_ids   JSON NULL,
  created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_global_arcs_book_idx (book_id, arc_index)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS character_profiles (
  character_id       VARCHAR(128) PRIMARY KEY,
  book_id            VARCHAR(64) NOT NULL,
  name               VARCHAR(128) NOT NULL,
  aliases            JSON NULL,
  core_static        JSON NULL,
  dynamic_state      JSON NULL,
  key_events         JSON NULL,
  version            INT NOT NULL DEFAULT 1,
  created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_character_profiles_book_name (book_id, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
