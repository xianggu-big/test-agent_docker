"""pytest 配置：把项目根目录加进 sys.path，使 test_agent 包可导入。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
