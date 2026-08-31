# Internal Telegram AI Bot

MVP bot Telegram internal multi-company dengan whitelist user, membership per perusahaan, communication profile berbasis jabatan, custom instruction, knowledge dari file Markdown, history sederhana di SQLite, dan AI provider yang bisa diganti antara OpenAI dan DeepSeek.

Dokumen arsitektur dan konsep aplikasi dipelihara di `docs/architecture.md`. Dokumen Markdown tersebut adalah source of truth selama pengembangan dan akan dibuat menjadi PDF setelah konsep stabil.

## Cara kerja

```text
Telegram → whitelist Telegram ID → company membership
         → active company + communication profile
         → company instruction + company knowledge
         → company-scoped history SQLite → AI provider
         → safe HTML renderer → Telegram
```

Bot memakai **long polling**, jadi tidak memerlukan domain, webhook, atau web server. Ini cocok untuk Railway worker sederhana.

## Fitur MVP

- Satu Telegram bot untuk seluruh tim
- Whitelist berdasarkan Telegram ID
- Master perusahaan dan membership user per perusahaan
- Perusahaan aktif dapat dilihat atau diganti melalui `/company`
- Jabatan, divisi, role level, profile, dan custom instruction per membership
- Communication profile `executive`, `manager`, `staff`, atau `default`
- Company profile, instruction, dan knowledge dari path yang dikonfigurasi per perusahaan
- History chat dipisahkan per user dan perusahaan di SQLite
- Provider abstraction OpenAI/DeepSeek melalui API yang kompatibel dengan OpenAI
- Telegram renderer untuk bold, italic, code, link, quote, spoiler, dan code block
- Raw HTML dari model di-escape dan fallback plain text tersedia
- Perintah `/start`, `/help`, `/company`, `/whoami`, dan `/reset`
- Jawaban panjang otomatis dipecah agar muat di Telegram

## Menjalankan lokal

Prasyarat: Python 3.12+ dan bot token dari `@BotFather`.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Isi `.env`, tambahkan perusahaan ke `config/companies.json`, lalu tambahkan user dan membership ke `config/users.json`. Formatnya mengikuti file `.example.json`. Untuk mengetahui Telegram ID, user yang belum terdaftar cukup mengirim `/start`; bot akan membalas ID yang perlu dikirim ke admin.

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

## Company

Edit `config/companies.json`:

```json
[
  {
    "id": "amazing-malang",
    "name": "Amazing Malang",
    "profile_file": "companies/amazing-malang/profile.md",
    "instruction_file": "companies/amazing-malang/company_instruction.md",
    "knowledge_dir": "companies/amazing-malang/knowledge",
    "active": true
  }
]
```

Company ID hanya memakai huruf kecil, angka, dan tanda minus. Semua path harus relatif terhadap project root dan tidak boleh memakai `..`.

## User, whitelist, dan membership

Edit `config/users.json`:

```json
[
  {
    "telegram_id": 123456789,
    "name": "Dyna",
    "active": true,
    "memberships": [
      {
        "company_id": "amazing-malang",
        "job_title": "Content Creator",
        "division": "Marketing",
        "role_level": "staff",
        "communication_profile": "staff",
        "custom_instruction": "Buat jawaban praktis dan sesuai brand.",
        "default": true,
        "active": true
      }
    ]
  }
]
```

Satu user dapat mempunyai beberapa membership dan jabatan yang berbeda pada setiap perusahaan. Hanya satu membership boleh memakai `default: true`. Contoh: General Manager dapat memakai `executive`, Head of Department memakai `manager`, dan Content Creator memakai `staff`.

File company dan user adalah source of truth yang disinkronkan ke SQLite saat bot menyala. Restart service setelah mengubah konfigurasi. Entry user, company, atau membership yang dihapus dari JSON dinonaktifkan di database; penggunaan `active: false` tetap disarankan agar pencabutan akses terlihat jelas di Git.

Saat migrasi pertama, history lama yang belum memiliki company ID dipindahkan ke membership default. Jika user hanya memiliki satu membership, membership tersebut dipakai otomatis.

Command Telegram:

```text
/company                    daftar perusahaan yang dapat diakses
/company amazing-malang     memilih perusahaan aktif
/whoami                     melihat membership dan profile aktif
/reset                      menghapus history perusahaan aktif saja
```

## Communication profile

Aturan terpusat berada di `config/role_profiles.json`:

| Profile | Bentuk jawaban |
|---|---|
| `executive` | Strategis, opsi, trade-off, risiko, dan keputusan |
| `manager` | Taktis, action plan, resource, timeline, dan KPI |
| `staff` | Operasional, langkah, checklist, contoh, dan standar selesai |
| `default` | Seimbang ketika profile tidak dikenali |

Jika `communication_profile` membership kosong, aplikasi mencoba mencocokkan `job_title` dengan `role_aliases`. Jika tidak ditemukan, aplikasi memakai `default`. Custom instruction membership hanya berlaku pada perusahaan tersebut.

## Company instruction dan knowledge

Setiap company dapat menunjuk tiga sumber berbeda:

- `profile_file` untuk identitas dan konteks perusahaan;
- `instruction_file` untuk aturan AI perusahaan;
- `knowledge_dir` untuk fakta, SOP, produk, dan referensi.

Bot membaca ulang file pada setiap pertanyaan, sehingga perubahan isi tidak memerlukan restart. Hanya content dari company aktif yang dimasukkan ke prompt. History juga difilter menggunakan company ID yang sama.

MVP ini sengaja belum memakai embeddings/vector database. Seluruh knowledge company aktif dimasukkan ke prompt sampai batas `KNOWLEDGE_MAX_CHARS`. Jika knowledge mulai besar, langkah berikutnya adalah retrieval dengan filter wajib `company_id`, lalu `module_id` bila relevan.

`knowledge/company.md` saat ini masih menjadi combined legacy Funnel Coach untuk `dk-corp-group`. Ini menjaga perilaku bot lama selama transisi. Arsitektur final tetap memisahkan Company Instruction, Module Playbook, dan Business Knowledge.

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
| `USERS_FILE` | JSON sumber whitelist | `config/users.json` |
| `COMPANIES_FILE` | JSON master company dan lokasi content | `config/companies.json` |
| `ROLE_PROFILES_FILE` | JSON aturan communication profile | `config/role_profiles.json` |
| `PROJECT_ROOT` | Root aman untuk resolusi path company | `.` |
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

## Telegram Response Renderer

AI menghasilkan subset Markdown yang terbatas. `app/telegram_renderer.py` meng-escape raw HTML lalu mengubah markup yang didukung menjadi Telegram HTML. Jawaban dikirim dengan `parse_mode=HTML`, link preview dinonaktifkan, dan parsing yang ditolak Telegram otomatis memakai fallback plain text.

Format yang didukung:

````text
**bold**
*italic*
`inline code`
```code block```
- bullet
> quote
||spoiler||
[label](https://example.com)
````

Conversation Delivery Policy seperti batas kata, satu pesan satu tujuan, dan progressive disclosure sengaja belum digabung ke renderer. Lapisan tersebut akan dikembangkan terpisah.

## Batas MVP yang disengaja

- Text-only, belum mendukung dokumen, gambar, voice note, atau tool calling
- Satu konfigurasi AI provider untuk seluruh bot
- SQLite cocok untuk satu instance bot; jangan menjalankan beberapa replica
- Knowledge sudah dipisahkan per company, tetapi belum per module/division/clearance
- Belum ada admin panel; user dikelola lewat JSON dan Git
- History dibatasi untuk konteks dan dipangkas menjadi 100 pesan per user-company

## Struktur

```text
app/
  bot.py          handler Telegram
  company_context.py loader content company aktif
  config.py       environment settings
  database.py     SQLite user, company, membership, session, dan history
  knowledge.py    loader file Markdown
  providers.py    abstraction AI provider
  prompts.py      penyusun system prompt
  role_profiles.py loader dan resolver communication profile
  telegram_renderer.py safe HTML renderer Telegram
  main.py         startup
config/
  companies.json
  users.json
  role_profiles.json
companies/
  <company-id>/
    profile.md
    company_instruction.md
    knowledge/
docs/
  architecture.md
knowledge/
tests/
```
