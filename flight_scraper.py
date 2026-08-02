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

    @staticmethod
    def _parse_airline_info(card_text: str) -> str:
        """从机票卡片文本中提取准确的航班号与中转信息"""
        parts = []

        # 航班号：如 FR8866 / FR 8866 / EK 302（两位字母 + 数字，排除机场代码与时间）
        m_fn = re.search(r'\b([A-Z]{2})\s*(\d{1,4})\b', card_text)
        if m_fn:
            parts.append(f"{m_fn.group(1)} {m_fn.group(2)}")

        # 中转信息：Nonstop / 直飞 / 1 stop (via X) / 1 次中转
        m_stop = re.search(
            r'(Nonstop|直飞|\d+\s*stop(?:s)?(?:\s*via\s+[A-Za-z.\- ]+)?|\d+\s*中转)',
            card_text,
            re.IGNORECASE,
        )
        if m_stop:
            raw = m_stop.group(0).strip()
            if re.match(r'\d+\s*stop', raw, re.IGNORECASE):
                m_num = re.match(r'(\d+)\s*stop', raw, re.IGNORECASE)
                m_via = re.search(r'via\s+([A-Za-z.\- ]+?)\s*$', raw, re.IGNORECASE)
                stops_txt = f"{m_num.group(1)} 次中转"
                if m_via:
                    stops_txt += f"（经 {m_via.group(1).strip()}）"
                parts.append(stops_txt)
            elif raw.lower() == "nonstop":
                parts.append("直飞")
            else:
                parts.append(raw)

        # 兜底：未识别出航班号/中转时，仅标记为未知航班，杜绝无关字符串入库
        if not parts:
            parts.append("未知航班")

        return " · ".join(parts)

    async def _extract_flight_prices(self, route: dict) -> list[dict]:
        """从 Google Flights 结果页按航班卡片提取价格、航班号、中转与预订链接"""
        await self._rand_delay(2, 3)

        origin = route["origin"]
        dest = route["destination"]
        date = route["date"]
        fallback_link = self._build_flight_url(route)

        price_re = re.compile(r'(?:CN¥|¥|HK\$)\s*([\d,]{3,6}(?:\.\d{1,2})?)')

        # 浏览器端一次性定位所有航班卡片（多策略，适配 Google Flights 不同 DOM 结构），
        # 取回卡片文本与首个链接
        try:
            cards_data = await self.page.evaluate(
                """() => {
                    const priceRe = /(?:CN¥|¥|HK\\$)\\s*[\\d,]{3,6}(?:\\.\\d{1,2})?/;
                    const hasPrice = (el) => priceRe.test((el.innerText || '').trim());
                    const isShortPrice = (el) => {
                        const t = (el.innerText || '').trim();
                        return priceRe.test(t) && t.length < 30 && el.children.length === 0;
                    };
                    const seen = new Set();
                    const cards = [];
                    const push = (el, maxLen) => {
                        if (el && !seen.has(el) && hasPrice(el) &&
                            (el.innerText || '').length < (maxLen || 3000)) {
                            seen.add(el);
                            cards.push(el);
                        }
                    };
                    // 策略1：语义化列表项 role=listitem
                    [...document.querySelectorAll('[role="listitem"]')].forEach((el) => push(el));
                    // 策略2：li 标签
                    [...document.querySelectorAll('li')].forEach((el) => push(el));
                    // 策略3：从"纯价格"叶子节点向上爬，找同时含时间+航程/中转信息的卡片容器
                    [...document.querySelectorAll('span, div')].forEach((el) => {
                        if (!isShortPrice(el)) return;
                        let cur = el.parentElement;
                        for (let i = 0; i < 8 && cur; i++) {
                            const t = (cur.innerText || '').trim();
                            if (/\\d{1,2}:\\d{2}/.test(t) &&
                                /(stop|nonstop|直飞|中转|h\\s*\\d|h\\d+m)/i.test(t)) {
                                push(cur, 1200);
                                break;
                            }
                            cur = cur.parentElement;
                        }
                    });
                    return cards.slice(0, 15).map((el) => {
                        const a = el.querySelector('a[href]');
                        return {
                            text: (el.innerText || '').trim(),
                            href: a ? a.href : null,
                        };
                    });
                }"""
            )
        except Exception:
            logger.warning("  ⚠ 浏览器端提取航班卡片失败，回退到全文扫描")
            cards_data = []

        flights_found: list[dict] = []
        seen_prices: set = set()

        for card in cards_data or []:
            card_text = card.get("text", "")
            m = price_re.search(card_text)
            if not m:
                continue
            price_val = float(m.group(1).replace(",", ""))
            if not (500 <= price_val <= 100000):
                continue
            if price_val in seen_prices:
                continue
            seen_prices.add(price_val)

            record = {
                "origin": origin,
                "destination": dest,
                "flight_date": date,
                "price_cny": str(price_val),
                "price_num": price_val,
                "airline_info": self._parse_airline_info(card_text),
                "booking_link": card.get("href") or fallback_link,
                "cabin_class": route.get("cabin_class", "economy"),
                "adults": route.get("adults", 2),
                "children": route.get("children", 1),
                "scraped_at": self._start_time,
            }
            flights_found.append(record)
            logger.info(
                f"    ✈ {origin} → {dest} | ¥{price_val:,.0f} | "
                f"{record['airline_info'][:60]}"
            )

        # 兜底：卡片解析失败时，从页面全文提取价格（标记来源，避免无关字符串）
        if not flights_found:
            logger.info("  尝试从页面全文提取价格 …")
            try:
                body_text = await self.page.inner_text("body")
                price_pattern = price_re.findall(body_text)
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
                            "airline_info": "价格（未知航班）",
                            "booking_link": fallback_link,
                            "cabin_class": route.get("cabin_class", "economy"),
                            "adults": route.get("adults", 2),
                            "children": route.get("children", 1),
                            "scraped_at": self._start_time,
                        }
                        flights_found.append(record)
                        logger.info(f"    ✈ [全文] {origin} → {dest} | ¥{val:,.0f}")
            except Exception as e:
                logger.error(f"  全文提取失败: {e}")

        # 按价格从低到高排序，最多保留 5 条
        flights_found.sort(key=lambda x: x["price_num"])
        return flights_found[:5]

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
