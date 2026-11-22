# -*- coding: utf-8 -*-
"""任务2（2026-11-22 零 API 工作包）：伪造网络生命周期管理。

--internal Docker 网络（mta-fakenet）——容器"有网的感觉、零真实外发"的底座：
- internal 网=网内容器互访（DNS 按容器名解析=桩可达的机制）+ 外部出网被 Docker 阻断
- 幂等：网络存在即复用（NotFound → create --internal）
- 任务结束不删网络（共享资源），容器随任务清理（runner.cleanup 同款）

用法：
  python -I scripts/ensure_fake_net.py           # 确保 mta-fakenet 存在（幂等）
  python -I scripts/ensure_fake_net.py --status  # 查看网络与挂载容器
  python -I scripts/ensure_fake_net.py --verify-isolation
      # 隔离实证：起临时容器挂 internal 网，外发必须失败（留档 stdout）
坑（交接文档）：Windows Docker Desktop 网络创建用 docker-py API 别 shell out；
internal 网的 DNS=容器名互相可解析，别用 IP 硬编码。
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

NET_NAME = "mta-fakenet"


def ensure_net(client) -> str:
    """幂等确保 internal 网存在。返回 'existing' | 'created'。"""
    import docker.errors
    try:
        client.networks.get(NET_NAME)
        return "existing"
    except docker.errors.NotFound:
        client.networks.create(NET_NAME, driver="bridge", internal=True,
                               labels={"mta": "fakenet"})
        return "created"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--verify-isolation", action="store_true",
                    help="隔离实证：internal 网内外发必须失败（证据留档）")
    args = ap.parse_args()
    import docker
    client = docker.from_env()

    state = ensure_net(client)
    print(f"[fakenet] {NET_NAME}: {state}")

    if args.status:
        net = client.networks.get(NET_NAME)
        info = {"name": NET_NAME, "internal": net.attrs.get("Internal"),
                "containers": list((net.attrs.get("Containers") or {}).values())}
        print(json.dumps({k: (v if k != "containers" else
                              [c.get("Name") for c in v])
                          for k, v in info.items()}, ensure_ascii=False))

    if args.verify_isolation:
        # 隔离实证（交接文档验收）：internal 网内容器外发被拒。
        # curlimages/curl 若无则用 py312 镜像 python urllib（不外拉=离线优先）
        img = "mta-test:py312"
        probe = ("import urllib.request,sys\n"
                 "try:\n"
                 "    urllib.request.urlopen('http://example.com', timeout=8)\n"
                 "    print('LEAK: external reachable')\n"
                 "except Exception as e:\n"
                 "    print(f'ISOLATED: {type(e).__name__}: {str(e)[:80]}')\n")
        out = client.containers.run(img, ["python", "-c", probe],
                                    name=f"mta-fakenet-verify-{abs(hash(probe))%9999}",
                                    network=NET_NAME, remove=True,
                                    detach=False)
        print("[verify]", out.decode("utf-8", errors="replace").strip())
        ok = b"ISOLATED" in out
        print("[result]", "PASS-隔离开启" if ok else "FAIL-外发可达（隔离未生效！）")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
