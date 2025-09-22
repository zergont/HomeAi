// apps/web/check_lmstudio.mjs
// Быстрый REST-пинг LM Studio: /v1/models, non-stream chat, stream chat.
const HOST = process.env.LMSTUDIO_HOST ?? "http://192.168.0.111:1234";
const MODEL = process.env.LMSTUDIO_MODEL ?? "qwen/qwen3-14b";

const fetchJson = async (url, init) => {
    const r = await fetch(url, init);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return await r.json();
};

async function listModels() {
    const data = await fetchJson(`${HOST}/v1/models`);
    console.log("== /v1/models ==\n", JSON.stringify(data, null, 2));
    const ids = (data.data ?? []).map(m => m.id);
    if (!ids.includes(MODEL)) {
        console.warn(`\n⚠️ Модель '${MODEL}' не найдена. Доступные:\n- ${ids.join("\n- ")}`);
    }
}

async function chatOnce() {
    const payload = {
        model: MODEL,
        messages: [
            { role: "system", content: "Отвечай кратко и по делу." },
            { role: "user", content: "Скажи привет одним предложением." }
        ],
        temperature: 0.3,
        max_tokens: 128
    };
    const data = await fetchJson(`${HOST}/v1/chat/completions`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
    });
    console.log("\n== non-stream chat ==");
    console.log("assistant:", data.choices?.[0]?.message?.content?.trim());
    console.log("usage:", data.usage ?? "(не предоставлен сервером)");
}

async function chatStream() {
    const payload = {
        model: MODEL,
        messages: [
            { role: "system", content: "Отвечай кратко и по делу." },
            { role: "user", content: "Дай 3 факта о зелёном чае." }
        ],
        temperature: 0.3,
        max_tokens: 256,
        stream: true
    };
    const r = await fetch(`${HOST}/v1/chat/completions`, {
        method: "POST",
        headers: { "content-type": "application/json", "accept": "text/event-stream" },
        body: JSON.stringify(payload),
    });
    if (!r.ok || !r.body) throw new Error(`${r.status} ${r.statusText}`);

    console.log("\n== stream chat ==");
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let i;
        while ((i = buffer.indexOf("\n\n")) >= 0) {
            const chunk = buffer.slice(0, i); buffer = buffer.slice(i + 2);
            if (!chunk.startsWith("data: ")) continue;
            const data = chunk.slice(6).trim();
            if (data === "[DONE]") { console.log("\n-- end of stream --"); return; }
            try {
                const j = JSON.parse(data);
                const delta = j?.choices?.[0]?.delta?.content ?? "";
                if (delta) process.stdout.write(delta);
            } catch { /* пропускаем служебные чанки */ }
        }
    }
}

(async () => {
    await listModels();
    console.log("\nℹ️ Контекстное окно через стандартный REST обычно не видно; для SDK см. getContextLength().");
    await chatOnce();
    await chatStream();
})().catch(e => { console.error("\n❌ Error:", e); process.exit(1); });
