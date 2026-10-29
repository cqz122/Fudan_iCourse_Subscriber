#!/usr/bin/env python3
"""把某门课的笔记导出成 Markdown 文件（不发邮件、不改数据库）。

用作「我看一眼这门课讲了啥」的场景：不需要走完整 pipeline，也不用发信。

输出：
    <out>/<课程名>_<日期>.md        合并版（和邮件附件完全同一个格式）
    <out>/lectures/NN_<课次>.md     每讲一个文件（只有 summary，不含逐字稿）

Usage:
    python scripts/export_notes.py --db data/icourse.db --title 集合与图论
    python scripts/export_notes.py --db data/icourse.db --course-id 30004
    python scripts/export_notes.py --db data/icourse.db --title 集合与图论 --no-split
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.emailer import Emailer  # noqa: E402


def _fname_safe(text: str, fallback: str = "lecture") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", text or "")
    cleaned = cleaned.strip(" ._")
    return (cleaned or fallback)[:60]


def _resolve_course(conn: sqlite3.Connection, course_id: str,
                    title: str) -> tuple[str, str]:
    """返回 (course_id, course_title)。title 支持子串匹配。"""
    if course_id:
        row = conn.execute(
            "SELECT course_id, title FROM courses WHERE course_id = ?",
            (course_id,),
        ).fetchone()
        if not row:
            _die_missing(conn, f"course_id={course_id} 不存在")
        return row["course_id"], row["title"] or course_id

    rows = conn.execute("SELECT course_id, title FROM courses").fetchall()
    hits = [r for r in rows if title and title in (r["title"] or "")]
    if len(hits) == 1:
        return hits[0]["course_id"], hits[0]["title"]
    if len(hits) > 1:
        names = "、".join(f"{r['title']}({r['course_id']})" for r in hits)
        _die_missing(conn, f"「{title}」匹配到多门课：{names}")
    _die_missing(conn, f"没有课程标题包含「{title}」")


def _die_missing(conn: sqlite3.Connection, message: str) -> None:
    print(f"错误：{message}", file=sys.stderr)
    rows = conn.execute("SELECT course_id, title FROM courses ORDER BY course_id").fetchall()
    if rows:
        print("\n数据库里现有的课程：", file=sys.stderr)
        for row in rows:
            print(f"  {row['course_id']}  {row['title']}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="导出某门课的笔记为 Markdown")
    parser.add_argument("--db", default=os.environ.get("DB_PATH", "data/icourse.db"))
    parser.add_argument("--course-id", default="", help="课程 ID（精确）")
    parser.add_argument("--title", default="", help="课程名（子串匹配）")
    parser.add_argument("--out", default="notes", help="输出目录")
    parser.add_argument("--no-split", action="store_true",
                        help="只输出合并版，不拆成每讲一个文件")
    args = parser.parse_args()

    if not os.path.isfile(args.db):
        print(f"数据库不存在: {args.db}", file=sys.stderr)
        sys.exit(1)
    if not args.course_id and not args.title:
        print("需要 --course-id 或 --title 之一", file=sys.stderr)
        sys.exit(2)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    course_id, course_title = _resolve_course(conn, args.course_id, args.title)

    rows = conn.execute(
        """SELECT sub_id, sub_title, date, summary
             FROM lectures
            WHERE course_id = ? AND summary IS NOT NULL AND TRIM(summary) <> ''
            ORDER BY CAST(sub_id AS INTEGER) ASC""",
        (course_id,),
    ).fetchall()
    conn.close()

    if not rows:
        print(f"课程「{course_title}」还没有任何已生成的摘要。", file=sys.stderr)
        sys.exit(1)

    items = [{
        "sub_id": str(r["sub_id"]),
        "course_title": course_title,
        "sub_title": r["sub_title"] or str(r["sub_id"]),
        "date": r["date"] or "",
        "summary": r["summary"],
    } for r in rows]

    os.makedirs(args.out, exist_ok=True)

    # 合并版：和邮件附件用的是同一个函数
    combined_name = f"{Emailer._safe_filename(course_title)}_{len(items)}讲.md"
    combined_path = os.path.join(args.out, combined_name)
    with open(combined_path, "w", encoding="utf-8") as fh:
        fh.write(Emailer._build_course_markdown(course_title, items))
    print(f"[合并] {combined_path}  ({len(items)} 讲)")

    if not args.no_split:
        lec_dir = os.path.join(args.out, "lectures")
        os.makedirs(lec_dir, exist_ok=True)
        for index, item in enumerate(items, start=1):
            name = f"{index:02d}_{_fname_safe(item['sub_title'])}.md"
            path = os.path.join(lec_dir, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"# {item['sub_title']}（{item['date']}）\n\n")
                fh.write(f"> 课程：{course_title} · sub_id {item['sub_id']}\n\n")
                fh.write(Emailer._demote_headings(item["summary"].strip()))
                fh.write("\n")
            print(f"[拆分] {path}  ({len(item['summary'])} 字)")

    print(f"\n完成：{course_title}（{course_id}），共 {len(items)} 讲")


if __name__ == "__main__":
    main()
