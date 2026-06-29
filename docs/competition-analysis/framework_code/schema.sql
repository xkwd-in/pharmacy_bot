-- 智慧药房 SPS SQLite Schema
-- 用法：sqlite3 pharmacy.db < schema.sql

PRAGMA foreign_keys = ON;

-- 药品主表
CREATE TABLE IF NOT EXISTS drugs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    barcode         TEXT UNIQUE NOT NULL,             -- 条码（EAN/UPC）
    name            TEXT NOT NULL,                    -- 中文药名
    spec            TEXT,                              -- 规格 "0.25g x 24 片"
    category        TEXT,                              -- 大类（OTC甲/OTC乙/Rx）
    indication      TEXT,                              -- 适应症摘要
    contraindication TEXT,                             -- 禁忌
    storage_position TEXT,                              -- 货架位置 "A03-12"
    image_path      TEXT,                              -- 训练样本图路径
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 库存表
CREATE TABLE IF NOT EXISTS inventory (
    drug_id         INTEGER PRIMARY KEY,
    qty_on_hand     INTEGER NOT NULL DEFAULT 0,
    qty_threshold   INTEGER NOT NULL DEFAULT 20,      -- 低位阈值，低于则补仓
    expiry_date     DATE,
    last_count_at   DATETIME,
    FOREIGN KEY (drug_id) REFERENCES drugs(id)
);

-- 处方表（医师 / AI 辅助生成）
CREATE TABLE IF NOT EXISTS prescriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id      TEXT,
    patient_age     INTEGER,
    chief_complaint TEXT,                              -- 主诉
    ai_suggestion   TEXT,                              -- AI 建议 JSON
    final_drugs     TEXT,                              -- 药师最终确认 JSON
    pharmacist_id   TEXT NOT NULL,                    -- 必须有人类药师签名
    status          TEXT CHECK(status IN ('pending','confirmed','dispensed','rejected')),
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    confirmed_at    DATETIME
);

-- 调剂记录
CREATE TABLE IF NOT EXISTS dispense_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    prescription_id INTEGER NOT NULL,
    drug_id         INTEGER NOT NULL,
    qty             INTEGER NOT NULL,
    vision_score    REAL,                              -- 三模态融合置信度
    arm_success     BOOLEAN,
    dispensed_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (prescription_id) REFERENCES prescriptions(id),
    FOREIGN KEY (drug_id) REFERENCES drugs(id)
);

-- 补仓提示
CREATE TABLE IF NOT EXISTS restock_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    drug_id         INTEGER NOT NULL,
    alert_qty       INTEGER NOT NULL,
    status          TEXT CHECK(status IN ('new','sent','ordered','received')),
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (drug_id) REFERENCES drugs(id)
);

-- 演示用 seed 数据（评委演示时直接看得到）
INSERT OR IGNORE INTO drugs (barcode, name, spec, category, storage_position) VALUES
  ('6901234567890', '布洛芬缓释胶囊', '0.3g x 20 粒', 'OTC甲', 'A01-03'),
  ('6901234567891', '对乙酰氨基酚片',  '0.5g x 12 片', 'OTC甲', 'A01-04'),
  ('6901234567892', '阿莫西林胶囊',    '0.25g x 24 粒','Rx',     'B02-15'),
  ('6901234567893', '复方甘草片',      '12 片',         'OTC乙', 'A02-08'),
  ('6901234567894', '感冒灵颗粒',      '10g x 9 袋',    'OTC甲', 'A01-12');

INSERT OR IGNORE INTO inventory (drug_id, qty_on_hand, qty_threshold, expiry_date) VALUES
  (1, 100, 30, '2027-06-01'),
  (2, 80,  20, '2026-12-31'),
  (3, 15,  20, '2027-03-15'),    -- 故意低于阈值，演示补仓提示
  (4, 50,  20, '2027-09-30'),
  (5, 200, 50, '2026-10-01');
