#!/usr/bin/env python3
"""按「生产格式」发一封预览邮件（只读数据库，不改任何进度）。

用途：第一次完整跑完之后，想确认新的邮件长什么样（正文只列课程名、笔记走
.md 附件），又不想让 check.yml 把整条流程再走一遍。

这个脚本只做三件事：
  1. 从数据库里读已生成的 summary；
  2. 挑几讲（默认每门课最近 1 讲、最多 3 门课）；
  3. 调用 ``Emailer.send()`` —— 和正式运行时**完全同一条代码路径**。

它**不会**写 processed_at / emailed_at，也不会碰 data 分支，所以对正常的
订阅进度零影响。

Usage:
    python scripts/preview_email.py                       # 每门课 1 讲，最多 3 门
    python scripts/preview_email.py --per-course 2 --max-courses 5
    python scripts/preview_email.py --course-id 30004
    python scripts/preview_email.py --dry-run --out preview.eml   # 只写 eml 不发信
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.emailer import Emailer  # noqa: E402


def pick_lectures(db_path: str, per_course: int, max_courses: int,
                  course_id: str = "") -> list[dict]:
    """从数据库挑出用于预览的课次，返回 Emailer.send() 需要的 items 结构。"""
    if not os.path.isfile(db_path):
        print(f"数据库不存在: {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT c.course_id, c.title AS course_title,
               l.sub_id, l.sub_title, l.date, l.summary
          FROM lectures l
          JOIN courses c ON c.course_id = l.course_id
         WHERE l.summary IS NOT NULL AND TRIM(l.summary) <> ''
    """
    params: list[str] = []
    if course_id:
        sql += " AND l.course_id = ?"
        params.append(course_id)
    sql += " ORDER BY CAST(l.sub_id AS INTEGER) DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    grouped: dict[str, dict] = {}
    for row in rows:
        entry = grouped.setdefault(
            row["course_id"],
            {"title": row["course_title"] or row["course_id"], "lectures": []},
        )
        entry["lectures"].append(dict(row))

    # 最近有课的课程排前面
    ordered = sorted(
        grouped.values(),
        key=lambda e: (e["lectures"][0]["date"] or ""),
        reverse=True,
    )
    if max_courses > 0:
        ordered = ordered[:max_courses]

    items: list[dict] = []
    for entry in ordered:
        for lec in entry["lectures"][:max(per_course, 1)]:
            items.append({
                "sub_id": str(lec["sub_id"]),
                "course_title": entry["title"],
                "sub_title": lec["sub_title"] or str(lec["sub_id"]),
                "date": lec["date"] or "",
                "summary": lec["summary"],
            })
    return items


def send_preview(items: list[dict], dry_run: bool, out_path: str) -> bool:
    """调用生产用的 Emailer.send()；dry_run 时截获报文写成 .eml。"""
    captured: dict[str, str] = {}
    if dry_run:
        import smtplib

        class _CaptureSMTP:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def login(self, *args, **kwargs):
                pass

            def sendmail(self, sender, receiver, message):
                captured["msg"] = message

        smtplib.SMTP_SSL = _CaptureSMTP  # type: ignore[assignment]

    ok = Emailer().send(items)

    if dry_run and "msg" in captured:
        with open(out_path, "w", encoding="ascii", errors="replace") as fh:
            fh.write(captured["msg"])
        print(f"[预览] 已写出 {out_path}（未发信）")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(
        description="按生产格式发一封预览邮件（只读数据库）"
    )
    parser.add_argument("--db", default=os.environ.get("DB_PATH", "data/icourse.db"),
                        help="数据库路径（默认 data/icourse.db）")
    parser.add_argument("--per-course", type=int, default=1,
                        help="每门课取最近几讲（默认 1）")
    parser.add_argument("--max-courses", type=int, default=3,
                        help="最多取几门课，0 表示不限（默认 3）")
    parser.add_argument("--course-id", default="", help="只预览指定课程 ID")
    parser.add_argument("--dry-run", action="store_true",
                        help="不连 SMTP，只把报文写成 .eml")
    parser.add_argument("--out", default="preview.eml", help="dry-run 输出文件名")
    args = parser.parse_args()

    items = pick_lectures(args.db, args.per_course, args.max_courses,
                          args.course_id)
    if not items:
        print("数据库里没有可用的摘要（summary 为空），无法预览。", file=sys.stderr)
        sys.exit(1)

    courses = {item["course_title"] for item in items}
    print(f"[预览] 选中 {len(items)} 个课次，来自 {len(courses)} 门课程：")
    for item in items:
        print(f"  - {item['course_title']} / {item['sub_title']}"
              f"（{item['date']}） {len(item['summary'])} 字")

    if not args.dry_run and not (
        os.environ.get("SMTP_EMAIL") and os.environ.get("SMTP_PASSWORD")
    ):
        print("缺少 SMTP_EMAIL / SMTP_PASSWORD，降级为 dry-run，只写 .eml。")
        args.dry_run = True

    ok = send_preview(items, args.dry_run, args.out)
    print(f"[预览] send() = {ok}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
