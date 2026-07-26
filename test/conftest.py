"""共享 fixtures：HTML 文件路径。"""
from pathlib import Path

FIX_DIR = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIX_DIR / name).read_text(encoding="utf-8")
