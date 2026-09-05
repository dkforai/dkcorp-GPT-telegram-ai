# Arsitektur dan Konsep DK Corp Telegram AI

## Kontrol dokumen

| Atribut | Nilai |
|---|---|
| Status | Living document |
| Versi | 1.24 |
| Terakhir diperbarui | 5 September 2026 |
| Source of truth | Repository `dkcorp-GPT-telegram-ai` |
| Format akhir | Markdown selama pengembangan, PDF setelah konsep stabil |

Dokumen ini wajib diperbarui bersama perubahan fitur, aturan, data model, alur, keamanan, atau deployment. PDF bukan source of truth selama aplikasi masih aktif dikembangkan.

## 1. Tujuan aplikasi

DK Corp Telegram AI adalah asisten internal yang menyediakan akses AI melalui satu bot Telegram. Sistem mengenali identitas dan konteks organisasi setiap user, menyusun jawaban sesuai level komunikasi, menggunakan knowledge perusahaan, serta menyimpan konteks percakapan sederhana.

Tujuan utama:

- user tidak perlu menulis system prompt atau mengunggah knowledge sendiri;
- jawaban menyesuaikan konteks jabatan dan gaya komunikasi otomatis dari Role level membership;
- jawaban mengikuti lima tingkat: Owner/Board, Executive/GM, Manager/Head, Supervisor/Coordinator, dan Staff/Operational;
- provider AI dapat diganti tanpa mengubah alur Telegram;
- MVP mudah dijalankan, diaudit, dan dikembangkan.

## 2. Prinsip desain

1. Identitas, hak akses, gaya komunikasi, dan knowledge adalah empat konsep berbeda.
2. Jabatan tidak menentukan gaya secara hardcoded. Profile efektif diturunkan dari Role level membership aktif; teks jabatan dan override profile lama tidak mengalahkan pemetaan tersebut.
3. Informasi yang tidak tersedia tidak boleh dikarang.
4. Knowledge merupakan referensi data, bukan instruksi yang boleh mengambil alih system prompt.
5. SQLite menjadi source of truth runtime pada fase satu-container. JSON hanya dipakai untuk bootstrap ketika registry database masih kosong.
6. MVP memakai komponen minimum yang cukup untuk satu instance.
7. Mutasi administratif harus tervalidasi, dilindungi CSRF, dan dicatat sebagai audit event.
8. Draft instruction tidak boleh memengaruhi bot. Runtime hanya membaca versi database yang sudah dipublikasikan atau file transisi bila belum ada versi database.
9. Draft knowledge tidak boleh memengaruhi bot. Runtime membaca versi published dari dokumen aktif; file transisi hanya dipakai sampai publish knowledge pertama pada company tersebut.
10. Akses module perusahaan bersifat default-deny per membership. Modul bersama/Learning memakai whitelist aktif lintas perusahaan; mode konteks perusahaan tetap memerlukan membership aktif. Semua runtime memerlukan module aktif, published, dan pilihan AI valid.
11. Draft module playbook tidak boleh memengaruhi bot. History General dan setiap module dipisahkan agar perpindahan pekerjaan tidak mencampur konteks.
12. API key Module tidak boleh disimpan sebagai plaintext di SQLite, audit, log, atau HTML respons. Key baru di Settings AI memakai authenticated encryption dengan master key terpisah di environment; named environment credential lama tetap didukung. Module memilih utama/cadangan dari registry. Cadangan hanya untuk kegagalan operasional yang diizinkan, bukan melewati akses atau salah konfigurasi.

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
    ├── owner      → governance/ownership
    ├── executive  → strategic
    ├── manager    → tactical-managerial
    ├── supervisor → coordination/control
    ├── staff      → operational-execution
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
    ├── General → global environment credential
    └── Module → AI utama → encrypted registry / legacy environment secret
                  └── kegagalan operasional tertentu → AI cadangan yang dipilih
    ↓
Conversation Delivery Policy
pendek, bertahap, dan satu tujuan (belum diimplementasikan penuh)
    ↓
Telegram Response Renderer
pisahkan naskah siap salin (polos) dari penjelasan (safe HTML) + split
    ↓
Jawaban ke user
```

## 4. Status implementasi

| Lapisan | Status | Implementasi saat ini |
|---|---|---|
| Telegram Bot | Sudah | Long polling; respons HTML biasa dan blok naskah siap salin polos pada implementasi v1.16 |
| Update Reliability | Sudah | Pending update dipertahankan, concurrency terbatas, dan serialization per user |
| Authentication | Sudah | Whitelist Telegram ID |
| User Context | Sudah | Identity global dan membership per perusahaan |
| Import user Excel | Sudah | `.xls`/`.xlsx`, lima kolom, insert-only untuk ID baru, validasi atomik dan skip total ID lama melalui `/admin/users/import` |
| Communication Profile | v1.23 deployed, 369 tes lokal lulus | Lima level komunikasi otomatis dari Role level; isi gaya dapat diubah super admin melalui Settings → Communication dan disimpan di SQLite |
| Penyederhanaan form user/membership | v1.15 deployed, form produksi terverifikasi | Divisi/profile tidak lagi diinput, runtime berbasis Role level, import empat kolom dengan kompatibilitas lima kolom lama. Data legacy dipertahankan; 198 tes lokal lulus |
| Custom Instruction | Sudah | Field global user dan field per membership |
| Knowledge Loader | Sudah | Published document aktif dari SQLite; folder Markdown menjadi fallback sampai publish pertama |
| Document Ingestion | Sebagian | Upload PDF, DOCX, TXT, dan Markdown menjadi draft teks; OCR dan `.doc` belum |
| Chat History | Sudah | SQLite dipisahkan per user, perusahaan, dan module; General memakai scope kosong tersendiri |
| Provider Abstraction | Implementasi v1.13 | OpenAI/DeepSeek/Gemini compatible API; Claude native Messages; General tetap OpenAI/DeepSeek |
| Settings AI | Implementasi v1.14 deployed; smoke HTTP produksi terverifikasi | Form Provider + API Key dan readiness encryption terverifikasi; 183 tes lokal lulus. Registry produksi masih kosong, sehingga tes provider nyata/pemilihan model produksi belum dilakukan |
| AI utama dan cadangan per Module | Sudah | Dua pilihan dari AI terdaftar; cadangan opsional, failover terbatas, tanpa fallback global; encrypted key atau legacy environment |
| Telegram Response Renderer | v1.16 deployed; 233 tes lulus; status Railway dan HTTP produksi terverifikasi | Safe HTML untuk jawaban biasa, teks polos khusus blok naskah siap salin; limit gabungan/history/split dan link preview off |
| Conversation Delivery Policy | Belum | Akan mengatur panjang, ritme, dan progressive disclosure |
| Multi-company membership | Sudah | Tabel membership SQLite; JSON hanya bootstrap awal |
| Company router dan active context | Sudah | Command `/company` dan session active company |
| Menu command adaptif | v1.18 deployed; 276 tes lokal lulus; Railway dan HTTP produksi terverifikasi | `/?`, `/help`, dan `/start`; satu company langsung daftar module, lebih dari satu menampilkan pilihan company; selalu sesuai akses aktif |
| Migrasi Company ID oleh operator | v1.19 produksi memakai `amz`, `ms`, dan `dkgroups`; migrasi serta HTTP admin terverifikasi | Startup-only, backup SQLite terverifikasi, transaksi atomik, seluruh relasi dan history dipertahankan; bukan field edit admin |
| Module router dan active context | Sudah | Command `/module`, General context, default-deny module access, dan reset module saat company berubah |
| Kode singkat module | v1.19 deployed; 304 tes lulus; TG tersimpan dan form produksi terverifikasi | Alias opsional 2–3 karakter per company; `/TG`, `/tg`, dan `/module TG` memilih ID canonical yang sama; menu sesuai akses |
| Konfirmasi perpindahan module | v1.20 deployed; 317 tes lulus; Railway dan HTTP produksi terverifikasi | Konfirmasi diikuti satu baris kosong dan deskripsi terkini dari form module; deskripsi kosong tidak ditampilkan |
| Company-scoped instruction | Sudah | Draft dan versi publish tersimpan di SQLite; file company menjadi fallback transisi |
| AI Module Playbook | Sudah | Registry per company, draft, preview, immutable publish, restore-to-draft, status, dan runtime prompt |
| Authorization per knowledge | Sebagian | Sudah company-scoped; knowledge khusus module, division, dan clearance belum |
| Response Validator | Sebagian | Pemisahan blok siap salin, normalisasi selektif, pemeriksaan jawaban kosong, batas panjang, dan split; belum ada policy classifier |
| Admin Panel | v1.23 deployed, 369 tes lokal lulus | Multi-admin berbasis database: super admin dan operator; session tujuh hari; Log Admin singkat dan read-only |
| Modul bersama | v1.21 deployed bersama v1.22; HTTP admin terverifikasi | Registry global terpisah; independent tanpa konteks perusahaan atau company-context dengan membership aktif; published snapshot, kode global unik, private history |
| Modul Learning | v1.23 deployed, 369 tes lokal lulus | AI utama/cadangan per buku, keterangan pembuka, custom instruction, persentase ekstraksi, readiness, PDF terindeks sekali, jadwal WIB, review sebelum publish |
| Retrieval/RAG | v1.24, 372 tes lokal lulus | FTS5 + cuplikan tetangga + recent chat; router lokal menormalisasi singkatan/variasi bahasa Indonesia untuk pertanyaan umum buku, tanpa embedding |
| Timeout, progress, dan retry | v1.23 deployed, 369 tes lokal lulus | Budget total AI 300 detik, primary maksimal 180 detik dan backup memakai sisa; pesan proses lokal setelah 60 detik; retry manual artikel tetap tersedia |

### 4.1 Modul bersama dan Learning (v1.21)

Tiga menu admin terpisah adalah **Modul perusahaan** (`/admin/modules`, perilaku existing dipertahankan), **Modul bersama** (`/admin/shared-modules`), dan **Modul Learning** (`/admin/learning`). Tidak ada migrasi otomatis module perusahaan menjadi global maupun perluasan ACL lama.

Modul bersama tersedia bagi semua Telegram ID whitelist aktif, bukan publik anonim. Admin menetapkan mode `company` (profile/instruction/knowledge/user preferences dan membership dari company aktif saja) atau `independent` (tidak memuat company, membership, role profile, custom instruction global user, ataupun history company). Independent dapat digunakan tanpa membership. AI utama/cadangan memakai registry, validasi model, dan failover existing. Tidak ada substitusi model maupun fallback credential global.

Kode bersama wajib 2–3 karakter ASCII diawali huruf. Namespace tidak boleh berbenturan dengan alias/ID company di seluruh company maupun built-in. `/shared` menampilkan katalog, `/module KODE` dan `/KODE` memilih module global; `/learning` merupakan command tersendiri. `/?`, `/help`, `/start`, `/module` menambahkan modul global yang tersedia. Command company tetap hanya ditampilkan bila membership aktif lebih dari satu. Pemilihan company/module perusahaan meninggalkan sesi global; pencabutan status/akses global tidak mengalihkan pesan diam-diam ke General.

`shared_modules` menyimpan draft/live JSON, `shared_versions` menyimpan snapshot immutable. Status dan alias operasional berlaku saat disimpan; isi/nama/description/mode/AI berubah di runtime hanya setelah publish. Timestamp mencegah form usang menimpa perubahan admin lain. `shared_messages` dipisahkan menurut user, module, company (kosong untuk independent), mode dan versi module. Publish/perubahan mode memulai konteks baru. Sesi ada di `shared_sessions`. Company ID migration mencakup `shared_messages.company_id` dengan validasi seluruh row dan backup.

Pengalaman belajar tidak di-hardcode. Setiap buku berisi judul (default nama PDF), keterangan pembuka, PDF, custom instruction, AI utama dan cadangan, mulai dan akhir `datetime-local`, status, serta konfirmasi review ekstraksi. AI dipilih per buku agar buku yang berbeda dapat memakai provider/model berbeda. Buku lama yang belum menyimpan pilihan masih membaca konfigurasi Learning legacy sebagai jalur migrasi; publish berikutnya mewajibkan AI pada buku. Editor Learning global dialihkan ke daftar buku. Tidak ada pemisahan library dan materi mingguan. Tidak ada kewajiban nomor halaman atau label analisis AI dalam jawaban.

Jadwal disimpan sebagai WIB dan UTC. Mulai inklusif, akhir eksklusif; default kedua jam 00.00. End harus setelah start dan jadwal live aktif tidak boleh overlap, termasuk saat reaktivasi. `learning_books` mempunyai draft/live snapshot dan `learning_versions` immutable. Runtime memeriksa jadwal pada setiap command/pesan dan kembali sebelum mengirim hasil AI. Jika expire/revoke/versi berubah di tengah request, jawaban lama dibuang. Buku baru aktif mengirim pembuka statis dan memakai history versi buku tersendiri. `learning_session_books` merekam buku yang sudah dibuka user. Pengulangan `/learning` tidak menghapus history. Tidak ada broadcast otomatis, cron, atau fallback ke buku kedaluwarsa.

PDF asli disimpan privat sebagai BLOB di `learning_sources` bersama SHA-256, filename, pages JSON dan laporan ekstraksi. Ini khusus Learning, berbeda dari upload knowledge existing yang hanya menyimpan teks/metadata. Sumber identik diproses sekali/deduplicated; sumber baru tidak menimpa versi lama. Bukan endpoint download publik. Review hanya untuk admin login, escaped HTML, per halaman. PDF maksimal 20 MB, 1.000 halaman dan 2 juta karakter. Ekstraksi `pypdf` di subprocess dengan timeout 40 detik, CPU 30 detik, address space Linux 768 MiB, satu ekstraksi per admin process. Request multipart dibatasi sebelum spooling; CSRF dan audit berlaku. PDF encrypted/rusak/tanpa teks/terlalu besar ditolak, tanpa truncation. PDF scan memerlukan OCR di luar fitur ini. Laporan menampilkan halaman terbaca, halaman tanpa teks, persentase halaman terbaca, dan menegaskan seluruh karakter yang berhasil diekstrak disimpan; persentase bukan ukuran keutuhan tabel/gambar/urutan teks. Upload baru selalu draft, menghapus tanda reviewed; publish PDF baru tanpa review ditolak.

DK menyetujui indeks lokal tanpa biaya embedding setelah risiko sinonim/parafrasa dijelaskan. `learning_chunks` per halaman maksimal 2.000 karakter dan SQLite FTS5 `learning_search` dipakai bersama, tanpa embedding atau ringkasan AI. Pencarian mempertimbangkan pesan/recent chat untuk pertanyaan lanjutan. Router lokal menormalisasi singkatan umum (`utk`, `dg`, `dgn`) serta bentuk seperti `manfaatnya`, `fungsinya`, `isinya`, dan `bukunya`; lalu mengenali daftar isi, susunan/isi bab, fungsi/manfaat, cakupan, gambaran atau penjelasan buku. Maksud umum memakai sampel tersebar tanpa panggilan AI tambahan. Pesan asli tetap dikirim ke provider dan disimpan di history, sehingga normalisasi tidak mengubah ucapan user. Tanpa kecocokan dan bukan maksud umum, bot meminta topik/bab. Variasi baru di luar router dan konteks lebih lama dari recent history tetap merupakan keterbatasan. Tidak ada OCR, ringkasan AI, cache respons lintas user, atau layanan embedding berbayar yang ditambahkan diam-diam.

Batas input source 16.000 karakter dan recent history maksimum konfigurasi `HISTORY_LIMIT` dengan batas 24.000 karakter khusus flow baru. Ini bukan pengukuran token exact atau jaminan persentase penghematan. Biaya output/custom instruction tetap ada. PDF/indeks bersama tidak berarti history user dibagikan. Pengujian biaya/relevansi pada buku nyata belum dilakukan. Instruksi DK agar opsi penghematan mendatang dijelaskan metode/risikonya dan diputuskan DK dicatat juga di `AGENTS.md`.

### 4.2 Timeout dan pengulangan modul artikel (v1.22)

Log produksi yang dikirim DK membuktikan Claude primary dan DeepSeek backup gagal dengan `TimeoutError`; backup berhenti sekitar 30 detik setelah failover. Log tersebut tidak membuktikan pembatasan dua pertanyaan, key salah, ataupun akar latensi provider/jaringan. DK menyetujui opsi 120 detik khusus pasangan ID `ms/artikel-web-generator`, tanpa mengganti model, mengurangi konteks, atau mengubah playbook produksi.

Sejak v1.23 seluruh inferensi AI memiliki budget total 300 detik. Primary mendapat maksimal 180 detik atau 60% budget, lalu backup yang memenuhi syarat memakai sisa waktu berdasarkan wall clock; tidak menjadi 300 detik per provider. HTTP adapter dan outer guard memakai budget request yang sama, dengan `max_retries=0`. Setelah 60 detik bot mengirim satu pesan status acak dari phrase bank lokal dan tetap menunggu task yang sama. Pesan ini tidak memanggil AI, tidak masuk history, dan tidak memulai ulang request. Tes koneksi/katalog tetap 30 detik. Budget lebih panjang mengurangi false timeout, tetapi tidak menjamin sukses atau menghemat token; provider primary mungkin tetap mengenakan biaya sebelum failover.

`module_pending_requests` menyimpan satu input tertunda per `(telegram_id, company_id, module_id)`, content, SHA-256 konteks efektif+history, dan timestamp. Tidak masuk history sukses, prompt user lain, maupun audit log. Input baru menggantikan pending pada scope sama. Sesudah gagal, user dapat mengetik `ulang` atau `/ulang` pada modul artikel yang sama. Retry membaca ulang otorisasi, published prompt, dan history; perubahan hash meminta user mengirim detail kembali. Retry tidak berjalan otomatis. `/ulang` di scope lain tidak mengambil pending artikel; kata biasa `ulang` pada modul lain tetap pesan normal.

Sesudah generasi berhasil, akses dan scope diperiksa kembali; transaksi atomik menyimpan pasangan user+assistant dan menghapus pending dengan pemeriksaan input/hash. `/reset` menghapus history dan pending pada scope aktif. Company ID maintenance menyertakan tabel pending dalam backup dan validasi migrasi. Pending disimpan di SQLite dengan perlindungan file yang sama seperti history; tidak mempunyai TTL otomatis, bertahan hingga sukses, reset, atau ditimpa input baru. Detail yang gagal sebelum versi ini tidak bisa dipulihkan otomatis dan perlu dikirim ulang sekali.

## 5. Target arsitektur multi-company

DK Corp memakai satu bot dan satu codebase untuk seluruh perusahaan. Perusahaan, membership user, modul, instruction, dan knowledge diperlakukan sebagai data terpisah. Sistem tidak membuat satu bot untuk setiap perusahaan.

```text
Telegram ID
    ↓
User
    ↓
User–Company Membership
company + job title + role level
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

Identitas user bersifat global, tetapi jabatan, Role level, dan akses bersifat per perusahaan. Kolom divisi/profile pada contoh data legacy berikut tetap tersimpan untuk kompatibilitas, bukan input admin atau sumber profile efektif sejak v1.15.

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

### 5.4 Lima cara berkomunikasi

Communication Profile tetap bersifat global agar standar komunikasi lintas grup konsisten. Super admin mengubah isi terpusat di Settings; membership hanya memilih Role level, bukan profile terpisah.

| Role level membership | Profile | Orientasi jawaban |
|---|---|---|
| Owner / Board | `owner` | Governance, nilai perusahaan, arah, alokasi modal, dan risiko besar |
| Executive / GM | `executive` | Keputusan, prioritas, risiko, opsi, dan trade-off |
| Manager / Head | `manager` | Rencana taktis, resource, timeline, KPI, dan koordinasi |
| Supervisor / Coordinator | `supervisor` | Pembagian kerja, kontrol mutu, hambatan tim, dan eskalasi |
| Staff / Operational | `staff` | Langkah kerja, checklist, contoh, standar selesai, dan eskalasi |

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
| `modules` | Modul milik perusahaan; `short_code` opsional unik case-insensitive per company; pasangan `ai_runtime_profile_id`/`ai_model` untuk utama dan `backup_ai_runtime_profile_id`/`backup_ai_model` untuk cadangan; model kosong mempertahankan fixed-model legacy |
| `ai_runtime_profiles` | Koneksi provider/endpoint, ciphertext API key atau environment lama, status tes dan timestamp katalog; model hanya untuk kompatibilitas profile lama |
| `ai_model_catalog` | Snapshot model per koneksi provider, flag kompatibilitas adapter chat, alasan dan timestamp; bukan daftar model hardcoded |
| `module_access` | Hak membership terhadap modul |
| `company_instruction_state` | Draft aktif dan pointer versi live per perusahaan |
| `company_instruction_versions` | Versi publish immutable per perusahaan |
| `module_playbook_state` | Draft aktif dan pointer versi live per module |
| `module_playbook_versions` | Versi playbook immutable per module |
| `knowledge_documents` | Metadata, status, draft, dan pointer versi live per company |
| `knowledge_document_versions` | Versi published immutable, title, content, checksum, actor, dan waktu publish |
| `user_sessions` | Active company dan active module per user |
| `messages` | History percakapan dengan scope user, company, dan module |
| `admin_users` | Akun admin, nama tampilan, hash password PBKDF2, peran super admin/operator, dan status |
| `communication_styles` | Isi lima gaya komunikasi dan fallback default yang dapat dikelola super admin |
| `learning_books` | Draft/live buku termasuk jadwal, sumber, instruction, serta pasangan AI utama/cadangan per buku |

Semua query knowledge wajib memiliki filter `company_id`. Filter `module_id` ditambahkan ketika modul mempunyai knowledge khusus.

### 5.9 Target admin control plane

Admin panel akan menjadi control plane sederhana di atas service dan data model yang sama dengan bot. Target antarmuka memakai HTML server-rendered, bukan SPA terpisah. Setelah migrasi selesai, database menjadi source of truth dan file JSON hanya dipakai untuk bootstrap, import, atau recovery terkontrol.

Empat area utama:

1. Dashboard ringkas;
2. Companies: profile, instruction, knowledge, dan modules;
3. Users & Access: identity dan company membership;
4. Log Admin: siapa login dan ringkasan tindakan administratif.

Instruction, Knowledge, dan Module Playbook memakai alur Draft → Preview → Publish → Restore to Draft. Edit draft tidak langsung memengaruhi bot produksi. Pada editor, **Review untuk publish** menyimpan isi terbaru dan langsung membuka Preview; Publish tetap menjadi tindakan terpisah.

Versi admin saat ini menyediakan:

- akun admin berbasis database dengan peran `super_admin` dan `operator`; credential environment membuat akun utama dan password environment menjadi jalur pemulihan saat restart;
- signed session cookie dengan masa aktif tujuh hari;
- pembatasan lima kegagalan login per lima menit per client;
- dashboard statistik company, user, membership, dan message;
- halaman Companies untuk tambah, ubah nama, aktivasi, dan nonaktivasi;
- halaman Users & Access untuk tambah/ubah user, status whitelist, serta membership;
- halaman Company Instructions untuk menyimpan draft, preview, publish, dan riwayat versi;
- halaman Knowledge untuk membuat dokumen per company, menyimpan draft, preview, publish, aktivasi/nonaktivasi, dan riwayat versi;
- form Knowledge menerima teks langsung atau upload PDF, DOCX, TXT, dan Markdown maksimal 10 MB;
- restore versi lama ke draft agar selalu melewati preview sebelum dipublikasikan kembali;
- halaman Modules untuk membuat registry per company, mengubah identitas, status, draft, preview, publish, dan riwayat versi playbook;
- form buat/edit Module menyediakan pilihan **AI utama** dan **AI cadangan** dari daftar AI aktif; halaman **Tambah AI** mendaftarkan koneksi sekali untuk dipakai ulang;
- Settings AI mengelola API key terenkripsi, empat provider, status aktif dan tes koneksi manual; form Module tetap dua pilihan nama, bukan input secret;
- akses module default-deny dikelola pada form edit membership;
- halaman Log Admin read-only menampilkan actor dan ringkasan tindakan; metadata minimum tetap disimpan internal untuk traceability tanpa menampilkan detail teknis/isi sensitif;
- super admin dapat membuat/nonaktifkan operator dan mengubah lima gaya komunikasi; operator tidak dapat mengelola akun admin, Settings AI, atau Settings Communication;
- security headers dan health endpoint.
- CSRF token untuk seluruh mutasi Company;
- audit event untuk create, update, activate, dan deactivate Company.
- tambah user selalu membuat membership pertama sebagai company default;
- membership tambahan dapat mengatur jabatan, Role level, custom instruction, status, dan default. Divisi/profile dihilangkan dari form serta tabel membership; gaya jawaban otomatis dari role.
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

### 5.9.4 Import user Excel

Admin membuka **Users & Access → Import user**, memilih file dan pengaturan batch, lalu menekan **Import user baru**. Route GET/POST `/admin/users/import` membutuhkan session admin; POST memvalidasi CSRF. Hasil menampilkan jumlah user/membership baru, nomor baris ID lama yang dilewati, dan duplikat identik. Tidak ada preview atau simpan parsial; error validasi menampilkan baris untuk diperbaiki dan mewajibkan upload ulang.

Kontrak file saat ini memakai **Nama, Telegram ID, Perusahaan, Jabatan**. Kolom kelima **Divisi** opsional hanya untuk kompatibilitas file lama; nilai kosong diperbolehkan, nilai nonkosong disimpan sebagai legacy dan tidak masuk konteks AI. Tab `Data_User` dipilih secara eksplisit; jika hanya satu tab, nama bebas. File multi-tab tanpa `Data_User` ditolak untuk mencegah tab contoh terimpor. Header harus lengkap, tanpa duplikat/kolom tambahan, dengan urutan fleksibel. Baris kosong diabaikan, maksimal baris 501 termasuk header pada baris 1.

Parser `app/user_import.py` menggunakan `openpyxl` untuk `.xlsx` dan `xlrd` untuk `.xls`; `defusedxml` mengamankan pembacaan XML. Upload maksimal 5 MB; jumlah byte request sebenarnya dibatasi sebelum multipart parsing (tambahan 64 KiB untuk form), bukan hanya mempercayai Content-Length. Maksimal satu file, delapan form fields, dan 20 MB expanded ZIP/1000 entries. Parser tidak menjalankan macro/formula atau mengambil external links; `.xlsx` formula/error ditolak, `.xls` membaca cached values. File rusak, password, tipe sel tidak didukung, ID pecahan/invalid, serta ID numerik lebih dari 15 digit ditolak; ID panjang harus berupa teks agar presisi tidak hilang.

`Database.import_new_users` mengambil write lock SQLite (`BEGIN IMMEDIATE`) sebelum memeriksa ID, memvalidasi seluruh data baru, lalu melakukan insert user, membership, dan audit dalam satu transaksi. Tidak ada UPDATE/UPSERT user lama. Semua baris Telegram ID yang sudah ada, termasuk ID nonaktif, dilewati sebelum validasi atribut bisnis sehingga tidak mengganti identitas, status, membership, default, instruction, session, history, atau akses module. Jika satu baris user baru tidak valid atau penulisan gagal, seluruh batch rollback. Impor ulang maupun import bersamaan tidak menimpa data.

Nama perusahaan dicocokkan secara exact setelah normalisasi spasi/case dengan company aktif; Company ID juga diterima. Kecocokan ambigu, tidak ada, atau company nonaktif ditolak; company tidak dibuat otomatis. Satu ID baru dapat memiliki beberapa membership jika nama konsisten; company pertama menjadi default. Duplikat identik ID/company dilewati dan konflik atribut ditolak.

Role level dan whitelist dipilih admin pada form untuk user baru dalam batch, default `staff` dan whitelist nonaktif. Communication profile diturunkan server dari role; field profile kiriman browser diabaikan. Membership baru aktif, custom instruction kosong, dan akses module tetap default-deny. Jabatan tidak digunakan untuk menebak hak akses/profile. Admin dapat menyesuaikan per user setelah import. Data upload tidak disimpan permanen; audit batch menyimpan nama file, SHA-256, pengaturan batch, serta jumlah user/membership/skip/duplikat, bukan isi workbook.

Tidak ada migrasi schema atau backfill. Penyederhanaan ADR-057 diterapkan pada v1.15 bersama form dan runtime, tanpa mengubah aturan insert-only/atomic import.

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

Contoh representasi legacy satu user (kolom role/divisi/profile global dipertahankan untuk kompatibilitas; konteks efektif menggunakan membership aktif):

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
| `division` | Data legacy, tidak lagi diinput atau dimasukkan ke konteks AI |
| `communication_profile` | Nilai legacy tersimpan; profile efektif diturunkan dari Role level membership aktif |
| `custom_instruction` | Kebutuhan khusus individual |
| `active` | Status akses bot |

## 7. Communication Profile

### 7.1 Owner / Board

Target user: owner, komisaris, board, dan pengambil keputusan tertinggi. Jawaban berfokus pada nilai perusahaan, arah, alokasi modal, risiko besar, dan keputusan yang perlu diambil.

### 7.2 Executive / GM

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

### 7.3 Manager / Head

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

### 7.4 Supervisor / Coordinator

Target user: supervisor, coordinator, dan team lead. Jawaban berfokus pada pembagian kerja, urutan eksekusi, kontrol mutu, hambatan tim, dan eskalasi.

### 7.5 Staff / Operational

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

### 7.6 Default

Dipakai ketika profile tidak dikenali. Jawaban bersifat seimbang, praktis, dan tidak mengasumsikan senioritas user.

## 8. Resolusi profile dan prioritas instruksi

Sejak v1.15, ADR-057 menjadi perilaku aktif. `profile_id_for_role` pada `app/role_profiles.py` menjadi pemetaan terpusat bagi form, import, dan runtime bot.

Isi profile awal berasal dari `config/role_profiles.json`, lalu di-bootstrap ke `communication_styles`. Runtime membaca SQLite agar perubahan **Settings → Communication** langsung berlaku. Pemilihan profile:

1. Ambil `role_level` membership company aktif: `owner` → `owner`, `gm` → `executive`, `manager` → `manager`, `supervisor` → `supervisor`, `staff` → `staff`.
2. Ambil isi profile dari `config/role_profiles.json`. Role legacy kosong/tidak dikenal atau ID profile tidak tersedia memakai profile default, tanpa fallback jabatan/override global.
3. Field `communication_profile` global/membership lama tidak memengaruhi pemilihan profile. Fungsi resolver alias lama tetap tersedia untuk kompatibilitas kode, tetapi bot selalu memberinya ID hasil pemetaan role.

Form tambah user dan membership mengabaikan field divisi/profile yang disisipkan pada request; membership baru tanpa Divisi disimpan dengan string kosong dan profile hasil pemetaan role. Form edit membership memvalidasi role baru, lalu mempertahankan nilai mentah divisi/profile existing dalam write transaction (`BEGIN IMMEDIATE`) agar tidak tertimpa field yang sudah dihilangkan. Tidak ada backfill/reset data. Profile efektif langsung mengikuti role baru meskipun kolom profile legacy masih berisi nilai lama. Divisi tidak lagi disisipkan ke system prompt atau `/whoami`; riwayat chat/knowledge lama tidak dihapus atau disaring ulang. Status whitelist, membership, default company, dan module access tetap independen.

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

Aturan dengan prioritas lebih rendah tidak boleh menonaktifkan aturan keamanan pada lapisan di atasnya. Kontrak pengiriman ditempatkan di akhir system prompt: jawaban biasa tetap terformat, hanya isi naskah siap salin memakai blok khusus. Kontrak format tidak mengubah substansi bisnis maupun aturan keamanan.

## 9. Telegram Response Renderer

Telegram Response Renderer adalah lapisan teknis di Python. Lapisan ini tidak menentukan apakah jawaban harus strategis, taktis, atau operasional. Tugasnya hanya memastikan draft respons tampil konsisten dan aman di Telegram.

```text
Jawaban AI: penjelasan Markdown + blok naskah siap salin bila relevan
    ↓
Pisahkan blok [[COPY_TEXT]] ... [[/COPY_TEXT]]
    ├── di luar blok → pertahankan Markdown
    └── isi blok → normalisasi subset markup menjadi teks polos
    ↓
Batas panjang gabungan dan pemeriksaan jawaban tidak kosong
    ↓
Simpan history tanpa penanda pengiriman
    ↓
Kirim setiap bagian tersendiri, split dengan target 3.500 karakter
    ├── penjelasan → safe HTML + fallback plain text jika parsing ditolak
    └── naskah → parse_mode=None, tanpa formatting entities
```

### 9.1 Pemilihan format berdasarkan isi

Default tetap renderer HTML sebagaimana ADR-008. Bold, italic, inline code, fenced code, bullet, quote, spoiler, heading, dan link HTTP/HTTPS diproses seperti sebelumnya. HTML mentah dari model di-escape; bukan dieksekusi.

Kontrak AI meminta `[[COPY_TEXT]]` dan `[[/COPY_TEXT]]` pada baris tersendiri hanya untuk ISI naskah siap disalin/diposting, misalnya Threads, caption Instagram, copy iklan, dan draft pesan. Judul/nomor pilihan, sapaan, pertanyaan, analisis, tips, dan petunjuk lanjutan tetap di luar blok. Setiap alternatif naskah memakai blok sendiri agar copy-paste tidak membawa petunjuk admin/bot. Perbedaan ini berlaku berdasarkan tujuan isi, bukan hardcode nama module: pilihan akun dalam Threads generator tetap terformat, sementara General dapat menghasilkan caption polos.

`prepare_response_parts` menghasilkan bagian beratribut `copyable`. Hanya bagian tersebut yang memakai normalisasi berikut.

| Input AI legacy | Hasil teks polos | Catatan |
|---|---|---|
| `**teks**`, `__teks__` | teks | Tanpa bold |
| `*teks*`, `_teks_` | teks | Tanpa italic |
| `` `teks` `` | teks | Isi literal dipertahankan |
| fenced code triple backtick | isi kode dengan indentasi | Tidak dibungkus style code block |
| `- item`, `1. item` | tetap berupa daftar teks | Tidak mengubah urutan pilihan |
| `> kutipan` | kutipan | Baris `>` kosong menjadi baris kosong |
| `||teks||`, `~~teks~~` | teks | Tanpa spoiler/strikethrough |
| `[label](https://...)` | label (https://...) | URL tidak hilang ketika disalin |

Di dalam blok siap salin, heading Markdown menjadi teks biasa. Kontrak meminta naskah tanpa tanda kutip pembungkus maupun bullet per baris. Paragraf, hashtag, emoji, dan tanda baca konten tetap dipertahankan. Pembersihan bersifat best-effort untuk subset legacy, bukan parser Markdown umum; HTML literal dan markup tak dikenal tidak diinterpretasikan di jalur polos. URL/mention dapat tetap dikenali otomatis oleh aplikasi Telegram.

Pemilihan blok bergantung pada kepatuhan model terhadap kontrak output; bukan classifier semantik deterministik atau API tambahan. Tanpa penanda, jawaban tetap terformat. Parser hanya mengenali penanda pada baris tersendiri di luar fenced code penjelasan. Blok tanpa penutup dianggap polos sampai akhir; penanda penutup liar/nested dihilangkan, bagian kosong tidak dikirim. Format campuran, penanda, dan limit diuji memakai fixture; kualitas penandaan oleh provider nyata belum diverifikasi.

### 9.2 Security boundary

- Jawaban biasa memakai safe HTML dengan escape semua HTML mentah dari model. Hanya tag buatan renderer diizinkan; parsing ditolak Telegram memakai fallback plain text.
- Blok siap salin memakai `parse_mode=None`, tanpa formatting entities. Penanda hanya memengaruhi pengiriman/format, bukan izin, routing, atau tool execution.
- URL HTTP/HTTPS dipertahankan sebagai teks; renderer tidak mengambil isi URL atau menjalankan kode.
- Pemisahan penanda dan normalisasi selektif berlangsung sebelum limit karakter dan split, agar penanda tidak bocor di batas chunk. Budget panjang berlaku gabungan termasuk pemisah history; jawaban kosong tidak disimpan sebagai sukses.
- Setiap bagian dikirim sebagai pesan tersendiri dengan target split 3.500 karakter. Jalur plain tidak memakai fallback HTML; jalur biasa mempertahankan fallback parsing lama.
- Link preview dinonaktifkan untuk respons AI.
- Berlaku sama pada General, AI utama Module, dan AI cadangan. Routing, timeout, credential, otorisasi, dan company/module isolation tidak berubah.
- History baru menyimpan gabungan teks bagian tanpa penanda, tetap mempertahankan Markdown penjelasan dan naskah polos. History existing, pesan Telegram lama, dan versi published playbook/instruction/knowledge tidak diubah; tidak ada migrasi database.

### 9.3 Batas tanggung jawab

Renderer hanya memisahkan artefak siap salin dari penjelasan melalui kontrak penanda. Panjang ideal, jumlah pilihan, tone, satu pesan satu tujuan, dan progressive disclosure umum tetap menjadi tanggung jawab Conversation Delivery Policy yang akan dibangun terpisah.

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
- credential profile Module dengan ID immutable, status aktif, provider, model, base URL, ciphertext atau named environment source, dan hasil tes koneksi.

Railway Volume dipasang pada `/app/data` agar database bertahan saat redeploy.

AI terdaftar menyimpan metadata dan API key terenkripsi di SQLite. Profile lama tetap membaca Railway Variables atau `.env` lewat `api_key_env`. Mode General memakai global `AI_*`; request Module tidak mencoba credential General ketika panggilan AI gagal.

### Settings AI, encryption, dan adapter (v1.14)

- `/admin/settings/ai` adalah registry bersama untuk admin. Tambah AI hanya meminta Provider dan API Key; label/ID dan endpoint resmi otomatis, status awal aktif. Provider berbeda didaftarkan sebagai koneksi baru; form edit existing tidak boleh mengganti provider. Tidak ada input nama AI/model/base URL. Beberapa koneksi provider yang sama diperbolehkan dengan ID otomatis unik untuk membedakannya.
- Migrasi additive/idempotent menambah `api_key_ciphertext`, `last_test_status`, `last_test_at` (TEXT default kosong). `api_key_env` kosong untuk encrypted profile. Referensi Module, playbook, akses, session, history dan credential lama dipertahankan.
- Fernet payload version 1 mengikat profile ID/provider. Ciphertext yang dipindahkan ke profile/provider lain ditolak. Master `AI_CREDENTIAL_ENCRYPTION_KEY` berada hanya di environment, terpisah dari SQLite dan admin session secret. Ciphertext tidak masuk HTML, audit atau repr profile.
- Master tidak dibuat/diganti otomatis. Master tidak valid/hilang memblokir encrypted save/decrypt, tanpa fallback environment pada ciphertext rusak. Legacy tetap bekerja. Backup master terpisah wajib; rotasi master otomatis belum tersedia. Jangan mengganti variable tanpa migrasi ciphertext/backup.
- Form edit dengan key kosong mempertahankan key, katalog dan status lama; key baru mengganti/converts legacy ke endpoint resmi. Metadata+key disimpan dalam write transaction. Audit hanya sumber/penggantian key, bukan nilai. Update credential menghapus katalog dan status tes; Module yang memakai pilihan katalog perlu tes ulang sebelum runtime berjalan. Profile fixed-model lama tetap dapat digunakan tanpa katalog.
- Encrypted key hanya menuju endpoint resmi. Custom endpoint tetap khusus legacy environment. HTTP redirect client Module/probe tidak diikuti. Startup bot/admin menonaktifkan HTTP/SDK DEBUG logging agar body/header sensitif tidak tercatat.
- Registry `openai`, `anthropic`, `deepseek`, `gemini`. Gemini memakai `https://generativelanguage.googleapis.com/v1beta/openai/`. Claude memakai native `/v1/messages`, `x-api-key`, version header, system terpisah, hanya text block, maksimum output chat 4096 token. Tools/multimodal tidak ditambahkan; General global tetap OpenAI/DeepSeek.
- POST **Tes & ambil model** membutuhkan admin+CSRF dan provider aktif. Tes tidak lagi menghasilkan teks; hanya GET katalog provider memakai key tersimpan tanpa data bisnis, retry atau fallback. OpenAI/DeepSeek memakai `/models`, Claude `/v1/models` dengan `after_id`, Gemini native `/v1beta/models` dengan `pageToken` dan header `x-goog-api-key` (bukan key di URL). Tombol sama untuk refresh manual; tidak ada polling otomatis saat GET halaman.
- Migrasi v1.14 additive/idempotent menambah tabel `ai_model_catalog`, timestamp `ai_runtime_profiles.catalog_updated_at`, serta `modules.ai_model` dan `modules.backup_ai_model` default kosong. Module menyimpan ID koneksi dan ID model secara terpisah; key tidak disalin untuk tiap model. Profile baru memakai kolom model kosong; pilihan Module lama dengan kolom model kosong membaca fixed-model profile lama. Tidak ada reset, auto-switch model, atau perubahan company/membership/playbook/history.
- Semua model yang dikembalikan provider tampil di Settings. Dropdown Module menampilkan seluruh model provider aktif; model non-chat atau belum didukung ditampilkan disabled beserta alasan. Gemini menggunakan `supportedGenerationMethods` ditambah pemilahan keluarga; OpenAI/Claude/DeepSeek memakai kebijakan keluarga adapter eksplisit karena metadata endpoint tidak seragam. Ini bukan jaminan endpoint/saldo/izin inferensi. OpenAI Responses-only (pro/Codex/deep-research), audio/realtime, image, embeddings, dan keluarga belum dikenal tidak selectable. Model lama ditandai **Konfigurasi lama** dan tetap tersedia. Tidak menambahkan Responses API atau kemampuan multimedia.
- Snapshot diganti atomik hanya setelah semua halaman valid. Batas 30 detik total, 15 detik per HTTP request, 20 halaman, 5.000 model dan 2 MiB per halaman; cursor berulang, ID malformed, respons parsial/terlalu besar ditolak tanpa mengganti katalog. Tidak mengikuti redirect atau URL halaman dari respons. Label provider/model response bebas tidak disimpan; ID valid ditampilkan dengan escaping template. Audit katalog hanya status dan jumlah model.
- Gagal refresh mempertahankan snapshot terakhir, timestamp dan pilihan Module. Refresh sukses kosong/yang menghapus model mencabut ketersediaan pilihan itu; runtime fail-closed sampai admin memilih ulang, tidak mengubah pilihan tersimpan atau berpindah ke General. Tes katalog membuktikan akses metadata, bukan saldo/chat. Uji inferensi dilakukan terpisah lewat bot setelah model dipilih. Kontrol model tetap milik admin Module, bukan pemilih model baru di Telegram.
- Status tes enum aman (`success`, `authentication`, `rate_limit`, `unavailable`, `configuration`, `request`, `response`) dan waktu UTC, bukan body error. Hasil katalog hanya diterapkan jika `updated_at` sama dengan snapshot sebelum request dan provider tetap aktif. Timestamp katalog terpisah membedakan snapshot kosong valid, belum tes katalog, dan tes inferensi versi lama. Key environment yang diganti di luar admin perlu dites ulang; hasil bukan jaminan saldo/uptime.
- Form credential maksimal 16 KiB sebelum parsing, tanpa file/field ganda. Tes maksimal tiga/menit total dan per profile, dua bersamaan, satu per profile, in-memory satu-process. Batas reset saat restart, bukan distributed limiter. Client probe ditutup setelah selesai.
- Failover mempertahankan ADR-060/061. Error koneksi/408/429/5xx Claude dinormalisasi setara provider compatible; auth/dekripsi/invalid request/empty/refusal tidak memicu backup. Cache key fingerprint berubah saat rotasi. API key tidak masuk prompt.
- Implementasi diuji dengan SQLite sementara dan HTTP mock. Rilis v1.13 melalui GitHub → Railway tanpa reset data. Pada 2 September 2026, master produksi yang sebelumnya belum ada dipasang melalui Railway CLI terautentikasi dengan target project/service/environment eksplisit. Key dibuat dalam memori dan dikirim melalui stdin, bukan argumen perintah, output, file repository, atau database; variable existing dipertahankan.
- Deployment aktivasi master `c61d258c-5bc3-48be-a58b-7234316f6277` berstatus `SUCCESS`. HTTP health, login admin, Settings AI dan form Tambah AI terverifikasi; warning master hilang dan empat provider tersedia. Ini memverifikasi readiness konfigurasi, bukan keberhasilan panggilan provider. 132 tes lokal lulus ulang dengan data sementara; tidak ada credential uji ditambahkan atau reset data produksi. Koneksi berbayar empat akun provider belum diuji.
- Salinan backup master di secret manager terpisah belum dibuat oleh proses deployment ini. Operator tetap wajib menyimpan backup master terpisah dari SQLite dan tidak merotasinya tanpa migrasi ciphertext. Login dashboard aplikasi tidak memberikan akses Railway Variables.
- Rilis v1.14 commit `a383167` melalui GitHub → Railway terverifikasi `SUCCESS`, deployment `48883395-d1c1-4941-b59f-3e47a2c4bc5b`. Health 200, akses anonim Settings/Tambah AI diarahkan ke login, login admin berhasil, Settings dan form dua-field empat provider berstatus 200, master encryption siap, serta tidak ada key lokal dipantulkan dalam HTML. Registry AI produksi kosong; Module baru menampilkan kondisi belum ada model, bukan dropdown. Pilihan utama/cadangan dan runtime diuji dengan fixture lokal, bukan akun provider produksi. Tidak ada reset data, rotasi master, atau credential uji dibuat di produksi.
- Kondisi Module tanpa opsi model mengarahkan admin ke Settings AI untuk mendaftarkan provider atau mengambil katalog koneksi aktif; bukan menganggap semua koneksi belum aktif. Pesan ini juga berlaku sebelum tes katalog pertama.

Referensi resmi: [OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create), [Claude Messages](https://platform.claude.com/docs/en/api/messages/create), [Gemini compatibility](https://ai.google.dev/gemini-api/docs/openai), [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/), [Fernet](https://cryptography.io/en/latest/fernet/).

Discovery: [OpenAI Models](https://developers.openai.com/api/reference/python/resources/models/methods/list), [Claude Models](https://platform.claude.com/docs/en/api/models/list), [DeepSeek Models](https://api-docs.deepseek.com/api/list-models/), [Gemini Models](https://ai.google.dev/api/models). Implementasi v1.14 diverifikasi lokal dengan HTTP simulasi/SQLite sementara serta smoke HTTP produksi setelah deployment. Tes akun provider nyata belum dilakukan untuk perubahan ini.

### 10.1. AI utama dan AI cadangan Module

- AI utama wajib dipilih. AI cadangan opsional, harus berbeda pasangan koneksi+model efektif. Koneksi yang sama boleh digunakan dengan model berbeda, tetapi dapat terkena gangguan/limit yang sama. Keduanya harus merujuk profile aktif dan model selectable dalam katalog koneksi itu saat disimpan (kecuali fixed-model legacy). Server memvalidasi pasangan, bukan mempercayai dropdown. Admin memilih provider+model tanpa mengisi key di Module.
- Kolom lama `modules.ai_runtime_profile_id` tetap menjadi AI utama. Migrasi idempotent menambah `backup_ai_runtime_profile_id TEXT REFERENCES ai_runtime_profiles(profile_id)` dengan nilai awal `NULL`, tanpa menghapus atau mengganti Module, versi, akses, maupun riwayat yang sudah ada.
- Update dari form/client lama yang tidak mengirim field cadangan mempertahankan pilihan cadangan existing. Menghapus cadangan memerlukan nilai kosong eksplisit; validasi dan update pasangan AI berada dalam write transaction.
- AI terdaftar dapat dipakai bersama oleh beberapa Module. Perubahan metadata AI berlaku pada semua referensi; perubahan pilihan utama/cadangan pada Module berlaku pada pesan berikutnya tanpa publish ulang playbook.
- Setiap pesan selalu mencoba AI utama terlebih dahulu. Cadangan yang dipilih hanya dicoba sekali setelah `APIConnectionError` (termasuk timeout SDK), timeout aplikasi, HTTP 408, HTTP 429, atau HTTP 5xx. Kedua AI tidak dipanggil paralel dan tidak ada loop retry/cadangan ketiga.
- HTTP 400/401/403/404/409/422, error tak dikenal, jawaban kosong/refusal, profile primary nonaktif, atau key primary kosong tidak memicu cadangan. Jawaban/refusal dari provider tidak dianggap alasan untuk mencoba provider lain. Klasifikasi error mengacu pada [OpenAI Docs](https://developers.openai.com/api/docs/guides/error-codes); pemilihan subset untuk failover adalah kebijakan aplikasi.
- Sejak v1.23 satu pesan mempunyai budget total 300 detik. Primary maksimal 180 detik/60%, backup memakai sisa wall-clock budget, dan SDK tetap `max_retries=0`. Satu pesan status lokal dikirim setelah 60 detik tanpa biaya token atau penulisan history. Probe/katalog tetap 30 detik. Pembatalan task tidak memicu failover.
- Profile/key cadangan baru di-resolve setelah utama gagal. Cadangan nonaktif, hilang, atau tanpa key menghentikan request. Menonaktifkan AI yang hanya dipakai sebagai cadangan tidak mematikan AI utama. Menonaktifkan profile utama tetap mengikuti perilaku lama: Module tidak ditawarkan dan session Module terkait dibersihkan ke General; ini bukan retry request yang gagal memakai key General.
- Failover memakai system prompt, knowledge, instruction, published playbook, company, user, module, dan history yang sama. Hanya jawaban final sukses yang ditulis bersama pesan user satu kali; jika kedua panggilan gagal, tidak ada penambahan history.
- Memilih cadangan merupakan izin admin agar konteks yang sama dapat dikirim ke provider cadangan. Hal ini dijelaskan di form. Timeout tidak menjamin provider utama belum memproses request, sehingga biaya dapat timbul pada kedua provider. Dua profil dengan provider/account yang sama belum tentu melindungi dari gangguan atau limit bersama.
- Perubahan pasangan AI masuk audit konfigurasi. Runtime failover dicatat di log dengan ID profile, tipe error, dan status HTTP saja, tanpa key, prompt, URL atau body error provider. Pemakaian cadangan belum memiliki dashboard usage/billing tersendiri.

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
- master key, credential General, dan credential legacy disimpan di environment;
- Settings menyimpan ciphertext, bukan plaintext API key. Module hanya referensi profile. Key tidak dirender kembali dan tidak masuk audit/log. Master dan backup SQLite harus disimpan terpisah;
- resolver Module membaca named secret saat runtime dan cache hanya memakai fingerprint SHA-256, bukan key mentah sebagai identifier;
- Module dengan AI utama nonaktif tidak dapat dipilih. Key utama yang hilang menghentikan request; hanya AI cadangan yang dipilih admin yang boleh menerima retry operasional, bukan AI Module lain atau General;
- `.env` dan database runtime tidak masuk Git;
- HTTP client log tidak menampilkan URL Telegram pada level normal;
- output model di-escape dan dirender melalui safe Telegram HTML;
- knowledge diperlakukan sebagai referensi, bukan system instruction.
- membership membatasi perusahaan yang dapat dipilih user;
- history dan content AI dipisahkan berdasarkan company aktif;
- path content company harus relatif dan tidak boleh keluar dari project root.
- admin memakai akun database, password hash PBKDF2, peran super admin/operator, dan signed session cookie tujuh hari;
- cookie admin bersifat `HttpOnly`, `SameSite=Lax`, dan `Secure` pada deployment;
- admin mengirim CSP, anti-frame, no-sniff, no-referrer, dan no-store headers;
- percobaan login admin dibatasi secara in-memory.
- operator dibatasi dari manajemen akun admin, credential AI, dan konfigurasi komunikasi; middleware server menegakkan batas selain navigasi UI;
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
- satu credential global untuk General dan AI terdaftar yang dapat dipakai ulang sebagai utama/cadangan pada Module;
- satu instance Railway;
- seluruh knowledge company aktif dimuat sampai `KNOWLEDGE_MAX_CHARS`;
- upload knowledge mendukung PDF text layer, DOCX, TXT, dan Markdown; OCR serta `.doc` lama belum;
- Company, user, membership, lima communication profile, Company Instruction, Knowledge, Module, Learning, dan module access dikelola melalui admin;
- module-scoped knowledge belum diimplementasikan; module masih memakai Knowledge company ditambah playbook;
- combined legacy Funnel Coach masih dipakai sebagai instruction DK Corp Group sampai dokumen dipisahkan;
- Company Instruction, Knowledge, dan Module Playbook sudah writable dan versioned; Log Admin menampilkan ringkasan tindakan administratif.
- concurrency masih berada dalam satu process dan belum memakai durable application queue terpisah.

## 14. Roadmap

### Penyederhanaan user/membership, diterapkan pada v1.15

Keputusan 2 September 2026 memilih opsi 1, sederhanakan form tetapi pertahankan data lama. DK meminta penerapannya pada 3 September 2026 dan mengotorisasi commit/deploy setelah tes lulus.

- Hilangkan input **Divisi** dan pilihan **Communication profile** dari form tambah/edit user serta membership. Pertahankan **Role level** yang ditetapkan admin.
- Tentukan profile secara konsisten dari Role level membership aktif: `gm` → `executive`, `manager` → `manager`, `staff` → `staff`. Jangan menebak dari teks jabatan atau membiarkan override profile lama mengalahkan pemetaan ini.
- Pertahankan kolom dan nilai divisi/profile lama di database agar perubahan dapat dikembalikan; tidak menghapus data. Setelah penyederhanaan diterapkan, divisi tidak lagi dimasukkan ke konteks AI.
- Selaraskan validasi backend, penyimpanan membership, resolusi runtime, form admin, serta proses import dalam perubahan yang sama. Status aktif, default company, dan akses module tetap dikelola admin; pemetaan profile tidak memberikan hak akses baru.
- Template import menjadi **Nama, Telegram ID, Perusahaan, Jabatan**. File lima kolom lama tetap diterima; Divisi lama opsional dan tidak menjadi konteks AI.
- Verifikasi user baru/lama, perubahan Role level, dan user dengan membership beberapa company. Pastikan data lama tetap utuh dan aturan akses tidak berubah.

Paket form/backend/runtime/import telah diterapkan, diuji lokal, dan dideploy. Commit implementasi `4d492ea` mendapat status Railway success pada GitHub. HTTP produksi memverifikasi health, proteksi login, form tambah user, import, tambah/edit membership DK, dan daftar membership. Input divisi/profile tidak ditemukan; Role level tetap tersedia dan petunjuk import empat kolom tampil. Tidak ada migrasi schema, penghapusan, atau backfill data existing. Uji perilaku bot memakai fixture lokal, tanpa mengirim pesan Telegram atau membuat membership uji di produksi.

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
- AI utama/cadangan per Module dengan named environment secret, failover operasional terbatas, dan fail-closed untuk salah konfigurasi; selesai pada v1.12;
- import user baru dari Excel dengan skip ID lama dan transaksi atomik; selesai untuk template lima kolom;
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
| ADR-008 | Model menghasilkan subset Markdown, Python menghasilkan safe HTML; tetap default dengan pengecualian ADR-067 | Menjaga jawaban biasa terformat dan aman; naskah siap salin memiliki jalur polos khusus |
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
| ADR-049 | Module runtime membutuhkan status aktif, akses aktif, playbook published, dan credential profile aktif | Draft, module tanpa otorisasi, atau Module tanpa konfigurasi AI yang valid tidak boleh muncul pada daftar Telegram maupun masuk ke prompt |
| ADR-050 | History memakai scope user, company, dan module | Perpindahan antara General dan workflow module tidak boleh mencampur konteks percakapan |
| ADR-051 | Perubahan company atau hilangnya module access mereset active module | Session tidak boleh mempertahankan pointer menuju konteks yang tidak lagi valid atau diizinkan |
| ADR-052 | Review untuk publish menyimpan draft lalu membuka Preview | Mengurangi alur konten siap dari tiga tindakan menjadi dua tanpa menghilangkan pemeriksaan terakhir atau membuat publish tidak sengaja |
| ADR-053 | Module menyimpan referensi credential profile, bukan API key | Routing terpisah dari secret; sejak v1.13 ciphertext registry sesuai ADR-062, bukan plaintext/audit/HTML |
| ADR-054 | Named environment untuk legacy credential | Tetap didukung; pendaftaran baru memakai encrypted registry ADR-062 |
| ADR-055 | Mode General tetap memakai credential global | Perilaku dasar tetap kompatibel dan custom Module dapat memakai billing/provider berbeda |
| ADR-056 | Kegagalan konfigurasi credential Module tetap fail-closed; pengecualian operasional dibatasi ADR-060 | Key salah/kosong dan pencabutan akses tidak boleh memicu fallback. Cadangan harus dipilih eksplisit oleh admin |
| ADR-057 | Diterapkan v1.15: hilangkan input Divisi dan Communication profile; profile otomatis dari Role level, data lama dipertahankan | Form/backend/runtime/import diselaraskan. Override lama tidak mengalahkan role, tidak ada backfill atau perubahan akses. Menggantikan pemilihan profile terpisah pada ADR-005 |
| ADR-058 | Import Excel insert-only, ID lama dilewati seluruhnya di dalam write transaction | Memenuhi larangan menimpa data existing, termasuk user nonaktif/membership lama, dan mencegah race antara pengecekan ID dan penyimpanan |
| ADR-059 | Batch import atomik dengan pengaturan akses eksplisit di form admin | Kesalahan baris tidak menghasilkan simpan parsial; jabatan dari spreadsheet tidak boleh otomatis menaikkan akses. Whitelist awal nonaktif dan akses module tetap default-deny |
| ADR-060 | Module memilih AI utama dan cadangan opsional dari AI terdaftar | Menyederhanakan form menjadi dua pilihan nama, mempertahankan Module lama, dan hanya mengalihkan kegagalan koneksi/timeout/408/429/5xx ke cadangan yang dipilih admin |
| ADR-061 | Digantikan ADR-075; sebelumnya satu percobaan maksimal 30 detik per AI | Riwayat keputusan dipertahankan; budget sekarang berlaku per pesan, bukan dijumlahkan penuh per provider |
| ADR-062 | Key Settings memakai Fernet, master terpisah di environment | Blank preserves, fail-closed decrypt, endpoint resmi, tanpa plaintext SQLite, legacy tetap berjalan |
| ADR-063 | Empat provider dengan adapter protocol eksplisit | Claude native Messages; GPT/DeepSeek/Gemini compatible. General tidak dimigrasikan otomatis |
| ADR-064 | Tes manual, sintetis, terbatas, dan revision-aware | Mencegah pengiriman data bisnis, biaya retry diam-diam, error mentah, dan status tes usang |
| ADR-065 | Registrasi Provider + API Key dan katalog model dari API resmi | Menghilangkan input nama/model, memisahkan koneksi dari model, serta menampilkan kapabilitas adapter secara jujur; v1.14 mengganti tes inferensi UI ADR-064 menjadi tes katalog read-only |
| ADR-066 | Module memilih pasangan koneksi+model, dengan legacy fallback eksplisit | Satu key untuk banyak model; pemilihan server-validated, snapshot atomik/revision-aware, key rotation menginvalidasi katalog, dan pilihan lama tidak diubah diam-diam |
| ADR-067 | Hanya blok naskah siap salin yang polos, penjelasan tetap safe HTML | Model menandai isi naskah berdasarkan tujuan, bukan nama module; renderer mengirim bagian terpisah dan membersihkan penanda sebelum history/limit/split. Tidak memutasi konten published atau history lama |
| ADR-068 | Rename Company ID hanya sebagai maintenance startup, bukan edit form | Runtime lama harus berhenti; backup konsisten, FK deferred, pembandingan seluruh row, audit dan repeat-safe mencegah relasi hilang atau writer memakai ID lama |
| ADR-069 | Menu command ditentukan akses efektif, bukan respons AI | Literal `/?` diproses sebelum chat; `/help` dan `/start` memakai menu yang sama. Company chooser hanya untuk >1 membership/company aktif, daftar module tetap default-deny |
| ADR-070 | Kode singkat module sebagai alias, bukan rename ID canonical | Admin dapat mengganti alias tanpa memindahkan history, akses, playbook, atau credential. Alias unik per company, case-insensitive, berbagi namespace dengan ID canonical, dan tetap memakai ACL |
| ADR-071 | Modul bersama memakai registry global terpisah, dua mode konteks dan whitelist aktif | Tidak memperluas ACL modul perusahaan; independent tidak memuat konteks/company history, mode company tetap butuh membership |
| ADR-072 | Learning memakai PDF privat terindeks lokal sekali, buku terjadwal dan history privat per versi | Menghindari kirim seluruh buku setiap pesan tanpa embedding berbayar; risiko pencarian leksikal diterima DK, tidak ada cache jawaban lintas user |
| ADR-073 | Override 120 detik hanya `ms/artikel-web-generator`, model/konteks tetap | Menangani batas 30 detik yang terbukti di log; maksimal dua AI, request-local agar tidak mempengaruhi modul lain |
| ADR-074 | Pending input artikel terpisah dari history; retry manual dan sukses atomik | Detail gagal tidak hilang atau masuk history ganda; otorisasi/scope/hash konteks diperiksa kembali, reset menghapus pending |
| ADR-075 | Budget AI total 300 detik, primary maksimal 180 detik, backup memakai sisa; progress lokal setelah 60 detik | Mengurangi false timeout tanpa retry tersembunyi; status tidak memakai token/history, namun request yang timeout masih dapat menimbulkan biaya provider |
| ADR-076 | AI Learning dipilih per buku dan konfigurasi global Learning ditutup dari editor | Buku berbeda dapat memakai model berbeda; snapshot published menentukan runtime, dengan fallback legacy terbatas untuk migrasi |
| ADR-077 | Multi-admin dua peran, session tujuh hari, dan Log Admin ringkas | Sekretaris dapat mengoperasikan konten tanpa akses credential/admin; metadata minimum tetap internal untuk traceability |
| ADR-078 | Lima gaya komunikasi tersimpan di SQLite dan diturunkan otomatis dari Role level | Owner, Executive/GM, Manager/Head, Supervisor/Coordinator, dan Staff/Operational dapat diubah terpusat tanpa field profile per membership |

### Kode singkat module

Field opsional `Kode singkat` pada tambah/edit module menerima 2–3 huruf/angka ASCII, diawali huruf. Server menyimpan lowercase dan UI/menu menampilkan uppercase. Blank menghapus alias; field yang tidak dikirim client lama mempertahankan nilai lama. Penyimpanan identitas berlaku langsung, tanpa publish ulang playbook.

Schema additive menambah `modules.short_code TEXT NOT NULL DEFAULT ''` dengan unique partial index `(company_id, short_code COLLATE NOCASE)` untuk nilai nonkosong. Tidak mengisi alias massal otomatis. Transaksi `BEGIN IMMEDIATE` memeriksa benturan alias, ID module lain (termasuk nonaktif), dan command sistem seperti `off`. Kode sama boleh berada di company berbeda. Perubahan tercatat di audit `module.created`/`module.updated`.

`/TG`, `/tg`, dan `/module TG` memilih module dalam company aktif saja. `/module <id-lama>` tetap berlaku. Handler shortcut tepat 2–3 karakter dipasang setelah command bawaan dan sebelum chat, bekerja dengan/tanpa entity Telegram serta memeriksa suffix `@bot_username` bila ada. Shortcut tidak memanggil AI, tidak menyimpan pesan ke history, dan tidak melewati whitelist atau resolver akses module. Session selalu menyimpan ID canonical, bukan alias. `TG` tanpa slash tetap pesan chat. Kode tidak dikenal ditolak tanpa mengubah konteks; tidak mencoba company lain.

Menu `/?`, `/help`, `/start`, dan daftar `/module` memakai alias jika tersedia, atau command lama bila kosong. Whitelist, company/membership/module/access/AI aktif dan playbook published tetap disyaratkan. Penetapan `TG` untuk Threads generator adalah konfigurasi data module yang diminta DK, bukan hardcode nama module dalam routing.

Setelah pemilihan module berhasil melalui shortcut, `/module <id>`, atau `/module <alias>`, bot mempertahankan kalimat konfirmasi lalu menambahkan `\n\n` dan isi `modules.description` yang sudah di-trim. Deskripsi diambil dari identitas module terkini, tanpa inferensi AI atau publish ulang playbook. Deskripsi kosong/whitespace tidak menambah baris atau placeholder. Nama dan deskripsi dikirim sebagai plain text dengan link preview nonaktif, bukan diparsing menjadi HTML/Markdown. Konfirmasi General dan kegagalan akses tidak berubah; pesan konfirmasi tidak dimasukkan ke history.

### Menu Telegram adaptif

User mengetik `/?` atau `/help` untuk melihat perintah yang tersedia. `/start` menambahkan sapaan dan memakai menu yang sama. Command umum mencakup `/start`, `/?`, `/help`, `/whoami`, dan, bila ada membership aktif, `/module` serta `/reset` untuk scope aktif saja. Tidak ada panggilan provider, perubahan session, atau penulisan history saat melihat menu.

Jumlah company dihitung dari `list_memberships`, yang mensyaratkan membership dan company aktif. Satu company langsung menampilkan daftar module dan General, tanpa command/pilihan `/company`. Lebih dari satu company menampilkan command dan pilihan `/company <id>` serta module pada company aktif. Jika belum ada company aktif/default, menu meminta pemilihan company sebelum menampilkan module. User tanpa membership diberi petunjuk menghubungi admin, tanpa company/module. Whitelist tetap diperiksa sebelum menu ditampilkan.

Daftar module memakai resolver akses yang sudah ada: company/membership/module/access/AI aktif dan playbook published. Tidak menampilkan module milik company lain atau module yang belum diberikan. `/company` tanpa argumen untuk satu company juga menampilkan daftar module; command eksplisit `/company <id>` tetap tervalidasi seperti sebelumnya. Menu panjang dipecah menjadi pesan maksimal 3500 karakter, tanpa menghilangkan pilihan terakhir.

Karena `?` bukan karakter nama command yang diterima `CommandHandler`, literal `/?` (dengan whitespace tepi opsional) memakai text filter sebelum handler chat pada group yang sama. Ini bekerja baik dengan maupun tanpa entity `bot_command` dari Telegram. Penyebutan `/?` di dalam kalimat bukan permintaan menu. Tidak mengubah native command list Telegram/BotFather, schema, credential, atau data produksi.

### Migrasi Company ID saat maintenance

Company ID tetap otomatis dan terkunci pada form admin. Pengecualian operator tersedia melalui `COMPANY_ID_MIGRATION`, JSON mapping ID lama ke ID baru. Mapping kosong/tidak valid, tujuan yang sudah terpakai, sumber hilang, partial migration, schema referensi baru, atau relasi rusak membuat startup gagal sebelum bot/admin menerima traffic. Tidak ada fallback yang membuat company baru atau menggabungkan history.

Jalankan hanya ketika runtime lama sudah berhenti. Pada Railway, volume produksi `/app/data` mencegah dua deployment aktif memakai volume yang sama, sehingga migrasi dijalankan di startup, bukan pre-deploy yang tidak mendapatkan volume ([referensi volume Railway](https://docs.railway.com/volumes/reference), [ketersediaan volume](https://docs.railway.com/volumes)). Jangan menjalankan instance lokal yang menulis database produksi selama maintenance.

Urutan startup adalah initialize schema, migrasi jika diminta, bootstrap, lalu admin dan polling Telegram. Migrasi mengambil write lock, membuat SQLite backup konsisten dengan file permission `0600` dalam `backups/` di direktori database, memverifikasi integrity/FK backup, dan memindahkan `companies`, membership, session active company, module, module access, instruction state/versions, knowledge documents, serta `messages`. Foreign key tetap aktif dengan deferred validation. Hash seluruh row dibandingkan dengan transformasi ID yang diharapkan sebelum commit; semua perubahan dibatalkan bila berbeda. Nama/path file, isi dan versi dokumen, credential/model AI, status/default, isi pesan, timestamp, dan audit lama tidak diubah. Tambahan hanya satu audit event `company.ids_migrated` dengan mapping dan lokasi backup.

Restart dengan mapping yang sama menjadi no-op hanya jika target lengkap, sumber tidak tersisa, dan audit cocok. Setelah migrasi terverifikasi, hapus `COMPANY_ID_MIGRATION` dari environment. ID lama bukan alias dan URL admin lama perlu dibuka ulang dari menu. Backup berisi data privat dan ciphertext credential, bukan master encryption key; jangan commit atau membagikannya. Pemulihan harus dilakukan saat semua writer berhenti menggunakan SQLite backup API, bukan menimpa file database hidup atau mengabaikan WAL.

## 16. Changelog dokumen

### 1.24 — 5 September 2026

- Memperbaiki router Learning untuk pertanyaan percakapan seperti `apa daftar isi` dan `apa manfaatnya utk saya dg baca buku ini`.
- Menambah normalisasi lokal singkatan/bentuk kata tanpa panggilan AI tambahan dan tanpa mengubah pesan asli pada prompt/history.
- Menambah tiga regresi bahasa percakapan; seluruh 372 tes lulus lokal dengan satu warning deprecation Starlette/httpx existing.

### 1.23 — 5 September 2026

- Menambah admin database dengan peran super admin/operator, password PBKDF2, session tujuh hari, pembatasan Settings sensitif, serta Log Admin ringkas. Akun environment tetap menjadi akun utama dan perubahan `ADMIN_PASSWORD` diterapkan saat restart sebagai jalur pemulihan.
- Menambah lima gaya komunikasi yang otomatis mengikuti Role level dan dapat dikelola super admin melalui Settings → Communication; data divisi/profile legacy tetap dipertahankan namun tidak menentukan prompt.
- Memindahkan pasangan AI utama/cadangan Learning ke setiap buku, menambah readiness serta label AI di daftar, dan mengalihkan editor konfigurasi Learning global ke daftar buku. Buku legacy masih dapat memakai konfigurasi lama sampai dipublish ulang.
- Menambah persentase halaman PDF terbaca dan penjelasan bahwa 100% karakter hasil ekstraksi disimpan, tanpa mengklaim tabel/gambar ikut terbaca.
- Memperbaiki retrieval pertanyaan umum seperti fungsi/manfaat/apa yang dapat dipelajari dari buku memakai routing frasa dan sampel tersebar lokal tanpa panggilan AI tambahan.
- Menaikkan budget total inferensi menjadi 300 detik: primary maksimal 180 detik dan backup memakai sisa. Setelah 60 detik bot mengirim satu pesan proses acak dari phrase bank lokal tanpa token/history.
- Seluruh 369 tes lulus lokal; satu warning deprecation Starlette/httpx existing.
- Commit implementasi `6939beb` berhasil dideploy Railway pada deployment `870630a0-6de0-4e09-a1a2-6f1664443cee`. Dashboard menunjukkan **Deployment successful**; `/health` merespons HTTP 200 `{"status":"ok"}` dan `/admin` mengarahkan user anonim ke `/admin/login` dengan HTTP 200. Tidak ada data bisnis yang dimutasi saat verifikasi.

### 1.22 — 3 September 2026

- DK menyetujui perbaikan timeout dan retry artikel serta deployment bersama fitur v1.21. Commit implementasi `571b308` berhasil deployed pada Railway `95333567-a336-450f-9308-adf50c7f5949` dengan status `SUCCESS`.
- Mengubah batas outer request dan HTTP menjadi 120 detik hanya untuk `ms/artikel-web-generator`; default 30 detik dan pilihan model/konteks modul lain dipertahankan.
- Menambah pending input privat, command `/ulang` dan kata `ulang` khusus artikel, pemeriksaan konteks, reset, serta transaksi sukses tanpa duplikasi history. Tidak memulihkan input yang sudah hilang sebelum rilis.
- Menambah 11 tes timeout transport/concurrency/failover/cancellation, retry sukses/gagal, perubahan konteks, pencabutan akses, rollback dan migrasi pending. Ditambah satu regresi agar retrieval Learning memprioritaskan hasil relevan sebelum halaman tetangganya saat budget sempit.
- Seluruh 357 tes lulus lokal, dengan satu warning deprecation Starlette/httpx existing; `git diff --check` bersih. Provider/HTTP disimulasikan, tanpa panggilan AI berbayar, polling Telegram lokal, atau penambahan data uji produksi. Uji kualitas buku nyata dan respons AI produksi tetap belum dilakukan.
- Seluruh 40 tes fitur baru diulang dan lulus. Pemeriksaan produksi sesudah deploy memastikan health HTTP 200, admin anonim tetap diarahkan ke login, login berhasil, serta katalog/form Modul bersama, Learning, tambah buku, dan konfigurasi AI Learning HTTP 200. Halaman baru ini sebelumnya 404.
- Hash field nama, kode, deskripsi, pilihan primary/backup dan draft playbook ketiga modul company sebelum/sesudah deploy identik. Published artikel/Threads tetap v2 dan Google Map Care v1. Tidak mengubah key/model/playbook produksi; Learning masih draft tanpa AI/buku yang dipilih. DK perlu mengatur AI Learning dan mengunggah/review/publish buku pertama. Keberhasilan alur runtime Telegram diverifikasi dengan handler simulasi, bukan pesan user produksi.

### 1.21 — 3 September 2026

- DK menyetujui implementasi dan deploy tiga menu: Modul perusahaan, Modul bersama, Modul Learning. Deployment dilakukan bersama v1.22; lihat verifikasi rilis di atas.
- Menambah registry global, snapshot draft/published, dua mode konteks, kode global, primary/backup existing dan private history terpisah.
- Menambah buku PDF tersimpan privat, ekstraksi bounded, review, jadwal WIB, overlap validation dan boundary checks sebelum/sesudah generasi.
- `/learning` mengirim keterangan tanpa AI; custom instruction per buku menentukan pengalaman belajar; tidak memaksakan rujukan halaman atau label analisis.
- DK menyetujui indeks lokal tanpa biaya embedding beserta keterbatasan sinonim/parafrasa; bot meminta topik/bab bila sumber tidak ditemukan.
- Mencatat instruksi DK: peluang penghematan token harus dijelaskan metode/risikonya dan tetap diputuskan DK.

### 3 September 2026 — v1.20

- menambahkan deskripsi dari form module pada konfirmasi perpindahan, dipisahkan tepat satu baris kosong, untuk shortcut serta command `/module`;
- deskripsi kosong tidak ditampilkan; teks tidak diberi label/tanda kurung tambahan dan tidak dirender sebagai HTML/Markdown;
- tidak mengubah schema, data produksi, ACL, pemilihan AI, playbook, atau history;
- seluruh 317 tes lokal lulus, termasuk 13 kasus baru untuk tiga jalur pemilihan, deskripsi kosong/whitespace/teks/markup literal, pengaturan deskripsi tanpa publish ulang, preservasi data di luar session, dan konfirmasi General yang tetap sama. Satu warning deprecation Starlette/httpx lama tetap ada;
- DK menyetujui commit/deploy melalui GitHub → Railway. Commit implementasi `4c062fe` berhasil deployed pada `765bc3a8-c5e9-4627-9afa-b59b6c97269a`. Health HTTP 200, admin anonim tetap diarahkan ke login, login admin berhasil, dan company `amz`/`dkgroups`/`ms` tetap tersedia;
- form `ms/google-map-care` terverifikasi memiliki kode GMC dan deskripsi terisi. Hash pengaturan nama, kode, deskripsi, pasangan AI, serta isi draft playbook sebelum/sesudah deployment identik. Tidak mengubah data produksi atau mengirim inferensi AI; perilaku balasan diuji dengan handler Telegram dan balasan simulasi. Uji langsung `/gmc` di akun Telegram dapat dilakukan DK.

### 3 September 2026 — v1.19

- menambahkan field Kode singkat pada tambah/edit module dan menampilkannya pada katalog; ID canonical tetap immutable;
- menambahkan shortcut Telegram case-insensitive, resolver alias dan menu adaptif dengan ACL yang sama; session, history, playbook, dan pilihan AI tetap menggunakan ID canonical;
- migrasi schema additive, unique index, pemeriksaan benturan namespace, audit, kompatibilitas client lama, dan penghapusan alias;
- seluruh 304 tes lokal lulus, termasuk 28 tes baru untuk validasi kode, isolasi company, duplicate alias/ID, schema legacy, migrasi Company ID bersama alias, form/CSRF, routing Telegram, dan penolakan akses tanpa mutasi. Satu warning deprecation Starlette/httpx yang sudah ada tetap muncul;
- DK meminta `dk-corp-group` → `DKGroups`; ID disimpan normalized `dkgroups`, command `/company DKGroups` diterima. DK menyetujui commit/deploy, penetapan TG, dan maintenance startup ADR-068 dengan backup otomatis. Preflight produksi memastikan sumber ada, target belum dipakai, Threads generator tetap Published v2 dengan primary `gpt-5.1` dan backup `gpt-4o`;
- commit implementasi `71bad79` berhasil deployed melalui GitHub → Railway pada deployment `adf4a2a1-8f78-4f33-8684-37a480c926de`. Alias TG disimpan melalui form admin ber-CSRF untuk `ms/threads-generator`. Perbandingan sebelum/sesudah memastikan nama, deskripsi, pilihan AI, dan isi draft playbook tidak berubah; published tetap v2;
- migrasi produksi `dk-corp-group` → `dkgroups` berhasil pada deployment `d30df905-e39d-4b95-8bd4-c01159a6530f`. Log startup mencatat `Company ID migration completed with verified backup`, dan Activity mencatat target `dkgroups`, actor `dkadmin`, 3 September 2026 18:35 WIB. Verifikasi backup, hash seluruh row, serta FK dijalankan oleh transaksi ADR-068; tidak mengunduh database atau membuka SSH untuk migrasi ini;
- pemeriksaan HTTP setelah migrasi memastikan company `amz`, `dkgroups`, dan `ms` tersedia; nama DK Corp Group tetap; akun DK, membership di `dkgroups`/`ms`, instruction, knowledge, dan Threads generator HTTP 200. URL company lama 404 sesuai desain tanpa alias. Field TG, primary `gpt-5.1`, backup `gpt-4o`, dan Published v2 tetap ada;
- variable sementara `COMPANY_ID_MIGRATION` sudah dihapus melalui Railway setelah verifikasi. Backup privat tetap di direktori `backups/` di samping database produksi; tidak dimasukkan Git. Tes shortcut memakai handler/Update Telegram dengan balasan simulasi, bukan polling lokal, pesan Telegram, atau inferensi AI berbayar. Uji langsung dari akun Telegram tetap dapat dilakukan DK.

### 3 September 2026 — v1.18

- menambahkan menu `/?` yang sama dengan `/help` dan `/start`, dengan perintah umum dan pilihan sesuai akses user;
- menyembunyikan `/company` untuk nol/satu company aktif, menampilkan module langsung untuk satu company, dan menampilkan pilihan company hanya untuk multi-company;
- memakai ACL dan published status module yang sudah ada, tanpa inference AI, mutasi konteks, atau penulisan history; daftar panjang dipecah tanpa truncation;
- 276 tes lokal lulus, termasuk 21 kasus menu/routing: literal `/?` dengan/tanpa entity Telegram, `/help`/`/start`, single/multi/no company, default kosong, pembatasan akses/publish/AI, user tidak terdaftar, preservasi session/history, dan daftar 150 module tanpa truncation;
- DK menyetujui commit/deploy melalui GitHub → Railway. Commit implementasi `866e188` mendapat status Railway `success`; health 200, admin anonim diarahkan ke login, login admin berhasil, serta Companies, Threads generator di `ms`, dan membership DK di `ms` HTTP 200. ID `amz` dan `ms` tetap tersedia;
- verifikasi routing `/?` dilakukan otomatis dengan objek Update/handler Telegram dan transport balasan simulasi. Tidak menjalankan polling lokal, mengirim pesan Telegram, atau memanggil AI produksi. Uji menu langsung dari akun Telegram dilakukan oleh DK. Tidak mengubah data produksi atau native command list Telegram.

### 3 September 2026 — v1.17

- DK menyetujui migrasi produksi `amazing-malang` → `amz` dan `malang-strudel` → `ms`, cadangan database, serta SSH key sementara yang wajib dicabut setelah selesai;
- menambah jalur maintenance startup-only dengan backup, rollback atomik, verifikasi keseluruhan data, dan idempotensi berbasis audit; form admin tetap immutable;
- 255 tes otomatis lulus, termasuk 22 kasus migrasi/startup: seluruh data dan backup identik kecuali ID, resolusi akses/history setelah rename, repeat tanpa mutasi, konflik mapping, referensi yatim, schema baru, FK rusak, kegagalan backup/audit/trigger, dan larangan runtime mulai sebelum migrasi;
- commit implementasi `a02b215` deployed melalui GitHub → Railway. Migrasi produksi berhasil pada deployment `a67b5ff7-8182-4835-bf82-bccc0455e943`; kedua ID baru aktif, DK tetap pada company `ms` dan module `threads-generator`;
- perbandingan dengan backup membuktikan semua row tetap sama kecuali Company ID: 3 company, 5 user, 8 membership, 1 module, 1 module access, 16 pesan, 1 session, 1 dokumen knowledge/versi, 1 instruction/versi, dan 2 versi playbook tetap utuh. Credential dan 124 entri katalog juga identik. Audit bertambah satu event dari 30 menjadi 31. Integrity OK dan FK errors 0;
- cadangan preflight ada di volume produksi `/app/data/backups/preflight-company-id-20260903-31wa9qy3.db`; snapshot tepat sebelum transaksi ada di `/app/data/backups/before-company-id-deakoo5e.db`, keduanya terverifikasi. Backup tidak disalin ke Git atau dibagikan. Restore harus dilakukan offline dengan SQLite backup API;
- health/login/admin, membership DK di `ms`, instruction, knowledge, dan Threads generator HTTP 200. Playbook tetap Published v2, primary `gpt-5.1`, backup `gpt-4o`. Pengujian resolver membaca database produksi; tidak mengirim pesan Telegram atau inferensi AI berbayar;
- variable `COMPANY_ID_MIGRATION` sudah dihapus setelah verifikasi. SSH key sementara telah dicabut melalui Railway API; private/public key serta known-hosts sementara lokal dihapus. Nama/path file lama sengaja dipertahankan, sedangkan URL/command company memakai ID baru.

### 3 September 2026 — v1.16

- merevisi rancangan sebelum deploy sesuai koreksi DK: bukan seluruh jawaban polos, hanya isi naskah siap disalin (Threads/caption/draft pesan); penjelasan, sapaan, dan tips tetap terformat;
- mempertahankan renderer HTML dan fallback lama, menambah protokol blok siap salin serta normalisasi subset markup khusus di dalam blok;
- memisahkan naskah ke pesan polos sendiri, tanpa label/petunjuk. Pemilihan berbasis isi oleh model, tidak hardcode module. Penanda dihapus sebelum history/limit/split; data lama tidak diubah;
- 233 tes lokal lulus, termasuk jawaban campuran, beberapa alternatif, sapaan module tetap HTML, caption di General polos, penanda tidak lengkap, limit gabungan, literal fenced code, fallback HTML, serta preservasi teks/indentasi. Tes memakai fixture tanpa memanggil AI/Telegram produksi; warning deprecation Starlette/httpx existing tetap ada;
- DK menyetujui commit dan deployment perbaikan format melalui GitHub → Railway. Commit implementasi `f78da57` mendapat status Railway `success`; `/health` mengembalikan 200, admin anonim dialihkan ke login, login admin berhasil, serta halaman Companies, Modules, dan Threads generator dapat diakses. Pemeriksaan read-only menemukan Threads generator masih Published v2 dengan primary `gpt-5.1` dan backup `gpt-4o`; pilihan tersebut tidak diubah oleh rilis ini.
- Verifikasi produksi terbatas pada status deployment dan HTTP, tanpa mengirim pesan Telegram atau inferensi AI berbayar. Kepatuhan model terhadap penanda naskah siap salin perlu diuji melalui respons baru; pesan Telegram lama tidak berubah. Perubahan Company ID tidak termasuk rilis ini dan tetap ditunda. Tidak ada perubahan model/credential/timeout maupun mutasi data bisnis produksi.

### 3 September 2026 — v1.15

- menerapkan ADR-057 pada form tambah user, tambah/edit membership, daftar membership, dan import Excel;
- communication profile otomatis mengikuti Role level membership aktif, tidak lagi memakai override legacy/global atau teks jabatan; divisi tidak disisipkan ke prompt dan `/whoami`;
- mempertahankan nilai divisi/profile lama saat edit dan restart, tanpa migrasi schema/backfill/reset. Role baru langsung memengaruhi profile efektif; akses/default/whitelist tetap terpisah;
- import empat kolom dengan kompatibilitas file lima kolom lama. Telegram ID existing tetap dilewati tanpa modifikasi. Field profile request browser tidak bisa mengalahkan role;
- 198 tes lokal lulus, termasuk tiga role, role legacy tidak dikenal, manipulasi field lama, preservasi nilai mentah, invalid role/CSRF, beberapa company, serta import empat/lima kolom `.xls`/`.xlsx`. Warning deprecation Starlette/httpx existing tetap ada;
- commit implementasi `4d492ea` dideploy dengan status Railway success, lalu form produksi dan health/auth terverifikasi melalui HTTP terautentikasi. Tidak ada mutasi data bisnis pada smoke check. Uji runtime bot tetap memakai fixture lokal, bukan pesan akun Telegram produksi.

### 2 September 2026 — v1.14

- Tambah AI hanya Provider + API Key, nama/ID otomatis dan aktif saat disimpan; perubahan provider existing ditolak oleh form/server;
- Tes & ambil model membaca katalog empat provider, termasuk pagination berbatas, tanpa inferensi berbayar/data perusahaan; snapshot atomik dan invalidasi credential revision;
- katalog model dan pilihan model utama/cadangan per Module, kompatibilitas legacy, validasi pasangan dan penolakan model non-chat/unsupported;
- 183 tes otomatis lulus, termasuk HTTP mock provider/pagination, respons gagal/malformed, batas katalog, timeout/cancellation, preservasi pilihan, rotasi key, stale test, penolakan secret yang dipantulkan sebagai cursor URL, serta alur form sampai runtime. Migrasi profile/module lama dan preservasi ciphertext/playbook diuji berulang. Satu warning deprecation Starlette/httpx yang sudah ada tetap muncul;
- deployment implementasi `a383167` berstatus `SUCCESS` pada Railway (`48883395-d1c1-4941-b59f-3e47a2c4bc5b`), health/auth/form Provider + API Key/readiness encryption produksi terverifikasi. Pesan Module tanpa katalog diperjelas untuk mengarahkan admin ke tes provider, dengan regresi lokal;
- tidak mereset database atau mengganti master encryption key. Registry AI produksi masih kosong saat verifikasi; tes memakai API key provider nyata dan pilihan model produksi belum dilakukan.

### 2 September 2026 — v1.13.1

- Aktivasi master enkripsi Settings AI di Railway production setelah otorisasi DK, tanpa menimpa key lama atau mengubah variable existing;
- deployment aktivasi berhasil, HTTP health/admin serta hilangnya warning master terverifikasi; form menyediakan GPT, Claude, DeepSeek dan Gemini;
- 132 tes otomatis lulus ulang; tidak ada reset data, credential uji produksi, atau panggilan API berbayar;
- mencatat batas verifikasi dan kebutuhan backup master terpisah yang belum dilakukan.

### 2 September 2026 — v1.13

- Settings AI untuk API key terenkripsi, empat provider, endpoint otomatis, tes koneksi dan status aktif;
- migrasi additive mempertahankan credential environment, seluruh data, dan dropdown AI utama/cadangan;
- master terpisah, binding profile/provider, safe errors/audit, batas form/tes dan invalidasi hasil usang;
- 132 tes otomatis lulus, termasuk enkripsi, keamanan form, migrasi, adapter HTTP simulasi, failover lintas provider, concurrency, cancellation dan hasil tes usang;
- DK menyetujui finalisasi dan deployment melalui GitHub → Railway; 132 tes lokal lulus ulang, tanpa reset data. Status deployment diperiksa melalui commit status Railway dan HTTP health/admin. Master produksi memerlukan akses Railway terautentikasi sebelum penyimpanan credential baru dapat dinyatakan siap. Uji API berbayar belum dilakukan.

### 1.12 — 2 September 2026

- mengganti istilah UI credential profile menjadi AI terdaftar/Tambah AI;
- menambah pilihan AI utama dan cadangan opsional pada form buat/edit dan daftar Module;
- menambah migrasi nullable backup profile; seluruh pilihan utama, playbook, akses, dan history lama dipertahankan;
- menerapkan failover operasional satu kali, timeout per percobaan 30 detik, tanpa retry tersembunyi dari SDK Module atau fallback global;
- menambah audit pasangan AI serta log kegagalan/failover tanpa body error atau secret;
- menguji migrasi berulang, validasi/CSRF form, kegagalan provider, konfigurasi kosong/nonaktif, pembatalan, isolasi konteks, dan history;
- rilis v1.12 melalui alur GitHub → Railway yang sudah ada; 97 pengujian otomatis lulus dengan provider/HTTP simulasi, tanpa panggilan API berbayar. Deployment memakai migrasi additive, tanpa reset data produksi. Status deployment diverifikasi terpisah melalui commit status Railway dan health endpoint.

### 1.11 — 2 September 2026

- menambahkan tombol dan halaman import Excel `.xls`/`.xlsx` pada Users & Access dengan lima kolom template sederhana;
- menambahkan parser berbatas ukuran/baris, validasi perusahaan/ID, dan laporan hasil atau error per baris;
- menerapkan skip total ID lama, transaksi atomik, dukungan multi-company untuk user baru, dan audit metadata;
- menambahkan pengujian format Excel, autentikasi/CSRF, upload, presisi ID, duplikat, rollback, import ulang, serta concurrent import;
- mempertahankan rencana penyederhanaan ADR-057 sebagai pekerjaan tertunda; tidak mengubah form user/membership lama atau schema database.

### 1.10 — 2 September 2026

- mencatat persetujuan DK untuk opsi 1 penyederhanaan user/membership sebagai pekerjaan tertunda saat penyempurnaan sistem;
- menetapkan rencana pemetaan Role level ke Communication profile, penghilangan input divisi, pelestarian data lama, dan penyelarasan import;
- hanya memperbarui dokumentasi; tidak menerapkan perubahan aplikasi, database, template, atau deployment.

### 1.9 — 2 September 2026

- menambahkan credential profile non-secret dengan provider, model, base URL, named environment variable, status, dan audit event;
- mewajibkan setiap Module memilih credential profile aktif sebelum dibuat, diaktifkan, atau dipublikasikan;
- mempertahankan mode General pada credential global dan merutekan chat Module melalui profile pilihannya;
- menerapkan fail-closed ketika profile nonaktif atau named secret belum tersedia, tanpa fallback ke key global;
- menambahkan pengelolaan credential profile pada Admin Modules tanpa menampilkan atau menyimpan nilai API key;
- menambahkan migrasi SQLite, pengujian resolver, alur admin, dan dokumentasi Railway Variables;
- memperbarui status implementasi, data model, deployment, security boundary, batas MVP, roadmap, dan keputusan arsitektur.

### 1.8 — 2 September 2026

- mengganti link Preview terpisah dengan tombol `Review untuk publish` pada editor Instruction, Knowledge, dan Module Playbook;
- membuat tindakan Review menyimpan isi editor terbaru sebelum redirect ke halaman Preview;
- mempertahankan `Simpan draft` untuk pekerjaan belum selesai dan Publish sebagai tindakan eksplisit terpisah;
- menambahkan pengujian bahwa isi terbaru tersimpan, tampil di Preview, dan belum live sebelum Publish;
- memperbarui dokumentasi workflow serta keputusan arsitektur.

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
