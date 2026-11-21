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


def _parse_shard(value: str | None):
    """"2/5" -> (2, 5): só tópicos com id % 5 == 2."""
    if not value:
        return None
    i, n = (int(x) for x in value.split("/"))
    if not 0 <= i < n:
        raise SystemExit("--shard deve ser I/N com 0 <= I < N")
    return i, n


def _parse_range(value: str | None):
    """"100-200" -> (100, 200); "-200" -> (None, 200); "100-" -> (100, None)."""
    if not value:
        return None
    lo, _, hi = value.partition("-")
    return (int(lo) if lo else None, int(hi) if hi else None)


def cmd_plan(args):
    db = Storage(args.db)
    for lo, hi in db.plan_id_ranges(args.n):
        print(f"{'' if lo is None else lo}-{'' if hi is None else hi}")
    db.close()


def _make_fetcher(args) -> Fetcher:
    fetcher = Fetcher(delay=args.delay, cookie=args.cookie or os.environ.get("GMBR_COOKIE"))
    user, password = os.environ.get("GMBR_USER"), os.environ.get("GMBR_PASS")
    if user and password and not fetcher.login(user, password):
        sys.exit(1)
    return fetcher


def cmd_discover(args):
    db = Storage(args.db)
    try:
        Crawler(db, _make_fetcher(args)).discover_forums()
    finally:
        db.close()


def cmd_list_pages(args):
    db = Storage(args.db)
    try:
        Crawler(db, _make_fetcher(args)).list_forum_pages(args.forum, args.first, args.last)
    finally:
        db.close()


def _with_crawler(args, fn):
    db = Storage(args.db)
    try:
        crawler = Crawler(db, _make_fetcher(args))
        fn(crawler)
        logging.info("%d requisições feitas", crawler.http.requests_made)
    finally:
        db.close()


def cmd_polls(args):
    _with_crawler(args, lambda c: c.scrape_polls(_parse_shard(args.shard)))


def cmd_memberlist(args):
    _with_crawler(args, lambda c: c.list_members(args.first, args.last))


def cmd_profiles(args):
    _with_crawler(args, lambda c: c.scrape_profiles(_parse_shard(args.shard), args.limit))


def cmd_mark_listed(args):
    db = Storage(args.db)
    for fid in args.forum:
        n = db.conn.execute("SELECT COUNT(*) FROM topics WHERE forum_id=?", (fid,)).fetchone()[0]
        db.mark_forum_scraped(fid, n)
    db.close()


def cmd_crawl(args):
    db = Storage(args.db)
    cookie = args.cookie or os.environ.get("GMBR_COOKIE")
    fetcher = Fetcher(delay=args.delay, cookie=cookie)
    user = os.environ.get("GMBR_USER")
    password = os.environ.get("GMBR_PASS")
    if user and password and not fetcher.login(user, password):
        db.close()
        sys.exit(1)
    crawler = Crawler(db, fetcher)
    try:
        crawler.run(
            forum_ids=args.forum, topic_ids=args.topic,
            skip_listing=args.skip_listing, refresh=args.refresh,
            max_topics=args.max_topics, max_forum_pages=args.max_forum_pages,
            shard=_parse_shard(args.shard), full_listing=args.full_listing,
            id_range=_parse_range(args.id_range), list_only=args.list_only,
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
    forums = [dict(r) for r in db.conn.execute(
        """SELECT f.*, s.topic_count, s.post_count, s.last_post_date
           FROM forums f JOIN v_forum_stats s ON s.forum_id = f.id ORDER BY f.id""")]
    topics = [dict(r) for r in db.conn.execute(
        """SELECT t.*, s.post_count, s.reply_count, s.page_count, s.participants,
                  s.author_id, s.author_name, s.first_post_date,
                  s.last_post_id, s.last_author_id, s.last_author_name, s.last_post_date,
                  s.likes
           FROM topics t JOIN v_topic_stats s ON s.topic_id = t.id ORDER BY t.id""")]
    post_cols = "p.id, p.topic_id, p.page_start, p.position, p.title, p.author_id, " \
                "p.author_name, p.author_id_inferred, p.date_raw, p.date, p.edit_info, " \
                "p.edit_count, p.last_edit_date, p.dislikes, p.vote_title"
    if args.with_content:
        post_cols += ", p.content_text, p.content_html, p.signature_html"
    posts = []
    for r in db.conn.execute(
            f"""SELECT {post_cols},
                       (SELECT json_group_array(liker_name) FROM post_likes l
                         WHERE l.post_id = p.id) AS liked_by
                FROM posts p ORDER BY p.topic_id, p.position"""):
        d = dict(r)
        d["liked_by"] = json.loads(d["liked_by"] or "[]")
        d["likes"] = len(d["liked_by"])
        posts.append(d)

    polls = []
    for r in db.conn.execute("SELECT * FROM v_poll_results ORDER BY topic_id"):
        d = dict(r)
        d["options"] = [dict(o) for o in db.conn.execute(
            "SELECT position, text, votes, percent FROM v_poll_options "
            "WHERE topic_id=? ORDER BY position", (d["topic_id"],))]
        polls.append(d)
    guests = [dict(r) for r in db.conn.execute(
        "SELECT * FROM v_guest_authors ORDER BY post_count DESC")]
    for name, rows in (("members", members), ("forums", forums),
                       ("topics", topics), ("posts", posts), ("polls", polls),
                       ("guest_authors", guests)):
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
    print(f"Perfis raspados:   {q('SELECT COUNT(*) FROM members WHERE profile_scraped_at IS NOT NULL')}")
    print(f"Enquetes:          {q('SELECT COUNT(*) FROM polls')}")
    diff = q("SELECT COUNT(*) FROM v_count_checks WHERE kind='topic'")
    print(f"Tópicos com contagem diferente do fórum: {diff}")
    print("\nTop 10 autores (mensagens raspadas):")
    for r in db.conn.execute(
            "SELECT name, post_count FROM v_member_stats ORDER BY post_count DESC LIMIT 10"):
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
    c.add_argument("--full-listing", action="store_true",
                   help="lista todas as páginas dos fóruns (sem parada incremental)")
    c.add_argument("--refresh", action="store_true",
                   help="raspa de novo todos os tópicos desde a 1ª página")
    c.add_argument("--max-topics", type=int, help="limita nº de tópicos (testes)")
    c.add_argument("--max-forum-pages", type=int, help="limita páginas de listagem por fórum")
    c.add_argument("--shard", metavar="I/N",
                   help="divide os tópicos entre N processos; este pega id %% N == I")
    c.add_argument("--id-range", metavar="MIN-MAX",
                   help="só tópicos com ID na faixa (ex.: 1000-5000, -999, 5001-)")
    c.add_argument("--list-only", action="store_true",
                   help="só lista os fóruns, sem raspar mensagens")
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

    d = sub.add_parser("discover", help="só descobre fóruns/subfóruns e grupos")
    d.add_argument("--delay", type=float, default=1.0)
    d.add_argument("--cookie")
    d.set_defaults(func=cmd_discover)

    lp = sub.add_parser("list-pages",
                        help="lista só um intervalo de páginas de um fórum (por offset)")
    lp.add_argument("--forum", type=int, required=True)
    lp.add_argument("--first", type=int, required=True, help="offset inicial (ex.: 3210)")
    lp.add_argument("--last", type=int, required=True, help="offset final, inclusive")
    lp.add_argument("--delay", type=float, default=1.0)
    lp.add_argument("--cookie")
    lp.set_defaults(func=cmd_list_pages)

    ml = sub.add_parser("mark-listed", help="marca a listagem de fóruns como concluída")
    ml.add_argument("--forum", type=int, action="append", required=True)
    ml.set_defaults(func=cmd_mark_listed)

    pr = sub.add_parser("plan-ranges",
                        help="divide os tópicos pendentes em N faixas de ID equilibradas")
    pr.add_argument("-n", type=int, default=10)
    pr.set_defaults(func=cmd_plan)

    po = sub.add_parser("polls", help="raspa as enquetes dos tópicos marcados [Votação]")
    po.add_argument("--shard", metavar="I/N")
    po.add_argument("--delay", type=float, default=1.0)
    po.add_argument("--cookie")
    po.set_defaults(func=cmd_polls)

    mb = sub.add_parser("memberlist", help="percorre a lista de membros (/memberlist)")
    mb.add_argument("--first", type=int, default=0, help="offset inicial (múltiplo de 30)")
    mb.add_argument("--last", type=int, help="offset final, inclusive")
    mb.add_argument("--delay", type=float, default=1.0)
    mb.add_argument("--cookie")
    mb.set_defaults(func=cmd_memberlist)

    pf = sub.add_parser("profiles", help="raspa as páginas de perfil (/uNN) dos membros")
    pf.add_argument("--shard", metavar="I/N")
    pf.add_argument("--limit", type=int)
    pf.add_argument("--delay", type=float, default=1.0)
    pf.add_argument("--cookie")
    pf.set_defaults(func=cmd_profiles)

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
