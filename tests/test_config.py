"""pyedit.toml discovery: layers, the parent pointer, loud schema errors."""

import pytest

from pyedit import config


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_no_config_anywhere(project):
    assert config.load(project).formatters == {}


def test_config_home_layer(project, config_home):
    write(config_home / "pyedit.toml", '[format]\nnix = ["nixfmt", "-"]\n')
    assert config.load(project).formatters == {"nix": ["nixfmt", "-"]}


def test_session_root_layer(project):
    write(project / "pyedit.toml", '[format]\npy = ["ruff", "format", "-"]\n')
    assert config.load(project).formatters == {"py": ["ruff", "format", "-"]}


def test_root_overrides_config_home(project, config_home):
    write(config_home / "pyedit.toml", '[format]\nnix = ["a"]\npy = ["b"]\n')
    write(project / "pyedit.toml", '[format]\npy = ["c"]\n')
    assert config.load(project).formatters == {"nix": ["a"], "py": ["c"]}


def test_parent_provides_defaults_root_overrides(project):
    write(project.parent / "umbrella.toml", '[format]\nnix = ["a"]\npy = ["b"]\n')
    write(project / "pyedit.toml", 'parent = "../umbrella.toml"\n[format]\npy = ["c"]\n')
    assert config.load(project).formatters == {"nix": ["a"], "py": ["c"]}


def test_parent_chain_follows_upwards(project):
    write(project.parent / "top.toml", '[format]\nnix = ["a"]\n')
    write(project.parent / "mid.toml", 'parent = "top.toml"\n[format]\npy = ["b"]\n')
    write(project / "pyedit.toml", 'parent = "../mid.toml"\n')
    assert config.load(project).formatters == {"nix": ["a"], "py": ["b"]}


def test_config_home_layer_also_follows_parent(project, config_home):
    write(config_home / "shared.toml", '[format]\nnix = ["a"]\n')
    write(config_home / "pyedit.toml", 'parent = "shared.toml"\n')
    assert config.load(project).formatters == {"nix": ["a"]}


def test_missing_parent_target_is_loud(project):
    write(project / "pyedit.toml", 'parent = "../absent.toml"\n')
    with pytest.raises(config.ConfigError, match="absent.toml"):
        config.load(project)


def test_parent_cycle_is_loud(project):
    write(project / "a.toml", 'parent = "b.toml"\n[format]\nnix = ["x"]\n')
    write(project / "b.toml", 'parent = "a.toml"\n[format]\nnix = ["y"]\n')
    write(project / "pyedit.toml", 'parent = "b.toml"\n')
    with pytest.raises(config.ConfigError, match="cycle"):
        config.load(project)


def test_self_pointer_is_a_cycle(project):
    write(project / "pyedit.toml", 'parent = "pyedit.toml"\n')
    with pytest.raises(config.ConfigError, match="cycle"):
        config.load(project)


def test_parent_must_be_a_string(project):
    write(project / "pyedit.toml", "parent = 3\n")
    with pytest.raises(config.ConfigError, match="parent"):
        config.load(project)


def test_invalid_toml_is_loud(project):
    write(project / "pyedit.toml", "[format\n")
    with pytest.raises(config.ConfigError, match="TOML"):
        config.load(project)


def test_unknown_key_is_loud(project):
    write(project / "pyedit.toml", 'formater = "typo"\n')
    with pytest.raises(config.ConfigError, match="formater"):
        config.load(project)


def test_format_value_must_be_an_argv_list(project):
    write(project / "pyedit.toml", '[format]\npy = "ruff"\n')
    with pytest.raises(config.ConfigError, match=r"\[format\].py"):
        config.load(project)


def test_empty_command_is_loud(project):
    write(project / "pyedit.toml", "[format]\npy = []\n")
    with pytest.raises(config.ConfigError, match=r"\[format\].py"):
        config.load(project)


def test_command_words_must_be_strings(project):
    write(project / "pyedit.toml", "[format]\npy = [1]\n")
    with pytest.raises(config.ConfigError, match=r"\[format\].py"):
        config.load(project)


def test_suffix_dots_are_normalized(project):
    write(project / "pyedit.toml", '[format]\n".py" = ["ruff", "format", "-"]\n')
    assert config.load(project).formatters == {"py": ["ruff", "format", "-"]}


def test_empty_suffix_is_loud(project):
    write(project / "pyedit.toml", '[format]\n"." = ["ruff"]\n')
    with pytest.raises(config.ConfigError, match="suffix"):
        config.load(project)


def test_config_file_uses_platformdirs(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "pyedit.config.user_config_dir", lambda *args, **kwargs: str(tmp_path)
    )
    assert config.config_file() == tmp_path / "pyedit.toml"
