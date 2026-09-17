#!/usr/bin/env python3
"""批量导出语雀知识库中全部 DOC 文档为 Markdown（附断点续传）。

用法:
  python3 scripts/export_yuque_book.py \
      --book-url https://dtstack.yuque.com/rd-center/tqk74v \
      --out raw/yuque \
      [--limit N] [--sleep SEC] [--retry N]

行为:
  - 通过 toc 读取知识库目录树, 仅处理 type==DOC 的真实文档;
  - 对每篇调用 POST /api/docs/{doc_id}/export(type=markdown),
    下载签名返回的 Markdown 并按 path 存为 .md;
  - 已存在且非空的 .md 视为已完成, 自动跳过(retry 后缀除外),
    支持断点续传;
  - 选项 options 默认 latexType=2 & useMdai=1(与语雀网页端一致)。

依赖 YUQUE_SESSION 环境变量; 复用同目录下 yuque 模块的客户端,
不接收/硬编码任何 Cookie。
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yuque  # noqa: E402


def flatten_docs(nodes: list[dict]) -> list[dict]:
    """递归展平, 只保留 type==DOC 的节点, 附带祖先 path(由节点自身 path 提供)."""
    docs: list[dict] = []

    def walk(items):
        for it in items or []:
            if it.get("type") == "DOC" and it.get("doc_id"):
                docs.append(it)
            walk(it.get("children"))

    walk(nodes)
    return docs


def filter_subtree(
    nodes: list[dict], target_slug_or_id: str
) -> tuple[list[dict], str]:
    """提取指定文档节点及其全部下级节点，并计算需要去除的前缀路径"""
    target_node = None
    for n in nodes:
        if n.get("url") == target_slug_or_id or str(n.get("doc_id")) == str(target_slug_or_id):
            target_node = n
            break

    # 未找到匹配的目标节点时抛出异常
    if not target_node:
        raise yuque.YuqueError(f"在知识库目录中未找到文档: {target_slug_or_id}")

    target_uuid = str(target_node.get("uuid") or "")
    by_parent: dict[str, list[dict]] = {}
    for n in nodes:
        pid = str(n.get("parent_uuid") or "")
        by_parent.setdefault(pid, []).append(n)

    subtree_nodes: list[dict] = [target_node]

    # 递归收集所有子孙节点
    def collect(pid: str):
        for child in by_parent.get(pid, []):
            subtree_nodes.append(child)
            collect(str(child.get("uuid") or ""))

    collect(target_uuid)

    docs = [dict(n) for n in subtree_nodes if n.get("type") == "DOC" and n.get("doc_id")]

    # 计算目标节点在知识库中的祖先目录前缀
    target_path = target_node.get("path") or ""
    parent_prefix = target_path.rsplit("/", 1)[0] if "/" in target_path else ""

    if parent_prefix:
        prefix_with_slash = f"{parent_prefix}/"
        for d in docs:
            p = d.get("path") or ""
            # 若路径包含祖先目录前缀，截取相对子路径
            if p.startswith(prefix_with_slash):
                d["path"] = p[len(prefix_with_slash):]

    return docs, parent_prefix


def safe_filename(path: str) -> str:
    """把知识库 path(例: 新人指南/新人文档) 转成安全相对路径(无 .md)."""
    parts = [p.strip() for p in path.split("/") if p.strip()]
    # 清洗每个片段中的非法文件名字符
    cleaned = []
    for p in parts:
        p = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", p).strip(" .")
        cleaned.append(p or "untitled")
    return str(Path(*cleaned))


def dedupe_paths(docs: list[dict]) -> list[tuple[int, str]]:
    """为每个 doc 计算唯一相对路径。

    知识库 path 可能出现重名(同目录下多篇同名文档), 直接用 path 会撞名:
    同一 .md 被多篇文档共享, 断点续传会把后到的误判为"已下载"而漏导。
    对重复 path, 追加 _doc{id} 后缀保证唯一。
    返回 [(doc_id, 相对路径不含 .md)] 列表, 顺序与 docs 一致。
    """
    base = [safe_filename(d.get("path") or d.get("title") or str(d["doc_id"]))
            for d in docs]
    from collections import Counter
    counts = Counter(base)
    out = []
    for doc_id, b in zip((d["doc_id"] for d in docs), base):
        if counts[b] > 1:
            out.append((doc_id, f"{b}_doc{doc_id}"))
        else:
            out.append((doc_id, b))
    return out


def export_markdown(client: yuque.YuqueClient, doc_id: int,
                    out_root: Path, rel: Path) -> bytes | None:
    """导出单篇 Markdown 原文, 失败返回 None; 返回原始字节."""
    body = {"type": "markdown", "force": 0,
            "options": '{"latexType":2,"useMdai":1}'}
    data = client.request_json(
        "POST", f"/api/docs/{doc_id}/export", json_body=body)
    dl = (data.get("data") or {}).get("url")
    if not dl:
        raise yuque.YuqueError(f"doc {doc_id} 导出响应缺少 url: {data}")
    resp = client.session.get(dl, timeout=yuque.DEFAULT_TIMEOUT,
                              allow_redirects=True)
    if not resp.ok:
        raise yuque.YuqueError(
            f"doc {doc_id} 下载失败 HTTP {resp.status_code}")
    return resp.content


def with_yuque_link(content: bytes, full_url: str) -> bytes:
    """在 Markdown 原文顶部插入语雀原文链接行."""
    header = f"> 语雀原文: {full_url}\n\n"
    return header.encode("utf-8") + content


def main() -> int:
    ap = argparse.ArgumentParser(description="导出语雀知识库全部或指定节点 DOC 为 Markdown")
    ap.add_argument("--book-url", "--url", dest="url", required=True,
                    help="知识库或文档 URL")
    ap.add_argument("--doc-url", default=None,
                    help="指定导出的根文档 URL (可选, 也可以直接传给 --url)")
    ap.add_argument("--out", required=True, help="输出根目录")
    ap.add_argument("--limit", type=int, default=None,
                    help="只处理前 N 篇(用于试跑)")
    ap.add_argument("--sleep", type=float, default=0.3,
                    help="每篇之间的间隔秒数, 缓解限流")
    ap.add_argument("--retry", type=int, default=2, help="单篇失败重试次数")
    args = ap.parse_args()

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    target_input_url = args.doc_url or args.url
    book_info = yuque.parse_yuque_url(target_input_url)
    origin = book_info["origin"]
    book_base_url = f"{origin}/{book_info['user']}/{book_info['book']}"

    toc = yuque.toc_book(book_base_url)
    all_nodes = toc.get("nodes") or []

    target_doc = book_info.get("doc")
    # 判断是否指定了子文档或单篇文档节点
    if target_doc:
        docs, _ = filter_subtree(all_nodes, target_doc)
        print(f"文档节点 {target_input_url} 及下级: 共 {len(docs)} 篇真实 DOC 文档", file=sys.stderr)
    else:
        docs = flatten_docs(all_nodes)
        print(f"知识库 {book_base_url}: 共 {len(docs)} 篇真实 DOC 文档", file=sys.stderr)

    if args.limit:
        docs = docs[: args.limit]
        print(f"试跑模式: 仅处理前 {len(docs)} 篇", file=sys.stderr)

    # 为每篇计算唯一相对路径(重复 path 追加 _doc{id} 后缀)
    rel_paths = dedupe_paths(docs)

    client = yuque.YuqueClient(origin)

    # 构建 doc_id -> 语雀 URL 映射
    doc_url: dict[int, str] = {}
    for d in docs:
        slug = d.get("url")
        if slug:
            doc_url[d["doc_id"]] = f"{book_base_url}/{slug}"

    ok = skip = failed = 0
    failed_list: list[tuple[int, str, str]] = []

    for i, (doc, (doc_id, rel)) in enumerate(zip(docs, rel_paths), 1):
        rel = f"{rel}.md"
        target = out_root / rel
        # 断点续传: 已存在且非空视为完成
        if target.exists() and target.stat().st_size > 0:
            skip += 1
            continue

        last_err: Exception | None = None
        for attempt in range(args.retry + 1):
            try:
                content = export_markdown(client, doc_id, out_root, rel)
                # 顶部插入语雀原文链接
                full_url = doc_url.get(doc_id, "")
                if full_url:
                    content = with_yuque_link(content, full_url)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                ok += 1
                print(f"[{i}/{len(docs)}] OK  {rel} ({len(content)}B)",
                      file=sys.stderr)
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < args.retry:
                    time.sleep(args.sleep * 4)
        else:
            failed += 1
            failed_list.append((doc_id, str(rel), f"{type(last_err).__name__}: {last_err}"))
            print(f"[{i}/{len(docs)}] FAIL {rel}: {last_err}", file=sys.stderr)

        time.sleep(args.sleep)

    print(file=sys.stderr)
    print(f"完成: 成功 {ok}, 跳过(已存在) {skip}, 失败 {failed}", file=sys.stderr)
    for doc_id, rel, err in failed_list:
        print(f"  失败 doc_id={doc_id} {rel}: {err}", file=sys.stderr)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())