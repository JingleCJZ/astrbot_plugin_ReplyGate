"""标记解析的离线测试（不依赖 astrbot）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.markers import parse_output  # noqa: E402


class TestPassthrough(unittest.TestCase):
    def test_plain_text_passes_through(self):
        parsed = parse_output("那拜拜啦\n$$$不聊了")
        self.assertIsNone(parsed.marker)
        self.assertEqual(parsed.text, "那拜拜啦\n$$$不聊了")
        self.assertEqual(parsed.parse_error, "")

    def test_dollar_split_preserved(self):
        parsed = parse_output("第一段\n$$$第二段")
        self.assertIn("$$$", parsed.text)


class TestSilence(unittest.TestCase):
    def test_silence_discards_body_permanently(self):
        text = '我看到了\n%%SILENCE%% {"reason":"goodbye"}'
        parsed = parse_output(text)
        self.assertEqual(parsed.marker.kind, "silence")
        self.assertEqual(parsed.marker.reason, "goodbye")
        self.assertEqual(parsed.marker.wait, 0)
        self.assertEqual(parsed.text, "我看到了")

    def test_silence_without_body(self):
        parsed = parse_output('%%SILENCE%% {"reason":"no_value"}')
        self.assertEqual(parsed.marker.reason, "no_value")
        self.assertEqual(parsed.text, "")

    def test_silence_rejects_hold_reason(self):
        parsed = parse_output('正文\n%%SILENCE%% {"reason":"waiting_content","wait":10}')
        self.assertEqual(parsed.parse_error, "marker_bad_reason")

    def test_silence_ignores_wait_field(self):
        parsed = parse_output('正文\n%%SILENCE%% {"reason":"user_busy","wait":30}')
        self.assertEqual(parsed.marker.wait, 0)

    def test_marker_inside_body_is_not_a_marker(self):
        text = '%%SILENCE%% {"reason":"goodbye"}\n后面还有正文'
        parsed = parse_output(text)
        self.assertIsNone(parsed.marker)


class TestHold(unittest.TestCase):
    def test_hold_keeps_body(self):
        text = '这是对图片的回答\n%%HOLD%% {"reason":"waiting_explanation","wait":8}'
        parsed = parse_output(text)
        self.assertEqual(parsed.marker.kind, "hold")
        self.assertEqual(parsed.marker.reason, "waiting_explanation")
        self.assertEqual(parsed.marker.wait, 8)
        self.assertEqual(parsed.text, "这是对图片的回答")

    def test_hold_requires_body(self):
        parsed = parse_output('%%HOLD%% {"reason":"waiting_explanation","wait":8}')
        self.assertEqual(parsed.parse_error, "hold_without_body")

    def test_hold_wait_minimum_one(self):
        parsed = parse_output('a\n%%HOLD%% {"reason":"waiting_explanation","wait":0}')
        self.assertEqual(parsed.marker.wait, 1)

    def test_hold_wait_clamped_by_max(self):
        parsed = parse_output('a\n%%HOLD%% {"reason":"waiting_explanation","wait":999}',
                              hold_max_wait_seconds=30)
        self.assertEqual(parsed.marker.wait, 30)

    def test_hold_default_wait(self):
        parsed = parse_output('a\n%%HOLD%% {"reason":"waiting_content"}',
                              hold_default_wait_seconds=7)
        self.assertEqual(parsed.marker.wait, 7)

    def test_waiting_content_truncated(self):
        body = "催" * 500
        parsed = parse_output(f'{body}\n%%HOLD%% {{"reason":"waiting_content","wait":10}}',
                              followup_max_chars=50)
        self.assertEqual(len(parsed.text), 50)

    def test_waiting_explanation_not_truncated(self):
        body = "答" * 500
        parsed = parse_output(f'{body}\n%%HOLD%% {{"reason":"waiting_explanation","wait":8}}',
                              followup_max_chars=50)
        self.assertEqual(len(parsed.text), 500)

    def test_hold_rejects_silence_reason(self):
        parsed = parse_output('正文\n%%HOLD%% {"reason":"goodbye","wait":5}')
        self.assertEqual(parsed.parse_error, "marker_bad_reason")


class TestErrors(unittest.TestCase):
    def test_bad_json_keeps_body(self):
        parsed = parse_output('正文\n%%SILENCE%% {reason: goodbye}')
        self.assertEqual(parsed.parse_error, "marker_bad_json")
        self.assertEqual(parsed.text, "正文")

    def test_unknown_reason(self):
        parsed = parse_output('正文\n%%SILENCE%% {"reason":"whatever"}')
        self.assertEqual(parsed.parse_error, "marker_bad_reason")

    def test_marker_without_payload(self):
        parsed = parse_output('正文\n%%SILENCE%%')
        self.assertEqual(parsed.parse_error, "marker_without_payload")
        self.assertEqual(parsed.text, "正文")

    def test_fenced_json_accepted(self):
        text = '正文\n%%SILENCE%% ```json\n{"reason":"no_value"}\n```'
        parsed = parse_output(text)
        self.assertEqual(parsed.marker.reason, "no_value")

    def test_custom_marker(self):
        text = '正文\n@@SILENCE@@ {"reason":"no_value"}'
        parsed = parse_output(text, silence_marker="@@SILENCE@@")
        self.assertEqual(parsed.marker.reason, "no_value")
        self.assertEqual(parsed.text, "正文")

    def test_empty_input(self):
        parsed = parse_output("")
        self.assertIsNone(parsed.marker)
        self.assertEqual(parsed.text, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)