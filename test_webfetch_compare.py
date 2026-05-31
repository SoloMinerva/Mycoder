"""对比 urllib+正则 vs requests+BeautifulSoup 的 HTML 去标签效果"""

import re
import urllib.request
import urllib.error

import requests
from bs4 import BeautifulSoup

TEST_URL = "https://docs.python.org/3/library/subprocess.html"


def fetch_with_urllib_regex(url: str, max_length: int = 3000) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "mycoder/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8", errors="replace")

    text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]*>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:max_length]


def fetch_with_requests_bs4(url: str, max_length: int = 3000) -> str:
    resp = requests.get(url, headers={"User-Agent": "mycoder/1.0"}, timeout=30)
    resp.encoding = "utf-8"
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:max_length]


if __name__ == "__main__":
    print("=" * 60)
    print("方法 1：urllib + 正则")
    print("=" * 60)
    result1 = fetch_with_urllib_regex(TEST_URL)
    print(result1)

    print("\n" + "=" * 60)
    print("方法 2：requests + BeautifulSoup")
    print("=" * 60)
    result2 = fetch_with_requests_bs4(TEST_URL)
    print(result2)
