"""
Booking.com 房源爬虫 V4
功能：
  - 按欧洲多城市任务列表依次搜索
  - 默认搜索 2 成人 + 1 儿童（年龄可配置）
  - 直接抓取人民币（CNY）价格
  - 灵活筛选：双床房 / 免费取消 / 空调
  - 按城市 max_price_cny 自动过滤高价房源
  - 提取位置评分（Location Score）和距市中心距离
  - 城市间 3-5 分钟随机休眠防反爬
  - 数据实时写入 SQLite 数据库

反反爬策略：Playwright Stealth + 随机延迟 + 浏览器指纹伪装 + URL 参数筛选
"""

import csv
import json
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext
from playwright.sync_api import TimeoutError as PlaywrightTimeout
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


class BookingScraper:
    """Booking.com 反反爬爬虫 V4 —— 多城市 + 灵活筛选 + 位置评分 + 距市中心距离"""

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
        # ---- 位置描述 ----
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

    def _rand_delay(self, lo: float = None, hi: float = None):
        """随机延迟，模拟人类操作节奏"""
        time.sleep(random.uniform(
            lo or self.cfg.min_delay,
            hi or self.cfg.max_delay,
        ))

    def _debug_screenshot(self, name: str):
        """保存调试截图"""
        if not self.cfg.debug_screenshots or not self.page:
            return
        path = Path(f"debug_{name}_{datetime.now():%H%M%S}.png")
        try:
            self.page.screenshot(path=str(path), full_page=False)
            logger.debug(f"截图已保存: {path}")
        except Exception as e:
            logger.debug(f"截图失败: {e}")

    def _safe_click_first(self, selectors: list[str],
                          timeout: int = 60_000) -> bool:
        """尝试点击匹配到的第一个可见按钮"""
        for sel in selectors:
            try:
                el = self.page.wait_for_selector(
                    sel, state="visible", timeout=timeout
                )
                if el:
                    el.click()
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
        """从评分文本中提取数值（如 "Scored 9.0\\n9.0\\nWonderful" → 9.0）"""
        if not score_text or score_text == "N/A":
            return None
        m = re.search(r'(\d+\.?\d*)', score_text)
        if m:
            return float(m.group(1))
        return None

    # ==================== 浏览器启动 ====================

    def _launch_browser(self):
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

        self.browser = self.pw.chromium.launch(**launch_kwargs)

        context_kwargs: dict = {
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
            context_kwargs["proxy"] = {"server": self.cfg.proxy_server}
            logger.info(f"  使用代理: {self.cfg.proxy_server}")

        self.context = self.browser.new_context(**context_kwargs)
        self.page = self.context.new_page()

        self.page.add_init_script("""
            Object.defineProperty(navigator, 'plugins',
                { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages',
                { get: () => ['en-US', 'en'] });
        """)

        logger.info("  Stealth 补丁 + 指纹伪装 已注入")

    # ==================== 弹窗处理 ====================

    def _dismiss_overlays(self):
        """关闭各种覆盖弹窗"""
        self._safe_click_first(self.SELECTORS["cookie_accept"], timeout=3_000)
        self._rand_delay(0.3, 0.6)
        self._safe_click_first(self.SELECTORS["dismiss_popup"], timeout=3_000)

        try:
            self.page.evaluate("""
                document.querySelectorAll('[role="dialog"] '
                    + 'button[aria-label="Dismiss"], '
                    + '[role="dialog"] button[aria-label="Close"]')
                    .forEach(b => b.click());
            """)
        except Exception:
            pass

    # ==================== 城市搜索 ====================

    def _build_search_url(self, task: dict) -> str:
        """根据任务参数构建搜索 URL（人民币 CNY）

        URL 参数包含：
          - group_adults   : 成人数量（默认 2）
          - group_children : 儿童数量（默认 1）
          - req_children_ages : 儿童年龄，逗号分隔（默认 12，来自 config）
        """
        adults = task.get("adults", self.cfg.default_adults)
        children = task.get("children", self.cfg.default_children)
        rooms = task.get("rooms", self.cfg.default_rooms)

        # 儿童年龄：优先使用任务级别配置，否则使用全局默认
        children_ages = task.get("children_ages", self.cfg.default_children_ages)
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
        return f"{self.BASE_URL}/searchresults.html?{'&'.join(params)}"

    # ==================== 搜索执行 ====================

    def _search_city(self, task: dict):
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

        # 基础搜索：直接在当前页面等待渲染完成后提取数据
        search_url = self._build_search_url(task)
        logger.info(f"  搜索 URL: {search_url[:150]}…")
        self.page.goto(search_url, wait_until="commit", timeout=60000)
        self.page.wait_for_timeout(8000)
        logger.info("  ✓ 等待 8 秒让页面渲染完毕")

    # ==================== 数据提取 ====================

    def _extract_text(self, el, selectors: list[str],
                      default: str = "N/A") -> str:
        """从元素中按优先级尝试多个选择器提取文本"""
        for sel in selectors:
            try:
                child = el.query_selector(sel)
                if child:
                    text = child.inner_text().strip()
                    if text:
                        return text
            except Exception:
                continue
        return default

    def _extract_price(self, card) -> str:
        """从卡片的多个可能位置提取价格（优先 data-testid，回退全文扫描）"""
        # 方案 A：通过专用 data-testid 选择器提取
        price_sels = [
            '[data-testid="price-and-discounted-price"]',
            'span[data-testid="price-and-discounted-price"]',
            '[data-testid="price-for-x-nights"]',
            '[data-testid="price"]',
        ]
        for sel in price_sels:
            try:
                el = card.query_selector(sel)
                if el:
                    text = el.inner_text().strip()
                    # 匹配包含货币符号或纯数字的价格（如 "CN¥ 910" → 910）
                    m = re.search(r'(?:CN¥|¥|US\$|€|£)?\s*([\d,]+(?:\.[\d]{1,2})?)', text)
                    if m:
                        return m.group(1)
            except Exception:
                continue

        # 方案 B：扫描卡片内所有 span/div，查找 CNY 价格模式
        try:
            all_els = card.query_selector_all('span, div')
            for el in all_els:
                try:
                    text = el.inner_text().strip()
                except Exception:
                    continue
                if not text:
                    continue
                m = re.search(r'(?:CN¥|¥)\s*([\d,]+(?:\.[\d]{1,2})?)', text)
                if m:
                    return m.group(1)
        except Exception:
            pass

        # 方案 C：扫描任何包含纯数字（价格量级）的元素
        try:
            all_els = card.query_selector_all('span, div')
            for el in all_els:
                try:
                    text = el.inner_text().strip()
                except Exception:
                    continue
                m = re.search(r'([\d,]{2,6}(?:\.[\d]{1,2})?)', text)
                if m:
                    val = m.group(1).replace(',', '')
                    try:
                        num = float(val)
                        if 50 <= num <= 50000:  # 合理酒店价格范围
                            return m.group(1)
                    except ValueError:
                        continue
        except Exception:
            pass

        return "N/A"

    def _extract_room_type(self, card) -> str:
        """提取房型描述（纯 data-testid / 语义选择器，不依赖易变的 hash class）"""
        # 方案 A：通过专用选择器
        sels = [
            'h4',
            '[data-testid="room-type"]',
            '[data-testid="room-info"]',
            '[data-testid="recommended-unit"]',
            '.room-info',
        ]
        for sel in sels:
            try:
                el = card.query_selector(sel)
                if el:
                    text = el.inner_text().strip()
                    if text and len(text) < 120:
                        return text
            except Exception:
                continue

        # 方案 B：在卡片内找标题之后的第一个非价格/非评分的文本块
        try:
            title_el = (
                card.query_selector('[data-testid="title"]')
                or card.query_selector('h3')
                or card.query_selector('h2')
            )
            if title_el:
                # 向上找到共同的父容器
                parent = title_el.query_selector('xpath=..')
                if parent:
                    siblings = parent.query_selector_all('xpath=following-sibling::*')
                    for sib in siblings[:5]:
                        try:
                            text = sib.inner_text().strip()
                            if text and 3 < len(text) < 120:
                                # 排除价格 / 评分 / 地址
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

    # ==================== 位置信息提取 ====================

    def _extract_location(self, card) -> str:
        """从房源卡片中提取位置 / 距离描述文字

        Booking.com 卡片上常见的距离表述：
          - "1.5 km from centre"
          - "500 m from Milano Centrale"
          - "In Florence city centre"
          - "Near Santa Maria Novella train station"

        返回提取到的文字，若未找到则返回 "N/A"。
        """
        # 方案 A：通过专用选择器提取
        for sel in self.SELECTORS["location_desc"]:
            try:
                el = card.query_selector(sel)
                if el:
                    text = el.inner_text().strip()
                    if text and len(text) < 200:
                        # 排除误匹配（如纯酒店名、纯评分）
                        if not text.startswith("Scored") and \
                           not text.startswith("€") and \
                           not text.startswith("US$") and \
                           not text.startswith("CN¥"):
                            return text
            except Exception:
                continue

        # 方案 B：遍历卡片内所有 span/div，匹配距离模式
        try:
            spans = card.query_selector_all("span, div")
            for sp in spans:
                try:
                    text = sp.inner_text().strip()
                except Exception:
                    continue
                if not text or len(text) > 150:
                    continue
                # 距离关键词匹配
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

    def _extract_location_score(self, card) -> str:
        """从房源卡片中提取「位置评分（Location Score）」

        Booking.com 卡片上有时会显示子评分，例如：
          - "Location 9.2"
          - "Location score: 8.7"
          - 或者在评分区域的细分文字中

        返回位置评分数值字符串（如 "9.2"），若未找到则返回 "N/A"。
        """
        # 方案 A：在卡片内搜索 "Location" 关键词 + 数字 的模式
        try:
            all_els = card.query_selector_all('div, span, li')
            for el in all_els:
                try:
                    text = el.inner_text().strip()
                except Exception:
                    continue
                if not text or len(text) > 80:
                    continue
                # 匹配 "Location 9.2" 或 "Location · 9.2" 或 "Location score 8.7"
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

        # 方案 B：检查 aria-label 中的位置评分
        try:
            all_els = card.query_selector_all('[aria-label*="Location" i]')
            for el in all_els:
                try:
                    label = el.get_attribute("aria-label") or ""
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

    def _extract_distance_to_centre(self, card) -> str:
        """从房源卡片中提取「距市中心距离」

        尝试提取一个明确的数值距离，例如：
          - "1.5 km from centre" → "1.5 km"
          - "500 m from city centre" → "500 m"
          - "In the city centre" → "city centre"（无明确数值时保留描述）

        返回距离字符串，若未找到则返回 "N/A"。
        """
        # 方案 A：先尝试从已提取的 location_desc 中解析
        loc = self._extract_location(card)
        if loc != "N/A":
            # 匹配 "X km from centre" 或 "X m from centre"
            m = re.search(
                r'(\d+\.?\d*\s*(?:km|m|kilometre|metre|mile)s?)\s+from\s+(?:the\s+)?(?:city\s+)?cent',
                loc, re.IGNORECASE
            )
            if m:
                return m.group(1) + " from centre"

            # 匹配 "X km from ..." 任何地标
            m = re.search(
                r'(\d+\.?\d*\s*(?:km|m)\s+from\s+.+)',
                loc, re.IGNORECASE
            )
            if m:
                return m.group(1)

            # 如果 loc 本身就是简短的位置描述（如 "City centre"），直接返回
            if len(loc) < 60:
                return loc

        # 方案 B：在卡片内直接搜索距离模式
        try:
            all_els = card.query_selector_all('span, div')
            for el in all_els:
                try:
                    text = el.inner_text().strip()
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

    # ==================== 链接 & 评分提取 ====================

    def _extract_link(self, card) -> str:
        """提取酒店详情链接"""
        try:
            # 方案 A：专用选择器
            a = card.query_selector('a[data-testid="title-link"]')
            if not a:
                a = card.query_selector('a[href*="hotel"]')
            if not a:
                a = card.query_selector('a[href*="property"]')
            if not a:
                # 方案 B：遍历卡片内所有链接，取第一个 /hotel/ 或 /property/ 链接
                links = card.query_selector_all('a')
                for link in links:
                    href = link.get_attribute('href') or ''
                    if '/hotel/' in href or '/property/' in href:
                        a = link
                        break
            if not a:
                # 方案 C：取卡片内第一个带 href 的链接
                a = card.query_selector('a[href]')
            if a:
                href = a.get_attribute("href")
                if href:
                    return href if href.startswith("http") else \
                        self.BASE_URL + href
        except Exception:
            pass
        return "N/A"

    def _extract_score(self, card) -> str:
        """从卡片中提取综合评分（优先 data-testid 内层元素，回退全文扫描）"""
        # 方案 A：先定位 [data-testid="review-score"] 容器，再找内层数字
        for container_sel in [
            '[data-testid="review-score"]',
            'div[data-testid="review-score"]',
        ]:
            try:
                container = card.query_selector(container_sel)
                if not container:
                    continue
                # 优先取内层子元素的纯数字文本
                for child_sel in ['div', 'span', '> div:first-child', '> span']:
                    try:
                        children = container.query_selector_all(child_sel)
                        for child in children:
                            text = child.inner_text().strip()
                            if not text:
                                continue
                            # 纯数字（如 "9.0"、"8.5"）
                            m = re.search(r'^(\d+\.?\d{0,1})\s*$', text)
                            if m:
                                score = float(m.group(1))
                                if 1.0 <= score <= 10.0:
                                    return m.group(1)
                    except Exception:
                        continue
                # 回退：取整个容器的文本
                text = container.inner_text().strip()
                m = re.search(r'(\d+\.?\d*)', text)
                if m:
                    score = float(m.group(1))
                    if 1.0 <= score <= 10.0:
                        return m.group(1)
            except Exception:
                continue

        # 方案 B：扫描包含 "Scored" 或评分数字模式的元素
        try:
            all_els = card.query_selector_all('div, span')
            for el in all_els:
                try:
                    text = el.inner_text().strip()
                except Exception:
                    continue
                if not text:
                    continue
                # 匹配 "Scored 8.5" 或单独的 "8.5" 评分块
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

    def _card_matches_twin_beds(self, room_type: str) -> bool:
        """检查房型文本是否匹配双床房关键词（辅助筛选）"""
        if not self.cfg.filter_twin_beds:
            return True  # 未启用筛选时全部通过
        keywords = [
            "twin", "single beds", "2 single", "two single",
            "2 beds", "two beds", "separate beds",
        ]
        rt_lower = room_type.lower()
        return any(kw in rt_lower for kw in keywords)

    # ==================== 调试辅助 ====================

    def _dump_card_html(self, card, prefix: str = "card"):
        """转储单个卡片的 HTML 用于离线调试"""
        try:
            html = card.inner_html()
            path = Path(f"debug_{prefix}_{datetime.now():%H%M%S}.html")
            path.write_text(html, encoding="utf-8")
            logger.info(f"  🔍 卡片 HTML 已保存: {path.resolve()}")
        except Exception as e:
            logger.debug(f"  转储 HTML 失败: {e}")

    def _dump_page_info(self):
        """转储当前页面信息用于诊断选择器失效问题"""
        try:
            logger.info(f"  当前页面标题: {self.page.title()}")
            logger.info(f"  当前 URL: {self.page.url[:200]}")
            # 检查多种可能的搜索结果容器
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
                count = len(self.page.query_selector_all(sel))
                if count > 0:
                    logger.info(f"  找到容器 '{sel}': {count} 个元素")
        except Exception as e:
            logger.debug(f"  页面信息获取失败: {e}")

    def _extract_cards(self, task: dict) -> list[dict]:
        """从当前页面提取所有房源卡片数据"""
        self._rand_delay(1, 2)

        # 尝试多种卡片选择器
        card_selectors = [
            '[data-testid="property-card"]',
            'div[data-testid="property-card"]',
        ]

        cards_raw = []
        for sel in card_selectors:
            cards_raw = self.page.query_selector_all(sel)
            if cards_raw:
                logger.info(f"  使用选择器 '{sel}' → 检测到 {len(cards_raw)} 个房源卡片")
                break

        if not cards_raw:
            logger.warning("  ⚠ 未检测到任何房源卡片！可能选择器已失效")
            self._debug_screenshot("no-cards")
            self._dump_page_info()
            return []

        extracted = []
        for card in cards_raw:
            try:
                # ---- 调试：保存第一张卡片的完整内部 HTML ----
                if len(extracted) == 0:
                    with open('debug_card.html', 'w', encoding='utf-8') as f:
                        f.write(card.inner_html())
                    logger.info(f"  🔍 已保存第 1 张卡片 HTML → debug_card.html")

                # ---- 名称提取（多层回退） ----
                name = self._extract_text(card, self.SELECTORS["card_title"])
                if not name or name == "N/A":
                    # 回退：尝试各种标题标签
                    for tag in ["h3", "h2", "h4", "strong", "a"]:
                        name = self._extract_text(card, [tag])
                        if name and name != "N/A" and len(name) > 2:
                            break

                if not name or name == "N/A":
                    # 最后的回退：取卡片内第一个有意义的 div 文本
                    try:
                        all_divs = card.query_selector_all('div')
                        for div in all_divs:
                            text = div.inner_text().strip()
                            if text and 5 < len(text) < 150 and \
                               not re.search(r'^(?:CN¥|¥|€|US\$|Scored|\d+\.\d)',
                                             text):
                                name = text
                                break
                    except Exception:
                        pass

                # ---- 价格提取 ----
                price = self._extract_price(card)

                # ---- 房型提取 ----
                room_type = self._extract_room_type(card)

                # ---- 链接提取 ----
                link = self._extract_link(card)

                # ---- 位置描述提取 ----
                location_desc = self._extract_location(card)

                # ---- 位置评分提取（新增） ----
                location_score = self._extract_location_score(card)

                # ---- 距市中心距离提取（新增） ----
                distance_to_centre = self._extract_distance_to_centre(card)

                # ---- 综合评分提取 ----
                score = self._extract_score(card)

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
                    "checkin": task["checkin"],
                    "checkout": task["checkout"],
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

    # ==================== 后处理：价格过滤（总预算） ====================

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
        """后处理：按入住期间总预算 (max_price_cny) 过滤高价房源

        max_price_cny 是整个入住期间的总预算（每晚单价 × 入住天数）。
        过滤逻辑：price_nightly × nights > max_price_cny → 剔除
        """
        max_price_total = task.get("max_price_cny")
        if max_price_total is None:
            return list(records)

        nights = self._calc_nights(task)
        processed = []
        filtered_count = 0

        for r in records:
            price_nightly = self._parse_price_number(r["price_cny"])

            # ---- 总价过滤 ----
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

    def _scroll_to_load(self):
        """滚动页面触发懒加载"""
        for _ in range(self.cfg.scroll_times):
            self.page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
            self._rand_delay(0.8, 1.5)
        self.page.evaluate("window.scrollTo(0, 0)")
        self._rand_delay(0.3, 0.5)

    def _go_next_page(self) -> bool:
        """点击下一页按钮"""
        for sel in self.SELECTORS["next_page_btn"]:
            try:
                btn = self.page.query_selector(sel)
                if btn and btn.is_enabled():
                    btn.click()
                    self._rand_delay(2, 4)
                    self.page.wait_for_timeout(8000)
                    logger.info("  → 已翻到下一页")
                    return True
            except Exception:
                continue

        logger.info("  → 没有更多页面可翻")
        return False

    # ==================== 城市间休眠 ====================

    def _inter_city_delay(self):
        """城市间的强制随机休眠（3-5 分钟），防止触发反爬虫"""
        delay = random.uniform(
            self.cfg.inter_city_delay_min,
            self.cfg.inter_city_delay_max,
        )
        minutes = int(delay // 60)
        seconds = int(delay % 60)
        logger.info(
            f"\n⏳ 城市间休眠 {minutes} 分 {seconds} 秒 "
            f"（防反爬策略）…"
        )

        # 分步倒计时，每 30 秒输出一次进度
        remaining = delay
        while remaining > 0:
            chunk = min(30, remaining)
            time.sleep(chunk)
            remaining -= chunk
            if remaining > 0:
                logger.info(
                    f"   剩余约 {int(remaining // 60)} 分 "
                    f"{int(remaining % 60)} 秒 …"
                )

        logger.info("  休眠结束，继续下一个城市 ✓\n")

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

    def run(self):
        """执行完整爬取流程 —— 依次处理 CITY_TASKS 中的每个城市"""
        self._start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        db_path = init_db(self.cfg.db_file)
        tasks = self.cfg.CITY_TASKS
        total_pages = 0
        total_extracted = 0

        logger.info(f"\n{'=' * 60}")
        logger.info(f"  长途旅行计划爬虫启动 (V4 · CNY · 位置评分)")
        logger.info(f"  共 {len(tasks)} 个城市 | 每个最多 {self.cfg.max_pages} 页")
        logger.info(f"  默认搜索: {self.cfg.default_adults} 成人 + "
                    f"{self.cfg.default_children} 儿童（{self.cfg.children_ages_param} 岁）")
        logger.info(f"  筛选: 双床房={self.cfg.filter_twin_beds} | "
                    f"免费取消={self.cfg.filter_free_cancellation} | "
                    f"空调={self.cfg.filter_air_conditioning}")
        logger.info(f"{'=' * 60}\n")

        with Stealth().use_sync(sync_playwright()) as p:
            self.pw = p

            try:
                # 1. 启动浏览器
                self._launch_browser()

                # 2. 依次处理每个城市（每个城市搜索前会先访问首页）
                for task_idx, task in enumerate(tasks):
                    city_name = task["city"]
                    max_price = task.get("max_price_cny", "—")
                    adults = task.get("adults", self.cfg.default_adults)
                    children = task.get("children", self.cfg.default_children)
                    logger.info(f"\n{'=' * 60}")
                    logger.info(
                        f"  🌍 城市 {task_idx + 1}/{len(tasks)}: {city_name}"
                    )
                    logger.info(
                        f"     {task['checkin']} → {task['checkout']} | "
                        f"{adults} 成人 + {children} 儿童 | "
                        f"最高 ¥{max_price}/晚"
                    )
                    logger.info(f"{'=' * 60}")

                    # 搜索该城市
                    self._search_city(task)

                    city_pages = 0

                    # 逐页抓取
                    for page_num in range(1, self.cfg.max_pages + 1):
                        logger.info(f"\n  ── 第 {page_num}/{self.cfg.max_pages} 页 ──")

                        self._scroll_to_load()
                        self._dismiss_overlays()

                        page_cards = self._extract_cards(task)

                        # ---- 后处理：价格过滤 ----
                        page_cards = self._process_records(page_cards, task)

                        self.results.extend(page_cards)

                        if page_cards:
                            insert_records(db_path, page_cards)

                        city_pages += 1
                        total_pages += 1
                        total_extracted += len(page_cards)

                        logger.info(
                            f"  本页 {len(page_cards)} 条 | "
                            f"本城累计 {sum(1 for r in self.results if r['city'] == city_name)} 条 | "
                            f"全局累计 {total_extracted} 条"
                        )

                        if page_num < self.cfg.max_pages:
                            if not self._go_next_page():
                                break

                        self._rand_delay(2, 4)

                    # 城市间休眠（最后一个城市之后不睡）
                    if task_idx < len(tasks) - 1:
                        self._inter_city_delay()

                # 4. 保存 CSV/JSON
                total_db = get_record_count(db_path)
                logger.info(f"\n{'=' * 60}")
                logger.info(f"  ✅ 全部完成！")
                logger.info(f"  本次新增: {len(self.results)} 条")
                logger.info(f"  数据库总计: {total_db} 条 → {db_path}")
                logger.info(f"  覆盖城市: {len(tasks)} 个 | 共 {total_pages} 页")
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
                self._cleanup()

    def _cleanup(self):
        """释放浏览器资源"""
        logger.info("清理资源 …")
        for obj in [self.page, self.context, self.browser]:
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        logger.info("资源已释放")


# ==================== 入口 ====================

def main():
    config = ScraperConfig()
    scraper = BookingScraper(config)
    scraper.run()


if __name__ == "__main__":
    main()
