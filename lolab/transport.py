"""Bounded, resumable HTTP jobs. No hidden redirects or credential-bearing logs."""
from __future__ import annotations

import email.utils
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from . import net
from .archive import now, safe_url


class DeferredRequest(ValueError):
    pass


class RequestError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_request(request, timeout=30):
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=net.ssl_context()))
    return opener.open(request, timeout=timeout)


def retry_delay(value, attempt):
    try:
        return max(0.0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0.0, (email.utils.parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return min(60, 2 ** attempt)


class Client:
    def __init__(self, warehouse, source, api_version, headers=None, interval=1.3, opener=open_request):
        self.warehouse, self.source, self.api_version = warehouse, source, api_version
        self.headers, self.interval, self.opener = headers or {}, max(0, interval), opener
        self.last = 0.0

    def fetch(self, url, kind, source_id, refresh=False):
        wh = self.warehouse
        key = f"{self.source}:{kind}:{source_id}:{safe_url(url)}"
        with wh.connect() as db:
            job = db.execute("SELECT * FROM jobs WHERE job_key=?", (key,)).fetchone()
            if job and job["status"] == "done" and not refresh:
                return job["response_id"], json.loads(wh.raw(job["response_id"]))
            if job and job["next_attempt"] > time.time():
                raise DeferredRequest("仍在限速等待期，请稍后重新运行；进度已保存")
            db.execute("INSERT OR IGNORE INTO jobs(job_key,status,updated_at) VALUES (?,'pending',?)", (key, now()))
        for attempt in range(4):
            with wh.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                limit = db.execute("SELECT next_request FROM source_limits WHERE source=?", (self.source,)).fetchone()
                moment = time.time()
                wait = max(0, (limit[0] if limit else moment) - moment)
                if wait > 30:
                    raise DeferredRequest("此来源仍在限速等待期；已保存断点，稍后重试")
                db.execute("INSERT OR REPLACE INTO source_limits VALUES (?,?)", (self.source, moment + wait + self.interval))
            if wait:
                time.sleep(wait)
            requested_at = now()
            with wh.connect() as db:
                db.execute("UPDATE jobs SET status='running',attempts=attempts+1,updated_at=? WHERE job_key=?", (now(), key))
            response_id, status, headers = None, 0, {}
            try:
                with self.opener(urllib.request.Request(url, headers={"User-Agent": "lolab/0.2", **self.headers}), timeout=30) as response:
                    body, status, headers = response.read(), response.status, dict(response.headers)
            except urllib.error.HTTPError as err:
                body, status, headers = err.read(), err.code, dict(err.headers or {})
            except (urllib.error.URLError, TimeoutError, OSError):
                body = None
            finally:
                self.last = time.monotonic()
            if body is not None:
                response_id = wh.capture(body, source=self.source, kind=kind, source_id=source_id,
                                         endpoint=url, api_version=self.api_version, status=status,
                                         headers=headers, requested_at=requested_at)
            if status == 200:
                try:
                    obj = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    self._status(key, "failed", "HTTP 200 返回了无效 JSON", response_id)
                    raise ValueError("接口返回了无效 JSON；原始响应已保留") from None
                self._status(key, "done", None, response_id)
                return response_id, obj
            retry = status in (0, 429, 500, 502, 503, 504)
            delay = retry_delay(next((v for k, v in headers.items() if k.lower() == "retry-after"), None), attempt)
            message = f"HTTP {status}" if status else "网络连接失败"
            self._status(key, "pending" if retry else "failed", message, response_id,
                         time.time() + delay if retry else 0)
            if status == 429:
                with wh.connect() as db:
                    db.execute("INSERT INTO source_limits VALUES (?,?) ON CONFLICT(source) DO UPDATE SET next_request=MAX(next_request,excluded.next_request)", (self.source, time.time() + delay))
            # Persist long cooldowns instead of hanging an interactive process.
            if not retry:
                raise RequestError(status, f"{message}，请检查数据权限或接口配置；响应已存档")
            if delay > 30 or attempt == 3:
                raise DeferredRequest(f"{message}，等待时间及断点已保存，稍后重新运行")
            time.sleep(delay)
        raise AssertionError("unreachable")

    def _status(self, key, status, error, response_id, next_attempt=0):
        with self.warehouse.connect() as db:
            db.execute("UPDATE jobs SET status=?,error=?,response_id=?,next_attempt=?,updated_at=? WHERE job_key=?",
                       (status, error, response_id, next_attempt, now(), key))
