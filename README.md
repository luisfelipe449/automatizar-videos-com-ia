# Automatizar Vídeos com IA

Pipeline em Python que transforma um prompt de texto em um YouTube Short publicado, usando apenas APIs gratuitas: **Groq** (roteiro com LLM Llama + transcrição Whisper), **Gemini** (TTS, e Imagen como fallback opcional de imagens), **Pollinations.ai** (geração de imagens primária, sem chave) e a **YouTube Data API v3** (upload OAuth2). Tudo roda localmente, renderiza com FFmpeg em 1080x1920 e faz upload resumível do Short.

## Arquitetura

`prompt -> script_service -> tts_service + caption_service -> visual_service -> render_service -> youtube_service`

## Pré-requisitos

- Python 3.12+
- FFmpeg 8+ no PATH (ou caminho explícito em `FFMPEG_PATH`)
- Conta Google com:
  - Gemini API key (AI Studio) para TTS
  - OAuth2 client (Desktop app) para YouTube Data API v3
- Conta Groq com API key

## Setup

```bash
# 1. clonar
git clone <repo> && cd automatizar-videos-com-ia

# 2. venv + dependências
python -m venv .venv
source .venv/Scripts/activate   # Windows (Git Bash)
# source .venv/bin/activate     # Linux/macOS
pip install -r requirements.txt

# 3. FFmpeg — instale pelo gerenciador do seu SO
# Windows:  winget install Gyan.FFmpeg
# macOS:    brew install ffmpeg
# Debian:   sudo apt install ffmpeg

# 4. configurar .env
cp .env.example .env
# preencher GROQ_API_KEY, GEMINI_API_KEY, YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET
```

## Refresh token do YouTube (uma vez)

```bash
# Opção A: usar o client_secret.json baixado do Google Cloud Console
python scripts/bootstrap_youtube_oauth.py --client-secrets path/to/client_secret.json

# Opção B: se YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET já estão no .env
python scripts/bootstrap_youtube_oauth.py
```

O script abre o navegador, pede consentimento com o escopo `youtube.upload` e imprime uma linha `YOUTUBE_REFRESH_TOKEN=...`. Copie essa linha para o `.env`.

## Gerar e publicar um Short

```bash
python -m app.orchestrator --prompt "3 curiosidades sobre buracos negros"
```

Flags úteis:

- `--skip-upload` — renderiza mas não faz upload (teste local).
- `--job-id <id>` — sobrescreve o `job_id` derivado do prompt.

O upload usa `YOUTUBE_PRIVACY_STATUS` do `.env` (default `private`). Só troque para `public` depois de validar 2 a 3 uploads privados manualmente no YouTube Studio.

## Artefatos gerados

| Caminho | Conteúdo |
|---|---|
| `assets/generated/<job_id>/script.json` | Roteiro validado (Pydantic) |
| `assets/generated/<job_id>/narration.wav` | Áudio TTS 24 kHz 16-bit mono |
| `assets/generated/<job_id>/captions.srt` | Legendas sincronizadas (Whisper) |
| `assets/generated/<job_id>/scene_XX.png` | Imagem por cena (1080x1920) |
| `assets/generated/<job_id>/*.hash` | Hash de entrada p/ cache idempotente |
| `assets/generated/<job_id>/status.json` | Estado de cada etapa |
| `assets/generated/<job_id>/upload.json` | Registro do upload (`video_id`) |
| `assets/rendered/<job_id>/final.mp4` | Vídeo final 9:16 |

## Idempotência

Cada etapa cacheia por hash do input (texto da narração, prompt de imagem) ou mtime (render). Rodar duas vezes o mesmo prompt com o mesmo `job_id` é no-op até o upload (que também é pulado se `upload.json` já tiver `video_id`). Para forçar refazer uma etapa, apague o artefato (e o `.hash`) correspondente.

## Testes

```bash
pytest -v                 # suíte completa (rápida, apenas unitários)
pytest -v -m slow         # inclui smoke de render com FFmpeg real
pytest -v -m "not slow"   # apenas unitários (explícito)
```

## Limites conhecidos

- **YouTube Data API**: `videos.insert` custa 1600 de 10000 unidades/dia — máx. ~6 uploads/dia sem quota increase.
- **Pollinations.ai** é o gerador primário (sem chave); qualidade pode variar. Configure `GEMINI_API_KEY` para habilitar o fallback Imagen em caso de erro/indisponibilidade do Pollinations.
- **Gemini TTS** está em preview. Se a API mudar, ajuste `GEMINI_TTS_MODEL` no `.env`.
- **Refresh token** expira após ~6 meses de inatividade ou troca de senha da conta. Rode o pipeline pelo menos 1x/mês.

## Solução de problemas

- `MissingEnvVar: Environment variable 'X' is required` — preencha `.env` seguindo `.env.example`.
- `FFmpegError ...` ou `ffmpeg not found` — rode `ffmpeg -version`; se não estiver no PATH, defina `FFMPEG_PATH` no `.env` apontando para o executável.
- `YouTubeUploadError ... 401 UNAUTHENTICATED` / `invalid_grant` — o refresh token expirou ou foi revogado; rode `scripts/bootstrap_youtube_oauth.py` de novo e cole o novo valor no `.env`.
- `VisualGenerationError ... Pollinations ...` + `no fallback available` — Pollinations está offline e não há `GEMINI_API_KEY` configurada; tente mais tarde ou configure Gemini para habilitar o fallback Imagen.
- Upload falha com `quotaExceeded` — você passou do limite diário do YouTube; espere até ~00:00 PT (Pacific Time) para o reset.

## Estrutura de diretórios

Veja `PLANO.md` para o detalhe arquitetural. Resumo:

| Caminho | Papel |
|---|---|
| `app/orchestrator.py` | Entry point CLI; orquestra as 6 etapas com cache e `status.json`. |
| `app/services/*.py` | Um arquivo por integração externa (script, tts, captions, visuals, render, youtube). |
| `app/utils/*.py` | `filepaths` (paths determinísticos), `cache` (hash), `retries` (tenacity), `ffmpeg_helpers`, `quota`. |
| `app/models.py` | Schemas Pydantic — contrato JSON entre etapas. |
| `app/config.py` | Carrega `.env`, define constantes e diretórios base. |
| `scripts/bootstrap_youtube_oauth.py` | One-shot para obter o `refresh_token`. |
| `tests/` | Unitários + smoke de render (`-m slow`). |
