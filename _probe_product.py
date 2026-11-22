import gzip
import zlib
from pathlib import Path

path = Path("_product.bin").resolve()
raw = path.read_bytes()
print("path", path)
print("size", len(raw))
print("head", raw[:20].hex())
try:
    text = gzip.decompress(raw)
except Exception as exc:
    print("gzip fail", type(exc).__name__, exc)
    text = zlib.decompress(raw, 16 + zlib.MAX_WBITS)
html = text.decode("utf-8", "replace")
Path("_product.html").write_text(html, encoding="utf-8")
print("html", len(html))
print(html[:4000])
