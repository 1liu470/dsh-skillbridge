"""skillbridge —— Hermes 与 DeepSeek Harness 的技能互转工具。

两侧共用 ~/.agents/skills，本包负责把技能写成对方能直接读取的形式。
"""

__version__ = "1.0.0"

from .cli import main  # noqa: F401

__all__ = ["main", "__version__"]
