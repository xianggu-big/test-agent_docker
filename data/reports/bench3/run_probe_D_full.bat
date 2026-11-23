@echo off
cd /d "D:\test agent_docker\data\reports\bench3"
rem T4 探针 +D 全量 14 格（2026-11-23 12:49 低价窗；arm 命名空间+C1-skip 已根修）
rem 不用 timeout（无控制台窗口下静默炸的坑）；外部靠 13:50 硬停
set MTA_REPO_PROBE=1
set BENCH3_ROUND_TIMEOUT_S=90
set MTA_PER_TEST_TIMEOUT_S=5
python -X utf8 -I run_bench3.py --run --bench BugsInPy --arm-tag D > run_probe_D_full.log 2>&1
echo DONE > run_probe_D_full.DONE.flag
