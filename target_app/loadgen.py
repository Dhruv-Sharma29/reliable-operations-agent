"""Background traffic so metrics exist: ~5 job submissions/s and ~2.5 status reads/s."""

from __future__ import annotations

import asyncio
import random

import httpx

from sandbox import paths


async def main() -> None:
    rng = random.Random(7)
    recent: list[int] = []
    async with httpx.AsyncClient(base_url=paths.API_URL, timeout=5) as client:
        tick = 0
        while True:
            tick += 1
            try:
                payload = {"amount": rng.randint(5, 500)}
                if rng.random() < 0.7:
                    payload["customer"] = f"c{rng.randint(1, 50)}"  # 30% of payloads have no customer
                r = await client.post("/jobs", json={"kind": "invoice", "payload": payload})
                if r.status_code == 201:
                    recent = (recent + [r.json()["id"]])[-50:]
                if tick % 2 == 0 and recent:
                    await client.get(f"/jobs/{rng.choice(recent)}")
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)


if __name__ == "__main__":
    asyncio.run(main())
