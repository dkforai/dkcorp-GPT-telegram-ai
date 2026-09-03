# Internal Telegram AI Bot

MVP bot Telegram internal multi-company dengan whitelist user, membership per perusahaan, communication profile otomatis dari Role level, custom instruction, knowledge dari file Markdown, history sederhana di SQLite, dan AI provider yang bisa diganti antara OpenAI dan DeepSeek.

Dokumen arsitektur dan konsep aplikasi dipelihara di `docs/architecture.md`. Dokumen Markdown tersebut adalah source of truth selama pengembangan dan akan dibuat menjadi PDF setelah konsep stabil.

## Cara kerja

```text
Telegram → whitelist Telegram ID → company membership
         → active company + communication profile
         → company instruction + company knowledge
         → optional active module + published module playbook
         → company/module-scoped history SQLite → AI provider
         → renderer HTML biasa / teks polos khusus naskah siap salin → Telegram
```

Bot memakai **long polling** dan tidak memerlukan webhook. Admin panel memakai HTTP server dalam container yang sama. Domain Railway hanya diperlukan untuk membuka halaman admin.

Pemrosesan update memakai controlled concurrency. User berbeda dapat diproses paralel sampai batas `MAX_CONCURRENT_UPDATES`, sedangkan pesan dan command dari Telegram ID yang sama memakai satu lock agar urutan company context, history, dan jawaban tidak tertukar. Pending update tidak dibuang saat startup.

## Fitur MVP

- Satu Telegram bot untuk seluruh tim
- Whitelist berdasarkan Telegram ID
- Master perusahaan dan membership user per perusahaan
- Perusahaan aktif dapat dilihat atau diganti melalui `/company`
- Jabatan, Role level, dan custom instruction per membership; profile otomatis mengikuti role
- Communication profile `executive`, `manager`, `staff`, atau `default`
- Company profile, instruction, dan knowledge dari path yang dikonfigurasi per perusahaan
- History chat dipisahkan per user, perusahaan, dan module aktif di SQLite
- Pending Telegram update dipertahankan saat bot restart
- Controlled concurrency dengan urutan pesan per user tetap dijaga
- Provider abstraction OpenAI/DeepSeek melalui API yang kompatibel dengan OpenAI
- Jawaban biasa tetap terformat; naskah siap copy-paste seperti Threads/caption dikirim sebagai pesan polos tersendiri
- Hanya blok naskah siap salin yang dibersihkan dari style; penjelasan, pertanyaan, dan tips tetap memakai safe HTML
- Admin web dengan login, dashboard, company registry, dan user access directory
- Company Management untuk menambah, mengganti nama, mengaktifkan, dan menonaktifkan tenant
- Users & Access Management untuk whitelist dan membership per company
- Import user baru dari Excel `.xls`/`.xlsx`, tanpa menimpa Telegram ID yang sudah terdaftar
- Company Instruction Management dengan draft, preview, publish, dan riwayat versi
- Knowledge Management per company dengan draft, preview, publish, status, dan riwayat versi
- Upload PDF, DOCX, TXT, atau Markdown menjadi draft Knowledge yang dapat diperiksa sebelum publish
- Module Management per company dengan draft, preview, publish, status, dan riwayat versi playbook
- Settings AI dengan API key terenkripsi, empat provider, tes koneksi, serta pilihan AI utama/cadangan per Module; legacy environment tetap didukung
- Akses module bersifat default-deny dan diberikan per membership
- Module aktif dapat dilihat atau diganti melalui `/module`; mode General tetap tersedia
- History chat dipisahkan per user, perusahaan, dan module aktif
- Menu adaptif `/?` atau `/help`, serta perintah `/start`, `/company`, `/module`, `/whoami`, dan `/reset`
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

Company ID dibuat otomatis oleh server dari nama company. Semua path harus relatif terhadap project root dan tidak boleh memakai `..`.

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
/? atau /help               menu perintah dan pilihan sesuai akses user
/start                      sapaan dan menu yang sama
/company                    pilihan perusahaan, hanya muncul untuk >1 company aktif
/company amz                memilih perusahaan aktif (contoh ID Amazing Malang)
/company DKGroups           memilih DK Corp Group (ID tersimpan dkgroups)
/module                     daftar module yang boleh diakses pada perusahaan aktif
/module marketing           memilih module dan playbook aktif
/TG atau /tg                memilih module berkode TG di company aktif
/module general             kembali ke konteks perusahaan tanpa module khusus
/whoami                     melihat membership dan profile aktif
/reset                      menghapus history company/module yang sedang aktif saja
```

Menu menghitung membership dan company yang sama-sama aktif. Jika hanya satu company, langsung tampil seluruh module yang boleh dipakai pada company itu beserta General, tanpa `/company`. Jika lebih dari satu, tampil pilihan company dan module perusahaan yang sedang aktif; bila company belum dipilih, user memilih company terlebih dahulu. Command umum tetap ditampilkan sesuai akses. `/company` tanpa argumen pada user satu company juga langsung menampilkan module, tanpa mengganti context. Akses company tidak otomatis membuka seluruh module: module harus aktif, published, memiliki AI aktif, dan diberikan kepada membership. User tanpa membership mendapat arahan ke admin; user belum terdaftar tidak melihat daftar company/module.

`/?` ditangani sebagai teks khusus sebelum chat AI karena tanda `?` bukan nama command Telegram yang valid. `/help` tetap tersedia sebagai command standar. Menu tidak memanggil AI, menulis history, atau mengubah company/module aktif. Daftar panjang dikirim dalam beberapa pesan agar tidak terpotong.

Kode singkat diatur melalui **Modules → Kelola → Kode singkat → Simpan pengaturan**, tanpa publish ulang playbook. Opsional, 2–3 huruf/angka ASCII, diawali huruf, tidak membedakan huruf besar/kecil, dan unik per company (termasuk module nonaktif). Tidak boleh bentrok dengan ID module lain atau command sistem. `/?` menampilkan `/TG` bila alias TG diisi. `/module TG` dan `/module threads-generator` tetap menuju ID internal yang sama, sehingga akses dan history tidak berubah. Alias hanya berlaku pada company aktif dan tetap membutuhkan whitelist, membership, module/access/AI aktif serta playbook published. Kosongkan field untuk menghapus alias. Pesan biasa `TG` tanpa slash tidak mengganti module.

## Communication profile

Aturan terpusat berada di `config/role_profiles.json`:

| Profile | Bentuk jawaban |
|---|---|
| `executive` | Strategis, opsi, trade-off, risiko, dan keputusan |
| `manager` | Taktis, action plan, resource, timeline, dan KPI |
| `staff` | Operasional, langkah, checklist, contoh, dan standar selesai |
| `default` | Seimbang ketika profile tidak dikenali |

Profile selalu mengikuti `role_level` membership aktif: `gm` → `executive`, `manager` → `manager`, `staff` → `staff`. Role lama yang kosong/tidak dikenal memakai `default`, bukan menebak dari jabatan. Override `communication_profile` lama tidak digunakan bot. Nilai divisi/profile lama tetap disimpan untuk kompatibilitas, tetapi divisi tidak lagi dimasukkan ke prompt atau `/whoami`. Custom instruction membership hanya berlaku pada perusahaan tersebut.

## Company instruction dan knowledge

Setiap company dapat menunjuk tiga sumber berbeda:

- `profile_file` untuk identitas dan konteks perusahaan;
- `instruction_file` untuk aturan AI perusahaan;
- `knowledge_dir` untuk fakta, SOP, produk, dan referensi.

Company Instruction dikelola melalui admin dengan alur **Draft → Preview → Publish**. Draft tidak memengaruhi bot. Setelah publish, bot membaca versi SQLite yang aktif pada pesan berikutnya. File `instruction_file` tetap menjadi fallback transisi selama company belum mempunyai versi database yang dipublikasikan.

Knowledge dikelola melalui admin dengan alur **Draft → Preview → Publish** per dokumen. Bot hanya membaca versi published dari dokumen aktif milik company aktif. Sebelum publish knowledge pertama, folder `knowledge_dir` masih dibaca sebagai fallback transisi. Setelah publish pertama, database menjadi sumber aktif dan draft tidak masuk ke prompt.

Pada saat membuat Knowledge Document, admin dapat menulis teks langsung atau upload PDF, Word `.docx`, TXT, dan Markdown maksimal 10 MB. File diekstrak menjadi teks draft; file aslinya tidak disimpan di SQLite. PDF harus mempunyai text layer. PDF hasil scan memerlukan OCR dan Word binary lama `.doc` harus disimpan ulang menjadi `.docx`.

Profile masih berbasis file dan dibaca ulang pada setiap pertanyaan. Hanya content dari company aktif yang dimasukkan ke prompt. History juga difilter menggunakan company ID yang sama.

Module dikelola melalui admin dengan alur **Draft → Preview → Publish** untuk playbook. Module baru tidak dapat dipilih bot sebelum playbook dipublikasikan dan aksesnya dicentang pada membership. Memilih `/company` mereset module ke `General`; mengganti module tidak menghapus history lama, tetapi memakai ruang history yang terpisah. Knowledge khusus module belum tersedia, sehingga module aktif masih memakai Knowledge company yang sama ditambah playbook module.

Setiap Module memilih **AI utama** dan **AI cadangan** dari **AI terdaftar**. AI utama wajib, cadangan opsional dan harus berbeda. Daftarkan AI melalui **Settings → AI → Tambah AI**, dengan nama, provider, ID model, dan API key. OpenAI/GPT, Anthropic/Claude, DeepSeek, dan Gemini didukung. Key baru disimpan terenkripsi di SQLite, bukan plaintext; credential environment lama tetap didukung. Pilihan AI langsung berlaku pada pesan berikutnya tanpa publish ulang playbook. Module lama mempertahankan pilihan AI-nya.

AI utama dicoba terlebih dahulu. Cadangan dicoba sekali saat terjadi masalah koneksi, timeout, HTTP 408/429 atau 5xx, menggunakan konteks dan history yang sama. Tiap percobaan maksimal 30 detik, tanpa retry SDK berulang. Key salah/kosong, AI nonaktif, error request/izin akses, atau refusal tidak memicu cadangan. History ditulis sekali setelah jawaban sukses. Mode `/module general` tetap memakai `AI_PROVIDER`, `AI_API_KEY`, `AI_MODEL`, dan `AI_BASE_URL` global; tidak digunakan sebagai cadangan kegagalan request Module.

Memilih cadangan mengizinkan konteks dikirim ke provider tersebut. Kedua provider dapat mengenakan biaya jika utama timeout setelah request diproses. Perpindahan tercatat di log server tanpa key atau isi percakapan. Dua profile berbeda dengan account/provider sama bisa tetap terkena limit atau gangguan yang sama.

### Settings AI dan kunci enkripsi

Administrator server membuat master key sekali setelah memasang dependencies:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Simpan hasilnya sebagai `AI_CREDENTIAL_ENCRYPTION_KEY` pada Railway Variables atau `.env` lokal, lalu restart service. Jangan kirim ke chat, commit ke Git, gunakan `ADMIN_SESSION_SECRET` sebagai pengganti, atau taruh di database yang sama. Backup kunci di secret manager terpisah dari backup SQLite. Kehilangan kunci membuat API key tersimpan tidak terbaca. Rotasi master key belum otomatis: jangan mengganti variable tanpa rencana migrasi ciphertext dan backup.

1. Buka **Settings → AI → Tambah AI**.
2. Isi **Provider** dan **API Key** saja. Nama/ID koneksi dan endpoint resmi ditentukan sistem; provider otomatis aktif setelah disimpan. Tidak ada input nama AI atau model.
3. Klik **Tes & ambil model** pada provider aktif. Sistem membaca seluruh halaman katalog model dengan key tersebut, tanpa menghasilkan teks, retry/failover, atau mengirim data perusahaan. Buka daftar model di Settings untuk melihat hasilnya. Tombol yang sama memperbarui katalog saat diperlukan.
4. Pada Module, pilih pasangan **provider + model** sebagai AI utama dan cadangan opsional. Semua model hasil provider aktif ditampilkan; model gambar/audio/embedding, Responses-only atau keluarga yang belum didukung adapter chat bot tampil nonaktif dengan alasan. Model baru memerlukan katalog hasil tes, bukan ID yang diketik bebas.
5. Tes katalog bukan tes jawaban, saldo, atau jaminan akses chat setiap model. Uji percakapan Telegram setelah memilih model untuk memeriksa akses inferensi akun. User Telegram tetap memilih Module sesuai aksesnya; pilihan provider/model diatur admin di form Module.

Form Edit tidak menampilkan key. Key kosong mempertahankan key dan katalog lama; key baru menggantinya dan menghapus katalog sehingga perlu tes ulang. Provider koneksi existing tidak dapat diubah melalui form; daftarkan koneksi baru untuk provider berbeda. Model pilihan tiap Module disimpan terpisah dari key, sehingga satu key dapat digunakan beberapa Module dengan model berbeda. Cadangan boleh memakai provider/key sama tetapi model harus berbeda; key yang sama bisa mengalami gangguan/limit yang sama.

Migrasi additive mempertahankan model/key environment maupun encrypted profile lama dan pilihan Module yang sudah berjalan. Opsi bertanda **Konfigurasi lama** tetap menggunakan model lama tanpa auto-discovery wajib. Penggantian key environment dari luar admin perlu tes ulang. Custom endpoint legacy tetap untuk runtime lama; discovery hanya menerima endpoint resmi. Mengisi key baru di form legacy beralih ke encrypted storage dan endpoint resmi.

Hasil tes berupa status aman dan waktu UTC, bukan error mentah. Timeout total 30 detik, maksimum 20 halaman, 5.000 model, 2 MiB respons per halaman; hasil parsial atau malformed tidak mengganti katalog sebelumnya. Request yang selesai setelah key/status berubah ditolak. Maksimal tiga tes per menit total/per koneksi dan dua bersamaan pada server satu-process. Kegagalan refresh mempertahankan katalog terakhir; refresh sukses yang menghapus model memblokir penggunaan pilihan itu sampai admin memilih ulang. Tidak ada perpindahan otomatis ke model lain atau General saat katalog/key tidak siap.

Discovery menggunakan [OpenAI Models](https://developers.openai.com/api/reference/python/resources/models/methods/list), [Claude Models](https://platform.claude.com/docs/en/api/models/list), [DeepSeek Models](https://api-docs.deepseek.com/api/list-models/) dan [Gemini Models](https://ai.google.dev/api/models). Metadata endpoint tidak seragam: selain kapabilitas Gemini, pemilahan keluarga model adalah kebijakan kompatibilitas adapter aplikasi, bukan jaminan provider. Keluarga baru mungkin perlu pembaruan adapter/policy.

OpenAI/DeepSeek/Gemini memakai Chat Completions-compatible API; Claude memakai native Messages API dengan system terpisah dan batas output chat 4096 token. Dukungan khusus chat teks, tidak menjamin fitur multimodal/tools/reasoning seluruh model. Provider baru memerlukan adapter/validasi tersendiri. General tetap menggunakan konfigurasi global OpenAI/DeepSeek.

Master key tidak valid memblokir encrypted save/decrypt tanpa fallback diam-diam. Profile environment lama tetap berjalan. Data tidak direset. Pengujian otomatis memakai database sementara dan HTTP simulasi, bukan API berbayar.

Pada ketiga editor tersebut, admin dapat memilih **Simpan draft** untuk pekerjaan yang belum selesai atau **Review untuk publish**. Tombol Review menyimpan isi terbaru lalu langsung membuka Preview, sehingga konten yang siap cukup melewati dua tindakan: Review lalu Publish. Publish tetap menjadi tindakan terpisah agar perubahan tidak langsung masuk ke bot secara tidak sengaja.

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
- `/admin/users/import` untuk upload Excel dan import user baru tanpa mengubah user lama;
- `/admin/instructions` untuk status instruction semua company;
- `/admin/instructions/<company-id>` untuk draft, preview, publish, dan riwayat versi;
- `/admin/knowledge` untuk status knowledge semua company;
- `/admin/knowledge/<company-id>` untuk dokumen, draft, publish, status, dan riwayat versi;
- `/admin/modules` untuk registry module semua company;
- `/admin/settings/ai` untuk registry AI, status, dan tes koneksi;
- `/admin/runtime-profiles/new` untuk mendaftarkan AI dengan API key terenkripsi;
- `/admin/modules/new` untuk membuat module dengan Module ID otomatis;
- `/admin/modules/<company-id>/<module-id>` untuk identitas, playbook, preview, publish, status, dan riwayat versi;
- `/admin/activity` untuk audit administratif read-only termasuk perubahan module dan akses;
- `/health` untuk health check Railway.

Company, user whitelist, membership, Company Instruction, Knowledge, Module, dan akses Module sudah dapat dikelola melalui admin. Company ID, document key, dan Module ID dibuat otomatis oleh server lalu dikunci. Telegram ID berasal dari Telegram dan dikunci setelah user dibuat. Versi instruction, knowledge, atau playbook lama dapat dipulihkan ke draft, lalu harus dipreview dan dipublikasikan kembali.

### Import user dari Excel

Di **Users & Access → Import user**, upload file `.xls` atau `.xlsx` dengan empat kolom berikut pada baris pertama. Urutan kolom boleh berbeda; nama kolom jangan diganti.

| Nama | Telegram ID | Perusahaan | Jabatan |
|---|---|---|---|

- Gunakan tab `Data_User`; file dengan satu tab boleh memakai nama tab lain. Tab contoh/petunjuk tidak dibaca jika `Data_User` tersedia.
- Maksimal 5 MB dan 500 baris data (baris 2–501). Isi nilai biasa, bukan formula atau error Excel. `.xlsx` dengan formula ditolak; `.xls` hanya dibaca nilainya yang tersimpan, tidak menjalankan formula/macro.
- Telegram ID adalah ID numerik Telegram, bukan nomor telepon atau `@username`. Gunakan format teks terutama untuk ID lebih dari 15 digit agar Excel tidak membulatkan digitnya.
- Isi nama perusahaan yang sudah aktif di Companies, dengan ejaan sama; huruf besar/kecil dan spasi berlebih diabaikan. Company ID juga diterima. Nama ambigu atau perusahaan tidak ditemukan/nonaktif menghasilkan error, bukan membuat company otomatis.
- Admin memilih Role level dan aktivasi whitelist di halaman import untuk seluruh user baru dalam batch. Profile otomatis mengikuti role. Default `staff` dan whitelist nonaktif. Pengaturan per user dapat disesuaikan melalui Kelola setelah import. Jabatan tidak otomatis menentukan role/profile.
- Telegram ID yang sudah ada, termasuk user nonaktif, **dilewati seluruhnya**. Nama, status, membership, default company, custom instruction, session, history, dan akses module lama tidak berubah; perusahaan baru pada baris tersebut tidak ditambahkan.
- Untuk ID baru di beberapa perusahaan, ulangi ID dan nama pada beberapa baris. Satu user dibuat dengan beberapa membership aktif; perusahaan pada baris pertama menjadi default. Duplikat identik dilewati; data yang bertentangan ditolak.
- Klik **Import user baru** untuk menyimpan langsung. Jika ada data user baru tidak valid, seluruh batch dibatalkan tanpa simpan parsial. Hasil menampilkan jumlah user/membership baru dan nomor baris yang dilewati.
- Akses module tidak diberikan otomatis. Upload tidak disimpan permanen dan isi file tidak dimasukkan audit; audit mencatat metadata/checksum dan jumlah hasil import.

Penyederhanaan ADR-057 diterapkan pada form tambah user, tambah/edit membership, import, dan runtime bot. File lama dengan kolom kelima **Divisi** tetap diterima; nilainya boleh kosong dan disimpan sebagai data legacy, bukan konteks AI. User yang sudah ada tetap dilewati seluruhnya. Form edit tidak menghapus atau menimpa kolom divisi/profile lama meskipun Role level berubah; profile efektif dihitung saat runtime.

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

### Migrasi Company ID oleh operator

Company ID tidak diedit lewat form. Untuk migrasi yang sudah disetujui, operator dapat mengatur `COMPANY_ID_MIGRATION` berupa JSON mapping, misalnya `{"company-lama":"company-baru"}`, lalu deploy/restart. Ini hanya aman ketika runtime lama sudah berhenti dan tidak ada writer lain; volume tunggal Railway memastikan deployment lama tidak berjalan bersamaan dengan deployment pengganti. Jangan menjalankannya pada bot/admin lokal yang masih menulis database yang sama.

Startup membuat backup SQLite terverifikasi di folder `backups/` di sebelah database, lalu memindahkan ID dan seluruh referensi dalam satu transaksi sebelum bot/admin aktif. Isi history, dokumen, versi, path file, membership, pilihan model/key, dan audit lama dipertahankan. Konflik atau pemeriksaan gagal menghentikan startup, bukan melanjutkan migrasi sebagian. Eksekusi ulang hanya dilewati bila audit migrasi yang sama terverifikasi. Hapus variable setelah sukses. ID lama tidak menjadi alias. Backup privat jangan di-commit; restore harus dilakukan dalam maintenance dengan SQLite backup API dan seluruh writer berhenti.

### Update data di Railway

Company, user, membership, Company Instruction, Knowledge, Module, serta aksesnya dikelola dari dashboard admin. Database, versi instruction/knowledge/playbook, dan history tetap aman selama volume `/app/data` terpasang.

## Environment variables

| Variable | Fungsi | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Token BotFather | wajib |
| `AI_PROVIDER` | `openai` atau `deepseek` | `openai` |
| `AI_API_KEY` | API key provider aktif | wajib |
| `AI_MODEL` | Nama model provider | sesuai provider |
| `AI_BASE_URL` | Override endpoint provider | sesuai provider |
| `AI_KEY_<NAMA>` | API key bernama yang dirujuk credential profile Module | sesuai profile |
| `AI_CREDENTIAL_ENCRYPTION_KEY` | Master key Fernet Settings AI, simpan terpisah dari backup SQLite | wajib untuk key tersimpan; tidak diperlukan profile environment lama |
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

Jawaban biasa tetap memakai subset Markdown menjadi safe Telegram HTML (bold, italic, command, quote, dan lainnya). Jika Telegram menolak HTML, chunk tersebut memakai fallback teks. Format polos tidak dipaksakan pada semua jawaban atau semua percakapan dalam module tertentu.

Khusus naskah siap disalin/diposting seperti Threads, caption Instagram, copy iklan, atau draft pesan, AI diminta menandai ISINYA dengan `[[COPY_TEXT]]` dan `[[/COPY_TEXT]]` pada baris tersendiri. Penjelasan/judul pilihan/tips ada di luar blok. `app/telegram_renderer.py` memisahkan blok ini sebagai pesan polos (`parse_mode=None`) dan menghilangkan penandanya; setiap alternatif menjadi pesan tersendiri. Pesan biasa di sekitarnya tetap terformat. Sapaan dan pemilihan akun di Threads generator tetap normal; General juga bisa menghasilkan caption siap salin.

Hanya isi blok siap salin yang dibersihkan dari subset markup lama. Paragraf, hashtag, emoji, dan URL tetap dipertahankan. Link berlabel menjadi `label (URL)`. Normalisasi dilakukan sebelum limit gabungan/split/history agar penanda tidak bocor ketika jawaban panjang dipotong. History baru menyimpan teks tanpa penanda pengiriman; Markdown penjelasan tetap ada. Preview tautan dinonaktifkan pada kedua jalur.

Pemilihan blok bergantung pada model mengikuti kontrak output, bukan pencocokan nama module/kata kunci atau classifier tambahan. Tanpa penanda, jawaban tetap masuk jalur terformat. Parser menerima penanda pada baris tersendiri, mengabaikannya dalam fenced code penjelasan, dan memakai teks polos sampai akhir bila penutup blok hilang. Pembersihan subset markup bersifat best-effort, bukan parser Markdown umum. Telegram masih dapat mengenali URL/mention otomatis.

Tidak ada perubahan pada model, credential, published playbook, atau data lama. Pesan Telegram dan history existing tidak ditulis ulang.

Conversation Delivery Policy seperti batas kata, satu pesan satu tujuan, dan progressive disclosure sengaja belum digabung ke renderer. Lapisan tersebut akan dikembangkan terpisah.

## Batas MVP yang disengaja

- Text-only, belum mendukung dokumen, gambar, voice note, atau tool calling
- Satu konfigurasi AI provider untuk seluruh bot
- SQLite cocok untuk satu instance bot; jangan menjalankan beberapa replica
- Controlled concurrency dibatasi maksimal 16 dan default 4
- Knowledge sudah dipisahkan per company, tetapi belum per module/division/clearance
- Upload knowledge belum mendukung OCR, PDF scan, dan Word `.doc` lama
- Admin writable untuk Company, user, membership, Company Instruction, Knowledge, Module, serta module access; Activity menampilkan audit administratif read-only
- History dibatasi untuk konteks dan dipangkas menjadi 100 pesan per user-company-module

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
  telegram_renderer.py safe HTML + blok teks polos khusus naskah siap salin
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
