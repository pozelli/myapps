"""Crawler: percorre fóruns → tópicos → páginas, gravando tudo no SQLite."""
from __future__ import annotations

import logging
import re
import time

import requests

from . import parser
from .storage import Storage

log = logging.getLogger("gmbr")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 gmbr-archiver"
)


class Fetcher:
    """Sessão HTTP com intervalo mínimo entre requisições e novas tentativas."""

    def __init__(self, delay: float = 1.0, retries: int = 4, timeout: float = 30,
                 cookie: str | None = None):
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "pt-BR,pt;q=0.9",
        })
        if cookie:
            # Cookie de uma sessão logada (copiado do navegador), opcional.
            # Com login, as mensagens trazem links /uNN e os IDs dos membros.
            self.session.headers["Cookie"] = cookie
        self._last = 0.0
        self.requests_made = 0

    def get(self, path_or_url: str) -> str:
        url = parser.abs_url(path_or_url)
        for attempt in range(1, self.retries + 1):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.session.get(url, timeout=self.timeout)
                self.requests_made += 1
                if r.status_code == 200:
                    r.encoding = r.encoding or "utf-8"
                    return r.text
                if r.status_code == 404:
                    raise FileNotFoundError(url)
                log.warning("HTTP %s em %s (tentativa %d)", r.status_code, url, attempt)
            except requests.RequestException as e:
                log.warning("Erro de rede em %s: %s (tentativa %d)", url, e, attempt)
            time.sleep(min(60, 2 ** attempt * max(self.delay, 1)))
        raise RuntimeError(f"Falha ao baixar {url}")


def _page_path(base_url: str, kind: str, item_id: int, start: int) -> str:
    """/t123-slug -> /t123p50-slug (start=0 devolve a própria URL)."""
    path = re.sub(r"^https?://[^/]+", "", base_url)
    if start == 0:
        return path
    return re.sub(rf"^/{kind}{item_id}(?:p\d+)?-", f"/{kind}{item_id}p{start}-", path)


class Crawler:
    def __init__(self, storage: Storage, fetcher: Fetcher):
        self.db = storage
        self.http = fetcher

    # -------------------------------------------------------------- descoberta
    def discover_forums(self) -> list[int]:
        """Lê o índice e todos os fóruns/subfóruns encontrados recursivamente."""
        html = self.http.get("/")
        for g in parser.parse_groups(html):
            self.db.upsert_group(g)
        queue = parser.parse_forum_links(html)
        seen: set[int] = set()
        for f in queue:
            self.db.upsert_forum(f)
        while queue:
            f = queue.pop(0)
            if f["id"] in seen:
                continue
            seen.add(f["id"])
            try:
                page = parser.parse_forum_page(self.http.get(f["url"]), f["id"])
            except FileNotFoundError:
                log.warning("Fórum %s não encontrado", f["id"])
                continue
            crumbs = page["breadcrumb"]
            parent = crumbs[-2]["id"] if len(crumbs) >= 2 else None
            self.db.upsert_forum(f, parent_id=parent)
            for sub in page["subforums"]:
                if sub["id"] not in seen:
                    self.db.upsert_forum(sub, parent_id=f["id"])
                    queue.append(sub)
        self.db.commit()
        log.info("%d fóruns encontrados", len(seen))
        return sorted(seen)

    def list_forum_topics(self, forum_id: int, max_pages: int | None = None) -> int:
        """Percorre todas as páginas de listagem de um fórum."""
        row = self.db.conn.execute("SELECT url FROM forums WHERE id=?", (forum_id,)).fetchone()
        base = row["url"] if row and row["url"] else f"/f{forum_id}-"
        start, step, last, pages, total = 0, 30, 0, 0, 0
        seen: set[int] = set()
        while True:
            html = self.http.get(_page_path(base, "f", forum_id, start))
            data = parser.parse_forum_page(html, forum_id)
            if pages == 0:
                step = data["page_step"] or step
            last = max(last, data["last_offset"])
            new = 0
            for t in data["topics"]:
                if t["id"] in seen:
                    continue  # anúncios/fixos se repetem em todas as páginas
                seen.add(t["id"])
                self.db.upsert_topic_listing(t)
                new += 1
            total += new
            pages += 1
            self.db.commit()
            log.info("Fórum %d: página %d (start=%d) → %d tópicos", forum_id, pages, start, new)
            if not data["topics"] or start >= last or (max_pages and pages >= max_pages):
                break
            start += step
        self.db.mark_forum_scraped(forum_id, total)
        self.db.commit()
        return total

    # ----------------------------------------------------------------- tópicos
    def scrape_topic(self, topic_id: int, full: bool = False) -> int:
        """Raspa um tópico página por página. Retorna nº de mensagens gravadas.

        Retoma do ponto onde parou: começa pela última página já raspada (que
        pode ter recebido mensagens novas) a menos que full=True.
        """
        row = self.db.get_topic(topic_id)
        base = row["url"] if row and row["url"] else f"/t{topic_id}-"
        start = 0 if full else (self.db.last_page_start(topic_id) or 0)
        step, last = 50, 0
        pages = 0
        saved = 0
        title = forum_id = None
        while True:
            try:
                html = self.http.get(_page_path(base, "t", topic_id, start))
            except FileNotFoundError:
                log.warning("Tópico %d (start=%d) não encontrado", topic_id, start)
                break
            if parser.is_login_page(html):
                log.warning("Tópico %d exige login; pulando", topic_id)
                break
            data = parser.parse_topic_page(html, topic_id)
            if pages == 0:
                step = data["page_step"] or step
                title, forum_id = data["title"], data["forum_id"]
            last = max(last, data["last_offset"])
            for i, post in enumerate(data["posts"]):
                self.db.upsert_post(post, start, start + i)
                prof = post.get("profile") or {}
                if prof.get("name"):
                    self.db.upsert_member(prof, post["id"], post.get("signature_html"))
                saved += 1
            self.db.mark_page(topic_id, start, len(data["posts"]))
            self.db.commit()
            pages += 1
            log.info("Tópico %d: start=%d → %d mensagens", topic_id, start, len(data["posts"]))
            if not data["posts"] or start >= last:
                break
            start += step
        total_pages = (last // step) + 1 if step else pages
        self.db.finish_topic(topic_id, title, forum_id, total_pages)
        self.db.commit()
        return saved

    def run(self, forum_ids: list[int] | None = None, topic_ids: list[int] | None = None,
            skip_listing: bool = False, refresh: bool = False,
            max_topics: int | None = None, max_forum_pages: int | None = None):
        if topic_ids:
            for tid in topic_ids:
                self.db.ensure_topic(tid)
                self.scrape_topic(tid, full=refresh)
            return

        if not skip_listing:
            all_forums = self.discover_forums()
            targets = forum_ids or all_forums
            for fid in targets:
                self.list_forum_topics(fid, max_pages=max_forum_pages)

        pending = self.db.topics_to_scrape(forum_ids, refresh=refresh)
        if max_topics:
            pending = pending[:max_topics]
        log.info("%d tópicos para raspar", len(pending))
        for n, t in enumerate(pending, 1):
            log.info("[%d/%d] t%d %s", n, len(pending), t["id"], t["title"] or "")
            try:
                self.scrape_topic(t["id"], full=refresh)
            except RuntimeError as e:
                log.error("%s — seguindo para o próximo tópico", e)
