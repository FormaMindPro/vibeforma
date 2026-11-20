"""Dictionary: literal case-insensitive replacements and prompt terms."""
import dictionary


def test_replacement_ignores_case():
    text, count = dictionary.apply(
        "I use vibforma daily.", [{"from": "VibForma", "to": "VibeForma"}])
    assert text == "I use VibeForma daily."
    assert count == 1


def test_terms_respect_word_boundaries():
    text, count = dictionary.apply(
        "concatenate cat", [{"from": "cat", "to": "dog"}])
    assert text == "concatenate dog"
    assert count == 1


def test_longest_term_wins():
    entries = [{"from": "new york", "to": "NY"},
               {"from": "york", "to": "Y"}]
    text, count = dictionary.apply("new york and york", entries)
    assert text == "NY and Y"
    assert count == 2


def test_replacements_are_not_reprocessed():
    entries = [{"from": "foo", "to": "bar"}, {"from": "bar", "to": "baz"}]
    text, _count = dictionary.apply("foo", entries)
    assert text == "bar"


def test_empty_entries_keep_the_text():
    text, count = dictionary.apply("hello", [])
    assert text == "hello"
    assert count == 0


def test_entries_without_replacement_bias_only():
    entries = dictionary.normalize([{"from": "alpha", "to": ""}])
    assert entries == [{"from": "alpha", "to": ""}]
    text, count = dictionary.apply("alpha beta", entries)
    assert text == "alpha beta"
    assert count == 0


def test_normalize_drops_duplicates_and_invalid_items():
    entries = dictionary.normalize([
        {"from": "cat", "to": "dog"},
        {"from": "CAT", "to": "mouse"},
        {"from": "", "to": "x"},
        {"from": "same", "to": "same"},
        "not a pair",
    ])
    assert entries == [{"from": "cat", "to": "dog"}]


def test_normalize_caps_the_entry_list():
    raw = [{"from": f"term{i:03d}", "to": "x"} for i in range(510)]
    assert len(dictionary.normalize(raw)) == dictionary.MAX_ENTRIES


def test_hotwords_merge_without_duplicates():
    merged = dictionary.merge_hotwords(
        "alpha, Beta",
        [{"from": "gamma", "to": "G"}, {"from": "beta", "to": "B"}])
    assert merged == "alpha, Beta, gamma"


def test_hotwords_stay_within_the_prompt_cap():
    raw = [{"from": f"term{i:03d}", "to": ""} for i in range(200)]
    merged = dictionary.merge_hotwords("", raw)
    assert len(merged) <= dictionary.MAX_PROMPT


def test_json_roundtrip():
    entries = [{"from": "cat", "to": "dog"}]
    assert dictionary.from_json(dictionary.to_json(entries)) == entries


def test_json_accepts_pairs_and_wrapped_objects():
    assert dictionary.from_json('[["a", "b"]]') == [{"from": "a", "to": "b"}]
    wrapped = '{"dictionary": [{"from": "a", "to": "b"}]}'
    assert dictionary.from_json(wrapped) == [{"from": "a", "to": "b"}]


def test_json_rejects_malformed_payloads():
    for raw in ("not json", '{"dictionary": 1}', '"text"'):
        try:
            dictionary.from_json(raw)
        except ValueError:
            continue
        raise AssertionError(f"ValueError expected for {raw!r}")


def test_changes_report_counts_in_entry_order():
    entries = [{"from": "cat", "to": "dog"},
               {"from": "bird", "to": "fish"},
               {"from": "bias", "to": ""}]
    changes = dictionary.changes("cat CAT bird bias", entries)
    assert changes == [{"from": "cat", "to": "dog", "count": 2},
                       {"from": "bird", "to": "fish", "count": 1}]
