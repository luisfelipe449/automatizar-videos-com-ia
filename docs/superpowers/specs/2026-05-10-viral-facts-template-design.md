# Viral Facts Template — Design

## Problema

No modo Tema (5 vídeos), o usuário quer que os 5 scripts gerados sigam
uma estrutura padrão fixa — a diferença entre eles é *só* o título. Hoje
o `_SYSTEM_PROMPT` de `app/services/script_service.py` mistura "viral
facts" com lógica condicional de Top-N (rank/rank_title), CTA suggestions
variadas, etc. — o que faz a saída variar bastante entre jobs, mesmo
quando só o título deveria mudar.

O usuário forneceu um template textual com slots `{VIDEO_TITLE}` que
descreve a estrutura desejada (Opening Hook → Intro Line → 8-12 fatos →
CTAs → Emotional Hooks → Closing). Essa estrutura deve virar o único
system prompt do scriptwriter, válido tanto para o modo Tema quanto para
o modo CLI.

## Objetivo

- Substituir `_SYSTEM_PROMPT` por uma versão que funde o template do
  usuário (estrutura fixa viral facts) com as regras JSON que o pipeline
  já consome (schema, scenes, image_prompt, duration budget).
- Injetar o título específico de cada job via `VIDEO_TITLE` no user
  message (não no system prompt — cache-friendlier e mais claro).
- No modo Tema, usar `angle.title` como `VIDEO_TITLE` de cada um dos 5
  jobs. No modo CLI, usar o próprio `--prompt`.
- Remover a lógica de ranking (Top-N, rank/rank_title) do prompt — os
  campos do schema Pydantic ficam (sempre null) para preservar
  compatibilidade.

## Não-escopo

- NÃO mudar o schema Pydantic `Script`/`Scene` em `app/models.py`.
- NÃO mudar `tts_service`, `caption_service`, `visual_service`,
  `render_service`, `youtube_service` — o contrato JSON fica idêntico.
- NÃO mexer em `_ANGLES_SYSTEM_PROMPT` nem em `generate_angles()`.
- NÃO adicionar novas variáveis de ambiente (o `SCRIPT_PROMPT_FILE`
  override continua funcionando).
- NÃO mudar temperatura, `max_tokens` nem o retry loop.

## Abordagem

Opção A (selecionada): reescrever `_SYSTEM_PROMPT` fundindo o template
do usuário com as regras JSON existentes, e injetar `VIDEO_TITLE` no
user message.

Rejeitadas:

- **Opção B** — `SCRIPT_PROMPT_FILE` externo. Não suporta injeção
  dinâmica de `VIDEO_TITLE` por job (env var é global).
- **Opção C** — dois system prompts (Tema vs CLI). O usuário pediu
  "substitui tudo", então manter bifurcação é overhead sem benefício.

## Arquitetura

```
Modo Tema:
  POST /jobs/create-batch
    → para cada angle em angles[]:
        script_service.generate_script(
            prompt=angle.prompt,
            job_id=<derivado>,
            video_title=angle.title,   # ← novo
        )

Modo CLI:
  python -m app.orchestrator --prompt "X"
    → run_pipeline(prompt="X", ...)
        → _stage_script: generate_script(
              prompt="X",
              job_id=jid,
              video_title="X",         # ← novo: prompt = título
          )
```

O system prompt descreve a estrutura fixa viral facts e instrui o LLM a
ler `VIDEO_TITLE: <título>` do user message e usá-lo como o campo
`title` da saída (podendo recortar/adaptar para ≤100 chars se
necessário).

## Arquivos afetados

### `app/services/script_service.py`

**`_SYSTEM_PROMPT` reescrito.** Remove blocos de ranking; descreve a
estrutura viral facts (7 passos) + schema JSON + constraints numéricos.
Instrui o LLM a extrair `VIDEO_TITLE` do user message.

**`generate_script()` — nova assinatura:**

```python
def generate_script(
    prompt: str,
    *,
    job_id: str | None = None,
    video_title: str | None = None,  # ← novo
) -> Script:
```

Quando `video_title is None`, usa `prompt` como fallback (invariante:
sempre existe um título para injetar).

**User message novo:**

```
Topic for the Short: {prompt}
VIDEO_TITLE: {video_title}
```

### `app/orchestrator.py`

`_stage_script` passa `video_title=prompt` para `generate_script`. O
`--prompt` do CLI passa a ter papel duplo (tópico + título-fonte), que
é o comportamento aceitável combinado no brainstorm.

### `app/web/routes/jobs.py` (`create-batch`)

No loop que gera os N scripts, passar o título do ângulo:

```python
script_service.generate_script(
    prompt_text,
    job_id=job_id,
    video_title=angles[idx - 1]["title"],
)
```

### `app/models.py`

Sem mudanças. `rank`/`rank_title` continuam `Optional` e serão sempre
`null` na prática.

## Error handling

Sem mudanças. O novo parâmetro `video_title` é apenas string — não
adiciona caminho de erro. `ScriptGenerationError` continua disparando
após 2 falhas de validação, `default_retry` continua cobrindo falhas
transientes de rede.

## Testes

### `tests/test_script_service.py`

1. **Atualizar** `test_happy_path_writes_script_json` para assertar que
   o user message do primeiro call contém `VIDEO_TITLE:`.
2. **Novo** `test_video_title_forwarded_to_user_message`: chamar
   `generate_script("topic", video_title="My Title")` e verificar que
   `FakeGroq.last_instance.calls[0]["messages"]` contém uma mensagem
   user com `VIDEO_TITLE: My Title`.
3. **Novo** `test_video_title_defaults_to_prompt`: chamar sem
   `video_title`; user message deve conter `VIDEO_TITLE: <prompt>`.

### `tests/test_web_routes.py`

4. **Verificar/adicionar** um teste que mocka `script_service.generate_script`
   em `/jobs/create-batch` e asserta que cada chamada recebe
   `video_title=angles[i]["title"]`.

### `tests/test_orchestrator.py`

5. **Verificar/adicionar** um teste que confirma que `_stage_script`
   chama `generate_script` com `video_title=prompt`.

### Regressão

Os testes existentes que mockam JSON do LLM (não dependem do texto
literal do prompt) continuam passando sem alteração.

## Riscos

- **LLM ignora o `VIDEO_TITLE` do user message** e inventa um título
  aleatório. Mitigação: instrução explícita no system prompt + exemplo
  inline + título é validado contra ≤100 chars pelo Pydantic (não
  quebra, só solta um título estranho).
- **Template rígido → scripts repetitivos** quando o modo Tema gera 5
  vídeos com títulos similares. Mitigação: a diversidade vem dos 5
  títulos distintos; temperatura 0.9 mantém variação dentro da
  estrutura.
- **Remover lógica de ranking quebra usuários que pediam Top-N.**
  Mitigação: o CLAUDE.md e a spec anterior (`2026-05-10-viral-titles`)
  deixam claro que o foco é viral facts; Top-N não era um recurso
  estável. Os campos do schema ficam para trás-compat.
- **CLI passa prompt longo como título.** O prompt vira `VIDEO_TITLE`
  verbatim, e o LLM tem que recortar para ≤100 chars. O Pydantic
  garante o corte; a qualidade pode cair em prompts longos, mas é o
  comportamento acordado ("O próprio --prompt vira VIDEO_TITLE").

## Observações

- O override `SCRIPT_PROMPT_FILE` fica intocado — usuários que querem
  genre/linguagem diferente continuam podendo sobrepor o prompt todo.
- Cache de scripts por `job_id` / `prompt` não é afetado (a mudança é
  no conteúdo do prompt, não no hashing).
