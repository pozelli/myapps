"""Funções puras de parsing do HTML do Forumeiros (tema prosilver).

Nenhuma função aqui faz requisições HTTP: todas recebem HTML (str) e devolvem
dicionários/listas, o que facilita testes e reprocessamento.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

BASE_URL = "https://gmbr.forumeiros.com"

# --------------------------------------------------------------------------
# Datas
# --------------------------------------------------------------------------
_MONTHS = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}
# "Ter 24 Jan 2017, 15:35" / "Sex 16 maio 2025, 08:07"
_DATE_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-zçÇ]+)\.?\s+(\d{4}),?\s+(\d{1,2}):(\d{2})", re.UNICODE
)
# "Hoje às 10:12" / "Ontem às 22:01"
_REL_RE = re.compile(r"\b(Hoje|Ontem)\b\D*(\d{1,2}):(\d{2})", re.IGNORECASE)
# "26/02/2008"
_DMY_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def parse_date(text: str | None, now: datetime | None = None) -> str | None:
    """Converte uma data do fórum em ISO-8601 (sem fuso; horário do fórum)."""
    if not text:
        return None
    m = _DATE_RE.search(text)
    if m:
        day, mon, year, hh, mm = m.groups()
        month = _MONTHS.get(mon.lower()[:3])
        if month:
            try:
                return datetime(int(year), month, int(day), int(hh), int(mm)).isoformat()
            except ValueError:
                return None
    m = _REL_RE.search(text)
    if m:
        word, hh, mm = m.groups()
        base = (now or datetime.now()).replace(second=0, microsecond=0)
        if word.lower() == "ontem":
            base -= timedelta(days=1)
        return base.replace(hour=int(hh), minute=int(mm)).isoformat()
    m = _DMY_RE.search(text)
    if m:
        d, mo, y = map(int, m.groups())
        try:
            return datetime(y, mo, d).date().isoformat()
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def abs_url(href: str | None) -> str | None:
    if not href:
        return None
    return urljoin(BASE_URL + "/", href)


def _int(text: str | None) -> int | None:
    if text is None:
        return None
    m = re.search(r"-?\d[\d.]*", text)
    if not m:
        return None
    try:
        return int(m.group(0).replace(".", ""))
    except ValueError:
        return None


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


_FORUM_HREF_RE = re.compile(r"^/f(\d+)(?:p(\d+))?-([^/?#]*)")
_TOPIC_HREF_RE = re.compile(r"^/t(\d+)(?:p(\d+))?-([^/?#]*)")
_USER_HREF_RE = re.compile(r"^/u(\d+)")


def forum_id_from_href(href: str | None) -> int | None:
    m = _FORUM_HREF_RE.match(href or "")
    return int(m.group(1)) if m else None


def topic_id_from_href(href: str | None) -> int | None:
    m = _TOPIC_HREF_RE.match(href or "")
    return int(m.group(1)) if m else None


def _pagination_offsets(soup: BeautifulSoup, href_re: re.Pattern, item_id: int) -> list[int]:
    offsets = set()
    for a in soup.find_all("a", href=True):
        m = href_re.match(a["href"])
        if m and int(m.group(1)) == item_id and m.group(2):
            offsets.add(int(m.group(2)))
    return sorted(offsets)


def pagination_info(soup: BeautifulSoup, kind: str, item_id: int, default_step: int):
    """Retorna (passo, maior_offset) a partir dos links de paginação.

    kind: "t" para tópico, "f" para fórum.
    """
    href_re = _TOPIC_HREF_RE if kind == "t" else _FORUM_HREF_RE
    offsets = _pagination_offsets(soup, href_re, item_id)
    if not offsets:
        return default_step, 0
    step = offsets[0]
    # O primeiro offset visível costuma ser a página 2; se a página atual
    # for a 2, o menor offset visível pode ser maior — usamos o MDC.
    from math import gcd
    for o in offsets[1:]:
        step = gcd(step, o)
    return step, offsets[-1]


# --------------------------------------------------------------------------
# Índice e páginas de fórum
# --------------------------------------------------------------------------
def parse_forum_links(html: str) -> list[dict]:
    """Encontra todos os fóruns/subfóruns linkados numa página."""
    soup = soup_of(html)
    found: dict[int, dict] = {}
    # Links com classe forumtitle são os mais confiáveis (têm o nome completo)
    for a in soup.select("a.forumtitle, a.forumlink, a[href^='/f']"):
        href = a.get("href", "")
        m = _FORUM_HREF_RE.match(href)
        if not m or m.group(2):  # ignora links de paginação
            continue
        fid = int(m.group(1))
        name = _clean(a.get_text(" "))
        if fid not in found or (name and len(name) > len(found[fid]["name"] or "")):
            found[fid] = {
                "id": fid,
                "slug": m.group(3),
                "name": name or None,
                "url": abs_url(f"/f{fid}-{m.group(3)}"),
            }
    return list(found.values())


_GROUP_HREF_RE = re.compile(r"^/g(\d+)-([^/?#]*)")


def parse_groups(html: str) -> list[dict]:
    """Legenda de grupos do índice (ex.: /g14-administradores)."""
    soup = soup_of(html)
    out: dict[int, dict] = {}
    for a in soup.find_all("a", href=_GROUP_HREF_RE):
        m = _GROUP_HREF_RE.match(a["href"])
        gid = int(m.group(1))
        color = re.search(r"color:\s*([^;]+)", a.get("style") or "")
        out[gid] = {
            "id": gid,
            "class": f"group-{gid}",
            "name": _clean(a.get_text(" ")) or None,
            "slug": m.group(2),
            "color": color.group(1).strip() if color else None,
        }
    return list(out.values())


def parse_breadcrumb_forums(soup: BeautifulSoup) -> list[dict]:
    """Fóruns no breadcrumb (a.nav), do mais externo ao mais interno."""
    seen, out = set(), []
    for a in soup.select("a.nav"):
        fid = forum_id_from_href(a.get("href"))
        if fid and fid not in seen:
            seen.add(fid)
            out.append({"id": fid, "name": _clean(a.get_text(" "))})
    return out


def parse_forum_page(html: str, forum_id: int) -> dict:
    """Lê uma página de listagem de tópicos de um fórum."""
    soup = soup_of(html)
    topics = []
    for a in soup.select("a.topictitle"):
        tid = topic_id_from_href(a.get("href"))
        if tid is None:
            continue
        row = a.find_parent("li") or a.find_parent("tr")
        topic = {
            "id": tid,
            "title": _clean(a.get_text(" ")),
            "url": abs_url(re.sub(r"#.*$", "", a["href"])),
            "forum_id": forum_id,
            "author_name": None,
            "replies": None,
            "views": None,
            "last_post_author": None,
            "last_post_date_raw": None,
            "last_post_date": None,
            "description": None,
            "section": None,
            "last_post_avatar": None,
            "last_post_id": None,
            "prefix": None,      # ex.: "[Votação]" (tópico com enquete)
            "topic_type": None,  # ícone da listagem
        }
        if row is not None:
            by = row.select_one(".span-tab strong, .span-tab a, .topic-author")
            if by:
                topic["author_name"] = _clean(by.get_text(" "))
            icon = row.select_one("dl.icon")
            m_icon = re.search(r"/([a-z_]+)\.(?:gif|png)", (icon.get("style") or "") if icon else "")
            if m_icon:
                # topic_read, topic_read_hot, topic_read_locked, sticky_read, announce_read...
                topic["topic_type"] = m_icon.group(1)
            dterm = row.select_one("dd.dterm")
            if dterm:
                prefix = dterm.find("strong", recursive=False)
                text = _clean(prefix.get_text(" ")) if prefix else ""
                # o <strong> também pode ser a paginação do tópico ("1 , 2 , 3")
                if text and not re.fullmatch(r"[\d\s,.]+", text):
                    topic["prefix"] = text
                # Descrição/subtítulo: texto solto entre o título e o "por"
                desc = [_clean(t) for t in dterm.find_all(string=True, recursive=False)]
                desc = [d for d in desc if d]
                topic["description"] = " ".join(desc) or None
            posts = row.select_one("dd.posts")
            if posts:
                topic["replies"] = _int(posts.get_text(" "))
            views = row.select_one("dd.views")
            if views:
                topic["views"] = _int(views.get_text(" "))
            last = row.select_one("dd.lastpost")
            if last:
                dfn = last.find("dfn")
                span = dfn.parent if dfn else last
                parts = [
                    _clean(t) for t in span.find_all(string=True, recursive=False)
                    if _clean(t)
                ]
                author = span.find("a", href=_USER_HREF_RE) or span.find("strong")
                if author:
                    topic["last_post_author"] = _clean(author.get_text(" "))
                elif len(parts) >= 2:
                    topic["last_post_author"] = parts[0]
                if parts:
                    topic["last_post_date_raw"] = parts[-1]
                    topic["last_post_date"] = parse_date(parts[-1])
                av = last.select_one(".lastpost-avatar img")
                if av and av.get("src") and "empty.gif" not in av["src"]:
                    topic["last_post_avatar"] = abs_url(av["src"])
                lp = last.find("a", href=_TOPIC_HREF_RE)
                if lp and "#" in lp["href"]:
                    topic["last_post_id"] = _int(lp["href"].split("#", 1)[1])
            # Seção da lista (ex.: "Tópicos", "Anúncios", "Fixos")
            header = row.find_previous("li", class_="header")
            if header and header.find("dt"):
                topic["section"] = _clean(header.find("dt").get_text(" "))
        topics.append(topic)

    step, last_offset = pagination_info(soup, "f", forum_id, default_step=30)
    return {
        "topics": topics,
        "subforums": [f for f in parse_forum_links(html) if f["id"] != forum_id],
        "breadcrumb": parse_breadcrumb_forums(soup),
        "page_step": step,
        "last_offset": last_offset,
        "title": _clean(soup.title.get_text()) if soup.title else None,
    }


# --------------------------------------------------------------------------
# Páginas de tópico
# --------------------------------------------------------------------------
def _parse_profile_fields(dd: Tag) -> tuple[dict, list]:
    """Extrai pares rótulo→valor do bloco de perfil ao lado da mensagem.

    O Forumeiros renderiza campos como:
        <span class="label"><span>Data de inscrição</span> :</span> 26/02/2008<br>
        <span class="label"><span><img alt="Medalhas"></span></span> x 0
    """
    fields: dict[str, str] = {}
    images: list[dict] = []
    for label in dd.select("span.label"):
        key = _clean(label.get_text(" ")).rstrip(":").strip()
        if not key:
            img = label.find("img")
            key = _clean(img.get("alt")) if img else ""
        if not key:
            continue
        value_parts = []
        for sib in label.next_siblings:
            if isinstance(sib, Tag):
                if sib.name in ("br", "hr") or "label" in (sib.get("class") or []):
                    break
                if sib.name == "img":
                    if sib.get("src") and "empty.gif" not in sib["src"]:
                        images.append({"field": key, "alt": sib.get("alt"),
                                       "title": sib.get("title"), "src": sib["src"]})
                    continue
                value_parts.append(sib.get_text(" "))
            elif isinstance(sib, NavigableString):
                value_parts.append(str(sib))
        value = _clean(" ".join(value_parts))
        value = re.sub(r"^x\s+", "", value)  # "x 0" -> "0"
        fields[key] = value
    return fields, images


def _parse_postprofile(profile: Tag) -> dict:
    info: dict = {
        "name": None, "user_id": None, "profile_url": None,
        "avatar_url": None, "group_class": None, "color": None,
        "rank_title": None, "rank_image": None,
        "joined_raw": None, "joined_date": None,
        "reputation": None, "post_count": None,
        "website": None, "extra_fields": {}, "profile_images": [],
    }
    dl = profile.find("dl") or profile
    dt = dl.find("dt")
    if dt:
        av = dt.find("img")
        if av and av.get("src") and "empty.gif" not in av["src"]:
            info["avatar_url"] = abs_url(av["src"])
        link = dt.find("a", href=_USER_HREF_RE)
        if link:
            info["user_id"] = int(_USER_HREF_RE.match(link["href"]).group(1))
            info["profile_url"] = abs_url(link["href"])
        name_span = dt.select_one("span.usr_grp_clr, span[class^=group-]")
        if name_span:
            info["name"] = _clean(name_span.get_text(" "))
            for c in name_span.get("class") or []:
                if re.match(r"group-\d+", c):
                    info["group_class"] = c
            m = re.search(r"color:\s*([^;]+)", name_span.get("style") or "")
            if m:
                info["color"] = m.group(1).strip()
        else:
            strong = dt.find("strong")
            if strong:
                info["name"] = _clean(strong.get_text(" "))
    dds = dl.find_all("dd", recursive=False)
    for dd in dds:
        classes = dd.get("class") or []
        if "dd_award" in classes or "award_more" in classes:
            for img in dd.find_all("img"):
                info["profile_images"].append({"field": "award", "alt": img.get("alt"),
                                               "title": img.get("title"),
                                               "src": img.get("src")})
            continue
        if dd.select("span.label"):
            fields, imgs = _parse_profile_fields(dd)
            info["extra_fields"].update(fields)
            info["profile_images"].extend(imgs)
        else:
            # Primeiro dd sem rótulos: título/imagem de rank, quando existir
            text = _clean(dd.get_text(" "))
            img = dd.find("img")
            if (text or img) and info["rank_title"] is None and info["rank_image"] is None \
                    and not dd.find("a", href=re.compile(r"^https?://")):
                info["rank_title"] = text or None
                if img and img.get("src"):
                    info["rank_image"] = abs_url(img["src"])
        for a in dd.find_all("a", href=True):
            title = (a.get("title") or "").lower()
            if "homepage" in title or "site" in title:
                info["website"] = a["href"]
    ef = info["extra_fields"]
    for k, v in ef.items():
        kl = k.lower()
        if "inscri" in kl:
            info["joined_raw"] = v
            info["joined_date"] = parse_date(v)
        elif "reputa" in kl:
            info["reputation"] = _int(v)
        elif "mensagens" in kl:
            info["post_count"] = _int(v)
    # Remove do extra_fields o que já virou coluna
    info["extra_fields"] = {
        k: v for k, v in ef.items()
        if not any(s in k.lower() for s in ("inscri", "reputa", "mensagens"))
    }
    return info


def _content_text(content: Tag) -> str:
    clone = BeautifulSoup(str(content), "lxml")
    for br in clone.find_all("br"):
        br.replace_with("\n")
    text = clone.get_text()
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _parse_likers(text: str) -> list[str]:
    """ "A, B e C gostam desta mensagem" -> ["A", "B", "C"]."""
    if not text:
        return []
    text = re.sub(r"\s+gostam?\s+des[st]a\s+mensagem.*$", "", text, flags=re.IGNORECASE)
    names = re.split(r",\s*|\s+e\s+", text)
    return [n.strip() for n in names if n.strip()]


def parse_topic_page(html: str, topic_id: int, now: datetime | None = None) -> dict:
    """Lê uma página de tópico e devolve mensagens + metadados do tópico."""
    soup = soup_of(html)
    title_el = soup.select_one("h1.page-title a, h1.page-title, h2.topic-title a")
    topic_title = _clean(title_el.get_text(" ")) if title_el else (
        _clean(soup.title.get_text()) if soup.title else None)

    posts = []
    for div in soup.select("div.post[id^=p]"):
        m = re.fullmatch(r"p(\d+)", div.get("id", ""))
        if not m or m.group(1) == "0":
            continue  # p0 = "Conteúdo patrocinado" (anúncio)
        post_id = int(m.group(1))
        post: dict = {"id": post_id, "topic_id": topic_id}

        title_a = div.select_one("h2.topic-title a, h3 a")
        post["title"] = _clean(title_a.get_text(" ")) if title_a else None

        author_p = div.select_one("p.author")
        author_name, author_id = None, None
        author_text = ""
        if author_p:
            a = author_p.find("a", href=_USER_HREF_RE)
            if a:
                author_id = int(_USER_HREF_RE.match(a["href"]).group(1))
                author_name = _clean(a.get_text(" "))
            else:
                s = author_p.find("strong")
                if s:
                    author_name = _clean(s.get_text(" "))
            author_text = re.sub(r"^(por|by)\s+", "", _clean(author_p.get_text(" ")),
                                 flags=re.IGNORECASE)

        profile_el = div.select_one(".postprofile")
        profile = _parse_postprofile(profile_el) if profile_el else {}
        if profile:
            author_name = author_name or profile.get("name")
            author_id = author_id or profile.get("user_id")
            if profile.get("name") is None:
                profile["name"] = author_name
            if author_id and not profile.get("user_id"):
                profile["user_id"] = author_id

        # O nome do autor nem sempre vem em tag própria ("por Fulano Ter 24 Jan
        # 2017, 15:35"), então removemos o nome conhecido para isolar a data.
        date_raw = author_text or None
        if author_name and author_text.startswith(author_name):
            date_raw = author_text[len(author_name):].strip() or None
        elif author_text:
            dm = _DATE_RE.search(author_text) or _REL_RE.search(author_text)
            if dm:
                date_raw = author_text[dm.start():].strip()
                if not author_name:
                    author_name = author_text[:dm.start()].strip() or None

        content = div.select_one("div.content")
        post["content_html"] = content.decode_contents().strip() if content else None
        post["content_text"] = _content_text(content) if content else None

        # "Última edição por X em <data>, editado N vez(es)"
        edit_info = None
        postbody = div.select_one(".postbody")
        if postbody:
            for t in postbody.find_all(string=re.compile(r"Última edição|Last edited")):
                edit_info = _clean(t)
                break
        post["edit_info"] = edit_info
        post["edit_count"] = None
        post["last_edit_date"] = None
        if edit_info:
            mm = re.search(r"editado\s+(\d+)\s+vez", edit_info)
            if mm:
                post["edit_count"] = int(mm.group(1))
            post["last_edit_date"] = parse_date(edit_info, now)

        sig = div.select_one(".signature_div")
        post["signature_html"] = sig.decode_contents().strip() if sig else None

        likes = div.select_one(".fa_like_list")
        likes_text = _clean(likes.get_text(" ")) if likes else ""
        post["likes_text"] = likes_text or None
        post["liked_by"] = _parse_likers(likes_text)
        vote = div.select_one(".vote-bar")
        post["vote_title"] = vote.get("title") if vote else None
        rep_counts = [_int(s.get_text()) for s in div.select(".rep-nb")]
        post["likes"] = rep_counts[0] if rep_counts else None
        post["dislikes"] = rep_counts[1] if len(rep_counts) > 1 else None

        post["author_name"] = author_name
        post["author_id"] = author_id
        post["date_raw"] = date_raw
        post["date"] = parse_date(date_raw, now)
        post["profile"] = profile
        posts.append(post)

    step, last_offset = pagination_info(soup, "t", topic_id, default_step=50)
    crumbs = parse_breadcrumb_forums(soup)
    return {
        "title": topic_title,
        "poll": parse_poll(soup),
        "posts": posts,
        "page_step": step,
        "last_offset": last_offset,
        "forum_id": crumbs[-1]["id"] if crumbs else None,
        "breadcrumb": crumbs,
    }


def parse_poll(soup: BeautifulSoup) -> dict | None:
    """Enquete do tópico (só aparece na 1ª página)."""
    fs = soup.select_one("fieldset.polls")
    if fs is None:
        return None
    box = fs.find_parent("div", class_="content") or fs.parent
    q = box.find("h2") if box else None
    options, total, closed = [], None, False
    for dl in fs.find_all(["dl", "d1"]):
        label = dl.find("label")
        dds = dl.find_all("dd")
        text = _clean(dl.get_text(" "))
        if label:
            pct = votes = None
            for dd in dds:
                t = _clean(dd.get_text(" "))
                m = re.search(r"\[\s*(\d+)\s*\]", t)
                if m:
                    votes = int(m.group(1))
                m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", t)
                if m and pct is None:
                    pct = float(m.group(1).replace(",", "."))
            radio = dl.find("input")
            options.append({"text": _clean(label.get_text(" ")), "votes": votes,
                            "percent": pct,
                            "option_value": radio.get("value") if radio else None})
        elif "Total de votos" in text:
            total = _int(text.split(":")[-1])
        elif "encerrad" in text.lower():
            closed = True
    return {"question": _clean(q.get_text(" ")) if q else None, "options": options,
            "total_votes": total, "closed": closed}


# --------------------------------------------------------------------------
# Lista de membros e perfis
# --------------------------------------------------------------------------
def parse_memberlist_page(html: str) -> dict:
    """Uma página de /memberlist (30 membros por página)."""
    soup = soup_of(html)
    members = []
    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue
        a = tds[1].find("a", href=_USER_HREF_RE)
        if not a:
            continue
        img = a.find("img")
        www = tds[7].find("a") if len(tds) > 7 else None
        joined = _clean(tds[3].get_text(" ")) or None
        last = _clean(tds[4].get_text(" ")) or None
        members.append({
            "user_id": int(_USER_HREF_RE.match(a["href"]).group(1)),
            "name": _clean(a.get_text(" ")),
            "profile_url": abs_url(a["href"]),
            "avatar_url": abs_url(img["src"]) if img and img.get("src") else None,
            "humor": _clean(tds[2].get_text(" ")) or None,
            "joined_raw": joined, "joined_date": parse_date(joined),
            "last_visit_raw": last, "last_visit": parse_date(last),
            "post_count": _int(tds[5].get_text(" ")),
            "website": www.get("href") if www else None,
        })
    offsets = [int(x) for x in re.findall(r"/memberlist\?[^\"']*?start=(\d+)", html)]
    return {"members": members, "last_offset": max(offsets, default=0)}


# Rótulos do perfil que não guardamos (ações/contato, não dados do membro)
_PROFILE_SKIP = ("Mensagem privada", "Mensagens privadas", "Contactar por e-mail",
                 "Amigo(a)s e ignorados", "Gerenciar membro", "Encontrar todas")


def parse_profile_page(html: str, user_id: int) -> dict:
    """Página /uNN: todos os campos visíveis do perfil."""
    soup = soup_of(html)
    main = soup.select_one("#main-content") or soup
    out: dict = {"user_id": user_id, "fields": {}, "groups": [], "images": []}
    h1 = main.select_one("h1.page-title")
    out["name"] = None
    out["color"] = None
    if h1:
        out["name"] = _clean(h1.get_text(" ")).removeprefix("Perfil de").strip() or None
        span = h1.find("span", style=True)
        m = re.search(r"color:\s*(#[0-9a-fA-F]{3,6})", span["style"]) if span else None
        out["color"] = m.group(1) if m else None
    for dl in main.select("dl"):
        for dt in dl.find_all("dt", recursive=False):
            label = _clean(dt.get_text(" ")).rstrip(":* ").strip()
            dd = dt.find_next_sibling("dd")
            if not label or dd is None or label.startswith(_PROFILE_SKIP):
                continue
            if dl.get("id") == "groups-details":
                out["groups"] = [{"id": int(m.group(1)), "name": _clean(a.get_text(" "))}
                                 for a in dd.select("a[href^='/g']")
                                 if (m := re.match(r"/g(\d+)", a["href"]))]
                continue
            uneditable = dd.find("div", class_="field_uneditable")
            val_el = uneditable or dd
            text = _clean(val_el.get_text(" ")).lstrip("-").strip()
            # texto fora do bloco não editável (ex.: "[0.16% do total ...]")
            extra = _clean(" ".join(dd.find_all(string=True, recursive=False))) \
                if uneditable else ""
            links = [a.get("href") for a in val_el.find_all("a", href=True)
                     if not a["href"].startswith(("/profile", "/privmsg", "/admin"))]
            imgs = [abs_url(i.get("src")) for i in val_el.find_all("img")
                    if i.get("src") and "icon_contact" not in i["src"]
                    and "empty.gif" not in i["src"]]
            if label == "Avatar":
                out["avatar_url"] = imgs[0] if imgs else None
                continue
            value: dict | str | None = text or None
            if links or imgs or extra:
                value = {"text": text or None}
                if links:
                    value["links"] = links
                if imgs:
                    value["images"] = imgs
                if extra:
                    value["extra"] = extra
            out["fields"][label] = value
    out.setdefault("avatar_url", None)
    f = out["fields"]
    out["last_visit_raw"] = f.get("Última visita") if isinstance(f.get("Última visita"), str) else None
    out["last_visit"] = parse_date(out["last_visit_raw"])
    out["rank_title"] = f.get("Rank") if isinstance(f.get("Rank"), str) else None
    out["status"] = f.get("Estado") if isinstance(f.get("Estado"), str) else None
    return out


def is_login_page(html: str) -> bool:
    soup = soup_of(html)
    return bool(soup.select_one("form[action*='/login'] input[name=password]")) \
        and not soup.select("div.post[id^=p]") and not soup.select("a.topictitle")
