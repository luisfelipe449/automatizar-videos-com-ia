# Canal de cortes GTA 6 (Five Star Clips)

**Data**: 2026-11-17
**Status**: aprovado (sessão de grilling)

## Problema

O GTA 6 é lançado em 19/11/2026. Queremos dois canais do YouTube — **Five Star
Clips** (en) e **Five Star Clips BR** (pt-BR) — publicando compilações
automáticas de clips de streamers jogando GTA 6, com o mínimo de trabalho
manual.

O pipeline existente gera conteúdo do zero (prompt → roteiro → TTS → imagens).
Cortes têm outro fluxo: fonte → seleção → corte → reenquadramento. Aproveitamos
render FFmpeg, TTS, contas, quota e upload; script, legendas e visuais não
entram.

A skill `davixavier-coder/skill-clips` serve só de referência de fluxo. O repo
não declara licença, então nenhum código dela é copiado.

## Grade

Por canal:

| Formato | Peças | pt-BR | en |
|---|---|---|---|
| GTA 6 Top Clips of the Day #N | Short + médio | 11h `America/Sao_Paulo` | 15h `America/New_York` |
| GTA 6 Funny Moments #N | Short + médio | 17h `America/Sao_Paulo` | 15h `America/Los_Angeles` |
| GTA 6 Top 10 Plays of the Week #N (quinta) | longo | 14h `America/Sao_Paulo` | 16h `America/New_York` |
| GTA 6 Funny Moments of the Week #N (sexta) | longo | 14h `America/Sao_Paulo` | 16h `America/New_York` |

Os horários miram a saída da escola: turnos da manhã e da tarde no Brasil;
saída única nos EUA, uma na costa Leste e outra na Oeste. Os horários ficam
guardados no fuso do público, então o horário de verão americano não exige
mudança.

Títulos são fixos por idioma, com contador `#N` por (canal, formato):

| en | pt-BR |
|---|---|
| GTA 6 Top Clips of the Day #N | GTA 6 Melhores Clipes do Dia #N |
| GTA 6 Funny Moments #N | GTA 6 Momentos Engraçados #N |
| GTA 6 Top 10 Plays of the Week #N | GTA 6 Top 10 Jogadas da Semana #N |
| GTA 6 Funny Moments of the Week #N | GTA 6 Momentos Engraçados da Semana #N |

Descrição e CTA do outro também são textos fixos por idioma.

Quota: 4 uploads (1.600 cada) + 2 thumbnails (50 cada) = 6.500/dia por canal;
na quinta e na sexta, mais 1 longo. Cada canal tem seu próprio projeto GCP.

## Formatos de vídeo

- **Short**: 9:16, ≤ 59 s. É o clip #1 do médio correspondente. O clip fica
  inteiro sobre um fundo borrado dele mesmo. Se passar de ~55 s, corta-se o
  começo, porque o clímax costuma estar no fim. O limite de 59 s não vem da
  plataforma: Shorts acima de 1 min com qualquer claim de Content ID são
  bloqueados no mundo inteiro.
- **Médio**: 16:9, 3–5 min, 8–12 clips inteiros, em contagem regressiva até o #1.
  Capítulos por posição ("#8" … "#1"), thumbnail automática.
- **Longo** (semanal): 16:9, 8–10 min, mesmas regras do médio.
- **Em todos**: crédito na tela durante o clip daquele streamer (ícone da
  plataforma + nome do canal). Sem lista de créditos na descrição, sem legenda
  da fala do streamer, sem filtro de spoiler.
- **Outro**: animação da marca (Kling, 16:9, 5 s). No 16:9, o último quadro é
  congelado até ~10 s para dar espaço às telas finais. No 9:16, o outro vai
  sobre um fundo borrado. Por cima: CTA em texto + TTS no idioma do canal +
  música da YouTube Audio Library.

## Um vídeo, dois idiomas

Seleção, cortes, ordem e crédito são neutros de idioma e são renderizados uma
vez (o "base"). A camada de idioma — cartão de título, CTA e TTS do outro — é
aplicada em um segundo passe barato, por idioma. Ranking privilegia clips que
funcionam sem entender a fala (`needs_speech=false`).

## Funil de clips

1. **Coleta** (08h BRT, diária): Twitch Helix `Get Clips` por `game_id` do GTA
   VI, últimas 24 h, com paginação. Pré-filtro por views.
2. **Download**: `yt-dlp` por clip (a Helix não dá URL de MP4).
3. **Classificação**: Gemini Flash assiste uma proxy em 480p do clip e devolve
   `{category, funny_score, hype_score, needs_speech}`. Se a quota estourar ou
   der erro, cai para o Groq/Llama só com o título. Começa no free tier. Se o
   fallback for frequente, ativa-se billing com teto de US$ 15/mês.
4. **Seleção**:
   - Top Clips: ordena por views; a classificação só desempata.
   - Funny: só `category=funny`, ordena por `funny_score × log(1+views)`.
   - Top Clips e Funny não repetem clip no mesmo dia.
   - Semanais: mesmo critério sobre 7 dias; reaproveitar clips dos diários é
     permitido.
   - O Short é o #1 do médio. É a única repetição permitida.
5. **Render**: base → passe de idioma → thumbnail.
6. **Aprovação**: um lote por dia no dashboard. Player pt/en por peça,
   aprovar/rejeitar, baixar o MP4, copiar título e descrição. Peça rejeitada
   sai do dia; não se publica vídeo ruim para cumprir grade.
7. **Upload**: manual pelo YouTube Studio até a auditoria do projeto GCP sair.
   Uploads via API de projeto não auditado ficam travados como privados.

## Fontes por fase

| Fonte | Como | Fase |
|---|---|---|
| Twitch | Helix `Get Clips` + yt-dlp | 19/11 |
| Link de live (YouTube/Kick) colado no dashboard | áudio → picos (ebur128) + Groq Whisper → LLM escolhe janelas → `yt-dlp --download-sections` | 26/11 |
| Kick automático | endpoint não oficial | depois |

Todas as fontes produzem o mesmo `Clip` e passam pelo mesmo classificador.

## Decisão de modelagem: `Edition` em vez de `Job`

O `Job` atual é "1 prompt → 1 vídeo → 1 conta", processado por um worker por
conta. Uma edição de cortes é compartilhada entre os dois canais e gera de 2 a
4 arquivos. Forçar isso no `Job` misturaria dois ciclos de vida. Por isso os
cortes ganham o módulo `app/clips/` com as tabelas `Clip`, `Edition` e
`EditionClip`, e um ponto de entrada próprio agendado pelo sistema operacional.
O `Job` volta a ser usado quando o upload automático entrar, com um job de
upload por (edição, canal).

Idioma, fuso, textos e voz de cada canal ficam num perfil
(`assets/input/channels/<perfil>.json`) referenciado por `Account.channel_profile`.
O pipeline de prompt continua lendo o `.env`.

## Fora de escopo agora

- Layout com a câmera do streamer em cima e o jogo embaixo.
- Kick automático.
- Upload automático (depende da auditoria).
- Trocar um clip e re-renderizar pela tela.
- Vídeos médios de WTF (precisa de aumento de quota).
