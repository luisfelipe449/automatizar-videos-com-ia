# Viral Titles — Design

## Problema

Os títulos gerados atualmente pelo LLM em `app/services/script_service.py`
são secos e genéricos (ex.: "Amazing Facts About Space", "Top 5 Greatest
Brazilians"). O prompt do sistema pede apenas um título "hook-style, ≤100
chars, no misleading clickbait", sem exemplos, sem fórmulas, sem
contra-exemplos — o modelo default-a pro mínimo viável.

Isso afeta dois campos em dois prompts diferentes:

1. `_SYSTEM_PROMPT` — gera o `title` do `Script` final (o título que vai pro
   YouTube).
2. `_ANGLES_SYSTEM_PROMPT` — gera o `title` de cada ângulo sugerido na
   página de brainstorming de temas.

## Objetivo

Tornar os títulos gerados por ambos os prompts mais virais e variados,
mantendo guardrails contra clickbait enganoso.

## Abordagem

Adicionar uma seção "Title formulas" em ambos os prompts do sistema,
listando três fórmulas virais nomeadas, com exemplos bons e contra-exemplos.
O LLM escolhe a fórmula que melhor encaixa no tópico — a variedade entre
vídeos vem de ele não ficar preso a um só formato.

### As três fórmulas

**A — Curiosity gap + número**
- `"9 Space Facts That Broke My Brain"`
- `"5 Brazilians Who Literally Changed The World"`
- `"The #1 Shark Fact Nobody Tells You"`

**B — Choque + superlativo emocional**
- `"These Space Facts Are INSANE"`
- `"This Shark Fact Will RUIN Your Sleep"`
- `"Pelé Did Something NOBODY Talks About"`

**C — Pergunta / desafio ao espectador**
- `"Did You Know Space Smells Like Steak?"`
- `"Bet You Can't Guess Fact #7"`
- `"Can You Name The Greatest Brazilian Ever?"`

### Guardrails

- Title ≤ 100 chars (mantido).
- Sem clickbait enganoso — toda promessa do título precisa ser sustentada
  pelo conteúdo (mantido e reforçado).
- Até UMA palavra em CAPS permitida por título (INSANE, NEVER, #1, RUIN) —
  nunca frase inteira.
- SEM emojis no título.
- Escolher a fórmula que melhor encaixa no tópico; não forçar sempre a
  mesma.

### Contra-exemplos a incluir no prompt

- Seco: `"Amazing Facts About Space"`, `"Top 5 Greatest Brazilians"`
- Clickbait enganoso: `"SCIENTISTS CAN'T EXPLAIN THIS!!!"` (quando o vídeo
  nada tem a ver com cientistas nem inexplicabilidade)
- CAPS total: `"THESE SPACE FACTS WILL BLOW YOUR MIND"`

## Arquivos afetados

- `app/services/script_service.py`
  - Editar `_SYSTEM_PROMPT`: inserir a seção "Title formulas" antes do
    bloco "Hard numeric constraints".
  - Editar `_ANGLES_SYSTEM_PROMPT`: inserir a mesma seção ajustada ao
    schema de ângulos (o schema já tem `title` ≤80 chars — os exemplos e
    fórmulas valem igual).

## Não-escopo

- NÃO mexer em `description`, `narration`, `tags`, `scene_text`,
  `image_prompt`, ou qualquer outro campo do schema.
- NÃO mudar o tamanho máximo do título.
- NÃO adicionar novas variáveis de ambiente nem novas configs.
- NÃO mudar temperatura nem `max_tokens`.

Uma mudança por vez — só o texto dos dois prompts.

## Testes

O suite existente (`tests/test_script_service.py`) mocka o JSON retornado
pelo LLM e não depende do texto literal dos prompts, então passa sem
alteração. Rodar `pytest -v tests/test_script_service.py` pra confirmar
regressão zero.

Verificação real da qualidade dos títulos é manual: rodar um ou dois jobs
com o prompt novo e checar se os títulos saem no padrão esperado.

## Riscos

- **Drift pra clickbait enganoso.** Mitigação: regra explícita no prompt +
  contra-exemplo marcado como ❌.
- **LLM sempre escolhendo a mesma fórmula.** Mitigação: pedir
  explicitamente para escolher conforme o tópico e ter exemplos das três
  fórmulas.
- **Título saindo acima do limite de chars.** O schema Pydantic já valida
  ≤100 chars no `title` do Script e ≤80 no `title` do Angle; o retry loop
  corrige se estourar.
