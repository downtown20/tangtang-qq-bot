from tools.prepare_michele_refs import (
    asr_agreement,
    build_segments,
    choose_shortlist,
    normalize_asr_text,
    parse_silences,
    pick_emotion,
    quality_flags,
)


def test_parse_silences_pairs_start_and_end_events():
    log = """
    [silencedetect] silence_start: 0
    [silencedetect] silence_end: 1.25 | silence_duration: 1.25
    [silencedetect] silence_start: 5.5
    [silencedetect] silence_end: 7.0 | silence_duration: 1.5
    """

    assert parse_silences(log) == [(0.0, 1.25), (5.5, 7.0)]


def test_parse_silences_ignores_unfinished_event():
    assert parse_silences("silence_start: 3.0") == []


def test_build_segments_keeps_padding_inside_adjacent_silence():
    segments = build_segments(
        12.0,
        [(0.0, 1.0), (5.0, 7.0), (11.0, 12.0)],
        min_duration=2.0,
        max_duration=10.0,
        padding=0.2,
    )

    assert segments == [(0.8, 5.2), (6.8, 11.2)]


def test_build_segments_filters_too_short_and_too_long_regions():
    segments = build_segments(
        20.0,
        [(0.0, 1.0), (2.0, 3.0), (16.0, 17.0)],
        min_duration=2.0,
        max_duration=10.0,
        padding=0.0,
    )

    assert segments == [(17.0, 20.0)]


def test_quality_flags_marks_only_measured_risks():
    metrics = {
        "duration": 2.5,
        "rms_dbfs": -18.0,
        "peak_dbfs": -0.05,
        "clipped_samples": 0,
        "silence_ratio": 0.1,
    }

    assert quality_flags(metrics) == ["nonideal_duration", "near_clipping"]


def test_asr_normalization_preserves_words_and_discards_punctuation():
    assert normalize_asr_text(" 啊，我没在开玩笑！ ") == "啊我没在开玩笑"
    assert asr_agreement("我是认真的。", "我是认真的") == 1.0


def test_pick_emotion_uses_chinese_label_and_highest_score():
    emotion, score, all_scores = pick_emotion(
        ["中立/neutral", "开心/happy"], [0.2, 0.8]
    )

    assert (emotion, score) == ("开心", 0.8)
    assert all_scores == {"中立": 0.2, "开心": 0.8}


def test_shortlist_excludes_flagged_candidates_and_ranks_by_emotion_score():
    manifest = [
        {"id": "good", "emotion": "开心", "emotion_score": 0.8, "asr_agreement": 1.0,
         "quality_flags": [], "asr_flags": []},
        {"id": "best", "emotion": "开心", "emotion_score": 0.9, "asr_agreement": 0.9,
         "quality_flags": [], "asr_flags": []},
        {"id": "flagged", "emotion": "开心", "emotion_score": 1.0, "asr_agreement": 1.0,
         "quality_flags": ["near_clipping"], "asr_flags": []},
    ]

    assert [item["id"] for item in choose_shortlist(manifest)["开心"]] == ["best", "good"]
