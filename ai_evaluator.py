"""
AI 酒店位置评估器
从 config.py 的 CITY_TASKS 读取城市数据，调用 DeepSeek API 评估每个城市
的便利程度（1-10），结果写入 hotels 表中对应城市的所有酒店行。

当 API Key 为占位符时，自动切换到本地启发式评分模式：
  - 综合 review_score、location_score、distance_to_centre 三个维度
  - 无需网络 / API 即可产出 1-10 的 AI 评分

V2 更新：
  - 所有城市评分请求并发发送（asyncio.gather + Semaphore）
  - 异步 OpenAI 客户端（AsyncOpenAI）
  - 增强错误处理：区分网络错误（可重试）与 API 错误（跳过）
  - 指数退避重试机制
"""

import asyncio
import logging
import os
import re
import sqlite3
from pathlib import Path

from config import ScraperConfig

# OpenAI 异步客户端，仅在 API Key 有效时才导入（避免未安装 openai 包时脚本崩溃）
_AsyncOpenAI = None


def _get_async_openai_client_class():
    """懒加载 AsyncOpenAI 客户端类"""
    global _AsyncOpenAI
    if _AsyncOpenAI is None:
        from openai import AsyncOpenAI as _AsyncOpenAI
    return _AsyncOpenAI


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
DEEPSEEK_MODEL = "deepseek-v4-pro"

# 并发控制：同时发送的最大请求数
AI_CONCURRENCY = int(os.getenv("AI_CONCURRENCY", "5"))

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

# 网络错误关键词（可重试的错误类型）
_RETRYABLE_ERRORS = (
    "ConnectionError",
    "Connection reset",
    "Timeout",
    "timeout",
    "Too Many Requests",
    "RateLimitError",
    "ServiceUnavailable",
    "InternalServerError",
    "APIConnectionError",
    "APITimeoutError",
)


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


# ==================== 错误分类 ====================

def _is_retryable_error(error: Exception) -> bool:
    """判断异常是否为可重试的网络错误（而非 API 参数/认证错误）"""
    error_str = str(error)
    # 检查错误消息中是否包含可重试关键词
    for keyword in _RETRYABLE_ERRORS:
        if keyword in error_str:
            return True
    # 检查异常类型名称
    error_type = type(error).__name__
    for keyword in _RETRYABLE_ERRORS:
        if keyword in error_type:
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


# ==================== 异步 API 调用 ====================

async def evaluate_city(
    city_info: dict,
    semaphore: asyncio.Semaphore,
    max_retries: int = 3,
) -> tuple[str, int | None, str | None]:
    """异步调用 DeepSeek API 评估一个城市的便利程度

    返回 (city_name, score, error_message) 三元组：
      - score 为 None 表示评估失败
      - error_message 仅在失败时非空

    错误分类：
      - 可重试错误（网络超时/连接重置/限速）：指数退避重试
      - 不可重试错误（认证失败/参数错误）：立即放弃，不浪费重试次数
    """
    city = city_info.get("city", "Unknown")
    user_message = build_user_message(city_info)

    AsyncOpenAI = _get_async_openai_client_class()
    client = AsyncOpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL,
        timeout=30.0,  # 单次请求超时 30 秒
        default_headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        },
    )

    async with semaphore:
        last_error = None

        for attempt in range(1, max_retries + 1):
            try:
                response = await client.chat.completions.create(
                    model=DEEPSEEK_MODEL,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_message},
                    ],
                    temperature=0.3,
                    max_tokens=4096,
                )

                # 读取模型最终回答文本。DeepSeek V4 思考模式的推理过程存放在
                # reasoning_content 字段，真正的答案仍在 content 字段；max_tokens
                # 太小会导致思考过程耗尽全部额度，content 变成空。
                content = response.choices[0].message.content
                raw = (content or "").strip()

                # 模型返回空文字：打印完整 response 对象，便于定位是额度耗尽
                # 还是字段缺失（而非静默跳过）。
                if not raw:
                    logger.error(
                        f"  ✗ {city} 模型返回空文字，完整响应对象如下:\n{response}"
                    )
                    return (city, None, "模型返回空文字（完整响应已打印到日志）")

                m = re.search(r'\b(10|[1-9])\b', raw)
                if m:
                    score = int(m.group(1))
                    logger.info(f"  ✓ {city} → {score} 分")
                    return (city, score, None)
                else:
                    logger.warning(f"  ⚠ {city} 无法解析分数，原始输出: {raw[:80]}")
                    # 解析失败不是网络错误，直接放弃重试
                    return (city, None, f"无法解析 API 输出: {raw[:80]}")

            except Exception as e:
                last_error = e
                error_str = str(e)

                if _is_retryable_error(e):
                    if attempt < max_retries:
                        backoff = 2 ** attempt  # 指数退避：2s, 4s, 8s
                        logger.warning(
                            f"  ⚠ {city} 网络错误 (第 {attempt}/{max_retries} 次): "
                            f"{type(e).__name__}: {error_str[:80]}"
                        )
                        logger.info(f"    将等待 {backoff}s 后重试 …")
                        await asyncio.sleep(backoff)
                        continue
                    else:
                        logger.error(
                            f"  ✗ {city} 重试 {max_retries} 次后仍然失败: "
                            f"{type(e).__name__}: {error_str[:80]}"
                        )
                else:
                    # 不可重试错误（认证/参数等）：立即放弃
                    logger.error(
                        f"  ✗ {city} API 错误（不可重试）: "
                        f"{type(e).__name__}: {error_str[:120]}"
                    )
                    break  # 不重试

        return (city, None, f"{type(last_error).__name__}: {str(last_error)[:200]}")


# ==================== 主入口 ====================

async def run_async_evaluation(db_path: str, cities: list[dict]):
    """异步并发评估所有城市，返回 (scored, failed, error_details)"""
    semaphore = asyncio.Semaphore(AI_CONCURRENCY)

    logger.info(f"  并发度: {AI_CONCURRENCY} | 城市数: {len(cities)}")

    # 同时发起所有请求，semaphore 控制并发数
    tasks = [evaluate_city(city_info, semaphore) for city_info in cities]
    results = await asyncio.gather(*tasks)

    scored = 0
    failed = 0
    error_details: list[tuple[str, str]] = []

    for city_name, score, error_msg in results:
        if score is not None:
            updated = update_ai_score_for_city(db_path, city_name, score)
            logger.info(f"  → 已更新 {updated} 条 {city_name} 酒店记录")
            scored += 1
        else:
            logger.warning(f"  → 最终失败，跳过 {city_name}")
            if error_msg:
                error_details.append((city_name, error_msg))
            failed += 1

    return scored, failed, error_details


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
        logger.warning("  如需 AI 评分，请在环境变量中配置真实 DEEPSEEK_API_KEY")
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

    # 3. 异步并发评分
    logger.info(f"\n{'=' * 40}")
    logger.info(f"  开始并发 AI 评分 (V2 · Async · DeepSeek)")
    logger.info(f"{'=' * 40}")

    scored, failed, error_details = asyncio.run(
        run_async_evaluation(db_path, cities)
    )

    # 4. 汇总报告
    logger.info(f"\n{'=' * 40}")
    logger.info(f"  评估完成：成功 {scored} 个城市，失败 {failed} 个")
    if error_details:
        logger.info(f"  失败详情:")
        for city_name, err in error_details:
            logger.info(f"    - {city_name}: {err[:150]}")
    logger.info(f"  数据库: {db_path}")
    logger.info(f"{'=' * 40}")


if __name__ == "__main__":
    main()
