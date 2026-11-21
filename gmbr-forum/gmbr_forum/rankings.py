"""Página de rankings: top 10 de pessoas e de conteúdo sob vários critérios.

Cada ranking declara título, grupo, critério (mostrado na página) e uma função
que devolve linhas (rótulo, link, valor, detalhe). O resultado fica em cache
por combinação de filtros (período, junção de nomes) até o índice mudar.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import unicodedata
from collections import Counter, OrderedDict, defaultdict
from urllib.parse import quote, urlencode

from .app import e, fmt_date, layout, prefix_html, topic_href
from .search import JOIN_MODES, KEY_COL, SUFFIX, TP_TABLE, Params, join_mode, opt

TOP = 10
GUESTS = ("convidado", "convidad")
PROJECT_FORUMS = (5, 6, 9, 13)        # andamento, completos, Hall of Fame, Museu
HOF_FORUMS = (9, 13)
QA_FORUM = 10                          # Perguntas e Respostas
TUTORIAL_FORUMS = (55, 19, 12, 41)     # tutoriais, em construção, engines, extensões
CONTEST_FORUMS = (4, 26)
GRADE_POINTS = {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1}
GAME_GRADE_BONUS = {"A": 15, "B": 10, "C": 5, "D": 2, "E": 0}
_GRADE_PREFIX = re.compile(r"^\s*([A-E])\s*-\s")

STOP = set("""
a o e as os um uma uns umas de do da dos das no na nos nas em por para pra pro com sem sobre ao aos
que se nao não sim mais menos muito muita meu minha meus minhas seu sua seus suas teu tua isso isto
esse essa este esta aquele aquela ele ela eles elas eu voce você vc vcs me te nos lhe como quando onde
qual quais porque pq por quê quem the of and to in for is on with my your how what this
ja já tem ter ser estar esta está fica ficar faz fazer feito fiz dar dá pode posso poder quero queria
alguem alguém algum alguma tudo todo toda todos outro outra mesmo coisa coisas so só ou entre ate até
re ajuda ajudem ajudar help duvida dúvida duvidas dúvidas problema problemas pergunta perguntas
gm gml game maker gamemaker gms jogo jogos game games novo nova novos tópico topico versao versão
urgente favor pfv pls please alguem algum preciso precisando sei saber resolvido resolvida
v1 v2 1 2 3 4 5 0 x d vs criar criando cria faço faco fazendo usando usar ajudem pedido pedidos
gente galera aqui ai aí bem bom boa
""".split())
QA_STOP = STOP | set("erro errado funciona funcionando consigo consegue usar uso criar código codigo".split())

TOOLS = [
    ("GameMaker 8 / 8.1", '"game maker 8" OR gm8 OR "gm 8" OR "gm8.1" OR "game maker 8.1"'),
    ("GameMaker 7 ou anterior", '"game maker 7" OR gm7 OR "gm 7" OR gm6 OR "game maker 6" OR gm5'),
    ("GameMaker Studio 1", '"game maker studio" OR "gamemaker studio" OR gms OR "gm studio" OR gms1'),
    ("GameMaker Studio 2", 'gms2 OR "gms 2" OR "studio 2" OR "gamemaker studio 2"'),
    ("RPG Maker", '"rpg maker" OR rpgmaker OR rmxp OR rmvx'),
    ("Construct", 'construct OR "construct 2" OR c2'),
    ("Unity", 'unity OR unity3d'),
    ("Godot", 'godot'),
    ("Unreal", 'unreal OR udk'),
    ("Blender", 'blender'),
    ("Photoshop", 'photoshop'),
    ("GIMP", 'gimp'),
    ("Multimedia Fusion / MMF", '"multimedia fusion" OR mmf OR mmf2 OR "games factory"'),
    ("Java", 'java'),
    ("C++ / C# / Visual Studio", 'cpp OR csharp OR "visual studio" OR "visual c"'),
    ("Python", 'python'),
    ("Android", 'android'),
    ("HTML5 / JavaScript", 'html5 OR javascript'),
]


def fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


class Ranking:
    def __init__(self, key, group, title, criterion, fn, period=True, unit=""):
        self.key, self.group, self.title, self.criterion = key, group, title, criterion
        self.fn, self.period, self.unit = fn, period, unit


class Rankings:
    def __init__(self, forum, search):
        self.f = forum
        self.s = search
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        R = Ranking
        self.items = [
            # ------------------------------------------------------- pessoas
            R("postadores", "Pessoas", "Quem mais postou",
              "Mensagens arquivadas no período.", self.r_posters, unit="mensagens"),
            R("ajudantes", "Pessoas", "Quem mais respondeu dúvidas",
              "Tópicos de Perguntas e Respostas, criados por outra pessoa, em que respondeu. "
              "O detalhe mostra o total de respostas.", self.r_helpers, unit="tópicos"),
            R("criadores", "Pessoas", "Quem mais criou jogos",
              "Projetos abertos em Projetos em Andamento, Projetos Completos e Hall of Fame/Museu.",
              self.r_creators, unit="projetos"),
            R("melhores_criadores", "Pessoas", "Melhores criadores de jogos",
              "Notas dos avaliadores no perfil (A=5, B=4, C=3, D=2, E=1), +8 por jogo no Hall of Fame/Museu "
              "e medalhas de concursos (ouro 5, prata 3, bronze 2). Notas e medalhas vêm do perfil e "
              "não dependem do período.", self.r_best_creators, unit="pontos"),
            R("citados", "Pessoas", "Mais citados",
              "Vezes em que outras pessoas citaram suas mensagens.", self.r_quoted, unit="citações"),
            R("influentes", "Pessoas", "Mais influentes",
              "PageRank do grafo de interações: quem cita, responde e curte quem (citação e curtida valem 1, "
              "resposta no tópico de alguém vale 0,5). Ser citado ou respondido por quem também é influente "
              "conta mais. 100 = o mais influente.", self.r_influence, unit="índice"),
            R("importantes", "Pessoas", "Mais importantes (Índice GMBR)",
              "Média de 7 notas de 0 a 100 (escala logarítmica, 100 = o maior do período): volume de mensagens, "
              "tópicos criados, alcance (pessoas distintas com quem interagiu), citações recebidas, influência, "
              "contribuições técnicas (projetos, tutoriais, engines) e anos ativos. Só quem tem 20+ mensagens.",
              self.r_importance, unit="índice"),
            R("tutores", "Pessoas", "Quem mais publicou tutoriais e engines",
              "Tópicos criados em Tutoriais, Tutoriais em Construção, Engines e Extensões e Bibliotecas.",
              self.r_tutors, unit="tópicos"),
            R("medalhistas", "Pessoas", "Mais medalhas em concursos",
              "Ouro, prata e bronze do perfil (ouro 3, prata 2, bronze 1 para ordenar). Não depende do período.",
              self.r_medals, period=False, unit="pontos"),
            R("conversadores", "Pessoas", "Quem conversou com mais gente",
              "Pessoas distintas com quem trocou citações, respostas ou curtidas.", self.r_reach, unit="pessoas"),
            R("veteranos", "Pessoas", "Veteranos",
              "Anos distintos com mensagens (desempate: tempo entre a primeira e a última).",
              self.r_veterans, unit="anos"),
            R("curtidos", "Pessoas", "Mais curtidos",
              "Curtidas recebidas (o fórum registrou poucas curtidas, então a lista é curta).",
              self.r_liked, unit="curtidas"),
            R("reputacao", "Pessoas", "Maior reputação",
              "Reputação exibida no perfil do fórum. Não depende do período.", self.r_reputation,
              period=False, unit="reputação"),
            # ------------------------------------------------------- conteúdo
            R("jogos", "Conteúdo", "Jogos e projetos de destaque",
              "Pessoas distintas que comentaram (sem contar o autor) + 5×log10(visitas) + nota da avaliação "
              "no título (A +15, B +10, C +5, D +2) + 20 se está no Hall of Fame/Museu + 5 se é projeto completo.",
              self.r_games, unit="pontos"),
            R("assuntos", "Conteúdo", "Assuntos mais falados",
              "Palavras e pares de palavras mais frequentes nos títulos dos tópicos (sem palavras genéricas).",
              self.r_subjects, unit="tópicos"),
            R("duvidas", "Conteúdo", "Dúvidas mais comuns",
              "Temas mais frequentes nos títulos de Perguntas e Respostas, com a dúvida mais vista de cada um.",
              self.r_questions, unit="tópicos"),
            R("funcoes", "Conteúdo", "Funções do GML mais discutidas",
              "Mensagens que usam a função no texto ou no código (ex.: instance_create(...)).",
              self.r_functions, unit="mensagens"),
            R("ferramentas", "Conteúdo", "Ferramentas e engines mais mencionadas",
              "Mensagens que mencionam cada ferramenta (busca de texto, sem acentos).", self.r_tools,
              unit="mensagens"),
            R("vistos", "Conteúdo", "Tópicos mais vistos", "Visitas registradas pelo fórum.",
              self.r_most_viewed, unit="visitas"),
            R("discussoes", "Conteúdo", "Maiores discussões", "Pessoas distintas que participaram do tópico.",
              self.r_biggest, unit="pessoas"),
            R("respondidos", "Conteúdo", "Tópicos mais respondidos", "Número de respostas.",
              self.r_most_replied, unit="respostas"),
            R("duradouros", "Conteúdo", "Discussões mais duradouras",
              "Tempo entre a primeira e a última mensagem (tópicos com 10+ mensagens).", self.r_longest,
              unit="anos"),
            R("enquetes", "Conteúdo", "Enquetes mais votadas", "Total de votos.", self.r_polls, unit="votos"),
            R("tutoriais", "Conteúdo", "Tutoriais e engines mais populares",
              "Visitas dos tópicos em Tutoriais, Engines e Extensões.", self.r_tutorials, unit="visitas"),
            R("concursos", "Conteúdo", "Concursos com mais participantes",
              "Pessoas distintas nos tópicos de Concursos.", self.r_contests, unit="pessoas"),
            R("mensagens_curtidas", "Conteúdo", "Mensagens mais curtidas", "Curtidas recebidas pela mensagem.",
              self.r_liked_posts, unit="curtidas"),
            R("foruns", "Conteúdo", "Fóruns mais ativos", "Mensagens no período.", self.r_forums,
              unit="mensagens"),
            R("anos", "Conteúdo", "Anos mais movimentados", "Mensagens por ano.", self.r_years,
              unit="mensagens"),
        ]
        self.by_key = {r.key: r for r in self.items}

    # =============================================================== infra
    @property
    def db(self):
        return self.f.db

    def q(self, sql, *args):
        return self.db.execute(sql, args).fetchall()

    def period(self, P: Params) -> tuple[str | None, str | None]:
        de, ate = P.int("de", 1990, 2100), P.int("ate", 1990, 2100)
        if de and ate and de > ate:
            de, ate = ate, de
        return (f"{de}-01-01" if de else None), (f"{ate + 1}-01-01" if ate else None)

    def dwhere(self, col: str, ctx) -> tuple[str, list]:
        sql, args = "", []
        if ctx["d1"]:
            sql += f" AND {col} >= ?"
            args.append(ctx["d1"])
        if ctx["d2"]:
            sql += f" AND {col} < ?"
            args.append(ctx["d2"])
        return sql, args

    def person(self, key, ctx) -> tuple[str, str]:
        name = ctx["labels"].get(key) or key
        return name, f"/membro/{quote(name)}" + (f"?juntar={ctx['mode']}" if ctx["mode"] != "pessoa" else "")

    def people_rows(self, rows, ctx, detail=None):
        """rows: [(key, valor, ...)] -> linhas prontas, com rótulos resolvidos."""
        rows = [r for r in rows if r[0] and r[0] not in GUESTS][:TOP]
        ctx["labels"].update(self.s.key_labels([r[0] for r in rows if r[0] not in ctx["labels"]]))
        out = []
        for r in rows:
            name, link = self.person(r[0], ctx)
            out.append((name, link, r[1], detail(r) if detail else ""))
        return out

    def topic_rows(self, rows, value_fmt=None, detail=None):
        out = []
        for t in rows:
            label = (t["prefix"] + " " if t["prefix"] and not (t["title"] or "").startswith(t["prefix"]) else "") + (t["title"] or "")
            out.append((label, topic_href(t["ref"]), value_fmt(t) if value_fmt else t["v"],
                        detail(t) if detail else f"por {t['author'] or '?'} · {fmt_date(t['created'])[:10]}"))
        return out

    def msearch(self, ctx, **kw) -> str:
        kw = {k: v for k, v in kw.items() if v not in (None, "")}
        if ctx["de"]:
            kw.setdefault("data_de", f"{ctx['de']}-01-01")
        if ctx["ate"]:
            kw.setdefault("data_ate", f"{ctx['ate']}-12-31")
        if ctx["mode"] != "pessoa":
            kw["juntar"] = ctx["mode"]
        return "/busca/mensagens?" + urlencode(kw)

    def tsearch(self, ctx, **kw) -> str:
        kw = {k: v for k, v in kw.items() if v not in (None, "")}
        if ctx["de"]:
            kw.setdefault("criado_de", f"{ctx['de']}-01-01")
        if ctx["ate"]:
            kw.setdefault("criado_ate", f"{ctx['ate']}-12-31")
        return "/busca/topicos?" + urlencode(kw)

    # ============================================================ pessoas
    def r_posters(self, ctx):
        a = ctx["a"]
        dw, args = self.dwhere("date", ctx)
        rows = self.q(f"""SELECT author_{a} k, COUNT(*) n, COUNT(DISTINCT topic_ref) t FROM s.pm
                          WHERE author_{a} IS NOT NULL {dw} GROUP BY 1 ORDER BY n DESC LIMIT 15""", *args)
        return self.people_rows(rows, ctx, lambda r: f"em {r[2]} tópicos")

    def r_helpers(self, ctx):
        a = ctx["a"]
        dw, args = self.dwhere("p.date", ctx)
        rows = self.q(f"""SELECT p.author_{a} k, COUNT(DISTINCT p.topic_ref) n, COUNT(*) m
                          FROM s.pm p JOIN s.tm t ON t.ref = p.topic_ref
                          WHERE t.forum_id = {QA_FORUM} AND p.position > 0 AND p.author_{a} <> t.author_{a} {dw}
                          GROUP BY 1 ORDER BY n DESC LIMIT 15""", *args)
        return self.people_rows(rows, ctx, lambda r: f"{r[2]} respostas")

    def r_creators(self, ctx):
        a = ctx["a"]
        dw, args = self.dwhere("created", ctx)
        rows = self.q(f"""SELECT author_{a} k, COUNT(*) n,
                                 SUM(forum_id = 6) c, SUM(forum_id IN {HOF_FORUMS}) h
                          FROM s.tm WHERE forum_id IN {PROJECT_FORUMS} AND author_{a} IS NOT NULL {dw}
                          GROUP BY 1 ORDER BY n DESC, c DESC LIMIT 15""", *args)
        return self.people_rows(rows, ctx, lambda r: f"{r[2]} completos" + (f" · {r[3]} no Hall of Fame" if r[3] else ""))

    def _profile_by_key(self, ctx) -> dict:
        """Medalhas e notas do perfil, somadas por pessoa/conta."""
        kc = KEY_COL[ctx["mode"]]
        rows = self.q(f"""SELECT COALESCE(i.{kc}, lower(m.name)) k, m.extra_fields, m.reputation
                          FROM members m LEFT JOIN s.ident i ON i.name_lc = lower(m.name)""")
        out: dict = defaultdict(lambda: {"ouro": 0, "prata": 0, "bronze": 0, "notas": [], "rep": 0})
        for k, ef, rep in rows:
            try:
                d = json.loads(ef or "{}")
            except ValueError:
                d = {}
            o = out[k]
            for f, col in (("Ouro", "ouro"), ("Prata", "prata"), ("Bronze", "bronze")):
                try:
                    o[col] += max(0, int(str(d.get(f) or 0).strip() or 0))
                except ValueError:
                    pass
            notas = d.get("Notas recebidas") or ""
            if isinstance(notas, dict):
                notas = notas.get("text") or ""
            o["notas"] += re.findall(r"(?<![A-Za-z])([A-E])(?![a-z])", str(notas))
            o["rep"] = max(o["rep"], rep or 0)
        return out

    def r_best_creators(self, ctx):
        a = ctx["a"]
        prof = self._profile_by_key(ctx)
        dw, args = self.dwhere("created", ctx)
        hof = dict(self.q(f"""SELECT author_{a}, COUNT(*) FROM s.tm WHERE forum_id IN {HOF_FORUMS}
                              AND author_{a} IS NOT NULL {dw} GROUP BY 1""", *args))
        score = []
        for k in set(prof) | set(hof):
            p = prof.get(k) or {"notas": [], "ouro": 0, "prata": 0, "bronze": 0}
            pts = sum(GRADE_POINTS[g] for g in p["notas"]) + 8 * hof.get(k, 0) + \
                5 * p["ouro"] + 3 * p["prata"] + 2 * p["bronze"]
            if pts:
                score.append((k, pts, p, hof.get(k, 0)))
        score.sort(key=lambda x: -x[1])

        def det(r):
            p, h = r[2], r[3]
            parts = []
            if p["notas"]:
                parts.append("notas " + "".join(sorted(p["notas"])))
            if h:
                parts.append(f"{h} no Hall of Fame")
            med = " ".join(f"{n}{s}" for n, s in ((p["ouro"], "🥇"), (p["prata"], "🥈"), (p["bronze"], "🥉")) if n)
            if med:
                parts.append(med)
            return " · ".join(parts)
        return self.people_rows(score[:20], ctx, det)

    def r_quoted(self, ctx):
        a = ctx["a"]
        dw, args = self.dwhere("p.date", ctx)
        rows = self.q(f"""SELECT q.quoted_{a} k, COUNT(*) n, COUNT(DISTINCT q.quoter_{a}) d
                          FROM s.quotes q JOIN s.pm p ON p.id = q.post_id
                          WHERE q.quoted_{a} <> q.quoter_{a} {dw} GROUP BY 1 ORDER BY n DESC LIMIT 15""", *args)
        return self.people_rows(rows, ctx, lambda r: f"por {r[2]} pessoas")

    def graph(self, ctx) -> dict:
        """Grafo de interações por chave: {(origem, destino): peso}."""
        if "graph" in ctx:
            return ctx["graph"]
        a = ctx["a"]
        dw, args = self.dwhere("p.date", ctx)
        w: dict = defaultdict(float)
        for src, dst, n in self.q(f"""SELECT q.quoter_{a}, q.quoted_{a}, COUNT(*) FROM s.quotes q
                                      JOIN s.pm p ON p.id = q.post_id
                                      WHERE q.quoter_{a} <> q.quoted_{a} {dw} GROUP BY 1, 2""", *args):
            w[(src, dst)] += n
        for src, dst, n in self.q(f"""SELECT l.liker_{a}, l.liked_{a}, COUNT(*) FROM s.likes l
                                      JOIN s.pm p ON p.id = l.post_id
                                      WHERE l.liker_{a} <> l.liked_{a} {dw} GROUP BY 1, 2""", *args):
            w[(src, dst)] += n
        for src, dst, n in self.q(f"""SELECT p.author_{a}, t.author_{a}, COUNT(*) FROM s.pm p
                                      JOIN s.tm t ON t.ref = p.topic_ref
                                      WHERE p.position > 0 AND p.author_{a} <> t.author_{a} {dw}
                                      GROUP BY 1, 2""", *args):
            w[(src, dst)] += 0.5 * n
        ctx["graph"] = {k: v for k, v in w.items() if k[0] and k[1] and k[0] not in GUESTS and k[1] not in GUESTS}
        return ctx["graph"]

    def pagerank(self, ctx) -> dict:
        if "pr" in ctx:
            return ctx["pr"]
        g = self.graph(ctx)
        nodes = {n for e_ in g for n in e_}
        if not nodes:
            ctx["pr"] = {}
            return {}
        out_w: dict = defaultdict(float)
        inc: dict = defaultdict(list)
        for (s, d), w in g.items():
            out_w[s] += w
            inc[d].append((s, w))
        N, damp = len(nodes), 0.85
        pr = {n: 1 / N for n in nodes}
        for _ in range(40):
            dangling = sum(pr[n] for n in nodes if out_w[n] == 0)
            base = (1 - damp) / N + damp * dangling / N
            new = {n: base + damp * sum(pr[s] * w / out_w[s] for s, w in inc[n]) for n in nodes}
            diff = sum(abs(new[n] - pr[n]) for n in nodes)
            pr = new
            if diff < 1e-9:
                break
        ctx["pr"] = pr
        return pr

    def r_influence(self, ctx):
        pr = self.pagerank(ctx)
        if not pr:
            return []
        top = max(pr.values())
        g = self.graph(ctx)
        indeg: dict = defaultdict(set)
        for s, d in g:
            indeg[d].add(s)
        rows = sorted(((k, round(100 * v / top, 1)) for k, v in pr.items()), key=lambda x: -x[1])[:15]
        return self.people_rows(rows, ctx, lambda r: f"recebeu interação de {len(indeg[r[0]])} pessoas")

    def reach(self, ctx) -> dict:
        if "reach" not in ctx:
            nb: dict = defaultdict(set)
            for s, d in self.graph(ctx):
                nb[s].add(d)
                nb[d].add(s)
            ctx["reach"] = nb
        return ctx["reach"]

    def r_reach(self, ctx):
        nb = self.reach(ctx)
        rows = sorted(((k, len(v)) for k, v in nb.items()), key=lambda x: -x[1])[:15]
        return self.people_rows(rows, ctx)

    def r_importance(self, ctx):
        a = ctx["a"]
        dw, args = self.dwhere("date", ctx)
        base = {r[0]: r[1:] for r in self.q(
            f"""SELECT author_{a}, COUNT(*), COUNT(DISTINCT substr(date, 1, 4)) FROM s.pm
                WHERE author_{a} IS NOT NULL {dw} GROUP BY 1 HAVING COUNT(*) >= 20""", *args)}
        base = {k: v for k, v in base.items() if k not in GUESTS}
        if not base:
            return []
        dwc, cargs = self.dwhere("created", ctx)
        started = dict(self.q(f"SELECT author_{a}, COUNT(*) FROM s.tm WHERE 1=1 {dwc} GROUP BY 1", *cargs))
        contrib = dict(self.q(f"""SELECT author_{a}, COUNT(*) FROM s.tm WHERE forum_id IN
                                  {PROJECT_FORUMS + TUTORIAL_FORUMS} {dwc} GROUP BY 1""", *cargs))
        dwp, pargs = self.dwhere("p.date", ctx)
        quoted = dict(self.q(f"""SELECT q.quoted_{a}, COUNT(*) FROM s.quotes q JOIN s.pm p ON p.id = q.post_id
                                 WHERE q.quoted_{a} <> q.quoter_{a} {dwp} GROUP BY 1""", *pargs))
        pr, nb = self.pagerank(ctx), self.reach(ctx)
        dims = {
            "mensagens": {k: v[0] for k, v in base.items()},
            "tópicos": {k: started.get(k, 0) for k in base},
            "alcance": {k: len(nb.get(k, ())) for k in base},
            "citações": {k: quoted.get(k, 0) for k in base},
            "influência": {k: pr.get(k, 0) for k in base},
            "contribuições": {k: contrib.get(k, 0) for k in base},
            "anos ativos": {k: v[1] for k, v in base.items()},
        }
        pct: dict = {}
        for dim, vals in dims.items():
            # escala logarítmica relativa ao maior valor da dimensão (0–100):
            # separa melhor o topo do que percentis, que saturam perto de 100
            if dim == "influência":
                vals = {k: v * 1e4 for k, v in vals.items()}
            top = math.log1p(max(vals.values()) or 0) or 1
            pct[dim] = {k: 100 * math.log1p(v) / top for k, v in vals.items()}
        score = {k: sum(pct[d][k] for d in dims) / len(dims) for k in base}
        rows = sorted(score.items(), key=lambda x: -x[1])[:15]

        def det(r):
            best = sorted(dims, key=lambda d: -pct[d][r[0]])[:3]
            return "destaque em " + ", ".join(best)
        return self.people_rows([(k, round(v, 1)) for k, v in rows], ctx, det)

    def r_tutors(self, ctx):
        a = ctx["a"]
        dw, args = self.dwhere("created", ctx)
        rows = self.q(f"""SELECT author_{a} k, COUNT(*) n, SUM(forum_id IN (55, 19)) tut, SUM(forum_id IN (12, 41)) eng
                          FROM s.tm WHERE forum_id IN {TUTORIAL_FORUMS} AND author_{a} IS NOT NULL {dw}
                          GROUP BY 1 ORDER BY n DESC LIMIT 15""", *args)
        return self.people_rows(rows, ctx, lambda r: f"{r[2]} tutoriais · {r[3]} engines/extensões")

    def r_medals(self, ctx):
        prof = self._profile_by_key(ctx)
        rows = sorted(((k, 3 * p["ouro"] + 2 * p["prata"] + p["bronze"], p) for k, p in prof.items()
                       if p["ouro"] + p["prata"] + p["bronze"]), key=lambda x: (-x[1], -x[2]["ouro"]))
        return self.people_rows(rows[:20], ctx, lambda r: f"🥇 {r[2]['ouro']} · 🥈 {r[2]['prata']} · 🥉 {r[2]['bronze']}")

    def r_veterans(self, ctx):
        a = ctx["a"]
        dw, args = self.dwhere("date", ctx)
        rows = self.q(f"""SELECT author_{a} k, COUNT(DISTINCT substr(date, 1, 4)) y, MIN(date) f, MAX(date) l, COUNT(*) n
                          FROM s.pm WHERE author_{a} IS NOT NULL AND date IS NOT NULL {dw}
                          GROUP BY 1 HAVING n >= 20
                          ORDER BY y DESC, julianday(MAX(date)) - julianday(MIN(date)) DESC LIMIT 15""", *args)
        return self.people_rows(rows, ctx, lambda r: f"{(r[2] or '')[:4]}–{(r[3] or '')[:4]} · {r[4]} mensagens")

    def r_liked(self, ctx):
        a = ctx["a"]
        dw, args = self.dwhere("p.date", ctx)
        rows = self.q(f"""SELECT l.liked_{a} k, COUNT(*) n, COUNT(DISTINCT l.liker_{a}) d
                          FROM s.likes l JOIN s.pm p ON p.id = l.post_id
                          WHERE l.liked_{a} <> l.liker_{a} {dw} GROUP BY 1 ORDER BY n DESC LIMIT 15""", *args)
        return self.people_rows(rows, ctx, lambda r: f"de {r[2]} pessoas")

    def r_reputation(self, ctx):
        prof = self._profile_by_key(ctx)
        rows = sorted(((k, p["rep"]) for k, p in prof.items() if p["rep"]), key=lambda x: -x[1])
        return self.people_rows(rows[:20], ctx)

    # =========================================================== conteúdo
    TOPIC_COLS = "t.ref, t.title, t.prefix, t.author, t.created, t.views, t.replies, t.n_participants, t.forum_id"

    def r_games(self, ctx):
        dw, args = self.dwhere("t.created", ctx)
        rows = self.q(f"""SELECT {self.TOPIC_COLS},
                                 (SELECT COUNT(*) FROM s.tp x WHERE x.topic_ref = t.ref
                                  AND x.author_lc <> t.author_lc AND x.author_lc NOT IN {GUESTS}) AS others
                          FROM s.tm t WHERE t.forum_id IN {PROJECT_FORUMS} {dw}""", *args)
        scored = []
        for t in rows:
            m = _GRADE_PREFIX.match(t["title"] or "")
            grade = m.group(1) if m else None
            pts = (t["others"] + 5 * math.log10((t["views"] or 0) + 1) + GAME_GRADE_BONUS.get(grade, 0)
                   + (20 if t["forum_id"] in HOF_FORUMS else 0) + (5 if t["forum_id"] == 6 else 0))
            scored.append((pts, grade, t))
        scored.sort(key=lambda x: -x[0])
        names = self.s.forum_names()
        out = []
        for pts, grade, t in scored[:TOP]:
            bits = [f"por {t['author'] or '?'}", names.get(t["forum_id"], "")]
            if grade:
                bits.append(f"nota {grade}")
            bits.append(f"{t['others']} pessoas · {t['views'] or 0} visitas")
            out.append(((t["title"] or ""), topic_href(t["ref"]), round(pts), " · ".join(bits)))
        return out

    def _terms(self, titles: list[tuple], stop: set) -> list[tuple[str, int, tuple]]:
        """Conta, por tópico, palavras e pares de palavras dos títulos.
        titles: [(título, ref, visitas)]. Devolve [(termo, nº de tópicos, exemplo)]."""
        uni, bi = Counter(), Counter()
        disp: dict = {}
        best: dict = {}
        tokenized = []
        vocab = set()
        for title, ref, views in titles:
            words = re.findall(r"[0-9a-zà-ÿ]+(?:[_+#][0-9a-zà-ÿ]+)*", (title or "").lower())
            tokenized.append((title, ref, views, words))
            vocab.update(fold(w) for w in words)

        def sing(f: str) -> str:  # "sprites" conta como "sprite"
            if len(f) > 4 and f.endswith("s") and f[:-1] in vocab:
                return f[:-1]
            if len(f) > 5 and f.endswith("es") and f[:-2] in vocab:
                return f[:-2]
            return f
        for title, ref, views, words in tokenized:
            folded = [sing(fold(w)) for w in words]
            seen = set()
            for w, f in zip(words, folded):
                if len(f) < 3 or f in stop or f.isdigit():
                    continue
                seen.add(f)
                disp.setdefault(f, Counter())[w] += 1
            pairs = set()
            for i in range(len(folded) - 1):
                a, b = folded[i], folded[i + 1]
                if a in stop or b in stop or len(a) < 3 or len(b) < 3 or a.isdigit() or b.isdigit():
                    continue
                pairs.add(f"{a} {b}")
                disp.setdefault(f"{a} {b}", Counter())[f"{words[i]} {words[i + 1]}"] += 1
            for term in seen | pairs:
                (bi if " " in term else uni)[term] += 1
                if term not in best or (views or 0) > best[term][2]:
                    best[term] = (title, ref, views or 0)
        cands = [(t, n) for t, n in uni.items()] + [(t, n) for t, n in bi.items() if n >= 3]
        cands.sort(key=lambda x: -x[1])
        out = []
        for term, n in cands:
            if " " not in term and any(term in b.split() and bi[b] >= 0.6 * n for b in bi):
                continue  # palavra que quase sempre aparece no mesmo par: fica o par
            out.append((disp[term].most_common(1)[0][0], n, best[term]))
            if len(out) == TOP:
                break
        return out

    def r_subjects(self, ctx):
        dw, args = self.dwhere("created", ctx)
        titles = self.q(f"SELECT title, ref, views FROM s.tm WHERE forum_id <> 24 {dw}", *args)
        return [(term, self.tsearch(ctx, q=term, onde="titulo"), n, f'ex.: “{ex[0]}”')
                for term, n, ex in self._terms(titles, STOP)]

    def r_questions(self, ctx):
        dw, args = self.dwhere("created", ctx)
        titles = self.q(f"SELECT title, ref, views FROM s.tm WHERE forum_id = {QA_FORUM} {dw}", *args)
        out = []
        for term, n, ex in self._terms(titles, QA_STOP):
            out.append((term, self.tsearch(ctx, q=term, onde="titulo", forum=QA_FORUM), n,
                        f'mais vista: <a href="{e(topic_href(ex[1]))}">{e(ex[0])}</a>'))
        return out

    def r_functions(self, ctx):
        dw, args = self.dwhere("p.date", ctx)
        rows = self.q(f"""SELECT f.fn, COUNT(DISTINCT f.post_id) n, COUNT(DISTINCT p.topic_ref) t
                          FROM s.fn_mentions f JOIN s.pm p ON p.id = f.post_id
                          WHERE 1=1 {dw} GROUP BY f.fn ORDER BY n DESC LIMIT {TOP}""", *args)
        return [(r[0] + "()", self.msearch(ctx, q=r[0], campo="texto"), r[1], f"em {r[2]} tópicos") for r in rows]

    def r_tools(self, ctx):
        dw, args = self.dwhere("p.date", ctx)
        out = []
        for label, query in TOOLS:
            n = self.q(f"""SELECT COUNT(*) FROM s.post_fts JOIN s.pm p ON p.id = post_fts.rowid
                           WHERE post_fts MATCH ? {dw}""", "{title body} : (" + query + ")", *args)[0][0]
            if n:
                out.append((label, self.msearch(ctx, q=query, campo="ambos"), n, ""))
        out.sort(key=lambda x: -x[2])
        return out[:TOP]

    def _topics(self, ctx, where: str, value: str):
        """Top tópicos por uma expressão `value` (decrescente)."""
        dw, args = self.dwhere("t.created", ctx)
        return self.q(f"""SELECT {self.TOPIC_COLS}, {value} AS v FROM s.tm t
                          WHERE {where} {dw} ORDER BY v DESC, t.views DESC LIMIT {TOP}""", *args)

    def r_most_viewed(self, ctx):
        return self.topic_rows(self._topics(ctx, "t.views IS NOT NULL AND t.forum_id <> 24", "t.views"))

    def r_biggest(self, ctx):
        return self.topic_rows(self._topics(ctx, "t.forum_id <> 24", "t.n_participants"),
                               detail=lambda t: f"por {t['author'] or '?'} · {t['replies']} respostas")

    def r_most_replied(self, ctx):
        return self.topic_rows(self._topics(ctx, "t.forum_id <> 24", "t.replies"))

    def r_longest(self, ctx):
        rows = self._topics(ctx, "t.n_posts >= 10 AND t.created IS NOT NULL AND t.last_date IS NOT NULL",
                            "julianday(t.last_date) - julianday(t.created)")
        return self.topic_rows(rows, value_fmt=lambda t: round(t["v"] / 365.25, 1),
                               detail=lambda t: f"{(t['created'] or '')[:4]} → {t['replies']} respostas")

    def r_polls(self, ctx):
        return self.topic_rows(self._topics(ctx, "t.has_poll = 1 AND t.poll_votes IS NOT NULL", "t.poll_votes"))

    def r_tutorials(self, ctx):
        names = self.s.forum_names()
        return self.topic_rows(self._topics(ctx, f"t.forum_id IN {TUTORIAL_FORUMS} AND t.views IS NOT NULL",
                                            "t.views"),
                               detail=lambda t: f"por {t['author'] or '?'} · {names.get(t['forum_id'], '')}")

    def r_contests(self, ctx):
        return self.topic_rows(self._topics(ctx, f"t.forum_id IN {CONTEST_FORUMS}", "t.n_participants"))

    def r_liked_posts(self, ctx):
        dw, args = self.dwhere("p.date", ctx)
        rows = self.q(f"""SELECT p.id, p.topic_ref, p.position, p.author, p.likes, p.date, t.title
                          FROM s.pm p LEFT JOIN s.tm t ON t.ref = p.topic_ref
                          WHERE p.likes > 0 {dw} ORDER BY p.likes DESC, p.date DESC LIMIT {TOP}""", *args)
        from .app import POSTS_PER_PAGE
        return [(f"{r['author']} em “{r['title']}”",
                 f"{topic_href(r['topic_ref'])}?p={(r['position'] or 0) // POSTS_PER_PAGE + 1}#p{r['id']}",
                 r["likes"], fmt_date(r["date"])[:10]) for r in rows]

    def r_forums(self, ctx):
        dw, args = self.dwhere("date", ctx)
        names = self.s.forum_names()
        rows = self.q(f"""SELECT forum_id, COUNT(*) n, COUNT(DISTINCT author_{ctx['a']}) d FROM s.pm
                          WHERE forum_id IS NOT NULL {dw} GROUP BY 1 ORDER BY n DESC LIMIT {TOP}""", *args)
        return [(names.get(r[0], "?"), f"/f/{r[0]}", r[1], f"{r[2]} pessoas") for r in rows]

    def r_years(self, ctx):
        dw, args = self.dwhere("date", ctx)
        rows = self.q(f"""SELECT substr(date, 1, 4) y, COUNT(*) n, COUNT(DISTINCT author_{ctx['a']}) d,
                                 COUNT(DISTINCT topic_ref) t
                          FROM s.pm WHERE date IS NOT NULL {dw} GROUP BY 1 ORDER BY n DESC LIMIT {TOP}""", *args)
        return [(r[0], self.msearch(ctx, ano=r[0]), r[1], f"{r[2]} pessoas · {r[3]} tópicos") for r in rows]

    # ============================================================== página
    def compute(self, P: Params) -> dict:
        d1, d2 = self.period(P)
        mode = join_mode(P)
        try:
            stamp = self.s.meta("stamp")
        except Exception:
            stamp = None
        ck = (stamp, d1, d2, mode, self.local_version())
        with self._lock:
            if ck in self._cache:
                self._cache.move_to_end(ck)
                return self._cache[ck]
        ctx = {"d1": d1, "d2": d2, "de": P.int("de", 1990, 2100), "ate": P.int("ate", 1990, 2100),
               "mode": mode, "a": SUFFIX[mode], "labels": {}}
        if ctx["de"] and ctx["ate"] and ctx["de"] > ctx["ate"]:
            ctx["de"], ctx["ate"] = ctx["ate"], ctx["de"]
        results, timing = {}, {}
        for r in self.items:
            t0 = time.time()
            try:
                results[r.key] = r.fn(ctx)
            except Exception as exc:  # um ranking com problema não derruba a página
                results[r.key] = exc
            timing[r.key] = time.time() - t0
        data = {"results": results, "timing": timing, "ctx": ctx}
        with self._lock:
            self._cache[ck] = data
            while len(self._cache) > 24:
                self._cache.popitem(last=False)
        return data

    def warm(self):
        """Calcula a visão padrão em segundo plano, para a primeira visita ser rápida."""
        def run():
            try:
                self.compute(Params({}))
            except Exception as exc:
                print(f"aviso: rankings: {exc}")
        threading.Thread(target=run, daemon=True).start()

    def local_version(self) -> int:
        """Muda quando entra mensagem nova (as mensagens locais entram nos rankings)."""
        try:
            return self.db.execute("SELECT COALESCE(MAX(id), 0) FROM loc.new_posts").fetchone()[0]
        except Exception:
            return 0

    def page(self, qs: dict) -> str:
        P = Params(qs)
        if not self.s.ready():
            return layout("Rankings", '<div class="notice">O índice de busca ainda não foi construído.</div>',
                          [("Índice", "/"), ("Rankings", None)])
        data = self.compute(P)
        ctx = data["ctx"]
        years = [y for y in (self.s.meta("years") or []) if y and y.isdigit()]
        de, ate = str(ctx["de"] or ""), str(ctx["ate"] or "")
        yopts = lambda cur, empty: opt("", empty, cur) + "".join(opt(y, y, cur) for y in years)
        jm = "".join(opt(k, lbl, ctx["mode"]) for k, lbl in JOIN_MODES)
        quick = " ".join(
            f'<a href="/rankings?{e(urlencode(dict(de=a, ate=b)))}">{lbl}</a>'
            for lbl, a, b in (("2008–2010", 2008, 2010), ("2011–2013", 2011, 2013), ("2014–2019", 2014, 2019),
                              ("2020 em diante", 2020, years[-1] if years else 2026)))
        form = f"""<form class="rk-form" action="/rankings">
<label>De <select name="de">{yopts(de, 'início')}</select></label>
<label>até <select name="ate">{yopts(ate, 'hoje')}</select></label>
<label>Juntar nomes <select name="juntar">{jm}</select></label><button>Atualizar</button>
<span class="small">Épocas: {quick} · <a href="/rankings">tudo</a></span></form>"""
        groups: dict = {}
        for r in self.items:
            groups.setdefault(r.group, []).append(r)
        nav = " · ".join(f'<a href="#{r.key}">{e(r.title)}</a>' for r in self.items)
        sections = []
        for g, items in groups.items():
            cards = "".join(self.card(r, data["results"][r.key], ctx) for r in items)
            sections.append(f'<h2 class="rk-group" id="g-{e(fold(g))}">{e(g)}</h2><div class="rk-grid">{cards}</div>')
        period = ("todo o período" if not (ctx["de"] or ctx["ate"])
                  else f"{ctx['de'] or 'início'} a {ctx['ate'] or 'hoje'}")
        intro = (f'<p class="small">Top {TOP} de {e(period)}. Nomes da mesma pessoa '
                 f'{"juntos" if ctx["mode"] == "pessoa" else ("juntos por conta" if ctx["mode"] == "conta" else "separados")}. '
                 f'Cada lista explica como foi calculada; os números levam às mensagens ou tópicos.</p>')
        body = CSS + f'<h1 class="rk-title">Rankings</h1>{form}{intro}<div class="rk-nav small">{nav}</div>{"".join(sections)}'
        return layout("Rankings", body, [("Índice", "/"), ("Rankings", None)])

    def card(self, r: Ranking, rows, ctx) -> str:
        if isinstance(rows, Exception):
            content = f'<div class="small err">Não foi possível calcular: {e(rows)}</div>'
        elif not rows:
            content = '<div class="small" style="padding:10px 12px">Sem dados no período.</div>'
        else:
            top = max((x[2] for x in rows if isinstance(x[2], (int, float))), default=0) or 1
            lis = []
            for i, (label, link, value, detail) in enumerate(rows, 1):
                bar = (f'<i style="width:{max(2, 100 * value / top):.0f}%"></i>'
                       if isinstance(value, (int, float)) and value > 0 else "")
                val = f"{value:,}".replace(",", ".") if isinstance(value, int) else (
                    f"{value:.1f}".replace(".", ",") if isinstance(value, float) else e(value))
                det = detail if detail.startswith("mais vista:") else e(detail)
                lis.append(f"""<li><span class="rk-n">{i}</span><div class="rk-main"><a href="{e(link)}">{e(label)}</a>
<div class="small">{det}</div><div class="rk-bar">{bar}</div></div><span class="rk-v">{val}</span></li>""")
            content = f'<ol class="rk">{"".join(lis)}</ol>'
        flag = "" if r.period else ' <span class="tag">perfil</span>'
        unit = f' <span class="rk-unit">{e(r.unit)}</span>' if r.unit else ""
        return (f'<section class="box rk-card" id="{r.key}"><h2>{e(r.title)}{flag}{unit}</h2>'
                f'<div class="rk-crit small">{e(r.criterion)}</div>{content}</section>')


CSS = """<style>
.rk-title{margin:6px 0 10px;font-size:24px}
.rk-form{display:flex;gap:8px 14px;flex-wrap:wrap;align-items:center;background:var(--card);border:1px solid var(--line);
border-radius:6px;padding:10px 12px;margin-bottom:8px}
.rk-nav{line-height:1.9;margin:0 0 6px}
.rk-group{font-size:19px;margin:22px 0 10px;padding-bottom:4px;border-bottom:2px solid var(--head2)}
.rk-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,340px),1fr));gap:0 16px;align-items:start}
.rk-card h2{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.rk-card h2 .tag{color:var(--muted)}
.rk-unit{margin-left:auto;font-weight:400;font-size:12px;opacity:.85}
.rk-crit{padding:8px 12px;border-bottom:1px solid var(--line);background:var(--alt)}
ol.rk{list-style:none;margin:0;padding:4px 0}
ol.rk li{display:flex;gap:10px;align-items:flex-start;padding:6px 12px;border-bottom:1px solid var(--line)}
ol.rk li:last-child{border-bottom:0}
.rk-n{width:22px;flex:none;text-align:right;font-weight:700;color:var(--muted)}
ol.rk li:nth-child(1) .rk-n{color:#c99700}ol.rk li:nth-child(2) .rk-n{color:#8a99a8}ol.rk li:nth-child(3) .rk-n{color:#b0703a}
.rk-main{flex:1;min-width:0;overflow-wrap:anywhere}
.rk-bar{height:4px;margin-top:3px}.rk-bar i{display:block;height:100%;background:var(--head2);opacity:.55;border-radius:2px}
.rk-v{flex:none;font-weight:700;font-variant-numeric:tabular-nums}
.err{color:#b3261e;padding:10px 12px}
</style>"""
