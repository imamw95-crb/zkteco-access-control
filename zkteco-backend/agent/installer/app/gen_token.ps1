<#
.SYNOPSIS
    Buat token acak yang kuat untuk agen push ZKTeco.

.DESCRIPTION
    Dipakai installer saat operator mengosongkan kolom token, dan bisa dipakai
    manual untuk memutar (rotate) token:

        powershell -ExecutionPolicy Bypass -File gen_token.ps1
        # 52okq0m3n8v1z9w7y6x5u4t3s2r1q0p9771ab

    Memakai RandomNumberGenerator (CSPRNG), bukan Get-Random: token ini setara
    kunci pintu - siapa pun yang memegangnya bisa menulis data ke panel dan
    membuka pintu lewat agen. Alfabet sengaja hanya [0-9a-z] supaya token aman
    ditempel ke command line, berkas .ini, dan HTTP header tanpa escaping.

    CATATAN: memutar token berarti backend harus diganti PUSH_AGENT_TOKEN-nya
    juga, kalau tidak setiap push akan berakhir 401.
#>
[CmdletBinding()]
param(
    # Tulis token ke berkas ini (tanpa newline). Kosongkan untuk cetak ke stdout.
    [string]$OutFile,

    # Jumlah byte acak; 30 byte -> 30 karakter alfabet 36 simbol (~155 bit).
    [int]$Bytes = 30
)

$ErrorActionPreference = 'Stop'

if ($Bytes -lt 16) {
    Write-Error 'Bytes minimal 16 supaya token tidak bisa ditebak.'
    exit 1
}

$alphabet = '0123456789abcdefghijklmnopqrstuvwxyz'
$raw = New-Object byte[] $Bytes
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $rng.GetBytes($raw)
}
finally {
    $rng.Dispose()
}

$builder = New-Object System.Text.StringBuilder
foreach ($b in $raw) {
    [void]$builder.Append($alphabet[$b % $alphabet.Length])
}
$token = $builder.ToString()

if ($OutFile) {
    Set-Content -LiteralPath $OutFile -Value $token -Encoding Ascii -NoNewline
}
else {
    Write-Output $token
}
exit 0
