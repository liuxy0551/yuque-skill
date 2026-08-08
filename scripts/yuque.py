#!/usr/bin/env python3

from __future__ import annotations

import argparse
import html
import json
import re
import os
import secrets
import string
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import requests


DEFAULT_TIMEOUT = 30


class YuqueError(RuntimeError):
    pass


class YuqueHttpError(YuqueError):
    """HTTP-layer Yuque error with a machine-readable status code."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


class TextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "h1", "h2", "h3", "h4", "h5", "h6",
        "p", "li", "tr", "table", "blockquote", "div", "pre",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag == "br":
            self.parts.append("\n")
        elif tag == "card":
            attrs_dict = dict(attrs)
            if attrs_dict.get("name") == "image":
                self.parts.append("\n[图片]\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        value = html.unescape("".join(self.parts))
        lines = [line.strip() for line in value.splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text(value: str) -> str:
    parser = TextExtractor()
    parser.feed(value or "")
    return parser.text()


def get_session_value() -> str:
    value = os.getenv("YUQUE_SESSION", "").strip()
    if not value:
        raise YuqueError(
            '缺少环境变量 YUQUE_SESSION，请先执行 '
            'export YUQUE_SESSION="<your _yuque_session value>"'
        )
    return value


def _normalize_yuque_origin(value: str) -> str:
    """Validate a Yuque HTTPS origin without relying on environment config."""
    value = value.strip().rstrip("/")
    if not value:
        raise YuqueError("语雀 Host 不能为空")

    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise YuqueError(f"语雀 Host 必须使用 HTTPS: {value!r}")

    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise YuqueError(
            "语雀 Host 必须是纯站点根地址，例如 "
            "https://dtstack.yuque.com"
        )

    try:
        port = parsed.port
    except ValueError as exc:
        raise YuqueError(f"非法语雀 Host 端口: {value!r}") from exc

    if port not in {None, 443}:
        raise YuqueError("语雀 Host 不允许自定义端口，仅允许 HTTPS 443")

    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "yuque.com":
        hostname = "www.yuque.com"

    if hostname != "yuque.com" and not hostname.endswith(".yuque.com"):
        raise YuqueError(
            f"只允许 yuque.com 或其子域名，当前 Host: {hostname!r}"
        )

    return f"https://{hostname}"


def same_origin(url: str, origin: str) -> bool:
    a = urlparse(url)
    b = urlparse(origin)
    return (
        a.scheme.lower() == b.scheme.lower()
        and (a.hostname or "").lower().rstrip(".")
        == (b.hostname or "").lower().rstrip(".")
        and (a.port or 443) == (b.port or 443)
    )


def clean_url(url: str) -> str:
    """Validate an absolute Yuque URL and canonicalize its origin."""
    value = url.strip()
    if not value:
        raise YuqueError("语雀 URL 不能为空")

    if value.startswith("/") or "://" not in value:
        raise YuqueError(
            "必须提供完整 HTTPS 语雀 URL，例如 "
            "https://dtstack.yuque.com/team/book/doc"
        )

    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise YuqueError(f"非法语雀 URL: {url}")

    if parsed.username or parsed.password:
        raise YuqueError("语雀 URL 不允许包含用户名或密码")

    try:
        port = parsed.port
    except ValueError as exc:
        raise YuqueError(f"非法语雀 URL 端口: {url!r}") from exc

    if port not in {None, 443}:
        raise YuqueError("语雀 URL 不允许自定义端口，仅允许 HTTPS 443")

    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "yuque.com":
        hostname = "www.yuque.com"

    if hostname != "yuque.com" and not hostname.endswith(".yuque.com"):
        raise YuqueError(
            f"只允许 yuque.com 或其子域名，当前地址: {hostname!r}"
        )

    path = parsed.path.rstrip("/")
    return urlunparse(("https", hostname, path, "", "", ""))


def parse_yuque_url(url: str) -> dict[str, Any]:
    url = clean_url(url)
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]

    if len(parts) < 2:
        raise YuqueError(f"语雀链接路径不足: {url}")

    user = parts[0]
    book = parts[1]

    if book == "notes":
        if len(parts) < 3:
            raise YuqueError("笔记链接缺少 note_id")
        return {
            "type": "note",
            "url": url,
            "origin": f"{parsed.scheme}://{parsed.netloc}",
            "user": user,
            "book": "notes",
            "doc": parts[2],
        }

    return {
        "type": "book" if len(parts) == 2 else "doc",
        "url": url,
        "origin": f"{parsed.scheme}://{parsed.netloc}",
        "user": user,
        "book": book,
        "doc": parts[2] if len(parts) > 2 else None,
    }


SEARCH_SCOPE_CHOICES = ("related", "organization", "group", "book")


def parse_search_scope_url(
    url: str,
    scope_mode: str | None = None,
) -> dict[str, Any]:
    """Resolve a Yuque search target into the Web search tab/scope pair.

    UI mapping observed on Yuque search pages:
      related      -> tab=related,      scope=/
      organization -> tab=organization, scope=/
      group        -> tab=group,        scope=<group>
      book         -> tab=book,         scope=<group>/<book>

    Group/book scopes are inferred from the URL when --scope is omitted.
    Root URLs require an explicit related/organization scope because both
    use the same "/" path.
    """
    normalized = clean_url(url)
    parsed = urlparse(normalized)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    parts = [part for part in parsed.path.split("/") if part]

    if parts and parts[0] == "search":
        raise YuqueError(
            "search 命令需要搜索范围 URL，不要传 /search 结果页 URL。"
            "例如使用站点根地址、团队 URL 或知识库 URL"
        )

    if scope_mode is not None and scope_mode not in SEARCH_SCOPE_CHOICES:
        raise YuqueError(
            f"不支持的搜索范围: {scope_mode!r}；"
            f"可选值: {', '.join(SEARCH_SCOPE_CHOICES)}"
        )

    inferred: str
    if not parts:
        if scope_mode not in {"related", "organization"}:
            raise YuqueError(
                "站点根地址无法区分“与我相关”和“组织搜索”，"
                "请显式指定 --scope related 或 --scope organization"
            )
        inferred = scope_mode
    elif len(parts) == 1:
        inferred = "group"
        if scope_mode is not None and scope_mode != inferred:
            raise YuqueError(
                "单段路径表示团队搜索，--scope 必须为 group 或省略"
            )
    else:
        inferred = "book"
        if scope_mode is not None and scope_mode != inferred:
            raise YuqueError(
                "两段及以上路径表示知识库搜索，--scope 必须为 book 或省略"
            )

    if inferred in {"related", "organization"}:
        internal_scope = "/"
        target_url = origin
    elif inferred == "group":
        internal_scope = parts[0]
        target_url = f"{origin}/{parts[0]}"
    else:
        internal_scope = f"{parts[0]}/{parts[1]}"
        target_url = f"{origin}/{internal_scope}"

    return {
        "origin": origin,
        "tab": inferred,
        "scope": internal_scope,
        "target_url": target_url,
    }


def unwrap_data(value: Any) -> Any:
    if isinstance(value, dict) and "data" in value:
        return value["data"]
    return value



def find_surrogates(value: str) -> list[tuple[int, int]]:
    return [
        (index, ord(char))
        for index, char in enumerate(value)
        if 0xD800 <= ord(char) <= 0xDFFF
    ]


def protect_non_bmp(value: str) -> tuple[str, dict[str, str]]:
    """Shield astral Unicode (mostly emoji) from Yuque's converter.

    Some Yuque deployments return broken lone UTF-16 surrogates when
    markdown containing non-BMP characters is converted to Lake.
    Replace those characters with ASCII-only tokens before conversion,
    then restore them in the serialized result.
    """
    bad = find_surrogates(value)
    if bad:
        pos, code = bad[0]
        raise YuqueError(
            "输入 Markdown 本身包含非法 surrogate 字符："
            f"位置 {pos}, U+{code:04X}"
        )

    non_bmp = [char for char in value if ord(char) > 0xFFFF]
    if not non_bmp:
        return value, {}

    # Pick a prefix that cannot collide with user content.
    serial = 0
    while True:
        prefix = f"YUQUEUNICODE{serial:04d}TOKEN"
        if prefix not in value:
            break
        serial += 1

    replacements: dict[str, str] = {}
    output: list[str] = []
    index = 0

    for char in value:
        if ord(char) <= 0xFFFF:
            output.append(char)
            continue

        token = f"{prefix}{index:06d}X"
        replacements[token] = char
        output.append(token)
        index += 1

    return "".join(output), replacements


def restore_non_bmp(value: str, replacements: dict[str, str]) -> str:
    result = value

    # Validate BEFORE replacement. If a converter drops, mutates, or
    # duplicates a token, replacing first would hide the corruption.
    for token in replacements:
        count = result.count(token)
        if count != 1:
            raise YuqueError(
                "Unicode 占位符在转换结果中未保持唯一完整："
                f"token={token!r}, count={count}。已停止写入"
            )

    for token, char in replacements.items():
        result = result.replace(token, char)

    bad = find_surrogates(result)
    if bad:
        pos, code = bad[0]
        raise YuqueError(
            "语雀转换结果仍包含非法 surrogate 字符："
            f"位置 {pos}, U+{code:04X}。已停止写入。"
        )

    return result


class YuqueClient:
    """Cookie-authenticated Yuque Web API client.

    The target origin must be explicit and is validated as a Yuque HTTPS
    domain. Every request is then hard-locked to that exact origin so
    `_yuque_session` cannot be forwarded to document-supplied links.
    """

    def __init__(self, origin: str) -> None:
        self.origin = _normalize_yuque_origin(origin)
        parsed = urlparse(self.origin)
        self.hostname = parsed.hostname or ""

        self.session = requests.Session()

        # Do not inherit HTTP(S)_PROXY / ALL_PROXY from the shell.
        # A Yuque login session is a credential and should not be
        # silently forwarded through local/global proxy software.
        self.session.trust_env = False

        self.session.cookies.set(
            "_yuque_session",
            get_session_value(),
            domain=self.hostname,
            path="/",
            secure=True,
        )

        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 yuque-skill/1.0",
            "Origin": self.origin,
            "Referer": self.origin + "/",
            "X-Requested-With": "XMLHttpRequest",
        })

        self._ctoken: str | None = None
        self._bootstrapped = False

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = urljoin(self.origin + "/", path.lstrip("/"))

        if not same_origin(url, self.origin):
            raise YuqueError(
                f"安全限制：拒绝向当前语雀 origin 之外发送请求: {url}"
            )
        return url

    def _raise_http_error(self, response: requests.Response) -> None:
        if response.status_code in {401, 403}:
            raise YuqueHttpError(
                response.status_code,
                "语雀登录态已失效或账号无权限，请刷新 YUQUE_SESSION",
            )

        snippet = response.text[:1200]
        raise YuqueHttpError(
            response.status_code,
            f"语雀接口失败 HTTP {response.status_code}: {snippet}",
        )

    def _ensure_json(self, response: requests.Response) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "")
        text_prefix = response.text[:500].lower()

        if "text/html" in content_type and "<html" in text_prefix:
            raise YuqueError(
                "语雀返回了 HTML 页面，YUQUE_SESSION 可能已失效"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise YuqueError(
                f"语雀接口未返回 JSON: {response.text[:1000]}"
            ) from exc

        if not isinstance(data, dict):
            raise YuqueError("语雀接口返回格式异常")

        return data

    def bootstrap(self, verify_auth: bool = True) -> str:
        if self._bootstrapped and self._ctoken:
            return self._ctoken

        # Never let requests auto-follow a redirect while carrying the
        # Yuque session. Validate every Location before sending the next
        # request so credentials cannot leave the exact origin.
        url = self.origin + "/"
        response: requests.Response | None = None

        for _ in range(5):
            try:
                response = self.session.get(
                    url,
                    timeout=DEFAULT_TIMEOUT,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                raise YuqueError(f"访问语雀失败: {exc}") from exc

            if 300 <= response.status_code < 400:
                location = response.headers.get("location", "").strip()
                if not location:
                    raise YuqueError(
                        f"语雀首页返回重定向 {response.status_code} 但缺少 Location"
                    )

                next_url = urljoin(url, location)
                if not same_origin(next_url, self.origin):
                    raise YuqueError(
                        "安全限制：语雀首页尝试跳转到当前 origin 之外，"
                        f"已在发送下一请求前停止: {next_url}"
                    )
                url = next_url
                continue

            break
        else:
            raise YuqueError("语雀首页重定向次数过多，已停止")

        assert response is not None

        if not response.ok:
            self._raise_http_error(response)

        if not same_origin(response.url or url, self.origin):
            raise YuqueError(
                "安全限制：语雀首页最终响应不属于当前 origin，已停止"
            )

        self._ctoken = (
            self.session.cookies.get("yuque_ctoken", domain=self.hostname)
            or self.session.cookies.get("yuque_ctoken")
        )

        if not self._ctoken:
            raise YuqueError(
                "未能从语雀首页响应获取 yuque_ctoken。"
                "当前站点可能需要不同的 CSRF 初始化方式。"
            )

        self._bootstrapped = True

        if verify_auth:
            self.request_json(
                "GET",
                "/api/mine/books",
                params={"limit": 1},
                bootstrap=False,
            )

        return self._ctoken

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
        bootstrap: bool = True,
        include_csrf: bool = False,
    ) -> dict[str, Any]:
        method = method.upper()

        if bootstrap and (method != "GET" or include_csrf):
            self.bootstrap()

        headers: dict[str, str] = {}
        if (method != "GET" or include_csrf) and self._ctoken:
            headers["x-csrf-token"] = self._ctoken

        try:
            response = self.session.request(
                method,
                self._url(path),
                params=params,
                json=json_body,
                data=form,
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise YuqueError(f"访问语雀失败: {exc}") from exc
        except UnicodeError as exc:
            raise YuqueError(
                f"语雀请求编码失败，输入可能包含非法 Unicode: {exc}"
            ) from exc

        if 300 <= response.status_code < 400:
            location = response.headers.get("location", "")
            raise YuqueError(
                f"语雀接口发生重定向 ({response.status_code} → "
                f"{location or 'unknown'})，登录态可能已失效"
            )

        if not response.ok:
            self._raise_http_error(response)

        return self._ensure_json(response)

    def convert(self, source: str, target: str, content: str) -> str:
        """Use Yuque's server-side document converter.

        The web editor calls this endpoint as form-urlencoded and its
        request middleware injects the `ctoken` field.
        """
        ctoken = self.bootstrap()

        payload = self.request_json(
            "POST",
            "/api/docs/convert",
            form={
                "from": source,
                "to": target,
                "content": content,
                "ctoken": ctoken,
            },
        )

        data = unwrap_data(payload)
        if not isinstance(data, dict):
            raise YuqueError(
                f"转换接口响应格式异常 ({source} → {target})"
            )

        result = data.get("content")
        if not isinstance(result, str) or not result.strip():
            raise YuqueError(
                f"转换接口没有返回 content ({source} → {target})"
            )

        return result

    def markdown_to_content(
        self,
        markdown: str,
    ) -> dict[str, str | None]:
        protected_markdown, unicode_map = protect_non_bmp(markdown)

        protected_body_asl = self.convert(
            "markdown",
            "lake",
            protected_markdown,
        )
        body_asl = restore_non_bmp(
            protected_body_asl,
            unicode_map,
        )

        # Do not reject the literal text "NaN": it is valid document
        # content (for example JavaScript documentation). Without a
        # schema-aware Lake parser, a substring check would be a false
        # positive. Unicode/surrogate integrity remains strictly checked.
        bad = find_surrogates(body_asl)
        if bad:
            pos, code = bad[0]
            raise YuqueError(
                "Markdown → Lake 结果包含非法 surrogate："
                f"位置 {pos}, U+{code:04X}，已停止写入"
            )

        body_html: str | None = None
        html_mode: str | None = None

        # HTML is optional for Lake documents. Some Yuque deployments
        # only allow markdown/asl -> lake on /api/docs/convert.
        # Probe HTML conversion best-effort; failure must not block save.
        #
        # For markdown -> html, use the protected Markdown too so the
        # server-side converter never sees astral Unicode directly.
        html_candidates = [
            # Keep astral Unicode shielded for lake -> html as well. This
            # prevents a converter from silently dropping/corrupting emoji
            # without producing an exception or lone surrogate.
            (
                "lake",
                "html",
                protected_body_asl,
                unicode_map,
            ),
            (
                "markdown",
                "html",
                protected_markdown,
                unicode_map,
            ),
        ]

        for source, target, content, restore_map in html_candidates:
            try:
                candidate = self.convert(source, target, content)
                if restore_map:
                    candidate = restore_non_bmp(
                        candidate,
                        restore_map,
                    )
            except YuqueError:
                continue

            if candidate.strip():
                bad = find_surrogates(candidate)
                if bad:
                    continue
                body_html = candidate
                html_mode = f"{source}->{target}"
                break

        return {
            "body_asl": body_asl,
            "body_html": body_html,
            "html_mode": html_mode,
        }


def _book_identity_candidates(
    book: dict[str, Any],
) -> tuple[set[str], set[str]]:
    """Return full namespaces and owner slugs exposed by a book payload."""
    namespaces: set[str] = set()
    owners: set[str] = set()

    namespace = book.get("namespace")
    if isinstance(namespace, str) and namespace.strip():
        normalized = namespace.strip().strip("/")
        namespaces.add(normalized)
        if "/" in normalized:
            owners.add(normalized.rsplit("/", 1)[0])

    for key in ("user", "owner", "group", "organization"):
        entity = book.get(key)
        if not isinstance(entity, dict):
            continue
        for field in ("login", "slug"):
            value = entity.get(field)
            if isinstance(value, str) and value.strip():
                owners.add(value.strip().strip("/"))

    return namespaces, owners


def _book_identity_match(
    book: dict[str, Any],
    requested_owner: str,
    requested_namespace: str,
) -> str:
    """Return exact / owner / mismatch / unknown for one book object.

    A concrete namespace is authoritative. Owner metadata is used only
    when the payload does not expose a namespace.
    """
    namespaces, owners = _book_identity_candidates(book)

    if namespaces:
        return "exact" if requested_namespace in namespaces else "mismatch"

    if owners:
        return "owner" if requested_owner in owners else "mismatch"

    return "unknown"


def _fetch_book_detail(
    client: YuqueClient,
    book_id: int,
) -> dict[str, Any] | None:
    """Fetch richer identity metadata for strict write-target validation."""
    try:
        payload = client.request_json(
            "GET",
            f"/api/books/{book_id}",
        )
    except YuqueHttpError as exc:
        # A deployment may simply not expose this detail route. Authentication,
        # permission, and server failures must not be hidden as "unknown".
        if exc.status_code in {404, 405}:
            return None
        raise

    data = unwrap_data(payload)
    return data if isinstance(data, dict) else None


def get_book_id(
    client: YuqueClient,
    info: dict[str, Any],
    *,
    require_verified_namespace: bool = False,
) -> int:
    if info["type"] == "note":
        raise YuqueError("笔记不需要 book_id")

    payload = client.request_json(
        "GET",
        "/api/mine/books",
        params={"limit": 1000},
    )

    books = payload.get("data") or []
    slug_matches = [
        book
        for book in books
        if isinstance(book, dict)
        and book.get("slug") == info["book"]
        and book.get("id") is not None
    ]

    if not slug_matches:
        raise YuqueError(
            f'当前账号可见知识库中找不到 slug={info["book"]}，'
            "请检查账号权限或 YUQUE_SESSION"
        )

    requested_owner = str(info["user"]).strip().strip("/")
    requested_namespace = f'{requested_owner}/{info["book"]}'

    matched: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    mismatch: list[dict[str, Any]] = []

    for book in slug_matches:
        state = _book_identity_match(
            book,
            requested_owner,
            requested_namespace,
        )
        if state in {"exact", "owner"}:
            matched.append(book)
        elif state == "unknown":
            unknown.append(book)
        else:
            mismatch.append(book)

    if len(matched) == 1:
        return int(matched[0]["id"])

    if len(matched) > 1:
        raise YuqueError(
            "知识库 namespace/owner 匹配到多个候选，拒绝自动选择："
            f"{requested_namespace}"
        )

    # When only one slug candidate exists but identity metadata is absent,
    # read-only operations may use it as a compatibility fallback. Writes
    # must first obtain richer metadata and prove the URL namespace/owner.
    if len(slug_matches) == 1 and len(unknown) == 1:
        candidate = unknown[0]
        candidate_id = int(candidate["id"])

        if not require_verified_namespace:
            return candidate_id

        detail = _fetch_book_detail(client, candidate_id)
        if detail is not None:
            detail_state = _book_identity_match(
                detail,
                requested_owner,
                requested_namespace,
            )
            if detail_state in {"exact", "owner"}:
                return candidate_id
            if detail_state == "mismatch":
                namespaces, owners = _book_identity_candidates(detail)
                observed = sorted(namespaces or owners)
                raise YuqueError(
                    "知识库 slug 匹配，但 URL namespace 与知识库详情不一致："
                    f"requested={requested_namespace}, observed={observed}"
                )

        raise YuqueError(
            "写操作拒绝仅凭唯一 slug 选择知识库："
            f"无法验证 URL namespace={requested_namespace}。"
            "请确认当前语雀实例的知识库接口能返回 namespace/owner 元数据"
        )

    if len(slug_matches) == 1 and mismatch:
        namespaces, owners = _book_identity_candidates(mismatch[0])
        observed = sorted(namespaces or owners)
        raise YuqueError(
            "知识库 slug 匹配，但 URL namespace 与服务端知识库归属不一致："
            f"requested={requested_namespace}, observed={observed}"
        )

    raise YuqueError(
        "存在多个相同 slug 的知识库，且无法唯一匹配 URL namespace；"
        f"拒绝按列表顺序选择。requested={requested_namespace}"
    )



def get_doc_data(
    client: YuqueClient,
    info: dict[str, Any],
    book_id: int | None = None,
) -> dict[str, Any]:
    if info["type"] != "doc":
        raise YuqueError("需要完整文档 URL")

    if book_id is None:
        book_id = get_book_id(client, info)

    payload = client.request_json(
        "GET",
        f'/api/docs/{info["doc"]}',
        params={"book_id": book_id},
    )

    data = unwrap_data(payload)
    if not isinstance(data, dict):
        raise YuqueError("文档接口响应格式异常")
    return data


def read_url(url: str) -> dict[str, Any]:
    info = parse_yuque_url(url)
    client = YuqueClient(info["origin"])

    if info["type"] == "note":
        payload = client.request_json(
            "GET",
            f'/api/notes/{info["doc"]}',
        )
        data = unwrap_data(payload)
        if not isinstance(data, dict):
            raise YuqueError("笔记接口响应格式异常")

        content = (
            data.get("content")
            or data.get("body_html")
            or data.get("body")
            or ""
        )
        return {
            "type": "note",
            "url": info["url"],
            "id": data.get("id"),
            "title": data.get("title") or data.get("name"),
            "updated_at": data.get("updated_at") or data.get("created_at"),
            "content_html": content,
            "content_available": bool(content),
            "content_state": "html" if content else "not_exposed_or_empty",
            "text": html_to_text(content),
            "raw": data,
        }

    if info["type"] == "book":
        raise YuqueError("知识库链接请使用 list 子命令")

    book_id = get_book_id(client, info)
    data = get_doc_data(client, info, book_id)

    content = (
        data.get("content")
        or data.get("body_html")
        or data.get("body")
        or ""
    )
    lake_body = (
        data.get("body_draft_asl")
        or data.get("body_asl")
        or ""
    )

    if content:
        content_state = "html"
    elif isinstance(lake_body, str) and lake_body.strip():
        content_state = "lake_only"
    else:
        content_state = "not_exposed_or_empty"

    return {
        "type": "doc",
        "url": info["url"],
        "id": data.get("id"),
        "book_id": book_id,
        "slug": data.get("slug") or info["doc"],
        "title": data.get("title"),
        "word_count": data.get("word_count"),
        "updated_at": data.get("updated_at") or data.get("created_at"),
        "published_at": data.get("published_at"),
        "draft_version": data.get("draft_version"),
        "content_html": content,
        "content_lake": lake_body,
        "content_available": bool(content),
        "lake_body_available": bool(
            isinstance(lake_body, str) and lake_body.strip()
        ),
        "content_state": content_state,
        "text": html_to_text(content),
        "raw": data,
    }


def _safe_search_result_url(
    origin: str,
    value: Any,
) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None

    candidate = urljoin(origin + "/", value.strip())
    if not same_origin(candidate, origin):
        return None

    parsed = urlparse(candidate)
    return urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        "",
        "",
        "",
    ))


def _normalize_search_hit(
    origin: str,
    hit: dict[str, Any],
) -> dict[str, Any]:
    record = hit.get("_record")
    if not isinstance(record, dict):
        record = {}

    abstract_html = hit.get("abstract")
    if not isinstance(abstract_html, str):
        abstract_html = ""

    raw_url = hit.get("url")
    absolute_url = _safe_search_result_url(origin, raw_url)

    return {
        "id": hit.get("id") or record.get("id"),
        "title": hit.get("title") or record.get("title"),
        "type": hit.get("type") or record.get("type"),
        "slug": hit.get("slug") or record.get("slug"),
        "url": absolute_url,
        "abstract": html_to_text(abstract_html),
        "abstract_html": abstract_html,
        "book_name": hit.get("book_name"),
        "group_name": hit.get("group_name"),
        "book_id": record.get("book_id"),
        "status": record.get("status"),
        "draft_version": record.get("draft_version"),
        "updated_at": record.get("updated_at"),
        "published_at": record.get("published_at"),
        "word_count": record.get("word_count"),
    }


def search_yuque(
    target_url: str,
    query: str,
    *,
    scope_mode: str | None = None,
    page: int = 1,
) -> dict[str, Any]:
    query = query.strip()
    if not query:
        raise YuqueError("搜索关键词不能为空")
    if page < 1:
        raise YuqueError("--page 必须大于等于 1")

    search_scope = parse_search_scope_url(
        target_url,
        scope_mode,
    )
    client = YuqueClient(search_scope["origin"])

    # The captured Yuque search-page request carries x-csrf-token even
    # though /api/zsearch is a GET. Bootstrap first and mirror that header
    # without copying browser-only cookies or x-login.
    payload = client.request_json(
        "GET",
        "/api/zsearch",
        params={
            "q": query,
            "type": "content",
            "scope": search_scope["scope"],
            "tab": search_scope["tab"],
            "p": page,
            "sence": "searchPage",
        },
        include_csrf=True,
    )

    data = unwrap_data(payload)
    if not isinstance(data, dict):
        raise YuqueError("zsearch 接口响应格式异常")

    hits = data.get("hits") or []
    if not isinstance(hits, list):
        raise YuqueError("zsearch hits 响应格式异常")

    results = [
        _normalize_search_hit(search_scope["origin"], hit)
        for hit in hits
        if isinstance(hit, dict)
    ]

    return {
        "type": "search",
        "query": query,
        "target_url": search_scope["target_url"],
        "search_scope": search_scope["tab"],
        "tab": search_scope["tab"],
        "scope": search_scope["scope"],
        "page": page,
        "total_hits": data.get("totalHits"),
        "num_hits": data.get("numHits"),
        "error_hits": data.get("errorHits"),
        "message": data.get("message"),
        "count": len(results),
        "results": results,
    }


def list_book(url: str) -> dict[str, Any]:
    info = parse_yuque_url(url)
    if info["type"] not in {"book", "doc"}:
        raise YuqueError("请输入知识库 URL")

    client = YuqueClient(info["origin"])
    book_id = get_book_id(client, info)

    page_size = 100
    max_docs = 5000
    offset = 0
    docs: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    pagination_supported: bool | None = None
    truncated = False

    while len(docs) < max_docs:
        payload = client.request_json(
            "GET",
            f"/api/books/{book_id}/docs",
            params={"limit": page_size, "offset": offset},
        )
        batch = payload.get("data") or []
        if not isinstance(batch, list):
            raise YuqueError("知识库文档列表接口响应格式异常")
        if not batch:
            break

        added = 0
        for item in batch:
            if not isinstance(item, dict):
                continue
            key = str(item.get("id") or item.get("slug") or "")
            if key and key in seen_keys:
                continue
            if key:
                seen_keys.add(key)

            slug = item.get("slug")
            doc_url = (
                f'{info["origin"]}/{info["user"]}/{info["book"]}/{slug}'
                if slug
                else None
            )
            docs.append({
                "id": item.get("id"),
                "title": item.get("title"),
                "slug": slug,
                "updated_at": item.get("updated_at"),
                "url": doc_url,
            })
            added += 1
            if len(docs) >= max_docs:
                truncated = True
                break

        if offset > 0 and added > 0:
            pagination_supported = True
        if len(batch) < page_size or len(docs) >= max_docs:
            break
        if added == 0:
            pagination_supported = False
            truncated = True
            break
        offset += page_size

    return {
        "type": "book",
        "url": f'{info["origin"]}/{info["user"]}/{info["book"]}',
        "book_id": book_id,
        "docs": docs,
        "count": len(docs),
        "truncated": truncated,
        "pagination_supported": pagination_supported,
    }


def get_catalog_nodes(
    client: YuqueClient,
    info: dict[str, Any],
    book_id: int | None = None,
) -> list[dict[str, Any]]:
    if info["type"] == "note":
        raise YuqueError("笔记没有知识库目录")

    if book_id is None:
        book_id = get_book_id(client, info)

    payload = client.request_json(
        "GET",
        "/api/catalog_nodes",
        params={"book_id": book_id},
    )

    data = unwrap_data(payload)
    if not isinstance(data, list):
        raise YuqueError("catalog_nodes 接口响应格式异常")

    nodes: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        nodes.append({
            "title": item.get("title"),
            "type": item.get("type"),
            "uuid": item.get("uuid"),
            "parent_uuid": item.get("parent_uuid") or "",
            "level": item.get("level"),
            "doc_id": item.get("doc_id"),
            "url": item.get("url"),
            "visible": item.get("visible"),
        })

    return nodes


def _catalog_paths(
    nodes: list[dict[str, Any]],
) -> dict[str, str]:
    by_uuid = {
        str(node["uuid"]): node
        for node in nodes
        if node.get("uuid")
    }
    cache: dict[str, str] = {}

    def build(uuid: str, seen: set[str] | None = None) -> str:
        if uuid in cache:
            return cache[uuid]

        seen = set(seen or ())
        if uuid in seen:
            return str(by_uuid.get(uuid, {}).get("title") or "")
        seen.add(uuid)

        node = by_uuid.get(uuid)
        if not node:
            return ""

        title = str(node.get("title") or "").strip()
        parent_uuid = str(node.get("parent_uuid") or "")

        if not parent_uuid or parent_uuid not in by_uuid:
            path = title
        else:
            parent_path = build(parent_uuid, seen)
            path = f"{parent_path}/{title}" if parent_path else title

        cache[uuid] = path
        return path

    for uuid in by_uuid:
        build(uuid)

    return cache


def catalog_tree(
    nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    paths = _catalog_paths(nodes)
    result = []
    for node in nodes:
        item = dict(node)
        uuid = str(node.get("uuid") or "")
        item["path"] = paths.get(uuid, str(node.get("title") or ""))
        result.append(item)
    return result


def resolve_parent_uuid(
    nodes: list[dict[str, Any]],
    parent: str | None,
    parent_uuid: str | None,
) -> str:
    if parent_uuid:
        matches = [
            node for node in nodes
            if str(node.get("uuid") or "") == parent_uuid
        ]
        if not matches:
            raise YuqueError(
                f"目录中不存在 parent UUID: {parent_uuid}"
            )
        return parent_uuid

    if not parent:
        return ""

    target = parent.strip().strip("/")
    if not target:
        return ""

    tree = catalog_tree(nodes)

    # Prefer exact path match.
    path_matches = [
        node for node in tree
        if str(node.get("path") or "").strip("/") == target
    ]
    if len(path_matches) == 1:
        return str(path_matches[0]["uuid"])
    if len(path_matches) > 1:
        raise YuqueError(
            f"目录路径存在多个同名节点: {target}"
        )

    # For a simple one-segment name, allow unique title lookup.
    if "/" not in target:
        title_matches = [
            node for node in tree
            if str(node.get("title") or "") == target
        ]
        if len(title_matches) == 1:
            return str(title_matches[0]["uuid"])
        if len(title_matches) > 1:
            candidates = ", ".join(
                str(node.get("path") or node.get("title") or "")
                for node in title_matches[:10]
            )
            raise YuqueError(
                f"目录名 {target!r} 不唯一，请使用完整 --parent-path。"
                f"候选: {candidates}"
            )

    raise YuqueError(
        f"找不到目录: {target!r}。"
        "可先执行 `python3 scripts/yuque.py toc <book-url>` 查看目录。"
    )


def toc_book(
    url: str,
) -> dict[str, Any]:
    info = parse_yuque_url(url)
    if info["type"] == "note":
        raise YuqueError("请输入知识库 URL")

    client = YuqueClient(info["origin"])
    book_id = get_book_id(client, info)
    nodes = get_catalog_nodes(client, info, book_id)

    return {
        "type": "toc",
        "url": f'{info["origin"]}/{info["user"]}/{info["book"]}',
        "book_id": book_id,
        "nodes": catalog_tree(nodes),
    }



def generate_slug(length: int = 16) -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def load_markdown(path: str) -> str:
    file_path = Path(path)
    if not file_path.exists():
        raise YuqueError(f"Markdown 文件不存在: {path}")
    return file_path.read_text(encoding="utf-8")


def load_markdown_source(
    markdown_file: str | None,
    use_stdin: bool,
) -> tuple[str, str]:
    """Read Markdown from an existing file or stdin.

    Prefer the original file when one already exists. stdin is intended
    for content that only exists in the current Agent/process context;
    do not create a temporary file merely to satisfy the CLI.
    """
    if bool(markdown_file) == bool(use_stdin):
        raise YuqueError(
            "必须且只能选择一种 Markdown 输入方式："
            "--file <path> 或 --stdin"
        )

    if use_stdin:
        markdown = sys.stdin.read()
        if not markdown:
            raise YuqueError("stdin 中没有收到 Markdown 内容")
        return markdown, "stdin"

    assert markdown_file is not None
    return load_markdown(markdown_file), markdown_file



_ATX_H1_RE = re.compile(r"^ {0,3}#(?:[ \t]+|$)(.*)$")
_SETEXT_H1_RE = re.compile(r"^ {0,3}=+[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _clean_atx_h1_text(value: str) -> str:
    value = re.sub(r"[ \t]+#+[ \t]*$", "", value)
    return value.strip()


def _normalize_title_for_compare(value: str) -> str:
    # Conservative comparison: normalize whitespace only.
    return re.sub(r"\s+", " ", value).strip()


def _is_setext_heading_text(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if re.match(
        r"^(?:#{1,6}\s|>|[-+*]\s|\d+[.)]\s|```|~~~|\|)",
        stripped,
    ):
        return False
    return True


def analyze_markdown_h1(markdown: str) -> dict[str, Any]:
    """Find real H1 blocks while ignoring fenced code."""
    lines = markdown.splitlines(keepends=True)
    h1s: list[dict[str, Any]] = []

    in_fence = False
    fence_char: str | None = None
    fence_len = 0
    first_block_line: int | None = None
    in_fence_flags: list[bool] = [False] * len(lines)

    for index, raw_line in enumerate(lines):
        line = raw_line.rstrip("\r\n")
        fence_match = _FENCE_RE.match(line)

        if in_fence:
            in_fence_flags[index] = True
            if fence_match:
                marker_text = fence_match.group(1)
                if (
                    marker_text[0] == fence_char
                    and len(marker_text) >= fence_len
                ):
                    in_fence = False
                    fence_char = None
                    fence_len = 0
            continue

        if fence_match:
            marker_text = fence_match.group(1)
            if first_block_line is None:
                first_block_line = index
            in_fence = True
            fence_char = marker_text[0]
            fence_len = len(marker_text)
            in_fence_flags[index] = True
            continue

        if not line.strip():
            continue

        if first_block_line is None:
            first_block_line = index

        atx_match = _ATX_H1_RE.match(line)
        if atx_match:
            h1s.append(
                {
                    "kind": "atx",
                    "title": _clean_atx_h1_text(atx_match.group(1)),
                    "start_line": index,
                    "end_line": index,
                }
            )

    for index in range(1, len(lines)):
        if in_fence_flags[index] or in_fence_flags[index - 1]:
            continue

        underline = lines[index].rstrip("\r\n")
        if not _SETEXT_H1_RE.match(underline):
            continue

        previous = lines[index - 1].rstrip("\r\n")
        if not _is_setext_heading_text(previous):
            continue
        if _ATX_H1_RE.match(previous):
            continue

        h1s.append(
            {
                "kind": "setext",
                "title": previous.strip(),
                "start_line": index - 1,
                "end_line": index,
            }
        )

    h1s.sort(key=lambda item: (item["start_line"], item["end_line"]))

    unique_first_h1 = (
        len(h1s) == 1
        and first_block_line is not None
        and h1s[0]["start_line"] == first_block_line
    )

    return {
        "count": len(h1s),
        "first_block_line": first_block_line,
        "unique_first_h1": unique_first_h1,
        "h1": h1s[0] if len(h1s) == 1 else None,
    }


def remove_markdown_block(
    markdown: str,
    start_line: int,
    end_line: int,
) -> str:
    lines = markdown.splitlines(keepends=True)
    body = "".join(lines[:start_line] + lines[end_line + 1 :])
    return body.lstrip("\r\n")


def _is_safe_plain_h1_title(value: str) -> bool:
    """Reject inline Markdown/HTML for automatic Yuque title inference."""
    value = value.strip()
    if not value:
        return False
    return not bool(re.search(r"[`<>\[\]*_~\\]", value))


def prepare_markdown_for_create(
    markdown: str,
    explicit_title: str | None,
) -> tuple[str, str, dict[str, Any]]:
    """Resolve Yuque title and safely remove a duplicated document H1."""
    analysis = analyze_markdown_h1(markdown)
    h1 = analysis.get("h1")

    if explicit_title is not None:
        title = explicit_title.strip()
        if not title:
            raise YuqueError("--title 不能为空")
        title_source = "explicit"
    else:
        if not analysis["unique_first_h1"] or not isinstance(h1, dict):
            raise YuqueError(
                "未提供 --title，且 Markdown 不能安全推断唯一文档标题。"
                "只有整篇恰好一个 H1 且该 H1 是第一个有效 Markdown block "
                "时才会自动作为语雀标题；否则请显式提供 --title。"
            )
        title = str(h1["title"]).strip()
        if not title:
            raise YuqueError("Markdown 唯一 H1 为空，无法作为语雀标题")
        if not _is_safe_plain_h1_title(title):
            raise YuqueError(
                "Markdown 唯一首 H1 含 Markdown/HTML 行内格式，"
                "为避免格式标记进入语雀 title，请显式提供 --title"
            )
        title_source = "markdown_h1"

    deduplicated = False
    body = markdown

    if analysis["unique_first_h1"] and isinstance(h1, dict):
        if (
            _normalize_title_for_compare(str(h1["title"]))
            == _normalize_title_for_compare(title)
        ):
            body = remove_markdown_block(
                markdown,
                int(h1["start_line"]),
                int(h1["end_line"]),
            )
            deduplicated = True

    return body, title, {
        "title_source": title_source,
        "h1_count": analysis["count"],
        "h1_deduplicated": deduplicated,
        "h1_kind": h1.get("kind") if isinstance(h1, dict) else None,
    }


def prepare_markdown_for_update(
    markdown: str,
    existing_title: str | None,
) -> tuple[str, dict[str, Any]]:
    """Remove a duplicated H1 only when it matches the existing Yuque title."""
    analysis = analyze_markdown_h1(markdown)
    h1 = analysis.get("h1")
    body = markdown
    deduplicated = False

    if (
        analysis["unique_first_h1"]
        and isinstance(h1, dict)
        and isinstance(existing_title, str)
        and existing_title.strip()
        and _normalize_title_for_compare(str(h1["title"]))
        == _normalize_title_for_compare(existing_title)
    ):
        body = remove_markdown_block(
            markdown,
            int(h1["start_line"]),
            int(h1["end_line"]),
        )
        deduplicated = True

    return body, {
        "h1_count": analysis["count"],
        "h1_deduplicated": deduplicated,
        "h1_kind": h1.get("kind") if isinstance(h1, dict) else None,
    }



def save_content(
    client: YuqueClient,
    doc_id: int,
    draft_version: int,
    content: dict[str, str | None],
) -> dict[str, Any]:
    body_asl = content.get("body_asl")
    if not isinstance(body_asl, str) or not body_asl.strip():
        raise YuqueError("保存前缺少有效 body_asl")

    payload: dict[str, Any] = {
        "format": "lake",
        "body_asl": body_asl,
        "draft_version": draft_version,
        "sync_dynamic_data": False,
        "save_type": "auto",
        "edit_type": "LakeCollab",
    }

    body_html = content.get("body_html")
    if isinstance(body_html, str) and body_html.strip():
        payload["body_html"] = body_html

    return client.request_json(
        "PUT",
        f"/api/docs/{doc_id}/content",
        json_body=payload,
    )


def _extract_nonempty_str(
    data: dict[str, Any] | None,
    field: str,
) -> str:
    if not isinstance(data, dict):
        return ""
    value = data.get(field)
    return value if isinstance(value, str) and value.strip() else ""


def _version_advanced(value: Any, previous: int) -> bool | None:
    try:
        return int(value) > int(previous)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def verify_saved_content(
    client: YuqueClient,
    info: dict[str, Any],
    book_id: int,
    previous_draft_version: int,
    *,
    save_response: dict[str, Any] | None = None,
    expected_doc_id: int | None = None,
) -> dict[str, Any]:
    """Verify the exact successful PUT /content version.

    The PUT response identifies the version produced by this write. A GET
    body is accepted as independent read-back proof only when its
    draft_version equals that exact saved version. If GET has already moved
    beyond it, another writer changed the document after this save and
    publication must stop.
    """
    response_doc = unwrap_data(save_response)
    if not isinstance(response_doc, dict):
        raise YuqueError(
            "保存接口返回成功，但响应中缺少可验证的文档对象"
        )

    response_id = _coerce_int(response_doc.get("id"))
    if expected_doc_id is not None:
        if response_id is None:
            raise YuqueError("保存响应缺少有效文档 id，无法确认目标文档")
        if response_id != int(expected_doc_id):
            raise YuqueError(
                "保存响应文档 id 与目标文档不一致："
                f"expected={expected_doc_id}, actual={response_id}"
            )

    saved_version = _coerce_int(response_doc.get("draft_version"))
    if saved_version is None:
        raise YuqueError("保存响应缺少有效 draft_version，无法确认本次写入版本")

    if saved_version <= int(previous_draft_version):
        raise YuqueError(
            "保存响应 draft_version 未前进："
            f"before={previous_draft_version}, saved={saved_version}"
        )

    response_asl = ""
    response_body_field: str | None = None
    for field in ("body_draft_asl", "body_asl"):
        value = _extract_nonempty_str(response_doc, field)
        if value:
            response_asl = value
            response_body_field = field
            break

    if not response_asl:
        raise YuqueError(
            "保存响应未返回 body_draft_asl/body_asl，"
            "无法确认本次写入正文"
        )

    current = get_doc_data(client, info, book_id)
    current_id = _coerce_int(current.get("id"))
    if expected_doc_id is not None and current_id is not None:
        if current_id != int(expected_doc_id):
            raise YuqueError(
                "保存后回读文档 id 与目标不一致："
                f"expected={expected_doc_id}, actual={current_id}"
            )

    readback_version = _coerce_int(current.get("draft_version"))
    if readback_version is not None and readback_version > saved_version:
        raise YuqueError(
            "检测到保存后的并发修改："
            f"本次保存版本={saved_version}，当前服务端版本={readback_version}。"
            "已停止后续发布"
        )

    saved_asl = response_asl
    body_field = response_body_field
    verification_source = "save_response"

    if readback_version == saved_version:
        readback_draft = _extract_nonempty_str(current, "body_draft_asl")
        if readback_draft:
            saved_asl = readback_draft
            body_field = "body_draft_asl"
            verification_source = "readback_exact_version"
        else:
            # For an unpublished document some deployments expose the
            # current Lake body through body_asl instead of body_draft_asl.
            unpublished = not bool(current.get("published_at"))
            readback_body = _extract_nonempty_str(current, "body_asl")
            if unpublished and readback_body:
                saved_asl = readback_body
                body_field = "body_asl"
                verification_source = "readback_exact_unpublished_body"

    bad = find_surrogates(saved_asl)
    if bad:
        pos, code = bad[0]
        raise YuqueError(
            "保存后的 Lake 正文包含非法 surrogate："
            f"位置 {pos}, U+{code:04X}，已停止后续发布"
        )

    return {
        "doc": current,
        "verification_source": verification_source,
        "body_field": body_field,
        "body_length": len(saved_asl),
        "draft_version": saved_version,
        "saved_draft_version": saved_version,
        "readback_draft_version": readback_version,
        "version_advanced": True,
    }



def publish_doc(
    client: YuqueClient,
    doc_id: int,
) -> dict[str, Any]:
    # The observed Yuque Web API payload does not expose a documented
    # conditional/version parameter. Therefore the pre/post version checks
    # around this call are best-effort, not an atomic compare-and-publish.
    return client.request_json(
        "PUT",
        f"/api/docs/{doc_id}/publish",
        json_body={
            "force": False,
            "notify": False,
            "cover": None,
            "ignoreGlobalMessage": True,
        },
    )


def _is_published_state(data: dict[str, Any] | None) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("published_at"):
        return True
    return data.get("status") in {1, "1", True, "published", "public"}


def _publish_marker(data: dict[str, Any] | None) -> tuple[Any, Any, Any]:
    if not isinstance(data, dict):
        return (None, None, None)
    return (
        data.get("published_at"),
        data.get("updated_at"),
        data.get("status"),
    )


def assert_publish_version(
    client: YuqueClient,
    info: dict[str, Any],
    book_id: int,
    *,
    expected_doc_id: int,
    expected_draft_version: int,
) -> dict[str, Any]:
    """Ensure the current document is still the exact draft we intend to publish."""
    last: dict[str, Any] | None = None

    for attempt in range(3):
        current = get_doc_data(client, info, book_id)
        last = current

        current_id = _coerce_int(current.get("id"))
        if current_id is not None and current_id != int(expected_doc_id):
            raise YuqueError(
                "发布前文档 id 与目标不一致："
                f"expected={expected_doc_id}, actual={current_id}"
            )

        current_version = _coerce_int(current.get("draft_version"))
        if current_version == int(expected_draft_version):
            return current

        if (
            current_version is not None
            and current_version > int(expected_draft_version)
        ):
            raise YuqueError(
                "发布前检测到并发修改："
                f"准备发布版本={expected_draft_version}，"
                f"当前服务端版本={current_version}。已停止发布"
            )

        if attempt < 2:
            time.sleep(0.35)

    actual = _coerce_int((last or {}).get("draft_version"))
    raise YuqueError(
        "发布前无法确认服务端已处于本次保存版本："
        f"expected={expected_draft_version}, actual={actual}"
    )


def verify_published_doc(
    client: YuqueClient,
    info: dict[str, Any],
    book_id: int,
    *,
    before_publish: dict[str, Any],
    expected_draft_version: int,
    publish_response: dict[str, Any] | None = None,
    expected_doc_id: int | None = None,
) -> dict[str, Any]:
    """Confirm this publish operation, not merely a historical published state."""
    response_doc = unwrap_data(publish_response)
    if not isinstance(response_doc, dict):
        response_doc = None

    if response_doc is not None and expected_doc_id is not None:
        response_id = _coerce_int(response_doc.get("id"))
        if response_id is not None and response_id != int(expected_doc_id):
            raise YuqueError(
                "发布响应文档 id 与目标不一致："
                f"expected={expected_doc_id}, actual={response_id}"
            )

    before_marker = _publish_marker(before_publish)
    response_marker = _publish_marker(response_doc)

    # A response marker that changed from the pre-publish snapshot is useful
    # evidence even when read-after-write is briefly stale.
    response_transition = (
        _is_published_state(response_doc)
        and response_marker != before_marker
    )

    for attempt in range(3):
        current = get_doc_data(client, info, book_id)

        current_id = _coerce_int(current.get("id"))
        if expected_doc_id is not None and current_id is not None:
            if current_id != int(expected_doc_id):
                raise YuqueError(
                    "发布后回读文档 id 与目标不一致："
                    f"expected={expected_doc_id}, actual={current_id}"
                )

        current_version = _coerce_int(current.get("draft_version"))
        if (
            current_version is not None
            and current_version > int(expected_draft_version)
        ):
            raise YuqueError(
                "发布过程中检测到并发修改："
                f"已保存版本={expected_draft_version}，"
                f"当前服务端版本={current_version}。"
                "无法确认发布的是本次保存内容"
            )

        marker = _publish_marker(current)
        if (
            _is_published_state(current)
            and marker != before_marker
            and current_version in {None, int(expected_draft_version)}
        ):
            return {
                "verified": True,
                "verification_source": "readback_transition",
                "published_at": current.get("published_at"),
                "updated_at": current.get("updated_at"),
                "status": current.get("status"),
                "draft_version": current_version,
                "concurrency_guarantee": "best_effort_pre_post_check",
            }

        if attempt < 2:
            time.sleep(0.35)

    if response_transition:
        response_version = _coerce_int(response_doc.get("draft_version"))
        if response_version not in {None, int(expected_draft_version)}:
            raise YuqueError(
                "发布响应版本与本次保存版本不一致："
                f"expected={expected_draft_version}, actual={response_version}"
            )
        return {
            "verified": True,
            "verification_source": "publish_response_transition",
            "published_at": response_doc.get("published_at"),
            "updated_at": response_doc.get("updated_at"),
            "status": response_doc.get("status"),
            "draft_version": response_version,
            "concurrency_guarantee": "best_effort_pre_post_check",
        }

    raise YuqueError(
        "发布接口返回成功，但没有观察到相对于发布前状态的新发布事件；"
        "拒绝把历史 published_at/status 当成本次发布成功证明"
    )



def save_with_retry(
    client: YuqueClient,
    info: dict[str, Any],
    book_id: int,
    content: dict[str, str | None],
    *,
    expected_doc_id: int | None = None,
    expected_draft_version: int | None = None,
    retry_on_409: bool = True,
) -> dict[str, Any]:
    """Save with optimistic concurrency.

    Existing-document updates should pass the id/version captured before
    conversion and set retry_on_409=False. That makes the PUT a true CAS:
    if another editor changes the document during conversion, the server's
    conflict is surfaced instead of refreshing to their version and
    overwriting it.

    Newly-created shell documents may retry one real HTTP 409 because they
    have no meaningful pre-existing collaborative content.
    """
    if (expected_doc_id is None) != (expected_draft_version is None):
        raise YuqueError(
            "expected_doc_id 与 expected_draft_version 必须同时提供或同时省略"
        )

    if expected_doc_id is not None and expected_draft_version is not None:
        doc = {
            "id": int(expected_doc_id),
            "draft_version": int(expected_draft_version),
        }
        doc_id_int = int(expected_doc_id)
        draft_version_int = int(expected_draft_version)
    else:
        doc = get_doc_data(client, info, book_id)
        doc_id = doc.get("id")
        draft_version = doc.get("draft_version")
        if doc_id is None or draft_version is None:
            raise YuqueError("无法从文档接口获取 id / draft_version")
        doc_id_int = int(doc_id)
        draft_version_int = int(draft_version)

    try:
        response = save_content(
            client,
            doc_id_int,
            draft_version_int,
            content,
        )
    except YuqueHttpError as exc:
        if exc.status_code != 409:
            raise

        if not retry_on_409:
            raise YuqueError(
                "文档在本次更新期间已被其他编辑者修改（HTTP 409）。"
                "为避免覆盖并发修改，已停止自动更新；"
                "请重新读取最新内容后再执行更新"
            ) from exc

        # Create-shell path only: refresh server version once and retry.
        refreshed = get_doc_data(client, info, book_id)
        refreshed_id = refreshed.get("id")
        refreshed_version = refreshed.get("draft_version")

        if refreshed_id is None or refreshed_version is None:
            raise YuqueError("版本冲突后仍无法获取 id / draft_version")

        if int(refreshed_id) != doc_id_int:
            raise YuqueError(
                "版本冲突后文档 id 发生变化，拒绝继续保存"
            )

        doc = refreshed
        draft_version_int = int(refreshed_version)
        response = save_content(
            client,
            doc_id_int,
            draft_version_int,
            content,
        )

    verification = verify_saved_content(
        client,
        info,
        book_id,
        draft_version_int,
        save_response=response,
        expected_doc_id=doc_id_int,
    )

    return {
        "doc": doc,
        "response": response,
        "verification": verification,
    }



def create_from_markdown(
    book_url: str,
    parent_uuid: str | None,
    parent_path: str | None,
    title: str | None,
    markdown_file: str | None,
    use_stdin: bool,
    publish: bool,
) -> dict[str, Any]:
    info = parse_yuque_url(book_url)
    if info["type"] not in {"book", "doc"}:
        raise YuqueError("book-url 必须是语雀知识库 URL")

    markdown, markdown_source = load_markdown_source(
        markdown_file,
        use_stdin,
    )
    markdown_body, resolved_title, title_meta = (
        prepare_markdown_for_create(
            markdown,
            title,
        )
    )
    client = YuqueClient(info["origin"])

    # Convert the final body before creating the shell document.
    content = client.markdown_to_content(markdown_body)

    book_id = get_book_id(
        client,
        info,
        require_verified_namespace=True,
    )
    catalog_nodes = get_catalog_nodes(client, info, book_id)
    target_uuid = resolve_parent_uuid(
        catalog_nodes,
        parent_path,
        parent_uuid,
    )
    slug = generate_slug()
    normalized_book_url = (
        f'{info["origin"]}/{info["user"]}/{info["book"]}'
    )

    created_url: str | None = None

    try:
        created_payload = client.request_json(
            "POST",
            "/api/docs",
            json_body={
                "book_id": int(book_id),
                "type": "Doc",
                "format": "lake",
                "title": resolved_title,
                "slug": slug,
                "body_draft_asl": None,
                "status": 0,
                "insert_to_catalog": True,
                "action": "prependChild",
                "target_uuid": target_uuid,
            },
        )

        created = unwrap_data(created_payload)
        if not isinstance(created, dict) or not created.get("id"):
            raise YuqueError(
                f"创建文档成功但响应中没有 doc id: {created!r}"
            )

        created_url = f"{normalized_book_url}/{slug}"
        created_info = parse_yuque_url(created_url)

        saved = save_with_retry(
            client,
            created_info,
            book_id,
            content,
        )

        if publish:
            saved_version = int(saved["verification"]["saved_draft_version"])
            doc_id = int(created["id"])
            before_publish = assert_publish_version(
                client,
                created_info,
                book_id,
                expected_doc_id=doc_id,
                expected_draft_version=saved_version,
            )
            publish_response = publish_doc(client, doc_id)
            publish_verification = verify_published_doc(
                client,
                created_info,
                book_id,
                before_publish=before_publish,
                expected_draft_version=saved_version,
                publish_response=publish_response,
                expected_doc_id=doc_id,
            )
        else:
            publish_verification = None

        saved_data = unwrap_data(saved["response"])
        return {
            "ok": True,
            "action": "create",
            "id": created.get("id"),
            "title": resolved_title,
            "slug": slug,
            "url": created_url,
            "published": publish,
            "publish_verification": publish_verification,
            "source": markdown_source,
            "title_source": title_meta["title_source"],
            "h1_count": title_meta["h1_count"],
            "h1_deduplicated": title_meta["h1_deduplicated"],
            "h1_kind": title_meta["h1_kind"],
            "parent_uuid": target_uuid,
            "parent_path": parent_path,
            "converter": {
                "markdown_to_lake": True,
                "html_mode": content.get("html_mode"),
            },
            "asl_length": len(content["body_asl"]),
            "html_length": len(content["body_html"] or ""),
            "draft_version": (
                saved_data.get("draft_version")
                if isinstance(saved_data, dict)
                else None
            ),
            "verification": saved.get("verification"),
        }

    except YuqueError as exc:
        if created_url:
            raise YuqueError(
                f"{exc}\n文档已经创建，但后续保存/发布或验证失败，请检查: {created_url}"
            ) from exc
        raise


def update_from_markdown(
    doc_url: str,
    markdown_file: str | None,
    use_stdin: bool,
    publish: bool,
) -> dict[str, Any]:
    info = parse_yuque_url(doc_url)
    if info["type"] != "doc":
        raise YuqueError("update 需要完整文档 URL")

    markdown, markdown_source = load_markdown_source(
        markdown_file,
        use_stdin,
    )
    client = YuqueClient(info["origin"])

    book_id = get_book_id(
        client,
        info,
        require_verified_namespace=True,
    )
    before = get_doc_data(client, info, book_id)

    markdown_body, title_meta = prepare_markdown_for_update(
        markdown,
        before.get("title"),
    )

    # Convert first; an unsupported converter cannot modify the doc.
    content = client.markdown_to_content(markdown_body)

    before_id = _coerce_int(before.get("id"))
    before_version = _coerce_int(before.get("draft_version"))
    if before_id is None or before_version is None:
        raise YuqueError("更新前无法获取有效 id / draft_version")

    saved = save_with_retry(
        client,
        info,
        book_id,
        content,
        expected_doc_id=before_id,
        expected_draft_version=before_version,
        retry_on_409=False,
    )

    if publish:
        saved_version = int(saved["verification"]["saved_draft_version"])
        doc_id = int(before["id"])
        before_publish = assert_publish_version(
            client,
            info,
            book_id,
            expected_doc_id=doc_id,
            expected_draft_version=saved_version,
        )
        publish_response = publish_doc(client, doc_id)
        publish_verification = verify_published_doc(
            client,
            info,
            book_id,
            before_publish=before_publish,
            expected_draft_version=saved_version,
            publish_response=publish_response,
            expected_doc_id=doc_id,
        )
    else:
        publish_verification = None

    saved_data = unwrap_data(saved["response"])
    return {
        "ok": True,
        "action": "update",
        "id": before.get("id"),
        "title": before.get("title"),
        "url": info["url"],
        "published": publish,
        "publish_verification": publish_verification,
        "source": markdown_source,
        "h1_count": title_meta["h1_count"],
        "h1_deduplicated": title_meta["h1_deduplicated"],
        "h1_kind": title_meta["h1_kind"],
        "converter": {
            "markdown_to_lake": True,
            "html_mode": content.get("html_mode"),
        },
        "asl_length": len(content["body_asl"]),
        "html_length": len(content["body_html"] or ""),
        "draft_version": (
            saved_data.get("draft_version")
            if isinstance(saved_data, dict)
            else None
        ),
        "verification": saved.get("verification"),
    }


def publish_existing(doc_url: str) -> dict[str, Any]:
    info = parse_yuque_url(doc_url)
    if info["type"] != "doc":
        raise YuqueError("publish 需要完整文档 URL")

    client = YuqueClient(info["origin"])
    book_id = get_book_id(
        client,
        info,
        require_verified_namespace=True,
    )
    doc = get_doc_data(client, info, book_id)

    if doc.get("id") is None:
        raise YuqueError("无法获取文档 id")

    draft_version = _coerce_int(doc.get("draft_version"))
    if draft_version is None:
        raise YuqueError("发布前无法获取有效 draft_version")

    doc_id = int(doc["id"])
    before_publish = assert_publish_version(
        client,
        info,
        book_id,
        expected_doc_id=doc_id,
        expected_draft_version=draft_version,
    )
    publish_response = publish_doc(client, doc_id)
    verification = verify_published_doc(
        client,
        info,
        book_id,
        before_publish=before_publish,
        expected_draft_version=draft_version,
        publish_response=publish_response,
        expected_doc_id=doc_id,
    )

    return {
        "ok": True,
        "action": "publish",
        "id": doc.get("id"),
        "title": doc.get("title"),
        "url": info["url"],
        "published": True,
        "publish_verification": verification,
    }


def check_markdown(
    host: str,
    markdown_file: str | None = None,
    use_stdin: bool = False,
) -> dict[str, Any]:
    """Convert a real Markdown file without modifying any document."""
    markdown, source = load_markdown_source(
        markdown_file,
        use_stdin,
    )
    client = YuqueClient(host)

    non_bmp_count = sum(
        1 for char in markdown
        if ord(char) > 0xFFFF
    )

    content = client.markdown_to_content(markdown)
    body_asl = content["body_asl"]
    body_html = content["body_html"]

    if not isinstance(body_asl, str) or not body_asl.strip():
        raise YuqueError("检查失败：Markdown → Lake 结果为空")

    surrogate_count = len(find_surrogates(body_asl))
    if surrogate_count:
        raise YuqueError(
            f"检查失败：Lake 中仍有 {surrogate_count} 个非法 surrogate"
        )

    return {
        "ok": True,
        "source": source,
        "markdown_length": len(markdown),
        "markdown_lines": len(markdown.splitlines()),
        "non_bmp_count": non_bmp_count,
        "asl_length": len(body_asl),
        "surrogate_count": surrogate_count,
        "html_available": bool(body_html),
        "html_mode": content.get("html_mode"),
        "html_length": len(body_html or ""),
        "safe_to_create": True,
    }



def doctor(host: str) -> dict[str, Any]:
    """Non-destructive capability probe using the real write conversion path."""
    client = YuqueClient(host)
    checks: dict[str, Any] = {
        "host": client.origin,
        "https": client.origin.startswith("https://"),
        "origin_lock": True,
        "proxy_env_disabled": client.session.trust_env is False,
        "authenticated": False,
        "ctoken": False,
        "markdown_to_lake": False,
        "lake_to_html": False,
        "markdown_to_html": False,
        "html_available": False,
        "unicode_shielding": True,
        "write_ready": False,
    }

    ctoken = client.bootstrap(verify_auth=True)
    checks["authenticated"] = True
    checks["ctoken"] = bool(ctoken)

    sample = (
        "# yuque-skill doctor\n\n"
        "This is a non-destructive conversion probe. 💡 📌 🚀\n\n"
        "- item 1\n"
        "- item 2\n"
    )

    content = client.markdown_to_content(sample)
    lake = content.get("body_asl") or ""
    html_body = content.get("body_html") or ""
    html_mode = content.get("html_mode")

    checks["markdown_to_lake"] = (
        isinstance(lake, str)
        and bool(lake.strip())
        and not find_surrogates(lake)
    )
    checks["lake_length"] = len(lake)
    checks["lake_to_html"] = html_mode == "lake->html"
    checks["markdown_to_html"] = html_mode == "markdown->html"
    checks["html_available"] = bool(html_body)
    checks["html_mode"] = html_mode
    checks["html_length"] = len(html_body)
    checks["write_ready"] = (
        checks["authenticated"]
        and checks["ctoken"]
        and checks["markdown_to_lake"]
    )

    return checks


def print_result(value: Any, format_name: str = "json") -> None:
    if format_name == "json":
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return

    if format_name == "raw":
        if isinstance(value, dict):
            raw = value.get("content_html") or value.get("content_lake")
            if raw is not None:
                print(raw)
                return
        print(value)
        return

    if format_name == "text":
        if isinstance(value, dict) and value.get("type") in {"doc", "note"}:
            title = value.get("title") or ""
            meta = []
            if value.get("word_count") is not None:
                meta.append(f'字数：{value["word_count"]}')
            if value.get("updated_at"):
                meta.append(f'更新时间：{value["updated_at"]}')

            if title:
                print(title)
            if meta:
                print(" | ".join(meta))
            if title or meta:
                print()

            text_value = value.get("text") or ""
            if text_value:
                print(text_value)
            elif value.get("content_state") == "lake_only":
                print("[正文仅以 Lake/ASL 返回；当前 text 输出不做猜测性解析]")
            elif value.get("content_state") == "not_exposed_or_empty":
                print("[正文未由当前接口返回；可能为空或企业实例隐藏未发布草稿正文]")
            return

        if isinstance(value, dict) and value.get("type") == "search":
            total = value.get("total_hits")
            count = value.get("count")
            print(
                f'搜索：{value.get("query") or ""} | '
                f'范围：{value.get("search_scope") or ""} '
                f'({value.get("scope") or "/"}) | '
                f'第 {value.get("page") or 1} 页 | '
                f'本页 {count if count is not None else "?"} 条'
                + (f" / 共 {total} 条" if total is not None else "")
            )
            results = value.get("results") or []
            if not results:
                print("未找到结果")
                return
            for index, item in enumerate(results, 1):
                print(f"\n{index}. {item.get('title') or ''}")
                if item.get("url"):
                    print(f"   {item['url']}")
                abstract = item.get("abstract") or ""
                if abstract:
                    print(f"   {abstract}")
            return

        if isinstance(value, dict) and value.get("type") == "book":
            for item in value.get("docs") or []:
                print(
                    f'- {item.get("title") or ""} '
                    f'{item.get("url") or ""}'.rstrip()
                )
            return

        if isinstance(value, dict) and value.get("type") == "toc":
            nodes = value.get("nodes") or []
            for node in nodes:
                level = node.get("level")
                try:
                    depth = max(0, int(level))
                except (TypeError, ValueError):
                    depth = max(0, str(node.get("path") or "").count("/"))
                prefix = "  " * depth
                kind = "[目录]" if node.get("type") == "TITLE" else "[文档]"
                uuid = node.get("uuid") or ""
                print(f"{prefix}{kind} {node.get('title') or ''}  ({uuid})")
            return

    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Search, read, and write Yuque documents with "
            "YUQUE_SESSION (no browser required)"
        )
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="只读检测登录态与服务端 Markdown/Lake 转换能力",
    )
    doctor_parser.add_argument(
        "--host",
        required=True,
        help="要检测的语雀实例根地址，例如 https://dtstack.yuque.com",
    )
    doctor_parser.set_defaults(command="doctor")

    check_parser = subparsers.add_parser(
        "check",
        help="无副作用检查真实 Markdown 是否可安全转换为 Lake",
    )
    check_parser.add_argument(
        "--host",
        required=True,
        help="执行转换检查的语雀实例根地址，例如 https://dtstack.yuque.com",
    )
    check_source = check_parser.add_mutually_exclusive_group(required=True)
    check_source.add_argument(
        "--file",
        help="待检查的现有 Markdown 文件",
    )
    check_source.add_argument(
        "--stdin",
        action="store_true",
        help="从标准输入读取 Markdown，不创建临时文件",
    )

    read_parser = subparsers.add_parser(
        "read",
        help="读取文档或笔记",
    )
    read_parser.add_argument("url")
    read_parser.add_argument(
        "--format",
        choices=["json", "text", "raw"],
        default="text",
    )

    search_parser = subparsers.add_parser(
        "search",
        help="搜索与我相关、组织、团队或知识库中的语雀内容",
    )
    search_parser.add_argument(
        "url",
        help=(
            "搜索范围 URL：站点根地址、团队 URL 或知识库 URL；"
            "不要传 /search 结果页 URL"
        ),
    )
    search_parser.add_argument("query", help="搜索关键词")
    search_parser.add_argument(
        "--scope",
        choices=SEARCH_SCOPE_CHOICES,
        help=(
            "搜索范围。根地址必须指定 related 或 organization；"
            "团队/知识库 URL 可自动推断 group/book"
        ),
    )
    search_parser.add_argument(
        "--page",
        type=int,
        default=1,
        help="搜索结果页码，默认 1",
    )
    search_parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="text",
    )

    list_parser = subparsers.add_parser(
        "list",
        help="列出知识库文档",
    )
    list_parser.add_argument("url")
    list_parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="text",
    )

    toc_parser = subparsers.add_parser(
        "toc",
        help="读取知识库完整目录树，包括 TITLE 分组与 DOC 节点",
    )
    toc_parser.add_argument("url")
    toc_parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="text",
    )

    create_parser = subparsers.add_parser(
        "create",
        help="从 Markdown 创建文档；默认仅保存草稿",
    )
    create_parser.add_argument("--book-url", required=True)
    parent_group = create_parser.add_mutually_exclusive_group()
    parent_group.add_argument(
        "--parent-uuid",
        help="目标目录/文档节点 UUID；高级用法",
    )
    parent_group.add_argument(
        "--parent-path",
        help="目标目录路径，如 分组1/子分组；推荐",
    )
    create_parser.add_argument(
        "--title",
        help=(
            "语雀文档标题。可省略：仅当 Markdown 恰好一个 H1 且该 H1 "
            "是第一个有效 block 时自动取该 H1"
        ),
    )
    create_source = create_parser.add_mutually_exclusive_group(required=True)
    create_source.add_argument(
        "--file",
        help="直接读取现有 Markdown 文件",
    )
    create_source.add_argument(
        "--stdin",
        action="store_true",
        help="从标准输入读取 Markdown，不创建临时文件",
    )
    create_parser.add_argument(
        "--publish",
        action="store_true",
        help="保存后立即发布；默认仅保存草稿",
    )

    update_parser = subparsers.add_parser(
        "update",
        help="用 Markdown 更新文档；默认仅保存草稿",
    )
    update_parser.add_argument("url")
    update_source = update_parser.add_mutually_exclusive_group(required=True)
    update_source.add_argument(
        "--file",
        help="直接读取现有 Markdown 文件",
    )
    update_source.add_argument(
        "--stdin",
        action="store_true",
        help="从标准输入读取 Markdown，不创建临时文件",
    )
    update_parser.add_argument(
        "--publish",
        action="store_true",
        help="保存后立即发布；默认仅保存草稿",
    )

    publish_parser = subparsers.add_parser(
        "publish",
        help="发布已有草稿",
    )
    publish_parser.add_argument("url")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "doctor":
            result = doctor(args.host)
            print_result(result)
            return 0 if result.get("write_ready") else 2

        if args.command == "check":
            print_result(
                check_markdown(
                    host=args.host,
                    markdown_file=args.file,
                    use_stdin=args.stdin,
                )
            )
            return 0

        if args.command == "read":
            print_result(read_url(args.url), args.format)
            return 0

        if args.command == "search":
            print_result(
                search_yuque(
                    args.url,
                    args.query,
                    scope_mode=args.scope,
                    page=args.page,
                ),
                args.format,
            )
            return 0

        if args.command == "list":
            print_result(list_book(args.url), args.format)
            return 0

        if args.command == "toc":
            print_result(toc_book(args.url), args.format)
            return 0

        if args.command == "create":
            print_result(
                create_from_markdown(
                    book_url=args.book_url,
                    parent_uuid=args.parent_uuid,
                    parent_path=args.parent_path,
                    title=args.title,
                    markdown_file=args.file,
                    use_stdin=args.stdin,
                    publish=args.publish,
                )
            )
            return 0

        if args.command == "update":
            print_result(
                update_from_markdown(
                    doc_url=args.url,
                    markdown_file=args.file,
                    use_stdin=args.stdin,
                    publish=args.publish,
                )
            )
            return 0

        if args.command == "publish":
            print_result(publish_existing(args.url))
            return 0

        parser.error("unknown command")
        return 2

    except YuqueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
