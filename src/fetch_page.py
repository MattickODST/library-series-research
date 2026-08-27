#!/usr/bin/env python3
import argparse, json, sys
from pathlib import Path
from scrapling.fetchers import Fetcher

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("output")
    args = ap.parse_args()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        page = Fetcher.get(args.url)
        text = " ".join(page.get_all_text().split())
        if not text:
            raise RuntimeError("retrieved page contained no readable text")
        out.write_text(text, encoding="utf-8")
        print(json.dumps({"ok": True, "url": args.url, "output": str(out), "chars": len(text)}))
    except Exception as exc:
        print(json.dumps({"ok": False, "url": args.url, "error": str(exc)}))
        sys.exit(2)

if __name__ == "__main__":
    main()
