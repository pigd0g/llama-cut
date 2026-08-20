
# FFmpeg Video Editor — Complete Skill Reference

  

> Single consolidated reference document for the `ffmpeg`.
> Tags: video, audio, ffmpeg, transcoding, editing, color-grading, normalization.

---
  

## Table of Contents

  

1. [Role & Operating Principles](#1-role--operating-principles)

2. [Capabilities](#2-capabilities)

3. [Installation & Requirements](#3-installation--requirements)

4. [Probe Input Files](#4-probe-input-files)

5. [Trim / Cut](#5-trim--cut)

6. [Transcode (H.264, H.265, VP9, AV1)](#6-transcode)

7. [Concatenate Files](#7-concatenate-files)

8. [Scale / Resize](#8-scale--resize)

9. [Crop](#9-crop)

10. [Overlay / Watermark / PiP](#10-overlay--watermark--pip)

11. [Draw Text / Titles](#11-draw-text--titles)

12. [Burn Subtitles](#12-burn-subtitles)

13. [Speed Change](#13-speed-change)

14. [Reverse](#14-reverse)

15. [Transitions (xfade)](#15-transitions-xfade)

16. [Audio: Volume & Normalization](#16-audio-volume--normalization)

17. [Audio: Fade In/Out](#17-audio-fade-inout)

18. [Mix / Combine Audio](#18-mix--combine-audio)

19. [Color Grading](#19-color-grading)

20. [Chroma Key / Green Screen](#20-chroma-key--green-screen)

21. [Frame Rate Conversion](#21-frame-rate-conversion)

22. [Extract Frames / Create Slideshow](#22-extract-frames--create-slideshow)

23. [Stacking / Grid Layout](#23-stacking--grid-layout)

24. [Hardware-Accelerated Encoding](#24-hardware-accelerated-encoding)

25. [Encoding Presets (assets/preset-profiles.json)](#25-encoding-presets)

26. [Multi-Stream Filter Graph Pattern](#26-multi-stream-filter-graph-pattern)

27. [Common Options Reference](#27-common-options-reference)

28. [Debugging Tips](#28-debugging-tips)

29. [Complete Video Filter Reference](#29-complete-video-filter-reference)

30. [Complete Audio Filter Reference](#30-complete-audio-filter-reference)

31. [Codec Selection Guide](#31-codec-selection-guide)

32. [xfade Transition Reference (all 40+ types)](#32-xfade-transition-reference)

33. [Hardware Acceleration Guide (full)](#33-hardware-acceleration-guide)

34. [Helper Scripts](#34-helper-scripts)

35. [Audio Normalization Presets](#35-audio-normalization-presets)

  

---

  

## 1. Role & Operating Principles

  

You are an expert FFmpeg operator. Use this skill to answer any video/audio

editing request by constructing correct `ffmpeg` command lines. Always probe

input files first, then select the appropriate operation category.
  

See [Section 33 (Hardware Acceleration)](#33-hardware-acceleration-guide) for details on detecting and using the GPU.
 

---

  

## 2. Capabilities

  

- **Transcode** between any format: H.264, H.265, VP9, AV1, ProRes, FFV1

- **Trim & Cut** with frame-accurate or fast stream-copy modes

- **Concatenate** multiple clips with the concat demuxer helper script

- **Scale, Crop & Pad** to any resolution or aspect ratio

- **Overlay & Composite** watermarks, picture-in-picture, green-screen keying

- **Draw Text & Titles** with custom fonts, animations, and timecodes

- **Burn Subtitles** from SRT or ASS/SSA files

- **Change Speed** (setpts + atempo, any multiplier)

- **Reverse** video and audio

- **Transitions** using xfade (40+ types — see [Section 32](#32-xfade-transition-reference))

- **Color Grade** with eq, hue, LUT3D (.cube), and Hald CLUT

- **Chroma Key** green/blue screen compositing

- **Audio Normalization** via two-pass EBU R128 loudnorm

- **Audio Mixing** with amix, sidechain compression, afade

- **Hardware Acceleration** via NVENC, QSV, AMF, VideoToolbox, VAAPI

- **Frame Extraction & Slideshow** creation

- **Grid Layouts** with hstack, vstack, xstack

- **Encoding Presets** for web, social media, preview, and lossless

  

---

  

## 3. Installation & Requirements

Assume installation complete

---

  

## 4. Probe Input Files

  

Run `scripts/probe.sh <file>` (make executable first with `chmod +x scripts/*.sh` if needed), or inspect manually:

  

```bash

ffprobe -v error -show_streams -show_format -of json input.mp4

```

  

### `scripts/probe.sh` usage

  

```

Usage: probe.sh [OPTIONS] <input_file>

  -j, --json        Full JSON output (default: summary)

  -v, --video       Video stream info only

  -a, --audio       Audio stream info only

  -f, --format      Container/format info only

  -s, --streams     All streams summary

```

  

Quick start example:

  

```bash

./scripts/probe.sh input.mp4

```

  

---

  

## 5. Trim / Cut

  

```bash

# Fast stream-copy trim (no re-encode)

ffmpeg -ss 00:01:00 -to 00:02:30 -i input.mp4 -c copy out.mp4

  

# Precise trim (re-encode around keyframes)

ffmpeg -ss 00:01:00 -i input.mp4 -t 90 -c:v libx264 -crf 18 -c:a aac out.mp4

```

  

---

  

## 6. Transcode

  

```bash

# H.264 web-safe (pix_fmt ensures broad player compatibility)

ffmpeg -i input.mp4 -c:v libx264 -crf 23 -preset slow \

  -pix_fmt yuv420p -c:a aac -b:a 128k -movflags +faststart out.mp4

  

# H.265 (smaller files)

ffmpeg -i input.mp4 -c:v libx265 -crf 28 -preset slow \

  -pix_fmt yuv420p -c:a aac -b:a 128k out.mp4

  

# VP9 for web

ffmpeg -i input.mp4 -c:v libvpx-vp9 -crf 33 -b:v 0 \

  -c:a libopus -b:a 128k out.webm

  

# AV1 (best compression)

ffmpeg -i input.mp4 -c:v libaom-av1 -crf 35 -cpu-used 4 \

  -c:a libopus -b:a 128k out.mkv

```

  

See [Section 31 (Codec Selection Guide)](#31-codec-selection-guide) for full codec comparison.

  

---

  

## 7. Concatenate Files

  

```bash

# Using helper script

scripts/concat.sh out.mp4 clip1.mp4 clip2.mp4 clip3.mp4

  

# Manual concat demuxer

printf "file 'a.mp4'\nfile 'b.mp4'\n" > list.txt

ffmpeg -f concat -safe 0 -i list.txt -c copy out.mp4

```

  

### `scripts/concat.sh` usage

  

```

Usage: concat.sh [OPTIONS] <output_file> <input1> [input2 ...]

  -r, --reencode    Re-encode output (default: stream copy)

  -v, --vcodec      Video codec for re-encode (default: libx264)

  -a, --acodec      Audio codec for re-encode (default: aac)

  --crf             CRF value for re-encode (default: 23)

  --preset          x264/x265 preset (default: slow)

  -k, --keep-list   Keep the temporary concat list file

```

  

Examples:

  

```bash

concat.sh out.mp4 a.mp4 b.mp4 c.mp4

concat.sh --reencode -v libx265 --crf 28 out.mp4 a.mp4 b.mp4

```

  

Behavior details:

  

- Requires at least two input files.

- Uses absolute paths (via `realpath`) with single-quote escaping for the concat list.

- Stream-copy mode (`-c copy`) is the default; re-encode mode uses `-c:v <vcodec> -crf <crf> -preset <preset> -c:a <acodec> -b:a 128k`.

- Adds `-movflags +faststart` automatically for `.mp4`/`.m4v` outputs in re-encode mode.

- Temp concat list kept only with `-k`; otherwise cleaned up on exit.

- After completion, prints output duration/size/bitrate via ffprobe.

  

---

  

## 8. Scale / Resize

  

```bash

# Scale to 1920×1080, keep aspect ratio, pad to fill

ffmpeg -i in.mp4 -vf "scale=1920:1080:force_original_aspect_ratio=decrease,\

pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black" out.mp4

  

# Scale by width only (auto height)

ffmpeg -i in.mp4 -vf "scale=1280:-2" out.mp4

```

  

---

  

## 9. Crop

  

```bash

# Crop 640×360 starting at (100,50)

ffmpeg -i in.mp4 -vf "crop=640:360:100:50" out.mp4

  

# Center crop to 1:1 square

ffmpeg -i in.mp4 -vf "crop=min(iw\,ih):min(iw\,ih)" out.mp4

```

  

---

  

## 10. Overlay / Watermark / PiP

  

```bash

# Overlay image (top-right, 10px margin)

ffmpeg -i video.mp4 -i logo.png \

  -filter_complex "[0:v][1:v] overlay=W-w-10:10" out.mp4

  

# PiP: small video on top of main

ffmpeg -i main.mp4 -i pip.mp4 \

  -filter_complex "[1:v]scale=320:-2[small];[0:v][small]overlay=W-w-10:H-h-10" out.mp4

```

  

---

  

## 11. Draw Text / Titles

  

```bash

ffmpeg -i in.mp4 -vf \

  "drawtext=text='My Title':fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf\

:fontsize=48:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2\

:shadowx=2:shadowy=2" out.mp4

```

  

---

  

## 12. Burn Subtitles

  

```bash

# SRT subtitles (re-encode required)

ffmpeg -i video.mp4 -vf "subtitles=subs.srt" out.mp4

  

# ASS/SSA subtitles (preserves styling)

ffmpeg -i video.mp4 -vf "ass=subs.ass" out.mp4

```

  

---

  

## 13. Speed Change

  

```bash

# 2× speed (video + audio)

ffmpeg -i in.mp4 -vf "setpts=0.5*PTS" -af "atempo=2.0" out.mp4

  

# 0.5× speed (half speed)

ffmpeg -i in.mp4 -vf "setpts=2.0*PTS" -af "atempo=0.5" out.mp4

  

# >4×: chain atempo (max 2.0 per filter)

ffmpeg -i in.mp4 -vf "setpts=0.25*PTS" -af "atempo=2.0,atempo=2.0" out.mp4

  

# <0.5×: chain atempo (min 0.5 per filter)

ffmpeg -i in.mp4 -vf "setpts=4.0*PTS" -af "atempo=0.5,atempo=0.5" out.mp4

```

  

---

  

## 14. Reverse

  

```bash

ffmpeg -i in.mp4 -vf "reverse" -af "areverse" out.mp4

```

  

---

  

## 15. Transitions (xfade)

  

```bash

# Dissolve between two clips (1s overlap at t=5)

ffmpeg -i a.mp4 -i b.mp4 \

  -filter_complex "[0:v][1:v]xfade=transition=dissolve:duration=1:offset=5[v];\

[0:a][1:a]acrossfade=d=1[a]" \

  -map "[v]" -map "[a]" out.mp4

```

  

See [Section 32](#32-xfade-transition-reference) for all 40+ xfade transition types.

  

---

  

## 16. Audio: Volume & Normalization

  

```bash

# Adjust volume

ffmpeg -i in.mp4 -af "volume=1.5" out.mp4

  

# EBU R128 normalization (two-pass)

scripts/normalize-audio.sh in.mp4 out.mp4

  

# One-pass loudnorm

ffmpeg -i in.mp4 -af "loudnorm=I=-16:TP=-1.5:LRA=11" out.mp4

```

  

### `scripts/normalize-audio.sh` usage

  

```

Usage: normalize-audio.sh [OPTIONS] <input> <output>

  -I, --integrated  Target LUFS (default: -16)

  -T, --true-peak   Max dBTP (default: -1.5)

  -L, --lra         Loudness range LU (default: 11)

  -s, --stereo      Force stereo downmix

  -r, --sample-rate Output sample rate in Hz (default: preserve)

  --no-video        Drop video stream (default: copy if present)

  --acodec          Output audio codec (default: aac)

  --abitrate        Output audio bitrate (default: 192k)

```

  

---

  

## 17. Audio: Fade In/Out

  

```bash

# Fade in 2s at start, fade out 3s before end (total 60s)

ffmpeg -i in.mp4 -af "afade=t=in:d=2,afade=t=out:st=57:d=3" out.mp4

```

  

---

  

## 18. Mix / Combine Audio

  

```bash

# Mix two audio streams

ffmpeg -i video.mp4 -i music.mp3 \

  -filter_complex "[0:a][1:a]amix=inputs=2:duration=first:weights=1 0.3[a]" \

  -map 0:v -map "[a]" -c:v copy out.mp4

```

  

---

  

## 19. Color Grading

  

```bash

# Brightness/Contrast/Saturation

ffmpeg -i in.mp4 -vf "eq=brightness=0.05:contrast=1.2:saturation=1.3" out.mp4

  

# Hue shift

ffmpeg -i in.mp4 -vf "hue=h=30:s=1.1" out.mp4

  

# Apply 3D LUT (.cube)

ffmpeg -i in.mp4 -vf "lut3d=file=lut.cube" out.mp4

  

# Apply Hald CLUT PNG

ffmpeg -i in.mp4 -i lut.png \

  -filter_complex "[0:v][1:v]haldclut" out.mp4

```

  

---

  

## 20. Chroma Key / Green Screen

  

```bash

# chromakey (color + similarity + blend)

ffmpeg -i foreground.mp4 -i background.mp4 \

  -filter_complex "[0:v]chromakey=0x00FF00:0.3:0.2[fg];\

[1:v][fg]overlay" out.mp4

  

# colorkey (simpler, exact color)

ffmpeg -i in.mp4 -vf "colorkey=green:0.3:0.2" out.mp4

```

  

---

  

## 21. Frame Rate Conversion

  

```bash

# Convert to 30fps

ffmpeg -i in.mp4 -vf "fps=30" out.mp4

  

# Use motion interpolation (minterpolate)

ffmpeg -i in.mp4 -vf "minterpolate=fps=60:mi_mode=mci" out.mp4

```

  

---

  

## 22. Extract Frames / Create Slideshow

  

```bash

# Extract one frame per second as PNG

ffmpeg -i in.mp4 -vf "fps=1" frames/%04d.png

  

# Slideshow from images (3s per image, 30fps output)

ffmpeg -framerate 1/3 -i frames/%04d.png -c:v libx264 -r 30 out.mp4

```

  

---

  

## 23. Stacking / Grid Layout

  

```bash

# Side by side (hstack)

ffmpeg -i a.mp4 -i b.mp4 -filter_complex "[0:v][1:v]hstack=inputs=2" out.mp4

  

# Top-bottom (vstack)

ffmpeg -i a.mp4 -i b.mp4 -filter_complex "[0:v][1:v]vstack=inputs=2" out.mp4

  

# 2×2 grid (xstack)

ffmpeg -i a.mp4 -i b.mp4 -i c.mp4 -i d.mp4 \

  -filter_complex "[0:v][1:v][2:v][3:v]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0" out.mp4

```

  

---

  

## 24. Hardware-Accelerated Encoding

  

```bash

# NVIDIA NVENC H.264

ffmpeg -hwaccel cuda -i in.mp4 -c:v h264_nvenc -preset p4 -cq 23 out.mp4

  

# Apple VideoToolbox H.265

ffmpeg -i in.mp4 -c:v hevc_videotoolbox -q:v 60 out.mp4

  

# Intel QuickSync H.264

ffmpeg -hwaccel qsv -i in.mp4 -c:v h264_qsv -global_quality 23 out.mp4

```

  

See [Section 33 (full hardware acceleration guide)](#33-hardware-acceleration-guide) for the complete GPU encoding guide.

  

---

  

## 25. Encoding Presets

  

When the task maps to a named use case below, read `assets/preset-profiles.json`

and use the `example` field as the ready-made ffmpeg command.

  

### Summary Table

  

| Profile | Codec | CRF | Use Case |

|--------------------------|-----------|-----|-----------------------|

| `web-optimized` | H.264 | 23 | YouTube, Vimeo, web |

| `high-quality` | H.264 | 18 | Client deliverables, archive |

| `social-media-vertical` | H.264 | 23 | TikTok, Reels (9:16) |

| `social-media-square` | H.264 | 23 | Instagram feed (1:1) |

| `fast-preview` | H.264 | 28 | Proxy, review links |

| `lossless-intermediate` | FFV1 | — | Post-production, preservation |

  

### Full Profile Details & Ready-Made Commands

  

#### `web-optimized`

  

- Description: H.264 optimized for browser streaming and web delivery

- Video: libx264, CRF 23, preset slow, profile high, level 4.0, `yuv420p`, `+faststart`

- Audio: aac, 128k, 44100 Hz, 2 channels

- Container: mp4

  

```bash

ffmpeg -i input.mp4 -c:v libx264 -crf 23 -preset slow -profile:v high -level 4.0 -pix_fmt yuv420p -c:a aac -b:a 128k -movflags +faststart output.mp4

```

  

#### `high-quality`

  

- **Use case:** Client deliverables, archiving, source for future transcodes.

- Video: libx264, CRF 18, preset veryslow, profile high, level 4.2, `yuv420p`, `+faststart`

- Audio: aac, 192k, 48000 Hz, 2 channels

- Container: mp4

  

```bash

ffmpeg -i input.mp4 -c:v libx264 -crf 18 -preset veryslow -profile:v high -level 4.2 -pix_fmt yuv420p -c:a aac -b:a 192k -ar 48000 -movflags +faststart output.mp4

```

  

#### `social-media-vertical`

  

- **Use case:** TikTok, Instagram Reels, YouTube Shorts, Stories.

- Dimensions: 1080×1920, aspect 9:16.

- Scale filter: `scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black`

- Video: libx264, CRF 23, preset slow, profile high, level 4.0, `yuv420p`, max bitrate 6M, `+faststart`

- Audio: aac, 128k, 44100 Hz, 2 channels

- Container: mp4

- Notes: Max file size 287.6 MB (TikTok) / 4 GB (Instagram); max duration 60s TikTok short / 3 min TikTok long / 90s Reels; recommended 30 or 60 fps.

  

```bash

ffmpeg -i input.mp4 -vf "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black" -c:v libx264 -crf 23 -preset slow -pix_fmt yuv420p -c:a aac -b:a 128k -movflags +faststart output.mp4

```

  

#### `social-media-square`

  

- **Use case:** Instagram feed posts, Facebook video, LinkedIn.

- **Dimensions:** 1080x1080, aspect 1:1.

- Scale filter: `scale=1080:1080:force_original_aspect_ratio=decrease,pad=1080:1080:(ow-iw)/2:(oh-ih)/2:black`

- Video: libx264, CRF 23, preset slow, profile high, level 4.0, `yuv420p`, max bitrate 3.5M, `+faststart`

- Audio: aac, 128k, 44100 Hz, 2 channels

- Container: mp4

- Notes: Instagram max 4 GB / max 60s feed video; Facebook max 10 GB / max 240 min.

  

```bash

ffmpeg -i input.mp4 -vf "scale=1080:1080:force_original_aspect_ratio=decrease,pad=1080:1080:(ow-iw)/2:(oh-ih)/2:black" -c:v libx264 -crf 23 -preset slow -pix_fmt yuv420p -c:a aac -b:a 128k -movflags +faststart output.mp4

```

  

#### `fast-preview`

  

- **Use case:** Client review links, editing proxy, quick previews.

- Video: libx264, CRF 28, preset ultrafast, profile baseline, level 3.0, `yuv420p`, scale `1280:-2`, `+faststart`

- Audio: aac, 96k, 44100 Hz, 2 channels

- Container: mp4

- Notes: Encodes 5–10× faster than real-time on modern hardware; not for final delivery — review only.

  

```bash

ffmpeg -i input.mp4 -vf "scale=1280:-2" -c:v libx264 -crf 28 -preset ultrafast -profile:v baseline -level 3.0 -pix_fmt yuv420p -c:a aac -b:a 96k -movflags +faststart preview.mp4

```

  

#### `lossless-intermediate`

  

- **Use case:** Intermediate between editing stages, long-term preservation.

- Video: `ffv1`, level 3, threads 8, coder 1, context 1, gop 1, slicecrc 1

- Audio: `flac`, compression level 8

- Container: mkv

- Notes: Lossless — no quality loss across multiple encode/decode cycles; large file (~1–3 GB per minute at 1080p); FFV1 level 3 supports multithreading and error detection; suitable for OAIS/NDSA preservation standards.

  

```bash

ffmpeg -i input.mp4 -c:v ffv1 -level 3 -threads 8 -coder 1 -context 1 -g 1 -slicecrc 1 -c:a flac output.mkv

```

  

---

  

## 26. Multi-Stream Filter Graph Pattern

  

```bash

ffmpeg \

  -i input_video.mp4 \

  -i overlay.png \

  -i audio_bed.mp3 \

  -filter_complex "

    [0:v]scale=1920:1080,setsar=1[base];

    [1:v]scale=200:-2,format=rgba,colorchannelmixer=aa=0.8[logo];

    [base][logo]overlay=W-w-20:20[composited];

    [0:a][2:a]amix=inputs=2:weights=1 0.2[audio_mix]

  " \

  -map "[composited]" -map "[audio_mix]" \

  -c:v libx264 -crf 18 -preset slow \

  -c:a aac -b:a 192k \

  -movflags +faststart \

  output.mp4

```

  

---

  

## 27. Common Options Reference

  

| Flag | Meaning |

|-------------------|----------------------------------------------|

| `-ss` | Seek/start time (before `-i` = fast seek) |

| `-to` | End time (absolute) |

| `-t` | Duration |

| `-c copy` | Stream copy (no re-encode) |

| `-crf` | Constant Rate Factor (lower = better) |

| `-preset` | Encoder speed/quality tradeoff |

| `-movflags +faststart` | Move moov atom to front (web streaming) |

| `-vf` | Video filter chain |

| `-af` | Audio filter chain |

| `-filter_complex` | Multi-input/output filter graph |

| `-map` | Select output streams explicitly |

| `-an` | Drop audio |

| `-vn` | Drop video |

| `-y` | Overwrite output without prompt |

  

---

  

## 28. Debugging Tips

  

```bash

# Dry-run: print filter graph without encoding

ffmpeg -i in.mp4 -vf "scale=1280:-2" -f null -

  

# Benchmark encode speed

ffmpeg -benchmark -i in.mp4 -c:v libx264 -f null -

  

# Show available encoders

ffmpeg -encoders | grep -E "^.V|^.A"

  

# Show filter options

ffmpeg -help filter=loudnorm

```

  

---

  

## 29. Complete Video Filter Reference

  

All filters are applied with `-vf "filter"` (video), `-af "filter"` (audio), or `-filter_complex "..."` (multi-stream).

  

### scale — Resize Video

  

```

scale=width:height[:flags]

```

  

| Parameter | Values | Description |

|-----------|--------|-------------|

| `width` / `height` | pixels or `-1` / `-2` | `-2` = auto (divisible by 2) |

| `force_original_aspect_ratio` | `disable`, `decrease`, `increase` | Letterbox/pillarbox mode |

| `flags` | `bilinear`, `bicubic`, `lanczos` | Scaling algorithm |

  

```bash

# Scale to 1280 wide, auto height (div by 2)

ffmpeg -i in.mp4 -vf "scale=1280:-2" out.mp4

  

# Letterbox to 1920×1080

ffmpeg -i in.mp4 -vf "scale=1920:1080:force_original_aspect_ratio=decrease,\

pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black" out.mp4

  

# Scale using expression

ffmpeg -i in.mp4 -vf "scale=iw/2:ih/2" out.mp4

```

  

### crop — Crop Video

  

```

crop=width:height:x:y

```

  

```bash

# Crop 640×360 at offset (100, 50)

ffmpeg -i in.mp4 -vf "crop=640:360:100:50" out.mp4

  

# Center crop to square

ffmpeg -i in.mp4 -vf "crop=min(iw\,ih):min(iw\,ih)" out.mp4

  

# Crop to 16:9 from 4:3

ffmpeg -i in.mp4 -vf "crop=ih*16/9:ih" out.mp4

  

# Crop bottom 20%

ffmpeg -i in.mp4 -vf "crop=iw:ih*0.8:0:0" out.mp4

```

  

### pad — Add Padding / Letterbox

  

```

pad=width:height:x:y[:color]

```

  

```bash

# Pillarbox 4:3 → 16:9

ffmpeg -i in.mp4 -vf "pad=iw*16/9:ih:(ow-iw)/2:0:black" out.mp4

  

# Add 10px white border

ffmpeg -i in.mp4 -vf "pad=iw+20:ih+20:10:10:white" out.mp4

```

  

### overlay — Composite Two Videos

  

```

overlay=x:y[:format][:eval]

```

  

```bash

# Watermark bottom-right, 10px margin

ffmpeg -i video.mp4 -i logo.png \

  -filter_complex "[0:v][1:v]overlay=W-w-10:H-h-10" out.mp4

  

# Semi-transparent overlay

ffmpeg -i bg.mp4 -i fg.png \

  -filter_complex "[1:v]format=rgba,colorchannelmixer=aa=0.5[fg];\

[0:v][fg]overlay=0:0" out.mp4

  

# Time-limited overlay (show logo 5–15s)

ffmpeg -i video.mp4 -i logo.png \

  -filter_complex "[0:v][1:v]overlay=10:10:enable='between(t,5,15)'" out.mp4

```

  

### drawtext — Render Text on Video

  

```

drawtext=text='...':fontfile=...:fontsize=...:fontcolor=...:x=...:y=...

```

  

| Parameter | Description |

|-----------|-------------|

| `text` | Static text string |

| `textfile` | Read text from file |

| `fontfile` | Path to TTF/OTF font |

| `fontsize` | Font size in pixels |

| `fontcolor` | Color name or `0xRRGGBB[@alpha]` |

| `x`, `y` | Position (supports expressions) |

| `shadowx/y` | Drop shadow offset |

| `borderw` | Stroke width |

| `bordercolor` | Stroke color |

| `box=1` | Enable background box |

| `boxcolor` | Background box color |

| `enable` | Time expression |

  

```bash

# Centered white title

ffmpeg -i in.mp4 -vf \

  "drawtext=text='Hello World':fontsize=72:fontcolor=white:\

x=(w-text_w)/2:y=(h-text_h)/2:shadowx=3:shadowy=3" out.mp4

  

# Scrolling ticker bottom

ffmpeg -i in.mp4 -vf \

  "drawtext=text='Breaking News':fontsize=36:fontcolor=yellow:\

x=w-mod(t*200\,w+text_w):y=h-50:box=1:boxcolor=red@0.8" out.mp4

  

# Timecode overlay

ffmpeg -i in.mp4 -vf \

  "drawtext=text='%{pts\\:hms}':fontsize=24:fontcolor=white:\

x=10:y=10:box=1:boxcolor=black@0.5" out.mp4

```

  

### subtitles — Burn SRT/ASS Subtitles

  

```

subtitles=filename[:stream_index=N]

ass=filename

```

  

```bash

# Burn SRT subtitles

ffmpeg -i video.mp4 -vf "subtitles=subs.srt" out.mp4

  

# Burn SRT with custom style

ffmpeg -i video.mp4 -vf \

  "subtitles=subs.srt:force_style='FontSize=24,PrimaryColour=&H00FFFFFF'" out.mp4

  

# Burn ASS/SSA (preserves styling)

ffmpeg -i video.mp4 -vf "ass=subs.ass" out.mp4

  

# Select subtitle track from container

ffmpeg -i video.mkv -vf "subtitles=video.mkv:si=0" out.mp4

```

  

### setpts — Change Presentation Timestamps (Speed)

  

```

setpts=expression*PTS

```

  

```bash

# 2× speed (half timestamps → faster playback)

ffmpeg -i in.mp4 -vf "setpts=0.5*PTS" out.mp4

  

# 0.5× speed (double timestamps → slower)

ffmpeg -i in.mp4 -vf "setpts=2.0*PTS" out.mp4

```

  

### reverse — Reverse Video

  

```

reverse

```

  

```bash

# Reverse entire clip (loads all frames into memory)

ffmpeg -i in.mp4 -vf "reverse" -af "areverse" out.mp4

  

# Reverse a short segment only

ffmpeg -i in.mp4 -ss 5 -t 3 -vf "reverse" -af "areverse" reversed_clip.mp4

```

  

### fps — Force Frame Rate

  

```

fps=rate[:round]

```

  

```bash

ffmpeg -i in.mp4 -vf "fps=30" out.mp4

ffmpeg -i in.mp4 -vf "fps=24000/1001" out.mp4   # 23.976 fps

ffmpeg -i in.mp4 -vf "fps=60,setpts=PTS" out.mp4  # 60fps with pts correction

```

  

### trim — Trim Video Stream

  

```

trim=start=...:end=...:duration=...

```

  

```bash

# Trim to 5–10 seconds then reset timestamps

ffmpeg -i in.mp4 -vf "trim=start=5:end=10,setpts=PTS-STARTPTS" out.mp4

```

  

### eq — Brightness, Contrast, Saturation, Gamma

  

```

eq=brightness=...:contrast=...:saturation=...:gamma=...

```

  

| Parameter | Range | Default |

|-----------|-------|---------|

| `brightness` | -1.0 to 1.0 | 0 |

| `contrast` | -1000 to 1000 | 1 |

| `saturation` | 0 to 3 | 1 |

| `gamma` | 0.1 to 10.0 | 1 |

| `gamma_r/g/b` | 0.1 to 10.0 | 1 |

  

```bash

ffmpeg -i in.mp4 -vf "eq=brightness=0.05:contrast=1.2:saturation=1.3:gamma=1.1" out.mp4

```

  

### hue — Hue/Saturation Shift

  

```

hue=h=degrees:s=saturation

```

  

```bash

# Shift hue 30°, boost saturation

ffmpeg -i in.mp4 -vf "hue=h=30:s=1.2" out.mp4

  

# Desaturate to grayscale

ffmpeg -i in.mp4 -vf "hue=s=0" out.mp4

```

  

### colorkey — Remove Solid Color

  

```

colorkey=color:similarity:blend

```

  

```bash

# Remove green background

ffmpeg -i in.mp4 -vf "colorkey=green:0.3:0.2" out.mp4

ffmpeg -i in.mp4 -vf "colorkey=0x00FF00:0.35:0.15" out.mp4

```

  

### chromakey — Green/Blue Screen Removal

  

```

chromakey=color:similarity:blend[:yuv=1]

```

  

More accurate than `colorkey` for chroma keying.

  

```bash

# Composite: green-screen foreground over background

ffmpeg -i fg.mp4 -i bg.mp4 \

  -filter_complex "[0:v]chromakey=0x00FF00:0.3:0.2[fg];\

[1:v][fg]overlay" out.mp4

  

# Blue screen

ffmpeg -i fg.mp4 -i bg.mp4 \

  -filter_complex "[0:v]chromakey=0x0000FF:0.15:0.1[fg];\

[1:v][fg]overlay" out.mp4

```

  

### lut3d — Apply 3D LUT File (.cube, .3dl, .m3d)

  

```

lut3d=file=filename[:interp=mode]

```

  

| `interp` | Description |

|----------|-------------|

| `nearest` | Nearest neighbor (fast) |

| `trilinear` | Trilinear interpolation |

| `tetrahedral` | Tetrahedral (highest quality, default) |

  

```bash

ffmpeg -i in.mp4 -vf "lut3d=file=grade.cube" out.mp4

ffmpeg -i in.mp4 -vf "lut3d=file=film_emulation.3dl:interp=tetrahedral" out.mp4

```

  

### haldclut — Apply Hald CLUT Image

  

Takes two inputs: video and CLUT image.

  

```bash

ffmpeg -i video.mp4 -i hald_clut.png \

  -filter_complex "[0:v][1:v]haldclut" out.mp4

```

  

### xfade — Video Transition Between Two Clips

  

```

xfade=transition=type:duration=secs:offset=secs

```

  

```bash

# Dissolve, 1s overlap, starting at t=5 of first clip

ffmpeg -i a.mp4 -i b.mp4 \

  -filter_complex "[0:v][1:v]xfade=transition=dissolve:duration=1:offset=5[v];\

[0:a][1:a]acrossfade=d=1[a]" \

  -map "[v]" -map "[a]" out.mp4

```

  

See [Section 32 (all transition types)](#32-xfade-transition-reference).

  

### minterpolate — Frame Interpolation

  

```

minterpolate=fps=N:mi_mode=mci[:mc_mode=aobmc]

```

  

```bash

# Smooth 24fps → 60fps with motion compensation

ffmpeg -i in.mp4 -vf "minterpolate=fps=60:mi_mode=mci" out.mp4

```

  

### hstack / vstack / xstack — Grid Layouts

  

```bash

# Side by side

ffmpeg -i a.mp4 -i b.mp4 -filter_complex "[0:v][1:v]hstack=inputs=2" out.mp4

  

# Top-bottom

ffmpeg -i a.mp4 -i b.mp4 -filter_complex "[0:v][1:v]vstack=inputs=2" out.mp4

  

# 2×2 grid

ffmpeg -i a.mp4 -i b.mp4 -i c.mp4 -i d.mp4 \

  -filter_complex "[0:v][1:v][2:v][3:v]xstack=inputs=4:\

layout=0_0|w0_0|0_h0|w0_h0[v]" \

  -map "[v]" out.mp4

```

  

---

  

## 30. Complete Audio Filter Reference

  

### volume — Adjust Audio Volume

  

```

volume=value[:eval=frame]

```

  

```bash

ffmpeg -i in.mp4 -af "volume=1.5" out.mp4       # +50%

ffmpeg -i in.mp4 -af "volume=0.5" out.mp4       # -50%

ffmpeg -i in.mp4 -af "volume=6dB" out.mp4       # +6 dB

ffmpeg -i in.mp4 -af "volume=-3dB" out.mp4      # -3 dB

```

  

### loudnorm — EBU R128 Loudness Normalization

  

```

loudnorm=I=target:TP=true_peak:LRA=range[:measured_I=...:linear=true]

```

  

| Parameter | Default | Description |

|-----------|---------|-------------|

| `I` | -24 | Integrated loudness target (LUFS) |

| `TP` | -2 | Max true peak (dBTP) |

| `LRA` | 7 | Loudness range target (LU) |

| `linear` | false | Linear mode (use with pass 2 values) |

| `print_format` | `none` | `summary` or `json` for measurements |

  

```bash

# One-pass (dynamic mode)

ffmpeg -i in.mp4 -af "loudnorm=I=-16:TP=-1.5:LRA=11" out.mp4

  

# Pass 1 — measure

ffmpeg -i in.mp4 -af "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json" -f null -

  

# Pass 2 — apply linear normalization

ffmpeg -i in.mp4 -af \

  "loudnorm=I=-16:TP=-1.5:LRA=11:measured_I=-23.5:measured_TP=-2.0:\

measured_LRA=8.1:measured_thresh=-34.1:linear=true" out.mp4

```

  

Use `scripts/normalize-audio.sh` for automated two-pass normalization.

  

### atempo — Change Audio Speed (Pitch-Preserving)

  

```

atempo=rate    (range: 0.5–2.0 per filter instance)

```

  

```bash

ffmpeg -i in.mp4 -af "atempo=1.5" out.mp4              # 1.5× speed

ffmpeg -i in.mp4 -af "atempo=2.0,atempo=2.0" out.mp4   # 4× speed

ffmpeg -i in.mp4 -af "atempo=0.5" out.mp4              # 0.5× speed

```

  

### areverse — Reverse Audio Stream

  

```bash

ffmpeg -i in.mp4 -af "areverse" out.mp4

```

  

### afade — Fade Audio In/Out

  

```

afade=t=in|out:st=start_time:d=duration

```

  

```bash

# Fade in 2s at start

ffmpeg -i in.mp4 -af "afade=t=in:st=0:d=2" out.mp4

  

# Fade out last 3s of a 60s clip

ffmpeg -i in.mp4 -af "afade=t=out:st=57:d=3" out.mp4

  

# Both

ffmpeg -i in.mp4 -af "afade=t=in:d=2,afade=t=out:st=57:d=3" out.mp4

```

  

### amix — Mix Multiple Audio Streams

  

```

amix=inputs=N[:duration=longest|shortest|first][:weights=w1 w2 ...]

```

  

```bash

# Mix two audio tracks (video at full volume, music at 30%)

ffmpeg -i video.mp4 -i music.mp3 \

  -filter_complex "[0:a][1:a]amix=inputs=2:duration=first:weights=1 0.3[a]" \

  -map 0:v -map "[a]" -c:v copy out.mp4

```

  

### acrossfade — Crossfade Between Two Audio Clips

  

```

acrossfade=d=duration[:c1=curve][:c2=curve]

```

  

```bash

ffmpeg -i a.mp4 -i b.mp4 \

  -filter_complex "[0:a][1:a]acrossfade=d=1[a]" \

  -map "[a]" out.aac

```

  

### sidechaincompress — Sidechain Compression (Ducking)

  

```

sidechaincompress=threshold:ratio:attack:release:level_sc=N

```

  

```bash

# Duck music when voice is present

ffmpeg -i music.mp3 -i voice.mp3 \

  -filter_complex "[1:a]asplit=2[sc][ref];\

[0:a][sc]sidechaincompress=threshold=0.02:ratio=4:attack=200:release=1000[compressed];\

[compressed][ref]amix=inputs=2[a]" \

  -map "[a]" out.mp3

```

  

### equalizer — Parametric EQ

  

```

equalizer=f=frequency:width_type=type:width=value:g=gain

```

  

```bash

# Boost 8kHz by 6dB (presence boost)

ffmpeg -i in.mp4 -af "equalizer=f=8000:width_type=o:width=2:g=6" out.mp4

  

# Cut 200Hz (muddy bass reduction)

ffmpeg -i in.mp4 -af "equalizer=f=200:width_type=o:width=1:g=-6" out.mp4

```

  

### highpass / lowpass — High/Low-Pass Filters

  

```bash

# Remove rumble below 80Hz

ffmpeg -i in.mp4 -af "highpass=f=80" out.mp4

  

# Telephone effect (bandpass 300–3400Hz)

ffmpeg -i in.mp4 -af "highpass=f=300,lowpass=f=3400" out.mp4

```

  

### agate — Noise Gate

  

```

agate=threshold:range:attack:release

```

  

```bash

# Gate background noise below -40dB

ffmpeg -i in.mp4 -af "agate=threshold=0.01:range=0.06:attack=10:release=200" out.mp4

```

  

### dynaudnorm — Dynamic Audio Normalizer

  

```

dynaudnorm=f=frame_len:p=peak_value

```

  

```bash

# Normalize frame-by-frame (good for spoken word)

ffmpeg -i in.mp4 -af "dynaudnorm=f=500:p=0.9" out.mp4

```

  

### aecho — Echo / Reverb

  

```

aecho=in_gain:out_gain:delays:decays

```

  

```bash

# Short room reverb

ffmpeg -i in.mp4 -af "aecho=0.8:0.88:60:0.4" out.mp4

  

# Long cave echo

ffmpeg -i in.mp4 -af "aecho=0.8:0.5:1000|1800:0.4|0.25" out.mp4

```

  

### aresample — Resample Audio

  

```bash

ffmpeg -i in.mp4 -af "aresample=48000" out.mp4

```

  

### channelmap / pan — Channel Remapping

  

```bash

# Extract left channel as mono

ffmpeg -i stereo.mp4 -af "channelmap=0|0:mono" out.mp4

  

# Mix stereo to mono

ffmpeg -i stereo.mp4 -af "pan=mono|c0=0.5*c0+0.5*c1" out.mp4

  

# Create stereo from two mono files

ffmpeg -i left.wav -i right.wav \

  -filter_complex "[0:a][1:a]join=inputs=2:channel_layout=stereo[a]" \

  -map "[a]" out.wav

```

  

---

  

## 31. Codec Selection Guide

  

### Video Codec Comparison

  

| Codec | Encoder | CRF Range | Best CRF | Presets | Rel. Speed | Browser | Use Case |

|-------|---------|-----------|----------|---------|------------|---------|----------|

| H.264 | `libx264` | 0–51 | 18–28 | ultrafast→veryslow | Baseline | All | Web, streaming, archive |

| H.265 | `libx265` | 0–51 | 24–32 | ultrafast→veryslow | ~2× slower | Safari/Edge | 4K, HDR, smaller files |

| VP9 | `libvpx-vp9` | 0–63 | 28–40 | `cpu-used` 0–5 | ~3× slower | Chrome/FF | Web video, YouTube |

| AV1 | `libaom-av1` | 0–63 | 28–45 | `cpu-used` 0–8 | ~10× slower | Modern browsers | Best compression, future-proof |

| ProRes | `prores_ks` | — | `-profile:v` | — | Fast | macOS | Post-production, editing |

| FFV1 | `ffv1` | — | lossless | `-level 3` | Moderate | — | Archiving, preservation |

  

> **CRF guide:** Lower = better quality, larger file. Values above are typical "sweet spots" for each codec.

  

### H.264 (`libx264`)

  

The most widely compatible codec. Ideal for delivery, streaming, and web.

  

```bash

# Standard web delivery

ffmpeg -i in.mp4 -c:v libx264 -crf 23 -preset slow \

  -c:a aac -b:a 128k -movflags +faststart out.mp4

  

# High quality archive

ffmpeg -i in.mp4 -c:v libx264 -crf 18 -preset veryslow \

  -c:a aac -b:a 192k -movflags +faststart out.mp4

  

# Fast proxy

ffmpeg -i in.mp4 -c:v libx264 -crf 28 -preset ultrafast \

  -c:a aac -b:a 96k out.mp4

  

# Constrained bitrate (streaming server)

ffmpeg -i in.mp4 -c:v libx264 -b:v 2M -maxrate 2.5M -bufsize 5M \

  -c:a aac -b:a 128k out.mp4

```

  

**Presets (speed/quality tradeoff, same CRF):**

  

`ultrafast` → `superfast` → `veryfast` → `faster` → `fast` → `medium` → `slow` → `slower` → `veryslow`

  

- Faster presets = larger files at same quality

- `slow` or `slower` recommended for final delivery

  

**H.264 Profiles:**

  

| Profile | `-profile:v` | Max level | Compatible with |

|---------|-------------|-----------|-----------------|

| Baseline | `baseline` | 3.0 | Old mobile, HLS |

| Main | `main` | 4.0 | Most devices |

| High | `high` | 5.2 | Modern devices (default) |

  

```bash

# Maximum compatibility (old devices, HLS)

ffmpeg -i in.mp4 -c:v libx264 -profile:v baseline -level 3.0 \

  -crf 23 -preset slow -c:a aac -b:a 128k out.mp4

```

  

### H.265 / HEVC (`libx265`)

  

~40–50% smaller than H.264 at equivalent quality. Excellent for 4K and HDR.

  

```bash

# Standard H.265

ffmpeg -i in.mp4 -c:v libx265 -crf 28 -preset slow \

  -c:a aac -b:a 128k -tag:v hvc1 out.mp4

  

# 4K HDR (HDR10)

ffmpeg -i in.4k.mp4 \

  -c:v libx265 -crf 22 -preset slow \

  -x265-params "hdr-opt=1:repeat-headers=1:colorprim=bt2020:transfer=smpte2084:\

colormatrix=bt2020nc:master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(40000000,50)" \

  -c:a aac -b:a 192k out.mp4

  

# -tag:v hvc1 needed for Apple device compatibility

```

  

### VP9 (`libvpx-vp9`)

  

Royalty-free, excellent for YouTube uploads and WebM delivery.

  

```bash

# Two-pass VP9 (best quality/size ratio)

ffmpeg -i in.mp4 -c:v libvpx-vp9 -b:v 0 -crf 33 -pass 1 \

  -an -f webm /dev/null

ffmpeg -i in.mp4 -c:v libvpx-vp9 -b:v 0 -crf 33 -pass 2 \

  -c:a libopus -b:a 128k out.webm

  

# Constrained bitrate VP9

ffmpeg -i in.mp4 -c:v libvpx-vp9 -b:v 2M -maxrate 3M -bufsize 6M \

  -c:a libopus -b:a 128k out.webm

  

# cpu-used: 0=slowest/best, 5=fastest for real-time

ffmpeg -i in.mp4 -c:v libvpx-vp9 -crf 33 -b:v 0 -cpu-used 2 \

  -c:a libopus out.webm

```

  

### AV1 (`libaom-av1`, `libsvtav1`)

  

Best compression ratio. libaom is the reference encoder (slow); SVT-AV1 is much faster.

  

```bash

# libaom-av1 (high quality, slow)

ffmpeg -i in.mp4 -c:v libaom-av1 -crf 35 -cpu-used 4 \

  -c:a libopus -b:a 128k out.mkv

  

# SVT-AV1 (much faster, nearly same quality)

ffmpeg -i in.mp4 -c:v libsvtav1 -crf 35 -preset 6 \

  -c:a libopus -b:a 128k out.mkv

  

# Two-pass SVT-AV1

ffmpeg -i in.mp4 -c:v libsvtav1 -b:v 2M -pass 1 -an -f null /dev/null

ffmpeg -i in.mp4 -c:v libsvtav1 -b:v 2M -pass 2 -c:a libopus out.mkv

```

  

### Apple ProRes (`prores_ks`)

  

Intermediate codec for post-production workflows. Intra-frame only (no long GOP).

  

```bash

# ProRes 422 HQ (editing master)

ffmpeg -i in.mp4 -c:v prores_ks -profile:v 3 \

  -c:a pcm_s16le out.mov

  

# ProRes 4444 (with alpha channel)

ffmpeg -i in.mp4 -c:v prores_ks -profile:v 4 \

  -c:a pcm_s24le out.mov

```

  

**ProRes Profiles:**

  

| Profile | `-profile:v` | Bitrate (1080p30) | Use Case |

|---------|-------------|-------------------|----------|

| ProRes 422 Proxy | 0 | ~45 Mbps | Offline editing proxy |

| ProRes 422 LT | 1 | ~102 Mbps | Light editing |

| ProRes 422 | 2 | ~147 Mbps | Standard post |

| ProRes 422 HQ | 3 | ~220 Mbps | High-quality master |

| ProRes 4444 | 4 | ~330 Mbps | With alpha, VFX |

| ProRes 4444 XQ | 5 | ~500 Mbps | Ultra quality |

  

### FFV1 (`ffv1`)

  

Lossless codec for archiving. Used by libraries and broadcasters.

  

```bash

# FFV1 lossless (level 3 = multithreaded, error detection)

ffmpeg -i in.mp4 -c:v ffv1 -level 3 -threads 8 \

  -coder 1 -context 1 -g 1 -slicecrc 1 \

  -c:a flac out.mkv

```

  

### Audio Codec Guide

  

| Codec | Encoder | Bitrate Range | Lossy? | Use Case |

|-------|---------|---------------|--------|----------|

| AAC | `aac` / `libfdk_aac` | 96–320k | Yes | Universal delivery |

| MP3 | `libmp3lame` | 64–320k | Yes | Legacy compatibility |

| Opus | `libopus` | 32–256k | Yes | Web, WebM, excellent at low bitrates |

| Vorbis | `libvorbis` | 80–500k | Yes | OGG containers |

| FLAC | `flac` | ~1000k | No | Lossless archive |

| PCM | `pcm_s16le` / `pcm_s24le` | ~1411k+ | No | Broadcast, editing |

  

```bash

# AAC (standard)

ffmpeg -i in.mp4 -c:a aac -b:a 192k out.mp4

  

# AAC (libfdk_aac — higher quality if available)

ffmpeg -i in.mp4 -c:a libfdk_aac -b:a 192k -vbr 4 out.mp4

  

# MP3

ffmpeg -i in.mp4 -c:a libmp3lame -b:a 320k -q:a 2 out.mp3

  

# Opus (excellent at 96k)

ffmpeg -i in.mp4 -c:a libopus -b:a 96k out.webm

  

# FLAC lossless

ffmpeg -i in.wav -c:a flac out.flac

```

  

### Container Format Guide

  

| Extension | Common Codecs | Notes |

|-----------|--------------|-------|

| `.mp4` | H.264, H.265, AAC | Universal, web-friendly |

| `.mov` | ProRes, H.264, PCM | macOS/iOS, Apple ecosystem |

| `.mkv` | Any | Open format, multiple streams |

| `.webm` | VP8/VP9/AV1, Opus/Vorbis | Web, HTML5 |

| `.ts` | H.264, AAC | Broadcast, HLS segments |

| `.mxf` | XDCAM, DNxHD | Broadcast, professional |

  

### Bitrate Target Reference (H.264)

  

| Resolution | Frame Rate | Streaming | Good Quality | Archive |

|-----------|------------|-----------|--------------|---------|

| 480p | 30 | 500k–1M | 1–2M | 3M |

| 720p | 30 | 1–2.5M | 2.5–4M | 6M |

| 1080p | 30 | 3–5M | 5–8M | 12M |

| 1080p | 60 | 4.5–7M | 7–12M | 16M |

| 4K | 30 | 15–25M | 25–35M | 50M |

| 4K | 60 | 20–35M | 35–50M | 70M |

  

---

  

## 32. xfade Transition Reference

  

Complete reference for all `xfade` filter transition types.

  

### Syntax

  

```bash

ffmpeg -i clip_a.mp4 -i clip_b.mp4 \

  -filter_complex \

    "[0:v][1:v]xfade=transition=TRANSITION_NAME:duration=DURATION:offset=OFFSET[v]; \

     [0:a][1:a]acrossfade=d=DURATION[a]" \

  -map "[v]" -map "[a]" \

  -c:v libx264 -crf 23 -preset slow \

  -c:a aac output.mp4

```

  

**Parameters:**

- `transition` — Transition effect name (see table below)

- `duration` — Overlap duration in seconds (e.g. `1.0`)

- `offset` — Time in clip A when transition starts (seconds from beginning)

  

**Tip:** `offset` = clip A duration − transition duration

  

### All Transition Types

  

| Name | Visual Description |

|------|--------------------|

| `fade` | Classic fade through black |

| `fadeblack` | Fade to black then in from black |

| `fadewhite` | Fade to white then in from white |

| `fadegrays` | Fade through grayscale |

| `dissolve` | Standard cross-dissolve blend |

| `pixelize` | Pixelate out, then pixelate in |

| `wipeleft` | Wipe from right to left |

| `wiperight` | Wipe from left to right |

| `wipeup` | Wipe from bottom to top |

| `wipedown` | Wipe from top to bottom |

| `slideleft` | Clip B slides in from right |

| `slideright` | Clip B slides in from left |

| `slideup` | Clip B slides in from bottom |

| `slidedown` | Clip B slides in from top |

| `smoothleft` | Smooth edge wipe left |

| `smoothright` | Smooth edge wipe right |

| `smoothup` | Smooth edge wipe up |

| `smoothdown` | Smooth edge wipe down |

| `circlecrop` | Crop to expanding/contracting circle |

| `rectcrop` | Crop to expanding/contracting rectangle |

| `circleopen` | Circle iris opens from center |

| `circleclose` | Circle iris closes to center |

| `vertopen` | Vertical split opens outward |

| `vertclose` | Vertical split closes inward |

| `horzopen` | Horizontal split opens outward |

| `horzclose` | Horizontal split closes inward |

| `zoomin` | Zoom in (clip B scales up) |

| `squeezev` | Vertical squeeze transition |

| `squeezeh` | Horizontal squeeze transition |

| `hlwind` | Horizontal left wind/swipe |

| `hrwind` | Horizontal right wind/swipe |

| `vuwind` | Vertical up wind/swipe |

| `vdwind` | Vertical down wind/swipe |

| `coverleft` | Clip B covers from right to left |

| `coverright` | Clip B covers from left to right |

| `coverup` | Clip B covers from bottom to top |

| `coverdown` | Clip B covers from top to bottom |

| `revealleft` | Clip A reveals from right to left |

| `revealright` | Clip A reveals from left to right |

| `revealup` | Clip A reveals from bottom to top |

| `revealdown` | Clip A reveals from top to bottom |

| `diagtl` | Diagonal wipe top-left |

| `diagtr` | Diagonal wipe top-right |

| `diagbl` | Diagonal wipe bottom-left |

| `diagbr` | Diagonal wipe bottom-right |

  

### Quick Examples

  

**Dissolve**

  

```bash

ffmpeg -i a.mp4 -i b.mp4 \

  -filter_complex "[0:v][1:v]xfade=transition=dissolve:duration=1:offset=5[v];\

[0:a][1:a]acrossfade=d=1[a]" \

  -map "[v]" -map "[a]" out.mp4

```

  

**Fade to Black**

  

```bash

ffmpeg -i a.mp4 -i b.mp4 \

  -filter_complex "[0:v][1:v]xfade=transition=fadeblack:duration=1.5:offset=8[v];\

[0:a][1:a]acrossfade=d=1.5[a]" \

  -map "[v]" -map "[a]" out.mp4

```

  

**Wipe Left**

  

```bash

ffmpeg -i a.mp4 -i b.mp4 \

  -filter_complex "[0:v][1:v]xfade=transition=wipeleft:duration=0.5:offset=10[v];\

[0:a][1:a]acrossfade=d=0.5[a]" \

  -map "[v]" -map "[a]" out.mp4

```

  

**Slide Left**

  

```bash

ffmpeg -i a.mp4 -i b.mp4 \

  -filter_complex "[0:v][1:v]xfade=transition=slideleft:duration=0.8:offset=4[v];\

[0:a][1:a]acrossfade=d=0.8[a]" \

  -map "[v]" -map "[a]" out.mp4

```

  

**Circle Open (Iris)**

  

```bash

ffmpeg -i a.mp4 -i b.mp4 \

  -filter_complex "[0:v][1:v]xfade=transition=circleopen:duration=1:offset=6[v];\

[0:a][1:a]acrossfade=d=1[a]" \

  -map "[v]" -map "[a]" out.mp4

```

  

**Zoom In**

  

```bash

ffmpeg -i a.mp4 -i b.mp4 \

  -filter_complex "[0:v][1:v]xfade=transition=zoomin:duration=0.6:offset=9[v];\

[0:a][1:a]acrossfade=d=0.6[a]" \

  -map "[v]" -map "[a]" out.mp4

```

  

**Pixelize**

  

```bash

ffmpeg -i a.mp4 -i b.mp4 \

  -filter_complex "[0:v][1:v]xfade=transition=pixelize:duration=1:offset=7[v];\

[0:a][1:a]acrossfade=d=1[a]" \

  -map "[v]" -map "[a]" out.mp4

```

  

**Diagonal (diagbr)**

  

```bash

ffmpeg -i a.mp4 -i b.mp4 \

  -filter_complex "[0:v][1:v]xfade=transition=diagbr:duration=0.8:offset=5[v];\

[0:a][1:a]acrossfade=d=0.8[a]" \

  -map "[v]" -map "[a]" out.mp4

```

  

### Multi-Clip Transition Chain

  

Chain multiple transitions across three or more clips:

  

```bash

# Two transitions: A→B (dissolve at 5s), B→C (wipeleft at 10s)

# Clip A: 6s, Clip B: 6s, Clip C: 6s

ffmpeg -i a.mp4 -i b.mp4 -i c.mp4 \

  -filter_complex "

    [0:v][1:v]xfade=transition=dissolve:duration=1:offset=5[ab];

    [ab][2:v]xfade=transition=wipeleft:duration=1:offset=10[v];

    [0:a][1:a]acrossfade=d=1[ab_a];

    [ab_a][2:a]acrossfade=d=1[a]

  " \

  -map "[v]" -map "[a]" \

  -c:v libx264 -crf 23 out.mp4

```

  

### Calculating Offset for Multi-Clip Chains

  

For N clips with transition duration T:

  

- Offset for transition 1: `clip1_duration - T`

- Offset for transition 2: `clip1_duration + clip2_duration - 2*T`

- Offset for transition N: `sum(clip1..N_durations) - N*T`

  

### Batch Helper (bash)

  

```bash

#!/usr/bin/env bash

# Build xfade chain from array of clips

CLIPS=("a.mp4" "b.mp4" "c.mp4" "d.mp4")

TRANS="dissolve"

DUR=1.0

OFFSET=0

FILTER=""

INPUTS=""

LAST_LABEL=""

  

for i in "${!CLIPS[@]}"; do

  INPUTS+="-i ${CLIPS[$i]} "

  if [[ $i -gt 0 ]]; then

    if [[ $i -eq 1 ]]; then

      PREV="[0:v]"

    else

      PREV="[v$((i-1))]"

    fi

    CLIP_DUR=$(ffprobe -v error -show_entries format=duration \

      -of default=noprint_wrappers=1:nokey=1 "${CLIPS[$((i-1))]}")

    OFFSET=$(echo "$OFFSET + $CLIP_DUR - $DUR" | bc)

    # Final clip uses [vout]; intermediate clips use [v1], [v2], etc.

    if [[ $i -eq $(( ${#CLIPS[@]} - 1 )) ]]; then

      OUT="[vout]"

    else

      OUT="[v$i]"

    fi

    LAST_LABEL="$OUT"

    FILTER+="${PREV}[$((i)):v]xfade=transition=${TRANS}:duration=${DUR}:offset=${OFFSET}${OUT}; "

  fi

done

  

echo "Inputs: $INPUTS"

echo "Filter: $FILTER"

echo "Map with: -map \"$LAST_LABEL\""

# Full command example:

# ffmpeg $INPUTS -filter_complex "$FILTER" -map "$LAST_LABEL" -c:v libx264 -crf 23 out.mp4

```

  

### Audio: acrossfade Parameters

  

```

acrossfade=d=duration[:c1=curve_in][:c2=curve_out]

```

  

| Curve | Description |

|-------|-------------|

| `tri` | Linear (default) |

| `qsin` | Quarter sine wave |

| `hsin` | Half sine wave |

| `esin` | Exponential sine |

| `log` | Logarithmic |

| `ipar` | Inverted parabola |

| `exp` | Exponential |

| `iqsin` | Inverted quarter sine |

  

```bash

# Smooth logarithmic audio crossfade

[0:a][1:a]acrossfade=d=1:c1=log:c2=log[a]

```

  

---

  

## 33. Hardware Acceleration Guide

  

GPU-accelerated encoding and decoding for faster processing.

  

### Detection

  

**Check available hardware encoders:**

  

```bash

ffmpeg -encoders 2>/dev/null | grep -E "nvenc|qsv|amf|videotoolbox|vaapi|v4l2"

```

  

**Check available hardware decoders:**

  

```bash

ffmpeg -decoders 2>/dev/null | grep -E "cuvid|qsv|vaapi|videotoolbox"

```

  

**List hwaccels:**

  

```bash

ffmpeg -hwaccels

```

  

**Detect NVIDIA GPU:**

  

```bash

nvidia-smi -L                         # Linux/Windows

nvidia-smi --query-gpu=name,driver_version --format=csv

```

  

**Detect Intel QSV (Linux):**

  

```bash

vainfo 2>/dev/null | grep -i "va_profile"

ls /dev/dri/renderD*

```

  

**Detect AMD GPU (Linux):**

  

```bash

rocminfo 2>/dev/null | grep -i "Device Type"

```

  

**Detect Apple VideoToolbox:**

  

```bash

ffmpeg -encoders 2>/dev/null | grep videotoolbox

```

  

### NVIDIA NVENC

  

Requires: NVIDIA GPU (Maxwell+), CUDA drivers, FFmpeg built with `--enable-cuda-nvcc` or `--enable-nvenc`.

  

**H.264 NVENC:**

  

```bash

# Basic H.264 NVENC

ffmpeg -i input.mp4 -c:v h264_nvenc -preset p4 -cq 23 -c:a aac output.mp4

  

# With hardware decode (zero-copy GPU pipeline)

ffmpeg -hwaccel cuda -hwaccel_output_format cuda \

  -i input.mp4 \

  -c:v h264_nvenc -preset p4 -cq 23 \

  -c:a aac -b:a 128k output.mp4

  

# Constrained bitrate (streaming)

ffmpeg -hwaccel cuda -i input.mp4 \

  -c:v h264_nvenc -preset p4 \

  -b:v 4M -maxrate 5M -bufsize 10M \

  -c:a aac -b:a 128k output.mp4

```

  

#### H.265 NVENC:

  

```bash

ffmpeg -hwaccel cuda -hwaccel_output_format cuda \

  -i input.mp4 \

  -c:v hevc_nvenc -preset p4 -cq 26 \

  -tag:v hvc1 \

  -c:a aac -b:a 128k output.mp4

```

  

#### AV1 NVENC (Ada Lovelace / RTX 40 series+):

  

```bash

ffmpeg -hwaccel cuda -i input.mp4 \

  -c:v av1_nvenc -preset p4 -cq 30 \

  -c:a libopus -b:a 128k output.mkv

```

  

#### NVENC Preset Reference

  

| Preset | Speed | Quality | Use Case |

|--------|-------|---------|----------|

| `p1` | Fastest | Lowest | Real-time, low latency |

| `p2` | Very fast | Low | Live streaming |

| `p3` | Fast | Medium | |

| `p4` | Medium | Good | General encoding |

| `p5` | Slow | Better | |

| `p6` | Slower | High | Archive |

| `p7` | Slowest | Highest | Offline quality |

  

#### NVENC Quality Modes

  

| Flag | Description |

|------|-------------|

| `-cq N` | Constant quality (0=best, 51=worst; like CRF) |

| `-b:v N` | Target bitrate |

| `-rc vbr` | Variable bitrate mode |

| `-rc cbr` | Constant bitrate (streaming) |

  

#### GPU Scaling Filter (stays on GPU)

  

```bash

ffmpeg -hwaccel cuda -hwaccel_output_format cuda \

  -i input.mp4 \

  -vf "scale_cuda=1920:1080" \

  -c:v h264_nvenc -preset p4 -cq 23 output.mp4

```

  

#### Full GPU Pipeline (decode + filter + encode)

  

```bash

ffmpeg \

  -hwaccel cuda -hwaccel_output_format cuda \

  -i input.mp4 \

  -vf "scale_cuda=1280:720,hwdownload,format=nv12" \

  -c:v h264_nvenc -preset p4 -cq 23 \

  -c:a aac -b:a 128k output.mp4

```

  

### Intel QuickSync (QSV)

  

Requires: Intel CPU with integrated GPU (Broadwell+), `intel-media-driver` or `libva-intel-driver`.

  

#### H.264 QSV

  

```bash

# Software decode + QSV encode

ffmpeg -i input.mp4 -c:v h264_qsv -global_quality 23 -preset slow \

  -c:a aac -b:a 128k output.mp4

  

# Hardware decode + encode

ffmpeg -hwaccel qsv -c:v h264_qsv -i input.mp4 \

  -c:v h264_qsv -global_quality 23 -preset slow output.mp4

```

  

#### H.265 QSV

  

```bash

ffmpeg -i input.mp4 -c:v hevc_qsv -global_quality 26 -preset slow \

  -c:a aac -b:a 128k output.mp4

```

  

#### AV1 QSV (Intel Arc / 12th gen+)

  

```bash

ffmpeg -i input.mp4 -c:v av1_qsv -global_quality 35 -preset slow \

  -c:a libopus output.mkv

```

  

#### QSV Presets

  

`veryfast` → `faster` → `fast` → `medium` → `slow` → `slower` → `veryslow`

  

#### QSV Scaling

  

```bash

ffmpeg -hwaccel qsv -i input.mp4 \

  -vf "scale_qsv=1280:720" \

  -c:v h264_qsv -global_quality 23 output.mp4

```

  

### AMD AMF (Advanced Media Framework)

  

Requires: AMD GPU (GCN+), AMF SDK, Windows or Linux with AMDGPU-Pro.

  

#### H.264 AMF

  

```bash

ffmpeg -i input.mp4 -c:v h264_amf -quality quality \

  -b:v 4M -c:a aac -b:a 128k output.mp4

```

  

#### H.265 AMF

  

```bash

ffmpeg -i input.mp4 -c:v hevc_amf -quality quality \

  -b:v 3M -c:a aac -b:a 128k output.mp4

```

  

#### AMF Quality Modes

  

| `-quality` | Description |

|-----------|-------------|

| `speed` | Fastest, lowest quality |

| `balanced` | Balanced (default) |

| `quality` | Slowest, highest quality |

  

### Apple VideoToolbox (macOS / iOS)

  

Requires: macOS 10.8+. Uses Apple Silicon Neural Engine or AMD GPU.

  

#### H.264 VideoToolbox

  

```bash

ffmpeg -i input.mp4 -c:v h264_videotoolbox -q:v 65 \

  -c:a aac -b:a 128k output.mp4

  

# Constrained bitrate

ffmpeg -i input.mp4 -c:v h264_videotoolbox -b:v 4M \

  -c:a aac -b:a 128k output.mp4

```

  

#### H.265 / HEVC VideoToolbox

  

```bash

ffmpeg -i input.mp4 -c:v hevc_videotoolbox -q:v 60 \

  -tag:v hvc1 -c:a aac -b:a 128k output.mp4

```

  

#### ProRes VideoToolbox

  

```bash

ffmpeg -i input.mp4 -c:v prores_videotoolbox -profile:v 3 \

  -c:a pcm_s16le output.mov

```

  

#### VideoToolbox Quality Scale

  

`-q:v` range: 1–100 (higher = better quality, larger file)

  

| Value | Approximate Quality |

|-------|---------------------|

| 40 | Low (proxy) |

| 55 | Medium |

| 65 | Good (default delivery) |

| 75 | High quality |

| 85 | Very high |

| 100 | Near-lossless |

  

### VAAPI (VA-API — Linux)

  

Requires: Linux, Mesa or Intel/AMD driver with VA-API support (`libva`).

  

#### Setup Check

  

```bash

vainfo                             # Check VA-API support

ls /dev/dri/renderD128             # Default render device

```

  

#### H.264 VAAPI

  

```bash

ffmpeg -vaapi_device /dev/dri/renderD128 \

  -i input.mp4 \

  -vf "format=nv12,hwupload" \

  -c:v h264_vaapi -qp 23 output.mp4

```

  

#### H.265 VAAPI

  

```bash

ffmpeg -vaapi_device /dev/dri/renderD128 \

  -i input.mp4 \

  -vf "format=nv12,hwupload" \

  -c:v hevc_vaapi -qp 26 output.mp4

```

  

#### VAAPI with HW Decode

  

```bash

ffmpeg -hwaccel vaapi -hwaccel_device /dev/dri/renderD128 \

  -hwaccel_output_format vaapi \

  -i input.mp4 \

  -vf "scale_vaapi=1280:720" \

  -c:v h264_vaapi -qp 23 output.mp4

```

  

#### VAAPI Scaling Filter

  

```bash

-vf "scale_vaapi=W:H"

```

  

### V4L2 (Video4Linux — Raspberry Pi, embedded)

  

```bash

# Raspberry Pi H.264 hardware encoder

ffmpeg -i input.mp4 -c:v h264_v4l2m2m -b:v 4M output.mp4

```

  

### Performance Comparison

  

| Method | 1080p H.264 Speed | Notes |

|--------|------------------|-------|

| `libx264` slow | ~80–150 fps | Best quality/compression |

| `libx264` ultrafast | ~400–800 fps | Larger files |

| `h264_nvenc` (RTX 3080) | ~800–1500 fps | Near-realtime |

| `h264_qsv` (i7) | ~300–600 fps | Moderate quality |

| `hevc_videotoolbox` (M1) | ~600–1000 fps | Excellent for Apple |

| `h264_vaapi` | ~200–500 fps | Driver dependent |

  

### Fallback Pattern (Try HW, Fall Back to SW)

  

```bash

#!/usr/bin/env bash

# Try NVENC first, fall back to libx264

encode() {

  local IN="$1" OUT="$2"

  if ffmpeg -hwaccels 2>/dev/null | grep -q cuda; then

    ffmpeg -hwaccel cuda -i "$IN" -c:v h264_nvenc -preset p4 -cq 23 \

      -c:a aac -b:a 128k "$OUT" && return

  fi

  ffmpeg -i "$IN" -c:v libx264 -crf 23 -preset slow \

    -c:a aac -b:a 128k "$OUT"

}

  

encode input.mp4 output.mp4

```

  

---

  

## 34. Helper Scripts

  

### `scripts/probe.sh` — full source behavior

  

A ffprobe wrapper with modes:

  

- `-j, --json` — full JSON output via `ffprobe -show_streams -show_format -of json`

- `-v, --video` — video stream info (index, codec_name, codec_long_name, profile, width, height, r_frame_rate, avg_frame_rate, pix_fmt, bit_rate, nb_frames, duration)

- `-a, --audio` — audio stream info (index, codec_name, codec_long_name, sample_rate, channels, channel_layout, bit_rate, duration, nb_frames)

- `-f, --format` — format/container info (filename, nb_streams, format_name, format_long_name, duration, size, bit_rate)

- `-s, --streams` — all streams summary (index, codec_type, codec_name, width, height, sample_rate, channels, bit_rate, duration)

- default — human-friendly summary (container, video, audio sections)

  

Errors handled: missing input arg, missing file, missing ffprobe, unknown options.

  

### `scripts/normalize-audio.sh` (full workflow)

  

Two-pass EBU R128 normalization:

  

1. **Pass 1 (analysis):** runs `loudnorm=I=<target>:TP=<target>:LRA=<target>:print_format=json` with `-vn -f null -`, captures stderr, extracts the JSON block with `awk`, parses `input_i`, `input_tp`, `input_lra`, `input_thresh`, and `target_offset` with grep/sed (no jq dependency).

2. **Pass 2 (apply):** runs `loudnorm=...:measured_I=...:measured_TP=...:measured_LRA=...:measured_thresh=...:offset=...:linear=true:print_format=summary`, output audio with `-c:a <acodec> -b:a <abitrate>` (default aac/192k).

3. Optional prefix filters: `aformat=channel_layouts=stereo` (`-s`) and `aresample=<hz>` (`-r`).

4. Video handling: `-c:v copy` if the input has video and `--no-video` is not set; otherwise `-vn`.

5. Adds `-movflags +faststart` for `.mp4`/`.m4v` outputs.

6. After completion, verifies output levels with a quick analysis run grepping `Input Integrated|Input True Peak|Input LRA`.

  

Error handling: missing args/files/ffmpeg, pass-1 failures, `-inf` measurements (silent file).

  

### `scripts/concat.sh` (full source behavior)

  

- Uses concat demuxer with `-f concat -safe 0 -i <list>`; list entries are absolute paths with single-quote escaping (`realpath` + escape).

- **Stream-copy mode** (default): `-c copy`, no quality loss, requires matching codecs/params across inputs.

- **Re-encode mode** (`-r`): `-c:v <vcodec> -crf <crf> -preset <preset> -c:a <acodec> -b:a 128k`; adds `-movflags +faststart` for `.mp4`/`.m4v`.

- Temp list file cleaned up via `trap` unless `-k` given.

- Final ffprobe summary of duration/size/bitrate printed.

  

---

  

## 35. Audio Normalization Presets

  

From `assets/preset-profiles.json`:

  

| Preset | Description | I (LUFS) | TP (dBTP) | LRA (LU) |

|--------|-------------|----------|-----------|----------|

| `ebu_r128_broadcast` | EBU R128 standard for broadcast | -23 | -1.0 | 18 |

| `ebu_r128_streaming` | EBU R128 for online streaming platforms | -16 | -1.5 | 11 |

| `youtube` | YouTube target (normalizes to -14 LUFS after upload) | -14 | -1.0 | 11 |

| `apple_podcasts` | Apple Podcasts loudness standard | -16 | -1.0 | 11 |

  

---

