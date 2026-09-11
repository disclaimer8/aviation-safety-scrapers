# ipiaam_ingest/cli.py
import argparse
import os

from . import ipiaam as src, db
from .pipeline import discover, fetch, parse, build


def _make_client():
    import httpx
    return httpx.Client(transport=httpx.HTTPTransport(retries=3), 
        headers={
            'User-Agent': src.UA,
            'Referer': src.REFERER,
        },
        follow_redirects=True,
        timeout=30.0,
    )


def main(argv=None):
    ap = argparse.ArgumentParser(prog='ipiaam-ingest')
    ap.add_argument('mode', choices=['discover', 'fetch', 'parse', 'build', 'all'])
    ap.add_argument('--db', default='ipiaam.db')
    ap.add_argument('--pdf-dir', default='pdfs')
    ap.add_argument('--full', action='store_true')
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    db.init_schema(conn)
    client = _make_client()
    try:
        if args.mode in ('discover', 'all'):
            print('discovered:', discover(conn, client, full=args.full))
        if args.mode in ('fetch', 'all'):
            print('fetched:', fetch(conn, client, args.pdf_dir))
        if args.mode in ('parse', 'all'):
            print('parsed:', parse(conn))
        if args.mode in ('build', 'all'):
            print('built:', build(conn))
    finally:
        client.close()
        conn.close()


if __name__ == '__main__':
    main()
