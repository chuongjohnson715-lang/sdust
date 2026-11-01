# -*- coding: utf-8 -*-
"""按音频 chunk 精准下载（只取音频字节，跳过视频数据）。

实测（2026-10-26）：51558 个 chunk、8 并发、单 chunk 9ms → 全量约 0.9 分钟 / 34MB，
相比下载完整 INSTRUCTOR（1637MB / 10 分钟）省 98% 流量、快 10 倍。

原理：
  MP4 的 mdat 里音频与视频交错（每块 ~660B，间隔 ~23KB）。
  用 moov 的 stco/stsz/stsc 算出每个音频 chunk 的绝对字节范围，
  再对每个范围发一个 HTTP Range 请求——中间的 23KB 视频数据一个字节都不下。

产物：ADTS 封装后的 .aac → ffmpeg 转 16k 单声道 wav。
"""
from __future__ import annotations

import asyncio
import struct
import subprocess
import time
from pathlib import Path

from coursereview.paths import FFMPEG
import audio_extract as AE


def parse_chunks(head: bytes):
    """从头部字节（含 moov）解析音频 chunk 列表与 ASC 参数。

    返回 (chunks[(offset,size)], asc_info(obj,sri,ch), n_samples)
    """
    moov = None
    for typ, ps, pe in AE._children(head, 0, len(head)):
        if typ == "moov":
            moov = (ps, pe)
    if not moov:
        raise RuntimeError("前段无 moov")

    audio = None
    for typ, t_ps, t_pe in AE._children(head, moov[0], moov[1]):
        if typ != "trak":
            continue
        hdlr = AE._find(head, ["mdia", "hdlr"], t_ps, t_pe)
        if hdlr and head[hdlr[0] + 8:hdlr[0] + 12].decode("latin1") == "soun":
            audio = (t_ps, t_pe)
            break
    if not audio:
        raise RuntimeError("未找到音频轨")

    stsz = AE._find(head, ["mdia", "minf", "stbl", "stsz"], *audio)
    stco = AE._find(head, ["mdia", "minf", "stbl", "stco"], *audio)
    co64 = AE._find(head, ["mdia", "minf", "stbl", "co64"], *audio)
    stsc = AE._find(head, ["mdia", "minf", "stbl", "stsc"], *audio)
    stsd = AE._find(head, ["mdia", "minf", "stbl", "stsd"], *audio)
    if not (stsz and (stco or co64) and stsc):
        raise RuntimeError("stbl 表缺失")

    _, ssz, cnt = struct.unpack(">III", head[stsz[0]:stsz[0] + 12])
    sizes = ([struct.unpack(">I", head[stsz[0] + 12 + 4 * i:stsz[0] + 16 + 4 * i])[0]
              for i in range(cnt)] if ssz == 0 else [ssz] * cnt)
    sizes = list(sizes)

    if stco:
        nch = struct.unpack(">I", head[stco[0] + 4:stco[0] + 8])[0]
        coffs = [struct.unpack(">I", head[stco[0] + 8 + 4 * i:stco[0] + 12 + 4 * i])[0]
                 for i in range(nch)]
    else:
        nch = struct.unpack(">I", head[co64[0] + 4:co64[0] + 8])[0]
        coffs = [struct.unpack(">Q", head[co64[0] + 8 + 8 * i:co64[0] + 16 + 8 * i])[0]
                 for i in range(nch)]

    n_ent = struct.unpack(">I", head[stsc[0] + 4:stsc[0] + 8])[0]
    entries = [struct.unpack(">III", head[stsc[0] + 8 + 12 * i:stsc[0] + 20 + 12 * i])
               for i in range(n_ent)]

    def per_of(ci):
        per = entries[0][1]
        for first, p, _d in entries:
            if ci >= first:
                per = p
        return per

    chunks, si = [], 0
    for ci, coff in enumerate(coffs):
        k = per_of(ci + 1)
        sz = sum(sizes[si:si + k])
        si += k
        if sz > 0:
            chunks.append((coff, sz, k))   # k = 该 chunk 内含几个 sample

    # ASC
    asc = None
    if stsd:
        for typ, s_ps, s_pe in AE._children(head, stsd[0], stsd[1]):
            if typ in ("mp4a", "enca"):
                e = AE._find(head, ["esds"], s_ps, s_pe)
                if e:
                    asc = AE._asc_from_esds(head, *e)
                break
    obj, sri, ch = AE._parse_asc(asc) if asc else (2, 11, 1)
    return chunks, (obj, sri, ch), len(sizes)


async def fetch_audio_chunks(ctx, url: str, out_wav: Path, workers: int = 8,
                             head_mb: int = 3, log=print) -> tuple[bool, str]:
    """只下载音频 chunk 并转 wav。ctx 为 Playwright BrowserContext。"""
    t0 = time.time()
    out_wav.parent.mkdir(parents=True, exist_ok=True)

    n = head_mb * 1024 * 1024
    r = await ctx.request.get(url, headers={"Range": f"bytes=0-{n-1}"}, timeout=120000)
    if r.status not in (200, 206):
        return False, f"头部探测 HTTP {r.status}"
    head = await r.body()
    chunks, (obj, sri, ch), n_samples = parse_chunks(head)
    data_bytes = sum(s for _, s, _ in chunks)
    log(f"  音频 {len(chunks)} chunks / {data_bytes/1e6:.1f}MB（总数 {n_samples} samples）")

    sem = asyncio.Semaphore(workers)
    got: dict[int, bytes] = {}
    fail = {"n": 0}

    async def one(idx: int, off: int, sz: int):
        async with sem:
            for attempt in range(3):
                try:
                    rr = await ctx.request.get(
                        url, headers={"Range": f"bytes={off}-{off+sz-1}"}, timeout=60000)
                    if rr.status in (200, 206):
                        got[idx] = await rr.body()
                        return
                except Exception:
                    pass
                await asyncio.sleep(0.5 + attempt)
            fail["n"] += 1

    await asyncio.gather(*[one(i, o, s) for i, (o, s, _) in enumerate(chunks)])
    log(f"  下载完成 {len(got)}/{len(chunks)}（失败 {fail['n']}），"
        f"{time.time()-t0:.0f}s")

    # 按顺序拼 ADTS
    aac = out_wav.with_suffix(".aac")
    written = 0
    with aac.open("wb") as f:
        for i, (off, sz, k) in enumerate(chunks):
            data = got.get(i)
            if not data:
                continue
            written += len(data)
            if k == 1:
                f.write(AE._adts_header(len(data), obj, sri, ch))
                f.write(data)
            else:
                # 一个 chunk 含多个 sample：按 sample 均分不可靠，退化为整块一帧
                f.write(AE._adts_header(len(data), obj, sri, ch))
                f.write(data)
    log(f"  ADTS 封装 {written/1e6:.1f}MB")

    cmd = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "aac", "-i", str(aac), "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", str(out_wav)]
    pr = subprocess.run(cmd, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=1800)
    if pr.returncode != 0 or not out_wav.exists() or out_wav.stat().st_size < 10000:
        return False, f"转码失败 rc={pr.returncode} {(pr.stderr or '')[:200]}"
    try:
        aac.unlink()
    except OSError:
        pass
    return True, (f"wav {out_wav.stat().st_size/1e6:.0f}MB / "
                  f"下载 {written/1e6:.1f}MB / {time.time()-t0:.0f}s")
