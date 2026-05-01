# ⚡ Setup Rápido - OAuth em 5 Minutos

## 🎯 Objetivo
Configurar OAuth para usar o botão **"Add via OAuth (Easy)"** no dashboard.

---

## 📋 Passo a Passo

### 1️⃣ Google Cloud Console (2 min)

**Acesse:** https://console.cloud.google.com/apis/credentials

**Crie Web OAuth Client:**
```
1. Click: "+ CREATE CREDENTIALS"
2. Select: "OAuth client ID"
3. Application type: "Web application"
4. Name: "YouTube Dashboard"
5. Authorized redirect URIs → ADD URI:
   http://127.0.0.1:8000/accounts/oauth/callback
6. Click: "CREATE"
7. COPY: Client ID e Client Secret
```

---

### 2️⃣ Configurar .env (1 min)

Abra seu arquivo `.env` e adicione:

```bash
# YouTube OAuth Web Credentials
YOUTUBE_WEB_CLIENT_ID=xxxxx.apps.googleusercontent.com
YOUTUBE_WEB_CLIENT_SECRET=GOCSPX-xxxxx
```

---

### 3️⃣ Habilitar YouTube API (1 min)

**Se ainda não habilitou:**

1. Acesse: https://console.cloud.google.com/apis/library/youtube.googleapis.com
2. Click: **"ENABLE"**

---

### 4️⃣ Configurar OAuth Consent (1 min)

**Se ainda não configurou:**

1. Acesse: https://console.cloud.google.com/apis/credentials/consent
2. User Type: **"External"**
3. App name: "YouTube Automation"
4. User support email: Seu email
5. Developer email: Seu email
6. Scopes: Adicionar `youtube.upload`
7. Test users: Adicionar seu email

---

### 5️⃣ Usar no Dashboard (30 seg)

1. **Reinicie o dashboard:**
   ```bash
   # Pressione Ctrl+C no terminal do dashboard
   python -m uvicorn app.web.main:app --host 127.0.0.1 --port 8000 --reload
   ```

2. **Acesse:** http://127.0.0.1:8000/accounts

3. **Click:** botão verde **"Add via OAuth (Easy)"**

4. **Autorize** no navegador que abrir

5. **Pronto!** Conta adicionada automaticamente! 🎉

---

## ✅ Checklist de Verificação

- [ ] Criei OAuth Web Client no Google Cloud Console
- [ ] Adicionei redirect URI: `http://127.0.0.1:8000/accounts/oauth/callback`
- [ ] Copiei Client ID e Secret para o `.env`
- [ ] Habilitei YouTube Data API v3
- [ ] Configurei OAuth consent screen
- [ ] Reiniciei o dashboard
- [ ] Testei o botão "Add via OAuth"

---

## 🚨 Erros Comuns

### "OAuth credentials not configured"
✅ **Solução:** Adicione `YOUTUBE_WEB_CLIENT_ID` e `YOUTUBE_WEB_CLIENT_SECRET` no `.env`

### "redirect_uri_mismatch"
✅ **Solução:** Verifique se o redirect URI no Google Cloud é EXATAMENTE:
```
http://127.0.0.1:8000/accounts/oauth/callback
```
⚠️ Use `127.0.0.1`, NÃO `localhost`

### "Access blocked: This app's request is invalid"
✅ **Solução:** Configure o OAuth consent screen e adicione seu email como test user

---

## 🆚 Comparação

| Método | Tempo | Dificuldade |
|--------|-------|-------------|
| **OAuth (Novo)** | 30 seg | ⭐ Fácil |
| Manual (Antigo) | 5 min | ⭐⭐⭐ Difícil |

---

## 📚 Documentação Completa

Para instruções detalhadas com screenshots, veja:
**[docs/GCP_OAUTH_SETUP.md](docs/GCP_OAUTH_SETUP.md)**

---

## 🎬 Próximos Passos

Depois de adicionar a conta via OAuth:

1. Vá para: http://127.0.0.1:8000/jobs
2. Click: "Create Job"
3. Selecione a conta que acabou de adicionar
4. Digite um prompt
5. Click: "Create Job"
6. Pronto! O worker vai processar automaticamente! 🚀
