# Multi-worker com claim atômico

**Data**: 2026-05-03
**Status**: aprovado

## Problema

O worker atual é single-process: um loop em `app/worker.py` chama
`JobQueueManager.get_next_pending` e processa um job de cada vez. Com N contas
YouTube cadastradas e jobs empilhados em cada conta, o sistema processa em
série mesmo havendo paralelismo possível por conta (quota isolada, credenciais
isoladas, OAuth isolado).

Além disso, `get_next_pending` + `update_status` é um ciclo de leitura-depois-
escrita em dois passos sem trava. Se dois workers rodarem em paralelo, ambos
podem ler o mesmo job `pending` antes que qualquer um faça o `UPDATE`, e o
pipeline roda duplicado (custo de API duplicado, corrida na escrita do
`status.json`).

## Objetivo

Permitir rodar um worker por conta YouTube em paralelo, mantendo:

- **Isolamento por conta**: nunca dois pipelines concorrentes na mesma conta
  (evita burst de 4 uploads seguidos, evita colidir credenciais OAuth em
  `os.environ`).
- **Sem duplicação**: um mesmo job nunca é pego por dois workers.
- **Resiliência a crash**: se um worker crashar no meio do pipeline, o job
  retorna ao pool da sua conta em vez de ficar travado em `running` pra sempre.

## Arquitetura

### Modelo: um worker por conta

Cada worker é um processo Python separado, iniciado com
`python -m app.worker --account-id N`. Esse worker SÓ pega jobs com
`account_id = N`. Paralelismo vem de ter N workers rodando simultaneamente,
um pra cada conta ativa.

**Por quê?** O gargalo real é a quota da YouTube Data API
(1.600 unidades/upload, 10k/dia/conta ⇒ ~6 uploads/dia/conta). Paralelizar
entre contas multiplica throughput; paralelizar dentro de uma conta gera
burst de uploads com zero ganho (mesma quota total) e risco de corrida em
estado compartilhado (`os.environ` com credenciais OAuth manipulado em
`pipeline_runner.py:108-110`).

Pool compartilhado de workers genéricos foi descartado por permitir
exatamente esse burst.

### Claim atômico

`JobQueueManager.claim_next_pending(account_id)` substitui o ciclo atual.
Usa um único `UPDATE ... WHERE ... RETURNING`:

```sql
UPDATE jobs
SET status = 'running',
    started_at = CURRENT_TIMESTAMP
WHERE id = (
    SELECT id FROM jobs
    WHERE status = 'pending' AND account_id = :account_id
    ORDER BY priority DESC, created_at ASC
    LIMIT 1
)
AND status = 'pending'
RETURNING *;
```

SQLite serializa writers: se dois workers rodarem esse `UPDATE`
simultaneamente, um ganha e o outro recebe zero linhas. A cláusula redundante
`AND status = 'pending'` no outer UPDATE protege contra o caso raro em que o
status mudou entre o SELECT interno e o UPDATE externo.

**Compat**: `get_next_pending` e `update_status` são mantidos — o dashboard e
a API REST podem continuar chamando. Só o worker migra pra `claim_next_pending`.

### WAL mode e busy_timeout

Sem WAL, SQLite bloqueia o banco inteiro em cada escrita; dois workers brigam
constantemente e um deles recebe `database is locked` com frequência.

Em `database.py`, habilitar via `event.listen(engine, "connect", ...)`:

- `PRAGMA journal_mode = WAL` — leitores não bloqueiam escritor; múltiplos
  writers ainda serializam, mas sem bloquear leituras.
- `PRAGMA busy_timeout = 5000` — aguarda até 5s antes de devolver
  `SQLITE_BUSY`. Evita exceções em contenção momentânea.

WAL é aplicado só a URLs `sqlite*`; outros drivers (se existirem) passam
batido.

### Recuperação de órfãos no startup

Se um worker crashar com um job em `running`, o job fica travado: nenhum
outro worker pega (status não é `pending`) e o próprio worker ao reiniciar
também ignora. Solução: ao iniciar, cada worker executa

```python
job_queue.reset_orphan_running(account_id=N)
```

que faz `UPDATE jobs SET status='pending', started_at=NULL WHERE account_id=N
AND status='running'`.

Isso é seguro porque o worker-por-conta é autoridade exclusiva sobre aquela
conta — se ele está subindo agora, ninguém mais está rodando jobs dessa
conta. Jobs que estavam legítimos em `running` seriam impossíveis nesse
modelo (o worker-dono acabou de subir).

**Limitação aceita**: durante o transiente em que dois workers pra mesma
conta são iniciados em paralelo (operador erra), o segundo a subir vai
resetar o job que o primeiro está processando. O primeiro continua rodando,
o segundo pode re-pegar depois. Documentar em README: NÃO iniciar dois
workers pra mesma conta.

### Supervisor opcional

`scripts/run_workers.py` é um utilitário: lê `SELECT id FROM accounts WHERE
is_active=TRUE`, spawna `python -m app.worker --account-id <id>` via
`subprocess.Popen` pra cada uma, encaminha SIGINT/SIGTERM pros filhos.

Operador pode ignorar esse script e rodar cada worker manualmente em
terminais separados ou via systemd/nssm/pm2. O script existe pra simplificar
o dia-a-dia, não como dependência do sistema.

## Mudanças de código

| Arquivo | O que muda |
|---|---|
| `app/db/database.py` | Adiciona `@event.listens_for(engine, "connect")` aplicando `PRAGMA journal_mode=WAL` e `busy_timeout=5000` quando a URL começa com `sqlite` |
| `app/core/job_queue.py` | Novo `claim_next_pending(account_id) -> Job \| None` via `UPDATE ... RETURNING`. Novo `reset_orphan_running(account_id) -> int`. `get_next_pending` e `update_status` mantidos intactos |
| `app/worker.py` | Argparse com `--account-id` (obrigatório). Startup: valida conta existe e está ativa, chama `reset_orphan_running`. Loop usa `claim_next_pending` no lugar de `get_next_pending` |
| `scripts/run_workers.py` | **Novo**. Lê contas ativas, spawna subprocess por conta, faz signal forwarding |
| `tests/test_job_queue.py` | Novo teste: duas threads chamam `claim_next_pending` simultâneo, só uma retorna o job |
| `tests/test_job_queue.py` | Novo teste: `reset_orphan_running` reseta só jobs da conta indicada |
| `tests/test_worker.py` | **Novo**. Testa que startup chama `reset_orphan_running` e que o loop usa `claim_next_pending` (com mocks) |
| `CLAUDE.md` | Adiciona seção "Running multiple workers" com comando e aviso de um-worker-por-conta |

## Contratos

### `JobQueueManager.claim_next_pending(account_id: int) -> Job | None`

- Retorna `Job` com status recém-atualizado para `running` + `started_at` preenchido, OU `None` se não há job pendente pra essa conta.
- Thread-safe: chamadas simultâneas retornam jobs diferentes ou `None`, nunca o mesmo job duas vezes.
- Não valida se `account_id` existe — validação é responsabilidade do chamador (worker faz isso no startup).

### `JobQueueManager.reset_orphan_running(account_id: int) -> int`

- Reseta todos os jobs com `status='running'` e `account_id=N` pra `status='pending'`, zera `started_at`.
- Retorna quantos jobs foram resetados (usado pra log no worker).
- Idempotente — chamar duas vezes não causa dano (segunda chamada retorna 0).

### `app/worker.py` novo CLI

```
python -m app.worker --account-id 3
```

- `--account-id` é obrigatório (erro claro se omitido).
- Startup: query `Account.id == account_id`; se não existe ou `is_active=False`, erro claro e exit 1.
- Startup: `reset_orphan_running(account_id)`; log `"[worker N] Reset M orphan jobs"` se M > 0.
- Loop: `claim_next_pending(account_id)` em vez de `get_next_pending`.
- Loop: `reset_daily_quotas()` continua sendo chamado (é global, não por-conta — barato).

### `scripts/run_workers.py`

- Lê `Account.query.filter_by(is_active=True).all()`.
- Pra cada: `subprocess.Popen([sys.executable, "-m", "app.worker", "--account-id", str(acc.id)])`.
- Registra handler SIGINT/SIGTERM que envia o sinal pra todos os filhos e faz `wait()`.
- Sai com código 0 se todos os filhos saíram com 0, senão 1.

## Fora do escopo

- **Heartbeats / lease timeouts**. Com worker-por-conta, a recuperação por
  startup-reset é suficiente. Heartbeats só fariam sentido num modelo pool
  compartilhado, que foi descartado.
- **Contenção de CPU (FFmpeg)**. N workers × FFmpeg paralelo satura máquina
  pequena. Sem tratamento automático: operador mede e decide quantas contas
  rodar simultaneamente. A arquitetura suporta 1..N; nada força rodar todas.
- **Contenção de APIs externas**. fal.ai / Pollinations têm rate limits
  próprios, compartilhados entre workers da mesma máquina. Nenhum worker
  coordena com outro. Quota-flags em `app/utils/quota.py` são in-process
  (por job_id), o que continua correto.
- **Orquestração produtiva (systemd/nssm)**. Fora do escopo do repo.
- **Claim baseado em fair-scheduling entre contas**. Modelo é explicitamente
  "worker por conta" — fair-scheduling não se aplica.
- **Migração de schema**. Nenhuma coluna é adicionada; não há migration.

## Riscos

- **`UPDATE ... RETURNING` em SQLite**: disponível desde SQLite 3.35 (2021).
  Verificar no startup do engine que a versão é compatível; falhar explícito
  se não for. Python 3.13 embarca SQLite moderno.
- **WAL em diretório sem permissão de escrita**: cria arquivos `-wal` e
  `-shm` ao lado do `.db`. Em dev isso é dado; em prod (containers) pode
  falhar. Aceitável — `data/app.db` já requer escrita.
- **Dois workers pra mesma conta**: causa o transiente descrito em
  "Recuperação de órfãos". Mitigação: documentação + eventualmente um
  lockfile/advisory lock por conta no futuro (YAGNI agora).

## Critérios de sucesso

1. Teste de concorrência passa: duas threads chamam `claim_next_pending`
   no mesmo instante em um banco com 1 job pendente; exatamente uma recebe
   o job, a outra recebe `None`.
2. Teste de órfão: job `running` com crash simulado é resetado pra `pending`
   no próximo startup e pego pelo worker.
3. Smoke-test manual: 2 contas ativas, 4 jobs pendentes (2 em cada),
   `run_workers.py` processa os 4 em ~metade do tempo comparado a rodar
   serial.
4. Suíte existente (`test_job_queue.py`, `test_pipeline_runner.py`,
   `test_worker.py` se existir) continua passando.
