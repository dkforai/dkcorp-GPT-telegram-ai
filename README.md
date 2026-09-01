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

Bot memakai **long polling** dan tidak memerlukan webhook. Admin panel memakai HTTP server dalam container yang sama. Domain Railway hanya diperlukan untuk membuka halaman admin.

Pemrosesan update memakai controlled concurrency. User berbeda dapat diproses paralel sampai batas `MAX_CONCURRENT_UPDATES`, sedangkan pesan dan command dari Telegram ID yang sama memakai satu lock agar urutan company context, history, dan jawaban tidak tertukar. Pending update tidak dibuang saat startup.

## Fitur MVP

- Satu Telegram bot untuk seluruh tim
- Whitelist berdasarkan Telegram ID
- Master perusahaan dan membership user per perusahaan
- Perusahaan aktif dapat dilihat atau diganti melalui `/company`
- Jabatan, divisi, role level, profile, dan custom instruction per membership
- Communication profile `executive`, `manager`, `staff`, atau `default`
- Company profile, instruction, dan knowledge dari path yang dikonfigurasi per perusahaan
- History chat dipisahkan per user dan perusahaan di SQLite
- Pending Telegram update dipertahankan saat bot restart
- Controlled concurrency dengan urutan pesan per user tetap dijaga
- Provider abstraction OpenAI/DeepSeek melalui API yang kompatibel dengan OpenAI
- Telegram renderer untuk bold, italic, code, link, quote, spoiler, dan code block
- Raw HTML dari model di-escape dan fallback plain text tersedia
- Admin web dengan login, dashboard, company registry, dan user access directory
- Company Management untuk menambah, mengganti nama, mengaktifkan, dan menonaktifkan tenant
- Users & Access Management untuk whitelist dan membership per company
- Company Instruction Management dengan draft, preview, publish, dan riwayat versi
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

Isi `.env`. Pada database baru, `config/companies.json` dan `config/users.json` dipakai sebagai bootstrap awal. Setelah admin aktif, Company, user, dan membership dikelola dari dashboard. Untuk mengetahui Telegram ID, user yang belum terdaftar cukup mengirim `/start`; bot akan membalas ID yang perlu dikirim ke admin.

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

SQLite adalah source of truth runtime. File company dan user hanya menjadi bootstrap pada database kosong. Setelah database terisi, perubahan JSON tidak menimpa data SQLite ketika bot restart atau Railway redeploy.

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

Company Instruction dikelola melalui admin dengan alur **Draft → Preview → Publish**. Draft tidak memengaruhi bot. Setelah publish, bot membaca versi SQLite yang aktif pada pesan berikutnya. File `instruction_file` tetap menjadi fallback transisi selama company belum mempunyai versi database yang dipublikasikan.

Profile dan knowledge berbasis file dibaca ulang pada setiap pertanyaan. Hanya content dari company aktif yang dimasukkan ke prompt. History juga difilter menggunakan company ID yang sama.

MVP ini sengaja belum memakai embeddings/vector database. Seluruh knowledge company aktif dimasukkan ke prompt sampai batas `KNOWLEDGE_MAX_CHARS`. Jika knowledge mulai besar, langkah berikutnya adalah retrieval dengan filter wajib `company_id`, lalu `module_id` bila relevan.

`knowledge/company.md` saat ini masih menjadi combined legacy Funnel Coach untuk `dk-corp-group`. Ini menjaga perilaku bot lama selama transisi. Arsitektur final tetap memisahkan Company Instruction, Module Playbook, dan Business Knowledge.

Jangan masukkan rahasia seperti API key, password, data kartu, atau kredensial ke knowledge.

## Admin web

Admin web berjalan dalam process yang sama dengan bot Telegram. Admin hanya aktif ketika tiga variable berikut diisi bersama:

```env
ADMIN_USERNAME=dkadmin
ADMIN_PASSWORD=password-minimal-12-karakter
ADMIN_SESSION_SECRET=secret-acak-minimal-32-karakter
```

Untuk menjalankan melalui HTTP lokal, tambahkan:

```env
ADMIN_COOKIE_SECURE=false
ADMIN_PORT=8080
```

Halaman yang tersedia:

- `/admin/login` untuk autentikasi;
- `/admin` untuk dashboard;
- `/admin/companies` untuk company registry;
- `/admin/companies/new` untuk menambah company;
- `/admin/users` untuk user dan membership;
- `/admin/users/new` untuk menambah whitelist user dan membership pertama;
- `/admin/instructions` untuk status instruction semua company;
- `/admin/instructions/<company-id>` untuk draft, preview, publish, dan riwayat versi;
- `/health` untuk health check Railway.

Company, user whitelist, membership, dan Company Instruction sudah dapat dikelola melalui admin. Company ID serta Telegram ID dikunci setelah dibuat. Versi instruction lama dapat dipulihkan ke draft, lalu harus dipreview dan dipublikasikan kembali. Knowledge masih berbasis file dan belum mempunyai workflow publish.

## Deploy ke Railway

1. Push proyek ke repository GitHub private.
2. Di Railway, pilih **New Project → Deploy from GitHub repo**.
3. Tambahkan semua variable dari `.env.example` pada menu Variables. Jangan upload file `.env`.
4. Tambahkan volume Railway dan mount ke `/app/data` agar database SQLite tidak hilang saat redeploy.
5. Deploy. Railway akan membaca `Dockerfile` dan `railway.json`.
6. Buat public domain Railway untuk membuka admin web.
7. Gunakan `/health` sebagai health-check URL bila diperlukan.
8. Periksa deploy logs bot dan admin web.

Bot dan admin tetap memakai satu replica selama database menggunakan SQLite.

### Update data di Railway

Company, user, membership, dan Company Instruction dikelola dari dashboard admin. Knowledge masih melalui tahap transisi yang dijelaskan pada dokumen arsitektur. Database, versi instruction, dan history tetap aman selama volume `/app/data` terpasang.

## Environment variables

| Variable | Fungsi | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Token BotFather | wajib |
| `AI_PROVIDER` | `openai` atau `deepseek` | `openai` |
| `AI_API_KEY` | API key provider aktif | wajib |
| `AI_MODEL` | Nama model provider | sesuai provider |
| `AI_BASE_URL` | Override endpoint provider | sesuai provider |
| `DATABASE_PATH` | Lokasi SQLite | `data/bot.db` |
| `USERS_FILE` | JSON bootstrap whitelist untuk database kosong | `config/users.json` |
| `COMPANIES_FILE` | JSON bootstrap company untuk database kosong | `config/companies.json` |
| `ROLE_PROFILES_FILE` | JSON aturan communication profile | `config/role_profiles.json` |
| `PROJECT_ROOT` | Root aman untuk resolusi path company | `.` |
| `HISTORY_LIMIT` | Jumlah pesan lama yang dikirim ke AI | `12` |
| `MAX_CONCURRENT_UPDATES` | Batas update Telegram yang diproses paralel | `4` |
| `KNOWLEDGE_MAX_CHARS` | Batas karakter knowledge dalam prompt | `50000` |
| `MAX_RESPONSE_CHARS` | Batas total jawaban AI | `12000` |
| `LOG_LEVEL` | Level log | `INFO` |
| `ADMIN_USERNAME` | Username login admin | kosong, admin nonaktif |
| `ADMIN_PASSWORD` | Password admin minimal 12 karakter | kosong, admin nonaktif |
| `ADMIN_SESSION_SECRET` | Secret penandatangan cookie minimal 32 karakter | kosong, admin nonaktif |
| `ADMIN_COOKIE_SECURE` | Cookie hanya dikirim melalui HTTPS | `true` |
| `ADMIN_PORT` | Port admin lokal; Railway memakai `PORT` | `8080` |

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
- Controlled concurrency dibatasi maksimal 16 dan default 4
- Knowledge sudah dipisahkan per company, tetapi belum per module/division/clearance
- Admin writable untuk Company, user, membership, dan Company Instruction; Knowledge, Modules, dan Activity masih tahap berikutnya
- History dibatasi untuk konteks dan dipangkas menjadi 100 pesan per user-company

## Struktur

```text
app/
  admin.py        web admin, login, session, dan routes
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
static/admin/
templates/admin/
tests/
```
