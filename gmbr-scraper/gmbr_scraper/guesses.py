"""Sugestões de autor para mensagens exibidas como "Convidado"/"Convidad".

O fórum mostra assim as mensagens de contas apagadas: sem nome, avatar,
assinatura nem link de perfil. As sugestões NÃO alteram posts.author_id;
ficam em post_author_guesses, uma linha por (mensagem, nome, método), e a view
v_post_author_best escolhe o nome mais provável de cada mensagem.

Métodos (score = confiança aproximada):

* citacao     (0.9, alta)  — uma mensagem posterior do mesmo tópico cita o
                             texto desta como "X escreveu:".
* edicao      (0.7, média) — "Última edição por X", quando X não costuma
                             editar mensagens alheias (moderadores ficam fora).
* mencao      (0.4, baixa) — a resposta seguinte, de autor conhecido, chama
                             alguém pelo nome nos primeiros 200 caracteres
                             (ou com @), e esse nome não é de ninguém que
                             escreveu ali perto.
* continuidade(0.3, baixa) — outra mensagem de convidado do mesmo tópico foi
                             atribuída (por citação/edição) a um único nome.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import Counter, defaultdict

from bs4 import BeautifulSoup

log = logging.getLogger("gmbr.guesses")

SCORES = {"citacao": 0.9, "edicao": 0.7, "mencao": 0.4, "continuidade": 0.3}
CONFIDENCE = {"citacao": "alta", "edicao": "média", "mencao": "baixa",
              "continuidade": "baixa"}
GUEST_FILTER = "author_id IS NULL AND author_name LIKE 'Convidad%'"

_WORD = re.compile(r"\w+", re.UNICODE)
_EDIT = re.compile(r"Última edição por (.+?) em ")
_CITE = re.compile(r"(.+?)\s+escreveu:\s*$", re.S)


def _norm(s: str | None) -> str:
    return " ".join(_WORD.findall((s or "").lower()))


def _quotes(html: str) -> list[tuple[str, str]]:
    """[(nome citado, texto citado normalizado)] de uma mensagem."""
    out = []
    soup = BeautifulSoup(html, "lxml")
    for bq in soup.find_all("blockquote"):
        cite = bq.find("cite")
        if not cite:
            continue
        m = _CITE.match(cite.get_text(" ").strip())
        if not m:
            continue
        name = m.group(1).strip()
        # texto próprio do bloco, sem a citação aninhada nem o cabeçalho
        clone = BeautifulSoup(str(bq), "lxml").find("blockquote")
        clone.find("cite").extract()
        for inner in clone.find_all("blockquote"):
            inner.extract()
        out.append((name, _norm(clone.get_text(" "))))
    return out


def _same_text(quoted: str, original: str) -> bool:
    if len(quoted) < 15 or len(original) < 15:
        return False
    return quoted[:80] in original or original[:80] in quoted


def build_guesses(conn: sqlite3.Connection) -> dict:
    conn.execute("DELETE FROM post_author_guesses")
    members = conn.execute("SELECT user_id, name, joined_date FROM members").fetchall()
    by_name: dict[str, list] = defaultdict(list)
    for uid, name, joined in members:
        by_name[name].append((uid, joined))

    def user_for(name: str, date: str | None) -> int | None:
        cands = [u for u, j in by_name.get(name, [])
                 if not date or not j or j <= date[:10]]
        return cands[0] if len(cands) == 1 else None

    # Quem edita mensagens de outros autores (moderação) não serve de pista.
    editors = Counter()
    for author, info in conn.execute(
            "SELECT author_name, edit_info FROM posts "
            "WHERE author_id IS NOT NULL AND edit_info LIKE 'Última edição por %'"):
        m = _EDIT.match(info)
        if m and m.group(1) != author:
            editors[m.group(1)] += 1
    moderators = {n for n, k in editors.items() if k >= 3}

    # Nomes que viram palavra comum no texto do fórum não servem para menção.
    word_freq: Counter = Counter()
    total_posts = 0
    for (text,) in conn.execute("SELECT content_text FROM posts"):
        total_posts += 1
        word_freq.update(set(_norm(text).split()))

    topics = [r[0] for r in conn.execute(
        f"SELECT DISTINCT topic_id FROM posts WHERE {GUEST_FILTER}")]
    rows: list[tuple] = []
    quote_names: set[str] = set()
    per_topic: dict[int, list] = {}
    for tid in topics:
        posts = conn.execute(
            "SELECT id, position, author_id, author_name, content_text, content_html, "
            "edit_info, date FROM posts WHERE topic_id=? ORDER BY position", (tid,)
        ).fetchall()
        per_topic[tid] = posts
        guests = [p for p in posts
                  if p[2] is None and (p[3] or "").startswith("Convidad")]
        normed = {p[0]: _norm(p[4]) for p in guests}
        for p in posts:
            if not p[5] or "escreveu:" not in p[5]:
                continue
            for name, qtext in _quotes(p[5]):
                quote_names.add(name)
                if name.startswith("Convidad"):
                    continue
                for g in guests:
                    if g[1] < p[1] and _same_text(qtext, normed[g[0]]):
                        rows.append((g[0], name, "citacao",
                                     {"post_que_cita": p[0]}, g[7]))
        for g in guests:
            m = _EDIT.match(g[6] or "")
            if m and m.group(1) not in moderators and not m.group(1).startswith("Convidad"):
                rows.append((g[0], m.group(1), "edicao", {"edit_info": g[6]}, g[7]))

    # Camada de contexto
    strong: dict[int, set] = defaultdict(set)
    for post_id, name, method, _, _ in rows:
        strong[post_id].add(name)
    known_names = {n for n in quote_names | set(by_name) | {r[1] for r in rows}
                   if len(n) >= 4 and not n.startswith("Convidad")
                   and word_freq.get(n.lower(), 0) < max(20, total_posts * 0.002)}
    name_re = {n: re.compile(r"(?<!\w)@?" + re.escape(n) + r"(?!\w)", re.I)
               for n in known_names}
    phrase_cache: dict[str, bool] = {}

    def common_phrase(name: str) -> bool:
        """Nome de várias palavras que aparece em muitas mensagens ("Game Maker",
        "criador de jogos") é termo do assunto, não pessoa."""
        if " " not in name.strip():
            return False
        if name not in phrase_cache:
            n = conn.execute("SELECT COUNT(*) FROM posts WHERE content_text LIKE ?",
                             (f"%{name}%",)).fetchone()[0]
            phrase_cache[name] = n >= max(20, total_posts * 0.002)
        return phrase_cache[name]

    by_first_word: dict[str, list[str]] = defaultdict(list)
    for n in known_names:
        parts = _norm(n).split()
        if parts:
            by_first_word[parts[0]].append(n)
    for tid, posts in per_topic.items():
        by_pos = {p[1]: p for p in posts}
        guests = [p for p in posts
                  if p[2] is None and (p[3] or "").startswith("Convidad")]
        topic_strong = set()
        for g in guests:
            topic_strong |= strong.get(g[0], set())
        for g in guests:
            nxt = by_pos.get(g[1] + 1)
            if nxt and nxt[2] is not None and nxt[4]:
                head = nxt[4][:200]
                nearby = {p[3] for k in range(g[1] - 3, g[1] + 2)
                          if (p := by_pos.get(k)) and p[2] is not None}
                hits = sorted({n for n in _candidates(head, by_first_word)
                               if name_re[n].search(head) and n not in nearby
                               and not common_phrase(n)})
                # "Ana" e "Ana Maria" no mesmo trecho: fica o nome mais longo
                hits = [n for n in hits
                        if not any(n != o and _norm(n) in _norm(o) for o in hits)]
                if len(hits) == 1:
                    rows.append((g[0], hits[0], "mencao",
                                 {"post_seguinte": nxt[0], "trecho": head[:120]}, g[7]))
            if not strong.get(g[0]) and len(topic_strong) == 1:
                rows.append((g[0], next(iter(topic_strong)), "continuidade",
                             {"topico": tid}, g[7]))

    seen = set()
    out = []
    for post_id, name, method, evidence, date in rows:
        key = (post_id, name, method)
        if key in seen:
            continue
        seen.add(key)
        out.append((post_id, name, user_for(name, date), method, CONFIDENCE[method],
                    SCORES[method], json.dumps(evidence, ensure_ascii=False)))
    conn.executemany(
        "INSERT INTO post_author_guesses (post_id, name, user_id, method, confidence, "
        "score, evidence) VALUES (?,?,?,?,?,?,?)", out)
    conn.commit()
    stats = Counter(r[3] for r in out)
    stats["mensagens"] = len({r[0] for r in out})
    stats["convidados"] = conn.execute(
        f"SELECT COUNT(*) FROM posts WHERE {GUEST_FILTER}").fetchone()[0]
    log.info("Sugestões: %s", dict(stats))
    return dict(stats)


def _candidates(text: str, by_first_word: dict[str, list[str]]):
    """Filtro rápido: só nomes cuja 1ª palavra aparece no trecho."""
    for w in set(_norm(text).split()):
        yield from by_first_word.get(w, ())
