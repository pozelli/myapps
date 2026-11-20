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
        self._cookie = cookie
        self.session = self._new_session()
        self._last = 0.0
        self.requests_made = 0
        self.credentials: tuple[str, str] | None = None
        self.expect_logged_in = bool(cookie)
        self._renewing = False

    def _new_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "pt-BR,pt;q=0.9",
        })
        if self._cookie:
            # Cookie de uma sessão logada (copiado do navegador), opcional.
            # Com login, as mensagens trazem links /uNN e os IDs dos membros.
            # Vai para o cookie jar (e não para o cabeçalho), senão o requests
            # descarta o cookie ao seguir redirecionamentos.
            for part in self._cookie.split(";"):
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    session.cookies.set(k, v, domain=parser.BASE_URL.split("//")[1])
        return session

    def renew_session(self) -> None:
        """Descarta a sessão HTTP atual e abre outra (refazendo o login)."""
        self.session = self._new_session()
        if self.credentials:
            self.login(*self.credentials)

    def login(self, username: str, password: str) -> bool:
        """Inicia sessão no fórum; os cookies ficam na sessão HTTP."""
        self.get("/login", check_session=False)
        r = self.session.post(parser.abs_url("/login"), timeout=self.timeout, data={
            "username": username, "password": password, "autologin": "on",
            "redirect": "", "query": "", "login": "Iniciar sessão",
        })
        self._last = time.monotonic()
        ok = r.status_code == 200 and "/login?logout" in self.get("/", check_session=False)
        if ok:
            self.credentials = (username, password)
            self.expect_logged_in = True
            log.info("Login como %s OK", username)
        else:
            log.error("Falha no login como %s", username)
        return ok

    def get(self, path_or_url: str, check_session: bool = True) -> str:
        try:
            text = self._get(path_or_url)
        except FileNotFoundError:
            # Sessões longas passam a receber 404 em páginas que existem; só
            # aceita o 404 depois de confirmar com uma sessão nova.
            log.warning("404 em %s; confirmando com sessão nova", path_or_url)
            self.renew_session()
            text = self._get(path_or_url)
        if check_session and self.expect_logged_in and "/login?logout" not in text:
            log.warning("Sessão de login expirou em %s", path_or_url)
            if self.credentials and self.login(*self.credentials):
                text = self._get(path_or_url)
        return text

    def _get(self, path_or_url: str) -> str:
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
                if r.status_code >= 500 and attempt == 2 and not self._renewing:
                    # Sessões longas também passam a receber 5xx em páginas
                    # que abrem normalmente numa sessão nova.
                    log.warning("Renovando sessão após erros %s", r.status_code)
                    self._renewing = True
                    try:
                        self.renew_session()
                    finally:
                        self._renewing = False
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

    def list_forum_topics(self, forum_id: int, max_pages: int | None = None,
                          full: bool = False) -> int:
        """Percorre as páginas de listagem de um fórum.

        A listagem vem ordenada pela última mensagem, então:
        - se o fórum já foi listado por completo antes, para na primeira página
          em que nenhum tópico mudou (sem tópicos novos nem respostas novas);
        - se uma listagem anterior foi interrompida, relê o começo até achar uma
          página sem mudanças e pula para onde tinha parado;
        - full=True força a listagem completa.
        """
        forum = self.db.get_forum(forum_id)
        base = forum["url"] if forum and forum["url"] else f"/f{forum_id}-"
        completed_before = bool(forum and forum["scraped_at"]) and not full
        resume_at = (forum["listing_next_start"] or 0) if forum and not full else 0
        start, step, last, pages, total = 0, 30, 0, 0, 0
        seen: set[int] = set()
        finished = False  # chegou ao fim (ou parou por não haver mudanças)
        while True:
            html = self.http.get(_page_path(base, "f", forum_id, start))
            data = parser.parse_forum_page(html, forum_id)
            if pages == 0:
                step = data["page_step"] or step
            last = max(last, data["last_offset"])
            page_topics = [t for t in data["topics"] if t["id"] not in seen]
            known = self.db.topic_last_post_ids([t["id"] for t in page_topics])
            changed = 0
            for t in page_topics:
                seen.add(t["id"])  # anúncios/fixos se repetem em todas as páginas
                if t["id"] not in known or t.get("last_post_id") is None \
                        or known[t["id"]] != t.get("last_post_id"):
                    changed += 1
                self.db.upsert_topic_listing(t)
            total += len(page_topics)
            pages += 1
            log.info("Fórum %d: página %d (start=%d) → %d tópicos, %d novos/alterados",
                     forum_id, pages, start, len(page_topics), changed)
            if not data["topics"] or start >= last:
                finished = True
                self.db.commit()
                break
            if changed == 0 and (completed_before or resume_at > start + step):
                if completed_before:
                    log.info("Fórum %d: sem mudanças a partir daqui; listagem encerrada",
                             forum_id)
                    finished = True
                    self.db.commit()
                    break
                # Listagem anterior interrompida: pula para onde parou, voltando
                # uma página para cobrir deslocamentos causados por tópicos novos.
                log.info("Fórum %d: retomando listagem em start=%d", forum_id,
                         resume_at - step)
                start = resume_at - step
                resume_at = 0
            else:
                start += step
            self.db.set_listing_progress(forum_id, start)
            self.db.commit()
            if max_pages and pages >= max_pages:
                break
        if finished:
            self.db.mark_forum_scraped(forum_id, total)
            self.db.commit()
        return total

    def list_forum_pages(self, forum_id: int, first: int, last: int) -> int:
        """Lista só as páginas com offset entre first e last (inclusive).

        Serve para dividir a listagem de um fórum grande entre processos. Não
        marca a listagem do fórum como concluída (use `mark-listed`)."""
        forum = self.db.get_forum(forum_id)
        base = forum["url"] if forum and forum["url"] else f"/f{forum_id}-"
        start, total = first, 0
        while start <= last:
            data = parser.parse_forum_page(
                self.http.get(_page_path(base, "f", forum_id, start)), forum_id)
            for t in data["topics"]:
                self.db.upsert_topic_listing(t)
            self.db.commit()
            total += len(data["topics"])
            log.info("Fórum %d: start=%d → %d tópicos", forum_id, start, len(data["topics"]))
            if not data["topics"] or start >= data["last_offset"]:
                break
            start += data["page_step"] or 30
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
                if pages == 0:
                    return 0  # não marca como concluído; fica pendente
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
            max_topics: int | None = None, max_forum_pages: int | None = None,
            shard: tuple[int, int] | None = None, full_listing: bool = False,
            id_range: tuple[int | None, int | None] | None = None,
            list_only: bool = False):
        if topic_ids:
            for tid in topic_ids:
                self.db.ensure_topic(tid)
                self.scrape_topic(tid, full=refresh)
            return

        if not skip_listing:
            known = [r["id"] for r in self.db.forums()]
            # Com --forum e fóruns já conhecidos, não refaz a descoberta
            all_forums = known if (forum_ids and known) else self.discover_forums()
            targets = forum_ids or all_forums
            for fid in targets:
                self.list_forum_topics(fid, max_pages=max_forum_pages, full=full_listing)

        if list_only:
            return
        pending = self.db.topics_to_scrape(forum_ids, refresh=refresh, shard=shard,
                                           id_range=id_range)
        if max_topics:
            pending = pending[:max_topics]
        log.info("%d tópicos para raspar", len(pending))
        for n, t in enumerate(pending, 1):
            log.info("[%d/%d] t%d %s", n, len(pending), t["id"], t["title"] or "")
            try:
                self.scrape_topic(t["id"], full=refresh)
            except RuntimeError as e:
                log.error("%s — seguindo para o próximo tópico", e)
