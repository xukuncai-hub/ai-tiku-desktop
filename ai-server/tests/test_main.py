import json
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI

os.environ["OPENAI_API_KEY"] = "test-key"
os.environ["OPENAI_BASE_URL"] = "https://openrouter.ai/api/v1"
os.environ["OPENAI_MODEL"] = "test-model"
os.environ["OPENAI_MODEL_HARD"] = ""
os.environ["OPENAI_SAMPLES"] = "1"
os.environ["BANK_FIRST"] = "0"
os.environ["TEXT_NORMALIZE"] = "0"
os.environ["SEARCH_FALLBACK"] = "0"

import main  # noqa: E402


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test.sqlite3")
        self.db_patch = patch.object(main, "CACHE_DB", self.db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.tmpdir.cleanup)
        main.init_cache()
        main._NO_JSON_SCHEMA_HOSTS.clear()

        self.env_patch = patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "test-key",
                "OPENAI_BASE_URL": "https://openrouter.ai/api/v1",
                "OPENAI_MODEL": "test-model",
                "OPENAI_MODEL_HARD": "",
                "OPENAI_SAMPLES": "1",
                "BANK_FIRST": "0",
                "TEXT_NORMALIZE": "0",
                "SEARCH_FALLBACK": "0",
                "OPENAI_VISION": "0",
            },
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        self.requests = []
        self.reply = {"answer": "A#C", "analysis": "解析"}
        self.finish_reason = "stop"
        self.status = "completed"
        self.http_status = 200
        self.reject_json_schema = False
        self.length_once = False
        self.http_client = httpx.Client(transport=httpx.MockTransport(self.respond))
        self.api_client = OpenAI(
            api_key="test",
            base_url="https://openrouter.ai/api/v1",
            http_client=self.http_client,
            max_retries=0,
        )
        self.client_patch = patch.object(main, "client", self.api_client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.addCleanup(self.http_client.close)
        self.flask = main.app.test_client()

    def respond(self, request):
        body = json.loads(request.content)
        self.requests.append((request.url.path, body))
        if self.http_status != 200:
            return httpx.Response(
                self.http_status,
                json={"error": {"message": "upstream error", "type": "invalid_request_error"}},
            )
        if (
            self.reject_json_schema
            and request.url.path.endswith("/chat/completions")
            and body.get("response_format", {}).get("type") == "json_schema"
        ):
            return httpx.Response(
                400,
                json={"error": {"message": "unsupported response_format json_schema", "type": "invalid_request_error"}},
            )
        content = json.dumps(self.reply, ensure_ascii=False)
        if request.url.path.endswith("/chat/completions"):
            fr = self.finish_reason
            if self.length_once and len(self.requests) == 1:
                fr = "length"
            return httpx.Response(
                200,
                json={
                    "id": "chat-1",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "test",
                    "choices": [{
                        "index": 0,
                        "finish_reason": fr,
                        "message": {"role": "assistant", "content": content},
                    }],
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "resp-1",
                "object": "response",
                "created_at": 1,
                "status": self.status,
                "model": "test",
                "output": [{
                    "id": "msg-1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": content, "annotations": []}],
                }],
            },
        )

    def search(self, api_format, options="A. 一\nC. 三", question_type="multiple", title="题目", **extra):
        with patch.dict(os.environ, {"OPENAI_API_FORMAT": api_format}):
            return self.flask.post(
                "/search",
                json={"title": title, "options": options, "type": question_type, **extra},
            )

    def test_judgement_letters_follow_the_actual_option_order(self):
        cases = [
            ("A. 错\nB. 对", "B", "对"),
            ("A. 错\nB. 对", "A", "错"),
            ("A. 对\nB. 错", "B", "错"),
            ("A. 对\nB. 错", "A", "对"),
            ("A. 错\nB. 对", "B. 对", "对"),
            ("A. 对\nB. 错", "正确", "对"),
            ("A. 对\nB. 错", "false", "错"),
        ]
        for idx, (options, answer, expected) in enumerate(cases):
            with self.subTest(options=options, answer=answer):
                self.reply["answer"] = answer
                data = self.search("chat_completions", options, "judgement", title=f"判断题-{idx}").get_json()
                self.assertEqual(data["code"], 1)
                self.assertEqual(data["answer"], expected)

    def test_judgement_unmapped_or_conflicting_letter_is_not_guessed(self):
        for idx, (options, answer) in enumerate([
            ("对\n错", "B"),
            ("A. 对\nB. 错", "B. 对"),
            ("A. 对\nA. 错", "A"),
        ]):
            with self.subTest(options=options, answer=answer):
                self.reply["answer"] = answer
                data = self.search("responses", options, "judgement", title=f"非法判断-{idx}").get_json()
                self.assertEqual(data["code"], 0)
                self.assertNotIn("answer", data)

    def test_chat_schema_and_response(self):
        response = self.search("chat_completions")
        self.assertEqual(response.get_json()["answer"], "A#C")
        path, body = self.requests[0]
        self.assertTrue(path.endswith("/chat/completions"))
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertTrue(body["provider"]["require_parameters"])

    def test_responses_schema_and_response(self):
        response = self.search("responses")
        self.assertEqual(response.get_json()["answer"], "A#C")
        path, body = self.requests[0]
        self.assertTrue(path.endswith("/responses"))
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertTrue(body["provider"]["require_parameters"])

    def test_invalid_answer_is_not_reported_as_success(self):
        for idx, answer in enumerate((None, " ", ["A", "C"])):
            with self.subTest(answer=answer):
                self.reply["answer"] = answer
                response = self.search("chat_completions", title=f"非法答案-{idx}")
                self.assertEqual(response.get_json()["code"], 0)
                self.assertNotIn("answer", response.get_json())

    def test_incomplete_response_is_not_reported_as_success(self):
        self.finish_reason = "length"
        self.assertEqual(self.search("chat_completions", title="截断测试1").get_json()["code"], 0)
        self.status = "incomplete"
        self.assertEqual(self.search("responses", title="截断测试2").get_json()["code"], 0)

    def test_invalid_request_and_format(self):
        self.assertEqual(self.flask.post("/search", json=[]).status_code, 400)
        self.assertEqual(self.search("unsupported").get_json()["code"], 0)
        self.assertEqual(self.requests, [])

    def test_upstream_failure_is_visible_without_fake_answer(self):
        self.http_status = 400
        response = self.search("chat_completions")
        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["code"], 0)
        self.assertIn(data["request_id"], data["msg"])
        self.assertNotIn("answer", data)

    def test_cache_survives_shuffled_options(self):
        self.reply = {"answer": "B", "analysis": "选乙"}
        first = self.search(
            "chat_completions",
            options="A. 甲\nB. 乙\nC. 丙",
            question_type="single",
            title="选项乱序题",
        ).get_json()
        self.assertEqual(first["code"], 1)
        self.assertEqual(first["answer"], "B")
        self.assertEqual(len(self.requests), 1)

        # 选项顺序打乱后应直接命中缓存，不发新请求，且映射为新的正确字母 A
        second = self.search(
            "chat_completions",
            options="A. 乙\nB. 丙\nC. 甲",
            question_type="single",
            title="选项乱序题",
        ).get_json()
        self.assertEqual(second["code"], 1)
        self.assertEqual(second["answer"], "A")
        self.assertEqual(len(self.requests), 1)

    def test_json_schema_fallback_is_remembered_per_host(self):
        self.reject_json_schema = True
        self.reply = {"answer": "A", "analysis": "解析"}
        r1 = self.search("chat_completions", options="A. 对项\nB. 错项", question_type="single", title="降级1").get_json()
        self.assertEqual(r1["code"], 1)
        # 第一次：json_schema 400 -> json_object 200
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0][1]["response_format"]["type"], "json_schema")
        self.assertEqual(self.requests[1][1]["response_format"]["type"], "json_object")

        # 第二次同主机应直接发 json_object，不再试错 400
        r2 = self.search("chat_completions", options="A. 对项\nB. 错项", question_type="single", title="降级2").get_json()
        self.assertEqual(r2["code"], 1)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.requests[2][1]["response_format"]["type"], "json_object")

    def test_length_retry_recovers_on_second_attempt(self):
        self.length_once = True
        self.reply = {"answer": "A", "analysis": "恢复成功"}
        r = self.search("chat_completions", options="A. 一\nB. 二", question_type="single", title="截断恢复").get_json()
        self.assertEqual(r["code"], 1)
        self.assertEqual(r["answer"], "A")
        self.assertEqual(len(self.requests), 2)
        self.assertGreater(self.requests[1][1]["max_tokens"], self.requests[0][1]["max_tokens"])

    def test_vision_payload_sent_when_enabled_without_ocr(self):
        self.reply = {"answer": "A", "analysis": "看图回答"}
        with patch.dict(os.environ, {"OPENAI_VISION": "1"}):
            r = self.search(
                "chat_completions",
                options="A. 10\nB. 20",
                question_type="single",
                title="看图计算",
                images=["https://example.com/q1.png"],
            ).get_json()
        self.assertEqual(r["code"], 1)
        user_msg = self.requests[0][1]["messages"][-1]["content"]
        self.assertIsInstance(user_msg, list)
        self.assertEqual(user_msg[1]["type"], "image_url")
        self.assertEqual(user_msg[1]["image_url"]["url"], "https://example.com/q1.png")

    def test_export_cache_and_history_csv(self):
        self.reply = {"answer": "资产=负债+所有者权益", "analysis": "会计恒等式"}
        self.search("chat_completions", options="", question_type="completion", title="写出恒等式")
        csv_cache = self.flask.get("/export").data.decode("utf-8-sig")
        self.assertIn("写出恒等式", csv_cache)
        self.assertIn("资产=负债+所有者权益", csv_cache)

        csv_hist = self.flask.get("/export?source=history").data.decode("utf-8-sig")
        self.assertIn("写出恒等式", csv_hist)


if __name__ == "__main__":
    unittest.main()
