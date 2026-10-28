"""阅卷协议 decoy 分母修正单测（模块复盘 [P0-A] 协议部分）。
锁死语义：占位条目不进 recall 分母、老 ground_truth 无 decoy 字段时口径与旧版
逐字节一致、compute_metrics 不认 decoy/幻觉 id 的判决、recompute 幂等且保留原明细。
grade() 主路径依赖 LLM 仲裁与真实任务库，不在此测——指标口径全部由纯函数锁定。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from grade_corpus import compute_metrics, recompute, split_planted  # noqa: E402


def _d(id_, decoy=None, detectable="assertion"):
    d = {"id": id_, "detectable_by": detectable}
    if decoy is not None:
        d["decoy"] = decoy
    return d


def _gt(*entries):
    return {"corpus_id": "corpus-t", "planted_defects": list(entries)}


def test_split_planted_three_way():
    gt = _gt(_d("D1"), _d("D2", decoy=True), _d("D3", decoy=False),
             _d("D4", detectable="performance"))
    planted, decoys, miss = split_planted(gt)
    assert [d["id"] for d in planted] == ["D1", "D3"]
    assert [d["id"] for d in decoys] == ["D2"]
    assert [d["id"] for d in miss] == ["D4"]


def test_split_planted_legacy_no_decoy_field_unchanged():
    """老 ground_truth 无 decoy 字段：三分类退化为旧口径（全进分母）。"""
    gt = _gt(_d("D1"), _d("D2"), _d("D3", detectable="performance"))
    planted, decoys, miss = split_planted(gt)
    assert len(planted) == 2 and not decoys and len(miss) == 1


def test_compute_metrics_excludes_decoy_and_phantom_ids():
    matches = [
        {"id": "D1", "verdict": "detected"},
        {"id": "D2", "verdict": "detected"},  # decoy 误判 detected 也不计
        {"id": "D9", "verdict": "detected"},  # 幻觉 id 不计
        {"id": "D3", "verdict": "uncertain"},
    ]
    m = compute_metrics(1, "corpus-t", matches, [_d("D1"), _d("D3"), _d("D4")], 2, 1)
    assert m["planted_assertion"] == 3
    assert m["detected"] == 1 and m["uncertain"] == 1 and m["not"] == 1
    assert m["recall"] == round(1 / 3, 2)
    assert m["extra_suspect"] == 2 and m["expected_miss_performance"] == 1


def test_compute_metrics_empty_planted_recall_none():
    m = compute_metrics(1, "corpus-t", [], [], 0, 0)
    assert m["recall"] is None and m["planted_assertion"] == 0


def test_recompute_idempotent_and_preserves(tmp_path, monkeypatch):
    """recompute：分母剔除 decoy、两遍幂等、witness_note/detail/extra 原样保留。"""
    cdir = tmp_path / "data" / "corpus" / "corpus-t"
    cdir.mkdir(parents=True)
    gt = _gt(_d("D1"), _d("D2", decoy=True), _d("D3"))
    (cdir / "ground_truth.json").write_text(
        json.dumps(gt, ensure_ascii=False), encoding="utf-8")
    detail = {"matches": [
        {"id": "D1", "verdict": "detected", "by": "test_a", "reason": "x"},
        {"id": "D2", "verdict": "not", "by": "", "reason": "占位判 not 属正确"},
        {"id": "D3", "verdict": "not", "by": "", "reason": "y"},
    ], "extra_suspect": [{"case": "test_b", "reason": "z"}]}
    old = {"task_id": 7, "corpus_id": "corpus-t", "planted_assertion": 3,
           "detected": 1, "uncertain": 0, "not": 2, "recall": 0.33,
           "extra_suspect": 1, "expected_miss_performance": 0,
           "witness_note": "宿主重放", "detail": detail}
    (cdir / "grade_task7.json").write_text(
        json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")

    import grade_corpus
    monkeypatch.setattr(grade_corpus, "ROOT", tmp_path)
    first = recompute("corpus-t")
    assert len(first) == 1
    r1 = first[0]
    assert r1["planted_assertion"] == 2 and r1["decoy_excluded"] == 1
    assert r1["detected"] == 1 and r1["recall"] == 0.5
    assert r1["witness_note"] == "宿主重放"
    assert r1["detail"] == detail  # 原判决全量保留（D2 的 not 留作证据）
    assert r1["extra_suspect"] == 1

    second = recompute("corpus-t")
    assert second == first  # 幂等
