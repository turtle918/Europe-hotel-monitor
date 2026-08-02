"""
机票价格抓取模块 V2
使用 Playwright (async) 从 Google Flights 抓取机票价格

支持的航线（来自 config.FLIGHT_ROUTES）：
  - 香港 → 法兰克福  (HKG → FRA)
  - 佛罗伦萨/比萨 → 巴塞罗那  (FLR/PSA → BCN)
  - 马德里 → 香港  (MAD → HKG)

数据存入 SQLite flights 表，与酒店数据共用 booking_data.db。

V2 更新：
  - 全异步 Playwright async_api 实现
  - 固定等待 (wait_for_timeout) → 智能等待 (wait_for_load_state + wait_for_selector)
  - 支持多航线并发抓取 (flight_concurrency 配置项)
"""

import asyncio
import logging
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page, Browser, BrowserContext
from playwright.async_api import TimeoutError as PlaywrightTimeout

from config import ScraperConfig
from database import init_flights_table, insert_flight_records, get_flight_record_count

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("FlightScraper")

# Google Flights base URL
GF_BASE = "https://www.google.com/travel/flights"

# SQLite 写入锁（并发安全）
_db_lock = asyncio.Lock()


class FlightScraper:
    """Google Flights 机票价格爬虫（异步版）"""

    def __init__(self, config: ScraperConfig):
        self.cfg = config
        self.pw = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.results: list[dict] = []
        self._start_time: str = ""

    # ==================== 工具方法 ====================

    async def _rand_delay(self, lo: float = 0.5, hi: float = 2.0):
        """随机异步延迟，模拟人类操作节奏"""
        await asyncio.sleep(random.uniform(lo, hi))

    async def _debug_screenshot(self, name: str):
        """保存调试截图"""
        if not self.page:
            return
        path = Path(f"debug_flight_{name}_{datetime.now():%H%M%S}.png")
        try:
            await self.page.screenshot(path=str(path), full_page=False)
            logger.debug(f"截图已保存: {path}")
        except Exception:
            pass

    # ==================== 浏览器启动 ====================

    async def _launch_browser(self):
        """启动浏览器"""
        logger.info("▸ 启动浏览器 …")
        launch_kwargs: dict = {
            "headless": self.cfg.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        }
        if self.cfg.use_local_chrome:
            try:
                launch_kwargs["channel"] = "msedge"
            except Exception:
                pass

        self.browser = await self.pw.chromium.launch(**launch_kwargs)
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="Asia/Hong_Kong",
        )
        self.page = await self.context.new_page()

    # ==================== 构建搜索 URL ====================

    @staticmethod
    def _build_flight_url(route: dict) -> str:
        """构建 Google Flights 搜索 URL"""
        origin = route["origin"].replace(" ", "+")
        dest = route["destination"].replace(" ", "+")
        date = route["date"]
        q = f"Flights+to+{dest}+from+{origin}+on+{date}"
        url = f"{GF_BASE}?q={q}&curr=CNY"
        return url

    # ==================== 搜索执行 ====================

    async def _search_route(self, route: dict):
        """导航到 Google Flights 航线搜索结果页"""
        origin = route["origin"]
        dest = route["destination"]
        date = route["date"]
        logger.info(f"  ✈ 搜索: {origin} → {dest} | {date}")

        search_url = self._build_flight_url(route)
        logger.info(f"  搜索 URL: {search_url[:150]}…")

        await self.page.goto(search_url, wait_until="domcontentloaded", timeout=60000)

        # ---- 智能等待：替换固定 8s 等待 ----
        try:
            await self.page.wait_for_load_state(
                "networkidle", timeout=self.cfg.smart_wait_timeout
            )
            logger.info("  ✓ 页面网络空闲")
        except PlaywrightTimeout:
            logger.warning(
                f"  ⚠ networkidle 超时 ({self.cfg.smart_wait_timeout}ms)，继续尝试 …"
            )

        # 验证页面内容已渲染：等待任意 span/div/li 元素出现
        try:
            await self.page.wait_for_selector(
                "span, div, li",
                state="attached",
                timeout=10_000,
            )
            await asyncio.sleep(1.0)  # 短暂沉降让 JS 完成渲染
            logger.info("  ✓ 页面元素已渲染")
        except PlaywrightTimeout:
            logger.warning("  ⚠ 未检测到内容元素，可能页面加载不完整")
            await asyncio.sleep(3.0)  # 回退等待

    # ==================== 数据提取 ====================

    @staticmethod
    def _parse_flight_price(text: str) -> Optional[float]:
        """从价格文本中提取数值"""
        if not text:
            return None
        m = re.search(r'(?:CN¥|¥|€|US\$|£|HK\$)?\s*([\d,]+(?:\.\d{1,2})?)', text)
        if m:
            return float(m.group(1).replace(",", ""))
        return None

    async def _extract_flight_prices(self, route: dict) -> list[dict]:
        """从 Google Flights 结果页提取航班价格"""
        await self._rand_delay(2, 3)

        flights_found: list[dict] = []
        origin = route["origin"]
        dest = route["destination"]
        date = route["date"]

        try:
            # 扫描页面中所有包含价格模式的文本
            all_spans = await self.page.query_selector_all("span, div, li")
            candidate_prices = []
            for el in all_spans:
                try:
                    text = ((await el.inner_text()) or "").strip()
                except Exception:
                    continue
                if not text:
                    continue
                m = re.search(
                    r'(?:CN¥|¥|HK\$|€|US\$)\s*([\d,]{3,6}(?:\.\d{1,2})?)',
                    text
                )
                if m:
                    price_val = float(m.group(1).replace(",", ""))
                    if 500 <= price_val <= 100000:
                        candidate_prices.append({
                            "price_text": text.strip()[:100],
                            "price_num": price_val,
                        })
        except Exception:
            candidate_prices = []

        # 去重（按价格数值）
        seen_prices = set()
        unique_flights = []
        for item in candidate_prices:
            if item["price_num"] not in seen_prices:
                seen_prices.add(item["price_num"])
                unique_flights.append(item)

        # 只保留价格最低的前 5 条
        unique_flights.sort(key=lambda x: x["price_num"])
        unique_flights = unique_flights[:5]

        for item in unique_flights:
            record = {
                "origin": origin,
                "destination": dest,
                "flight_date": date,
                "price_cny": str(item["price_num"]),
                "price_num": item["price_num"],
                "airline_info": item["price_text"],
                "cabin_class": route.get("cabin_class", "economy"),
                "adults": route.get("adults", 2),
                "children": route.get("children", 1),
                "scraped_at": self._start_time,
            }
            flights_found.append(record)
            logger.info(
                f"    ✈ {origin} → {dest} | "
                f"¥{item['price_num']:,.0f} | {item['price_text'][:50]}"
            )

        # 如果上述方案都没有找到，尝试直接读取页面可见文本提取价格
        if not flights_found:
            logger.info("  尝试从页面全文提取价格 …")
            try:
                body_text = await self.page.inner_text("body")
                price_pattern = re.findall(
                    r'(?:CN¥|¥|HK\$)\s*([\d,]{3,6}(?:\.\d{1,2})?)',
                    body_text
                )
                seen = set()
                for p in price_pattern:
                    val = float(p.replace(",", ""))
                    if 500 <= val <= 100000 and val not in seen:
                        seen.add(val)
                        record = {
                            "origin": origin,
                            "destination": dest,
                            "flight_date": date,
                            "price_cny": str(val),
                            "price_num": val,
                            "airline_info": "页面全文提取",
                            "cabin_class": route.get("cabin_class", "economy"),
                            "adults": route.get("adults", 2),
                            "children": route.get("children", 1),
                            "scraped_at": self._start_time,
                        }
                        flights_found.append(record)
                        logger.info(f"    ✈ [全文] {origin} → {dest} | ¥{val:,.0f}")
            except Exception as e:
                logger.error(f"  全文提取失败: {e}")

        return flights_found

    # ==================== 主流程 ====================

    async def run(self):
        """执行完整机票抓取流程"""
        self._start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if not self.cfg.flight_scrape_enabled:
            logger.info("机票抓取已禁用 (flight_scrape_enabled = False)")
            return

        routes = self.cfg.FLIGHT_ROUTES
        if not routes:
            logger.info("没有配置航线，跳过机票抓取")
            return

        # ---- 日期范围过滤 ----
        date_min = self.cfg.SCRAPE_DATE_MIN
        date_max = self.cfg.SCRAPE_DATE_MAX

        valid_routes = []
        for route in routes:
            flight_date = route.get("date", "")
            if flight_date < date_min or flight_date > date_max:
                logger.warning(
                    f"  ⚠ 跳过 {route['origin']}→{route['destination']}: "
                    f"日期 {flight_date} 超出允许范围 "
                    f"({date_min} ~ {date_max})"
                )
            else:
                valid_routes.append(route)

        if not valid_routes:
            logger.warning("  ⚠ 所有航线均超出日期范围，无任务可执行")
            return
        routes = valid_routes

        db_path = init_flights_table(self.cfg.flight_db_file)

        logger.info(f"\n{'=' * 60}")
        logger.info(f"  机票价格爬虫启动 (V2 · Async · Google Flights)")
        logger.info(f"  共 {len(routes)} 条航线 | 并发度: {self.cfg.flight_concurrency}")
        logger.info(f"  日期范围: {date_min} ~ {date_max}")
        logger.info(f"  货币: {self.cfg.flight_currency}")
        logger.info(f"{'=' * 60}\n")

        async with async_playwright() as p:
            self.pw = p
            try:
                await self._launch_browser()

                sem = asyncio.Semaphore(self.cfg.flight_concurrency)

                async def _scrape_one_route(route: dict, idx: int) -> list[dict]:
                    """在独立的 page 中抓取单条航线（支持并发）"""
                    async with sem:
                        logger.info(f"\n{'─' * 40}")
                        logger.info(
                            f"  航线 {idx + 1}/{len(routes)}: "
                            f"{route['origin']} → {route['destination']} | "
                            f"{route['date']}"
                        )
                        logger.info(f"{'─' * 40}")

                        # 每条航线使用独立 page 以保证并发安全
                        page = await self.context.new_page()
                        try:
                            saved_page = self.page
                            self.page = page
                            await self._search_route(route)
                            flights = await self._extract_flight_prices(route)
                            if flights:
                                async with _db_lock:
                                    insert_flight_records(db_path, flights)
                            return flights
                        finally:
                            self.page = saved_page
                            await page.close()

                # 并发执行所有航线
                all_results = await asyncio.gather(*[
                    _scrape_one_route(r, i) for i, r in enumerate(routes)
                ])

                for result_list in all_results:
                    self.results.extend(result_list)

                total_db = get_flight_record_count(db_path)
                logger.info(f"\n{'=' * 60}")
                logger.info(f"  ✅ 机票抓取完成！")
                logger.info(f"  本次新增: {len(self.results)} 条")
                logger.info(f"  数据库总计: {total_db} 条 → {db_path}")
                logger.info(f"{'=' * 60}")

            except KeyboardInterrupt:
                logger.info("\n⚠ 用户中断")
            except Exception as e:
                logger.error(f"机票抓取出错: {e}", exc_info=True)
            finally:
                await self._cleanup()

    async def _cleanup(self):
        """释放浏览器资源"""
        logger.info("清理资源 …")
        for obj in [self.page, self.context, self.browser]:
            try:
                if obj:
                    await obj.close()
            except Exception:
                pass


def main():
    config = ScraperConfig()
    scraper = FlightScraper(config)
    asyncio.run(scraper.run())


if __name__ == "__main__":
    main()
