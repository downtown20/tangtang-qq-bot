from tools.记忆回滚验收 import run_offline_reverification_rollback


def test_offline_reverification_promotion_can_be_rolled_back(tmp_path):
    report = run_offline_reverification_rollback(tmp_path)

    assert report["passed"] is True
    assert report["backup_name"].endswith(".bak-20260903-H3")
    assert report["before"]["trust_level"] == "legacy_unverified"
    assert report["promoted"]["trust_level"] == "verified"
    assert report["rolled_back"]["trust_level"] == "legacy_unverified"
    assert report["trusted_visible"]["before"] is False
    assert report["trusted_visible"]["promoted"] is True
    assert report["trusted_visible"]["rolled_back"] is False
