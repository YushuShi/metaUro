from pathlib import Path


def test_monthly_workflow_publishes_complete_update_overlay_with_deletions():
    workflow = (Path(__file__).resolve().parents[1]
                / ".github/workflows/monthly-source-refresh.yml").read_text()
    assert "for path in updates; do" in workflow
    assert 'git add -A -- "$path"' in workflow
