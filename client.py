"""123share's public web protocol (not an officially versioned API)."""

import asyncio
import ipaddress
import json
import re
import socket
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver


class ShareError(Exception):
    """Safe, user-facing transfer error."""


def safe_filename(name: str) -> str:
    name = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', "_", name).strip(" .")
    # Filesystems commonly limit each filename to 255 UTF-8 bytes. Leave room
    # for the UUID prefix used when staging attachments; Chinese/emoji names
    # must be bounded in bytes, not just in Python characters.
    name = name.encode("utf-8", errors="replace")[:180].decode("utf-8", errors="ignore")
    name = name.rstrip(" .") or "download.bin"
    if name.split(".")[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(10)),
        *(f"LPT{i}" for i in range(10)),
    }:
        name = "_" + name
    return name


def public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


class PublicResolver(AbstractResolver):
    """Validate the exact DNS results used by the connector (DNS rebinding safe)."""

    def __init__(self):
        self.resolver = aiohttp.resolver.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        results = await self.resolver.resolve(host, port, family)
        if not results or any(not public_ip(item["host"]) for item in results):
            raise OSError("Non-public download address is blocked")
        return results

    async def close(self):
        await self.resolver.close()


class ShareClient:
    def __init__(
        self,
        base_url: str = "https://123share.cn",
        timeout: int = 120,
        max_bytes: int = 100 * 1024 * 1024,
        download_hosts: tuple[str, ...] = (),
        allow_private: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.allow_private = allow_private
        self.download_hosts = {urlsplit(self.base_url).hostname, *download_hosts}
        self.session: aiohttp.ClientSession | None = None

    def validate_url(self, url: str, service_file: bool = False) -> str:
        parts = urlsplit(url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
        ):
            raise ShareError("文件地址无效，仅支持无账号密码的 HTTP(S) 地址。")
        if parts.fragment:
            raise ShareError("文件地址不能包含 URL 片段。")
        if not self.allow_private:
            if parts.hostname.lower() == "localhost" or parts.hostname.endswith(".localhost"):
                raise ShareError("已阻止访问本机或内网文件地址。")
            try:
                address = ipaddress.ip_address(parts.hostname)
            except ValueError:
                pass  # PublicResolver validates DNS at connection time.
            else:
                if not public_ip(str(address)):
                    raise ShareError("已阻止访问本机或内网文件地址。")
        if service_file and parts.hostname not in self.download_hosts:
            raise ShareError("服务返回了未授权的下载域名，请管理员核实后添加 download_hosts。")
        return url

    async def _session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            connector = aiohttp.TCPConnector(
                resolver=None if self.allow_private else PublicResolver(),
                limit=6,
            )
            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={"User-Agent": "AstrBot-123share/1.0"},
                trust_env=False,
            )
            # aiohttp >=3.10 may transparently retry an idempotent GET on a
            # disconnected socket. /api/get can consume burn-after-reading data,
            # so even that transport-level retry must be disabled. Older aiohttp
            # releases do not have this automatic retry flag.
            if hasattr(self.session, "_retry_connection"):
                self.session._retry_connection = False
        return self.session

    async def close(self):
        if self.session:
            await self.session.close()

    async def _json(self, method: str, endpoint: str, **kwargs) -> dict:
        url = self.validate_url(self.base_url + endpoint)
        session = await self._session()
        try:
            # No implicit retries: even GET can consume burn-after-reading data.
            async with session.request(method, url, allow_redirects=False, **kwargs) as response:
                raw = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    raw.extend(chunk)
                    if len(raw) > 256 * 1024:
                        raise ShareError("服务返回的数据过大。")
                if not 200 <= response.status < 300:
                    # Do not echo arbitrary upstream bodies (may contain secrets).
                    if response.status == 429:
                        raise ShareError("123share 请求过于频繁，请稍后再试。")
                    if response.status in {404, 410}:
                        raise ShareError("取件内容不存在、已过期或已被阅后即焚。")
                    raise ShareError(f"123share 拒绝请求（HTTP {response.status}）。")
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ShareError("123share 响应不是有效 JSON，接口可能已变更。") from exc
                if not isinstance(data, dict):
                    raise ShareError("123share 响应格式异常。")
                if data.get("error"):
                    raise ShareError("123share 未完成操作，请检查取件码或稍后重试。")
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise ShareError("连接 123share 失败或超时；请稍后手动重试（不会自动重试）。") from exc

    @staticmethod
    def _code(data: dict) -> str:
        code = str(data.get("code", ""))
        if not re.fullmatch(r"[0-9]{4}", code):
            raise ShareError("123share 未返回有效的四位取件码。")
        return code

    async def share_text(self, text: str, burn: bool = False) -> str:
        # Browser uses JS string.length, i.e. UTF-16 code units.
        if not text.strip():
            raise ShareError("分享文字不能为空。")
        if len(text.encode("utf-16-le")) // 2 > 20000:
            raise ShareError("文字超过 20000 字符限制，请改为文件分享。")
        return self._code(
            await self._json("POST", "/api/share/text", json={"text": text, "burn": burn})
        )

    async def share_file(self, path: Path, name: str, burn: bool = False) -> str:
        if not path.is_file():
            raise ShareError("无法读取文件，请重新发送文件附件。")
        if path.stat().st_size > self.max_bytes:
            raise ShareError("文件超过插件或站点的大小限制。")
        with path.open("rb") as stream:
            form = aiohttp.FormData()
            form.add_field(
                "file",
                stream,
                filename=safe_filename(name),
                content_type="application/octet-stream",
            )
            form.add_field("burn", str(burn).lower())
            return self._code(await self._json("POST", "/api/share/file", data=form))

    async def receive(self, code: str) -> dict:
        if not re.fullmatch(r"[0-9]{4}", code):
            raise ShareError("请输入四位数字取件码。")
        data = await self._json("GET", f"/api/get/{code}")
        if data.get("type") == "text" and isinstance(data.get("content"), str):
            if len(data["content"].encode("utf-16-le")) // 2 > 20000:
                raise ShareError("接收到的文本超过站点限制。")
            return data
        if data.get("type") == "file" and isinstance(data.get("downloadUrl"), str):
            data["downloadUrl"] = self.validate_url(
                urljoin(self.base_url + "/", data["downloadUrl"]), service_file=True
            )
            data["filename"] = safe_filename(data.get("filename") or "download.bin")
            return data
        raise ShareError("123share 返回了无法识别的内容类型。")

    async def download(self, url: str, path: Path, service_file: bool = False) -> Path:
        """Bounded streaming; disallow redirects so checks cannot be bypassed."""
        self.validate_url(url, service_file)
        session = await self._session()
        try:
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise ShareError(f"文件下载失败（HTTP {response.status}，不自动跟随重定向）。")
                if response.content_length is not None and response.content_length > self.max_bytes:
                    raise ShareError("文件超过插件或站点的大小限制。")
                size = 0
                with path.open("wb") as stream:
                    async for chunk in response.content.iter_chunked(256 * 1024):
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise ShareError("文件超过插件或站点的大小限制。")
                        await asyncio.to_thread(stream.write, chunk)
            return path
        except BaseException as exc:
            path.unlink(missing_ok=True)
            if isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError, OSError)):
                raise ShareError("下载文件失败或超时，请检查网络和文件地址。") from exc
            raise
