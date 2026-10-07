"""谱例修订与排练提示的 SQLite 持久化。

保存内容（沿用 POST /convert 的同一套拍号/速度点结构，以 JSON 原样落库）：

* ``scores``：谱例 ID、PPQ、拍号表、速度图（速度点表）、修订号；
* ``anchors``：录音锚点位置、锁定标志、创建时按原图计算并冻结的
  秒数 ``original_seconds`` 与“该位置一个 tick 的时长” ``tick_seconds``
  （锁定偏移阈值）；
* ``rehearsals``：排练（关联谱例 ID、已领取进度 ``last_elapsed``，
  只能前进；以及定位计时起点 ``origin_ticks`` / ``origin_elapsed``：
  未定位时为 ``0`` / ``0.0``，触发秒数 = 定位秒数 + 起点到提示的速度图
  时长）；
* ``rehearsal_cues``：排练提示（位置、文字、创建时按谱例拍号展开的
  tick 坐标——PPQ/拍号不随速度图修订变化，故坐标恒定，只有触发秒数
  随修订重算）；
* ``cue_deliveries``：发放记录（触发秒数、计算所用的速度图修订号、
  领取时的累计秒数），主键 ``(rehearsal_id, cue_index)`` 兜底保证
  同一提示最多发放一次；
* ``cue_skips``：定位跳过记录（定位时该修订号、提交秒数，以及产生该
  记录的定位操作序号 ``locate_seq``——撤销定位时只删除对应那次定位写入
  的跳过记录），主键 ``(rehearsal_id, cue_index)`` 兜底保证同一提示最多
  跳过一次，且与 ``cue_deliveries`` 互斥。
* ``locate_operations``：每次成功定位一条（``operation_id`` 即定位响应
  返回、撤销时提交的操作 ID）。保存**撤销所需的全部现场**：定位目标与
  提交秒数、**原计时起点**（``prev_origin_*``）、定位瞬间的谱例修订号，
  以及定位前已发提示数与最大批注序号等快照（撤销时逐项核对，定位后只要
  进度前进、提示发放、登记批注或速度图发布，操作即过期）。状态：
  ``active``（最近一次、可撤销）/ ``superseded``（被后续定位取代，连续
  定位时只有最后一次可撤销）/ ``undone``（已撤销，不可重复撤销）。
* ``rehearsal_annotations``：排练现场文字批注（登记先后 ``seq``、
  非空文字、提交的累计秒数，以及登记瞬间按当前定位起点与当前已提交
  速度图反推并量化的乐谱位置/tick 与所用修订号——全部冻结，速度图
  发布或再次定位后保持登记时的值）。

发布是单事务：``BEGIN IMMEDIATE`` 取得写锁后按 revision 做乐观并发控制
（预期修订号不匹配 -> 409），成功则一次更新速度图、未锁定锚点秒数和
修订号，锁定锚点秒数不变。进程内再用一把锁串行化写事务。

领取也是单事务：``BEGIN IMMEDIATE`` 后在同一快照内读出谱例当前已提交
的速度图与修订号，对未发（且未被定位跳过）提示计算触发秒数、写入发放
记录并推进领取进度。与发布交错时二者必居其一（读到完整的旧修订或完整
的新修订），因此**计算到点所用的修订号必然等于写入发放记录的修订号**；
并发领取被进程内写锁与 SQLite 写锁串行化，同一提示最多发放一次。

定位（``locate``）同样是单事务：提交的累计秒数必须**等于**已领取进度
（否则 :class:`OriginMismatch` -> 409，状态不变），随后把尚未发放且
位于目标 tick 之前的提示记入 ``cue_skips``（与定位时的修订号、提交秒数
及本次定位操作序号），并以（目标 tick, 该累计秒数）更新计时起点；
此后触发秒数为
``origin_elapsed + (seconds_at(cue) - seconds_at(origin))``。发布只改
速度图：待发提示按最新修订重算触发秒数，而定位起点（tick/累计秒数）、
已发记录与跳过状态都保持不变。定位/领取/发布共用同一把写锁串行化，
同一条提示不可能既跳过又发放。每次成功定位还会写入
``locate_operations``：保存原计时起点、定位前已发数/最大批注序号、
定位瞬间修订号等撤销现场，并把更早的未撤销定位标记为 ``superseded``
（连续定位只有最后一次可撤销），响应返回该定位的操作 ID。

撤销定位（``undo_locate``）也是单事务：提交某次定位的操作 ID。仅当该
操作存在、状态为 ``active``（最近一次、未被后续定位取代、未撤销过），
且自该定位以来进度未前进、没有提示发放、没有批注登记、谱例修订号未变
（速度图未发布）时才允许撤销：把计时起点恢复为该定位保存的**原起点**，
删除该次定位写入 ``cue_skips`` 的记录（这些提示恢复待发；更早发放或
跳过的提示一律不动），并把操作标记为 ``undone``。上述任一条件不满足
（操作 ID 不存在/已撤销/已被取代，或定位后发生过进度前进、提示发放、
批注登记、速度图发布）都抛 :class:`UndoNotAllowed`（-> 409）且状态
完全不变。匹配操作 ID、检查状态与恢复跳过在**同一已提交状态**（同一
``BEGIN IMMEDIATE`` 事务快照）内完成，与领取/批注/定位/发布交错时
二者必居其一；成功后待发提示自然按恢复的起点与当前速度图定时，全部
状态持久化，查询与重启后一致。

批注登记（``add_annotation``）同样是单事务：提交的累计秒数必须
**等于**已领取进度（否则 :class:`AnnotationProgressMismatch` -> 409，
不写入）。校验通过后，在同一 ``BEGIN IMMEDIATE`` 事务快照内把当前
定位起点（tick/累计秒数）与谱例当前已提交速度图/修订号交给引擎层
注入的纯计算回调，由它反推出乐谱位置并量化到最近 tick；因此进度、
起点与速度图必然来自**同一已提交状态**，写入的修订号就是反推所用的
修订号。位置字符串/tick/修订号在写入时冻结：此后速度图发布或再次
定位只改变排练当前状态，既有批注各列保持登记时的值。

查询同样是单事务：``GET /scores`` / ``GET /rehearsals`` 在同一显式读
事务内读取主行与全部明细行，二者共用一个已提交快照，避免与发布/领取
交错时跨修订拼接（旧修订号配新锚点秒数）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from typing import Any, Callable, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scores (
    id              TEXT PRIMARY KEY,
    ppq             INTEGER NOT NULL,
    time_signatures TEXT NOT NULL,
    tempo_points    TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS anchors (
    score_id         TEXT NOT NULL,
    position_index   INTEGER NOT NULL,
    position         TEXT NOT NULL,
    locked           INTEGER NOT NULL,
    total_ticks      INTEGER NOT NULL,
    original_seconds REAL NOT NULL,
    current_seconds  REAL NOT NULL,
    tick_seconds     REAL NOT NULL,
    PRIMARY KEY (score_id, position_index),
    FOREIGN KEY (score_id) REFERENCES scores(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rehearsals (
    id             TEXT PRIMARY KEY,
    score_id       TEXT NOT NULL,
    last_elapsed   REAL NOT NULL DEFAULT 0.0,
    origin_ticks   INTEGER NOT NULL DEFAULT 0,
    origin_elapsed REAL NOT NULL DEFAULT 0.0,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (score_id) REFERENCES scores(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rehearsal_cues (
    rehearsal_id TEXT NOT NULL,
    cue_index    INTEGER NOT NULL,
    position     TEXT NOT NULL,
    text         TEXT NOT NULL,
    total_ticks  INTEGER NOT NULL,
    PRIMARY KEY (rehearsal_id, cue_index),
    FOREIGN KEY (rehearsal_id) REFERENCES rehearsals(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS cue_deliveries (
    rehearsal_id    TEXT NOT NULL,
    cue_index       INTEGER NOT NULL,
    trigger_seconds REAL NOT NULL,
    revision        INTEGER NOT NULL,
    elapsed_seconds REAL NOT NULL,
    delivered_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (rehearsal_id, cue_index),
    FOREIGN KEY (rehearsal_id, cue_index)
        REFERENCES rehearsal_cues(rehearsal_id, cue_index) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS cue_skips (
    rehearsal_id    TEXT NOT NULL,
    cue_index       INTEGER NOT NULL,
    revision        INTEGER NOT NULL,
    elapsed_seconds REAL NOT NULL,
    locate_seq      INTEGER,
    skipped_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (rehearsal_id, cue_index),
    FOREIGN KEY (rehearsal_id, cue_index)
        REFERENCES rehearsal_cues(rehearsal_id, cue_index) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS locate_operations (
    rehearsal_id        TEXT NOT NULL,
    seq                 INTEGER NOT NULL,
    operation_id        TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL,
    target_ticks        INTEGER NOT NULL,
    elapsed_seconds     REAL NOT NULL,
    prev_origin_ticks   INTEGER NOT NULL,
    prev_origin_elapsed REAL NOT NULL,
    revision            INTEGER NOT NULL,
    deliveries_before   INTEGER NOT NULL,
    annotations_before  INTEGER NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (rehearsal_id, seq),
    FOREIGN KEY (rehearsal_id) REFERENCES rehearsals(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rehearsal_annotations (
    rehearsal_id    TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    annotation_id   TEXT NOT NULL UNIQUE,
    text            TEXT NOT NULL,
    elapsed_seconds REAL NOT NULL,
    position        TEXT NOT NULL,
    total_ticks     INTEGER NOT NULL,
    revision        INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (rehearsal_id, seq),
    FOREIGN KEY (rehearsal_id) REFERENCES rehearsals(id) ON DELETE CASCADE
);
"""


class ScoreNotFound(Exception):
    """谱例 ID 不存在（-> 404）。"""


class RevisionConflict(Exception):
    """预期修订号与当前修订号不一致（-> 409）。"""

    def __init__(self, expected: int, current: int):
        self.expected = expected
        self.current = current
        super().__init__(f"修订号冲突：预期 {expected}，当前 {current}")


class RehearsalNotFound(Exception):
    """排练 ID 不存在（-> 404）。"""


class ElapsedRegression(Exception):
    """领取秒数早于已领取进度（-> 409，状态不变）。"""

    def __init__(self, elapsed: float, last_elapsed: float):
        self.elapsed = elapsed
        self.last_elapsed = last_elapsed
        super().__init__(
            f"领取进度只能前进：本次 {elapsed}，已领取到 {last_elapsed}"
        )


class OriginMismatch(Exception):
    """定位提交的累计秒数不等于当前已领取进度（-> 409，状态不变）。"""

    def __init__(self, elapsed: float, last_elapsed: float):
        self.elapsed = elapsed
        self.last_elapsed = last_elapsed
        super().__init__(
            f"定位秒数必须等于当前已领取进度：提交 {elapsed}，"
            f"已领取到 {last_elapsed}"
        )


class AnnotationProgressMismatch(Exception):
    """批注提交的累计秒数不等于当前已领取进度（-> 409，不写入）。"""

    def __init__(self, elapsed: float, last_elapsed: float):
        self.elapsed = elapsed
        self.last_elapsed = last_elapsed
        super().__init__(
            f"批注秒数必须等于当前已领取进度：提交 {elapsed}，"
            f"已领取到 {last_elapsed}"
        )


class UndoNotAllowed(Exception):
    """定位操作不可撤销（-> 409，状态不变）。

    ``reason`` 取值：

    * ``not_found``：操作 ID 不存在或不属于该排练；
    * ``already_undone``：该操作已撤销过（不能重复撤销）；
    * ``superseded``：连续定位时已被后续定位取代（只能撤销最后一次）；
    * ``progress``：定位后领取进度前进（或有提示被发放）；
    * ``annotation``：定位后登记过批注；
    * ``revision``：定位后速度图已发布（修订号前进）。
    """

    def __init__(self, operation_id: str, reason: str):
        self.operation_id = operation_id
        self.reason = reason
        super().__init__(f"定位操作 {operation_id} 不可撤销：{reason}")


def new_id() -> str:
    return uuid.uuid4().hex


class Store:
    def __init__(self, db_path: Optional[str] = None):
        # 默认放到工作目录下；容器内由 TEMPO_DB_PATH=/data/tempo.db 指向挂载卷，
        # 从而 `docker compose down` 后再 up（卷保留）仍能读取已发布的数据。
        self.db_path = db_path or os.environ.get(
            "TEMPO_DB_PATH", os.path.join(os.getcwd(), "data", "tempo.db")
        )
        parent = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(parent, exist_ok=True)
        self._write_lock = threading.Lock()
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """旧库升级：补定位/撤销定位所需的表与列（幂等）。

        * ``rehearsals`` 补定位计时起点列（未定位排练原点 0/0.0）；
        * ``cue_skips`` 补定位操作序号列（旧跳过记录不属于任何可撤销
          操作，置 NULL：旧定位不支持撤销，但状态与计时保持原状）。

        ``locate_operations`` 表由建表脚本的 ``CREATE TABLE IF NOT EXISTS``
        自动补齐；新库建表时即带上述结构。
        """
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(rehearsals)")}
        if "origin_ticks" not in cols:
            conn.execute(
                "ALTER TABLE rehearsals "
                "ADD COLUMN origin_ticks INTEGER NOT NULL DEFAULT 0"
            )
        if "origin_elapsed" not in cols:
            conn.execute(
                "ALTER TABLE rehearsals "
                "ADD COLUMN origin_elapsed REAL NOT NULL DEFAULT 0.0"
            )
        skip_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(cue_skips)")
        }
        if skip_cols and "locate_seq" not in skip_cols:
            conn.execute(
                "ALTER TABLE cue_skips ADD COLUMN locate_seq INTEGER"
            )

    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    # ------------------------------------------------------------------ #
    def create_score(
        self,
        ppq: int,
        meters: list[dict],
        points: list[dict],
        anchors: list[dict],
    ) -> dict:
        """创建谱例（修订号 0）并冻结各锚点按原图计算的秒数。"""
        score_id = new_id()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO scores "
                "(id, ppq, time_signatures, tempo_points, revision) "
                "VALUES (?, ?, ?, ?, 0)",
                (score_id, ppq, self._dump(meters), self._dump(points)),
            )
            conn.executemany(
                "INSERT INTO anchors "
                "(score_id, position_index, position, locked, total_ticks, "
                " original_seconds, current_seconds, tick_seconds) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        score_id,
                        i,
                        a["position"],
                        1 if a["locked"] else 0,
                        a["total_ticks"],
                        a["original_seconds"],
                        a["original_seconds"],
                        a["tick_seconds"],
                    )
                    for i, a in enumerate(anchors)
                ],
            )
        return self.get_score(score_id)

    def get_score(self, score_id: str) -> dict:
        """返回谱例与其全部锚点；不存在抛 :class:`ScoreNotFound`。

        两张表在**同一个显式读事务**（延迟事务，快照取自第一条 SELECT）
        内读取：与发布交错时，整次查询要么读到发布前的完整旧修订，
        要么读到发布后的完整新修订，不会出现“旧修订号 + 旧速度点 +
        新修订锚点秒数”的跨修订拼接（WAL 下读者不阻塞写者提交）。
        """
        with self._connect() as conn:
            conn.execute("BEGIN")
            row = self._read_score_row(conn, score_id)
            if row is None:
                conn.rollback()
                raise ScoreNotFound(score_id)
            # 读锚点前即使另一连接提交了发布，本事务仍持第一条 SELECT
            # 取得的快照，锚点与谱例行必然来自同一次已提交修订。
            anchor_rows = self._read_anchor_rows(conn, score_id)
            conn.commit()
        return self._hydrate(row, anchor_rows)

    def _read_score_row(
        self, conn: sqlite3.Connection, score_id: str
    ) -> Optional[sqlite3.Row]:
        """读谱例行（独立方法，便于测试在该行读取前后插入发布）。"""
        return conn.execute(
            "SELECT * FROM scores WHERE id = ?", (score_id,)
        ).fetchone()

    def _read_anchor_rows(
        self, conn: sqlite3.Connection, score_id: str
    ) -> list[sqlite3.Row]:
        """读全部锚点行；须在 :meth:`get_score` 的同一读事务内调用。"""
        return conn.execute(
            "SELECT * FROM anchors WHERE score_id = ? ORDER BY position_index",
            (score_id,),
        ).fetchall()

    def publish_revision(
        self,
        score_id: str,
        expected_revision: int,
        points: list[dict],
        evaluated: list[dict],
    ) -> dict:
        """按乐观锁发布整份替换速度点表。

        ``evaluated`` 为按候选图重新计算后的各锚点结果（来自引擎层的
        纯函数评估），每项含 total_ticks / original_seconds / current_seconds
        / locked / tick_seconds。调用方须在进入本方法前完成
        “锁定偏移超过一 tick”的 422 判定。

        单事务：revision 不符 -> :class:`RevisionConflict`（数据不变）；
        成功则一次更新速度图、未锁定锚点秒数和修订号，锁定秒数不变。
        """
        with self._write_lock, self._connect() as conn:
            # BEGIN IMMEDIATE：立刻拿到写锁，序列化并发发布，
            # 锁内重读 revision，杜绝两个请求同时用同一旧修订号覆盖。
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT revision FROM scores WHERE id = ?", (score_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise ScoreNotFound(score_id)
            if row["revision"] != expected_revision:
                conn.rollback()
                raise RevisionConflict(expected_revision, row["revision"])

            conn.execute(
                "UPDATE scores SET tempo_points = ?, revision = ? WHERE id = ?",
                (self._dump(points), expected_revision + 1, score_id),
            )
            for i, a in enumerate(evaluated):
                # 锁定锚点：只更新 tick 坐标（位置字符串不变），秒数保持创建时的值。
                if a["locked"]:
                    conn.execute(
                        "UPDATE anchors SET total_ticks = ? "
                        "WHERE score_id = ? AND position_index = ?",
                        (a["total_ticks"], score_id, i),
                    )
                else:
                    conn.execute(
                        "UPDATE anchors SET total_ticks = ?, current_seconds = ? "
                        "WHERE score_id = ? AND position_index = ?",
                        (a["total_ticks"], a["current_seconds"], score_id, i),
                    )
            conn.commit()
        return self.get_score(score_id)

    # ------------------------------------------------------------------ #
    # 排练提示：创建 / 查询 / 领取
    # ------------------------------------------------------------------ #
    def create_rehearsal(self, score_id: str, cues: list[dict]) -> dict:
        """为已保存的谱例创建排练（提示位置已在引擎层校验并展开为 tick）。

        ``cues`` 每项含 position / text / total_ticks，按录入顺序编号。
        谱例不存在 -> :class:`ScoreNotFound`（-> 404）。
        """
        rehearsal_id = new_id()
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id FROM scores WHERE id = ?", (score_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise ScoreNotFound(score_id)
            conn.execute(
                "INSERT INTO rehearsals (id, score_id, last_elapsed) "
                "VALUES (?, ?, 0.0)",
                (rehearsal_id, score_id),
            )
            conn.executemany(
                "INSERT INTO rehearsal_cues "
                "(rehearsal_id, cue_index, position, text, total_ticks) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (rehearsal_id, i, c["position"], c["text"], c["total_ticks"])
                    for i, c in enumerate(cues)
                ],
            )
            conn.commit()
        return self.get_rehearsal(rehearsal_id)

    def get_rehearsal(self, rehearsal_id: str) -> dict:
        """返回排练、全部提示及其发放/跳过记录；不存在抛 :class:`RehearsalNotFound`。

        与 :meth:`get_score` 相同，各表在同一个显式读事务内读取，
        与领取/定位/发布交错时不会跨快照拼接。
        """
        with self._connect() as conn:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT * FROM rehearsals WHERE id = ?", (rehearsal_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise RehearsalNotFound(rehearsal_id)
            cue_rows = conn.execute(
                "SELECT * FROM rehearsal_cues WHERE rehearsal_id = ? "
                "ORDER BY cue_index",
                (rehearsal_id,),
            ).fetchall()
            delivery_rows = conn.execute(
                "SELECT * FROM cue_deliveries WHERE rehearsal_id = ?",
                (rehearsal_id,),
            ).fetchall()
            skip_rows = conn.execute(
                "SELECT * FROM cue_skips WHERE rehearsal_id = ?",
                (rehearsal_id,),
            ).fetchall()
            # 最近一次定位是否仍可撤销：状态为 active 且定位后进度未前进、
            # 无新发放/批注、谱例修订号未变。与 undo_locate 的拒绝条件保持
            # 同一快照口径（任一“继续使用”发生后，这里读出的就是 null，
            # 与实际撤销结果及重启后的状态一致）。
            active_op = conn.execute(
                "SELECT lo.operation_id FROM locate_operations lo "
                "JOIN rehearsals r ON r.id = lo.rehearsal_id "
                "JOIN scores s ON s.id = r.score_id "
                "WHERE lo.rehearsal_id = ? AND lo.status = 'active' "
                "  AND r.last_elapsed = lo.elapsed_seconds "
                "  AND lo.deliveries_before = "
                "      (SELECT COUNT(*) FROM cue_deliveries d "
                "       WHERE d.rehearsal_id = lo.rehearsal_id) "
                "  AND lo.annotations_before = "
                "      (SELECT COALESCE(MAX(a.seq), -1) FROM rehearsal_annotations a "
                "       WHERE a.rehearsal_id = lo.rehearsal_id) "
                "  AND lo.revision = s.revision",
                (rehearsal_id,),
            ).fetchone()
            conn.commit()
        deliveries = {d["cue_index"]: d for d in delivery_rows}
        skips = {s["cue_index"]: s for s in skip_rows}
        return {
            "id": row["id"],
            "score_id": row["score_id"],
            "last_elapsed_seconds": row["last_elapsed"],
            "origin_ticks": row["origin_ticks"],
            "origin_elapsed_seconds": row["origin_elapsed"],
            "undoable_operation_id": active_op["operation_id"]
            if active_op is not None
            else None,
            "created_at": row["created_at"],
            "cues": [
                self._hydrate_cue(
                    c,
                    deliveries.get(c["cue_index"]),
                    skips.get(c["cue_index"]),
                )
                for c in cue_rows
            ],
        }

    def claim_cues(
        self,
        rehearsal_id: str,
        elapsed: float,
        plan: Callable[[dict, list[dict]], list[dict]],
    ) -> dict:
        """单事务领取到点提示（进度只前进，每条提示最多发放一次）。

        ``plan(score, pending_cues)`` 是引擎层注入的纯计算回调：在
        ``BEGIN IMMEDIATE`` 事务内、基于本事务快照读出的谱例当前已提交
        速度图，对未发提示计算触发秒数并筛出到点项（按触发秒数、录入
        顺序排序）。整个读-算-写在同一写事务内完成，与发布交错时二者
        必居其一，故**写入发放记录的修订号必然等于本次计算到点所用的
        修订号**；并发领取被写锁串行化，同一提示最多发放一次
        （``cue_deliveries`` 主键兜底）。

        倒退（elapsed 小于已领取进度）-> :class:`ElapsedRegression`，
        状态不变；排练不存在 -> :class:`RehearsalNotFound`。

        待发 = 既未发放也未被定位跳过的提示；触发秒数相对定位计时起点
        （``origin_ticks`` / ``origin_elapsed``，未定位即 0/0.0）计算，
        由注入的 ``plan`` 回调完成。
        """
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM rehearsals WHERE id = ?", (rehearsal_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise RehearsalNotFound(rehearsal_id)
            last_elapsed = row["last_elapsed"]
            if elapsed < last_elapsed:
                conn.rollback()
                raise ElapsedRegression(elapsed, last_elapsed)

            score_row = conn.execute(
                "SELECT * FROM scores WHERE id = ?", (row["score_id"],)
            ).fetchone()
            if score_row is None:  # 外键保证不会发生；防御性处理
                conn.rollback()
                raise ScoreNotFound(row["score_id"])
            cue_rows = conn.execute(
                "SELECT * FROM rehearsal_cues WHERE rehearsal_id = ? "
                "ORDER BY cue_index",
                (rehearsal_id,),
            ).fetchall()
            delivered = {
                r["cue_index"]
                for r in conn.execute(
                    "SELECT cue_index FROM cue_deliveries WHERE rehearsal_id = ?",
                    (rehearsal_id,),
                ).fetchall()
            }
            skipped = {
                r["cue_index"]
                for r in conn.execute(
                    "SELECT cue_index FROM cue_skips WHERE rehearsal_id = ?",
                    (rehearsal_id,),
                ).fetchall()
            }
            settled = delivered | skipped
            pending = [
                {
                    "cue_index": c["cue_index"],
                    "position": c["position"],
                    "text": c["text"],
                    "total_ticks": c["total_ticks"],
                }
                for c in cue_rows
                if c["cue_index"] not in settled
            ]
            score = {
                "id": score_row["id"],
                "ppq": score_row["ppq"],
                "time_signatures": json.loads(score_row["time_signatures"]),
                "tempo_points": json.loads(score_row["tempo_points"]),
                "revision": score_row["revision"],
                "origin_ticks": row["origin_ticks"],
                "origin_elapsed": row["origin_elapsed"],
            }
            # 纯计算：按本事务快照里的当前已提交速度图给未发提示定时。
            due = plan(score, pending)
            revision = score["revision"]
            for d in due:
                conn.execute(
                    "INSERT INTO cue_deliveries "
                    "(rehearsal_id, cue_index, trigger_seconds, revision, "
                    " elapsed_seconds) VALUES (?, ?, ?, ?, ?)",
                    (
                        rehearsal_id,
                        d["cue_index"],
                        d["trigger_seconds"],
                        revision,
                        elapsed,
                    ),
                )
            # 进度只能前进：elapsed >= last_elapsed 已在校验中保证。
            conn.execute(
                "UPDATE rehearsals SET last_elapsed = ? WHERE id = ?",
                (elapsed, rehearsal_id),
            )
            conn.commit()

            by_index = {c["cue_index"]: c for c in pending}
            return {
                "id": rehearsal_id,
                "elapsed_seconds": elapsed,
                "revision": revision,
                "cues": [
                    {
                        "index": d["cue_index"],
                        "position": by_index[d["cue_index"]]["position"],
                        "text": by_index[d["cue_index"]]["text"],
                        "trigger_seconds": d["trigger_seconds"],
                        "revision": revision,
                    }
                    for d in due
                ],
            }

    # ------------------------------------------------------------------ #
    # 排练定位：跳到另一乐谱位置，更新计时起点并跳过目标之前的未发提示
    # ------------------------------------------------------------------ #
    def locate(
        self,
        rehearsal_id: str,
        elapsed: float,
        target_ticks: int,
        skip_plan: Callable[[dict, list[dict]], list[int]],
    ) -> dict:
        """单事务定位（跳转）：校验进度秒数、跳过目标前未发提示、更新起点。

        ``elapsed`` 必须**等于**排练当前已领取进度（``last_elapsed``），
        否则抛 :class:`OriginMismatch`（-> 409），状态完全不变；
        排练不存在 -> :class:`RehearsalNotFound`。

        ``skip_plan(score, pending_cues)`` 是引擎层注入的纯计算回调：
        在 ``BEGIN IMMEDIATE`` 事务内、基于本事务快照的当前已提交速度图
        与定位起点，返回**尚未发放/跳过且 tick 严格位于目标之前**的提示
        下标（位于目标处的提示不跳过，可在 ``elapsed`` 同一秒领取）。
        回调返回的下标被写入 ``cue_skips``（带定位时的修订号与提交秒数），
        随后以（目标 tick, elapsed）更新计时起点；``last_elapsed`` 不变。
        定位/领取/发布共用进程内写锁，同一条提示不可能既跳过又发放。
        """
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM rehearsals WHERE id = ?", (rehearsal_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise RehearsalNotFound(rehearsal_id)
            if elapsed != row["last_elapsed"]:
                conn.rollback()
                raise OriginMismatch(elapsed, row["last_elapsed"])

            score_row = conn.execute(
                "SELECT * FROM scores WHERE id = ?", (row["score_id"],)
            ).fetchone()
            if score_row is None:  # 外键保证不会发生；防御性处理
                conn.rollback()
                raise ScoreNotFound(row["score_id"])
            cue_rows = conn.execute(
                "SELECT * FROM rehearsal_cues WHERE rehearsal_id = ? "
                "ORDER BY cue_index",
                (rehearsal_id,),
            ).fetchall()
            delivered = {
                r["cue_index"]
                for r in conn.execute(
                    "SELECT cue_index FROM cue_deliveries WHERE rehearsal_id = ?",
                    (rehearsal_id,),
                ).fetchall()
            }
            skipped = {
                r["cue_index"]
                for r in conn.execute(
                    "SELECT cue_index FROM cue_skips WHERE rehearsal_id = ?",
                    (rehearsal_id,),
                ).fetchall()
            }
            settled = delivered | skipped
            pending = [
                {
                    "cue_index": c["cue_index"],
                    "position": c["position"],
                    "text": c["text"],
                    "total_ticks": c["total_ticks"],
                }
                for c in cue_rows
                if c["cue_index"] not in settled
            ]
            revision = score_row["revision"]
            score = {
                "id": score_row["id"],
                "ppq": score_row["ppq"],
                "time_signatures": json.loads(score_row["time_signatures"]),
                "tempo_points": json.loads(score_row["tempo_points"]),
                "revision": revision,
                "origin_ticks": row["origin_ticks"],
                "origin_elapsed": row["origin_elapsed"],
            }
            # 纯计算：在同一快照/同一修订下判定目标之前的待跳过提示。
            to_skip = skip_plan(score, pending, target_ticks)
            to_skip_set = set(to_skip)
            # 回调只应返回待发集合中的下标（防御性兜底，避免误写已决提示）。
            assert to_skip_set <= {c["cue_index"] for c in pending}

            # 撤销现场：本次定位操作序号、原起点、定位瞬间修订号，以及
            # 定位前的已发提示数与最大批注序号（撤销时核对这三个维度与
            # 修订号，任一前进即过期）。
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq) + 1, 0) AS next_seq "
                "FROM locate_operations WHERE rehearsal_id = ?",
                (rehearsal_id,),
            ).fetchone()
            locate_seq = int(seq_row["next_seq"])
            deliveries_count = conn.execute(
                "SELECT COUNT(*) AS n FROM cue_deliveries WHERE rehearsal_id = ?",
                (rehearsal_id,),
            ).fetchone()["n"]
            ann_seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) AS max_seq "
                "FROM rehearsal_annotations WHERE rehearsal_id = ?",
                (rehearsal_id,),
            ).fetchone()
            annotations_before = int(ann_seq_row["max_seq"])
            operation_id = new_id()

            # 连续定位：更早的未撤销定位一律被取代（只能撤销最后一次）。
            conn.execute(
                "UPDATE locate_operations SET status = 'superseded' "
                "WHERE rehearsal_id = ? AND status = 'active'",
                (rehearsal_id,),
            )
            conn.execute(
                "INSERT INTO locate_operations "
                "(rehearsal_id, seq, operation_id, status, target_ticks, "
                " elapsed_seconds, prev_origin_ticks, prev_origin_elapsed, "
                " revision, deliveries_before, annotations_before) "
                "VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)",
                (
                    rehearsal_id,
                    locate_seq,
                    operation_id,
                    target_ticks,
                    elapsed,
                    row["origin_ticks"],
                    row["origin_elapsed"],
                    revision,
                    int(deliveries_count),
                    annotations_before,
                ),
            )
            for cue_index in to_skip:
                conn.execute(
                    "INSERT INTO cue_skips "
                    "(rehearsal_id, cue_index, revision, elapsed_seconds, "
                    " locate_seq) VALUES (?, ?, ?, ?, ?)",
                    (rehearsal_id, cue_index, revision, elapsed, locate_seq),
                )
            # 每次定位以（提交秒数, 目标位置）重置计时起点；
            # last_elapsed 保持提交秒数（二者此刻相等，后续领取只能前进）。
            conn.execute(
                "UPDATE rehearsals SET origin_ticks = ?, origin_elapsed = ? "
                "WHERE id = ?",
                (target_ticks, elapsed, rehearsal_id),
            )
            conn.commit()

        rehearsal = self.get_rehearsal(rehearsal_id)
        return {
            "id": rehearsal_id,
            "operation_id": operation_id,
            "elapsed_seconds": elapsed,
            "revision": revision,
            "skipped": sorted(to_skip_set),
            "rehearsal": rehearsal,
        }

    # ------------------------------------------------------------------ #
    # 撤销定位：恢复原计时起点与该次定位跳过的待发提示
    # ------------------------------------------------------------------ #
    def undo_locate(self, rehearsal_id: str, operation_id: str) -> dict:
        """单事务撤销一次定位（误定位后、尚未继续使用时的后悔操作）。

        仅当全部条件同时满足时才撤销（否则 :class:`UndoNotAllowed` ->
        409，状态完全不变）：

        1. 操作 ID 属于该排练且存在（``not_found``）；
        2. 操作状态为 ``active``：未撤销过（``already_undone``）、未被
           后续定位取代（``superseded``，连续定位只能撤销最后一次）；
        3. 定位后进度未前进（``last_elapsed`` 仍等于定位提交秒数），且
           已发提示数没有增加（同秒补发也算“继续使用”，``progress``）；
        4. 定位后没有登记批注（``annotation``）；
        5. 谱例修订号与定位瞬间一致，即定位后没有发布过速度图
           （``revision``）。

        通过后在同一事务内：把计时起点恢复为该定位保存的**原起点**，
        删除该次定位写入的 ``cue_skips`` 行（``locate_seq`` 匹配，提示
        恢复待发；更早已发或跳过的提示不动——普通回跳不恢复旧提示，
        撤销同样不触碰它们），并把操作标记为 ``undone``。匹配操作 ID、
        检查状态与恢复提示全部基于同一已提交快照；与领取/批注/定位/
        发布共用进程内写锁串行化。成功后待发提示按恢复的起点与当前
        速度图（修订号未变）定时。

        排练不存在 -> :class:`RehearsalNotFound`（-> 404）。
        """
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rehearsal_row = conn.execute(
                "SELECT * FROM rehearsals WHERE id = ?", (rehearsal_id,)
            ).fetchone()
            if rehearsal_row is None:
                conn.rollback()
                raise RehearsalNotFound(rehearsal_id)

            op = conn.execute(
                "SELECT * FROM locate_operations "
                "WHERE rehearsal_id = ? AND operation_id = ?",
                (rehearsal_id, operation_id),
            ).fetchone()
            if op is None:
                conn.rollback()
                raise UndoNotAllowed(operation_id, "not_found")
            if op["status"] == "undone":
                conn.rollback()
                raise UndoNotAllowed(operation_id, "already_undone")
            if op["status"] != "active":
                conn.rollback()
                raise UndoNotAllowed(operation_id, "superseded")

            # 下列各项检查与后面的恢复写在同一事务：快照一致，任一
            # 交错进来的领取/批注/定位/发布要么整体先于本事务（被这里
            # 看到并拒绝），要么整体后于本事务（撤销提交后才生效）。
            if rehearsal_row["last_elapsed"] != op["elapsed_seconds"]:
                conn.rollback()
                raise UndoNotAllowed(operation_id, "progress")
            deliveries_count = conn.execute(
                "SELECT COUNT(*) AS n FROM cue_deliveries WHERE rehearsal_id = ?",
                (rehearsal_id,),
            ).fetchone()["n"]
            if int(deliveries_count) != int(op["deliveries_before"]):
                conn.rollback()
                raise UndoNotAllowed(operation_id, "progress")
            ann_seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) AS max_seq "
                "FROM rehearsal_annotations WHERE rehearsal_id = ?",
                (rehearsal_id,),
            ).fetchone()
            if int(ann_seq_row["max_seq"]) != int(op["annotations_before"]):
                conn.rollback()
                raise UndoNotAllowed(operation_id, "annotation")
            score_row = conn.execute(
                "SELECT revision FROM scores WHERE id = ?",
                (rehearsal_row["score_id"],),
            ).fetchone()
            if score_row is None:  # 外键保证不会发生；防御性处理
                conn.rollback()
                raise ScoreNotFound(rehearsal_row["score_id"])
            if score_row["revision"] != op["revision"]:
                conn.rollback()
                raise UndoNotAllowed(operation_id, "revision")

            # 只恢复该次定位跳过的提示（按 locate_seq 精确匹配）；更早
            # 跳过/已发记录不属于本次操作，一律保持原状。
            restored_skip_rows = conn.execute(
                "SELECT cue_index FROM cue_skips "
                "WHERE rehearsal_id = ? AND locate_seq = ?",
                (rehearsal_id, op["seq"]),
            ).fetchall()
            restored_indices = sorted(r["cue_index"] for r in restored_skip_rows)
            conn.execute(
                "DELETE FROM cue_skips "
                "WHERE rehearsal_id = ? AND locate_seq = ?",
                (rehearsal_id, op["seq"]),
            )
            conn.execute(
                "UPDATE rehearsals SET origin_ticks = ?, origin_elapsed = ? "
                "WHERE id = ?",
                (
                    op["prev_origin_ticks"],
                    op["prev_origin_elapsed"],
                    rehearsal_id,
                ),
            )
            conn.execute(
                "UPDATE locate_operations SET status = 'undone' "
                "WHERE rehearsal_id = ? AND operation_id = ?",
                (rehearsal_id, operation_id),
            )
            conn.commit()

        rehearsal = self.get_rehearsal(rehearsal_id)
        return {
            "id": rehearsal_id,
            "operation_id": operation_id,
            "revision": op["revision"],
            "restored": restored_indices,
            "rehearsal": rehearsal,
        }

    # ------------------------------------------------------------------ #
    # 排练批注：按当前领取进度登记文字批注并反推乐谱位置（登记时冻结）
    # ------------------------------------------------------------------ #
    def add_annotation(
        self,
        rehearsal_id: str,
        elapsed: float,
        text: str,
        locate_position: Callable[[dict, float], dict],
    ) -> dict:
        """单事务登记一条批注（秒数必须等于当前已领取进度）。

        ``elapsed`` 必须**等于**排练当前已领取进度（``last_elapsed``），
        否则抛 :class:`AnnotationProgressMismatch`（-> 409），**不写入**；
        排练不存在 -> :class:`RehearsalNotFound`。

        ``locate_position(score, elapsed)`` 是引擎层注入的纯计算回调：
        在 ``BEGIN IMMEDIATE`` 事务内、基于本事务快照的当前定位起点
        （``origin_ticks`` / ``origin_elapsed``）与谱例当前已提交速度图，
        按 ``elapsed`` 反推出乐谱位置并量化到最近 tick，返回
        ``{"position", "total_ticks"}``。整个校验-读-算-写在同一写事务
        内完成，与领取/定位/发布交错时二者必居其一，故**进度、起点与
        速度图（修订号）必然取自同一已提交状态**，写入的 ``revision``
        就是反推所用速度图的修订号。位置/tick/修订号写入即冻结，之后
        的发布或再次定位不修改本条批注。

        ``seq`` 在事务内按该排练既有批注数 +1 分配，登记先后即查询顺序。
        """
        annotation_id = new_id()
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM rehearsals WHERE id = ?", (rehearsal_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise RehearsalNotFound(rehearsal_id)
            if elapsed != row["last_elapsed"]:
                conn.rollback()
                raise AnnotationProgressMismatch(elapsed, row["last_elapsed"])

            score_row = conn.execute(
                "SELECT * FROM scores WHERE id = ?", (row["score_id"],)
            ).fetchone()
            if score_row is None:  # 外键保证不会发生；防御性处理
                conn.rollback()
                raise ScoreNotFound(row["score_id"])
            revision = score_row["revision"]
            score = {
                "id": score_row["id"],
                "ppq": score_row["ppq"],
                "time_signatures": json.loads(score_row["time_signatures"]),
                "tempo_points": json.loads(score_row["tempo_points"]),
                "revision": revision,
                "origin_ticks": row["origin_ticks"],
                "origin_elapsed": row["origin_elapsed"],
            }
            # 纯计算：同一快照的起点 + 当前已提交速度图反推乐谱位置。
            located = locate_position(score, elapsed)

            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq) + 1, 0) AS next_seq "
                "FROM rehearsal_annotations WHERE rehearsal_id = ?",
                (rehearsal_id,),
            ).fetchone()
            seq = int(seq_row["next_seq"])
            conn.execute(
                "INSERT INTO rehearsal_annotations "
                "(rehearsal_id, seq, annotation_id, text, elapsed_seconds, "
                " position, total_ticks, revision) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rehearsal_id,
                    seq,
                    annotation_id,
                    text,
                    elapsed,
                    located["position"],
                    located["total_ticks"],
                    revision,
                ),
            )
            conn.commit()

        return {
            "id": annotation_id,
            "seq": seq,
            "text": text,
            "elapsed_seconds": elapsed,
            "position": located["position"],
            "total_ticks": located["total_ticks"],
            "revision": revision,
        }

    def list_annotations(self, rehearsal_id: str) -> list[dict]:
        """按登记先后（``seq`` 升序）返回排练的全部批注。

        单读事务：与登记/发布/定位交错时整次查询取自同一已提交快照；
        排练不存在抛 :class:`RehearsalNotFound`。批注各列是登记时的
        冻结值，不随之后的速度图发布或再次定位重算。
        """
        with self._connect() as conn:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT id FROM rehearsals WHERE id = ?", (rehearsal_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise RehearsalNotFound(rehearsal_id)
            rows = conn.execute(
                "SELECT * FROM rehearsal_annotations WHERE rehearsal_id = ? "
                "ORDER BY seq",
                (rehearsal_id,),
            ).fetchall()
            conn.commit()
        return [
            {
                "id": r["annotation_id"],
                "text": r["text"],
                "elapsed_seconds": r["elapsed_seconds"],
                "position": r["position"],
                "total_ticks": r["total_ticks"],
                "revision": r["revision"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _hydrate_cue(
        cue: sqlite3.Row,
        delivery: Optional[sqlite3.Row],
        skip: Optional[sqlite3.Row] = None,
    ) -> dict:
        return {
            "index": cue["cue_index"],
            "position": cue["position"],
            "text": cue["text"],
            "total_ticks": cue["total_ticks"],
            "status": "delivered" if delivery is not None
            else ("skipped" if skip is not None else "pending"),
            "delivered": delivery is not None,
            "trigger_seconds": delivery["trigger_seconds"] if delivery else None,
            "revision": delivery["revision"] if delivery else None,
            "skipped_revision": skip["revision"] if skip else None,
        }

    @staticmethod
    def _hydrate(row: sqlite3.Row, anchor_rows: list[sqlite3.Row]) -> dict:
        return {
            "id": row["id"],
            "ppq": row["ppq"],
            "time_signatures": json.loads(row["time_signatures"]),
            "tempo_points": json.loads(row["tempo_points"]),
            "revision": row["revision"],
            "created_at": row["created_at"],
            "anchors": [
                {
                    "index": a["position_index"],
                    "position": a["position"],
                    "locked": bool(a["locked"]),
                    "total_ticks": a["total_ticks"],
                    "original_seconds": a["original_seconds"],
                    "current_seconds": a["current_seconds"],
                    "tick_seconds": a["tick_seconds"],
                }
                for a in anchor_rows
            ],
        }
