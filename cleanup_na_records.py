"""
清理数据库中的无效数据

删除 hotels 表中 hotel_name、price_cny、review_score 等核心字段
包含 "N/A" 的记录。在执行删除前会打印摘要并询问确认。
"""

import sqlite3
from pathlib import Path


DB_PATH = str(Path(__file__).parent / "booking_data.db")


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # ---- 查看符合条件的记录 ----
        na_hotels = conn.execute(
            """SELECT id, hotel_name, price_cny, review_score, city, checkin
               FROM hotels
               WHERE hotel_name = 'N/A'
                  OR price_cny = 'N/A'
                  OR review_score = 'N/A'"""
        ).fetchall()

        na_count = len(na_hotels)
        total = conn.execute("SELECT COUNT(*) FROM hotels").fetchone()[0]

        print(f"总记录数: {total}")
        print(f"包含 N/A 的记录数: {na_count}")

        if na_count == 0:
            print("没有需要清理的记录，退出。")
            return

        # 显示前 10 条
        print(f"\n前 10 条无效记录:")
        for row in na_hotels[:10]:
            print(f"  id={row['id']} | {row['city']} | "
                  f"name={row['hotel_name']} | price={row['price_cny']} | "
                  f"score={row['review_score']}")

        if na_count > 10:
            print(f"  ... 还有 {na_count - 10} 条")

        # ---- 确认删除 ----
        confirm = input(f"\n确认删除这 {na_count} 条记录？(y/N): ").strip().lower()
        if confirm != 'y':
            print("已取消。")
            return

        # ---- 执行删除 ----
        cursor = conn.execute(
            """DELETE FROM hotels
               WHERE hotel_name = 'N/A'
                  OR price_cny = 'N/A'
                  OR review_score = 'N/A'"""
        )
        conn.commit()
        deleted = cursor.rowcount
        remaining = conn.execute("SELECT COUNT(*) FROM hotels").fetchone()[0]

        print(f"\n✅ 已删除 {deleted} 条无效记录")
        print(f"   剩余有效记录: {remaining}")
        print(f"   数据库: {DB_PATH}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
