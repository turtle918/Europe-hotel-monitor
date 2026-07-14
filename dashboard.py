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

# ==================== 页面设置 ====================
st.set_page_config(
    page_title="酒店房源监控看板",
    page_icon="🏨",
    layout="wide",
)

st.title("🏨 酒店房源监控看板")
st.caption("Booking.com 爬虫数据 · 价格 / AI 综合评分 可视化")

# ==================== 从 config.py 读取城市列表 ====================

@st.cache_data
def get_config_cities() -> list[str]:
    """从 config.py 的 CITY_TASKS 中提取按顺序的城市名称列表"""
    config = ScraperConfig()
    cities = []
    for task in config.CITY_TASKS:
        city = task.get("city", "")
        if city and city not in cities:
            cities.append(city)
    return cities


CONFIG_CITIES = get_config_cities()

# ==================== 数据库读取 ====================

DB_PATH = str(Path(__file__).parent / "booking_data.db")


@st.cache_data(ttl=60)
def load_data(db_path: str) -> pd.DataFrame:
    """从 SQLite 读取全部酒店记录，返回 DataFrame"""
    if not Path(db_path).exists():
        st.error(f"❌ 数据库文件不存在: {db_path}")
        return pd.DataFrame()

    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query("SELECT * FROM hotels ORDER BY id DESC", conn)
    finally:
        conn.close()

    if df.empty:
        return df

    # ---- 派生数值列（解析原始字符串字段） ----
    def _parse_price(val):
        """将 price_cny 字符串解析为 float"""
        if pd.isna(val) or val == "N/A":
            return None
        try:
            return float(str(val).replace(",", "").replace("¥", "").replace("CN", "").strip())
        except (ValueError, TypeError):
            return None

    def _parse_score(val):
        """将 review_score / ai_score 字符串解析为 float"""
        if pd.isna(val) or val == "N/A":
            return None
        try:
            return float(str(val).strip())
        except (ValueError, TypeError):
            return None

    df["price_num"] = df["price_cny"].apply(_parse_price)
    df["score_num"] = df["review_score"].apply(_parse_score)

    # ---- AI 综合评分 ----
    if "ai_score" in df.columns:
        df["ai_score_num"] = df["ai_score"].apply(_parse_score)
    else:
        df["ai_score_num"] = None

    # ---- 位置评分 ----
    if "location_score" in df.columns:
        df["location_score_num"] = df["location_score"].apply(_parse_score)
    else:
        df["location_score_num"] = None

    # ---- 将 scraped_at 转为 datetime ----
    df["scraped_at_dt"] = pd.to_datetime(df["scraped_at"], errors="coerce")

    return df


df = load_data(DB_PATH)

# ==================== 侧边栏 —— 筛选面板 ====================

st.sidebar.header("🔍 数据筛选")

# ---- 城市选择（基于 config.py 的 9 个欧洲城市） ----
if not df.empty:
    # 将数据库中实际存在的城市与 config 城市取交集，保持 config 顺序
    db_cities = set(df["city"].dropna().unique())
    available_cities = [c for c in CONFIG_CITIES if c in db_cities]
    # 如果数据库中有 config 之外的城市，也加入
    extra_cities = sorted(db_cities - set(CONFIG_CITIES))
    all_available = available_cities + extra_cities

    selected_cities = st.sidebar.multiselect(
        "城市（按旅行计划顺序）",
        options=all_available,
        default=all_available,
        placeholder="选择城市 …",
    )
else:
    selected_cities = []

# ---- 时间范围 ----
if not df.empty:
    min_dt: datetime = df["scraped_at_dt"].min().to_pydatetime()
    max_dt: datetime = df["scraped_at_dt"].max().to_pydatetime()

    # 如果最小/最大相同，扩展一天避免 slider 错误
    if min_dt == max_dt:
        min_dt = min_dt - timedelta(days=1)
        max_dt = max_dt + timedelta(days=1)

    time_range = st.sidebar.slider(
        "抓取时间范围",
        min_value=min_dt,
        max_value=max_dt,
        value=(min_dt, max_dt),
        format="MM-DD HH:mm",
    )
else:
    time_range = None

# ---- 最高价格 ----
if not df.empty:
    valid_prices = df["price_num"].dropna()
    if not valid_prices.empty:
        price_min = max(int(valid_prices.min()), 0)
        price_max = int(valid_prices.max()) + 100
        max_price = st.sidebar.slider(
            "最高价格 (CNY / 晚)",
            min_value=price_min,
            max_value=price_max,
            value=price_max,
            step=50,
            format="¥%d",
        )
    else:
        max_price = 99999
else:
    max_price = 99999

st.sidebar.divider()
st.sidebar.caption(f"数据库路径: `{DB_PATH}`")
if not df.empty:
    st.sidebar.caption(f"总记录数: {len(df)}")
st.sidebar.caption(f"旅行城市: {len(CONFIG_CITIES)} 个")

# ==================== 数据过滤 ====================

if df.empty:
    st.warning("⚠️ 数据库中暂无数据，请先运行 booking_scraper.py 抓取数据。")
    st.stop()

mask = pd.Series(True, index=df.index)

if selected_cities:
    mask &= df["city"].isin(selected_cities)

if time_range is not None:
    mask &= (df["scraped_at_dt"] >= time_range[0]) & (df["scraped_at_dt"] <= time_range[1])

# 价格过滤（保留无价格记录）
price_mask = df["price_num"].isna() | (df["price_num"] <= max_price)
mask &= price_mask

filtered = df[mask].copy()

# ---- 汇总指标 ----
col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric("🏠 酒店数量", len(filtered))
with col2:
    avg_price = filtered["price_num"].dropna().mean()
    st.metric("💰 均价 (CNY)", f"¥{avg_price:,.0f}" if pd.notna(avg_price) else "N/A")
with col3:
    avg_score = filtered["score_num"].dropna().mean()
    st.metric("⭐ 平均综合评分", f"{avg_score:.1f}" if pd.notna(avg_score) else "N/A")
with col4:
    avg_ai = filtered["ai_score_num"].dropna().mean()
    st.metric("🤖 平均 AI 评分", f"{avg_ai:.1f}" if pd.notna(avg_ai) else "N/A")

# ==================== 散点图：价格 vs AI 评分 ====================

st.subheader("📈 价格 vs AI 综合评分 散点图")

scatter_df = filtered.dropna(subset=["price_num", "ai_score_num"]).copy()

if scatter_df.empty:
    st.info("当前筛选条件下没有同时包含价格和 AI 评分的记录，无法绘制散点图。")
else:
    # hover 信息
    scatter_df["hover_label"] = (
        scatter_df["hotel_name"].str[:40]
        + "<br>城市: " + scatter_df["city"]
        + "<br>价格: ¥" + scatter_df["price_num"].apply(lambda x: f"{x:,.0f}")
        + "<br>AI 评分: " + scatter_df["ai_score_num"].apply(lambda x: f"{x:.0f}" if pd.notna(x) else "N/A")
    )

    # 气泡大小用综合评分 review_score（有则用作参考）
    if "score_num" in scatter_df.columns and scatter_df["score_num"].notna().any():
        scatter_df["score_num"] = scatter_df["score_num"].fillna(0)
        size_col = "score_num"
        size_label = "综合评分"
    else:
        size_col = None
        size_label = None

    fig = px.scatter(
        scatter_df,
        x="price_num",
        y="ai_score_num",
        color="city",
        size=size_col,
        size_max=15,
        hover_name="hotel_name",
        hover_data={
            "hover_label": True,
            "price_num": False,
            "ai_score_num": False,
            "city": False,
        },
        labels={
            "price_num": "价格 (CNY / 晚)",
            "ai_score_num": "AI 综合评分",
            "city": "城市",
        },
        title="酒店价格 vs AI 综合评分（气泡大小 = Booking 综合评分）",
        height=550,
    )

    if size_col:
        fig.update_traces(
            hovertemplate=(
                "%{customdata[0]}<br>"
                "综合评分: %{marker.size:.1f}"
                "<extra></extra>"
            ),
        )
    else:
        fig.update_traces(
            hovertemplate=(
                "%{customdata[0]}"
                "<extra></extra>"
            ),
        )

    fig.update_layout(
        xaxis=dict(tickprefix="¥", tickformat=",d"),
        yaxis=dict(
            range=[0.5, 10.5],
            tickmode="linear",
            dtick=1,
            title="AI 综合评分 (1-10)",
        ),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )

    st.plotly_chart(fig, use_container_width=True)

# ==================== 数据表格 ====================

st.subheader("📋 酒店数据明细")

# 展示用列
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

# 仅保留存在的列
available_cols = [c for c in display_cols if c in filtered.columns]
table_df = filtered[available_cols].rename(columns={c: display_cols[c] for c in available_cols})

st.dataframe(
    table_df,
    use_container_width=True,
    hide_index=True,
    column_config={
        "详情链接": st.column_config.LinkColumn(width="small"),
        "酒店名称": st.column_config.TextColumn(width="medium"),
    },
)

# 导出按钮
csv_data = table_df.to_csv(index=False).encode("utf-8-sig")
st.download_button(
    label="📥 导出当前筛选结果为 CSV",
    data=csv_data,
    file_name=f"hotel_export_{datetime.now():%Y%m%d_%H%M}.csv",
    mime="text/csv",
)
