# Backup do banco do gmbr-scraper

Cópia criptografada (AES-256-CBC, PBKDF2) de `gmbr-scraper/data/gmbr.sqlite`.
Gerada em: 2026-11-21 10:08:56

```
Fóruns:            25
Tópicos listados:  31397
Tópicos completos: 31397
Mensagens:         225003
Membros:           17376
```

Restaurar (precisa da senha em `backup.key`):

```bash
cat gmbr.sqlite.gz.enc.part-* \
  | openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass file:backup.key \
  | gunzip > gmbr.sqlite
```
