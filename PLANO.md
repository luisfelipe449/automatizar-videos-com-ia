# Pipeline de Geração Automática de Shorts no YouTube

## Context

Projeto começando do zero em Python para gerar e publicar Shorts no YouTube de forma automatizada, usando **apenas APIs gratuitas**. O plano inicial tinha arquitetura correta (orquestrador + 4 serviços, contrato JSON entre etapas, FFmpeg, YouTube Data API v3), mas com três lacunas práticas:

1. O gerador de imagens ficava "em aberto" — maior risco do pipeline.
2. OAuth2 do YouTube em modo automatizado tem armadilhas específicas (service account não funciona para `videos.insert`).
3. Faltava legendas queimadas (críticas para retenção em Shorts), compliance de Shorts (<60s, hashtag), e estratégia de cota da YouTube Data API (`videos.insert` custa 1600 / 10000 unidades/dia → máx. ~6 uploads/dia).

Decisões fechadas:
- **LLM roteiro**: Groq (Llama 3.3 70B), structured output via `response_format`.
- **TTS**: Gemini TTS.
- **Imagem**: Google Imagen (AI Studio) como primário, fallback automático para Pollinations.ai ao estourar cota.
- **Legendas**: queimadas no vídeo via Whisper no Groq (transcreve o TTS para sincronia real).
- **Render**: FFmpeg (Ken Burns via `zoompan`, 9:16, legendas via filtro `subtitles`).
- **Upload**: YouTube Data API v3, OAuth2 com refresh token de conta real, upload resumível.
- **Volume**: até 6 uploads/dia (cabe na cota padrão, sem quota increase).

Objetivo: MVP funcional de ponta a ponta (prompt → Short publicado) rodando local, com idempotência por hash de prompt e retomada a partir do último artefato gerado.

## Estrutura do projeto

```
automatizar-videos-com-ia/
  app/
    orchestrator.py          # entry point; roda pipeline completo
    config.py                # carrega .env, paths, constantes
    models.py                # Pydantic: Script, Scene, Job, RenderSpec
    services/
      script_service.py      # Groq chat.completions, response_format=json_object
      tts_service.py         # Gemini TTS → PCM → WAV
      caption_service.py     # Groq Whisper large-v3 → .srt
      visual_service.py      # Imagen primário, Pollinations fallback
      render_service.py      # FFmpeg: 9:16, zoompan, subtitles, mux áudio
      youtube_service.py     # OAuth2 refresh_token, videos.insert resumível
    utils/
      filepaths.py           # paths determinísticos por job_id e hash
      retries.py             # tenacity: backoff exponencial + jitter
      ffmpeg_helpers.py      # wrappers subprocess, detecção de erros
      cache.py               # skip se artefato existe e hash do prompt bate
      quota.py               # detecção de 429 do Imagen → ativa fallback
  scripts/
    bootstrap_youtube_oauth.py  # script one-shot para obter refresh_token
  assets/
    input/                   # prompts-base, música de fundo CC0
    generated/<job_id>/      # scene_01.png, narration.wav, captions.srt, ...
    rendered/<job_id>/       # final.mp4
  tests/
    test_script_service.py
    test_render_service.py   # smoke test com áudio + 1 imagem
    test_youtube_auth.py     # valida refresh_token sem fazer upload
  .env.example
  requirements.txt
  README.md
```

## Contrato JSON (fonte da verdade entre etapas)

```json
{
  "job_id": "2026-04-30_tema-slug_abc123",
  "title": "Título do Short (≤100 chars)",
  "description": "Descrição ... #Shorts",
  "tags": ["tag1", "tag2"],
  "narration": "Texto completo da narração (≤150 palavras p/ caber em 60s)",
  "scenes": [
    {
      "id": 1,
      "scene_text": "texto curto da cena",
      "image_prompt": "prompt visual detalhado, estilo cinematográfico, 9:16",
      "duration_seconds": 4
    }
  ]
}
```

Regra: `sum(duration_seconds) ≤ 58` (buffer de 2s para fade out). Validado no `script_service` antes de prosseguir.

## Fases de implementação

### Fase 0 — Setup
- `requirements.txt`: `groq`, `google-genai`, `google-api-python-client`, `google-auth-oauthlib`, `pydantic`, `tenacity`, `python-dotenv`, `requests`, `httpx`.
- `.env.example` com: `GROQ_API_KEY`, `GEMINI_API_KEY`, `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`.
- `scripts/bootstrap_youtube_oauth.py`: abre browser uma vez, salva `refresh_token` no `.env`. Escopo: `https://www.googleapis.com/auth/youtube.upload`.
- Confirmar FFmpeg instalado (`ffmpeg -version`).

### Fase 1 — script_service
- Groq `chat.completions.create` com `response_format={"type": "json_object"}` e modelo `llama-3.3-70b-versatile`.
- System prompt força o schema exato acima.
- Validar com Pydantic; regenerar 1x se inválido; falhar se 2x inválido.
- Teste: prompt → JSON válido, duração total ≤58s, ≥3 cenas.

### Fase 2 — tts_service
- `google-genai` SDK, modelo `gemini-2.5-flash-preview-tts`, `response_modalities=['AUDIO']`.
- Salvar PCM retornado como WAV (16-bit, 24kHz) em `assets/generated/<job_id>/narration.wav`.
- Cache: se WAV existe e hash(narration) bate com `narration.hash`, pular.

### Fase 3 — caption_service
- Groq `audio.transcriptions.create`, modelo `whisper-large-v3`, `response_format=verbose_json`, `timestamp_granularities=['word']`.
- Converter segmentos para `.srt` com quebras a cada ~6 palavras (legibilidade em Shorts).
- Saída: `assets/generated/<job_id>/captions.srt`.

### Fase 4 — visual_service
- **Primário**: Gemini Imagen via `google-genai` (`imagen-3.0-generate-002`), aspect ratio `9:16`, 1 imagem por cena.
- **Detecção de cota**: catch de erro 429/RESOURCE_EXHAUSTED → marca flag em `quota.py` → próximas cenas do mesmo job vão direto para Pollinations.
- **Fallback**: `GET https://image.pollinations.ai/prompt/<url_encoded_prompt>?width=1080&height=1920&nologo=true` → salva PNG.
- Cache por `hash(image_prompt)`; nomes `scene_01.png`, `scene_02.png`, ...
- Moderação: regex/blocklist simples antes de enviar prompts.

### Fase 5 — render_service
FFmpeg em um único comando com `filter_complex`:
1. Para cada imagem: `zoompan=z='min(zoom+0.0015,1.2)':d=<duration*30>:s=1080x1920` (Ken Burns sutil).
2. Concatenar clipes com `concat` filter.
3. Mux com `narration.wav` (`-map` streams).
4. Queimar legendas: `subtitles=captions.srt:force_style='Fontsize=14,Alignment=2,MarginV=200,Outline=2'`.
5. Output: `-c:v libx264 -preset medium -crf 23 -c:a aac -b:a 128k -pix_fmt yuv420p -movflags +faststart rendered/<job_id>/final.mp4`.
6. Validação pós-render: `ffprobe` confirma duração ≤60s e resolução 1080x1920.

### Fase 6 — youtube_service
- `google-auth-oauthlib` + refresh_token do `.env` → Credentials.
- `build('youtube', 'v3', credentials=creds)`.
- `videos().insert(part='snippet,status', body={...}, media_body=MediaFileUpload(path, chunksize=-1, resumable=True))`.
- Body: `snippet.title`, `snippet.description` (sempre termina com `#Shorts`), `snippet.tags`, `snippet.categoryId='22'`, `status.privacyStatus='private'` no MVP (trocar para `public` depois de validado).
- Loop `next_chunk()` com retry em 5xx via tenacity.
- Registrar `videoId` retornado em `assets/generated/<job_id>/upload.json`.

### Fase 7 — orchestrator
Orquestra as fases 1→6 sequencialmente. Para cada etapa:
- Verifica se o artefato já existe (idempotência).
- Se sim e hash bate, pula.
- Se falhar, loga e para (não tenta etapa seguinte).
- Estado final: `assets/generated/<job_id>/status.json` com `{stage, ok, videoId?, error?}`.

## Arquivos críticos a criar

| Arquivo | Responsabilidade |
|---|---|
| `app/orchestrator.py` | Pipeline end-to-end, idempotência, status. |
| `app/models.py` | Schemas Pydantic que são o contrato entre etapas. |
| `app/services/script_service.py` | Único lugar que fala com Groq chat. |
| `app/services/tts_service.py` | Único lugar que fala com Gemini TTS. |
| `app/services/caption_service.py` | Único lugar que fala com Groq Whisper. |
| `app/services/visual_service.py` | Primário Imagen + fallback Pollinations + detecção de cota. |
| `app/services/render_service.py` | Wrapper do FFmpeg; comando único com filter_complex. |
| `app/services/youtube_service.py` | OAuth + resumable upload. |
| `scripts/bootstrap_youtube_oauth.py` | One-shot para gerar refresh_token. |
| `.env.example` | Documenta todas as credenciais necessárias. |

## Verificação (end-to-end)

1. `python scripts/bootstrap_youtube_oauth.py` — gera refresh_token uma única vez, abre browser, salva no `.env`.
2. `python -m app.orchestrator --prompt "3 curiosidades sobre buracos negros"` com `privacyStatus=private`.
3. Checar em `assets/generated/<job_id>/`:
   - `script.json` válido (Pydantic).
   - `narration.wav` toca e bate com `narration`.
   - `captions.srt` sincronizado.
   - `scene_*.png` em 1080x1920.
4. Checar `rendered/<job_id>/final.mp4`: `ffprobe` → duração ≤60s, 1080x1920, áudio presente, legendas visíveis.
5. Checar no YouTube Studio: vídeo uploaded como privado, com título/descrição/tags corretos, aparece na aba Shorts.
6. Rodar o mesmo comando de novo: deve ser no-op (tudo em cache, só re-tenta o upload se necessário).
7. Teste de fallback de imagem: forçar erro de cota no Imagen (mockar 429) → confirmar que Pollinations é chamado e o job termina com sucesso.
8. Trocar `privacyStatus` para `public` só depois de validar 2–3 vídeos privados manualmente.

## Riscos conhecidos e mitigações

- **Gemini TTS em preview**: API pode mudar. Mitigação: isolar no `tts_service.py`, trocar modelo via env var.
- **Imagen cota baixa**: o fallback Pollinations resolve; monitorar via `quota.py` logs.
- **Pollinations qualidade variável**: aceitável para MVP; upgrade futuro para HuggingFace ou SD local.
- **YouTube OAuth refresh_token expira** se não usar por 6 meses ou se a conta mudar senha. Mitigação: rodar o pipeline pelo menos 1x/mês; documentar como re-gerar.
- **Shorts detection**: YouTube detecta Short automaticamente se vertical e <60s; `#Shorts` no título/descrição reforça.
