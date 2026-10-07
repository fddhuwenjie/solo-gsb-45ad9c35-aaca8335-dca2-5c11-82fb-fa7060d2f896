"""续练副本：从现有排练当前状态创建独立副本、两路分别续练的端到端测试。

覆盖：副本沿用同一谱例并继承领取进度/定位起点/全部提示及已发跳过记录；
批注保留文字/秒数/位置/修订号但换新批注 ID；定位操作 ID 不转移（副本
undoable_operation_id 为 null、来源操作 ID 对副本 409、复制前的跳过
不可被副本撤销）；来源撤销自己的定位不影响副本；复制后两路独立领取/
定位/批注/撤销；发布后两边待发提示各按新图计时而已发记录与批注冻结；
来源不存在 404 且不留半份副本；复制与领取并发时取自同一已提交状态；
重启后副本状态保留。
"""

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """每个测试用独立 SQLite，并重新初始化模块级 store。"""
    db_path = tmp_path / "copies.db"
    monkeypatch.setenv("TEMPO_DB_PATH", str(db_path))

    import app.main as main
    from app.db import Store

    monkeypatch.setattr(main, "store", Store(str(db_path)))
    yield db_path


from app.main import app  # noqa: E402

client = TestClient(app)

# 120 BPM、4/4、PPQ=480：一拍 0.5s、一小节 2.0s
# 2:1:0 -> 2.0s，3:1:0 -> 4.0s，4:1:0 -> 6.0s，5:1:0 -> 8.0s
SCORE_BODY = {
    "ppq": 480,
    "time_signatures": [],
    "tempo_points": [{"position": "1:1:0", "bpm": 120, "curve": "step"}],
    "anchors": [{"position": "1:1:0", "locked": True}],
}
# 替换表：整体降到 60 BPM（所有触发秒数翻倍）
SLOW_POINTS = [{"position": "1:1:0", "bpm": 60, "curve": "step"}]

CUES_BCE = [
    {"position": "2:1:0", "text": "B"},  # 2.0s
    {"position": "3:1:0", "text": "C"},  # 4.0s
    {"position": "5:1:0", "text": "E"},  # 8.0s
]


def create_score():
    resp = client.post("/scores", json=SCORE_BODY)
    assert resp.status_code == 201, resp.text
    return resp.json()


def create_rehearsal(score_id, cues=CUES_BCE):
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
    return client.post(f"/rehearsals/{rid}/locate/{operation_id}/undo")


def annotate(rid, elapsed, text):
    return client.post(
        f"/rehearsals/{rid}/annotations",
        json={"elapsed_seconds": elapsed, "text": text},
    )


def copy(rid):
    return client.post(f"/rehearsals/{rid}/copies")


def get(rid):
    return client.get(f"/rehearsals/{rid}").json()


def statuses(rid):
    return {c["text"]: c["status"] for c in get(rid)["cues"]}


# ---------------------------------------------------------------------- #
# 创建副本：继承进度/起点/提示状态，返回新排练 ID
# ---------------------------------------------------------------------- #
def test_copy_inherits_progress_origin_and_cue_states():
    score = create_score()
    rid = create_rehearsal(score["id"])["id"]
    claim(rid, 2.0)  # B 已发，进度 2.0
    op = locate(rid, 2.0, "4:1:0").json()["operation_id"]  # C 跳过

    resp = copy(rid)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    # 新排练 ID，沿用同一谱例
    assert data["id"] != rid
    assert data["score_id"] == score["id"]
    # 继承创建瞬间的领取进度与定位计时起点
    assert data["last_elapsed_seconds"] == 2.0
    assert data["origin"] == {
        "position": "4:1:0",
        "total_ticks": 5760,
        "elapsed_seconds": 2.0,
    }
    # 全部提示及已发/跳过记录原样继承
    cues = {c["text"]: c for c in data["cues"]}
    assert cues["B"]["status"] == "delivered"
    assert cues["B"]["trigger_seconds"] == 2.0
    assert cues["B"]["revision"] == 0
    assert cues["C"]["status"] == "skipped"
    assert cues["C"]["skipped_revision"] == 0
    assert cues["E"]["status"] == "pending"
    # 定位操作 ID 不转移：副本没有可撤销的定位
    assert data["undoable_operation_id"] is None
    # 来源不受影响：自己的定位仍可撤销
    assert get(rid)["undoable_operation_id"] == op


def test_copy_of_untouched_rehearsal():
    score = create_score()
    rid = create_rehearsal(score["id"])["id"]
    data = copy(rid).json()
    assert data["last_elapsed_seconds"] == 0.0
    assert data["origin"] == {
        "position": "1:1:0",
        "total_ticks": 0,
        "elapsed_seconds": 0.0,
    }
    assert [c["status"] for c in data["cues"]] == ["pending"] * 3
    assert client.get(f"/rehearsals/{data['id']}/annotations").json() == []


# ---------------------------------------------------------------------- #
# 批注：内容保留，批注 ID 换新
# ---------------------------------------------------------------------- #
def test_copy_annotations_keep_content_with_new_ids():
    score = create_score()
    rid = create_rehearsal(score["id"])["id"]
    claim(rid, 2.0)
    a1 = annotate(rid, 2.0, "进副歌时铜管稍慢").json()  # 未定位：2:1:0
    locate(rid, 2.0, "4:1:0")
    a2 = annotate(rid, 2.0, "跳排：从第四小节重来").json()  # 定位目标：4:1:0

    copy_id = copy(rid).json()["id"]
    src_anns = client.get(f"/rehearsals/{rid}/annotations").json()
    dst_anns = client.get(f"/rehearsals/{copy_id}/annotations").json()
    assert [a["id"] for a in src_anns] == [a1["id"], a2["id"]]
    assert len(dst_anns) == 2
    for src, dst in zip(src_anns, dst_anns):
        # 文字、累计秒数、登记时的位置与修订号保留
        assert dst["text"] == src["text"]
        assert dst["elapsed_seconds"] == src["elapsed_seconds"]
        assert dst["position"] == src["position"]
        assert dst["total_ticks"] == src["total_ticks"]
        assert dst["revision"] == src["revision"]
        # 但使用新的批注 ID
        assert dst["id"] != src["id"]
    assert dst_anns[0]["position"] == "2:1:0"
    assert dst_anns[1]["position"] == "4:1:0"

    # 副本后续登记的批注接续编号，与来源互不影响
    claim(copy_id, 4.0)  # E 按定位起点 2.0+2.0 到点
    a3 = annotate(copy_id, 4.0, "副本自己的批注").json()
    assert a3["position"] == "5:1:0"  # 起点 4:1:0 + 2.0s
    assert a3["id"] not in {a["id"] for a in src_anns}
    dst_anns = client.get(f"/rehearsals/{copy_id}/annotations").json()
    assert [a["text"] for a in dst_anns][-1] == "副本自己的批注"
    # 来源仍只有复制前的两条
    assert len(client.get(f"/rehearsals/{rid}/annotations").json()) == 2


# ---------------------------------------------------------------------- #
# 定位操作不转移：副本不能撤销复制前的跳过；来源撤销不影响副本
# ---------------------------------------------------------------------- #
def test_copy_has_no_undoable_operation_and_source_op_id_is_not_found():
    score = create_score()
    rid = create_rehearsal(score["id"])["id"]
    claim(rid, 2.0)
    op = locate(rid, 2.0, "4:1:0").json()["operation_id"]

    copy_id = copy(rid).json()["id"]
    assert get(copy_id)["undoable_operation_id"] is None
    # 来源的操作 ID 对副本无效（不属于该排练）
    resp = undo(copy_id, op)
    assert resp.status_code == 409
    assert resp.json()["detail"][0]["reason"] == "not_found"
    # 副本状态不变
    assert statuses(copy_id) == {
        "B": "delivered",
        "C": "skipped",
        "E": "pending",
    }
    # 来源仍可撤销自己的定位
    assert undo(rid, op).status_code == 200


def test_source_undo_does_not_change_copy():
    score = create_score()
    rid = create_rehearsal(score["id"])["id"]
    claim(rid, 2.0)
    op = locate(rid, 2.0, "4:1:0").json()["operation_id"]  # C 跳过
    copy_id = copy(rid).json()["id"]

    data = undo(rid, op).json()
    assert data["origin"]["position"] == "1:1:0"
    assert [r["text"] for r in data["restored"]] == ["C"]
    # 副本保持复制时的状态：起点与跳过不变
    got = get(copy_id)
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


def test_copy_cannot_undo_pre_copy_skips_via_own_locate():
    # 副本之后自己的定位/撤销只影响复制后的操作：复制前的跳过
    # （locate_seq 为 NULL）不会被副本自己的撤销恢复。
    score = create_score()
    rid = create_rehearsal(score["id"])["id"]
    claim(rid, 2.0)
    locate(rid, 2.0, "4:1:0")  # C 跳过（复制前）
    copy_id = copy(rid).json()["id"]

    # 副本自己定位到 6:1:0：E（7680 < 9600）被跳过
    op_c = locate(copy_id, 2.0, "6:1:0").json()["operation_id"]
    assert statuses(copy_id)["E"] == "skipped"
    # 撤销副本自己的定位：只恢复 E；复制前跳过的 C 保持跳过
    data = undo(copy_id, op_c).json()
    assert [r["text"] for r in data["restored"]] == ["E"]
    assert {c["text"]: c["status"] for c in data["cues"]} == {
        "B": "delivered",
        "C": "skipped",
        "E": "pending",
    }


# ---------------------------------------------------------------------- #
# 复制后两路独立：领取/定位/批注/撤销互不改写
# ---------------------------------------------------------------------- #
def test_two_rehearsals_continue_independently():
    score = create_score()
    rid = create_rehearsal(score["id"])["id"]
    claim(rid, 2.0)  # B
    locate(rid, 2.0, "4:1:0")  # C 跳过，E 待发（2.0 + 2.0 = 4.0 到点）
    copy_id = copy(rid).json()["id"]

    # 来源推进到 4.0 领到 E；副本按自己的节奏走：3.0 未到点，6.0 才领
    assert [c["text"] for c in claim(rid, 4.0).json()["cues"]] == ["E"]
    assert claim(copy_id, 3.0).json()["cues"] == []
    assert [c["text"] for c in claim(copy_id, 6.0).json()["cues"]] == ["E"]
    assert get(rid)["last_elapsed_seconds"] == 4.0
    assert get(copy_id)["last_elapsed_seconds"] == 6.0

    # 来源登记批注（起点 4:1:0 + 2.0s -> 5:1:0），副本批注不变
    annotate(rid, 4.0, "来源的批注")
    assert client.get(f"/rehearsals/{copy_id}/annotations").json() == []

    # 副本回跳到 2:1:0：不重发已发、不恢复已跳过；来源起点不变
    data = locate(copy_id, 6.0, "2:1:0").json()
    assert data["skipped"] == []
    assert {c["text"]: c["status"] for c in data["cues"]} == {
        "B": "delivered",
        "C": "skipped",
        "E": "delivered",
    }
    assert get(rid)["origin"]["position"] == "4:1:0"
    assert get(copy_id)["origin"]["position"] == "2:1:0"

    # 副本撤销自己的回跳；来源不受影响
    op_c = data["operation_id"]
    undone = undo(copy_id, op_c).json()
    assert undone["origin"]["position"] == "4:1:0"
    assert get(rid)["origin"]["position"] == "4:1:0"
    assert get(rid)["last_elapsed_seconds"] == 4.0


# ---------------------------------------------------------------------- #
# 共享谱例发布：两边待发提示各按新图计时，已发记录与批注保持原样
# ---------------------------------------------------------------------- #
def test_publish_retimes_both_sides_pending_cues_only():
    score = create_score()
    sid = score["id"]
    rid = create_rehearsal(sid)["id"]
    claim(rid, 2.0)  # B 已发（rev0 / 2.0s）
    annotate(rid, 2.0, "进副歌时铜管稍慢")  # rev0，位置 2:1:0
    locate(rid, 2.0, "4:1:0")  # C 跳过，E 待发
    copy_id = copy(rid).json()["id"]

    # 来源撤销自己的定位（C 恢复待发、起点回到原点）；副本不变
    assert undo(rid, get(rid)["undoable_operation_id"]).status_code == 200
    assert statuses(rid) == {"B": "delivered", "C": "pending", "E": "pending"}
    assert statuses(copy_id) == {
        "B": "delivered",
        "C": "skipped",
        "E": "pending",
    }

    # 发布 60 BPM（rev0 -> rev1）
    pub = client.post(
        f"/scores/{sid}/revisions/0/publish", json={"tempo_points": SLOW_POINTS}
    )
    assert pub.status_code == 200, pub.text

    # 来源（起点已恢复为原点）：C 按新图绝对 8.0s 到点
    due_src = claim(rid, 8.0).json()
    assert [c["text"] for c in due_src["cues"]] == ["C"]
    assert due_src["cues"][0]["trigger_seconds"] == 8.0
    assert due_src["cues"][0]["revision"] == 1
    # 副本（起点仍是 4:1:0/2.0s）：E 按新图 2.0 + 4.0 = 6.0s 到点
    due_dst = claim(copy_id, 6.0).json()
    assert [c["text"] for c in due_dst["cues"]] == ["E"]
    assert due_dst["cues"][0]["trigger_seconds"] == 6.0
    assert due_dst["cues"][0]["revision"] == 1

    # 两边已发记录与批注保持原样（rev0 / 2.0s / 2:1:0）
    for rid_x in (rid, copy_id):
        got = get(rid_x)
        b = next(c for c in got["cues"] if c["text"] == "B")
        assert b["trigger_seconds"] == 2.0
        assert b["revision"] == 0
        anns = client.get(f"/rehearsals/{rid_x}/annotations").json()
        assert len(anns) == 1
        assert anns[0]["position"] == "2:1:0"
        assert anns[0]["revision"] == 0


# ---------------------------------------------------------------------- #
# 404：来源不存在，不留下半份副本
# ---------------------------------------------------------------------- #
def test_copy_unknown_rehearsal_is_404_and_leaves_no_partial_copy(fresh_db):
    resp = copy("deadbeef")
    assert resp.status_code == 404
    assert resp.json()["detail"][0]["loc"] == ["path", "rehearsal_id"]
    # 不留下半份副本：库里没有任何排练及其明细行
    conn = sqlite3.connect(str(fresh_db))
    try:
        for table in (
            "rehearsals",
            "rehearsal_cues",
            "cue_deliveries",
            "cue_skips",
            "rehearsal_annotations",
        ):
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert n == 0, table
    finally:
        conn.close()


# ---------------------------------------------------------------------- #
# 并发：复制与领取交错时，副本取自一次完整的已提交状态
# ---------------------------------------------------------------------- #
def test_concurrent_copy_and_claim_are_serialized(fresh_db):
    """复制与领取并发：每个副本要么取自领取前、要么取自领取后的完整状态。

    一半连接创建副本，一半连接领取到 1000s（全部提示到点）。串行化后，
    每个副本要么是“领取前”（进度 0.0、无发放记录），要么是“领取后”
    （进度 1000.0、全部 6 条提示的发放记录与来源一致），绝不出现
    “进度已前进但发放记录只复制了一半”的拼接状态。
    """
    from app.db import Store
    from app.main import _due_cues

    score = create_score()
    rid = create_rehearsal(
        score["id"],
        [{"position": f"{m}:1:0", "text": f"cue{m}"} for m in range(2, 8)],
    )["id"]
    db_path = fresh_db

    n_workers = 6
    stores = [Store(str(db_path)) for _ in range(n_workers)]
    barrier = threading.Barrier(n_workers)
    copy_ids: list[str] = []

    def worker(i, store):
        barrier.wait(timeout=10)
        if i % 2 == 0:
            copy_ids.append(store.copy_rehearsal(rid)["id"])
        else:
            store.claim_cues(
                rid, 1000.0, lambda s, p: _due_cues(s, p, 1000.0)
            )

    threads = [
        threading.Thread(target=worker, args=(i, s))
        for i, s in enumerate(stores)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(copy_ids) == n_workers // 2
    conn = sqlite3.connect(str(db_path))
    try:
        src_deliveries = {
            row[0]
            for row in conn.execute(
                "SELECT cue_index FROM cue_deliveries WHERE rehearsal_id = ?",
                (rid,),
            )
        }
        assert src_deliveries == set(range(6))
        for cid in copy_ids:
            last_elapsed, origin_ticks = conn.execute(
                "SELECT last_elapsed, origin_ticks FROM rehearsals WHERE id = ?",
                (cid,),
            ).fetchone()
            n_cues = conn.execute(
                "SELECT COUNT(*) FROM rehearsal_cues WHERE rehearsal_id = ?",
                (cid,),
            ).fetchone()[0]
            deliveries = {
                row[0]
                for row in conn.execute(
                    "SELECT cue_index FROM cue_deliveries WHERE rehearsal_id = ?",
                    (cid,),
                )
            }
            # 提示表总是完整复制
            assert n_cues == 6
            # 进度与发放记录来自同一已提交状态：要么都在领取前
            # （0.0 / 无发放），要么都在领取后（1000.0 / 与来源一致）
            if last_elapsed == 0.0:
                assert deliveries == set()
            else:
                assert last_elapsed == 1000.0
                assert deliveries == src_deliveries
            assert origin_ticks == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------- #
# 持久化：副本状态重启后保留，可继续领取
# ---------------------------------------------------------------------- #
def test_copy_state_survives_store_restart(fresh_db, monkeypatch):
    db_path = fresh_db
    score = create_score()
    rid = create_rehearsal(score["id"])["id"]
    claim(rid, 2.0)  # B
    locate(rid, 2.0, "4:1:0")  # C 跳过
    ann_id = annotate(rid, 2.0, "跳排备注").json()["id"]
    copy_id = copy(rid).json()["id"]
    copy_ann_id = client.get(f"/rehearsals/{copy_id}/annotations").json()[0]["id"]
    assert copy_ann_id != ann_id

    import app.main as main
    from app.db import Store

    # 模拟 Compose 重启：新 Store 实例打开同一库
    monkeypatch.setattr(main, "store", Store(str(db_path)))
    got = get(copy_id)
    assert got["last_elapsed_seconds"] == 2.0
    assert got["origin"] == {
        "position": "4:1:0",
        "total_ticks": 5760,
        "elapsed_seconds": 2.0,
    }
    assert got["undoable_operation_id"] is None
    assert {c["text"]: c["status"] for c in got["cues"]} == {
        "B": "delivered",
        "C": "skipped",
        "E": "pending",
    }
    anns = client.get(f"/rehearsals/{copy_id}/annotations").json()
    assert [a["id"] for a in anns] == [copy_ann_id]
    assert anns[0]["position"] == "4:1:0"

    # 重启后副本可继续领取（E 按定位起点 2.0 + 2.0 = 4.0s 到点）
    due = claim(copy_id, 4.0).json()
    assert [c["text"] for c in due["cues"]] == ["E"]
    # 来源不受副本领取影响
    assert get(rid)["last_elapsed_seconds"] == 2.0
