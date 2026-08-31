# Arsitektur dan Konsep DK Corp Telegram AI

## Kontrol dokumen

| Atribut | Nilai |
|---|---|
| Status | Living document |
| Versi | 0.3 |
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
5. Perubahan konfigurasi dilakukan melalui file yang dapat diaudit di Git.
6. MVP memakai komponen minimum yang cukup untuk satu instance.

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
name, role, division, communication_profile
    ↓
Authorization Policy
knowledge dan fungsi yang boleh diakses
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
| Authentication | Sudah | Whitelist Telegram ID |
| User Context | Sudah | Name, role, division, communication profile |
| Communication Profile | Sudah | Config terpusat dengan override per user |
| Custom Instruction | Sudah | Field per user |
| Knowledge Loader | Sudah | Semua `.md` di folder `knowledge/` |
| Chat History | Sudah | SQLite per user |
| Provider Abstraction | Sudah | OpenAI dan DeepSeek compatible API |
| Telegram Response Renderer | Sudah | Safe HTML, split, link preview off, dan fallback plain text |
| Conversation Delivery Policy | Belum | Akan mengatur panjang, ritme, dan progressive disclosure |
| Authorization per knowledge | Belum | Semua knowledge masih dapat dipakai semua user aktif |
| Response Validator | Sebagian | Batas panjang, split, escape HTML, dan fallback; belum ada policy classifier |
| Admin Panel | Belum | User dikelola melalui JSON dan Git |
| Retrieval/RAG | Belum | Seluruh knowledge dimuat sampai batas karakter |

## 5. Pemisahan konsep user

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

## 6. Communication Profile

### 6.1 Executive

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

### 6.2 Manager

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

### 6.3 Staff

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

### 6.4 Default

Dipakai ketika profile tidak dikenali. Jawaban bersifat seimbang, praktis, dan tidak mengasumsikan senioritas user.

## 7. Resolusi profile dan prioritas instruksi

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

## 8. Telegram Response Renderer

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

### 8.1 Format input yang didukung

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

### 8.2 Security boundary

- Semua `<`, `>`, dan `&` dari output model di-escape.
- Model tidak boleh mengirim HTML mentah.
- Hanya renderer yang boleh menghasilkan tag HTML Telegram.
- Link Markdown hanya dirender bila memakai `http://` atau `https://`.
- Pesan dipecah dari source dengan target 3.500 karakter agar berada di bawah batas 4.096 karakter Telegram setelah entities parsing.
- Jika Telegram menolak HTML, sistem mencatat warning tanpa isi pesan dan mengirim chunk yang sama sebagai plain text.
- Link preview dinonaktifkan untuk respons AI.

### 8.3 Batas tanggung jawab

Renderer tidak mengatur panjang ideal, jumlah pilihan, tone, satu pesan satu tujuan, atau progressive disclosure. Semua itu adalah tanggung jawab Conversation Delivery Policy yang akan dibangun terpisah.

## 9. Data dan penyimpanan

### 9.1 Konfigurasi Git

| Lokasi | Isi |
|---|---|
| `config/users.json` | Whitelist dan konteks user |
| `config/role_profiles.json` | Aturan communication profile |
| `knowledge/*.md` | Knowledge perusahaan |
| `docs/architecture.md` | Arsitektur dan keputusan konsep |

### 9.2 SQLite

SQLite menyimpan:

- salinan user hasil sinkronisasi konfigurasi;
- pesan user dan assistant;
- timestamp pesan dan pembaruan user.

Railway Volume dipasang pada `/app/data` agar database bertahan saat redeploy.

## 10. Deployment

```text
Perubahan lokal
    ↓ git commit + push
GitHub private repository
    ↓ automatic deployment
Railway service
    ├── Docker container
    ├── environment variables
    └── volume /app/data
    ↓
Telegram long polling
```

Hanya satu replica boleh berjalan selama database memakai SQLite dan bot memakai long polling.

## 11. Keamanan dan batas akses

Sudah diterapkan:

- whitelist Telegram ID;
- secret disimpan di environment variables;
- `.env` dan database runtime tidak masuk Git;
- HTTP client log tidak menampilkan URL Telegram pada level normal;
- output model di-escape dan dirender melalui safe Telegram HTML;
- knowledge diperlakukan sebagai referensi, bukan system instruction.

Belum diterapkan:

- knowledge access per division atau clearance;
- audit log administratif;
- enkripsi field aplikasi pada database;
- klasifikasi data sensitif pada jawaban;
- admin approval untuk perubahan akses.

Communication Profile bukan mekanisme keamanan. Profile hanya mengubah cara jawaban disampaikan, bukan menentukan informasi yang boleh diakses.

## 12. Batas MVP

- text-only;
- satu provider aktif untuk seluruh bot;
- satu instance Railway;
- seluruh knowledge dimuat sampai `KNOWLEDGE_MAX_CHARS`;
- user dan profile dikelola lewat JSON;
- seluruh user aktif masih memakai kumpulan knowledge yang sama.

## 13. Roadmap

### Fase 1 — Context-aware MVP

- whitelist dan user context;
- role-based communication profile;
- safe Telegram HTML renderer;
- Markdown knowledge;
- chat history;
- provider abstraction.

### Fase 2 — Organizational Access

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

## 14. Keputusan arsitektur

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

## 15. Changelog dokumen

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
