"""
Booking.com 爬虫配置文件
修改此文件中的参数来定制爬取行为

核心改动（V4）：
  - 默认搜索参数：2 成人 + 1 儿童（年龄可配置）
  - 新增 EUROPE_TRIP_CITIES 欧洲多城市旅行计划模板
  - 移除 price_per_score（性价比）逻辑
  - 新增 location_score（位置评分）和 distance_to_centre（距市中心距离）字段
  - 三个筛选开关：双床房 / 免费取消 / 空调
  - 城市间随机休眠 3-5 分钟，防止触发反爬
"""


class ScraperConfig:
    """爬虫全局配置"""

    # ==================== 搜索人数默认参数 ====================
    # 这些是全局默认值，CITY_TASKS 中每个任务可以单独覆盖

    default_adults: int = 2
    default_children: int = 1
    default_children_ages: list[int] = [12]  # 儿童年龄，可随时修改
    default_rooms: int = 1

    # ==================== 儿童年龄（URL 参数用） ====================
    # Booking.com 要求指定每个儿童的年龄，以逗号分隔
    # 例如：一个 12 岁儿童 → "12"；两个儿童（10 岁和 8 岁）→ "10,8"

    @property
    def children_ages_param(self) -> str:
        """将 children_ages 列表转为 URL 参数格式"""
        return ",".join(str(age) for age in self.default_children_ages)

    # ==================== 欧洲多城市旅行计划模板 ====================
    # EUROPE_TRIP_CITIES：完整的欧洲旅行城市清单
    # 这是一个清晰的字典数组结构，方便你填入整个旅行计划的所有城市
    # 每个城市的字段说明：
    #   city          - 城市/区域名称（Booking.com 搜索关键词）
    #   checkin       - 入住日期 (YYYY-MM-DD)
    #   checkout      - 退房日期 (YYYY-MM-DD)
    #   adults        - 成人数量（默认 2）
    #   children      - 儿童数量（默认 1）
    #   children_ages - 儿童年龄列表（默认 [12]，覆盖全局默认）
    #   rooms         - 房间数（默认 1）
    #   max_price_cny - 整个入住期间的总预算（人民币），null 表示不过滤
    #   notes         - 备注（仅用于你自己记录，不影响爬虫）

    # max_price_cny 现在是整个入住期间的总预算（按每晚上限 ¥1,500 × 入住天数计算）
    EUROPE_TRIP_CITIES: list[dict] = [
        {
            "city": "Stuttgart",
            "checkin": "2026-08-01",
            "checkout": "2026-08-03",
            "adults": 2,
            "children": 1,
            "children_ages": [12],
            "rooms": 1,
            "max_price_cny": 3000,
            "notes": "德国 · 斯图加特",
        },
        {
            "city": "Paris Chatelet",
            "checkin": "2026-08-03",
            "checkout": "2026-08-06",
            "adults": 2,
            "children": 1,
            "children_ages": [12],
            "rooms": 1,
            "max_price_cny": 4500,
            "notes": "法国 · 巴黎",
        },
        {
            "city": "Avignon",
            "checkin": "2026-08-06",
            "checkout": "2026-08-09",
            "adults": 2,
            "children": 1,
            "children_ages": [12],
            "rooms": 1,
            "max_price_cny": 4500,
            "notes": "法国 · 阿维尼翁",
        },
        {
            "city": "Milan Central Station",
            "checkin": "2026-08-09",
            "checkout": "2026-08-12",
            "adults": 2,
            "children": 1,
            "children_ages": [12],
            "rooms": 1,
            "max_price_cny": 4500,
            "notes": "意大利 · 米兰",
        },
        {
            "city": "Venice Mestre",
            "checkin": "2026-08-12",
            "checkout": "2026-08-15",
            "adults": 2,
            "children": 1,
            "children_ages": [12],
            "rooms": 1,
            "max_price_cny": 4500,
            "notes": "意大利 · 威尼斯",
        },
        {
            "city": "Florence Santa Maria Novella",
            "checkin": "2026-08-15",
            "checkout": "2026-08-17",
            "adults": 2,
            "children": 1,
            "children_ages": [12],
            "rooms": 1,
            "max_price_cny": 3000,
            "notes": "意大利 · 佛罗伦萨",
        },
        {
            "city": "Pienza",
            "checkin": "2026-08-17",
            "checkout": "2026-08-20",
            "adults": 2,
            "children": 1,
            "children_ages": [12],
            "rooms": 1,
            "max_price_cny": 4500,
            "notes": "意大利 · 皮恩扎",
        },
        {
            "city": "Barcelona",
            "checkin": "2026-08-20",
            "checkout": "2026-08-23",
            "adults": 2,
            "children": 1,
            "children_ages": [12],
            "rooms": 1,
            "max_price_cny": 4500,
            "notes": "西班牙 · 巴塞罗那",
        },
        {
            "city": "Madrid",
            "checkin": "2026-08-23",
            "checkout": "2026-08-25",
            "adults": 2,
            "children": 1,
            "children_ages": [12],
            "rooms": 1,
            "max_price_cny": 3000,
            "notes": "西班牙 · 马德里",
        },
    ]

    # ==================== 城市任务列表（实际执行） ====================
    # 程序会按列表顺序依次搜索每个城市
    # 默认使用 EUROPE_TRIP_CITIES 作为数据源
    # 如需快速测试，可以只保留前几个城市，其余注释掉

    CITY_TASKS: list[dict] = EUROPE_TRIP_CITIES

    # ==================== 筛选开关 ====================
    # 三个独立开关，设为 True 启用对应筛选条件

    # 双床房 / 独立单人床：启用后会在筛选栏中尝试勾选 Twin beds 选项，
    # 并辅助以房型关键词匹配（twin / single / 2 single beds 等）
    filter_twin_beds: bool = True

    # 免费取消：Booking.com 的 "Free cancellation" 筛选
    filter_free_cancellation: bool = True

    # 空调：设施代码 hotelfacility=11
    filter_air_conditioning: bool = True

    # ==================== 房型 / 设施筛选代码 ====================
    # 以下为 Booking.com nflt 参数中使用的筛选代码
    # 格式：key%3Dvalue，多个以 %3B 连接

    # 房型筛选：Apartments（公寓）  ht_id=201
    # 其他可选：204=Holiday homes, 206=Villas, 213=Hotels
    property_type_filter: str = "ht_id%3D201"

    # 基础设施：洗衣机  facility=46
    facility_washing_machine: str = "hotelfacility%3D46"

    # 空调  facility=11
    facility_air_conditioning: str = "hotelfacility%3D11"

    # 免费取消（nflt 参数中的表示）
    filter_free_cancellation_code: str = "fc%3D2"

    # ==================== 城市间休眠 ====================
    # 为防止触发反爬虫，每个城市任务之间强制随机休眠
    inter_city_delay_min: float = 180.0   # 3 分钟
    inter_city_delay_max: float = 300.0   # 5 分钟

    # ==================== 爬虫行为 ====================
    headless: bool = False          # 无头模式（True = 后台运行，不显示浏览器窗口）
    max_pages: int = 3              # 每个城市最多爬取的页数
    min_delay: float = 1.0          # 页面操作最小间隔（秒）
    max_delay: float = 3.0          # 页面操作最大间隔（秒）
    page_timeout: int = 60_000      # 页面加载超时（毫秒）
    scroll_times: int = 3           # 每页滚动次数（加载懒加载内容）

    # ==================== 输出 ====================
    db_file: str = "booking_data.db"
    output_file: str = "booking_results.csv"
    output_format: str = "csv"              # csv 或 json
    save_to_csv: bool = True
    debug_screenshots: bool = True

    # ==================== 浏览器伪装 ====================
    browser_locale: str = "en-US"
    viewport_width: int = 1920
    viewport_height: int = 1080
    use_local_chrome: bool = True

    # ==================== 代理（可选） ====================
    proxy_server: str = ""          # 例如 "http://127.0.0.1:7890"
