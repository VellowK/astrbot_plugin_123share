import unittest

from intent import extract_code, parse_intent


class IntentTests(unittest.TestCase):
    def test_share_requests(self):
        for text in (
            "帮我分享一下这个文件",
            "帮我分享一下这个文字",
            "请帮我上传一下这份文件",
            "帮我把这个文件分享一下",
            "这段文字帮我分享一下",
            "分享",
            "帮忙分享一下",
        ):
            with self.subTest(text=text):
                result = parse_intent(text)
                self.assertEqual(result.action, "share")
                self.assertEqual(result.payload, "")

    def test_inline_text_preserved(self):
        cases = {
            "帮我分享一下这个文字：你好\n世界": "你好\n世界",
            "分享：今天开会": "今天开会",
            "分享 这里是正文": "这里是正文",
            "/123share 分享 hello\nworld": "hello\nworld",
            "帮我分享一下这段文字\n  缩进正文": "  缩进正文",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_intent(text).payload, expected)

    def test_receive_requests(self):
        for text in (
            "帮我取一下",
            "帮我取一下1234",
            "请帮我提取 1234",
            "接收文件",
            "帮我下载一下 1234",
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_intent(text).action, "receive")

    def test_ordinary_conversation_not_triggered(self):
        for text in (
            "我想分享一下我的经历",
            "今天讨论分享功能",
            "取快递",
            "下载音乐",
            "我有取件码1234",
            "please share this",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_intent(text))

    def test_explicit_and_cancel(self):
        self.assertTrue(parse_intent("/123share share hello").explicit)
        self.assertEqual(parse_intent("/123share get 1234").payload, "1234")
        self.assertEqual(parse_intent("/123share").action, "help")
        self.assertEqual(parse_intent("取消快传").action, "cancel")

    def test_extract_code(self):
        for text in (
            "1234",
            "取件码：1234",
            "帮我取一下 １２３４",
            "https://123share.cn/?code=1234",
        ):
            with self.subTest(text=text):
                self.assertEqual(extract_code(text), "1234")
        for text in (
            "12345",
            "abc1234def",
            "1234 或 5678",
            "https://evil.example/?code=1234",
            "https://123share.cn.evil.example/?code=1234",
            "https://123share.cn/?code=12345",
            "https://123share.cn/?code=1234&code=5678",
        ):
            with self.subTest(text=text):
                self.assertIsNone(extract_code(text))
