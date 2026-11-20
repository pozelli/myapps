import argparse
import os

from .app import serve


def main():
    ap = argparse.ArgumentParser(prog="gmbr_forum", description="Fórum mínimo com o arquivo do GMBR")
    ap.add_argument("--db", default=os.environ.get("GMBR_DB", "data/gmbr.sqlite"),
                    help="banco do gmbr-scraper (aberto só para leitura)")
    ap.add_argument("--local", default=os.environ.get("GMBR_LOCAL_DB", "data/local.sqlite"),
                    help="banco das mensagens novas")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    if not os.path.exists(a.db):
        raise SystemExit(f"Banco {a.db} não encontrado. Rode scripts/restore_backup.sh ou use --db.")
    os.makedirs(os.path.dirname(os.path.abspath(a.local)), exist_ok=True)
    serve(a.db, a.local, a.host, a.port)


main()
