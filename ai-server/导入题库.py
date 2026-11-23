#!/usr/bin/env python3
"""导入题库到自动缓存（answers）或本地题库（local_bank）。

用法:
    python3 导入题库.py 我的题库.csv
    python3 导入题库.py --cache 失败记录_已修复.csv
"""
import argparse
import csv
import hashlib
import json
import os
import sys
import time

from utils import (
    ANALYSIS_KEYS,
    ANSWER_KEYS,
    ANSWER_TEXT_KEYS,
    OPTION_KEYS,
    TITLE_KEYS,
    TYPE_KEYS,
    TYPE_REVERSE_MAP,
    answer_to_texts,
    connect_db,
    norm_text,
    option_texts,
    pick_col,
)

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, ".runtime", "answers.sqlite3")
BANK_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS local_bank ("
    "key TEXT PRIMARY KEY, title TEXT NOT NULL, options TEXT, q_type TEXT,"
    "answer TEXT, answer_text TEXT, analysis TEXT, updated REAL NOT NULL)"
)
CACHE_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS answers ("
    "key TEXT PRIMARY KEY, answer TEXT NOT NULL, analysis TEXT NOT NULL, created REAL NOT NULL,"
    "title TEXT, options TEXT, q_type TEXT)"
)


def make_cache_key(title: str, options: str, q_type: str) -> str:
    norm_opts = sorted(norm_text(it) for it in option_texts(options or "") if norm_text(it))
    payload = f"{q_type}\x00{norm_text(title)}\x00" + "\x01".join(norm_opts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_text(answer, options):
    answer = str(answer or "").strip()
    if not answer:
        return ""
    texts = answer_to_texts(answer, options)
    return "#".join(texts) if texts else answer


def load_rows(path):
    if path.lower().endswith(".json"):
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get("questions") or data.get("data") or []
        return data
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main():
    parser = argparse.ArgumentParser(description="导入 CSV/JSON 题库")
    parser.add_argument("path", help="CSV 或 JSON 文件路径")
    parser.add_argument("--cache", action="store_true", help="直接写入 answers 自动缓存表")
    args = parser.parse_args()

    if not os.path.exists(args.path):
        print("找不到文件:", args.path)
        return 1
    rows = load_rows(args.path)
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    done = skipped = 0
    now = time.time()
    with connect_db(DB) as db:
        db.execute(BANK_SCHEMA)
        db.execute(CACHE_SCHEMA)
        for row in rows:
            title = str(pick_col(row, TITLE_KEYS) or "").strip()
            answer = str(pick_col(row, ANSWER_KEYS) or "").strip()
            text = str(pick_col(row, ANSWER_TEXT_KEYS) or "").strip()
            if not title or not (answer or text):
                skipped += 1
                continue
            options = str(pick_col(row, OPTION_KEYS) or "").strip()
            q_type = TYPE_REVERSE_MAP.get(str(pick_col(row, TYPE_KEYS)).strip(), "single")
            analysis = str(pick_col(row, ANALYSIS_KEYS) or "").strip()
            if args.cache:
                final_ans = answer or text
                key = make_cache_key(title, options, q_type)
                db.execute(
                    "INSERT OR REPLACE INTO answers (key, answer, analysis, created, title, options, q_type) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (key, final_ans, analysis or "CSV导入", now, title, options, q_type),
                )
            else:
                final_text = text or resolve_text(answer, options)
                db.execute(
                    "INSERT OR REPLACE INTO local_bank VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        hashlib.sha256(norm_text(title).encode("utf-8")).hexdigest(),
                        title, options, q_type, answer or final_text,
                        final_text, analysis, now,
                    ),
                )
            done += 1
    target_name = "答案缓存" if args.cache else "本地题库"
    print(f"已导入 {done} 条到{target_name}，跳过 {skipped} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
