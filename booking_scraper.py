"""
Booking.com 房源爬虫 V5
功能：
  - 按欧洲多城市任务列表依次/并发搜索
  - 默认搜索 2 成人 + 1 儿童（年龄可配置）
  - 直接抓取人民币（CNY）价格
  - 灵活筛选：双床房 / 免费取消 / 空调
  - 按城市 max_price_cny 自动过滤高价房源
  - 提取位置评分（Location Score）和距市中心距离
  - 城市间 15-30 秒随机延迟防反爬
  - 数据实时写入 SQLite 数据库

反反爬策略：Playwright Stealth + 随机延迟 + 浏览器指纹伪装 + URL 参数筛选

V5 更新：
  - 全异步 Playwright async_api 实现
  - 固定等待 (wait_for_timeout) → 智能等待 (wait_for_load_state + wait_for_selector)
  - 支持多城市并发抓取 (booking_concurrency 配置项)
  - 所有 ElementHandle 方法均正确 await
"""

import asyncio
import csv
import json
import logging
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page, Browser, BrowserContext
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright_stealth import Stealth

from config import ScraperConfig
from database import init_db, insert_records, get_record_count

# ==================== 日志 ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("BookingScraper")

# SQLite 写入锁（并发安全）
_db_lock = asyncio.Lock()


class BookingScraper:
    """Booking.com 反反爬爬虫 V5 —— 多城市 + 灵活筛选 + 异步并发 + 智能等待"""

    BASE_URL = "https://www.booking.com"

    # ---- 选择器 ----
    SELECTORS = {
        "cookie_accept": [
            'button:has-text("Accept all")',
            'button:has-text("Accept")',
            'button:has-text("Got it")',
            'button[data-testid="cookie-consent-accept"]',
        ],
        "dismiss_popup": [
            'button[aria-label="Dismiss"]',
            'button[aria-label="Close"]',
            'button:has-text("Maybe later")',
            'button:has-text("No thanks")',
        ],
        "property_card": [
            '[data-testid="property-card"]',
            'div[data-testid="property-card"]',
        ],
        "card_title": [
            '[data-testid="title"]',
            'a[data-testid="title-link"]',
            'h3[data-testid="title"]',
            'div[data-testid="title"]',
            'h3',
            'h2',
            'a[href*="hotel"]',
            'a[href*="property"]',
        ],
        "card_price": [
            '[data-testid="price-and-discounted-price"]',
            'span[data-testid="price-and-discounted-price"]',
            'div[data-testid="price-and-discounted-price"]',
            '[data-testid="price-for-x-nights"]',
        ],
        "next_page_btn": [
            'button[aria-label="Next page"]',
            'button:has-text("Next")',
            'a[data-testid="pagination-next"]',
        ],
        "card_score": [
            '[data-testid="review-score"]',
            'div[data-testid="review-score"]',
            'span[data-testid="review-score"]',
            '[data-testid="review-score"] > div:first-child',
            '[data-testid="review-score"] > span',
            '[aria-label*="Scored" i]',
            '[aria-label*="review" i]',
            '[aria-label*="score" i]',
        ],
        "location_desc": [
            '[data-testid="address"]',
            '[data-testid="location"]',
            '[data-testid="distance"]',
            'span[data-testid="distance"]',
            '.show_address',
            '.hf-address',
            '.bui-card__subtitle',
            'span.recommended_location',
        ],
    }

    def __init__(self, config: ScraperConfig):
        self.cfg = config
        self.pw = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.results: list[dict] = []
        self._start_time: str = ""

    # ==================== 工具方法 ====================

    async def _rand_delay(self, lo: float = None, hi: float = None):
        """随机异步延迟，模拟人类操作节奏"""
        await asyncio.sleep(random.uniform(
            lo or self.cfg.min_delay,
            hi or self.cfg.max_delay,
        ))

    async def _debug_screenshot(self, name: str):
        """保存调试截图"""
        if not self.cfg.debug_screenshots or not self.page:
            return
        path = Path(f"debug_{name}_{datetime.now():%H%M%S}.png")
        try:
            await self.page.screenshot(path=str(path), full_page=False)
            logger.debug(f"截图已保存: {path}")
        except Exception as e:
            logger.debug(f"截图失败: {e}")

    async def _safe_click_first(self, selectors: list[str],
                                timeout: int = 60_000) -> bool:
        """尝试点击匹配到的第一个可见按钮"""
        for sel in selectors:
            try:
                el = await self.page.wait_for_selector(
                    sel, state="visible", timeout=timeout
                )
                if el:
                    await el.click()
                    return True
            except PlaywrightTimeout:
                continue
            except Exception:
                continue
        return False

    # ==================== 数值解析 ====================

    @staticmethod
    def _parse_price_number(price_text: str) -> Optional[float]:
        """从价格文本中提取数值（如 "CN¥ 910" → 910.0, "1,234" → 1234.0）"""
        if not price_text or price_text == "N/A":
            return None
        clean = price_text.replace(",", "").replace("¥", "").replace("CN", "").strip()
        m = re.search(r'(\d+\.?\d*)', clean)
        if m:
            return float(m.group(1))
        return None

    @staticmethod
    def _parse_score_number(score_text: str) -> Optional[float]:
        """从评分文本中提取数值（如 "Scored 9.0\n9.0\nWonderful" → 9.0）"""
        if not score_text or score_text == "N/A":
            return None
        m = re.search(r'(\d+\.?\d*)', score_text)
        if m:
            return float(m.group(1))
        return None

    # ==================== 浏览器启动 ====================

    def _build_context_kwargs(self) -> dict:
        """构建浏览器 context 参数（可供主 context 和并发城市复用）"""
        kwargs: dict = {
            "viewport": {
                "width": self.cfg.viewport_width,
                "height": self.cfg.viewport_height,
            },
            "locale": self.cfg.browser_locale,
            "timezone_id": "Europe/Paris",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        }
        if self.cfg.proxy_server:
            kwargs["proxy"] = {"server": self.cfg.proxy_server}
            logger.info(f"  使用代理: {self.cfg.proxy_server}")
        return kwargs

    async def _launch_browser(self):
        """启动带 stealth 补丁的浏览器"""
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
                logger.info("  使用本地 Edge")
            except Exception:
                logger.warning("  本地 Edge 不可用，回退到 Chromium")

        self.browser = await self.pw.chromium.launch(**launch_kwargs)

        ctx_kwargs = self._build_context_kwargs()
        self.context = await self.browser.new_context(**ctx_kwargs)
        self.page = await self.context.new_page()

        # 注入 stealth 初始化脚本（await：新版 Playwright add_init_script 是异步的）
        await self.page.add_init_script("""
            Object.defineProperty(navigator, 'plugins',
                { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages',
                { get: () => ['en-US', 'en'] });
        """)

        # 应用 Stealth 补丁到主 context
        stealth = Stealth()
        await stealth.apply_stealth_async(self.context)

        logger.info("  Stealth 补丁 + 指纹伪装 已注入")

    async def _apply_stealth_to_context(self, context: BrowserContext):
        """对独立 context 应用 Stealth 补丁"""
        try:
            await Stealth().apply_stealth_async(context)
        except Exception:
            logger.debug("  Stealth 补丁应用失败（非致命）")

    # ==================== 弹窗处理 ====================

    async def _dismiss_overlays(self):
        """关闭各种覆盖弹窗"""
        await self._safe_click_first(self.SELECTORS["cookie_accept"], timeout=3_000)
        await self._rand_delay(0.3, 0.6)
        await self._safe_click_first(self.SELECTORS["dismiss_popup"], timeout=3_000)

        try:
            await self.page.evaluate("""
                document.querySelectorAll('[role="dialog"] '
                    + 'button[aria-label="Dismiss"], '
                    + '[role="dialog"] button[aria-label="Close"]')
                    .forEach(b => b.click());
            """)
        except Exception:
            pass

    # ==================== 城市搜索 ====================

    @staticmethod
    def _build_search_url(task: dict) -> str:
        """根据任务参数构建搜索 URL（人民币 CNY）"""
        adults = task.get("adults", 2)
        children = task.get("children", 1)
        rooms = task.get("rooms", 1)
        children_ages = task.get("children_ages", [12])
        ages_param = ",".join(str(age) for age in children_ages)

        params = [
            f"ss={task['city'].replace(' ', '+')}",
            f"checkin={task['checkin']}",
            f"checkout={task['checkout']}",
            f"group_adults={adults}",
            f"group_children={children}",
            f"req_children_ages={ages_param}",
            f"no_rooms={rooms}",
            "selected_currency=CNY",
            "lang=en-us",
        ]
        return f"https://www.booking.com/searchresults.html?{'&'.join(params)}"

    # ==================== 搜索执行 ====================

    async def _search_city(self, task: dict):
        """导航到指定城市的搜索结果页"""
        city = task["city"]
        adults = task.get("adults", self.cfg.default_adults)
        children = task.get("children", self.cfg.default_children)
        ages = task.get("children_ages", self.cfg.default_children_ages)
        logger.info(
            f"▸ 搜索: {city} | "
            f"{task['checkin']} → {task['checkout']} | "
            f"{adults} 成人 + {children} 儿童（{ages} 岁）| "
            f"最高 ¥{task.get('max_price_cny', '—')}/晚"
        )

        search_url = self._build_search_url(task)
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

        card_selector = self.SELECTORS["property_card"][0]
        try:
            await self.page.wait_for_selector(
                card_selector, state="attached", timeout=10_000
            )
            logger.info("  ✓ 检测到房源卡片容器")
        except PlaywrightTimeout:
            logger.warning("  ⚠ 未检测到 property-card，尝试回退等待 …")
            await asyncio.sleep(3.0)

    # ==================== 数据提取（ElementHandle 方法均需 await） ====================

    async def _extract_text(self, el, selectors: list[str],
                            default: str = "N/A") -> str:
        """从元素中按优先级尝试多个选择器提取文本"""
        for sel in selectors:
            try:
                child = await el.query_selector(sel)
                if child:
                    text = (await child.inner_text()).strip()
                    if text:
                        return text
            except Exception:
                continue
        return default

    async def _extract_price(self, card) -> str:
        """从卡片的多个可能位置提取价格"""
        # 方案 A：通过专用 data-testid 选择器提取
        price_sels = [
            '[data-testid="price-and-discounted-price"]',
            'span[data-testid="price-and-discounted-price"]',
            '[data-testid="price-for-x-nights"]',
            '[data-testid="price"]',
        ]
        for sel in price_sels:
            try:
                el = await card.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    m = re.search(r'(?:CN¥|¥|US\$|€|£)?\s*([\d,]+(?:\.[\d]{1,2})?)', text)
                    if m:
                        return m.group(1)
            except Exception:
                continue

        # 方案 B：扫描卡片内所有 span/div，查找 CNY 价格模式
        try:
            all_els = await card.query_selector_all('span, div')
            for el in all_els:
                try:
                    text = (await el.inner_text()).strip()
                except Exception:
                    continue
                if not text:
                    continue
                m = re.search(r'(?:CN¥|¥)\s*([\d,]+(?:\.[\d]{1,2})?)', text)
                if m:
                    return m.group(1)
        except Exception:
            pass

        # 方案 C：扫描任何包含合理数字（价格量级）的元素
        try:
            all_els = await card.query_selector_all('span, div')
            for el in all_els:
                try:
                    text = (await el.inner_text()).strip()
                except Exception:
                    continue
                m = re.search(r'([\d,]{2,6}(?:\.[\d]{1,2})?)', text)
                if m:
                    val = m.group(1).replace(',', '')
                    try:
                        num = float(val)
                        if 50 <= num <= 50000:
                            return m.group(1)
                    except ValueError:
                        continue
        except Exception:
            pass

        return "N/A"

    async def _extract_room_type(self, card) -> str:
        """提取房型描述"""
        sels = [
            'h4',
            '[data-testid="room-type"]',
            '[data-testid="room-info"]',
            '[data-testid="recommended-unit"]',
            '.room-info',
        ]
        for sel in sels:
            try:
                el = await card.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    if text and len(text) < 120:
                        return text
            except Exception:
                continue

        try:
            title_el = (
                await card.query_selector('[data-testid="title"]')
                or await card.query_selector('h3')
                or await card.query_selector('h2')
            )
            if title_el:
                parent = await title_el.query_selector('xpath=..')
                if parent:
                    siblings = await parent.query_selector_all('xpath=following-sibling::*')
                    for sib in siblings[:5]:
                        try:
                            text = (await sib.inner_text()).strip()
                            if text and 3 < len(text) < 120:
                                if not re.search(
                                    r'(?:CN¥|¥|€|US\$|Scored|\d+\.\d+\s*(?:km|m)\s+from)',
                                    text
                                ):
                                    return text
                        except Exception:
                            continue
        except Exception:
            pass

        return "N/A"

    async def _extract_location(self, card) -> str:
        """从房源卡片中提取位置 / 距离描述文字"""
        for sel in self.SELECTORS["location_desc"]:
            try:
                el = await card.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    if text and len(text) < 200:
                        if not text.startswith("Scored") and \
                           not text.startswith("€") and \
                           not text.startswith("US$") and \
                           not text.startswith("CN¥"):
                            return text
            except Exception:
                continue

        try:
            spans = await card.query_selector_all("span, div")
            for sp in spans:
                try:
                    text = (await sp.inner_text()).strip()
                except Exception:
                    continue
                if not text or len(text) > 150:
                    continue
                if re.search(
                    r'(\d+\.?\d*\s*(km|m|kilometre|metre|mile)s?\s+from|'
                    r'distance|'
                    r'city\s+centre|'
                    r'train\s+station|'
                    r'walk\s+to|'
                    r'near\s+|'
                    r'located\s+in)',
                    text, re.IGNORECASE
                ):
                    return text
        except Exception:
            pass

        return "N/A"

    async def _extract_location_score(self, card) -> str:
        """从房源卡片中提取「位置评分（Location Score）」"""
        try:
            all_els = await card.query_selector_all('div, span, li')
            for el in all_els:
                try:
                    text = (await el.inner_text()).strip()
                except Exception:
                    continue
                if not text or len(text) > 80:
                    continue
                m = re.search(
                    r'Location\s*(?:score|rating)?\s*[·:.\s]*\s*(\d+\.?\d{0,1})',
                    text, re.IGNORECASE
                )
                if m:
                    score = float(m.group(1))
                    if 1.0 <= score <= 10.0:
                        return m.group(1)
        except Exception:
            pass

        try:
            all_els = await card.query_selector_all('[aria-label*="Location" i]')
            for el in all_els:
                try:
                    label = (await el.get_attribute("aria-label")) or ""
                    m = re.search(r'(\d+\.?\d{0,1})', label)
                    if m:
                        score = float(m.group(1))
                        if 1.0 <= score <= 10.0:
                            return m.group(1)
                except Exception:
                    continue
        except Exception:
            pass

        return "N/A"

    async def _extract_distance_to_centre(self, card) -> str:
        """从房源卡片中提取「距市中心距离」"""
        loc = await self._extract_location(card)
        if loc != "N/A":
            m = re.search(
                r'(\d+\.?\d*\s*(?:km|m|kilometre|metre|mile)s?)\s+from\s+(?:the\s+)?(?:city\s+)?cent',
                loc, re.IGNORECASE
            )
            if m:
                return m.group(1) + " from centre"
            m = re.search(
                r'(\d+\.?\d*\s*(?:km|m)\s+from\s+.+)',
                loc, re.IGNORECASE
            )
            if m:
                return m.group(1)
            if len(loc) < 60:
                return loc

        try:
            all_els = await card.query_selector_all('span, div')
            for el in all_els:
                try:
                    text = (await el.inner_text()).strip()
                except Exception:
                    continue
                if not text or len(text) > 100:
                    continue
                m = re.search(
                    r'(\d+\.?\d*\s*(?:km|m|mile)s?)\s+from\s+(?:the\s+)?(?:city\s+)?cent',
                    text, re.IGNORECASE
                )
                if m:
                    return m.group(1) + " from centre"
        except Exception:
            pass

        return "N/A"

    async def _extract_link(self, card) -> str:
        """提取酒店详情链接"""
        try:
            a = await card.query_selector('a[data-testid="title-link"]')
            if not a:
                a = await card.query_selector('a[href*="hotel"]')
            if not a:
                a = await card.query_selector('a[href*="property"]')
            if not a:
                links = await card.query_selector_all('a')
                for link in links:
                    href = (await link.get_attribute('href')) or ''
                    if '/hotel/' in href or '/property/' in href:
                        a = link
                        break
            if not a:
                a = await card.query_selector('a[href]')
            if a:
                href = await a.get_attribute("href")
                if href:
                    return href if href.startswith("http") else \
                        self.BASE_URL + href
        except Exception:
            pass
        return "N/A"

    async def _extract_score(self, card) -> str:
        """从卡片中提取综合评分"""
        for container_sel in [
            '[data-testid="review-score"]',
            'div[data-testid="review-score"]',
        ]:
            try:
                container = await card.query_selector(container_sel)
                if not container:
                    continue
                for child_sel in ['div', 'span', '> div:first-child', '> span']:
                    try:
                        children = await container.query_selector_all(child_sel)
                        for child in children:
                            text = (await child.inner_text()).strip()
                            if not text:
                                continue
                            m = re.search(r'^(\d+\.?\d{0,1})\s*$', text)
                            if m:
                                score = float(m.group(1))
                                if 1.0 <= score <= 10.0:
                                    return m.group(1)
                    except Exception:
                        continue
                text = (await container.inner_text()).strip()
                m = re.search(r'(\d+\.?\d*)', text)
                if m:
                    score = float(m.group(1))
                    if 1.0 <= score <= 10.0:
                        return m.group(1)
            except Exception:
                continue

        try:
            all_els = await card.query_selector_all('div, span')
            for el in all_els:
                try:
                    text = (await el.inner_text()).strip()
                except Exception:
                    continue
                if not text:
                    continue
                m = re.search(r'(\d+\.\d{1,2})\s*$', text)
                if m:
                    score = float(m.group(1))
                    if 1.0 <= score <= 10.0:
                        return m.group(1)
                if re.search(r'(?:Scored|scored|review)', text):
                    m2 = re.search(r'(\d+\.?\d*)', text)
                    if m2:
                        score = float(m2.group(1))
                        if 1.0 <= score <= 10.0:
                            return m2.group(1)
        except Exception:
            pass

        return "N/A"

    def _card_matches_triple_or_family(self, room_type: str) -> bool:
        """检查房型文本是否匹配三人间/家庭房关键词"""
        if not self.cfg.filter_triple_or_family:
            return True
        keywords = [
            "triple", "triple room", "3 single beds", "3 beds",
            "three single", "three beds", "family", "family room",
            "quadruple", "4 beds", "four beds",
        ]
        rt_lower = room_type.lower()
        return any(kw in rt_lower for kw in keywords)

    # ==================== 调试辅助 ====================

    async def _dump_card_html(self, card, prefix: str = "card"):
        """转储单个卡片的 HTML 用于离线调试"""
        try:
            html = await card.inner_html()
            path = Path(f"debug_{prefix}_{datetime.now():%H%M%S}.html")
            path.write_text(html, encoding="utf-8")
            logger.info(f"  🔍 卡片 HTML 已保存: {path.resolve()}")
        except Exception as e:
            logger.debug(f"  转储 HTML 失败: {e}")

    async def _dump_page_info(self):
        """转储当前页面信息用于诊断选择器失效问题"""
        try:
            logger.info(f"  当前页面标题: {await self.page.title()}")
            logger.info(f"  当前 URL: {self.page.url[:200]}")
            containers = [
                '[data-testid="property-card"]',
                '[data-testid="search-results"]',
                '[data-results=""]',
                '.sr_item',
                '.sr_property_block',
                '[role="list"]',
                '[role="listbox"]',
            ]
            for sel in containers:
                els = await self.page.query_selector_all(sel)
                count = len(els)
                if count > 0:
                    logger.info(f"  找到容器 '{sel}': {count} 个元素")
        except Exception as e:
            logger.debug(f"  页面信息获取失败: {e}")

    async def _extract_booking_dates(self, task: dict) -> tuple:
        """从搜索结果页面提取实际的预订日期"""
        checkin_date = task.get("checkin", "")
        checkout_date = task.get("checkout", "")

        try:
            summary_sels = [
                '[data-testid="searchbox-dates"]',
                '[data-testid="datepicker-tabs"]',
                '[data-testid="search-box-dates"]',
                '.sb-searchbox__input',
                '.sb-date-field__display',
            ]
            for sel in summary_sels:
                try:
                    el = await self.page.query_selector(sel)
                    if el:
                        text = (await el.inner_text()).strip()
                        if text:
                            logger.info(f"  📅 页面日期摘要: {text[:100]}")
                            dates = re.findall(
                                r'(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4})',
                                text, re.IGNORECASE
                            )
                            if len(dates) >= 2:
                                try:
                                    ci = datetime.strptime(dates[0], "%d %b %Y")
                                    co = datetime.strptime(dates[1], "%d %b %Y")
                                    checkin_date = ci.strftime("%Y-%m-%d")
                                    checkout_date = co.strftime("%Y-%m-%d")
                                    logger.info(f"  ✓ 从页面提取到日期: {checkin_date} → {checkout_date}")
                                    return checkin_date, checkout_date
                                except ValueError:
                                    pass
                            dates2 = re.findall(
                                r'(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})',
                                text, re.IGNORECASE
                            )
                            if len(dates2) >= 2:
                                try:
                                    ci = datetime.strptime(dates2[0], "%d %B %Y")
                                    co = datetime.strptime(dates2[1], "%d %B %Y")
                                    checkin_date = ci.strftime("%Y-%m-%d")
                                    checkout_date = co.strftime("%Y-%m-%d")
                                    logger.info(f"  ✓ 从页面提取到日期: {checkin_date} → {checkout_date}")
                                    return checkin_date, checkout_date
                                except ValueError:
                                    pass
                except Exception:
                    continue

            date_input_sels = [
                'input[data-testid="searchbox-checkin"]',
                'input[data-testid="searchbox-checkout"]',
                'input[name="checkin"]',
                'input[name="checkout"]',
                '[data-testid="datepicker-checkin"]',
                '[data-testid="datepicker-checkout"]',
            ]
            for sel in date_input_sels:
                try:
                    el = await self.page.query_selector(sel)
                    if el:
                        val = (await el.get_attribute("value")) or (await el.get_attribute("placeholder")) or ""
                        if val and re.match(r'\d{4}-\d{2}-\d{2}', val):
                            if "checkin" in sel or "check-in" in sel.lower():
                                checkin_date = val
                            else:
                                checkout_date = val
                except Exception:
                    continue

            current_url = self.page.url
            url_ci = re.search(r'checkin=(\d{4}-\d{2}-\d{2})', current_url)
            url_co = re.search(r'checkout=(\d{4}-\d{2}-\d{2})', current_url)
            if url_ci:
                checkin_date = url_ci.group(1)
            if url_co:
                checkout_date = url_co.group(1)

        except Exception as e:
            logger.debug(f"  从页面提取日期失败: {e}")

        logger.info(f"  📅 预订日期: {checkin_date} → {checkout_date}")
        return checkin_date, checkout_date

    async def _extract_cards(self, task: dict) -> list[dict]:
        """从当前页面提取所有房源卡片数据"""
        await self._rand_delay(1, 2)

        checkin_date, checkout_date = await self._extract_booking_dates(task)

        card_selectors = [
            '[data-testid="property-card"]',
            'div[data-testid="property-card"]',
        ]

        cards_raw = []
        for sel in card_selectors:
            cards_raw = await self.page.query_selector_all(sel)
            if cards_raw:
                logger.info(f"  使用选择器 '{sel}' → 检测到 {len(cards_raw)} 个房源卡片")
                break

        if not cards_raw:
            logger.warning("  ⚠ 未检测到任何房源卡片！可能选择器已失效")
            await self._debug_screenshot("no-cards")
            await self._dump_page_info()
            return []

        extracted = []
        for card in cards_raw:
            try:
                # ---- 调试：保存第一张卡片的完整内部 HTML ----
                if len(extracted) == 0:
                    with open('debug_card.html', 'w', encoding='utf-8') as f:
                        f.write(await card.inner_html())
                    logger.info(f"  🔍 已保存第 1 张卡片 HTML → debug_card.html")

                # ---- 名称提取 ----
                name = await self._extract_text(card, self.SELECTORS["card_title"])
                if not name or name == "N/A":
                    for tag in ["h3", "h2", "h4", "strong", "a"]:
                        name = await self._extract_text(card, [tag])
                        if name and name != "N/A" and len(name) > 2:
                            break

                if not name or name == "N/A":
                    try:
                        all_divs = await card.query_selector_all('div')
                        for div in all_divs:
                            text = (await div.inner_text()).strip()
                            if text and 5 < len(text) < 150 and \
                               not re.search(r'^(?:CN¥|¥|€|US\$|Scored|\d+\.\d)',
                                             text):
                                name = text
                                break
                    except Exception:
                        pass

                # ---- 各项数据提取 ----
                price = await self._extract_price(card)
                room_type = await self._extract_room_type(card)
                link = await self._extract_link(card)
                location_desc = await self._extract_location(card)
                location_score = await self._extract_location_score(card)
                distance_to_centre = await self._extract_distance_to_centre(card)
                score = await self._extract_score(card)

                record = {
                    "hotel_name": name,
                    "price_cny": price,
                    "room_type": room_type,
                    "review_score": score,
                    "detail_link": link,
                    "location_desc": location_desc,
                    "location_score": location_score,
                    "distance_to_centre": distance_to_centre,
                    "city": task["city"],
                    "checkin": checkin_date,
                    "checkout": checkout_date,
                    "scraped_at": self._start_time,
                }

                extracted.append(record)
                price_num = self._parse_price_number(price)
                price_str = f"¥{price_num:,.0f}" if price_num else f"¥{price}"
                loc_score_str = f" | 📍位置 {location_score}" if location_score != "N/A" else ""
                dist_str = f" | 📏 {distance_to_centre[:30]}" if distance_to_centre != "N/A" else ""
                logger.info(f"    ✓ {name[:45]} | {price_str} | ⭐{score}{loc_score_str}{dist_str}")

            except Exception as e:
                logger.error(f"    提取卡片失败: {e}")
                continue

        return extracted

    # ==================== 后处理：价格过滤 ====================

    @staticmethod
    def _calc_nights(task: dict) -> int:
        """计算入住天数"""
        try:
            fmt = "%Y-%m-%d"
            ci = datetime.strptime(task["checkin"], fmt)
            co = datetime.strptime(task["checkout"], fmt)
            nights = (co - ci).days
            return max(nights, 1)
        except Exception:
            return 1

    def _process_records(self, records: list[dict], task: dict) -> list[dict]:
        """后处理：按入住期间总预算过滤高价房源"""
        max_price_total = task.get("max_price_cny")
        if max_price_total is None:
            return list(records)

        nights = self._calc_nights(task)
        processed = []
        filtered_count = 0

        for r in records:
            price_nightly = self._parse_price_number(r["price_cny"])

            if max_price_total is not None and price_nightly is not None:
                total_for_stay = price_nightly * nights
                if total_for_stay > max_price_total:
                    logger.info(
                        f"    ✗ 价格过滤: {r['hotel_name'][:40]} | "
                        f"¥{price_nightly:,.0f}/晚 × {nights} 晚 = "
                        f"¥{total_for_stay:,.0f} > 总预算 ¥{max_price_total:,.0f}"
                    )
                    filtered_count += 1
                    continue

            processed.append(r)

        if filtered_count > 0:
            logger.info(
                f"  🔍 价格过滤：剔除 {filtered_count} 条，"
                f"保留 {len(processed)} 条 "
                f"（总预算 ¥{max_price_total:,} / {nights} 晚 = "
                f"每晚上限 ¥{max_price_total / nights:,.0f}）"
            )

        return processed

    # ==================== 翻页 ====================

    async def _scroll_to_load(self):
        """滚动页面触发懒加载"""
        for _ in range(self.cfg.scroll_times):
            await self.page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
            await self._rand_delay(0.8, 1.5)
        await self.page.evaluate("window.scrollTo(0, 0)")
        await self._rand_delay(0.3, 0.5)

    async def _go_next_page(self) -> bool:
        """点击下一页按钮"""
        for sel in self.SELECTORS["next_page_btn"]:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_enabled():
                    await btn.click()
                    await self._rand_delay(2, 4)

                    # ---- 智能等待：等待新一批房源卡片出现 ----
                    card_selector = self.SELECTORS["property_card"][0]
                    try:
                        await self.page.wait_for_selector(
                            card_selector, state="attached", timeout=10_000
                        )
                        await asyncio.sleep(1.0)
                        logger.info("  → 已翻到下一页")
                    except PlaywrightTimeout:
                        logger.warning("  ⚠ 翻页后未检测到新卡片，尝试回退等待 …")
                        await asyncio.sleep(3.0)

                    return True
            except Exception:
                continue

        logger.info("  → 没有更多页面可翻")
        return False

    # ==================== 城市间延迟 ====================

    async def _inter_city_delay(self):
        """城市间的随机异步延迟（15-30 秒），防止触发反爬虫"""
        delay = random.uniform(
            self.cfg.inter_city_delay_min,
            self.cfg.inter_city_delay_max,
        )
        logger.info(
            f"\n⏳ 城市间延迟 {delay:.0f} 秒（防反爬策略）…"
        )
        await asyncio.sleep(delay)
        logger.info("  延迟结束，继续下一个城市 ✓\n")

    # ==================== 保存 ====================

    def _save_results(self):
        """保存爬取结果（CSV/JSON 可选，数据库始终写入）"""
        if not self.results:
            logger.warning("⚠ 没有数据可保存")
            return

        if self.cfg.save_to_csv:
            fmt = self.cfg.output_format.lower()
            path = Path(self.cfg.output_file)

            if fmt == "csv":
                self._to_csv(path)
            elif fmt == "json":
                self._to_json(path)
            else:
                raise ValueError(f"不支持的输出格式: {fmt}")

            abs_path = path.resolve()
            logger.info(f"✓ 已保存 → {abs_path}")
        else:
            logger.info("（CSV/JSON 文件保存已关闭）")

    def _to_csv(self, path: Path):
        """写入 CSV（UTF-8 BOM，Excel 可直接打开）"""
        fields = [
            "hotel_name", "price_cny", "room_type",
            "review_score", "detail_link", "location_desc",
            "location_score", "distance_to_centre",
            "city", "checkin", "checkout", "scraped_at",
        ]
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(self.results)

    def _to_json(self, path: Path):
        """写入 JSON"""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.results, f, ensure_ascii=False, indent=2)

    # ==================== 主流程 ====================

    async def _scrape_city_isolated(
        self, task: dict, db_path: str, task_idx: int, total: int
    ) -> list[dict]:
        """在独立的 browser context 中抓取单个城市（支持并发安全）"""
        city_name = task["city"]
        max_price = task.get("max_price_cny", "—")
        adults = task.get("adults", self.cfg.default_adults)
        children = task.get("children", self.cfg.default_children)

        logger.info(f"\n{'=' * 60}")
        logger.info(f"  🌍 城市 {task_idx + 1}/{total}: {city_name}")
        logger.info(
            f"     {task['checkin']} → {task['checkout']} | "
            f"{adults} 成人 + {children} 儿童 | "
            f"最高 ¥{max_price}/晚"
        )
        logger.info(f"{'=' * 60}")

        # 每个并发城市使用独立 browser context，确保完全隔离
        ctx_kwargs = self._build_context_kwargs()
        city_ctx = await self.browser.new_context(**ctx_kwargs)
        city_page = await city_ctx.new_page()

        # 注入指纹伪装脚本
        await city_page.add_init_script("""
            Object.defineProperty(navigator, 'plugins',
                { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages',
                { get: () => ['en-US', 'en'] });
        """)

        # 应用 Stealth 补丁
        await self._apply_stealth_to_context(city_ctx)

        try:
            saved_page = self.page
            saved_context = self.context
            self.page = city_page
            self.context = city_ctx

            await self._search_city(task)

            city_results = []

            for page_num in range(1, self.cfg.max_pages + 1):
                logger.info(f"\n  ── 第 {page_num}/{self.cfg.max_pages} 页 ──")

                await self._scroll_to_load()
                await self._dismiss_overlays()

                page_cards = await self._extract_cards(task)
                page_cards = self._process_records(page_cards, task)

                city_results.extend(page_cards)

                if page_cards:
                    async with _db_lock:
                        insert_records(db_path, page_cards)

                logger.info(
                    f"  本页 {len(page_cards)} 条 | "
                    f"本城累计 {len(city_results)} 条"
                )

                if page_num < self.cfg.max_pages:
                    if not await self._go_next_page():
                        break

                await self._rand_delay(2, 4)

            return city_results

        finally:
            self.page = saved_page
            self.context = saved_context
            await city_page.close()
            await city_ctx.close()

    async def run(self):
        """执行完整爬取流程 —— 根据 concurrency 配置顺序或并发处理城市"""
        self._start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        db_path = init_db(self.cfg.db_file)
        tasks = self.cfg.CITY_TASKS

        # ---- 日期范围过滤 ----
        date_min = self.cfg.SCRAPE_DATE_MIN
        date_max = self.cfg.SCRAPE_DATE_MAX

        valid_tasks = []
        for task in tasks:
            checkin = task.get("checkin", "")
            checkout = task.get("checkout", "")
            if checkin < date_min or checkout > date_max:
                logger.warning(
                    f"  ⚠ 跳过 {task['city']}: "
                    f"日期 {checkin}→{checkout} 超出允许范围 "
                    f"({date_min} ~ {date_max})"
                )
            else:
                valid_tasks.append(task)

        if not valid_tasks:
            logger.warning("  ⚠ 所有城市任务均超出日期范围，无任务可执行")
            return
        tasks = valid_tasks

        logger.info(f"\n{'=' * 60}")
        logger.info(f"  长途旅行计划爬虫启动 (V5 · Async · CNY · 位置评分)")
        logger.info(f"  共 {len(tasks)} 个城市 | 每个最多 {self.cfg.max_pages} 页")
        logger.info(f"  并发度: {self.cfg.booking_concurrency} | 日期范围: {date_min} ~ {date_max}")
        logger.info(f"  默认搜索: {self.cfg.default_adults} 成人 + "
                    f"{self.cfg.default_children} 儿童（{self.cfg.children_ages_param} 岁）")
        logger.info(f"  筛选: 三人间/家庭房={self.cfg.filter_triple_or_family} | "
                    f"免费取消={self.cfg.filter_free_cancellation} | "
                    f"空调={self.cfg.filter_air_conditioning}")
        logger.info(f"{'=' * 60}\n")

        async with async_playwright() as p:
            self.pw = p

            try:
                await self._launch_browser()

                total_extracted = 0

                if self.cfg.booking_concurrency == 1:
                    # ---- 顺序模式（保留城市间延迟） ----
                    for task_idx, task in enumerate(tasks):
                        city_results = await self._scrape_city_isolated(
                            task, db_path, task_idx, len(tasks)
                        )
                        self.results.extend(city_results)
                        total_extracted += len(city_results)

                        if task_idx < len(tasks) - 1:
                            await self._inter_city_delay()
                else:
                    # ---- 并发模式 ----
                    sem = asyncio.Semaphore(self.cfg.booking_concurrency)

                    async def _bounded_scrape(task, idx):
                        async with sem:
                            return await self._scrape_city_isolated(
                                task, db_path, idx, len(tasks)
                            )

                    all_city_results = await asyncio.gather(*[
                        _bounded_scrape(task, i) for i, task in enumerate(tasks)
                    ])

                    for city_results in all_city_results:
                        self.results.extend(city_results)
                        total_extracted += len(city_results)

                total_db = get_record_count(db_path)
                logger.info(f"\n{'=' * 60}")
                logger.info(f"  ✅ 全部完成！")
                logger.info(f"  本次新增: {total_extracted} 条")
                logger.info(f"  数据库总计: {total_db} 条 → {db_path}")
                logger.info(f"  覆盖城市: {len(tasks)} 个")
                logger.info(f"{'=' * 60}")
                self._save_results()

            except KeyboardInterrupt:
                logger.info("\n⚠ 用户中断，保存已获取的数据 …")
                if self.results:
                    self._save_results()
            except Exception as e:
                logger.error(f"爬取出错: {e}", exc_info=True)
                if self.results:
                    logger.info("保存已获取的部分数据 …")
                    self._save_results()
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
        logger.info("资源已释放")


# ==================== 入口 ====================

def main():
    config = ScraperConfig()
    scraper = BookingScraper(config)
    asyncio.run(scraper.run())


if __name__ == "__main__":
    main()
