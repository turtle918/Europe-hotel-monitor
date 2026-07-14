"""
AI 酒店位置评估器
从 config.py 的 CITY_TASKS 读取城市数据，调用 DeepSeek API 评估每个城市
的便利程度（1-10），结果写入 hotels 表中对应城市的所有酒店行。

当 API Key 为占位符时，自动切换到本地启发式评分模式：
  - 综合 review_score、location_score、distance_to_centre 三个维度
  - 无需网络 / API 即可产出 1-10 的 AI 评分
"""

import logging
import os
import re
import sqlite3
import time
from pathlib import Path

from config import ScraperConfig

# OpenAI 客户端仅在 API Key 有效时才导入（避免未安装 openai 包时脚本崩溃）
_OpenAI = None


def _get_openai_client():
    """懒加载 OpenAI 客户端类"""
    global _OpenAI
    if _OpenAI is None:
        from openai import OpenAI as _OpenAI
    return _OpenAI

# ==================== 日志 ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("AIEvaluator")

# ==================== DeepSeek 配置 ====================
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")  # 从环境变量读取
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

# ==================== 提示词 ====================
SYSTEM_PROMPT = (
    "你是一个旅游专家。用户会提供酒店名字和地址。"
    "你需要评价这家酒店距离市中心、车站和餐厅的方便程度。"
    "你直接输出一个1到10的数字。你不要输出其他文字。"
)

# 占位符 API Key 特征（用于检测用户是否已配置真实 Key）
_PLACEHOLDER_PATTERNS = [
    "sk-xxxxxxxx",
    "your-api-key",
    "your_api_key",
    "sk-your-",
    "placeholder",
    "替换为你的",
]


def get_db_path(db_file: str = "booking_data.db") -> str:
    """返回数据库的绝对路径"""
    return str(Path(__file__).parent / db_file)


def ensure_ai_score_column(db_path: str):
    """如果 ai_score 列不存在则添加"""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute("PRAGMA table_info(hotels)")
        columns = {row[1] for row in cursor.fetchall()}
        if "ai_score" not in columns:
            conn.execute("ALTER TABLE hotels ADD COLUMN ai_score TEXT")
            conn.commit()
            logger.info("⚡ 数据库迁移：新增 hotels.ai_score 列")
    finally:
        conn.close()


def get_unique_cities_from_tasks() -> list[dict]:
    """从 config.py 的 CITY_TASKS 中提取去重后的城市列表"""
    config = ScraperConfig()
    tasks = config.CITY_TASKS

    seen = set()
    cities = []
    for task in tasks:
        city_name = task.get("city", "")
        if city_name and city_name not in seen:
            seen.add(city_name)
            cities.append({
                "city": city_name,
                "notes": task.get("notes", ""),
            })
    return cities


def _is_placeholder_api_key(key: str) -> bool:
    """检测 API Key 是否为占位符（未配置真实 Key）"""
    if not key or key.strip() == "":
        return True
    key_lower = key.lower()
    for pat in _PLACEHOLDER_PATTERNS:
        if pat.lower() in key_lower:
            return True
    return False


# ==================== 本地启发式评分（API Key 不可用时的后备方案） ====================

def _parse_float(val) -> float | None:
    """安全解析浮点数"""
    if val is None:
        return None
    try:
        return float(str(val).replace(",", "").replace("¥", "").strip())
    except (ValueError, TypeError):
        return None


def _parse_distance_miles(dist_str) -> float | None:
    """从 distance_to_centre 字符串中提取英里数

    示例：
      "1.1 miles from downtown" → 1.1
      "0.5 km from centre" → 0.31  (转换为英里)
      "500 m from centre" → 0.31
    """
    if not dist_str or dist_str == "N/A":
        return None
    s = str(dist_str).lower().strip()
    # miles
    m = re.search(r'([\d.]+)\s*miles?', s)
    if m:
        return float(m.group(1))
    # km
    m = re.search(r'([\d.]+)\s*km', s)
    if m:
        return float(m.group(1)) * 0.621371
    # meters
    m = re.search(r'([\d,.]+)\s*m\b', s)
    if m:
        return float(m.group(1).replace(",", "")) * 0.000621371
    # bare number (assume miles if < 50)
    m = re.search(r'([\d.]+)', s)
    if m:
        val = float(m.group(1))
        if val < 50:
            return val
    return None


def heuristic_score_for_hotel(row: dict) -> float:
    """基于酒店现有数据计算启发式 AI 评分 (1-10)

    综合三个维度：
      1. review_score (50%) — Booking 综合评分
      2. location_score (25%) — 位置评分
      3. distance_to_centre (25%) — 距市中心距离

    返回 1-10 的浮点数。
    """
    parts = []
    weights = []

    # 维度 1: review_score (Booking 评分，通常 0-10 或 1-10)
    rev = _parse_float(row.get("review_score"))
    if rev is not None and 0 < rev <= 10:
        parts.append(rev)
        weights.append(0.50)
    elif rev is not None and rev > 10:
        # 有些评分是百分制，缩放到 1-10
        parts.append(max(1.0, min(10.0, rev / 10.0)))
        weights.append(0.50)

    # 维度 2: location_score
    loc = _parse_float(row.get("location_score"))
    if loc is not None and 0 < loc <= 10:
        parts.append(loc)
        weights.append(0.25)
    elif loc is not None and loc > 10:
        parts.append(max(1.0, min(10.0, loc / 10.0)))
        weights.append(0.25)

    # 维度 3: distance_to_centre（越近分越高）
    dist = _parse_distance_miles(row.get("distance_to_centre"))
    if dist is not None:
        # 距离 → 分数映射（英里）
        # ≤0.3mi → 10, ≤0.5mi → 9, ≤1mi → 8, ≤1.5mi → 7,
        # ≤2mi → 6, ≤3mi → 5, ≤5mi → 4, ≤10mi → 3, >10mi → 2
        if dist <= 0.3:
            dist_score = 10.0
        elif dist <= 0.5:
            dist_score = 9.0
        elif dist <= 1.0:
            dist_score = 8.0
        elif dist <= 1.5:
            dist_score = 7.0
        elif dist <= 2.0:
            dist_score = 6.0
        elif dist <= 3.0:
            dist_score = 5.0
        elif dist <= 5.0:
            dist_score = 4.0
        elif dist <= 10.0:
            dist_score = 3.0
        else:
            dist_score = 2.0
        parts.append(dist_score)
        weights.append(0.25)

    if not parts:
        return 5.0  # 无数据时返回中等分数

    # 加权平均，权重归一化
    total_weight = sum(weights)
    if total_weight == 0:
        return 5.0
    weighted_sum = sum(p * w for p, w in zip(parts, weights))
    normalized = weighted_sum / total_weight

    return round(max(1.0, min(10.0, normalized)), 1)


def run_heuristic_scoring(db_path: str):
    """本地启发式评分：逐行计算并写入 ai_score"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM hotels").fetchall()
        updated = 0
        for row in rows:
            score = heuristic_score_for_hotel(dict(row))
            conn.execute(
                "UPDATE hotels SET ai_score = ? WHERE id = ?",
                (str(score), row["id"]),
            )
            updated += 1
        conn.commit()
        logger.info(f"✅ 本地启发式评分完成：{updated} 条记录已更新")
        return updated
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def count_hotels_by_city(db_path: str, city: str) -> int:
    """统计指定城市的酒店记录数"""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM hotels WHERE city = ?", (city,)
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def update_ai_score_for_city(db_path: str, city: str, score: int) -> int:
    """将 AI 评分写入指定城市的所有酒店行，返回更新的行数"""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "UPDATE hotels SET ai_score = ? WHERE city = ?",
            (str(score), city),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def build_user_message(city_info: dict) -> str:
    """根据城市信息构建发送给 AI 的查询文本"""
    city = city_info.get("city", "Unknown")
    notes = city_info.get("notes", "")
    parts = [f"酒店名称: {city}"]
    if notes:
        parts.append(f"地址描述: {notes}")
    return "\n".join(parts)


def evaluate_city(city_info: dict, retries: int = 3) -> int | None:
    """调用 DeepSeek API 评估一个城市的便利程度，返回 1-10 的整数分数"""
    user_message = build_user_message(city_info)
    city = city_info.get("city", "Unknown")

    OpenAI = _get_openai_client()
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL,
    )

    for attempt in range(1, retries + 1):
        try:
            response = client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.3,
                max_tokens=10,
            )

            raw = response.choices[0].message.content.strip()
            m = re.search(r'\b(10|[1-9])\b', raw)
            if m:
                score = int(m.group(1))
                logger.info(f"  ✓ {city} → {score} 分")
                return score
            else:
                logger.warning(f"  ⚠ {city} 无法解析分数，原始输出: {raw[:80]}")
                if attempt < retries:
                    time.sleep(1)

        except Exception as e:
            logger.error(f"  ✗ {city} API 调用失败 (第 {attempt} 次): {e}")
            if attempt < retries:
                time.sleep(2 ** attempt)

    return None


def main():
    db_path = get_db_path()

    # 1. 确保 ai_score 列存在
    ensure_ai_score_column(db_path)

    # 2. 从 config.py 的 CITY_TASKS 中读取城市数据
    cities = get_unique_cities_from_tasks()
    logger.info(f"从 CITY_TASKS 读取到 {len(cities)} 个城市:\n")
    for i, c in enumerate(cities):
        hotel_count = count_hotels_by_city(db_path, c["city"])
        logger.info(f"  {i + 1}. {c['city']} ({c['notes']}) — 数据库 {hotel_count} 条记录")

    # ---- 检测 API Key 是否为占位符 ----
    if _is_placeholder_api_key(DEEPSEEK_API_KEY):
        logger.warning("=" * 50)
        logger.warning("⚠️  DeepSeek API Key 为占位符，将使用本地启发式评分模式")
        logger.warning("  如需 AI 评分，请在 ai_evaluator.py 中配置真实 API Key")
        logger.warning("  当前将综合 review_score + location_score + distance_to_centre 计算评分")
        logger.warning("=" * 50)
        run_heuristic_scoring(db_path)
        logger.info(f"\n{'=' * 40}")
        logger.info(f"  启发式评分完成")
        logger.info(f"  数据库: {db_path}")
        logger.info(f"{'=' * 40}")
        return

    if not cities:
        logger.info("CITY_TASKS 为空，退出。")
        return

    # 3. 逐城市评分并批量写入对应酒店
    scored = 0
    failed = 0
    for i, city_info in enumerate(cities):
        city = city_info["city"]
        logger.info(f"\n[{i + 1}/{len(cities)}] 评估: {city} ({city_info['notes']})")

        score = evaluate_city(city_info)
        if score is not None:
            updated = update_ai_score_for_city(db_path, city, score)
            logger.info(f"  → 已更新 {updated} 条酒店记录")
            scored += 1
        else:
            logger.warning(f"  → 最终失败，跳过 {city}")
            failed += 1

        # API 限速保护
        if i < len(cities) - 1:
            time.sleep(1)

    logger.info(f"\n{'=' * 40}")
    logger.info(f"  评估完成：成功 {scored} 个城市，失败 {failed} 个")
    logger.info(f"  数据库: {db_path}")
    logger.info(f"{'=' * 40}")


if __name__ == "__main__":
    main()
