from aiohttp import web
from conftest import run, serve

from bot.search import TavilySearch, should_search
from bot.voice import Transcriber


def test_should_search():
    yes = ["ข่าวเด่นวันนี้", "ราคาทองวันนี้", "หาตั้งค่า delta force", "ช่วยหาร้านกาแฟแถวสยาม",
           "ค้นหา ร้านอาหาร", "หาคลิปให้หน่อย", "latest gpu price"]
    no = ["สวัสดี", "วันนี้วันอะไร", "ตอนนี้เหนื่อยจัง", "หารสองได้เท่าไหร่", "หายใจไม่ออก", "เขียนกลอน"]
    assert all(should_search(q) for q in yes)
    assert not any(should_search(q) for q in no)


def test_clip_search_prefers_youtube_and_uses_context():
    requests = []
    youtube_count = {"n": 1}

    async def handler(request):
        body = await request.json()
        requests.append((body["query"], body.get("include_domains")))
        domains = body.get("include_domains")
        if domains == ["youtube.com"]:
            results = [{"title": f"YT{i}", "url": f"https://youtube.com/{i}"} for i in range(youtube_count["n"])]
        else:
            results = [{"title": "TT", "url": "https://tiktok.com/1"}]
        return web.json_response({"results": results})

    async def main():
        async with serve(("POST", "/search", handler)) as url:
            search = TavilySearch("tvly-test")
            search.URL = f"{url}/search"
            few = await search.search("หาคลิปให้หน่อย", context="ตั้งค่า delta force")
            youtube_count["n"] = 3
            many = await search.search("หาคลิปตั้งค่า delta force")
            await search.close()
        return few, many

    few, many = run(main())
    assert [r.title for r in few] == ["YT0", "TT"]          # YouTube ไม่พอ → เติม TikTok
    assert requests[0] == ("ตั้งค่า delta force หาคลิปให้หน่อย", ["youtube.com"])
    assert [r.title for r in many] == ["YT0", "YT1", "YT2"]  # YouTube พอแล้ว ไม่ค้น TikTok


def test_transcriber_rotates_keys():
    keys = []

    async def handler(request):
        keys.append(request.headers["Authorization"])
        form = await request.post()
        assert form["model"] == "whisper-test"
        if len(keys) == 1:
            return web.json_response({}, status=429)
        return web.json_response({"text": " สวัสดีครับ "})

    async def main():
        async with serve(("POST", "/t", handler)) as url:
            transcriber = Transcriber(("bad", "good"), "whisper-test")
            transcriber.URL = f"{url}/t"
            text = await transcriber.transcribe(b"OggS", "voice-message.ogg", "audio/ogg")
            await transcriber.close()
        return text

    assert run(main()) == "สวัสดีครับ"
    assert keys == ["Bearer bad", "Bearer good"]
