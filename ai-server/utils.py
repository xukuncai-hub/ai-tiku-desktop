"""公共工具模块：文本归一化、选项/答案映射、JSON 解析与 SQLite 连接。"""
import json
import re
import sqlite3

TYPE_MAPPING = {
    "single": "单选题",
    "multiple": "多选题",
    "judgement": "判断题",
    "completion": "填空题",
    "vote": "投票题",
    "unknown": "未知类型",
    "Unknown": "未知类型",
}

TYPE_REVERSE_MAP = {
    "单选题": "single",
    "多选题": "multiple",
    "判断题": "judgement",
    "填空题": "completion",
    "投票题": "vote",
    "single": "single",
    "multiple": "multiple",
    "judgement": "judgement",
    "completion": "completion",
    "vote": "vote",
}

TITLE_KEYS = ("题目", "题干", "问题", "title", "question")
OPTION_KEYS = ("选项", "options", "option")
ANSWER_KEYS = ("答案", "正确答案", "answer")
ANSWER_TEXT_KEYS = ("答案文本", "答案文字", "answertext")
ANALYSIS_KEYS = ("解析", "analysis", "explain")
TYPE_KEYS = ("题型", "type")


def connect_db(db_path: str) -> sqlite3.Connection:
    """打开 SQLite 连接并启用 WAL 模式与超时等待，降低并发写锁概率。"""
    conn = sqlite3.connect(db_path, timeout=15.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    return conn


def norm_text(s) -> str:
    """去除空白与常见中英文标点并转小写，用于稳健匹配题目/选项文本。"""
    return re.sub(r"[\s，,。.、；;：:'\"“”‘’()（）\[\]【】]", "", str(s)).lower()


def option_texts(options: str) -> list[str]:
    """从多行选项文本中提取各选项的正文内容。"""
    items = []
    for line in str(options or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^[A-Za-z][.、．)）]\s*(.*)$", line)
        items.append(m.group(1).strip() if m else line)
    return items


def answer_key(answer: str) -> str:
    """比较用的答案归一化：多选排序去重，其余去空格大写。"""
    a = str(answer or "").strip().upper()
    if "#" in a:
        return "#".join(sorted({p.strip() for p in a.split("#") if p.strip()}))
    return re.sub(r"\s+", "", a)


def answer_to_texts(answer: str, options: str) -> list[str]:
    """将字母答案（如 A#C）解析为对应的选项文本列表。"""
    items = option_texts(options)
    raw = str(answer or "").strip()
    if not items or not raw:
        return []
    parts = [p.strip() for p in raw.split("#") if p.strip()]
    if parts and all(len(p) == 1 and p.isalpha() for p in parts):
        texts = []
        for p in parts:
            idx = ord(p.upper()) - 65
            if 0 <= idx < len(items):
                texts.append(items[idx])
        return texts
    return []


def texts_to_keys(texts: list[str], options: str, q_type: str = "single") -> str:
    """将选项文本列表按当前选项顺序映射回字母（如 A 或 A#C），匹配失败返回空串。"""
    items = option_texts(options)
    if not items or not texts:
        return ""
    norm_items = [norm_text(it) for it in items]
    keys = []
    for text in texts:
        want = norm_text(text)
        if not want:
            return ""
        pos = next((i for i, it in enumerate(norm_items) if it == want), None)
        if pos is None:
            pos = next(
                (i for i, it in enumerate(norm_items) if want in it or it in want),
                None,
            )
        if pos is None:
            return ""
        keys.append(chr(65 + pos))
    return "#".join(keys) if q_type == "multiple" else keys[0]


def pick_col(row: dict, keys: tuple[str, ...]) -> str:
    """从字典行中按候选列名（忽略大小写与空白）取值。"""
    for k in keys:
        for actual in row:
            if actual and actual.strip().lower() == k.lower():
                return row[actual]
    return ""


def parse_model_json(content: str) -> dict:
    """尽量从模型输出（可能含 markdown 代码块或前后缀文字）中解析 JSON 对象。"""
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            return json.loads(match.group(0))
        raise ValueError(f"模型输出不是合法 JSON: {text[:120]}")
