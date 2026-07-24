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


def test_translate_batches_receive_neighbor_context(monkeypatch):
    cues = [
        {
            "id": index,
            "time": "00:00:00,000 --> 00:00:01,000",
            "text": f"[S01] line {index}",
        }
        for index in range(1, 62)
    ]
    calls = []

    def fake_translate(
        batch,
        api_key,
        model,
        session,
        context_before,
        context_after,
    ):
        calls.append((batch, context_before, context_after))
        return {cue["id"]: cue["text"] for cue in batch}

    monkeypatch.setattr(translator, "_translate_batch", fake_translate)

    translator.translate_cues(cues, "key", batch_size=60)

    assert len(calls) == 2
    assert calls[0][1] == []
    assert [cue["id"] for cue in calls[0][2]] == [61]
    assert [cue["id"] for cue in calls[1][1]] == list(range(53, 61))
    assert calls[1][2] == []
