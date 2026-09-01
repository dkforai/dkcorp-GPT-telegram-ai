# Arsitektur dan Konsep DK Corp Telegram AI

## Kontrol dokumen

| Atribut | Nilai |
|---|---|
| Status | Living document |
| Versi | 1.7 |
| Terakhir diperbarui | 2 September 2026 |
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
5. SQLite menjadi source of truth runtime pada fase satu-container. JSON hanya dipakai untuk bootstrap ketika registry database masih kosong.
6. MVP memakai komponen minimum yang cukup untuk satu instance.
7. Mutasi administratif harus tervalidasi, dilindungi CSRF, dan dicatat sebagai audit event.
8. Draft instruction tidak boleh memengaruhi bot. Runtime hanya membaca versi database yang sudah dipublikasikan atau file transisi bila belum ada versi database.
9. Draft knowledge tidak boleh memengaruhi bot. Runtime membaca versi published dari dokumen aktif; file transisi hanya dipakai sampai publish knowledge pertama pada company tersebut.
10. Akses module bersifat default-deny per membership. Runtime hanya menerima module aktif yang statusnya aktif, playbook-nya sudah dipublikasikan, dan aksesnya diberikan admin.
11. Draft module playbook tidak boleh memengaruhi bot. History General dan setiap module dipisahkan agar perpindahan pekerjaan tidak mencampur konteks.

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
published knowledge database atau file Markdown transisi
    ↓
Prompt Composer
global policy + user context + company instruction
+ optional module playbook + communication profile
+ custom instruction + company knowledge
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
| Knowledge Loader | Sudah | Published document aktif dari SQLite; folder Markdown menjadi fallback sampai publish pertama |
| Document Ingestion | Sebagian | Upload PDF, DOCX, TXT, dan Markdown menjadi draft teks; OCR dan `.doc` belum |
| Chat History | Sudah | SQLite dipisahkan per user, perusahaan, dan module; General memakai scope kosong tersendiri |
| Provider Abstraction | Sudah | OpenAI dan DeepSeek compatible API |
| Telegram Response Renderer | Sudah | Safe HTML, split, link preview off, dan fallback plain text |
| Conversation Delivery Policy | Belum | Akan mengatur panjang, ritme, dan progressive disclosure |
| Multi-company membership | Sudah | Tabel membership SQLite; JSON hanya bootstrap awal |
| Company router dan active context | Sudah | Command `/company` dan session active company |
| Module router dan active context | Sudah | Command `/module`, General context, default-deny module access, dan reset module saat company berubah |
| Company-scoped instruction | Sudah | Draft dan versi publish tersimpan di SQLite; file company menjadi fallback transisi |
| AI Module Playbook | Sudah | Registry per company, draft, preview, immutable publish, restore-to-draft, status, dan runtime prompt |
| Authorization per knowledge | Sebagian | Sudah company-scoped; knowledge khusus module, division, dan clearance belum |
| Response Validator | Sebagian | Batas panjang, split, escape HTML, dan fallback; belum ada policy classifier |
| Admin Panel | Sebagian | Company, user, membership, Instruction, Knowledge, Module, dan module access writable; Activity read-only |
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
| `company_instruction_state` | Draft aktif dan pointer versi live per perusahaan |
| `company_instruction_versions` | Versi publish immutable per perusahaan |
| `module_playbook_state` | Draft aktif dan pointer versi live per module |
| `module_playbook_versions` | Versi playbook immutable per module |
| `knowledge_documents` | Metadata, status, draft, dan pointer versi live per company |
| `knowledge_document_versions` | Versi published immutable, title, content, checksum, actor, dan waktu publish |
| `user_sessions` | Active company dan active module per user |
| `messages` | History percakapan dengan scope user, company, dan module |

Semua query knowledge wajib memiliki filter `company_id`. Filter `module_id` ditambahkan ketika modul mempunyai knowledge khusus.

### 5.9 Target admin control plane

Admin panel akan menjadi control plane sederhana di atas service dan data model yang sama dengan bot. Target antarmuka memakai HTML server-rendered, bukan SPA terpisah. Setelah migrasi selesai, database menjadi source of truth dan file JSON hanya dipakai untuk bootstrap, import, atau recovery terkontrol.

Empat area utama:

1. Dashboard ringkas;
2. Companies: profile, instruction, knowledge, dan modules;
3. Users & Access: identity dan company membership;
4. Activity: perubahan konfigurasi, versi, dan error operasional.

Instruction, Knowledge, dan Module Playbook memakai alur Draft → Preview → Publish → Restore to Draft. Edit draft tidak langsung memengaruhi bot produksi.

Versi admin saat ini menyediakan:

- login admin berbasis environment credential;
- signed session cookie dengan masa aktif delapan jam;
- pembatasan lima kegagalan login per lima menit per client;
- dashboard statistik company, user, membership, dan message;
- halaman Companies untuk tambah, ubah nama, aktivasi, dan nonaktivasi;
- halaman Users & Access untuk tambah/ubah user, status whitelist, serta membership;
- halaman Company Instructions untuk menyimpan draft, preview, publish, dan riwayat versi;
- halaman Knowledge untuk membuat dokumen per company, menyimpan draft, preview, publish, aktivasi/nonaktivasi, dan riwayat versi;
- form Knowledge menerima teks langsung atau upload PDF, DOCX, TXT, dan Markdown maksimal 10 MB;
- restore versi lama ke draft agar selalu melewati preview sebelum dipublikasikan kembali;
- halaman Modules untuk membuat registry per company, mengubah identitas, status, draft, preview, publish, dan riwayat versi playbook;
- akses module default-deny dikelola pada form edit membership;
- halaman Activity read-only untuk 100 audit event terbaru dengan filter kategori termasuk Modules;
- security headers dan health endpoint.
- CSRF token untuk seluruh mutasi Company;
- audit event untuk create, update, activate, dan deactivate Company.
- tambah user selalu membuat membership pertama sebagai company default;
- membership tambahan dapat mengatur jabatan, divisi, role level, communication profile, custom instruction, status, dan default.
- bot hanya membaca `published_version_id`; draft tersimpan terpisah dan tidak masuk ke system prompt.
- file instruction tetap dibaca sebagai fallback selama suatu company belum mempunyai versi database yang dipublikasikan.
- bot hanya membaca knowledge published dari dokumen aktif dan selalu memfilternya dengan `company_id`;
- file knowledge tetap menjadi fallback sampai company mempunyai publish knowledge pertama. Setelah itu database tetap menjadi sumber aktif meskipun semua dokumen dinonaktifkan.
- bot hanya menawarkan module yang aktif, memiliki playbook published, dan telah diberikan pada membership aktif;
- command `/module` memilih module aktif atau kembali ke `General` tanpa module khusus;
- perubahan company, pencabutan akses, atau nonaktivasi module membersihkan pointer active module;
- history disimpan dengan scope `telegram_id + company_id + module_id` sehingga perpindahan module tidak mencampur konteks.

Admin dan bot sementara berjalan dalam satu container dan memakai SQLite yang sama. SQLite menjadi source of truth runtime. `config/companies.json` dan `config/users.json` hanya diimpor ketika tabel terkait masih kosong, sehingga restart atau redeploy tidak menimpa perubahan admin.

### 5.9.1 Lifecycle Company Instruction

```text
Editor admin
    ↓ Simpan draft
company_instruction_state.draft_content
    ↓ Preview
Tampilan read-only, belum dibaca bot
    ↓ Publish dalam satu transaksi
company_instruction_versions versi baru
    + update published_version_id
    ↓
Bot memakai versi live pada pesan berikutnya
```

Setiap publish selalu menambah `version_number`; versi lama tidak diubah atau dihapus. Aksi restore hanya menyalin isi versi yang dipilih ke draft. Admin wajib melakukan preview dan publish untuk menjadikannya live. Jika `published_version_id` belum tersedia, runtime memakai `companies.instruction_file` sebagai fallback transisi. Nilai draft tidak pernah menjadi fallback runtime.

### 5.9.2 Lifecycle Knowledge Document

```text
Editor admin per company
    ↓ Buat atau simpan draft
knowledge_documents.title + draft_content
    ↓ Preview
Tampilan read-only, belum dibaca bot
    ↓ Publish dalam satu transaksi
knowledge_document_versions versi immutable baru
    + update knowledge_documents.published_version_id
    ↓
Bot menggabungkan versi live dari dokumen aktif company tersebut
```

`document_key` immutable dan unik di dalam satu company, tetapi key yang sama boleh dipakai company lain. Setiap versi menyimpan SHA-256 content agar integritas isi dapat diperiksa tanpa memasukkan isi penuh ke audit event. Restore menyalin judul dan isi versi lama ke draft.

Upload tidak disimpan sebagai file runtime. Server memvalidasi extension dan signature dasar, membatasi ukuran, mengekstrak teks, lalu menyimpan teks tersebut sebagai draft. Metadata source berupa filename aman, media type, ukuran, dan SHA-256 file disimpan pada `knowledge_documents` dan audit event. Admin tetap wajib memeriksa hasil ekstraksi sebelum publish.

Format yang didukung adalah PDF dengan text layer, Word Open XML `.docx`, UTF-8 `.txt`, dan `.md`. PDF scan tanpa text layer membutuhkan OCR yang belum diimplementasikan. Format Word binary lama `.doc` ditolak dan harus disimpan ulang sebagai `.docx`.

Sebelum publish pertama pada suatu company, runtime memakai folder Markdown transisi. Publish pertama memindahkan company ke mode database. Setelah mode database aktif, hasil knowledge dapat kosong bila seluruh dokumen dinonaktifkan; runtime tidak boleh membangkitkan kembali file transisi secara implisit.

### 5.9.3 Lifecycle AI Module Playbook

```text
Admin membuat module dalam satu company
    ↓ Module ID dibuat server dan dikunci
Admin menyimpan draft playbook
    ↓ Preview read-only, belum dapat dipilih bot
Admin publish
    ↓ module_playbook_versions versi immutable baru
Admin memberi akses pada membership
    ↓ module_access default-deny
User memilih /module <module-id>
    ↓ user_sessions.active_module_id
Bot memakai playbook live dan history khusus module
```

Module hanya dapat dipilih ketika company dan membership aktif, module aktif, playbook published tersedia, dan row `module_access` aktif. Module ID unik di dalam company dan dibuat otomatis dari nama dengan suffix numerik bila terjadi collision. Restore menyalin versi lama ke draft; runtime tetap memakai versi live sampai admin memublikasikan draft tersebut.

Mode `/module general` mengosongkan `active_module_id` dan memakai history General. Perubahan `/company`, pencabutan module access, atau nonaktivasi module juga mengosongkan pointer module. Data history lama tidak dihapus dan tetap dapat digunakan kembali jika akses module diberikan lagi.

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

1. schema database multi-tenant dan repository/service layer disiapkan — selesai untuk company, user, membership, session, message, dan audit event;
2. data JSON diimpor hanya saat registry database kosong — selesai;
3. runtime bot membaca SQLite sebagai source of truth — selesai;
4. admin panel menulis entitas secara bertahap ke database — Company, user, membership, Company Instruction, Knowledge, Module, dan module access selesai;
5. instruction, knowledge, dan module playbook memakai draft, preview, immutable published version, dan restore-to-draft — selesai;
6. PostgreSQL menjadi target ketika bot dan admin dipisahkan menjadi service berbeda — belum.

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
| `config/companies.json` | Bootstrap company untuk database kosong |
| `config/users.json` | Bootstrap whitelist dan membership untuk database kosong |
| `config/role_profiles.json` | Aturan communication profile |
| `companies/<company-id>/` | Profile, instruction, dan knowledge perusahaan |
| `knowledge/company.md` | Combined legacy playbook DK Corp Group selama transisi |
| `docs/architecture.md` | Arsitektur dan keputusan konsep |

### 10.2 SQLite

SQLite menjadi source of truth runtime dan menyimpan:

- user dan status whitelist;
- master company;
- user-company membership;
- perusahaan aktif per user;
- pesan user dan assistant dengan `company_id`;
- timestamp pesan dan pembaruan konfigurasi;
- audit event perubahan administratif.
- draft Company Instruction dan pointer versi yang sedang live;
- versi Company Instruction yang sudah dipublikasikan dan bersifat immutable.
- metadata, status, draft, dan pointer versi live Knowledge Document per company;
- versi Knowledge Document immutable beserta SHA-256 content.
- metadata source upload Knowledge tanpa menyimpan file mentah.

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
- seluruh mutasi Company memakai CSRF token yang terikat pada session;
- Company ID divalidasi dan tidak dapat diubah setelah dibuat;
- Company dengan membership aktif tidak dapat dinonaktifkan;
- perubahan Company dicatat pada `admin_audit_events`.
- Telegram ID divalidasi dan tidak dapat diubah setelah user dibuat;
- user whitelist dapat dinonaktifkan tanpa menghapus membership atau history;
- user hanya memiliki satu membership default aktif;
- membership default tidak dapat dinonaktifkan selama masih ada membership aktif lain sebelum default dipindahkan;
- mutasi user dan membership memakai CSRF serta dicatat pada audit event.
- draft Company Instruction tidak pernah dibaca runtime;
- publish instruction membuat versi immutable baru dan mengubah pointer live secara atomik;
- restore versi lama hanya menyalin isinya ke draft, sehingga tetap harus dipreview dan dipublikasikan kembali;
- mutasi instruction memakai CSRF dan audit event tanpa menyimpan isi instruction ke audit details.
- semua query knowledge database memakai filter `company_id` langsung atau relasi dokumen yang tervalidasi;
- draft knowledge tidak pernah dibaca runtime;
- publish knowledge membuat versi immutable baru dan mengubah pointer live secara atomik;
- deactivate knowledge menghentikan pemakaian dokumen tanpa menghapus draft atau riwayat versi;
- restore knowledge lintas company ditolak oleh filter tenant pada query;
- audit knowledge menyimpan metadata, jumlah karakter, dan checksum, bukan isi dokumen penuh.
- upload knowledge dibatasi 10 MB, filename dibersihkan, extension dan struktur file divalidasi, serta hasil ekstraksi dibatasi 100.000 karakter;
- file upload tidak menjadi instruction dan tidak langsung live; hanya teks draft yang telah dipublish yang dibaca runtime.
- module access bersifat default-deny dan divalidasi terhadap membership serta company yang sama;
- module draft tidak masuk ke prompt; runtime hanya membaca published playbook dari module aktif yang diizinkan;
- query dan restore playbook selalu divalidasi dengan pasangan `company_id + module_id`;
- pergantian company, pencabutan akses, dan nonaktivasi module menghapus pointer module aktif;
- history General dan module dipisahkan dengan `module_id`, tanpa menghapus history scope lain;
- audit module menyimpan metadata, versi, jumlah karakter, dan checksum, bukan isi playbook.

Belum diterapkan:

- knowledge access per division atau clearance;
- enkripsi field aplikasi pada database;
- klasifikasi data sensitif pada jawaban;
- admin approval untuk perubahan akses.

Communication Profile bukan mekanisme keamanan. Profile hanya mengubah cara jawaban disampaikan, bukan menentukan informasi yang boleh diakses.

## 13. Batas MVP

- text-only;
- satu provider aktif untuk seluruh bot;
- satu instance Railway;
- seluruh knowledge company aktif dimuat sampai `KNOWLEDGE_MAX_CHARS`;
- upload knowledge mendukung PDF text layer, DOCX, TXT, dan Markdown; OCR serta `.doc` lama belum;
- Company, user, membership, communication profile, Company Instruction, Knowledge, Module, dan module access dikelola melalui admin;
- module-scoped knowledge belum diimplementasikan; module masih memakai Knowledge company ditambah playbook;
- combined legacy Funnel Coach masih dipakai sebagai instruction DK Corp Group sampai dokumen dipisahkan;
- Company Instruction, Knowledge, dan Module Playbook sudah writable dan versioned; Activity menampilkan audit administratif.
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
- company instruction terpisah dan versioned; selesai, migrasi isi legacy per company dilakukan melalui editor admin;
- company knowledge terpisah dan versioned; selesai untuk company scope, dengan fallback file selama transisi;
- active module router, default-deny access, dan versioned module playbook; selesai melalui `/module` dan admin;
- knowledge metadata per division;
- authorization policy dan clearance;
- admin command atau admin panel;
- audit log perubahan user dan akses.

### Fase 3 — Scalable Knowledge

- document ingestion file/upload di luar editor teks;
- retrieval/RAG;
- citation ke sumber internal;
- freshness policy knowledge; versioning dasar sudah selesai;
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
| ADR-022 | Asset admin memakai path internal tetap | CSS dan favicon harus tetap same-origin di balik reverse proxy HTTPS Railway tanpa bergantung pada rekonstruksi scheme dari request |
| ADR-023 | SQLite menjadi source of truth pada fase satu-container | Admin dan bot memakai database serta volume yang sama; JSON tidak boleh menimpa mutasi admin saat restart |
| ADR-024 | JSON hanya diimpor ketika registry terkait kosong | Menjaga bootstrap deployment baru tanpa menciptakan dua source of truth aktif |
| ADR-025 | Company ID immutable dan deactivation dijaga | Tenant boundary tidak boleh berubah dan company dengan membership aktif tidak boleh terputus tanpa pemindahan akses |
| ADR-026 | Mutasi admin memakai CSRF dan audit event | Perubahan state melalui web harus terlindungi serta dapat ditelusuri |
| ADR-027 | Telegram ID immutable | Telegram ID adalah identity key whitelist dan relasi history, sehingga koreksi dilakukan dengan membuat user yang benar, bukan mengganti primary key |
| ADR-028 | Membership pertama otomatis default | User baru harus langsung memiliki active company context yang tidak ambigu |
| ADR-029 | Perpindahan default mendahului deactivation | Membership default tidak boleh dinonaktifkan ketika akses aktif lain masih ada karena fallback company akan menjadi ambigu |
| ADR-030 | Draft Company Instruction dipisahkan dari versi live | Edit admin tidak boleh langsung mengubah perilaku bot produksi |
| ADR-031 | Publish membuat versi immutable baru | Riwayat perubahan tetap dapat diaudit dan versi lama tidak ditimpa |
| ADR-032 | Restore menyalin versi lama ke draft | Rollback tetap melewati preview dan publish, bukan mengganti runtime secara diam-diam |
| ADR-033 | Database instruction mengalahkan file transisi hanya setelah publish | Company lama tetap berjalan sebelum migrasi, sedangkan draft baru aman dari runtime |
| ADR-034 | Draft Knowledge dipisahkan dari versi live | Edit fakta bisnis tidak boleh mengubah konteks bot sebelum preview dan publish |
| ADR-035 | Publish Knowledge pertama mengaktifkan mode database per company | Migrasi dapat dilakukan bertahap tanpa mematikan file transisi sebelum data baru siap |
| ADR-036 | Document key unik dalam scope company | Identitas dokumen stabil sekaligus mengizinkan taxonomy key yang sama pada tenant berbeda |
| ADR-037 | Mode database tidak kembali ke fallback ketika seluruh dokumen inactive | Deactivation yang disengaja tidak boleh diam-diam menghidupkan data file lama |
| ADR-038 | Versi Knowledge immutable dan menyimpan SHA-256 | Riwayat dapat diaudit serta integritas isi dapat diperiksa tanpa menaruh dokumen penuh di audit log |
| ADR-039 | Upload diekstrak menjadi draft, bukan disimpan sebagai konteks file mentah | Admin dapat memeriksa hasil parsing dan alur publish tetap menjadi satu-satunya jalan menuju runtime |
| ADR-040 | MVP upload mendukung PDF text layer dan DOCX, bukan `.doc` atau OCR | Parsing tetap portable pada container Railway tanpa binary office/OCR tambahan |
| ADR-041 | Source upload menyimpan metadata dan SHA-256, bukan file asli | Provenance dan audit tersedia tanpa memperbesar SQLite dengan binary document |
| ADR-042 | Company ID dan document key dibuat server dari nama atau judul | Admin tidak perlu memahami slug teknis dan request yang dimanipulasi tidak dapat menentukan tenant key |
| ADR-043 | Collision identifier memakai suffix numerik deterministik | Nama yang sama tetap dapat dibuat tanpa meminta admin menyusun key manual |
| ADR-044 | Activity hanya membaca audit event dan tidak mempunyai mutasi | Riwayat administratif tidak boleh menjadi jalur untuk mengubah atau menghapus state produksi |
| ADR-045 | Activity tidak menampilkan isi instruction atau knowledge | Audit cukup menyimpan metadata, ukuran, versi, dan checksum tanpa membuka konten sensitif |
| ADR-046 | Module merupakan entitas company-scoped dengan ID immutable | Nama dapat berubah tanpa memutus access, version history, session, atau history percakapan |
| ADR-047 | Module access memakai explicit default-deny per membership | Membership company tidak otomatis membuka seluruh workflow dan playbook internal company tersebut |
| ADR-048 | Module Playbook memakai draft dan immutable published version | Perubahan admin tidak boleh langsung mengubah perilaku bot dan versi lama tetap dapat diaudit |
| ADR-049 | Module runtime membutuhkan status aktif, akses aktif, dan playbook published | Draft atau module tanpa otorisasi tidak boleh muncul pada daftar Telegram maupun masuk ke prompt |
| ADR-050 | History memakai scope user, company, dan module | Perpindahan antara General dan workflow module tidak boleh mencampur konteks percakapan |
| ADR-051 | Perubahan company atau hilangnya module access mereset active module | Session tidak boleh mempertahankan pointer menuju konteks yang tidak lagi valid atau diizinkan |

## 16. Changelog dokumen

### 1.7 — 2 September 2026

- membuka Module Management per company dengan Module ID otomatis dan immutable;
- menambahkan draft, preview, immutable publish, version history, checksum, dan restore-to-draft untuk AI Module Playbook;
- menolak publish playbook kosong agar module live selalu mempunyai instruksi kerja;
- menambahkan module access default-deny pada setiap membership;
- menambahkan command `/module`, mode `General`, dan tampilan active module pada `/whoami`;
- mengkomposisikan published module playbook ke prompt hanya setelah status dan akses tervalidasi;
- memisahkan history berdasarkan user, company, dan module serta mereset pointer module ketika company atau authorization berubah;
- menambahkan audit module tanpa menyimpan isi playbook dan pengujian tenant/access/runtime/history boundary;
- memperbarui status implementasi, persistence, security boundary, batas MVP, roadmap, dan keputusan arsitektur.

### 1.6 — 1 September 2026

- membuka halaman Activity sebagai audit log administratif read-only;
- menampilkan 100 event terbaru dalam waktu WIB dengan filter Company, User, Membership, Instruction, dan Knowledge;
- menerjemahkan action dan metadata audit menjadi label yang dapat dibaca admin;
- memendekkan checksum dan tidak menampilkan isi instruction, custom instruction, atau knowledge;
- menambahkan pengujian login, filter kategori, dan pencegahan kebocoran isi knowledge melalui Activity;
- memperbarui status admin, security boundary, batas MVP, dan keputusan arsitektur.

### 1.5 — 1 September 2026

- menghapus input Company ID dan document key dari form create;
- membuat identifier di sisi server dari nama company atau judul knowledge;
- menambahkan suffix `-2`, `-3`, dan seterusnya ketika identifier sudah digunakan;
- mempertahankan Telegram ID sebagai input eksternal dari Telegram dan seluruh secret di environment variables;
- menambahkan pengujian bahwa nilai identifier hasil manipulasi form diabaikan server.

### 1.4 — 1 September 2026

- menambahkan upload PDF, DOCX, TXT, dan Markdown pada form Knowledge;
- mengekstrak file menjadi teks draft yang tetap melewati preview dan publish;
- menambahkan batas file 10 MB, batas 500 halaman PDF, batas uncompressed DOCX, validasi struktur, dan batas hasil 100.000 karakter;
- menolak Word `.doc`, file dengan extension palsu, PDF encrypted, dan PDF scan tanpa text layer dengan pesan yang dapat ditindaklanjuti;
- menyimpan filename, media type, ukuran, serta SHA-256 source tanpa menyimpan binary file;
- menambahkan dependency `pypdf` dan `python-docx` serta pengujian parser dan workflow upload admin;
- memperbarui status implementasi, lifecycle knowledge, security boundary, batas MVP, dan keputusan arsitektur.

### 1.3 — 1 September 2026

- membuka Knowledge Management per company pada admin;
- menambahkan dokumen draft, preview, publish, status aktif/nonaktif, immutable version history, dan restore-to-draft;
- menjadikan published knowledge database sebagai konteks runtime setelah publish pertama, dengan file Markdown sebagai fallback transisi sebelumnya;
- memastikan draft, dokumen inactive, dan data company lain tidak masuk ke prompt;
- menambahkan checksum SHA-256 per versi serta audit event tanpa isi dokumen penuh;
- menambahkan pengujian fallback, tenant isolation, versioning, runtime switching, CSRF, dan workflow admin;
- memperbarui status implementasi, persistence, security boundary, batas MVP, roadmap, dan keputusan arsitektur.

### 1.2 — 1 September 2026

- membuka Company Instruction Management pada admin;
- menambahkan tabel draft state dan immutable published versions per company;
- menambahkan alur save draft, preview, publish, riwayat versi, serta restore-to-draft;
- memastikan draft tidak pernah masuk ke system prompt;
- menjadikan versi database yang dipublikasikan sebagai sumber runtime dengan file instruction sebagai fallback transisi;
- menambahkan CSRF, audit event tanpa isi sensitif, validasi 50.000 karakter, dan pengujian workflow;
- memperbarui status implementasi, persistence, security boundary, batas MVP, roadmap, dan keputusan arsitektur.

### 1.1 — 1 September 2026

- membuka Users & Access Management untuk create, rename, activate, dan deactivate whitelist;
- membuka membership management per company;
- menambahkan field jabatan, divisi, role level, communication profile, custom instruction, default, dan status pada formulir membership;
- mengunci Telegram ID setelah user dibuat;
- menetapkan membership pertama sebagai company default;
- menjaga default membership sebelum deactivation;
- menambahkan CSRF dan audit event untuk mutasi user serta membership;
- memperbarui status admin, persistence, security boundary, batas MVP, dan keputusan arsitektur.

### 1.0 — 1 September 2026

- menetapkan SQLite sebagai source of truth runtime pada fase satu-container;
- mengubah JSON company dan user menjadi bootstrap ketika registry database kosong;
- membuka Company Management untuk create, rename, activate, dan deactivate;
- mengunci Company ID setelah dibuat dan mencegah deactivation ketika masih memiliki membership aktif;
- menambahkan perlindungan CSRF pada mutasi Company;
- menambahkan tabel audit event administratif;
- memperbarui status implementasi, batas MVP, persistence, dan security boundary.

### 0.9 — 1 September 2026

- memperbaiki pemuatan CSS dan favicon admin pada public domain HTTPS Railway;
- menetapkan `/admin/static/*` sebagai path asset same-origin yang tidak bergantung pada scheme hasil rekonstruksi reverse proxy;
- menambahkan pengujian respons stylesheet admin.

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
