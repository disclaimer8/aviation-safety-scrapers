#!/usr/bin/env python3
"""Parallel KBSZ HU PDF download.

1. Update DB for files already on disk (from partial downloads)
2. Get remaining hu_new list
3. Upload URL list to hetzner
4. Run parallel downloads (20 concurrent) using xargs
5. Rsync back, update DB
"""
import sqlite3, subprocess, os, re, time, sys

DB = os.path.expanduser("~/kbsz-ingest/kbsz.db")
PDFDIR = os.path.expanduser("~/kbsz-ingest/pdfs")
# The OCR host is read from the environment. It used to be written in
# here; a hostname and the account it is reached as are infrastructure
# detail this repository deliberately carries none of — see the other
# sources, which all take it from OCR_REMOTE.
HETZNER_HOST = os.environ.get("OCR_REMOTE", "")
REMOTE_DIR = "/tmp/kbsz-hu-pdfs"

def mark_disk_files(c):
    """Mark as hu_fetched any hu_new rows whose PDF is already on disk."""
    rows = c.execute(
        "SELECT case_id, source_url FROM kbsz_reports WHERE status='hu_new'"
    ).fetchall()
    marked = 0
    for row in rows:
        cid = row["case_id"]
        url = row["source_url"]
        safe_name = "hu-" + re.sub(r"[^A-Za-z0-9_.\-]", "_", cid) + ".pdf"
        dest = os.path.join(PDFDIR, safe_name)
        if os.path.exists(dest) and os.path.getsize(dest) > 500:
            with open(dest, "rb") as f:
                if f.read(4) == b"%PDF":
                    c.execute(
                        "UPDATE kbsz_reports SET pdf_path=?, archive_url=?, status='hu_fetched', updated_at=? WHERE case_id=?",
                        (dest, url, int(time.time()*1000), cid),
                    )
                    c.commit()
                    marked += 1
    return marked

def main():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    # Step 1: mark already-downloaded files
    marked = mark_disk_files(c)
    print(f"[fetch] marked {marked} already-on-disk files as hu_fetched")

    # Step 2: get remaining
    rows = c.execute(
        "SELECT case_id, source_url FROM kbsz_reports WHERE status='hu_new'"
    ).fetchall()
    print(f"[fetch] {len(rows)} still need downloading")

    if not rows:
        print("[fetch] all done!")
        return

    downloads = []
    for row in rows:
        cid = row["case_id"]
        url = row["source_url"]
        safe_name = "hu-" + re.sub(r"[^A-Za-z0-9_.\-]", "_", cid) + ".pdf"
        dest = os.path.join(PDFDIR, safe_name)
        downloads.append((cid, url, safe_name, dest))

    # Step 3: build URL list and pipe to parallel download script on hetzner
    # Each line: "destpath\turl" tab-separated
    lines = []
    for cid, url, safe_name, dest in downloads:
        lines.append(f"{REMOTE_DIR}/{safe_name}\t{url}")
    url_list = "\n".join(lines) + "\n"

    subprocess.run(["ssh", HETZNER_HOST, "rm", "-rf", REMOTE_DIR], capture_output=True, timeout=15)
    subprocess.run(["ssh", HETZNER_HOST, "mkdir", "-p", REMOTE_DIR], capture_output=True, timeout=10)

    # Step 4: pipe URL list directly to the parallel download script on hetzner
    print(f"[fetch] running {len(downloads)} parallel downloads on hetzner (20 concurrent)...")
    run_r = subprocess.run(
        ["ssh", HETZNER_HOST, "/usr/local/bin/kbsz_parallel_dl.sh"],
        input=url_list.encode(),
        capture_output=True,
        timeout=1200,  # 20 min
    )
    output = run_r.stdout.decode("utf-8", "replace")
    ok_count = sum(1 for l in output.splitlines() if l.startswith("OK:"))
    fail_count = sum(1 for l in output.splitlines() if l.startswith("FAIL"))
    skip_count = sum(1 for l in output.splitlines() if l.startswith("SKIP:"))
    print(f"[fetch] hetzner done: OK={ok_count} FAIL={fail_count} SKIP={skip_count} rc={run_r.returncode}")
    if fail_count > 0:
        fails = [l for l in output.splitlines() if l.startswith("FAIL")]
        print(f"  fails: {fails[:10]}")

    # Step 5: rsync back to minipc
    print(f"[fetch] rsyncing from hetzner...")
    rsync = subprocess.run(
        ["rsync", "-az",
         f"{HETZNER_HOST}:{REMOTE_DIR}/",
         PDFDIR + "/"],
        capture_output=True, timeout=600
    )
    print(f"[fetch] rsync rc={rsync.returncode}")

    # Step 6: update DB
    updated = 0
    failed = 0
    for cid, url, safe_name, dest in downloads:
        if os.path.exists(dest) and os.path.getsize(dest) > 500:
            with open(dest, "rb") as f:
                if f.read(4) == b"%PDF":
                    c.execute(
                        "UPDATE kbsz_reports SET pdf_path=?, archive_url=?, status='hu_fetched', updated_at=? WHERE case_id=?",
                        (dest, url, int(time.time()*1000), cid),
                    )
                    c.commit()
                    updated += 1
                    continue
        c.execute(
            "UPDATE kbsz_reports SET status='skipped', skip_reason='fetch-failed', updated_at=? WHERE case_id=?",
            (int(time.time()*1000), cid),
        )
        c.commit()
        failed += 1

    print(f"[fetch] db: updated={updated} failed={failed}")

    # Cleanup hetzner
    subprocess.run(
        ["ssh", HETZNER_HOST, "rm", "-rf", REMOTE_DIR, "/tmp/kbsz_urls.txt", "/tmp/kbsz_dl_parallel.sh"],
        capture_output=True, timeout=30
    )

    print("\n=== DB STATUS ===")
    for row in c.execute("SELECT status, skip_reason, count(*) n FROM kbsz_reports GROUP BY status, skip_reason ORDER BY n DESC"):
        print(f"  {row[0]:15s}  {(row[1] or ''):25s}  {row[2]}")

if __name__ == "__main__":
    main()
