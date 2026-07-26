"""
机票价格抓取模块 V1
使用 Playwright 从 Google Flights 抓取机票价格

支持的航线（来自 config.FLIGHT_ROUTES）：
  - 香港 → 法兰克福  (HKG → FRA)
  - 佛罗伦萨/比萨 → 巴塞罗那  (FLR/PSA → BCN)
  - 马德里 → 香港  (MAD → HKG)

数据存入 SQLite flights 表，与酒店数据共用 booking_data.db。
"""

import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext
from playwright.sync_api import TimeoutError as PlaywrightTimeout

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


class FlightScraper:
    """Google Flights 机票价格爬虫"""

    def __init__(self, config: ScraperConfig):
        self.cfg = config
        self.pw = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.results: list[dict] = []
        self._start_time: str = ""

    # ==================== 工具方法 ====================

    def _rand_delay(self, lo: float = 0.5, hi: float = 2.0):
        time.sleep(random.uniform(lo, hi))

    def _debug_screenshot(self, name: str):
        if not self.page:
            return
        path = Path(f"debug_flight_{name}_{datetime.now():%H%M%S}.png")
        try:
            self.page.screenshot(path=str(path), full_page=False)
            logger.debug(f"截图已保存: {path}")
        except Exception:
            pass

    # ==================== 浏览器启动 ====================

    def _launch_browser(self):
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

        self.browser = self.pw.chromium.launch(**launch_kwargs)
        self.context = self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="Asia/Hong_Kong",
        )
        self.page = self.context.new_page()

    # ==================== 构建搜索 URL ====================

    def _build_flight_url(self, route: dict) -> str:
        """构建 Google Flights 搜索 URL

        Google Flights URL 格式示例：
        https://www.google.com/travel/flights?q=
          Flights+to+BCN+from+FLR+on+2026-08-15
        """
        origin = route["origin"].replace(" ", "+")
        dest = route["destination"].replace(" ", "+")
        date = route["date"]

        # Google Flights 查询格式: "Flights to {dest} from {origin} on {date}"
        q = f"Flights+to+{dest}+from+{origin}+on+{date}"
        url = f"{GF_BASE}?q={q}&curr={self.cfg.flight_currency}"
        return url

    # ==================== 搜索执行 ====================

    def _search_route(self, route: dict):
        """导航到 Google Flights 航线搜索结果页"""
        origin = route["origin"]
        dest = route["destination"]
        date = route["date"]
        logger.info(f"  ✈ 搜索: {origin} → {dest} | {date}")

        search_url = self._build_flight_url(route)
        logger.info(f"  搜索 URL: {search_url[:150]}…")
        try:
            self.page.goto(search_url, wait_until="commit", timeout=60000)
            self.page.wait_for_timeout(8000)
            logger.info("  ✓ 等待 8 秒让页面渲染完毕")
        except Exception as e:
            logger.warning(f"  ⚠ 页面加载异常: {e}")
            self._rand_delay(2, 4)

    # ==================== 数据提取 ====================

    @staticmethod
    def _parse_flight_price(text: str) -> Optional[float]:
        """从价格文本中提取数值"""
        if not text:
            return None
        # 匹配 "CN¥ 5,234" / "¥ 5,234" / "5,234" / "€ 456" 等
        m = re.search(r'(?:CN¥|¥|€|US\$|£|HK\$)?\s*([\d,]+(?:\.\d{1,2})?)', text)
        if m:
            return float(m.group(1).replace(",", ""))
        return None

    def _extract_flight_prices(self, route: dict) -> list[dict]:
        """从 Google Flights 结果页提取航班价格"""
        self._rand_delay(2, 3)

        flights_found: list[dict] = []
        origin = route["origin"]
        dest = route["destination"]
        date = route["date"]

        try:
            # 方案 A：提取 "Best flights" 列表项
            # Google Flights 的航班结果通常在包含价格信息的列表项中
            price_selectors = [
                '[data-testid="flight-result"]',
                'li[role="listitem"]',
                '.flight-result',
                '[jsname]',  # 回退：查找带有 jsname 属性的容器
            ]

            for sel in price_selectors:
                try:
                    items = self.page.query_selector_all(sel)
                    if items and len(items) > 0:
                        logger.info(f"  使用选择器 '{sel}' → 检测到 {len(items)} 个元素")
                except Exception:
                    continue

            # 方案 B：直接扫描页面中所有包含价格模式的文本
            try:
                all_spans = self.page.query_selector_all("span, div, li")
                candidate_prices = []
                for el in all_spans:
                    try:
                        text = el.inner_text().strip()
                    except Exception:
                        continue
                    if not text:
                        continue
                    # 匹配货币 + 数字的价格模式
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

        except Exception as e:
            logger.error(f"  提取航班价格失败: {e}")
            self._debug_screenshot("flight-extract-error")

        # 如果上述方案都没有找到，尝试直接读取页面可见文本提取价格
        if not flights_found:
            logger.info("  尝试从页面全文提取价格 …")
            try:
                body_text = self.page.inner_text("body")
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

    def run(self):
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
        skipped_routes = []

        valid_routes = []
        for route in routes:
            flight_date = route.get("date", "")
            if flight_date < date_min or flight_date > date_max:
                logger.warning(
                    f"  ⚠ 跳过 {route['origin']}→{route['destination']}: "
                    f"日期 {flight_date} 超出允许范围 "
                    f"({date_min} ~ {date_max})"
                )
                skipped_routes.append(route)
            else:
                valid_routes.append(route)

        if skipped_routes:
            logger.info(
                f"  日期过滤: 跳过 {len(skipped_routes)} 条航线，"
                f"保留 {len(valid_routes)} 条"
            )
        routes = valid_routes

        if not routes:
            logger.warning("  ⚠ 所有航线均超出日期范围，无任务可执行")
            return

        db_path = init_flights_table(self.cfg.flight_db_file)
        total_extracted = 0

        logger.info(f"\n{'=' * 60}")
        logger.info(f"  机票价格爬虫启动 (V1 · Google Flights)")
        logger.info(f"  共 {len(routes)} 条航线")
        logger.info(f"  日期范围: {date_min} ~ {date_max}")
        logger.info(f"  货币: {self.cfg.flight_currency}")
        logger.info(f"{'=' * 60}\n")

        with sync_playwright() as p:
            self.pw = p
            try:
                self._launch_browser()

                for idx, route in enumerate(routes):
                    logger.info(f"\n{'─' * 40}")
                    logger.info(
                        f"  航线 {idx + 1}/{len(routes)}: "
                        f"{route['origin']} → {route['destination']} | "
                        f"{route['date']}"
                    )
                    logger.info(f"{'─' * 40}")

                    self._search_route(route)
                    flights = self._extract_flight_prices(route)

                    if flights:
                        self.results.extend(flights)
                        insert_flight_records(db_path, flights)
                        total_extracted += len(flights)

                    # 航线间隔休眠
                    if idx < len(routes) - 1:
                        self._rand_delay(3, 6)

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
                self._cleanup()

    def _cleanup(self):
        logger.info("清理资源 …")
        for obj in [self.page, self.context, self.browser]:
            try:
                if obj:
                    obj.close()
            except Exception:
                pass


def main():
    config = ScraperConfig()
    scraper = FlightScraper(config)
    scraper.run()


if __name__ == "__main__":
    main()
