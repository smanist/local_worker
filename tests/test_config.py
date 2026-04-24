from pathlib import Path

import pytest

from ai_issue_worker.config import ConfigError, config_from_dict, load_config, write_default_config


def test_default_config_generation_works(tmp_path: Path):
    path = tmp_path / ".ai-issue-worker.yaml"
    write_default_config(path)
    config = load_config(path)
    assert config.repo == "owner/repo"
    assert config.issue_selection.ready_label == "ai-ready"


def test_config_missing_repo_fails_clearly():
    with pytest.raises(ConfigError, match="repo"):
        config_from_dict({"base_branch": "main"})


def test_init_refuses_overwrite_without_force(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="already exists"):
        write_default_config(path)

