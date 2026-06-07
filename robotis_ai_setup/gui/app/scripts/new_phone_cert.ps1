# new_phone_cert.ps1 — Mint a self-signed cert for the phone-camera HTTPS receiver
#
# The phone-as-3rd-camera receiver (gui/app/phone_camera.py) serves HTTPS because
# mobile browsers refuse getUserMedia outside a secure context. This script makes
# the cert+key ONCE using Windows' built-in New-SelfSignedCertificate (NO admin —
# it writes to the per-user Cert:\CurrentUser\My store) and exports them as PEM
# files that Python's ssl.SSLContext.load_cert_chain can read.
#
# Outputs (under -OutDir, default %LOCALAPPDATA%\EduBotics\phone-cert):
#   cert.pem  — the certificate (PEM, "CERTIFICATE")
#   key.pem   — the RSA private key (PEM, "RSA PRIVATE KEY", PKCS#1)
#
# Idempotent-ish: always (re)writes the two PEMs. The Python wrapper
# (phone_cert.py) only calls this when the PEMs are missing/unparseable, so a
# valid pair is reused across runs.
#
# Why PEM and not PFX: ssl.load_cert_chain wants separate cert + key PEM files.
# Why "RSA PRIVATE KEY" (PKCS#1): ExportRSAPrivateKey() exists on .NET Framework
# 4.7+ (Windows PowerShell 5.1) AND .NET 5+ (PowerShell 7), so one code path
# covers both shells. OpenSSL is NOT required.

param(
    [string]$OutDir = (Join-Path $env:LOCALAPPDATA 'EduBotics\phone-cert'),
    [string]$DnsName = 'edubotics-phone-cam'
)

$ErrorActionPreference = 'Stop'

function Write-Pem {
    param([string]$Path, [string]$Header, [byte[]]$Der)
    $b64 = [System.Convert]::ToBase64String($Der, 'InsertLineBreaks')
    $pem = "-----BEGIN $Header-----`n$b64`n-----END $Header-----`n"
    # ASCII, no BOM — PEM parsers (incl. Python ssl) reject a UTF-8 BOM.
    [System.IO.File]::WriteAllText($Path, $pem, (New-Object System.Text.ASCIIEncoding))
}

try {
    if (-not (Test-Path $OutDir)) {
        New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
    }

    # User-scope cert. -KeyExportPolicy Exportable so we can pull the private key
    # back out. 825 days keeps it under the common 825-day max-validity limit.
    $cert = New-SelfSignedCertificate `
        -Subject "CN=$DnsName" `
        -DnsName $DnsName `
        -CertStoreLocation 'Cert:\CurrentUser\My' `
        -KeyExportPolicy Exportable `
        -KeyAlgorithm RSA `
        -KeyLength 2048 `
        -NotAfter (Get-Date).AddDays(825) `
        -KeyUsage DigitalSignature, KeyEncipherment

    $certPath = Join-Path $OutDir 'cert.pem'
    $keyPath  = Join-Path $OutDir 'key.pem'

    # Certificate PEM from the raw DER.
    Write-Pem -Path $certPath -Header 'CERTIFICATE' -Der $cert.RawData

    # Private key PEM (PKCS#1). New-SelfSignedCertificate stores the key in CNG,
    # whose handle can refuse a direct ExportRSAPrivateKey() on Windows
    # PowerShell 5.1 even when minted -Exportable. The reliable cross-version
    # path is to round-trip through a temporary PFX (always allowed for an
    # Exportable key) and reload it with the Exportable flag, then pull the RSA
    # key out. No OpenSSL involved; ExportRSAPrivateKey() exists on .NET 4.7+.
    $pfxPath = Join-Path $OutDir ('_tmp_' + [System.Guid]::NewGuid().ToString('N') + '.pfx')
    $pfxPwd  = [System.Guid]::NewGuid().ToString('N')
    $securePwd = ConvertTo-SecureString -String $pfxPwd -AsPlainText -Force
    Export-PfxCertificate -Cert $cert -FilePath $pfxPath -Password $securePwd | Out-Null
    try {
        $flags = ([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable -bor `
                  [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet)
        $loaded = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 `
            -ArgumentList $pfxPath, $pfxPwd, $flags
        $rsa = [System.Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPrivateKey($loaded)
        if ($null -eq $rsa) {
            throw 'Privater Schlüssel konnte nicht aus dem Zertifikat gelesen werden.'
        }
        $keyDer = $rsa.ExportRSAPrivateKey()
        Write-Pem -Path $keyPath -Header 'RSA PRIVATE KEY' -Der $keyDer
    }
    finally {
        try { Remove-Item -Path $pfxPath -Force -ErrorAction SilentlyContinue } catch { }
    }

    # Best-effort: remove the cert from the user store so we don't accumulate a
    # new cert on every (rare) regeneration. The PEM files are the source of
    # truth for the server; the store entry is only the minting vehicle.
    try {
        Remove-Item -Path ("Cert:\CurrentUser\My\" + $cert.Thumbprint) -ErrorAction SilentlyContinue
    } catch { }

    Write-Output "OK: $certPath"
    Write-Output "OK: $keyPath"
    exit 0
}
catch {
    Write-Error ("Zertifikat-Erstellung fehlgeschlagen: " + $_.Exception.Message)
    exit 1
}
