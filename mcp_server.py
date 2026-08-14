"""
MCP Server —— 为 AI 模型暴露 Booking 酒店数据查询工具

工具列表:
  - get_lowest_price  按城市 + 日期查询最便宜的 5 个房源
  - get_hotel_history 按酒店名称查询所有历史价格记录

启动方式:
  python mcp_server.py
"""

import sqlite3
import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ==================== 日志 ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("BookingMCP")

# ==================== 服务器初始化 ====================
mcp = FastMCP(
    name="Booking Hotel Monitor",
    description="查询 Booking.com 酒店/公寓价格数据库，支持按城市、日期和酒店名称检索",
)

# 数据库路径（与爬虫共用同一个 SQLite 文件）
DB_PATH = str(Path(__file__).resolve().parent / "booking_data.db")


# ==================== 辅助函数 ====================

def _get_connection() -> sqlite3.Connection:
    """获取只读数据库连接"""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    """将 sqlite3.Row 转为普通字典"""
    return dict(row)


def _normalize_price(price_str: str) -> float:
    """将价格字符串转为浮点数（去除逗号等）"""
    if not price_str or price_str == "N/A":
        return float("inf")  # 无价格的排到最后
    try:
        return float(price_str.replace(",", ""))
    except (ValueError, AttributeError):
        return float("inf")


# ==================== 工具定义 ====================

@mcp.tool()
def get_lowest_price(city: str, date: str) -> list[dict]:
    """查询指定城市和入住日期下，价格最便宜的 5 个房源。

    参数
    ----
    city : str
        城市名称（例如 "Paris"、"London"），需与数据库中的 city 字段匹配。
    date : str
        入住日期，格式 YYYY-MM-DD（例如 "2026-08-01"）。

    返回
    ----
    list[dict]
        最多 5 条记录，按价格从低到高排序。每条记录包含：
        - hotel_name   : 酒店/公寓名称
        - price_usd    : 价格（美元）
        - room_type    : 房型
        - review_score : 评分
        - detail_link  : 详情页链接
        - checkin      : 入住日期
        - checkout     : 退房日期
    """
    logger.info(f"查询最低价: city={city!r}, date={date!r}")

    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT hotel_name, price_usd, room_type, review_score,
                   detail_link, location_desc,
                   city, checkin, checkout
            FROM hotels
            WHERE city = ? AND checkin = ?
            ORDER BY id DESC
            """,
            (city, date),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        logger.info(f"  无匹配记录")
        return []

    # 转为字典列表，按价格数值排序取前 5
    records = [_row_to_dict(r) for r in rows]
    records.sort(key=lambda r: _normalize_price(r.get("price_usd", "")))
    top5 = records[:5]

    logger.info(f"  返回 {len(top5)} 条记录")
    return top5


@mcp.tool()
def get_hotel_history(hotel_name: str) -> list[dict]:
    """查询指定酒店在数据库中的所有历史价格记录。

    参数
    ----
    hotel_name : str
        酒店或公寓名称（支持模糊匹配，例如 "Tour Eiffel" 可匹配
        "Appart Tour Eiffel / Champs de Mars"）。

    返回
    ----
    list[dict]
        该酒店的所有历史记录，按抓取时间从新到旧排序。每条记录包含：
        - hotel_name   : 酒店/公寓名称
        - price_usd    : 价格（美元）
        - room_type    : 房型
        - review_score : 评分
        - detail_link  : 详情页链接
        - city         : 城市
        - checkin      : 入住日期
        - checkout     : 退房日期
        - scraped_at   : 抓取时间
    """
    logger.info(f"查询酒店历史: hotel_name={hotel_name!r}")

    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT hotel_name, price_usd, room_type, review_score,
                   detail_link, location_desc,
                   city, checkin, checkout, scraped_at
            FROM hotels
            WHERE hotel_name LIKE ?
            ORDER BY scraped_at DESC
            """,
            (f"%{hotel_name}%",),
        ).fetchall()
    finally:
        conn.close()

    records = [_row_to_dict(r) for r in rows]
    logger.info(f"  返回 {len(records)} 条记录")
    return records


# ==================== 入口 ====================

if __name__ == "__main__":
    logger.info(f"数据库路径: {DB_PATH}")
    if not Path(DB_PATH).exists():
        logger.warning(
            f"⚠ 数据库文件不存在: {DB_PATH}\n"
            f"  请先运行 booking_scraper.py 爬取数据。"
        )
    mcp.run()
