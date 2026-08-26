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
- 通过类似 GitHub Desktop 的版本页查看变更、差异和历史，以及暂存、取消暂存、提交、Fetch、仅快进拉取和推送；进入版本管理页会自动刷新状态，Git 操作在后台串行执行，Windows 不显示终端窗口。

“保存后自动同步到源文件”默认关闭，偏好仅保存在本机。关闭时，“保存 `lyrics.metadata`”不会修改歌词文件；需要点击“同步到文件”。手动同步会先保存当前表单，再只写回当前曲目实体关联的 TTML/LRCN。建议同步前通过版本管理页检查差异。

运行测试：

```powershell
python -m unittest discover -s tools/lyrics_manager/tests -v
```

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

由喵锵（[@MiaowCham](https://github.com/MiaowCham)）整理制作的按节拍划分的歌词文件依根目录 [LICENSE](LICENSE) 使用 CC BY-NC-SA 4.0 许可协议。
（不包含歌词内容）允许在署名、非商业和相同许可协议下进行共享和改编。

歌词内容版权归原作者所有。

`tools/` 管理器代码、Pages 页面代码及其测试使用 [Apache License 2.0](LICENSE-APACHE)。Apache-2.0 不适用于歌词数据、歌词文件或歌词内容。
