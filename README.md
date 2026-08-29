# Internal Telegram AI Bot

MVP bot Telegram internal dengan whitelist user, profil per user, custom instruction, knowledge dari file Markdown, history sederhana di SQLite, dan AI provider yang bisa diganti antara OpenAI dan DeepSeek.

## Cara kerja

```text
Telegram → whitelist Telegram ID → profil + custom instruction
         → knowledge/*.md + history SQLite → AI provider → Telegram
```

Bot memakai **long polling**, jadi tidak memerlukan domain, webhook, atau web server. Ini cocok untuk Railway worker sederhana.

## Fitur MVP

- Satu Telegram bot untuk seluruh tim
- Whitelist berdasarkan Telegram ID
- Profil `name`, `role`, `division`, dan `custom_instruction`
- Knowledge base dari seluruh file `.md` di folder `knowledge/`
- History chat per user di SQLite
- Provider abstraction OpenAI/DeepSeek melalui API yang kompatibel dengan OpenAI
- Perintah `/start`, `/help`, `/whoami`, dan `/reset`
- Jawaban panjang otomatis dipecah agar muat di Telegram

## Menjalankan lokal

Prasyarat: Python 3.12+ dan bot token dari `@BotFather`.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Isi `.env`, lalu tambahkan user ke `config/users.json`. Formatnya mengikuti `config/users.example.json`. Untuk mengetahui Telegram ID, user yang belum terdaftar cukup mengirim `/start`; bot akan membalas ID yang perlu dikirim ke admin.

Jalankan:

```bash
python -m app.main
```

## Konfigurasi OpenAI

```env
AI_PROVIDER=openai
AI_API_KEY=sk-...
AI_MODEL=gpt-5.4-mini
AI_BASE_URL=
```

Implementasi memakai Chat Completions API sebagai kontrak bersama kedua provider. Model default dapat diganti melalui `AI_MODEL` tanpa perubahan kode.

## Konfigurasi DeepSeek

```env
AI_PROVIDER=deepseek
AI_API_KEY=...
AI_MODEL=deepseek-chat
AI_BASE_URL=https://api.deepseek.com
```

## User dan whitelist

Edit `config/users.json`:

```json
[
  {
    "telegram_id": 123456789,
    "name": "Dyna",
    "role": "Content Creator",
    "division": "Marketing",
    "custom_instruction": "Buat jawaban praktis dan sesuai brand.",
    "active": true
  }
]
```

File ini disinkronkan ke SQLite saat bot menyala. Restart service setelah mengubah user. User yang dihapus dari JSON tidak otomatis dihapus dari database; untuk mencabut akses, pertahankan entry-nya dan ubah `active` menjadi `false`.

## Knowledge base

Taruh file Markdown di `knowledge/`, termasuk subfolder bila perlu. Bot membaca ulang semua file pada setiap pertanyaan, sehingga update knowledge tidak memerlukan restart.

MVP ini sengaja belum memakai embeddings/vector database. Seluruh knowledge dimasukkan ke prompt sampai batas `KNOWLEDGE_MAX_CHARS`. Jika knowledge mulai besar, langkah evolusi berikutnya adalah retrieval per dokumen atau per division.

Jangan masukkan rahasia seperti API key, password, data kartu, atau kredensial ke knowledge.

## Deploy ke Railway

1. Push proyek ke repository GitHub private.
2. Di Railway, pilih **New Project → Deploy from GitHub repo**.
3. Tambahkan semua variable dari `.env.example` pada menu Variables. Jangan upload file `.env`.
4. Tambahkan volume Railway dan mount ke `/app/data` agar database SQLite tidak hilang saat redeploy.
5. Deploy. Railway akan membaca `Dockerfile` dan `railway.json`.
6. Periksa logs hingga muncul pesan sinkronisasi user dan bot mulai polling.

Service ini tidak menyediakan HTTP health check karena berjalan sebagai worker polling. Jangan mengaktifkan health-check URL Railway.

### Update user atau knowledge di Railway

Untuk alur paling sederhana, edit file di repository lalu push. Railway akan redeploy. Database history tetap aman selama volume `/app/data` terpasang.

## Environment variables

| Variable | Fungsi | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Token BotFather | wajib |
| `AI_PROVIDER` | `openai` atau `deepseek` | `openai` |
| `AI_API_KEY` | API key provider aktif | wajib |
| `AI_MODEL` | Nama model provider | sesuai provider |
| `AI_BASE_URL` | Override endpoint provider | sesuai provider |
| `DATABASE_PATH` | Lokasi SQLite | `data/bot.db` |
| `KNOWLEDGE_DIR` | Folder Markdown | `knowledge` |
| `USERS_FILE` | JSON sumber whitelist | `config/users.json` |
| `HISTORY_LIMIT` | Jumlah pesan lama yang dikirim ke AI | `12` |
| `KNOWLEDGE_MAX_CHARS` | Batas karakter knowledge dalam prompt | `50000` |
| `MAX_RESPONSE_CHARS` | Batas total jawaban AI | `12000` |
| `LOG_LEVEL` | Level log | `INFO` |

## Pengujian

```bash
pip install pytest
pytest -q
```

Pengujian tidak memanggil Telegram atau AI API.

## Batas MVP yang disengaja

- Text-only, belum mendukung dokumen, gambar, voice note, atau tool calling
- Satu konfigurasi AI provider untuk seluruh bot
- SQLite cocok untuk satu instance bot; jangan menjalankan beberapa replica
- Knowledge belum difilter per role/division
- Belum ada admin panel; user dikelola lewat JSON dan Git
- History dibatasi untuk konteks dan dipangkas menjadi 100 pesan per user

## Struktur

```text
app/
  bot.py          handler Telegram
  config.py       environment settings
  database.py     SQLite user dan history
  knowledge.py    loader file Markdown
  providers.py    abstraction AI provider
  prompts.py      penyusun system prompt
  main.py         startup
config/
  users.json
knowledge/
tests/
```
