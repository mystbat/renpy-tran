"""pytest 共享夹具（T0 骨架；各工作包可在此追加自己的 fixture）。

注意：夹具只放测试基础设施（临时目录、mock provider 等），
不得引入被测模块的业务逻辑。
"""
from __future__ import annotations

import pytest
from pathlib import Path


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    """标准工作目录布局（设计文档 §2.2）。"""
    for sub in ("bak", "output", "logs"):
        (tmp_path / sub).mkdir()
    return tmp_path
