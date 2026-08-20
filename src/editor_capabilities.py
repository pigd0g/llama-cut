"""Plain-English description of the downstream video editor's capabilities.

This is the single source of truth used by the storyboard builder so it can
produce a storyboard that is practical to implement. It is intentionally
written in plain English with NO tool names, function names, or technical
operation names — the storyboard must read as a creative document.

The editor (Stage 8) consumes the storyboard and executes it via a set of
controlled tools. By giving the storyboard visibility of what the editor can
practically do (and the constraints it must respect), the storyboard stays
grounded in what is actually achievable.

Constraints are stated explicitly so the storyboard does not propose edits
the editor cannot fulfil.
"""

# --- Capabilities (plain English) -------------------------------------------

CAPABILITIES = """\
## Editor Capabilities

The storyboard you produce will be executed by an automated video editor. \
To keep the storyboard practical to implement, it must stay within the \
editor's capabilities described below.

The storyboard itself must be written in plain English. Never reference \
tools, function names, or technical operations. Describe the creative \
intent — what the audience should see and hear — not how the editor \
achieves it technically.

### What the editor can do

- **Select footage by exact source and time range.** Any contiguous \
segment of any source video can be used. Sources are referenced by their \
exact filename.
- **Reorder and sequence.** Clips can be arranged in any order to build \
the narrative. Each clip is one contiguous segment from one source.
- **Retiming.** A clip can be sped up or slowed down.
- **Framing.** A clip can be cropped, scaled, or reframed to a target \
aspect ratio (for example 16:9 or 9:16).
- **Color grading.** Brightness, contrast, saturation, and gamma can be \
adjusted per clip.
- **Audio per clip.** Volume can be changed, and fade-in / fade-out can \
be applied. Overall loudness can be normalized.
- **Background music.** Audio files (e.g. .mp3, .wav, .m4a) placed in the \
project folder can be mixed in as background music. Reference them by their \
exact filename. The available music files are listed in the provided context \
under "Available Music Files".
- **Transitions between two clips.** The supported transitions are: \
hard cut, dissolve (cross-fade), fade-to-black, fade-to-white, \
fade-to-gray, wipes (left, right, up, down), slides (left, right, up, \
down), circle open, circle close, and zoom-in.
- **Assembly.** Clips and transitions are assembled in order into a single \
continuous timeline.
- **Render.** The final video can be rendered at 1080p or 4K, in H.264. \
A fast low-quality preview can be rendered before the final high-quality \
output.

### Constraints the storyboard must respect

- **Never invent footage.** Only footage present in the provided source \
videos may be used. If a needed shot is missing, identify the gap rather \
than inventing it.
- **One clip = one contiguous segment from one source.** A single clip \
cannot blend multiple sources or jump around within a file; use multiple \
clips and transitions instead.
- **Transitions are between exactly two clips.** A transition always \
sits between the end of one clip and the start of the next.
- **Source filenames must be exact.** Reference source videos by their \
exact filename as provided in the context.
- **Timestamps must be real.** Any timestamp you reference must fall \
within the actual duration of the source video, as given in its technical \
metadata.

### Guidance

- Prefer specific, achievable shots over elaborate ones the editor \
cannot realise.
- When recommending a moment, reference the source filename and an \
approximate timestamp range where the editor should look.
- Match the creative treatment to what the available footage actually \
supports.
"""


def capabilities_block() -> str:
    """Return the capabilities block for injection into storyboard prompts."""
    return CAPABILITIES.strip()