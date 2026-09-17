import random

from src.picking import POLICIES, normalize, pick


def candidate(backend_id: int, score: float, excluded: bool = False) -> dict:
    return {"backend_id": backend_id, "name": f"b{backend_id}", "score": score, "excluded": excluded}


class TestNormalize:
    def test_strips_trailing_slash(self):
        assert normalize("http://svc:8000/") == "http://svc:8000"

    def test_leaves_clean_url_alone(self):
        assert normalize("http://svc:8000") == "http://svc:8000"


class TestPoliciesSet:
    def test_matches_control_plane_vocabulary(self):
        assert POLICIES == {
            "weighted_random",
            "least_latency",
            "round_robin",
            "session_sticky",
        }


class TestDeterministicPolicies:
    def test_returns_top_ranked(self):
        ranked = [candidate(2, 2.5), candidate(1, 0.9)]
        for policy in ("least_latency", "round_robin", "session_sticky"):
            assert pick(ranked, policy)["backend_id"] == 2

    def test_skips_excluded_even_if_highest(self):
        ranked = [candidate(2, 9.9, excluded=True), candidate(1, 0.9)]
        assert pick(ranked, "least_latency")["backend_id"] == 1


class TestEmptyAndBlocked:
    def test_empty_candidates_returns_none(self):
        assert pick([], "weighted_random") is None

    def test_all_excluded_returns_none(self):
        ranked = [candidate(1, 1.0, excluded=True), candidate(2, 2.0, excluded=True)]
        assert pick(ranked, "weighted_random") is None

    def test_excluded_candidates_are_never_sampled(self):
        ranked = [candidate(9, 100.0, excluded=True), candidate(1, 1.0)]
        for _ in range(50):
            assert pick(ranked, "weighted_random")["backend_id"] == 1


class TestWeightedRandom:
    def test_zero_total_score_falls_back_to_top(self):
        ranked = [candidate(1, 0.0), candidate(2, 0.0)]
        assert pick(ranked, "weighted_random")["backend_id"] == 1

    def test_single_candidate_is_always_chosen(self):
        ranked = [candidate(7, 0.3)]
        for _ in range(20):
            assert pick(ranked, "weighted_random")["backend_id"] == 7

    def test_sampling_follows_score_proportion(self):
        random.seed(20260918)
        ranked = [candidate(1, 0.9), candidate(2, 0.1)]
        draws = [pick(ranked, "weighted_random")["backend_id"] for _ in range(2000)]
        share = draws.count(1) / len(draws)
        assert 0.85 < share < 0.95, share

    def test_three_way_shares_track_scores(self):
        random.seed(7)
        ranked = [candidate(1, 0.5), candidate(2, 0.3), candidate(3, 0.2)]
        draws = [pick(ranked, "weighted_random")["backend_id"] for _ in range(3000)]
        shares = {bid: draws.count(bid) / len(draws) for bid in (1, 2, 3)}
        assert 0.45 < shares[1] < 0.55, shares
        assert 0.25 < shares[2] < 0.35, shares
        assert 0.15 < shares[3] < 0.25, shares
