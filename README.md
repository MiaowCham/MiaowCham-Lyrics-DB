# MiaowCham Lyrics DB

喵锵的个人歌词库

本词库中的歌词按 `歌手/曲目/` 归档。同一曲目的不同格式、版本和附加内容放在同一个曲目目录中，便于查找和对照。

```text
lyrics raw file/
├── 歌手/
│   └── 曲目/
│       ├── lyrics.metadata
│       ├── applettml.曲目.ttml
│       ├── applettml.曲目-Format.ttml
│       ├── applejson.曲目.json
│       ├── 曲目.lys
│       ├── 曲目 (2).lys
│       ├── 曲目-trans.lrc
│       ├── 曲目.lrcn
│       └── 曲目.ass
├── LICENSE
├── LICENSE-APACHE
├── lyrics-index.json
├── tools/
└── README.md
```

## 元数据与索引

每个歌词目录包含一个 UTF-8 JSON `lyrics.metadata`。管理器会从 TTML 与 Lyrics Next/LRCN 头部提取曲名、艺术家、专辑、语言、词曲作者、平台 ID、制作者与来源；缺失字段可在界面中手工填写。根目录的 `lyrics-index.json` 是由管理器生成的轻量索引，供本地搜索与 GitHub Pages 使用。

元数据 schema 当前为 v2。一个目录可以包含多首歌曲：`tracks` 保存独立曲目实体，`files[].metadataRef` 把同一歌曲的 TTML、LRCN 或其他格式关联到同一实体。修改共享实体后，其关联文件会一起获得更新；不同 `metadataRef` 的歌曲不会互相污染。扫描只补充可发现的信息，已有人工值优先。

## Database 管理器

管理器使用 Python 3 标准库，无需安装第三方依赖。在仓库根目录运行：

```powershell
python -m tools.lyrics_manager
```

Windows 用户也可以直接双击根目录的 `lyrics-manager.pyw`，无控制台启动图形界面。

主要功能：

- 自动扫描 `Lyrics/歌手/目录`，创建或维护 `lyrics.metadata` 与根索引；
- 使用可拖动宽度的注册表式目录树浏览、搜索并编辑曲目信息、平台 ID、来源；每个歌词文件是独立入口，可跨目录关联到其他曲目、选择迁移到目标目录，或拆分为独立实体；歌词和曲目节点可拖放后选择关联、移动或取消；
- 快捷打开歌词或在文件管理器中定位；除 ASS 外的仓库歌词格式可按“行号、Agent、原文、翻译、音译”逐行预览；
- 文件名可直接编辑，保存时确认是否同步重命名；歌词库扫描在后台执行，进度与一般操作结果显示在窗口底部状态栏；
- 左侧所有节点均可删除：文件删除会解除其元数据关联，曲目实体删除会保留歌词为未绑定状态，目录、歌手和歌词库根节点删除均会进行明确确认；
- 兼容 TTML body 内旧式翻译/音译和背景歌词；旧格式文件在树中标黄，可一键转换到 head，新旧转换提供 `.bak` 原文件备份与恢复；
- 显式把元数据同步回 TTML/LRCN 头部，保留歌词正文、时间轴和未知字段；
- 通过类似 GitHub Desktop 的版本页查看变更、差异和历史，以及暂存、取消暂存、提交、Fetch、仅快进拉取和推送；进入版本管理页会自动刷新状态，Git 操作在后台串行执行，Windows 不显示终端窗口；
- 注重明暗外观：自动识别系统暗色模式，并提供「跟随系统 / 浅色 / 暗色」三种主题，在设置页即时切换并持久化；
- 集成 GitHub 拉取请求：通过 [GitHub CLI (`gh`)](https://cli.github.com) 连接远端，在「拉取请求」页登录 GitHub、浏览/预览/审阅/合并 PR；「创建 PR」会将当前分支推送并立即开启拉取请求，把直接 Push 降级为备用选项（点击时会推荐改用 PR 流程）；仓库所有者可在开启 PR 后选择立即以 squash 方式合并。

Push 现在作为备用选项：点击「Push（备用）」会先弹窗推荐使用 PR 流程。创建 PR 依赖 `gh` CLI，请先安装并登录（在「拉取请求」页点击「登录 GitHub」）。

“保存后自动同步到源文件”默认关闭，偏好仅保存在本机。关闭时，“保存 `lyrics.metadata`”不会修改歌词文件；需要点击“同步到文件”。手动同步会先保存当前表单，再只写回当前曲目实体关联的 TTML/LRCN。建议同步前通过版本管理页检查差异。

运行测试：

```powershell
python -m unittest discover -s tools/lyrics_manager/tests -v
```

### TTML 一键格式化脚本

`tools/lyrics_manager/ttml_formatter.py` 扫描仓库中**压缩态（单行、未格式化）**的 TTML 文件，把不规范的逐节拍发音/音节 span 转为更标准的形式，并生成可读的多行 `-Format.ttml`：

```powershell
# 文件路径方式，可从任意目录运行；不带根目录时自动定位本仓库的 Lyrics
python tools/lyrics_manager/ttml_formatter.py                        # 处理全部压缩 TTML
python tools/lyrics_manager/ttml_formatter.py --list                 # 仅列出候选文件
python tools/lyrics_manager/ttml_formatter.py D:\Github\MiaowCham-Lyrics-DB\Lyrics
# 也可在仓库根目录用模块方式：
python -m tools.lyrics_manager.ttml_formatter --list
```

处理规则：

1. 普通发音 span 末尾补 `xmlns="http://www.w3.org/ns/ttml"`（如 `<span begin="36.626" end="37.988" xmlns="...">mo</span>`）。
2. 带 `ttm:role="x-bg"` 的发音 span 规范为外层 `<span xmlns:ttm="...#metadata" ttm:role="x-bg" xmlns="...">` 包裹内层纯 span。
3. 音节内置的空格移动到音节外（也应用到逐节拍发音）。
4. 发音语言为 ja-Latn / zh-Latn-pinyin / zh-Latn-jyutping / ko-Latn，且整个发音内无内置或音节间空格时，在所有音节间补空格；若发音音节与原文音节字母一致（忽略符号与空格），则按原文音节是否带空格决定。
5. 其他发音语言按原文音节是否带空格决定。

脚本还会把**旧式 TTML**（翻译/音译以 `x-translation`/`x-transliteration`/`x-roman` 附属形式夹杂在 `body` 内）迁移到 `head` 的 iTunesMetadata 容器（含背景 `x-bg` 行，生成 `L<n>:bgN`）。

写入策略：内容变化时先把原文件备份为 `.bak`，原位写回规范化的单行版；无论是否变化都会生成同名 `-Format.ttml`（若已存在，先备份其 `.bak`）。脚本只用 Python 标准库，不改动歌词正文与未知字段。

`pages` 分支包含静态 GitHub Pages 搜索页面及公开索引快照。仓库管理员仍需在 GitHub 的 Pages 设置中选择 `pages` 分支根目录作为发布源。
页面快照可用 `python tools/export_pages.py <输出目录> --source-commit <main 提交>` 重新生成；公开数据只含搜索字段、相对路径和固定提交链接，不包含歌词全文或本地路径。

## Apple Syllable 文件命名规则

符合 Apple Syllable 结构的文件使用命名空间风格：

```text
<类型>.<曲目>[-<扩展>].<格式>
```

- TTML 使用 `applettml`，例如 `applettml.曲目.ttml`、`applettml.曲目-Format.ttml`。
- JSON 传输版本使用 `applejson`，例如 `applejson.曲目.json`。
- AMLL TTML 等其他来源使用对应类型名，例如 `amllttml.曲目-Tutorial.ttml`。
- `Format`、`LbVer`、`noEnPron`、`Tutorial` 等版本信息放在扩展段，多个扩展用连字符连接。

## 格式说明

- `applettml.*.ttml`：符合 Apple Syllable 结构的 TTML；原始版本可被 AMLL 解析。
- `applejson.*.json`：Apple Music 传输结构的 JSON 包装版本。
- `.lys`、`.lrc`、`.qrc`：Lyricify 及其他播放器使用的歌词格式。
- `.ass`：使用 Aegisub 制作或导出的字幕/歌词工程。
- `.lrcn`：一种基于 LRC 的按节拍划分歌词格式，[说明文档](https://docs.miaowcham.com/docs/Lyrics_Next/v2.3.html)

Apple Syllable 允许使用两个以上的人声 ID，不同平台的对唱视图可能呈现不同。本仓库只保证歌词内容与时间轴的准确性，不保证各播放器的对唱布局一致。

## 转换教程

- [如何将 AMLL TTML 转换为 Apple Syllable（简体中文）](https://docs.miaowcham.com/docs/lyric/How-to-Convert.html)
- [How to Convert AMLL TTML to Apple Syllable (English)](https://docs.miaowcham.com/docs/lyric/How-to-Convert-EN.html)

## 版权声明与许可

由喵锵（[@MiaowCham](https://github.com/MiaowCham)）和其他贡献者整理制作的按节拍划分的歌词文件依根目录 [LICENSE](LICENSE) 使用 CC BY-NC-SA 4.0 许可协议。
（不包含歌词内容）允许在署名（逐字歌词制作者）、非商业和相同许可协议下进行共享和改编。

著作权主张是为了更好的维护项目的运行与发展。本项目额外允许在条件不允许的情况下\*不展示“逐字歌词制作者”的署名。具体著作权认定根据当地法律法规执行。

歌词内容（包含翻译）著作权归原作者所有。若（著作权人）认为本仓库涉嫌侵犯了您的著作权，请（著作权人或著作权人指定的代理方）联系仓库所有者进行协商和删除。

`tools/` 管理器代码、Pages 页面代码及其测试使用 [Apache License 2.0](LICENSE-APACHE)。Apache-2.0 不适用于歌词数据、歌词文件或歌词内容。


\* 条件不允许：类似滚动歌词页面、项目整体布局不允许直接展示逐字歌词制作者的情况下，且在**不修改文件主体内容**（包含歌词、时间戳、歌词正文的样式结构）的前提下，视为条件不允许的署名例外。