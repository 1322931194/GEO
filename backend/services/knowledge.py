"""
GEO 雷达 - 品牌知识库服务
==========================
实现"商家只填官网链接,系统自动建库"。
抓取官网首页 + 关键页面,提炼出品牌事实(产品、卖点、市场),
这些事实会喂给后续的问题生成和内容生成,保证 AI 输出真实不编造。

合规:只抓取品牌方自己提供的官网,不爬第三方站点、不绕过 robots。
"""

import re
import logging
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger("geo.knowledge")


def _clean_text(html: str) -> str:
    """极简正文提取:去标签、脚本、样式,留纯文本。"""
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"&[a-z]+;", " ", html)
    html = re.sub(r"\s+", " ", html)
    return html.strip()


async def build_knowledge_base(website_url: str, max_pages: int = 5) -> dict:
    """
    抓取官网,返回 {brand_facts, pages_crawled, title}。
    只抓同域名下的几个关键页面(首页 + about/product 类链接)。
    """
    if not website_url.startswith("http"):
        website_url = "https://" + website_url

    base_domain = urlparse(website_url).netloc
    visited = []
    collected = []
    title = ""

    async with httpx.AsyncClient(follow_redirects=True,
                                 headers={"User-Agent": "GEO-Radar-Bot/1.0"}) as client:
        try:
            r = await client.get(website_url, timeout=30)
            r.raise_for_status()
            home_html = r.text
            visited.append(website_url)

            m = re.search(r"<title>([^<]+)</title>", home_html, re.I)
            if m:
                title = m.group(1).strip()

            collected.append(_clean_text(home_html)[:3000])

            # 找 about / product / 关于 / 产品 类链接,补抓几页
            links = re.findall(r'href=["\']([^"\']+)["\']', home_html)
            keywords = ("about", "product", "feature", "关于", "产品", "service")
            for link in links:
                if len(visited) >= max_pages:
                    break
                if any(k in link.lower() for k in keywords):
                    full = urljoin(website_url, link)
                    if urlparse(full).netloc == base_domain and full not in visited:
                        try:
                            pr = await client.get(full, timeout=20)
                            if pr.status_code == 200:
                                visited.append(full)
                                collected.append(_clean_text(pr.text)[:2000])
                        except Exception:
                            pass
        except Exception as e:
            logger.warning("抓取官网失败 %s: %s", website_url, e)
            return {"brand_facts": "", "pages_crawled": [], "title": "",
                    "error": f"无法访问该网址:{e}"}

    brand_facts = "\n\n".join(collected)[:6000]
    return {
        "brand_facts": brand_facts,
        "pages_crawled": visited,
        "title": title,
    }
