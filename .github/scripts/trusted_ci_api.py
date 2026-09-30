#!/usr/bin/env python3
"""Small stdlib REST client for trusted CI jobs on GitHub and Forgejo."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any


def api_base(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    api_url = env.get("GITHUB_API_URL", "").strip().rstrip("/")
    if api_url:
        parsed = urllib.parse.urlparse(api_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("GITHUB_API_URL must be an absolute HTTP(S) URL")
        return api_url

    server_url = env.get("GITHUB_SERVER_URL", "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(server_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("GITHUB_API_URL or GITHUB_SERVER_URL must be set")
    # Forgejo exposes the Gitea-compatible REST API under /api/v1. GitHub
    # Actions supplies GITHUB_API_URL directly, so this fallback is only used
    # by compatible self-hosted runners that omit it.
    return f"{server_url}/api/v1"


def _next_link(value: str) -> str | None:
    for part in value.split(","):
        match = re.match(r'\s*<([^>]+)>\s*;\s*rel="next"\s*$', part)
        if match:
            return match.group(1)
    return None


class TrustedCIAPI:
    def __init__(
        self,
        token: str,
        base_url: str,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not token:
            raise ValueError("GH_TOKEN must be set")
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("API base URL must be absolute HTTP(S)")
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.opener = opener

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        accept: str = "application/json",
        raw_response: bool = False,
    ) -> tuple[Any, Any]:
        if path.startswith(("http://", "https://")):
            raise ValueError("absolute API request URLs are not allowed")
        relative = path.lstrip("/")
        if not relative.startswith("repos/"):
            raise ValueError("API path must begin with repos/")
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/{relative}",
            data=data,
            method=method.upper(),
            headers={
                "Accept": accept,
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener(request, timeout=30) as response:
                response_body = response.read()
                if raw_response:
                    value = response_body
                else:
                    value = (
                        json.loads(response_body.decode("utf-8"))
                        if response_body
                        else None
                    )
                return value, response.headers
        except urllib.error.HTTPError as error:
            # Never include response bodies, request headers, or URLs in logs.
            raise APIError(error.code) from None

    def get(self, path: str) -> Any:
        return self.request("GET", path)[0]

    def get_raw(self, path: str, accept: str) -> bytes:
        return self.request("GET", path, accept=accept, raw_response=True)[0]

    def paginate(self, path: str) -> list[Any]:
        result: list[Any] = []
        next_url = f"{self.base_url}/{path.lstrip('/')}"
        while next_url:
            parsed = urllib.parse.urlparse(next_url)
            base = urllib.parse.urlparse(self.base_url)
            if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
                raise ValueError("API pagination link changed origin")
            prefix = base.path.rstrip("/") + "/"
            if not parsed.path.startswith(prefix):
                raise ValueError("API pagination link escaped API base path")
            relative = parsed.path[len(prefix) :] + (f"?{parsed.query}" if parsed.query else "")
            page, headers = self.request("GET", relative)
            if not isinstance(page, list):
                raise TypeError("paginated API endpoint must return a JSON list")
            result.extend(page)
            next_url = _next_link(headers.get("Link", "")) or ""
        return result


class APIError(RuntimeError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"API request failed with HTTP {status}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("method", choices=("GET", "POST", "DELETE"))
    parser.add_argument("path")
    parser.add_argument("--paginate", action="store_true")
    parser.add_argument("--body", help="JSON request body")
    parser.add_argument("--raw", action="store_true", help="write the response body unchanged")
    parser.add_argument("--accept", default="application/json", help="Accept header for --raw")
    args = parser.parse_args(argv)
    try:
        client = TrustedCIAPI(os.environ.get("GH_TOKEN", ""), api_base())
        body = json.loads(args.body) if args.body is not None else None
        if args.raw and (args.paginate or args.method != "GET" or body is not None):
            raise ValueError("raw responses are available only for GET requests")
        if args.paginate:
            if args.method != "GET" or body is not None:
                raise ValueError("pagination is available only for GET requests")
            result = client.paginate(args.path)
        elif args.raw:
            result = client.get_raw(args.path, args.accept)
        else:
            result, _ = client.request(args.method, args.path, body)
        if result is not None:
            if args.raw:
                sys.stdout.buffer.write(result)
            else:
                json.dump(result, sys.stdout, separators=(",", ":"))
                sys.stdout.write("\n")
        return 0
    except (OSError, ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
