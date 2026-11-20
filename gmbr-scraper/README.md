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

Tabelas (e arquivos do `export`):

- **forums** — id, nome, url, `parent_id` (subfóruns).
- **topics** — id, fórum, título, descrição, autor, respostas, visualizações,
  última mensagem (id/autor/data), páginas e mensagens raspadas.
- **posts** — id, tópico, posição no tópico, título, `author_name`, data
  (`date_raw` original e `date` em ISO), edição (contagem e data), curtidas
  (`liked_by`), reputação, conteúdo em texto e HTML, assinatura.
- **members** — nome, avatar, grupo (`group_class` + `group_name`, ex.:
  Administradores), cor, data de inscrição, reputação, nº de mensagens do
  perfil, site, assinatura, `extra_fields` (Medalhas, Tutoriais, Moedas,
  Ouro/Prata/Bronze, Insígnias, Notas…), `is_guest`. O export acrescenta
  estatísticas calculadas: `posts_found`, `topics_participated`,
  `topics_started`, `first_post_date`, `last_post_date`.
  Com `memberlist`/`profiles` (abaixo) também: `in_memberlist`, `humor`,
  `last_visit`, `profile_fields` (JSON com todos os campos visíveis de `/uNN`:
  idade, "Quem sou eu", plataformas, projetos, redes sociais, notas etc.),
  `profile_groups` (todos os grupos do membro) e `profile_scraped_at`.
- **topics.prefix / topic_type** — prefixo da listagem (ex.: `[Votação]`) e
  ícone (`sticky_read`, `announce_read`, `topic_read_locked`, `topic_read_hot`…).
- **polls / poll_options** — enquetes: pergunta, total de votos, encerrada,
  opções com votos e percentual.
- **groups** — legenda de grupos do índice.

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
  bloqueia `/u`). Por isso os membros são identificados pelo **nome**, e os
  dados de perfil vêm do bloco ao lado das mensagens — que mostra o estado
  **atual** do membro. Membros que nunca postaram não aparecem.
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
  (`is_guest = 1`) — todas agrupadas sob esse nome.
- Se um membro mudou de nome, as mensagens antigas aparecem com o nome novo
  (é como o fórum renderiza).
- Datas estão no horário configurado no fórum; "Hoje/Ontem" são convertidas
  usando a data do momento do crawl.
- Blocos "Conteúdo patrocinado" (anúncios) são ignorados.

## Testes

```bash
python -m unittest discover -s tests
```
