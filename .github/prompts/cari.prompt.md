---
description: "Cari di mana sebuah fitur/simbol diimplementasikan tanpa membanjiri chat dengan isi file (hemat token)."
name: "cari"
agent: "agent"
argument-hint: "fitur atau nama simbol, mis. 'carry_over' atau 'tombol tambah pintu'"
---
Cari: `${input:query}`

Lakukan eksplorasi **murah**: delegasikan ke subagent `Explore`, atau `grep_search` dulu dan
`read_file` hanya pada rentang baris yang cocok. Jangan membaca `README.md` / `docs/*.md` /
`dashboard.html` utuh kecuali memang perlu.

Balas **maksimal 10 baris**: implementasi utama (`file:line`) · entry point (route / CLI / elemen UI) ·
tes yang mengunci perilakunya · catatan singkat kalau fiturnya sudah ada (agar tidak ditulis ulang).
