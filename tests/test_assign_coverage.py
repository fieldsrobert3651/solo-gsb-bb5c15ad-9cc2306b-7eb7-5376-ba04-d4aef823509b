"""POST /assign-coverage 的端到端测试。"""

from __future__ import annotations

import random
from itertools import combinations, product

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _required(m: dict, default: int) -> int:
    r = m.get("required_reviewers")
    return r if r is not None else default


def _verify_coverage_constraints(body: dict, response: dict) -> None:
    """校验分配满足全部约束：篇均评审数、容量与下限、赛道限额、主题匹配、冲突回避。"""
    assert response["status"] == "feasible"
    assignments = {a["manuscript_id"]: a["reviewer_ids"] for a in response["assignments"]}
    reviewers = {r["id"]: r for r in body["reviewers"]}
    default_req = body.get("required_reviewers", 1)

    used = {rid: 0 for rid in reviewers}
    track_used: dict[str, dict[str, int]] = {rid: {} for rid in reviewers}
    for m in body["manuscripts"]:
        rid_list = assignments[m["id"]]
        assert len(rid_list) == _required(m, default_req), f"{m['id']} 评审数不足"
        assert len(set(rid_list)) == len(rid_list), "同一稿件重复分配评审人"
        track = m.get("track")
        for rid in rid_list:
            r = reviewers[rid]
            used[rid] += 1
            assert rid not in m["authors"], "自稿冲突"
            assert not set(m["authors"]) & set(r.get("conflicted_authors", [])), "利益冲突"
            assert m["id"] not in r.get("conflicted_manuscripts", [])
            if m["topics"]:
                assert set(m["topics"]) & set(r["topics"]), "主题不匹配"
            if track is not None:
                per = track_used[rid]
                per[track] = per.get(track, 0) + 1
    for rid, n in used.items():
        assert n <= reviewers[rid]["capacity"], f"{rid} 超出容量"
        assert n >= reviewers[rid].get("min_assignments", 0), f"{rid} 低于下限"
    for rid, per_track in track_used.items():
        caps = reviewers[rid].get("track_capacities") or {}
        for track, n in per_track.items():
            if track in caps:
                assert n <= caps[track], f"{rid} 赛道 {track} 超出限额"


# ---------- 可行场景 ----------

def test_coverage_min_forces_spread():
    """评审人下限迫使稿件分摊：无下限时 /assign 会把两篇都压给 r1。"""
    manuscripts = [
        {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 1},
        {"id": "m2", "authors": [], "topics": ["t"], "required_reviewers": 1},
    ]
    plain = client.post("/assign", json={
        "manuscripts": manuscripts,
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 2},
            {"id": "r2", "topics": ["t"], "capacity": 2},
        ],
    }).json()
    assert {a["manuscript_id"]: a["reviewer_ids"] for a in plain["assignments"]} == {
        "m1": ["r1"], "m2": ["r1"],
    }

    body = {
        "manuscripts": manuscripts,
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 2, "min_assignments": 1},
            {"id": "r2", "topics": ["t"], "capacity": 2, "min_assignments": 1},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "feasible"
    assert data["total_required"] == 2
    assert data["total_min_required"] == 2
    assert data["total_assigned"] == 2
    assert data["reviewer_conflicts"] is None
    assert data["unsatisfied"] is None
    assert data["reviewer_shortfalls"] is None
    _verify_coverage_constraints(body, data)
    # 确定性结果：每人至少 1 篇 -> 一篇一人
    assert {a["manuscript_id"]: a["reviewer_ids"] for a in data["assignments"]} == {
        "m1": ["r1"], "m2": ["r2"],
    }


def test_coverage_default_min_zero_behaves_like_assign():
    """min_assignments 缺省为 0：行为与 /assign 普通模式一致（按 ID 填满 r1）。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 1},
            {"id": "m2", "authors": [], "topics": ["t"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 2},
            {"id": "r2", "topics": ["t"], "capacity": 2},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "feasible"
    assert data["total_min_required"] == 0
    assert {a["manuscript_id"]: a["reviewer_ids"] for a in data["assignments"]} == {
        "m1": ["r1"], "m2": ["r1"],
    }


def test_coverage_min_with_track_and_conflicts():
    """下限与赛道限额、自稿冲突同时生效：r1 回避 m1，每人恰好 1 篇。"""
    body = {
        "required_reviewers": 1,
        "manuscripts": [
            {"id": "m1", "authors": ["r1"], "topics": ["t"], "track": "A"},
            {"id": "m2", "authors": [], "topics": ["t"], "track": "A"},
            {"id": "m3", "authors": [], "topics": ["t"]},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 2, "min_assignments": 1,
             "track_capacities": {"A": 1}},
            {"id": "r2", "topics": ["t"], "capacity": 1, "min_assignments": 1},
            {"id": "r3", "topics": ["t"], "capacity": 1, "min_assignments": 1},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "feasible"
    _verify_coverage_constraints(body, data)
    used = {}
    for a in data["assignments"]:
        for rid in a["reviewer_ids"]:
            used[rid] = used.get(rid, 0) + 1
    assert used == {"r1": 1, "r2": 1, "r3": 1}


def test_coverage_global_lower_bound_flow_not_greedy():
    """逐稿贪心会失败、全局带下界流才能成功的情形。

    m1 可由 r1/r2 评审，m2 只能由 r1 评审；r2 下限 1。贪心先满足稿件会把
    m1->r1、m2 无人可分（或先分 m2->r1 后 r2 达不到下限）；全局解必须是
    m1->r2、m2->r1。
    """
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t1"], "required_reviewers": 1},
            {"id": "m2", "authors": [], "topics": ["t2"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t1", "t2"], "capacity": 1},
            {"id": "r2", "topics": ["t1"], "capacity": 1, "min_assignments": 1},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "feasible"
    _verify_coverage_constraints(body, data)
    assert {a["manuscript_id"]: a["reviewer_ids"] for a in data["assignments"]} == {
        "m1": ["r2"], "m2": ["r1"],
    }


def test_coverage_empty_manuscripts_feasible():
    """无稿件且下限全为 0：可行，空名单。"""
    body = {"manuscripts": [], "reviewers": [{"id": "r1", "capacity": 1}]}
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "feasible"
    assert data["assignments"] == []
    assert data["total_required"] == 0
    assert data["total_min_required"] == 0
    assert data["total_assigned"] == 0


# ---------- 结构性冲突：不需求解即可判定 ----------

def test_coverage_min_exceeds_capacity_conflict():
    """下限大于容量：结构性冲突，不输出部分名单，也不给流量诊断。"""
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
    assert data["total_assigned"] == 0
    conflicts = data["reviewer_conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["type"] == "min_exceeds_capacity"
    assert conflicts[0]["reviewer_id"] == "r1"
    assert conflicts[0]["min_assignments"] == 2
    assert conflicts[0]["limit"] == 1
    assert data["unsatisfied"] is None
    assert data["shared_pool"] is None
    assert data["reviewer_shortfalls"] is None


def test_coverage_min_exceeds_eligible_conflict():
    """下限大于合格稿件数（资格不足）：结构性冲突。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["ml"], "required_reviewers": 1},
            {"id": "m2", "authors": [], "topics": ["db"], "required_reviewers": 1},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["ml"], "capacity": 5, "min_assignments": 2},
            {"id": "r2", "topics": ["ml", "db"], "capacity": 5},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None
    conflicts = data["reviewer_conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["type"] == "min_exceeds_eligible"
    assert conflicts[0]["reviewer_id"] == "r1"
    assert conflicts[0]["min_assignments"] == 2
    assert conflicts[0]["limit"] == 1  # 仅 m1 合格


def test_coverage_min_exceeds_eligible_via_track_limit():
    """下限大于赛道限额折算后的个人可接上限：两篇 A 稿但 A 赛道限额 1。"""
    body = {
        "required_reviewers": 1,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "track": "A"},
            {"id": "m2", "authors": [], "topics": ["t"], "track": "A"},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 5, "min_assignments": 2,
             "track_capacities": {"A": 1}},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "infeasible"
    conflicts = data["reviewer_conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["type"] == "min_exceeds_eligible"
    assert conflicts[0]["reviewer_id"] == "r1"
    assert conflicts[0]["limit"] == 1


def test_coverage_conflicts_sorted_and_order_independent():
    """多个结构性冲突按类型、评审人 ID 确定排序，与输入顺序无关。"""
    reviewers = [
        {"id": "r2", "topics": ["t"], "capacity": 1, "min_assignments": 2},
        {"id": "r1", "topics": ["t"], "capacity": 0, "min_assignments": 1},
        {"id": "r3", "topics": ["x"], "capacity": 5, "min_assignments": 1},
    ]
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 1},
        ],
        "reviewers": reviewers,
    }
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "infeasible"
    conflicts = data["reviewer_conflicts"]
    assert [(c["type"], c["reviewer_id"]) for c in conflicts] == [
        ("min_exceeds_capacity", "r1"),
        ("min_exceeds_capacity", "r2"),
        ("min_exceeds_eligible", "r3"),
    ]
    shuffled = {
        "manuscripts": body["manuscripts"],
        "reviewers": list(reversed(reviewers)),
    }
    assert client.post("/assign-coverage", json=shuffled).json() == data


# ---------- 全局不可行：两侧诊断 ----------

def test_coverage_infeasible_shared_capacity():
    """共享容量冲突：总容量 < 总需求，稿件侧诊断非空，不输出部分名单。"""
    body = {
        "required_reviewers": 2,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"]},
            {"id": "m2", "authors": [], "topics": ["t"]},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 1},
            {"id": "r2", "topics": ["t"], "capacity": 2, "min_assignments": 2},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None
    assert data["reviewer_conflicts"] is None
    # 稿件侧：即使忽略下限，容量 3 < 需求 4
    assert data["unsatisfied"] is not None
    assert data["shared_pool"] is not None
    # 评审人侧：r2 的下限 2 可以满足（两篇都给它）-> 无下限缺口
    assert data["reviewer_shortfalls"] is None


def test_coverage_infeasible_min_shortfall():
    """合格关系不足：r1、r2 只能争同一篇稿件，评审人侧诊断非空。"""
    body = {
        "required_reviewers": 1,
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"]},
            {"id": "m2", "authors": [], "topics": ["t"]},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 1, "min_assignments": 1,
             "conflicted_manuscripts": ["m2"]},
            {"id": "r2", "topics": ["t"], "capacity": 1, "min_assignments": 1,
             "conflicted_manuscripts": ["m2"]},
            {"id": "r3", "topics": ["t"], "capacity": 1},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None
    assert data["reviewer_conflicts"] is None
    # 稿件侧：m1->r1/r2/r3、m2->r3，覆盖可行
    assert data["unsatisfied"] is None
    assert data["shared_pool"] is None
    # 评审人侧：最优努力下只有一人能达到下限（确定性：r1 先得到满足）
    assert data["reviewer_shortfalls"] == [
        {"reviewer_id": "r2", "min_assignments": 1, "attained": 0, "missing": 1}
    ]


def test_coverage_infeasible_min_without_demand():
    """稿件需求为 0 时评审人下限无处可分：评审人侧缺口（不触发结构冲突）。"""
    body = {
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"], "required_reviewers": 0},
        ],
        "reviewers": [
            {"id": "r1", "topics": ["t"], "capacity": 2, "min_assignments": 1},
        ],
    }
    data = client.post("/assign-coverage", json=body).json()
    assert data["status"] == "infeasible"
    assert data["assignments"] is None
    assert data["reviewer_conflicts"] is None  # 个人可接上限 1（m1 合格），不触发
    assert data["unsatisfied"] is None
    assert data["reviewer_shortfalls"] == [
        {"reviewer_id": "r1", "min_assignments": 1, "attained": 0, "missing": 1}
    ]


# ---------- 确定性 / 稳定性 ----------

def _random_coverage_instance(seed: int) -> dict:
    rng = random.Random(seed)
    topics = ["ml", "nlp", "cv"]
    authors = [f"a{i}" for i in range(8)]
    manuscripts = [
        {
            "id": f"m{i}",
            "authors": rng.sample(authors, rng.randint(0, 2)),
            "topics": rng.sample(topics, rng.randint(1, 2)),
            "required_reviewers": rng.randint(1, 2),
            **({"track": rng.choice(["A", "B"])} if rng.random() < 0.4 else {}),
        }
        for i in range(rng.randint(2, 5))
    ]
    reviewers = [
        {
            "id": f"r{j}",
            "topics": rng.sample(topics, rng.randint(1, 3)),
            "capacity": rng.randint(1, 3),
            "min_assignments": rng.randint(0, 2),
            "conflicted_authors": rng.sample(authors, rng.randint(0, 2)),
            **(
                {"track_capacities": {
                    t: rng.randint(0, 2) for t in rng.sample(["A", "B"], 1)
                }}
                if rng.random() < 0.4 else {}
            ),
        }
        for j in range(rng.randint(2, 5))
    ]
    return {"manuscripts": manuscripts, "reviewers": reviewers}


def test_coverage_same_input_same_output():
    body = _random_coverage_instance(42)
    r1 = client.post("/assign-coverage", json=body).json()
    r2 = client.post("/assign-coverage", json=body).json()
    assert r1 == r2


def test_coverage_input_order_does_not_change_result():
    body = _random_coverage_instance(7)
    shuffled = {
        "manuscripts": list(reversed(body["manuscripts"])),
        "reviewers": list(reversed(body["reviewers"])),
    }
    r1 = client.post("/assign-coverage", json=body).json()
    r2 = client.post("/assign-coverage", json=shuffled).json()
    assert r1 == r2


# ---------- 请求校验 ----------

def test_coverage_negative_min_assignments_rejected():
    body = {
        "manuscripts": [],
        "reviewers": [{"id": "r1", "capacity": 2, "min_assignments": -1}],
    }
    resp = client.post("/assign-coverage", json=body)
    assert resp.status_code == 422


def test_coverage_duplicate_manuscript_id_rejected():
    body = {
        "manuscripts": [{"id": "m1"}, {"id": "m1"}],
        "reviewers": [],
    }
    resp = client.post("/assign-coverage", json=body)
    assert resp.status_code == 422


def test_coverage_duplicate_reviewer_id_rejected():
    body = {
        "manuscripts": [],
        "reviewers": [
            {"id": "r1", "capacity": 1},
            {"id": "r1", "capacity": 2},
        ],
    }
    resp = client.post("/assign-coverage", json=body)
    assert resp.status_code == 422


def test_coverage_extra_field_rejected():
    body = {"manuscripts": [], "reviewers": [], "previous_assignments": []}
    resp = client.post("/assign-coverage", json=body)
    assert resp.status_code == 422


# ---------- 与既有接口并存 ----------

def test_coverage_existing_endpoints_still_work():
    """新增 /assign-coverage 不影响既有 /assign 与 /assign-roles。"""
    body = {
        "manuscripts": [{"id": "m1", "authors": [], "topics": ["t"]}],
        "reviewers": [{"id": "r1", "topics": ["t"], "capacity": 1}],
    }
    data = client.post("/assign", json=body).json()
    assert data["status"] == "feasible"
    roles = client.post("/assign-roles", json={
        "manuscripts": [
            {"id": "m1", "authors": [], "topics": ["t"],
             "role_requirements": {"reviewer": 1}},
        ],
        "reviewers": [{"id": "r1", "topics": ["t"], "capacity": 1,
                       "roles": ["reviewer"]}],
    }).json()
    assert roles["status"] == "feasible"


# ---------- 暴力枚举对拍 ----------

def test_coverage_matches_brute_force():
    """小实例暴力枚举：可行性判定一致；不可行时两侧诊断至少其一非空。"""
    rng = random.Random(20260926)
    topics = ["t1", "t2"]
    tracks = ["A", "B"]
    checked = {"feasible": 0, "infeasible": 0, "conflict": 0}
    for _ in range(150):
        manuscripts = [
            {
                "id": f"m{i}",
                "authors": [],
                "topics": rng.sample(topics, rng.randint(0, 2)),
                "required_reviewers": rng.randint(0, 2),
                **({"track": rng.choice(tracks)} if rng.random() < 0.4 else {}),
            }
            for i in range(rng.randint(1, 3))
        ]
        reviewers = [
            {
                "id": f"r{j}",
                "topics": rng.sample(topics, rng.randint(0, 2)),
                "capacity": rng.randint(0, 3),
                "min_assignments": rng.randint(0, 2),
                **(
                    {"track_capacities": {
                        t: rng.randint(0, 2) for t in rng.sample(tracks, 1)
                    }}
                    if rng.random() < 0.4 else {}
                ),
            }
            for j in range(rng.randint(1, 4))
        ]
        body = {"manuscripts": manuscripts, "reviewers": reviewers}
        data = client.post("/assign-coverage", json=body).json()

        def eligible(m: dict, r: dict) -> bool:
            if m["topics"] and not set(m["topics"]) & set(r["topics"]):
                return False
            return True

        per_m = [
            list(combinations(
                [r for r in reviewers if eligible(m, r)],
                m["required_reviewers"],
            ))
            for m in manuscripts
        ]
        feasible = False
        for combo in product(*per_m):
            loads = {r["id"]: 0 for r in reviewers}
            track_loads: dict[str, dict[str, int]] = {
                r["id"]: {} for r in reviewers
            }
            for m, picks in zip(manuscripts, combo):
                for r in picks:
                    loads[r["id"]] += 1
                    track = m.get("track")
                    if track is not None:
                        per = track_loads[r["id"]]
                        per[track] = per.get(track, 0) + 1
            ok = True
            for r in reviewers:
                n = loads[r["id"]]
                if n > r["capacity"] or n < r["min_assignments"]:
                    ok = False
                caps = r.get("track_capacities") or {}
                for track, cnt in track_loads[r["id"]].items():
                    if track in caps and cnt > caps[track]:
                        ok = False
            if ok:
                feasible = True
                break

        if feasible:
            assert data["status"] == "feasible"
            assert data["reviewer_conflicts"] is None
            assert data["total_assigned"] == data["total_required"]
            _verify_coverage_constraints(body, data)
            checked["feasible"] += 1
        else:
            assert data["status"] == "infeasible"
            assert data["assignments"] is None
            assert data["total_assigned"] == 0
            if data["reviewer_conflicts"] is not None:
                # 结构性冲突：不再进行流量求解，两侧诊断为 null
                assert data["unsatisfied"] is None
                assert data["reviewer_shortfalls"] is None
                checked["conflict"] += 1
            else:
                # 全局不可行时，稿件侧与评审人侧诊断至少其一非空
                assert data["unsatisfied"] is not None or (
                    data["reviewer_shortfalls"] is not None
                )
            checked["infeasible"] += 1
    assert checked["feasible"] > 0
    assert checked["infeasible"] > 0
    assert checked["conflict"] > 0
