"""Where published JSON goes: a local directory, Cloudflare Workers KV (``kv://<account_id>/<namespace_id>``, token in
``CLOUDFLARE_API_TOKEN``), or a ``gs://`` prefix.

Writes pass a cost guard: an object name must match the caller's ``writable`` pattern and stay under ``max_bytes``, so
a misbehaving loop can rewrite the same few objects but never multiply them. Each site passes its own pattern.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger(__name__)

KV_API = "https://api.cloudflare.com/client/v4/accounts/{account}/storage/kv/namespaces/{ns}/values/{key}"
MAX_OBJECT_BYTES = 8 * 1024 * 1024


class Store:
    def __init__(self, root: str, writable: re.Pattern[str], max_bytes: int = MAX_OBJECT_BYTES):
        self.root = root.rstrip("/")
        self.writable = writable
        self.max_bytes = max_bytes
        self._bucket = None
        self._kv = None
        if self.root.startswith("kv://"):
            import requests

            account, _, ns = self.root[5:].partition("/")
            token = os.environ["CLOUDFLARE_API_TOKEN"].strip()
            self._kv = (account, ns, requests.Session())
            self._kv[2].headers["Authorization"] = f"Bearer {token}"
        elif self.root.startswith("gs://"):
            from google.cloud import storage

            bucket, _, prefix = self.root[5:].partition("/")
            self._bucket = storage.Client().bucket(bucket)
            self._prefix = prefix

    def _blob(self, rel: str):
        name = f"{self._prefix}/{rel}" if self._prefix else rel
        return self._bucket.blob(name)

    def _kv_url(self, rel: str) -> str:
        account, ns, _ = self._kv
        return KV_API.format(account=account, ns=ns, key=quote(rel, safe=""))

    def read_json(self, rel: str) -> dict | None:
        if self._kv is not None:
            resp = self._kv[2].get(self._kv_url(rel), timeout=60)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        if self._bucket is not None:
            blob = self._blob(rel)
            return json.loads(blob.download_as_text()) if blob.exists() else None
        path = Path(self.root) / rel
        return json.loads(path.read_text()) if path.exists() else None

    def read_bytes(self, rel: str) -> bytes | None:
        if self._kv is not None:
            resp = self._kv[2].get(self._kv_url(rel), timeout=60)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.content
        if self._bucket is not None:
            blob = self._blob(rel)
            return blob.download_as_bytes() if blob.exists() else None
        path = Path(self.root) / rel
        return path.read_bytes() if path.exists() else None

    def write_json(self, rel: str, obj: dict, max_age: int) -> None:
        text = json.dumps(obj, separators=(",", ":"), allow_nan=False)
        self.write_bytes(rel, text.encode(), max_age, content_type="application/json")

    def write_bytes(self, rel: str, data: bytes, max_age: int, content_type: str = "application/octet-stream") -> None:
        if not self.writable.fullmatch(rel):
            raise ValueError(f"refusing to write unexpected object name: {rel}")
        if len(data) > self.max_bytes:
            raise ValueError(f"refusing to write {rel}: {len(data)} bytes exceeds {self.max_bytes}")
        if self._kv is not None:
            resp = self._kv[2].put(self._kv_url(rel), data=data, headers={"Content-Type": content_type}, timeout=120)
            resp.raise_for_status()
        elif self._bucket is not None:
            blob = self._blob(rel)
            blob.cache_control = f"public, max-age={max_age}"
            blob.upload_from_string(data, content_type=content_type)
        else:
            path = Path(self.root) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        log.info("wrote %s/%s (%d bytes)", self.root, rel, len(data))
