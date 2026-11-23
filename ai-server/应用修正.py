#!/usr/bin/env python3
"""应用 Gemini 审校后的修正 CSV。

用法:
    python3 应用修正.py 待审_修正.csv

做的事:
  1. 把每行写进本地题库 local_bank（答案按文本存，选项乱序也对）
  2. 同步覆盖缓存表 answers 里同题目的旧答案，下次同题立即返回新答案

CSV 需要包含：题目、答案文本（或答案）、可选的选项/解析/题型。
"""
import csv
import hashlib
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
    pick_col,
    texts_to_keys,
)

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, ".runtime", "answers.sqlite3")
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS local_bank ("
    "key TEXT PRIMARY KEY, title TEXT NOT NULL, options TEXT, q_type TEXT,"
    "answer TEXT, answer_text TEXT, analysis TEXT, updated REAL NOT NULL)"
)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    path = sys.argv[1]
    if not os.path.exists(path):
        print("找不到文件:", path)
        return 1
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    bank = cache = 0
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with connect_db(DB) as db:
        db.execute(SCHEMA)
        for row in rows:
            title = str(pick_col(row, TITLE_KEYS) or "").strip()
            raw = str(pick_col(row, ANSWER_KEYS) or "").strip()
            text = str(pick_col(row, ANSWER_TEXT_KEYS) or "").strip()
            if not title or not (raw or text):
                continue
            options = str(pick_col(row, OPTION_KEYS) or "").strip()
            q_type = TYPE_REVERSE_MAP.get(str(pick_col(row, TYPE_KEYS)).strip(), "single")
            analysis = str(pick_col(row, ANALYSIS_KEYS) or "").strip()
            texts = [t.strip() for t in text.split("#") if t.strip()] if text else []
            if not texts and raw:
                texts = answer_to_texts(raw, options)
            answer_text = "#".join(texts) if texts else (text or raw)
            db.execute(
                "INSERT OR REPLACE INTO local_bank VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    hashlib.sha256(norm_text(title).encode("utf-8")).hexdigest(),
                    title, options, q_type, raw or answer_text, answer_text, analysis, time.time(),
                ),
            )
            bank += 1
            # 同步覆盖缓存：同题目的旧答案作废（支持题干归一化匹配）
            target_norm = norm_text(title)
            for key, cached_title, cached_options, cached_type in db.execute(
                "SELECT key, title, options, q_type FROM answers"
            ).fetchall():
                if norm_text(cached_title) != target_norm:
                    continue
                mapped = texts_to_keys(texts, cached_options, cached_type or q_type) if texts else ""
                if mapped:
                    db.execute(
                        "UPDATE answers SET answer = ?, analysis = ? WHERE key = ?",
                        (mapped, analysis or "已人工修正", key),
                    )
                    cache += 1
    print(f"本地题库写入 {bank} 条，缓存覆盖 {cache} 条")
    print("数据库:", DB)
    return 0


if __name__ == "__main__":
    sys.exit(main())
