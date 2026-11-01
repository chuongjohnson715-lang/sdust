# AGENTS.md ｜ 期末复习材料工作流总纲

> 这是给 **Agent / 协作者** 的操作手册。人类入口见 [`README.md`](README.md)，
> 设计细节见 [`docs/`](docs/)。
>
> 一句话：从录播里**只取必要字节**，变成**每条结论都能倒查原文**的考前复习材料。

---

## 1. 这是什么

输入：学校课程平台的录播（双路 mp4，或只给播放页）。
输出：一门课的**五件套 + 课程级文档**，全部带证据编号。

```
录播播放页
  ├─ 音频：按 chunk 精准 Range         ≈34 MB/节
  └─ 视频：只取关键帧（I 帧）          ≈62 MB/节
        ↓
证据层：12 阶段管线（状态机 + 断点恢复）
        ↓
学习层：语义整理（evidence_pack → semantic → merge/render/validate）
        ↓
交付层：五件套（00–04）+ 脑图 HTML + 考试重点 HTML
```

**非目标**：不做题库、不做自动答题、不替代老师给的考试范围、
不把「正常讲授」包装成「考试重点」。

---

## 2. 铁律（改代码或取数前先读完这一节）

### 2.1 合规与只读

1. **平台账号只读**：只看课程、只播放录播。**不提交、不发帖、不改资料、不下载他人作品**。
2. **不绕过访问控制**：不识别验证码、不破解风控、不做请求重放。
   首次验证码由人工输入一次，之后复用持久化会话；平台升级风控就回退人工。
3. **不上传**任何课程音视频、转写文本、课件截图到外部服务。模型推理全离线。

### 2.2 取数红线

| # | 规则 | 原因 |
|---|---|---|
| 1 | **不下载整包**，只按字节 Range 取音频 chunk 与关键帧 | 整包 4.2 GB/节 → 96 MB/节，省 97.7% |
| 2 | **直链现取现下**，`tracks.json` 里的旧 URL 一律不复用 | 直链是时效签名，过期必 403 |
| 3 | 403 时**自动重取配置并重试**（≤3 次） | token 过期是常态，不是异常 |
| 4 | 必须走 **Playwright `context.request`** | ffmpeg/urllib/OpenCV 直连会被 WAF 按 TLS 指纹拦成 403 |
| 5 | 取配置前先**按域名阻断存储/媒体域** | 否则播放器自动拉预览流，每次白下 200 MB+ |
| 6 | 抽帧用**像素变化检测**（阈值默认 2%），不用场景检测 / dHash | 变化率呈双峰，阈值落在谷底；dHash 对压缩噪声过敏 |

### 2.3 内存与并发

- 抽帧**只保留前一帧灰度图**做差分；把全片帧累积进列表会到 GB 级并 OOM。
- ASR 单进程约 1.9 GB；**批量只能串行**，并发多路必 OOM。
- 临时帧文件复用同一文件名，避免批量删除触发沙箱拦截。

### 2.4 时间戳与判定诚实

- 本地 ASR 是**非自回归模型，不输出时间戳**。只能给「30 秒块级 + 块内能量切分」，
  产物必须声明「不代表句子起始时刻」。
- **禁止伪造句级时间戳**；**禁止把「正常讲授」写成「考试重点」**。
- 查不到的证据写「空缺 / 待核实」，**不用常识补写**。

### 2.5 下载前必须声明（硬规则）

任何课程媒体下载之前，先向用户说清三件事并得到确认：

1. **形式**：`轻量取字节（音频 chunk + 关键帧）` / `整包下载` / `只取音频`；
2. **流量**：单节与合计（轻量 ≈96 MB/节、整包 ≈4.2 GB/节、只取音频 ≈34 MB/节）；
3. **代价**：会缺哪些产物（如只取音频 → 无课件 OCR 证据）。

单次 ≤8 MB 的配置类请求（player-url / embed-token / captures）无需单独确认，
但要在同一条消息里注明「仅取配置，约 X MB」。

---

## 3. 目录结构

```
<仓库根>/
├── AGENTS.md              本文件（Agent 主入口）
├── README.md              人类入口
├── run_course.py          端到端编排（登录/发现/下载/管线/语义/交付）
├── config.example.json    凭据模板（真实值写 .secrets/config.json）
├── requirements.txt       依赖清单（本机验证过的组合）
├── coursereview/          包：config / browser / discover / fetch / runner / semantic_auto
├── scripts/               取数、管线、语义、交付、归档、机检（见 §8）
├── templates/
│   ├── 课程复盘五件套/     空白骨架（00–04 + 脑图 HTML）
│   └── new-course.ps1     实例化骨架（UTF-8 with BOM）
├── docs/                  01 取数 / 02 证据与判定 / 03 环境与依赖
├── .secrets/              凭据与浏览器 profile（gitignore）
├── .state/                登录态、清单、下载进度（gitignore）
├── _work/                 下载与中间产物（gitignore）
├── input/                 本地双轨 mp4 放这里（gitignore）
└── courses/               交付物根目录
```

---

## 4. 从零到一份可交接的材料

### 4.1 准备（只做一次）

```bash
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
mkdir -p .secrets && cp config.example.json .secrets/config.json    # 填账号/密码/平台入口/课程关键字
python run_course.py login                     # 人工过一次验证码（如有）
```

### 4.2 路线选择

| 路线 | 何时用 | 命令 |
|---|---|---|
| **A 双轨** | 平台允许整包下载，或本地已有双路 mp4 | `python run_course.py all` |
| **B 轻量（推荐）** | 平台拦整包 / 想省流量 | `python scripts/run_lesson_browser.py …` 逐节 |

### 4.3 路线 B（逐节）

```bash
# 1) 取数 + 管线 + 语义 + 五件套，一节一条命令
python scripts/run_lesson_browser.py \
  --activity <活动id> --date <YYYY-MM-DD> \
  --course "<课程名>" --course-id <课程id> \
  --deliver-root ./_work/deliver

# 2) 归档（默认预演；先导出映射模板，补 lecture/topic）
python scripts/stage_originals.py --deliver ./_work/deliver \
  --course "<课程名>" --init-map --map map.csv
python scripts/stage_originals.py --deliver ./_work/deliver \
  --course "<课程名>" --map map.csv --out ./courses --apply

# 3) 精修 00–03（只改这四个，04 逐字节不动）
#    精修稿落到 courses/<课程名>/每节课的简短总结/<讲次>/{02,04}

# 4) 课程级文档出 Word，再跑机检
python scripts/md_to_docx.py "<课程名>/期末考试重点与考情分析.md" ...
python scripts/check_refined.py "<课程名>"
```

### 4.4 路线 A（批量）

```bash
python run_course.py discover     # 列出全部录播活动
python run_course.py fetch        # 逐节下载双轨 mp4
python run_course.py pipeline     # 12 阶段
python run_course.py semantic     # 规则法学习层 + 自检
python run_course.py deliver      # 五件套 → ./courses/
python run_course.py status       # 看每节进度
```

> 进度以 `courses/**/_自检.json` 的 `ok` 为准，不要只信 `status` 的状态字段。

---

## 5. 12 阶段管线

| # | 阶段 | 做什么 | 关键产物 |
|---|---|---|---|
| 1 | `probe` | 探测媒体轨道、时长、分辨率、音轨 RMS | `media.json` |
| 2 | `audio` | 选音轨（按实测 RMS，不按文件名）、解码 16 kHz wav | `audio/raw_16k.wav` |
| 3 | `asr` | SenseVoice 分块转写（30 s 块，不伪造时间戳） | `asr/segments.jsonl` |
| 4 | `slides` | 采样抽帧 + 变化检测去重 + 代表帧 | `slides/slides.json` |
| 5 | `ocr` | EasyOCR 原样识别（不修正、不补写） | `slides/ocr.json` |
| 6 | `notes` | 时间窗关联「画面 ↔ 讲解」，生成课堂笔记 | `课程笔记.md` |
| 7 | `verify` | 一致性校验（引用、页码、空段） | `验证报告.md` |
| 8 | `tracks_audio` | 双轨：逐路提音频（不做跨轨合并） | `tracks/<轨>/raw_16k.wav` |
| 9 | `tracks_asr` | 双轨：逐路转写 | `tracks/<轨>/transcript.jsonl` |
| 10 | `tracks_slides` | 双轨：轨道判定（OCR 密度 + Office UI 关键词）后抽帧 | `slides_tracks/slides.json` |
| 11 | `tracks_ocr` | 双轨：逐页 OCR | `slides_tracks/ocr.json` |
| 12 | `tracks_align` | 双轨：画面 ↔ 讲解时间线 | `slides_tracks/timeline.jsonl` |

特性：

- 每个阶段有**状态机 + 输入指纹**，中断后重跑会跳过已完成阶段；
- 每阶段写 JSONL 记录（耗时、峰值内存、输入输出、状态）；
- 新增阶段只能**追加**，不要改已有阶段的输出契约；
- 轻量路线用 `--only` 跳过 8–12（没有双轨输入）。

```bash
# 60 秒回归
python scripts/pipeline.py --window 0:60 --work-root ./_work/data
python scripts/pipeline.py --window 0:60 --work-root ./_work/data --print-state
```

---

## 6. 五件套格式契约

### 6.1 文件清单

一门课（课程根）交付物：

```
courses/<课程名>/
├── 期末考试重点与考情分析.docx      课程级：题型 + 按讲次考点图
├── 全课程知识体系与思维导图.docx    课程级：章↔讲次 + 必考点明细 + 依赖图
├── 全课程思维导图.html              全课程一份，节点标注讲次
├── 考试重点.html                    全课程一份，按判定/讲次筛选
├── 每节课的简短总结/
│   ├── 00_使用说明.docx             课程级唯一一份
│   ├── MM-DD_第X讲-<章或主题>/
│   │   ├── 02_逐节课知识点与课堂内容记录.md
│   │   └── 04_证据文件夹/           ← 逐字节原样，不得改动
│   └── …
└── 原样/<课程名>/MM-DD_第X讲-<章或主题>/   ← 精修前的完整五件套（00–04 + assets）
```

逐节五件套（生成器输出）：

| 文件 | 内容 | 精修 |
|---|---|---|
| `00_使用说明` | 怎么用、文件地图、字段说明 | 可改 |
| `01_期末考试重点与考情分析` | 题型、按讲次考点、必考/不考 | 必精修 |
| `02_逐节课知识点与课堂内容记录` | 按段的知识点与原文 | 必精修 |
| `03_全课程知识体系与思维导图` | 框架、演变、依赖、对比 | 必精修 |
| `04_证据文件夹/` | A/B/C/D 四级证据 + 索引 + 页码对照 | **只读，逐字节复制** |

### 6.2 证据分册与引用

| 分册 | 来源 |
|---|---|
| `A` | 课件 OCR |
| `B` | 课堂转写 |
| `C` | 课程要求（大纲/考核/作业） |
| `D` | 课堂原话待回听 |

引用格式：`〔A-007 · P.012 · 08:30〕`（分册·编号·源页码·块级时间）。
编号必须在 `04_证据文件夹/00_证据索引.md` 查得到，页码不得超过该分册最大页。
分册文件名**禁止数字前缀**（会与顶层 `01_/02_/03_` 撞号）。

### 6.3 判定档位（只取原话支撑）

| 判定 | 含义 | 依据 |
|---|---|---|
| ⭐ 必考 | 老师对本课程明确说要考 | A 级原话 + 时间 |
| ⭐ 必记 | 「一定要记住 / 必须掌握」，未明说考 | 原话 + 时间 |
| 重点 | 强词，但语境是考研/统考 | 标「考研向」，不得写必考 |
| 了解 | 「了解一下就行」 | 原话时间 |
| 教材补 | 录播没讲，按教材补齐 | 写明「无课堂证据」 |
| ⛔ 不考 | 明确说本课程不考 | 原话时间 |
| 待确认 | 有信号但转写残缺/对象不明 | 「待回听」 |
| 非考点 | 教务事务、作业要求、复习方法 | 不参与优先级排序 |

- 老师说「要考/不考」才判必考/不考；「掌握/记住」→ ⭐ 必记；「了解」→ 了解。
- 「考研常考」「408 常考」**不是**本课程必考，最多写「重点（考研向）」。
- 没说的标「待确认」或留空标红，**不许替换成「必考」**。

### 6.4 精修四条标准

1. **01 只留真考点**：删空泛标签；准确归类；空段标红并写原因，不硬凑。
2. **02 只留知识点**：砍时间轴/段落边界/整段原话（下沉 04）；按老师原词分级。
3. **03 只收已确定**：学分/学时/教材/教师/先修收进来；补真实依赖，不按时间硬串。
4. **诚实分级**：见 §6.3；查不到保留原引用并标「待核」。

精修**只改 00–03**；`04_证据文件夹/` 原样复制并保持逐字节一致；
精修不新造引用。

### 6.5 目录命名（逐节）

`MM-DD_第X讲-<章或主题>`

- **日期在最前**：只说「第几讲」不带日期，无法判断是哪一次课；
- 讲次用中文数字：`第3讲` → `第三讲`；
- 第三段写**可确证的章号**（`第2章`），查不到就写主题，**不许猜章号**；
- 不带活动 id；`原样/` 与精修目录同名一一对应。

### 6.6 课程级文档只出 Word

| 文档 | 格式 |
|---|---|
| 期末考试重点与考情分析 | `.docx` |
| 全课程知识体系与思维导图 | `.docx` |
| 每节课的简短总结/00_使用说明 | `.docx` |

- 交付目录**不留同名 `.md` 副本**；各讲 `02` 与 `04` 仍是 `.md` 证据；
- 转换：`python scripts/md_to_docx.py <x.md>`（Windows + Word COM，不装新依赖，A4 版式）；
- 改内容后必须**重跑转换再删 md**，否则 docx 是旧版。

### 6.7 脑图与考试重点 HTML

- `全课程思维导图.html`：全课程一份，节点标注讲次，可折叠/搜索；
  骨架在 `templates/课程复盘五件套/assets/全课程思维导图.html`。
- `考试重点.html`：全课程一份，按判定与讲次筛选（红＝必考 / 灰＝不考 / 蓝＝其余）；
  骨架在 [`templates/考试重点.html`](templates/考试重点.html)，
  精修时替换其中的 `NODES` 数组（字段：`id / p / t / lvl / raw / les / pts / why / ev`）。
- 两者都不逐讲复制；改完把内联 `<script>` 抽出来过一遍 `node --check`（或 `new Function`）。

---

## 7. 交付前自检（9 条，全部为 0 才可交付）

1. 残留占位符 `{{…}}` = 0；
2. 残留说明注释 `<!--` = 0；
3. 待办标记 `TODO / TBD / 待填` = 0；
4. 悬空证据引用 = 0（正文每个编号都能在 04 索引查到）；
5. 越界页码 = 0（`P.xxx` ≤ 该分册最大页）；
6. 04 证据夹与 `原样/` 副本**逐字节一致**；
7. 「一页速记卡」残留 = 0（该形态已取消）；
8. 课程级文档仍在逐讲目录里 = 0（应只在课程根/`每节课的简短总结/`）；
9. 课程名串课、模板残留 = 0。

机检命令：`python scripts/check_refined.py <课程名>` → 输出「问题 0」。

---

## 8. 脚本索引

### 8.1 端到端

| 脚本 | 职责 |
|---|---|
| `run_course.py` | 编排入口：login / discover / fetch / pipeline / semantic / deliver / all / status |
| `scripts/run_lesson_browser.py` | **单节全流程（推荐）**：取字节 → 本地提音频/抽帧 → 管线 → 五件套 |
| `scripts/run_light_lesson.py` | 轻量产物合成 mp4 → 标准管线（被上一脚本调用） |

### 8.2 取数（路线 B）

| 脚本 | 职责 |
|---|---|
| `scripts/light_common.py` | 共享原语：音频抽取、跳读抽帧、直链新鲜度守卫 |
| `scripts/audio_chunks.py` | 按音频 chunk Range 下载（moov → stco/stsz/stsc） |
| `scripts/keyframe_frames.py` | 只下关键帧抽图（stss + AVCC→Annex B） |
| `scripts/captures_api.py` | 阻断媒体域，纯 API 取 `lesson_videos[]` |
| `scripts/fetch_tracks_v2.py` | 取播放配置的参考实现 |
| `scripts/audio_extract.py` | MP4 box 解析与单轨提取工具 |

### 8.3 管线

| 脚本 | 职责 |
|---|---|
| `scripts/pipeline.py` | 12 阶段主控 + 状态机 + 断点恢复 |
| `scripts/extract_slides.py` | 抽帧 / 裁剪 / 变化检测 / 文本去重 / 时间线 |
| `scripts/transcribe_tracks.py` | 双轨逐路提取与转写 |
| `scripts/asr_from_wav.py` | 对本地 wav 补转写（合成 mp4 被截断时用） |
| `scripts/dedupe_slides.py` | 离线像素去重（不重新下载） |
| `scripts/make_review.py` | 规则法复习材料（不调生成式模型） |
| `scripts/check_lesson_content.py` | 空课/内容量判定辅助 |

### 8.4 语义与交付

| 脚本 | 职责 |
|---|---|
| `scripts/semantic_review.py` | 学习层：evidence_pack → merge → render → validate |
| `coursereview/semantic_auto.py` | 规则法语义（无外部 API） |
| `scripts/build_deliverables.py` | 证据层 + 学习层 → 五件套（自检失败退出非零） |
| `scripts/md_to_docx.py` | Markdown → Word（A4） |
| `scripts/stage_originals.py` | 原样五件套归档（日期在最前，默认预演） |

### 8.5 机检与工具

| 脚本 | 职责 |
|---|---|
| `scripts/check_refined.py` | 精修稿机检（悬空引用/越界页码/占位/04 一致性/速记卡） |
| `scripts/lesson_brief.py` | 单讲精修速览（A 原文 + B 关键词 + 页码映射 + D 候选） |
| `scripts/test_light_common.py` | 直链新鲜度守卫自测（不需要登录） |
| `scripts/paths.py` | 外部程序与模型路径的唯一解析点 |
| `scripts/res_sampler.py` | 零依赖资源采样器 |

---

## 9. 改完必跑

```bash
python -m py_compile scripts/*.py coursereview/*.py run_course.py
python scripts/test_light_common.py
python scripts/pipeline.py --window 0:60 --work-root ./_work/data
python scripts/pipeline.py --window 0:60 --work-root ./_work/data --print-state
python scripts/check_refined.py <课程名>
```

期望：12 阶段全 `succeeded`、退出码 0；`_work/data` 下有 `课程笔记.md` 与
`slides_tracks/timeline.jsonl`；机检输出「问题 0」；
`test_light_common.py` 全部 PASS（存储节点不可达时允许 preflight 一项 SKIP）。

提交前另做敏感信息扫描：

```bash
grep -rniE "真实的学校域名|真实课程名|真实账号|本地绝对路径" .
```

命中即视为不可提交。

---

## 10. 隐私与发布

1. `.secrets/`、`.state/`、`_work/`、`input/`、`data/`、`output/`、`logs/`、`courses/`
   **默认都不入库**（`courses/` 可用 `git add -f` 显式加入）；
2. 课件 OCR 与转写含第三方（老师和同学）个人信息：**仓库必须保持私有**，
   `courses/` 目录是否入库由使用者自行决定；
3. 公开这份工作流代码时，删掉所有真实域名、课程名、活动 id、账号与本地路径；
4. 日志与异常统一过 `redact()`，密码与账号不得出现在输出中。

---

## 11. 明确不做

- ❌ 不绕过访问控制、不识别验证码、不做请求重放
- ❌ 不上传课程音视频到任何外部服务
- ❌ 不安装新依赖；外部程序一律走 `scripts/paths.py` 解析，不写死盘符
- ❌ 不引入 Web 框架 / GUI / 多 Agent 框架
- ❌ 不把推测写成结论；无证据的章节保留空缺并写明原因
- ❌ 不写「一页速记卡」；考前速记由 `考试重点.html` 承担
