"""Trashing or moving a folder that CONTAINS a blocked root carries the secrets
along: into the run trash, which gc later deletes."""
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIB = ROOT / "lib"
sys.path.insert(0, str(LIB))
import config as cfgmod  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    return {"off_limits_resolved": [str(tmp_path / "home/.ssh"),
                                    str(tmp_path / "home/.config/gh"),
                                    str(tmp_path / "home/code/secrets")]}


@pytest.mark.parametrize("path", ["home/.ssh", "home/.ssh/id_rsa", "home/.config/gh/hosts.yml"])
def test_the_root_and_what_is_inside_it(cfg, tmp_path, path):
    assert cfgmod.is_off_limits(tmp_path / path, cfg)


@pytest.mark.parametrize("path", ["home", "home/.config", "home/code", "."])
def test_a_folder_that_contains_a_root(cfg, tmp_path, path):
    assert cfgmod.is_off_limits(tmp_path / path, cfg)


def test_the_filesystem_root_contains_them_all(cfg):
    assert cfgmod.is_off_limits("/", cfg)


@pytest.mark.parametrize("path", [
    "home/code/project", "home/code/project/file.txt", "home/.config/other",
    "home/code/secrets-not", "home/.sshd", "elsewhere",
])
def test_siblings_stay_allowed(cfg, tmp_path, path):
    assert not cfgmod.is_off_limits(tmp_path / path, cfg)


def test_ml_fs_refuses_to_trash_a_parent_of_a_secret(tmp_path):
    """Through the real CLI, against the real always-off-limits list."""
    (tmp_path / ".config/gh").mkdir(parents=True)
    (tmp_path / ".config/gh/hosts.yml").write_text("token")
    r = _ml_fs(tmp_path, "trash", str(tmp_path / ".config"))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "off-limits" in r.stderr
    assert (tmp_path / ".config/gh/hosts.yml").exists()


def test_moving_a_file_into_a_folder_that_contains_a_root_is_fine(cfg, tmp_path):
    # Only what is moved has to be clear of a root; the folder it lands in need not be.
    assert not cfgmod.is_off_limits(tmp_path / "home/code", cfg, contains=False)
    assert cfgmod.is_off_limits(tmp_path / "home/.ssh/x", cfg, contains=False)


def _ml_fs(home, *args):
    run = home / "run"
    run.mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, str(LIB / "ml_fs.py"), *args],
        capture_output=True, text=True, timeout=20,
        env={"HOME": str(home), "ML_RUN_DIR": str(run), "PATH": "/usr/bin:/bin"})


def test_ml_fs_moves_a_file_into_such_a_folder(tmp_path):
    (tmp_path / "code/secrets").mkdir(parents=True)
    (tmp_path / "note.txt").write_text("x")
    r = _ml_fs(tmp_path, "move", str(tmp_path / "note.txt"), str(tmp_path / "code"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "code/note.txt").exists()


def test_ml_fs_refuses_to_move_a_parent_of_a_secret(tmp_path):
    (tmp_path / "code/secrets").mkdir(parents=True)
    (tmp_path / "code/secrets/key").write_text("x")
    r = _ml_fs(tmp_path, "move", str(tmp_path / "code"), str(tmp_path / "elsewhere"))
    assert r.returncode == 3, r.stdout + r.stderr
    assert (tmp_path / "code/secrets/key").exists()
