# -*- coding: utf-8 -*-
"""只下载 MP4 的音频轨（Range 精准提取），绕开 WAF 的 TLS 指纹拦截。

为什么需要：
  平台 WAF 按 TLS 指纹拦截非浏览器客户端 → ffmpeg / urllib 直连直链一律 403，
  只有浏览器网络栈（Playwright context.request）能拿到 206。
  但完整 INSTRUCTOR 是 1.64GB/节（46 节 = 75GB），不可接受。
  实测 moov 在文件头 1.5MB 处，音频轨只占 34.2MB（2.1%）→ 只下音频。

流程：
  1. Range 下载头部 3MB（含 ftyp + moov）
  2. 解析 moov → 音频 trak → stbl 的 stco/stsz/stsc + esds(AudioSpecificConfig)
  3. 算出每个音频 chunk 的 (offset, size)，合并相邻范围
  4. 逐个 Range 下载（浏览器网络栈）
  5. 每个 AAC 帧加 7 字节 ADTS header → .aac
  6. ffmpeg -f aac → 16k 单声道 wav（供 ASR）

实测：单节下载量从 1.64GB 降到 ~34MB，省 97.9%。
"""
from __future__ import annotations

import struct
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from coursereview.paths import FFMPEG  # noqa: E402


# ---------------------------------------------------------------- box 解析
def _hdr(buf: bytes, off: int):
    if off + 8 > len(buf):
        return None
    size, typ = struct.unpack(">I4s", buf[off:off + 8])
    hdr = 8
    if size == 1:
        if off + 16 > len(buf):
            return None
        size = struct.unpack(">Q", buf[off + 8:off + 16])[0]
        hdr = 16
    elif size == 0:
        size = len(buf) - off
    return size, typ.decode("latin1"), hdr


def _find(buf: bytes, path: list[str], start: int, end: int):
    off = start
    want = path[0]
    while off + 8 <= end:
        h = _hdr(buf, off)
        if not h:
            return None
        size, typ, hdr = h
        if size < 8:
            return None
        if typ == want:
            ps, pe = off + hdr, min(off + size, len(buf))
            if len(path) == 1:
                return ps, pe
            return _find(buf, path[1:], ps, pe)
        off += size
    return None


def _children(buf: bytes, start: int, end: int):
    out = []
    off = start
    while off + 8 <= end:
        h = _hdr(buf, off)
        if not h:
            break
        size, typ, hdr = h
        if size < 8:
            break
        out.append((typ, off + hdr, min(off + size, len(buf))))
        off += size
    return out


# ---------------------------------------------------------------- esds → ASC
def _asc_from_esds(buf: bytes, esds_ps: int, esds_pe: int) -> bytes | None:
    """从 esds 里找 AudioSpecificConfig（tag 0x05）。"""
    data = buf[esds_ps:esds_pe]
    # 跳过 version/flags(4)
    i = 4
    # 简易 TLV 扫描：找 0x05 标签
    while i < len(data) - 1:
        tag = data[i]
        i += 1
        # 读长度（可变长）
        ln = 0
        for _ in range(4):
            if i >= len(data):
                break
            b = data[i]
            i += 1
            ln = (ln << 7) | (b & 0x7F)
            if not (b & 0x80):
                break
        if tag == 0x05:
            return data[i:i + ln]
        i += ln
    return None


def _parse_asc(asc: bytes) -> tuple[int, int, int]:
    """返回 (object_type, sample_rate_index, channel_config)。"""
    if not asc or len(asc) < 2:
        return 2, 11, 1
    b0, b1 = asc[0], asc[1]
    obj = (b0 >> 3) & 0x1F
    sri = ((b0 & 0x07) << 1) | ((b1 >> 7) & 0x01)
    ch = (b1 >> 3) & 0x0F
    return obj, sri, ch


# ---------------------------------------------------------------- ADTS
SR_TABLE = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
            16000, 12000, 11025, 8000, 7350]


def _adts_header(frame_len: int, obj: int, sri: int, ch: int) -> bytes:
    """7 字节 ADTS header（无 CRC）。"""
    profile = max(0, obj - 1)          # AAC-LC: obj=2 → profile=1
    fl = frame_len + 7
    b = bytearray(7)
    b[0] = 0xFF
    b[1] = 0xF1                        # MPEG-4, layer 0, no CRC
    b[2] = ((profile & 0x03) << 6) | ((sri & 0x0F) << 2) | ((ch >> 2) & 0x01)
    b[3] = ((ch & 0x03) << 6) | ((fl >> 11) & 0x03)
    b[4] = (fl >> 3) & 0xFF
    b[5] = ((fl & 0x07) << 5) | 0x1F
    b[6] = 0xFC
    return bytes(b)


# ---------------------------------------------------------------- 主流程
def extract_audio(sess_ctx, url: str, out_wav: Path, probe_mb: int = 3,
                  log=print) -> tuple[bool, str]:
    """从直链只下音频轨并转 16k wav。

    sess_ctx: Playwright BrowserContext（用其 request 走浏览器网络栈）
    """
    t0 = time.time()
    out_wav.parent.mkdir(parents=True, exist_ok=True)

    # 1) 探测头部
    n = probe_mb * 1024 * 1024
    r = sess_ctx.request.get(url, headers={"Range": f"bytes=0-{n-1}"}, timeout=120000)
    if r.status not in (200, 206):
        return False, f"头部探测 HTTP {r.status}"
    head = r.body()
    log(f"  头部 {len(head)/1e6:.1f}MB")

    # 2) 找 moov
    moov = None
    for typ, ps, pe in _children(head, 0, len(head)):
        if typ == "moov":
            moov = (ps, pe)
            break
    if not moov:
        return False, "前段无 moov（可能在尾部，需另处理）"

    # 3) 遍历 trak 找音频轨
    audio = None
    for typ, t_ps, t_pe in _children(head, moov[0], moov[1]):
        if typ != "trak":
            continue
        hdlr = _find(head, ["mdia", "hdlr"], t_ps, t_pe)
        handler = ""
        if hdlr and hdlr[1] - hdlr[0] >= 12:
            handler = head[hdlr[0] + 8:hdlr[0] + 12].decode("latin1")
        if handler != "soun":
            continue
        audio = (t_ps, t_pe)
        break
    if not audio:
        return False, "未找到音频轨"

    # 4) stsz / stco / stsc / esds
    stsz = _find(head, ["mdia", "minf", "stbl", "stsz"], *audio)
    stco = _find(head, ["mdia", "minf", "stbl", "stco"], *audio)
    co64 = _find(head, ["mdia", "minf", "stbl", "co64"], *audio)
    stsc = _find(head, ["mdia", "minf", "stbl", "stsc"], *audio)
    esds = _find(head, ["mdia", "minf", "stbl", "stsd"], *audio)
    if not (stsz and (stco or co64) and stsc):
        return False, "stbl 表缺失"

    # sample sizes
    _, sample_size, count = struct.unpack(">III", head[stsz[0]:stsz[0] + 12])
    sizes = []
    if sample_size == 0:
        off = stsz[0] + 12
        for _ in range(count):
            sizes.append(struct.unpack(">I", head[off:off + 4])[0])
            off += 4
    else:
        sizes = [sample_size] * count

    # chunk offsets
    if stco:
        nch = struct.unpack(">I", head[stco[0] + 4:stco[0] + 8])[0]
        coffs = [struct.unpack(">I", head[stco[0] + 8 + 4 * i:stco[0] + 12 + 4 * i])[0]
                 for i in range(nch)]
    else:
        nch = struct.unpack(">I", head[co64[0] + 4:co64[0] + 8])[0]
        coffs = [struct.unpack(">Q", head[co64[0] + 8 + 8 * i:co64[0] + 16 + 8 * i])[0]
                 for i in range(nch)]

    # stsc → 每个 chunk 的 sample 数
    n_ent = struct.unpack(">I", head[stsc[0] + 4:stsc[0] + 8])[0]
    entries = []
    for i in range(n_ent):
        base = stsc[0] + 8 + 12 * i
        first, per, _desc = struct.unpack(">III", head[base:base + 12])
        entries.append((first, per))
    chunk_samples = []
    for ci in range(1, nch + 1):
        per = entries[0][1]
        for first, p in entries:
            if ci >= first:
                per = p
        chunk_samples.append(per)

    # 计算每个 chunk 的 (offset, size)
    ranges = []
    si = 0
    for ci, coff in enumerate(coffs):
        cnt = chunk_samples[ci]
        sz = sum(sizes[si:si + cnt])
        si += cnt
        if sz > 0:
            ranges.append((coff, sz))

    total = sum(s for _, s in ranges)
    log(f"  音频 {len(ranges)} chunks / {total/1e6:.1f}MB")

    # 5) 合并相邻（间隙 < 64KB 就一起下，减少请求数）
    merged = []
    for off, sz in ranges:
        if merged and off - (merged[-1][0] + merged[-1][1]) < 65536:
            merged[-1] = (merged[-1][0], off + sz - merged[-1][0])
        else:
            merged.append((off, sz))
    log(f"  合并为 {len(merged)} 个请求")

    # 6) 下载并拼 ADTS
    asc = None
    if esds:
        # stsd 下还有 avc1/mp4a，esds 在其内
        for typ, s_ps, s_pe in _children(head, esds[0], esds[1]):
            if typ in ("mp4a", "enca"):
                # mp4a: 6+8 字节头，之后是 esds
                e = _find(head, ["esds"], s_ps, s_pe)
                if e:
                    asc = _asc_from_esds(head, *e)
                break
    obj, sri, ch = _parse_asc(asc) if asc else (2, 11, 1)
    log(f"  ASC: obj={obj} sample_rate_index={sri}({SR_TABLE[sri] if sri < len(SR_TABLE) else '?'}Hz) ch={ch}")

    aac = out_wav.with_suffix(".aac")
    written = 0
    # 逐 chunk 拼 ADTS（需要按 sample 粒度，但 chunk 内可能多 sample）
    si = 0
    with aac.open("wb") as f:
        for off, sz in merged:
            rr = sess_ctx.request.get(url, headers={"Range": f"bytes={off}-{off+sz-1}"},
                                      timeout=180000)
            if rr.status not in (200, 206):
                return False, f"下载 chunk@{off} HTTP {rr.status}"
            data = rr.body()
            written += len(data)
            # 该范围内含多个 sample，按 sizes 切开逐帧加 ADTS
            p = 0
            while si < len(sizes) and p < len(data):
                sl = sizes[si]
                frame = data[p:p + sl]
                if len(frame) < sl:
                    break
                f.write(_adts_header(sl, obj, sri, ch))
                f.write(frame)
                p += sl
                si += 1
    log(f"  下载 {written/1e6:.1f}MB → aac {aac.stat().st_size/1e6:.1f}MB")

    # 7) 转 16k 单声道 wav
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
    return True, (f"{out_wav.stat().st_size/1e6:.0f}MB wav / "
                  f"下载 {written/1e6:.1f}MB / {time.time()-t0:.0f}s")
