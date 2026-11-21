# gmbr-scraper

Scraper do fórum **gmbr.forumeiros.com** (Forumeiros/phpBB, tema prosilver).

Percorre **fórum → tópico → página** (50 mensagens por página), grava cada
mensagem com autor e metadados e monta a **lista de membros** a partir do
perfil exibido ao lado de cada mensagem (avatar, grupo, data de inscrição,
reputação, nº de mensagens, campos extras como Medalhas/Moedas, site,
assinatura).

Tudo vai para um banco **SQLite**, então dá para interromper (Ctrl+C) e
rodar de novo: o crawl continua de onde parou.

## Instalação

```bash
cd gmbr-scraper
pip install -r requirements.txt
```

## Uso

```bash
# Fórum inteiro (descobre fóruns/subfóruns, lista tópicos, raspa mensagens)
python -m gmbr_scraper crawl

# Só alguns fóruns (IDs do link: /f12-engines → 12)
python -m gmbr_scraper crawl --forum 12 --forum 55

# Só um tópico (/t32892-retorno-da-gmbr → 32892)
python -m gmbr_scraper crawl --topic 32892

# Teste rápido
python -m gmbr_scraper crawl --forum 2 --max-forum-pages 1 --max-topics 5

# Resumo, export JSON/CSV e download de avatares
python -m gmbr_scraper stats
python -m gmbr_scraper export --out export --with-content
python -m gmbr_scraper avatars --out avatars
```

Opções úteis do `crawl`:

| opção | efeito |
|---|---|
| `--delay 1.0` | intervalo entre requisições (seja gentil com o servidor) |
| `--skip-listing` | não relista fóruns; só raspa tópicos pendentes já no banco |
| `--refresh` | raspa de novo todos os tópicos desde a 1ª página |
| `--full-listing` | lista todas as páginas dos fóruns, sem parada incremental |
| `--cookie "..."` / `GMBR_COOKIE` | cookie de uma sessão logada (ver abaixo) |
| `GMBR_USER` + `GMBR_PASS` | faz login antes de raspar (ver abaixo) |
| `--db arquivo.sqlite` | banco a usar (opção global, vem antes do comando) |

### Execuções incrementais

- **Listagem:** as listas de tópicos vêm ordenadas pela última mensagem.
  Depois que um fórum foi listado por completo uma vez, as próximas execuções
  param na primeira página sem tópicos novos nem respostas novas — em geral
  1 ou 2 páginas por fórum. Use `--full-listing` para forçar a listagem
  completa (ex.: para pegar tópicos movidos de outro fórum).
- **Listagem interrompida:** o banco guarda até onde chegou
  (`forums.listing_next_start`); ao retomar, relê o início para pegar
  novidades e pula direto para esse ponto.
- **Mensagens:** só são raspados os tópicos **não concluídos** ou cujo número
  de respostas **mudou**. Nesses, o crawl recomeça pela última página já
  vista (onde entram as mensagens novas).

## Dados gerados

Schema versão 4 (`PRAGMA user_version = 4`). Bancos das versões 1 a 3 são
migrados automaticamente ao abrir (uma transação; se falhar, nada muda).

**Regra das contagens:** nada que dependa de outra tabela é guardado como
fonte de verdade. Mensagens por tópico, tópicos por fórum, mensagens por
membro, votos de enquete, curtidas etc. vêm das views `v_*`, contadas na hora
(com índices). Colunas `*_forum` guardam o número que o **fórum** exibe —
usadas no crawl incremental e para conferência (`v_count_checks`). Colunas
"informativas" (`topics.pages`, `topics.posts_scraped`, `topic_pages.post_count`,
`forums.topics_listed`) só registram o que a raspagem viu; nada depende delas.

Tabelas:

- **forums** — id, nome, url, `parent_id` (subfóruns).
- **topics** — id, fórum, título, descrição, prefixo (`[Votação]`), ícone
  (`sticky_read`, `topic_read_locked`…), `replies_forum`, `views_forum`,
  `last_post_id_forum`; controle do crawl (`completed`, `scraped_replies`).
- **posts** — id, tópico, posição (0 = mensagem inicial), título,
  `author_id` (→ members), `author_name` (nome exibido; convidados só têm este),
  `author_id_inferred` (1 = ligado ao membro pelo nome, porque a mensagem não
  tinha link de perfil), datas, edição, `dislikes`, conteúdo, `has_signature`
  (a mensagem exibia a assinatura do membro). A assinatura é uma só por membro;
  o fórum às vezes pendura numa mensagem a assinatura de outra do mesmo tópico
  (caso único: mensagem 52760, de loupa, exibe a de romulo_rla), o que é ignorado.
- **post_likes** — (`post_id`, `liker_name`, `user_id`): quem curtiu cada mensagem.
- **members** — chave `user_id` (`/uNN`); nome atual, avatar, `group_id` (grupo
  que dá a cor ao nome), cor, rank, inscrição, última visita, reputação,
  `post_count_forum`, site, humor, idade, `about_me`, `specialty`, `platforms`,
  `projects`, `infractions`, `grades_received`, `nick_history`,
  `signature_html` (uma por membro; sem os `alt` com o título do tópico que o
  fórum injeta nas imagens); JSON brutos
  `profile_fields` (página `/uNN`), `sidebar_fields`/`sidebar_images` (bloco ao
  lado das mensagens).
- **member_groups** — (`user_id`, `group_id`): todos os grupos de cada membro.
- **member_awards** — (`user_id`, `award`, `quantity`): Medalhas, Tutoriais,
  Moedas, Ouro, Prata, Bronze, Insígnias (só quantidades ≠ 0).
- **groups** — id (`/gNN`), nome, cor.
- **polls / poll_options** — pergunta, encerrada; opções com votos.
- **topic_pages** — páginas já raspadas (retomada do crawl).
- **post_author_guesses** — autores prováveis das mensagens de contas apagadas
  (exibidas como "Convidado"/"Convidad"), uma linha por (mensagem, nome,
  método), com evidência em JSON. Não altera `posts.author_id`. Recalcule com
  `python -m gmbr_scraper guess-authors`. `user_id`/`user_link` ligam o nome a
  uma conta, decidido só por citação ou edição confirmada: `conta` (já existia na data da
  mensagem) ou `recadastro` (a única conta com o nome foi criada depois — a
  pessoa voltou com conta nova após a antiga ser apagada). Métodos:

  | Método | Confiança | Pista |
  |---|---|---|
  | `citacao` | alta | resposta posterior cita o texto como "X escreveu:" |
  | `edicao` | média se outra pista aponta o mesmo nome; senão baixa | "Última edição por X" — X pode ter sido moderador (conta apagada), então editores que editaram mensagem de outro membro ou que contradizem uma citação são descartados |
  | `mencao` | baixa | a resposta seguinte chama X pelo nome (~65% de acerto medido contra citação/edição) |
  | `continuidade` | baixa | outra mensagem de convidado do tópico foi atribuída a X (~30%) |
  | `periodo` | baixa | só se não há outra pista: até 3 nomes identificados (citação/edição confirmada) em mensagens de convidado de ±15 dias, em outros tópicos, peso dobrado no mesmo fórum; exclui quem já tinha conta na data. 1º candidato acerta ~57%, um dos 3 ~87% (validado escondendo citações conhecidas) |

Todas as relações têm chave estrangeira (verificada no commit).

Views:

| View | Conteúdo |
|---|---|
| `v_topic_stats` | mensagens, respostas, páginas, participantes, autor e 1ª/última mensagem, curtidas |
| `v_forum_stats` | tópicos, mensagens e última mensagem por fórum |
| `v_member_stats` | mensagens, tópicos iniciados/participados, 1ª/última mensagem, curtidas recebidas/dadas |
| `v_guest_authors` | autores sem conta (convidados, contas apagadas) |
| `v_post_author_best` | sugestão mais provável por mensagem (soma dos scores) e nº de candidatos |
| `v_poll_results` / `v_poll_options` | total de votos e percentuais |
| `v_group_members` | membros por grupo |
| `v_count_checks` | onde o número do fórum difere do banco |

O `export` gera JSON/CSV de membros, fóruns, tópicos, mensagens, enquetes e
autores convidados e sugestões de autor, já com as contagens das views.

### Passada complementar (com login)

```bash
python -m gmbr_scraper memberlist             # todos os membros registrados
python -m gmbr_scraper polls                  # enquetes dos tópicos [Votação]
python -m gmbr_scraper profiles --shard 0/10  # páginas /uNN (mais ativos primeiro)
scripts/run_extras.sh 10 20                   # tudo isso em 10 processos
scripts/run_all.sh 10 20                      # espera a raspagem, depois extras
```

Dados de contato (e-mail, MP) e o formulário de edição do perfil não são lidos.

## Observações / limitações

- Para visitantes, o fórum **não expõe páginas de perfil** (`/u123`,
  `/memberlist` e `/groups` redirecionam para o login, e o `robots.txt`
  bloqueia `/u`). Os membros são identificados pelo `user_id` desse link,
  então a raspagem de membros exige login. O bloco ao lado das mensagens
  mostra o estado **atual** do membro.
- **Com login**, as mensagens trazem o link `/uNN` e o scraper preenche
  `author_id` nas mensagens e `user_id`/`profile_url` nos membros (o scraper
  só acessa as páginas `/u` nos comandos `profiles`/`memberlist`). Duas formas:

  ```bash
  GMBR_USER=usuario GMBR_PASS=senha python -m gmbr_scraper crawl
  GMBR_COOKIE="fa_gmbr_forumeiros_com_data=...; fa_gmbr_forumeiros_com_sid=..." \
      python -m gmbr_scraper crawl
  ```

  Nunca coloque credenciais/cookies em arquivos versionados.
- Mensagens de visitantes e de contas excluídas aparecem como **Convidado**
  (ou com o nome digitado), sem `author_id`; não viram membros
  (veja `v_guest_authors`). Sem login o fórum não mostra `/uNN`, então os
  membros só são gravados em raspagens com login.
- Se um membro mudou de nome, as mensagens antigas aparecem com o nome novo
  (é como o fórum renderiza).
- Datas estão no horário configurado no fórum; "Hoje/Ontem" são convertidas
  usando a data do momento do crawl.
- Blocos "Conteúdo patrocinado" (anúncios) são ignorados.

## Testes

```bash
python -m unittest discover -s tests
```
