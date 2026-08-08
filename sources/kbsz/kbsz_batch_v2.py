#!/usr/bin/env python3
"""Batch download remaining HU PDFs via hetzner.

Strategy:
1. Build list of (filename, url) for all hu_new rows
2. Write a bash script on hetzner that downloads all PDFs with curl
3. Run it in background on hetzner, poll until done
4. rsync the dir back to minipc
5. Update DB
"""
import sqlite3, subprocess, os, re, time, sys

DB = os.path.expanduser("~/kbsz-ingest/kbsz.db")
PDFDIR = os.path.expanduser("~/kbsz-ingest/pdfs")
# The OCR host is read from the environment. It used to be written in
# here; a hostname and the account it is reached as are infrastructure
# detail this repository deliberately carries none of — see the other
# sources, which all take it from OCR_REMOTE.
HETZNER_HOST = os.environ.get("OCR_REMOTE", "")
REMOTE_DIR = "/tmp/kbsz-pdfs-hu"

def sh_run(cmd_list, **kw):
    """Run subprocess, return CompletedProcess. Always capture."""
    return subprocess.run(cmd_list, capture_output=True, **kw)

def main():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    rows = c.execute(
        "SELECT case_id, source_url FROM kbsz_reports WHERE status='hu_new'"
    ).fetchall()
    print(f"[batch] {len(rows)} hu_new rows")

    # Build download list
    downloads = []  # (case_id, url, safe_name, local_dest)
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

    print(f"[batch] {len(downloads)} to download")
    if not downloads:
        return

    # Step 1: setup remote dir
    sh_run(["ssh", HETZNER_HOST, "rm", "-rf", REMOTE_DIR])
    sh_run(["ssh", HETZNER_HOST, "mkdir", "-p", REMOTE_DIR])

    # Step 2: build and upload download script
    # Use a script that downloads each URL, one per line
    lines = ["#!/bin/bash"]
    lines.append(f"cd {REMOTE_DIR}")
    for cid, url, safe_name, dest in downloads:
        # Single-quote the URL to handle spaces and special chars in filename part
        # URL encode any spaces (already done), but the filename has spaces - use just the safe_name
        lines.append(f"curl -sk --max-time 120 -o {safe_name!r} {url!r}")
    lines.append("echo 'ALL_DONE'")
    script = "\n".join(lines) + "\n"

    # Write script to hetzner via stdin
    write_r = sh_run(
        ["ssh", HETZNER_HOST, "bash", "-c", f"cat > /tmp/kbsz_dl.sh && chmod +x /tmp/kbsz_dl.sh"],
        input=script.encode(),
        timeout=30,
    )
    print(f"  write script rc={write_r.returncode}")
    if write_r.returncode != 0:
        print(f"  stderr: {write_r.stderr}")
        sys.exit(1)

    # Verify script got written correctly
    verify_r = sh_run(["ssh", HETZNER_HOST, "wc", "-l", "/tmp/kbsz_dl.sh"], timeout=10)
    print(f"  script lines: {verify_r.stdout.decode().strip()}")

    # Test one download manually first
    print(f"  testing one download...")
    cid0, url0, name0, _ = downloads[0]
    test_r = sh_run(
        ["ssh", HETZNER_HOST, "curl", "-sk", "--max-time", "60",
         "-o", f"{REMOTE_DIR}/{name0}", url0],
        timeout=90
    )
    size_r = sh_run(["ssh", HETZNER_HOST, "stat", "-c", "%s", f"{REMOTE_DIR}/{name0}"], timeout=10)
    print(f"  test download rc={test_r.returncode} size={size_r.stdout.decode().strip()}")

    # Step 3: run the full download script on hetzner
    print(f"  running batch download on hetzner (background)...")
    # Run in background on hetzner, redirect output to log
    bg_cmd = "nohup bash /tmp/kbsz_dl.sh > /tmp/kbsz_dl.log 2>&1 &"
    sh_run(["ssh", HETZNER_HOST, "bash", "-c", bg_cmd], timeout=15)

    # Poll for completion
    print("  polling hetzner for completion...")
    max_wait = 1800  # 30 min
    poll_interval = 30
    waited = 0
    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        # Check if script is still running and how many files we have
        check_r = sh_run(
            ["ssh", HETZNER_HOST, "bash", "-c",
             f"grep -c 'ALL_DONE' /tmp/kbsz_dl.log 2>/dev/null; ls {REMOTE_DIR}/*.pdf 2>/dev/null | wc -l"],
            timeout=30
        )
        out = check_r.stdout.decode().strip().split("\n")
        done_flag = int(out[0]) if out and out[0].strip().isdigit() else 0
        file_count = int(out[1]) if len(out) > 1 and out[1].strip().isdigit() else 0
        print(f"  [{waited}s] done_flag={done_flag} files={file_count}", flush=True)
        if done_flag > 0:
            print(f"  download complete! {file_count} files on hetzner")
            break

    # Step 4: rsync to minipc
    print(f"  rsyncing pdfs from hetzner to minipc...")
    rsync_r = sh_run(
        ["rsync", "-az", f"{HETZNER_HOST}:{REMOTE_DIR}/",
         PDFDIR + "/"],
        timeout=600
    )
    print(f"  rsync rc={rsync_r.returncode}")

    # Step 5: update DB
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
        # Not downloaded or invalid
        c.execute(
            "UPDATE kbsz_reports SET status='skipped', skip_reason='fetch-failed', updated_at=? WHERE case_id=?",
            (int(time.time()*1000), cid),
        )
        c.commit()
        failed += 1

    print(f"[batch] updated={updated} failed={failed}")

    # Cleanup hetzner
    sh_run(["ssh", HETZNER_HOST, "rm", "-rf", REMOTE_DIR, "/tmp/kbsz_dl.sh", "/tmp/kbsz_dl.log"],
           timeout=30)

    # Print DB status
    for row in c.execute("SELECT status, skip_reason, count(*) n FROM kbsz_reports GROUP BY status, skip_reason ORDER BY n DESC"):
        print(f"  {row[0]:15s}  {(row[1] or ''):25s}  {row[2]}")

if __name__ == "__main__":
    main()
