"""test/ 目录下的所有测试用例都需 import 项目根的模块。

每个测试文件第一行:
    import _path   # noqa: F401

即可让 sys.path 包含项目根(本文件的父目录的父目录)。
"""
import os
import sys

# __file__ = .../test/_path.py
# dirname(__file__) = .../test/
# dirname(dirname(__file__)) = 项目根
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)