import random
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.frame_analysis import (
    FRAME_ANALYSIS_PROMPT,
    FrameAnalysisSettings,
    OllamaConfig,
    analyse_frame,
    append_sections,
    build_ollama_client,
    build_prompt,
    format_section,
    format_timestamp_hms,
    is_config_valid,
    load_ollama_config,
)
from src.state import Frame


# --- OllamaConfig + env loading ---------------------------------------------

def test_load_ollama_config_reads_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_API_KEY", "secret")
    monkeypatch.setenv("OLLAMA_VISION_MODEL", "gemma3")
    cfg = load_ollama_config()
    assert cfg == OllamaConfig("http://localhost:11434", "secret", "gemma3")


def test_load_ollama_config_strips_whitespace(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "  http://localhost  ")
    monkeypatch.setenv("OLLAMA_API_KEY", "  key  ")
    monkeypatch.setenv("OLLAMA_VISION_MODEL", "  gemma3  ")
    cfg = load_ollama_config()
    assert cfg.host == "http://localhost"
    assert cfg.api_key == "key"
    assert cfg.model == "gemma3"


def test_load_ollama_config_missing_vars(monkeypatch):
    for v in ("OLLAMA_HOST", "OLLAMA_API_KEY", "OLLAMA_VISION_MODEL"):
        monkeypatch.delenv(v, raising=False)
    cfg = load_ollama_config()
    assert cfg.host == ""
    assert cfg.api_key == ""
    assert cfg.model == ""


# --- is_config_valid --------------------------------------------------------

def test_is_config_valid_ok():
    cfg = OllamaConfig("http://localhost:11434", "", "gemma3")
    ok, msg = is_config_valid(cfg)
    assert ok is True
    assert msg == ""


def test_is_config_valid_ok_with_api_key():
    cfg = OllamaConfig("https://ollama.com", "key", "gemma3")
    ok, _ = is_config_valid(cfg)
    assert ok is True


def test_is_config_valid_missing_host():
    cfg = OllamaConfig("", "key", "gemma3")
    ok, msg = is_config_valid(cfg)
    assert ok is False
    assert "OLLAMA_HOST" in msg


def test_is_config_valid_missing_model():
    cfg = OllamaConfig("http://localhost:11434", "", "")
    ok, msg = is_config_valid(cfg)
    assert ok is False
    assert "OLLAMA_VISION_MODEL" in msg


def test_is_config_valid_missing_both():
    cfg = OllamaConfig("", "", "")
    ok, msg = is_config_valid(cfg)
    assert ok is False
    assert "OLLAMA_HOST" in msg
    assert "OLLAMA_VISION_MODEL" in msg


def test_is_config_valid_uses_loaded_env_when_no_arg(monkeypatch):
    for v in ("OLLAMA_HOST", "OLLAMA_API_KEY", "OLLAMA_VISION_MODEL"):
        monkeypatch.delenv(v, raising=False)
    ok, msg = is_config_valid()
    assert ok is False
    assert "OLLAMA_HOST" in msg


# --- build_ollama_client ----------------------------------------------------

def test_build_ollama_client_with_api_key():
    captured = {}
    class FakeClient:
        def __init__(self, host=None, headers=None, **kw):
            captured["host"] = host
            captured["headers"] = headers
    with patch("ollama.Client", FakeClient):
        cfg = OllamaConfig("https://ollama.com", "tok123", "gemma3")
        build_ollama_client(cfg)
    assert captured["host"] == "https://ollama.com"
    assert captured["headers"] == {"Authorization": "Bearer tok123"}


def test_build_ollama_client_without_api_key_no_auth_header():
    captured = {}
    class FakeClient:
        def __init__(self, host=None, headers=None, **kw):
            captured["host"] = host
            captured["headers"] = headers
    with patch("ollama.Client", FakeClient):
        cfg = OllamaConfig("http://localhost:11434", "", "gemma3")
        build_ollama_client(cfg)
    # Local Ollama (no key) -> no auth header; pass headers=None so the SDK
    # uses its own defaults.
    assert captured["host"] == "http://localhost:11434"
    assert captured["headers"] is None


# --- FrameAnalysisSettings --------------------------------------------------

def test_frame_analysis_settings_defaults():
    s = FrameAnalysisSettings()
    assert s.concurrency == 1


def test_frame_analysis_settings_roundtrip():
    s = FrameAnalysisSettings(concurrency=4)
    d = s.to_dict()
    assert d == {"concurrency": 4}
    s2 = FrameAnalysisSettings.from_dict(d)
    assert s2 == s


def test_frame_analysis_settings_ignores_unknown_keys():
    s = FrameAnalysisSettings.from_dict({"concurrency": 2, "bogus": "x"})
    assert s.concurrency == 2
    assert not hasattr(s, "bogus")


# --- build_prompt -----------------------------------------------------------

def test_build_prompt_substitutes_all_placeholders():
    out = build_prompt(
        project_ctx="Project about birds.",
        video_ctx="A nature walk in the park.",
        video_filename="ClipA.mp4",
        timestamp="00:01:23.470",
        frame_number=23,
    )
    assert "{{PROJECT_CONTEXT}}" not in out
    assert "{{VIDEO_CONTEXT}}" not in out
    assert "{{VIDEO_FILENAME}}" not in out
    assert "{{TIMESTAMP}}" not in out
    assert "{{FRAME_NUMBER}}" not in out
    assert "Project about birds." in out
    assert "A nature walk in the park." in out
    assert "ClipA.mp4" in out
    assert "00:01:23.470" in out
    assert "23" in out


def test_build_prompt_empty_project_context_uses_placeholder():
    out = build_prompt("", "video ctx", "v.mp4", "00:00:01.000", 1)
    assert "_No project context provided._" in out


def test_build_prompt_whitespace_only_context_uses_placeholder():
    out = build_prompt("   \n  ", "  \n", "v.mp4", "00:00:01.000", 1)
    assert "_No project context provided._" in out
    assert "_No video context provided._" in out


def test_build_prompt_preserves_template_structure():
    # The header section and the structured return format must survive.
    out = build_prompt("p", "v", "v.mp4", "00:00:01.000", 1)
    assert "## Project Context" in out
    assert "## Video Context" in out
    assert "## Frame Information" in out
    assert "Scene:" in out
    assert "Uncertainty:" in out


def test_frame_analysis_prompt_template_constant_unmodified():
    # Spot-check that the constant contains the expected anchors.
    assert "## Project Context" in FRAME_ANALYSIS_PROMPT
    assert "{{PROJECT_CONTEXT}}" in FRAME_ANALYSIS_PROMPT
    assert "Return a structured analysis using this format:" in FRAME_ANALYSIS_PROMPT


# --- format_timestamp_hms ---------------------------------------------------

@pytest.mark.parametrize("pts,expected", [
    (0.0, "00:00:00.000"),
    (0.47, "00:00:00.470"),
    (1.0, "00:00:01.000"),
    (83.47, "00:01:23.470"),
    (3723.5, "01:02:03.500"),
    (-5.0, "00:00:00.000"),
])
def test_format_timestamp_hms(pts, expected):
    assert format_timestamp_hms(pts) == expected


# --- format_section ---------------------------------------------------------

def _make_frame(pts=12.34, idx=5, filename="ClipA-00-00-12-340-0005.jpg",
                stem="ClipA", path="/tmp/ClipA-00-00-12-340-0005.jpg"):
    return Frame(
        path=path, filename=filename, video_path=f"/tmp/{stem}.mp4",
        video_stem=stem, pts_time=pts, index=idx, strategy="fps_2s",
    )


def test_format_section_includes_header_and_body():
    f = _make_frame()
    sec = format_section(f, "Scene:\nA forest.")
    assert "## Frame ClipA-00-00-12-340-0005.jpg — 00:00:12.340 (#5)" in sec
    assert "Scene:\nA forest." in sec


def test_format_section_empty_text_uses_placeholder():
    f = _make_frame()
    sec = format_section(f, "   ")
    assert "_(no response)_" in sec


def test_format_section_strips_trailing_whitespace_from_text():
    f = _make_frame()
    sec = format_section(f, "Scene:\nA forest.\n\n")
    assert sec.endswith("A forest.")


# --- append_sections --------------------------------------------------------

def test_append_sections_empty_existing():
    out = append_sections("", "2026-08-19T10:00:00", ["## Frame A", "body A"])
    # No leading blank line when existing content is empty.
    assert out.startswith("## Run — 2026-08-19T10:00:00\n\n## Frame A")
    assert out.endswith("body A\n")


def test_append_sections_with_existing_content():
    existing = "# Frame Analysis\n\n## Run — earlier\n\n## Frame 0\n\nbody 0"
    out = append_sections(existing, "2026-08-19T10:00:00",
                          ["## Frame 1\n\nbody 1"])
    assert out.startswith("# Frame Analysis")
    assert "## Run — earlier" in out
    assert "## Run — 2026-08-19T10:00:00" in out
    assert "## Frame 1" in out
    assert "body 1" in out
    # Existing content should come first, then the new run.
    assert out.index("## Run — earlier") < out.index("## Run — 2026-08-19T10:00:00")


def test_append_sections_handles_missing_trailing_newline_on_existing():
    existing = "# Frame Analysis\n\n## Run — earlier\n\n## Frame 0\n\nbody 0"
    # existing ends with no newline
    out = append_sections(existing, "ts", ["## Frame 1\n\nbody 1"])
    # Exactly one blank line between existing and the new run header.
    assert "body 0\n\n## Run — ts" in out


def test_append_sections_multiple_new_sections():
    out = append_sections("", "ts", [
        "## Frame A\n\nbody A",
        "## Frame B\n\nbody B",
    ])
    assert "## Frame A" in out
    assert "## Frame B" in out
    assert out.index("## Frame A") < out.index("## Frame B")


# --- analyse_frame ----------------------------------------------------------

def test_analyse_frame_calls_client_chat_with_image_and_temperature_zero():
    captured = {}

    class FakeMessage:
        def __init__(self, content):
            self.content = content

    class FakeResponse:
        def __init__(self, content):
            self.message = FakeMessage(content)

    class FakeClient:
        def chat(self, model, messages, options=None, **kw):
            captured["model"] = model
            captured["messages"] = messages
            captured["options"] = options
            return FakeResponse("Scene:\nA forest.")

    f = _make_frame()
    out = analyse_frame(
        FakeClient(), "gemma3", f.path,
        "project ctx", "video ctx", "ClipA.mp4", f,
    )
    assert out == "Scene:\nA forest."
    assert captured["model"] == "gemma3"
    assert captured["options"] == {"temperature": 0}
    msg = captured["messages"][0]
    assert msg["role"] == "user"
    assert msg["images"] == [f.path]
    assert "ClipA.mp4" in msg["content"]
    assert "00:00:12.340" in msg["content"]
    assert "Frame number: 5" in msg["content"]


def test_analyse_frame_empty_response_returns_empty_string():
    class FakeMessage:
        content = None  # missing content

    class FakeResponse:
        message = FakeMessage()

    class FakeClient:
        def chat(self, **kw):
            return FakeResponse()

    f = _make_frame()
    out = analyse_frame(FakeClient(), "gemma3", f.path, "", "", "v.mp4", f)
    assert out == ""


# --- Ordering with concurrency ----------------------------------------------

def test_concurrent_submission_preserves_chronological_order_in_output():
    """Sections must be collected in submission (chronological) order, not
    completion order, even when the worker pool runs concurrently."""
    from src.frame_analysis import format_section

    # A fake analyse_frame that sleeps a random short duration so completion
    # order is non-deterministic.
    real_rng = random.Random(0)

    def fake_analyse_frame(client, model, frame_path, project_ctx, video_ctx,
                            video_filename, frame):
        time.sleep(real_rng.uniform(0.001, 0.02))
        return f"result for {frame.index}"

    frames = [_make_frame(pts=i, idx=i, filename=f"f{i}.jpg") for i in range(8)]

    sections: list[str] = []
    with patch("src.frame_analysis.analyse_frame", fake_analyse_frame):
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(fake_analyse_frame, None, "m", f.path,
                                    "", "", "v.mp4", f) for f in frames]
            for i, fut in enumerate(futures):
                text = fut.result()
                sections.append(format_section(frames[i], text))

    # Verify the resulting sections are in frame-index order (submission order).
    indices_in_order = [
        int(sec.split("(#")[1].split(")")[0]) for sec in sections
    ]
    assert indices_in_order == list(range(8))


# --- Worker integration: ContextStore write + run header --------------------

def test_worker_writes_append_section_to_context_store(tmp_path, monkeypatch):
    """End-to-end worker test with a mocked Ollama client."""
    from src.context import ContextStore, ContextType
    from src.workers.frame_analysis_worker import FrameAnalysisWorker

    # Provide config so the worker proceeds past validation.
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_API_KEY", "")
    monkeypatch.setenv("OLLAMA_VISION_MODEL", "gemma3")

    # Mock the ollama.Client used inside build_ollama_client.
    class FakeClient:
        def __init__(self, host=None, headers=None, **kw):
            self.host = host
            self.headers = headers
        def chat(self, model, messages, options=None, **kw):
            return SimpleNamespace(
                message=SimpleNamespace(content=f"Scene:\nFrame {messages[0]['images'][0]}")
            )
    import ollama
    monkeypatch.setattr(ollama, "Client", FakeClient)

    # Build a temp working folder with frames + an existing frame_analysis.md.
    ctx_dir = tmp_path / "context"
    ctx_dir.mkdir()
    store = ContextStore(ctx_dir)
    existing = "# Frame Analysis\n\n## Run — earlier\n\n## Frame old\n\nold body"
    store.save("ClipA", ContextType.FRAME_ANALYSIS, existing)

    frames = [_make_frame(pts=i, idx=i, filename=f"ClipA-00-00-0{i}-000-000{i}.jpg",
                          stem="ClipA") for i in range(3)]
    # Pre-create the frame image files so the worker's existence checks pass
    # (the fake client doesn't actually read them, but the worker passes
    # the path to ollama which we're mocking).
    for f in frames:
        Path(f.path).parent.mkdir(parents=True, exist_ok=True)
        Path(f.path).write_bytes(b"\xff\xd8\xff\xe0dummy")  # JPEG-ish stub

    settings = FrameAnalysisSettings(concurrency=2)
    worker = FrameAnalysisWorker(frames, settings, "", {}, store)

    # Capture signals synchronously.
    received_progress = []
    received_log = []
    received_finished = [None]

    worker.progress.connect(lambda d, t, m: received_progress.append((d, t, m)))
    worker.log.connect(lambda m: received_log.append(m))
    worker.finished_all.connect(lambda failed: received_finished.__setitem__(0, failed))

    # Run the worker synchronously on this thread (skip the QThread event loop).
    worker.run()

    assert received_finished[0] is False  # no failures

    # The context file should now contain the existing run + the new run header
    # + 3 frame sections in chronological order.
    doc = store.get("ClipA", ContextType.FRAME_ANALYSIS)
    assert doc is not None
    content = doc.content
    assert "## Run — earlier" in content
    assert "## Run — " in content  # the new run header (with current timestamp)
    # The 3 new frames should appear in chronological order.
    for i in range(3):
        assert f"ClipA-00-00-0{i}-000-000{i}.jpg" in content
    # Verify order: earlier run comes before the new run.
    assert content.index("## Run — earlier") < content.rindex("## Run — ")
    # New frame sections should appear after the new run header.
    new_run_pos = content.rindex("## Run — ")
    for i in range(3):
        assert content.index(f"ClipA-00-00-0{i}-000-000{i}.jpg") > new_run_pos


def test_worker_skips_when_no_frames(tmp_path, monkeypatch):
    from src.context import ContextStore
    from src.workers.frame_analysis_worker import FrameAnalysisWorker

    store = ContextStore(tmp_path / "context")
    worker = FrameAnalysisWorker([], FrameAnalysisSettings(), "", {}, store)
    received_finished = [None]
    worker.finished_all.connect(lambda failed: received_finished.__setitem__(0, failed))
    worker.run()
    assert received_finished[0] is False


def test_worker_fails_fast_on_missing_config(tmp_path, monkeypatch):
    from src.context import ContextStore
    from src.workers.frame_analysis_worker import FrameAnalysisWorker

    for v in ("OLLAMA_HOST", "OLLAMA_API_KEY", "OLLAMA_VISION_MODEL"):
        monkeypatch.delenv(v, raising=False)

    store = ContextStore(tmp_path / "context")
    frames = [_make_frame(idx=0)]
    worker = FrameAnalysisWorker(frames, FrameAnalysisSettings(), "", {}, store)
    received_finished = [None]
    received_log: list[str] = []
    worker.finished_all.connect(lambda failed: received_finished.__setitem__(0, failed))
    worker.log.connect(lambda m: received_log.append(m))
    worker.run()
    assert received_finished[0] is True  # any_failed
    assert any("OLLAMA" in m for m in received_log)


# --- State integration ------------------------------------------------------

def test_state_persists_and_loads_frame_analysis_settings(tmp_path):
    from src.state import PipelineState

    state = PipelineState()
    state.set_working_folder(str(tmp_path))
    state.set_frame_analysis_settings(FrameAnalysisSettings(concurrency=5))

    # Load into a fresh state without re-triggering a default persist via
    # set_working_folder (which would overwrite the file with defaults).
    state2 = PipelineState()
    state2._working_folder = str(tmp_path)
    state2.load()
    assert state2.frame_analysis_settings.concurrency == 5