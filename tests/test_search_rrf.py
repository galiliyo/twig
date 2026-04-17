from core.db import _rrf_merge


def test_rrf_items_in_both_lists_rank_higher():
    sem = [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}, {"id": 3, "title": "C"}]
    fuzz = [{"id": 2, "title": "B"}, {"id": 4, "title": "D"}, {"id": 1, "title": "A"}]
    results = _rrf_merge(sem, fuzz, top_k=4)
    ids = [r["id"] for r in results]
    assert set(ids[:2]) == {1, 2}
    assert len(results) == 4


def test_rrf_score_field_present():
    sem = [{"id": 1, "title": "A"}]
    fuzz = [{"id": 1, "title": "A"}]
    results = _rrf_merge(sem, fuzz, top_k=1)
    assert "score" in results[0]
    assert results[0]["score"] > 0
