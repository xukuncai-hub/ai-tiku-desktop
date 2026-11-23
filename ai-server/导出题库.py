#!/usr/bin/env python3
"""把本地 AI 题库缓存或历史作答导出成 CSV（可双击运行，也可命令行指定输出路径或 --history）。"""
import argparse
import csv
import os
import sqlite3
import sys
import time

from utils import TYPE_MAPPING, answer_to_texts, connect_db

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, ".runtime", "answers.sqlite3")


def export_rows_to_csv(rows, out_path):
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["题目", "选项", "题型", "答案", "答案文本", "解析", "时间"])
        for title, options, q_type, answer, analysis, created in rows:
            texts = answer_to_texts(answer, options)
            answer_text = "#".join(texts) if texts else str(answer or "")
            w.writerow([
                title or "",
                options or "",
                TYPE_MAPPING.get(q_type or "", q_type or ""),
                answer or "",
                answer_text,
                analysis or "",
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created)) if created else "",
            ])


def main():
    parser = argparse.ArgumentParser(description="导出 AI 题库缓存或作答历史为 CSV")
    parser.add_argument("output", nargs="?", default=os.path.join(BASE, "我的题库.csv"), help="输出 CSV 路径")
    parser.add_argument("--history", action="store_true", help="导出作答历史（含已清缓存的题目，按题目去重取最新）")
    parser.add_argument("--errors", action="store_true", help="仅导出历史中的失败记录")
    args = parser.parse_args()

    if not os.path.exists(DB):
        print("还没有数据库记录，先跑几道题再来导出。")
        return 1

    with connect_db(DB) as db:
        if args.errors:
            rows = db.execute(
                "SELECT title, options, q_type, answer, analysis, ts FROM history "
                "WHERE path = 'error' ORDER BY id DESC"
            ).fetchall()
        elif args.history:
            rows = db.execute(
                "SELECT title, options, q_type, answer, analysis, ts FROM history "
                "WHERE id IN (SELECT MAX(id) FROM history WHERE path != 'error' GROUP BY title, options, q_type) "
                "ORDER BY id DESC"
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT title, options, q_type, answer, analysis, created FROM answers ORDER BY created DESC"
            ).fetchall()

    if not rows:
        print("没有可导出的记录。")
        return 1

    export_rows_to_csv(rows, args.output)
    print(f"已导出 {len(rows)} 道题 -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
