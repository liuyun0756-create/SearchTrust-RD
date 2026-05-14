"""
快速测试 httpx 抓取效果
用法：python test_httpx.py
"""

import asyncio
import random
import re
import httpx

# 测试的 URL，改成你实际要分析的网站
TEST_URLS = [
    "https://nxtlvlautospa.com",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
]

FAILURE_KEYWORDS = (
    "access denied", "403 forbidden", "captcha",
    "just a moment", "enable javascript", "browser check",
    "ddos protection", "verify you are human", "ray id",
)


async def test_httpx(url: str) -> None:
    print(f"\n{'='*60}")
    print(f"测试 URL: {url}")
    print("="*60)

    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        ) as client:
            resp = await client.get(url, headers=headers)

        print(f"状态码: {resp.status_code}")
        print(f"响应大小: {len(resp.text)} 字符")

        if resp.status_code != 200:
            print(f"❌ 失败：状态码 {resp.status_code}")
            return

        # 提取文本（和项目里一样的逻辑）
        html = resp.text
        text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        print(f"提取文本长度: {len(text)} 字符")

        # 检查失败关键词
        text_lower = text.lower()
        for kw in FAILURE_KEYWORDS:
            if kw in text_lower:
                print(f"❌ 内容包含反爬关键词: '{kw}'")
                return

        if len(text) < 300:
            print(f"❌ 内容太短（< 300字符），可能被拦截")
            return

        # 显示前 500 字符预览
        print(f"\n✅ 抓取成功！内容预览（前500字符）：")
        print("-"*40)
        print(text[:500])
        print("-"*40)

    except httpx.TimeoutException:
        print("❌ 超时")
    except Exception as e:
        print(f"❌ 错误: {e}")


async def main():
    print("开始测试 httpx 抓取能力...")
    for url in TEST_URLS:
        await test_httpx(url)
    print("\n\n测试完成！")
    print("✅ = httpx 可以直接抓取，不需要 Firecrawl")
    print("❌ = 需要 Jina 或 Firecrawl 处理（JS渲染/反爬）")


if __name__ == "__main__":
    asyncio.run(main())
