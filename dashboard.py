"""
Streamlit 数据可视化看板 —— 酒店房源监控
读取 booking_data.db，提供城市 / 时间 / 价格筛选，散点图（价格 × AI 评分）+ 数据表格
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from config import ScraperConfig
from database import get_latest_flight_prices, get_flight_record_count

# ==================== 页面设置 & CSS 样式覆写 ====================
st.set_page_config(
    page_title="酒店房源监控看板",
    page_icon="🏨",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 隐藏 Streamlit 默认顶部菜单栏和底部标志 + 微调样式
st.markdown("""
<style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}

    /* 侧边栏 expander 间距紧凑 */
    [data-testid="stSidebar"] .stExpander {
        margin-bottom: 0.25rem;
    }

    /* 指标数值加粗 */
    [data-testid="stMetricValue"] {
        font-weight: 700;
    }

    /* container 区块间距 */
    .stContainer {
        margin-bottom: 1.5rem;
    }
</style>
""", unsafe_allow_html=True)

# ==================== 辅助函数 ====================

def _parse_price(val):
    """将 price_cny 字符串解析为 float"""
    if pd.isna(val) or val == "N/A":
        return None
    try:
        return float(str(val).replace(",", "").replace("¥", "").replace("CN", "").strip())
    except (ValueError, TypeError):
        return None


def _parse_score(val):
    """将评分字符串解析为 float"""
    if pd.isna(val) or val == "N/A":
        return None
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return None


def _build_date_slider(df, date_col, label, fmt):
    """通用日期范围 slider 工厂 —— 返回 (lo, hi) 或 None"""
    valid = df[date_col].dropna()
    if valid.empty:
        return None
    lo = valid.min().to_pydatetime()
    hi = valid.max().to_pydatetime()
    if lo == hi:
        lo -= timedelta(days=1)
        hi += timedelta(days=1)
    return st.sidebar.slider(label, min_value=lo, max_value=hi, value=(lo, hi), format=fmt)


def _fmt_price(x):
    """将数值格式化为 ¥1,234 风格"""
    return f"¥{x:,.0f}" if pd.notna(x) else "N/A"


def _fmt_score(x, decimals=1):
    """将评分格式化为保留 1 位小数"""
    return f"{x:.{decimals}f}" if pd.notna(x) else "N/A"


# ==================== 配置 & 缓存数据加载 ====================

@st.cache_data
def get_config_cities() -> list[str]:
    """从 ScraperConfig 中提取城市名称列表（保持旅行计划顺序）"""
    config = ScraperConfig()
    cities = []
    for task in config.CITY_TASKS:
        city = task.get("city", "")
        if city and city not in cities:
            cities.append(city)
    return cities


CONFIG_CITIES = get_config_cities()
DB_PATH = str(Path(__file__).parent / "booking_data.db")


@st.cache_data(ttl=60)
def load_hotel_data(db_path: str) -> pd.DataFrame:
    """从 SQLite 读取全部酒店记录，预处理派生列"""
    if not Path(db_path).exists():
        return pd.DataFrame()

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='hotels'"
        )
        if cur.fetchone() is None:
            return pd.DataFrame()
        df = pd.read_sql_query("SELECT * FROM hotels ORDER BY id DESC", conn)
    finally:
        conn.close()

    if df.empty:
        return df

    # ---- 派生数值列 ----
    df["price_num"] = df["price_cny"].apply(_parse_price)
    df["score_num"] = df["review_score"].apply(_parse_score)

    if "ai_score" in df.columns:
        df["ai_score_num"] = df["ai_score"].apply(_parse_score)
    else:
        df["ai_score_num"] = None

    if "location_score" in df.columns:
        df["location_score_num"] = df["location_score"].apply(_parse_score)
    else:
        df["location_score_num"] = None

    # ---- 日期列 ----
    df["scraped_at_dt"] = pd.to_datetime(df["scraped_at"], errors="coerce")
    df["checkin_dt"] = pd.to_datetime(df["checkin"], errors="coerce")
    df["checkout_dt"] = pd.to_datetime(df["checkout"], errors="coerce")

    return df


# ==================== 数据加载（带 spinner + toast） ====================

with st.spinner("🔄 正在从数据库加载酒店数据 …"):
    df = load_hotel_data(DB_PATH)

if not df.empty:
    st.toast(f"✅ 数据加载完成，共 {len(df):,} 条酒店记录", icon="✅")

# ==================== 城市名称中英文映射 ====================

CITY_NAME_MAP = {
    "Stuttgart City Centre": "斯图加特",
    "Paris Chatelet": "巴黎",
    "Nice City Centre": "尼斯",
    "Milan Central Station": "米兰",
    "Venice": "威尼斯",
    "Florence Santa Maria Novella": "佛罗伦萨",
    "Pienza": "皮恩扎",
    "Barcelona City Centre": "巴塞罗那",
    "Madrid City Centre": "马德里",
}

# 使用 map 将数据中的城市英文名翻译为中文名（未匹配到映射时保留英文原名）
if not df.empty:
    df["city"] = df["city"].map(CITY_NAME_MAP).fillna(df["city"])

# 同步翻译 CONFIG_CITIES（供侧边栏筛选使用）
CONFIG_CITIES_CN = [CITY_NAME_MAP.get(c, c) for c in CONFIG_CITIES]

# ==================== 侧边栏 —— 筛选面板 ====================

with st.sidebar:
    st.header("🔍 数据筛选")

    # ---- 全局搜索 ----
    search_query = st.sidebar.text_input("🔍 全局搜索", "")

    # ---- 城市选择 ----
    with st.expander("🏙️ 城市选择", expanded=False):
        if not df.empty:
            db_cities = set(df["city"].dropna().unique())
            available = [c for c in CONFIG_CITIES_CN if c in db_cities]
            extra = sorted(db_cities - set(CONFIG_CITIES_CN))
            all_available = available + extra

            selected_cities = st.multiselect(
                "按旅行计划顺序选择城市",
                options=all_available,
                default=all_available,
                placeholder="选择城市 …",
                label_visibility="collapsed",
            )
        else:
            selected_cities = []

    # ---- 时间范围 ----
    with st.expander("📅 时间范围", expanded=False):
        if df.empty:
            time_range = None
            checkin_range = None
            checkout_range = None
        else:
            time_range = _build_date_slider(
                df, "scraped_at_dt", "抓取时间", "MM-DD HH:mm",
            )
            checkin_range = _build_date_slider(
                df, "checkin_dt", "🏨 入住日期", "YYYY-MM-DD",
            )
            checkout_range = _build_date_slider(
                df, "checkout_dt", "🏨 退房日期", "YYYY-MM-DD",
            )

    # ---- 价格筛选 ----
    with st.expander("💰 价格筛选", expanded=False):
        if not df.empty:
            valid_prices = df["price_num"].dropna()
            if not valid_prices.empty:
                p_min = max(int(valid_prices.min()), 0)
                p_max = int(valid_prices.max()) + 100
                max_price = st.slider(
                    "最高价格 (CNY / 晚)",
                    min_value=p_min,
                    max_value=p_max,
                    value=p_max,
                    step=50,
                    format="¥%d",
                )
            else:
                max_price = 99999
        else:
            max_price = 99999

    # ---- 数据摘要 ----
    st.divider()
    if not df.empty:
        valid_ci = df["checkin_dt"].dropna()
        valid_co = df["checkout_dt"].dropna()
        if not valid_ci.empty:
            st.caption(
                f"📅 入住: {valid_ci.min().strftime('%Y-%m-%d')} → "
                f"{valid_ci.max().strftime('%Y-%m-%d')}"
            )
        if not valid_co.empty:
            st.caption(
                f"📅 退房: {valid_co.min().strftime('%Y-%m-%d')} → "
                f"{valid_co.max().strftime('%Y-%m-%d')}"
            )
    latest_scrape = df["scraped_at_dt"].max() if not df.empty else None
    if pd.notna(latest_scrape):
        st.caption(f"🕐 最新抓取: {latest_scrape.strftime('%m-%d %H:%M')}")
    st.caption(f"💾 数据库: `{Path(DB_PATH).name}`")
    st.caption(f"📊 总记录: {len(df):,}" if not df.empty else "📊 总记录: 0")
    st.caption(f"🌍 旅行城市: {len(CONFIG_CITIES)} 个")

# ==================== 数据过滤 ====================

if df.empty:
    if not Path(DB_PATH).exists():
        st.info("🏨 系统正在等待首次数据抓取，请稍后刷新查看最新房源。")
    else:
        st.info("🏨 数据库中暂无数据，系统正在等待首次数据抓取，请稍后刷新查看最新房源。")
    st.stop()

mask = pd.Series(True, index=df.index)

if selected_cities:
    mask &= df["city"].isin(selected_cities)

if time_range is not None:
    mask &= (df["scraped_at_dt"] >= time_range[0]) & (df["scraped_at_dt"] <= time_range[1])

if checkin_range is not None:
    mask &= (df["checkin_dt"] >= checkin_range[0]) & (df["checkin_dt"] <= checkin_range[1])

if checkout_range is not None:
    mask &= (df["checkout_dt"] >= checkout_range[0]) & (df["checkout_dt"] <= checkout_range[1])

price_mask = df["price_num"].isna() | (df["price_num"] <= max_price)
mask &= price_mask

# ---- 全局搜索：在酒店名称、地址、描述等所有文本列中匹配关键词 ----
if search_query:
    mask &= df.astype(str).apply(
        lambda x: x.str.contains(search_query, case=False, na=False)
    ).any(axis=1)

filtered = df[mask].copy()

# ==================== 主界面 Tab 切换 ====================

tab_hotel, tab_flight = st.tabs(["🏨 酒店分析", "✈️ 机票监控"])

# ==================== Tab 1: 酒店分析 ====================

with tab_hotel:

    # ---- 汇总指标（st.container 区块） ----
    with st.container(border=True):
        st.caption("📊 核心指标概览")
        c1, c2, c3, c4, c5 = st.columns(5)

        hotel_count = len(filtered)
        avg_price = filtered["price_num"].dropna().mean()
        avg_score = filtered["score_num"].dropna().mean()
        avg_ai = filtered["ai_score_num"].dropna().mean()
        n_cities = filtered["city"].nunique()

        c1.metric("🏠 酒店数量", f"{hotel_count:,}")
        c2.metric("🌆 覆盖城市", str(n_cities))
        c3.metric("💰 均价 (CNY)", _fmt_price(avg_price))
        c4.metric("⭐ 平均综合评分", _fmt_score(avg_score))
        c5.metric("🤖 平均 AI 评分", _fmt_score(avg_ai))

    # ---- 散点图：价格 vs AI 评分 ----
    with st.container(border=True):
        st.caption("📈 价格 vs AI 综合评分 散点图")

        scatter_df = filtered.dropna(subset=["price_num", "ai_score_num"]).copy()

        if scatter_df.empty:
            st.info("当前筛选条件下没有同时包含价格和 AI 评分的记录，无法绘制散点图。")
        else:
            # 构建 hover 信息
            scatter_df["hover_label"] = (
                scatter_df["hotel_name"].str[:40]
                + "<br>城市: " + scatter_df["city"]
                + "<br>入住: " + scatter_df["checkin"].fillna("N/A")
                + "<br>退房: " + scatter_df["checkout"].fillna("N/A")
                + "<br>价格: "
                + scatter_df["price_num"].apply(lambda x: f"¥{x:,.0f}")
                + "<br>AI 评分: "
                + scatter_df["ai_score_num"].apply(
                    lambda x: f"{x:.0f}" if pd.notna(x) else "N/A"
                )
            )

            # 气泡大小：用综合评分或位置评分
            if (
                "score_num" in scatter_df.columns
                and scatter_df["score_num"].notna().any()
            ):
                scatter_df["score_num"] = scatter_df["score_num"].fillna(0)
                size_col = "score_num"
            elif (
                "location_score_num" in scatter_df.columns
                and scatter_df["location_score_num"].notna().any()
            ):
                scatter_df["location_score_num"] = scatter_df["location_score_num"].fillna(0)
                size_col = "location_score_num"
            else:
                size_col = None

            fig = px.scatter(
                scatter_df,
                x="price_num",
                y="ai_score_num",
                color="city",
                size=size_col,
                size_max=15,
                color_discrete_sequence=px.colors.qualitative.G10,
                hover_name="hotel_name",
                hover_data={
                    "hover_label": True,
                    "price_num": False,
                    "ai_score_num": False,
                    "city": False,
                },
                custom_data=["detail_link", "hotel_name"],
                labels={
                    "price_num": "价格 (CNY / 晚)",
                    "ai_score_num": "AI 综合评分",
                    "city": "城市",
                },
                height=550,
            )

            fig.update_traces(
                hovertemplate=(
                    "%{customdata[2]}"
                    "<extra></extra>"
                ),
            )

            fig.update_layout(
                xaxis=dict(tickprefix="¥", tickformat=",d", title="价格 (CNY / 晚)"),
                yaxis=dict(
                    range=[
                        max(0, scatter_df["ai_score_num"].min() - 0.5),
                        min(10.5, scatter_df["ai_score_num"].max() + 0.5),
                    ],
                    title="AI 综合评分 (1-10)",
                ),
                legend=dict(
                    orientation="h",
                    yanchor="bottom",
                    y=1.02,
                    title=None,
                ),
                margin=dict(l=40, r=20, t=40, b=40),
            )

            selected_event = st.plotly_chart(
                fig,
                use_container_width=True,
                on_select="rerun",
                selection_mode="points",
            )

            # ---- 点击数据点 → 展示对应酒店详情链接 ----
            selected_points = (
                selected_event.selection.points
                if selected_event and selected_event.selection
                else []
            )
            if selected_points:
                pt = selected_points[0]
                customdata = (
                    pt.get("customdata")
                    if isinstance(pt, dict)
                    else getattr(pt, "customdata", None) or []
                )
                link = customdata[0] if len(customdata) > 0 else None
                name = customdata[1] if len(customdata) > 1 else "所选酒店"
                if link:
                    st.markdown("---")
                    st.success(f"📍 已选酒店：**{name}**")
                    st.link_button("🔗 打开酒店详情页", link, type="primary")
            else:
                st.caption("💡 点击散点图上的数据点，可在下方查看对应酒店的详情链接。")

    # ---- 酒店数据明细（折叠面板） ----
    with st.expander("📋 酒店数据明细", expanded=False):
        display_cols = {
            "hotel_name": "酒店名称",
            "city": "城市",
            "price_cny": "价格 (CNY)",
            "review_score": "综合评分",
            "ai_score": "AI 评分",
            "location_score": "位置评分",
            "distance_to_centre": "距市中心",
            "room_type": "房型",
            "location_desc": "位置描述",
            "detail_link": "详情链接",
            "checkin": "入住",
            "checkout": "退房",
            "scraped_at": "抓取时间",
        }

        available_cols = [c for c in display_cols if c in filtered.columns]
        table_df = filtered[available_cols].rename(
            columns={c: display_cols[c] for c in available_cols}
        )

        st.dataframe(
            table_df,
            use_container_width=True,
            hide_index=True,
            height=400,
            column_config={
                "详情链接": st.column_config.LinkColumn(width="small"),
                "酒店名称": st.column_config.TextColumn(width="medium"),
                "价格 (CNY)": st.column_config.TextColumn(width="small"),
                "综合评分": st.column_config.NumberColumn(width="small", format="%.1f"),
                "AI 评分": st.column_config.NumberColumn(width="small", format="%.1f"),
                "位置评分": st.column_config.NumberColumn(width="small", format="%.1f"),
                "距市中心": st.column_config.TextColumn(width="small"),
                "房型": st.column_config.TextColumn(width="medium"),
                "位置描述": st.column_config.TextColumn(width="medium"),
                "入住": st.column_config.DateColumn(width="small"),
                "退房": st.column_config.DateColumn(width="small"),
                "抓取时间": st.column_config.DatetimeColumn(width="small"),
                "城市": st.column_config.TextColumn(width="small"),
            },
        )

        csv_data = table_df.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            label="📥 导出当前筛选结果为 CSV",
            data=csv_data,
            file_name=f"hotel_export_{datetime.now():%Y%m%d_%H%M}.csv",
            mime="text/csv",
        )

# ==================== Tab 2: 机票监控 ====================

with tab_flight:

    # ---- 机票数据加载 ----
    with st.spinner("🔄 正在加载机票数据 …"):
        try:
            flight_records = get_latest_flight_prices(DB_PATH)
        except Exception:
            flight_records = []

    if not flight_records:
        st.info("暂无机票数据，请运行 flight_scraper.py 抓取机票价格。")
    else:
        flight_df = pd.DataFrame(flight_records)
        st.toast(f"✅ 机票数据加载完成，共 {len(flight_df)} 条记录", icon="✅")

        # ---- 机票汇总指标 ----
        with st.container(border=True):
            st.caption("✈️ 机票核心指标")
            fc1, fc2, fc3, fc4, fc5 = st.columns(5)

            n_routes = flight_df[["origin", "destination"]].drop_duplicates().shape[0]
            min_price = (
                flight_df["price_num"].min()
                if "price_num" in flight_df.columns
                else None
            )
            max_price = (
                flight_df["price_num"].max()
                if "price_num" in flight_df.columns
                else None
            )
            total_flights = get_flight_record_count(DB_PATH)

            fc1.metric("✈️ 航线数量", str(n_routes))
            fc2.metric("💰 最低票价", _fmt_price(min_price))
            fc3.metric("💸 最高票价", _fmt_price(max_price))
            fc4.metric("📊 总抓取记录", f"{total_flights:,}")
            fc5.metric("🌐 数据来源", "Google Flights")

        # ---- 各航线最低票价柱状图 ----
        with st.container(border=True):
            st.caption("📊 各航线最低票价对比")

            if (
                "price_num" in flight_df.columns
                and "origin" in flight_df.columns
                and "destination" in flight_df.columns
            ):
                # 聚合：每个航线取最低票价（一条航线一根柱子，避免多行分组色块混乱）
                route_min = (
                    flight_df.groupby(["origin", "destination"], as_index=False)
                    .agg(price_num=("price_num", "min"))
                )
                route_min["route_label"] = (
                    route_min["origin"] + " → " + route_min["destination"]
                )
                # 排序：按最低票价从低到高，图表更易读
                route_min = route_min.sort_values("price_num", ascending=True)

                # 根据数据最大值/最小值自动计算 Y 轴范围（不固定上限，防止高票价被截断）
                y_axis_min = min(0, float(route_min["price_num"].min()))
                y_axis_max = float(route_min["price_num"].max()) * 1.15

                fig_flight = px.bar(
                    route_min,
                    x="route_label",
                    y="price_num",
                    color="route_label",
                    color_discrete_sequence=px.colors.qualitative.Pastel,
                    labels={
                        "route_label": "航线",
                        "price_num": "最低票价 (CNY)",
                    },
                    height=400,
                    text_auto=".0f",
                )
                fig_flight.update_layout(
                    yaxis=dict(
                        range=[y_axis_min, y_axis_max],
                        tickprefix="¥", tickformat=",d", title="最低票价 (CNY)",
                    ),
                    xaxis=dict(tickangle=-25, title=None),
                    showlegend=False,
                    margin=dict(l=40, r=20, t=40, b=60),
                )
                fig_flight.update_traces(
                    width=0.3,
                    hovertemplate=(
                        "航线: %{x}<br>"
                        "最低票价: ¥%{y:,.0f}"
                        "<extra></extra>"
                    ),
                    texttemplate="¥%{text:,.0f}",
                    textposition="outside",
                )
                st.plotly_chart(fig_flight, use_container_width=True)
            else:
                st.info("机票数据缺少价格或航线字段，无法绘图。")

        # ---- 机票明细表格（折叠面板） ----
        with st.expander("📋 机票数据明细", expanded=False):
            flight_display_cols = {
                "origin": "出发地",
                "destination": "目的地",
                "flight_date": "出发日期",
                "price_cny": "价格 (CNY)",
                "airline_info": "航班信息",
                "booking_link": "预订链接",
                "cabin_class": "舱位",
                "adults": "成人",
                "scraped_at": "抓取时间",
            }
            flight_available = [c for c in flight_display_cols if c in flight_df.columns]
            flight_table = flight_df[flight_available].rename(
                columns={c: flight_display_cols[c] for c in flight_available}
            )

            st.dataframe(
                flight_table,
                use_container_width=True,
                hide_index=True,
                height=400,
                column_config={
                    "出发地": st.column_config.TextColumn(width="small"),
                    "目的地": st.column_config.TextColumn(width="small"),
                    "出发日期": st.column_config.DateColumn(width="small"),
                    "价格 (CNY)": st.column_config.NumberColumn(width="small", format="¥%.0f"),
                    "航班信息": st.column_config.TextColumn(width="medium"),
                    "预订链接": st.column_config.LinkColumn(
                        width="medium", display_text="预订"
                    ),
                    "舱位": st.column_config.TextColumn(width="small"),
                    "成人": st.column_config.NumberColumn(width="small", format="%d"),
                    "抓取时间": st.column_config.DatetimeColumn(width="small"),
                },
            )
