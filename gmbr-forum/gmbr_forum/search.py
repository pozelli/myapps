"""Busca avançada: mensagens, tópicos e usuários, com filtros, facetas e
correlações (quem participa com quem, quem cita/curte quem)."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta
from math import ceil
from urllib.parse import quote, urlencode

from .app import POSTS_PER_PAGE, e, fmt_date, layout, member_link, prefix_html, topic_href

PER_PAGE = 25
MAX_NAMES = 20

# ------------------------------------------------------------------ parâmetros


class Params:
    """Leitura tolerante da query string (valores inválidos são ignorados)."""

    def __init__(self, qs: dict[str, list[str]]):
        self.qs = {k: [x.strip() for x in v if x.strip()] for k, v in qs.items()}

    def str(self, k: str, default: str = "", maxlen: int = 300) -> str:
        v = self.qs.get(k)
        return v[0][:maxlen] if v else default

    def choice(self, k: str, options, default: str) -> str:
        v = self.str(k)
        return v if v in options else default

    def int(self, k: str, lo: int | None = None, hi: int | None = None) -> int | None:
        v = self.str(k)
        if not re.fullmatch(r"-?\d{1,12}", v):
            return None
        n = int(v)
        if lo is not None:
            n = max(lo, n)
        if hi is not None:
            n = min(hi, n)
        return n

    def date(self, k: str) -> str | None:
        v = self.str(k)
        if re.fullmatch(r"\d{4}", v):
            v += "-01-01" if k.endswith("de") else "-12-31"
        try:
            return date.fromisoformat(v).isoformat()
        except ValueError:
            return None

    def flag(self, k: str) -> bool:
        return self.str(k) in ("1", "on", "sim", "true")

    def names(self, k: str) -> list[str]:
        out = []
        for v in self.qs.get(k, []):
            out += [n.strip().lower() for n in re.split(r"[,;\n]", v) if n.strip()]
        return list(dict.fromkeys(out))[:MAX_NAMES]

    def page(self) -> int:
        return self.int("p", 1, 10_000) or 1

    def url(self, path: str, **override) -> str:
        """URL desta busca com parâmetros trocados (None remove)."""
        items = [(k, v) for k, vs in self.qs.items() if k not in override and k != "p" for v in vs]
        for k, v in override.items():
            if v is not None and v != "":
                items.append((k, str(v)))
        return path + ("?" + urlencode(items) if items else "")

    def any_set(self, keys) -> bool:
        return any(self.qs.get(k) for k in keys)


def next_day(iso: str) -> str:
    return (date.fromisoformat(iso) + timedelta(days=1)).isoformat()


# ---------------------------------------------------------------- texto livre
_TOKEN = re.compile(r'(-?)"([^"]*)"|(\S+)')


def fts_query(text: str, mode: str = "todas") -> tuple[str | None, str | None]:
    """Converte a busca do usuário numa expressão FTS5 segura.
    Aceita: palavras (todas), "frase exata", -excluir, OR, prefixo*."""
    text = text.strip()
    if not text:
        return None, None

    def term(t: str, prefix: bool = False) -> str | None:
        t = t.strip()
        if not re.search(r"\w", t):
            return None
        return '"' + t.replace('"', '""') + '"' + ("*" if prefix else "")

    if mode == "frase":
        t = term(text.replace('"', " "))
        return (t, None) if t else (None, "Digite algum texto.")
    pos: list[list[str]] = []
    neg: list[str] = []
    join_or = False
    for m in _TOKEN.finditer(text):
        minus, phrase, word = m.group(1), m.group(2), m.group(3)
        if word == "OR":
            join_or = True
            continue
        if phrase is not None:
            t = term(phrase)
            is_neg = bool(minus)
        else:
            is_neg = word.startswith("-") and len(word) > 1
            w = word[1:] if is_neg else word
            prefix = w.endswith("*") and len(w) > 1
            t = term(w.rstrip("*") if prefix else w, prefix)
        if not t:
            continue
        if is_neg:
            neg.append(t)
        elif join_or and pos and mode != "qualquer":
            pos[-1].append(t)
        else:
            pos.append([t])
        join_or = False
    if not pos:
        return None, "A busca precisa de pelo menos um termo que não seja de exclusão (-palavra)."
    groups = ["(" + " OR ".join(g) + ")" if len(g) > 1 else g[0] for g in pos]
    expr = (" OR " if mode == "qualquer" else " AND ").join(groups)
    if len(groups) > 1:
        expr = f"({expr})"
    for n in neg:
        expr += f" NOT {n}"
    return expr, None


def highlight(snip: str | None) -> str:
    return e(snip or "").replace("\x02", "<mark>").replace("\x03", "</mark>")


def clip(text: str | None, n: int = 280) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return e(text[:n] + ("…" if len(text) > n else ""))


# ======================================================================= UI
CSS = """
<style>
.tabs{display:flex;gap:4px;margin:14px 0 0;flex-wrap:wrap}
.tabs a{padding:8px 16px;border:1px solid var(--line);border-bottom:0;border-radius:6px 6px 0 0;background:var(--alt)}
.tabs a.on{background:var(--head2);color:#fff;border-color:var(--head2)}
form.search{background:var(--card);border:1px solid var(--line);border-radius:0 6px 6px 6px;padding:14px;margin-bottom:16px}
form.search .main{display:flex;gap:8px;flex-wrap:wrap}form.search .main input[type=search]{flex:1;min-width:220px}
form.search details{margin-top:12px}form.search summary{cursor:pointer;color:var(--link);font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px;margin-top:10px}
fieldset{border:1px solid var(--line);border-radius:6px;padding:8px 10px 10px;margin:0;min-width:0}
legend{font-size:12px;text-transform:uppercase;color:var(--muted);padding:0 4px}
fieldset label{display:block;font-size:13px;color:var(--muted);margin-top:6px}
fieldset input:not([type=checkbox]):not([type=radio]),fieldset select{width:100%}
fieldset .pair{display:flex;gap:6px}fieldset .pair>*{flex:1;min-width:0}
fieldset label.ck{display:inline-flex;gap:4px;align-items:center;margin-right:10px;color:var(--ink)}
.hint{font-size:12px;color:var(--muted);margin-top:6px}
.results{display:grid;grid-template-columns:minmax(0,1fr) 250px;gap:16px;align-items:start}
.results>*{min-width:0}.hit p{overflow-wrap:anywhere}
.results.nofacets{grid-template-columns:1fr}
.facets .box h2{font-size:13px}.facets ul{list-style:none;margin:0;padding:6px 0}
.facets li{display:flex;justify-content:space-between;gap:8px;padding:2px 12px;font-size:13px}
.facets li span{color:var(--muted)}
.bars{padding:8px 12px}.bars div{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted)}
.bars i{display:block;height:9px;background:var(--head2);border-radius:2px;min-width:1px}
.bars a{width:34px;flex:none}
.rhead{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 8px}
.rhead form{display:flex;gap:6px;align-items:center}
.hit .tags{margin-left:4px}.hit mark{background:#ffe58a;color:inherit;padding:0 1px}
@media (prefers-color-scheme:dark){.hit mark{background:#6b5800}}
.err{background:#fdecea;color:#7a1c12;border:1px solid #f3b8b0;padding:10px 14px;border-radius:6px;margin:0 0 16px}
@media (prefers-color-scheme:dark){.err{background:#3d1612;color:#f7c2ba;border-color:#6d2a22}}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0 0}.chips a{font-size:12px;padding:2px 8px;border:1px solid var(--line);border-radius:12px;background:var(--card)}
table.list td.r{text-align:right;white-space:nowrap}
@media (max-width:800px){.results{grid-template-columns:minmax(0,1fr)}}
</style>
"""


def opt(value, label, current) -> str:
    sel = " selected" if str(value) == str(current) else ""
    return f'<option value="{e(value)}"{sel}>{e(label)}</option>'


def inp(name, P: Params, type_="text", placeholder="", **attrs) -> str:
    extra = "".join(f' {k.replace("_", "-")}="{e(v)}"' for k, v in attrs.items())
    return (f'<input type="{type_}" name="{name}" value="{e(P.str(name))}" '
            f'placeholder="{e(placeholder)}"{extra}>')


def ck(name, P: Params, label) -> str:
    c = " checked" if P.flag(name) else ""
    return f'<label class="ck"><input type="checkbox" name="{name}" value="1"{c}>{e(label)}</label>'


def tri(name, P: Params, label) -> str:
    v = P.choice(name, ("", "sim", "nao"), "")
    return (f'<label>{e(label)}<select name="{name}">{opt("", "tanto faz", v)}'
            f'{opt("sim", "sim", v)}{opt("nao", "não", v)}</select></label>')


def rng(prefix, P: Params, label, type_="number") -> str:
    return (f'<label>{e(label)}</label><div class="pair">{inp(prefix + "_min", P, type_, "mín.", min="0")}'
            f'{inp(prefix + "_max", P, type_, "máx.", min="0")}</div>')


def dates(prefix, P: Params, label) -> str:
    return (f'<label>{e(label)}</label><div class="pair">{inp(prefix + "_de", P, "date")}'
            f'{inp(prefix + "_ate", P, "date")}</div>')


def pager(P: Params, path: str, page: int, total: int) -> str:
    pages = max(1, ceil(total / PER_PAGE))
    if pages <= 1:
        return ""
    shown = sorted({1, pages, *range(max(1, page - 2), min(pages, page + 2) + 1)})
    out, prev = ["Página:"], 0
    for n in shown:
        if n - prev > 1:
            out.append("…")
        out.append(f"<b>{n}</b>" if n == page else f'<a href="{e(P.url(path, p=n))}">{n}</a>')
        prev = n
    return '<div class="pages">' + " ".join(out) + "</div>"


def facet_box(title: str, items: list[tuple[str, str, int]]) -> str:
    """items: (rótulo, url, contagem)."""
    if not items:
        return ""
    lis = "".join(f'<li><a href="{e(u)}">{e(lbl)}</a><span>{n}</span></li>' for lbl, u, n in items)
    return f'<div class="box"><h2>{e(title)}</h2><ul>{lis}</ul></div>'


def year_bars(title: str, rows: list[tuple[str, int]], url_for) -> str:
    rows = [(y, n) for y, n in rows if y]
    if not rows:
        return ""
    top = max(n for _, n in rows) or 1
    lines = "".join(
        f'<div><a href="{e(url_for(y))}">{e(y)}</a><i style="width:{max(1, 150 * n / top):.0f}px"></i>{n}</div>'
        for y, n in rows)
    return f'<div class="box"><h2>{e(title)}</h2><div class="bars">{lines}</div></div>'


LABELS = {
    "autor": "autor", "excluir": "sem autor", "forum": "fórum", "subforuns": None, "topico": "tópico",
    "data_de": "desde", "data_ate": "até", "tipo": "mensagem", "img": "imagem", "link": "link",
    "codigo": "código", "citacao": "citação", "spoiler": "spoiler", "video": "vídeo", "editada": "editada",
    "curtidas_min": "curtidas ≥", "tam_min": "tamanho ≥", "tam_max": "tamanho ≤", "cita": "cita",
    "curtida_por": "curtida por", "em_topicos_de": "em tópicos de", "com_participante": "com participação de",
    "origem": "origem", "campo": "em", "incluir_citado": "com texto citado", "agrupar": "agrupado por tópico",
    "ano": "ano", "onde": "em", "participantes": "participantes", "part_modo": None,
    "sem_participante": "sem", "msg_de": "respondido por", "prefixo": "prefixo", "enquete": "enquete",
    "trancado": "trancado", "fixo": "fixo", "resp_min": "respostas ≥", "resp_max": "respostas ≤",
    "vis_min": "visitas ≥", "vis_max": "visitas ≤", "part_min": "participantes ≥", "part_max": "participantes ≤",
    "votos_min": "votos ≥", "votos_max": "votos ≤", "criado_de": "criado desde", "criado_ate": "criado até",
    "ativ_de": "ativo desde", "ativ_ate": "ativo até", "perfil": "perfil", "nome_modo": None, "grupo": "grupo",
    "cargo": "cargo", "insc_de": "inscrito desde", "insc_ate": "inscrito até", "acesso_de": "acesso desde",
    "acesso_ate": "acesso até", "postou_de": "postou desde", "postou_ate": "postou até",
    "msgs_min": "mensagens ≥", "msgs_max": "mensagens ≤", "arq_min": "arquivadas ≥", "arq_max": "arquivadas ≤",
    "rep_min": "reputação ≥", "rep_max": "reputação ≤", "top_min": "tópicos ≥", "citado_min": "citado ≥",
    "curtido_min": "curtido ≥", "avatar": "avatar", "assinatura": "assinatura", "site": "site",
    "com_mensagens": "com mensagens", "rel_user": "relação com", "rel": None, "rel_min": "relação ≥",
    "juntar": "juntar nomes",
}
DEFAULTS = {"campo": "ambos", "tipo": "todas", "origem": ("todas", "todos"), "onde": "titulo",
            "part_modo": "todos", "subforuns": "1", "nome_modo": "contem", "rel": "conversou",
            "juntar": "pessoa"}


def chips(P: Params, path: str, forum_names: dict) -> str:
    out = []
    for k, vs in P.qs.items():
        label = LABELS.get(k, "")
        d = DEFAULTS.get(k)
        if not label or not vs or vs[0] in ((d,) if isinstance(d, str) else (d or ())):
            continue
        v = vs[0]
        if k == "forum" and v.isdigit():
            v = forum_names.get(int(v), v)
        elif v in ("1", "on") and k in ("agrupar", "incluir_citado", "com_mensagens"):
            v = ""
        elif v == "nao":
            v = "não"
        text = f"{label}: {v}" if v else label
        out.append(f'<a href="{e(P.url(path, **{k: None}))}" title="remover filtro">{e(text)} ×</a>')
    return f'<div class="chips">{"".join(out)}<a href="{path}">limpar tudo</a></div>' if len(out) > 0 else ""


# Juntar nomes: "pessoa" (contas da mesma pessoa), "conta" (nicks antigos da
# mesma conta) ou "nome" (cada nome por si).
JOIN_MODES = (("pessoa", "da mesma pessoa (várias contas)"), ("conta", "da mesma conta (nicks antigos)"),
              ("nome", "não juntar"))
SUFFIX = {"pessoa": "per", "conta": "acc", "nome": "lc"}
KEY_COL = {"pessoa": "per_key", "conta": "acc_key", "nome": "name_lc"}
TP_TABLE = {"pessoa": "tp_per", "conta": "tp_acc", "nome": "tp"}


def join_mode(P: "Params") -> str:
    return P.choice("juntar", [k for k, _ in JOIN_MODES], "pessoa")


def join_select(P: "Params") -> str:
    cur = join_mode(P)
    return (f'<label>Juntar nomes<select name="juntar">'
            + "".join(opt(k, lbl, cur) for k, lbl in JOIN_MODES) + "</select></label>")


def topic_tags(t) -> str:
    tags = ""
    if t["is_local"]:
        tags += '<span class="tag new">novo</span>'
    if t["has_poll"]:
        tags += '<span class="tag">enquete</span>'
    if t["locked"]:
        tags += '<span class="tag">trancado</span>'
    if t["sticky"]:
        tags += '<span class="tag">fixo</span>'
    return tags


# ===================================================================== páginas
class SearchPages:
    def __init__(self, forum):
        self.f = forum
        self._meta = None

    @property
    def db(self) -> sqlite3.Connection:
        return self.f.db

    def ready(self) -> bool:
        try:
            self.db.execute("SELECT 1 FROM s.meta LIMIT 1")
            return True
        except sqlite3.Error:
            return False

    def meta(self, key: str):
        if self._meta is None:
            self._meta = {k: v for k, v in self.db.execute("SELECT key, value FROM s.meta")}
        v = self._meta.get(key)
        try:
            return json.loads(v) if v and v[:1] in "[{" else v
        except ValueError:
            return v

    # ------------------------------------------------------------ fóruns
    def forum_tree(self):
        forums = self.f.forums()
        kids: dict = {}
        for fr in forums:
            kids.setdefault(fr["parent_id"], []).append(fr)
        out = []

        def walk(pid, depth):
            for fr in kids.get(pid, []):
                out.append((fr["id"], ("— " * depth) + fr["name"]))
                walk(fr["id"], depth + 1)
        walk(None, 0)
        return out, kids

    def forum_ids(self, P: Params) -> list[int] | None:
        fid = P.int("forum")
        if fid is None:
            return None
        ids = [fid]
        if P.str("subforuns", "1") != "0":
            _, kids = self.forum_tree()
            stack = [fid]
            while stack:
                for ch in kids.get(stack.pop(), []):
                    ids.append(ch["id"])
                    stack.append(ch["id"])
        return ids

    def forum_names(self) -> dict[int, str]:
        return {fr["id"]: fr["name"] for fr in self.f.forums()}

    def forum_select(self, P: Params) -> str:
        tree, _ = self.forum_tree()
        cur = P.str("forum")
        opts = opt("", "todos os fóruns", cur) + "".join(opt(i, n, cur) for i, n in tree)
        sub = P.str("subforuns", "1")
        return (f'<label>Fórum<select name="forum">{opts}</select></label>'
                f'<label>Subfóruns<select name="subforuns">{opt("1", "incluir subfóruns", sub)}'
                f'{opt("0", "só este fórum", sub)}</select></label>')

    # ------------------------------------------------------------- comum
    def shell(self, kind: str, P: Params, form: str, body: str, title: str) -> str:
        tabs = "".join(
            f'<a class="{"on" if k == kind else ""}" href="/busca/{k}'
            f'{("?q=" + quote(P.str("q"))) if P.str("q") and k != "usuarios" else ""}">{lbl}</a>'
            for k, lbl in (("mensagens", "Mensagens"), ("topicos", "Tópicos"), ("usuarios", "Usuários")))
        form = form.replace("</form>", chips(P, f"/busca/{kind}", self.forum_names()) + "</form>", 1)
        return layout(title, CSS + f'<div class="tabs">{tabs}</div>{form}{body}',
                      [("Índice", "/"), ("Busca", None)], P.str("q") if kind == "mensagens" else "")

    def page(self, kind: str, qs: dict) -> str:
        P = Params(qs)
        if not self.ready():
            body = ('<div class="err">O índice de busca ainda não foi construído. Reinicie o servidor '
                    '(ele constrói o índice ao iniciar) ou rode <code>python3 -m gmbr_forum --index-only</code>.</div>')
            return self.shell(kind, P, "", body, "Busca")
        return getattr(self, f"page_{kind}")(P)

    def sort_form(self, P: Params, path: str, options: list[tuple[str, str]], current: str) -> str:
        hidden = "".join(f'<input type="hidden" name="{e(k)}" value="{e(v)}">'
                         for k, vs in P.qs.items() if k not in ("ordem", "p") for v in vs)
        sel = "".join(opt(k, lbl, current) for k, lbl in options)
        return (f'<form action="{path}">{hidden}<label class="small">Ordenar por '
                f'<select name="ordem">{sel}</select></label><button>OK</button></form>')

    def run(self, sql: str, args: list):
        return self.db.execute(sql, args).fetchall()

    def key_of(self, name: str, mode: str) -> str:
        n = name.strip().lower()
        if mode == "nome":
            return n
        r = self.db.execute(f"SELECT {KEY_COL[mode]} FROM s.ident WHERE name_lc = ?", (n,)).fetchone()
        return r[0] if r and r[0] else n

    def expand(self, names: list[str], mode: str) -> list[str]:
        """Todos os nomes que pertencem à mesma conta/pessoa dos nomes dados."""
        if not names or mode == "nome":
            return names
        keys = list({self.key_of(n, mode) for n in names})
        q = ",".join("?" * len(keys))
        found = [r[0] for r in self.db.execute(
            f"SELECT name_lc FROM s.ident WHERE {KEY_COL[mode]} IN ({q})", keys)]
        return list(dict.fromkeys(names + keys + found))[:400]

    def same_names(self, key: str, mode: str) -> list[str]:
        if mode == "nome":
            return []
        return [r[0] for r in self.db.execute(
            f"SELECT name FROM s.ident WHERE {KEY_COL[mode]} = ? AND name_lc <> ? ORDER BY name COLLATE NOCASE",
            (key, key))]

    def key_labels(self, keys) -> dict[str, str]:
        keys = [k for k in keys if k]
        if not keys:
            return {}
        q = ",".join("?" * len(keys))
        return {r[0]: r[1] for r in self.db.execute(
            f"SELECT name_lc, name FROM s.ident WHERE name_lc IN ({q})", keys)}

    # ============================================================ MENSAGENS
    MSG_ORDERS = [("relevancia", "relevância"), ("recentes", "mais recentes"), ("antigas", "mais antigas"),
                  ("curtidas", "mais curtidas"), ("maiores", "mais longas"), ("menores", "mais curtas")]
    MSG_ADV = ("autor", "excluir", "forum", "topico", "data_de", "data_ate", "tipo", "img", "link",
               "codigo", "citacao", "spoiler", "video", "editada", "curtidas_min", "tam_min", "tam_max",
               "cita", "curtida_por", "em_topicos_de", "com_participante", "origem", "campo",
               "incluir_citado", "agrupar", "modo", "ano")

    def msg_where(self, P: Params, errors: list) -> tuple[str, list, str | None]:
        where, args = ["1=1"], []
        mode = join_mode(P)
        fts, err = fts_query(P.str("q"), P.choice("modo", ("todas", "qualquer", "frase"), "todas"))
        if err and P.str("q"):
            errors.append(err)
        if fts:
            campo = P.choice("campo", ("texto", "titulo", "ambos"), "ambos")
            cols = {"texto": ["body"], "titulo": ["title"], "ambos": ["title", "body"]}[campo]
            if P.flag("incluir_citado") and "body" in cols:
                cols.append("quoted")
            fts = "{" + " ".join(cols) + "} : (" + fts + ")"
        names = self.expand(P.names("autor"), mode)
        if names:
            where.append(f"p.author_lc IN ({','.join('?' * len(names))})")
            args += names
        ex = self.expand(P.names("excluir"), mode)
        if ex:
            where.append(f"COALESCE(p.author_lc, '') NOT IN ({','.join('?' * len(ex))})")
            args += ex
        fids = self.forum_ids(P)
        if fids:
            where.append(f"p.forum_id IN ({','.join('?' * len(fids))})")
            args += fids
        top = P.str("topico")
        if top:
            m = re.search(r"(?:^|/)([tn]?)(\d+)", top)
            if m:
                where.append("p.topic_ref = ?")
                args.append((m.group(1) or "t") + m.group(2))
        if (d := P.date("data_de")):
            where.append("p.date >= ?")
            args.append(d)
        if (d := P.date("data_ate")):
            where.append("p.date < ?")
            args.append(next_day(d))
        if (y := P.int("ano", 1990, 2100)):
            where.append("p.date >= ? AND p.date < ?")
            args += [f"{y}-01-01", f"{y + 1}-01-01"]
        tipo = P.choice("tipo", ("todas", "inicial", "respostas"), "todas")
        if tipo == "inicial":
            where.append("p.position = 0")
        elif tipo == "respostas":
            where.append("p.position > 0")
        for k, col in (("img", "has_img"), ("link", "has_link"), ("codigo", "has_code"),
                       ("citacao", "has_quote"), ("spoiler", "has_spoiler"), ("video", "has_video"),
                       ("editada", "edited")):
            v = P.choice(k, ("", "sim", "nao"), "")
            if v:
                where.append(f"p.{col} = {1 if v == 'sim' else 0}")
        if (n := P.int("curtidas_min", 0)):
            where.append("p.likes >= ?")
            args.append(n)
        if (n := P.int("tam_min", 0)) is not None:
            where.append("p.nchars >= ?")
            args.append(n)
        if (n := P.int("tam_max", 0)) is not None:
            where.append("p.nchars <= ?")
            args.append(n)
        sfx = SUFFIX[mode]
        if (q := list(dict.fromkeys(self.key_of(n, mode) for n in P.names("cita")))):
            where.append(f"p.id IN (SELECT post_id FROM s.quotes WHERE quoted_{sfx} IN ({','.join('?' * len(q))}))")
            args += q
        if (q := list(dict.fromkeys(self.key_of(n, mode) for n in P.names("curtida_por")))):
            where.append(f"p.id IN (SELECT post_id FROM s.likes WHERE liker_{sfx} IN ({','.join('?' * len(q))}))")
            args += q
        if (q := self.expand(P.names("em_topicos_de"), mode)):
            where.append(f"p.topic_ref IN (SELECT ref FROM s.tm WHERE author_lc IN ({','.join('?' * len(q))}))")
            args += q
        for name in P.names("com_participante"):
            where.append(f"p.topic_ref IN (SELECT topic_ref FROM s.{TP_TABLE[mode]} WHERE author_lc = ?)")
            args.append(self.key_of(name, mode))
        origem = P.choice("origem", ("todas", "arquivo", "novas"), "todas")
        if origem != "todas":
            where.append(f"p.is_local = {1 if origem == 'novas' else 0}")
        return " AND ".join(where), args, fts

    def page_mensagens(self, P: Params) -> str:
        path = "/busca/mensagens"
        errors: list[str] = []
        where, args, fts = self.msg_where(P, errors)
        has_filter = bool(fts) or P.any_set([k for k in self.MSG_ADV if k not in ("campo", "modo", "agrupar")])
        order = P.choice("ordem", [k for k, _ in self.MSG_ORDERS], "relevancia" if fts else "recentes")
        if order == "relevancia" and not fts:
            order = "recentes"
        group = P.flag("agrupar")
        page = P.page()
        body = ""
        if errors:
            body += "".join(f'<div class="err">{e(x)}</div>' for x in errors)
        if has_filter and not errors:
            frm = "s.pm p" + (" JOIN s.post_fts ON post_fts.rowid = p.id" if fts else "")
            fargs = ([fts] if fts else []) + args
            fwhere = ("post_fts MATCH ? AND " if fts else "") + where
            key = {"relevancia": "bm25(post_fts, 4.0, 1.0, 0.2)", "recentes": "p.date",
                   "antigas": "p.date", "curtidas": "p.likes", "maiores": "p.nchars",
                   "menores": "p.nchars"}[order]
            desc = order in ("recentes", "curtidas", "maiores")
            try:
                if group:
                    # bm25() não pode ir direto num agregado: calcula antes, agrupa depois.
                    # Com MAX()/MIN(), o SQLite devolve o id da linha vencedora de cada tópico.
                    cte = (f"WITH h AS MATERIALIZED (SELECT p.id, p.topic_ref, p.date AS d, {key} AS k "
                           f"FROM {frm} WHERE {fwhere}) ")
                    total = self.run(cte + "SELECT COUNT(DISTINCT topic_ref) FROM h", fargs)[0][0]
                    agg = "MAX" if desc else "MIN"
                    rows = self.run(
                        cte + f"""SELECT id, COUNT(*) AS hits, {agg}(k) AS kk FROM h GROUP BY topic_ref
                                  ORDER BY kk {'DESC' if desc else 'ASC'}, id DESC LIMIT ? OFFSET ?""",
                        fargs + [PER_PAGE, (page - 1) * PER_PAGE])
                    ids = [r["id"] for r in rows]
                    hits = {r["id"]: r["hits"] for r in rows}
                else:
                    total = self.run(f"SELECT COUNT(*) FROM {frm} WHERE {fwhere}", fargs)[0][0]
                    ids = [r[0] for r in self.run(
                        f"""SELECT p.id FROM {frm} WHERE {fwhere}
                            ORDER BY {key} {'DESC' if desc else 'ASC'}, p.id DESC LIMIT ? OFFSET ?""",
                        fargs + [PER_PAGE, (page - 1) * PER_PAGE])]
                    hits = {}
                facets = self.msg_facets(P, path, frm, fwhere, fargs)
            except sqlite3.OperationalError as exc:
                body += f'<div class="err">Não foi possível executar a busca: {e(exc)}</div>'
                return self.shell("mensagens", P, self.msg_form(P, path), body, "Busca de mensagens")
            body += self.msg_results(P, path, ids, hits, total, page, fts, order, group, facets)
        elif not errors:
            body += self.msg_help()
        return self.shell("mensagens", P, self.msg_form(P, path), body,
                          f"Busca: {P.str('q')}" if P.str("q") else "Busca de mensagens")

    def msg_facets(self, P, path, frm, fwhere, fargs) -> str:
        names = self.forum_names()
        forums = self.run(f"""SELECT p.forum_id, COUNT(*) n FROM {frm} WHERE {fwhere}
                              GROUP BY p.forum_id ORDER BY n DESC LIMIT 12""", fargs)
        col = f"p.author_{SUFFIX[join_mode(P)]}"
        authors = self.run(f"""SELECT {col} k, MAX(p.author) a, COUNT(*) n FROM {frm} WHERE {fwhere}
                               AND {col} IS NOT NULL GROUP BY {col} ORDER BY n DESC LIMIT 12""", fargs)
        labels = self.key_labels(r["k"] for r in authors)
        authors = [(labels.get(r["k"], r["a"]), r["n"]) for r in authors]
        years = self.run(f"""SELECT substr(p.date, 1, 4) y, COUNT(*) n FROM {frm} WHERE {fwhere}
                             GROUP BY y ORDER BY y""", fargs)
        return (facet_box("Fóruns", [(names.get(r[0], f"Fórum {r[0]}"), P.url(path, forum=r[0], subforuns=0), r[1])
                                     for r in forums if r[0] is not None])
                + facet_box("Autores", [(r[0], P.url(path, autor=r[0]), r[1]) for r in authors])
                + year_bars("Por ano", [(r[0], r[1]) for r in years],
                            lambda y: P.url(path, ano=y, data_de=None, data_ate=None)))

    def msg_results(self, P, path, ids, hits, total, page, fts, order, group, facets) -> str:
        details = {}
        if ids:
            q = ",".join("?" * len(ids))
            snip = ("snippet(post_fts, -1, char(2), char(3), '…', 32)" if fts else "NULL")
            frm = "s.post_fts JOIN s.pm p ON p.id = post_fts.rowid" if fts else "s.pm p"
            cond = "post_fts MATCH ? AND " if fts else ""
            fa = [self.msg_where(P, [])[2]] if fts else []
            for r in self.run(
                    f"""SELECT p.*, {snip} AS snip, t.body AS body, tm.title AS ttitle, tm.prefix,
                               tm.has_poll, tm.locked, tm.sticky, tm.is_local AS tlocal
                        FROM {frm} JOIN s.post_text t ON t.id = p.id
                        LEFT JOIN s.tm tm ON tm.ref = p.topic_ref
                        WHERE {cond} p.id IN ({q})""", fa + ids):
                details[r["id"]] = r
        names = self.forum_names()
        members = self.f.members_by_name(r["author"] for r in details.values())
        out = []
        for i in ids:
            r = details.get(i)
            if not r:
                continue
            pos = r["position"] or 0
            pg = pos // POSTS_PER_PAGE + 1
            anchor = f"p{r['id']}" if r["id"] > 0 else f"n{-r['id']}"
            href = f"{topic_href(r['topic_ref'])}?p={pg}#{anchor}"
            text = highlight(r["snip"]) if r["snip"] else clip(r["body"])
            badges = ""
            for col, lbl in (("has_img", "imagem"), ("has_code", "código"), ("has_video", "vídeo"),
                             ("has_quote", "citação"), ("edited", "editada")):
                if r[col]:
                    badges += f'<span class="tag">{lbl}</span>'
            if r["likes"]:
                badges += f'<span class="tag">👍 {r["likes"]}</span>'
            if r["is_local"]:
                badges += '<span class="tag new">nova</span>'
            more = ""
            if group and hits.get(i, 1) > 1:
                more = (f' · <a href="{e(P.url(path, agrupar=None, topico=r["topic_ref"]))}">'
                        f'{hits[i]} mensagens neste tópico</a>')
            kind = "mensagem inicial" if pos == 0 else f"resposta #{pos}"
            out.append(f"""<div class="hit"><a class="title" href="{e(href)}">{prefix_html(r['prefix'], r['ttitle'])}{e(r['ttitle'] or 'Tópico')}</a><span class="tags">{badges}</span>
<div class="small">{member_link(r['author'], members.get(r['author']))} · {e(fmt_date(r['date']))} · {kind} ·
<a href="/f/{r['forum_id']}">{e(names.get(r['forum_id'], ''))}</a>{more}</div><p>{text}</p></div>""")
        head = (f'<div class="rhead"><b>{total} {"tópicos" if group else "mensagens"} encontrad{"os" if group else "as"}</b>'
                f'{self.sort_form(P, path, self.MSG_ORDERS if fts else self.MSG_ORDERS[1:], order)}</div>')
        pg = pager(P, path, page, total)
        lst = "".join(out) or '<div class="hit">Nada encontrado. Tente menos filtros ou o modo "qualquer palavra".</div>'
        return (f'<div class="results"><div>{head}{pg}<div class="box">{lst}</div>{pg}</div>'
                f'<aside class="facets">{facets}</aside></div>')

    def msg_form(self, P: Params, path: str) -> str:
        modo = P.choice("modo", ("todas", "qualquer", "frase"), "todas")
        campo = P.choice("campo", ("texto", "titulo", "ambos"), "ambos")
        tipo = P.choice("tipo", ("todas", "inicial", "respostas"), "todas")
        origem = P.choice("origem", ("todas", "arquivo", "novas"), "todas")
        adv_open = ""
        return f"""<form class="search" action="{path}">
<div class="main"><input type="search" name="q" value="{e(P.str('q'))}" placeholder='Ex.: colisão "game maker" -rpg sprite*' aria-label="Texto">
<select name="modo" aria-label="Modo">{opt('todas', 'todas as palavras', modo)}{opt('qualquer', 'qualquer palavra', modo)}{opt('frase', 'frase exata', modo)}</select>
<button>Buscar</button></div>
<div class="hint">Acentos e maiúsculas não importam. Use "aspas" para frases, -palavra para excluir, OR entre alternativas e prefixo* para começo de palavra.</div>
<details{adv_open}><summary>Filtros avançados</summary><div class="grid">
<fieldset><legend>Texto</legend>
<label>Procurar em<select name="campo">{opt('ambos', 'título e texto', campo)}{opt('texto', 'só no texto', campo)}{opt('titulo', 'só no título', campo)}</select></label>
<label class="ck" style="margin-top:8px"><input type="checkbox" name="incluir_citado" value="1"{' checked' if P.flag('incluir_citado') else ''}>incluir texto citado de outros</label>
{rng('tam', P, 'Tamanho do texto (caracteres)')}</fieldset>
<fieldset><legend>Autores</legend>
<label>Escrita por (vírgula separa vários)</label>{inp('autor', P, placeholder='ex.: saim, PedroX')}
<label>Excluir autores</label>{inp('excluir', P)}</fieldset>
<fieldset><legend>Onde</legend>{self.forum_select(P)}
<label>Tópico (nº ou link)</label>{inp('topico', P, placeholder='ex.: 9026')}
<label>Mensagem<select name="tipo">{opt('todas', 'qualquer', tipo)}{opt('inicial', 'só a inicial do tópico', tipo)}{opt('respostas', 'só respostas', tipo)}</select></label></fieldset>
<fieldset><legend>Quando</legend>{dates('data', P, 'Data (de / até)')}
<label>Origem<select name="origem">{opt('todas', 'arquivo e mensagens novas', origem)}{opt('arquivo', 'só arquivo do gmbr', origem)}{opt('novas', 'só mensagens novas', origem)}</select></label></fieldset>
<fieldset><legend>Conteúdo</legend><div class="pair">{tri('img', P, 'Imagem')}{tri('link', P, 'Link')}</div>
<div class="pair">{tri('codigo', P, 'Código')}{tri('citacao', P, 'Citação')}</div>
<div class="pair">{tri('video', P, 'Vídeo')}{tri('spoiler', P, 'Spoiler')}</div>
<div class="pair">{tri('editada', P, 'Editada')}<label>Curtidas (mín.){inp('curtidas_min', P, 'number', min='0')}</label></div></fieldset>
<fieldset><legend>Correlações</legend>
<label>Cita o usuário</label>{inp('cita', P, placeholder='mensagens que citam…')}
<label>Curtida por</label>{inp('curtida_por', P)}
<label>Em tópicos criados por</label>{inp('em_topicos_de', P)}
<label>Em tópicos onde também postou</label>{inp('com_participante', P, placeholder='todos os nomes devem ter postado')}
{join_select(P)}</fieldset>
</div>
<div style="margin-top:10px"><label class="ck"><input type="checkbox" name="agrupar" value="1"{' checked' if P.flag('agrupar') else ''}>agrupar por tópico (1 resultado por tópico)</label>
<button>Buscar</button> <a href="{path}" class="small">limpar</a></div></details></form>"""

    def msg_help(self) -> str:
        return """<div class="box"><h2>Exemplos</h2><div class="hit">
<p><a href="/busca/mensagens?q=colis%C3%A3o+plataforma&amp;codigo=sim">colisão plataforma, só mensagens com código</a></p>
<p><a href="/busca/mensagens?q=%22game+maker+studio%22+-gms2&amp;ordem=antigas">"game maker studio" sem gms2, das mais antigas</a></p>
<p><a href="/busca/mensagens?cita=saim&amp;autor=vinians">mensagens de vinians que citam saim</a></p>
<p><a href="/busca/mensagens?autor=PedroX&amp;com_participante=saim&amp;agrupar=1">tópicos em que PedroX e saim conversaram</a></p>
<p><a href="/busca/mensagens?curtidas_min=2&amp;ordem=curtidas">mensagens mais curtidas</a></p>
</div></div>"""

    # ============================================================== TÓPICOS
    TOPIC_ORDERS = [("relevancia", "relevância"), ("atividade", "atividade recente"),
                    ("criacao", "criação (recentes)"), ("criacao_asc", "criação (antigos)"),
                    ("respostas", "mais respostas"), ("visitas", "mais visitas"),
                    ("participantes", "mais participantes"), ("votos", "mais votos na enquete"),
                    ("titulo", "título (A–Z)")]
    TOPIC_ADV = ("onde", "autor", "participantes", "part_modo", "sem_participante", "forum", "prefixo",
                 "enquete", "trancado", "fixo", "origem", "resp_min", "resp_max", "vis_min", "vis_max",
                 "part_min", "part_max", "votos_min", "criado_de", "criado_ate", "ativ_de", "ativ_ate",
                 "ano", "msg_de")

    def page_topicos(self, P: Params) -> str:
        path = "/busca/topicos"
        errors: list[str] = []
        where, args = ["1=1"], []
        joins = ""
        cte = ""
        rank = None
        jm = join_mode(P)
        mode = P.choice("modo", ("todas", "qualquer", "frase"), "todas")
        fts, err = fts_query(P.str("q"), mode)
        if err and P.str("q"):
            errors.append(err)
        onde = P.choice("onde", ("titulo", "inicial", "qualquer"), "titulo")
        if fts:
            if onde == "titulo":
                joins += " JOIN s.topic_fts ON topic_fts.rowid = t.rid"
                where.append("topic_fts MATCH ?")
                args.append(fts)
                rank = "bm25(topic_fts, 3.0, 1.0)"
            else:
                sub = "AND p.position = 0" if onde == "inicial" else ""
                cte = f"""WITH m1 AS MATERIALIZED (
                            SELECT p.topic_ref AS ref, bm25(post_fts, 4.0, 1.0, 0.2) AS r
                            FROM s.post_fts JOIN s.pm p ON p.id = post_fts.rowid
                            WHERE post_fts MATCH ? {sub}),
                          mm AS (SELECT ref, MIN(r) AS r, COUNT(*) AS hits FROM m1 GROUP BY ref) """
                joins += " JOIN mm ON mm.ref = t.ref"
                args.append("{title body} : (" + fts + ")")
                rank = "mm.r"
        if (q := self.expand(P.names("autor"), jm)):
            where.append(f"t.author_lc IN ({','.join('?' * len(q))})")
            args += q
        tpt = TP_TABLE[jm]
        parts = list(dict.fromkeys(self.key_of(n, jm) for n in P.names("participantes")))
        if parts:
            if P.choice("part_modo", ("todos", "qualquer"), "todos") == "todos":
                for n in parts:
                    where.append(f"t.ref IN (SELECT topic_ref FROM s.{tpt} WHERE author_lc = ?)")
                    args.append(n)
            else:
                where.append(f"t.ref IN (SELECT topic_ref FROM s.{tpt} WHERE author_lc IN ({','.join('?' * len(parts))}))")
                args += parts
        if (q := list(dict.fromkeys(self.key_of(n, jm) for n in P.names("sem_participante")))):
            where.append(f"t.ref NOT IN (SELECT topic_ref FROM s.{tpt} WHERE author_lc IN ({','.join('?' * len(q))}))")
            args += q
        if (q := self.expand(P.names("msg_de"), jm)):  # tópicos onde X respondeu (não criou)
            where.append(f"t.ref IN (SELECT topic_ref FROM s.pm WHERE author_lc IN ({','.join('?' * len(q))}) AND position > 0)")
            args += q
        fids = self.forum_ids(P)
        if fids:
            where.append(f"t.forum_id IN ({','.join('?' * len(fids))})")
            args += fids
        if (pf := P.str("prefixo")):
            if pf == "-":
                where.append("t.prefix IS NULL")
            else:
                where.append("t.prefix = ?")
                args.append(pf)
        for k, col in (("enquete", "has_poll"), ("trancado", "locked"), ("fixo", "sticky")):
            v = P.choice(k, ("", "sim", "nao"), "")
            if v:
                where.append(f"t.{col} = {1 if v == 'sim' else 0}")
        origem = P.choice("origem", ("todos", "arquivo", "novos"), "todos")
        if origem != "todos":
            where.append(f"t.is_local = {1 if origem == 'novos' else 0}")
        for pfx, col in (("resp", "replies"), ("vis", "views"), ("part", "n_participants"), ("votos", "poll_votes")):
            if (n := P.int(pfx + "_min", 0)) is not None:
                where.append(f"t.{col} >= ?")
                args.append(n)
            if (n := P.int(pfx + "_max", 0)) is not None:
                where.append(f"t.{col} <= ?")
                args.append(n)
        for pfx, col in (("criado", "created"), ("ativ", "last_date")):
            if (d := P.date(pfx + "_de")):
                where.append(f"t.{col} >= ?")
                args.append(d)
            if (d := P.date(pfx + "_ate")):
                where.append(f"t.{col} < ?")
                args.append(next_day(d))
        if (y := P.int("ano", 1990, 2100)):
            where.append("t.created >= ? AND t.created < ?")
            args += [f"{y}-01-01", f"{y + 1}-01-01"]

        has_filter = bool(fts) or P.any_set([k for k in self.TOPIC_ADV if k not in ("onde", "part_modo")])
        order = P.choice("ordem", [k for k, _ in self.TOPIC_ORDERS], "relevancia" if rank else "atividade")
        if order == "relevancia" and not rank:
            order = "atividade"
        key = {"relevancia": f"{rank} ASC", "atividade": "t.last_date DESC", "criacao": "t.created DESC",
               "criacao_asc": "t.created ASC", "respostas": "t.replies DESC", "visitas": "t.views DESC",
               "participantes": "t.n_participants DESC", "votos": "t.poll_votes DESC",
               "titulo": "t.title COLLATE NOCASE ASC"}[order]
        body = "".join(f'<div class="err">{e(x)}</div>' for x in errors)
        if has_filter and not errors:
            frm = f"s.tm t{joins}"
            w = " AND ".join(where)
            page = P.page()
            try:
                total = self.run(cte + f"SELECT COUNT(*) FROM {frm} WHERE {w}", args)[0][0]
                extra = ", mm.hits AS hits" if "mm" in joins else ", NULL AS hits"
                rows = self.run(cte + f"""SELECT t.*{extra} FROM {frm} WHERE {w}
                                    ORDER BY {key}, t.last_date DESC LIMIT ? OFFSET ?""",
                                args + [PER_PAGE, (page - 1) * PER_PAGE])
                names = self.forum_names()
                forums = self.run(cte + f"SELECT t.forum_id, COUNT(*) n FROM {frm} WHERE {w} GROUP BY 1 ORDER BY n DESC LIMIT 12", args)
                acol = f"t.author_{SUFFIX[jm]}"
                authors = self.run(cte + f"""SELECT {acol} k, MAX(t.author) a, COUNT(*) n FROM {frm} WHERE {w}
                                       AND {acol} IS NOT NULL GROUP BY {acol} ORDER BY n DESC LIMIT 10""", args)
                partic = self.run(cte + f"""SELECT x.author_lc k, x.author_lc a, COUNT(*) n FROM {frm}
                                      JOIN s.{tpt} x ON x.topic_ref = t.ref WHERE {w} AND x.author_lc NOT IN ('convidado', 'convidad')
                                      GROUP BY x.author_lc ORDER BY n DESC LIMIT 10""", args) if total <= 5000 else []
                labels = self.key_labels([r["k"] for r in authors] + [r["k"] for r in partic])
                authors = [(labels.get(r["k"], r["a"]), r["n"]) for r in authors]
                partic = [(labels.get(r["k"], r["a"]), r["n"]) for r in partic]
                years = self.run(cte + f"SELECT substr(t.created, 1, 4) y, COUNT(*) FROM {frm} WHERE {w} GROUP BY y ORDER BY y", args)
            except sqlite3.OperationalError as exc:
                body += f'<div class="err">Não foi possível executar a busca: {e(exc)}</div>'
                return self.shell("topicos", P, self.topic_form(P, path), body, "Busca de tópicos")
            trs = []
            for t in rows:
                hits = f'<div class="small">{t["hits"]} mensagens com o texto</div>' if t["hits"] else ""
                desc = f'<div class="desc">{e(t["description"])}</div>' if t["description"] else ""
                trs.append(f"""<tr><td>{prefix_html(t['prefix'], t['title'])}<a class="title" href="{topic_href(t['ref'])}">{e(t['title'])}</a>{topic_tags(t)}{desc}
<div class="small">por {member_link(t['author'])} · {e(fmt_date(t['created']))} · <a href="/f/{t['forum_id']}">{e(names.get(t['forum_id'], ''))}</a></div>{hits}</td>
<td class="num">{t['replies']}</td><td class="num v">{e(t['views'] if t['views'] is not None else '–')}</td>
<td class="num v">{t['n_participants']}</td><td class="last">{e(fmt_date(t['last_date']))}</td></tr>""")
            pg = pager(P, path, page, total)
            facets = (facet_box("Fóruns", [(names.get(r[0], f"Fórum {r[0]}"), P.url(path, forum=r[0], subforuns=0), r[1])
                                           for r in forums if r[0] is not None])
                      + facet_box("Criados por", [(r[0], P.url(path, autor=r[0]), r[1]) for r in authors])
                      + facet_box("Quem mais participou", [(r[0], P.url(path, participantes=",".join(
                          P.names("participantes") + [r[0]])), r[1]) for r in partic])
                      + year_bars("Criados por ano", [(r[0], r[1]) for r in years],
                                  lambda y: P.url(path, ano=y, criado_de=None, criado_ate=None)))
            head = (f'<div class="rhead"><b>{total} tópicos encontrados</b>'
                    f'{self.sort_form(P, path, self.TOPIC_ORDERS if rank else self.TOPIC_ORDERS[1:], order)}</div>')
            table = (f"""<div class="box"><table class="list"><tr><th>Tópico</th><th>Respostas</th><th class="v">Visitas</th>
<th class="v">Particip.</th><th class="last">Última atividade</th></tr>{''.join(trs)}</table></div>""" if trs
                     else '<div class="box"><div class="hit">Nenhum tópico encontrado.</div></div>')
            body += f'<div class="results"><div>{head}{pg}{table}{pg}</div><aside class="facets">{facets}</aside></div>'
        elif not errors:
            body += """<div class="box"><h2>Exemplos</h2><div class="hit">
<p><a href="/busca/topicos?enquete=sim&amp;ordem=votos">enquetes mais votadas</a></p>
<p><a href="/busca/topicos?participantes=saim%2C+vinians&amp;ordem=participantes">tópicos onde saim e vinians participaram</a></p>
<p><a href="/busca/topicos?q=engine+plataforma&amp;onde=inicial&amp;forum=12">engines de plataforma (texto da mensagem inicial)</a></p>
<p><a href="/busca/topicos?resp_min=100&amp;ordem=respostas">tópicos com 100+ respostas</a></p>
<p><a href="/busca/topicos?resp_max=0&amp;trancado=nao">tópicos sem resposta</a></p></div></div>"""
        return self.shell("topicos", P, self.topic_form(P, path), body,
                          f"Tópicos: {P.str('q')}" if P.str("q") else "Busca de tópicos")

    def topic_form(self, P: Params, path: str) -> str:
        modo = P.choice("modo", ("todas", "qualquer", "frase"), "todas")
        onde = P.choice("onde", ("titulo", "inicial", "qualquer"), "titulo")
        pm = P.choice("part_modo", ("todos", "qualquer"), "todos")
        origem = P.choice("origem", ("todos", "arquivo", "novos"), "todos")
        prefixes = self.meta("prefixes") or []
        pcur = P.str("prefixo")
        popts = opt("", "qualquer", pcur) + opt("-", "sem prefixo", pcur) + "".join(opt(x, x, pcur) for x in prefixes)
        adv_open = ""
        return f"""<form class="search" action="{path}">
<div class="main"><input type="search" name="q" value="{e(P.str('q'))}" placeholder='Ex.: engine plataforma, "top down"' aria-label="Texto">
<select name="onde" aria-label="Procurar em">{opt('titulo', 'no título/descrição', onde)}{opt('inicial', 'na mensagem inicial', onde)}{opt('qualquer', 'em qualquer mensagem', onde)}</select>
<select name="modo" aria-label="Modo">{opt('todas', 'todas as palavras', modo)}{opt('qualquer', 'qualquer palavra', modo)}{opt('frase', 'frase exata', modo)}</select>
<button>Buscar</button></div>
<details{adv_open}><summary>Filtros avançados</summary><div class="grid">
<fieldset><legend>Pessoas</legend>
<label>Criado por</label>{inp('autor', P, placeholder='vírgula separa vários')}
<label>Participantes</label>{inp('participantes', P, placeholder='ex.: saim, vinians')}
<label>Os participantes<select name="part_modo">{opt('todos', 'todos postaram', pm)}{opt('qualquer', 'pelo menos um postou', pm)}</select></label>
<label>Respondido por (sem ter criado)</label>{inp('msg_de', P)}
<label>Sem participação de</label>{inp('sem_participante', P)}
{join_select(P)}</fieldset>
<fieldset><legend>Onde</legend>{self.forum_select(P)}
<label>Prefixo<select name="prefixo">{popts}</select></label>
<label>Origem<select name="origem">{opt('todos', 'arquivo e novos', origem)}{opt('arquivo', 'só arquivo do gmbr', origem)}{opt('novos', 'só tópicos novos', origem)}</select></label></fieldset>
<fieldset><legend>Tipo</legend>{tri('enquete', P, 'Com enquete')}{tri('trancado', P, 'Trancado')}{tri('fixo', P, 'Fixo/anúncio')}
{rng('votos', P, 'Votos na enquete')}</fieldset>
<fieldset><legend>Números</legend>{rng('resp', P, 'Respostas')}{rng('vis', P, 'Visitas')}{rng('part', P, 'Participantes')}</fieldset>
<fieldset><legend>Datas</legend>{dates('criado', P, 'Criado (de / até)')}{dates('ativ', P, 'Última atividade (de / até)')}</fieldset>
</div><div style="margin-top:10px"><button>Buscar</button> <a href="{path}" class="small">limpar</a></div></details></form>"""

    # ============================================================= USUÁRIOS
    # {s}: sufixo da coluna (lc/acc/per); {tp}: tabela de participantes do modo
    RELATIONS = {
        "conversou": ("participaram dos mesmos tópicos que",
                      """SELECT t2.author_lc AS k, COUNT(*) AS c FROM s.{tp} t1
                         JOIN s.{tp} t2 ON t2.topic_ref = t1.topic_ref AND t2.author_lc <> t1.author_lc
                         WHERE t1.author_lc = ? GROUP BY t2.author_lc""", "tópicos em comum"),
        "citou": ("citaram", """SELECT quoter_{s} AS k, COUNT(*) AS c FROM s.quotes
                               WHERE quoted_{s} = ? AND quoter_{s} <> quoted_{s} GROUP BY 1""", "citações"),
        "citado": ("foram citados por", """SELECT quoted_{s} AS k, COUNT(*) AS c FROM s.quotes
                                          WHERE quoter_{s} = ? AND quoter_{s} <> quoted_{s} GROUP BY 1""", "citações"),
        "curtiu": ("curtiram mensagens de", """SELECT liker_{s} AS k, COUNT(*) AS c FROM s.likes
                                              WHERE liked_{s} = ? AND liker_{s} <> liked_{s} GROUP BY 1""", "curtidas"),
        "curtido": ("foram curtidos por", """SELECT liked_{s} AS k, COUNT(*) AS c FROM s.likes
                                            WHERE liker_{s} = ? AND liker_{s} <> liked_{s} GROUP BY 1""", "curtidas"),
        "respondeu": ("responderam em tópicos criados por",
                      """SELECT p.author_{s} AS k, COUNT(*) AS c FROM s.pm p JOIN s.tm t ON t.ref = p.topic_ref
                         WHERE t.author_{s} = ? AND p.position > 0 AND p.author_{s} <> t.author_{s} GROUP BY 1""",
                      "respostas"),
        "respondido": ("tiveram tópicos respondidos por",
                       """SELECT t.author_{s} AS k, COUNT(*) AS c FROM s.pm p JOIN s.tm t ON t.ref = p.topic_ref
                          WHERE p.author_{s} = ? AND p.position > 0 AND p.author_{s} <> t.author_{s} GROUP BY 1""",
                       "respostas"),
    }

    def rel_sql(self, rel: str, mode: str) -> str:
        return self.RELATIONS[rel][1].format(s=SUFFIX[mode], tp=TP_TABLE[mode])

    USER_ORDERS = [("relacao", "força da relação"), ("relevancia", "relevância do texto"),
                   ("mensagens", "mensagens (perfil)"), ("arquivadas", "mensagens arquivadas"),
                   ("reputacao", "reputação"), ("topicos", "tópicos criados"),
                   ("citado", "mais citados"), ("curtido", "mais curtidos"),
                   ("antigos", "inscrição (antigos)"), ("novos", "inscrição (recentes)"),
                   ("acesso", "último acesso"), ("ativo", "última mensagem"), ("nome", "nome (A–Z)")]
    USER_ADV = ("perfil", "nome_modo", "grupo", "cargo", "insc_de", "insc_ate", "acesso_de", "acesso_ate",
                "postou_de", "postou_ate", "msgs_min", "msgs_max", "arq_min", "arq_max", "rep_min", "rep_max",
                "top_min", "citado_min", "curtido_min", "avatar", "assinatura", "site", "com_mensagens",
                "forum", "origem", "rel_user", "rel", "rel_min", "ano")

    def page_usuarios(self, P: Params) -> str:
        path = "/busca/usuarios"
        errors: list[str] = []
        where, args, joins = ["1=1"], [], ""
        rel_col = "NULL"
        rank = None
        name = P.str("q", maxlen=60).lower()
        if name:
            nm = P.choice("nome_modo", ("contem", "comeca", "exato"), "contem")
            esc = re.sub(r"([%_\\])", r"\\\1", name)
            pat = {"contem": f"%{esc}%", "comeca": f"{esc}%", "exato": esc}[nm]
            where.append("u.name_lc LIKE ? ESCAPE '\\'")
            args.append(pat)
        fts, err = fts_query(P.str("perfil"), "todas")
        if err and P.str("perfil"):
            errors.append(err)
        if fts:
            joins += " JOIN s.user_fts ON user_fts.rowid = u.rid"
            where.append("user_fts MATCH ?")
            args.append("{profile signature} : (" + fts + ")")
            rank = "bm25(user_fts)"
        rel = P.choice("rel", tuple(self.RELATIONS), "conversou")
        jm = join_mode(P)
        rel_user = P.str("rel_user", maxlen=60).strip().lower()
        rel_label = None
        if rel_user:
            label, _, unit = self.RELATIONS[rel]
            rel_label = unit
            rel_key = self.key_of(rel_user, jm)
            joins = f" JOIN ({self.rel_sql(rel, jm)}) rel ON rel.k = u.name_lc" + joins
            args = [rel_key] + args
            where.append("u.name_lc <> ?")
            args.append(rel_key)
            if (n := P.int("rel_min", 1)):
                where.append("rel.c >= ?")
                args.append(n)
            rel_col = "rel.c"
        if (g := P.str("grupo")):
            where.append("u.groups LIKE ?")
            args.append(f"%|{g}|%")
        if (c := P.str("cargo")):
            where.append("u.rank_title = ?")
            args.append(c)
        for pfx, col in (("insc", "joined"), ("acesso", "last_visit")):
            if (d := P.date(pfx + "_de")):
                where.append(f"u.{col} >= ?")
                args.append(d)
            if (d := P.date(pfx + "_ate")):
                where.append(f"u.{col} < ?")
                args.append(next_day(d))
        if (y := P.int("ano", 1990, 2100)):
            where.append("u.joined >= ? AND u.joined < ?")
            args += [f"{y}-01-01", f"{y + 1}-01-01"]
        d1, d2 = P.date("postou_de"), P.date("postou_ate")
        fids = self.forum_ids(P)
        if d1 or d2 or fids:
            cond, cargs = ["author_lc = u.name_lc"], []
            if d1:
                cond.append("date >= ?")
                cargs.append(d1)
            if d2:
                cond.append("date < ?")
                cargs.append(next_day(d2))
            if fids:
                cond.append(f"forum_id IN ({','.join('?' * len(fids))})")
                cargs += fids
            where.append(f"EXISTS (SELECT 1 FROM s.pm WHERE {' AND '.join(cond)})")
            args += cargs
        for pfx, col in (("msgs", "post_count"), ("arq", "posts_found"), ("rep", "reputation"),
                         ("top", "topics_started"), ("citado", "quotes_recv"), ("curtido", "likes_recv")):
            if (n := P.int(pfx + "_min")) is not None:
                where.append(f"u.{col} >= ?")
                args.append(n)
            if (n := P.int(pfx + "_max")) is not None:
                where.append(f"u.{col} <= ?")
                args.append(n)
        for k, col in (("avatar", "has_avatar"), ("assinatura", "has_signature"), ("site", "has_website")):
            v = P.choice(k, ("", "sim", "nao"), "")
            if v:
                where.append(f"u.{col} = {1 if v == 'sim' else 0}")
        if P.flag("com_mensagens"):
            where.append("u.posts_found > 0")
        origem = P.choice("origem", ("todos", "membros", "locais"), "todos")
        if origem != "todos":
            where.append(f"u.is_local = {1 if origem == 'locais' else 0}")

        orders = [o for o in self.USER_ORDERS if (o[0] != "relacao" or rel_user) and (o[0] != "relevancia" or rank)]
        default = "relacao" if rel_user else ("relevancia" if rank else "mensagens")
        order = P.choice("ordem", [k for k, _ in orders], default)
        key = {"relacao": "rel.c DESC", "relevancia": f"{rank} ASC",
               "mensagens": "COALESCE(u.post_count, u.posts_found) DESC", "arquivadas": "u.posts_found DESC",
               "reputacao": "COALESCE(u.reputation, -1) DESC", "topicos": "u.topics_started DESC",
               "citado": "u.quotes_recv DESC", "curtido": "u.likes_recv DESC",
               "antigos": "COALESCE(u.joined, '9999') ASC", "novos": "COALESCE(u.joined, '') DESC",
               "acesso": "COALESCE(u.last_visit, '') DESC", "ativo": "COALESCE(u.last_post, '') DESC",
               "nome": "u.name COLLATE NOCASE ASC"}[order]
        has_filter = bool(name) or bool(fts) or P.any_set([k for k in self.USER_ADV if k not in ("nome_modo", "rel", "rel_min")])
        body = "".join(f'<div class="err">{e(x)}</div>' for x in errors)
        if has_filter and not errors:
            frm = f"s.um u{joins}"
            w = " AND ".join(where)
            page = P.page()
            try:
                total = self.run(f"SELECT COUNT(*) FROM {frm} WHERE {w}", args)[0][0]
                kc = KEY_COL[jm]
                same = (f"(SELECT group_concat(i.name, ', ') FROM s.ident i WHERE i.{kc} = u.name_lc "
                        f"AND i.name_lc <> u.name_lc)" if jm != "nome" else "NULL")
                rows = self.run(f"""SELECT u.*, {rel_col} AS relc, {same} AS same FROM {frm} WHERE {w}
                                    ORDER BY {key}, u.posts_found DESC, u.name LIMIT ? OFFSET ?""",
                                args + [PER_PAGE, (page - 1) * PER_PAGE])
                groups = self.run(f"""SELECT u.groups, COUNT(*) FROM {frm} WHERE {w} AND u.groups IS NOT NULL
                                      GROUP BY u.groups""", args)
                years = self.run(f"SELECT substr(u.joined, 1, 4) y, COUNT(*) FROM {frm} WHERE {w} GROUP BY y ORDER BY y", args)
            except sqlite3.OperationalError as exc:
                body += f'<div class="err">Não foi possível executar a busca: {e(exc)}</div>'
                return self.shell("usuarios", P, self.user_form(P, path), body, "Busca de usuários")
            gcount: dict[str, int] = {}
            for gs, n in groups:
                for g in gs.strip("|").split("|"):
                    gcount[g] = gcount.get(g, 0) + n
            members = self.f.members_by_name(r["name"] for r in rows)
            trs = []
            for u in rows:
                m = members.get(u["name"])
                grp = (u["groups"] or "").strip("|").replace("|", ", ") or (u["rank_title"] or "")
                relcell = ""
                if rel_user:
                    relq = {"conversou": dict(autor=u["name"], com_participante=rel_user, agrupar=1),
                            "citou": dict(autor=u["name"], cita=rel_user),
                            "citado": dict(autor=rel_user, cita=u["name"]),
                            "curtiu": dict(autor=rel_user, curtida_por=u["name"]),
                            "curtido": dict(autor=u["name"], curtida_por=rel_user),
                            "respondeu": dict(autor=u["name"], em_topicos_de=rel_user, tipo="respostas"),
                            "respondido": dict(autor=rel_user, em_topicos_de=u["name"], tipo="respostas")}[rel]
                    relq["juntar"] = jm
                    relcell = f'<td class="num"><a href="/busca/mensagens?{e(urlencode(relq))}">{u["relc"]}</a></td>'
                trs.append(f"""<tr><td>{member_link(u['name'], m)}{'<span class="tag new">local</span>' if u['is_local'] else ''}
<div class="small">{e(grp)}{(' · desde ' + e(fmt_date(u['joined'])[:10])) if u['joined'] else ''}</div>{('<div class="small" title="nomes juntados">+ ' + e(u['same']) + '</div>') if u['same'] else ''}</td>{relcell}
<td class="num">{e(u['post_count'] if u['post_count'] is not None else '–')}</td>
<td class="num v">{u['posts_found']}</td><td class="num v">{e(u['reputation'] if u['reputation'] is not None else '–')}</td>
<td class="num v">{u['quotes_recv']}</td><td class="last">{e(fmt_date(u['last_post'])) or '–'}</td></tr>""")
            relh = f"<th>{e(rel_label)}</th>" if rel_user else ""
            table = (f"""<div class="box"><table class="list"><tr><th>Usuário</th>{relh}<th>Msgs.</th><th class="v">Arquiv.</th>
<th class="v">Rep.</th><th class="v">Citado</th><th class="last">Última mensagem</th></tr>{''.join(trs)}</table></div>"""
                     if trs else '<div class="box"><div class="hit">Nenhum usuário encontrado.</div></div>')
            facets = (facet_box("Grupos", [(g, P.url(path, grupo=g), n)
                                           for g, n in sorted(gcount.items(), key=lambda x: -x[1])])
                      + year_bars("Inscritos por ano", [(r[0], r[1]) for r in years],
                                  lambda y: P.url(path, ano=y, insc_de=None, insc_ate=None)))
            note = ""
            if rel_user:
                note = (f'<div class="chips"><span class="small">Usuários que {e(self.RELATIONS[rel][0])} '
                        f'<b>{e(P.str("rel_user"))}</b>. O número leva às mensagens.</span></div>')
            head = f'<div class="rhead"><b>{total} usuários encontrados</b>{self.sort_form(P, path, orders, order)}</div>'
            pg = pager(P, path, page, total)
            body += f'<div class="results"><div>{head}{note}{pg}{table}{pg}</div><aside class="facets">{facets}</aside></div>'
        elif not errors:
            body += """<div class="box"><h2>Exemplos</h2><div class="hit">
<p><a href="/busca/usuarios?rel_user=saim&amp;rel=conversou">com quem saim mais conversou</a></p>
<p><a href="/busca/usuarios?rel_user=PedroX&amp;rel=citado">quem PedroX mais citou</a></p>
<p><a href="/busca/usuarios?grupo=Lendas&amp;ordem=reputacao">Lendas por reputação</a></p>
<p><a href="/busca/usuarios?perfil=programador&amp;com_mensagens=1">perfis que mencionam “programador”</a></p>
<p><a href="/busca/usuarios?postou_de=2024-01-01&amp;ordem=ativo">quem postou desde 2024</a></p></div></div>"""
        return self.shell("usuarios", P, self.user_form(P, path), body, "Busca de usuários")

    def user_form(self, P: Params, path: str) -> str:
        nm = P.choice("nome_modo", ("contem", "comeca", "exato"), "contem")
        rel = P.choice("rel", tuple(self.RELATIONS), "conversou")
        origem = P.choice("origem", ("todos", "membros", "locais"), "todos")
        gcur, ccur = P.str("grupo"), P.str("cargo")
        gopts = opt("", "qualquer", gcur) + "".join(opt(g, g, gcur) for g in (self.meta("groups") or []))
        copts = opt("", "qualquer", ccur) + "".join(opt(c, c, ccur) for c in (self.meta("ranks") or []))
        ropts = "".join(opt(k, v[0], rel) for k, v in self.RELATIONS.items())
        adv_open = ""
        return f"""<form class="search" action="{path}">
<div class="main"><input type="search" name="q" value="{e(P.str('q'))}" placeholder="Nome do usuário" aria-label="Nome">
<select name="nome_modo" aria-label="Como comparar o nome">{opt('contem', 'contém', nm)}{opt('comeca', 'começa com', nm)}{opt('exato', 'nome exato', nm)}</select>
<button>Buscar</button></div>
<details{adv_open}><summary>Filtros avançados e correlações</summary><div class="grid">
<fieldset><legend>Correlação</legend>
<label>Usuários que…<select name="rel">{ropts}</select></label>
<label>…o usuário</label>{inp('rel_user', P, placeholder='ex.: saim')}
<label>Pelo menos (vezes)</label>{inp('rel_min', P, 'number', min='1', placeholder='1')}
{join_select(P)}</fieldset>
<fieldset><legend>Perfil</legend>
<label>Texto no perfil/assinatura</label>{inp('perfil', P, placeholder='ex.: programador, "rpg maker"')}
<label>Grupo<select name="grupo">{gopts}</select></label><label>Cargo<select name="cargo">{copts}</select></label>
<div class="pair">{tri('avatar', P, 'Avatar')}{tri('assinatura', P, 'Assinatura')}</div>{tri('site', P, 'Site')}</fieldset>
<fieldset><legend>Números</legend>{rng('msgs', P, 'Mensagens (perfil)')}{rng('arq', P, 'Mensagens arquivadas')}
{rng('rep', P, 'Reputação')}<label>Tópicos criados (mín.)</label>{inp('top_min', P, 'number', min='0')}
<div class="pair"><label>Citado (mín.){inp('citado_min', P, 'number', min='0')}</label><label>Curtido (mín.){inp('curtido_min', P, 'number', min='0')}</label></div></fieldset>
<fieldset><legend>Atividade</legend>{dates('insc', P, 'Inscrição (de / até)')}{dates('acesso', P, 'Último acesso (de / até)')}
{dates('postou', P, 'Postou entre')}{self.forum_select(P)}</fieldset>
<fieldset><legend>Outros</legend><label class="ck"><input type="checkbox" name="com_mensagens" value="1"{' checked' if P.flag('com_mensagens') else ''}>só quem tem mensagens arquivadas</label>
<label>Origem<select name="origem">{opt('todos', 'todos', origem)}{opt('membros', 'membros do gmbr', origem)}{opt('locais', 'só usuários novos', origem)}</select></label></fieldset>
</div><div style="margin-top:10px"><button>Buscar</button> <a href="{path}" class="small">limpar</a></div></details></form>"""

    # ===================================================== perfil: correlações
    def member_correlations(self, name: str, mode: str = "pessoa") -> str:
        if not self.ready():
            return ""
        mode = mode if mode in SUFFIX else "pessoa"
        sfx, tpt = SUFFIX[mode], TP_TABLE[mode]
        key = self.key_of(name, mode)
        one = lambda sql, *a: self.db.execute(sql, a).fetchone()[0]
        posts = one(f"SELECT COUNT(*) FROM s.pm WHERE author_{sfx} = ?", key)
        if not posts:
            return ""
        stats = {
            "posts": posts,
            "started": one(f"SELECT COUNT(*) FROM s.tm WHERE author_{sfx} = ?", key),
            "topics": one(f"SELECT COUNT(*) FROM s.{tpt} WHERE author_lc = ?", key),
            "qg": one(f"SELECT COUNT(*) FROM s.quotes WHERE quoter_{sfx} = ? AND quoted_{sfx} <> quoter_{sfx}", key),
            "qr": one(f"SELECT COUNT(*) FROM s.quotes WHERE quoted_{sfx} = ? AND quoted_{sfx} <> quoter_{sfx}", key),
            "lg": one(f"SELECT COUNT(*) FROM s.likes WHERE liker_{sfx} = ? AND liked_{sfx} <> liker_{sfx}", key),
            "lr": one(f"SELECT COUNT(*) FROM s.likes WHERE liked_{sfx} = ? AND liked_{sfx} <> liker_{sfx}", key),
        }
        first, last = self.db.execute(f"SELECT MIN(date), MAX(date) FROM s.pm WHERE author_{sfx} = ?", (key,)).fetchone()

        def top(rel, n=8):
            return self.db.execute(
                f"""SELECT COALESCE(i.name, x.k) AS name, x.c FROM ({self.rel_sql(rel, mode)}) x
                    LEFT JOIN s.ident i ON i.name_lc = x.k
                    WHERE x.k <> ? AND x.k IS NOT NULL AND x.k NOT IN ('convidado', 'convidad')
                    ORDER BY x.c DESC LIMIT {n}""", (key, key)).fetchall()

        def box(title, rel, rows, link):
            if not rows:
                return ""
            lis = "".join(f'<li>{member_link(r["name"])}<a href="{e(link(r["name"]))}">{r["c"]}</a></li>' for r in rows)
            more = (f'<li><a href="/busca/usuarios?{e(urlencode(dict(rel_user=name, rel=rel, juntar=mode)))}">'
                    f'ver todos…</a></li>')
            return f'<div class="box"><h2>{e(title)}</h2><ul>{lis}{more}</ul></div>'

        q = lambda **kw: "/busca/mensagens?" + urlencode({**kw, "juntar": mode})
        boxes = [
            box("Mais conversou com", "conversou", top("conversou"),
                lambda n: q(autor=name, com_participante=n, agrupar=1)),
            box("Quem mais citou", "citado", top("citado"), lambda n: q(autor=name, cita=n)),
            box("Quem mais o citou", "citou", top("citou"), lambda n: q(autor=n, cita=name)),
            box("Respondeu mais a", "respondido", top("respondido"),
                lambda n: q(autor=name, em_topicos_de=n, tipo="respostas")),
            box("Mais respondido por", "respondeu", top("respondeu"),
                lambda n: q(autor=n, em_topicos_de=name, tipo="respostas")),
            box("Curtiu mensagens de", "curtido", top("curtido"), lambda n: q(autor=n, curtida_por=name)),
            box("Curtido por", "curtiu", top("curtiu"), lambda n: q(autor=name, curtida_por=n)),
        ]
        names = self.forum_names()
        fr = self.db.execute(f"""SELECT forum_id, COUNT(*) c FROM s.pm WHERE author_{sfx} = ?
                                 GROUP BY forum_id ORDER BY c DESC LIMIT 10""", (key,)).fetchall()
        if fr:
            lis = "".join(f'<li><a href="{e(q(autor=name, forum=r[0], subforuns=0))}">{e(names.get(r[0], "?"))}</a>'
                          f'<span>{r[1]}</span></li>' for r in fr)
            boxes.insert(0, f'<div class="box"><h2>Onde mais postou</h2><ul>{lis}</ul></div>')
        years = self.db.execute(f"""SELECT substr(date, 1, 4), COUNT(*) FROM s.pm WHERE author_{sfx} = ?
                                    GROUP BY 1 ORDER BY 1""", (key,)).fetchall()
        boxes.insert(1, year_bars("Mensagens por ano", [(r[0], r[1]) for r in years],
                                  lambda y: q(autor=name, ano=y)))
        sv = (f'<div class="stats"><span><b>{stats["posts"]}</b> mensagens arquivadas</span>'
              f'<span><b>{stats["started"]}</b> tópicos criados</span>'
              f'<span><b>{stats["topics"]}</b> tópicos com participação</span>'
              f'<span><b>{stats["qg"]}</b> citações feitas / <b>{stats["qr"]}</b> recebidas</span>'
              f'<span><b>{stats["lg"]}</b> curtidas dadas / <b>{stats["lr"]}</b> recebidas</span>'
              f'<span>{e(fmt_date(first))} → {e(fmt_date(last))}</span></div>')
        # modo de junção e nomes considerados
        switch = " · ".join(
            (f"<b>{e(lbl)}</b>" if k == mode else f'<a href="/membro/{quote(name)}?juntar={k}">{e(lbl)}</a>')
            for k, lbl in JOIN_MODES)
        same = self.same_names(key, mode)
        if key != name.lower():
            same = [n for n in same if n.lower() != name.lower()]
            rep_name = self.key_labels([key]).get(key, key)
            same = [rep_name] + same
        joined = (f'<div class="small" style="margin-top:4px">Contando também: '
                  + ", ".join(member_link(n) for n in same) + "</div>") if same else ""
        links = (f'<div class="chips"><a href="{e(q(autor=name))}">todas as mensagens</a>'
                 f'<a href="/busca/topicos?{e(urlencode(dict(autor=name, juntar=mode)))}">tópicos criados</a>'
                 f'<a href="/busca/topicos?{e(urlencode(dict(participantes=name, juntar=mode)))}">tópicos em que participou</a>'
                 f'<a href="{e(q(cita=name))}">mensagens que o citam</a></div>')
        grid = "".join(b for b in boxes if b)
        return (CSS + f'<div class="box"><h2>Atividade e correlações</h2>{sv}'
                f'<div style="padding:0 12px 10px"><div class="small">Juntar nomes: {switch}</div>{joined}{links}</div></div>'
                f'<div class="facets" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:0 16px">{grid}</div>')
