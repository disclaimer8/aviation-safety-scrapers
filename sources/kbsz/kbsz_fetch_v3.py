#!/usr/bin/env python3
"""Clean batch fetch of KBSZ HU PDFs via hetzner.

Pipes URL list to a script on hetzner, which downloads them sequentially
(fast enough since hetzner↔kbsz.hu is direct). Then rsync back.
"""
import sqlite3, subprocess, os, re, time

DB = os.path.expanduser("~/kbsz-ingest/kbsz.db")
PDFDIR = os.path.expanduser("~/kbsz-ingest/pdfs")
# The OCR host is read from the environment. It used to be written in
# here; a hostname and the account it is reached as are infrastructure
# detail this repository deliberately carries none of — see the other
# sources, which all take it from OCR_REMOTE.
HETZNER_HOST = os.environ.get("OCR_REMOTE", "")
REMOTE_DIR = "/tmp/kbsz-hu-pdfs"

def main():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    rows = c.execute(
        "SELECT case_id, source_url FROM kbsz_reports WHERE status='hu_new'"
    ).fetchall()
    print(f"[fetch] {len(rows)} hu_new rows")

    # Build input list
    downloads = []
    for row in rows:
        cid = row["case_id"]
        url = row["source_url"]
        safe_name = "hu-" + re.sub(r"[^A-Za-z0-9_.\-]", "_", cid) + ".pdf"
        dest = os.path.join(PDFDIR, safe_name)
        # Skip if already valid on disk
        if os.path.exists(dest) and os.path.getsize(dest) > 500:
            with open(dest, "rb") as f:
                if f.read(4) == b"%PDF":
                    c.execute(
                        "UPDATE kbsz_reports SET pdf_path=?, status='hu_fetched', updated_at=? WHERE case_id=?",
                        (dest, int(time.time()*1000), cid),
                    )
                    c.commit()
                    continue
        downloads.append((cid, url, safe_name, dest))

    print(f"[fetch] {len(downloads)} to download (others already on disk)")

    if not downloads:
        return

    # Build stdin: "FILENAME URL\n" per line
    stdin_lines = "\n".join(f"{safe_name} {url}" for _, url, safe_name, _ in downloads) + "\n"

    # Ensure remote dir is clean
    subprocess.run(["ssh", HETZNER_HOST, "rm", "-rf", REMOTE_DIR], capture_output=True, timeout=20)
    subprocess.run(["ssh", HETZNER_HOST, "mkdir", "-p", REMOTE_DIR], capture_output=True, timeout=20)

    print(f"[fetch] running /usr/local/bin/kbsz_fetch_all.sh on hetzner...")
    result = subprocess.run(
        ["ssh", HETZNER_HOST, "/usr/local/bin/kbsz_fetch_all.sh"],
        input=stdin_lines.encode(),
        capture_output=True,
        timeout=7200,  # 2 hour max
    )
    output = result.stdout.decode("utf-8", "replace")
    print(f"[fetch] rc={result.returncode} output_lines={len(output.strip().splitlines())}")

    # Parse output
    ok_files = set()
    fail_files = set()
    for line in output.strip().splitlines():
        if line.startswith("OK:") or line.startswith("SKIP:"):
            ok_files.add(line.split(":", 1)[1])
        elif line.startswith("FAIL:"):
            fail_files.add(line.split(":", 1)[1])
    print(f"[fetch] hetzner: ok={len(ok_files)} fail={len(fail_files)}")
    if fail_files:
        print(f"  fail sample: {list(fail_files)[:5]}")

    # Print summary line if present
    for line in output.strip().splitlines():
        if line.startswith("SUMMARY:"):
            print(f"  {line}")

    # rsync all PDFs back to minipc
    print(f"[fetch] rsyncing from hetzner...")
    rsync = subprocess.run(
        ["rsync", "-az",
         f"{HETZNER_HOST}:{REMOTE_DIR}/",
         PDFDIR + "/"],
        capture_output=True, timeout=600
    )
    print(f"[fetch] rsync rc={rsync.returncode}")
    if rsync.returncode != 0:
        print(f"  rsync stderr: {rsync.stderr[:300].decode()}")

    # Update DB
    updated = 0
    failed = 0
    for cid, url, safe_name, dest in downloads:
        if os.path.exists(dest) and os.path.getsize(dest) > 500:
            with open(dest, "rb") as f:
                magic = f.read(4)
            if magic == b"%PDF":
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
        ["ssh", HETZNER_HOST, "rm", "-rf", REMOTE_DIR],
        capture_output=True, timeout=30
    )

    # Print DB status
    print("\n=== DB STATUS ===")
    for row in c.execute("SELECT status, skip_reason, count(*) n FROM kbsz_reports GROUP BY status, skip_reason ORDER BY n DESC"):
        print(f"  {row[0]:15s}  {(row[1] or ''):25s}  {row[2]}")

if __name__ == "__main__":
    main()
