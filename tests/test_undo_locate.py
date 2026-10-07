"""撤销定位：误定位后撤销最近一次定位的端到端测试。

覆盖：定位响应返回操作 ID（原有字段不变）；撤销恢复原计时起点与该次
跳过的待发提示（更早已发/跳过不动）；操作 ID 不存在/已撤销/已被后续
定位取代 -> 409 且状态不变；定位后进度前进（含同秒发放）、登记批注、
速度图发布 -> 409；连续定位只能撤销最后一次，撤销后更早定位不复活；
普通回跳仍不恢复旧提示；撤销与领取/批注/定位/发布交错时基于同一已
提交状态；撤销成功后待发提示按恢复起点与当前速度图定时；查询反映
可撤销操作 ID，重启后状态一致；旧版库自动迁移。
"""

import math
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """每个测试用独立 SQLite，并重新初始化模块级 store。"""
    db_path = tmp_path / "undo.db"
    monkeypatch.setenv("TEMPO_DB_PATH", str(db_path))

    import app.main as main
    from app.db import Store

    monkeypatch.setattr(main, "store", Store(str(db_path)))
    yield db_path


from app.main import app  # noqa: E402

client = TestClient(app)

# 120 BPM、4/4、PPQ=480：一拍 0.5s、一小节 2.0s
SCORE_BODY = {
    "ppq": 480,
    "time_signatures": [],
    "tempo_points": [{"position": "1:1:0", "bpm": 120, "curve": "step"}],
    "anchors": [{"position": "1:1:0", "locked": True}],
}
SLOW_POINTS = [{"position": "1:1:0", "bpm": 60, "curve": "step"}]


def create_score():
    resp = client.post("/scores", json=SCORE_BODY)
    assert resp.status_code == 201, resp.text
    return resp.json()


def create_rehearsal(score_id, cues):
    resp = client.post("/rehearsals", json={"score_id": score_id, "cues": cues})
    assert resp.status_code == 201, resp.text
    return resp.json()


def claim(rid, elapsed):
    return client.post(
        f"/rehearsals/{rid}/claims", json={"elapsed_seconds": elapsed}
    )


def locate(rid, elapsed, position):
    return client.post(
        f"/rehearsals/{rid}/locate",
        json={"elapsed_seconds": elapsed, "position": position},
    )


def undo(rid, operation_id):
    return client.post(
        f"/rehearsals/{rid}/locate/{operation_id}/undo"
    )


CUES_BCE = [
    {"position": "2:1:0", "text": "B"},  # 2.0s
    {"position": "3:1:0", "text": "C"},  # 4.0s
    {"position": "5:1:0", "text": "E"},  # 8.0s
]


# ---------------------------------------------------------------------- #
# 定位响应带操作 ID，且原有字段保持不变
# ---------------------------------------------------------------------- #
def test_locate_response_carries_operation_id_with_existing_fields():
    score = create_score()
    r = create_rehearsal(score["id"], CUES_BCE)
    rid = r["id"]
    claim(rid, 2.0)  # B 已发
    resp = locate(rid, 2.0, "4:1:0")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 新增字段
    op = data["operation_id"]
    assert isinstance(op, str) and len(op) == 32
    # 原有字段保持不变
    assert data["id"] == rid
    assert data["elapsed_seconds"] == 2.0
    assert data["revision"] == 0
    assert data["origin"] == {
        "position": "4:1:0",
        "total_ticks": 5760,
        "elapsed_seconds": 2.0,
    }
    assert [s["text"] for s in data["skipped"]] == ["C"]
    assert {c["text"]: c["status"] for c in data["cues"]} == {
        "B": "delivered",
        "C": "skipped",
        "E": "pending",
    }


def test_rehearsal_query_reports_undoable_operation_id():
    score = create_score()
    rid = create_rehearsal(score["id"], CUES_BCE)["id"]
    # 未定位：无可撤销操作
    assert client.get(f"/rehearsals/{rid}").json()["undoable_operation_id"] is None
    op = locate(rid, 0.0, "3:1:0").json()["operation_id"]
    got = client.get(f"/rehearsals/{rid}").json()
    assert got["undoable_operation_id"] == op


# ---------------------------------------------------------------------- #
# 撤销：恢复原起点与该次跳过的待发提示
# ---------------------------------------------------------------------- #
def test_undo_restores_origin_and_skipped_cues():
    score = create_score()
    rid = create_rehearsal(score["id"], CUES_BCE)["id"]
    claim(rid, 2.0)  # B 已发，进度 2.0

    op = locate(rid, 2.0, "4:1:0").json()["operation_id"]
    resp = undo(rid, op)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["operation_id"] == op
    assert data["revision"] == 0
    # 原起点恢复为未定位的 1:1:0 / 0.0s
    assert data["origin"] == {
        "position": "1:1:0",
        "total_ticks": 0,
        "elapsed_seconds": 0.0,
    }
    # 仅恢复本次跳过的 C；B 已发、E 待发不动
    assert [r["text"] for r in data["restored"]] == ["C"]
    restored_c = data["restored"][0]
    assert restored_c["index"] == 1
    assert restored_c["position"] == "3:1:0"
    assert restored_c["total_ticks"] == 3840
    statuses = {c["text"]: c["status"] for c in data["cues"]}
    assert statuses == {"B": "delivered", "C": "pending", "E": "pending"}
    c_cue = next(c for c in data["cues"] if c["text"] == "C")
    assert c_cue["skipped_revision"] is None
    assert c_cue["trigger_seconds"] is None and c_cue["revision"] is None
    # 撤销后不再有可撤销操作
    assert client.get(f"/rehearsals/{rid}").json()["undoable_operation_id"] is None


def test_after_undo_pending_cues_follow_restored_origin_and_current_map():
    # 恢复原起点后，C 重新按全图绝对秒数 4.0s 触发（可正常领取）；
    # B 的已发记录不撤回、不重发。
    score = create_score()
    rid = create_rehearsal(score["id"], CUES_BCE)["id"]
    claim(rid, 2.0)  # B
    op = locate(rid, 2.0, "4:1:0").json()["operation_id"]
    undo(rid, op)

    # 撤销后进度仍为 2.0；早于 2.0 的领取仍是倒退 409
    assert claim(rid, 1.0).status_code == 409
    # C 在恢复的原起点（全图绝对计时）下 4.0s 触发；
    # E 此前按定位起点定时也是 4.0s（2.0 + 4:1:0->5:1:0 的 2.0s），
    # 撤销后恢复全图计时为 8.0s，故 4.0s 只到 C。
    due = claim(rid, 4.0).json()
    assert [c["text"] for c in due["cues"]] == ["C"]
    assert math.isclose(due["cues"][0]["trigger_seconds"], 4.0)
    assert due["cues"][0]["revision"] == 0
    # B 不重发
    assert claim(rid, 4.0).json()["cues"] == []
    # E 恢复绝对计时：8.0s 才到点（而不是定位后的 4.0s）
    assert claim(rid, 7.999).json()["cues"] == []
    late = claim(rid, 8.0).json()
    assert [c["text"] for c in late["cues"]] == ["E"]
    assert math.isclose(late["cues"][0]["trigger_seconds"], 8.0)
    got = client.get(f"/rehearsals/{rid}").json()
    assert got["origin"]["position"] == "1:1:0"
    assert got["last_elapsed_seconds"] == 8.0


def test_undo_restores_to_origin_of_previous_locate():
    # 定位 A 到 2:1:0（提交 0.0），再定位 B 到 4:1:0；撤销 B 后起点恢复
    # 为 A 的起点（2:1:0 / 0.0），而不是乐谱原点。
    score = create_score()
    rid = create_rehearsal(score["id"], CUES_BCE)["id"]
    locate(rid, 0.0, "2:1:0")  # B 在目标处，不跳过
    op_b = locate(rid, 0.0, "4:1:0").json()  # 此时 B、C 位于目标之前 -> 跳过
    data = undo(rid, op_b["operation_id"]).json()
    assert data["origin"] == {
        "position": "2:1:0",
        "total_ticks": 1920,
        "elapsed_seconds": 0.0,
    }
    assert [r["text"] for r in data["restored"]] == ["B", "C"]
    statuses = {c["text"]: c["status"] for c in data["cues"]}
    assert statuses == {"B": "pending", "C": "pending", "E": "pending"}
    # 恢复后 B 位于起点 tick 上，同一秒（0.0）即可领取
    same_second = claim(rid, 0.0).json()
    assert [c["text"] for c in same_second["cues"]] == ["B"]
    assert math.isclose(same_second["cues"][0]["trigger_seconds"], 0.0)


def test_undo_without_skips_only_restores_origin():
    # 一次没有跳过任何提示的定位（定位到首个待发提示处）仍可撤销：
    # restored 为空，仅起点恢复。
    score = create_score()
    rid = create_rehearsal(
        score["id"], [{"position": "2:1:0", "text": "B"}]
    )["id"]
    op = locate(rid, 0.0, "2:1:0").json()["operation_id"]
    data = undo(rid, op).json()
    assert data["restored"] == []
    assert data["origin"]["position"] == "1:1:0"
    assert data["cues"][0]["status"] == "pending"
    # B 恢复全图计时：2.0s 到点
    assert [c["text"] for c in claim(rid, 2.0).json()["cues"]] == ["B"]


def test_undo_keeps_earlier_delivered_and_skipped_cues_untouched():
    # 更早发放（B）与更早跳过（C，由上一次已不可撤销的定位产生）的提示
    # 在撤销最近一次定位时保持原状；最近一次定位自身无跳过 -> restored 空。
    score = create_score()
    rid = create_rehearsal(score["id"], CUES_BCE)["id"]
    claim(rid, 2.0)  # B 已发
    first = locate(rid, 2.0, "4:1:0")  # C 跳过
    assert [s["text"] for s in first.json()["skipped"]] == ["C"]
    # 再定位（连续）：没有新的待发提示位于目标之前 -> skipped 空
    second = locate(rid, 2.0, "5:1:0")
    assert second.json()["skipped"] == []
    # 第一次定位已被取代，撤销第二次：只恢复第二次（无跳过），C 仍跳过
    data = undo(rid, second.json()["operation_id"]).json()
    assert data["restored"] == []
    assert data["origin"]["position"] == "4:1:0"  # 恢复为第一次定位的起点
    statuses = {c["text"]: c["status"] for c in data["cues"]}
    assert statuses == {"B": "delivered", "C": "skipped", "E": "pending"}


# ---------------------------------------------------------------------- #
# 409：操作 ID 不存在 / 已撤销 / 已被后续定位取代
# ---------------------------------------------------------------------- #
def test_undo_unknown_operation_is_409_without_state_change():
    score = create_score()
    rid = create_rehearsal(score["id"], CUES_BCE)["id"]
    locate(rid, 0.0, "3:1:0")
    resp = undo(rid, "0123456789abcdef0123456789abcdef")
    assert resp.status_code == 409
    detail = resp.json()["detail"][0]
    assert detail["loc"] == ["path", "operation_id"]
    assert detail["reason"] == "not_found"
    assert detail["operation_id"] == "0123456789abcdef0123456789abcdef"
    # 状态不变：定位仍可撤销
    got = client.get(f"/rehearsals/{rid}").json()
    assert got["undoable_operation_id"] is not None
    assert got["origin"]["position"] == "3:1:0"


def test_undo_twice_is_409_and_state_unchanged():
    score = create_score()
    rid = create_rehearsal(score["id"], CUES_BCE)["id"]
    claim(rid, 2.0)
    op = locate(rid, 2.0, "4:1:0").json()["operation_id"]
    assert undo(rid, op).status_code == 200
    again = undo(rid, op)
    assert again.status_code == 409
    detail = again.json()["detail"][0]
    assert detail["reason"] == "already_undone"
    # 撤销结果保持：C 仍待发、起点仍是原起点
    got = client.get(f"/rehearsals/{rid}").json()
    assert got["origin"]["position"] == "1:1:0"
    assert {c["text"]: c["status"] for c in got["cues"]} == {
        "B": "delivered",
        "C": "pending",
        "E": "pending",
    }


def test_consecutive_locations_only_last_is_undoable():
    score = create_score()
    rid = create_rehearsal(score["id"], CUES_BCE)["id"]
    first = locate(rid, 0.0, "2:1:0").json()["operation_id"]
    second = locate(rid, 0.0, "4:1:0").json()["operation_id"]

    # 第一次定位已被第二次取代
    resp = undo(rid, first)
    assert resp.status_code == 409
    assert resp.json()["detail"][0]["reason"] == "superseded"
    # 状态不变：起点仍在 4:1:0
    got = client.get(f"/rehearsals/{rid}").json()
    assert got["origin"]["position"] == "4:1:0"

    # 撤销最后一次成功
    assert undo(rid, second).status_code == 200
    # 撤销最后一次后，更早的定位不“复活”为可撤销
    resp = undo(rid, first)
    assert resp.status_code == 409
    assert resp.json()["detail"][0]["reason"] == "superseded"
    assert client.get(f"/rehearsals/{rid}").json()["undoable_operation_id"] is None


def test_new_locate_after_undo_is_itself_undoable():
    # 撤销后再次定位：新操作独立可撤销，撤销后回到该次定位前的起点
    # （即上一次撤销恢复出的起点）。
    score = create_score()
    rid = create_rehearsal(score["id"], CUES_BCE)["id"]
    op1 = locate(rid, 0.0, "4:1:0").json()["operation_id"]
    undo(rid, op1)
    assert client.get(f"/rehearsals/{rid}").json()["origin"]["position"] == "1:1:0"

    # 再定位到 3:1:0：只有 B（2:1:0）位于目标之前被跳过；C 在目标处不跳过
    op2 = locate(rid, 0.0, "3:1:0").json()["operation_id"]
    data = undo(rid, op2).json()
    assert data["origin"]["position"] == "1:1:0"
    assert [r["text"] for r in data["restored"]] == ["B"]
    assert {c["text"]: c["status"] for c in data["cues"]} == {
        "B": "pending",
        "C": "pending",
        "E": "pending",
    }


# ---------------------------------------------------------------------- #
# 409：定位后继续使用（进度前进 / 提示发放 / 批注 / 发布）
# ---------------------------------------------------------------------- #
def test_undo_after_progress_advance_is_409_and_state_unchanged():
    score = create_score()
    rid = create_rehearsal(score["id"], CUES_BCE)["id"]
    claim(rid, 2.0)  # B 已发，进度 2.0
    op = locate(rid, 2.0, "4:1:0").json()["operation_id"]
    # 定位后继续领取到 3.0（无提示到点，但进度前进）
    assert claim(rid, 3.0).json()["cues"] == []
    resp = undo(rid, op)
    assert resp.status_code == 409
    assert resp.json()["detail"][0]["reason"] == "progress"
    # 状态不变：起点仍 4:1:0、C 仍跳过、进度 3.0
    got = client.get(f"/rehearsals/{rid}").json()
    assert got["origin"]["position"] == "4:1:0"
    assert got["last_elapsed_seconds"] == 3.0
    assert {c["text"]: c["status"] for c in got["cues"]} == {
        "B": "delivered",
        "C": "skipped",
        "E": "pending",
    }
    assert got["undoable_operation_id"] is None


def test_undo_after_same_second_delivery_is_409():
    # 定位到目标处的提示可在同一秒领取；一旦同秒发放，定位即不可撤销。
    score = create_score()
    rid = create_rehearsal(
        score["id"],
        [
            {"position": "4:1:0", "text": "T"},
            {"position": "5:1:0", "text": "E"},
        ],
    )["id"]
    op = locate(rid, 0.0, "4:1:0").json()["operation_id"]
    same_second = claim(rid, 0.0)
    assert [c["text"] for c in same_second.json()["cues"]] == ["T"]
    resp = undo(rid, op)
    assert resp.status_code == 409
    assert resp.json()["detail"][0]["reason"] == "progress"
    # T 保持已发，不被撤销恢复
    got = client.get(f"/rehearsals/{rid}").json()
    assert got["cues"][0]["status"] == "delivered"


def test_undo_after_annotation_is_409_and_state_unchanged():
    score = create_score()
    rid = create_rehearsal(score["id"], CUES_BCE)["id"]
    claim(rid, 2.0)
    op = locate(rid, 2.0, "4:1:0").json()["operation_id"]
    ann = client.post(
        f"/rehearsals/{rid}/annotations",
        json={"elapsed_seconds": 2.0, "text": "跳排备注"},
    )
    assert ann.status_code == 201, ann.text
    resp = undo(rid, op)
    assert resp.status_code == 409
    assert resp.json()["detail"][0]["reason"] == "annotation"
    # 状态不变：批注保留、跳过保留、起点不变
    anns = client.get(f"/rehearsals/{rid}/annotations").json()
    assert [a["text"] for a in anns] == ["跳排备注"]
    got = client.get(f"/rehearsals/{rid}").json()
    assert got["origin"]["position"] == "4:1:0"
    assert next(c for c in got["cues"] if c["text"] == "C")["status"] == "skipped"


def test_undo_after_publish_is_409_and_state_unchanged():
    score = create_score()
    sid = score["id"]
    rid = create_rehearsal(sid, CUES_BCE)["id"]
    claim(rid, 2.0)
    op = locate(rid, 2.0, "4:1:0").json()["operation_id"]
    pub = client.post(
        f"/scores/{sid}/revisions/0/publish",
        json={"tempo_points": SLOW_POINTS},
    )
    assert pub.status_code == 200, pub.text
    resp = undo(rid, op)
    assert resp.status_code == 409
    assert resp.json()["detail"][0]["reason"] == "revision"
    # 起点与跳过状态保持定位时的值（发布只重算待发提示）
    got = client.get(f"/rehearsals/{rid}").json()
    assert got["origin"] == {
        "position": "4:1:0",
        "total_ticks": 5760,
        "elapsed_seconds": 2.0,
    }
    assert {c["text"]: c["status"] for c in got["cues"]} == {
        "B": "delivered",
        "C": "skipped",
        "E": "pending",
    }
    assert got["undoable_operation_id"] is None


def test_undoable_id_cleared_after_each_invalidating_event():
    # 进度前进/发放/批注/发布/再定位后，GET 不再报告可撤销操作。
    score = create_score()
    sid = score["id"]

    rid = create_rehearsal(sid, CUES_BCE)["id"]
    claim(rid, 2.0)
    op = locate(rid, 2.0, "4:1:0").json()["operation_id"]
    assert client.get(f"/rehearsals/{rid}").json()["undoable_operation_id"] == op
    claim(rid, 3.0)
    assert client.get(f"/rehearsals/{rid}").json()["undoable_operation_id"] is None

    rid2 = create_rehearsal(sid, CUES_BCE)["id"]
    claim(rid2, 0.0)
    op2 = locate(rid2, 0.0, "3:1:0").json()["operation_id"]
    client.post(
        f"/rehearsals/{rid2}/annotations",
        json={"elapsed_seconds": 0.0, "text": "x"},
    )
    assert client.get(f"/rehearsals/{rid2}").json()["undoable_operation_id"] is None
    assert undo(rid2, op2).status_code == 409

    rid3 = create_rehearsal(sid, CUES_BCE)["id"]
    claim(rid3, 0.0)
    op3 = locate(rid3, 0.0, "3:1:0").json()["operation_id"]
    assert client.get(f"/rehearsals/{rid3}").json()["undoable_operation_id"] == op3
    locate(rid3, 0.0, "4:1:0")  # 再定位：op3 被取代，新操作可撤销
    got = client.get(f"/rehearsals/{rid3}").json()
    assert got["undoable_operation_id"] is not None
    assert got["undoable_operation_id"] != op3


# ---------------------------------------------------------------------- #
# 普通回跳不恢复旧提示（撤销语义与普通定位严格区分）
# ---------------------------------------------------------------------- #
def test_plain_backward_locate_does_not_restore_skipped_cues():
    score = create_score()
    rid = create_rehearsal(score["id"], CUES_BCE)["id"]
    claim(rid, 2.0)  # B
    locate(rid, 2.0, "4:1:0")  # C 跳过，E 待发
    claim(rid, 4.0)  # E 按定位计时到点（2.0 + 4:1:0->5:1:0 的 2.0s）

    # 普通回跳到 2:1:0：不恢复 C，也不重发 B/E
    data = locate(rid, 4.0, "2:1:0").json()
    assert data["skipped"] == []
    statuses = {c["text"]: c["status"] for c in data["cues"]}
    assert statuses == {"B": "delivered", "C": "skipped", "E": "delivered"}
    assert claim(rid, 1000.0).json()["cues"] == []


def test_failed_locate_leaves_no_undoable_operation():
    # 秒数不等于进度的定位 409 且不产生操作记录；此前可撤销的定位不受影响。
    score = create_score()
    rid = create_rehearsal(score["id"], CUES_BCE)["id"]
    claim(rid, 2.0)
    op = locate(rid, 2.0, "4:1:0").json()["operation_id"]
    bad = locate(rid, 3.0, "5:1:0")
    assert bad.status_code == 409
    # 原定位仍可撤销（失败定位没有取代它）
    got = client.get(f"/rehearsals/{rid}").json()
    assert got["undoable_operation_id"] == op
    assert undo(rid, op).status_code == 200


# ---------------------------------------------------------------------- #
# 404 与 422 边界
# ---------------------------------------------------------------------- #
def test_undo_unknown_rehearsal_is_404():
    resp = client.post(
        "/rehearsals/deadbeef/locate/0123456789abcdef0123456789abcdef/undo"
    )
    assert resp.status_code == 404
    assert resp.json()["detail"][0]["loc"] == ["path", "rehearsal_id"]


def test_undo_with_other_rehearsals_operation_id_is_409_not_found():
    # 属于其他排练的操作 ID：在本排练下视为找不到。
    score = create_score()
    r1 = create_rehearsal(score["id"], CUES_BCE)["id"]
    r2 = create_rehearsal(score["id"], CUES_BCE)["id"]
    op1 = locate(r1, 0.0, "3:1:0").json()["operation_id"]
    resp = undo(r2, op1)
    assert resp.status_code == 409
    assert resp.json()["detail"][0]["reason"] == "not_found"


# ---------------------------------------------------------------------- #
# 交错并发：撤销与领取/定位共用写锁，检查与恢复基于同一已提交状态
# ---------------------------------------------------------------------- #
def test_concurrent_undo_and_claim_are_serialized(fresh_db):
    """撤销与领取并发：二者必居其一，不会出现“既撤销又发放同一条提示”。

    定位跳过 B/C 后，一半连接撤销（恢复 B/C 待发），一半连接直接领取到
    1000s。串行化后：撤销先到则 B、C 在恢复后被发放（delivered）；领取
    先到则撤销全部 409（进度 1000），B、C 保持跳过。任何结局中跳过与
    发放集合不相交，且撤销不会把已发提示改回待发。
    """
    from app.db import Store, UndoNotAllowed
    from app.main import _due_cues

    score = create_score()
    rid = create_rehearsal(
        score["id"],
        [{"position": f"{m}:1:0", "text": f"cue{m}"} for m in range(2, 8)],
    )["id"]
    db_path = fresh_db
    # 定位到 4:1:0（tick 5760），跳过 2:1:0、3:1:0 两条
    loc = locate(rid, 0.0, "4:1:0")
    assert [s["index"] for s in loc.json()["skipped"]] == [0, 1]
    op = loc.json()["operation_id"]

    n_workers = 6
    stores = [Store(str(db_path)) for _ in range(n_workers)]
    barrier = threading.Barrier(n_workers)
    outcomes: list[str] = []

    def worker(i, store):
        barrier.wait(timeout=10)
        if i % 2 == 0:
            try:
                store.undo_locate(rid, op)
                outcomes.append("undone")
            except UndoNotAllowed:
                outcomes.append("undo_rejected")
        else:
            store.claim_cues(
                rid, 1000.0, lambda s, p: _due_cues(s, p, 1000.0)
            )
            outcomes.append("claimed")

    threads = [
        threading.Thread(target=worker, args=(i, s))
        for i, s in enumerate(stores)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    conn = sqlite3.connect(str(db_path))
    delivered = {
        row[0]
        for row in conn.execute(
            "SELECT cue_index FROM cue_deliveries WHERE rehearsal_id = ?", (rid,)
        )
    }
    skipped = {
        row[0]
        for row in conn.execute(
            "SELECT cue_index FROM cue_skips WHERE rehearsal_id = ?", (rid,)
        )
    }
    progress = conn.execute(
        "SELECT last_elapsed, origin_ticks FROM rehearsals WHERE id = ?", (rid,)
    ).fetchone()
    statuses = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT operation_id, status FROM locate_operations WHERE rehearsal_id = ?",
            (rid,),
        )
    }
    conn.close()

    assert not (delivered & skipped)
    assert delivered | skipped == set(range(6))
    assert statuses[op] in ("undone", "active")
    if statuses[op] == "undone":
        # 撤销先提交：没有成功领取能早于它（领取都会 409/被串行到撤销后，
        # 撤销后领取进度 1000，B/C 恢复后一并发放），最终全部发放。
        assert delivered == set(range(6))
        assert progress[1] == 0  # 起点恢复为原点
    else:
        # 领取先提交：所有撤销都被拒，B/C 保持跳过、起点仍在 4:1:0。
        assert "undo_rejected" in outcomes
        assert skipped == {0, 1}
        assert progress == (1000.0, 5760)


# ---------------------------------------------------------------------- #
# 持久化：撤销相关状态重启后保留
# ---------------------------------------------------------------------- #
def test_undo_state_survives_store_restart(fresh_db, monkeypatch):
    db_path = fresh_db
    score = create_score()
    rid = create_rehearsal(score["id"], CUES_BCE)["id"]
    claim(rid, 2.0)  # B
    op = locate(rid, 2.0, "4:1:0").json()["operation_id"]  # C 跳过

    import app.main as main
    from app.db import Store

    # 重启前已定位：可撤销操作与跳过状态保留
    monkeypatch.setattr(main, "store", Store(str(db_path)))
    got = client.get(f"/rehearsals/{rid}").json()
    assert got["undoable_operation_id"] == op
    assert got["origin"]["position"] == "4:1:0"
    assert {c["text"]: c["status"] for c in got["cues"]} == {
        "B": "delivered",
        "C": "skipped",
        "E": "pending",
    }

    # 重启后撤销仍生效
    data = undo(rid, op).json()
    assert [r["text"] for r in data["restored"]] == ["C"]
    assert data["origin"]["position"] == "1:1:0"

    # 再重启：撤销结果保留（不可重复撤销，C 待发并按原起点计时）
    monkeypatch.setattr(main, "store", Store(str(db_path)))
    assert undo(rid, op).status_code == 409
    got = client.get(f"/rehearsals/{rid}").json()
    assert got["undoable_operation_id"] is None
    assert next(c for c in got["cues"] if c["text"] == "C")["status"] == "pending"
    due = claim(rid, 4.0).json()
    assert [c["text"] for c in due["cues"]] == ["C"]


# ---------------------------------------------------------------------- #
# 旧版库迁移：旧跳过记录不属于任何可撤销操作，不影响新定位/撤销
# ---------------------------------------------------------------------- #
def test_old_skip_rows_have_null_locate_seq_and_undo_only_touches_own_skips(
    tmp_path,
):
    import json as _json

    import app.main as main
    from app.db import Store

    db_path = tmp_path / "old_undo.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE scores (id TEXT PRIMARY KEY, ppq INTEGER NOT NULL,
            time_signatures TEXT NOT NULL, tempo_points TEXT NOT NULL,
            revision INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE anchors (score_id TEXT NOT NULL, position_index INTEGER NOT NULL,
            position TEXT NOT NULL, locked INTEGER NOT NULL, total_ticks INTEGER NOT NULL,
            original_seconds REAL NOT NULL, current_seconds REAL NOT NULL,
            tick_seconds REAL NOT NULL, PRIMARY KEY (score_id, position_index));
        CREATE TABLE rehearsals (id TEXT PRIMARY KEY, score_id TEXT NOT NULL,
            last_elapsed REAL NOT NULL DEFAULT 0.0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE rehearsal_cues (rehearsal_id TEXT NOT NULL, cue_index INTEGER NOT NULL,
            position TEXT NOT NULL, text TEXT NOT NULL, total_ticks INTEGER NOT NULL,
            PRIMARY KEY (rehearsal_id, cue_index));
        CREATE TABLE cue_deliveries (rehearsal_id TEXT NOT NULL, cue_index INTEGER NOT NULL,
            trigger_seconds REAL NOT NULL, revision INTEGER NOT NULL,
            elapsed_seconds REAL NOT NULL,
            delivered_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (rehearsal_id, cue_index));
        CREATE TABLE cue_skips (rehearsal_id TEXT NOT NULL, cue_index INTEGER NOT NULL,
            revision INTEGER NOT NULL, elapsed_seconds REAL NOT NULL,
            skipped_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (rehearsal_id, cue_index));
        """
    )
    points = _json.dumps(
        [{"position": "1:1:0", "bpm": 120, "curve": "step"}], ensure_ascii=False
    )
    conn.execute(
        "INSERT INTO scores VALUES ('s1', 480, '[]', ?, 0, datetime('now'))",
        (points,),
    )
    conn.execute(
        "INSERT INTO anchors VALUES ('s1', 0, '1:1:0', 1, 0, 0.0, 0.0, 0.001)"
    )
    conn.execute(
        "INSERT INTO rehearsals (id, score_id, last_elapsed) VALUES ('r1','s1', 2.0)"
    )
    for idx, (pos, ticks) in enumerate(
        [("2:1:0", 1920), ("3:1:0", 3840), ("5:1:0", 7680)]
    ):
        conn.execute(
            "INSERT INTO rehearsal_cues VALUES ('r1', ?, ?, ?, ?)",
            (idx, pos, f"cue{idx}", ticks),
        )
    conn.execute(
        "INSERT INTO cue_deliveries VALUES ('r1', 0, 2.0, 0, 2.0, datetime('now'))"
    )
    # 旧版本写入的跳过记录（无 locate_seq 概念）
    conn.execute(
        "INSERT INTO cue_skips (rehearsal_id, cue_index, revision, elapsed_seconds) "
        "VALUES ('r1', 1, 0, 2.0)"
    )
    conn.commit()
    conn.close()

    # 打开旧库：自动迁移（rehearsals 补起点列、cue_skips 补 locate_seq、
    # 新建 locate_operations）。
    store = Store(str(db_path))
    main.store = store

    # 旧跳过记录的 locate_seq 为 NULL，且没有任何可撤销操作
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT locate_seq FROM cue_skips WHERE rehearsal_id = 'r1'"
    ).fetchone()
    conn.close()
    assert row[0] is None
    got = client.get("/rehearsals/r1").json()
    assert got["undoable_operation_id"] is None
    assert [c["status"] for c in got["cues"]] == [
        "delivered",
        "skipped",
        "pending",
    ]

    # 新定位（进度 2.0 -> 4:1:0；旧跳过的 cue1 已 settled，本次无新跳过）
    loc = client.post(
        "/rehearsals/r1/locate",
        json={"elapsed_seconds": 2.0, "position": "4:1:0"},
    )
    assert loc.status_code == 200, loc.text
    op = loc.json()["operation_id"]
    assert loc.json()["skipped"] == []

    # 撤销新定位：只恢复属于该操作的跳过（没有），旧跳过记录纹丝不动
    data = undo("r1", op).json()
    assert data["restored"] == []
    assert [c["status"] for c in data["cues"]] == [
        "delivered",
        "skipped",
        "pending",
    ]
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT cue_index, locate_seq FROM cue_skips WHERE rehearsal_id = 'r1'"
    ).fetchall()
    conn.close()
    assert rows == [(1, None)]
