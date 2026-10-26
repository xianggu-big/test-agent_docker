"""镜像构建/自检脚本 —— "自动拉取容器"的入口（阶段 1）。

用法：
  python scripts/build_images.py            # 自检并补齐全部注册表镜像
  python scripts/build_images.py --only mta-test:py312
  python scripts/build_images.py --mirror   # 国内网络：pip 走清华源、maven 走阿里云

镜像设计约定见 docker/*/Dockerfile：只装工具链，被测依赖运行时装（依赖随项目走）。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根入 path，支持直跑

from test_agent import config
from test_agent.sandbox.runner import SandboxRunner

PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
MAVEN_MIRROR = "https://maven.aliyun.com/repository/public"


def main() -> int:
    ap = argparse.ArgumentParser(description="构建/自检 L4 执行镜像")
    ap.add_argument("--only", default=None, help="只处理指定镜像 tag")
    ap.add_argument("--mirror", action="store_true", help="国内网络镜像源")
    args = ap.parse_args()

    targets = [args.only] if args.only else list(config.IMAGE_BUILD_CTX)
    buildargs = ({}
                 if not args.mirror else
                 {"PIP_INDEX_URL": PIP_MIRROR, "MAVEN_MIRROR": MAVEN_MIRROR})

    runner = SandboxRunner()
    failed = []
    for image in targets:
        if runner.ensure_image(image, buildargs=buildargs):
            print(f"[OK]   {image}")
        else:
            print(f"[FAIL] {image}")
            failed.append(image)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
