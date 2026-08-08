#!/usr/bin/env python3
"""Patchright helper: render mintrans.uz listing page and print HTML to stdout.
Run as: xvfb-run -a <cenipa-venv-python> aaiuz_patchright_helper.py <url>
"""
import sys, time, re

url = sys.argv[1] if len(sys.argv) > 1 else "https://www.mintrans.uz/ru/aviatsiyahodisalari"
PROFILE = "~/israel-ingest/.cf-profile"

from patchright.sync_api import sync_playwright

pw = sync_playwright().__enter__()
ctx = pw.chromium.launch_persistent_context(
    PROFILE, headless=False, args=["--disable-dev-shm-usage"]
)
page = ctx.pages[0] if ctx.pages else ctx.new_page()
page.goto(url, wait_until="domcontentloaded", timeout=30000)
time.sleep(5)
html = page.content()
sys.stdout.buffer.write(html.encode("utf-8", "replace"))
sys.stdout.flush()
try:
    ctx.close()
except Exception:
    pass
