# Yuque Web API Notes

> 这些接口来自语雀 Web 页面行为与前端源码，不是稳定的官方公开 API 契约。

## Session

```bash
YUQUE_SESSION=<value of _yuque_session>
```

目标 origin 来自命令中的完整语雀 URL；`doctor` / `check` 使用显式 `--host`。只接受 HTTPS `yuque.com` / `*.yuque.com`，并将所有内部请求锁定到该 exact origin。

## CSRF

先：

```http
GET /
Cookie: _yuque_session=...
```

服务器通常会设置：

```text
yuque_ctoken=...
```

写请求携带 `x-csrf-token`。

语雀前端自己的 AJAX middleware 也会将 `yuque_ctoken` 作为 `ctoken` 参数补进表单请求。

## 搜索 `/api/zsearch`

语雀 Web 搜索页使用：

```http
GET /api/zsearch
```

当前已验证的内容搜索公共参数：

```text
q=<query>
type=content
p=<page>
sence=searchPage
```

搜索边界由 `tab + scope` 决定：

| UI | `tab` | `scope` |
| --- | --- | --- |
| 与我相关 | `related` | `/` |
| 组织 | `organization` | `/` |
| 团队 | `group` | `<group-login>` |
| 知识库 | `book` | `<group-login>/<book-slug>` |

例如知识库搜索：

```http
GET /api/zsearch?q=bugfix-workflow&type=content&scope=rd-center%2Ftqk74v&tab=book&p=1&sence=searchPage
```

响应 `data` 中已观察到：

```text
hits
totalHits
numHits
errorHits
message
info
```

每个 hit 可包含：

```text
id
title
slug
type
url
abstract
book_name
group_name
_record
```

其中 `abstract` 带 `<em>` 搜索高亮；文本输出通过 HTML parser 去除标记，JSON 同时保留 `abstract` 纯文本与 `abstract_html` 原始值。

浏览器抓包中的 GET `/api/zsearch` 携带 `x-csrf-token`。实现会先 bootstrap `yuque_ctoken`，再为该 GET 附带 CSRF header；不会复制 `x-login`、`acw_tc`、`ssxmod_*` 等浏览器专用 header/cookie。

返回的 hit URL 只在解析后仍与当前 Yuque exact origin 同源时才作为可点击/后续读取 URL 暴露。

## 服务端 Markdown 转 Lake

编辑器配置：

```text
markdownPasteParse.convertURL = /api/docs/convert
```

前端 fallback 请求：

```http
POST /api/docs/convert
Content-Type: application/x-www-form-urlencoded
X-Requested-With: XMLHttpRequest

from=markdown
&to=lake
&content=<markdown>
&ctoken=<yuque_ctoken>
```

响应正文使用：

```text
response.data.content
```

## HTML

已捕获源码明确证明 `markdown → lake`。

`body_html` 不是 HTTP-only Lake 写入的硬前置条件。客户端在真实转换路径中按 fallback 顺序尝试 HTML：

1. 优先 `lake → html`
2. 仅当上一条失败时，再尝试 `markdown → html`

一旦前一条路径成功，后一条不会继续调用。因此 `doctor` 的 `lake_to_html` / `markdown_to_html` 字段反映最终采用的 HTML 模式，不等价于“两条路径都独立探测过”。

若任一路径可用，则保存时附带 `body_html`；若均不可用，则完全省略 `body_html` 字段，只提交 `body_asl`。不要传 `body_html: ""`。

## 创建

```http
POST /api/docs
Content-Type: application/json
x-csrf-token: ...

{
  "book_id": 82130857,
  "type": "Doc",
  "format": "lake",
  "title": "文档标题",
  "slug": "generated-slug",
  "body_draft_asl": null,
  "status": 0,
  "insert_to_catalog": true,
  "action": "prependChild",
  "target_uuid": "catalog-node-uuid"
}
```

## 保存草稿

```http
PUT /api/docs/{id}/content
Content-Type: application/json
x-csrf-token: ...
```

```json
{
  "format": "lake",
  "body_asl": "<converted Lake>",
  "draft_version": 24,
  "sync_dynamic_data": false,
  "save_type": "auto",
  "edit_type": "LakeCollab"
}
```

不要手工递增 `draft_version`。update 使用开始读取到的版本做 CAS，HTTP 409 时停止；只有 create 的新建空壳保存遇到真实 HTTP 409 时才允许重新读取后最多重试一次。

### 可选 body_html

如果目标实例支持 HTML 转换，可额外提交：

```json
{
  "body_html": "<converted HTML>"
}
```

不支持 HTML 转换时，省略该字段。

保存成功后以 `PUT /content` 响应中的 `draft_version` 作为本次写入的精确版本。GET 若返回更高版本则判定发生并发修改；只有 GET 版本与保存版本完全一致时，GET 正文才可作为独立回读证明。企业实例隐藏草稿正文时可使用 PUT 响应验证。发布前必须再次确认当前版本仍等于该保存版本。

## 发布

```http
PUT /api/docs/{id}/publish
```

```json
{
  "force": false,
  "notify": false,
  "cover": null,
  "ignoreGlobalMessage": true
}
```


发布后的历史 `published_at` / 已发布 `status` 不能单独证明本次发布成功。当前实现比较 `(published_at, updated_at, status)` 与发布前快照：文档仍处于已发布状态且该 marker 发生变化时，可作为 best-effort 的发布后验证信号；同时必须排除更高 `draft_version` 的并发修改。

## Catalog / TOC

完整目录：

```http
GET /api/catalog_nodes?book_id={book_id}
```

返回节点可包含：

```json
{
  "title": "分组1",
  "type": "TITLE",
  "uuid": "...",
  "parent_uuid": "",
  "level": 0,
  "doc_id": null,
  "url": null,
  "visible": true
}
```

`TITLE` 是纯目录节点，`DOC` 是文档节点。DOC 也可能拥有子节点。

创建文档时，选中的节点 `uuid` 作为：

```json
{
  "target_uuid": "<uuid>"
}
```


## Unicode / Emoji

部分企业语雀实例的：

```text
POST /api/docs/convert
from=markdown
to=lake
```

在直接接收非 BMP Unicode（例如 Emoji）时，可能返回孤立 UTF-16 surrogate。

客户端策略：

1. 转换前扫描输入；输入本身若有孤立 surrogate，直接拒绝。
2. 将 `ord(char) > 0xFFFF` 的字符替换为唯一 ASCII token。
3. 调用 `markdown -> lake`。
4. 保留受保护的 Lake 副本用于可选 `lake → html`，避免 HTML 转换器直接接收 astral Unicode。
5. 分别恢复 Lake / HTML 中的 token；每个 token 必须恰好出现一次。
6. 再次扫描结果；若仍出现 U+D800~U+DFFF，停止写入。
7. 不应直接保存含孤立 surrogate 的 Lake。

`check --host <origin> --file` / `check --host <origin> --stdin` 仅用于无副作用诊断；正常 `create` / `update` 已内置这条 Unicode 校验链路。

## CLI Markdown input

CLI 支持两种互斥输入源：

```text
--file <existing-path>
--stdin
```

已有可访问的 Markdown 文件时直接使用原文件；只有内容存在于当前 Agent/进程上下文时才使用 stdin，不为传输目的额外创建临时文件。


## Markdown H1 / Yuque title

Before `markdown -> lake`, the client performs conservative document-title
de-duplication.

An H1 is removed only when:

1. the whole Markdown document contains exactly one real H1;
2. that H1 is the first non-empty Markdown block;
3. its normalized text equals the final Yuque title.

ATX and Setext H1 are supported. H1-like text inside fenced code is ignored.

For create, `--title` may be omitted only when a unique first H1 can be safely
inferred. For update, the existing Yuque title is used for comparison and is
not changed.


## Target resolution hardening

Book lookup must not select the first matching slug. A concrete `namespace`
field is authoritative and has priority over owner metadata.

For write operations (`create`, `update`, `publish`), if `/api/mine/books`
does not expose enough namespace/owner metadata, the client tries a richer
`GET /api/books/{id}` lookup. If the URL `<owner>/<book>` still cannot be
verified, the write is rejected rather than guessed from a unique slug.

Read-only operations may retain a single-slug fallback for compatibility.

## Save and publish verification

The successful `PUT /content` response is the primary identity of this write:
matching doc id, non-empty body, and an advanced `draft_version` are required.
A GET body is independent proof only when its `draft_version` exactly equals
the version returned by this PUT. A higher GET version means another writer
changed the document after this save.

For `update`, the PUT uses the `draft_version` captured before Markdown
conversion. HTTP 409 stops the update; the client does not refresh to the
collaborator's newer version and overwrite it. Only the create-shell path may
retry one real HTTP 409.

Before publish, the current version is checked again. After publish, the client
requires a new publish-state transition and rejects higher draft versions.
This is best-effort only: the observed `/publish` request has no verified
conditional revision parameter, so the pre-check and publish call are not an
atomic compare-and-publish operation.

Literal text such as `NaN` is valid document content and is not rejected by
substring matching. Unicode integrity is enforced using surrogate validation
and non-BMP token round-trips, including optional HTML conversion.


## Read content states

`read --format json` reports `content_state` as `html`, `lake_only`, or `not_exposed_or_empty`. The last value intentionally does not claim that a draft is empty because some enterprise deployments hide unpublished bodies.

## Shell stdin safety

`--stdin` is safe when the caller writes raw bytes to process stdin. Shell callers must never embed untrusted Markdown in a fixed heredoc delimiter. Use a fresh high-entropy delimiter verified absent as a standalone source line, or use a safely written temporary file.
