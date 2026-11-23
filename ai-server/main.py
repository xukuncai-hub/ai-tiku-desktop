import os
import json
import re
import time
import uuid
import hashlib
import sqlite3
import csv
import difflib
import io
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from datetime import datetime
from urllib.parse import urlparse
from flask import Flask, Response, jsonify, request
from openai import APIStatusError, BadRequestError, OpenAI
from dotenv import load_dotenv
from colorama import init, Fore, Style

from utils import (
    TYPE_MAPPING,
    answer_key,
    answer_to_texts,
    connect_db,
    norm_text as _norm_text,
    option_texts,
    parse_model_json,
    texts_to_keys,
)

# 初始化 colorama
init(autoreset=True)

# 加载环境变量
load_dotenv()

app = Flask(__name__)

# ── 本地答案缓存：同一道题只花一次钱 ──────────────────────────────
CACHE_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".runtime", "answers.sqlite3")
_CACHE_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS answers ("
    "key TEXT PRIMARY KEY, answer TEXT NOT NULL, analysis TEXT NOT NULL, created REAL NOT NULL,"
    "title TEXT, options TEXT, q_type TEXT)"
)

# 本地题库（人工整理/导入），由 导入题库.py / 应用修正.py 写入，已接入查询链路
_BANK_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS local_bank ("
    "key TEXT PRIMARY KEY, title TEXT NOT NULL, options TEXT, q_type TEXT,"
    "answer TEXT, answer_text TEXT, analysis TEXT, updated REAL NOT NULL)"
)

# 每次作答的历史，便于事后核对哪道错了
_HISTORY_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS history ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, title TEXT, options TEXT,"
    "q_type TEXT, answer TEXT, analysis TEXT, path TEXT)"
)

# 题目清洗结果缓存：同一段原文只清洗一次
_NORM_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS norm_cache ("
    "key TEXT PRIMARY KEY, title TEXT, options TEXT, created REAL NOT NULL)"
)

# 每次模型调用的真实 Token 消耗与成本记录
_USAGE_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS usage_logs ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, model TEXT,"
    "prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL,"
    "cost_rmb REAL NOT NULL, q_title TEXT)"
)


def record_usage(model, usage, q_title=""):
    if usage is None:
        return
    prompt_tok = int(getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", None) or 0)
    comp_tok = int(getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", None) or 0)
    if prompt_tok == 0 and comp_tok == 0:
        return
    price_in = float(os.getenv("PRICE_INPUT_PER_M", "2.0") or "2.0")
    price_out = float(os.getenv("PRICE_OUTPUT_PER_M", "8.0") or "8.0")
    cost_rmb = (prompt_tok * price_in + comp_tok * price_out) / 1_000_000.0
    try:
        with connect_db(CACHE_DB) as db:
            db.execute(_USAGE_SCHEMA)
            db.execute(
                "INSERT INTO usage_logs (ts, model, prompt_tokens, completion_tokens, cost_rmb, q_title)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), str(model or ""), prompt_tok, comp_tok, cost_rmb, str(q_title or "")[:80]),
            )
    except sqlite3.Error:
        pass


def history_add(title, options, q_type, answer, analysis, path):
    try:
        with connect_db(CACHE_DB) as db:
            db.execute(_HISTORY_SCHEMA)
            db.execute(
                "INSERT INTO history (ts, title, options, q_type, answer, analysis, path)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), title, options, q_type, answer, analysis, path),
            )
    except sqlite3.Error as exc:
        log_error(f"历史写入失败: {exc}")


def legacy_cache_key(title, options, q_type):
    return hashlib.sha256(f"{q_type}\x00{title}\x00{options}".encode("utf-8")).hexdigest()


def cache_key(title, options, q_type):
    """抗选项乱序的缓存键：题干与选项分别归一化后按排序后的选项集合计算哈希。"""
    norm_opts = sorted(_norm_text(it) for it in option_texts(options or "") if _norm_text(it))
    payload = f"{q_type}\x00{_norm_text(title)}\x00" + "\x01".join(norm_opts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def init_cache():
    os.makedirs(os.path.dirname(CACHE_DB), exist_ok=True)
    with connect_db(CACHE_DB) as db:
        db.execute(_CACHE_SCHEMA)
        db.execute(_BANK_SCHEMA)
        db.execute(_HISTORY_SCHEMA)
        db.execute(_NORM_SCHEMA)
        db.execute(_USAGE_SCHEMA)
        # 旧库没有正文列，补上以便导出题库
        cols = {row[1] for row in db.execute("PRAGMA table_info(answers)")}
        for col in ("title", "options", "q_type"):
            if col not in cols:
                db.execute(f"ALTER TABLE answers ADD COLUMN {col} TEXT")


def cache_lookup(key, legacy_key=None):
    try:
        with connect_db(CACHE_DB) as db:
            row = db.execute(
                "SELECT answer, analysis, options FROM answers WHERE key = ?", (key,)
            ).fetchone()
            if not row and legacy_key and legacy_key != key:
                row = db.execute(
                    "SELECT answer, analysis, options FROM answers WHERE key = ?", (legacy_key,)
                ).fetchone()
            return row
    except sqlite3.Error:
        return None


def cache_store(key, title, options, q_type, answer, analysis):
    try:
        os.makedirs(os.path.dirname(CACHE_DB), exist_ok=True)
        with connect_db(CACHE_DB) as db:
            # 运行中删库后表也没了，所以写入时补建，别只依赖启动时那一次
            db.execute(_CACHE_SCHEMA)
            db.execute(
                "INSERT OR REPLACE INTO answers (key, answer, analysis, created, title, options, q_type)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, answer, analysis, time.time(), title, options, q_type),
            )
    except sqlite3.Error as exc:
        log_error(f"缓存写入失败: {exc}")


# ── 上游题库（tikuAdapter）：2 秒快查 ───────────────────────────
BANK_URL = os.getenv("BANK_URL", "http://127.0.0.1:8060/adapter-service/search")
BANK_USE = os.getenv("BANK_USE", "buguake")
BANK_TIMEOUT = float(os.getenv("BANK_TIMEOUT", "2"))
BANK_FIRST = os.getenv("BANK_FIRST", "1").strip() == "1"
# 付费源 token：填了才会带进请求
_BANK_TOKENS = {
    "enncyToken": os.getenv("BANK_ENNCY_TOKEN", "").strip(),
    "icodefToken": os.getenv("BANK_ICODEF_TOKEN", "").strip(),
    "wannengToken": os.getenv("BANK_WANNENG_TOKEN", "").strip(),
    "tikuhaiToken": os.getenv("BANK_TIKUHAI_TOKEN", "").strip(),
    "aidianYToken": os.getenv("BANK_AIDIAN_TOKEN", "").strip(),
    "lemonToken": os.getenv("BANK_LEMON_TOKEN", "").strip(),
}
_BANK_TYPES = {"single": 0, "multiple": 1, "completion": 2, "judgement": 3}


def bank_lookup(title, options, q_type):
    """查 tikuAdapter。它可能重排选项，所以答案按文本匹配回原选项顺序。"""
    items = option_texts(options)
    if os.getenv("BANK_FIRST", "1" if BANK_FIRST else "0").strip() != "1" or not items:
        return None
    body = json.dumps({
        "question": title,
        "options": items,
        "type": _BANK_TYPES.get(q_type, 0),
    }).encode("utf-8")
    params = []
    if BANK_USE:
        params.append("use=" + urllib.parse.quote(BANK_USE))
    for name, token in _BANK_TOKENS.items():
        if token:
            params.append(f"{name}={urllib.parse.quote(token)}")
    url = BANK_URL + ("?" + "&".join(params) if params else "")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=BANK_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 题库不可用时静默转 AI 兜底
        log_error(f"题库请求失败，转 AI: {exc}")
        return None
    answer = (data or {}).get("answer") or {}
    texts = answer.get("bestAnswer") or []
    if not texts:
        return None
    keys = []
    for text in texts:
        want = _norm_text(text)
        for i, item in enumerate(items):
            if _norm_text(item) == want:
                keys.append(chr(65 + i))
                break
    if len(keys) != len(texts) or not keys:
        log_info(f"题库答案无法匹配到选项: {texts}")
        return None
    final = "#".join(keys) if q_type == "multiple" else keys[0]
    return {"answer": final, "texts": [str(t) for t in texts]}


def local_bank_lookup(title, options, q_type):
    """本地题库查询。答案按文本匹配回当前选项，选项乱序也正确；填空题直接返回文本。"""
    try:
        with connect_db(CACHE_DB) as db:
            row = db.execute(
                "SELECT answer, answer_text, analysis, options FROM local_bank WHERE key = ?",
                (hashlib.sha256(_norm_text(title).encode("utf-8")).hexdigest(),),
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    raw, answer_text, analysis, stored_options = row
    items = option_texts(options)
    texts = [t.strip() for t in (answer_text or "").split("#") if t.strip()]
    if items:
        if texts:
            mapped = texts_to_keys(texts, options, q_type)
            if mapped:
                return {"answer": mapped, "analysis": analysis or "本地题库"}
        if raw and re.fullmatch(r"[A-Za-z#]+", raw.strip()):
            stored_texts = answer_to_texts(raw, stored_options or "")
            if stored_texts:
                mapped = texts_to_keys(stored_texts, options, q_type)
                if mapped:
                    return {"answer": mapped, "analysis": analysis or "本地题库"}
    elif texts:
        return {"answer": "#".join(texts), "analysis": analysis or "本地题库"}
    return None


init_cache()

# 配置 OpenAI 客户端
api_key = os.getenv("OPENAI_API_KEY")
base_url = os.getenv("OPENAI_BASE_URL")

if not api_key:
    print(Fore.RED + "Warning: OPENAI_API_KEY is not set in environment variables.")

client = OpenAI(
    api_key=api_key,
    base_url=base_url if base_url else None,
    timeout=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "30")),
    max_retries=1,
)

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "analysis": {"type": "string"},
    },
    "required": ["answer", "analysis"],
    "additionalProperties": False,
}


def log_info(msg):
    print(
        f"{Fore.CYAN}[INFO] {datetime.now().strftime('%H:%M:%S')} {Style.RESET_ALL}{msg}")


def log_success(msg):
    print(
        f"{Fore.GREEN}[SUCCESS] {datetime.now().strftime('%H:%M:%S')} {Style.RESET_ALL}{msg}")


def log_error(msg):
    print(
        f"{Fore.RED}[ERROR] {datetime.now().strftime('%H:%M:%S')} {Style.RESET_ALL}{msg}")


def log_request(title, options, q_type):
    print(
        f"\n{Fore.YELLOW}新的请求 [{datetime.now().strftime('%H:%M:%S')}] {Style.RESET_ALL}")
    print(f"{Fore.BLUE}题目:{Style.RESET_ALL} {title}")
    print(f"{Fore.BLUE}类型:{Style.RESET_ALL} {q_type}")
    if options:
        print(f"{Fore.BLUE}选项:{Style.RESET_ALL} \n{options.strip()}")


def log_response(request_id, answer, analysis):
    log_info(f"请求 {request_id} 的模型结果:")
    print(f"{Fore.MAGENTA}答案:{Style.RESET_ALL} {answer}")
    print(f"{Fore.MAGENTA}解析:{Style.RESET_ALL} {analysis}")


JUDGEMENT_VALUES = {
    **{word: "对" for word in ("是", "对", "正确", "确定", "√", "对的", "是的", "正确的", "true", "t", "yes", "1")},
    **{word: "错" for word in ("非", "否", "错", "错误", "×", "x", "错的", "不对", "不正确的", "不正确", "不是", "不是的", "false", "f", "no", "0")},
}

_NO_JSON_SCHEMA_HOSTS = set()


def _current_base_url():
    return os.getenv("OPENAI_BASE_URL", base_url or "").strip()


def _is_deepseek_host(url):
    return bool(url) and urlparse(url).hostname == "api.deepseek.com"


def normalize_judgement_answer(answer, options):
    """将判断题答案转换为 OCS 可识别的“对”或“错”。"""
    labels = {}
    for line in options.splitlines():
        match = re.fullmatch(r"\s*([A-Za-z])\s*[.．、:：)）]\s*(.*?)\s*", line)
        if match:
            label = match.group(1).upper()
            value = JUDGEMENT_VALUES.get(match.group(2).strip().lower())
            if label in labels:
                raise ValueError(f"判断题选项 {label} 重复")
            labels[label] = value

    answer = answer.strip()
    label_match = re.fullmatch(r"([A-Za-z])(?:\s*[.．、:：)）]\s*(.*))?", answer)
    if label_match:
        label = label_match.group(1).upper()
        if label in labels:
            value = labels[label]
            text = label_match.group(2)
            if value is None or (text and JUDGEMENT_VALUES.get(text.strip().lower()) != value):
                raise ValueError(f"判断题答案 {answer} 与选项不匹配")
            return value

    value = JUDGEMENT_VALUES.get(answer.lower())
    if value is not None:
        return value
    raise ValueError(f"无法将判断题答案 {answer} 对应到对或错")


_FONT_FIXES = [
    ("选顼", "选项"), ("税串", "税率"), ("销誊", "销售"), ("誊价", "售价"),
    ("棠", "某"), ("蚪", "料"), ("迸口", "进口"), ("垂托", "委托"),
    ("商档", "高档"), ("牛产", "生产"), ("编刺", "编制"), ("手哀", "手表"),
    ("竽哀", "香水"), ("盹", "吨"), ("袼", "格"), ("篚设", "建设"), ("顼", "项"),
    ("衣业", "农业"), ("衣产品", "农产品"), ("杭械", "机械"), ("谋炭", "煤炭"),
    ("笸别", "区别"), ("哀现", "表现"), ("权贳", "权责"), ("发牛", "发生"),
    ("实现刺", "实现制"), ("生刺", "生制"), ("茌选", "任选"), ("娶橐", "要素"),
    ("代哀", "代表"), ("对鱼", "对象"), ("税且", "税目"), ("捐赚", "捐赠"),
    ("愦务", "债务"), ("耋组", "重组"), ("计鼻", "计算"), ("惆整", "调整"),
    ("螨增", "调增"), ("螨减", "调减"), ("发居", "发展"), ("薷娶", "需要"),
    ("中同", "中国"), ("经菅", "经营"), ("其仙", "其他"), ("辜顼", "事项"),
    ("攴出", "支出"), ("韶限额", "超限额"), ("韶豪华", "超豪华"), ("邡分", "部分"),
    ("敦育", "教育"), ("有限贳茌", "有限责任公司"), ("珠宙店", "珠宝店"),
    ("零誊", "零售"), ("分泉", "分录"), ("广舍宣侍", "广告宣传"), ("皇接", "直接"),
]


def fix_font_text(text):
    """修正学习通/超星自定义字体反爬造成的形近错字。"""
    if not text:
        return text
    fixed = text
    for wrong, right in _FONT_FIXES:
        fixed = fixed.replace(wrong, right)
    # “6 且”“5 且” 应为 “6 月”“5 月”
    fixed = re.sub(r"(\d)\s*且(?=[\s、，。：:]|$)", r"\1 月", fixed)
    fixed = re.sub(r"(\d)\s*拙(?=[\s、，。：:]|$)", r"\1 批", fixed)
    return fixed


def normalize_text(title, options, q_type):
    """用模型按上下文还原字体反爬错字；改动过大就保留原文。结果按原文缓存。"""
    if os.getenv("TEXT_NORMALIZE", "1").strip() != "1":
        return title, options
    key = hashlib.sha256((title + "\x00" + (options or "")).encode("utf-8")).hexdigest()
    try:
        with connect_db(CACHE_DB) as db:
            db.execute(_NORM_SCHEMA)
            row = db.execute("SELECT title, options FROM norm_cache WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        row = None
    if row:
        return row[0], row[1]
    prompt = (
        "下面是一道中文题目，可能因网页字体反爬出现形近错字"
        "（例如“选项”显示成“选顼”、“税率”显示成“税串”）。"
        "请只修正明显的形近错字，不要改写术语、不要增删内容；如果没有错字就原样返回。\n"
        f"题目: {title}\n选项: {options}\n"
        '以 JSON 输出 {"title": "修正后的题目", "options": "修正后的选项"}'
    )
    try:
        kwargs = {
            "model": os.getenv("OPENAI_MODEL", "").strip() or "deepseek-flash",
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "max_tokens": 600,
        }
        if _is_deepseek_host(_current_base_url()):
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        r = client.chat.completions.create(**kwargs)
        data = parse_model_json(r.choices[0].message.content or "{}")
        new_title = str(data.get("title") or "")
        new_options = str(data.get("options") or "")
        usage = getattr(r, "usage", None)
        if usage is not None:
            record_usage(kwargs["model"], usage, title)
            log_info(
                f"清洗 tokens: 输入 {getattr(usage, 'prompt_tokens', '?')}"
                f" / 输出 {getattr(usage, 'completion_tokens', '?')}"
            )
    except Exception as exc:  # noqa: BLE001 - 清洗失败就沿用原文
        log_error(f"题目清洗失败，沿用原文: {exc}")
        return title, options
    # 选项通常很短，错两个字就可能低于 0.85；阈值按长度放宽
    if not new_title or difflib.SequenceMatcher(None, title, new_title).ratio() < 0.6:
        new_title = title
    if not new_title or len(new_title) > len(title) * 1.6 + 10:
        new_title = title
    if options:
        if (not new_options
                or difflib.SequenceMatcher(None, options, new_options).ratio() < 0.4
                or len(new_options) > len(options) * 1.6 + 10):
            new_options = options
    else:
        new_options = ""
    try:
        with connect_db(CACHE_DB) as db:
            db.execute(_NORM_SCHEMA)
            db.execute("INSERT OR REPLACE INTO norm_cache VALUES (?, ?, ?, ?)",
                       (key, new_title, new_options, time.time()))
    except sqlite3.Error:
        pass
    return new_title, new_options


def remap_answer(answer, options, q_type, cached_options=None):
    """把答案（字母或文字）映射成当前选项的字母，保证选项乱序后 OCS 仍能匹配。"""
    items = option_texts(options or "")
    parts = [p.strip() for p in str(answer or "").split("#") if p.strip()]
    if not items or not parts:
        return answer
    if all(len(p) == 1 and p.isalpha() for p in parts):
        if cached_options and _norm_text(cached_options) != _norm_text(options or ""):
            texts = answer_to_texts(answer, cached_options)
            if texts:
                mapped = texts_to_keys(texts, options, q_type)
                if mapped:
                    return mapped
        return answer
    mapped = texts_to_keys(parts, options, q_type)
    return mapped or answer


def web_snippets(question, limit=3):
    """用必应搜题目，取前几条结果摘要做参考资料。失败返回空列表。"""
    try:
        url = "https://cn.bing.com/search?q=" + urllib.parse.quote(question[:100])
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120 Safari/537.36",
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", "ignore")
    except Exception as exc:  # noqa: BLE001 - 联网失败就按无资料处理
        log_error(f"联网检索失败: {exc}")
        return []
    snippets = []
    for block in re.findall(r'<li class="b_algo".*?</li>', html, re.S):
        text = re.sub(r"<[^>]+>", "", block)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            snippets.append(text[:300])
        if len(snippets) >= limit:
            break
    return snippets


def _normalize_image_urls(images):
    if not images:
        return []
    if isinstance(images, str):
        items = [images.strip()] if images.strip() else []
    elif isinstance(images, list):
        items = [str(x).strip() for x in images if isinstance(x, str) and x.strip()]
    else:
        return []
    return [u for u in items if u.startswith(("http://", "https://", "data:image/"))][:4]


def get_chatgpt_answer(title, options, original_type, plan=None, samples=None, extra_prompt="", images=None):
    """
    调用大模型获取答案
    """
    question_type = TYPE_MAPPING.get(original_type, original_type)

    special_instruction = ""
    if original_type == "multiple":
        special_instruction = "这是一道【多选题】，请逐项判断每个选项是否正确，再汇总所有正确项，并严格用 '#' 号分隔（例如：A#C#D）。不要漏选！"
    elif original_type == "completion":
        special_instruction = (
            "这是一道【填空题/分录题】，可能有多个空。请按空的顺序逐个给出答案，"
            "空与空之间用 # 分隔，不要使用换行，不要输出序号。"
        )
    elif original_type == "judgement":
        special_instruction = "这是一道【判断题】，answer 只能填写‘对’或‘错’，不要输出选项字母。"

    prompt = f"""
请仔细阅读题目和选项，选出最正确的答案。请逐项核对每个选项后再给出结论。
注意：题干括号中的英文术语可能是错译，中英文矛盾时以中文术语为准。

题目: {title}
选项: {options}
题目类型: {question_type}
{special_instruction}

answer 填写最准确的答案，多选答案使用 # 分隔；analysis 填写简短解析（不超过 50 字）。
{extra_prompt}
"""
    vision_urls = _normalize_image_urls(images) if os.getenv("OPENAI_VISION", "0").strip() == "1" else []
    if vision_urls:
        user_content = [{"type": "text", "text": prompt}]
        for img_url in vision_urls:
            user_content.append({"type": "image_url", "image_url": {"url": img_url}})
    else:
        user_content = prompt

    messages = [
        {"role": "system", "content": "你是一个专业的做题助手。"},
        {"role": "user", "content": user_content},
    ]
    model = os.getenv("OPENAI_MODEL", "").strip() or "deepseek-flash"
    if vision_urls and os.getenv("OPENAI_VISION_MODEL", "").strip():
        model = os.getenv("OPENAI_VISION_MODEL", "").strip()
    elif original_type == "multiple":
        model = os.getenv("OPENAI_MODEL_HARD", "").strip() or model
    log_info(f"使用模型: {model}" + (f"（含 {len(vision_urls)} 张图片）" if vision_urls else ""))
    api_format = os.getenv("OPENAI_API_FORMAT", "chat_completions").strip().lower()

    cur_base = _current_base_url()
    cur_host = urlparse(cur_base).hostname if cur_base else None
    provider_options = (
        {"extra_body": {"provider": {"require_parameters": True}}}
        if cur_host == "openrouter.ai" else {}
    )
    is_deepseek = _is_deepseek_host(cur_base)
    limit = int(os.getenv("OPENAI_MAX_TOKENS", "4096"))

    def _make_opts(effort):
        opts = dict(provider_options)
        if is_deepseek:
            extra = dict(opts.get("extra_body") or {})
            if effort:
                extra["thinking"] = {"type": "enabled"}
                extra["reasoning_effort"] = effort
            else:
                extra["thinking"] = {"type": "disabled"}
            opts["extra_body"] = extra
        return opts

    def _append_json_hint(msgs):
        hint = (
            "\n请严格以 JSON 对象输出，不要使用 markdown 代码块，"
            '格式：{"answer": "答案", "analysis": "简短解析"}'
        )
        last = msgs[-1]
        if isinstance(last["content"], list):
            last["content"] = [*last["content"], {"type": "text", "text": hint}]
        else:
            last["content"] = last["content"] + hint

    def attempt(effort):
        """一次作答。effort: None 关闭思考，否则 low/medium/high。"""
        msgs = [dict(m) for m in messages]
        opts = _make_opts(effort)
        if api_format == "chat_completions":
            skip_schema = is_deepseek or (cur_host in _NO_JSON_SCHEMA_HOSTS)
            if skip_schema:
                _append_json_hint(msgs)
                schema_format = {"type": "json_object"}
                response = client.chat.completions.create(
                    model=model, messages=msgs, response_format=schema_format,
                    max_tokens=limit, **opts,
                )
            else:
                schema_format = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "question_answer",
                        "strict": True,
                        "schema": ANSWER_SCHEMA,
                    },
                }
                try:
                    response = client.chat.completions.create(
                        model=model, messages=msgs, response_format=schema_format,
                        max_tokens=limit, **opts,
                    )
                except BadRequestError as exc:
                    if "response_format" not in str(exc):
                        raise
                    if cur_host:
                        _NO_JSON_SCHEMA_HOSTS.add(cur_host)
                    _append_json_hint(msgs)
                    schema_format = {"type": "json_object"}
                    response = client.chat.completions.create(
                        model=model, messages=msgs, response_format=schema_format,
                        max_tokens=limit, **opts,
                    )
            if not response.choices:
                raise ValueError("模型未返回候选结果")
            choice = response.choices[0]
            if choice.finish_reason == "length":
                bigger = min(limit * 4, 8192)
                retry_opts = _make_opts(None) if effort else opts
                log_info(f"输出达到 max_tokens={limit} 被截断，提高到 {bigger} 并关闭思考重试")
                response = client.chat.completions.create(
                    model=model, messages=msgs, response_format=schema_format,
                    max_tokens=bigger, **retry_opts,
                )
                if not response.choices:
                    raise ValueError("模型未返回候选结果")
                choice = response.choices[0]
            if choice.finish_reason != "stop":
                raise ValueError(f"模型回答未正常完成: {choice.finish_reason}")
            content = choice.message.content
            usage = getattr(response, "usage", None)
            if usage is not None:
                record_usage(model, usage, title)
                log_info(
                    f"tokens: 输入 {getattr(usage, 'prompt_tokens', '?')}"
                    f" / 输出 {getattr(usage, 'completion_tokens', '?')}"
                )
        elif api_format == "responses":
            response = client.responses.create(
                model=model,
                input=msgs,
                text={"format": {
                    "type": "json_schema",
                    "name": "question_answer",
                    "strict": True,
                    "schema": ANSWER_SCHEMA,
                }},
                **opts,
            )
            if response.status != "completed":
                raise ValueError(f"模型回答未正常完成: {response.status}")
            content = response.output_text
            record_usage(model, getattr(response, "usage", None), title)
        else:
            raise ValueError("OPENAI_API_FORMAT 必须是 chat_completions 或 responses")

        if not isinstance(content, str) or not content.strip():
            raise ValueError("模型未返回文本答案，可能拒绝回答")
        result = parse_model_json(content)
        if not isinstance(result, dict):
            raise ValueError("模型返回的结果不是 JSON 对象")
        answer = result.get("answer")
        analysis = result.get("analysis")
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("模型返回的 answer 必须是非空字符串")
        if not isinstance(analysis, str):
            raise ValueError("模型返回的 analysis 必须是字符串")
        answer = answer.strip()
        if original_type == "judgement":
            answer = normalize_judgement_answer(answer, options)
        elif original_type == "completion":
            lines = [ln.strip() for ln in answer.splitlines() if ln.strip()]
            if len(lines) > 1:
                answer = "#".join(lines)
            else:
                answer = answer.replace("\n", " ")
        elif original_type in ("single", "multiple"):
            letters = set(re.findall(r"(?m)^\s*([A-Za-z])[.、．)）]", options))
            parts = [p.strip().upper() for p in answer.split("#") if p.strip()]
            if letters and parts and all(len(p) == 1 and p.isalpha() for p in parts):
                if not set(parts) <= {x.upper() for x in letters}:
                    raise ValueError(f"答案 {answer} 不在选项 {sorted(letters)} 中")
            elif letters and parts:
                mapped = texts_to_keys(parts, options, original_type)
                if mapped:
                    answer = mapped
        return {"answer": answer, "analysis": analysis.strip()}

    mode = os.getenv("OPENAI_THINKING", "auto").strip().lower()
    if original_type == "completion" and plan is None and samples is None:
        plan = [None]
        samples = 1
    if plan is not None:
        plan = list(plan)
    elif mode == "disabled":
        plan = [None]
    elif mode in ("low", "medium", "high"):
        plan = [mode]
    else:
        plan = ["low", "high"] if original_type in ("single", "judgement") else ["medium", "high"]

    last_error = None
    default_samples = os.getenv("OPENAI_SAMPLES", "1")
    if original_type in ("single", "judgement") and os.getenv("OPENAI_SAMPLES_SINGLE", "").strip():
        default_samples = os.getenv("OPENAI_SAMPLES_SINGLE", "").strip()
    samples = max(1, int(samples if samples is not None else default_samples))
    log_info(f"思考计划: {plan}，每题采样 {samples} 次")
    t0 = time.monotonic()
    for i, effort in enumerate(plan):
        reps = samples if i == 0 else 1
        results = []
        if reps > 1:
            with ThreadPoolExecutor(max_workers=reps) as pool:
                futures = [pool.submit(attempt, effort) for _ in range(reps)]
                for future in futures:
                    try:
                        results.append(future.result())
                    except APIStatusError as exc:
                        if getattr(exc, "status_code", None) in (401, 402, 403):
                            raise
                        last_error = exc
                    except Exception as exc:  # noqa: BLE001
                        last_error = exc
        else:
            try:
                results.append(attempt(effort))
            except APIStatusError as exc:
                if getattr(exc, "status_code", None) in (401, 402, 403):
                    raise
                last_error = exc
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        if not results:
            continue
        if len(results) == 1:
            return results[0]
        keys = [answer_key(r["answer"]) for r in results]
        counts = Counter(keys)
        best, n = counts.most_common(1)[0]
        if n * 2 > len(results):
            for r, k in zip(results, keys):
                if k == best:
                    log_info(f"采样一致 {n}/{len(results)}: {r['answer']}")
                    return r
        log_info(f"采样不一致 {dict(counts)}，提升思考程度重试")
    if os.getenv("SEARCH_FALLBACK", "0").strip() == "1":
        if time.monotonic() - t0 > 45:
            log_info("已耗时过长，跳过联网检索直接返回")
        else:
            snippets = web_snippets(title)
            if snippets:
                log_info(f"改用联网检索重答（{len(snippets)} 条摘要）")
                extra_msg = (
                    "以下是网络检索到的参考资料（可能有噪音）：\n"
                    + "\n---\n".join(snippets)
                    + "\n请结合题目给出最可能的答案，仍按 JSON 输出。"
                )
                messages.append({"role": "user", "content": extra_msg})
                try:
                    return attempt("high")
                except APIStatusError as exc:
                    if getattr(exc, "status_code", None) in (401, 402, 403):
                        raise
                    last_error = exc
                except Exception as exc:  # noqa: BLE001 - 检索重答的网络错误不掩盖原结果
                    log_error(f"检索重答失败: {exc}")
    if last_error:
        raise last_error
    raise ValueError("模型未给出可用答案")


@app.route('/', methods=['GET', 'HEAD'])
def index():
    return jsonify({"code": 1, "msg": "OCS ChatGPT Server is running"}), 200


@app.route('/export', methods=['GET'])
def export_cache():
    """把缓存或历史里的问答导出成 CSV 题库（Excel/WPS 可直接打开）。"""
    source = request.args.get("source", "cache").strip().lower()
    with connect_db(CACHE_DB) as db:
        db.execute(_CACHE_SCHEMA)
        db.execute(_HISTORY_SCHEMA)
        if source == "history":
            rows = db.execute(
                "SELECT title, options, q_type, answer, analysis, ts FROM history "
                "WHERE id IN (SELECT MAX(id) FROM history WHERE path != 'error' GROUP BY title, options, q_type) "
                "ORDER BY id DESC"
            ).fetchall()
            filename = "ai_history.csv"
        else:
            rows = db.execute(
                "SELECT title, options, q_type, answer, analysis, created FROM answers ORDER BY created DESC"
            ).fetchall()
            filename = "ai_tiku.csv"
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["题目", "选项", "题型", "答案", "答案文本", "解析", "时间"])
    for title, options, q_type, answer, analysis, created in rows:
        texts = answer_to_texts(answer, options)
        answer_text = "#".join(texts) if texts else str(answer or "")
        writer.writerow([
            title or "",
            options or "",
            TYPE_MAPPING.get(q_type or "", q_type or ""),
            answer or "",
            answer_text,
            analysis or "",
            datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M:%S") if created else "",
        ])
    return Response(
        "\ufeff" + buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route('/recent', methods=['GET'])
def recent():
    """最近 30 次作答记录，用于核对哪道题错了。"""
    with connect_db(CACHE_DB) as db:
        db.execute(_HISTORY_SCHEMA)
        rows = db.execute(
            "SELECT ts, title, q_type, answer, analysis, path FROM history ORDER BY id DESC LIMIT 30"
        ).fetchall()
    return jsonify([
        {
            "time": datetime.fromtimestamp(r[0]).strftime("%H:%M:%S"),
            "title": r[1], "type": r[2], "answer": r[3],
            "analysis": r[4], "path": r[5],
        }
        for r in rows
    ])


_LAST_ALERT_TS = 0.0


def _notify_critical(msg: str):
    global _LAST_ALERT_TS
    now = time.monotonic()
    if now - _LAST_ALERT_TS < 60:
        return
    _LAST_ALERT_TS = now
    try:
        import subprocess
        subprocess.Popen(
            ["notify-send", "-u", "critical", "AI 题库告警", msg],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001
        pass


def _lookup_cache(title, options, q_type, request_id, started):
    key = cache_key(title, options, q_type)
    lkey = legacy_cache_key(title, options, q_type)
    hit = cache_lookup(key, lkey)
    if hit:
        log_info(f"请求 {request_id} 命中缓存，耗时 {time.monotonic() - started:.1f} 秒")
        cached = remap_answer(hit[0], options, q_type, cached_options=hit[2])
        history_add(title, options, q_type, cached, hit[1], "cache")
        return {"code": 1, "question": title, "answer": cached, "analysis": hit[1]}
    return None


@app.route('/search', methods=['GET', 'HEAD'])
def search_probe():
    # OCS 的连接测试用 GET 探接口，只接受 POST 会让它显示 405 连接失败
    return jsonify({"code": 1, "msg": "OCS AI Answerer ready"}), 200


@app.route('/search', methods=['POST'])
def search_answer():
    started = time.monotonic()
    request_id = uuid.uuid4().hex[:8]
    try:
        data = request.get_json(force=True, silent=True)
        if not isinstance(data, dict):
            return jsonify({"code": 0, "msg": "请求数据必须是 JSON 对象"}), 400

        title = data.get('title', '')
        options = data.get('options', '')
        q_type = data.get('type', 'Unknown')
        suggestion_title = data.get('suggestion_title') or ''
        suggestion_options = data.get('suggestion_options') or ''
        images = data.get('images')

        if not isinstance(title, str) or not title.strip():
            return jsonify({"code": 0, "msg": "题目为空或格式错误"}), 400
        if not isinstance(options, str) or not isinstance(q_type, str):
            return jsonify({"code": 0, "msg": "选项和题目类型必须是字符串"}), 400
        if not isinstance(suggestion_title, str) or not isinstance(suggestion_options, str):
            return jsonify({"code": 0, "msg": "图片题的 suggestion 字段必须是字符串"}), 400
        if images is not None and not isinstance(images, (str, list)):
            return jsonify({"code": 0, "msg": "images 必须是字符串或数组"}), 400
        if options:
            options = "\n".join([line.strip() for line in options.split('\n') if line.strip()])

        # 图片题：OCS 的图片优化结果优先于原始文本
        if suggestion_title.strip():
            title = suggestion_title.strip()
        if suggestion_options.strip():
            options = "\n".join([line.strip() for line in suggestion_options.split('\n') if line.strip()])
        image_count = len(images) if isinstance(images, list) else (1 if images else 0)
        vision_images = None
        if image_count:
            has_ocr = bool(suggestion_title.strip())
            if not has_ocr and os.getenv("OPENAI_VISION", "0").strip() == "1":
                vision_images = images
                log_info(f"图片题：收到 {image_count} 张图片，将随题目发送给多模态模型")
            else:
                log_info(f"图片题：收到 {image_count} 张图片" + ("，已使用 OCR 文本" if has_ocr else "，无 OCR 文本，按原题作答"))

        fixed_title = fix_font_text(title)
        fixed_options = fix_font_text(options)
        if fixed_title != title or fixed_options != options:
            log_info("已修正字体反爬错字")
            title, options = fixed_title, fixed_options

        # 获取中文题型名称用于日志显示
        display_type = TYPE_MAPPING.get(q_type, q_type)
        log_request(title, options, display_type)
        log_info(f"请求 {request_id} 开始，API 格式: {os.getenv('OPENAI_API_FORMAT', 'chat_completions')}")

        # 1. 自动缓存快查（抗选项乱序，多号共享秒中）
        fast_hit = _lookup_cache(title, options, q_type, request_id, started)
        if fast_hit:
            return jsonify(fast_hit)

        norm_title, norm_options = normalize_text(title, options, q_type)
        if (norm_title, norm_options) != (title, options):
            log_info("题目已按上下文还原错字")
            title, options = norm_title, norm_options
            fast_hit = _lookup_cache(title, options, q_type, request_id, started)
            if fast_hit:
                return jsonify(fast_hit)

        key = cache_key(title, options, q_type)
        arbiter = os.getenv("ARBITER", "direct").strip().lower()
        bank = bank_lookup(title, options, q_type)
        if bank:
            log_info(f"请求 {request_id} 题库命中 {bank['answer']}：{'；'.join(bank['texts'])[:40]}")
            if arbiter in ("direct", "bank"):
                cache_store(key, title, options, q_type, bank["answer"], "题库命中")
                history_add(title, options, q_type, bank["answer"], "题库命中", "bank")
                return jsonify({
                    "code": 1, "question": title,
                    "answer": bank["answer"], "analysis": "题库命中",
                })
            # agree：仅在显式开启时让 AI 复核 1 次
            try:
                check = get_chatgpt_answer(title, options, q_type, plan=[None], samples=1, images=vision_images)
            except Exception as exc:  # noqa: BLE001 - AI 不可用时直接采用题库答案
                log_error(f"请求 {request_id} AI 复核失败，直接采用题库答案: {exc}")
                cache_store(key, title, options, q_type, bank["answer"], "题库命中（AI不可用）")
                history_add(title, options, q_type, bank["answer"], "题库命中（AI不可用）", "bank")
                return jsonify({
                    "code": 1, "question": title,
                    "answer": bank["answer"], "analysis": "题库命中（AI不可用）",
                })
            if answer_key(check["answer"]) == answer_key(bank["answer"]):
                final, note = bank["answer"], "题库命中（AI 复核一致）"
                log_info(f"请求 {request_id} 题库与 AI 一致: {final}")
            else:
                final, note = check["answer"], "题库与AI不一致，采用AI答案"
                log_info(f"请求 {request_id} 不一致: 题库 {bank['answer']} / AI {check['answer']}，采用 AI")
            cache_store(key, title, options, q_type, final, note)
            history_add(title, options, q_type, final, note, "bank+ai")
            return jsonify({"code": 1, "question": title, "answer": final, "analysis": note})

        result = get_chatgpt_answer(title, options, q_type, images=vision_images)

        answer = result["answer"]
        analysis = result["analysis"]
        cache_store(key, title, options, q_type, answer, analysis)
        history_add(title, options, q_type, answer, analysis, "ai")

        log_response(request_id, answer, analysis)
        response = jsonify({
            "code": 1,
            "question": title,
            "answer": answer,
            "analysis": analysis
        })
        log_info(f"请求 {request_id} 已准备响应，耗时 {time.monotonic() - started:.1f} 秒")
        return response

    except Exception as e:
        log_error(f"请求 {request_id} 失败，耗时 {time.monotonic() - started:.1f} 秒: {type(e).__name__}: {e}")
        history_add(locals().get("title", "?"), locals().get("options", ""),
                    locals().get("q_type", "?"), "-", f"{type(e).__name__}: {e}", "error")
        if isinstance(e, APIStatusError) and getattr(e, "status_code", None) in (401, 402, 403):
            _notify_critical(f"API 返回 {e.status_code}，请检查余额或密钥！")
        # 使用 HTTP 200 让 OCS 的 handler 能读取业务错误，而不是把错误当作答案。
        return jsonify({"code": 0, "msg": f"模型调用或结果解析失败，请检查服务端日志（请求 {request_id}）", "request_id": request_id})


if __name__ == '__main__':
    from waitress import serve

    bind_host = os.getenv("BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
    bind_port = int(os.getenv("BIND_PORT", "5000"))
    threads = max(4, int(os.getenv("THREADS", "16")))
    log_info(f"Waitress 生产服务启动在 http://{bind_host}:{bind_port}（并发线程 {threads}）")
    serve(app, host=bind_host, port=bind_port, threads=threads)
