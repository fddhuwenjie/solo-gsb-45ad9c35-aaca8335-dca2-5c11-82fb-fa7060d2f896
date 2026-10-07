"""排练副本：从当前已提交状态创建独立续练副本的端到端测试。

覆盖：副本返回新排练 ID 并沿用同一谱例；继承创建瞬间的领取进度、定位
计时起点、全部提示及已发/跳过记录（触发秒数与修订号原样保留）；批注的
文字、累计秒数、位置与修订号保留但使用新批注 ID；来源定位操作 ID 不
转移（副本不能撤销复制前的跳过），来源仍可撤销自己的定位且不改副本；
复制后两份排练分别领取/定位/登记批注/撤销互不改写；共享谱例发布后两边
待发提示各按新图计时、已发记录与批注冻结；副本自己的定位撤销不误删
复制来的跳过记录；来源不存在 404 且不留半份副本；复制取自一次完整的
已提交状态（复制后来源继续前进不改副本；与领取并发时不出现拼接态）；
新 Store 实例（模拟 Compose 重启）后副本状态保留、可继续领取。
"""

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """每个测试用独立 SQLite，并重新初始化模块级 store。"""
    db_path = tmp_path / "copy.db"
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
    resp = client.post(
        "/rehearsals", json={"score_id": score_id, "cues": cues}
    )
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
    resp = client.get(f"/rehearsals/{rid}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def cues_by_text(rehearsal):
    return {c["text"]: c for c in rehearsal["cues"]}


# ---------------------------------------------------------------------- #
# 继承：进度、定位起点、提示与已发/跳过记录、批注（新批注 ID）
# ---------------------------------------------------------------------- #
def test_copy_inherits_progress_origin_cue_states_and_annotations():
    score = create_score()
    rid = create_rehearsal(score["id"])["id"]
    claim(rid, 2.0)  # B 已发（rev0 / 2.0s），进度 2.0
    ann = annotate(rid, 2.0, "第二小节铜管偏慢")  # 未定位：落在 2:1:0
    assert ann.status_code == 201, ann.text
    source_ann_id = ann.json()["id"]
    op = locate(rid, 2.0, "4:1:0").json()["operation_id"]  # C 跳过

    resp = copy(rid)
    assert resp.status_code == 201, resp.text
    dup = resp.json()

    # 新排练 ID、同一谱例；继承领取进度与定位计时起点
    assert dup["id"] != rid
    assert dup["score_id"] == score["id"]
    assert dup["last_elapsed_seconds"] == 2.0
    assert dup["origin"] == {
        "position": "4:1:0",
        "total_ticks": 5760,
        "elapsed_seconds": 2.0,
    }
    # 来源的定位操作 ID 不转移：副本没有可撤销定位
    assert dup["undoable_operation_id"] is None

    # 全部提示及其已发/跳过记录原样继承
    by_text = cues_by_text(dup)
    assert by_text["B"]["status"] == "delivered"
    assert by_text["B"]["trigger_seconds"] == 2.0
    assert by_text["B"]["revision"] == 0
    assert by_text["C"]["status"] == "skipped"
    assert by_text["C"]["skipped_revision"] == 0
    assert by_text["E"]["status"] == "pending"
    assert by_text["E"]["trigger_seconds"] is None

    # 批注：文字、累计秒数、登记时的位置与修订号保留，但使用新批注 ID
    anns = client.get(f"/rehearsals/{dup['id']}/annotations").json()
    assert len(anns) == 1
    assert anns[0]["id"] != source_ann_id
    assert anns[0]["text"] == "第二小节铜管偏慢"
    assert anns[0]["elapsed_seconds"] == 2.0
    assert anns[0]["position"] == "2:1:0"
    assert anns[0]["total_ticks"] == 1920
    assert anns[0]["revision"] == 0

    # 来源状态不因复制改变（仍可撤销自己的定位）
    src = get(rid)
    assert src["undoable_operation_id"] == op
    assert src["last_elapsed_seconds"] == 2.0


def test_copy_of_fresh_rehearsal_uses_default_progress_and_origin():
    score = create_score()
    rid = create_rehearsal(score["id"])["id"]

    dup = copy(rid).json()
    assert dup["id"] != rid
    assert dup["last_elapsed_seconds"] == 0.0
    assert dup["origin"] == {
        "position": "1:1:0",
        "total_ticks": 0,
        "elapsed_seconds": 0.0,
    }
    assert all(c["status"] == "pending" for c in dup["cues"])
    assert client.get(f"/rehearsals/{dup['id']}/annotations").json() == []

    # 副本从起点独立领取
    got = claim(dup["id"], 2.0).json()
    assert [c["text"] for c in got["cues"]] == ["B"]
    assert get(rid)["last_elapsed_seconds"] == 0.0  # 来源不动


def test_copy_preserves_annotation_order_and_contents():
    score = create_score()
    rid = create_rehearsal(score["id"])["id"]
    claim(rid, 2.0)
    annotate(rid, 2.0, "第一条")
    claim(rid, 4.0)  # C 已发
    annotate(rid, 4.0, "第二条")
    source_ids = {
        a["id"] for a in client.get(f"/rehearsals/{rid}/annotations").json()
    }

    dup_id = copy(rid).json()["id"]
    anns = client.get(f"/rehearsals/{dup_id}/annotations").json()
    assert [a["text"] for a in anns] == ["第一条", "第二条"]  # 登记先后保留
    assert [a["elapsed_seconds"] for a in anns] == [2.0, 4.0]
    assert [a["position"] for a in anns] == ["2:1:0", "3:1:0"]
    assert source_ids.isdisjoint(a["id"] for a in anns)  # 全部新批注 ID


# ---------------------------------------------------------------------- #
# 定位操作 ID 不转移：副本不能撤销复制前的跳过；来源撤销不影响副本
# ---------------------------------------------------------------------- #
def test_copy_cannot_undo_precopy_skips_with_source_operation_id():
    score = create_score()
    rid = create_rehearsal(score["id"])["id"]
    claim(rid, 2.0)
    op = locate(rid, 2.0, "4:1:0").json()["operation_id"]  # C 跳过

    dup_id = copy(rid).json()["id"]

    # 副本用来源的操作 ID 撤销：操作不存在于副本 -> 409，状态不变
    resp = undo(dup_id, op)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"][0]["reason"] == "not_found"
    dup = get(dup_id)
    assert cues_by_text(dup)["C"]["status"] == "skipped"
    assert dup["origin"]["position"] == "4:1:0"


def test_source_can_still_undo_own_locate_without_touching_copy():
    score = create_score()
    rid = create_rehearsal(score["id"])["id"]
    claim(rid, 2.0)
    op = locate(rid, 2.0, "4:1:0").json()["operation_id"]  # C 跳过
    dup_id = copy(rid).json()["id"]

    # 来源撤销自己的定位：起点恢复、C 在来源中恢复待发
    resp = undo(rid, op)
    assert resp.status_code == 200, resp.text
    src = resp.json()
    assert src["origin"] == {
        "position": "1:1:0",
        "total_ticks": 0,
        "elapsed_seconds": 0.0,
    }
    assert cues_by_text(src)["C"]["status"] == "pending"

    # 副本不变：起点仍是定位后的 4:1:0，C 仍为跳过
    dup = get(dup_id)
    assert dup["origin"]["position"] == "4:1:0"
    assert cues_by_text(dup)["C"]["status"] == "skipped"
    assert cues_by_text(dup)["C"]["skipped_revision"] == 0


def test_copy_own_locate_undo_does_not_restore_copied_skips():
    """副本自己定位后再撤销：只恢复本次跳过的提示，复制来的跳过不动。"""
    score = create_score()
    rid = create_rehearsal(score["id"])["id"]
    claim(rid, 2.0)
    locate(rid, 2.0, "4:1:0")  # 来源中 C 跳过
    dup_id = copy(rid).json()["id"]  # 副本继承：B 已发、C 跳过、E 待发

    # 副本自己定位到 6:1:0：E（待发且在目标之前）被跳过
    resp = locate(dup_id, 2.0, "6:1:0")
    assert resp.status_code == 200, resp.text
    op2 = resp.json()["operation_id"]
    assert [c["text"] for c in resp.json()["skipped"]] == ["E"]

    # 撤销副本自己的定位：E 恢复待发；复制来的 C 跳过记录不受影响
    resp = undo(dup_id, op2)
    assert resp.status_code == 200, resp.text
    assert [c["text"] for c in resp.json()["restored"]] == ["E"]
    dup = get(dup_id)
    by_text = cues_by_text(dup)
    assert by_text["B"]["status"] == "delivered"
    assert by_text["C"]["status"] == "skipped"  # 复制前的跳过不可撤销
    assert by_text["C"]["skipped_revision"] == 0
    assert by_text["E"]["status"] == "pending"
    assert dup["origin"]["position"] == "4:1:0"  # 恢复为复制时的起点


# ---------------------------------------------------------------------- #
# 独立：复制后两份排练分别领取/定位/登记批注，互不改写
# ---------------------------------------------------------------------- #
def test_copy_and_source_continue_independently():
    score = create_score()
    rid = create_rehearsal(score["id"])["id"]
    claim(rid, 2.0)  # B 已发，进度 2.0
    dup_id = copy(rid).json()["id"]

    # 副本前进到 4.0：C 在副本中发放；来源进度与提示状态不变
    got = claim(dup_id, 4.0).json()
    assert [c["text"] for c in got["cues"]] == ["C"]
    src = get(rid)
    assert src["last_elapsed_seconds"] == 2.0
    assert cues_by_text(src)["C"]["status"] == "pending"

    # 来源定位到 4:1:0：C 在来源中记为跳过；副本起点与已发记录不变
    locate(rid, 2.0, "4:1:0")
    dup = get(dup_id)
    assert dup["last_elapsed_seconds"] == 4.0
    assert dup["origin"]["position"] == "1:1:0"
    assert cues_by_text(dup)["C"]["status"] == "delivered"

    # 副本在当前进度登记批注；来源批注列表仍为空
    ann = annotate(dup_id, 4.0, "副本的批注")
    assert ann.status_code == 201, ann.text
    assert ann.json()["position"] == "3:1:0"  # 副本未定位，按绝对秒数反推
    assert client.get(f"/rehearsals/{rid}/annotations").json() == []

    # 来源在自己的进度登记批注（落在定位目标）；副本批注不增加
    ann = annotate(rid, 2.0, "来源的批注")
    assert ann.json()["position"] == "4:1:0"
    dup_anns = client.get(f"/rehearsals/{dup_id}/annotations").json()
    assert [a["text"] for a in dup_anns] == ["副本的批注"]


def test_copy_reflects_committed_state_at_copy_instant():
    """复制取创建瞬间的状态：此后来源继续前进不改变副本。"""
    score = create_score()
    rid = create_rehearsal(score["id"])["id"]
    claim(rid, 2.0)  # B 已发，进度 2.0
    dup_id = copy(rid).json()["id"]

    # 来源继续前进：领取 C、再定位跳过 E
    claim(rid, 4.0)
    locate(rid, 4.0, "6:1:0")

    dup = get(dup_id)
    assert dup["last_elapsed_seconds"] == 2.0
    assert dup["origin"]["position"] == "1:1:0"
    by_text = cues_by_text(dup)
    assert by_text["B"]["status"] == "delivered"
    assert by_text["C"]["status"] == "pending"
    assert by_text["E"]["status"] == "pending"


# ---------------------------------------------------------------------- #
# 共享谱例发布：两边待发提示各按新图计时，已发记录与批注冻结
# ---------------------------------------------------------------------- #
def test_publish_after_copy_retimes_pending_in_both_and_freezes_records():
    score = create_score()
    rid = create_rehearsal(
        score["id"], [{"position": "2:1:0", "text": "B"},
                      {"position": "5:1:0", "text": "E"}]
    )["id"]
    claim(rid, 2.0)  # B 已发（rev0 / 2.0s）
    annotate(rid, 2.0, "发布前的批注")  # 落在 2:1:0，rev0
    dup_id = copy(rid).json()["id"]

    # 共享谱例发布 60 BPM（rev1）：一小节 2.0s -> 4.0s
    pub = client.post(
        f"/scores/{score['id']}/revisions/0/publish",
        json={"tempo_points": SLOW_POINTS},
    )
    assert pub.status_code == 200, pub.text

    # 两边待发提示各按新图计时：E 在 60 BPM 下触发于 16.0s
    for some_id in (rid, dup_id):
        got = claim(some_id, 15.9).json()
        assert got["cues"] == []
        got = claim(some_id, 16.0).json()
        assert [c["text"] for c in got["cues"]] == ["E"]
        assert got["cues"][0]["trigger_seconds"] == 16.0
        assert got["cues"][0]["revision"] == 1

        detail = get(some_id)
        by_text = cues_by_text(detail)
        # 已发记录保持发放时的值（rev0 / 2.0s），不随发布改变
        assert by_text["B"]["trigger_seconds"] == 2.0
        assert by_text["B"]["revision"] == 0
        # 批注保持登记时的位置与修订号
        anns = client.get(f"/rehearsals/{some_id}/annotations").json()
        assert len(anns) == 1
        assert anns[0]["position"] == "2:1:0"
        assert anns[0]["revision"] == 0


# ---------------------------------------------------------------------- #
# 404：来源不存在，不留下半份副本
# ---------------------------------------------------------------------- #
def test_copy_unknown_rehearsal_is_404_and_leaves_no_partial_copy(fresh_db):
    score = create_score()
    rid = create_rehearsal(score["id"])["id"]

    resp = copy("no-such-rehearsal")
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"][0]["loc"] == ["path", "rehearsal_id"]

    # 库里只有来源一个排练：没有留下半份副本
    conn = sqlite3.connect(str(fresh_db))
    try:
        rows = conn.execute(
            "SELECT id FROM rehearsals"
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == [rid]


# ---------------------------------------------------------------------- #
# 并发：复制与领取交错时，副本取自一次完整的已提交状态
# ---------------------------------------------------------------------- #
def test_concurrent_copy_and_claim_read_one_committed_state(fresh_db):
    from app.db import Store
    from app.main import _due_cues

    score = create_score()
    rid = create_rehearsal(score["id"])["id"]
    claim(rid, 2.0)  # B 已发，进度 2.0；C(4.0s)、E(8.0s) 待发
    db_path = fresh_db

    stores = [Store(str(db_path)) for _ in range(2)]  # 独立连接
    barrier = threading.Barrier(2)
    outcome: dict = {}
    errors: list[Exception] = []

    def do_copy(store):
        try:
            barrier.wait(timeout=10)
            outcome["copy"] = store.copy_rehearsal(rid)
        except Exception as exc:  # pragma: no cover - 失败时下面断言暴露
            errors.append(exc)

    def do_claim(store):
        try:
            barrier.wait(timeout=10)
            store.claim_cues(rid, 8.0, lambda s, p: _due_cues(s, p, 8.0))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=do_copy, args=(stores[0],)),
        threading.Thread(target=do_claim, args=(stores[1],)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors
    dup = outcome["copy"]
    by_text = cues_by_text(dup)
    if dup["last_elapsed_seconds"] == 2.0:
        # 复制整体先于领取提交：只有 B 已发
        assert by_text["B"]["status"] == "delivered"
        assert by_text["C"]["status"] == "pending"
        assert by_text["E"]["status"] == "pending"
    else:
        # 复制整体后于领取提交：全部已发，进度 8.0；
        # 不允许出现“进度 8.0 但 C/E 未发”之类的拼接态
        assert dup["last_elapsed_seconds"] == 8.0
        assert all(c["status"] == "delivered" for c in dup["cues"])

    # 来源最终状态：领取只生效一次，全部已发
    src = get(rid)
    assert src["last_elapsed_seconds"] == 8.0
    assert all(c["status"] == "delivered" for c in src["cues"])


# ---------------------------------------------------------------------- #
# SQLite 持久化：新 Store 实例（模拟 Compose 重启）后副本状态保留
# ---------------------------------------------------------------------- #
def test_copy_survives_store_restart(fresh_db, monkeypatch):
    db_path = fresh_db
    score = create_score()
    rid = create_rehearsal(score["id"])["id"]
    claim(rid, 2.0)
    locate(rid, 2.0, "4:1:0")  # C 跳过，起点 4:1:0 / 2.0s
    annotate(rid, 2.0, "重启前的批注")
    dup_id = copy(rid).json()["id"]

    # 重新打开同一文件（不删除卷的 Compose 重启等价场景）
    import app.main as main
    from app.db import Store

    monkeypatch.setattr(main, "store", Store(str(db_path)))

    dup = get(dup_id)
    assert dup["last_elapsed_seconds"] == 2.0
    assert dup["origin"] == {
        "position": "4:1:0",
        "total_ticks": 5760,
        "elapsed_seconds": 2.0,
    }
    assert dup["undoable_operation_id"] is None
    by_text = cues_by_text(dup)
    assert by_text["B"]["status"] == "delivered"
    assert by_text["C"]["status"] == "skipped"
    assert by_text["E"]["status"] == "pending"
    anns = client.get(f"/rehearsals/{dup_id}/annotations").json()
    assert [a["text"] for a in anns] == ["重启前的批注"]

    # 重启后副本可继续领取：E 按“起点 4:1:0 + 一小节 2.0s”触发于 4.0s
    got = claim(dup_id, 4.0).json()
    assert [c["text"] for c in got["cues"]] == ["E"]
    assert got["cues"][0]["trigger_seconds"] == 4.0
    # 来源进度不被副本推进
    assert get(rid)["last_elapsed_seconds"] == 2.0
