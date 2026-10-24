"""AstrBot entry point: explicit consent, bounded transfers, per-user follow-ups."""

import asyncio
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Plain, Reply
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .client import ShareClient, ShareError, safe_filename
from .intent import Intent, extract_code, parse_intent

HELP = """123share 极简快传
• 帮我分享一下这个文字：需要分享的正文
• 帮我分享一下这个文件（附文件或引用文件消息）
• 先说“帮我分享一下”，再发送文字或文件
• 先发内容，再说“帮我分享一下这个文件/这段文字”
• 帮我取一下 1234；或先说“帮我取一下”，再发取件码
• /123share 分享 正文；/123share 取 1234
• /123share 取消；/123share 帮助
分享会上传到第三方 123share.cn，通常 2 小时过期。
取件可能消耗阅后即焚内容，不会自动重试。"""


@dataclass
class UserState:
    touched: float = field(default_factory=time.monotonic)
    pending: str = ""
    deadline: float = 0
    recent_text: str = ""
    recent_files: list = field(default_factory=list)
    recent_at: float = 0
    busy: bool = False
    last_transfer: float = float("-inf")


def number(config, key: str, default: int, low: int, high: int) -> int:
    try:
        return min(high, max(low, int(config.get(key, default))))
    except (TypeError, ValueError):
        return default


class Share123Plugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.wait_seconds = number(config, "followup_timeout", 120, 15, 600)
        self.cache_seconds = number(config, "recent_timeout", 120, 15, 600)
        self.retention = number(config, "file_retention_minutes", 30, 5, 120) * 60
        self.cooldown = number(config, "cooldown_seconds", 3, 0, 60)
        self.max_files = number(config, "max_files_per_message", 3, 1, 10)
        self.client = ShareClient(
            timeout=number(config, "request_timeout", 120, 10, 600),
            max_bytes=number(config, "max_file_mb", 100, 1, 100) * 1024 * 1024,
            download_hosts=tuple(str(x).lower() for x in config.get("download_hosts", [])),
            # Private attachment URLs are sometimes used by self-hosted adapters.
            # This opt-in applies to downloads, never to text-provided paths.
            allow_private=bool(config.get("allow_private_downloads", False)),
        )
        self.states: dict[tuple[str, str], UserState] = {}
        self.slots = asyncio.Semaphore(3)
        self.root = (
            Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_123share" / "cache"
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self.owned: dict[Path, float] = {}
        self.cleaner: asyncio.Task | None = None

    def _prune(self):
        now = time.monotonic()
        for key, state in list(self.states.items()):
            if state.pending and state.deadline < now:
                state.pending = ""
            if now - state.recent_at > self.cache_seconds:
                state.recent_text = ""
                state.recent_files.clear()
            if (
                not state.busy
                and not state.pending
                and now - state.touched > max(self.wait_seconds, self.cache_seconds)
            ):
                del self.states[key]

    async def _clean_files(self):
        """Only remove generated transfer directories, never adapter-owned files."""
        now = time.time()
        active = set(self.owned)
        for path in self.root.iterdir():
            if not path.is_dir() or path.is_symlink() or not path.name.startswith("transfer_"):
                continue
            if path in active and self.owned[path] == float("inf"):
                continue
            expiry = self.owned.get(path, path.stat().st_mtime + self.retention)
            if expiry < now:
                await asyncio.to_thread(shutil.rmtree, path)
                self.owned.pop(path, None)

    async def _maintenance(self):
        while True:
            try:
                self._prune()
                await self._clean_files()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"123share 缓存清理失败：{type(exc).__name__}")
            await asyncio.sleep(60)

    def _directory(self) -> Path:
        directory = self.root / ("transfer_" + uuid.uuid4().hex)
        directory.mkdir()
        self.owned[directory] = float("inf")
        return directory

    @staticmethod
    def _contents(components: list) -> tuple[str, list]:
        text = "".join(part.text for part in components if isinstance(part, Plain))
        files = [part for part in components if isinstance(part, File)]
        return text, files

    def _remember(self, state: UserState, text: str, files: list):
        if text.strip() or files:
            # Do not retain arbitrarily large messages or attachment lists.
            state.recent_text = text if len(text) <= 20000 else ""
            state.recent_files = files[: self.max_files + 1]
            state.recent_at = time.monotonic()

    async def _local_file(self, component: File, directory: Path) -> tuple[Path, str]:
        name = safe_filename(component.name or "file.bin")
        # get_file(allow_return_url=True) avoids an unbounded core downloader.
        source = component.url or await component.get_file(allow_return_url=True)
        if not source:
            raise ShareError("平台没有提供可读取的文件地址，请重新发送附件。")
        if source.startswith(("https://", "http://")):
            path = directory / (uuid.uuid4().hex + "_" + name)
            await self.client.download(source, path)
            return path, name
        path = Path(source)
        if not path.is_file():
            raise ShareError("附件不在 bot 可访问的文件系统中，请检查平台适配器。")
        return path, name

    def _shared_notice(self, code: str, label: str, burn: bool) -> str:
        return (
            f"{label}分享成功，取件码：{code}\n"
            f"https://123share.cn/?code={code}\n"
            f"{'已开启阅后即焚；' if burn else ''}站点通常 2 小时后销毁，请及时提取。"
        )

    async def _share(self, event: AstrMessageEvent, text: str, files: list):
        if len(files) > self.max_files:
            raise ShareError(f"一次最多分享 {self.max_files} 个文件，请分批发送。")
        burn = bool(self.config.get("burn_after_reading", False))
        if not files and not text.strip():
            raise ShareError("没有找到可以分享的文字或文件。")
        # Files and text are separate shares: the upstream has no combined API.
        directory = self._directory() if files else None
        try:
            for component in files:
                path, name = await self._local_file(component, directory)
                code = await self.client.share_file(path, name, burn)
                await event.send(
                    event.plain_result(self._shared_notice(code, f"文件 {name} ", burn))
                )
            if text.strip():
                code = await self.client.share_text(text, burn)
                await event.send(event.plain_result(self._shared_notice(code, "文字", burn)))
        finally:
            if directory:
                self.owned[directory] = time.time() + self.retention

    async def _receive(self, event: AstrMessageEvent, code: str):
        data = await self.client.receive(code)
        burn_hint = "\n注意：站点已/将按阅后即焚规则销毁内容。" if data.get("burn") else ""
        if data["type"] == "text":
            content = data["content"]
            # Avoid a single huge platform message. Do not wrap it in an LLM.
            await event.send(event.plain_result("文字提取成功。" + burn_hint))
            for start in range(0, len(content), 1500):
                await event.send(event.plain_result(content[start : start + 1500]))
            if not content:
                await event.send(event.plain_result("（空文本）"))
            return
        directory = self._directory()
        path = directory / data["filename"]
        try:
            await self.client.download(data["downloadUrl"], path, service_file=True)
            try:
                await event.send(
                    event.chain_result([File(name=data["filename"], file=str(path.resolve()))])
                )
            except Exception as exc:
                logger.warning(f"123share 文件发送失败：{type(exc).__name__}")
                # Do not promise the burn-after-reading download is still available.
                await event.send(
                    event.plain_result(
                        "已下载文件，但当前平台发送附件失败。请管理员检查平台文件发送能力。"
                        "若是阅后即焚文件，远端内容可能已销毁；本地缓存暂时保留供管理员恢复。"
                    )
                )
                return
            await event.send(event.plain_result(f"文件 {data['filename']} 提取成功。" + burn_hint))
        finally:
            # Some adapters read lazily; don't delete immediately after send().
            self.owned[directory] = time.time() + self.retention

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_message(self, event: AstrMessageEvent):
        """识别中文快传请求，并处理同一发送者的限时后续消息。"""
        if self.cleaner is None:
            self.cleaner = asyncio.create_task(self._maintenance())
        self._prune()
        sender = str(event.get_sender_id())
        allowed = {str(item) for item in self.config.get("allowed_users", [])}
        if allowed and sender not in allowed:
            return
        group = bool(event.get_group_id())
        if group and not self.config.get("enable_group", False):
            return
        components = event.get_messages()
        text, files = self._contents(components)
        intent = parse_intent(text)
        key = (event.unified_msg_origin, sender)
        state = self.states.get(key)
        pending = state.pending if state else ""
        if group and self.config.get("group_require_at", True) and not pending:
            if not (getattr(event, "is_at_or_wake_command", False) or (intent and intent.explicit)):
                return
        if state is None:
            if len(self.states) >= 256:
                # Bound memory without evicting active/pending interactions.
                candidates = [
                    (k, s) for k, s in self.states.items() if not s.busy and not s.pending
                ]
                if not candidates:
                    return
                del self.states[min(candidates, key=lambda pair: pair[1].touched)[0]]
            state = self.states[key] = UserState()
        state.touched = time.monotonic()
        if intent and intent.action == "help":
            event.stop_event()
            await event.send(event.plain_result(HELP))
            return
        if intent and intent.action == "cancel":
            state.pending = ""
            state.recent_text = ""
            state.recent_files.clear()
            event.stop_event()
            await event.send(
                event.plain_result(
                    "已取消等待并清除最近内容引用。"
                    + ("进行中的传输不会中止。" if state.busy else "")
                )
            )
            return
        if state.busy:
            if intent or pending:
                event.stop_event()
                await event.send(event.plain_result("你的上一项快传操作仍在进行中，请稍候。"))
            return
        followup = intent is None and bool(pending)
        if followup and text.strip() in {"取消", "/取消"}:
            state.pending = ""
            event.stop_event()
            await event.send(event.plain_result("已取消快传等待。"))
            return
        if followup:
            intent = Intent(pending, text)
        if intent is None:
            self._remember(state, text, files)
            return
        if not self.config.get("natural_language", True) and not intent.explicit and not followup:
            self._remember(state, text, files)
            return
        event.stop_event()
        if intent.action == "share":
            payload = intent.payload
            # Inline content wins; quoted content wins over the recent-message cache.
            if not payload.strip() and not files and not followup:
                for part in components:
                    if isinstance(part, Reply):
                        quoted_text, quoted_files = self._contents(part.chain or [])
                        payload = quoted_text or part.message_str or ""
                        files = quoted_files
                        if payload.strip() or files:
                            break
                if (
                    not payload.strip()
                    and not files
                    and any(word in text for word in ("这个", "这份", "这段", "这条", "这些"))
                ):
                    payload, files = state.recent_text, state.recent_files[:]
            if not payload.strip() and not files:
                state.pending = "share"
                state.deadline = time.monotonic() + self.wait_seconds
                await event.send(
                    event.plain_result(
                        f"请在 {self.wait_seconds} 秒内发送要分享的文字或文件（将上传到第三方 123share.cn）。\n"
                        "同一聊天中只接收你自己的下一条内容；发送“取消”可退出。"
                    )
                )
                return
        else:
            code = extract_code(intent.payload)
            if code is None:
                state.pending = "receive"
                state.deadline = time.monotonic() + self.wait_seconds
                await event.send(
                    event.plain_result(
                        f"请在 {self.wait_seconds} 秒内发送一个四位数字取件码或 123share 分享链接。\n发送“取消”可退出。"
                    )
                )
                return
        if time.monotonic() - state.last_transfer < self.cooldown:
            await event.send(event.plain_result("操作过于频繁，请稍等几秒后重新发送。"))
            return
        state.pending = ""
        state.recent_text = ""
        state.recent_files = []
        state.busy = True
        state.last_transfer = time.monotonic()
        try:
            async with self.slots:
                if intent.action == "share":
                    await self._share(event, payload, files)
                else:
                    await self._receive(event, code)
        except ShareError as exc:
            await event.send(event.plain_result(str(exc)))
        except Exception as exc:
            # Exception messages may contain signed URLs or content: log type only.
            logger.error(f"123share 操作失败：{type(exc).__name__}")
            await event.send(
                event.plain_result(
                    "快传操作失败，请检查网络、平台适配器和插件配置。不会自动重试；若部分内容已分享成功，其取件码仍有效。"
                )
            )
        finally:
            state.busy = False
            state.touched = time.monotonic()

    async def terminate(self):
        """Release network resources on plugin reload/unload."""
        if self.cleaner:
            self.cleaner.cancel()
            try:
                await self.cleaner
            except asyncio.CancelledError:
                pass
        await self.client.close()
        self.states.clear()
