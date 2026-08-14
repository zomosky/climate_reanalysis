"""冷端路径拼接——归档（``archive.py``）与需要冷端回退的单元（``restore.py``、
``refresh.py``）共用的唯一真值，不该各自拼路径。

单独成一个不依赖任何单元的模块，是为了理顺依赖方向：``restore.py``（单元 B）
和 ``refresh.py`` 都需要按 basename 去冷端找文件，但它们不该反过来 import
``archive.py``（单元 C）——那会把"谁依赖谁"的方向搞反（B/刷新单元依赖归档
单元的实现细节，而归档只是把已经确认固化的原始文件挪个位置，概念上更下游）。
"""

from __future__ import annotations

from pathlib import Path


def cold_path(cold_dir: Path, month: str, name: str) -> Path:
    """冷端某个原始文件应落盘的位置——布局与 ``raw_dir/<month>/<name>`` 同构，
    只是根换成冷盘。
    """
    return Path(cold_dir) / month / name
