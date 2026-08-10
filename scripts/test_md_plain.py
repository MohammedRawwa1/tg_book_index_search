"""Manual regression checks for markdown escaping/parsing helpers.

Run:  python scripts/test_md_plain.py

Covers the filename patterns that used to break the Telegraph HTML view
(/p/<page_id>): labels containing underscores, brackets, parentheses and
other Markdown special characters must always be preserved literally and
must always produce a clickable anchor.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.utils.helpers import (
    md_to_plain_text,
    md_to_html,
    md_to_markdown,
    extract_md_link_label,
    normalize_bracket_links,
    md_links_to_clickable,
)

examples = [
    r"Tom\_Torero\_Word\_Tour\_Day\_24\_Powder\_Day\_1080p\_30fps\_H264\_128kbit.mp4",
    r"Tom\_Torero Conversation King \[Br2KX8jnAMU\].mp4",
    r"1) [Sample video](https://t.me/c/12345/1001)",
]

print("=== md_to_plain_text ===")
for s in examples:
    print("ORIG :", s)
    print("PLAIN:", md_to_plain_text(s))
    print()

print("=== md_to_html (HTML view) ===")
nasty = [
    "1) [Tom_Torero_Word_Tour_Day_24_Powder_Day_1080p_30fps_H264_128kbit.mp4](https://t.me/c/123/451)",
    "2) [Tom_Torero Conversation King [Br2KX8jnAMU].mp4](https://t.me/c/123/452)",
    "3) [Book (Part 1) - Final Edition.pdf](https://t.me/c/123/453)",
    "4) [star*wars_episode[IV]_final.mp4](https://t.me/c/123/454)",
    "5) [plain filename.txt](https://t.me/c/123/455)",
]
for ln in nasty:
    out = md_to_html(ln, one_per_line=True)
    ok = 'href=' in out
    print(f"[{'OK' if ok else 'FAIL'}] {out.strip()}")
    assert ok, f"md_to_html failed to produce an anchor for: {ln}"

print()
print("=== md_to_markdown (Telegram v1) ===")
print(md_to_markdown("1) [Tom [Br].mp4](https://t.me/c/1/2)"))

print()
print("=== extract_md_link_label ===")
for ln in ["1) [Tom [Part2].mp4](https://t.me/c/1/2)", "- [Book (1).pdf](u)", "5) plain filename.txt"]:
    print(f"{ln!r} -> {extract_md_link_label(ln)!r}")

print()
print("=== normalize_bracket_links ===")
print(normalize_bracket_links("[Some Book] https://t.me/c/123/460\n[Tom [Part]] https://t.me/c/123/461"))

print()
print("=== md_links_to_clickable ===")
print(md_links_to_clickable(r"1) [Tom\_Torero.mp4](https://t.me/c/1/2)"))

print()
print("ALL_CHECKS_PASSED")
