#!/usr/bin/env python3
"""Batch download remaining HU PDFs: generate curl script on hetzner, rsync back.

This is much faster than per-file SSH round trips: we run curl in parallel
on hetzner (20 at a time), then rsync everything back in one shot.
"""
import sqlite3, subprocess, os, re, time, urllib.parse

DB = os.path.expanduser("~/kbsz-ingest/kbsz.db")
PDFDIR = os.path.expanduser("~/kbsz-ingest/pdfs")
# The OCR host is read from the environment. It used to be written in
# here; a hostname and the account it is reached as are infrastructure
# detail this repository deliberately carries none of — see the other
# sources, which all take it from OCR_REMOTE.
HETZNER_HOST = os.environ.get("OCR_REMOTE", "")
HETZNER_TMP = "/tmp/kbsz-batch-pdfs"

def main():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    # Get all remaining hu_new rows
    rows = c.execute(
        "SELECT case_id, source_url FROM kbsz_reports WHERE status='hu_new'"
    ).fetchall()
    print(f"[batch] {len(rows)} rows to download")

    # Build download list: (safe_filename, url)
    downloads = []
    for row in rows:
        cid = row["case_id"]
        url = row["source_url"]
        safe_name = "hu-" + re.sub(r"[^A-Za-z0-9_.\-]", "_", cid) + ".pdf"
        # Skip if already on disk
        dest = os.path.join(PDFDIR, safe_name)
        if os.path.exists(dest) and os.path.getsize(dest) > 500:
            with open(dest, "rb") as f:
                if f.read(4) == b"%PDF":
                    print(f"  {cid}: already on disk, marking fetched")
                    c.execute(
                        "UPDATE kbsz_reports SET pdf_path=?, archive_url=?, status='hu_fetched', updated_at=? WHERE case_id=?",
                        (dest, url, int(time.time()*1000), cid),
                    )
                    c.commit()
                    continue
        downloads.append((safe_name, url, cid, dest))

    if not downloads:
        print("[batch] nothing to download")
        return

    print(f"[batch] {len(downloads)} to download via hetzner")

    # Step 1: create remote tmp dir and generate a parallel curl script
    setup = subprocess.run(
        ["ssh", HETZNER_HOST, "mkdir", "-p", HETZNER_TMP],
        capture_output=True, timeout=30
    )
    print(f"  mkdir rc={setup.returncode}")

    # Build curl commands for parallel execution (20 at a time via xargs)
    # Each line: curl -sk -o /tmp/kbsz-batch-pdfs/FILENAME.pdf URL
    curl_lines = []
    for safe_name, url, cid, dest in downloads:
        remote_dest = f"{HETZNER_TMP}/{safe_name}"
        # URL-encode the URL properly for curl (it handles spaces in URLs)
        curl_lines.append(f"curl -sk --max-time 120 -o {remote_dest} {url}")

    # Write the script to hetzner
    script_content = "\n".join(curl_lines) + "\n"
    write_result = subprocess.run(
        ["ssh", HETZNER_HOST, f"cat > /tmp/kbsz_dl.sh"],
        input=script_content.encode(),
        capture_output=True, timeout=30
    )
    print(f"  write script rc={write_result.returncode}")

    # Step 2: execute in parallel (20 concurrent) on hetzner
    print(f"  running {len(curl_lines)} parallel downloads on hetzner...")
    run_result = subprocess.run(
        ["ssh", HETZNER_HOST,
         "bash", "-c",
         "chmod +x /tmp/kbsz_dl.sh && cat /tmp/kbsz_dl.sh | xargs -P20 -I{} bash -c '{}' 2>/dev/null; echo DONE"],
        capture_output=True, timeout=1800  # 30 min timeout
    )
    stdout = run_result.stdout.decode("utf-8", "replace")
    print(f"  xargs rc={run_result.returncode} done={'DONE' in stdout}")

    # Step 3: check which files are valid PDFs on hetzner
    check_result = subprocess.run(
        ["ssh", HETZNER_HOST,
         "bash", "-c",
         f"for f in {HETZNER_TMP}/hu-*.pdf; do "
         "magic=$(head -c 4 \"$f\" 2>/dev/null); "
         "size=$(stat -c%s \"$f\" 2>/dev/null || echo 0); "
         "if [ \"$magic\" = \"%PDF\" ] && [ \"$size\" -gt 500 ]; then "
         "  echo \"OK:$(basename $f)\"; "
         "else "
         "  echo \"FAIL:$(basename $f)\"; "
         "fi; done"],
        capture_output=True, timeout=120
    )
    check_out = check_result.stdout.decode("utf-8", "replace")

    ok_files = set()
    fail_files = set()
    for line in check_out.strip().split("\n"):
        if line.startswith("OK:"):
            ok_files.add(line[3:])
        elif line.startswith("FAIL:"):
            fail_files.add(line[5:])

    print(f"  hetzner check: OK={len(ok_files)} FAIL={len(fail_files)}")
    if fail_files:
        print(f"  failed files (sample): {list(fail_files)[:5]}")

    # Step 4: rsync valid PDFs to minipc
    print(f"  rsyncing {len(ok_files)} valid PDFs to minipc...")
    rsync = subprocess.run(
        ["rsync", "-az",
         "--include=hu-*.pdf",
         "--exclude=*",
         f"{HETZNER_HOST}:{HETZNER_TMP}/",
         PDFDIR + "/"],
        capture_output=True, timeout=600
    )
    print(f"  rsync rc={rsync.returncode}")
    if rsync.returncode != 0:
        print(f"  rsync stderr: {rsync.stderr[:200].decode()}")

    # Step 5: update DB for successfully transferred files
    updated = 0
    for safe_name, url, cid, dest in downloads:
        if safe_name in ok_files:
            if os.path.exists(dest) and os.path.getsize(dest) > 500:
                c.execute(
                    "UPDATE kbsz_reports SET pdf_path=?, archive_url=?, status='hu_fetched', updated_at=? WHERE case_id=?",
                    (dest, url, int(time.time()*1000), cid),
                )
                c.commit()
                updated += 1
            else:
                print(f"  WARN: {safe_name} in ok_files but missing locally at {dest}")
        else:
            c.execute(
                "UPDATE kbsz_reports SET status='skipped', skip_reason='fetch-failed', updated_at=? WHERE case_id=?",
                (int(time.time()*1000), cid),
            )
            c.commit()

    print(f"[batch] updated={updated} db rows to hu_fetched")

    # Step 6: cleanup hetzner
    subprocess.run(
        ["ssh", HETZNER_HOST, "rm", "-rf", HETZNER_TMP, "/tmp/kbsz_dl.sh"],
        capture_output=True, timeout=60
    )
    print("[batch] done")

    # Print summary
    for row in c.execute("SELECT status, skip_reason, count(*) n FROM kbsz_reports GROUP BY status, skip_reason"):
        print(f"  {row[0]:15s}  {(row[1] or ''):25s}  {row[2]}")


if __name__ == "__main__":
    main()
