import gzip
import re
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (compatible; OpticsCopilot/0.1)"}


def get(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as response:
        raw = response.read()
        encoding = response.headers.get("Content-Encoding", "")
        if raw[:2] == b"\x1f\x8b" or "gzip" in encoding:
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "replace")


news = get("https://www.electronicsweekly.com/news/")
print("NEWS LEN", len(news))
classes = set(re.findall(r'class="([^"]+)"', news))
interesting = [
    c
    for c in classes
    if any(k in c.lower() for k in ("post", "article", "news", "entry", "card", "title", "excerpt"))
]
print("CLASSES")
for name in sorted(interesting):
    print("-", name)
index = news.find("<article")
print("ARTICLE INDEX", index)
print(news[index : index + 1800] if index >= 0 else "no article")
h2 = news.find("<h2")
print("H2 SNIP")
print(news[max(0, h2 - 300) : h2 + 700])

print("\n\n===== PRODUCT =====\n")
url = "https://www.ryanoptics.com/Marine-Gyro-Stabilized-Camera-pl44267786.html"
req = urllib.request.Request(url, headers={**UA, "Accept-Encoding": "gzip, deflate"})
with urllib.request.urlopen(req, timeout=40) as response:
    raw = response.read()
    print("status", response.status, "ctype", response.headers.get("content-type"))
    print("encoding", response.headers.get("content-encoding"))
    print("first", raw[:30])
open("_product.bin", "wb").write(raw)
