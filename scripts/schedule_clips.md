# Agendamento do canal de cortes (cron do WSL)

O lote diário (`python -m app.clips daily`) roda pelo cron do WSL, por meio de
`scripts/run_clips_daily.sh`. O Agendador de Tarefas do Windows não é usado.

## Como funciona

O cron chama o script a cada 30 minutos. O script decide se roda:

| Regra | Como |
|---|---|
| Não antes das 08h de Brasília | `TZ=America/Sao_Paulo` dentro do script; o fuso do WSL (aqui `America/Cuiaba`, −04) não importa |
| Uma vez por dia | execução bem-sucedida grava `data/clips-cron/<dia>.done` |
| No máximo 4 tentativas com falha por dia | `data/clips-cron/<dia>.attempts`; muda com `CLIPS_CRON_MAX_ATTEMPTS`. Cada tentativa pode classificar clips novos na Gemini, que é cobrada |
| Nunca duas ao mesmo tempo | `flock` em `data/clips-cron/lock` |
| Dia perdido é recuperado | se o WSL estava desligado às 08h, roda no primeiro tique depois que ele ligar, enquanto ainda for o mesmo dia em Brasília |

"Falha" é código de saída diferente de 0: erro na coleta ou na classificação,
ou uma edição que falhou no render. Formato sem material suficiente não é
falha; a edição só não é criada.

Log de cada dia: `data/clips-cron/logs/<dia>.log`.

## Pré-requisitos (já atendidos nesta máquina)

- `/etc/wsl.conf` com `[boot]` / `systemd=true`.
- `systemctl is-active cron` → `active`; `systemctl is-enabled cron` → `enabled`.
- Um job do cron, lançado pelo systemd, executa o `.venv/Scripts/python.exe` do
  Windows normalmente (testado em 17/11/2026 com `PATH=/usr/bin:/bin`).

## Instalar

```bash
(crontab -l 2>/dev/null; echo "*/30 * * * * /bin/bash /mnt/c/Users/04766246144/Documents/automatizar-videos-com-ia/scripts/run_clips_daily.sh") | crontab -
crontab -l
```

O script é chamado via `/bin/bash` porque o bit de execução não se mantém em
`/mnt/c`.

## O WSL precisa estar rodando

O cron só dispara com a distro ligada. O WSL desliga a distro quando nenhuma
sessão está aberta, e serviços do systemd como o cron não contam como
sessão. Na prática:

- deixe um terminal do WSL aberto (uma aba do Windows Terminal basta);
- se o WSL for aberto depois das 08h, o lote roda em até 30 minutos.

Para conferir se o cron está vivo: `systemctl status cron`.

## Operação

```bash
# Ver o log de hoje
cat data/clips-cron/logs/$(TZ=America/Sao_Paulo date +%F).log

# Rodar de novo hoje (depois de corrigir o que falhou)
rm -f data/clips-cron/$(TZ=America/Sao_Paulo date +%F).{done,attempts}
bash scripts/run_clips_daily.sh

# Argumentos extras vão para o `daily`
bash scripts/run_clips_daily.sh --skip-classify

# Desligar o agendamento
crontab -e   # apague a linha do run_clips_daily.sh
```

Rodar de novo é seguro: o pipeline retoma de onde parou e não troca os clips
de uma edição já criada (ver `app/clips/pipeline.py`).

## Dry run antes do lançamento

O dry run não passa pelo cron. Rode na mão, com o jogo e os formatos forçados:

```bash
CLIPS_GAME_NAME="Grand Theft Auto V" .venv/Scripts/python.exe -m app.clips daily \
  --date 2026-11-18 --dry-run --format top_daily --format funny_daily
```

Variáveis do shell do WSL não chegam ao `python.exe` do Windows sem
`WSLENV`; para o comando acima funcionar, use
`WSLENV=CLIPS_GAME_NAME CLIPS_GAME_NAME="Grand Theft Auto V" ...` ou ponha
`CLIPS_GAME_NAME` no `.env`.
