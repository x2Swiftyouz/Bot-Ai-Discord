import json

from aiohttp import web
from conftest import run, serve

from bot import providers as P
from bot.config import Config


def gemini_ok(text="ตอบ", **candidate):
    return web.json_response({"candidates": [{"content": {"parts": [{"text": text}]}, **candidate}]})


def test_gemini_answer_with_search_sources(env):
    env(WEB_SEARCH="true")
    seen = []

    async def handler(request):
        seen.append(await request.json())
        return gemini_ok(groundingMetadata={
            "webSearchQueries": ["q"],
            "groundingChunks": [{"web": {"uri": "https://a", "title": "a.com"}}],
        })

    async def main():
        async with serve(("POST", "/{model}", handler)) as url:
            provider = P.GeminiProvider(Config.load())
            provider.BASE_URL = url
            result = await provider.generate([], "ข่าววันนี้")
            await provider.close()
        return result

    result = run(main())
    assert result.text == "ตอบ" and result.searched and result.sources == (("a.com", "https://a"),)
    assert seen[0]["tools"] == [{"google_search": {}}]
    assert "Google Search" in seen[0]["systemInstruction"]["parts"][0]["text"]


def test_search_429_retries_without_search(env):
    env(WEB_SEARCH="true")
    calls = []

    async def handler(request):
        body = await request.json()
        calls.append("tools" in body)
        if "tools" in body:
            return web.json_response({"error": {}}, status=429)
        return gemini_ok()

    async def main():
        async with serve(("POST", "/{model}", handler)) as url:
            provider = P.GeminiProvider(Config.load())
            provider.BASE_URL = url
            text = (await provider.generate([], "q")).text
            await provider.generate([], "q")  # ช่วงพักการค้นเว็บ ไม่ส่ง tools แล้ว
            await provider.close()
        return text

    assert run(main()) == "ตอบ"
    assert calls == [True, False, False]


def test_retry_then_fallback_model_and_key_rotation(env):
    env(GEMINI_FALLBACK_MODELS="gemini-b", GEMINI_API_KEYS="key-2", AI_MAX_RETRIES="1", WEB_SEARCH="false")
    calls = []

    async def handler(request):
        model = request.match_info["model"].split(":")[0]
        key = request.headers["x-goog-api-key"]
        calls.append((model, key))
        if model == "gemini-test":
            return web.json_response({"error": {}}, status=503)
        if key == "gemini-key":
            return web.json_response({"error": {}}, status=429)
        return gemini_ok(f"จาก {model}")

    async def main():
        async with serve(("POST", "/{model}", handler)) as url:
            provider = P.GeminiProvider(Config.load())
            provider.BASE_URL = url
            result = await provider.generate([], "q")
            await provider.close()
        return result

    assert run(main()).text == "จาก gemini-b"
    # 503: ลองซ้ำ 1 ครั้งแล้วไปโมเดลสำรอง · 429 ที่ key แรก → สลับไป key-2
    assert calls == [
        ("gemini-test", "gemini-key"), ("gemini-test", "gemini-key"),
        ("gemini-b", "gemini-key"), ("gemini-b", "key-2"),
    ]


def test_streaming_gemini_and_openai(env):
    env(GROQ_API_KEY="g", GROQ_MODEL="q", STREAMING="true", WEB_SEARCH="false")

    def sse(objects):
        return "".join(f"data: {json.dumps(o)}\n\n" for o in objects)

    async def gemini(request):
        return web.Response(content_type="text/event-stream", text=sse([
            {"candidates": [{"content": {"parts": [{"text": "สวัส"}]}}]},
            {"candidates": [{"content": {"parts": [{"thought": True, "text": "x"}, {"text": "ดี"}]}}]},
        ]))

    async def groq(request):
        return web.Response(content_type="text/event-stream", text=sse([
            {"choices": [{"delta": {"content": "Hel"}}]}, {"choices": [{"delta": {"content": "lo"}}]},
        ]) + "data: [DONE]\n\n")

    async def main():
        async with serve(("POST", "/g/{model}", gemini), ("POST", "/q", groq)) as url:
            cfg = Config.load()
            gem = P.GeminiProvider(cfg)
            gem.BASE_URL = f"{url}/g"
            grq = P.GroqProvider(cfg)
            grq.url = f"{url}/q"
            d1, d2 = [], []
            r1 = await gem.generate([], "q", (), d1.append)
            r2 = await grq.generate([], "q", (), d2.append)
            await gem.close()
            await grq.close()
        return r1, d1, r2, d2

    r1, d1, r2, d2 = run(main())
    assert (r1.text, d1) == ("สวัสดี", ["สวัส", "สวัสดี"])
    assert (r2.text, d2) == ("Hello", ["Hel", "Hello"])


def test_backup_chain_status_events(env):
    env(GROQ_API_KEY="g", GROQ_MODEL="q", BACKUP_PROVIDER="groq")
    provider = P.create_provider(Config.load())
    events = []
    provider.on_event = lambda kind, title, detail: events.append(kind)
    mode = {"gemini": "429", "groq": "ok"}

    def fake(name):
        async def generate(history, prompt, images=(), on_delta=None, retry=True):
            if mode[name] == "429":
                raise P.RateLimitError()
            if mode[name] == "503":
                raise P.ServiceUnavailableError("x")
            if mode[name] == "400":
                raise P.BadRequestError("x")
            return P.AIResult(f"จาก {name}", name)
        return generate

    provider.primary.generate = fake("gemini")
    provider.backup.generate = fake("groq")

    async def ask():
        try:
            return (await provider.generate([], "q")).text
        except P.AIError as e:
            return type(e).__name__

    async def main():
        out = [await ask(), await ask()]          # ล่ม → แจ้งครั้งเดียว แม้ถามซ้ำ
        provider._paused_until.clear()
        mode["gemini"] = "ok"
        out.append(await ask())                   # กลับมา → แจ้ง up
        mode["gemini"] = "400"
        out.append(await ask())                   # 400 ไม่นับว่าล่ม
        mode.update(gemini="503", groq="503")
        out.append(await ask())                   # ล่มทุกตัว
        mode["groq"] = "ok"
        out.append(await ask())
        return out

    out = run(main())
    assert out == ["จาก groq", "จาก groq", "จาก gemini", "จาก groq", "ServiceUnavailableError", "จาก groq"]
    assert events == ["down", "up", "outage", "up", "recovered"]


def test_check_models_replaces_retired_model(env):
    env(
        AI_PROVIDER="openrouter", OPENROUTER_API_KEY="or-key",
        OPENROUTER_MODEL="meta-llama/llama-3.3-70b-instruct:free",
        OPENROUTER_VISION_MODEL="old/vision:free",
    )
    models = [
        "apodex/apodex-1.1-mini:free", "google/gemma-4-26b-a4b-it:free",
        "google/gemma-4-31b-it:free", "openai/gpt-4o", "nvidia/nemotron-3-ultra-550b-a55b:free",
    ]

    async def handler(request):
        return web.json_response({"data": [{"id": m} for m in models]})

    async def main():
        async with serve(("GET", "/models", handler)) as url:
            provider = P.OpenRouterProvider(Config.load())
            provider.url = url + "/chat/completions"
            problems = await provider.check_models()
            await provider.close()
        return provider, problems

    provider, problems = run(main())
    # ตระกูลที่ชอบก่อน แล้วเลือกตัวใหญ่สุด · เดิมใช้ตัวฟรีจึงไม่เลือก openai/gpt-4o
    assert provider.models == ["google/gemma-4-31b-it:free"]
    assert provider.model == "google/gemma-4-31b-it:free"
    assert provider.vision_model is None
    assert len(problems) == 2 and "ใช้ 'google/gemma-4-31b-it:free' แทน" in problems[0]


def test_check_models_drops_only_missing_fallback(env):
    env(GEMINI_FALLBACK_MODELS="gemini-gone")

    async def handler(request):
        return web.json_response({"models": [{"name": "models/gemini-test"}]})

    async def main():
        async with serve(("GET", "/", handler)) as url:
            provider = P.GeminiProvider(Config.load())
            provider.BASE_URL = url
            problems = await provider.check_models()
            await provider.close()
        return provider, problems

    provider, problems = run(main())
    assert provider.models == ["gemini-test"] and len(problems) == 1
