# -*- coding: utf-8 -*-
"""只下关键帧抽图：把抽帧流量从 2.58GB 降到 ~62MB。

原理：
  H.264 的 IDR 关键帧可独立解码。ENCODER 每约 1 秒一个关键帧（6604 个 / 6600s），
  抽帧只需 441 个采样点（每 15s），对应 441 个关键帧 ≈ 62MB（占全片 2.4%）。

  解码要点：MP4 里的关键帧是 AVCC 格式（4 字节长度前缀 + NAL），且 SPS/PPS 存在
  avcC box 里而非码流中。要单独解码一帧，必须：
    1. 从 avcC 取出 SPS / PPS
    2. 把 AVCC 转成 Annex B（00 00 00 01 + NAL）
    3. 拼成 [SPS][PPS][IDR] 交给 ffmpeg
"""
from __future__ import annotations

import asyncio
import struct
import subprocess
import time
from pathlib import Path

from coursereview.paths import FFMPEG
import audio_extract as AE


def parse_video_full(head: bytes):
    """解析视频轨：返回 (samples[(off,size)], keyframe_indices_1based, sps_list, pps_list)"""
    moov = None
    for typ, ps, pe in AE._children(head, 0, len(head)):
        if typ == "moov":
            moov = (ps, pe)
    if not moov:
        raise RuntimeError("无 moov")
    video = None
    for typ, t_ps, t_pe in AE._children(head, moov[0], moov[1]):
        if typ != "trak":
            continue
        hdlr = AE._find(head, ["mdia", "hdlr"], t_ps, t_pe)
        if hdlr and head[hdlr[0] + 8:hdlr[0] + 12].decode("latin1") == "vide":
            video = (t_ps, t_pe)
            break
    if not video:
        raise RuntimeError("无视频轨")

    stsz = AE._find(head, ["mdia", "minf", "stbl", "stsz"], *video)
    stco = AE._find(head, ["mdia", "minf", "stbl", "stco"], *video)
    co64 = AE._find(head, ["mdia", "minf", "stbl", "co64"], *video)
    stsc = AE._find(head, ["mdia", "minf", "stbl", "stsc"], *video)
    stss = AE._find(head, ["mdia", "minf", "stbl", "stss"], *video)
    stsd = AE._find(head, ["mdia", "minf", "stbl", "stsd"], *video)

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

    samples, si = [], 0
    for ci, coff in enumerate(coffs):
        k = per_of(ci + 1)
        off = coff
        for _ in range(k):
            if si >= len(sizes):
                break
            samples.append((off, sizes[si]))
            off += sizes[si]
            si += 1

    keys = []
    if stss:
        n = struct.unpack(">I", head[stss[0] + 4:stss[0] + 8])[0]
        keys = [struct.unpack(">I", head[stss[0] + 8 + 4 * i:stss[0] + 12 + 4 * i])[0]
                for i in range(n)]

    # avcC → SPS/PPS
    # 注意：avc1 是 VisualSampleEntry，payload 前 78 字节是固定字段（宽高/分辨率/depth 等），
    # 子 box（avcC/pasp）从 +78 开始。从 s_ps 直接遍历会把固定字段误当 box header。
    sps_list, pps_list = [], []
    if stsd:
        for typ, s_ps, s_pe in AE._children(head, stsd[0], stsd[1]):
            if typ in ("avc1", "avc3", "encv"):
                start = s_ps + 78
                avcc = AE._find(head, ["avcC"], start, s_pe)
                if avcc:
                    b = head[avcc[0]:avcc[1]]
                    i = 5  # version(1)+profile(1)+compat(1)+level(1)+lengthSizeMinusOne(1)
                    nsps = b[i] & 0x1F
                    i += 1
                    for _ in range(nsps):
                        ln = struct.unpack(">H", b[i:i + 2])[0]
                        i += 2
                        sps_list.append(b[i:i + ln])
                        i += ln
                    npps = b[i]
                    i += 1
                    for _ in range(npps):
                        ln = struct.unpack(">H", b[i:i + 2])[0]
                        i += 2
                        pps_list.append(b[i:i + ln])
                        i += ln
                break
    return samples, keys, sps_list, pps_list


def avcc_to_annexb(data: bytes) -> bytes:
    """AVCC（4 字节长度前缀）转 Annex B（00 00 00 01 前缀）。"""
    out = bytearray()
    i = 0
    while i + 4 <= len(data):
        ln = struct.unpack(">I", data[i:i + 4])[0]
        i += 4
        if ln <= 0 or i + ln > len(data):
            break
        out += b"\x00\x00\x00\x01" + data[i:i + ln]
        i += ln
    return bytes(out)


async def sample_keyframes(ctx, url: str, out_dir: Path, step_s: int = 15,
                           duration_s: float | None = None, workers: int = 16,
                           head_mb: int = 5, log=print) -> tuple[bool, str]:
    """只下关键帧并解码成 JPEG。返回 (ok, msg)。"""
    t0 = time.time()
    out_dir.mkdir(parents=True, exist_ok=True)

    n = head_mb * 1024 * 1024
    r = await ctx.request.get(url, headers={"Range": f"bytes=0-{n-1}"}, timeout=120000)
    if r.status not in (200, 206):
        return False, f"头部探测 HTTP {r.status}"
    head = await r.body()
    samples, keys, sps, pps = parse_video_full(head)
    if not keys:
        return False, "无关键帧索引"
    log(f"  视频 {len(samples)} samples / {len(keys)} 关键帧 / SPS {len(sps)} PPS {len(pps)}")

    dur = duration_s or (len(samples) / 30.0)
    fps = len(samples) / dur
    n_samp = int(dur // step_s) + 1

    # 每个采样点 → 最近的前一个关键帧
    plan = []   # (t_rep_s, key_index_1based)
    for i in range(n_samp):
        t = i * step_s
        fi = int(t * fps)
        cand = [k for k in keys if k - 1 <= fi]
        if cand:
            plan.append((t, cand[-1]))
    uniq = sorted({k for _, k in plan})
    need_bytes = sum(samples[k - 1][1] for k in uniq if 0 < k <= len(samples))
    log(f"  采样 {n_samp} → 需 {len(uniq)} 个关键帧 ≈ {need_bytes/1e6:.1f}MB")

    sem = asyncio.Semaphore(workers)
    data_map: dict[int, bytes] = {}

    async def one(k: int):
        async with sem:
            off, sz = samples[k - 1]
            for attempt in range(3):
                try:
                    rr = await ctx.request.get(
                        url, headers={"Range": f"bytes={off}-{off+sz-1}"}, timeout=60000)
                    if rr.status in (200, 206):
                        data_map[k] = await rr.body()
                        return
                except Exception:
                    pass
                await asyncio.sleep(0.5 + attempt)

    await asyncio.gather(*[one(k) for k in uniq])
    log(f"  下载 {len(data_map)}/{len(uniq)} 关键帧，{time.time()-t0:.0f}s")

    # 逐个解码成 JPEG（带**像素去重**）
    # 用同一个临时文件名反复覆盖，避免 441 个临时文件触发沙箱「批量删除 >50」拦截。
    # 去重：与上一保留帧灰度差 >25 的像素占比 <0.8% 视为同一页，跳过。
    #   实测 441 个采样点去重后约 79 页（PPT 翻页频率远低于 15s），
    #   不然后续 OCR 要对 441 页做识别，慢 5 倍。
    import cv2
    prefix = b"".join(b"\x00\x00\x00\x01" + s for s in sps)
    prefix += b"".join(b"\x00\x00\x00\x01" + p for p in pps)
    recs, idx = [], 0
    tmp_h = out_dir.parent / "_one.h264"
    tmp_j = out_dir.parent / "_one.jpg"
    prev_gray = None
    n_decoded = 0
    for t_rep, k in plan:
        blob = data_map.get(k)
        if not blob:
            continue
        tmp_h.write_bytes(prefix + avcc_to_annexb(blob))
        pr = subprocess.run(
            [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
             "-f", "h264", "-i", str(tmp_h), "-frames:v", "1", "-q:v", "2", str(tmp_j)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        if pr.returncode != 0 or not tmp_j.exists() or tmp_j.stat().st_size < 1000:
            continue
        im = cv2.imread(str(tmp_j))
        if im is None:
            continue
        n_decoded += 1
        gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
        if prev_gray is None:
            keep = True
        else:
            gb = gray
            if prev_gray.shape != gb.shape:
                gb = cv2.resize(gb, (prev_gray.shape[1], prev_gray.shape[0]))
            d = cv2.absdiff(prev_gray, gb)
            keep = (d > 25).sum() / d.size * 100.0 >= 0.8
        if keep:
            idx += 1
            jpg = out_dir / f"slide_{idx:03d}.jpg"
            import shutil as _sh
            _sh.copyfile(tmp_j, jpg)
            recs.append({"index": idx, "t_rep_s": float(t_rep),
                         "t_rep_hms": f"{int(t_rep)//3600:02d}:{(int(t_rep)%3600)//60:02d}:{int(t_rep)%60:02d}",
                         "image": str(jpg), "image_rel": f"slides/{jpg.name}",
                         "image_kb": round(jpg.stat().st_size / 1024, 1)})
        prev_gray = gray
    for f in (tmp_h, tmp_j):
        try:
            f.unlink()
        except OSError:
            pass
    import json
    (out_dir / "slides.json").write_text(json.dumps({
        "source": "keyframe_only", "sample_step_s": step_s, "n_samples": n_samp,
        "n_slides": len(recs), "slides": recs}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return True, (f"{len(recs)} 页 / 下载 {sum(len(v) for v in data_map.values())/1e6:.1f}MB / "
                  f"{time.time()-t0:.0f}s")
