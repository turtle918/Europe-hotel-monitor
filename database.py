"""
SQLite 数据库模块
负责创建数据表、写入爬取结果、查询历史数据

V4 更新：
  - 移除 price_per_score 列（不再计算性价比）
  - 新增 location_score 列（位置评分）
  - 新增 distance_to_centre 列（距市中心距离 / 地理位置描述）
  - 自动迁移旧数据库结构
"""

import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger("BookingScraper.DB")


# ==================== 数据库初始化 ====================

def init_db(db_path: str = "booking_data.db") -> str:
    """初始化 SQLite 数据库，创建表（如不存在），返回数据库绝对路径"""
    abs_path = str(Path(db_path).resolve())

    conn = sqlite3.connect(abs_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hotels (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                hotel_name         TEXT    NOT NULL,
                price_cny          TEXT,
                room_type          TEXT,
                review_score       TEXT,
                detail_link        TEXT,
                location_desc      TEXT,
                location_score     TEXT,
                distance_to_centre TEXT,
                ai_score           TEXT,
                city               TEXT,
                checkin            TEXT,
                checkout           TEXT,
                scraped_at         TEXT    NOT NULL
            )
        """)

        # ---- 向前兼容迁移 ----
        _migrate_add_column(conn, "hotels", "location_desc", "TEXT")
        _migrate_add_column(conn, "hotels", "location_score", "TEXT")
        _migrate_add_column(conn, "hotels", "distance_to_centre", "TEXT")
        _migrate_add_column(conn, "hotels", "ai_score", "TEXT")

        # price_usd → price_cny 列重命名（向前兼容旧数据库）
        _migrate_rename_column(conn, "hotels", "price_usd", "price_cny")

        # 为常用查询字段建索引
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scraped_at
            ON hotels(scraped_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_hotel_name
            ON hotels(hotel_name)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_city_checkin
            ON hotels(city, checkin)
        """)
        conn.commit()
        logger.info(f"✓ 数据库已就绪: {abs_path}")
    finally:
        conn.close()

    return abs_path


def _migrate_add_column(conn: sqlite3.Connection, table: str,
                        column: str, col_type: str):
    """如果列不存在则添加（向前兼容旧数据库）"""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        logger.info(f"⚡ 数据库迁移：新增 {table}.{column} 列")


def _migrate_rename_column(conn: sqlite3.Connection, table: str,
                           old_name: str, new_name: str):
    """如果旧列名存在且新列名不存在，则重命名列（SQLite 3.25+）"""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    columns = {row[1] for row in cursor.fetchall()}
    if old_name in columns and new_name not in columns:
        conn.execute(
            f"ALTER TABLE {table} RENAME COLUMN {old_name} TO {new_name}"
        )
        logger.info(f"⚡ 数据库迁移：{old_name} → {new_name}")


# ==================== 数据写入 ====================

def insert_records(db_path: str, records: list[dict]) -> int:
    """批量插入爬取记录，返回插入条数"""
    if not records:
        return 0

    conn = sqlite3.connect(db_path)
    try:
        sql = """
            INSERT INTO hotels
                (hotel_name, price_cny, room_type, review_score,
                 detail_link, location_desc, location_score, distance_to_centre,
                 city, checkin, checkout, scraped_at)
            VALUES
                (:hotel_name, :price_cny, :room_type, :review_score,
                 :detail_link, :location_desc, :location_score, :distance_to_centre,
                 :city, :checkin, :checkout, :scraped_at)
        """
        conn.executemany(sql, records)
        conn.commit()
        count = conn.total_changes
        logger.info(f"✓ 已写入数据库 {count} 条记录")
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ==================== 数据查询 ====================

def get_record_count(db_path: str) -> int:
    """查询数据库中的总记录数"""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM hotels").fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def get_latest_scrape_time(db_path: str) -> Optional[str]:
    """查询最近一次抓取的时间"""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(scraped_at) FROM hotels"
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_all_records(db_path: str, limit: int = 100) -> list[dict]:
    """查询最近抓取的所有记录"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM hotels ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
