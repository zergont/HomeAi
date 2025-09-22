# check_lmstudio.py
# Python 3.13
import os, sys, json, asyncio, time
import httpx

HOST = os.getenv("LMSTUDIO_HOST", "http://192.168.0.111:1234")
MODEL = os.getenv("LMSTUDIO_MODEL", "qwen/qwen3-14b")  # укажи точное имя своей скачанной модели

def pretty(obj): return json.dumps(obj, ensure_ascii=False, indent=2)

async def list_models(client: httpx.AsyncClient):
    r = await client.get(f"{HOST}/v1/models", timeout=10)
    r.raise_for_status()
    data = r.json()
    print("== /v1/models ==")
    print(pretty(data))
    # Пытаемся найти наш MODEL в списке
    ids = [m.get("id") for m in (data.get("data") or [])]
    if MODEL not in ids:
        print(f"\n⚠️  Модель '{MODEL}' не найдена в списке. Доступные:\n- " + "\n- ".join(map(str, ids)))
    return data

async def chat_once(client: httpx.AsyncClient, system: str, user: str):
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": 256
    }
    r = await client.post(f"{HOST}/v1/chat/completions", json=payload, timeout=60)
    r.raise_for_status()
    data = r.json()
    print("\n== non-stream chat ==")
    print("assistant:", data["choices"][0]["message"]["content"].strip())
    usage = data.get("usage")
    if usage:
        print("usage:", usage)
    else:
        print("usage: (не предоставлен сервером)")

async def chat_stream(client: httpx.AsyncClient, system: str, user: str):
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": 256,
        "stream": True,
    }
    print("\n== stream chat ==")
    async with client.stream("POST", f"{HOST}/v1/chat/completions", json=payload, timeout=None) as r:
        r.raise_for_status()
        buf = []
        async for line in r.aiter_lines():
            if not line:
                continue
            if line.startswith("data: "):
                chunk = line[6:]
                if chunk.strip() == "[DONE]":
                    break
                try:
                    j = json.loads(chunk)
                    delta = j["choices"][0].get("delta", {})
                    text = delta.get("content", "")
                    if text:
                        buf.append(text)
                        sys.stdout.write(text)
                        sys.stdout.flush()
                except Exception as e:
                    # бывает служебный чанκ без delta
                    pass
        print("\n-- end of stream --")
        print("collected chars:", sum(len(x) for x in buf))

async def main():
    async with httpx.AsyncClient(headers={"Accept": "application/json"}) as client:
        await list_models(client)
        # Контекстное окно через REST обычно НЕ доступно:
        print("\nℹ️  Размер контекстного окна через стандартный REST не возвращается. "
              "Если нужен из SDK — см. Node-скрипт ниже или SDK-метод getContextLength().")
        await chat_once(client, "Отвечай кратко и по делу.", "Скажи привет одним предложением.")
        await chat_stream(client, "Отвечай кратко и по делу.", "Дай 3 факта о чёрном чае.")

if __name__ == "__main__":
    asyncio.run(main())
