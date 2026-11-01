<p align="center">
  <a href="./README.md">简体中文</a> · English
</p>

<!-- README_SYNC: source=working-tree; updated=2026-11-01 -->

# Lecture Replay → Exam Notes You Can Trace Back

A local workflow for students who review their own recorded lectures: fetch only the audio chunks and
keyframes (about 96 MB per lecture in the author's test), transcribe and OCR them, then produce exam
notes where every "the instructor said this will be on the test" claim points back to a timestamp,
slide, and evidence ID. Anything not backed by a quote stays marked as unconfirmed.

If this saves you a few evenings of scrubbing through 4 GB videos,
[give it a star](https://github.com/chuongjohnson715-lang/study-github/stargazers) so you can find it
again — and so the next student with the same problem can find it too.

<p align="center">
  <a href="https://github.com/chuongjohnson715-lang/study-github/stargazers">
    <img src="https://img.shields.io/github/stars/chuongjohnson715-lang/study-github?style=flat-square&label=Stars" alt="Stars">
  </a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="https://ffmpeg.org/"><img src="https://img.shields.io/badge/ffmpeg-8.x-007808?style=flat-square" alt="ffmpeg 8.x"></a>
  <a href="./docs/03_环境与依赖.md"><img src="https://img.shields.io/badge/Platform-Windows-lightgrey?style=flat-square" alt="Platform: Windows"></a>
</p>

## The Problem

Exam week usually means a pile of multi-gigabyte lecture videos. You drag the progress bar looking for
"did the instructor say this would be tested?", take notes while watching, and later cannot find the
original sentence. Transcription tools give you text without slide numbers; screenshots give you slides
without knowing which explanation they belong to.

This project splits that chain and only does what is necessary:

| Manual workflow | This project |
| --- | --- |
| Download a 4 GB+ lecture | Fetch only audio and keyframes; about 96 MB per lecture in the author's test |
| Notes drift away from the source | Every claim carries `〔A-007 · P.012 · 08:30〕` and can be traced back |
| Transcription has no timestamps | Only 30-second block times, explicitly marked as non-sentence-level |
| "This feels like it will be tested" | Judgement follows the instructor's words: must-test / must-remember / key / aware / textbook-only / not-tested / unconfirmed |
| Every review looks different | A fixed five-document contract plus a pre-delivery checker |

It is not a one-click SaaS. The fetching layer targets one university LMS player API; switching
platforms means adapting login, discovery, and capture endpoints (see below).

## What It Produces

Each course ends up with a tree like this (`courses/` is git-ignored by default):

```text
courses/<course name>/
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

`04_证据文件夹/` is split into A (slide OCR), B (lecture transcript), C (course requirements), and
D (quotes to re-listen). Refinement only touches 00–03; 04 must stay byte-for-byte identical.

## Five Design Choices

1. **Byte ranges, not whole files.** Parse the MP4 `moov` box, then issue HTTP Range requests for
   audio chunks and keyframes only. Video fetches just I-frames, converted to Annex B for single-frame
   decoding.
2. **Fetch the signed URL right before use.** Platform links carry `?ts=&token=` and expire. A freshness
   guard rejects anything older than 30 minutes, and a 403 triggers a fresh capture request.
3. **Constant-memory frame sampling.** Only the previous grayscale frame is kept for pixel diffing.
   Accumulating every frame reaches gigabytes and is the usual cause of OOM.
4. **Evidence before conclusions.** OCR is stored as-is, with no "corrections"; transcripts carry only
   block-level time; missing evidence stays missing and is marked for verification.
5. **A format contract plus a checker.** The five documents, course-level docs, mind map, and exam page
   all have fixed structures. `check_refined.py` checks dangling references, out-of-range page numbers,
   placeholders, and 04 consistency; a non-zero problem count blocks delivery.

## Quick Start

Requirements: Python 3.10+, `ffmpeg` / `ffprobe`, local SenseVoiceSmall and EasyOCR Chinese models.
Converting to `.docx` relies on Microsoft Word on Windows.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

```bash
mkdir -p .secrets && cp config.example.json .secrets/config.json
```

Fill in the account, password, three platform endpoints, and the course keyword. `.secrets/` is
`.gitignore`d.

```bash
python run_course.py login
```

If a CAPTCHA appears, solve it manually once; later runs reuse the browser profile. The tool never
recognizes or bypasses CAPTCHAs.

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

`map.csv` needs two columns filled in: the lecture number and a confirmed chapter or topic. Do not
guess chapter numbers.

```bash
python scripts/check_refined.py "<course name>"
```

Run this after refining 00–03. It must print zero problems.

## Workflow

```text
Player page
  ├─ audio chunks (Range)   ≈34 MB per lecture
  └─ keyframes (I-frames)   ≈62 MB per lecture
        ↓
12-stage pipeline (state machine + resume)
  probe → audio → asr → slides → ocr → notes → verify
  + tracks_audio → tracks_asr → tracks_slides → tracks_ocr → tracks_align
        ↓
Semantic layer: evidence pack → knowledge points → exam judgement
        ↓
Delivery: five documents + evidence folder + mind map HTML + exam HTML
```

The pipeline lives in [scripts/pipeline.py](scripts/pipeline.py). Each stage stores an input
fingerprint, so a rerun skips completed work; the lightweight route uses `--only` to skip the five
dual-track stages. Audio-track selection uses measured RMS, not filenames. Slide-to-speech alignment
is a time-window match, not pretend frame-accurate alignment.

## Configuration and Platform Adaptation

All real endpoints are placeholders you must fill in:

- `lms_base` / `portal_url` / `cas_login` in `config.example.json`;
- environment variables: `COURSE_REVIEW_PYTHON`, `COURSE_REVIEW_FFMPEG`, `COURSE_REVIEW_SENSEVOICE`,
  `COURSE_REVIEW_WORK_ROOT`, `COURSE_REVIEW_OUT`, `COURSE_REVIEW_PROFILE`, `COURSE_REVIEW_BYPASS_PROXY`,
  `COURSE_REVIEW_PLAYER_BASE` (player origin, default `http://lms.example.edu:8002`);
- to switch schools, only the fetching layer changes:
  [coursereview/discover.py](coursereview/discover.py), [coursereview/fetch.py](coursereview/fetch.py),
  [scripts/captures_api.py](scripts/captures_api.py), [scripts/fetch_tracks_v2.py](scripts/fetch_tracks_v2.py),
  [scripts/run_lesson_browser.py](scripts/run_lesson_browser.py), [scripts/light_common.py](scripts/light_common.py).
  The evidence, semantic, and delivery layers are platform-independent.

## Limits and Compliance

- **Read-only**: the account is used to view and play lectures. No submissions, posts, or profile edits.
- **No access-control bypass**: no CAPTCHA solving, no request replay; if risk control tightens, fall
  back to manual login.
- **No uploads**: video, transcripts, and slide images stay local. Model inference runs offline.
- **Known loss**: the lightweight route samples every 15 seconds by default; without the full video,
  you cannot review handwriting process or intermediate animation frames.
- **Platform drift**: capture uses internal endpoints; a platform update requires fetching-layer changes.
- **One measured course**: the 96 MB figure comes from one course in the author's environment, not a
  universal promise.

## What Has Been Verified

- Every Python file passes `py_compile`;
- `scripts/test_light_common.py` ran on 2026-11-01: 9 PASS, 1 SKIP (the sample storage node was
  unreachable, as expected);
- Relative links in the README and docs resolve;
- No CI and no end-to-end fixture: a full run needs your own account, course, and local models.

## FAQ

**Everything returns 403?**
It is an expired signature, not a missing header. Re-capture the configuration for a fresh URL and make
sure requests go through Playwright `context.request`. Direct ffmpeg or urllib requests are blocked by
the WAF on TLS fingerprint.

**Can I use it for another university?**
Yes, but you must adapt the six fetching files listed above. Login flow and player capture endpoints
are platform-specific.

**Why is manual refinement still required?**
Rule-generated 00–03 contain placeholder phrases, contradictory judgements, and lost module bindings.
The checklist and four refinement standards are in [docs/02_证据与判定.md](docs/02_证据与判定.md)
(Chinese).

**Why no precise transcript timestamps?**
The local SenseVoice model is non-autoregressive and emits no timestamps. The project exposes
30-second block times and labels them honestly instead of inventing sentence-level moments.

## Repository Layout

| Path | Contents |
| --- | --- |
| [AGENTS.md](AGENTS.md) | Workflow charter: rules, pipeline, document contract, checklist, script index |
| [run_course.py](run_course.py) | End-to-end orchestration entry point |
| [coursereview/](coursereview/config.py) | Config, browser, course discovery, download, orchestration |
| [scripts/](scripts/pipeline.py) | Fetching, 12-stage pipeline, semantics, delivery, staging, checks |
| [templates/](templates/new-course.ps1) | Five-document skeleton, mind-map template, exam HTML template |
| [docs/00_工作流总览.md](docs/00_工作流总览.md) | Data flow, A/B routes, output tree, acceptance table |
| [docs/01_取数方案.md](docs/01_取数方案.md) | Why byte-range fetching, how keyframes are chosen, where 403 comes from |
| [docs/02_证据与判定.md](docs/02_证据与判定.md) | Four evidence books, judgement levels, refinement standards |
| [docs/03_环境与依赖.md](docs/03_环境与依赖.md) | Interpreter, dependencies, models, environment variables, troubleshooting |

Documentation is primarily in Chinese.

## Final Note

If this solves a real problem for you,
[give it a star](https://github.com/chuongjohnson715-lang/study-github/stargazers). If a platform
changed, a command fails, or the docs are wrong, open an issue. The project makes no fixed release
promise; a star means someone finds it useful, not that maintenance is guaranteed.

## License

There is currently **no** `LICENSE` file. Until one is added, all rights are reserved. If you intend to
let others reuse it, choose a mature license that matches your intent (for example MIT or Apache-2.0)
and add it first rather than distributing this repository as if it were open source.
