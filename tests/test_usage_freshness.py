"""The gauge freshness states must stay distinguishable: live, cached-stale,
and missing-entirely. Labelling a no-data state as "cached, as of ?" sends
troubleshooting down the wrong path (there is nothing cached to be shown)."""
import pathlib
import sys

LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import gate  # noqa: E402


def test_no_usage_at_all_is_missing_not_stale():
    f = gate._usage_freshness(None)
    assert f["missing"] is True
    assert f["stale"] is False
    assert f["as_of"] is None


def test_served_usage_is_never_missing(monkeypatch):
    monkeypatch.setattr(gate.usage_api, "last_serve_info",
                        lambda: {"fetched_at": 1_000.0, "age": 30.0, "stale": True})
    f = gate._usage_freshness({"five_hour": {}})
    assert f["missing"] is False
    assert f["stale"] is True
