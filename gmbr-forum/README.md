# gmbr-forum

Fórum mínimo com o arquivo do **gmbr.forumeiros.com**, montado em cima do banco
SQLite gerado pelo [`gmbr-scraper`](../../tree/claude/gmbr-forum-scraper-tdm143/gmbr-scraper).
Só usa a biblioteca padrão do Python (3.9+), sem dependências.

- **Índice** com fóruns, subfóruns, contadores e última mensagem.
- **Fórum** com a lista de tópicos (fixos primeiro, depois por atividade).
- **Tópico** com mensagens, perfil do autor, assinatura e edições. Links internos
  do gmbr (`/t123-...`) apontam para o próprio fórum local.
- **Membros**, **perfil** com as mensagens arquivadas, **busca** e **recentes**.
- **Mensagens novas**: dá para criar tópicos e responder (inclusive em tópicos
  do arquivo). Ficam num banco separado (`data/local.sqlite`); o banco do
  scraper é aberto **só para leitura** e pode ser trocado por um backup mais
  novo sem perder nada.

Tópicos que o scraper só listou (sem mensagens ainda) aparecem como
"não arquivado", com link para o original.

## Rodar

```bash
cd gmbr-forum
# 1. Restaurar o backup da branch claude/gmbr-data (precisa da senha do backup)
echo -n 'SENHA' > data/backup.key        # ou: export GMBR_BACKUP_KEY='SENHA'
scripts/restore_backup.sh                # → data/gmbr.sqlite
# 2. Servir
python3 -m gmbr_forum                    # http://127.0.0.1:8000/
python3 -m gmbr_forum --host 0.0.0.0 --port 8080 --db outro.sqlite --local minhas.sqlite
```

Rode `scripts/restore_backup.sh` de novo para pegar um backup mais recente
enquanto o scraper continua trabalhando. Também dá para apontar `--db`
direto para `gmbr-scraper/data/gmbr.sqlite`.

`data/` não é versionado. **Nunca** faça commit da senha nem do banco
descriptografado.

## Segurança

- O HTML raspado passa por uma lista de tags e atributos permitidos
  (`gmbr_forum/sanitize.py`): sem scripts, iframes, eventos `on*`,
  `javascript:` nem `url()` em estilos.
- Mensagens novas são texto puro, com HTML escapado.
- As respostas saem com Content-Security-Policy sem scripts.
- Não há login: o nome é livre e fica salvo num cookie. Antes de expor o
  fórum na internet, coloque-o atrás de autenticação ou de um proxy com
  limite de requisições.

## Testes

```bash
python3 -m unittest discover -s tests
```
