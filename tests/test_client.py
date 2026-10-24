import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web

from client import PublicResolver, ShareClient, ShareError, public_ip, safe_filename


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.records = []
        self.received_code = None
        self.received_text = "测试正文"
        self.burn = False
        self.text_status = 200
        self.disconnects = 0
        app = web.Application()

        async def share_text(request):
            self.records.append(await request.json())
            return web.json_response({"code": "0123"}, status=self.text_status)

        async def share_file(request):
            reader = await request.multipart()
            parts = []
            async for part in reader:
                parts.append((part.name, part.filename, bytes(await part.read())))
            self.records.append(parts)
            return web.json_response({"code": "4567"})

        async def receive(request):
            self.received_code = request.match_info["code"]
            if self.received_code == "0000":
                return web.json_response({"error": "gone"}, status=404)
            if self.received_code == "4567":
                return web.json_response(
                    {
                        "type": "file",
                        "filename": "../../unsafe.txt",
                        "downloadUrl": "/download",
                        "burn": self.burn,
                    }
                )
            return web.json_response(
                {"type": "text", "content": self.received_text, "burn": self.burn}
            )

        async def download(request):
            return web.Response(body=b"test file content")

        async def redirect(request):
            raise web.HTTPFound("/download")

        async def disconnect(request):
            self.disconnects += 1
            request.transport.close()
            return web.Response()

        async def bad(request):
            return web.Response(text="<html>not JSON</html>")

        async def large(request):
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b"a" * 32)
            await response.write_eof()
            return response

        app.router.add_post("/api/share/text", share_text)
        app.router.add_post("/api/share/file", share_file)
        app.router.add_get("/api/get/{code}", receive)
        app.router.add_get("/download", download)
        app.router.add_get("/redirect", redirect)
        app.router.add_get("/bad", bad)
        app.router.add_get("/disconnect", disconnect)
        app.router.add_get("/large", large)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        self.client = ShareClient(self.base, allow_private=True)
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()
        self.tmp.cleanup()

    async def test_text_roundtrip(self):
        code = await self.client.share_text("文字 🌏", True)
        self.assertEqual(code, "0123")
        self.assertEqual(self.records, [{"text": "文字 🌏", "burn": True}])
        self.assertEqual((await self.client.receive(code))["content"], "测试正文")

    async def test_file_protocol_and_download(self):
        path = self.path / "input.txt"
        path.write_bytes(b"original bytes")
        self.assertEqual(await self.client.share_file(path, "input.txt", True), "4567")
        self.assertEqual(
            self.records[0], [("file", "input.txt", b"original bytes"), ("burn", None, b"true")]
        )
        data = await self.client.receive("4567")
        self.assertEqual(data["filename"], "unsafe.txt")
        output = self.path / data["filename"]
        await self.client.download(data["downloadUrl"], output, service_file=True)
        self.assertEqual(output.read_bytes(), b"test file content")

    async def test_burn_flag_preserved(self):
        self.burn = True
        self.assertTrue((await self.client.receive("1234"))["burn"])

    async def test_utf16_text_limit(self):
        with self.assertRaises(ShareError):
            await self.client.share_text("🌏" * 10001)
        with self.assertRaises(ShareError):
            await self.client.share_text(" \n ")
        self.assertEqual(len(self.records), 0)

    async def test_content_length_size_limit(self):
        self.client.max_bytes = 4
        output = self.path / "large.bin"
        with self.assertRaises(ShareError):
            await self.client.download(self.base + "/download", output)
        self.assertFalse(output.exists())

    async def test_chunked_size_limit(self):
        self.client.max_bytes = 8
        output = self.path / "large.bin"
        with self.assertRaises(ShareError):
            await self.client.download(self.base + "/large", output)
        self.assertFalse(output.exists())

    async def test_redirect_blocked(self):
        with self.assertRaises(ShareError):
            await self.client.download(self.base + "/redirect", self.path / "redirect.bin")

    async def test_invalid_and_missing_code(self):
        with self.assertRaises(ShareError):
            await self.client.receive("12345")
        with self.assertRaises(ShareError):
            await self.client.receive("0000")
        with self.assertRaises(ShareError):
            await self.client._json("GET", "/bad")

    async def test_no_retry_on_rate_limit(self):
        self.text_status = 429
        with self.assertRaises(ShareError):
            await self.client.share_text("hello")
        self.assertEqual(len(self.records), 1)

    async def test_no_transport_retry_after_disconnect(self):
        with self.assertRaises(ShareError):
            await self.client._json("GET", "/disconnect")
        self.assertEqual(self.disconnects, 1)

    async def test_oversized_upload(self):
        self.client.max_bytes = 2
        path = self.path / "big.bin"
        path.write_bytes(b"too large")
        with self.assertRaises(ShareError):
            await self.client.share_file(path, "big.bin")
        self.assertEqual(len(self.records), 0)

    async def test_dns_resolver_blocks_private_results(self):
        resolver = PublicResolver()
        try:
            with patch.object(resolver.resolver, "resolve", return_value=[{"host": "127.0.0.1"}]):
                with self.assertRaises(OSError):
                    await resolver.resolve("anything.example", 80)
        finally:
            await resolver.close()


class SafetyTests(unittest.TestCase):
    def test_private_urls_blocked(self):
        client = ShareClient()
        for url in (
            "http://127.0.0.1/x",
            "http://localhost/x",
            "http://10.1.2.3/x",
            "http://169.254.169.254/x",
            "http://[::1]/x",
            "http://[::ffff:127.0.0.1]/x",
            "file:///etc/passwd",
            "https://name:password@123share.cn/x",
        ):
            with self.subTest(url=url), self.assertRaises(ShareError):
                client.validate_url(url)
        self.assertEqual(
            client.validate_url("https://123share.cn/download/a", True),
            "https://123share.cn/download/a",
        )
        with self.assertRaises(ShareError):
            client.validate_url("https://untrusted.example/a", True)

    def test_public_ip(self):
        self.assertTrue(public_ip("8.8.8.8"))
        for address in ("192.168.1.1", "127.0.0.1", "::1", "::ffff:127.0.0.1", "224.0.0.1"):
            self.assertFalse(public_ip(address))

    def test_filename_sanitization(self):
        self.assertEqual(safe_filename("../../secret.txt"), "secret.txt")
        self.assertEqual(safe_filename("C:\\folder\\test.txt"), "test.txt")
        self.assertEqual(safe_filename("CON.txt"), "_CON.txt")
        self.assertEqual(safe_filename("..."), "download.bin")
        self.assertNotIn("\n", safe_filename("x\ny.txt"))
        self.assertLessEqual(len(safe_filename("文件🌏" * 100).encode("utf-8")), 180)
