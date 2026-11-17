# Automatizar Vídeos com IA

Pipeline em Python que transforma um prompt de texto em um YouTube Short publicado, usando apenas APIs gratuitas: **Groq** (roteiro com LLM Llama + transcrição Whisper), **Gemini** (TTS, e Imagen como fallback opcional de imagens), **Pollinations.ai** (geração de imagens primária, sem chave) e a **YouTube Data API v3** (upload OAuth2). Tudo roda localmente, renderiza com FFmpeg em 1080x1920 e faz upload resumível do Short.

## Arquitetura

`prompt -> script_service -> tts_service + caption_service -> visual_service -> render_service -> youtube_service`

## Pré-requisitos

- Python 3.13+ (versão oficial do projeto)
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

### Modo CLI (original)

```bash
python -m app.orchestrator --prompt "3 curiosidades sobre buracos negros"
```

Flags úteis:

- `--skip-upload` — renderiza mas não faz upload (teste local).
- `--job-id <id>` — sobrescreve o `job_id` derivado do prompt.

O upload usa `YOUTUBE_PRIVACY_STATUS` do `.env` (default `private`). Só troque para `public` depois de validar 2 a 3 uploads privados manualmente no YouTube Studio.

### Modo Dashboard (multi-account)

Para gerenciar múltiplas contas do YouTube e processar jobs em fila, use o dashboard web:

#### 1. Inicializar o banco de dados

```bash
python scripts/init_db.py
```

Isso cria o banco SQLite em `data/app.db` com as tabelas necessárias.

#### 2. Iniciar o dashboard

```bash
uvicorn app.web.main:app --reload --port 8000
```

Acesse http://localhost:8000 para ver o dashboard.

#### 3. Iniciar o worker (terminal separado)

```bash
python -m scripts.run_workers
python -m app.worker
```

O worker processa jobs da fila continuamente. Pressione Ctrl+C para parar.

#### 4. Adicionar conta do YouTube

1. Acesse http://localhost:8000/accounts
2. Clique em "Add Account"
3. Preencha os dados OAuth2:
   - **Name**: Nome identificador (ex: "Canal Principal")
   - **Email**: Email da conta Google
   - **Client ID**: OAuth2 client_id do Google Cloud Console
   - **Client Secret**: OAuth2 client_secret
   - **Refresh Token**: Obtido via `scripts/bootstrap_youtube_oauth.py`
4. A conta será validada e adicionada ao pool

#### 5. Submeter job

1. Acesse http://localhost:8000/jobs
2. Digite o prompt (ex: "5 fatos sobre astronomia")
3. Clique em "Create Job"
4. O worker processará automaticamente usando a conta com mais quota disponível
5. Acompanhe o progresso na página de detalhes do job

## Modo Clips (canal de cortes de GTA 6)

Pipeline separado do modo prompt, em `app/clips/`. Todo dia ele produz as
edições "Top Clips of the Day" e "Funny Moments" a partir de clips da Twitch,
em dois canais (en e pt-BR). Cada edição tem um vídeo médio 16:9 e um Short
9:16 por canal. As edições ficam no dashboard para aprovação; o upload é
manual pelo YouTube Studio. Desenho completo:
`docs/superpowers/specs/2026-11-17-gta6-clips-design.md`.

### Fluxo

`coleta (Twitch Helix) → download (yt-dlp) → classificação (Gemini assiste o clip; Groq pelo título se a Gemini falhar) → seleção → render base → render por canal → aprovação no dashboard`

- **Seleção** (`app/clips/select.py`): Top ordena por views; Funny usa só
  `category=funny`, por `funny_score × log(1+views)`, com peso menor para
  clip que depende da fala. Top e Funny do mesmo dia não repetem clip. O
  médio tem 8–12 clips inteiros (3–5 min), em contagem regressiva até o #1;
  o Short é o #1, com o começo cortado se não couber em 59 s junto com o
  outro.
- **Render base** (`app/clips/render.py`): um segmento 1080p30 por clip, com
  áudio normalizado, selo `#N` e crédito do streamer (ícone da plataforma +
  nome); os segmentos são unidos com transição de 0,3 s.
- **Render por canal**: cartão de título + base + outro com CTA narrado
  (Gemini TTS) e música; Short com título na faixa de cima; thumbnail
  1280×720 em JPEG.
- **Numeração** (`app/clips/metadata.py`): `#N` conta por canal e formato, e
  só edições **aprovadas** contam.

### Configuração

No `.env` (detalhes em `.env.example`):

| Variável | Papel |
|---|---|
| `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` | App da Twitch (dev.twitch.tv/console), token de app |
| `CLIPS_GAME_NAME` | Jogo coletado (`Grand Theft Auto VI`; `Grand Theft Auto V` para testes) |
| `CLIPS_DIR` | Onde ficam clips, outros e edições (vários GB por dia). Vazio = `assets/clips`; ex.: `F:/five-star-clips` |
| `CLIPS_MIN_VIEWS`, `CLIPS_MAX_CANDIDATES` | Pré-filtro de views e teto de clips por coleta: é o controle de custo da classificação |
| `CLIPS_CLASSIFIER_MODELS` | Modelos Gemini que assistem cada clip, em ordem |
| `GEMINI_API_KEY` | Classificação e TTS do CTA. **Com billing ativo, cada clip classificado é cobrado** |
| `GROQ_API_KEY` | Classificação de reserva, só pelo título |

Os canais são perfis em `assets/input/channels/<chave>.json` (título por
formato com `{n}`, descrição, horário de publicação no fuso do público, CTA
do outro, voz, hashtags, tags). Cada edição renderiza para todos os perfis
dessa pasta.

Arquivos opcionais em `assets/`:

- `input/outro_16x9.mp4` + `input/outro_16x9.json`: animação da marca no
  outro. O JSON tem a caixa `delogo` que cobre a marca d'água do gerador e
  pertence àquele vídeo: ao trocar o vídeo, atualize ou apague o JSON. Sem o
  vídeo, um outro provisório é gerado.
- `input/music/*.mp3`: música do outro (`upbeat_future.mp3` tem preferência).
- `icons/{twitch,kick,youtube}.png`: ícone do crédito. Sem ele, um quadrado
  na cor da plataforma ocupa o lugar.

### Rodar à mão

O Python é o do Windows (`.venv/Scripts/python.exe`). Variáveis do shell do
WSL só chegam a ele listadas em `WSLENV`.

```bash
# Lote do dia (formatos do calendário; data padrão = hoje em Brasília)
.venv/Scripts/python.exe -m app.clips daily

# Dry run: coleta, baixa e classifica, planeja e imprime a seleção, sem render.
# Nem o outro é renderizado: o espaço do Short usa o outro 9:16 já em disco ou
# uma estimativa, e o relatório diz qual.
.venv/Scripts/python.exe -m app.clips daily --date 2026-11-18 --dry-run \
  --format top_daily --format funny_daily

# Planejar sem Twitch e/ou sem a classificação paga
.venv/Scripts/python.exe -m app.clips daily --skip-harvest --skip-classify

# Outro jogo só nesta execução
WSLENV=CLIPS_GAME_NAME CLIPS_GAME_NAME="Grand Theft Auto V" \
  .venv/Scripts/python.exe -m app.clips daily --dry-run --format top_daily
```

Rodar de novo no mesmo dia retoma de onde parou: clips já baixados e
classificados são reaproveitados, uma edição já criada mantém seus clips, e
edições aprovadas ou rejeitadas não são tocadas.

### Agendamento

Cron do WSL, a cada 30 minutos, chamando `scripts/run_clips_daily.sh`, que
roda o lote uma vez por dia a partir das 08h de Brasília. Instalação, logs e
limitações em `scripts/schedule_clips.md`.

### Aprovação

`http://localhost:8000/editions` lista o lote do dia. A página da edição tem
os players de cada canal, a thumbnail, título e descrição com botão de
copiar, o horário de publicação no fuso do público e em BRT, e os botões
Aprovar/Rejeitar. A decisão é final: aprovar avança a numeração.

### Artefatos

```
<CLIPS_DIR>/                            # padrão: assets/clips/
  raw/<fonte>_<id>.mp4 (+ .proxy.mp4)   # clip baixado e proxy 480p do classificador
  outro/                                # outros por canal e aspecto, CTA em cache
  editions/<data>_<formato>/
    seg_NN.mp4, base_16x9.mp4, base_9x16.mp4, base.json, peak.png
    status.json                         # etapas, tempos e último erro
    <perfil>/medio.mp4, short.mp4, thumb.jpg, metadata.json
data/clips-cron/                        # estado e logs do agendamento
```

### Forçar re-render

Cada saída tem um `.hash` ao lado e só é refeita quando suas entradas mudam.
O `daily` só renderiza edições ainda não decididas (`planned` ou `rendered`).

- **Uma peça de um canal**: apague o arquivo e o `.hash` dele em
  `<perfil>/`, depois rode o `daily` daquela data.
- **A edição inteira, com os mesmos clips**: apague a pasta
  `assets/clips/editions/<data>_<formato>/` e rode o `daily` daquela data.
- **Outra seleção de clips**: apague a edição do banco (os itens vão
  junto) e rode o `daily`:

  ```bash
  .venv/Scripts/python.exe - <<'EOF'
  from datetime import date
  from app.db.database import get_session
  from app.db.models import Edition
  with get_session() as s:
      ed = s.query(Edition).filter_by(format_key="top_daily", edition_date=date(2026, 11, 19)).one()
      s.delete(ed)
      s.commit()
  EOF
  ```

- **Outro número de episódio**: zere `numbers` da edição
  (`ed.numbers = {}`) e rode o `daily`; o número é recalculado a partir das
  edições aprovadas, e as peças com o título antigo são refeitas.

## Multi-Account Setup

O sistema de multi-contas permite gerenciar múltiplas contas do YouTube para distribuir a carga de uploads e contornar os limites de quota da API.

### Como obter credenciais OAuth2

1. Acesse o [Google Cloud Console](https://console.cloud.google.com/)
2. Crie um projeto (ou use existente)
3. Habilite a **YouTube Data API v3**:
   - Menu lateral: APIs & Services > Library
   - Busque "YouTube Data API v3"
   - Clique em "Enable"
4. Configure a tela de consentimento OAuth:
   - Menu lateral: APIs & Services > OAuth consent screen
   - Tipo: External (ou Internal para Google Workspace)
   - Preencha nome do app, email de suporte
   - Adicione escopo: `https://www.googleapis.com/auth/youtube.upload`
   - Adicione seu email como usuário de teste (se External)
5. Crie credenciais OAuth2:
   - Menu lateral: APIs & Services > Credentials
   - Create Credentials > OAuth client ID
   - Application type: Desktop app
   - Baixe o JSON (ou copie client_id e client_secret)
6. Obtenha o refresh token:
   ```bash
   python scripts/bootstrap_youtube_oauth.py --client-secrets path/to/client_secret.json
   ```
   Siga o fluxo OAuth no navegador e copie o `YOUTUBE_REFRESH_TOKEN` impresso.

### Limites de Quota

- Cada conta do YouTube tem **10.000 unidades/dia** de quota
- Um upload (`videos.insert`) consome **1.600 unidades**
- Portanto, cada conta pode fazer ~**6 uploads/dia**
- Com 3 contas, você pode fazer até **18 uploads/dia**

### Rotação de Contas

O sistema seleciona automaticamente a conta com **mais quota disponível**:

1. Worker verifica `quota_used` de cada conta ativa
2. Filtra contas com quota suficiente (≥ 1600 unidades restantes)
3. Seleciona a conta com maior quota disponível
4. Após upload bem-sucedido, incrementa `quota_used` da conta
5. Quando `quota_reset_at` é atingido (24h depois), zera `quota_used`

Se todas as contas estiverem sem quota, o job falha com `"No available account with sufficient quota"` e deve ser retentado após o reset.

### Adicionar múltiplas contas

Para cada canal do YouTube que você gerencia:

1. Obtenha credenciais OAuth2 separadas (ou use as mesmas, se o refresh token autorizar múltiplos canais)
2. No dashboard, clique em "Add Account" e preencha os dados
3. A conta será validada e adicionada ao pool
4. O worker distribuirá jobs entre as contas automaticamente

## Artefatos gerados

| Caminho | Conteúdo |
|---|---|
| `assets/generated/<job_id>/script.json` | Roteiro validado (Pydantic) |
| `assets/generated/<job_id>/narration.wav` | Áudio TTS 24 kHz 16-bit mono |
| `assets/generated/<job_id>/captions.ass` | Legendas ASS com karaokê por palavra (Whisper + libass) |
| `assets/generated/<job_id>/scene_XX.png` | Imagem por cena (1080x1920) |
| `assets/generated/<job_id>/*.hash` | Hash de entrada p/ cache idempotente |
| `assets/generated/<job_id>/status.json` | Estado de cada etapa |
| `assets/generated/<job_id>/upload.json` | Registro do upload (`video_id`) |
| `assets/rendered/<job_id>/final.mp4` | Vídeo final 9:16 |

## Idempotência

Cada etapa cacheia por hash do input (texto e voz da narração, prompt de imagem) ou mtime (render). Rodar duas vezes o mesmo prompt com o mesmo `job_id` é no-op até o upload (que também é pulado se `upload.json` já tiver `video_id`). Para forçar refazer uma etapa, apague o artefato (e o `.hash`) correspondente.

## Testes

```bash
pytest -v                 # suíte completa (rápida, apenas unitários)
pytest -v -m slow         # inclui smoke de render com FFmpeg real
pytest -v -m "not slow"   # apenas unitários (explícito)
```

## Legendas karaokê (CapCut look)

As legendas são queimadas em formato **ASS** (`captions.ass`) com realce por palavra ao estilo CapCut: a palavra que está sendo falada aparece em amarelo (`#FFD700`), as palavras já faladas ficam brancas, contorno preto e sombra discreta para ler sobre qualquer fundo. A fonte é **Bebas Neue Regular**, carregada diretamente de `assets/fonts/BebasNeue-Regular.ttf` via `fontsdir=` do libass — não precisa instalar no sistema. Para trocar a fonte, basta substituir o `.ttf` em `assets/fonts/` e ajustar `_ASS_FONTNAME` em `app/services/caption_service.py` para o nome interno da nova fonte.

## Limites conhecidos

- **YouTube Data API**: `videos.insert` custa 1600 de 10000 unidades/dia — máx. ~6 uploads/dia sem quota increase.
- **Pollinations.ai** é o gerador primário (sem chave); qualidade pode variar. Configure `GEMINI_API_KEY` para habilitar o fallback Imagen em caso de erro/indisponibilidade do Pollinations.
- **Gemini TTS** está em preview. Se a API mudar, ajuste `GEMINI_TTS_MODEL` no `.env`.
- **Refresh token** expira após ~6 meses de inatividade ou troca de senha da conta. Rode o pipeline pelo menos 1x/mês.

## Solução de problemas

### Problemas gerais (CLI e Dashboard)

- `MissingEnvVar: Environment variable 'X' is required` — preencha `.env` seguindo `.env.example`.
- `FFmpegError ...` ou `ffmpeg not found` — rode `ffmpeg -version`; se não estiver no PATH, defina `FFMPEG_PATH` no `.env` apontando para o executável.
- `YouTubeUploadError ... 401 UNAUTHENTICATED` / `invalid_grant` — o refresh token expirou ou foi revogado; rode `scripts/bootstrap_youtube_oauth.py` de novo e cole o novo valor no `.env`.
- `VisualGenerationError ... Pollinations ...` + `no fallback available` — Pollinations está offline e não há `GEMINI_API_KEY` configurada; tente mais tarde ou configure Gemini para habilitar o fallback Imagen.
- Upload falha com `quotaExceeded` — você passou do limite diário do YouTube; espere até ~00:00 PT (Pacific Time) para o reset.

### Problemas do Dashboard

#### Worker não processa jobs

**Sintoma**: Jobs ficam em `pending` indefinidamente.

**Soluções**:
1. Verifique se o worker está rodando:
   ```bash
   python -m app.worker
   ```
   Deve exibir `[worker] Starting job processor...`

2. Verifique se há contas ativas com quota:
   - Acesse http://localhost:8000/accounts
   - Confirme que existe pelo menos 1 conta com `is_active=True`
   - Verifique `quota_used` < `daily_quota`

3. Verifique logs do worker no terminal:
   - `No available account with sufficient quota` → todas as contas esgotaram quota
   - `Job X not found` → banco corrompido, reinicialize com `scripts/init_db.py`

4. Se o worker parou com erro, reinicie-o. Ele é resiliente e retomará de onde parou.

#### Dashboard não inicia

**Sintoma**: `uvicorn app.web.main:app` falha ou retorna 500.

**Soluções**:
1. Verifique se o banco foi inicializado:
   ```bash
   python scripts/init_db.py
   ```

2. Verifique dependências:
   ```bash
   pip install -r requirements.txt
   ```

3. Verifique se a porta 8000 está livre:
   ```bash
   # Windows
   netstat -ano | findstr :8000
   # Linux/macOS
   lsof -i :8000
   ```

4. Tente porta alternativa:
   ```bash
   uvicorn app.web.main:app --port 8080
   ```

#### Erro ao adicionar conta (OAuth)

**Sintoma**: Formulário retorna erro de validação ou "Credential validation failed".

**Soluções**:
1. Verifique se o refresh token é válido:
   - Rode `scripts/bootstrap_youtube_oauth.py` novamente
   - Use o token recém-gerado

2. Confirme que `client_id` e `client_secret` correspondem ao projeto que gerou o refresh token.

3. Verifique se a YouTube Data API v3 está habilitada no Google Cloud Console.

4. Se o erro for "Account with name 'X' already exists", use nome diferente ou remova a conta antiga.

#### Job falha imediatamente

**Sintoma**: Job muda de `pending` para `failed` em segundos.

**Soluções**:
1. Verifique `error_message` na página de detalhes do job.

2. Erros comuns:
   - `No available account with sufficient quota` → adicione mais contas ou espere reset de quota
   - `GroqError ... API key ...` → configure `GROQ_API_KEY` no `.env`
   - `GeminiTTSError ...` → configure `GEMINI_API_KEY` no `.env`
   - `FFmpegError ...` → instale FFmpeg ou configure `FFMPEG_PATH`

3. Teste o pipeline manualmente para diagnóstico:
   ```bash
   python -m app.orchestrator --prompt "teste" --skip-upload
   ```

#### Quota sempre excedida

**Sintoma**: Todas as contas mostram `quota_used >= daily_quota`.

**Soluções**:
1. Aguarde até `quota_reset_at` (visível na página de contas). O worker reseta automaticamente.

2. Se `quota_reset_at` passou mas quota não resetou:
   - Reinicie o worker (ele verifica resets a cada iteração)
   - Ou force manualmente via Python shell:
     ```python
     from app.db.database import get_session
     from app.core.account_manager import AccountManager
     with get_session() as session:
         mgr = AccountManager(session)
         count = mgr.reset_daily_quotas()
         print(f"Reset {count} accounts")
     ```

3. Considere adicionar mais contas ou aumentar `daily_quota` no Google Cloud Console (requer solicitação ao Google).

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
| `app/clips/` | Modo Clips: coleta, classificação, seleção, render e pipeline do canal de cortes. |
| `scripts/run_clips_daily.sh` | Ponto de entrada do cron do Modo Clips. |
| `tests/` | Unitários + smoke de render (`-m slow`). |
