import re
from pathlib import Path

html = Path("_product.html").read_text(encoding="utf-8")
urls = re.findall(r"https://www\.ryanoptics\.com/[^\"'\s<>]+-pd\d+\.html", html)
print("product urls", len(urls))
for url in dict.fromkeys(urls):
    print(url)

print("\n--- snippet around first product url in body ---")
if urls:
    idx = html.find(urls[0])
    print("idx", idx)
    print(html[idx - 500 : idx + 800])
