# Gemini Web2API - Cloudflare Workers Deployment Guide

## 📖 Project Overview

Gemini Web2API is a serverless proxy service deployed on Cloudflare Workers that converts the Google Gemini Web interface into an OpenAI-compatible API. No server required, no API key needed (optional), ready to use out of the box.

### Key Features

- **Zero-Cost Deployment**: Based on Cloudflare Workers Free plan (100,000 requests/day)
- **Global Acceleration**: Automatically deployed to 300+ Cloudflare edge locations worldwide
- **OpenAI Compatible**: Fully compatible with `/v1/chat/completions` and `/v1/models` endpoints
- **Typewriter Streaming Output**: True SSE (Server-Sent Events) streaming response
- **Multi-Fingerprint Rotation**: 8 browser fingerprints + 6 language preferences randomized to reduce detection risk
- **Multi-Cookie Rotation**: Supports configuring multiple Google account cookies with random rotation
- **Concurrency Safe**: Request-level configuration isolation, completely eliminating cross-talk under high concurrency
- **Tool Calling Support**: Compatible with OpenAI Function Calling format

### Use Cases

- Provide a free Gemini API backend for clients such as NextChat, Cherry Studio, ChatBox, etc.
- Serve as a Gemini model backend in tools like WorkBuddy
- AI integration for personal learning, research, and lightweight projects

---

## 🚀 Quick Deployment

### Step 1: Log in to Cloudflare

1. Open [Cloudflare Dashboard](https://dash.cloudflare.com)
2. Log in to your Cloudflare account (sign up for free if you don't have one)
3. Navigate to **Workers & Pages** in the left sidebar

### Step 2: Create Worker

1. Click **Create application** → **Create Worker**
2. Name your Worker (e.g., `api`)
3. Click the **Deploy** button
4. Click **Edit code**
5. Clear the default code in the editor
6. Paste the complete code of this project into the editor
7. Click **Save and deploy** in the top right corner

### Step 3: Get Test URL

After successful deployment, your API address will be:

```
https://<your-worker-name>.<your-subdomain>.workers.dev
```

For example: `https://api.geminai.workers.dev`

### Step 4: Verify Deployment

Visit the following URL in your browser:

```
https://<your-worker>.workers.dev/health
```

If you see a JSON response similar to the following, the deployment was successful:

```json
{
  "status": "ok",
  "version": "1.5.0-cf-multifingerprint",
  "platform": "Cloudflare Workers",
  "models": ["gemini-3.6-flash", "gemini-3.5-flash", "..."],
  "hasCookie": false,
  "hasSapisid": false
}
```

---

## 🔧 Client Configuration

### NextChat (ChatGPT-Next-Web)

| Setting | Value |
|---------|-------|
| API Type | OpenAI |
| Endpoint | `https://<your-worker>.workers.dev/v1` |
| API Key | `sk-gemini` (default key) |
| Model | `gemini-3.6-flash` |

### Cherry Studio

| Setting | Value |
|---------|-------|
| API Endpoint | `https://<your-worker>.workers.dev/v1` |
| API Key | `sk-gemini` |
| Model | `gemini-3.6-flash` |

### ChatBox

| Setting | Value |
|---------|-------|
| API Mode | OpenAI API |
| API Domain | `https://<your-worker>.workers.dev` |
| API Path | `/v1/chat/completions` |
| API Key | `sk-gemini` |

### Test with curl

```bash
# Non-streaming request
curl https://<your-worker>.workers.dev/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-gemini" \
  -d '{
    "model": "gemini-3.6-flash",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": false
  }'

# Streaming request (typewriter effect)
curl -N https://<your-worker>.workers.dev/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-gemini" \
  -d '{
    "model": "gemini-3.6-flash",
    "messages": [{"role": "user", "content": "Tell me a story"}],
    "stream": true
  }'
```

---

## ⚙️ Environment Variables Configuration (Optional)

Configure in Cloudflare Dashboard → Workers → Your Worker → Settings → Variables → Environment Variables:

### Authentication

| Variable | Description | Example Value |
|----------|-------------|---------------|
| `COOKIE_STRING` | Gemini Cookie, separate multiple with `\|` | `cookie1\| cookie2\| cookie3` |
| `SAPISID` | SAPISID value, separate multiple with `\|` | `sapisid1\| sapisid2\| sapisid3` |
| `API_KEYS` | API key whitelist (JSON array) | `["sk-gemini", "my-key"]` |

### Gemini Configuration

| Variable | Description | Example Value |
|----------|-------------|---------------|
| `GEMINI_BL` | Gemini build label (update when encountering 405) | `boq_assistant-bard-web-server_20260716.08_p0` |
| `DEFAULT_MODEL` | Default model | `gemini-3.6-flash` |
| `AUTH_USER` | Multi-account index | `0` |

### Performance Tuning

| Variable | Description | Default Value |
|----------|-------------|---------------|
| `RETRY_ATTEMPTS` | Number of retries | `3` |
| `RETRY_DELAY_SEC` | Retry interval (seconds) | `2` |
| `REQUEST_TIMEOUT_SEC` | Request timeout (seconds) | `28` |
| `FINGERPRINT_JITTER_MS` | Max random jitter delay (ms) | `1500` |
| `RATE_LIMIT_MAX` | Max requests per rate limit window | `3000` |
| `RATE_LIMIT_WINDOW` | Rate limit window (seconds) | `60` |

---

## 🍪 Obtaining Gemini Cookies

### Why are Cookies needed?

Anonymous requests are easily rate-limited by Gemini (returning HTTP 429 errors). Configuring valid cookies can:
- Significantly reduce the chance of rate limiting
- Improve routing quality for Pro models
- Provide a more stable service experience

### Steps to Obtain

1. Open Chrome / Edge browser
2. Visit https://gemini.google.com/app and log in to your Google account
3. Press **F12** to open Developer Tools
4. Go to the **Application** tab
5. In the left panel, select **Cookies** → `https://gemini.google.com`
6. Find the following cookies and copy their values:
   - `__Secure-1PSID`
   - `__Secure-3PSID`
   - `SAPISID`
7. Combine them into a complete Cookie string:
   ```
   __Secure-1PSID=your_value; __Secure-3PSID=your_value; SAPISID=your_value
   ```

### Multi-Account Configuration

If you have multiple Google accounts, separate multiple cookies with `|`:

```
COOKIE_STRING = "cookie_account1| cookie_account2| cookie_account3"
SAPISID = "sapisid_1| sapisid_2| sapisid_3"
```

Each request will randomly pick one cookie, greatly reducing the rate-limiting risk for any single account.

---

## 🔄 Updating the BL Version

If you encounter an `HTTP 405: Method Not Allowed` error, it means the Gemini frontend has been updated and the build label needs to be refreshed:

1. Open https://gemini.google.com/app in your browser
2. Press **F12** → **Network** tab
3. Search for `boq_assistant` in any request URL
4. Copy the latest version string, for example:
   ```
   boq_assistant-bard-web-server_20260730.02_p0
   ```
5. Update the `GEMINI_BL` environment variable or the `geminiBl` setting in code

---

## 🎭 Multi-Fingerprint Rotation Mechanism

This project includes a built-in browser fingerprint rotation system. Each request randomly selects different browser identifiers:

| Fingerprint Type | Pool Size | Description |
|------------------|-----------|-------------|
| User-Agent | 8 types | Weighted random, simulating real browser market share |
| Accept-Language | 6 types | Uniform random, simulating users from different regions |
| Sec-Ch-Ua | 3 types | Chrome version headers (only added for Chrome UA) |
| Random Delay | 0-1500ms | Adds a random jitter delay before request to simulate human behavior |

---

## 🛡️ Security Recommendations

1. **Change Default API Key**: Replace `sk-gemini` in `apiKeys` with your own secret key
2. **Set Rate Limiting**: Adjust `RATE_LIMIT_MAX` based on your actual usage
3. **Periodically Update Cookies**: Google cookies expire and should be refreshed periodically
4. **Never Share Cookies**: Cookies are equivalent to your Google account credentials

---

## ❓ Frequently Asked Questions (FAQ)

### Q: Returned `empty response from server`

**Cause**: NextChat streaming parsing issue.  
**Solution**: Ensure you are using the latest code version (SSE format has been fixed).

### Q: Returned `HTTP 429: Too Many Requests`

**Cause**: Gemini rate-limited the request; anonymous requests have stricter rate limits.  
**Solution**: Configure valid `COOKIE_STRING` and `SAPISID`.

### Q: Returned `HTTP 405: Method Not Allowed`

**Cause**: The BL version is outdated.  
**Solution**: Update the `geminiBl` configuration (see the "Updating the BL Version" section above).

### Q: Returned `invalid api key`

**Cause**: Incorrect client API key configuration.  
**Solution**: Verify that the client is configured with the correct API Key (default is `sk-gemini`).

### Q: Cross-talk occurs when used in WorkBuddy

**Cause**: Concurrent multi-model requests were sharing global configurations.  
**Solution**: The current version resolves this issue via request-level configuration isolation.

---

## 📊 Supported Models List

| Model ID | Type | Description |
|----------|------|-------------|
| `gemini-3.6-flash` | FAST | Latest all-round model |
| `gemini-3.5-flash` | FAST | Alias for 3.6 Flash |
| `gemini-3.5-flash-thinking` | THINKING | Deep thinking mode |
| `gemini-3.1-pro` | PRO | Professional version (requires Cookie) |
| `gemini-auto` | AUTO | Automatic model selection |
| `gemini-3.5-flash-thinking-lite` | DYNAMIC | Adaptive dynamic thinking |
| `gemini-flash-lite` | LITE | Lightweight fast model |

Supports overriding thinking mode via the `@think=` parameter:
- `gemini-3.6-flash@think=0` — Flash model + deep thinking
- `gemini-3.1-pro@think=4` — Pro model + auto thinking

---

## 📝 Changelog

| Version | Date | Changes |
|---------|------|---------|
| 1.5.0 | 2026-07-31 | Added multi-fingerprint rotation, multi-cookie rotation, random delay mechanism |
| 1.4.0 | 2026-07-30 | Fixed concurrency cross-talk, rate limit memory safety |
| 1.3.0 | 2026-07-29 | Fixed SSE streaming format, NextChat compatibility |
| 1.0.0 | 2026-07-16 | Initial release, ported from gemini-web2api v1.1.0 |

---

## 📄 License

Ported based on the original [gemini-web2api](https://github.com/your-repo/gemini-web2api) project and follows the open source license of the original project.
