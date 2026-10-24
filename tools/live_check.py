"""Opt-in live roundtrip: only retrieves codes created by this very run."""

import argparse
import asyncio
import hashlib
import tempfile
import uuid
from pathlib import Path

from client import ShareClient


async def check():
    client = ShareClient(timeout=45)
    try:
        # All data is synthetic, and burn=True avoids leaving readable shares.
        text = "AstrBot-123share self-test " + uuid.uuid4().hex
        text_code = await client.share_text(text, burn=True)
        result = await client.receive(text_code)
        if result.get("type") != "text" or result.get("content") != text:
            raise RuntimeError("Live text roundtrip mismatch")
        print("PASS: live text upload + receive (burn-after-reading)")
        with tempfile.TemporaryDirectory(prefix="123share-selftest-") as temporary:
            directory = Path(temporary)
            payload = ("Synthetic AstrBot plugin test. " + uuid.uuid4().hex).encode()
            original = directory / "astrbot-selftest.txt"
            original.write_bytes(payload)
            file_code = await client.share_file(original, original.name, burn=True)
            result = await client.receive(file_code)
            if result.get("type") != "file":
                raise RuntimeError("Live file metadata type mismatch")
            output = directory / "received.txt"
            await client.download(result["downloadUrl"], output, service_file=True)
            if hashlib.sha256(output.read_bytes()).digest() != hashlib.sha256(payload).digest():
                raise RuntimeError("Live file SHA-256 mismatch")
            print("PASS: live file upload + metadata + download; SHA-256 matches")
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm uploading tiny synthetic text/file to 123share.cn",
    )
    if not parser.parse_args().confirm:
        parser.error("pass --confirm to permit synthetic test uploads to the real service")
    asyncio.run(check())
