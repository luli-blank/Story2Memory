CREATE TABLE IF NOT EXISTS `sessions` (
    `id` VARCHAR(36) PRIMARY KEY COMMENT '会话唯一ID',
    `user_id` VARCHAR(36) NOT NULL COMMENT '用户ID',
    `title` VARCHAR(255) COMMENT '会话标题',
    `book_id` INT DEFAULT NULL COMMENT '归属书籍ID（用于精确删除）',
    `session_kind` ENUM('qa', 'roleplay') NOT NULL DEFAULT 'qa' COMMENT '会话类型',
    `character_id` BIGINT DEFAULT NULL COMMENT '角色扮演会话绑定的角色ID',
    `current_summary` TEXT COMMENT '当前对话的压缩摘要',
    `last_summarized_msg_id` BIGINT DEFAULT 0 COMMENT '最后一次摘要处理到的消息ID',
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX `idx_user` (`user_id`),
    INDEX `idx_session_book` (`book_id`, `session_kind`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `messages` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(36) NOT NULL,
    `role` ENUM('user', 'assistant', 'system') NOT NULL,
    `content` TEXT NOT NULL,
    `token_count` INT DEFAULT 0 COMMENT '本条消息消耗的Token数',
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT `fk_session` FOREIGN KEY (`session_id`) REFERENCES `sessions`(`id`) ON DELETE CASCADE,
    INDEX `idx_session_time` (`session_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `books` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `title` VARCHAR(255) NOT NULL COMMENT '书名',
    `author` VARCHAR(100) DEFAULT '未知' COMMENT '作者',
    `source_format` ENUM('txt', 'epub') DEFAULT 'txt' COMMENT '原始来源格式',
    `cover_url` VARCHAR(512) COMMENT '封面图片路径',
    `cover_asset_id` BIGINT DEFAULT NULL COMMENT '封面资源ID（已废弃）',
    `description` TEXT COMMENT '书籍简介/大纲',
    `total_chapters` INT DEFAULT 0 COMMENT '总章节数',
    `total_words` INT DEFAULT 0 COMMENT '总字数',
    `status` ENUM('pending', 'processing', 'completed', 'error') DEFAULT 'pending' COMMENT '分析状态',
    `file_path` VARCHAR(512) COMMENT '原始文件存储路径',
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX `idx_title` (`title`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `book_chapters` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `book_id` INT NOT NULL COMMENT '关联的书籍ID',
    `chapter_index` INT NOT NULL COMMENT '章节序号（第几章）',
    `title` VARCHAR(255) COMMENT '章节标题',
    `status` ENUM('pending', 'success', 'error') DEFAULT NULL COMMENT '章节信息提取状态',
    `content` MEDIUMTEXT NOT NULL COMMENT '章节原文（支持百万字小说中单章长文本）',
    `chapter_summary` TEXT COMMENT '预留：章节级摘要（用于后续组织情节级摘要）',
    `character` LONGTEXT COMMENT '章节级角色信息（名称 + 行为/语言概括）',
    `special_existence` LONGTEXT COMMENT '章节级特殊存在/特殊物品信息（名称 + 一句话描述）',
    `origanizations` LONGTEXT COMMENT '章节级组织/势力信息（名称 + 一句话概括）',
    `world_rules` LONGTEXT COMMENT '章节级世界规则/设定/限制信息',
    `raw_summary_json` JSON COMMENT '章节摘要模型原始输出',
    `word_count` INT DEFAULT 0 COMMENT '本章字数',
    `plot_id` INT DEFAULT 0 COMMENT '预留：所属卷ID（便于未来扩展情节级映射）',
    `volume_id` INT DEFAULT 0 COMMENT '预留：所属卷ID（便于未来扩展卷级映射）',
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT `fk_book_id` FOREIGN KEY (`book_id`) REFERENCES `books`(`id`) ON DELETE CASCADE,
    INDEX `idx_book_chapter` (`book_id`, `chapter_index`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `book_plots` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `book_id` INT NOT NULL,
    `volume_id` INT NOT NULL COMMENT '归属的卷ID',
    `plot_id` INT NOT NULL DEFAULT 0 COMMENT '在本书中的情节序号',
    `start_chapter_index` INT NOT NULL COMMENT '情节起始章节',
    `end_chapter_index` INT NOT NULL COMMENT '情节结束章节',
    `title` VARCHAR(255) COMMENT '情节标题（如：误入白虎堂）',
    `status` ENUM('pending', 'success', 'error') DEFAULT NULL COMMENT '情节信息提取状态',
    `plot_summary` TEXT COMMENT '完整的情节摘要（用于大模型阅读）',
    `character` JSON COMMENT '情节级角色聚合信息',
    `special_existence` JSON COMMENT '情节级特殊存在聚合信息',
    `origanizations` JSON COMMENT '情节级组织/势力聚合信息',
    `world_rules` JSON COMMENT '情节级世界规则聚合信息',
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_range` (`book_id`, `start_chapter_index`, `end_chapter_index`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `book_volumes` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `book_id` INT NOT NULL COMMENT '关联的书籍ID',
    `volume_index` INT NOT NULL COMMENT '卷序号（第几卷，如 1, 2, 3...）',
    `title` VARCHAR(255) COMMENT '卷名（如：第一卷 鬼眼刑警）',
    -- 【范围映射：双重索引】
    -- 既能通过 Plot ID 快速找情节，也能通过 Chapter ID 快速定位原文
    `start_plot_index` INT NOT NULL COMMENT '本卷起始的情节序号(plot_id)',
    `end_plot_index` INT NOT NULL COMMENT '本卷结束的情节序号(plot_id)',
    `start_chapter_index` INT NOT NULL COMMENT '本卷起始章节序号',
    `end_chapter_index` INT NOT NULL COMMENT '本卷结束章节序号',
    -- 【核心内容：给人类读】
    `volume_summary` TEXT COMMENT '卷级宏观摘要（侧重于主角人生阶段改变、世界观揭示和主线推进）',
    -- 【辅助信息】
    `time_span` VARCHAR(100) COMMENT '本卷发生的时间跨度（如：三个月，或 修仙历3000-3500年）',
    `plot_count` INT DEFAULT 0 COMMENT '本卷包含的情节数量统计',
    `raw_volume_json` JSON COMMENT '卷摘要模型原始输出',
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- 索引优化：高频查询通常是“某本书的第几卷”
    INDEX `idx_book_vol` (`book_id`, `volume_index`),
    -- 范围查询优化：查找某个 Plot ID 属于哪一卷
    INDEX `idx_plot_range` (`book_id`, `start_plot_index`, `end_plot_index`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='卷级索引表：记录宏观叙事与角色状态快照';

CREATE TABLE IF NOT EXISTS `characters` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `book_id` INT NOT NULL,
    `name` VARCHAR(255) NOT NULL,
    `aliases` JSON NOT NULL,
    `records` JSON NOT NULL,
    `NEED_DELETE` ENUM('yes', 'no') NOT NULL DEFAULT 'no'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `special_existences` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `book_id` INT NOT NULL,
    `name` VARCHAR(255) NOT NULL,
    `aliases` JSON NOT NULL,
    `records` JSON NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `origanizations` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `book_id` INT NOT NULL,
    `name` VARCHAR(255) NOT NULL,
    `aliases` JSON NOT NULL,
    `records` JSON NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `world_rules` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `book_id` INT NOT NULL,
    `name` VARCHAR(255) NOT NULL,
    `aliases` JSON NOT NULL,
    `records` JSON NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `character_profile_jobs` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `book_id` INT NOT NULL,
    `character_id` BIGINT NOT NULL,
    `character_name` VARCHAR(255) NOT NULL,
    `status` ENUM('pending', 'running', 'completed', 'error', 'cached') NOT NULL DEFAULT 'pending',
    `error_message` TEXT NULL,
    `started_at` TIMESTAMP NULL DEFAULT NULL,
    `finished_at` TIMESTAMP NULL DEFAULT NULL,
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX `idx_character_profile_jobs_lookup` (`book_id`, `character_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `character_profiles` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `book_id` INT NOT NULL,
    `character_id` BIGINT NOT NULL,
    `character_name` VARCHAR(255) NOT NULL,
    `aliases_json` JSON NOT NULL,
    `first_chapter_index` INT NOT NULL DEFAULT 0,
    `last_chapter_index` INT NOT NULL DEFAULT 0,
    `record_count` INT NOT NULL DEFAULT 0,
    `profile_json` JSON NOT NULL,
    `source_chapters_json` JSON NOT NULL,
    `version_hash` VARCHAR(64) NOT NULL,
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY `uniq_character_profile` (`book_id`, `character_id`),
    INDEX `idx_character_profile_version` (`book_id`, `version_hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `character_profile_chunks` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `book_id` INT NOT NULL,
    `character_id` BIGINT NOT NULL,
    `character_name` VARCHAR(255) NOT NULL,
    `volume_index` INT NOT NULL DEFAULT 0,
    `chunk_index` INT NOT NULL DEFAULT 0,
    `chapter_start` INT NOT NULL DEFAULT 0,
    `chapter_end` INT NOT NULL DEFAULT 0,
    `source_chapters_json` JSON NOT NULL,
    `chunk_json` JSON NOT NULL,
    `version_hash` VARCHAR(64) NOT NULL,
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY `uniq_character_profile_chunk` (`book_id`, `character_id`, `volume_index`, `chunk_index`),
    INDEX `idx_character_profile_chunk_version` (`book_id`, `character_id`, `version_hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `character_profile_volume_groups` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `book_id` INT NOT NULL,
    `character_id` BIGINT NOT NULL,
    `character_name` VARCHAR(255) NOT NULL,
    `volume_index` INT NOT NULL DEFAULT 0,
    `chunk_ids_json` JSON NOT NULL,
    `group_json` JSON NOT NULL,
    `version_hash` VARCHAR(64) NOT NULL,
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY `uniq_character_profile_volume_group` (`book_id`, `character_id`, `volume_index`),
    INDEX `idx_character_profile_volume_group_version` (`book_id`, `character_id`, `version_hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `character_relations` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `book_id` INT NOT NULL,
    `source_character_id` BIGINT NOT NULL,
    `source_character_name` VARCHAR(255) NOT NULL,
    `target_character_id` BIGINT NULL,
    `target_character_name` VARCHAR(255) NOT NULL,
    `summary` TEXT NOT NULL,
    `relation_model_json` JSON NULL,
    `history_json` JSON NOT NULL,
    `first_chapter_index` INT NOT NULL DEFAULT 0,
    `last_chapter_index` INT NOT NULL DEFAULT 0,
    `version_hash` VARCHAR(64) NOT NULL,
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY `uniq_character_relation` (`book_id`, `source_character_id`, `target_character_name`),
    INDEX `idx_character_relation_source` (`book_id`, `source_character_id`, `version_hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `character_relation_chunks` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `book_id` INT NOT NULL,
    `source_character_id` BIGINT NOT NULL,
    `source_character_name` VARCHAR(255) NOT NULL,
    `target_character_id` BIGINT NULL,
    `target_character_name` VARCHAR(255) NOT NULL,
    `volume_index` INT NOT NULL DEFAULT 0,
    `chunk_index` INT NOT NULL DEFAULT 0,
    `chapter_start` INT NOT NULL DEFAULT 0,
    `chapter_end` INT NOT NULL DEFAULT 0,
    `source_chapters_json` JSON NOT NULL,
    `chunk_json` JSON NOT NULL,
    `version_hash` VARCHAR(64) NOT NULL,
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY `uniq_character_relation_chunk` (`book_id`, `source_character_id`, `target_character_name`, `volume_index`, `chunk_index`),
    INDEX `idx_character_relation_chunk_version` (`book_id`, `source_character_id`, `version_hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `character_relation_volume_groups` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY,
    `book_id` INT NOT NULL,
    `source_character_id` BIGINT NOT NULL,
    `source_character_name` VARCHAR(255) NOT NULL,
    `target_character_id` BIGINT NULL,
    `target_character_name` VARCHAR(255) NOT NULL,
    `volume_index` INT NOT NULL DEFAULT 0,
    `chunk_ids_json` JSON NOT NULL,
    `group_json` JSON NOT NULL,
    `version_hash` VARCHAR(64) NOT NULL,
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY `uniq_character_relation_volume_group` (`book_id`, `source_character_id`, `target_character_name`, `volume_index`),
    INDEX `idx_character_relation_volume_group_version` (`book_id`, `source_character_id`, `version_hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
