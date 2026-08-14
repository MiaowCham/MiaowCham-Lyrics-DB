# MiaowCham Lyrics DB

本目录中的歌词按 `歌手/曲目/` 归档。同一曲目的不同格式、版本和附加内容放在同一个曲目目录中，便于查找和对照。

```text
lyrics raw file/
├── 歌手/
│   └── 曲目/
│       ├── applettml.曲目.ttml
│       ├── applettml.曲目-Format.ttml
│       ├── applejson.曲目.json
│       ├── 曲目.lys
│       ├── 曲目 (2).lys
│       ├── 曲目-trans.lrc
│       ├── 曲目.lrcn
│       └── 曲目.ass
├── LICENSE
└── README.md
```

## 命名规则

### Apple Syllable

Apple Syllable 文件使用命名空间风格：

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

由喵锵（[@MiaowCham](https://github.com/MiaowCham)）整理制作的按节拍划分的歌词文件使用 [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) 许可协议  
(*不包含歌词内容* *) 允许在署名和相同许可协议下进行共享和改编

\* *歌词内容版权归原作者所有*