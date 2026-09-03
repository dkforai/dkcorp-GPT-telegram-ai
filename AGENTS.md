# Project maintenance instructions

`docs/architecture.md` adalah living document dan source of truth untuk arsitektur serta konsep DK Corp Telegram AI.

Setiap perubahan pada fitur, alur, data model, konfigurasi, security boundary, integrasi, deployment, atau roadmap wajib memperbarui `docs/architecture.md` dalam perubahan yang sama. Perbarui versi, tanggal, status implementasi, keputusan arsitektur, dan changelog bila relevan.

Jangan membuat PDF arsitektur selama konsep masih aktif berubah. PDF dibuat dari Markdown hanya ketika DK menyatakan dokumen siap difinalkan.

## Efisiensi token

Untuk instruksi berikutnya, evaluasi peluang penghematan token AI yang relevan. Jelaskan metode, manfaat, risiko kualitas/privasi/kompleksitas, dan batas pengukurannya kepada DK. DK yang memutuskan; jangan mengganti model, mengurangi konteks, menambah cache jawaban lintas user, atau memakai layanan berbayar baru secara diam-diam. Eksekusi langsung hanya untuk metode yang sudah disetujui.
