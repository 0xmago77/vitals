"""Content-addressed deliverable store for the bnbagent SDK.

`upload(manifest)` writes the canonical manifest JSON to
<data>/deliverables/<keccak256>.json and returns
https://<base>/deliverables/<keccak256>.json. The hash is exactly the
DeliverableManifest.manifest_hash() the SDK submits on chain, so anyone can
fetch the file, hash its bytes, and compare with the job's `deliverable`.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import requests
from bnbagent.storage.storage_provider import StorageProvider
from eth_utils import keccak

HASH_RE = re.compile(r"^0x[0-9a-f]{64}$")


def canonical(data: dict) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def content_hash(data: dict) -> str:
    return "0x" + keccak(text=canonical(data)).hex()


class ContentStore(StorageProvider):
    uses_file_url = False

    def __init__(self, directory: str | Path, base_url: str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url.rstrip("/")

    def path_for(self, h: str) -> Path:
        if not HASH_RE.fullmatch(h):  # match() would accept a trailing newline
            raise ValueError("not a deliverable hash")
        return self.dir / f"{h}.json"

    def url_for(self, h: str) -> str:
        return f"{self.base_url}/deliverables/{h}.json"

    def put(self, data: dict) -> tuple[str, str]:
        text = canonical(data)
        h = "0x" + keccak(text=text).hex()
        p = self.path_for(h)
        if not p.exists():
            tmp = p.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, p)
        return h, self.url_for(h)

    def read_bytes(self, h: str) -> bytes | None:
        p = self.path_for(h)
        return p.read_bytes() if p.exists() else None

    async def upload(self, data: dict, filename: str | None = None) -> str:  # noqa: ARG002
        _, url = self.put(data)
        return url

    async def download(self, url: str) -> dict:
        m = re.search(r"/deliverables/(0x[0-9a-f]{64})\.json$", url)
        if m and url.startswith(self.base_url):
            raw = self.read_bytes(m.group(1))
            if raw is not None:
                return json.loads(raw)
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()

    async def exists(self, url: str) -> bool:
        m = re.search(r"/deliverables/(0x[0-9a-f]{64})\.json$", url)
        return bool(m and self.path_for(m.group(1)).exists())
