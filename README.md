# yuque-skill

基于普通浏览器登录 Session 访问语雀 Web 内部接口的 CLI 工具与 Agent Skill。

免语雀 VIP Token，免官方 MCP，**无需 Chromium / Playwright**，仅依赖轻量级 `requests`。

---

## ✨ 核心特性

- 🔍 **精准搜索**：支持“与我相关”、整个组织、指定团队、指定知识库等全维度检索
- 📖 **文档读取**：纯文本阅读或 JSON 结构化提取（支持普通文档与小记）
- 🗂️ **目录树解析**：完整获取知识库层级目录（包含分组 `TITLE` 与文档 `DOC`）
- ✍️ **文档创建与更新**：本地 Markdown 直接由语雀服务端转 Lake 格式，支持草稿保存与显式发布
- 📦 **知识库批量导出**：一键导出整库文档为本地 Markdown 树，支持目录还原、自动重名消歧与断点续传
- 🛡️ **健壮保护**：版本 CAS 并发保护、保存回读校验、Emoji/Unicode 防损坏及严格同源锁定

---

## 🚀 快速开始

### 1. 环境准备

Python 3.10+，除原生标准库外仅依赖 `requests`（若环境已安装则无需任何操作）：

```bash
pip install requests  # 仅当缺失 requests 时安装
```

### 2. 配置登录凭证

从浏览器（已登录语雀的标签页）Cookie 中获取 `_yuque_session` 的值并导出到环境变量：

```bash
export YUQUE_SESSION="<你的 _yuque_session 值>"
```

> **安全说明**：目标 Origin 会自动从命令中的完整语雀 URL 解析，仅允许 HTTPS 且限制在 `*.yuque.com`；请求均被锁定在当前同源域，Session Cookie 不会发送到外部站点。

### 3. 环境与连通性诊断

首次使用或切换语雀域名后，运行 `doctor` 验证登录态与服务端能力：

```bash
python3 scripts/yuque.py doctor --host "https://your-team.yuque.com"
```

输出 `write_ready: true` 即表示登录态与 Lake 转换环境就绪（无任何文档读写副作用）。

---

## 💻 常用命令速查 (Cheat Sheet)

### 搜索内容

```bash
# 1. 与我相关搜索
python3 scripts/yuque.py search "https://your-team.yuque.com/" "关键词" --scope related

# 2. 整个组织搜索
python3 scripts/yuque.py search "https://your-team.yuque.com/" "关键词" --scope organization

# 3. 指定团队搜索
python3 scripts/yuque.py search "https://your-team.yuque.com/team-slug" "关键词"

# 4. 指定知识库搜索
python3 scripts/yuque.py search "https://your-team.yuque.com/team/book" "关键词"
```

### 读取文档与目录

```bash
# 读取文档为纯文本（添加 --format json 获取元数据与原始内容）
python3 scripts/yuque.py read "https://your-team.yuque.com/team/book/doc-slug"

# 查看知识库目录树（包含分组与文档）
python3 scripts/yuque.py toc "https://your-team.yuque.com/team/book"
```

### 批量导出知识库

```bash
# 批量导出整库全部文档为本地 Markdown 文件（支持断点续传）
python3 scripts/export_yuque_book.py \
  --book-url "https://your-team.yuque.com/team/book" \
  --out "./output_dir" \
  [--limit 10] [--sleep 0.3] [--retry 2]
```

*说明：同名路径自动追加 `_doc{id}` 避免覆盖；已存在且非空的本地 `.md` 自动跳过，随时中断可随时续传。*

### 创建文档

```bash
# 创建草稿（推荐使用 --parent-path 指定目录分组路径）
python3 scripts/yuque.py create \
  --book-url "https://your-team.yuque.com/team/book" \
  --parent-path "目录分组/子分组" \
  --title "文档标题" \
  --file "./doc.md"

# 创建并立即发布（追加 --publish）
python3 scripts/yuque.py create \
  --book-url "https://your-team.yuque.com/team/book" \
  --title "文档标题" \
  --file "./doc.md" \
  --publish
```

### 更新与发布

```bash
# 更新已有草稿
python3 scripts/yuque.py update "https://your-team.yuque.com/team/book/doc-slug" --file "./doc.md"

# 更新并立即发布
python3 scripts/yuque.py update "https://your-team.yuque.com/team/book/doc-slug" --file "./doc.md" --publish

# 发布已有草稿
python3 scripts/yuque.py publish "https://your-team.yuque.com/team/book/doc-slug"
```

---

## 💡 核心设计与保障

- **纯原生服务端转换**：复用语雀 Web 内部 `/api/docs/convert` 接口完成 `markdown → lake` 转换，彻底告别重量级无头浏览器与内存开销。
- **并发保护（CAS）**：更新时基于读取时的原版本号提交，遭遇其他人并发保存产生 HTTP 409 时主动终止，绝不强制覆盖对方内容。
- **保存回读校验**：提交草稿后比对响应版本号，并二次 GET 核验状态，确保内容真实落库后再执行发布。
- **Emoji / Unicode 保护**：针对语雀转换器偶发孤立 UTF-16 surrogate 的问题，转换前通过 ASCII 占位符保护并在生成 Lake 后恢复。
- **智能标题去重**：正文首行若存在与目标标题一致的唯一 H1，自动识别并去重，避免正文中多出一行大标题。

---

## 📚 延伸文档

- 🤖 **Agent 调度指令规范**：[SKILL.md](./SKILL.md)
- 🔌 **Web 内部 API 逆向细节备忘**：[references/api.md](./references/api.md)
- 🔒 **安全边界与隔离说明**：[SECURITY.md](./SECURITY.md)
