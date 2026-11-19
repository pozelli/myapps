"""Linha de comando: python -m gmbr_scraper <comando> [opções]."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
from pathlib import Path

from .crawler import Crawler, Fetcher
from .storage import Storage


def cmd_crawl(args):
    db = Storage(args.db)
    cookie = args.cookie or os.environ.get("GMBR_COOKIE")
    crawler = Crawler(db, Fetcher(delay=args.delay, cookie=cookie))
    try:
        crawler.run(
            forum_ids=args.forum, topic_ids=args.topic,
            skip_listing=args.skip_listing, refresh=args.refresh,
            max_topics=args.max_topics, max_forum_pages=args.max_forum_pages,
        )
    except KeyboardInterrupt:
        logging.warning("Interrompido — progresso salvo; rode de novo para continuar.")
    finally:
        db.close()
        logging.info("%d requisições feitas", crawler.http.requests_made)


def _write_csv(path: Path, rows: list[dict]):
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list))
                        else v for k, v in r.items()})


def cmd_export(args):
    db = Storage(args.db)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    members = db.members_report()
    forums = [dict(r) for r in db.forums()]
    topics = [dict(r) for r in db.conn.execute("SELECT * FROM topics ORDER BY id")]
    post_cols = "id, topic_id, page_start, position, title, author_name, author_id, " \
                "date_raw, date, edit_info, edit_count, last_edit_date, likes, " \
                "dislikes, liked_by, vote_title"
    if args.with_content:
        post_cols += ", content_text, content_html, signature_html"
    posts = []
    for r in db.conn.execute(f"SELECT {post_cols} FROM posts ORDER BY topic_id, position"):
        d = dict(r)
        d["liked_by"] = json.loads(d["liked_by"] or "[]")
        posts.append(d)

    for name, rows in (("members", members), ("forums", forums),
                       ("topics", topics), ("posts", posts)):
        (out / f"{name}.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_csv(out / f"{name}.csv", rows)
        logging.info("%s: %d registros", name, len(rows))
    db.close()


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name, flags=re.UNICODE).strip("_") or "member"


def cmd_avatars(args):
    """Baixa os avatares dos membros para uma pasta local."""
    db = Storage(args.db)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    http = Fetcher(delay=args.delay)
    rows = db.conn.execute(
        "SELECT name, avatar_url FROM members WHERE avatar_url IS NOT NULL").fetchall()
    ok = 0
    for r in rows:
        ext = os.path.splitext(r["avatar_url"].split("?")[0])[1][:5] or ".img"
        target = out / f"{_safe_name(r['name'])}{ext}"
        if target.exists():
            continue
        try:
            resp = http.session.get(r["avatar_url"], timeout=30)
            if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image"):
                target.write_bytes(resp.content)
                ok += 1
            else:
                logging.warning("Avatar de %s indisponível (%s)", r["name"], resp.status_code)
        except Exception as e:  # noqa: BLE001
            logging.warning("Avatar de %s falhou: %s", r["name"], e)
    logging.info("%d avatares baixados em %s", ok, out)
    db.close()


def cmd_stats(args):
    db = Storage(args.db)
    q = lambda sql: db.conn.execute(sql).fetchone()[0]  # noqa: E731
    print(f"Fóruns:            {q('SELECT COUNT(*) FROM forums')}")
    print(f"Tópicos listados:  {q('SELECT COUNT(*) FROM topics')}")
    print(f"Tópicos completos: {q('SELECT COUNT(*) FROM topics WHERE completed=1')}")
    print(f"Mensagens:         {q('SELECT COUNT(*) FROM posts')}")
    print(f"Membros:           {q('SELECT COUNT(*) FROM members')}")
    print("\nTop 10 autores (mensagens raspadas):")
    for r in db.conn.execute(
            "SELECT author_name, COUNT(*) c FROM posts GROUP BY author_name "
            "ORDER BY c DESC LIMIT 10"):
        print(f"  {r[1]:6d}  {r[0]}")
    db.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gmbr_scraper",
                                 description="Scraper do fórum gmbr.forumeiros.com")
    ap.add_argument("--db", default="gmbr.sqlite", help="arquivo SQLite (padrão: gmbr.sqlite)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("crawl", help="raspa fóruns, tópicos e mensagens")
    c.add_argument("--forum", type=int, action="append",
                   help="ID de fórum a raspar (repita para vários). Padrão: todos")
    c.add_argument("--topic", type=int, action="append",
                   help="raspa apenas este tópico (repita para vários)")
    c.add_argument("--delay", type=float, default=1.0,
                   help="segundos entre requisições (padrão: 1.0)")
    c.add_argument("--skip-listing", action="store_true",
                   help="não relista os fóruns; só raspa tópicos pendentes já no banco")
    c.add_argument("--refresh", action="store_true",
                   help="raspa de novo todos os tópicos desde a 1ª página")
    c.add_argument("--max-topics", type=int, help="limita nº de tópicos (testes)")
    c.add_argument("--max-forum-pages", type=int, help="limita páginas de listagem por fórum")
    c.add_argument("--cookie", help="cabeçalho Cookie de uma sessão logada (ou GMBR_COOKIE)")
    c.set_defaults(func=cmd_crawl)

    e = sub.add_parser("export", help="exporta JSON/CSV")
    e.add_argument("--out", default="export")
    e.add_argument("--with-content", action="store_true",
                   help="inclui texto/HTML das mensagens no export de posts")
    e.set_defaults(func=cmd_export)

    a = sub.add_parser("avatars", help="baixa avatares dos membros")
    a.add_argument("--out", default="avatars")
    a.add_argument("--delay", type=float, default=0.3)
    a.set_defaults(func=cmd_avatars)

    s = sub.add_parser("stats", help="resumo do banco")
    s.set_defaults(func=cmd_stats)

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    args.func(args)


if __name__ == "__main__":
    main()
