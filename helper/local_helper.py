from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import websockets


def build_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Tiny local helper (MVP): send transcript lines to backend websocket."
    )
    parser.add_argument("--session-id", default="default-room")
    parser.add_argument("--server", default="ws://127.0.0.1:8000")
    parser.add_argument("--provider", default="mock")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--context", default="")
    parser.add_argument("--history-mode", default="focused", choices=["focused", "full", "stateless"])
    return parser.parse_args()


def parse_line(line: str) -> tuple[str, str]:
    clean = line.strip()
    if ":" in clean:
        prefix, text = clean.split(":", 1)
        prefix = prefix.strip().lower()
        if prefix in {"mic", "system"}:
            return prefix, text.strip()
    return "system", clean


async def receiver(ws: websockets.ClientConnection) -> None:
    async for message in ws:
        data: dict[str, Any] = json.loads(message)
        msg_type = data.get("type")
        if msg_type == "suggestion":
            print(
                f"\n[{data.get('provider')}/{data.get('model')}] "
                f"{data.get('latency_ms')}ms\n{data.get('text')}\n"
            )
        elif msg_type == "status":
            print(f"[status] {data.get('message')}")
        elif msg_type == "error":
            print(f"[error] {data.get('message')}")


async def sender(ws: websockets.ClientConnection) -> None:
    print("Type transcript lines and press Enter.")
    print("Optional prefix: 'mic:' or 'system:'")
    while True:
        line = await asyncio.to_thread(input, "> ")
        if not line.strip():
            continue
        source, text = parse_line(line)
        await ws.send(
            json.dumps({"type": "transcript", "source": source, "text": text, "final": True})
        )


async def main() -> None:
    args = build_args()
    url = f"{args.server.rstrip('/')}/ws/{args.session_id}"
    async with websockets.connect(url) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "configure",
                    "provider": args.provider,
                    "model": args.model,
                    "context": args.context,
                    "history_mode": args.history_mode,
                }
            )
        )
        await asyncio.gather(receiver(ws), sender(ws))


if __name__ == "__main__":
    asyncio.run(main())
