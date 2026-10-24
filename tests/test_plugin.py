"""Core interaction tests with minimal AstrBot API doubles (no running bot needed)."""

import asyncio
import importlib.util
import logging
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]


class Plain:
    def __init__(self, text):
        self.text = text


class File:
    def __init__(self, name="test.txt", file="", url=""):
        self.name, self.file, self.url = name, file, url

    async def get_file(self, allow_return_url=False):
        return self.file


class Reply:
    def __init__(self, chain=None, message_str=""):
        self.chain, self.message_str = chain, message_str


class Star:
    def __init__(self, context):
        self.context = context


class Filters:
    EventMessageType = types.SimpleNamespace(ALL="all")

    @staticmethod
    def event_message_type(*args, **kwargs):
        return lambda function: function


def load_plugin():
    modules = {
        "astrbot": {},
        "astrbot.api": {"AstrBotConfig": dict, "logger": logging.getLogger("test_123share")},
        "astrbot.api.event": {"AstrMessageEvent": object, "filter": Filters},
        "astrbot.api.message_components": {"File": File, "Plain": Plain, "Reply": Reply},
        "astrbot.api.star": {"Context": object, "Star": Star},
        "astrbot.core": {},
        "astrbot.core.utils": {},
        "astrbot.core.utils.astrbot_path": {"get_astrbot_data_path": lambda: "unused"},
    }
    for name, attributes in modules.items():
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        sys.modules[name] = module
    package_name = "_123share_under_test"
    spec = importlib.util.spec_from_file_location(
        package_name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    return __import__(package_name + ".main", fromlist=["Share123Plugin"])


plugin_module = load_plugin()


class Event:
    def __init__(
        self,
        text="",
        files=None,
        reply=None,
        sender="alice",
        origin="private:a",
        group="",
        wake=False,
    ):
        self.components = [Plain(text), *(files or []), *([reply] if reply else [])]
        self.sender, self.unified_msg_origin, self.group = sender, origin, group
        self.is_at_or_wake_command = wake
        self.sent = []
        self.stopped = False

    def get_messages(self):
        return self.components

    def get_sender_id(self):
        return self.sender

    def get_group_id(self):
        return self.group

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain

    def stop_event(self):
        self.stopped = True

    async def send(self, result):
        self.sent.append(result)


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        plugin_module.get_astrbot_data_path = lambda: self.tmp.name
        self.plugin = plugin_module.Share123Plugin(None, {"cooldown_seconds": 0})
        self.plugin.client = types.SimpleNamespace(
            share_text=AsyncMock(return_value="1234"),
            share_file=AsyncMock(return_value="5678"),
            receive=AsyncMock(return_value={"type": "text", "content": "取回的内容"}),
            download=AsyncMock(),
            close=AsyncMock(),
        )

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.tmp.cleanup()

    async def dispatch(self, text="", **kwargs):
        event = Event(text, **kwargs)
        await self.plugin.on_message(event)
        return event

    async def test_inline_text(self):
        event = await self.dispatch("帮我分享一下这个文字：你好")
        self.plugin.client.share_text.assert_awaited_once_with("你好", False)
        self.assertTrue(event.stopped)
        self.assertIn("取件码：1234", event.sent[0])

    async def test_share_followup(self):
        first = await self.dispatch("帮我分享一下")
        self.assertIn("120 秒", first.sent[0])
        self.plugin.client.share_text.assert_not_awaited()
        await self.dispatch("这是需要分享的文字")
        self.plugin.client.share_text.assert_awaited_once_with("这是需要分享的文字", False)

    async def test_followup_isolated_by_sender_and_session(self):
        await self.dispatch("帮我分享一下")
        await self.dispatch("别人的内容", sender="bob")
        await self.dispatch("另一会话的内容", origin="private:other")
        self.plugin.client.share_text.assert_not_awaited()
        await self.dispatch("自己的内容")
        self.plugin.client.share_text.assert_awaited_once_with("自己的内容", False)

    async def test_receive_followup(self):
        await self.dispatch("帮我取一下")
        await self.dispatch("１２３４")
        self.plugin.client.receive.assert_awaited_once_with("1234")

    async def test_invalid_code_does_not_consume_wait(self):
        await self.dispatch("帮我取一下")
        invalid = await self.dispatch("abc12345")
        self.assertIn("四位数字", invalid.sent[0])
        self.plugin.client.receive.assert_not_awaited()
        await self.dispatch("1234")
        self.plugin.client.receive.assert_awaited_once()

    async def test_recent_text_only_after_explicit_reference(self):
        await self.dispatch("最近的文字")
        self.plugin.client.share_text.assert_not_awaited()
        await self.dispatch("帮我分享一下这个文字")
        self.plugin.client.share_text.assert_awaited_once_with("最近的文字", False)

    async def test_generic_share_does_not_upload_recent_text(self):
        await self.dispatch("最近的文字")
        await self.dispatch("帮我分享一下")
        self.plugin.client.share_text.assert_not_awaited()

    async def test_recent_file(self):
        source = Path(self.tmp.name) / "local.txt"
        source.write_text("test", encoding="utf-8")
        await self.dispatch(files=[File(file=str(source))])
        await self.dispatch("帮我分享一下这个文件")
        self.plugin.client.share_file.assert_awaited_once_with(source, "test.txt", False)
        self.assertTrue(source.exists())

    async def test_quote_content(self):
        await self.dispatch("帮我分享一下这个文字", reply=Reply(chain=[Plain("引用的正文")]))
        self.plugin.client.share_text.assert_awaited_once_with("引用的正文", False)

    async def test_cancel(self):
        await self.dispatch("帮我分享一下")
        cancelled = await self.dispatch("取消")
        self.assertIn("取消", cancelled.sent[0])
        await self.dispatch("不应该分享")
        self.plugin.client.share_text.assert_not_awaited()

    async def test_expired_wait(self):
        await self.dispatch("帮我取一下")
        self.plugin.states[("private:a", "alice")].deadline = time.monotonic() - 1
        result = await self.dispatch("1234")
        self.assertFalse(result.stopped)
        self.plugin.client.receive.assert_not_awaited()

    async def test_recent_expiry(self):
        await self.dispatch("旧的正文")
        self.plugin.states[("private:a", "alice")].recent_at = time.monotonic() - 1000
        await self.dispatch("帮我分享一下这个文字")
        self.plugin.client.share_text.assert_not_awaited()

    async def test_group_disabled_by_default(self):
        event = await self.dispatch("分享：正文", group="g1")
        self.assertFalse(event.stopped)
        self.plugin.client.share_text.assert_not_awaited()

    async def test_group_requires_mention_and_followup_allows_no_mention(self):
        self.plugin.config["enable_group"] = True
        await self.dispatch("分享：不应上传", group="g1")
        self.plugin.client.share_text.assert_not_awaited()
        await self.dispatch("帮我分享一下", group="g1", wake=True)
        await self.dispatch("正文", group="g1")
        self.plugin.client.share_text.assert_awaited_once_with("正文", False)

    async def test_allowlist(self):
        self.plugin.config["allowed_users"] = ["bob"]
        result = await self.dispatch("分享：正文")
        self.assertFalse(result.stopped)
        self.plugin.client.share_text.assert_not_awaited()

    async def test_disable_natural_language(self):
        self.plugin.config["natural_language"] = False
        await self.dispatch("分享：不应上传")
        self.plugin.client.share_text.assert_not_awaited()
        await self.dispatch("/123share 分享 正文")
        self.plugin.client.share_text.assert_awaited_once_with("正文", False)

    async def test_multi_file_limit(self):
        event = await self.dispatch("分享这个文件", files=[File() for _ in range(4)])
        self.plugin.client.share_file.assert_not_awaited()
        self.assertIn("最多分享 3", event.sent[0])

    async def test_file_receive_keeps_cache_for_lazy_adapter(self):
        self.plugin.client.receive.return_value = {
            "type": "file",
            "filename": "hello.txt",
            "downloadUrl": "https://123share.cn/download/hello",
        }

        async def download(url, path, **kwargs):
            path.write_bytes(b"hello")
            return path

        self.plugin.client.download.side_effect = download
        event = await self.dispatch("帮我取一下 1234")
        component = event.sent[0][0]
        self.assertTrue(Path(component.file).is_file())
        self.assertEqual(Path(component.file).read_bytes(), b"hello")

    async def test_attachment_send_failure_preserves_recovery_cache(self):
        self.plugin.client.receive.return_value = {
            "type": "file",
            "filename": "recover.txt",
            "downloadUrl": "https://123share.cn/download/recover",
            "burn": True,
        }

        async def download(url, path, **kwargs):
            path.write_bytes(b"recoverable")
            return path

        self.plugin.client.download.side_effect = download
        event = Event("帮我取一下 1234")

        async def send(result):
            if isinstance(result, list):
                raise RuntimeError("unsupported file send")
            event.sent.append(result)

        event.send = send
        await self.plugin.on_message(event)
        self.assertIn("本地缓存暂时保留", event.sent[0])
        self.assertEqual(self.plugin.client.receive.await_count, 1)
        self.assertTrue(any(self.plugin.root.glob("transfer_*/recover.txt")))

    async def test_long_text_is_split_without_modification(self):
        content = "字" * 3200
        self.plugin.client.receive.return_value = {"type": "text", "content": content}
        event = await self.dispatch("帮我取一下 1234")
        self.assertEqual([len(part) for part in event.sent[1:]], [1500, 1500, 200])
        self.assertEqual("".join(event.sent[1:]), content)

    async def test_cleanup_only_plugin_cache(self):
        original = Path(self.tmp.name) / "original.txt"
        original.write_bytes(b"original")
        directory = self.plugin._directory()
        (directory / "expired.txt").write_bytes(b"expired")
        self.plugin.owned[directory] = time.time() - 1
        await self.plugin._clean_files()
        self.assertFalse(directory.exists())
        self.assertTrue(original.exists())

    async def test_busy_guard(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def share(text, burn):
            started.set()
            await release.wait()
            return "1234"

        self.plugin.client.share_text.side_effect = share
        first = asyncio.create_task(self.dispatch("分享：正文"))
        try:
            await started.wait()
            second = await self.dispatch("分享：第二条")
            self.assertIn("仍在进行中", second.sent[0])
            self.assertEqual(self.plugin.client.share_text.await_count, 1)
        finally:
            release.set()
            await first

    async def test_state_memory_bound(self):
        for index in range(300):
            await self.dispatch("普通聊天", sender=str(index))
        self.assertLessEqual(len(self.plugin.states), 256)
