"""Read the local LLM call log.

Usage from project root:

    python -m pillow_assistant.tools.show_llm_log              # today, last 10
    python -m pillow_assistant.tools.show_llm_log -d 1         # yesterday
    python -m pillow_assistant.tools.show_llm_log --errors     # failures only
    python -m pillow_assistant.tools.show_llm_log --tail 5     # last 5
    python -m pillow_assistant.tools.show_llm_log --tail 5 -v  # full prompts/responses

Lives in ``pillow_assistant.tools`` so PyInstaller / packaging include it; users
who don't want it can simply not run it.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta

from pillow_assistant.core.llm_log import _log_file_for


def main() -> int:
    ap = argparse.ArgumentParser(description="Show local LLM call log")
    ap.add_argument("-d", "--days-ago", type=int, default=0,
                    help="0 = today (default), 1 = yesterday, ...")
    ap.add_argument("--errors", action="store_true", help="failures only")
    ap.add_argument("--tail", type=int, default=10, help="last N records (default 10)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print full prompts and responses, not truncated")
    args = ap.parse_args()

    day = datetime.now() - timedelta(days=args.days_ago)
    path = _log_file_for(day)
    if not path.exists():
        print(f"no log file: {path}")
        return 1

    records: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # Tolerate corrupt lines without aborting the whole readout.
                continue

    if args.errors:
        records = [r for r in records if not r.get("ok", False)]
    if args.tail:
        records = records[-args.tail:]

    truncate = (lambda s: s) if args.verbose else (lambda s: (s or "")[:200])

    for r in records:
        status = "OK " if r.get("ok") else "ERR"
        print(f"[{r.get('ts')}] {status} {r.get('provider')}/{r.get('model')} "
              f"{r.get('duration_ms')}ms  id={r.get('request_id')}")
        prompt = r.get("prompt") or ""
        print(f"  prompt:   {truncate(prompt)}")
        if r.get("ok"):
            print(f"  response: {truncate(r.get('response') or '')}")
            if r.get("usage"):
                print(f"  usage:    {r['usage']}")
        else:
            print(f"  error:    {r.get('error_type')}: {r.get('error_msg')}")
            partial = r.get("partial_response") or ""
            if partial:
                print(f"  partial:  {truncate(partial)}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
