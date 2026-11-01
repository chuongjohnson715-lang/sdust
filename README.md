<p align="center">
  简体中文 · <a href="./README_EN.md">English</a>
</p>

<!-- README_SYNC: source=working-tree; updated=2026-11-01 -->

# 录播复盘：把一门课的录播，变成能倒查原话的复习材料

面向自己整理期末复习的大学生：只从课程录播里取**音频 chunk 和关键帧**（单节约 96 MB），
自动转写、识别课件，产出带证据编号的考情、知识点、知识体系与考试重点页。
每一条「老师说要考」都能回到原话和页码；没说的，就写「待确认」。

如果这个流程能帮你少花几个晚上翻录播，[点一个 Star](https://github.com/chuongjohnson715-lang/study-github/stargazers)
方便回来，也让更多同样被录播困住的人找到它。

<p align="center">
  <a href="https://github.com/chuongjohnson715-lang/study-github/stargazers">
    <img src="https://img.shields.io/github/stars/chuongjohnson715-lang/study-github?style=flat-square&label=Stars" alt="Stars">
  </a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="https://ffmpeg.org/"><img src="https://img.shields.io/badge/ffmpeg-8.x-007808?style=flat-square" alt="ffmpeg 8.x"></a>
  <a href="./docs/03_环境与依赖.md"><img src="https://img.shields.io/badge/Platform-Windows-lightgrey?style=flat-square" alt="Platform: Windows"></a>
</p>

## 它解决什么问题

期末复习时，课程录播通常是一堆 4 GB 的整包视频：拖进度条找「老师是不是说过这个要考」，
边看边记，记完又找不到原话。转写工具能给文字，但不给页码；课件截图能给画面，但不知道对应哪段讲解。

这个项目把这条链路拆开，只做必要的事：

| 手工方式 | 这个项目 |
| --- | --- |
| 下载整节 4 GB+ 视频 | 只取音频与关键帧，作者在一门课上实测单节约 96 MB |
| 边看边记，结论与原文脱节 | 正文每条结论带 `〔A-007 · P.012 · 08:30〕`，可回查 |
| 转写没有时间戳，无法定位 | 只给 30 秒块级时间，并明确声明「不代表句子起点」 |
| 凭印象判断「这个应该考」 | 只按老师原话分档：必考 / 必记 / 重点 / 了解 / 教材补 / 不考 / 待确认 |
| 每次整理格式都不一样 | 五件套 + 课程级文档的固定契约，交付前跑机检 |

它不是一键 SaaS：取数层是按某校教学平台的播放器接口写的，换平台要改登录、发现与配置接口（见下文）。

## 它产出什么

每门课最终得到这样一棵目录（`courses/` 默认不入库）：

```text
courses/<课程名>/
├── 期末考试重点与考情分析.docx
├── 全课程知识体系与思维导图.docx
├── 全课程思维导图.html
├── 考试重点.html
├── 每节课的简短总结/
│   ├── 00_使用说明.docx
│   └── MM-DD_第X讲-<章或主题>/
│       ├── 02_逐节课知识点与课堂内容记录.md
│       └── 04_证据文件夹/
└── 原样/<课程名>/MM-DD_第X讲-<章或主题>/
    ├── 00_使用说明.md
    ├── 01_期末考试重点与考情分析.md
    ├── 02_逐节课知识点与课堂内容记录.md
    ├── 03_全课程知识体系与思维导图.md
    └── 04_证据文件夹/
```

`04_证据文件夹/` 按 A（课件 OCR）/ B（课堂转写）/ C（课程要求）/ D（待回听原话）分册，
精修只改 00–03，04 必须逐字节保持原样。

## 五个关键选择

1. **按字节取，不下载整包**：解析 MP4 的 `moov`，对音频 chunk 与关键帧逐个发 HTTP Range；
   中间的视频数据一个字节都不下。视频只取 I 帧，转成 Annex B 后单帧解码。
2. **直链现取现下**：平台直链是 `?ts=&token=` 的时效签名。代码里有新鲜度守卫，
   超过 30 分钟就拒绝复用，必须重新取配置；403 也会触发重取。
3. **抽帧内存常数级**：只保留前一帧灰度图做像素差分，不把全片帧塞进列表。
   全片累积会到 GB 级，这是最常见的 OOM 原因。
4. **证据优先于结论**：OCR 原样保存，不做「修正」；转写只给块级时间；
   查不到就留空并标「待核实」，不拿常识补写。
5. **格式契约 + 机检**：五件套、课程级文档、脑图和考试重点页都有固定结构；
   `check_refined.py` 检查悬空引用、越界页码、占位符和 04 一致性，问题不为 0 就不算交付。

## 快速开始

前置：Python 3.10+、`ffmpeg` / `ffprobe`、本地 SenseVoiceSmall 与 EasyOCR 中文模型。
Word 转 `.docx` 依赖 Windows 上的 Microsoft Word。

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

```bash
mkdir -p .secrets && cp config.example.json .secrets/config.json
```

填好账号、密码、三个平台入口和课程关键字。`.secrets/` 已在 `.gitignore` 中。

```bash
python run_course.py login
```

首次遇到验证码时人工输入一次，之后复用浏览器 profile；程序不识别、不绕过验证码。

```bash
python scripts/run_lesson_browser.py \
  --activity <activity_id> --date <YYYY-MM-DD> \
  --course "<course name>" --course-id <course_id> \
  --deliver-root ./_work/deliver
```

```bash
python scripts/stage_originals.py --deliver ./_work/deliver \
  --course "<course name>" --init-map --map map.csv
python scripts/stage_originals.py --deliver ./_work/deliver \
  --course "<course name>" --map map.csv --out ./courses --apply
```

`map.csv` 需要补两列：讲次（数字）和第三段主题（可确证的章号或主题，不猜章号）。

```bash
python scripts/check_refined.py "<course name>"
```

精修 00–03 后跑这一条，输出「问题 0」才算通过。

## 工作流

```text
播放页
  ├─ 音频 chunk（Range）   ≈34 MB/节
  └─ 关键帧（I 帧）        ≈62 MB/节
        ↓
12 阶段管线（状态机 + 断点恢复）
  probe → audio → asr → slides → ocr → notes → verify
  + tracks_audio → tracks_asr → tracks_slides → tracks_ocr → tracks_align
        ↓
语义层：证据包 → 知识点 → 考情判定
        ↓
交付层：五件套 + 证据夹 + 脑图 HTML + 考试重点 HTML
```

管线写在 [scripts/pipeline.py](scripts/pipeline.py)：每个阶段有输入指纹，
中断后重跑会跳过已完成阶段；轻量路线用 `--only` 跳过双轨的 5 个阶段。
挑音轨按实测 RMS，不按文件名；时间关联只做时间窗匹配，不假装精确对齐。

## 配置与平台适配

所有真实地址都是占位符，需要你自己填：

- `config.example.json` 里的 `lms_base` / `portal_url` / `cas_login`；
- 环境变量：`COURSE_REVIEW_PYTHON`、`COURSE_REVIEW_FFMPEG`、`COURSE_REVIEW_SENSEVOICE`、
  `COURSE_REVIEW_WORK_ROOT`、`COURSE_REVIEW_OUT`、`COURSE_REVIEW_PROFILE`、`COURSE_REVIEW_BYPASS_PROXY`、
  `COURSE_REVIEW_PLAYER_BASE`（播放器所在域名，默认 `http://lms.example.edu:8002`）；
- 换学校时，只需要改取数层：
  [coursereview/discover.py](coursereview/discover.py)、[coursereview/fetch.py](coursereview/fetch.py)、
  [scripts/captures_api.py](scripts/captures_api.py)、[scripts/fetch_tracks_v2.py](scripts/fetch_tracks_v2.py)、
  [scripts/run_lesson_browser.py](scripts/run_lesson_browser.py)、[scripts/light_common.py](scripts/light_common.py)。
  证据层、语义层、交付层与平台无关。

## 边界与合规

- **只读**：账号只用于看课与播放，不提交、不发帖、不改资料。
- **不绕过访问控制**：不识别验证码、不做请求重放；风控升级就回退人工。
- **不上传**：音视频、转写和课件截图只留在本机，模型推理离线运行。
- **会缺东西**：轻量路线的时间分辨率默认 15 秒；没有整段视频，复查不了板书过程和动画中间帧。
- **平台会变**：取配置用的是平台内部接口，改版后取数层要跟着改。
- **只测过一门课**：96 MB 这组数字来自作者的单门课实测，不是所有平台的通用承诺。

## 已经做到的验证

- 全仓库 Python 文件 `py_compile` 通过；
- `scripts/test_light_common.py` 在 2026-11-01 运行：9 项 PASS，1 项 SKIP
  （样例存储节点不可达，属预期）；
- README 与文档的相对链接检查无悬空；
- 没有 CI、没有端到端演示数据：完整跑通需要你自己的账号、课程与本地模型。

## 常见问题

**下载全 403？**
不是请求头问题，是签名过期。重新取配置拿新直链，并确认请求走的是 Playwright
`context.request`；ffmpeg 或 urllib 直连会被 WAF 按 TLS 指纹拦。

**能换学校吗？**
能，但要改上文列出的六个取数文件。登录流程与播放器配置接口是平台相关的。

**为什么还要人工精修？**
规则法生成的 00–03 会有占位语、判定矛盾、模块绑定丢失等问题，
清单和四条精修标准写在 [docs/02_证据与判定.md](docs/02_证据与判定.md)。

**为什么转写没有精确时间？**
本地 SenseVoice 是非自回归模型，不输出时间戳。项目只给 30 秒块级时间，并明确标注，
不会伪造句级时刻。

## 仓库结构

| 路径 | 内容 |
| --- | --- |
| [AGENTS.md](AGENTS.md) | 工作流总纲：铁律、流程、五件套契约、自检清单、脚本索引 |
| [run_course.py](run_course.py) | 端到端编排入口 |
| [coursereview/](coursereview/config.py) | 配置、浏览器、课程发现、下载、编排 |
| [scripts/](scripts/pipeline.py) | 取数、12 阶段管线、语义、交付、归档、机检 |
| [templates/](templates/new-course.ps1) | 五件套骨架、脑图模板、考试重点 HTML 模板 |
| [docs/00_工作流总览.md](docs/00_工作流总览.md) | 数据流、A/B 路线、产物树与验收表 |
| [docs/01_取数方案.md](docs/01_取数方案.md) | 为什么按字节取、关键帧怎么挑、403 从哪来 |
| [docs/02_证据与判定.md](docs/02_证据与判定.md) | 四级证据、判定档位、精修标准 |
| [docs/03_环境与依赖.md](docs/03_环境与依赖.md) | 解释器、依赖、模型、环境变量与排障 |

文档以中文为主。

## 最后

如果它解决了你的实际问题，[点一个 Star](https://github.com/chuongjohnson715-lang/study-github/stargazers)；
跑不通、平台对不上、文档有坑，直接提 Issue。这个项目没有承诺固定更新节奏，
Star 只代表「这东西有人用」，不会被写成维护承诺。

## 许可证

仓库目前**没有** `LICENSE` 文件。在补充许可证之前，默认保留所有权利（All rights reserved）。
如果打算让别人复用，请先按你的意图选一个成熟协议（例如 MIT 或 Apache-2.0）再加进来，
不要直接把本仓库当开源项目分发。
