# PubMed 与 OpenAlex 检索

此功能通过官方 API 获取文献元数据和可用摘要，支持选题核查。运行需要 Python 3.10+ 和网络，只用 Python 标准库。官方接口规则核查日期：2026-09-30。

## 使用

从本次任务的工作目录运行，以**安装后的脚本绝对路径**调用；`--out` 指向任务输出目录，不写入 Skill 安装目录。已有同名文件会被拒绝覆盖。先用解释器的 `--version` 检查 Python 3.10+；Windows 常用 `python` 或 `py -3`，macOS/Linux 常用 `python3`，以宿主实际可用命令为准。

以下 PowerShell / Bash / zsh 示例中，把 `<脚本绝对路径>` 替换为实际的 `scripts/search_literature.py` 路径（保留引号），并按环境替换解释器命令：

```sh
python "<脚本绝对路径>" --pubmed-query '(heat[Title/Abstract] OR temperature[Title/Abstract]) AND "mental health"[Title/Abstract]' --openalex-query '(heat OR temperature) AND "mental health"' --from-date 2020-01-01 --to-date 2026-09-30 --limit 50 --out ./literature-search.json
```

`cmd.exe` 的引号规则不同，不能照搬上面的单引号检索式；可改用 PowerShell，或由宿主按参数数组执行。若宿主禁止执行命令或外网连接，仍可离线选题或使用其已提供的检索工具，但应说明没有运行本数据库脚本。导入 Skill 本身不会授予额外运行权限。

只查一个库时仅提供对应的 `--pubmed-query` 或 `--openalex-query`。`--limit` 是**每个数据库**的获取上限，默认 50，可设为 1–1000；不代表总命中数。日期参数可省略，按发表日期筛选。不要未经判断使用示例中的关键词、日期和数量。

- PubMed 支持字段标签、MeSH 和布尔运算；脚本使用 ESearch 查 PMID、EFetch 批量取题录与摘要，并保留自动转换后的检索式和 API 警告。
- OpenAlex 使用 `works?search=...`，可用大写 `AND`/`OR`/`NOT` 和双引号短语；不能直接复用 PubMed 的 `[Title/Abstract]` 等标签。日期作为 `from_publication_date` / `to_publication_date` 过滤器。每页最多 100 条，以 cursor 翻页。
- 两库都按相关性取前若干条，适合有明确范围的选题核查。要做系统综述，需另行制定多库检索策略、完整导出、筛选和复核流程；这个有上限的脚本不能替代该流程。

## 认证与限额

PubMed 已实测可进行无密钥的少量查询。OpenAlex 官方认证规则允许无密钥基础查询，但 2026-09-30 本次测试时官方临时暂停匿名搜索并返回 503；成功搜索路径目前仅通过离线模拟测试。可等待匿名服务恢复，或配置自己的 key 后重新验证。可选环境变量如下；脚本不会自动读取 `.env` 文件：

| 环境变量 | 作用 |
| --- | --- |
| `NCBI_API_KEY` | NCBI 账户的 E-utilities key，可提高官方请求速率上限 |
| `NCBI_EMAIL` | 向 NCBI 提供的维护者联系邮箱；留空也能进行基础查询 |
| `OPENALEX_API_KEY` | OpenAlex key，通过 Authorization header 发送，可提高每日额度 |

可在 [NCBI 账户设置](https://www.ncbi.nlm.nih.gov/account/settings/) 与 [OpenAlex API 设置](https://openalex.org/settings/api) 获取自己的 key。在实际执行脚本的本机进程或宿主运行环境中配置环境变量，不要把真实 key 放进 skill、报告、命令示例或共享仓库。桌面应用、终端和云端沙箱不一定共享环境变量；修改持久环境变量后，已有终端/应用通常需重新启动才能继承。

NCBI 无 key 的限制是每个 IP 每秒 3 次，有 key 通常为每秒 10 次；脚本仍采用保守串行请求，多个进程的合计请求率需由使用者控制。OpenAlex 的无 key 基础额度与有 key 额度不同，额度与定价以官方页面为准；脚本不会自动购买或升级额度。429 或服务端暂时错误会有限重试，持续失败则记录错误并退出。

## 输出及解释

一个 JSON 同时保存检索记录与标准化文献：

- `searched_at_utc`：检索开始时间。
- `searches`：各库的原生检索式、日期、排序、`requested_limit`（获取上限）、总命中数、实际获取数、`truncated`、`status`、警告/错误及是否配置认证；不包含密钥或联系邮箱。`total_count: null` 表示尚未获得命中数。
- `records`：标题、作者、发表日期/年份、期刊、DOI、PMID、OpenAlex ID、可用摘要、原始链接、撤稿标记及 `sources`。缺失字段保持空缺，撤稿标记也不能代替独立核查。

跨库主要按规范化 DOI 或 PMID 合并并保留来源，不以相似标题强行合并。合并后的文献数可能小于各库获取数之和；没有共同标识符的重复记录可能仍然存在。

退出码 `0` 表示所选库均成功（包括真实零命中）；`1` 表示至少一库失败或只完成部分获取，已成功的结果仍写入文件；`2` 表示参数或输出路径错误。阅读结果时先查看 `status` 和 `truncated`。达到数量上限、网络失败和真正零命中是不同情况，必须分别说明。保留 JSON 能追溯当次检索，但数据库持续更新，同一检索式日后不保证返回完全相同结果。

## 官方依据

- [NCBI E-utilities 使用要求与认证](https://www.ncbi.nlm.nih.gov/books/NBK25497/)
- [NCBI ESearch / EFetch 参数](https://www.ncbi.nlm.nih.gov/books/NBK25499/)
- [NCBI 免责声明与版权说明](https://www.ncbi.nlm.nih.gov/About/disclaimer.html)：部分摘要有第三方版权，获取元数据不代表取得全文或再发布许可。
- [OpenAlex 认证与限额](https://help.openalex.org/api/authentication/)
- [OpenAlex 关键词检索](https://help.openalex.org/api/searching/)、[日期过滤](https://help.openalex.org/api/filtering/)、[分页](https://help.openalex.org/api/paging/)、[字段选择](https://help.openalex.org/api/selecting-fields/)
