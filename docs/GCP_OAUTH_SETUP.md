# Como Configurar OAuth no Google Cloud Console

## Passo 1: Criar/Acessar Projeto

1. Acesse: https://console.cloud.google.com/
2. No topo, clique no **seletor de projetos**
3. Clique em **"NEW PROJECT"**
   - Nome: `YouTube Automation` (ou qualquer nome)
   - Clique **"CREATE"**

---

## Passo 2: Habilitar YouTube Data API v3

1. No menu lateral (☰), vá em:
   ```
   APIs & Services > Library
   ```

2. Na busca, digite:
   ```
   YouTube Data API v3
   ```

3. Clique no resultado **"YouTube Data API v3"**

4. Clique no botão azul **"ENABLE"**

---

## Passo 3: Configurar Tela de Consentimento OAuth

1. No menu lateral, vá em:
   ```
   APIs & Services > OAuth consent screen
   ```

2. Escolha **"External"** (ou Internal se tiver Google Workspace)
   - Clique **"CREATE"**

3. Preencha os campos obrigatórios:
   - **App name:** `YouTube Video Automation`
   - **User support email:** Seu email
   - **Developer contact:** Seu email
   - Clique **"SAVE AND CONTINUE"**

4. Na tela **"Scopes"**, clique **"ADD OR REMOVE SCOPES"**
   - Na busca, digite: `youtube.upload`
   - Marque: `https://www.googleapis.com/auth/youtube.upload`
   - Clique **"UPDATE"**
   - Clique **"SAVE AND CONTINUE"**

5. Na tela **"Test users"**, clique **"ADD USERS"**
   - Digite seu email do YouTube
   - Clique **"ADD"**
   - Clique **"SAVE AND CONTINUE"**

6. Na tela de resumo, clique **"BACK TO DASHBOARD"**

---

## Passo 4: Criar Credenciais OAuth

1. No menu lateral, vá em:
   ```
   APIs & Services > Credentials
   ```

2. Clique no botão **"+ CREATE CREDENTIALS"** no topo

3. Escolha **"OAuth client ID"**

4. **Application type:** Selecione **"Web application"**

5. Preencha:
   - **Name:** `YouTube Dashboard OAuth`
   
   - **Authorized redirect URIs:** Clique em **"+ ADD URI"** e cole:
     ```
     http://127.0.0.1:8000/accounts/oauth/callback
     ```
     
   - Clique **"CREATE"**

6. Um popup aparecerá com suas credenciais:
   ```
   Client ID: xxxxx.apps.googleusercontent.com
   Client Secret: GOCSPX-xxxxx
   ```

7. **COPIE** ambos e guarde (você vai precisar)

8. Clique **"OK"**

---

## Passo 5: Usar no Dashboard

1. Abra seu arquivo `.env` e adicione:
   ```bash
   # OAuth Web Application Credentials
   YOUTUBE_WEB_CLIENT_ID=xxxxx.apps.googleusercontent.com
   YOUTUBE_WEB_CLIENT_SECRET=GOCSPX-xxxxx
   ```

2. Reinicie o dashboard

3. Acesse: http://127.0.0.1:8000/accounts

4. Clique em **"Add Account via OAuth"**

5. Siga o fluxo de autorização no navegador

6. Pronto! Conta adicionada automaticamente! 🎉

---

## 🔧 Troubleshooting

### Erro: "Access blocked: This app's request is invalid"
- Certifique-se que adicionou o Redirect URI correto
- URL deve ser EXATAMENTE: `http://127.0.0.1:8000/accounts/oauth/callback`

### Erro: "The OAuth client was not found"
- Verifique se copiou o Client ID e Secret corretamente
- Verifique se estão no arquivo `.env`

### Erro: "redirect_uri_mismatch"
- O redirect URI no Google Cloud deve ser EXATAMENTE igual
- Não use `localhost`, use `127.0.0.1`
- Não esqueça a porta `:8000`

---

## 📌 Resumo Rápido

```
1. Google Cloud Console → Novo Projeto
2. Habilitar: YouTube Data API v3
3. OAuth consent screen → External → Adicionar scope youtube.upload
4. Credentials → OAuth client ID → Web application
5. Redirect URI: http://127.0.0.1:8000/accounts/oauth/callback
6. Copiar Client ID e Secret → Adicionar no .env
7. Dashboard → Add Account via OAuth → Autorizar
```

**Tempo total:** ~5 minutos ⏱️
