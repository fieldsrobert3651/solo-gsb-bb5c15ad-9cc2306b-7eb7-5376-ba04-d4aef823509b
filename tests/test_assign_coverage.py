"""POST /assign-coverage 的端到端测试。"""

from __future__ import annotations

import random
from itertools import combinations, product

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _assignment_map(response: dict) -> dict[str, list[str]]:
    return {a["manuscript_id"]: a["reviewer_ids"] for a in response["assignments"]}


def _verify_coverage(body: dict, response: dict) -> dict[str, int]:
    """校验完整可行解：每稿人数、评审人 [min, capacity]、赛道限额、资格、确定性顺序。"""
    assert response["status"] == "feasible"
    assert response["assignments"] is not None
    assert response["conflicts"] is None
    assignments = _assignment_map(response)
    reviewers = {r["id"]: r for r in body["reviewers"]}
    default_req = body.get("required_reviewers", 1)

    # 输出顺序确定：稿件按 ID 排序、名单按 ID 排序
    assert list(assignments) == sorted(assignments)
    used = {rid: 0 for rid in reviewers}
    track_used: dict[str, dict[str, int]] = {rid: {} for rid in reviewers}
    for m in body["manuscripts"]:
        rids = assignments[m["id"]]
        assert rids == sorted(rids)
        req = m.get("required_reviewers")
        req = req if req is not None else default_req
        assert len(rids) == req, f"{m['id']} 评审数不足"
        assert len(set(rids)) == len(rids), "同一稿件重复分配评审人"
        for rid in rids:
            r = reviewers[rid]
            used[rid] += 1
            assert rid not in m.get("authors", []), "自稿冲突"
            assert not set(m.get("authors", [])) & set(r.get("conflicted_authors", []))
            assert m["id"] not in r.get("conflicted_manuscripts", [])
            if m.get("topics"):
                assert set(m["topics"]) & set(r["topics"]), "主题不匹配"
            if m.get("track") is not None:
                per = track_used[rid]
                per[m["track"]] = per.get(m["track"], 0) + 1
    for rid, n in used.items():
        r = reviewers[rid]
        assert r.get("min_assignments", 0) <= n <= r["capacity"], (
            f"{rid} 负载 {n} 不在 [{r.get('min_assignments', 0)}, {r['capacity']}] 内"
        )
        caps = r.get("track_capacities") or {}
        for track, k in track_used[rid].items():
            if track in caps:
                assert k <= caps[track], f"{rid} 赛道 {track} 超出限额"
    return used


# ---------- 基础可行 ----------

def test_feasible_without_min_behaves_like_assign():
    """全部 min_assignments 缺省（0）时，结果与 /assign 普通模式一致。"""
    body = {
        "required_reviewers": 1,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t1"], "required_reviewers": 1},
            {"id": "m2", "authors": [], "topics": ["t2"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t1", "t2"], "capacity": 1},
            {"id": "r2", "topics": ["t1"], "capacity": 1},
        ],
    }
    cov = client.post("/assign-coverage", json=body).json()
    plain = client.post("/assign", json=body).json()
    assert cov["status"] == plain["status"] == "feasible"
    assert cov["assignments"] == plain["assignments"]
    _verify_coverage(body, cov)


def test_min_assignment_forces_extra_reviewer():
    """无下限时两篇都会分给 r1；r2 声明 min=1 后必须至少分到一篇。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 1},
            {"id": "m2", "authors": [], "topics": ["t"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 2},
            {"id": "r2", "topics": ["t"], "capacity": 2, "min_assignments": 1},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    used = _verify_coverage(body, data)
    assert used == {"r1": 1, "r2": 1}


def test_min_equal_capacity_exact_fill():
    """下限之和恰好等于总需求：每位评审人的负载被唯一确定。"""
    body = {
        "manuscripts": [
            {"id": f"m{i}", "authors": [], "topics": ["t"], "required_reviewers": 1}
            for i in range(3)
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 2, "min_assignments": 2},
            {"id": "r2", "topics": ["t"], "capacity": 1, "min_assignments": 1},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    used = _verify_coverage(body, data)
    assert used == {"r1": 2, "r2": 1}
    assert data["total_required"] == data["total_assigned"] == 3


def test_zero_min_default():
    """min_assignments 缺省为 0：capacity=0 的评审人分不到稿件也不违例。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r0", "topics": ["t"], "capacity": 0},
            {"id": "r1", "topics": ["t"], "capacity": 1},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "feasible"
    assert data["assignments"] == [{"manuscript_id": "m1", "reviewer_ids": ["r1"]}]


def test_empty_manuscripts_with_zero_min_feasible():
    body = {
        "manuscripts": [],
        "reviewers": [{"id": "r1", "topics": ["t"], "capacity": 2}],
    }
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "feasible"
    assert data["assignments"] == []


def test_min_respects_track_capacity():
    """下限与赛道限额联立：r1 在 poster 限额 1，下限 2 必须靠 oral 名额补足。"""
    body = {
        "required_reviewers": 1,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "track": "poster"},
            {"id": "m2", "authors": [], "topics": ["t"], "track": "poster"},
            {"id": "m3", "authors": [], "topics": ["t"], "track": "oral"},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 3, "min_assignments": 2,
             "track_capacities": {"poster": 1}},
            {"id": "r2", "topics": ["t"], "capacity": 3},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    _verify_coverage(body, data)


# ---------- 确定性 ----------

def test_deterministic_same_input():
    body = _random_instance(seed=5)
    r1 = client.post("/assign-coverage", json=body).json()
    r2 = client.post("/assign-coverage", json=body).json()
    assert r1 == r2


def test_deterministic_reordered_input():
    """相同业务输入重排稿件/评审人顺序，响应逐字节不变。"""
    body = _random_instance(seed=11)
    r1 = client.post("/assign-coverage", json=body).json()
    shuffled = {
        "required_reviewers": body.get("required_reviewers", 1),
        "manuscripts": list(reversed(body["manuscripts"])),
        "reviewers": list(reversed(body["reviewers"])),
    }
    r2 = client.post("/assign-coverage", json=shuffled).json()
    assert r1 == r2


# ---------- 不可行：结构化原因，不输出部分名单 ----------

def test_infeasible_min_exceeds_capacity():
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 1, "min_assignments": 2},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None
    assert data["unsatisfied"] is None
    c = data["conflicts"][0]
    assert c["type"] == "min_exceeds_capacity"
    assert c["reviewer_id"] == "r1"
    assert c["minimum"] == 2
    assert c["capacity"] == 1


def test_infeasible_min_with_zero_capacity():
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 2},
            {"id": "r2", "topics": ["t"], "capacity": 0, "min_assignments": 1},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None
    types = {c["type"]: c for c in data["conflicts"]}
    assert types["min_exceeds_capacity"]["reviewer_id"] == "r2"


def test_infeasible_min_exceeds_eligible_manuscripts():
    """r1 是 m1 的唯一合格评审，m1 仅需 1 人，下限 2 无法达到。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 1},
            {"id": "m2", "authors": [], "topics": ["x"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 5, "min_assignments": 2},
            {"id": "r2", "topics": ["x"], "capacity": 5},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None
    c = data["conflicts"][0]
    assert c["type"] == "min_exceeds_eligible_manuscripts"
    assert c["reviewer_id"] == "r1"
    assert c["minimum"] == 2
    assert c["eligible"] == 1


def test_infeasible_min_blocked_by_conflict_avoidance():
    """回避导致合格稿件数不足：r1 声明回避 m1，是 m2 作者，仅 m3 合格。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": ["alice"], "topics": ["t"], "required_reviewers": 1},
            {"id": "m2", "authors": ["r1"], "topics": ["t"], "required_reviewers": 1},
            {"id": "m3", "authors": ["carol"], "topics": ["t"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 3, "min_assignments": 2,
             "conflicted_manuscripts": ["m1"]},
            {"id": "r2", "topics": ["t"], "capacity": 3},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "infeasible"
    c = data["conflicts"][0]
    assert c["type"] == "min_exceeds_eligible_manuscripts"
    assert c["eligible"] == 1


def test_infeasible_min_exceeds_track_capacity():
    """两篇 A 稿、r1 的 A 赛道限额 1，下限 2 无法达到（结构冲突）。"""
    body = {
        "required_reviewers": 1,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "track": "A"},
            {"id": "m2", "authors": [], "topics": ["t"], "track": "A"},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 3, "min_assignments": 2,
             "track_capacities": {"A": 1}},
            {"id": "r2", "topics": ["t"], "capacity": 3},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None
    c = data["conflicts"][0]
    assert c["type"] == "min_exceeds_track_capacity"
    assert c["reviewer_id"] == "r1"
    assert c["track"] == "A"
    assert c["limit"] == 1
    assert c["minimum"] == 2


def test_infeasible_min_sum_exceeds_total_demand():
    """两名评审人各需至少 1 篇，但只有 1 个总名额。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 1, "min_assignments": 1},
            {"id": "r2", "topics": ["t"], "capacity": 1, "min_assignments": 1},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None
    c = data["conflicts"][0]
    assert c["type"] == "min_exceeds_total_demand"
    assert c["minimum_sum"] == 2
    assert c["total_required"] == 1


def test_infeasible_shared_capacity_conflict():
    """结构性预检全部通过，但下限在共享容量下联立不可行。

    m1/m2/m3 各需 1 人：r1 可评 m1,m2（下限 2），r2 只能评 m1（下限 1），
    r3 只能评 m3。r2 必须占 m1，r1 只能再评 m2，负载 1 < 2，无完整解。
    """
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["a"], "required_reviewers": 1},
            {"id": "m2", "authors": [], "topics": ["b"], "required_reviewers": 1},
            {"id": "m3", "authors": [], "topics": ["c"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["a", "b"], "capacity": 2, "min_assignments": 2},
            {"id": "r2", "topics": ["a"], "capacity": 2, "min_assignments": 1},
            {"id": "r3", "topics": ["c"], "capacity": 1},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None
    assert data["total_assigned"] == 0
    c = data["conflicts"][0]
    assert c["type"] == "coverage_infeasible"
    # r2 的下限边无法补满
    assert c["reviewer_ids"] == ["r2"]


def test_infeasible_demand_side_keeps_assign_diagnostics():
    """忽略全部下限仍不可行：复用 /assign 的 unsatisfied/shared_pool/bottleneck_cut。"""
    body = {
        "required_reviewers": 2,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"]},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 1},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None
    assert data["conflicts"][0]["type"] == "coverage_infeasible"
    assert data["unsatisfied"][0]["manuscript_id"] == "m1"
    assert data["unsatisfied"][0]["missing"] == 1
    assert data["shared_pool"] is not None
    cut = data["bottleneck_cut"]
    assert cut is not None
    assert cut["max_assignable"] == 1
    assert cut["shortfall"] == 1


def test_infeasible_conflicts_sorted_and_complete():
    """多名评审人下限冲突：一次返回全部结构化原因（按类型/ID 确定排序）。

    ra/rb 容量为 0 却声明下限 1（min_exceeds_capacity），rc 合格稿件不足
    （min_exceeds_eligible_manuscripts），三类原因一次返回，互不掩盖。
    """
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 1},
            {"id": "m2", "authors": [], "topics": ["x"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "rb", "topics": ["t"], "capacity": 0, "min_assignments": 1},
            {"id": "ra", "topics": ["t"], "capacity": 0, "min_assignments": 1},
            {"id": "rc", "topics": ["t"], "capacity": 5, "min_assignments": 2},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None
    by_type: dict[str, list[str]] = {}
    for c in data["conflicts"]:
        by_type.setdefault(c["type"], []).append(c["reviewer_id"] or "")
    # 同类型内按评审人 ID 排序
    assert by_type["min_exceeds_capacity"] == ["ra", "rb"]
    assert by_type["min_exceeds_eligible_manuscripts"] == ["rc"]
    # 类型间按类型名字典序确定排序
    types = [c["type"] for c in data["conflicts"]]
    assert types == sorted(types)


# ---------- 与 /assign 的字段关系 ----------

def test_assign_ignores_min_assignments():
    """/assign 接受但忽略 min_assignments 字段（行为与不携带时完全一致）。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 1},
            {"id": "m2", "authors": [], "topics": ["t"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 2, "min_assignments": 2},
        ],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    # /assign 不保证下限：两篇都给 r1 恰好满足，但字段本身不产生冲突诊断
    assert data["assignments"] == [
        {"manuscript_id": "m1", "reviewer_ids": ["r1"]},
        {"manuscript_id": "m2", "reviewer_ids": ["r1"]},
    ]


def test_coverage_rejects_incremental_fields():
    """覆盖度接口不支持 previous/locked/balance：多余字段返回 422。"""
    body = {
        "manuscripts": [{"id": "m1", "authors": [], "topics": ["t"]}],
        "reviewers": [{"id": "r1", "topics": ["t"], "capacity": 1}],
        "balance_workload": True,
    }
    assert client.post("/assign-coverage", json=body).status_code == 422
    body = {
        "manuscripts": [{"id": "m1", "authors": [], "topics": ["t"]}],
        "reviewers": [{"id": "r1", "topics": ["t"], "capacity": 1}],
        "previous_assignments": [],
    }
    assert client.post("/assign-coverage", json=body).status_code == 422


def test_duplicate_ids_and_bad_min_rejected():
    body = {
        "manuscripts": [{"id": "m1"}, {"id": "m1"}],
        "reviewers": [],
    }
    assert client.post("/assign-coverage", json=body).status_code == 422
    body = {
        "manuscripts": [],
        "reviewers": [{"id": "r1", "capacity": 1, "min_assignments": -1}],
    }
    assert client.post("/assign-coverage", json=body).status_code == 422


# ---------- 随机实例：与暴力枚举完全一致 ----------

def _random_instance(seed: int) -> dict:
    rng = random.Random(seed)
    topics = ["t1", "t2", "t3"]
    manuscripts = [
        {
            "id": f"m{i}",
            "authors": rng.sample(["a1", "a2", "a3"], rng.randint(0, 1)),
            "topics": rng.sample(topics, rng.randint(1, 2)),
            "required_reviewers": rng.randint(1, 2),
            **({"track": rng.choice(["A", "B"])} if rng.random() < 0.5 else {}),
        }
        for i in range(rng.randint(1, 5))
    ]
    reviewers = [
        {
            "id": f"r{j}",
            "topics": rng.sample(topics, rng.randint(1, 2)),
            "capacity": rng.randint(0, 3),
            "min_assignments": rng.choice([0, 0, 1, 2]),
            **(
                {"track_capacities": {t: rng.randint(0, 2) for t in rng.sample(["A", "B"], 1)}}
                if rng.random() < 0.4 else {}
            ),
        }
        for j in range(rng.randint(1, 5))
    ]
    return {"manuscripts": manuscripts, "reviewers": reviewers}


def test_matches_brute_force():
    """小实例暴力枚举：可行性必须与全局带下界流判定一致，可行时输出满足全部约束。"""
    counts = {"feasible": 0, "infeasible": 0}
    for seed in range(250):
        body = _random_instance(seed)
        data = client.post("/assign-coverage", json=body).json()
        reviewers = {r["id"]: r for r in body["reviewers"]}

        def eligible(m: dict, rid: str) -> bool:
            r = reviewers[rid]
            if rid in m["authors"]:
                return False
            if set(m["authors"]) & set(r.get("conflicted_authors", [])):
                return False
            if m["id"] in r.get("conflicted_manuscripts", []):
                return False
            return not m["topics"] or bool(set(m["topics"]) & set(r["topics"]))

        per_m = [
            list(combinations(
                [rid for rid in reviewers if eligible(m, rid)], m["required_reviewers"]
            ))
            for m in body["manuscripts"]
        ]
        brute = False
        for combo in product(*per_m):
            loads = {rid: 0 for rid in reviewers}
            track_loads = {rid: {} for rid in reviewers}
            for m, picks in zip(body["manuscripts"], combo):
                for rid in picks:
                    loads[rid] += 1
                    if m.get("track") is not None:
                        d = track_loads[rid]
                        d[m["track"]] = d.get(m["track"], 0) + 1
            ok = True
            for rid, r in reviewers.items():
                if not (r.get("min_assignments", 0) <= loads[rid] <= r["capacity"]):
                    ok = False
                    break
                caps = r.get("track_capacities") or {}
                for t, n in track_loads[rid].items():
                    if t in caps and n > caps[t]:
                        ok = False
                if not ok:
                    break
            if ok:
                brute = True
                break

        assert data["status"] == ("feasible" if brute else "infeasible"), seed
        if brute:
            counts["feasible"] += 1
            _verify_coverage(body, data)
        else:
            counts["infeasible"] += 1
            assert data["assignments"] is None
    assert counts["feasible"] > 0
    assert counts["infeasible"] > 0
