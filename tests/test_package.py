import importlib.util
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("package", ROOT / "infra" / "package.py")
package_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package_module)


def test_packages_the_function_app_with_posix_paths(tmp_path):
    target = tmp_path / "app.zip"
    names = package_module.package(ROOT / "functions", target)
    assert {"function_app.py", "host.json", "requirements.txt", "pipeline/processor.py"} <= set(names)
    assert all("\\" not in name and "__pycache__" not in name for name in names)
    with zipfile.ZipFile(target) as archive:
        assert sorted(archive.namelist()) == sorted(names)


def test_excludes_local_settings_and_caches(tmp_path):
    source = tmp_path / "app"
    (source / "pipeline" / "__pycache__").mkdir(parents=True)
    for name in ("function_app.py", "host.json", "requirements.txt", "local.settings.json"):
        (source / name).write_text("x")
    (source / "pipeline" / "__pycache__" / "x.cpython-312.pyc").write_text("x")
    (source / "pipeline" / "rules.py").write_text("x")
    names = package_module.package(source, tmp_path / "out.zip")
    assert sorted(names) == ["function_app.py", "host.json", "pipeline/rules.py", "requirements.txt"]


def test_refuses_a_directory_that_is_not_a_function_app(tmp_path):
    with pytest.raises(FileNotFoundError, match=r"function_app.py"):
        package_module.package(tmp_path, tmp_path / "out.zip")
