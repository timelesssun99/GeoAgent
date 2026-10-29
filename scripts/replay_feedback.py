"""回答反馈审计回放：把 👍/👎 反馈导出为问题库候选。

用法：
    backend/.venv/Scripts/python.exe scripts/replay_feedback.py
    backend/.venv/Scripts/python.exe scripts/replay_feedback.py --down-only --limit 50
    backend/.venv/Scripts/python.exe scripts/replay_feedback.py --export data/feedback_candidates.jsonl

数据来源优先 `geoagent.qa_feedback`，没有数据库时回退 `data/qa_feedback.jsonl`。
导出的候选项只进入 `data/`，不直接改问题库；人工确认后再合并到
`skills/qa-library/assets/question_library.json` 或 holdout 集。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import asyncpg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from geoagent.config import Settings, load_dotenv  # noqa: E402


async def _load_from_db(settings: Settings, limit: int) -> list[dict[str, Any]]:
    if not settings.chat_dsn:
        return []
    try:
        conn = await asyncpg.connect(settings.chat_dsn)
    except Exception:
        return []
    try:
        rows = await conn.fetch(
            f"""
            SELECT id, conversation_id, message_id, rating, reason, question, answer,
                   entry_id, dataset_id, answer_origin, created_at
              FROM {settings.chat_schema}.qa_feedback
             ORDER BY created_at DESC
             LIMIT $1
            """,
            limit,
        )
    except Exception:
        rows = []
    finally:
        await conn.close()
    return [
        {
            **dict(row),
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]


def _load_from_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            items.append(item)
    return items[-limit:][::-1]


def _candidate(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "question": item.get("question") or "",
        "answer": item.get("answer") or "",
        "rating": item.get("rating") or "",
        "reason": item.get("reason") or "",
        "entry_id": item.get("entry_id") or "",
        "dataset_id": item.get("dataset_id") or "",
        "answer_origin": item.get("answer_origin") or "",
        "conversation_id": item.get("conversation_id") or "",
        "created_at": item.get("created_at") or item.get("ts") or "",
        "status": "pending",
        "suggested_action": "add_alias" if item.get("rating") == "up" else "review",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="回答反馈审计回放 / 问题库候选导出")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--down-only", action="store_true", help="只看不准确反馈")
    parser.add_argument(
        "--export",
        default=str(ROOT / "data" / "feedback_candidates.jsonl"),
        help="候选导出路径（JSONL，status=pending）",
    )
    args = parser.parse_args()
    load_dotenv()
    settings = Settings()
    items = asyncio.run(_load_from_db(settings, args.limit))
    if not items:
        items = _load_from_jsonl(settings.data_dir / "qa_feedback.jsonl", args.limit)
    if args.down_only:
        items = [item for item in items if item.get("rating") == "down"]
    if not items:
        print("没有反馈记录。先在页面回答下方点 👍/👎。")
        return 0

    up = sum(1 for item in items if item.get("rating") == "up")
    down = sum(1 for item in items if item.get("rating") == "down")
    print(f"反馈：{len(items)} 条（准确 {up} / 不准确 {down}）\n")
    for item in items:
        print(f"[{item.get('rating')}] {item.get('reason') or ''}")
        print(f"  问：{item.get('question') or ''}")
        answer = str(item.get("answer") or "").replace("\n", " ")
        print(f"  答：{answer[:180]}{'…' if len(answer) > 180 else ''}")
        print(f"  条目：{item.get('entry_id') or '-'}；期次：{item.get('dataset_id') or '-'}\n")

    export = Path(args.export)
    export.parent.mkdir(parents=True, exist_ok=True)
    with export.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(_candidate(item), ensure_ascii=False) + "\n")
    print(f"已导出 {len(items)} 条候选到 {export}")
    print("下一步：人工确认后合并进 aliases / holdout，不要直接改锁定集。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
