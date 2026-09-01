# Arsitektur dan Konsep DK Corp Telegram AI

## Kontrol dokumen

| Atribut | Nilai |
|---|---|
| Status | Living document |
| Versi | 0.8 |
| Terakhir diperbarui | 31 Agustus 2026 |
| Source of truth | Repository `dkcorp-GPT-telegram-ai` |
| Format akhir | Markdown selama pengembangan, PDF setelah konsep stabil |

Dokumen ini wajib diperbarui bersama perubahan fitur, aturan, data model, alur, keamanan, atau deployment. PDF bukan source of truth selama aplikasi masih aktif dikembangkan.

## 1. Tujuan aplikasi

DK Corp Telegram AI adalah asisten internal yang menyediakan akses AI melalui satu bot Telegram. Sistem mengenali identitas dan konteks organisasi setiap user, menyusun jawaban sesuai level komunikasi, menggunakan knowledge perusahaan, serta menyimpan konteks percakapan sederhana.

Tujuan utama:

- user tidak perlu menulis system prompt atau mengunggah knowledge sendiri;
- jawaban menyesuaikan jabatan, divisi, dan level komunikasi;
- GM menerima jawaban strategis, manager menerima jawaban taktis, dan staff menerima jawaban operasional;
- provider AI dapat diganti tanpa mengubah alur Telegram;
- MVP mudah dijalankan, diaudit, dan dikembangkan.

## 2. Prinsip desain

1. Identitas, hak akses, gaya komunikasi, dan knowledge adalah empat konsep berbeda.
2. Jabatan tidak menentukan gaya secara hardcoded. User mempunyai `communication_profile` tersendiri.
3. Informasi yang tidak tersedia tidak boleh dikarang.
4. Knowledge merupakan referensi data, bukan instruksi yang boleh mengambil alih system prompt.
5. Pada fase transisi, perubahan konfigurasi dilakukan melalui file yang dapat diaudit di Git. Target final memakai database versioned melalui admin control plane.
6. MVP memakai komponen minimum yang cukup untuk satu instance.
7. Sinkronisasi konfigurasi bersifat fail-closed: entry yang hilang dari source of truth dinonaktifkan di database.

## 3. Arsitektur logis

```text
User Telegram
    ↓
Telegram Bot
    ↓
Authentication
Whitelist Telegram ID
    ↓
User Context
user identity + active company membership
    ↓
Authorization Policy
company, module, dan knowledge yang boleh diakses
    ↓
Company Context
company instruction + business knowledge
    ↓
Active Module
AI Module Playbook + module-scoped knowledge
    ↓
Communication Profile
    ├── executive → strategic
    ├── manager   → tactical-managerial
    ├── staff     → operational-execution
    └── default   → balanced
    ↓
Custom Instruction per user
    ↓
Knowledge Loader
file Markdown perusahaan
    ↓
Prompt Composer
global policy + user context + communication profile
+ custom instruction + knowledge
    ↓
Chat History SQLite
    ↓
AI Provider Abstraction
    ├── OpenAI
    └── DeepSeek
    ↓
Conversation Delivery Policy
pendek, bertahap, dan satu tujuan (belum diimplementasikan penuh)
    ↓
Telegram Response Renderer
safe HTML + split + fallback plain text
    ↓
Jawaban ke user
```

## 4. Status implementasi

| Lapisan | Status | Implementasi saat ini |
|---|---|---|
| Telegram Bot | Sudah | Long polling dan respons HTML terformat |
| Update Reliability | Sudah | Pending update dipertahankan, concurrency terbatas, dan serialization per user |
| Authentication | Sudah | Whitelist Telegram ID |
| User Context | Sudah | Identity global dan membership per perusahaan |
| Communication Profile | Sudah | Config terpusat dengan override per membership |
| Custom Instruction | Sudah | Field global user dan field per membership |
| Knowledge Loader | Sudah | Folder knowledge ditentukan oleh company aktif |
| Chat History | Sudah | SQLite dipisahkan per user dan perusahaan |
| Provider Abstraction | Sudah | OpenAI dan DeepSeek compatible API |
| Telegram Response Renderer | Sudah | Safe HTML, split, link preview off, dan fallback plain text |
| Conversation Delivery Policy | Belum | Akan mengatur panjang, ritme, dan progressive disclosure |
| Multi-company membership | Sudah | Tabel membership dan konfigurasi JSON |
| Company router dan active context | Sudah | Command `/company` dan session active company |
| Company-scoped instruction | Sudah | File profile, instruction, dan knowledge ditentukan per company |
| Authorization per knowledge | Sebagian | Sudah company-scoped; module, division, dan clearance belum |
| Response Validator | Sebagian | Batas panjang, split, escape HTML, dan fallback; belum ada policy classifier |
| Admin Panel | Sebagian | Login, dashboard, Companies, dan Users & Access read-only |
| Retrieval/RAG | Belum | Seluruh knowledge dimuat sampai batas karakter |

## 5. Target arsitektur multi-company

DK Corp memakai satu bot dan satu codebase untuk seluruh perusahaan. Perusahaan, membership user, modul, instruction, dan knowledge diperlakukan sebagai data terpisah. Sistem tidak membuat satu bot untuk setiap perusahaan.

```text
Telegram ID
    ↓
User
    ↓
User–Company Membership
company + job title + division + role level
    ↓
Active Company Context
    ├── Company Profile
    ├── Company Instruction
    ├── Business Knowledge
    └── Modules yang diizinkan
            ↓
        Active Module
        AI Module Playbook
            ↓
Communication Profile
executive / manager / staff
    ↓
Conversation Delivery Policy
    ↓
AI Provider
    ↓
Telegram Response Renderer
```

### 5.1 User dan membership

Identitas user bersifat global, tetapi jabatan, divisi, role level, dan akses bersifat per perusahaan.

```json
{
  "user": {
    "telegram_id": 123456789,
    "name": "Tiko",
    "active": true
  },
  "memberships": [
    {
      "company_id": "lapis-malang",
      "job_title": "General Manager",
      "division": "Management",
      "role_level": "gm",
      "communication_profile": "executive",
      "allowed_modules": ["funnel-coach", "sales-performance"]
    },
    {
      "company_id": "malang-strudel",
      "job_title": "Advisor",
      "division": "Management",
      "role_level": "manager",
      "communication_profile": "manager",
      "allowed_modules": ["read-only-insight"]
    }
  ]
}
```

Satu user dapat mempunyai level berbeda pada perusahaan berbeda. Karena itu `role` tidak boleh tetap menjadi atribut tunggal pada tabel user.

### 5.2 Domain perusahaan

Setiap perusahaan mempunyai empat sumber konteks yang terpisah:

| Sumber | Fungsi | Bersifat instruksi? |
|---|---|---|
| Company Profile | Identitas, positioning, istilah, struktur, KPI | Sebagian konteks |
| Company Instruction | Aturan AI khusus perusahaan | Ya |
| Business Knowledge | Fakta, SOP, produk, data, dan dokumen | Tidak |
| Module Catalog | Daftar pekerjaan AI yang tersedia | Konfigurasi |

Company Instruction tidak boleh dicampur ke Business Knowledge. Knowledge diperlakukan sebagai bukti atau referensi, sedangkan instruction menentukan perilaku AI.

### 5.3 Modul dan playbook

Modul adalah pekerjaan AI yang dipilih dalam company context, misalnya Funnel Coach, Complaint Assistant, Sales Performance, atau Product Brainstorming.

Setiap modul mempunyai:

- tujuan;
- AI Module Playbook;
- input yang diperlukan;
- knowledge scope;
- output contract;
- akses user;
- memory atau state modul bila diperlukan.

Framework modul dapat digunakan bersama, tetapi instance perusahaan tetap mempunyai instruction, target, istilah, dan knowledge sendiri.

### 5.4 Tiga cara berkomunikasi

Communication Profile tetap bersifat global agar standar GM, manager, dan staff konsisten di seluruh grup. Company dapat memberi override terbatas untuk istilah atau tone, tetapi tidak menduplikasi seluruh profile.

| Role level membership | Profile | Orientasi jawaban |
|---|---|---|
| GM / executive | `executive` | Keputusan, prioritas, risiko, opsi, dan trade-off |
| Manager | `manager` | Rencana taktis, resource, timeline, KPI, dan koordinasi |
| Staff | `staff` | Langkah kerja, checklist, contoh, standar selesai, dan eskalasi |

Pesan pendek tidak berarti analisis dangkal. Model tetap menganalisis konteks lengkap, lalu Conversation Delivery Policy menentukan bagian yang perlu disampaikan sekarang.

### 5.5 Resolusi konteks aktif

1. Jika user hanya mempunyai satu company membership, perusahaan dipilih otomatis.
2. Jika user mempunyai lebih dari satu membership, bot meminta user memilih perusahaan atau memakai company aktif dari sesi terakhir.
3. Bot hanya menampilkan modul yang diizinkan pada membership tersebut.
4. Session menyimpan `active_company_id` dan `active_module_id`.
5. Semua instruction, knowledge, memory, dan audit berikutnya wajib memakai context aktif tersebut.

### 5.6 Komposisi konteks AI

```text
1. Global system dan security policy
2. Identity dan authorization result
3. Company Instruction
4. AI Module Playbook
5. Communication Profile membership
6. User-specific instruction yang diizinkan
7. Retrieved Business Knowledge yang sudah difilter
8. Session memory
9. Pertanyaan terbaru
10. Technical markup contract
```

Urutan prioritas bila terjadi konflik:

```text
Global security
> authorization
> company policy
> module playbook
> communication profile
> user-specific preference
> knowledge content
```

Knowledge tidak pernah mempunyai hak untuk mengubah instruksi pada lapisan di atasnya.

### 5.7 Struktur file fase transisi

Sebelum memakai database dan RAG penuh, struktur file dapat dibuat:

```text
companies/
  lapis-malang/
    profile.md
    company_instruction.md
    knowledge/
      products.md
      sop.md
      business-facts.md
    modules/
      funnel-coach/
        playbook.md
        module_knowledge.md
  malang-strudel/
    profile.md
    company_instruction.md
    knowledge/
    modules/
```

Folder adalah bentuk transisi yang mudah diaudit. Target produksi skala lanjut menggunakan metadata `company_id`, `module_id`, dan access level pada database/retrieval layer.

### 5.8 Entitas data target

| Entitas | Fungsi |
|---|---|
| `users` | Identitas Telegram global |
| `companies` | Master perusahaan |
| `user_company_memberships` | Jabatan, divisi, level, dan profile per perusahaan |
| `modules` | Modul milik atau aktif pada perusahaan |
| `module_access` | Hak membership terhadap modul |
| `company_instructions` | Instruction versioned per perusahaan |
| `module_playbooks` | Playbook versioned per modul |
| `knowledge_documents` | Dokumen dengan scope perusahaan/modul |
| `chat_sessions` | Active company, active module, dan summary |
| `messages` | Audit percakapan |

Semua query knowledge wajib memiliki filter `company_id`. Filter `module_id` ditambahkan ketika modul mempunyai knowledge khusus.

### 5.9 Target admin control plane

Admin panel akan menjadi control plane sederhana di atas service dan data model yang sama dengan bot. Target antarmuka memakai HTML server-rendered, bukan SPA terpisah. Setelah migrasi selesai, database menjadi source of truth dan file JSON hanya dipakai untuk bootstrap, import, atau recovery terkontrol.

Empat area utama:

1. Dashboard ringkas;
2. Companies: profile, instruction, knowledge, dan modules;
3. Users & Access: identity dan company membership;
4. Activity: perubahan konfigurasi, versi, dan error operasional.

Instruction dan knowledge memakai alur Draft → Preview → Publish → Rollback agar edit admin tidak langsung memengaruhi bot produksi.

Versi admin pertama sudah menyediakan:

- login admin berbasis environment credential;
- signed session cookie dengan masa aktif delapan jam;
- pembatasan lima kegagalan login per lima menit per client;
- dashboard statistik company, user, membership, dan message;
- halaman Companies read-only;
- halaman Users & Access read-only;
- placeholder navigasi Knowledge, Modules, dan Activity;
- security headers dan health endpoint.

Admin dan bot sementara berjalan dalam satu container dan memakai SQLite yang sama. Mutasi data belum dibuka agar tidak menciptakan dua source of truth selama JSON masih menjadi konfigurasi bootstrap aktif.

### 5.10 Target persistence multi-tenant

Target final menggunakan database multi-tenant dengan `company_id` sebagai tenant boundary utama. Semua entitas yang membawa data perusahaan wajib mempunyai scope company secara langsung atau melalui relasi yang tidak ambigu.

Data yang menjadi source of truth database:

- company;
- user dan company membership;
- role level dan access policy;
- instruction beserta draft, published version, dan revision history;
- module dan module playbook;
- metadata knowledge document;
- session dan message scope;
- audit event administratif.

Isi file knowledge dapat tetap berada di object/file storage, tetapi metadata, ownership, status publish, version, checksum, dan access scope disimpan di database. Bot tidak boleh mengambil instruction atau knowledge hanya berdasarkan path; query harus melewati authorization dan filter tenant.

Implementasi dilakukan bertahap:

1. SQLite dan JSON tetap menjalankan bot selama migrasi;
2. schema database multi-tenant dan repository/service layer disiapkan;
3. data JSON diimpor secara idempotent;
4. admin panel menulis draft dan published records ke database;
5. runtime bot membaca database sebagai source of truth;
6. JSON diturunkan menjadi bootstrap/recovery, lalu PostgreSQL menjadi target deployment ketika bot dan admin dipisahkan menjadi service berbeda.

## 6. Pemisahan konsep user

Contoh satu user:

```json
{
  "telegram_id": 123456789,
  "name": "Nama User",
  "role": "General Manager Amazing Malang",
  "division": "Media",
  "communication_profile": "executive",
  "custom_instruction": "Prioritaskan implikasi terhadap pertumbuhan audience dan revenue.",
  "active": true
}
```

Makna field:

| Field | Fungsi |
|---|---|
| `telegram_id` | Identitas autentikasi Telegram |
| `name` | Nama yang digunakan dalam konteks percakapan |
| `role` | Jabatan atau fungsi organisasi sebenarnya |
| `division` | Unit bisnis atau divisi |
| `communication_profile` | Level kedalaman dan bentuk jawaban |
| `custom_instruction` | Kebutuhan khusus individual |
| `active` | Status akses bot |

## 7. Communication Profile

### 7.1 Executive

Target user: owner, director, general manager, dan pimpinan setara.

Karakter jawaban:

- dimulai dari implikasi bisnis dan keputusan;
- membandingkan opsi, trade-off, risiko, dan prioritas;
- membedakan fakta, asumsi, dan hal yang belum diketahui;
- detail operasional hanya diberikan jika memengaruhi keputusan;
- rekomendasi harus mempunyai alasan dan next step.

Struktur default:

```text
Executive summary
→ fakta dan diagnosis
→ opsi dan trade-off
→ rekomendasi
→ keputusan atau next step
```

### 7.2 Manager

Target user: manager, head, supervisor senior, dan project lead.

Karakter jawaban:

- menerjemahkan strategi menjadi rencana taktis;
- menekankan target, KPI, resource, koordinasi, dan risiko eksekusi;
- menghasilkan urutan kerja, PIC, timeline, dan checkpoint bila relevan;
- tetap menjelaskan alasan bisnis, tetapi tidak berhenti pada konsep.

Struktur default:

```text
Tujuan
→ diagnosis
→ action plan
→ PIC/resource/timeline
→ KPI dan checkpoint
```

### 7.3 Staff

Target user: staff, officer, analyst, creator, crew, dan pelaksana.

Karakter jawaban:

- konkret, langsung, dan berorientasi pengerjaan;
- memecah pekerjaan menjadi langkah dan checklist;
- menyertakan contoh output atau standar selesai bila membantu;
- menghindari diskusi strategi yang tidak diperlukan untuk tugas.

Struktur default:

```text
Tujuan tugas
→ langkah pengerjaan
→ contoh/checklist
→ standar selesai
→ hal yang harus dieskalasikan
```

### 7.4 Default

Dipakai ketika profile tidak dikenali. Jawaban bersifat seimbang, praktis, dan tidak mengasumsikan senioritas user.

## 8. Resolusi profile dan prioritas instruksi

Pemilihan profile:

1. Jika user memiliki `communication_profile`, gunakan profile tersebut.
2. Jika kosong, cocokkan `role` dengan alias pada `config/role_profiles.json`.
3. Jika tidak ada kecocokan, gunakan `default`.

Urutan komposisi prompt:

```text
Global company policy
    ↓
Security policy
    ↓
User identity dan organization context
    ↓
Communication profile
    ↓
User custom instruction
    ↓
Knowledge reference
    ↓
Chat history dan pertanyaan terbaru
```

Aturan dengan prioritas lebih rendah tidak boleh menonaktifkan aturan keamanan pada lapisan di atasnya.

## 9. Telegram Response Renderer

Telegram Response Renderer adalah lapisan teknis di Python. Lapisan ini tidak menentukan apakah jawaban harus strategis, taktis, atau operasional. Tugasnya hanya memastikan draft respons tampil konsisten dan aman di Telegram.

```text
Draft AI dalam subset Markdown
    ↓
Escape semua HTML mentah
    ↓
Konversi markup yang diizinkan
    ↓
Pecah pesan dengan ruang aman terhadap batas Telegram
    ↓
Kirim dengan parse_mode=HTML
    ↓ jika Telegram menolak parsing
Fallback ke plain text
```

### 9.1 Format input yang didukung

| Input AI | Hasil Telegram HTML | Fungsi |
|---|---|---|
| `**teks**` | `<b>teks</b>` | Bold |
| `*teks*` | `<i>teks</i>` | Italic |
| `` `teks` `` | `<code>teks</code>` | Inline code |
| triple backtick | `<pre>...</pre>` | Code block |
| `- item` | `• item` | Bullet |
| `> kutipan` | `<blockquote>...</blockquote>` | Quote |
| `||teks||` | `<tg-spoiler>teks</tg-spoiler>` | Spoiler |
| `[label](https://...)` | `<a href="...">label</a>` | Link HTTPS/HTTP |

Heading Markdown diubah menjadi bold. Tabel, raw HTML, nested formatting, dan skema URL selain HTTP/HTTPS tidak menjadi bagian kontrak MVP.

### 9.2 Security boundary

- Semua `<`, `>`, dan `&` dari output model di-escape.
- Model tidak boleh mengirim HTML mentah.
- Hanya renderer yang boleh menghasilkan tag HTML Telegram.
- Link Markdown hanya dirender bila memakai `http://` atau `https://`.
- Pesan dipecah dari source dengan target 3.500 karakter agar berada di bawah batas 4.096 karakter Telegram setelah entities parsing.
- Jika Telegram menolak HTML, sistem mencatat warning tanpa isi pesan dan mengirim chunk yang sama sebagai plain text.
- Link preview dinonaktifkan untuk respons AI.

### 9.3 Batas tanggung jawab

Renderer tidak mengatur panjang ideal, jumlah pilihan, tone, satu pesan satu tujuan, atau progressive disclosure. Semua itu adalah tanggung jawab Conversation Delivery Policy yang akan dibangun terpisah.

## 10. Data dan penyimpanan

### 10.1 Konfigurasi Git

| Lokasi | Isi |
|---|---|
| `config/companies.json` | Master company dan lokasi content |
| `config/users.json` | Whitelist dan membership user per company |
| `config/role_profiles.json` | Aturan communication profile |
| `companies/<company-id>/` | Profile, instruction, dan knowledge perusahaan |
| `knowledge/company.md` | Combined legacy playbook DK Corp Group selama transisi |
| `docs/architecture.md` | Arsitektur dan keputusan konsep |

### 10.2 SQLite

SQLite menyimpan:

- salinan user hasil sinkronisasi konfigurasi;
- master company;
- user-company membership;
- perusahaan aktif per user;
- pesan user dan assistant dengan `company_id`;
- timestamp pesan dan pembaruan konfigurasi.

Railway Volume dipasang pada `/app/data` agar database bertahan saat redeploy.

## 11. Deployment

```text
Perubahan lokal
    ↓ git commit + push
GitHub private repository
    ↓ automatic deployment
Railway service
    ├── Docker container
    ├── Telegram polling worker
    ├── Admin HTTP server
    ├── environment variables
    └── volume /app/data
    ↓
Telegram long polling
```

Hanya satu replica boleh berjalan selama database memakai SQLite dan bot memakai long polling. Admin HTTP server memakai port dari variable Railway `PORT` dan menyediakan health endpoint `/health`.

Telegram update memakai controlled concurrency dengan default empat update dan batas konfigurasi maksimum enam belas. Semua update dari Telegram ID yang sama melewati lock yang sama, sehingga command `/company`, `/reset`, history, dan chat tidak berlomba mengubah context user. Update dari user berbeda dapat berjalan paralel. Startup polling memakai `drop_pending_updates=False` agar antrean update tidak sengaja dihapus saat restart.

## 12. Keamanan dan batas akses

Sudah diterapkan:

- whitelist Telegram ID;
- secret disimpan di environment variables;
- `.env` dan database runtime tidak masuk Git;
- HTTP client log tidak menampilkan URL Telegram pada level normal;
- output model di-escape dan dirender melalui safe Telegram HTML;
- knowledge diperlakukan sebagai referensi, bukan system instruction.
- membership membatasi perusahaan yang dapat dipilih user;
- history dan content AI dipisahkan berdasarkan company aktif;
- path content company harus relatif dan tidak boleh keluar dari project root.
- admin memakai username, password, dan signed session cookie;
- cookie admin bersifat `HttpOnly`, `SameSite=Lax`, dan `Secure` pada deployment;
- admin mengirim CSP, anti-frame, no-sniff, no-referrer, dan no-store headers;
- percobaan login admin dibatasi secara in-memory.

Belum diterapkan:

- knowledge access per division atau clearance;
- audit log administratif;
- enkripsi field aplikasi pada database;
- klasifikasi data sensitif pada jawaban;
- admin approval untuk perubahan akses.

Communication Profile bukan mekanisme keamanan. Profile hanya mengubah cara jawaban disampaikan, bukan menentukan informasi yang boleh diakses.

## 13. Batas MVP

- text-only;
- satu provider aktif untuk seluruh bot;
- satu instance Railway;
- seluruh knowledge company aktif dimuat sampai `KNOWLEDGE_MAX_CHARS`;
- company, membership, dan profile masih dikelola lewat JSON;
- modul, module access, dan module-scoped knowledge belum diimplementasikan;
- combined legacy Funnel Coach masih dipakai sebagai instruction DK Corp Group sampai dokumen dipisahkan;
- admin panel belum dapat melakukan mutasi data.
- concurrency masih berada dalam satu process dan belum memakai durable application queue terpisah.

## 14. Roadmap

### Fase 1 — Context-aware MVP

- whitelist dan user context;
- role-based communication profile;
- safe Telegram HTML renderer;
- Markdown knowledge;
- chat history;
- provider abstraction.

### Fase 2 — Organizational Access

- master company dan user-company membership; selesai untuk company scope;
- active company router; selesai melalui `/company`;
- company instruction terpisah; selesai secara struktur, migrasi isi legacy belum;
- active module router dan module playbook;
- knowledge metadata per division;
- authorization policy dan clearance;
- admin command atau admin panel;
- audit log perubahan user dan akses.

### Fase 3 — Scalable Knowledge

- document ingestion;
- retrieval/RAG;
- citation ke sumber internal;
- freshness dan versioning knowledge;
- evaluasi kualitas jawaban per role.

### Fase 4 — Operational Platform

- dashboard penggunaan dan biaya;
- multi-provider routing;
- approval workflow;
- integrasi data bisnis dan tool internal.

## 15. Keputusan arsitektur

| ID | Keputusan | Alasan |
|---|---|---|
| ADR-001 | Telegram long polling untuk MVP | Tidak membutuhkan domain atau webhook |
| ADR-002 | SQLite dengan satu replica | Operasional sederhana dan cukup untuk MVP |
| ADR-003 | Markdown sebagai knowledge awal | Mudah ditulis, diaudit, dan di-versioning |
| ADR-004 | Provider menggunakan kontrak compatible API | OpenAI dan DeepSeek dapat diganti melalui konfigurasi |
| ADR-005 | Jabatan dipisahkan dari communication profile | Struktur organisasi tidak selalu sama dengan level komunikasi |
| ADR-006 | Dokumen arsitektur Markdown sebagai source of truth | Mudah diperbarui bersama source code sebelum dibuat PDF |
| ADR-007 | Conversation Delivery Policy dipisahkan dari Telegram Renderer | Cara menyampaikan pesan tidak dicampur dengan format teknis channel |
| ADR-008 | Model menghasilkan subset Markdown, Python menghasilkan safe HTML | Mencegah raw HTML model sekaligus menjaga tampilan Telegram konsisten |
| ADR-009 | Satu bot dan satu codebase untuk seluruh perusahaan | Menghindari fragmentasi bot, akses, knowledge, dan maintenance |
| ADR-010 | Jabatan dan profile disimpan pada user-company membership | Satu orang dapat mempunyai peran berbeda pada perusahaan berbeda |
| ADR-011 | Company Instruction dipisahkan dari Business Knowledge | Instruksi AI tidak boleh bercampur dengan fakta dan dokumen referensi |
| ADR-012 | Semua retrieval wajib difilter dengan company_id | Mencegah kebocoran konteks antarperusahaan |
| ADR-013 | Chat history memakai scope user dan company | Pergantian perusahaan tidak boleh membawa konteks percakapan perusahaan lain |
| ADR-014 | Admin panel dibangun setelah domain model stabil | UI harus mengelola company dan membership model yang benar, bukan struktur user global lama |
| ADR-015 | Database multi-tenant menjadi source of truth final | Company, membership, access, instruction, knowledge metadata, version, dan audit harus dikelola konsisten melalui admin control plane |
| ADR-016 | JSON hanya menjadi bootstrap/import setelah migrasi | Runtime tidak boleh memiliki dua source of truth yang dapat saling bertentangan |
| ADR-017 | PostgreSQL menjadi target saat bot dan admin dipisahkan | SQLite cukup untuk transisi satu process, tetapi bukan storage bersama untuk beberapa service Railway |
| ADR-018 | Admin versi pertama server-rendered dan read-only | Memvalidasi control plane, autentikasi, serta tampilan data sebelum membuka mutasi production |
| ADR-019 | Bot dan admin sementara berjalan dalam satu container | SQLite dan Railway Volume tetap mempunyai satu writer boundary selama fase transisi |
| ADR-020 | Pending Telegram update tidak dibuang saat startup | Pesan yang masuk saat restart atau redeploy tidak boleh sengaja dihapus oleh aplikasi |
| ADR-021 | Controlled concurrency global dan serialization per user | User berbeda dapat dilayani paralel tanpa merusak urutan context, command, dan history user yang sama |

## 16. Changelog dokumen

### 0.8 — 31 Agustus 2026

- menghentikan pembuangan pending Telegram update saat startup;
- menambahkan controlled concurrency dengan default empat update;
- menambahkan per-user serialization untuk chat dan seluruh command;
- menambahkan `MAX_CONCURRENT_UPDATES` dengan batas satu sampai enam belas;
- mencatat durable queue sebagai batas yang belum diimplementasikan.

### 0.7 — 31 Agustus 2026

- menambahkan admin HTTP server dalam process aplikasi yang sama;
- menambahkan login, signed cookie, login rate limit, dan security headers;
- menambahkan dashboard, Companies, dan Users & Access read-only;
- menambahkan navigasi dasar Knowledge, Modules, dan Activity;
- menambahkan endpoint `/health` dan konfigurasi public port Railway;
- mempertahankan JSON sebagai source of truth selama admin masih read-only.

### 0.6 — 31 Agustus 2026

- menetapkan database multi-tenant sebagai arsitektur persistence final;
- menurunkan JSON menjadi bootstrap, import, atau recovery setelah migrasi;
- menetapkan `company_id` sebagai tenant boundary wajib;
- menambahkan versioning instruction, metadata knowledge, access policy, dan audit sebagai data database;
- menetapkan PostgreSQL sebagai target ketika bot dan admin berjalan sebagai service berbeda.

### 0.5 — 31 Agustus 2026

- mengimplementasikan master company dan user-company membership;
- menambahkan active company session dan command `/company`;
- memisahkan history berdasarkan user dan company;
- menambahkan company-scoped profile, instruction, dan knowledge loader;
- memigrasikan history lama ke company default ketika resolusinya tidak ambigu;
- menetapkan target admin control plane sederhana;
- mencatat combined legacy Funnel Coach sebagai pekerjaan migrasi, bukan arsitektur final.

### 0.4 — 31 Agustus 2026

- menetapkan target arsitektur satu bot multi-company;
- mengganti role global menjadi user-company membership;
- memisahkan Company Instruction, Business Knowledge, dan AI Module Playbook;
- menetapkan tiga Communication Profile pada level membership;
- menambahkan active company/module context, struktur file transisi, entitas data target, dan filter knowledge wajib.

### 0.3 — 31 Agustus 2026

- memisahkan Conversation Delivery Policy dari Telegram Response Renderer;
- mengimplementasikan subset Markdown ke safe Telegram HTML;
- menambahkan escape karakter, message split, link preview off, dan fallback plain text;
- mencatat kontrak format dan security boundary renderer.

### 0.2 — 31 Agustus 2026

- menambahkan Communication Profile executive, manager, staff, dan default;
- memisahkan role dari communication profile;
- mencatat perbedaan Communication Profile dan Authorization Policy;
- menyelaraskan status implementasi, deployment, keamanan, dan roadmap.

### 0.1 — 29 Agustus 2026

- MVP awal Telegram, whitelist, user context, Markdown knowledge, SQLite, provider abstraction, dan Railway.
