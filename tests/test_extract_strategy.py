from src.ffmpeg.extract import (
    STRATEGY_FPS_2S,
    STRATEGY_FPS_5S,
    STRATEGY_FPS_10S,
    STRATEGY_KEYFRAME,
    STRATEGY_SCENE,
    STRATEGY_THUMBNAIL,
    SUBSAMPLE_CAP,
    SUBSAMPLE_TARGET,
    decision_from_strategy,
    even_subsample_indices,
    fallback_for,
    select_dynamic_strategy,
    select_fixed_strategy,
    select_strategy,
)
from src.ffmpeg.extract import _scale_filter
from src.state import ExtractSettings


def test_dynamic_short_video_uses_fps_2s():
    d = select_dynamic_strategy(30.0, 30.0)
    assert d.strategy == STRATEGY_FPS_2S
    assert "fps=1/2" in d.command_filter


def test_dynamic_one_to_ten_min_uses_scene():
    d = select_dynamic_strategy(300.0, 30.0)
    assert d.strategy == STRATEGY_SCENE
    assert "scene" in d.command_filter
    assert d.needs_vsync_vfr is True


def test_dynamic_ten_to_thirty_min_uses_keyframe():
    d = select_dynamic_strategy(900.0, 30.0)
    assert d.strategy == STRATEGY_KEYFRAME
    assert "-skip_frame" in d.extra_input_args
    assert d.needs_vsync_vfr is True


def test_dynamic_thirty_plus_uses_thumbnail():
    d = select_dynamic_strategy(3600.0, 30.0)
    assert d.strategy == STRATEGY_THUMBNAIL
    assert "thumbnail=" in d.command_filter


def test_dynamic_boundary_60s_is_fps_2s():
    # <=60s -> fps_2s
    assert select_dynamic_strategy(60.0, 30.0).strategy == STRATEGY_FPS_2S


def test_dynamic_boundary_600s_is_scene():
    # <=600s -> scene
    assert select_dynamic_strategy(600.0, 30.0).strategy == STRATEGY_SCENE


def test_dynamic_zero_duration_defaults_to_short():
    d = select_dynamic_strategy(0.0, 30.0)
    assert d.strategy == STRATEGY_FPS_2S


def test_fixed_quick_targets_30():
    d = select_fixed_strategy("quick", 60.0, 0)
    assert d.target_count == 30
    assert "fps=1/" in d.command_filter


def test_fixed_standard_targets_60():
    d = select_fixed_strategy("standard", 60.0, 0)
    assert d.target_count == 60


def test_fixed_detailed_targets_80():
    d = select_fixed_strategy("detailed", 60.0, 0)
    assert d.target_count == 80


def test_fixed_custom_uses_custom_count():
    d = select_fixed_strategy("custom", 60.0, 42)
    assert d.target_count == 42


def test_select_strategy_dynamic():
    s = ExtractSettings()
    d = select_strategy(s.mode, 30.0, 30.0, 0)
    assert d.strategy == STRATEGY_FPS_2S


def test_select_strategy_fixed():
    s = ExtractSettings(mode="quick")
    d = select_strategy(s.mode, 30.0, 30.0, 0)
    assert d.target_count == 30


def test_fallback_chain_scene_to_keyframe():
    assert fallback_for(STRATEGY_SCENE) == STRATEGY_KEYFRAME
    assert fallback_for(STRATEGY_KEYFRAME) == STRATEGY_THUMBNAIL
    assert fallback_for(STRATEGY_THUMBNAIL) == STRATEGY_FPS_5S
    assert fallback_for(STRATEGY_FPS_5S) == STRATEGY_FPS_10S
    assert fallback_for(STRATEGY_FPS_10S) is None


def test_decision_from_strategy_fps_5s():
    d = decision_from_strategy(STRATEGY_FPS_5S, 300.0, 30.0)
    assert d.strategy == STRATEGY_FPS_5S
    assert "fps=1/5" in d.command_filter


def test_decision_from_strategy_fps_10s():
    d = decision_from_strategy(STRATEGY_FPS_10S, 300.0, 30.0)
    assert d.strategy == STRATEGY_FPS_10S
    assert "fps=1/10" in d.command_filter


def test_decision_from_strategy_keyframe_has_skip_frame():
    d = decision_from_strategy(STRATEGY_KEYFRAME, 300.0, 30.0)
    assert "-skip_frame" in d.extra_input_args


def test_scale_filter_present():
    assert "scale=" in _scale_filter()


def test_subsample_constants():
    assert SUBSAMPLE_CAP == 100
    assert SUBSAMPLE_TARGET == 80


def test_subsample_to_80():
    idxs = even_subsample_indices(120, 80)
    assert len(idxs) == 80
    assert idxs == sorted(idxs)