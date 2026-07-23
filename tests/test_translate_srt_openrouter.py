import translate_srt_openrouter as translator


def test_translate_preserves_speaker_and_time(monkeypatch):
    cues = [{
        "id": 1,
        "time": "00:00:00,000 --> 00:00:01,000",
        "text": "[S01] Hello",
    }]
    monkeypatch.setattr(
        translator,
        "_translate_batch",
        lambda batch, *_: {1: "[S99] 你好"},
    )
    translated = translator.translate_cues(cues, "key")
    assert translated == [{
        "id": 1,
        "time": "00:00:00,000 --> 00:00:01,000",
        "text": "[S01] 你好",
    }]
    assert cues[0]["text"] == "[S01] Hello"
